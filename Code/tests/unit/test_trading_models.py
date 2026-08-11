"""Trading model arithmetic and coherence rules.

These paths were at 74% coverage with the money arithmetic entirely untested —
found by measuring rather than reading. All of it turned out to be correct, which
is the point: an untested `unrealized_pnl` that happens to be right today is one
refactor away from inverting every short position, and the risk engine uses that
number to decide when to cut. A sign error there cuts winners and holds losers,
and it would not raise.

The validators here are the other half of invariant 5 ("every position has a stop
and a time exit"): a stop that exists but sits on the wrong side of entry is not
a stop, it is a guaranteed immediate exit or no exit at all.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from algotrader.common.enums import (
    Direction,
    OrderIntent,
    OrderStatus,
    OrderType,
    Product,
    RejectReason,
    Side,
)
from algotrader.common.models.trading import (
    Order,
    OrderRequest,
    Position,
    RiskDecision,
    SizingResult,
    Trigger,
)


def _position(
    direction: Direction,
    entry: str,
    stop: str,
    quantity: int = 100,
) -> Position:
    now = dt.datetime.now(dt.UTC)
    return Position(
        correlation_id=uuid.uuid4(),
        symbol="INFY",
        slot_index=0,
        direction=direction,
        quantity=quantity,
        entry_price=Decimal(entry),
        stop_price=Decimal(stop),
        opened_at=now,
        squareoff_deadline=now + dt.timedelta(hours=5),
    )


class TestLongPositionArithmetic:
    def test_risk_is_the_distance_to_the_stop(self) -> None:
        p = _position(Direction.LONG, "1000", "990")
        assert p.risk_per_share == Decimal(10)
        assert p.initial_risk == Decimal(1000)

    def test_a_rising_price_is_profit(self) -> None:
        p = _position(Direction.LONG, "1000", "990")
        assert p.unrealized_pnl(Decimal("1010")) == Decimal(1000)

    def test_touching_the_stop_is_exactly_minus_one_r(self) -> None:
        """The definition of R. If this drifts, every risk metric drifts."""
        p = _position(Direction.LONG, "1000", "990")
        assert p.unrealized_pnl(Decimal("990")) == Decimal(-1000)
        assert p.r_multiple(Decimal("990")) == Decimal(-1)

    def test_r_multiple_scales_with_the_move(self) -> None:
        p = _position(Direction.LONG, "1000", "990")
        assert p.r_multiple(Decimal("1010")) == Decimal(1)
        assert p.r_multiple(Decimal("1030")) == Decimal(3)


class TestShortPositionArithmetic:
    """The sign flip that would silently invert every short."""

    def test_a_falling_price_is_profit(self) -> None:
        p = _position(Direction.SHORT, "1000", "1010")
        assert p.unrealized_pnl(Decimal("990")) == Decimal(1000), (
            "a short that fell in price must show a PROFIT"
        )

    def test_a_rising_price_is_loss(self) -> None:
        p = _position(Direction.SHORT, "1000", "1010")
        assert p.unrealized_pnl(Decimal("1010")) == Decimal(-1000)

    def test_touching_the_stop_is_exactly_minus_one_r(self) -> None:
        p = _position(Direction.SHORT, "1000", "1010")
        assert p.r_multiple(Decimal("1010")) == Decimal(-1)

    def test_the_short_r_multiple_is_positive_when_winning(self) -> None:
        p = _position(Direction.SHORT, "1000", "1010")
        assert p.r_multiple(Decimal("990")) == Decimal(1)

    def test_long_and_short_are_mirror_images(self) -> None:
        """Same distance moved, same magnitude, opposite direction of travel."""
        long_p = _position(Direction.LONG, "1000", "990")
        short_p = _position(Direction.SHORT, "1000", "1010")
        assert long_p.unrealized_pnl(Decimal("1020")) == short_p.unrealized_pnl(Decimal("980"))


class TestTheStopMustBeOnTheProtectiveSide:
    """A stop on the wrong side is not protection, it is a bug that parses."""

    @pytest.mark.parametrize(
        ("direction", "entry", "stop"),
        [
            (Direction.LONG, "1000", "1010"),
            (Direction.LONG, "1000", "1000"),
            (Direction.SHORT, "1000", "990"),
            (Direction.SHORT, "1000", "1000"),
        ],
    )
    def test_a_stop_on_the_wrong_side_does_not_parse(
        self, direction: Direction, entry: str, stop: str
    ) -> None:
        with pytest.raises(ValidationError):
            _position(direction, entry, stop)

    @pytest.mark.parametrize(
        ("direction", "entry", "stop"),
        [(Direction.LONG, "1000", "990"), (Direction.SHORT, "1000", "1010")],
    )
    def test_a_correctly_placed_stop_parses(
        self, direction: Direction, entry: str, stop: str
    ) -> None:
        assert _position(direction, entry, stop).risk_per_share == Decimal(10)


class TestMoneyStaysExact:
    """Decimal end to end. A float anywhere here is a slow-leaking error."""

    def test_fractional_rupees_do_not_drift(self) -> None:
        p = _position(Direction.LONG, "1234.5678", "1234.1234", quantity=3)
        assert p.risk_per_share == Decimal("0.4444")
        assert p.initial_risk == Decimal("1.3332")
        assert p.unrealized_pnl(Decimal("1234.9999")) == Decimal("1.2963")

    def test_the_arithmetic_returns_decimal_not_float(self) -> None:
        p = _position(Direction.LONG, "1000", "990")
        assert isinstance(p.risk_per_share, Decimal)
        assert isinstance(p.initial_risk, Decimal)
        assert isinstance(p.unrealized_pnl(Decimal("1010")), Decimal)
        assert isinstance(p.r_multiple(Decimal("1010")), Decimal)

    def test_a_paise_move_is_not_rounded_away(self) -> None:
        p = _position(Direction.LONG, "100.0000", "99.0000", quantity=1)
        assert p.unrealized_pnl(Decimal("100.0500")) == Decimal("0.0500")


class TestTriggerStopDirection:
    """The same protective-side rule, one layer earlier.

    A Trigger with an inverted stop would be sized and turned into a real order
    before anything noticed. Catching it at the strategy boundary means a broken
    strategy cannot emit a position that is already past its stop.
    """

    @staticmethod
    def _trigger(direction: Direction, price: str, stop: str) -> Trigger:
        return Trigger(
            symbol="INFY",
            strategy_id="orb_classic",
            direction=direction,
            trigger_price=Decimal(price),
            suggested_stop=Decimal(stop),
            timeframe_agreement=2,
            fired_at=dt.datetime.now(dt.UTC),
        )

    @pytest.mark.parametrize(
        ("direction", "price", "stop"),
        [
            (Direction.LONG, "1000", "1010"),
            (Direction.LONG, "1000", "1000"),
            (Direction.SHORT, "1000", "990"),
            (Direction.SHORT, "1000", "1000"),
        ],
    )
    def test_an_inverted_stop_does_not_parse(
        self, direction: Direction, price: str, stop: str
    ) -> None:
        with pytest.raises(ValidationError):
            self._trigger(direction, price, stop)

    def test_stop_distance_is_absolute_for_both_directions(self) -> None:
        long_t = self._trigger(Direction.LONG, "1000", "990")
        short_t = self._trigger(Direction.SHORT, "1000", "1010")
        assert long_t.stop_distance == Decimal(10)
        assert short_t.stop_distance == Decimal(10)


class TestRiskDecisionCoherence:
    """An incoherent decision is worse than a rejection: it is unexplainable.

    Every one of these states would pass a naive `if decision.approved` check
    and then fail somewhere further downstream, with the audit log recording a
    decision that cannot be reconstructed.
    """

    @staticmethod
    def _sizing() -> SizingResult:
        return SizingResult(
            quantity=10,
            entry_price=Decimal("1000"),
            stop_price=Decimal("990"),
            capital_at_risk=Decimal("100"),
            binding_constraint="risk_per_trade",
        )

    def test_an_approved_decision_without_sizing_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="must carry sizing"):
            RiskDecision(approved=True, evaluated_at=dt.datetime.now(dt.UTC))

    def test_an_approved_decision_carrying_a_reject_reason_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="must not carry a reject reason"):
            RiskDecision(
                approved=True,
                sizing=self._sizing(),
                reason=RejectReason.STALE_DATA,
                evaluated_at=dt.datetime.now(dt.UTC),
            )

    def test_a_rejected_decision_without_a_reason_is_refused(self) -> None:
        """A rejection with no reason cannot be explained or acted on."""
        with pytest.raises(ValidationError, match="must carry a reject reason"):
            RiskDecision(approved=False, evaluated_at=dt.datetime.now(dt.UTC))

    def test_a_coherent_approval_parses(self) -> None:
        d = RiskDecision(approved=True, sizing=self._sizing(), evaluated_at=dt.datetime.now(dt.UTC))
        assert d.sizing is not None
        assert d.sizing.notional == Decimal("10000")

    def test_a_coherent_rejection_parses(self) -> None:
        d = RiskDecision(
            approved=False,
            reason=RejectReason.STALE_DATA,
            evaluated_at=dt.datetime.now(dt.UTC),
        )
        assert d.sizing is None


class TestOrderRequestRequiresItsPrices:
    """An order type missing its price is rejected by the broker mid-session.

    Catching it at construction turns a live rejection into a parse error.
    """

    @staticmethod
    def _request(order_type: OrderType, **kw: object) -> OrderRequest:
        base: dict = {
            "client_order_id": "abcdef1234",
            "correlation_id": uuid.uuid4(),
            "symbol": "INFY",
            "side": Side.BUY,
            "order_type": order_type,
            "product": Product.MIS,
            "quantity": 10,
            "intent": OrderIntent.ENTRY,
        }
        base.update(kw)
        return OrderRequest(**base)

    def test_limit_without_a_limit_price_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="requires a limit price"):
            self._request(OrderType.LIMIT)

    def test_sl_needs_both_a_limit_and_a_trigger(self) -> None:
        with pytest.raises(ValidationError, match="requires a limit price"):
            self._request(OrderType.SL, trigger_price=Decimal("995"))
        with pytest.raises(ValidationError, match="requires a trigger price"):
            self._request(OrderType.SL, limit_price=Decimal("990"))

    def test_slm_without_a_trigger_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="requires a trigger price"):
            self._request(OrderType.SLM, market_protection=Decimal(-1))

    def test_a_complete_sl_order_parses(self) -> None:
        r = self._request(OrderType.SL, limit_price=Decimal("990"), trigger_price=Decimal("995"))
        assert r.limit_price == Decimal("990")


class TestOrderProgress:
    """`remaining` drives the recovery path, so it must never go negative."""

    @staticmethod
    def _order(quantity: int, filled: int, status: OrderStatus) -> Order:
        now = dt.datetime.now(dt.UTC)
        return Order(
            client_order_id="abcdef1234",
            correlation_id=uuid.uuid4(),
            symbol="INFY",
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            product=Product.MIS,
            quantity=quantity,
            limit_price=Decimal("1000"),
            status=status,
            filled_quantity=filled,
            intent=OrderIntent.ENTRY,
            placed_at=now,
            last_update_at=now,
        )

    def test_a_partial_fill_reports_what_is_left(self) -> None:
        assert self._order(100, 40, OrderStatus.OPEN).remaining == 60

    def test_an_overfill_clamps_to_zero_rather_than_going_negative(self) -> None:
        """A broker reporting more filled than ordered must not yield -N.

        A negative remaining would be read downstream as "still working" and
        could trigger a duplicate follow-up order.
        """
        assert self._order(100, 130, OrderStatus.FILLED).remaining == 0

    def test_completion_follows_the_status_not_the_quantity(self) -> None:
        assert self._order(100, 100, OrderStatus.FILLED).is_complete
        assert not self._order(100, 40, OrderStatus.OPEN).is_complete
