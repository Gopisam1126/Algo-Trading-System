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
import re
from typing import Final

from algotrader.common.enums import Timeframe


class UnsafeKeyError(ValueError):
    """Raised when a key component would corrupt the keyspace."""


#: What may appear in a key component. NSE tickers are uppercase alphanumerics
#: with ``-`` and ``&`` (M&M, BAJAJ-AUTO); service names add lowercase and ``_``.
#: Everything else is refused.
#:
#: **Used with ``fullmatch``, and deliberately unanchored.** The first version
#: was ``re.compile(r"^...$").match(...)``, which is a bypass: in Python ``$``
#: also matches immediately *before* a trailing newline, so ``"svc\n"`` passed
#: validation and a newline reached the keyspace. ``fullmatch`` has no such
#: exception — it requires the entire string, newline included, to match.
#: (``\Z`` would work too; ``fullmatch`` is harder to get wrong later.)
_SAFE_COMPONENT: Final = re.compile(r"[A-Za-z0-9_\-&.]{1,64}")

#: How much of a rejected value to echo back. An unbounded echo turns a hostile
#: 200 KB ticker into 200 KB of log, which is a denial-of-service on the log
#: pipeline and a log-injection vector in its own right.
_ECHO_LIMIT: Final = 64


def _safe(component: str, what: str) -> str:
    """Validate a value before it becomes part of a key.

    **This is a security control.** Instrument symbols arrive from the broker's
    daily dump — external data this system does not author — and flow straight
    into Redis key names. Without validation:

    - ``:`` shifts field boundaries. ``indicator_state("A:5m:x", "5m")`` yields
      ``state:indicator:A:5m:x:5m``, which is indistinguishable from a key for
      some other symbol/timeframe pair. Two logical states then share one slot,
      and one silently overwrites the other.
    - ``*`` and ``?`` are glob metacharacters. A symbol of ``*`` produces
      ``state:quote:*``, which matches every quote key the moment anything
      uses it in a ``SCAN``/``KEYS`` pattern.
    - ``\\n`` and ``\\x00`` are accepted by Redis but break the moment the same
      value reaches PostgreSQL, which rejects null bytes in text.

    None of these raise on their own. They corrupt state quietly, which is the
    worst failure mode for a component holding positions and a kill switch.
    """
    if not _SAFE_COMPONENT.fullmatch(component):
        shown = component[:_ECHO_LIMIT]
        suffix = f"... ({len(component)} chars)" if len(component) > _ECHO_LIMIT else ""
        raise UnsafeKeyError(
            f"{what} {shown!r}{suffix} is not safe to use in a Redis key. Allowed: "
            f"letters, digits, and _ - & . (1-64 chars). A ':' would shift key "
            f"boundaries and collide with another key; '*' or '?' would act as "
            f"glob wildcards in any SCAN."
        )
    return component


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
    return f"{STATE}:indicator:{_safe(symbol, 'symbol')}:{_tf(timeframe)}"


def current_bar(symbol: str, timeframe: Timeframe | str) -> str:
    """HASH — the bar currently being built. Not yet persisted; not yet final."""
    return f"{STATE}:bar:current:{_safe(symbol, 'symbol')}:{_tf(timeframe)}"


def quote(symbol: str) -> str:
    """HASH — latest quote. 60 s TTL: an expired quote IS the staleness signal."""
    return f"{STATE}:quote:{_safe(symbol, 'symbol')}"


def position_state(symbol: str) -> str:
    """HASH — live position. No TTL; deleted on close."""
    return f"{STATE}:position:{_safe(symbol, 'symbol')}"


def slots() -> str:
    """HASH — slot index to symbol. The fast path; `uq_open_slot` is the guarantee."""
    return f"{STATE}:slots"


# --- plan -------------------------------------------------------------------


def plan(trade_date: dt.date | str) -> str:
    return f"{PLAN}:{_day(trade_date)}"


def plan_candidate(trade_date: dt.date | str, symbol: str) -> str:
    return f"{PLAN}:candidate:{_day(trade_date)}:{_safe(symbol, 'symbol')}"


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
    return f"{CONTROL}:health:{_safe(service, 'service name')}"


# --- locks ------------------------------------------------------------------


def slot_lock(slot_index: int) -> str:
    if not isinstance(slot_index, int) or isinstance(slot_index, bool) or slot_index < 0:
        raise UnsafeKeyError(f"slot_index must be a non-negative int, got {slot_index!r}")
    return f"{LOCK}:slot:{slot_index}"


def symbol_lock(symbol: str) -> str:
    return f"{LOCK}:symbol:{_safe(symbol, 'symbol')}"


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
    short = stream_name.removeprefix(f"{STREAM}:").removeprefix("dlq:")
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
