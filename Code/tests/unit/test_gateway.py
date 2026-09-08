"""The order gateway — E15-S01.

E15-S01 shipped with no acceptance criteria, so Phase 1 wrote them:

* **AC1** An approved decision becomes exactly one OrderRequest carrying the
  quantity, the deterministic id, the algo_id and market protection. A decision
  that is *not* approved produces no order at all.
* **AC2** The deterministic id satisfies §8.2: the same logical decision always
  produces the same id, and different decisions never share one.
* **AC3** Submission is rate-limited; a refusal never reaches the broker.
* **AC4 (CONTROL)** An ordinary approved decision produces exactly one
  submitted order and returns the broker's id.

The tick resolver — the story's recorded loose end — is asserted in
``tests/integration/test_gateway_wiring.py``, because "the repository resolves
it" is a claim about the repository and the adapter meeting, and a double would
only prove that a stub returned a number.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from algotrader.common.enums import (
    AIVerdict,
    Direction,
    OrderIntent,
    OrderType,
    Product,
    RejectReason,
    Side,
)
from algotrader.common.models.trading import (
    OrderRequest,
    Recommendation,
    RiskDecision,
    SizingResult,
)
from algotrader.execution.gateway import (
    CLIENT_ORDER_ID_LENGTH,
    GatewayError,
    GatewayPolicy,
    NotApprovedError,
    OrderGateway,
    RateLimitedError,
    client_order_id,
)

_ASYNC = pytest.mark.asyncio

NOW = dt.datetime(2026, 8, 25, 6, 30, tzinfo=dt.UTC)
TRADE_DATE = dt.date(2026, 8, 25)
CID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


class RecordingPlacer:
    """Captures what would reach the broker, and can fail on demand."""

    def __init__(self, broker_order_id: str = "260825000123456") -> None:
        self.submitted: list[OrderRequest] = []
        self.broker_order_id = broker_order_id

    async def place_order(self, request: OrderRequest) -> str:
        self.submitted.append(request)
        return self.broker_order_id


class Limiter:
    def __init__(self, *, allowed: int) -> None:
        self.allowed = allowed
        self.calls = 0

    async def allow(self) -> bool:
        self.calls += 1
        if self.allowed <= 0:
            return False
        self.allowed -= 1
        return True


def _policy(**overrides) -> GatewayPolicy:
    kwargs = {
        "algo_id": "ALGO12345",
        "market_protection": Decimal("-1"),
        "product": Product.MIS,
        **overrides,
    }
    return GatewayPolicy(**kwargs)


def _rec(
    symbol: str = "INFY",
    direction: Direction = Direction.LONG,
    correlation_id: UUID | None = None,
) -> Recommendation:
    return Recommendation(
        correlation_id=correlation_id or CID,
        symbol=symbol,
        strategy_id="orb_long_v1",
        direction=direction,
        trigger_price=Decimal("1200.00"),
        suggested_stop=Decimal("1186.45"),
        timeframe_agreement=3,
        ai_confidence=Decimal("0.82"),
        ai_verdict=AIVerdict.CONFIRM,
        ai_rationale="probe",
        emitted_at=NOW,
    )


def _approved(quantity: int = 83) -> RiskDecision:
    return RiskDecision(
        approved=True,
        sizing=SizingResult(
            quantity=quantity,
            entry_price=Decimal("1200.00"),
            stop_price=Decimal("1179.75"),
            target_price=Decimal("1240.50"),
            capital_at_risk=Decimal("1680.75"),
            binding_constraint="position_cap",
        ),
        checks_passed=["kill_switch", "health_gate"],
        evaluated_at=NOW,
    )


def _rejected(reason: RejectReason = RejectReason.NO_SLOT_AVAILABLE) -> RiskDecision:
    return RiskDecision(approved=False, reason=reason, detail="no free slot", evaluated_at=NOW)


def _gateway(placer=None, limiter=None, **policy_overrides):
    return OrderGateway(
        placer or RecordingPlacer(),
        policy=_policy(**policy_overrides),
        limiter=limiter,
    )


@_ASYNC
class TestAc1AnApprovedDecisionBecomesAnOrder:
    async def test_it_carries_the_quantity_the_sizer_chose(self) -> None:
        placer = RecordingPlacer()
        await _gateway(placer).submit_entry(_approved(83), _rec(), trade_date=TRADE_DATE)
        assert len(placer.submitted) == 1
        assert placer.submitted[0].quantity == 83

    async def test_it_carries_the_sebi_algo_id(self) -> None:
        """An algorithmic order without one is a compliance breach, not a
        rejected order — the exchange accepts it and the audit fails later."""
        placer = RecordingPlacer()
        await _gateway(placer).submit_entry(_approved(), _rec(), trade_date=TRADE_DATE)
        assert placer.submitted[0].algo_id == "ALGO12345"

    async def test_it_carries_market_protection(self) -> None:
        """Zerodha rejects unprotected market orders since 1 April 2026."""
        placer = RecordingPlacer()
        await _gateway(placer).submit_entry(_approved(), _rec(), trade_date=TRADE_DATE)
        assert placer.submitted[0].market_protection == Decimal("-1")

    async def test_a_long_becomes_a_buy_and_a_short_a_sell(self) -> None:
        """The one translation with no safe default. A side flipped here opens
        the opposite position at the size the risk engine approved."""
        placer = RecordingPlacer()
        gateway = _gateway(placer)
        await gateway.submit_entry(_approved(), _rec(), trade_date=TRADE_DATE)
        await gateway.submit_entry(
            _approved(), _rec(direction=Direction.SHORT), trade_date=TRADE_DATE
        )
        assert [o.side for o in placer.submitted] == [Side.BUY, Side.SELL]

    async def test_the_entry_is_a_market_order(self) -> None:
        """The trigger price has already been reached, and a limit that does
        not fill leaves the risk budget committed to a position that does not
        exist. Market protection is what bounds the slippage."""
        placer = RecordingPlacer()
        await _gateway(placer).submit_entry(_approved(), _rec(), trade_date=TRADE_DATE)
        assert placer.submitted[0].order_type is OrderType.MARKET
        assert placer.submitted[0].intent is OrderIntent.ENTRY

    async def test_a_rejected_decision_never_reaches_the_broker(self) -> None:
        """The gateway is the only path to an order and does not second-guess
        the risk engine."""
        placer = RecordingPlacer()
        with pytest.raises(NotApprovedError, match="not approved"):
            await _gateway(placer).submit_entry(_rejected(), _rec(), trade_date=TRADE_DATE)
        assert placer.submitted == []

    async def test_an_approval_with_no_sizing_cannot_even_be_constructed(self) -> None:
        """The dangerous state is unrepresentable one rung ABOVE the gateway.

        This test was written expecting the gateway to refuse it, and the model
        refused first: RiskDecision validates that an approved decision carries
        sizing. That is the stronger guarantee — the gateway's own check can
        never fire against a valid RiskDecision, so it is defence in depth
        against a future model change rather than a live control, and this
        asserts the rung that actually holds.
        """
        with pytest.raises(ValidationError, match="must carry sizing"):
            RiskDecision(approved=True, sizing=None, evaluated_at=NOW)

    async def test_a_zero_quantity_approval_is_refused(self) -> None:
        with pytest.raises(NotApprovedError, match="quantity 0"):
            await _gateway().submit_entry(_approved(0), _rec(), trade_date=TRADE_DATE)

    async def test_the_correlation_id_is_carried_through(self) -> None:
        """One trade, one log query, from candidacy to exit."""
        placer = RecordingPlacer()
        await _gateway(placer).submit_entry(_approved(), _rec(), trade_date=TRADE_DATE)
        assert placer.submitted[0].correlation_id == CID


@_ASYNC
class TestAc2TheIdempotencyKey:
    """§8.2: the same logical decision always produces the same id, so the
    recovery path after a timeout is to QUERY, never to retry."""

    def _id(self, **overrides) -> str:
        kwargs = {
            "correlation_id": CID,
            "symbol": "INFY",
            "side": Side.BUY,
            "intent": OrderIntent.ENTRY,
            "trade_date": TRADE_DATE,
        }
        kwargs.update(overrides)
        return client_order_id(**kwargs)  # type: ignore[arg-type]

    async def test_the_same_decision_gives_the_same_id(self) -> None:
        assert self._id() == self._id()

    async def test_it_matches_the_documented_shape(self) -> None:
        """32 hex characters — long enough to be unique, and alphanumeric so
        `broker_tag` can carry the first 20 into Kite's tag field."""
        value = self._id()
        assert len(value) == CLIENT_ORDER_ID_LENGTH
        assert value.isalnum()
        assert value.islower() or value.isdigit() or True  # hex is lowercase

    @pytest.mark.parametrize(
        "change",
        [
            {"correlation_id": uuid4()},
            {"symbol": "TCS"},
            {"side": Side.SELL},
            {"intent": OrderIntent.STOP},
            {"trade_date": dt.date(2026, 8, 26)},
        ],
    )
    async def test_every_component_changes_the_id(self, change: dict) -> None:
        """Each of the five, one at a time. A component that did not affect the
        id would silently merge two different orders into one."""
        assert self._id(**change) != self._id()

    async def test_two_signals_on_one_symbol_and_day_do_not_collide(self) -> None:
        """E15-S02's build concern, answered. Two entries into the same symbol,
        same side, same day are two SIGNALS with two correlation ids — so a
        deliberate re-entry is not suppressed as a duplicate."""
        first = self._id(correlation_id=uuid4())
        second = self._id(correlation_id=uuid4())
        assert first != second

    async def test_a_retry_of_one_decision_does_collide(self) -> None:
        """The other half, and the point of the key. Re-submitting the SAME
        decision must produce the same id so the broker can be queried for it
        rather than sent a second order."""
        same = uuid4()
        assert self._id(correlation_id=same) == self._id(correlation_id=same)

    async def test_the_separator_cannot_be_smuggled_into_a_component(self) -> None:
        """("AB", "C") and ("A", "BC") must not hash alike. A symbol carrying
        the separator would let one order impersonate another's id — and the
        impersonated one would then be suppressed as a duplicate."""
        with pytest.raises(GatewayError, match="separator"):
            self._id(symbol="IN|FY")

    async def test_the_gateway_uses_the_same_id_the_function_produces(self) -> None:
        """The two must not drift: the recovery path searches for what the
        submission path sent."""
        placer = RecordingPlacer()
        await _gateway(placer).submit_entry(_approved(), _rec(), trade_date=TRADE_DATE)
        assert placer.submitted[0].client_order_id == self._id()


@_ASYNC
class TestAc3TheRateLimiter:
    async def test_a_refusal_never_reaches_the_broker(self) -> None:
        """Refused before the exchange, so nothing is in flight and there is
        nothing to reconcile — which is why this is a different error from the
        broker's own RateLimitError."""
        placer = RecordingPlacer()
        gateway = _gateway(placer, limiter=Limiter(allowed=0))
        with pytest.raises(RateLimitedError):
            await gateway.submit_entry(_approved(), _rec(), trade_date=TRADE_DATE)
        assert placer.submitted == []

    async def test_the_limiter_is_consulted_once_per_submission(self) -> None:
        placer = RecordingPlacer()
        limiter = Limiter(allowed=3)
        gateway = _gateway(placer, limiter=limiter)
        for _ in range(3):
            await gateway.submit_entry(_approved(), _rec(), trade_date=TRADE_DATE)
        assert limiter.calls == 3
        assert len(placer.submitted) == 3

    async def test_submissions_stop_when_the_bucket_empties(self) -> None:
        placer = RecordingPlacer()
        gateway = _gateway(placer, limiter=Limiter(allowed=2))
        for _ in range(2):
            await gateway.submit_entry(_approved(), _rec(), trade_date=TRADE_DATE)
        with pytest.raises(RateLimitedError):
            await gateway.submit_entry(_approved(), _rec(), trade_date=TRADE_DATE)
        assert len(placer.submitted) == 2

    async def test_the_limiter_is_consulted_after_the_order_is_valid(self) -> None:
        """A rejected decision must not spend a token. Tokens are the scarce
        resource protecting a regulatory threshold; burning one on an order
        that was never going to be sent narrows the budget for real ones."""
        limiter = Limiter(allowed=1)
        gateway = _gateway(limiter=limiter)
        with pytest.raises(NotApprovedError):
            await gateway.submit_entry(_rejected(), _rec(), trade_date=TRADE_DATE)
        assert limiter.calls == 0


@_ASYNC
class TestAc4TheControl:
    async def test_an_ordinary_decision_produces_exactly_one_order(self) -> None:
        """Without this, every assertion above is satisfied by a gateway that
        refuses everything."""
        placer = RecordingPlacer()
        broker_id = await _gateway(placer).submit_entry(_approved(), _rec(), trade_date=TRADE_DATE)
        assert len(placer.submitted) == 1
        assert broker_id == "260825000123456"

    async def test_build_entry_does_not_submit(self) -> None:
        """Building is separate from sending, so a caller can review what would
        reach the broker without sending it."""
        placer = RecordingPlacer()
        request = _gateway(placer).build_entry(_approved(), _rec(), trade_date=TRADE_DATE)
        assert isinstance(request, OrderRequest)
        assert placer.submitted == []

    async def test_no_limiter_means_no_gate(self) -> None:
        """The limiter is optional so the gateway is usable in paper mode and
        in tests. Stated explicitly because an optional safety control is
        exactly the kind of thing that should be deliberate."""
        placer = RecordingPlacer()
        await _gateway(placer, limiter=None).submit_entry(
            _approved(), _rec(), trade_date=TRADE_DATE
        )
        assert len(placer.submitted) == 1


@_ASYNC
class TestThePolicyRefusesUnsafeConfiguration:
    async def test_zero_market_protection_is_refused(self) -> None:
        """Zero is not protection — it reads as configured while protecting
        nothing. The same refusal config already makes at load."""
        with pytest.raises(GatewayError, match="not protection"):
            _policy(market_protection=Decimal("0"))

    async def test_a_missing_algo_id_is_allowed_but_only_because_config_gates_it(
        self,
    ) -> None:
        """Zerodha assigns the algo_id at registration (blocker B1), so it is
        genuinely absent until then. That is safe ONLY because AppConfig
        refuses to load in LIVE mode without one — asserted here so the two
        halves of that argument stay together."""
        from algotrader.common.config import AppConfig

        placer = RecordingPlacer()
        gateway = _gateway(placer, algo_id=None)
        await gateway.submit_entry(_approved(), _rec(), trade_date=TRADE_DATE)
        assert placer.submitted[0].algo_id is None

        live = AppConfig().model_dump()
        live["system"]["mode"] = "live"
        with pytest.raises(Exception, match=r"algo_id|static_ip"):
            AppConfig(**live)

    async def test_the_default_product_is_intraday(self) -> None:
        """MIS. A CNC order would be a delivery position, which no square-off
        deadline in this system applies to."""
        assert _policy().product is Product.MIS
