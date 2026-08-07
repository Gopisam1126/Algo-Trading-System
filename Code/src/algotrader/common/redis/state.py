"""Typed state accessors — Pydantic in, Pydantic out.

**The contract: a value that fails validation is treated as absent.**

That is the whole design of this module, and it is a deliberate trade. When a
stored snapshot no longer parses — a schema changed, a write was truncated, a
key collided — the alternatives are to return it partially parsed or to raise.
Both are worse:

- Partially parsed trading state is the dangerous case. An ``IndicatorSnapshot``
  missing its ``ready`` flag reads as "not ready" in some code paths and as a
  usable snapshot in others. **Half-valid state is worse than none**, because
  the system keeps acting on it.
- Raising forces every read site to handle corruption, and they will not.

Returning ``None`` routes corruption into the same path as "no data yet", and
that path already fails closed: no indicators means no signal, no plan means no
trade. The corruption is logged loudly so it is not silent — but the *system's*
behaviour is safe by default.
"""

from __future__ import annotations

import logging
from typing import TypeVar

import redis.asyncio as aioredis
from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)


async def set_state(
    client: aioredis.Redis,
    key: str,
    model: BaseModel,
    *,
    ttl_seconds: int | None = None,
) -> None:
    """Store a model as JSON.

    ``model_dump_json()`` rather than ``dict`` + a JSON dump: it is the same
    serialisation the rest of the system uses, so ``Decimal`` and ``datetime``
    round-trip exactly. A float would not — and money is never float here.

    ``ttl_seconds=None`` means no expiry, which is correct only for keys whose
    lifetime is managed explicitly (positions, control flags). Session-scoped
    state should always carry a TTL so a crashed session cannot leave stale
    state that outlives the trading day.
    """
    payload = model.model_dump_json()
    if ttl_seconds is None:
        await client.set(key, payload)
    else:
        await client.set(key, payload, ex=ttl_seconds)


async def get_state(client: aioredis.Redis, key: str, model_type: type[M]) -> M | None:
    """Read and validate. Returns ``None`` if absent **or invalid**.

    See the module docstring for why invalid maps to absent rather than raising.
    """
    raw = await client.get(key)
    if raw is None:
        return None
    try:
        return model_type.model_validate_json(raw)
    except ValidationError:
        # Loud, because it means something upstream is writing garbage — but the
        # caller still sees "no data", which every caller already handles safely.
        log.error(
            "discarding unparseable state at %s (expected %s) — treating as absent",
            key,
            model_type.__name__,
            exc_info=True,
        )
        return None


async def get_many(client: aioredis.Redis, keys: list[str], model_type: type[M]) -> dict[str, M]:
    """Read many keys in one round trip. Absent and invalid entries are omitted.

    One ``MGET`` rather than a loop: the pre-market warm-up reads state for
    ~150 symbols across 3 timeframes, and 450 sequential round trips is the
    difference between comfortably inside the 45-minute window and outside it.
    """
    if not keys:
        return {}
    raws = await client.mget(keys)
    out: dict[str, M] = {}
    for key, raw in zip(keys, raws, strict=True):
        if raw is None:
            continue
        try:
            out[key] = model_type.model_validate_json(raw)
        except ValidationError:
            log.error("discarding unparseable state at %s — treating as absent", key)
    return out


async def delete_state(client: aioredis.Redis, *keys: str) -> int:
    """Delete keys. Returns how many existed."""
    if not keys:
        return 0
    return int(await client.delete(*keys))


async def exists(client: aioredis.Redis, key: str) -> bool:
    return bool(await client.exists(key))


# ---------------------------------------------------------------------------
# Scalar control flags
# ---------------------------------------------------------------------------


async def set_flag(
    client: aioredis.Redis, key: str, value: str, *, ttl_seconds: int | None = None
) -> None:
    """Set a plain string flag — mode, interval, health."""
    if ttl_seconds is None:
        await client.set(key, value)
    else:
        await client.set(key, value, ex=ttl_seconds)


async def get_flag(client: aioredis.Redis, key: str) -> str | None:
    raw = await client.get(key)
    return None if raw is None else str(raw)


async def is_kill_switch_active(client: aioredis.Redis, key: str) -> bool:
    """Whether trading is halted.

    **Fails closed.** If Redis is unreachable this returns ``True`` — the kill
    switch is treated as ENGAGED. Reading "not halted" from a failed lookup
    would let the system keep trading precisely when its coordination layer is
    broken, which is the exact circumstance in which it should stop.

    The asymmetry is intentional: a false halt costs a missed trade, a false
    all-clear costs an uncontrolled position.
    """
    try:
        return bool(await client.exists(key))
    except Exception:
        log.error("kill-switch lookup failed — treating as ENGAGED (fail closed)", exc_info=True)
        return True
