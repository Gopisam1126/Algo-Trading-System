"""Publishing indicator state for other processes (E15-S14).

The indicator engine runs inside the strategy side and nowhere else, and live
bars are never written to Postgres during the session (``LOW_LEVEL_ARCHITECTURE.md
§4.1``: L1 streams, flushed to L2 after it). So the one place another process
can read a live ATR is the hot state this module writes:
``state:indicator:{symbol}:{timeframe}``, an :class:`IndicatorSnapshot` as JSON.

The execution service sizes every position from the ``atr_14`` it reads here.
That is why the write is kept apart from :mod:`algotrader.indicators.engine`,
which stays pure, and why the caller's obligation is stated here rather than
left to be discovered:

**Publish before you emit.** The strategy loop must publish a timeframe's state
after folding in a bar and BEFORE emitting any signal evaluated on that bar.
The reader refuses a state more than one bar old; a signal that overtakes its
own bar's state is refused at execution for exactly that reason, which is safe
and is also a lost trade.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from algotrader.common.redis import keys, state

if TYPE_CHECKING:
    import redis.asyncio as aioredis

    from algotrader.common.models.market import IndicatorSnapshot
    from algotrader.indicators.engine import IndicatorSet

#: Session-scoped state carries a TTL (``state.set_state``): a crashed session
#: must not leave a value that outlives the trading day. The reader's own age
#: bound is the control; this only keeps the keyspace from carrying yesterday.
INDICATOR_STATE_TTL_SECONDS = 12 * 3600


async def publish_indicator_state(
    client: aioredis.Redis,
    indicator_set: IndicatorSet,
    *,
    ttl_seconds: int = INDICATOR_STATE_TTL_SECONDS,
) -> IndicatorSnapshot | None:
    """Write ``indicator_set``'s published state. Returns what was written.

    Writes nothing, and returns ``None``, before the set has seen a bar: there
    is no as-of time to give the reader, and an existing key is left to age
    out under the reader's bound rather than be replaced by a value it could
    not judge.
    """
    snapshot = indicator_set.published()
    if snapshot is None:
        return None
    await state.set_state(
        client,
        keys.indicator_state(snapshot.symbol, snapshot.timeframe),
        snapshot,
        ttl_seconds=ttl_seconds,
    )
    return snapshot


__all__ = ["INDICATOR_STATE_TTL_SECONDS", "publish_indicator_state"]
