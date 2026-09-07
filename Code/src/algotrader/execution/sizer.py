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
    quantity      = floor_to_lot(min(raw_qty, position_cap, slot_cap, margin_cap,
                                     sector_headroom, net_headroom))

**E14-S10 added the last two.** ``max_sector_exposure_pct`` and
``max_net_directional_exposure_pct`` had been in ``system.yaml`` since the
beginning and were enforced by nothing. §5.7's formula does not list them, and
checks 9 and 10 cannot supply them: risk checks run BEFORE sizing, so at check
time the candidate has a direction but no quantity, and an exposure cap is a
statement about notional. Those checks can only refuse a book that is ALREADY
at a cap — a book at 39% of a 40% sector cap passes, and sizing was then free
to add another 20% position.

The sizer is the only component that knows the quantity and already applies a
``min()`` over clamps while recording which one bound, so the constraint's
natural home is here. The rejected alternative was to have check 9 assume the
worst case — that the candidate will take the full ``max_position_pct`` — and
refuse if that would breach. Safe, but badly over-tight: real ATR-based sizes
are typically far under the 20% cap, so the system would sit out trades that
would have fit comfortably, for a reason no operator could see in the book.

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
from algotrader.common.text import one_safe_line
from algotrader.execution.risk.context import RiskContext

#: What bound the quantity. Recorded on every :class:`SizingResult` so a
#: surprisingly small position is explainable from the audit log rather than
#: investigated (E14-S07 AC2).
RISK_BUDGET = "risk_budget"
POSITION_CAP = "position_cap"
SLOT_CAP = "slot_cap"
MARGIN_CAP = "margin_cap"
SECTOR_CAP = "sector_cap"
NET_EXPOSURE_CAP = "net_exposure_cap"
LOT_ROUNDING = "lot_rounding"

#: The clamps in the order §5.7 lists them, with E14-S10's two appended. Ties go
#: to the EARLIER entry, which is why this is a sequence rather than a dict: when
#: the risk budget and a cap produce the same number, the honest answer is that
#: risk bound it — the cap merely agreed.
#:
#: The two portfolio clamps go LAST deliberately. They are the newest and the
#: least familiar, so on a tie the reported constraint stays the one a reader
#: already understands; a tie means both were equally binding and naming either
#: is true.
_CLAMP_ORDER = (
    RISK_BUDGET,
    POSITION_CAP,
    SLOT_CAP,
    MARGIN_CAP,
    SECTOR_CAP,
    NET_EXPOSURE_CAP,
)

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
    #: E14-S10. Required, not optional-with-a-default, and that is the whole
    #: point of the story: these two numbers have been in ``system.yaml`` since
    #: the beginning and were binding on nothing. An optional field here would
    #: reproduce the defect one layer down — a policy built without them would
    #: silently disable the clamp, which is "configuring a limit means the limit
    #: is enforced" told backwards.
    max_sector_exposure_pct: Decimal
    max_net_directional_exposure_pct: Decimal

    def __post_init__(self) -> None:
        for name in (
            "risk_pct",
            "atr_multiplier_stop",
            "max_position_pct",
            "capital_per_slot_pct",
            "target_r_multiple",
            "max_sector_exposure_pct",
            "max_net_directional_exposure_pct",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(
                    f"SizingPolicy.{name} must be a positive finite Decimal, got "
                    f"{value!r}. Every one of these is multiplied into a rupee "
                    f"amount or a share count."
                )
        for name in ("max_sector_exposure_pct", "max_net_directional_exposure_pct"):
            value = getattr(self, name)
            if value > 100:
                raise ValueError(
                    f"SizingPolicy.{name} is {value}, which is more than 100% of "
                    f"capital. A cap above the whole book cannot bind, and a "
                    f"limit that cannot bind is worse than none — it reads as a "
                    f"control in every review and stops nothing."
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


def _portfolio_headroom(
    rec: Recommendation, ctx: RiskContext, policy: SizingPolicy
) -> tuple[Decimal, Decimal]:
    """Rupees of sector and net-directional room left, for THIS candidate.

    Both are returned as rupee amounts; the caller divides by entry to get a
    share count, exactly as it does for the position and slot caps.

    **Sector is unsigned.** :meth:`RiskContext.sector_exposure` sums notional
    regardless of direction, and that is right: a sector shock moves every name
    in the sector, and a short in the same sector is exposed to the same event.
    So the headroom does not depend on which way this candidate points.

    **Net directional is signed, and that changes the arithmetic.** The cap is
    on ``|net|``. A long adds to net, a short subtracts, so the room available
    depends on which side the book is already leaning::

        LONG  from net=+2L, cap 3L  ->  1L of room   (cap - net)
        SHORT from net=+2L, cap 3L  ->  5L of room   (cap + net)

    A short into a long book is *reducing* directional risk, and a formula that
    ignored the sign would refuse it while permitting the long that makes the
    book worse. One expression covers both: ``cap - sign * net``.

    Both figures can come back zero or negative when the book is already at or
    past a cap. That is not special-cased — a negative headroom becomes a
    negative share count, ``min()`` selects it, and the lot floor turns it into
    a quantity of zero with the right clamp named. Checks 9 and 10 normally
    refuse before it gets here; this stays correct when they have not run.
    """
    unclassified = ctx.positions_missing_a_sector()
    if unclassified:
        # Check 9 raises on this too. Repeated rather than assumed, because a
        # sector total computed while a position is unclassified is too SMALL,
        # which makes the headroom too LARGE and the clamp too loose. A safety
        # clamp computed from an untrustworthy input is worse than no clamp:
        # it reports a number nobody has reason to doubt.
        # `one_safe_line` on the symbols, not just on the finished detail.
        #
        # OpenPosition.symbol carries NO validator — unlike Trigger,
        # Recommendation and OrderRequest, which all gained `_validate_symbol`
        # after log forgery was found on them four separate times. It arrives
        # from the position store, which is fed by the broker, so it is
        # untrusted by the same argument that applied to those three.
        #
        # RiskDecision.detail would escape this downstream (QA-SEC-38), but the
        # exception also reaches `log.exception`'s traceback, which that
        # validator never sees. Escaping at the point of interpolation is the
        # rung above relying on a downstream consumer.
        #
        # It also bounds the LENGTH, which taking the first eight does not:
        # eight symbols with no length limit is how QA-SEC-29 produced an
        # 80,054-character detail while looking capped.
        named = one_safe_line(", ".join(sorted(unclassified)[:8]))
        raise ValueError(
            f"{len(unclassified)} open position(s) have no sector "
            f"({named}), so sector exposure cannot be totalled and the "
            f"headroom clamp would be computed from a number that is "
            f"known to be wrong."
        )

    sector = ctx.require(ctx.symbol_sector, f"sector classification for {rec.symbol}")
    sector_cap_value = ctx.capital * policy.max_sector_exposure_pct / 100
    sector_headroom = sector_cap_value - ctx.sector_exposure(sector)

    net_cap_value = ctx.capital * policy.max_net_directional_exposure_pct / 100
    sign = Decimal(1) if rec.direction is Direction.LONG else Decimal(-1)
    net_headroom = net_cap_value - sign * ctx.net_exposure()

    return sector_headroom, net_headroom


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
    sector_headroom, net_headroom = _portfolio_headroom(rec, ctx, policy)
    candidates: dict[str, Decimal] = {
        RISK_BUDGET: risk_amount / stop_distance,
        POSITION_CAP: (ctx.capital * policy.max_position_pct / 100) / entry,
        SLOT_CAP: (ctx.capital * policy.capital_per_slot_pct / 100) / entry,
        MARGIN_CAP: margin / per_share,
        # E14-S10. The clamp is on the room LEFT, so the resulting book lands at
        # or under the cap — not merely "the book was under the cap before this
        # trade", which is all checks 9 and 10 can assert from where they run.
        SECTOR_CAP: sector_headroom / entry,
        NET_EXPOSURE_CAP: net_headroom / entry,
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
