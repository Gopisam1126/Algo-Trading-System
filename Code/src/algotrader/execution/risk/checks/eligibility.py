"""Symbol eligibility checks 5–7 (E14-S03).

Checks five through seven of the fourteen. Where the pre-conditions asked
whether to trade *at all*, these are the first that look at the candidate: may
we trade **this instrument**, is there room for it, and are we already in it?

## The design record

**Decision.** Three checks — ``symbol_tradable``, ``slot_available``,
``symbol_not_already_held`` — in that order, reading only values already on
:class:`RiskContext`. No factories: unlike the trading-window check, none of
these needs a dependency closed over, because their inputs are session state
rather than a calendar or a config table.

**Alternative rejected.** Having ``check_symbol_tradable`` consult the
surveillance lists itself. That would put a network or database read inside an
order decision, which is exactly what :class:`RiskContext` exists to prevent,
and it would make the pipeline unreplayable. The eligibility answer is
computed upstream (E04) and arrives as a value.

**The failure it prevents.** Three concrete ones, in increasing subtlety:

1. An intraday order on a symbol the exchange will not let us exit the same
   day — a T2T name — which turns an intended intraday trade into an
   unintended delivery position.
2. More concurrent positions than the configured slots, which breaks the
   capital arithmetic that sizing depends on.
3. A second position in a name already held, which is 2× the intended risk on
   one symbol while every per-trade limit still reads as satisfied. This is
   the one that looks fine in every individual check.

**What would make this wrong.** If the system ever wanted to *add to* a
winning position or reverse an existing one, ``symbol_not_already_held`` would
be blocking a deliberate feature rather than an accident. That would be a
change to this check with its own story, not a quiet loosening — pyramiding
and reversal have their own risk profiles and neither is in scope here.

## Why "not checked" is a rejection and not a default

``symbol_restrictions is None`` means eligibility was never established. The
tempting reading is "nothing was found, so it must be fine", and it is wrong
in the direction that costs money: a fetcher that failed, a symbol missing
from the day's snapshot, or an E04 that is not wired yet would all silently
become "tradable".

So the check refuses, and it refuses through
:meth:`RiskContext.require`, which raises. The framework turns a raising check
into a rejection carrying ``RISK_ENGINE_FAULT`` — deliberately a *different*
reason code from ``SYMBOL_NOT_TRADABLE``. "We checked and this symbol is
banned" and "we could not check" call for different responses, and SIT-001
established what it costs to conflate the two: an operator sent to investigate
the wrong thing.
"""

from __future__ import annotations

from algotrader.common.enums import RejectReason
from algotrader.common.models.trading import Recommendation
from algotrader.execution.risk.context import RiskContext
from algotrader.execution.risk.framework import CheckOutcome, RiskCheck

#: How many restriction labels a rejection names before summarising. The same
#: bound, for the same reason, as the health gate's: a detail string reaches
#: the audit payload and a log line once per rejected candidate per bar, and
#: nothing upstream promises this list is short. QA-SEC-29.
MAX_RESTRICTIONS_NAMED = 8


# ---------------------------------------------------------------------------
# 5. Symbol tradable
# ---------------------------------------------------------------------------


def check_symbol_tradable(rec: Recommendation, ctx: RiskContext) -> CheckOutcome:
    """May we trade this instrument at all today?

    ``ctx.symbol_restrictions`` is re-read here rather than trusted from the
    pre-market plan, because a symbol can enter a surveillance list intraday —
    the plan was right when it was built and wrong by 11:00.
    """
    # Raises RiskContextError when eligibility was never established. NOT
    # caught: the framework converts that into a fail-closed rejection, and
    # catching it here to return a tidy outcome would only duplicate the
    # framework's job while losing the distinct RISK_ENGINE_FAULT reason.
    restrictions = ctx.require(ctx.symbol_restrictions, f"eligibility for {rec.symbol}")

    if restrictions:
        total = len(restrictions)
        shown = sorted(restrictions)[:MAX_RESTRICTIONS_NAMED]
        names = ", ".join(shown)
        if total > MAX_RESTRICTIONS_NAMED:
            names += f", and {total - MAX_RESTRICTIONS_NAMED} more"
        return CheckOutcome.fail(
            RejectReason.SYMBOL_NOT_TRADABLE,
            f"{rec.symbol} carries {total} blocking restriction(s): {names}",
        )
    return CheckOutcome.ok()


# ---------------------------------------------------------------------------
# 6. Slot available
# ---------------------------------------------------------------------------


def check_slot_available(rec: Recommendation, ctx: RiskContext) -> CheckOutcome:
    """Is there a free capital slot?

    Slots are how concurrent risk is bounded: capital is divided into a fixed
    number of them, and sizing works from a slot's share. Taking a position
    without one would size against capital that another position is already
    using.

    The detail gives used/total rather than just "no slot", because those are
    two different problems — five of five used is contention and will clear on
    its own; zero of zero is a configuration that can never trade.
    """
    if ctx.slots_available <= 0:
        return CheckOutcome.fail(
            RejectReason.NO_SLOT_AVAILABLE,
            f"no free capital slot ({ctx.slots_used} of {ctx.slots_total} in "
            f"use). Sizing divides capital per slot, so a position without one "
            f"would be sized against capital another position holds.",
        )
    return CheckOutcome.ok()


# ---------------------------------------------------------------------------
# 7. Not already held
# ---------------------------------------------------------------------------


def check_symbol_not_already_held(rec: Recommendation, ctx: RiskContext) -> CheckOutcome:
    """Are we already in this name?

    Direction-blind on purpose. A second LONG on a held LONG is doubling the
    position; a SHORT on a held LONG is a reversal. Both are decisions with
    their own risk profile, and neither is an *entry* — which is the only thing
    this pipeline is for. Every per-trade limit would still read as satisfied
    while the name carried twice its intended risk, which is what makes this
    worth a dedicated gate rather than leaving it to the exposure checks.
    """
    if ctx.holds(rec.symbol):
        held = next(p for p in ctx.open_positions if p.symbol == rec.symbol)
        return CheckOutcome.fail(
            RejectReason.ALREADY_HOLDING,
            f"{rec.symbol} is already held ({held.direction.value} x"
            f"{held.quantity}). A {rec.direction.value} entry here would "
            f"double the position or reverse it; neither is an entry.",
        )
    return CheckOutcome.ok()


# ---------------------------------------------------------------------------
# The ordered set
# ---------------------------------------------------------------------------

SYMBOL_TRADABLE_CHECK = RiskCheck(
    id="symbol_tradable",
    fn=check_symbol_tradable,
    description="no blocking surveillance restriction, re-verified at order time",
)

SLOT_AVAILABLE_CHECK = RiskCheck(
    id="slot_available",
    fn=check_slot_available,
    description="a free capital slot exists",
)

NOT_ALREADY_HELD_CHECK = RiskCheck(
    id="symbol_not_already_held",
    fn=check_symbol_not_already_held,
    description="no open position in this symbol, in either direction",
)


def build_eligibility_checks() -> tuple[RiskCheck, ...]:
    """The three, in the order they must run.

    Cheapest and most absolute first, as with the pre-conditions. Eligibility
    is a property of the instrument and holds regardless of the book, so it
    precedes the two that read portfolio state; and "already held" comes last
    because it is the only one that has to scan a collection.

    A function rather than a module constant so the ordering has one
    definition, and so this matches ``build_precondition_checks`` at the call
    site that assembles the engine.
    """
    return (SYMBOL_TRADABLE_CHECK, SLOT_AVAILABLE_CHECK, NOT_ALREADY_HELD_CHECK)


#: The ids, in order, for tests and for anything asserting the pipeline shape.
ELIGIBILITY_ORDER = tuple(check.id for check in build_eligibility_checks())
