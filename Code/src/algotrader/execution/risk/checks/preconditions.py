"""Pre-condition checks 1–4 (E14-S02).

The first four gates of the fourteen. They share a property that sets them
apart from everything after: **none of them look at the symbol.** They ask
whether the system should be trading *at all* right now — so they are cheapest
and most absolute, and therefore first.

## The design record

**Decision.** Four checks, ordered kill switch → health gate → trading window →
no-trade window, each built by a factory that closes over the calendar or the
config it needs. State comes from :class:`RiskContext`; dependencies come from
the closure.

**Alternative rejected.** Putting the calendar and the window list on
``RiskContext``. That would have made the context carry *dependencies* rather
than *state*, and every caller assembling one would need a calendar even for
checks that do not use it. Closing over them mirrors how ``RiskEngine.sizer``
is injected and keeps the context what its docstring says it is.

**The failure it prevents.** A recommendation acted on while the kill switch is
engaged, a component is dead, the market is shut, or the session is inside a
declared blackout. Each of those is a "do not trade at all" condition, and
discovering one *after* sizing has run is wasted work at best and an order at
worst.

**What would make this wrong.** If a pre-condition ever needed the symbol — say
a per-symbol trading halt — it would belong in S03 (symbol eligibility) rather
than here, and the "none of these look at the symbol" property that justifies
their position would no longer hold.

## The spec conflict, and how it resolved

``LOW_LEVEL_ARCHITECTURE.md §5.7`` lists these as two hardcoded times:
``check_within_trading_window`` (*"no entries after 15:00"*) and
``check_not_in_no_trade_window`` (*"09:15–09:20 opening noise"*).

Config already models both as one mechanism —
``config.execution.no_trade_windows`` is ``[(09:15, 09:20), (15:00, 15:30)]``
in ``system.yaml`` — so implementing the spec literally would hardcode 15:00
*and* duplicate a window that is already configured.

They split cleanly on **source** instead of on time:

* :func:`build_trading_window_check` asks the **calendar** whether the market
  is open at all — weekend, holiday, pre-open, post-close — and yields
  ``OUTSIDE_TRADING_WINDOW``.
* :func:`build_no_trade_window_check` asks **config** whether ``now`` falls in
  a declared blackout, and yields ``NO_TRADE_WINDOW``.

Both reason codes already exist in :class:`RejectReason`, which is the design
saying it wants them told apart. It also keeps
``signals_rejected_total{check}`` able to distinguish "blocked at the open"
from "blocked near the close" — operationally very different signals that a
single merged check would flatten.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from algotrader.common.calendar import IST, MarketCalendar
from algotrader.common.enums import RejectReason
from algotrader.common.models.trading import Recommendation
from algotrader.execution.risk.context import RiskContext
from algotrader.execution.risk.framework import CheckOutcome, RiskCheck


#: Windows are half-open, ``start <= now < end``. A trade at exactly 09:20:00 is
#: allowed and one at 09:19:59 is not, which is the reading a human gives
#: "09:15 to 09:20". Closed-at-both-ends would make two adjacent windows
#: overlap on their shared boundary and reject a second that neither meant to.
def _in_window(now: dt.time, start: dt.time, end: dt.time) -> bool:
    """Half-open containment, with wrap-around refused rather than guessed.

    A window whose end precedes its start (22:00–02:00) would be a
    cross-midnight blackout. NSE does not have one, and silently supporting it
    would mean a typo like ``["15:00", "09:20"]`` blocks the entire session
    instead of failing. So this returns False and
    :func:`validate_no_trade_windows` refuses the configuration outright.
    """
    if start >= end:
        return False
    return start <= now < end


#: How many unhealthy services a rejection names before summarising.
#:
#: The count is always exact; only the list is truncated. Unbounded, this
#: produced a 48,957-character detail from 5000 services — and that detail goes
#: into the audit payload and a log line, once per rejected candidate per bar.
#: The system has around a dozen services, so the cap only fires when something
#: has gone very wrong, which is exactly when flooding the log helps least.
MAX_SERVICES_NAMED = 12


def validate_no_trade_windows(windows: Sequence[tuple[dt.time, dt.time]]) -> None:
    """Refuse an incoherent blackout list at WIRING time.

    A window with ``start >= end`` matches nothing, so a typo produces a
    blackout that silently never fires — the system trades through a period
    someone deliberately fenced off, and nothing in the logs says so. Checking
    here means the mistake surfaces when the engine is assembled rather than
    on the one morning the window mattered.
    """
    for start, end in windows:
        if start >= end:
            raise ValueError(
                f"no_trade_window {start}-{end} has start >= end, so it can never "
                f"match. NSE has no cross-midnight blackout; this is a typo, and "
                f"left alone it would silently disable the window."
            )


# ---------------------------------------------------------------------------
# 1. Kill switch (C8)
# ---------------------------------------------------------------------------


def check_kill_switch(rec: Recommendation, ctx: RiskContext) -> CheckOutcome:
    """The most absolute condition there is, so it runs first.

    This only READS the flag. Arming it, persisting it and auto-demotion are
    E14-S09; keeping them apart means the check has no way to clear the switch
    it is testing.
    """
    if ctx.kill_switch_active:
        return CheckOutcome.fail(
            RejectReason.KILL_SWITCH_ACTIVE,
            f"the kill switch is engaged; no new risk may be taken "
            f"({rec.symbol} {rec.direction.value} not evaluated further)",
        )
    return CheckOutcome.ok()


# ---------------------------------------------------------------------------
# 2. Health gate
# ---------------------------------------------------------------------------


def check_health_gate(rec: Recommendation, ctx: RiskContext) -> CheckOutcome:
    """Any unhealthy service blocks new risk. Invariant 6: component down → no
    new risk.

    Deliberately not "a quorum" or "the critical ones". Naming which services
    may be down while trading continues is a judgement that would have to be
    right for every combination, and the safe default — refuse while anything
    is unhealthy — is both simpler and correct. The detail names them so an
    operator does not have to go looking.
    """
    if ctx.unhealthy_services:
        total = len(ctx.unhealthy_services)
        shown = sorted(ctx.unhealthy_services)[:MAX_SERVICES_NAMED]
        names = ", ".join(shown)
        if total > MAX_SERVICES_NAMED:
            names += f", and {total - MAX_SERVICES_NAMED} more"
        return CheckOutcome.fail(
            RejectReason.HEALTH_GATE_FAILED,
            f"{total} service(s) unhealthy: {names}. No new risk while any component is down.",
        )
    return CheckOutcome.ok()


# ---------------------------------------------------------------------------
# 3. Trading window — the CALENDAR's answer
# ---------------------------------------------------------------------------


def build_trading_window_check(calendar: MarketCalendar) -> RiskCheck:
    """Is the market open at all right now?

    Closes over the calendar rather than reading one from the context: the
    calendar is a dependency, not session state, and a check that had to
    construct one would be doing setup in an order path.
    """

    def check(rec: Recommendation, ctx: RiskContext) -> CheckOutcome:
        # `is_market_open` raises HolidayDataError for a year the loaded list
        # does not cover. It is deliberately NOT caught here: the framework
        # turns a raising check into a rejection, which is the fail-closed
        # answer. Catching it to return a tidy outcome would just duplicate
        # that, and swallowing it would be the bug (AC7).
        if not calendar.is_market_open(ctx.now):
            ist = ctx.now.astimezone(IST)
            return CheckOutcome.fail(
                RejectReason.OUTSIDE_TRADING_WINDOW,
                f"the market is not in continuous trading at "
                f"{ist:%Y-%m-%d %H:%M} IST (session is 09:15-15:30 on a "
                f"trading day)",
            )
        return CheckOutcome.ok()

    return RiskCheck(
        id="trading_window",
        fn=check,
        description="market is open — not a weekend, holiday, pre-open or post-close",
    )


# ---------------------------------------------------------------------------
# 4. No-trade window — CONFIG's answer
# ---------------------------------------------------------------------------


def build_no_trade_window_check(
    windows: Sequence[tuple[dt.time, dt.time]],
) -> RiskCheck:
    """Is ``now`` inside a configured blackout?

    Validates the list at construction. A window that can never match is a
    configuration error, and finding it here beats finding it by noticing the
    system traded through a period someone fenced off.
    """
    validate_no_trade_windows(windows)
    frozen = tuple(windows)

    def check(rec: Recommendation, ctx: RiskContext) -> CheckOutcome:
        now = ctx.now.astimezone(IST).time()
        for start, end in frozen:
            if _in_window(now, start, end):
                return CheckOutcome.fail(
                    RejectReason.NO_TRADE_WINDOW,
                    f"{now:%H:%M:%S} IST is inside the configured no-trade "
                    f"window {start:%H:%M}-{end:%H:%M}",
                )
        return CheckOutcome.ok()

    return RiskCheck(
        id="no_trade_window",
        fn=check,
        description="not inside a configured blackout (opening noise, near close)",
    )


# ---------------------------------------------------------------------------
# The ordered set
# ---------------------------------------------------------------------------

KILL_SWITCH_CHECK = RiskCheck(
    id="kill_switch",
    fn=check_kill_switch,
    description="C8 — the kill switch is not engaged",
)

HEALTH_GATE_CHECK = RiskCheck(
    id="health_gate",
    fn=check_health_gate,
    description="every service is heartbeating",
)


def build_precondition_checks(
    calendar: MarketCalendar,
    no_trade_windows: Sequence[tuple[dt.time, dt.time]],
) -> tuple[RiskCheck, ...]:
    """The four, in the order they must run.

    **The order is the design, not a preference.** Fail-fast means the first
    refusal is the one an operator sees, so the sequence runs cheapest and most
    absolute first: there is no point asking the calendar what day it is when
    the kill switch has already settled the matter, and no point reading a
    window list when a service is down.

    It also puts the two checks that need no dependencies ahead of the two that
    do, so a partially-wired engine still refuses correctly.
    """
    return (
        KILL_SWITCH_CHECK,
        HEALTH_GATE_CHECK,
        build_trading_window_check(calendar),
        build_no_trade_window_check(no_trade_windows),
    )


#: The ids, in order, for tests and the health panel. Stated separately so a
#: reordering shows up as a diff on this line rather than only inside a
#: function body.
PRECONDITION_ORDER = ("kill_switch", "health_gate", "trading_window", "no_trade_window")
