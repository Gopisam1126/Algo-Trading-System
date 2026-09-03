"""Loss limit checks 11–12 (E14-S05).

The circuit breakers. Where the exposure checks asked whether the book is too
concentrated, these ask whether the *day* has gone badly enough that the system
should stop opening new risk at all.

## The design record

**Decision.** Two checks — ``daily_loss`` and ``consecutive_loss`` — each
rejecting if **either** the live figure breaches its limit **or** a latch is
set. The latch is read here and written by E14-S09.

**Alternative rejected**, and it is the obvious one: a pure predicate over
``ctx.realised_pnl_today`` and ``ctx.consecutive_losses``. It reads correctly,
it is simpler, and it **un-halts itself**.

A daily loss limit trips precisely when losing positions are open — that is
what put the day underwater. Those positions then close. One closes at a
profit, ``realised_pnl_today`` rises back above the threshold, the predicate
passes, and the system resumes trading on a day a risk limit already stopped.
``consecutive_losses`` is worse: a single winning close resets the counter to
zero, so one good exit clears a halt that three losses caused.

``LOW_LEVEL_ARCHITECTURE.md §8.1`` forbids exactly this: *"HALTED is terminal
for the day and is only exited by explicit operator action. There is no
automatic un-halt — if a risk limit tripped, a human decides whether resuming
is appropriate."* A predicate over a mutable number cannot express "terminal".

**Why both halves, rather than only the latch.** The latch alone leaves a
window: the loss breaches, and until whatever watches P&L writes the flag, the
live figure is the only thing that knows. Reading both means the breach is
caught at the instant it happens *and* stays caught afterwards. Neither half is
sufficient; together they fail closed at both ends.

**The failure it prevents.** A bad day compounding into a worse one. The
specific shape is not "one big loss" — it is the system quietly resuming after
a limit has already fired, which is how a 3% day becomes a 9% day while every
individual trade passes every per-trade limit.

**What would make this wrong.** If the limits ever needed to consider
*unrealised* P&L. ``realised_pnl_today`` is realised by name and by intent, so
a day showing -1% realised and -6% mark-to-market passes both checks here.
Whether that is right is a real question and deliberately not decided here.

## What these checks do NOT stop

**Exits.** ``LOW_LEVEL_ARCHITECTURE.md`` §1136 and §1373 both scope the breach
to *new entries*, with the operator deciding on existing positions. That is
structural rather than something these checks enforce: ``RiskEngine.evaluate``
only ever receives a :class:`Recommendation`, which is an entry candidate. It
is stated because a loss limit that also blocked square-off would strand
losing positions overnight — turning an intraday loss into an
unbounded one.
"""

from __future__ import annotations

from decimal import Decimal

from algotrader.common.enums import RejectReason
from algotrader.common.models.trading import Recommendation
from algotrader.execution.risk.context import RiskContext
from algotrader.execution.risk.framework import CheckOutcome, RiskCheck

# ---------------------------------------------------------------------------
# 11. Daily loss limit
# ---------------------------------------------------------------------------


def build_daily_loss_check(max_loss_pct: Decimal) -> RiskCheck:
    """Reject once the session's realised loss reaches its limit.

    The threshold is a percentage of **configured** capital, not of a running
    balance. A limit measured against a shrinking balance would tighten as the
    day lost — 3% of what is left is fewer rupees each time — so the same
    configuration would mean a different thing at 15:00 than at 09:20.
    """
    if not (0 < max_loss_pct <= 100):
        raise ValueError(
            f"max_daily_loss_pct {max_loss_pct} is outside (0, 100]. At 0 the "
            f"system could never trade; above 100 the limit could never fire."
        )

    def check(rec: Recommendation, ctx: RiskContext) -> CheckOutcome:
        limit = ctx.capital * max_loss_pct / 100
        loss = -ctx.realised_pnl_today  # positive when the day is down

        if ctx.daily_loss_halted:
            return CheckOutcome.fail(
                RejectReason.DAILY_LOSS_LIMIT,
                f"the daily loss limit has already halted trading today "
                f"(limit {max_loss_pct}% = {limit:.2f}; realised "
                f"{ctx.realised_pnl_today:.2f}). A halt is terminal for the "
                f"day and only an operator clears it.",
            )
        if loss >= limit:
            return CheckOutcome.fail(
                RejectReason.DAILY_LOSS_LIMIT,
                f"realised loss {loss:.2f} has reached the {max_loss_pct}% "
                f"daily limit of {limit:.2f} on capital {ctx.capital:.2f}. No "
                f"new entries; existing positions are the operator's call.",
            )
        return CheckOutcome.ok()

    return RiskCheck(
        id="daily_loss",
        fn=check,
        description=f"realised loss today is below {max_loss_pct}% of capital",
    )


# ---------------------------------------------------------------------------
# 12. Consecutive loss limit
# ---------------------------------------------------------------------------


def build_consecutive_loss_check(max_streak: int) -> RiskCheck:
    """Reject once losses have run consecutively to the configured count.

    A different signal from the daily loss limit, not a smaller version of it.
    Three small losses in a row may cost little and still mean the strategy is
    reading the session wrong; the rupee limit would not have noticed.
    """
    if max_streak < 1:
        raise ValueError(
            f"consecutive_loss_halt is {max_streak}; below 1 the system would "
            f"halt before placing a single trade"
        )

    def check(rec: Recommendation, ctx: RiskContext) -> CheckOutcome:
        if ctx.consecutive_loss_halted:
            return CheckOutcome.fail(
                RejectReason.CONSECUTIVE_LOSS_LIMIT,
                f"the consecutive-loss limit of {max_streak} has already "
                f"halted trading today. A later winning exit resets the "
                f"counter but does not clear the halt — only an operator does.",
            )
        if ctx.consecutive_losses >= max_streak:
            return CheckOutcome.fail(
                RejectReason.CONSECUTIVE_LOSS_LIMIT,
                f"{ctx.consecutive_losses} consecutive losing trades has "
                f"reached the limit of {max_streak}. Repeated losses suggest "
                f"the session is not behaving as the strategy expects.",
            )
        return CheckOutcome.ok()

    return RiskCheck(
        id="consecutive_loss",
        fn=check,
        description=f"fewer than {max_streak} consecutive losing trades",
    )


# ---------------------------------------------------------------------------
# The ordered set
# ---------------------------------------------------------------------------


def build_loss_checks(
    *,
    max_daily_loss_pct: Decimal,
    consecutive_loss_halt: int,
) -> tuple[RiskCheck, ...]:
    """The two, in the order they must run.

    Daily loss first: it is the limit denominated in the thing that actually
    matters, and an operator seeing both tripped should see the rupee one.

    Keyword-only, like the exposure factory — a percentage and a count next to
    each other is a signature where a positional call can swap them and nothing
    complains until a 3-trade streak is compared against 3% of capital.
    """
    return (
        build_daily_loss_check(max_daily_loss_pct),
        build_consecutive_loss_check(consecutive_loss_halt),
    )


#: The ids, in order, for tests and anything asserting the pipeline shape.
LOSS_ORDER = ("daily_loss", "consecutive_loss")
