"""E15-S09: the reconciliation cycle, one acceptance criterion at a time.

The doubles answer from configured facts - "the broker lists this stop live",
"this position exists" - never by calling the reconciler's own helpers.
QA-E15-12's lesson: a double that shares an implementation with the code under
test agrees with it by construction.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from algotrader.broker.adapter import BrokerPosition
from algotrader.common.audit import AuditEntry
from algotrader.common.enums import (
    Direction,
    OrderIntent,
    OrderStatus,
    OrderType,
    Product,
    Side,
)
from algotrader.common.metrics import get_metrics, reset_metrics_for_testing
from algotrader.common.models.trading import Order, Position
from algotrader.execution import reconciliation as rec
from algotrader.execution.halt import HaltReason
from algotrader.execution.positions import (
    ExitingPosition,
    PositionError,
    PositionManager,
    ProtectedPosition,
    UnverifiedPosition,
)
from algotrader.execution.protective_stop import EstablishedPosition
from algotrader.execution.reconciliation import (
    Drift,
    Reconciler,
    ReconciliationLoop,
    ReconciliationReadError,
    broker_target,
    decide_order,
    find_unknown,
)

# 10:30 IST on an ordinary Tuesday.
NOW = dt.datetime(2026, 8, 25, 5, 0, tzinfo=dt.UTC)
DEADLINE = dt.datetime(2026, 8, 25, 9, 45, tzinfo=dt.UTC)
GRACE = dt.timedelta(seconds=60)


@pytest.fixture(autouse=True)
def _fresh_metrics() -> None:
    reset_metrics_for_testing()


def _cid() -> str:
    return uuid4().hex


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class FakeBroker:
    """The broker's two reads, recorded in the order they happen."""

    def __init__(self) -> None:
        self.positions: list[BrokerPosition] = []
        self.orderbook: list[Order] = []
        self.calls: list[str] = []
        self.fail_on: str | None = None

    async def fetch_positions(self) -> list[BrokerPosition]:
        self.calls.append("positions")
        if self.fail_on == "positions":
            raise ConnectionError("positions read timed out")
        return list(self.positions)

    async def fetch_orderbook(self) -> list[Order]:
        self.calls.append("orderbook")
        if self.fail_on == "orderbook":
            raise ConnectionError("orderbook read timed out")
        return list(self.orderbook)

    def order_key(self, client_order_id: str) -> str:
        return client_order_id[:20]


class FakeLedger:
    """Our order rows. Records every write and applies it, like the table."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.writes: list[dict[str, Any]] = []
        self.fail = False

    async def orders_placed_since(self, since: dt.datetime) -> list[dict[str, Any]]:
        if self.fail:
            raise RuntimeError("database unavailable")
        return [dict(r) for r in self.rows]

    async def apply_broker_state(self, client_order_id: str, **kwargs: Any) -> None:
        self.writes.append({"client_order_id": client_order_id, **kwargs})
        for row in self.rows:
            if row["client_order_id"] == client_order_id:
                row["status"] = kwargs["status"]
                row["filled_quantity"] = kwargs["filled_quantity"]
                row["average_price"] = kwargs["average_price"]
                if kwargs["broker_order_id"] is not None:
                    row["broker_order_id"] = kwargs["broker_order_id"]


class FakeProtector:
    """Answers protection questions from configured facts, and records exits."""

    def __init__(self) -> None:
        self.live_stops: set[str] = set()
        self.live_exits: set[str] = set()
        self.exits: list[tuple[Any, str]] = []
        self.exit_raises: dict[str, Exception] = {}
        self.adopted: list[str] = []

    async def is_protected(self, position: Position, *, trade_date: dt.date) -> bool:
        return position.symbol in self.live_stops

    async def exit_is_live(self, target: Any, *, trade_date: dt.date) -> bool:
        return target.symbol in self.live_exits

    async def adopt(
        self, position: Position, *, trade_date: dt.date, now: dt.datetime
    ) -> EstablishedPosition | None:
        if position.symbol not in self.live_stops:
            return None
        self.adopted.append(position.symbol)
        return EstablishedPosition(
            position=position,
            stop_client_order_id="STOP" + position.symbol,
            stop_broker_order_id="BROKER-STOP-" + position.symbol,
            established_at=now,
        )

    async def exit_now(
        self, target: Any, *, trade_date: dt.date, now: dt.datetime, because: str
    ) -> None:
        self.exits.append((target, because))
        if target.symbol in self.exit_raises:
            raise self.exit_raises[target.symbol]


class RecordingHalter:
    def __init__(self) -> None:
        self.armed: list[tuple[HaltReason, str]] = []

    async def arm(self, reason, *, detail, armed_by, now):
        self.armed.append((reason, detail))
        return object()


class _Store:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []

    async def open_position(self, position: dict[str, Any]) -> int:
        return 1

    async def open_positions(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.rows]

    async def close_position(self, position_id: int, **kwargs: Any) -> None:
        pass


class _Mirror:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def write(self, symbol: str, snapshot: Any) -> None:
        self.data[symbol] = snapshot

    async def clear(self, symbol: str) -> None:
        self.data.pop(symbol, None)


def _manager(rows: list[dict[str, Any]] | None = None) -> tuple[PositionManager, _Mirror]:
    mirror = _Mirror()
    manager = PositionManager(
        protector=None,  # type: ignore[arg-type]  # never used by these tests
        store=_Store(rows),
        mirror=mirror,
        calendar=None,
    )
    return manager, mirror


def _reconciler(
    *,
    broker: FakeBroker | None = None,
    ledger: FakeLedger | None = None,
    manager: PositionManager | None = None,
    protector: FakeProtector | None = None,
    halter: RecordingHalter | None = None,
    audit: Any = None,
) -> tuple[Reconciler, FakeBroker, FakeLedger, FakeProtector, RecordingHalter]:
    broker = broker or FakeBroker()
    ledger = ledger or FakeLedger()
    protector = protector or FakeProtector()
    halter = halter or RecordingHalter()
    reconciler = Reconciler(
        broker=broker,
        ledger=ledger,
        manager=manager or _manager()[0],
        protector=protector,  # type: ignore[arg-type]
        halter=halter,
        audit=audit,
        metrics=get_metrics(),
    )
    return reconciler, broker, ledger, protector, halter


def _row(
    cid: str,
    *,
    status: str = "SUBMITTED",
    side: str = "BUY",
    quantity: int = 40,
    filled: int = 0,
    intent: str = "ENTRY",
    symbol: str = "INFY",
    correlation: UUID | None = None,
    placed_at: dt.datetime = NOW - dt.timedelta(minutes=5),
    broker_order_id: str | None = "B-1",
) -> dict[str, Any]:
    return {
        "client_order_id": cid,
        "broker_order_id": broker_order_id,
        "correlation_id": correlation or uuid4(),
        "symbol": symbol,
        "side": side,
        "order_type": "MARKET",
        "product": "MIS",
        "quantity": quantity,
        "status": status,
        "filled_quantity": filled,
        "average_price": None,
        "intent": intent,
        "placed_at": placed_at,
        "last_update_at": placed_at,
    }


def _broker_order(
    cid: str,
    *,
    status: OrderStatus = OrderStatus.OPEN,
    side: Side = Side.BUY,
    quantity: int = 40,
    filled: int = 0,
    price: str | None = None,
    symbol: str = "INFY",
    broker_order_id: str = "B-1",
) -> Order:
    return Order(
        client_order_id=cid[:20],
        broker_order_id=broker_order_id,
        correlation_id=uuid4(),
        symbol=symbol,
        side=side,
        order_type=OrderType.MARKET,
        product=Product.MIS,
        quantity=quantity,
        status=status,
        filled_quantity=filled,
        average_price=Decimal(price) if price else None,
        intent=OrderIntent.ENTRY,
        placed_at=NOW,
        last_update_at=NOW,
    )


def _pos(
    symbol: str = "INFY",
    *,
    quantity: int = 0,
    bought: int = 0,
    sold: int = 0,
    product: str = "MIS",
    exchange: str = "NSE",
) -> BrokerPosition:
    return BrokerPosition(
        symbol=symbol,
        exchange=exchange,
        product=product,
        quantity=quantity,
        day_buy_quantity=bought,
        day_sell_quantity=sold,
    )


def _position(symbol: str = "INFY", *, correlation: UUID | None = None) -> Position:
    return Position(
        position_id=7,
        correlation_id=correlation or uuid4(),
        symbol=symbol,
        slot_index=0,
        direction=Direction.LONG,
        quantity=40,
        entry_price=Decimal("1200.0000"),
        stop_price=Decimal("1186.4500"),
        opened_at=NOW,
        squareoff_deadline=DEADLINE,
    )


def _counter(name: str, **labels: str) -> float:
    for metric in get_metrics().registry.collect():
        for sample in metric.samples:
            if sample.name == name and all(sample.labels.get(k) == v for k, v in labels.items()):
                return sample.value
    return 0.0


def _run(reconciler: Reconciler, now: dt.datetime = NOW) -> rec.CycleReport:
    return asyncio.run(reconciler.run_cycle(now=now))


# ---------------------------------------------------------------------------
# AC1 — an unknown position halts within one cycle
# ---------------------------------------------------------------------------


class TestAc1UnknownPositionsHalt:
    def test_a_position_nobody_here_ordered_halts_in_one_cycle(self) -> None:
        reconciler, broker, _l, _p, halter = _reconciler()
        broker.positions = [_pos("WIPRO", quantity=25, bought=25)]
        report = _run(reconciler)
        assert report.halted
        assert [r for r, _ in halter.armed] == [HaltReason.UNKNOWN_POSITION]
        assert [u.symbol for u in report.unknown] == ["WIPRO"]
        assert [d.kind for d in report.drifts] == [Drift.UNKNOWN_POS]

    def test_extra_shares_on_a_symbol_we_also_hold_halt(self) -> None:
        """We bought 40. The broker says 140 were bought today."""
        cid = _cid()
        reconciler, broker, _l, _p, _h = _reconciler(
            ledger=FakeLedger([_row(cid, status="FILLED", filled=40)])
        )
        broker.orderbook = [_broker_order(cid, status=OrderStatus.FILLED, filled=40)]
        broker.positions = [_pos("INFY", quantity=140, bought=140)]
        report = _run(reconciler)
        assert report.halted
        assert report.unknown[0].foreign_buys == 100

    def test_a_foreign_sale_netting_against_ours_halts(self) -> None:
        """The dangerous one. Our book says long 40; someone sold 40 by hand; the
        broker is flat. Our stop, when it triggers, sells shares that are gone.
        Net quantity is ZERO here - a net-based check sees nothing wrong."""
        cid = _cid()
        reconciler, broker, _l, _p, _h = _reconciler(
            ledger=FakeLedger([_row(cid, status="FILLED", filled=40)])
        )
        broker.orderbook = [_broker_order(cid, status=OrderStatus.FILLED, filled=40)]
        broker.positions = [_pos("INFY", quantity=0, bought=40, sold=40)]
        report = _run(reconciler)
        assert report.halted
        assert report.unknown[0].foreign_sells == 40
        assert not report.unknown[0].is_open

    def test_an_mis_position_on_another_exchange_is_not_ours(self) -> None:
        """This system places orders on NSE only."""
        cid = _cid()
        reconciler, broker, _l, _p, _h = _reconciler(
            ledger=FakeLedger([_row(cid, status="FILLED", filled=40)])
        )
        broker.orderbook = [_broker_order(cid, status=OrderStatus.FILLED, filled=40)]
        broker.positions = [_pos("INFY", quantity=40, bought=40, exchange="BSE")]
        assert _run(reconciler).halted

    @pytest.mark.parametrize("product", ["CNC", "NRML", "MTF"])
    def test_products_this_system_never_trades_do_not_halt(self, product: str) -> None:
        """The control that keeps the check usable at all. The account is also a
        human's; a delivery holding must not stop the day."""
        reconciler, broker, _l, _p, halter = _reconciler()
        broker.positions = [_pos("HDFCBANK", quantity=10, bought=10, product=product)]
        report = _run(reconciler)
        assert not report.halted
        assert halter.armed == []

    def test_our_own_explained_position_does_not_halt(self) -> None:
        """The control for everything above."""
        cid = _cid()
        reconciler, broker, _l, _p, halter = _reconciler(
            ledger=FakeLedger([_row(cid, status="FILLED", filled=40)])
        )
        broker.orderbook = [_broker_order(cid, status=OrderStatus.FILLED, filled=40)]
        broker.positions = [_pos("INFY", quantity=40, bought=40)]
        assert not _run(reconciler).halted
        assert halter.armed == []

    def test_a_row_for_a_position_closed_today_is_not_a_position(self) -> None:
        """Kite keeps a closed intraday position in the list at quantity 0."""
        entry, exit_ = _cid(), _cid()
        corr = uuid4()
        reconciler, broker, _l, _p, _h = _reconciler(
            ledger=FakeLedger(
                [
                    _row(entry, status="FILLED", filled=40, correlation=corr),
                    _row(
                        exit_,
                        status="FILLED",
                        filled=40,
                        side="SELL",
                        intent="SQUAREOFF",
                        correlation=corr,
                    ),
                ]
            )
        )
        broker.orderbook = [
            _broker_order(entry, status=OrderStatus.FILLED, filled=40),
            _broker_order(exit_, status=OrderStatus.FILLED, filled=40, side=Side.SELL),
        ]
        broker.positions = [_pos("INFY", quantity=0, bought=40, sold=40)]
        assert not _run(reconciler).halted

    def test_the_unknown_alert_is_separate_from_the_halt(self, caplog) -> None:
        """The build concern: 'an unknown position exists' and 'the system halted'
        need different human responses on different clocks."""
        reconciler, broker, _l, _p, _h = _reconciler()
        broker.positions = [_pos("WIPRO", quantity=25, bought=25)]
        with caplog.at_level(logging.CRITICAL):
            _run(reconciler)
        lines = [r.getMessage() for r in caplog.records if r.levelno == logging.CRITICAL]
        assert any(line.startswith("UNKNOWN POSITION: NSE:WIPRO net +25 OPEN") for line in lines)
        assert any(line.startswith("SESSION HALTED by reconciliation") for line in lines)
        assert _counter("unknown_positions_total") == 1

    def test_the_halt_is_armed_before_anything_else_acts(self) -> None:
        """Order matters: the halt stops new entries; nothing later in the cycle
        should run before it."""
        order: list[str] = []

        class OrderedHalter(RecordingHalter):
            async def arm(self, reason, **kw):
                order.append("halt")
                return await super().arm(reason, **kw)

        class OrderedLedger(FakeLedger):
            async def apply_broker_state(self, client_order_id: str, **kw: Any) -> None:
                order.append("write")
                await super().apply_broker_state(client_order_id, **kw)

        cid = _cid()
        reconciler, broker, _l, _p, _h = _reconciler(
            ledger=OrderedLedger([_row(cid, status="SUBMITTED")]), halter=OrderedHalter()
        )
        broker.orderbook = [_broker_order(cid, status=OrderStatus.OPEN)]
        broker.positions = [_pos("WIPRO", quantity=25, bought=25)]
        _run(reconciler)
        assert order == ["halt", "write"]


# ---------------------------------------------------------------------------
# AC2 — a fill racing the two reads never produces a false halt
# ---------------------------------------------------------------------------


class TestAc2NoFalseHaltFromARace:
    def test_positions_are_read_before_the_orderbook(self) -> None:
        reconciler, broker, _l, _p, _h = _reconciler()
        _run(reconciler)
        assert broker.calls == ["positions", "orderbook"]

    def test_an_entry_fill_between_the_reads_is_not_unknown(self) -> None:
        """Positions read before the fill; the orderbook after it."""
        cid = _cid()
        reconciler, broker, _l, _p, halter = _reconciler(
            ledger=FakeLedger([_row(cid, status="SUBMITTED")])
        )
        broker.positions = []
        broker.orderbook = [_broker_order(cid, status=OrderStatus.FILLED, filled=40)]
        report = _run(reconciler)
        assert not report.halted
        assert halter.armed == []

    def test_an_exit_fill_between_the_reads_is_not_unknown(self) -> None:
        """The case a NET comparison gets wrong. Positions still show long 40;
        the orderbook already shows our exit filled. Net: broker +40, ours 0 -
        forty 'unknown' shares and a halted day on our own square-off. Per side:
        broker bought 40 (we bought 40), broker sold 0 (we sold 40) - nothing
        foreign on either side."""
        entry, exit_ = _cid(), _cid()
        corr = uuid4()
        reconciler, broker, _l, _p, _h = _reconciler(
            ledger=FakeLedger(
                [
                    _row(entry, status="FILLED", filled=40, correlation=corr),
                    _row(
                        exit_, status="SUBMITTED", side="SELL", intent="SQUAREOFF", correlation=corr
                    ),
                ]
            )
        )
        broker.positions = [_pos("INFY", quantity=40, bought=40, sold=0)]
        broker.orderbook = [
            _broker_order(entry, status=OrderStatus.FILLED, filled=40),
            _broker_order(exit_, status=OrderStatus.FILLED, filled=40, side=Side.SELL),
        ]
        report = _run(reconciler)
        assert not report.halted, "the day halted on its own exit"
        assert Drift.QTY_SHORT in [d.kind for d in report.drifts]

    def test_the_race_is_recorded_rather_than_ignored(self) -> None:
        cid = _cid()
        reconciler, broker, _l, _p, _h = _reconciler(
            ledger=FakeLedger([_row(cid, status="SUBMITTED")])
        )
        broker.positions = [_pos("INFY", quantity=10, bought=10)]
        broker.orderbook = [_broker_order(cid, status=OrderStatus.FILLED, filled=40)]
        report = _run(reconciler)
        assert [d.kind for d in report.drifts if d.kind is Drift.QTY_SHORT] == [Drift.QTY_SHORT]


# ---------------------------------------------------------------------------
# AC3 — every booked position checked every cycle
# ---------------------------------------------------------------------------


def _book(manager: PositionManager, tracked: Any) -> None:
    manager._book[tracked.position.symbol] = tracked


def _protected(symbol: str = "INFY") -> ProtectedPosition:
    position = _position(symbol)
    return ProtectedPosition(
        established=EstablishedPosition(
            position=position,
            stop_client_order_id="S",
            stop_broker_order_id="BS",
            established_at=NOW,
        )
    )


class TestAc3EveryPositionEveryCycle:
    def test_a_live_stop_is_left_alone(self) -> None:
        manager, _m = _manager()
        _book(manager, _protected("INFY"))
        reconciler, _b, _l, protector, _h = _reconciler(manager=manager)
        protector.live_stops = {"INFY"}
        report = _run(reconciler)
        assert protector.exits == []
        assert report.clean

    def test_a_stop_rejected_after_it_was_verified_is_exited(self) -> None:
        """The asynchronous case E15-S04 named and no synchronous check reaches."""
        manager, _m = _manager()
        _book(manager, _protected("INFY"))
        reconciler, _b, _l, protector, _h = _reconciler(manager=manager)
        report = _run(reconciler)
        assert [t.symbol for t, _ in protector.exits] == ["INFY"]
        assert Drift.UNPROTECTED in [d.kind for d in report.drifts]

    def test_every_position_is_checked_not_just_the_first(self) -> None:
        manager, _m = _manager()
        for sym in ("INFY", "TCS", "WIPRO"):
            _book(manager, _protected(sym))
        reconciler, _b, _l, protector, _h = _reconciler(manager=manager)
        protector.live_stops = {"TCS"}
        _run(reconciler)
        assert sorted(t.symbol for t, _ in protector.exits) == ["INFY", "WIPRO"]

    def test_one_failed_exit_does_not_stop_the_others(self) -> None:
        """exit_now arms the halt before raising; the loop records it and moves
        on, because the next position is no less naked."""
        manager, _m = _manager()
        for sym in ("INFY", "TCS"):
            _book(manager, _protected(sym))
        reconciler, _b, _l, protector, _h = _reconciler(manager=manager)
        protector.exit_raises = {"INFY": RuntimeError("exit refused")}
        report = _run(reconciler)
        assert sorted(t.symbol for t, _ in protector.exits) == ["INFY", "TCS"]
        assert any("INFY" in e for e in report.errors)

    def test_the_mirror_saying_protected_changes_nothing(self) -> None:
        """Protection comes from the broker, never the cache (E15-S05 constraint 9).
        The mirror here claims PROTECTED and the broker disagrees."""
        manager, mirror = _manager()
        tracked = _protected("INFY")
        _book(manager, tracked)
        mirror.data["INFY"] = {"protection": "ProtectedPosition"}
        reconciler, _b, _l, protector, _h = _reconciler(manager=manager)
        _run(reconciler)
        assert [t.symbol for t, _ in protector.exits] == ["INFY"]

    def test_an_exiting_position_with_a_live_exit_is_not_exited_again(self) -> None:
        """Calling exit_now every cycle would be idempotent and would log a
        CRITICAL naked-position line and count a failure every 30 seconds."""
        manager, _m = _manager()
        _book(manager, ExitingPosition(position=_position("INFY"), because="stop refused"))
        reconciler, _b, _l, protector, _h = _reconciler(manager=manager)
        protector.live_exits = {"INFY"}
        _run(reconciler)
        assert protector.exits == []

    def test_an_exiting_position_whose_exit_died_goes_back_to_the_exit_path(self) -> None:
        manager, _m = _manager()
        _book(manager, ExitingPosition(position=_position("INFY"), because="stop refused"))
        reconciler, _b, _l, protector, _h = _reconciler(manager=manager)
        _run(reconciler)
        assert [t.symbol for t, _ in protector.exits] == ["INFY"]


# ---------------------------------------------------------------------------
# AC4 — an unverified position is resolved from the broker
# ---------------------------------------------------------------------------


class TestAc4UnverifiedIsResolved:
    def test_a_restored_position_with_a_live_stop_becomes_protected(self) -> None:
        manager, mirror = _manager()
        position = _position("INFY")
        _book(manager, UnverifiedPosition(position=position))
        reconciler, _b, _l, protector, _h = _reconciler(manager=manager)
        protector.live_stops = {"INFY"}
        report = _run(reconciler)
        assert isinstance(manager.tracked("INFY"), ProtectedPosition)
        assert report.promoted == ["INFY"]
        assert mirror.data["INFY"].protection == "ProtectedPosition"
        assert protector.exits == []

    def test_a_restored_position_with_no_stop_is_exited(self) -> None:
        manager, _m = _manager()
        _book(manager, UnverifiedPosition(position=_position("INFY")))
        reconciler, _b, _l, protector, _h = _reconciler(manager=manager)
        _run(reconciler)
        assert [t.symbol for t, _ in protector.exits] == ["INFY"]
        # This line asserted UnverifiedPosition until SIT-006 - the test had
        # written the defect down as the correct behaviour. An exited position
        # is EXITING, and saying otherwise is the book claiming a state the
        # broker has just disproved.
        assert isinstance(manager.tracked("INFY"), ExitingPosition)

    def test_promotion_keeps_the_excursions(self) -> None:
        """A restart must not reset a position's MFE and MAE a second time."""
        from algotrader.execution.positions import Marks

        manager, _m = _manager()
        marks = Marks(max_favourable_excursion=Decimal("900"), max_adverse_excursion=Decimal("-40"))
        _book(manager, UnverifiedPosition(position=_position("INFY"), marks=marks))
        reconciler, _b, _l, protector, _h = _reconciler(manager=manager)
        protector.live_stops = {"INFY"}
        _run(reconciler)
        tracked = manager.tracked("INFY")
        assert tracked is not None
        assert tracked.marks.max_favourable_excursion == Decimal("900")

    def test_promote_refuses_anything_but_an_unverified_position(self) -> None:
        """A promote aimed at an EXITING position would label a holding whose
        stop is known to have failed as protected."""
        manager, _m = _manager()
        position = _position("INFY")
        _book(manager, ExitingPosition(position=position, because="stop refused"))
        proof = EstablishedPosition(
            position=position,
            stop_client_order_id="S",
            stop_broker_order_id="B",
            established_at=NOW,
        )
        with pytest.raises(PositionError, match="only an UnverifiedPosition"):
            asyncio.run(manager.promote(proof))

    def test_promote_refuses_a_proof_for_a_different_holding(self) -> None:
        manager, _m = _manager()
        _book(manager, UnverifiedPosition(position=_position("INFY")))
        other = _position("INFY")
        proof = EstablishedPosition(
            position=other,
            stop_client_order_id="S",
            stop_broker_order_id="B",
            established_at=NOW,
        )
        with pytest.raises(PositionError, match="correlation"):
            asyncio.run(manager.promote(proof))


# ---------------------------------------------------------------------------
# AC5 — broker status through the state machine
# ---------------------------------------------------------------------------


class TestAc5TheStateMachineDecides:
    def test_open_with_a_fill_in_it_is_partial(self) -> None:
        """E15-S03's constraint 1: Kite says OPEN for a partially filled order."""
        assert broker_target(_broker_order("A" * 32, filled=15)) is OrderStatus.PARTIAL
        assert broker_target(_broker_order("A" * 32, filled=0)) is OrderStatus.OPEN

    def test_a_partial_order_reported_open_updates_its_fill_rather_than_refusing(self) -> None:
        """Without consulting the fill this is PARTIAL -> OPEN, refused every
        cycle. With it, it is a fill update."""
        cid = _cid()
        write, event = decide_order(
            _row(cid, status="PARTIAL", filled=10),
            [_broker_order(cid, filled=25)],
            now=NOW,
            submitting_grace=GRACE,
        )
        assert write is not None and write.status is OrderStatus.PARTIAL
        assert write.filled_quantity == 25
        assert event is not None and event.kind is Drift.FILL_UPDATE

    def test_a_legal_move_is_written(self) -> None:
        cid = _cid()
        write, event = decide_order(
            _row(cid, status="SUBMITTED"),
            [_broker_order(cid, status=OrderStatus.FILLED, filled=40, price="1201.5")],
            now=NOW,
            submitting_grace=GRACE,
        )
        assert write is not None and write.status is OrderStatus.FILLED
        assert write.average_price == Decimal("1201.5")
        assert event is not None and event.kind is Drift.TRANSITION

    def test_an_illegal_move_records_reconcile_required_instead(self) -> None:
        """OPEN -> SUBMITTED (a pre-open status reported late) is refused."""
        cid = _cid()
        write, event = decide_order(
            _row(cid, status="OPEN"),
            [_broker_order(cid, status=OrderStatus.SUBMITTED)],
            now=NOW,
            submitting_grace=GRACE,
        )
        assert write is not None and write.status is OrderStatus.RECONCILE_REQUIRED
        assert event is not None and event.kind is Drift.REFUSED

    def test_a_refusal_is_recorded_once_not_every_cycle(self) -> None:
        """An alarm that is always on is not read."""
        cid = _cid()
        ledger = FakeLedger([_row(cid, status="OPEN")])
        reconciler, broker, _l, _p, _h = _reconciler(ledger=ledger)
        broker.orderbook = [_broker_order(cid, status=OrderStatus.SUBMITTED)]
        first = _run(reconciler)
        second = _run(reconciler)
        assert [d.kind for d in first.drifts] == [Drift.REFUSED]
        assert second.drifts == []
        assert len(ledger.writes) == 1

    def test_a_shrinking_fill_keeps_the_higher_number(self) -> None:
        """Fills never shrink; losing the knowledge that one happened is the
        mistake PARTIAL -> OPEN is refused to prevent."""
        cid = _cid()
        write, event = decide_order(
            _row(cid, status="PARTIAL", filled=30),
            [_broker_order(cid, filled=10)],
            now=NOW,
            submitting_grace=GRACE,
        )
        assert write is not None
        assert write.status is OrderStatus.RECONCILE_REQUIRED
        assert write.filled_quantity == 30
        assert event is not None and event.kind is Drift.REFUSED

    def test_a_status_we_do_not_model_is_not_guessed(self) -> None:
        cid = _cid()
        write, _event = decide_order(
            _row(cid, status="OPEN"),
            [_broker_order(cid, status=OrderStatus.RECONCILE_REQUIRED)],
            now=NOW,
            submitting_grace=GRACE,
        )
        assert write is not None and write.status is OrderStatus.RECONCILE_REQUIRED

    def test_two_broker_orders_for_one_key_are_flagged_once(self) -> None:
        cid = _cid()
        ledger = FakeLedger([_row(cid, status="SUBMITTED")])
        reconciler, broker, _l, _p, _h = _reconciler(ledger=ledger)
        broker.orderbook = [
            _broker_order(cid, broker_order_id="B-1"),
            _broker_order(cid, broker_order_id="B-2"),
        ]
        first = _run(reconciler)
        second = _run(reconciler)
        assert [d.kind for d in first.drifts] == [Drift.DUPLICATE]
        assert second.drifts == []

    def test_nothing_changed_writes_nothing(self) -> None:
        """The control: a reconciler that wrote every row every cycle would pass
        every test above."""
        cid = _cid()
        write, event = decide_order(
            _row(cid, status="OPEN", broker_order_id="B-1"),
            [_broker_order(cid, status=OrderStatus.OPEN, broker_order_id="B-1")],
            now=NOW,
            submitting_grace=GRACE,
        )
        assert (write, event) == (None, None)

    def test_an_unreadable_local_status_is_recorded_and_not_touched(self) -> None:
        cid = _cid()
        write, event = decide_order(
            _row(cid, status="NOT_A_STATUS"),
            [_broker_order(cid)],
            now=NOW,
            submitting_grace=GRACE,
        )
        assert write is None
        assert event is not None and event.kind is Drift.REFUSED

    def test_a_live_order_the_broker_does_not_list_is_flagged(self) -> None:
        cid = _cid()
        write, event = decide_order(_row(cid, status="OPEN"), [], now=NOW, submitting_grace=GRACE)
        assert write is not None and write.status is OrderStatus.RECONCILE_REQUIRED
        assert event is not None and event.kind is Drift.MISSING


# ---------------------------------------------------------------------------
# AC6 — SUBMITTING rows
# ---------------------------------------------------------------------------


class TestAc6SubmittingRows:
    def test_inside_the_grace_window_it_is_left_alone(self) -> None:
        """The request may still be in flight."""
        cid = _cid()
        write, event = decide_order(
            _row(
                cid,
                status="SUBMITTING",
                placed_at=NOW - dt.timedelta(seconds=20),
                broker_order_id=None,
            ),
            [],
            now=NOW,
            submitting_grace=GRACE,
        )
        assert (write, event) == (None, None)

    def test_past_the_window_and_absent_it_is_submit_failed(self) -> None:
        """SUBMIT_FAILED has been modelled since E15-S03 and nothing wrote it."""
        cid = _cid()
        write, event = decide_order(
            _row(
                cid,
                status="SUBMITTING",
                placed_at=NOW - dt.timedelta(minutes=3),
                broker_order_id=None,
            ),
            [],
            now=NOW,
            submitting_grace=GRACE,
        )
        assert write is not None and write.status is OrderStatus.SUBMIT_FAILED
        assert event is not None and event.kind is Drift.SUBMIT_FAIL

    def test_present_at_the_broker_it_is_adopted_with_its_id(self) -> None:
        cid = _cid()
        write, event = decide_order(
            _row(cid, status="SUBMITTING", broker_order_id=None),
            [_broker_order(cid, status=OrderStatus.OPEN, broker_order_id="B-77")],
            now=NOW,
            submitting_grace=GRACE,
        )
        assert write is not None and write.status is OrderStatus.OPEN
        assert write.broker_order_id == "B-77"
        assert event is not None and event.kind is Drift.ADOPTED

    def test_submit_failed_and_still_absent_is_left_alone(self) -> None:
        cid = _cid()
        assert decide_order(
            _row(cid, status="SUBMIT_FAILED", broker_order_id=None),
            [],
            now=NOW,
            submitting_grace=GRACE,
        ) == (None, None)

    def test_submit_failed_then_listed_goes_to_reconcile_required(self) -> None:
        """It landed after all; SUBMIT_FAILED's only legal move is this one."""
        cid = _cid()
        write, event = decide_order(
            _row(cid, status="SUBMIT_FAILED", broker_order_id=None),
            [_broker_order(cid, status=OrderStatus.OPEN)],
            now=NOW,
            submitting_grace=GRACE,
        )
        assert write is not None and write.status is OrderStatus.RECONCILE_REQUIRED
        assert event is not None and event.kind is Drift.ADOPTED

    def test_a_zero_grace_window_is_refused(self) -> None:
        with pytest.raises(ValueError, match="still in flight"):
            Reconciler(
                broker=FakeBroker(),
                ledger=FakeLedger(),
                manager=_manager()[0],
                protector=FakeProtector(),  # type: ignore[arg-type]
                halter=RecordingHalter(),
                submitting_grace_seconds=0,
            )


# ---------------------------------------------------------------------------
# AC7 — a holding we explain but do not book is exited
# ---------------------------------------------------------------------------


class TestAc7UnbookedHoldings:
    def test_our_fill_that_is_not_in_the_book_is_exited(self) -> None:
        cid = _cid()
        corr = uuid4()
        reconciler, broker, _l, protector, _h = _reconciler(
            ledger=FakeLedger([_row(cid, status="FILLED", filled=40, correlation=corr)])
        )
        broker.orderbook = [_broker_order(cid, status=OrderStatus.FILLED, filled=40)]
        broker.positions = [_pos("INFY", quantity=40, bought=40)]
        report = _run(reconciler)
        ((target, _because),) = protector.exits
        assert (target.symbol, target.quantity, target.direction) == ("INFY", 40, Direction.LONG)
        assert target.correlation_id == corr
        assert Drift.UNBOOKED in [d.kind for d in report.drifts]
        assert not report.halted, "our own fill is not an unknown position"

    def test_a_booked_position_is_not_exited_as_unbooked(self) -> None:
        """The control. Without it, a reconciler exiting every fill would pass."""
        cid = _cid()
        corr = uuid4()
        manager, _m = _manager()
        tracked = _protected("INFY")
        tracked = ProtectedPosition(
            established=EstablishedPosition(
                position=_position("INFY", correlation=corr),
                stop_client_order_id="S",
                stop_broker_order_id="BS",
                established_at=NOW,
            )
        )
        _book(manager, tracked)
        reconciler, broker, _l, protector, _h = _reconciler(
            manager=manager,
            ledger=FakeLedger([_row(cid, status="FILLED", filled=40, correlation=corr)]),
        )
        protector.live_stops = {"INFY"}
        broker.orderbook = [_broker_order(cid, status=OrderStatus.FILLED, filled=40)]
        broker.positions = [_pos("INFY", quantity=40, bought=40)]
        _run(reconciler)
        assert protector.exits == []

    def test_a_holding_whose_exit_is_already_working_is_not_exited_again(self) -> None:
        cid = _cid()
        reconciler, broker, _l, protector, _h = _reconciler(
            ledger=FakeLedger([_row(cid, status="FILLED", filled=40)])
        )
        protector.live_exits = {"INFY"}
        broker.orderbook = [_broker_order(cid, status=OrderStatus.FILLED, filled=40)]
        broker.positions = [_pos("INFY", quantity=40, bought=40)]
        _run(reconciler)
        assert protector.exits == []

    def test_only_the_open_remainder_is_exited(self) -> None:
        entry, exit_ = _cid(), _cid()
        corr = uuid4()
        reconciler, broker, _l, protector, _h = _reconciler(
            ledger=FakeLedger(
                [
                    _row(entry, status="FILLED", filled=40, correlation=corr),
                    _row(
                        exit_,
                        status="CANCELLED",
                        side="SELL",
                        intent="SQUAREOFF",
                        filled=15,
                        correlation=corr,
                    ),
                ]
            )
        )
        broker.orderbook = [
            _broker_order(entry, status=OrderStatus.FILLED, filled=40),
            _broker_order(exit_, status=OrderStatus.CANCELLED, filled=15, side=Side.SELL),
        ]
        broker.positions = [_pos("INFY", quantity=25, bought=40, sold=15)]
        _run(reconciler)
        assert [t.quantity for t, _ in protector.exits] == [25]

    def test_a_short_fill_is_exited_as_a_short(self) -> None:
        cid = _cid()
        reconciler, broker, _l, protector, _h = _reconciler(
            ledger=FakeLedger([_row(cid, status="FILLED", filled=30, side="SELL")])
        )
        broker.orderbook = [
            _broker_order(cid, status=OrderStatus.FILLED, filled=30, side=Side.SELL)
        ]
        broker.positions = [_pos("INFY", quantity=-30, sold=30)]
        _run(reconciler)
        assert protector.exits[0][0].direction is Direction.SHORT

    def test_a_fully_closed_round_trip_is_not_exited(self) -> None:
        entry, exit_ = _cid(), _cid()
        corr = uuid4()
        reconciler, broker, _l, protector, _h = _reconciler(
            ledger=FakeLedger(
                [
                    _row(entry, status="FILLED", filled=40, correlation=corr),
                    _row(
                        exit_,
                        status="FILLED",
                        side="SELL",
                        intent="STOP",
                        filled=40,
                        correlation=corr,
                    ),
                ]
            )
        )
        broker.orderbook = [
            _broker_order(entry, status=OrderStatus.FILLED, filled=40),
            _broker_order(exit_, status=OrderStatus.FILLED, filled=40, side=Side.SELL),
        ]
        broker.positions = [_pos("INFY", quantity=0, bought=40, sold=40)]
        _run(reconciler)
        assert protector.exits == []


# ---------------------------------------------------------------------------
# AC8 — every difference audited, and it fits the table
# ---------------------------------------------------------------------------


class TestAc8AuditedAndFits:
    def test_a_clean_cycle_writes_nothing(self) -> None:
        written: list[AuditEntry] = []

        async def sink(entry: AuditEntry) -> None:
            written.append(entry)

        reconciler, _b, _l, _p, _h = _reconciler(audit=sink)
        report = _run(reconciler)
        assert report.clean
        assert written == []

    def test_each_difference_is_exactly_one_entry(self) -> None:
        written: list[AuditEntry] = []

        async def sink(entry: AuditEntry) -> None:
            written.append(entry)

        cid = _cid()
        reconciler, broker, _l, _p, _h = _reconciler(
            ledger=FakeLedger([_row(cid, status="SUBMITTED")]), audit=sink
        )
        broker.orderbook = [_broker_order(cid, status=OrderStatus.OPEN)]
        report = _run(reconciler)
        assert len(written) == len(report.drifts) == 1
        (entry,) = written
        assert (entry.stage, entry.outcome, entry.service) == (
            "RECONCILIATION_DRIFT",
            "TRANSITION",
            "reconciler",
        )

    def test_a_failing_audit_sink_does_not_stop_the_halt_or_the_exits(self) -> None:
        async def broken(entry: AuditEntry) -> None:
            raise RuntimeError("audit database down")

        manager, _m = _manager()
        _book(manager, _protected("TCS"))
        reconciler, broker, _l, protector, halter = _reconciler(manager=manager, audit=broken)
        broker.positions = [_pos("WIPRO", quantity=25, bought=25)]
        report = _run(reconciler)
        assert report.halted
        assert [r for r, _ in halter.armed] == [HaltReason.UNKNOWN_POSITION]
        assert [t.symbol for t, _ in protector.exits] == ["TCS"]

    def test_every_outcome_fits_the_real_column(self) -> None:
        """Read off the model, not restated: UNKNOWN_POSITION is 16 characters
        against String(12) and would fail the insert at the moment it mattered."""
        from algotrader.common.db.models import DecisionLog

        width = DecisionLog.__table__.c.outcome.type.length
        assert all(len(d.value) <= width for d in Drift)
        assert len("UNKNOWN_POSITION") > width, "the label this module avoided would not fit"

    def test_a_label_too_wide_for_the_table_refuses_to_load(self, monkeypatch) -> None:
        """The guard itself, probed: a stage one character too long is refused."""
        monkeypatch.setattr(rec, "STAGE", "R" * 29)
        with pytest.raises(RuntimeError, match="would not fit"):
            rec._assert_fits_audit_columns()

    def test_drift_is_counted_by_kind_on_the_real_registry(self) -> None:
        reconciler, broker, _l, _p, _h = _reconciler()
        broker.positions = [_pos("WIPRO", quantity=25, bought=25)]
        _run(reconciler)
        assert _counter("reconciliation_drift_total", kind="UNKNOWN_POS") == 1
        assert _counter("reconciliation_cycles_total") == 1


# ---------------------------------------------------------------------------
# AC9 — a failed read is never acted on; persistent failure halts
# ---------------------------------------------------------------------------


class _Calendar:
    def __init__(self, open_: bool = True) -> None:
        self.open = open_

    def is_market_open(self, moment: dt.datetime) -> bool:
        return self.open


class TestAc9NoActionOnAPartialView:
    @pytest.mark.parametrize("where", ["positions", "orderbook"])
    def test_a_failed_broker_read_takes_no_action_at_all(self, where: str) -> None:
        """Even the halt: with no orderbook, a foreign-looking position cannot be
        told apart from one of ours."""
        cid = _cid()
        manager, _m = _manager()
        _book(manager, _protected("TCS"))
        reconciler, broker, ledger, protector, halter = _reconciler(
            manager=manager, ledger=FakeLedger([_row(cid, status="SUBMITTED")])
        )
        broker.positions = [_pos("WIPRO", quantity=25, bought=25)]
        broker.orderbook = [_broker_order(cid, status=OrderStatus.FILLED, filled=40)]
        broker.fail_on = where
        with pytest.raises(ReconciliationReadError, match="no action taken"):
            _run(reconciler)
        assert (halter.armed, ledger.writes, protector.exits) == ([], [], [])
        assert _counter("reconciliation_read_failures_total") == 1

    def test_a_failed_database_read_takes_no_action_either(self) -> None:
        reconciler, broker, ledger, _p, halter = _reconciler(ledger=FakeLedger())
        ledger.fail = True
        broker.positions = [_pos("WIPRO", quantity=25, bought=25)]
        with pytest.raises(ReconciliationReadError):
            _run(reconciler)
        assert halter.armed == []

    def test_three_failed_cycles_in_a_row_halt(self) -> None:
        reconciler, broker, _l, _p, _rh = _reconciler()
        broker.fail_on = "positions"
        halter = RecordingHalter()
        loop = ReconciliationLoop(
            reconciler, calendar=_Calendar(), halter=halter, clock=lambda: NOW
        )
        for _ in range(2):
            assert asyncio.run(loop.tick()) is None
        assert halter.armed == []
        asyncio.run(loop.tick())
        assert [r for r, _ in halter.armed] == [HaltReason.BROKER_DISCONNECTED]

    def test_a_success_resets_the_count(self) -> None:
        """The control: without it, three blips spread across a day would halt."""
        reconciler, broker, _l, _p, _rh = _reconciler()
        halter = RecordingHalter()
        loop = ReconciliationLoop(
            reconciler, calendar=_Calendar(), halter=halter, clock=lambda: NOW
        )
        broker.fail_on = "positions"
        asyncio.run(loop.tick())
        asyncio.run(loop.tick())
        broker.fail_on = None
        asyncio.run(loop.tick())
        broker.fail_on = "positions"
        asyncio.run(loop.tick())
        asyncio.run(loop.tick())
        assert halter.armed == []
        assert loop.consecutive_failures == 2

    def test_outside_market_hours_nothing_runs(self) -> None:
        reconciler, broker, _l, _p, _h = _reconciler()
        loop = ReconciliationLoop(
            reconciler, calendar=_Calendar(open_=False), halter=RecordingHalter(), clock=lambda: NOW
        )
        assert asyncio.run(loop.tick()) is None
        assert broker.calls == []

    def test_a_reconnect_runs_whatever_the_clock_says(self) -> None:
        reconciler, broker, _l, _p, _h = _reconciler()
        loop = ReconciliationLoop(
            reconciler, calendar=_Calendar(open_=False), halter=RecordingHalter(), clock=lambda: NOW
        )
        assert asyncio.run(loop.on_reconnect()) is not None
        assert broker.calls == ["positions", "orderbook"]

    def test_the_loop_runs_on_its_interval_until_stopped(self) -> None:
        reconciler, broker, _l, _p, _h = _reconciler()
        stop = asyncio.Event()
        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)
            if len(slept) == 3:
                stop.set()

        loop = ReconciliationLoop(
            reconciler,
            calendar=_Calendar(),
            halter=RecordingHalter(),
            clock=lambda: NOW,
            sleep=fake_sleep,
        )
        asyncio.run(loop.run(stop))
        assert slept == [30.0, 30.0, 30.0]
        assert broker.calls.count("positions") == 3

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [({"interval_seconds": 0}, "positive"), ({"max_consecutive_failures": 0}, "neither")],
    )
    def test_nonsense_settings_are_refused(self, kwargs: dict, match: str) -> None:
        reconciler, _b, _l, _p, _h = _reconciler()
        with pytest.raises(ValueError, match=match):
            ReconciliationLoop(
                reconciler,
                calendar=_Calendar(),
                halter=RecordingHalter(),
                clock=lambda: NOW,
                **kwargs,
            )

    def test_a_naive_timestamp_is_refused(self) -> None:
        reconciler, _b, _l, _p, _h = _reconciler()
        with pytest.raises(ValueError, match="naive"):
            asyncio.run(reconciler.run_cycle(now=dt.datetime(2026, 8, 25, 10, 30)))


# ---------------------------------------------------------------------------
# find_unknown, stated as a property of the two sides
# ---------------------------------------------------------------------------


class TestFindUnknownIsPerSide:
    def test_excess_on_either_side_is_foreign(self) -> None:
        found = find_unknown([_pos("INFY", quantity=5, bought=50, sold=45)], {"INFY": (40, 45)})
        assert [(u.foreign_buys, u.foreign_sells) for u in found] == [(10, 0)]

    def test_our_side_exceeding_the_brokers_is_never_foreign(self) -> None:
        assert find_unknown([_pos("INFY", quantity=40, bought=40)], {"INFY": (40, 40)}) == []


# ---------------------------------------------------------------------------
# STRIDE, spoofing: a tag is readable, so a tag can be copied (REC-001)
# ---------------------------------------------------------------------------


class TestACopiedTagIsNotOurs:
    """Found by the STRIDE pass against this story's own attribution rule. The
    first version, faced with our FILLED entry and a foreign order carrying the
    same tag, raised no halt, never examined the duplicate (terminal rows skip
    order reconciliation) and sent an exit for all 140 shares - 100 of them
    someone else's."""

    def _spoofed(self):
        cid = _cid()
        reconciler, broker, ledger, protector, halter = _reconciler(
            ledger=FakeLedger([_row(cid, status="FILLED", filled=40)])
        )
        broker.orderbook = [
            _broker_order(cid, status=OrderStatus.FILLED, filled=40, broker_order_id="OURS"),
            _broker_order(cid, status=OrderStatus.FILLED, filled=100, broker_order_id="THEIRS"),
        ]
        broker.positions = [_pos("INFY", quantity=140, bought=140)]
        return reconciler, broker, ledger, protector, halter

    def test_a_copied_tag_on_a_filled_order_halts(self) -> None:
        reconciler, _b, _l, _p, halter = self._spoofed()
        report = _run(reconciler)
        assert report.halted
        assert [r for r, _ in halter.armed] == [HaltReason.DUPLICATE_ORDER]
        assert [d.kind for d in report.drifts] == [Drift.DUPLICATE]

    def test_a_holding_it_cannot_attribute_is_not_traded(self) -> None:
        """The loop does not trade a position it did not open."""
        reconciler, _b, _l, protector, _h = self._spoofed()
        _run(reconciler)
        assert protector.exits == [], "the reconciler sold shares that were not ours"

    def test_it_is_not_misnamed_an_unknown_position(self) -> None:
        """The shares carry our key; 'unknown' would send an operator looking for
        a stranger instead of at our idempotency."""
        reconciler, _b, _l, _p, _h = self._spoofed()
        assert _run(reconciler).unknown == []

    def test_it_is_reported_once_not_every_cycle(self) -> None:
        reconciler, _b, _l, _p, _h = self._spoofed()
        first, second = _run(reconciler), _run(reconciler)
        assert [d.kind for d in first.drifts] == [Drift.DUPLICATE]
        assert second.drifts == []

    def test_one_match_per_key_is_the_normal_case(self) -> None:
        """The control: every one of our orders has exactly one broker order,
        and that must never read as a duplicate."""
        cid = _cid()
        reconciler, broker, _l, _p, halter = _reconciler(
            ledger=FakeLedger([_row(cid, status="FILLED", filled=40)])
        )
        broker.orderbook = [_broker_order(cid, status=OrderStatus.FILLED, filled=40)]
        broker.positions = [_pos("INFY", quantity=40, bought=40)]
        report = _run(reconciler)
        assert Drift.DUPLICATE not in [d.kind for d in report.drifts]
        assert HaltReason.DUPLICATE_ORDER not in [r for r, _ in halter.armed]

    def test_decide_order_only_does_the_bookkeeping(self) -> None:
        """Duplicates are reported by the cycle, which sees terminal rows too."""
        cid = _cid()
        write, event = decide_order(
            _row(cid, status="SUBMITTED"),
            [_broker_order(cid, broker_order_id="A"), _broker_order(cid, broker_order_id="B")],
            now=NOW,
            submitting_grace=GRACE,
        )
        assert write is not None and write.status is OrderStatus.RECONCILE_REQUIRED
        assert event is None


class TestNothingSecretReachesTheHaltRecord:
    """STRIDE, information disclosure (REC-002). The halt record is written to
    Redis, outside the logging layer's redaction, and the first version put the
    broker's exception text straight into it."""

    def test_the_halt_names_the_error_type_not_its_message(self) -> None:
        secret = "access_token=Zq7xT9vLmP2kR8wY4nB6cD1f"

        class LeakyBroker(FakeBroker):
            async def fetch_positions(self) -> list[BrokerPosition]:
                raise PermissionError(f"Invalid session. {secret}")

        reconciler, _b, _l, _p, _h = _reconciler(broker=LeakyBroker())
        halter = RecordingHalter()
        loop = ReconciliationLoop(
            reconciler,
            calendar=_Calendar(),
            halter=halter,
            clock=lambda: NOW,
            max_consecutive_failures=1,
        )
        asyncio.run(loop.tick())
        ((reason, detail),) = halter.armed
        assert reason is HaltReason.BROKER_DISCONNECTED
        assert "PermissionError" in detail
        assert secret not in detail and "Zq7xT9" not in detail


class TestTheWidthGuardRunsAtImport:
    """Mutation M25 survived the first run: the test above calls the guard
    directly, so deleting the import-time CALL changed nothing it could see. The
    property is that the module refuses to LOAD, which only an import can show -
    and an import in this process would redefine classes other tests hold."""

    def _import_with_outcome_width(self, width: int | None) -> Any:
        import subprocess
        import sys

        patch = (
            ""
            if width is None
            else "from algotrader.common.db.models import DecisionLog\n"
            f"DecisionLog.__table__.c.outcome.type.length = {width}\n"
        )
        return subprocess.run(
            [sys.executable, "-c", patch + "import algotrader.execution.reconciliation\n"],
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_a_column_too_narrow_for_its_labels_stops_the_import(self) -> None:
        result = self._import_with_outcome_width(5)
        assert result.returncode != 0
        assert "would not fit" in result.stderr

    def test_the_real_column_imports_cleanly(self) -> None:
        """The control: a guard that refused every import would pass the test above."""
        assert self._import_with_outcome_width(None).returncode == 0


class TestAnExitedPositionSaysSo:
    """SIT-006. The loop exited a position whose stop was dead and left its label
    as it was, so the book said PROTECTED about a position the broker had just
    shown was not - and the next cycle ran the exit path again, logging a
    CRITICAL naked-position line and counting a failure every 30 seconds."""

    def test_a_protected_position_whose_stop_died_becomes_exiting(self) -> None:
        manager, mirror = _manager()
        _book(manager, _protected("INFY"))
        reconciler, _b, _l, _p, _h = _reconciler(manager=manager)
        _run(reconciler)
        assert isinstance(manager.tracked("INFY"), ExitingPosition)
        assert mirror.data["INFY"].protection == "ExitingPosition"

    def test_the_next_cycle_does_not_run_the_exit_path_again(self) -> None:
        manager, _m = _manager()
        _book(manager, _protected("INFY"))
        reconciler, _b, _l, protector, _h = _reconciler(manager=manager)
        _run(reconciler)
        protector.live_exits = {"INFY"}
        _run(reconciler)
        assert len(protector.exits) == 1, "the exit path ran again on a position already exiting"

    def test_a_failed_exit_leaves_the_label_alone(self) -> None:
        """exit_now raises having halted: there is NO live exit, so calling the
        position EXITING would be the same false claim in the other direction."""
        manager, _m = _manager()
        _book(manager, _protected("INFY"))
        reconciler, _b, _l, protector, _h = _reconciler(manager=manager)
        protector.exit_raises = {"INFY": RuntimeError("exit refused")}
        _run(reconciler)
        assert isinstance(manager.tracked("INFY"), ProtectedPosition)

    def test_mark_exiting_keeps_the_marks_and_is_idempotent(self) -> None:
        from algotrader.execution.positions import Marks

        manager, _m = _manager()
        marks = Marks(max_favourable_excursion=Decimal("500"))
        _book(manager, UnverifiedPosition(position=_position("INFY"), marks=marks))
        first = asyncio.run(manager.mark_exiting("INFY", because="stop dead"))
        second = asyncio.run(manager.mark_exiting("INFY", because="again"))
        assert first is second
        assert first.marks.max_favourable_excursion == Decimal("500")
        assert first.because == "stop dead"

    def test_mark_exiting_refuses_a_symbol_not_held(self) -> None:
        manager, _m = _manager()
        with pytest.raises(PositionError, match="not held"):
            asyncio.run(manager.mark_exiting("WIPRO", because="x"))


class TestAStrangerIsAlertedOnce:
    """SIT-005. Detection runs every cycle; the alert runs once per stranger, or
    again if its size changes."""

    def test_the_second_cycle_detects_but_does_not_alert(self) -> None:
        reconciler, broker, _l, _p, _h = _reconciler()
        broker.positions = [_pos("WIPRO", quantity=25, bought=25)]
        first, second = _run(reconciler), _run(reconciler)
        assert first.unknown and second.unknown, "detection stopped"
        assert [d.kind for d in first.drifts] == [Drift.UNKNOWN_POS]
        assert second.drifts == []
        assert _counter("unknown_positions_total") == 1

    def test_the_halt_is_re_armed_every_cycle_while_the_stranger_remains(self) -> None:
        """An operator who clears the latch while an unknown position exists is
        halted again by the next cycle."""
        reconciler, broker, _l, _p, halter = _reconciler()
        broker.positions = [_pos("WIPRO", quantity=25, bought=25)]
        _run(reconciler)
        _run(reconciler)
        assert [r for r, _ in halter.armed] == [HaltReason.UNKNOWN_POSITION] * 2

    def test_a_stranger_that_grows_is_alerted_again(self) -> None:
        reconciler, broker, _l, _p, _h = _reconciler()
        broker.positions = [_pos("WIPRO", quantity=25, bought=25)]
        _run(reconciler)
        broker.positions = [_pos("WIPRO", quantity=60, bought=60)]
        report = _run(reconciler)
        assert [d.kind for d in report.drifts] == [Drift.UNKNOWN_POS]
        assert _counter("unknown_positions_total") == 2
