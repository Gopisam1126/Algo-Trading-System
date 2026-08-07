"""Every Redis key the system uses, built by a function.

**No key string literal may appear anywhere else in the codebase.** That is the
whole point of this module, and it is enforced by a test that enumerates the
builders. Typo'd key names do not raise — they silently read `None`, which in a
trading system reads as "no position", "no plan", "kill switch not set". A
missing key and a mistyped key are indistinguishable at the call site, so the
only defence is to make mistyping impossible.

Key layout follows `EPIC01_TECHNICAL_SPEC.md §9`. The prefix carries the
lifecycle, which is what makes bulk operations and debugging tractable:

``state:``    live trading state, session-scoped
``plan:``     the day's plan, 48 h
``context:``  macro/market context, 90 min
``control:``  operator switches — kill switch, mode, health
``lock:``     mutual exclusion, always TTL'd
``ratelimit:`` token buckets
``timer:``    scheduled work (ZSET)
``stream:``   event streams (E01-S04)
"""

from __future__ import annotations

import datetime as dt
from typing import Final

from algotrader.common.enums import Timeframe

# --- namespaces -------------------------------------------------------------

STATE: Final = "state"
PLAN: Final = "plan"
CONTEXT: Final = "context"
CONTROL: Final = "control"
LOCK: Final = "lock"
RATELIMIT: Final = "ratelimit"
TIMER: Final = "timer"
STREAM: Final = "stream"


def _tf(timeframe: Timeframe | str) -> str:
    return timeframe.value if isinstance(timeframe, Timeframe) else str(timeframe)


def _day(day: dt.date | str) -> str:
    """Dates in keys are always ISO, and always the IST trade date.

    Never ``date.today()`` at a call site — that resolves against the server's
    timezone, which is UTC in every deployment. Between 00:00 and 05:30 IST the
    UTC date is the previous day, and a plan written under one key would be read
    under another. Pass the trade date explicitly, from ``MarketCalendar``.
    """
    return day.isoformat() if isinstance(day, dt.date) else str(day)


# --- state ------------------------------------------------------------------


def indicator_state(symbol: str, timeframe: Timeframe | str) -> str:
    """HASH — the incremental indicator engine's state for one symbol/timeframe."""
    return f"{STATE}:indicator:{symbol}:{_tf(timeframe)}"


def current_bar(symbol: str, timeframe: Timeframe | str) -> str:
    """HASH — the bar currently being built. Not yet persisted; not yet final."""
    return f"{STATE}:bar:current:{symbol}:{_tf(timeframe)}"


def quote(symbol: str) -> str:
    """HASH — latest quote. 60 s TTL: an expired quote IS the staleness signal."""
    return f"{STATE}:quote:{symbol}"


def position_state(symbol: str) -> str:
    """HASH — live position. No TTL; deleted on close."""
    return f"{STATE}:position:{symbol}"


def slots() -> str:
    """HASH — slot index to symbol. The fast path; `uq_open_slot` is the guarantee."""
    return f"{STATE}:slots"


# --- plan -------------------------------------------------------------------


def plan(trade_date: dt.date | str) -> str:
    return f"{PLAN}:{_day(trade_date)}"


def plan_candidate(trade_date: dt.date | str, symbol: str) -> str:
    return f"{PLAN}:candidate:{_day(trade_date)}:{symbol}"


# --- context ----------------------------------------------------------------


def market_context() -> str:
    """STRING — macro regime snapshot.

    TTL is 90 minutes against a 20-minute refresh, deliberately. A macro-service
    outage then degrades gracefully: consumers keep seeing the last snapshot with
    an explicit ``as_of`` and can decide for themselves, rather than the key
    vanishing and every consumer needing to handle absence as a separate case.
    """
    return f"{CONTEXT}:market"


# --- control ----------------------------------------------------------------


def kill_switch() -> str:
    """STRING — read by everything before it acts. Never TTL'd."""
    return f"{CONTROL}:killswitch"


def mode() -> str:
    return f"{CONTROL}:mode"


def interval() -> str:
    """STRING — the derived trading interval, set by the orchestrator."""
    return f"{CONTROL}:interval"


def health(service: str) -> str:
    """STRING — 30 s TTL.

    The expiry IS the liveness mechanism: no separate heartbeat-timeout logic
    exists or is needed. If the key is gone, the service is down.
    """
    return f"{CONTROL}:health:{service}"


# --- locks ------------------------------------------------------------------


def slot_lock(slot_index: int) -> str:
    return f"{LOCK}:slot:{slot_index}"


def symbol_lock(symbol: str) -> str:
    return f"{LOCK}:symbol:{symbol}"


# --- rate limiting / timers -------------------------------------------------


def order_rate_limit() -> str:
    """The broker order token bucket. Account-wide, so a single key."""
    return f"{RATELIMIT}:orders"


def squareoff_timer() -> str:
    """ZSET — score is the deadline epoch; member is the position id."""
    return f"{TIMER}:squareoff"


# --- streams (E01-S04) ------------------------------------------------------


def stream_ticks() -> str:
    return f"{STREAM}:ticks"


def stream_bars(timeframe: Timeframe | str) -> str:
    return f"{STREAM}:bars:{_tf(timeframe)}"


def stream_snapshots() -> str:
    return f"{STREAM}:snapshots"


def stream_signals() -> str:
    return f"{STREAM}:signals"


def stream_orders() -> str:
    return f"{STREAM}:orders"


def stream_audit() -> str:
    return f"{STREAM}:audit"


def stream_dlq(stream_name: str) -> str:
    """Dead letter for a stream. Takes the FULL stream key, not a bare name."""
    short = stream_name.removeprefix(f"{STREAM}:")
    return f"{STREAM}:dlq:{short}"


#: Every builder, for the test that asserts §9 is fully covered and that no key
#: literal exists elsewhere. Update this when adding a builder.
ALL_BUILDERS: Final[tuple[str, ...]] = (
    "indicator_state",
    "current_bar",
    "quote",
    "position_state",
    "slots",
    "plan",
    "plan_candidate",
    "market_context",
    "kill_switch",
    "mode",
    "interval",
    "health",
    "slot_lock",
    "symbol_lock",
    "order_rate_limit",
    "squareoff_timer",
    "stream_ticks",
    "stream_bars",
    "stream_snapshots",
    "stream_signals",
    "stream_orders",
    "stream_audit",
    "stream_dlq",
)
