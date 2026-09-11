"""Protective stop attachment (E15-S04), one class per acceptance criterion.

The invariant under test is the fifth in CLAUDE.md — every position has a stop —
and the thing that makes it hard is that the dangerous state is *transient*: a
position is naked only between the entry filling and the stop going live. No
database row shows it, because BR-1 makes ``stop_price`` NOT NULL and it is
populated from the sizer whether or not any order is protecting anything.

So every test here asserts on what reached the BROKER, or on what the halt
controller was told, rather than on the state of a record.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from decimal import Decimal

import pytest

from algotrader.common.enums import Direction, OrderIntent, OrderStatus, OrderType, Side
from algotrader.common.models.trading import Order, Position
from algotrader.execution.halt import HaltReason
from algotrader.execution.protective_stop import (
    EstablishedPosition,
    NakedPositionError,
    ProtectiveStop,
    StopNotEstablishedError,
    _exit_cid,
    is_live,
    stop_client_order_id,
)

_ASYNC = pytest.mark.asyncio

NOW = dt.datetime(2026, 8, 25, 6, 30, tzinfo=dt.UTC)
TRADE_DATE = dt.date(2026, 8, 25)


def _position(
    direction: Direction = Direction.LONG,
    quantity: int = 83,
    symbol: str = "INFY",
) -> Position:
    """A filled position. Long stops sit below entry, short stops above —
    ``Position`` refuses anything else, so this cannot build a nonsense one."""
    entry = Decimal("1200.00")
    stop = Decimal("1179.75") if direction is Direction.LONG else Decimal("1220.25")
    return Position(
        correlation_id=uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
        symbol=symbol,
        slot_index=0,
        direction=direction,
        quantity=quantity,
        entry_price=entry,
        stop_price=stop,
        opened_at=NOW,
        squareoff_deadline=dt.datetime(2026, 8, 25, 9, 40, tzinfo=dt.UTC),
    )


def _order(cid: str, status: OrderStatus = OrderStatus.OPEN, symbol: str = "INFY") -> Order:
    return Order(
        client_order_id=cid,
        broker_order_id="260825000777",
        correlation_id=uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
        symbol=symbol,
        side=Side.SELL,
        order_type=OrderType.SLM,
        product="MIS",
        quantity=83,
        status=status,
        intent=OrderIntent.STOP,
        placed_at=NOW,
        last_update_at=NOW,
    )


class FakeGateway:
    """Records what was submitted; fails on demand.

    ``orderbook`` is what ``find_order`` returns, and it is populated
    SEPARATELY from ``submit_stop`` succeeding — that separation is the whole
    subject of AC3. A broker that accepts an order and does not list it is not
    a contrived case; it is the case the story's build concern names.
    """

    def __init__(self) -> None:
        self.stops: list[Position] = []
        self.exits: list[Position] = []
        self.exit_orders: list = []
        self.orderbook: dict[str, Order] = {}
        self.stop_fails_with: Exception | None = None
        self.exit_fails_with: Exception | None = None
        self.auto_list = True
        self.auto_list_exit = True

    async def submit_stop(self, position: Position, *, trade_date: dt.date) -> str:
        self.stops.append(position)
        if self.stop_fails_with is not None:
            raise self.stop_fails_with
        if self.auto_list:
            # Derived through the REAL gateway builder, not through
            # `stop_client_order_id`. Using the same helper the attacher uses
            # made the double self-consistent: flipping the side inside that
            # helper changed both the key written here and the key looked up,
            # so the error cancelled out and the mutation survived (M12).
            cid = _real_gateway().build_stop(position, trade_date=trade_date).client_order_id
            self.orderbook[cid] = _order(cid, symbol=position.symbol)
        return "STOP-BROKER-1"

    async def submit_exit(self, position: Position, *, trade_date: dt.date) -> str:
        self.exits.append(position)
        if self.exit_fails_with is not None:
            raise self.exit_fails_with
        # A real broker LISTS an order it accepted, and the attacher verifies
        # the exit exactly as it verifies the stop. The double did not list it
        # at first, which made every contained failure look like a naked
        # position — the double was wrong, not the code.
        if self.auto_list_exit:
            built = _real_gateway().build_exit(position, trade_date=trade_date)
            self.exit_orders.append(built)
            self.orderbook[built.client_order_id] = _order(
                built.client_order_id, symbol=position.symbol
            )
        return "EXIT-BROKER-1"

    async def find_order(self, client_order_id_: str) -> Order | None:
        return self.orderbook.get(client_order_id_)


class FakeHalter:
    def __init__(self) -> None:
        self.armed: list[tuple[HaltReason, str, str]] = []

    async def arm(self, reason, *, detail: str, armed_by: str, now: dt.datetime) -> object:
        self.armed.append((reason, detail, armed_by))
        return object()


class Counter:
    def __init__(self) -> None:
        self.count = 0

    def inc(self) -> None:
        self.count += 1


class FakeMetrics:
    def __init__(self) -> None:
        self.stop_attach_failures_total = Counter()
        self.naked_positions_total = Counter()


def _attacher(gateway=None, halter=None, metrics=None):
    return ProtectiveStop(
        gateway or FakeGateway(),
        halter=halter or FakeHalter(),
        metrics=metrics,
    )


def _real_gateway():
    """A REAL OrderGateway over doubles, for asserting on built orders.

    Half-constructing one with ``__new__`` and poking ``_policy`` in would
    test a shape the constructor can never produce, and would keep passing if
    the constructor started doing something the builders depend on.
    """
    from algotrader.execution.gateway import GatewayPolicy, OrderGateway

    class _NoPlacer:
        async def place_order(self, request):  # pragma: no cover - never called
            raise AssertionError("build-only gateway was asked to submit")

        async def find_by_client_order_id(self, cid):  # pragma: no cover
            return None

    class _NoStore:
        async def insert_submitting(self, order):  # pragma: no cover
            return 1

        async def attach_broker_id(self, cid, bid):  # pragma: no cover
            return None

        async def mark_rejected(self, cid, *, reason):  # pragma: no cover
            return None

        async def find_by_client_order_id(self, cid):  # pragma: no cover
            return None

    return OrderGateway(
        _NoPlacer(),
        policy=GatewayPolicy(algo_id=None, market_protection=Decimal("-1")),
        store=_NoStore(),
    )


@_ASYNC
class TestAc1TheStopMatchesThePosition:
    async def test_a_long_is_protected_by_a_sell(self) -> None:
        gateway = FakeGateway()
        await _attacher(gateway).attach(_position(), trade_date=TRADE_DATE, now=NOW)
        assert len(gateway.stops) == 1
        assert gateway.stops[0].direction is Direction.LONG

    async def test_the_order_is_slm_at_the_positions_stop_price(self) -> None:
        """Built through the real gateway builder, because the ORDER is what
        reaches the broker and the position is only its input."""
        position = _position()
        request = _real_gateway().build_stop(position, trade_date=TRADE_DATE)

        assert request.order_type is OrderType.SLM
        assert request.side is Side.SELL
        assert request.trigger_price == position.stop_price
        assert request.quantity == position.quantity
        assert request.intent is OrderIntent.STOP

    async def test_a_short_is_protected_by_a_buy(self) -> None:
        request = _real_gateway().build_stop(
            _position(direction=Direction.SHORT), trade_date=TRADE_DATE
        )
        assert request.side is Side.BUY
        assert request.trigger_price == Decimal("1220.25")

    async def test_an_slm_stop_carries_market_protection(self) -> None:
        """Zerodha rejects SL-M without it since 1 Apr 2026 — a stop that the
        broker refuses is a position with no stop at all. ``OrderRequest``
        enforces this at construction, so this asserts the enforcement is
        actually reached rather than that we remembered to pass it."""
        assert _real_gateway().build_stop(
            _position(), trade_date=TRADE_DATE
        ).market_protection == Decimal("-1")

    async def test_the_stop_id_is_distinct_from_the_entrys(self) -> None:
        """Same correlation_id, different intent. If they collided, the stop
        would be suppressed as a duplicate of the entry and never placed —
        silently, by the idempotency check that exists to help."""
        from algotrader.execution.gateway import client_order_id

        position = _position()
        entry = client_order_id(
            correlation_id=position.correlation_id,
            symbol=position.symbol,
            side=Side.BUY,
            intent=OrderIntent.ENTRY,
            trade_date=TRADE_DATE,
        )
        assert stop_client_order_id(position, trade_date=TRADE_DATE) != entry

    async def test_the_stop_id_is_derivable_from_the_position_alone(self) -> None:
        """What makes a schema column unnecessary: reconciliation can compute
        this having never seen the order placed."""
        first = stop_client_order_id(_position(), trade_date=TRADE_DATE)
        second = stop_client_order_id(_position(), trade_date=TRADE_DATE)
        assert first == second


@_ASYNC
class TestAc2AFailedStopClosesThePosition:
    async def test_a_refused_stop_closes_at_market(self) -> None:
        gateway = FakeGateway()
        gateway.stop_fails_with = RuntimeError("broker said no")
        with pytest.raises(StopNotEstablishedError):
            await _attacher(gateway).attach(_position(), trade_date=TRADE_DATE, now=NOW)
        assert len(gateway.exits) == 1, "the position was left open"

    async def test_the_close_happens_before_the_error_is_raised(self) -> None:
        """A caller that swallows the exception must still find the position
        closed. Ordering is the property; the end state is the same either
        way, which is exactly why it needs asserting separately."""
        order: list[str] = []

        class OrderingGateway(FakeGateway):
            async def submit_exit(self, position, *, trade_date):
                order.append("exit")
                return await super().submit_exit(position, trade_date=trade_date)

        gateway = OrderingGateway()
        gateway.stop_fails_with = RuntimeError("nope")
        try:
            await _attacher(gateway).attach(_position(), trade_date=TRADE_DATE, now=NOW)
        except StopNotEstablishedError:
            order.append("raised")
        assert order == ["exit", "raised"]

    async def test_an_unknown_outcome_also_closes(self) -> None:
        """An ambiguous stop submission is not a stop. The response to "we do
        not know whether the position is protected" is the same as the response
        to "it is not" — anything else is guessing in the direction of leaving
        a position naked."""
        from algotrader.broker.adapter import AmbiguousOrderError

        gateway = FakeGateway()
        gateway.stop_fails_with = AmbiguousOrderError("timed out")
        with pytest.raises(StopNotEstablishedError):
            await _attacher(gateway).attach(_position(), trade_date=TRADE_DATE, now=NOW)
        assert len(gateway.exits) == 1

    async def test_the_exit_closes_the_whole_position(self) -> None:
        """Asserted on the ORDER, not on the position handed to the gateway.

        The first version checked `gateway.exits[0].quantity`, which is the
        POSITION that went in - so a builder that halved the quantity on the
        way out was invisible, and the mutation survived (M16). The order is
        what reaches the broker; the position is only its input.
        """
        gateway = FakeGateway()
        gateway.stop_fails_with = RuntimeError("no")
        position = _position(quantity=41)
        with pytest.raises(StopNotEstablishedError):
            await _attacher(gateway).attach(position, trade_date=TRADE_DATE, now=NOW)
        assert gateway.exit_orders[0].quantity == position.quantity == 41

    async def test_the_exit_is_a_market_order_on_the_opposite_side(self) -> None:
        gateway = FakeGateway()
        gateway.stop_fails_with = RuntimeError("no")
        with pytest.raises(StopNotEstablishedError):
            await _attacher(gateway).attach(_position(), trade_date=TRADE_DATE, now=NOW)
        sent = gateway.exit_orders[0]
        assert sent.order_type is OrderType.MARKET
        assert sent.side is Side.SELL
        assert sent.intent is OrderIntent.SQUAREOFF


@_ASYNC
class TestAc3AcceptanceIsNotLiveness:
    async def test_a_stop_the_broker_will_not_list_is_not_live(self) -> None:
        """The build concern's first half. ``submit_stop`` returned a broker
        id; the orderbook does not have it. Submitted, not protected."""
        gateway = FakeGateway()
        gateway.auto_list = False
        with pytest.raises(StopNotEstablishedError, match="absent from the orderbook"):
            await _attacher(gateway).attach(_position(), trade_date=TRADE_DATE, now=NOW)
        assert len(gateway.exits) == 1

    @pytest.mark.parametrize("status", [OrderStatus.REJECTED, OrderStatus.CANCELLED])
    async def test_a_dead_stop_is_not_live(self, status: OrderStatus) -> None:
        gateway = FakeGateway()
        gateway.auto_list = False
        cid = stop_client_order_id(_position(), trade_date=TRADE_DATE)
        gateway.orderbook[cid] = _order(cid, status=status)
        with pytest.raises(StopNotEstablishedError, match=status.value):
            await _attacher(gateway).attach(_position(), trade_date=TRADE_DATE, now=NOW)
        assert len(gateway.exits) == 1

    @pytest.mark.parametrize(
        "status", [OrderStatus.OPEN, OrderStatus.SUBMITTED, OrderStatus.FILLED]
    )
    async def test_a_working_or_pending_stop_counts_as_live(self, status: OrderStatus) -> None:
        """SUBMITTED counts deliberately: an order still validating has not
        been refused, and closing a healthy position on a normal broker delay
        would be a self-inflicted loss. FILLED counts because a triggered stop
        did its job."""
        assert is_live(_order("x" * 32, status=status))

    async def test_absence_is_not_live(self) -> None:
        assert not is_live(None)

    async def test_no_established_position_is_returned_when_not_live(self) -> None:
        """AC3 stated structurally: the caller gets an exception, not an object
        it might use."""
        gateway = FakeGateway()
        gateway.auto_list = False
        with pytest.raises(StopNotEstablishedError):
            await _attacher(gateway).attach(_position(), trade_date=TRADE_DATE, now=NOW)


@_ASYNC
class TestAc4AFailedCloseHaltsTheSession:
    async def test_the_halt_is_armed(self) -> None:
        gateway, halter = FakeGateway(), FakeHalter()
        gateway.stop_fails_with = RuntimeError("no stop")
        gateway.exit_fails_with = RuntimeError("no exit either")
        with pytest.raises(NakedPositionError):
            await _attacher(gateway, halter).attach(_position(), trade_date=TRADE_DATE, now=NOW)
        assert len(halter.armed) == 1

    async def test_it_halts_for_the_right_reason(self) -> None:
        """NAKED_POSITION, not UNKNOWN_POSITION. Borrowing the nearest
        plausible neighbour is how HEALTH_GATE_FAILED came to mean four
        different things here, and the metric label is the whole value of a
        halt reason."""
        gateway, halter = FakeGateway(), FakeHalter()
        gateway.stop_fails_with = RuntimeError("no stop")
        gateway.exit_fails_with = RuntimeError("no exit")
        with pytest.raises(NakedPositionError):
            await _attacher(gateway, halter).attach(_position(), trade_date=TRADE_DATE, now=NOW)
        reason, detail, armed_by = halter.armed[0]
        assert reason is HaltReason.NAKED_POSITION
        assert "INFY" in detail
        assert armed_by == "protective-stop"

    async def test_the_halt_is_armed_before_the_error_is_raised(self) -> None:
        """A caller that catches NakedPositionError must not be able to leave
        the system trading. Arm first, raise second."""
        events: list[str] = []

        class OrderingHalter(FakeHalter):
            async def arm(self, reason, *, detail, armed_by, now):
                events.append("armed")
                return await super().arm(reason, detail=detail, armed_by=armed_by, now=now)

        gateway, halter = FakeGateway(), OrderingHalter()
        gateway.stop_fails_with = RuntimeError("no")
        gateway.exit_fails_with = RuntimeError("no")
        try:
            await _attacher(gateway, halter).attach(_position(), trade_date=TRADE_DATE, now=NOW)
        except NakedPositionError:
            events.append("raised")
        assert events == ["armed", "raised"]

    async def test_a_successful_close_does_not_halt(self) -> None:
        """The boundary. A stop failure that IS contained must not stop the
        day — halting on every stop rejection would make one bad symbol end
        trading."""
        gateway, halter = FakeGateway(), FakeHalter()
        gateway.stop_fails_with = RuntimeError("no stop")
        with pytest.raises(StopNotEstablishedError):
            await _attacher(gateway, halter).attach(_position(), trade_date=TRADE_DATE, now=NOW)
        assert halter.armed == []

    async def test_an_exit_the_broker_will_not_list_also_halts(self) -> None:
        """QA-E15-11. Acceptance is not liveness — the claim this whole module
        rests on — and it applies to the EXIT exactly as it applies to the
        stop. The first version of this code trusted the exit's acceptance and
        logged "closed at market" having verified nothing, which is the same
        defect it was written to prevent, one component further along."""
        gateway, halter = FakeGateway(), FakeHalter()
        gateway.stop_fails_with = RuntimeError("no stop")
        gateway.auto_list_exit = False
        with pytest.raises(NakedPositionError):
            await _attacher(gateway, halter).attach(_position(), trade_date=TRADE_DATE, now=NOW)
        assert len(gateway.exits) == 1, "the exit was submitted"
        assert halter.armed[0][0] is HaltReason.NAKED_POSITION

    async def test_a_rejected_exit_halts(self) -> None:
        gateway, halter = FakeGateway(), FakeHalter()
        gateway.stop_fails_with = RuntimeError("no stop")
        gateway.auto_list_exit = False
        cid = _exit_cid(_position(), TRADE_DATE)
        gateway.orderbook[cid] = _order(cid, status=OrderStatus.REJECTED)
        with pytest.raises(NakedPositionError):
            await _attacher(gateway, halter).attach(_position(), trade_date=TRADE_DATE, now=NOW)
        assert len(halter.armed) == 1

    async def test_a_naive_timestamp_is_refused(self) -> None:
        """Which session a naked position belongs to must be unambiguous."""
        with pytest.raises(ValueError, match="naive"):
            await _attacher().attach(
                _position(), trade_date=TRADE_DATE, now=dt.datetime(2026, 8, 25, 6, 30)
            )


@_ASYNC
class TestAc5FailuresAreVisibleWithoutADatabase:
    async def test_a_stop_failure_is_logged_at_critical(self, caplog) -> None:
        gateway = FakeGateway()
        gateway.stop_fails_with = RuntimeError("broker said no")
        with caplog.at_level(logging.CRITICAL):
            with pytest.raises(StopNotEstablishedError):
                await _attacher(gateway).attach(_position(), trade_date=TRADE_DATE, now=NOW)
        assert any("NAKED POSITION" in r.getMessage() for r in caplog.records)

    async def test_a_failed_close_is_logged_at_critical_too(self, caplog) -> None:
        gateway = FakeGateway()
        gateway.stop_fails_with = RuntimeError("no")
        gateway.exit_fails_with = RuntimeError("no")
        with caplog.at_level(logging.CRITICAL):
            with pytest.raises(NakedPositionError):
                await _attacher(gateway).attach(_position(), trade_date=TRADE_DATE, now=NOW)
        assert any("COULD NOT BE CLOSED" in r.getMessage() for r in caplog.records)

    async def test_the_counters_move(self) -> None:
        gateway, metrics = FakeGateway(), FakeMetrics()
        gateway.stop_fails_with = RuntimeError("no")
        gateway.exit_fails_with = RuntimeError("no")
        with pytest.raises(NakedPositionError):
            await _attacher(gateway, metrics=metrics).attach(
                _position(), trade_date=TRADE_DATE, now=NOW
            )
        assert metrics.stop_attach_failures_total.count == 1
        assert metrics.naked_positions_total.count == 1

    async def test_a_broken_metrics_backend_does_not_worsen_the_failure(self) -> None:
        """This is called from the path whose job is to close a naked
        position. A metrics backend that is down must not turn a contained
        failure into an uncontained one."""

        class ExplodingCounter:
            def inc(self) -> None:
                raise RuntimeError("prometheus is down")

        class BadMetrics:
            stop_attach_failures_total = ExplodingCounter()
            naked_positions_total = ExplodingCounter()

        gateway = FakeGateway()
        gateway.stop_fails_with = RuntimeError("no stop")
        with pytest.raises(StopNotEstablishedError):
            await _attacher(gateway, metrics=BadMetrics()).attach(
                _position(), trade_date=TRADE_DATE, now=NOW
            )
        assert len(gateway.exits) == 1, "a metrics failure prevented the close"

    async def test_the_forged_detail_cannot_forge_a_log_line(self) -> None:
        """The broker's error text is untrusted and reaches a CRITICAL line
        and a halt record. A newline in it would forge an entry in the log an
        incident is reconstructed from."""
        gateway, halter = FakeGateway(), FakeHalter()
        gateway.stop_fails_with = RuntimeError("no")
        gateway.exit_fails_with = RuntimeError(
            "rejected" + chr(10) + "CRITICAL kill switch disarmed by operator"
        )
        with pytest.raises(NakedPositionError) as excinfo:
            await _attacher(gateway, halter).attach(_position(), trade_date=TRADE_DATE, now=NOW)
        assert chr(10) not in str(excinfo.value)
        assert chr(10) not in halter.armed[0][1]


@_ASYNC
class TestAc6TheReconciliationPredicate:
    async def test_a_live_stop_reports_protected(self) -> None:
        gateway = FakeGateway()
        position = _position()
        await _attacher(gateway).attach(position, trade_date=TRADE_DATE, now=NOW)
        assert await _attacher(gateway).is_protected(position, trade_date=TRADE_DATE)

    async def test_a_stop_rejected_after_attaching_reports_unprotected(self) -> None:
        """The build concern, exactly: accepted, verified live, and rejected a
        second later. No synchronous check can see this — only a later poll
        can, which is why E15-S09 must call this predicate."""
        gateway = FakeGateway()
        position = _position()
        attacher = _attacher(gateway)
        await attacher.attach(position, trade_date=TRADE_DATE, now=NOW)

        cid = stop_client_order_id(position, trade_date=TRADE_DATE)
        gateway.orderbook[cid] = _order(cid, status=OrderStatus.REJECTED)
        assert not await attacher.is_protected(position, trade_date=TRADE_DATE)

    async def test_a_stop_that_vanished_reports_unprotected(self) -> None:
        gateway = FakeGateway()
        position = _position()
        attacher = _attacher(gateway)
        await attacher.attach(position, trade_date=TRADE_DATE, now=NOW)
        gateway.orderbook.clear()
        assert not await attacher.is_protected(position, trade_date=TRADE_DATE)

    async def test_it_needs_no_record_of_the_attachment(self) -> None:
        """A fresh attacher, which never placed the stop, answers correctly —
        which is what lets a reconciliation loop use it after a restart."""
        gateway = FakeGateway()
        position = _position()
        await _attacher(gateway).attach(position, trade_date=TRADE_DATE, now=NOW)
        assert await ProtectiveStop(gateway, halter=FakeHalter()).is_protected(
            position, trade_date=TRADE_DATE
        )


@_ASYNC
class TestAc7TheHealthyPathIsStillHealthy:
    """Without this, an implementation that closed every position immediately
    would satisfy AC2 through AC5."""

    async def test_one_stop_no_exit_no_halt(self) -> None:
        gateway, halter = FakeGateway(), FakeHalter()
        result = await _attacher(gateway, halter).attach(
            _position(), trade_date=TRADE_DATE, now=NOW
        )
        assert len(gateway.stops) == 1
        assert gateway.exits == []
        assert halter.armed == []
        assert isinstance(result, EstablishedPosition)

    async def test_the_result_names_the_verified_stop(self) -> None:
        gateway = FakeGateway()
        position = _position()
        result = await _attacher(gateway).attach(position, trade_date=TRADE_DATE, now=NOW)
        assert result.position is position
        assert result.stop_broker_order_id == "STOP-BROKER-1"
        assert result.stop_client_order_id == stop_client_order_id(position, trade_date=TRADE_DATE)
        assert result.established_at == NOW

    async def test_nothing_is_logged_at_critical_on_the_happy_path(self, caplog) -> None:
        """A CRITICAL line per successful entry would make the level useless
        for the case it exists to signal."""
        with caplog.at_level(logging.CRITICAL):
            await _attacher().attach(_position(), trade_date=TRADE_DATE, now=NOW)
        assert [r for r in caplog.records if r.levelno >= logging.CRITICAL] == []


class TestTheCountersExistOnTheRealMetrics:
    """`_count` reads by name with getattr and returns quietly if absent.

    That keeps a metrics outage from worsening a naked-position failure, and
    it also means a TYPO in the counter name would silently count nothing —
    "configured but binding on nothing", which this repository has already
    found twice (the two exposure caps, and the special-sessions flag). The
    names are asserted against the real registry rather than a double.
    """

    def test_both_counters_are_registered(self) -> None:
        from algotrader.common.metrics import Metrics

        metrics = Metrics()
        assert metrics.stop_attach_failures_total is not None
        assert metrics.naked_positions_total is not None

    def test_the_attacher_increments_the_real_ones(self) -> None:
        import asyncio

        from algotrader.common.metrics import Metrics

        metrics = Metrics()
        gateway = FakeGateway()
        gateway.stop_fails_with = RuntimeError("no stop")
        gateway.exit_fails_with = RuntimeError("no exit")

        with pytest.raises(NakedPositionError):
            asyncio.run(
                _attacher(gateway, metrics=metrics).attach(
                    _position(), trade_date=TRADE_DATE, now=NOW
                )
            )
        samples = {
            m.name: m.samples[0].value
            for m in metrics.registry.collect()
            if m.name in {"stop_attach_failures", "naked_positions"}
        }
        assert samples == {"stop_attach_failures": 1.0, "naked_positions": 1.0}


class TestSnappingNeverWidensAProtectiveStop:
    """A property of `round_to_tick` that this story now depends on.

    The rule was written for limit ENTRIES — "never pay more than intended" —
    and it happens to mean "never risk more than budgeted" for stops, on both
    sides. That is a happy accident, which makes it exactly the kind of thing
    someone later "fixes". Locked down here so the fix fails a test instead of
    quietly widening every stop in the system.
    """

    @pytest.mark.parametrize("raw", ["1179.71", "1179.73", "1179.77", "1179.79"])
    def test_a_long_stop_only_moves_closer_to_entry(self, raw: str) -> None:
        from algotrader.broker.kite.mapping import round_to_tick

        entry = Decimal("1200.00")
        snapped = round_to_tick(Decimal(raw), Decimal("0.05"), side=Side.SELL)
        assert entry - snapped <= entry - Decimal(raw)

    @pytest.mark.parametrize("raw", ["1220.21", "1220.23", "1220.27", "1220.29"])
    def test_a_short_stop_only_moves_closer_to_entry(self, raw: str) -> None:
        from algotrader.broker.kite.mapping import round_to_tick

        entry = Decimal("1200.00")
        snapped = round_to_tick(Decimal(raw), Decimal("0.05"), side=Side.BUY)
        assert snapped - entry <= Decimal(raw) - entry
