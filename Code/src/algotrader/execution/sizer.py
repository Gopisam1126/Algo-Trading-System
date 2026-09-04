"""ATR-based position sizing (E14-S07).

The component that turns fourteen passed checks into a quantity. Everything
before this refuses or permits; this is the first thing that decides *how much*.

## The design record

**Decision.** Risk budget divided by an ATR-derived stop distance, then clamped
by position value, slot capital and broker margin, then floored to a whole lot.
Exactly ``LOW_LEVEL_ARCHITECTURE.md §5.7``'s formula, with the binding clamp
recorded.

    risk_amount   = capital x risk_pct / 100
    stop_distance = ATR x atr_multiplier_stop
    raw_qty       = risk_amount / stop_distance
    quantity      = floor_to_lot(min(raw_qty, position_cap, slot_cap, margin_cap))

**The failure it prevents.** Position size chosen by price rather than by risk.
Sizing a 3,000-rupee stock and a 100-rupee stock by rupees committed makes the
volatile one carry many times the risk of the quiet one, and every per-trade
limit still reads as satisfied. Dividing the same rupee budget by each name's
own volatility is what makes "1% per trade" mean one thing across the book.

**What would make this decision wrong.** If ATR stopped being a reasonable
proxy for the distance price travels against a position — a gapping stock is
the obvious case, since ATR measures intraday range and says nothing about an
overnight gap. This system is intraday and every position has a time exit, so
the gap risk it is exposed to is bounded; a system that held overnight would
need a different denominator.

## Floor, never nearest — and it is what makes the risk bound true

``raw_qty = risk_amount / stop_distance``, so any ``quantity <= raw_qty`` gives
``quantity x stop_distance <= risk_amount``. Flooring preserves that. Rounding
to nearest breaks it on every round-up, by up to one lot's worth of stop
distance, and the breach is invisible: the position looks ordinary and the
audit log records a risk figure that is simply wrong.

Both the ``min()`` clamps and the lot rounding floor, so the bound holds
through every path. That is E14-S07's AC1 and it is a property test, not a
comment.

## The stop is computed here, not taken from the Recommendation

``Recommendation.suggested_stop`` exists and is deliberately **not** an input.
Invariant 1 says the executable stop price is computed downstream of the AI
boundary; a ``Recommendation`` whose suggested stop drove the quantity would be
carrying a sizing field under another name. The suggested stop stays useful as
a sanity signal — it is not arithmetic here.

## Tick rounding belongs to the order gateway, and is safe

``broker/kite/mapping.py``'s ``round_to_tick`` is documented as the one used
before submission, and it needs a ``Side`` and the instrument's tick size —
neither of which belongs on :class:`RiskContext`.

Leaving it there does not weaken the risk bound, which was **verified rather
than assumed**: it rounds a BUY down and a SELL up, so a long's stop (a SELL)
moves *up* toward entry and a short's stop (a BUY) moves *down* toward entry.
Across every sub-tick offset the snapped stop is never further from entry than
the computed one, so submission-time rounding only ever *reduces* realised
risk.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from algotrader.common.enums import Direction
from algotrader.common.models.trading import Recommendation, SizingResult
from algotrader.execution.risk.context import RiskContext

#: What bound the quantity. Recorded on every :class:`SizingResult` so a
#: surprisingly small position is explainable from the audit log rather than
#: investigated (E14-S07 AC2).
RISK_BUDGET = "risk_budget"
POSITION_CAP = "position_cap"
SLOT_CAP = "slot_cap"
MARGIN_CAP = "margin_cap"
LOT_ROUNDING = "lot_rounding"

#: The clamps in the order §5.7 lists them. Ties go to the EARLIER entry, which
#: is why this is a sequence rather than a dict: when the risk budget and a cap
#: produce the same number, the honest answer is that risk bound it — the cap
#: merely agreed.
_CLAMP_ORDER = (RISK_BUDGET, POSITION_CAP, SLOT_CAP, MARGIN_CAP)

#: `Price` is `decimal_places=4`, so a stop derived from an ATR of 0.0501 and a
#: 1.5 multiplier (1199.92485) does not fit. The distance is quantised ONCE and
#: then used for the quantity, the stop and the risk figure alike.
#:
#: Rounding DOWN, for two reasons that agree. It puts the stop marginally
#: closer to entry, which is the same direction submission-time tick rounding
#: moves it. And using one quantised distance for both the divisor and the
#: multiplier keeps `quantity x distance <= risk_amount` exact — quantising
#: afterwards would leave the recorded risk describing a stop that is not the
#: one being placed.
_PRICE_PLACES = Decimal("0.0001")


def _quantise(value: Decimal) -> Decimal:
    return value.quantize(_PRICE_PLACES, rounding=ROUND_DOWN)


@dataclass(frozen=True)
class SizingPolicy:
    """The configured numbers sizing needs, in one object.

    Separate from :class:`RiskContext` for the same reason the calendar is:
    these are *dependencies* read from config, not per-candidate state. Passing
    them as a value keeps the sizer a pure function of (recommendation,
    context, policy).
    """

    risk_pct: Decimal
    atr_multiplier_stop: Decimal
    max_position_pct: Decimal
    capital_per_slot_pct: Decimal
    target_r_multiple: Decimal

    def __post_init__(self) -> None:
        for name in (
            "risk_pct",
            "atr_multiplier_stop",
            "max_position_pct",
            "capital_per_slot_pct",
            "target_r_multiple",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(
                    f"SizingPolicy.{name} must be a positive finite Decimal, got "
                    f"{value!r}. Every one of these is multiplied into a rupee "
                    f"amount or a share count."
                )


def _floor_to_lot(quantity: Decimal, lot_size: int) -> int:
    """Whole lots, always rounding **down**.

    ``int()`` on a Decimal truncates toward zero, which for a non-negative
    quantity is a floor. Stated explicitly because "truncate" and "floor" part
    company for negatives, and a negative quantity here would be a sell order
    where a buy was intended.
    """
    if quantity <= 0:
        return 0
    lots = int(quantity / lot_size)
    return lots * lot_size


def size_position(rec: Recommendation, ctx: RiskContext, policy: SizingPolicy) -> SizingResult:
    """Compute the quantity, the executable stop, and what bound them.

    Returns a :class:`SizingResult` with ``quantity == 0`` when no position
    fits. It does **not** raise for that case: zero is a real answer with a
    real explanation, and :meth:`RiskEngine._size` turns it into a rejection
    carrying the binding constraint. Raising would lose which clamp it was.
    """
    atr = ctx.require(ctx.atr, f"ATR for {rec.symbol}")
    margin = ctx.require(ctx.available_margin, f"live broker margin for {rec.symbol}")
    per_share = ctx.require(ctx.margin_per_share, f"per-share margin requirement for {rec.symbol}")

    entry = rec.trigger_price
    stop_distance = _quantise(atr * policy.atr_multiplier_stop)
    risk_amount = ctx.capital * policy.risk_pct / 100
    if stop_distance <= 0:
        # An ATR small enough to vanish at four decimal places. Dividing by it
        # would be an infinite quantity, and a zero-width stop is not a stop.
        return SizingResult(
            quantity=0,
            entry_price=entry,
            stop_price=entry,
            target_price=None,
            capital_at_risk=Decimal(0),
            binding_constraint=(
                f"ATR {atr} x {policy.atr_multiplier_stop} rounds to a "
                f"zero-width stop at 4dp; no position is sizeable"
            ),
        )

    # Every candidate is a share count. Named so the binding one can be
    # reported by name rather than by position in a tuple.
    candidates: dict[str, Decimal] = {
        RISK_BUDGET: risk_amount / stop_distance,
        POSITION_CAP: (ctx.capital * policy.max_position_pct / 100) / entry,
        SLOT_CAP: (ctx.capital * policy.capital_per_slot_pct / 100) / entry,
        MARGIN_CAP: margin / per_share,
    }
    smallest = min(candidates.values())
    # First in §5.7's order wins a tie: if the risk budget and a cap agree on
    # the number, risk is what bound it and the cap merely concurred.
    binding = next(name for name in _CLAMP_ORDER if candidates[name] == smallest)

    quantity = _floor_to_lot(smallest, ctx.lot_size)
    if quantity == 0 and smallest >= 1:
        # The clamps allowed at least one share and the LOT SIZE is what took
        # it to nothing. Distinct from a clamp allowing less than a share,
        # where naming lot rounding would point at the wrong thing entirely —
        # `smallest >= 1` is the line between the two.
        binding = LOT_ROUNDING

    reward = _quantise(stop_distance * policy.target_r_multiple)
    if rec.direction is Direction.LONG:
        stop_price = entry - stop_distance
        target_price = entry + reward
    else:
        stop_price = entry + stop_distance
        target_price = entry - reward

    if stop_price <= 0 or target_price <= 0:
        # A stop distance wider than the price itself. Possible for a penny
        # stock with an enormous ATR, and `Price` requires gt=0, so building
        # the result would raise somewhere less informative than here.
        return SizingResult(
            quantity=0,
            entry_price=entry,
            stop_price=entry,
            target_price=None,
            capital_at_risk=Decimal(0),
            binding_constraint=(
                f"stop distance {stop_distance} is not less than the entry "
                f"price {entry}; no position is sizeable"
            ),
        )

    return SizingResult(
        quantity=quantity,
        entry_price=entry,
        stop_price=stop_price,
        target_price=target_price,
        capital_at_risk=quantity * stop_distance,
        binding_constraint=binding,
    )


def build_sizer(policy: SizingPolicy) -> Callable[[Recommendation, RiskContext], SizingResult]:
    """Bind a policy, producing what :class:`RiskEngine` wants for ``sizer``."""

    def sizer(rec: Recommendation, ctx: RiskContext) -> SizingResult:
        return size_position(rec, ctx, policy)

    return sizer
