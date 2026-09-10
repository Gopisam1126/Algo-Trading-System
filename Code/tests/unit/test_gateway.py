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
from typing import ClassVar
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from algotrader.broker.adapter import (
    AmbiguousOrderError,
    DuplicateBrokerOrderError,
    OrderRejectedError,
)
from algotrader.common.enums import (
    AIVerdict,
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
    OrderNeverLandedError,
    OrderStateUnknownError,
    RateLimitedError,
    client_order_id,
)
from algotrader.execution.order_state import IllegalTransitionError

_ASYNC = pytest.mark.asyncio

NOW = dt.datetime(2026, 8, 25, 6, 30, tzinfo=dt.UTC)
TRADE_DATE = dt.date(2026, 8, 25)
CID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


class RecordingPlacer:
    """Captures what would reach the broker, and can fail on demand.

    ``fail_with`` makes the NEXT submission raise; ``orderbook`` is what the
    recovery query then finds. The two together are how an ambiguous failure
    is staged: the call raises, and whether the order is really there is a
    separate fact the test controls independently. That separation is the
    point of E15-S02 — the exception says "unknown", not "absent".
    """

    def __init__(self, broker_order_id: str = "260825000123456") -> None:
        self.submitted: list[OrderRequest] = []
        self.broker_order_id = broker_order_id
        self.fail_with: Exception | None = None
        self.orderbook: dict[str, Order] = {}
        self.find_calls: list[str] = []

    async def place_order(self, request: OrderRequest) -> str:
        self.submitted.append(request)
        if self.fail_with is not None:
            raise self.fail_with
        return self.broker_order_id

    async def find_by_client_order_id(self, client_order_id: str) -> Order | None:
        self.find_calls.append(client_order_id)
        return self.orderbook.get(client_order_id)

    #: A distinct sentinel, because None is a MEANINGFUL broker order id
    #: here — it is the half-answer AC3 probes. Defaulting on None would
    #: make that state inexpressible, and the probe would silently test the
    #: happy path instead.
    _DEFAULT_ID: ClassVar[object] = object()

    def landed(self, request: OrderRequest, broker_order_id: object = _DEFAULT_ID) -> None:
        """Say that this order IS in the broker's orderbook after all."""
        resolved = self.broker_order_id if broker_order_id is self._DEFAULT_ID else broker_order_id
        self.orderbook[request.client_order_id] = _broker_order(request, resolved)  # type: ignore[arg-type]


def _broker_order(request: OrderRequest, broker_order_id: str | None) -> Order:
    return Order(
        client_order_id=request.client_order_id,
        broker_order_id=broker_order_id,
        correlation_id=request.correlation_id,
        symbol=request.symbol,
        side=request.side,
        order_type=request.order_type,
        product=request.product,
        quantity=request.quantity,
        status=OrderStatus.OPEN,
        intent=request.intent,
        placed_at=NOW,
        last_update_at=NOW,
    )


class RecordingStore:
    """An order store with the durability contract, and no database.

    ``insert_submitting`` enforces uniqueness on ``client_order_id`` the way
    the real ``uq_client_order`` constraint does, so a test that manages to
    reach a second insert for one decision fails here rather than passing and
    failing in production.
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.inserts: list[dict] = []
        self.attaches: list[tuple[str, str]] = []
        self.fail_insert: Exception | None = None
        self.fail_attach: Exception | None = None

    async def insert_submitting(self, order: dict) -> int:
        if self.fail_insert is not None:
            raise self.fail_insert
        cid = order["client_order_id"]
        if cid in self.rows:
            raise AssertionError(
                f"uq_client_order would have rejected a second insert of {cid}. "
                f"Reaching here means the idempotency check above it did not fire."
            )
        row = {**order, "status": "SUBMITTING", "broker_order_id": None}
        self.rows[cid] = row
        # A COPY: `attach_broker_id` mutates the stored row, and an alias here
        # would rewrite history so that AC1 could never observe SUBMITTING.
        self.inserts.append(dict(row))
        return len(self.rows)

    async def attach_broker_id(self, client_order_id: str, broker_order_id: str) -> None:
        if self.fail_attach is not None:
            raise self.fail_attach
        self.attaches.append((client_order_id, broker_order_id))
        self.rows[client_order_id]["broker_order_id"] = broker_order_id
        self.rows[client_order_id]["status"] = "SUBMITTED"

    async def find_by_client_order_id(self, client_order_id: str) -> dict | None:
        row = self.rows.get(client_order_id)
        return None if row is None else dict(row)

    def preexisting(self, cid: str, *, broker_order_id: str | None, status: str) -> None:
        """Seed a row as an earlier, separate process would have left it."""
        self.rows[cid] = {
            "client_order_id": cid,
            "broker_order_id": broker_order_id,
            "status": status,
        }


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


def _gateway(placer=None, limiter=None, store=None, **policy_overrides):
    return OrderGateway(
        placer or RecordingPlacer(),
        policy=_policy(**policy_overrides),
        store=store if store is not None else RecordingStore(),
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
        """CORRECTED 10 Sep 2026 while delivering E15-S02.

        This used to submit the SAME decision three times and assert that
        three orders reached the broker — the duplicate-position bug E15-S02
        exists to prevent, written down as a passing assertion. Three
        *submissions* means three distinct signals, so each now carries its
        own correlation_id and its own idempotency key.
        """
        placer = RecordingPlacer()
        limiter = Limiter(allowed=3)
        gateway = _gateway(placer, limiter=limiter)
        for _ in range(3):
            await gateway.submit_entry(
                _approved(), _rec(correlation_id=uuid4()), trade_date=TRADE_DATE
            )
        assert limiter.calls == 3
        assert len(placer.submitted) == 3

    async def test_a_suppressed_duplicate_does_not_spend_a_token(self) -> None:
        """The other half, and it is not merely bookkeeping. Tokens are the
        scarce resource protecting a regulatory threshold: one spent on an
        order that is never sent narrows the budget for one that is."""
        placer = RecordingPlacer()
        limiter = Limiter(allowed=3)
        gateway = _gateway(placer, limiter=limiter)
        for _ in range(3):
            await gateway.submit_entry(_approved(), _rec(), trade_date=TRADE_DATE)
        assert limiter.calls == 1
        assert len(placer.submitted) == 1

    async def test_submissions_stop_when_the_bucket_empties(self) -> None:
        placer = RecordingPlacer()
        gateway = _gateway(placer, limiter=Limiter(allowed=2))
        for _ in range(2):
            await gateway.submit_entry(
                _approved(), _rec(correlation_id=uuid4()), trade_date=TRADE_DATE
            )
        with pytest.raises(RateLimitedError):
            await gateway.submit_entry(
                _approved(), _rec(correlation_id=uuid4()), trade_date=TRADE_DATE
            )
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

    async def test_a_missing_algo_id_is_the_expected_state_not_a_gap(
        self,
    ) -> None:
        """CORRECTED 9 Sep 2026, and the correction is the point.

        This test used to assert that ``None`` was safe *only because*
        ``AppConfig`` refused LIVE mode without an algo_id. Both halves were
        wrong: SEBI requires a registered Algo-ID only at or above the
        order-rate threshold (circular 4 Feb 2025, I(c)), so at 5 orders/sec
        none is ever issued, and the config gate no longer demands one.

        What actually makes ``None`` safe is structural: the hard cap sits
        below the threshold, so this system cannot enter the regime where an
        Algo-ID is required. Asserted here so the two halves of the *current*
        argument stay together.
        """
        from algotrader.common.config import (
            MAX_ORDERS_PER_SECOND,
            SEBI_ALGO_REGISTRATION_OPS,
            AppConfig,
        )

        placer = RecordingPlacer()
        gateway = _gateway(placer, algo_id=None)
        await gateway.submit_entry(_approved(), _rec(), trade_date=TRADE_DATE)
        assert placer.submitted[0].algo_id is None

        assert MAX_ORDERS_PER_SECOND < SEBI_ALGO_REGISTRATION_OPS

        live = AppConfig().model_dump()
        live["system"]["mode"] = "live"
        live["system"]["static_ip"] = "1.2.3.4"
        live["broker"]["algo_id"] = ""
        assert AppConfig(**live).broker.algo_id == ""

    async def test_live_mode_still_refuses_to_load_without_a_static_ip(self) -> None:
        """The obligation that did NOT dissolve, kept adjacent to the one that
        did. Static IP applies at every order rate, and an order from an
        unwhitelisted address is rejected outright."""
        from algotrader.common.config import AppConfig

        live = AppConfig().model_dump()
        live["system"]["mode"] = "live"
        live["system"]["static_ip"] = ""
        with pytest.raises(Exception, match="static_ip"):
            AppConfig(**live)

    async def test_the_default_product_is_intraday(self) -> None:
        """MIS. A CNC order would be a delivery position, which no square-off
        deadline in this system applies to."""
        assert _policy().product is Product.MIS


# ===========================================================================
# E15-S02 — idempotency: persist before submitting, query rather than retry
# ===========================================================================


@_ASYNC
class TestAc1TheIntentIsRecordedBeforeTheBrokerIsCalled:
    """The two-transaction rule. Without TX1 there is nothing for
    reconciliation to find, and 8.2's whole recovery design is unimplementable
    — a process that dies during the broker call leaves no trace that an order
    might exist."""

    async def test_the_row_exists_before_submission(self) -> None:
        """Asserted by ORDERING, not by the end state. A store written after
        the broker call would leave exactly the same rows behind."""
        order: list[str] = []

        class OrderingStore(RecordingStore):
            async def insert_submitting(self, o: dict) -> int:
                order.append("insert")
                return await super().insert_submitting(o)

        class OrderingPlacer(RecordingPlacer):
            async def place_order(self, request: OrderRequest) -> str:
                order.append("place")
                return await super().place_order(request)

        await _gateway(OrderingPlacer(), store=OrderingStore()).submit_entry(
            _approved(), _rec(), trade_date=TRADE_DATE
        )
        assert order == ["insert", "place"], "the broker was called before the record"

    async def test_the_row_carries_no_broker_id_yet(self) -> None:
        """SUBMITTING with a null broker id is what reconciliation keys on."""
        store = RecordingStore()
        await _gateway(store=store).submit_entry(_approved(), _rec(), trade_date=TRADE_DATE)
        assert store.inserts[0]["status"] == "SUBMITTING"
        assert store.inserts[0]["broker_order_id"] is None

    async def test_it_records_what_was_actually_sent(self) -> None:
        """A row that disagrees with the order is worse than no row:
        reconciliation would compare the broker's truth against a fiction."""
        placer = RecordingPlacer()
        store = RecordingStore()
        await _gateway(placer, store=store).submit_entry(
            _approved(83), _rec(), trade_date=TRADE_DATE
        )
        sent, recorded = placer.submitted[0], store.inserts[0]
        assert recorded["client_order_id"] == sent.client_order_id
        assert recorded["quantity"] == sent.quantity == 83
        assert recorded["side"] == sent.side.value
        assert recorded["intent"] == sent.intent.value
        assert recorded["market_protection"] == sent.market_protection

    async def test_a_rejected_decision_records_nothing(self) -> None:
        store = RecordingStore()
        with pytest.raises(NotApprovedError):
            await _gateway(store=store).submit_entry(_rejected(), _rec(), trade_date=TRADE_DATE)
        assert store.rows == {}

    async def test_a_rate_limited_order_records_nothing(self) -> None:
        """The ordering that matters most here. A SUBMITTING row for an order
        that was never sent would send the reconciliation loop hunting an order
        the broker has never heard of, every 30 seconds, all day."""
        placer, store = RecordingPlacer(), RecordingStore()
        with pytest.raises(RateLimitedError):
            await _gateway(placer, limiter=Limiter(allowed=0), store=store).submit_entry(
                _approved(), _rec(), trade_date=TRADE_DATE
            )
        assert store.rows == {}
        assert placer.submitted == []

    async def test_a_store_failure_stops_the_order(self) -> None:
        """Fail closed: a fact we cannot record is a fact we must not create."""
        placer, store = RecordingPlacer(), RecordingStore()
        store.fail_insert = RuntimeError("disk on fire")
        with pytest.raises(RuntimeError, match="disk on fire"):
            await _gateway(placer, store=store).submit_entry(
                _approved(), _rec(), trade_date=TRADE_DATE
            )
        assert placer.submitted == [], "an unrecordable order still reached the broker"


@_ASYNC
class TestAc2TheSameDecisionTwiceIsOneOrder:
    """The property the whole story exists for."""

    async def test_a_completed_submission_is_not_sent_again(self) -> None:
        placer, store = RecordingPlacer(), RecordingStore()
        gateway = _gateway(placer, store=store)
        first = await gateway.submit_entry(_approved(), _rec(), trade_date=TRADE_DATE)
        second = await gateway.submit_entry(_approved(), _rec(), trade_date=TRADE_DATE)
        assert first == second
        assert len(placer.submitted) == 1, "the decision was submitted twice"

    async def test_it_returns_the_id_the_broker_gave_the_first_time(self) -> None:
        placer, store = RecordingPlacer(), RecordingStore()
        gateway = _gateway(placer, store=store)
        first = await gateway.submit_entry(_approved(), _rec(), trade_date=TRADE_DATE)
        placer.broker_order_id = "DIFFERENT-999"
        assert await gateway.submit_entry(_approved(), _rec(), trade_date=TRADE_DATE) == first

    async def test_a_row_left_mid_flight_queries_the_broker(self) -> None:
        """A previous PROCESS died between recording and submitting. Only the
        broker knows which side of the call it died on, so it is asked."""
        placer, store = RecordingPlacer(), RecordingStore()
        request = _gateway(placer, store=store).build_entry(
            _approved(), _rec(), trade_date=TRADE_DATE
        )
        store.preexisting(request.client_order_id, broker_order_id=None, status="SUBMITTING")
        placer.landed(request, "ADOPTED-1")

        adopted = await _gateway(placer, store=store).submit_entry(
            _approved(), _rec(), trade_date=TRADE_DATE
        )
        assert adopted == "ADOPTED-1"
        assert placer.submitted == [], "a mid-flight row was resubmitted"
        assert store.rows[request.client_order_id]["broker_order_id"] == "ADOPTED-1"

    async def test_a_different_signal_on_the_same_symbol_is_a_different_order(self) -> None:
        """The control on all of the above. Idempotency that suppressed
        genuinely new orders would pass every test in this class."""
        placer, store = RecordingPlacer(), RecordingStore()
        gateway = _gateway(placer, store=store)
        await gateway.submit_entry(_approved(), _rec(), trade_date=TRADE_DATE)
        await gateway.submit_entry(_approved(), _rec(correlation_id=uuid4()), trade_date=TRADE_DATE)
        assert len(placer.submitted) == 2


@_ASYNC
class TestAc3AnAmbiguousFailureQueriesAndNeverRetries:
    async def test_an_order_that_did_land_is_adopted(self) -> None:
        """The timeout that was a lie. The call raised; the order is there."""
        placer, store = RecordingPlacer(), RecordingStore()
        request = _gateway(placer, store=store).build_entry(
            _approved(), _rec(), trade_date=TRADE_DATE
        )
        placer.fail_with = AmbiguousOrderError("read timed out")
        placer.landed(request, "REALLY-THERE")

        assert (
            await _gateway(placer, store=store).submit_entry(
                _approved(), _rec(), trade_date=TRADE_DATE
            )
            == "REALLY-THERE"
        )
        assert len(placer.submitted) == 1, "an ambiguous failure was retried"
        assert placer.find_calls == [request.client_order_id]

    async def test_an_order_that_never_landed_raises_rather_than_resubmitting(self) -> None:
        """8.2 says such an order *may* be resubmitted. Permission, not
        obligation: if this absence call is ever wrong, an automatic retry is
        a second real position."""
        placer, store = RecordingPlacer(), RecordingStore()
        placer.fail_with = AmbiguousOrderError("read timed out")
        with pytest.raises(OrderNeverLandedError, match="never landed"):
            await _gateway(placer, store=store).submit_entry(
                _approved(), _rec(), trade_date=TRADE_DATE
            )
        assert len(placer.submitted) == 1

    async def test_a_half_answer_is_not_treated_as_either_answer(self) -> None:
        """A record with no broker id supports neither conclusion. Fail closed
        rather than picking the convenient one."""
        placer, store = RecordingPlacer(), RecordingStore()
        request = _gateway(placer, store=store).build_entry(
            _approved(), _rec(), trade_date=TRADE_DATE
        )
        placer.fail_with = AmbiguousOrderError("read timed out")
        placer.landed(request, None)
        with pytest.raises(OrderStateUnknownError):
            await _gateway(placer, store=store).submit_entry(
                _approved(), _rec(), trade_date=TRADE_DATE
            )

    async def test_a_duplicate_at_the_broker_is_not_resolved_by_picking_one(self) -> None:
        """The adversarial case. If the broker holds TWO orders carrying our
        key, the failure idempotency exists to prevent has already happened.
        The adapter raises rather than returning one, and the gateway must let
        that through: adopting either id would report success over a duplicate
        position, and picking the stale one reads as "not placed" and invites
        a third."""

        class _DuplicatingPlacer(RecordingPlacer):
            async def find_by_client_order_id(self, client_order_id: str):
                self.find_calls.append(client_order_id)
                raise DuplicateBrokerOrderError(client_order_id, ["ONE", "TWO"])

        placer, store = _DuplicatingPlacer(), RecordingStore()
        placer.fail_with = AmbiguousOrderError("read timed out")
        with pytest.raises(DuplicateBrokerOrderError):
            await _gateway(placer, store=store).submit_entry(
                _approved(), _rec(), trade_date=TRADE_DATE
            )
        assert len(placer.submitted) == 1

    async def test_an_outright_rejection_is_not_treated_as_ambiguous(self) -> None:
        """The control. A rejection is KNOWN — it must not trigger a recovery
        query, and it must not be swallowed into an adoption."""
        placer, store = RecordingPlacer(), RecordingStore()
        placer.fail_with = OrderRejectedError("insufficient funds", reason_code="MARGIN")
        with pytest.raises(OrderRejectedError):
            await _gateway(placer, store=store).submit_entry(
                _approved(), _rec(), trade_date=TRADE_DATE
            )
        assert placer.find_calls == []


@_ASYNC
class TestTheResumePathConsultsTheStateMachine:
    """E15-S03's wiring, and it existed untested until mutation said so.

    Before E15-S03 `_resume` inspected only `broker_order_id`. A row recorded
    REJECTED or CANCELLED carries none — there is no live order — so it fell
    into the mid-flight branch and would have been queried and adopted as
    though a submission were still in progress. Asking the state machine
    whether the recorded status may legally reach SUBMITTED answers every one
    of those cases at once.
    """

    @pytest.mark.parametrize("recorded", ["FILLED", "CANCELLED", "REJECTED"])
    async def test_a_finished_order_is_never_resumed(self, recorded: str) -> None:
        """Terminal states have no legal moves at all, so this is AC2 reaching
        the gateway: an order the system already considers finished cannot be
        dragged back into a submission."""
        placer, store = RecordingPlacer(), RecordingStore()
        cid = (
            _gateway(placer, store=store)
            .build_entry(_approved(), _rec(), trade_date=TRADE_DATE)
            .client_order_id
        )
        store.preexisting(cid, broker_order_id=None, status=recorded)

        with pytest.raises(IllegalTransitionError):
            await _gateway(placer, store=store).submit_entry(
                _approved(), _rec(), trade_date=TRADE_DATE
            )
        assert placer.submitted == []
        assert placer.find_calls == [], "the broker was queried about a finished order"

    async def test_a_reconcile_owned_order_is_not_resumed(self) -> None:
        """RECONCILE_REQUIRED deliberately cannot return to SUBMITTED: §8.3
        gives that order to the reconciliation loop, and a second owner is how
        it gets submitted twice."""
        placer, store = RecordingPlacer(), RecordingStore()
        cid = (
            _gateway(placer, store=store)
            .build_entry(_approved(), _rec(), trade_date=TRADE_DATE)
            .client_order_id
        )
        store.preexisting(cid, broker_order_id=None, status="RECONCILE_REQUIRED")

        with pytest.raises(IllegalTransitionError):
            await _gateway(placer, store=store).submit_entry(
                _approved(), _rec(), trade_date=TRADE_DATE
            )
        assert placer.submitted == []

    async def test_an_unmodelled_stored_status_is_refused(self) -> None:
        """No safe default exists. Treating an unreadable status as SUBMITTING
        would let a corrupted or hand-edited row be adopted as in-flight."""
        placer, store = RecordingPlacer(), RecordingStore()
        cid = (
            _gateway(placer, store=store)
            .build_entry(_approved(), _rec(), trade_date=TRADE_DATE)
            .client_order_id
        )
        store.preexisting(cid, broker_order_id=None, status="NOT_A_REAL_STATUS")

        with pytest.raises(GatewayError, match="not a modelled OrderStatus"):
            await _gateway(placer, store=store).submit_entry(
                _approved(), _rec(), trade_date=TRADE_DATE
            )
        assert placer.submitted == []

    async def test_the_refusal_does_not_echo_a_forged_status_verbatim(self) -> None:
        """The stored status is data, and a newline in it would forge a log
        line in the message this raises."""
        placer, store = RecordingPlacer(), RecordingStore()
        cid = (
            _gateway(placer, store=store)
            .build_entry(_approved(), _rec(), trade_date=TRADE_DATE)
            .client_order_id
        )
        forged = "OPEN" + chr(10) + "CRITICAL kill switch disarmed by operator"
        store.preexisting(cid, broker_order_id=None, status=forged)
        with pytest.raises(GatewayError) as excinfo:
            await _gateway(placer, store=store).submit_entry(
                _approved(), _rec(), trade_date=TRADE_DATE
            )
        assert chr(10) not in str(excinfo.value)

    async def test_a_mid_flight_row_is_still_resumed(self) -> None:
        """The control. SUBMITTING legally reaches SUBMITTED, so the recovery
        path E15-S02 built must still run — otherwise this check would have
        closed the hole by closing the feature."""
        placer, store = RecordingPlacer(), RecordingStore()
        request = _gateway(placer, store=store).build_entry(
            _approved(), _rec(), trade_date=TRADE_DATE
        )
        store.preexisting(request.client_order_id, broker_order_id=None, status="SUBMITTING")
        placer.landed(request, "ADOPTED-2")

        assert (
            await _gateway(placer, store=store).submit_entry(
                _approved(), _rec(), trade_date=TRADE_DATE
            )
            == "ADOPTED-2"
        )
        assert placer.submitted == []

    async def test_a_completed_row_still_short_circuits_before_the_check(self) -> None:
        """A row carrying a broker id is answered from the record without
        consulting the state machine at all: it was submitted, and the id it
        got is the honest answer whatever the order later became."""
        placer, store = RecordingPlacer(), RecordingStore()
        cid = (
            _gateway(placer, store=store)
            .build_entry(_approved(), _rec(), trade_date=TRADE_DATE)
            .client_order_id
        )
        store.preexisting(cid, broker_order_id="ALREADY-1", status="FILLED")

        assert (
            await _gateway(placer, store=store).submit_entry(
                _approved(), _rec(), trade_date=TRADE_DATE
            )
            == "ALREADY-1"
        )
        assert placer.submitted == []


@_ASYNC
class TestAc4TheBrokerIdIsRecordedAfterwards:
    async def test_a_successful_submission_attaches_the_id(self) -> None:
        placer, store = RecordingPlacer(), RecordingStore()
        broker_id = await _gateway(placer, store=store).submit_entry(
            _approved(), _rec(), trade_date=TRADE_DATE
        )
        cid = placer.submitted[0].client_order_id
        assert store.attaches == [(cid, broker_id)]
        assert store.rows[cid]["status"] == "SUBMITTED"

    async def test_a_failure_to_record_does_not_report_the_order_as_failed(self) -> None:
        """The subtle one, and the reason TX2 swallows. The order IS live. A
        raise here reads as 'submission failed', and the natural response to
        that is to submit again — the duplicate this story exists to prevent.
        A stale row is recoverable; a duplicate position is not."""
        placer, store = RecordingPlacer(), RecordingStore()
        store.fail_attach = RuntimeError("connection reset")
        assert (
            await _gateway(placer, store=store).submit_entry(
                _approved(), _rec(), trade_date=TRADE_DATE
            )
            == placer.broker_order_id
        )
        assert len(placer.submitted) == 1

    async def test_the_swallowed_failure_is_logged_loudly(self, caplog) -> None:
        """Swallowing without a record would be the fail-open this codebase
        keeps finding."""
        import logging as _logging

        placer, store = RecordingPlacer(), RecordingStore()
        store.fail_attach = RuntimeError("connection reset")
        with caplog.at_level(_logging.ERROR):
            await _gateway(placer, store=store).submit_entry(
                _approved(), _rec(), trade_date=TRADE_DATE
            )
        assert any("LANDED" in r.message or "LANDED" in r.getMessage() for r in caplog.records)


@_ASYNC
class TestAc5TheOrdinaryPathIsStillOrdinary:
    """Without this, a gateway that refused everything would satisfy AC1-AC4."""

    async def test_persists_once_submits_once_attaches_once(self) -> None:
        placer, store = RecordingPlacer(), RecordingStore()
        await _gateway(placer, store=store).submit_entry(_approved(), _rec(), trade_date=TRADE_DATE)
        assert len(store.inserts) == 1
        assert len(placer.submitted) == 1
        assert len(store.attaches) == 1
        assert placer.find_calls == [], "the recovery query ran on a healthy submission"

    async def test_a_gateway_cannot_be_built_without_a_store(self) -> None:
        """The structural half of AC1: there is no configuration in which
        orders are placed untracked, because such a gateway cannot exist."""
        with pytest.raises(TypeError, match="store"):
            OrderGateway(RecordingPlacer(), policy=_policy())  # type: ignore[call-arg]


@_ASYNC
class TestTheChaosScenario:
    """The story's original acceptance criterion, stated as a scenario:
    a timeout and a reconnect produce exactly one order."""

    async def test_timeout_then_reconnect_produces_exactly_one_order(self) -> None:
        placer, store = RecordingPlacer(), RecordingStore()
        request = _gateway(placer, store=store).build_entry(
            _approved(), _rec(), trade_date=TRADE_DATE
        )

        # The submission times out. It DID land — the response was lost, not
        # the order, which is the case blind retry gets wrong.
        placer.fail_with = AmbiguousOrderError("read timed out")
        placer.landed(request, "SURVIVOR-1")
        first = await _gateway(placer, store=store).submit_entry(
            _approved(), _rec(), trade_date=TRADE_DATE
        )

        # Reconnect: a fresh gateway, the same store, the same decision.
        reconnected = RecordingPlacer()
        reconnected.orderbook = placer.orderbook
        second = await _gateway(reconnected, store=store).submit_entry(
            _approved(), _rec(), trade_date=TRADE_DATE
        )

        assert first == second == "SURVIVOR-1"
        assert len(placer.submitted) == 1
        assert reconnected.submitted == [], "the reconnect placed a second order"
        assert len(store.inserts) == 1
