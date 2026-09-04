"""Margin and timing checks 13–14 (E14-S06).

The last two gates, and the only ones that ask about the world outside this
process: can the account actually afford a position, and is there time left for
one to work?

## The design record

**Decision.** Two checks — ``margin_sufficient`` then ``time_to_squareoff`` —
in the order ``LOW_LEVEL_ARCHITECTURE.md §5.7`` lists them, running last in the
pipeline.

**Alternative rejected**, on the order: timing first. It is the cheaper
question — it needs no broker at all — and this pipeline otherwise runs
cheapest-and-most-absolute first. The spec's order is kept because the two only
disagree when *both* fail, which is a narrow case, and following the written
order costs nothing while diverging from it silently would.

**The failure it prevents.** Two different ones:

1. An order the account cannot carry. Kite rejects it, or worse, accepts it and
   leaves the account over-leveraged into the next fill.
2. A position entered with no time to work. Enter at 15:04 against a 15:05 CAS
   deadline and the trade is closed a minute later at whatever the price
   happens to be — a coin flip that pays brokerage both ways.

**What would make this wrong.** If the system ever held overnight, check 14
would be measuring a deadline that no longer applies. Every position here has a
time exit by invariant 5, so the deadline is always real — but that is the
assumption the check rests on, not a property it verifies.

## Staleness is deliberately NOT re-checked here

``margin_for_sizing()`` (E02-S07) **raises** ``StaleMarginError`` when the
snapshot is older than its TTL, which closes the time-of-check/time-of-use gap:
available margin falls after every fill, so a snapshot taken before the
previous entry can authorise a position the account cannot carry.

By the time a number reaches :attr:`RiskContext.available_margin` it is
therefore **fresh or absent**. This check rejects absent. Re-implementing the
TTL here would give two places to disagree about how old is too old, and the
one that is wrong would be the one nobody was looking at.

## What check 13 can and cannot do

It runs **before** sizing, so there is no quantity yet and it cannot verify
margin for "the intended position". What it *can* do is refuse when margin is
unknown, or when it will not cover a **single share** — the cheapest position
that exists.

Unlike the sector and net-directional caps (E14-S10), the proportional limit is
not missing: §5.7's sizing formula already clamps on
``available_margin / margin_per_share``. So this check is a pre-filter and the
binding constraint arrives with E14-S07, rather than being absent entirely.
"""

from __future__ import annotations

from algotrader.common.calendar import IST
from algotrader.common.enums import RejectReason
from algotrader.common.models.trading import Recommendation
from algotrader.execution.risk.context import RiskContext
from algotrader.execution.risk.framework import CheckOutcome, RiskCheck

# ---------------------------------------------------------------------------
# 13. Margin sufficient
# ---------------------------------------------------------------------------


def build_margin_check() -> RiskCheck:
    """Reject when the account cannot afford even one share, or when we do not
    know whether it can.

    No configured threshold: the bar is one share, which is not a policy
    choice. Anything above it is sizing's problem.
    """

    def check(rec: Recommendation, ctx: RiskContext) -> CheckOutcome:
        # Both raise RiskContextError when absent, which the framework turns
        # into a RISK_ENGINE_FAULT rejection. Deliberately NOT caught here: "we
        # could not reach the broker" is a fault, not a business rejection, and
        # conflating the two is what SIT-001 cost.
        margin = ctx.require(ctx.available_margin, f"live broker margin for {rec.symbol}")
        per_share = ctx.require(
            ctx.margin_per_share, f"per-share margin requirement for {rec.symbol}"
        )

        if margin < per_share:
            return CheckOutcome.fail(
                RejectReason.INSUFFICIENT_MARGIN,
                f"available margin {margin:.2f} will not cover one share of "
                f"{rec.symbol} at {per_share:.2f} per share. Sizing cannot "
                f"rescue an account that cannot afford a single unit.",
            )
        return CheckOutcome.ok()

    return RiskCheck(
        id="margin_sufficient",
        fn=check,
        description="live broker margin covers at least one share",
    )


# ---------------------------------------------------------------------------
# 14. Time to square-off
# ---------------------------------------------------------------------------


def build_squareoff_runway_check(min_minutes: int) -> RiskCheck:
    """Reject when too little of the session remains for a trade to work.

    Distinct from the no-trade window, which asks whether the *session* is in a
    blackout. This asks whether *this stock* has runway: a CAS name's deadline
    is 15:10 minus the exit buffer, so at 14:59 — inside the tradable window —
    six minutes remain.
    """
    if min_minutes < 1:
        raise ValueError(
            f"min_minutes_to_squareoff is {min_minutes}; below 1 the check "
            f"would admit a trade with no time at all to work"
        )

    def check(rec: Recommendation, ctx: RiskContext) -> CheckOutcome:
        remaining = ctx.minutes_to_squareoff
        if remaining < min_minutes:
            deadline_ist = ctx.squareoff_deadline.astimezone(IST)
            if remaining < 0:
                shape = (
                    f"the square-off deadline for {rec.symbol} passed "
                    f"{abs(remaining):.0f} minute(s) ago"
                )
            else:
                shape = (
                    f"only {remaining:.0f} minute(s) remain before "
                    f"{rec.symbol}'s square-off deadline"
                )
            return CheckOutcome.fail(
                RejectReason.TOO_CLOSE_TO_SQUAREOFF,
                f"{shape} at {deadline_ist:%H:%M} IST, against a "
                f"{min_minutes}-minute minimum. A position with no runway is "
                f"closed at whatever the price happens to be.",
            )
        return CheckOutcome.ok()

    return RiskCheck(
        id="time_to_squareoff",
        fn=check,
        description=f"at least {min_minutes} minutes before this stock's deadline",
    )


# ---------------------------------------------------------------------------
# The ordered set
# ---------------------------------------------------------------------------


def build_margin_timing_checks(*, min_minutes_to_squareoff: int) -> tuple[RiskCheck, ...]:
    """The last two, in ``LOW_LEVEL_ARCHITECTURE.md §5.7``'s order.

    Keyword-only for consistency with the other factories, though there is only
    one argument today — the next person to add a second should not have to
    think about whether the first was positional.
    """
    return (
        build_margin_check(),
        build_squareoff_runway_check(min_minutes_to_squareoff),
    )


#: The ids, in order, for tests and anything asserting the pipeline shape.
MARGIN_TIMING_ORDER = ("margin_sufficient", "time_to_squareoff")


#: A convenience for the one thing that reads the whole pipeline: the fourteen
#: check ids in the order §5.7 declares them. Derived from the group constants
#: rather than retyped, so it cannot drift from what the factories build.
def all_check_ids() -> tuple[str, ...]:
    from algotrader.execution.risk.checks.eligibility import ELIGIBILITY_ORDER
    from algotrader.execution.risk.checks.exposure import EXPOSURE_ORDER
    from algotrader.execution.risk.checks.loss import LOSS_ORDER
    from algotrader.execution.risk.checks.preconditions import PRECONDITION_ORDER

    return (
        *PRECONDITION_ORDER,
        *ELIGIBILITY_ORDER,
        *EXPOSURE_ORDER,
        *LOSS_ORDER,
        *MARGIN_TIMING_ORDER,
    )
