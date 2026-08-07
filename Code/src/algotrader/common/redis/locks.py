"""Distributed locks with a mandatory TTL.

**The signature is the story.** ``ttl_ms`` is a required positional argument
with no default, and that is the single most important line in this module:

    A lock without a TTL survives the death of the process holding it.

If the execution service is killed while holding ``lock:slot:3``, a lock with no
expiry deadlocks slot allocation for the rest of the trading session — the
system stops opening positions and nothing raises. Making the argument mandatory
means that failure cannot be introduced by omission, only by deliberately
passing a bad value.

**Release is compare-and-delete, via Lua.** The naive `if get() == mine:
delete()` is a race: between the check and the delete, the lock can expire and
be acquired by another process, and you then delete *their* lock. The Lua script
makes the compare and the delete atomic. This is not hypothetical under a TTL
short enough to be useful.

These locks are the *fast path*, not the guarantee. The partial unique indexes
``uq_open_slot`` and ``uq_open_symbol`` are the guarantee (see
``common/db/models.py``). A lock can be lost to a network partition or an
expiry; an index cannot.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

import redis.asyncio as aioredis

log = logging.getLogger(__name__)

#: Compare-and-delete. Returns 1 if we held it and released it, 0 otherwise.
_RELEASE_LUA: Final = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""

#: Extend only if still ours. Same race as release, same fix.
_EXTEND_LUA: Final = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2])
else
    return 0
end
"""

#: A lock held longer than this is a bug, not a slow operation. Slot allocation
#: takes milliseconds; the session itself is only ~6 hours.
MAX_TTL_MS: Final = 300_000


class LockError(RuntimeError):
    """Raised when a lock operation is used incorrectly."""


async def acquire_lock(
    client: aioredis.Redis,
    key: str,
    holder: str,
    ttl_ms: int,
) -> bool:
    """Try once to take ``key`` for ``holder``. No blocking, no retry.

    ``ttl_ms`` is REQUIRED and has no default — see the module docstring.

    Non-blocking is deliberate: every caller in this system has a better answer
    than waiting. If the slot lock is held, the slot is taken and the signal
    should be rejected now with a reason, not queued behind a lock while the
    price moves.

    Returns True if acquired.
    """
    if ttl_ms <= 0:
        raise LockError(
            f"ttl_ms must be positive, got {ttl_ms}. A non-expiring lock deadlocks "
            f"slot allocation for the session if the holder dies."
        )
    if ttl_ms > MAX_TTL_MS:
        raise LockError(
            f"ttl_ms {ttl_ms} exceeds the {MAX_TTL_MS} ms ceiling. Nothing in this "
            f"system legitimately holds a lock that long; this is almost certainly a "
            f"unit mistake (seconds passed where milliseconds were expected)."
        )
    if not holder:
        raise LockError("holder must be a non-empty identifier — it is what makes release safe")

    return bool(await client.set(key, holder, nx=True, px=ttl_ms))


async def release_lock(client: aioredis.Redis, key: str, holder: str) -> bool:
    """Release ``key`` only if ``holder`` still owns it. Atomic.

    Returns False when the lock had already expired and been taken by someone
    else — which is information, not an error: it means the holder overran its
    TTL and must assume it no longer has exclusivity.
    """
    result = await client.eval(_RELEASE_LUA, 1, key, holder)
    return bool(result)


async def extend_lock(client: aioredis.Redis, key: str, holder: str, ttl_ms: int) -> bool:
    """Push the expiry out, only if still ours. Returns False if ownership was lost."""
    if ttl_ms <= 0 or ttl_ms > MAX_TTL_MS:
        raise LockError(f"ttl_ms must be in 1..{MAX_TTL_MS}, got {ttl_ms}")
    return bool(await client.eval(_EXTEND_LUA, 1, key, holder, ttl_ms))


@asynccontextmanager
async def lock(
    client: aioredis.Redis,
    key: str,
    ttl_ms: int,
    *,
    holder: str | None = None,
) -> AsyncIterator[bool]:
    """Context manager form. Yields whether the lock was acquired.

    It yields a bool rather than raising on contention because contention is a
    normal, expected outcome here — two signals firing on the same cycle for the
    same slot is ordinary, not exceptional::

        async with lock(client, keys.slot_lock(3), ttl_ms=60_000) as got:
            if not got:
                return reject(RejectReason.NO_SLOT_AVAILABLE)
            ...

    Release is best-effort and never masks an exception raised inside the block:
    if the body failed, that error is what the caller needs to see, not a
    secondary failure from releasing a lock that had already expired.
    """
    owner = holder or uuid.uuid4().hex
    acquired = await acquire_lock(client, key, owner, ttl_ms)
    try:
        yield acquired
    finally:
        if acquired:
            try:
                await release_lock(client, key, owner)
            except Exception:
                # Swallowed on purpose. If the body raised, THAT is the error the
                # caller needs; masking it with a secondary failure from releasing
                # an already-expired lock would hide the real fault. The lock has
                # a TTL, so the worst case is it clears on its own.
                log.warning("could not release lock %s; it will expire", key, exc_info=True)
