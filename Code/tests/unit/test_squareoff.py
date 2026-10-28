"""E15-S06: the square-off timer, one acceptance criterion at a time.

The gateway and the position manager are the real ones; the account double
answers from configured facts and records every call in order, so a test can
assert the SEQUENCE - stop cancelled, cancel visible, THEN the exit - and not
only the end state.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from algotrader.broker.adapter import BrokerPosition
from algotrader.common.audit import AuditEntry
from algotrader.common.calendar import IST, MarketCalendar, load_holidays_with_status
from algotrader.common.config import MIN_EXIT_BUFFER_MINUTES
from algotrader.common.enums import (
    Direction,
    ExitReason,
    OrderIntent,
    OrderStatus,
    OrderType,
    Product,
    Side,
)
from algotrader.common.metrics import get_metrics, reset_metrics_for_testing
from algotrader.common.models.trading import Order, OrderRequest, Position
from algotrader.execution.gateway import GatewayPolicy, OrderGateway
from algotrader.execution.halt import HaltReason
from algotrader.execution.positions import (
    ExitingPosition,
    PositionManager,
    ProtectedPosition,
    UnverifiedPosition,
)
from algotrader.execution.protective_stop import (
    EstablishedPosition,
    exit_client_order_id,
    stop_client_order_id,
)
from algotrader.execution.squareoff import Outcome, SquareOffTimer

TRADE_DATE = dt.date(2026, 8, 25)


def _ist(hour: int, minute: int, second: int = 0) -> dt.datetime:
    return dt.datetime.combine(TRADE_DATE, dt.time(hour, minute, second), tzinfo=IST).astimezone(
        dt.UTC
    )


NON_CAS_DEADLINE = _ist(15, 15)  # 15:20 minus the 5-minute buffer
CAS_DEADLINE = _ist(15, 5)  # 15:10 minus the buffer


@pytest.fixture(autouse=True)
def _fresh_metrics() -> None:
    reset_metrics_for_testing()


def _calendar() -> MarketCalendar:
    path = Path(__file__).resolve().parents[2] / "config" / "nse_holidays.yaml"
    status = load_holidays_with_status(str(path))
    return MarketCalendar(status.dates, covers_years=status.covers_years)


# ---------------------------------------------------------------------------
# The account: orders by key, positions, and every call in order
# ---------------------------------------------------------------------------


class Account:
    def __init__(self) -> None:
        self.orders: dict[str, Order] = {}
        self.calls: list[str] = []
        self.positions: list[BrokerPosition] = []
        self.position_reads = 0
        self.fail_positions = False
        self.fail_cancel = False
        #: Reads that still show a cancelled stop as OPEN (Kite's CANCEL PENDING).
        self.cancel_pending_reads = 0
        #: Make the stop FILL when it is cancelled - the race at the deadline.
        self.fill_on_cancel = False
        #: Accept the exit but never list it.
        self.hide_exits = False
        self._pending: dict[str, int] = {}
        self._ids = 0

    # -- placer ---------------------------------------------------------------
    async def place_order(self, request: OrderRequest) -> str:
        self._ids += 1
        broker_id = f"B-{self._ids}"
        self.calls.append(f"place:{request.intent.value}:{request.side.value}:{request.quantity}")
        if not (self.hide_exits and request.intent is OrderIntent.SQUAREOFF):
            self.orders[request.client_order_id] = Order(
                client_order_id=request.client_order_id,
                broker_order_id=broker_id,
                correlation_id=request.correlation_id,
                symbol=request.symbol,
                side=request.side,
                order_type=request.order_type,
                product=request.product,
                quantity=request.quantity,
                status=OrderStatus.OPEN,
                filled_quantity=0,
                intent=request.intent,
                placed_at=_ist(10, 0),
                last_update_at=_ist(10, 0),
            )
        self.last_request = request
        return broker_id

    async def find_by_client_order_id(self, cid: str) -> Order | None:
        order = self.orders.get(cid)
        if order is not None and cid in self._pending:
            if self._pending[cid] > 0:
                self._pending[cid] -= 1
            else:
                del self._pending[cid]
                order = order.model_copy(update={"status": OrderStatus.CANCELLED})
                self.orders[cid] = order
        return order

    async def cancel_order(self, broker_order_id: str) -> None:
        if self.fail_cancel:
            raise ConnectionError("cancel timed out")
        for cid, order in self.orders.items():
            if order.broker_order_id != broker_order_id:
                continue
            self.calls.append(f"cancel:{order.intent.value}")
            if self.fill_on_cancel:
                self.orders[cid] = order.model_copy(
                    update={"status": OrderStatus.FILLED, "filled_quantity": order.quantity}
                )
            elif self.cancel_pending_reads:
                self._pending[cid] = self.cancel_pending_reads
            else:
                self.orders[cid] = order.model_copy(update={"status": OrderStatus.CANCELLED})

    # -- positions --------------------------------------------------------------
    async def fetch_positions(self) -> list[BrokerPosition]:
        self.position_reads += 1
        if self.fail_positions:
            raise ConnectionError("positions read timed out")
        return list(self.positions)

    def hold(self, symbol: str, quantity: int, *, product: str = "MIS") -> None:
        self.positions.append(
            BrokerPosition(
                symbol=symbol,
                exchange="NSE",
                product=product,
                quantity=quantity,
                day_buy_quantity=max(quantity, 0),
                day_sell_quantity=max(-quantity, 0),
            )
        )

    def seed_stop(
        self, position: Position, *, filled: int = 0, status: OrderStatus | None = None
    ) -> str:
        cid = stop_client_order_id(position, trade_date=TRADE_DATE)
        self._ids += 1
        self.orders[cid] = Order(
            client_order_id=cid,
            broker_order_id=f"B-{self._ids}",
            correlation_id=position.correlation_id,
            symbol=position.symbol,
            side=Side.SELL if position.direction is Direction.LONG else Side.BUY,
            order_type=OrderType.SLM,
            product=Product.MIS,
            quantity=position.quantity,
            status=status
            or (OrderStatus.FILLED if filled == position.quantity else OrderStatus.OPEN),
            filled_quantity=filled,
            intent=OrderIntent.STOP,
            placed_at=_ist(10, 0),
            last_update_at=_ist(10, 0),
        )
        return cid


class Orders:
    """The orders table, as the gateway needs it."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def insert_submitting(self, order: dict[str, Any]) -> int:
        self.rows[order["client_order_id"]] = {**order, "status": "SUBMITTING"}
        return len(self.rows)

    async def attach_broker_id(self, cid: str, broker_order_id: str) -> None:
        self.rows[cid].update(status="SUBMITTED", broker_order_id=broker_order_id)

    async def mark_rejected(self, cid: str, *, reason: str | None) -> None:
        self.rows[cid]["status"] = "REJECTED"

    async def find_by_client_order_id(self, cid: str) -> dict[str, Any] | None:
        row = self.rows.get(cid)
        return None if row is None else dict(row)


class Store:
    async def open_position(self, position: dict[str, Any]) -> int:
        return 1

    async def open_positions(self) -> list[dict[str, Any]]:
        return []

    async def close_position(self, position_id: int, **kwargs: Any) -> None:
        return None


class Mirror:
    async def write(self, symbol: str, snapshot: Any) -> None:
        return None

    async def clear(self, symbol: str) -> None:
        return None


class Halter:
    def __init__(self) -> None:
        self.armed: list[HaltReason] = []

    async def arm(self, reason, *, detail, armed_by, now):
        self.armed.append(reason)
        return object()


class Emergency:
    def __init__(self) -> None:
        self.exits: list[tuple[Any, str]] = []
        self.raises: Exception | None = None

    async def exit_now(self, target, *, trade_date, now, because) -> None:
        self.exits.append((target, because))
        if self.raises is not None:
            raise self.raises


class Rig:
    def __init__(self) -> None:
        self.account = Account()
        self.gateway = OrderGateway(
            self.account,
            policy=GatewayPolicy(algo_id=None, market_protection=Decimal("-1")),
            store=Orders(),
        )
        self.manager = PositionManager(
            protector=None,  # type: ignore[arg-type]
            store=Store(),
            mirror=Mirror(),
            calendar=_calendar(),
        )
        self.halter = Halter()
        self.emergency = Emergency()
        self.audit: list[AuditEntry] = []
        self.sleeps: list[float] = []

        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)

        async def write(entry: AuditEntry) -> None:
            self.audit.append(entry)

        self.timer = SquareOffTimer(
            gateway=self.gateway,
            broker=self.account,
            manager=self.manager,
            protector=self.emergency,
            halter=self.halter,
            audit=write,
            metrics=get_metrics(),
            sleep=sleep,
        )

    def position(
        self,
        symbol: str = "INFY",
        *,
        deadline: dt.datetime = NON_CAS_DEADLINE,
        quantity: int = 40,
        direction: Direction = Direction.LONG,
        kind: str = "protected",
        held: int | None = None,
        stop: bool = True,
    ) -> Position:
        long = direction is Direction.LONG
        position = Position(
            position_id=7,
            correlation_id=uuid4(),
            symbol=symbol,
            slot_index=0,
            direction=direction,
            quantity=quantity,
            entry_price=Decimal("1200.0000"),
            stop_price=Decimal("1186.4500") if long else Decimal("1213.5500"),
            opened_at=_ist(10, 0),
            squareoff_deadline=deadline,
        )
        if kind == "protected":
            tracked: Any = ProtectedPosition(
                established=EstablishedPosition(
                    position=position,
                    stop_client_order_id="S",
                    stop_broker_order_id="BS",
                    established_at=_ist(10, 0),
                )
            )
        elif kind == "unverified":
            tracked = UnverifiedPosition(position=position)
        else:
            tracked = ExitingPosition(position=position, because="stop refused")
        self.manager._book[symbol] = tracked
        if stop:
            self.account.seed_stop(position)
        held = quantity if held is None else held
        if held:
            self.account.hold(symbol, held if long else -held)
        return position

    def sweep(self, at: dt.datetime):
        return asyncio.run(self.timer.tick(at))

    def outcomes(self) -> list[str]:
        return [e.outcome for e in self.audit]


# ---------------------------------------------------------------------------
# AC1 - our deadline always precedes the broker's, every stock class
# ---------------------------------------------------------------------------


class TestAc1OurDeadlinePrecedesTheBrokers:
    @settings(max_examples=300, deadline=None)
    @given(
        day=st.dates(min_value=dt.date(2026, 1, 1), max_value=dt.date(2026, 12, 31)),
        is_cas=st.booleans(),
        is_fno=st.booleans(),
        buffer=st.integers(min_value=MIN_EXIT_BUFFER_MINUTES, max_value=120),
    )
    def test_the_calendar_deadline_is_strictly_before_the_brokers(
        self, day: dt.date, is_cas: bool, is_fno: bool, buffer: int
    ) -> None:
        calendar = _calendar()
        ours = calendar.squareoff_deadline(
            day, is_cas_stock=is_cas, is_fno=is_fno, buffer_minutes=buffer
        )
        broker_time = calendar.broker_squareoff_time(is_cas_stock=is_cas, is_fno=is_fno)
        broker = dt.datetime.combine(day, broker_time, tzinfo=IST)
        assert ours < broker
        assert ours.tzinfo is not None and ours.astimezone(IST).date() == day

    @settings(max_examples=100, deadline=None)
    @given(
        is_cas=st.booleans(), buffer=st.integers(min_value=MIN_EXIT_BUFFER_MINUTES, max_value=60)
    )
    def test_a_position_opens_with_a_deadline_before_the_brokers(
        self, is_cas: bool, buffer: int
    ) -> None:
        """The deadline the timer acts on is the one stored at open."""
        from algotrader.common.models.trading import SizingResult

        class Protector:
            async def attach(self, position, *, trade_date, now):
                return EstablishedPosition(
                    position=position,
                    stop_client_order_id="S",
                    stop_broker_order_id="BS",
                    established_at=now,
                )

        calendar = _calendar()
        manager = PositionManager(
            protector=Protector(),  # type: ignore[arg-type]
            store=Store(),
            mirror=Mirror(),
            calendar=calendar,
            exit_buffer_minutes=buffer,
        )
        order = Order(
            client_order_id="E" * 32,
            broker_order_id="B-E",
            correlation_id=uuid4(),
            symbol="INFY",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            product=Product.MIS,
            quantity=40,
            status=OrderStatus.FILLED,
            filled_quantity=40,
            average_price=Decimal("1200.0000"),
            intent=OrderIntent.ENTRY,
            placed_at=_ist(10, 0),
            last_update_at=_ist(10, 0),
        )
        sizing = SizingResult(
            quantity=40,
            entry_price=Decimal("1200.0000"),
            stop_price=Decimal("1181.2500"),
            capital_at_risk=Decimal("750.00"),
            binding_constraint="risk_budget",
        )
        tracked = asyncio.run(
            manager.open_from_fill(
                order,
                sizing=sizing,
                slot_index=0,
                trade_date=TRADE_DATE,
                now=_ist(10, 1),
                is_cas_stock=is_cas,
            )
        )
        broker_time = calendar.broker_squareoff_time(is_cas_stock=is_cas)
        assert tracked.position.squareoff_deadline < dt.datetime.combine(
            TRADE_DATE, broker_time, tzinfo=IST
        )

    @pytest.mark.parametrize("buffer", [0, -5, 1.5, "5"])
    def test_a_manager_with_no_buffer_is_refused(self, buffer: Any) -> None:
        """0 puts our deadline ON the broker's: the time exit would race it."""
        with pytest.raises(ValueError, match="exit_buffer_minutes"):
            PositionManager(
                protector=None,  # type: ignore[arg-type]
                store=Store(),
                mirror=Mirror(),
                calendar=_calendar(),
                exit_buffer_minutes=buffer,
            )


# ---------------------------------------------------------------------------
# AC2 (component) - at the deadline the stop goes, THEN the exit
# ---------------------------------------------------------------------------


class TestAc2TheTimerExitsAtTheDeadline:
    def test_nothing_happens_a_second_before_the_deadline(self) -> None:
        rig = Rig()
        rig.position()
        report = rig.sweep(NON_CAS_DEADLINE - dt.timedelta(seconds=1))
        assert rig.account.calls == []
        assert rig.account.position_reads == 0, "a sweep with nothing due reads nothing"
        assert report.exited == []

    def test_at_the_deadline_the_stop_is_cancelled_then_the_exit_sent(self) -> None:
        rig = Rig()
        position = rig.position()
        report = rig.sweep(NON_CAS_DEADLINE)
        assert rig.account.calls == ["cancel:STOP", "place:SQUAREOFF:SELL:40"]
        assert report.exited == ["INFY"]
        tracked = rig.manager.tracked("INFY")
        assert isinstance(tracked, ExitingPosition)
        assert tracked.reason is ExitReason.TIME
        exit_key = exit_client_order_id(position, trade_date=TRADE_DATE)
        assert rig.account.orders[exit_key].order_type is OrderType.MARKET

    def test_the_exit_carries_market_protection(self) -> None:
        """Build concern 2: without it Zerodha rejects the order - exactly
        when a rejection cannot be afforded."""
        rig = Rig()
        rig.position()
        rig.sweep(NON_CAS_DEADLINE)
        assert rig.account.last_request.market_protection == Decimal("-1")
        assert rig.account.last_request.order_type is OrderType.MARKET

    def test_the_exit_is_never_sent_while_the_stop_is_working(self) -> None:
        """Kite shows CANCEL PENDING as OPEN. Two working exits for one
        position could both fill and leave the account short."""
        rig = Rig()
        rig.position()
        rig.account.cancel_pending_reads = 50
        report = rig.sweep(NON_CAS_DEADLINE)
        assert rig.account.calls == ["cancel:STOP"]
        assert report.waiting == ["INFY"] and report.exited == []
        assert isinstance(rig.manager.tracked("INFY"), ProtectedPosition)
        assert Outcome.CANCELLING.value in rig.outcomes()
        assert len(rig.sleeps) == 4, "settled within the sweep, bounded"

    def test_once_the_cancel_is_visible_the_next_sweep_exits(self) -> None:
        rig = Rig()
        rig.position()
        rig.account.cancel_pending_reads = 6
        rig.sweep(NON_CAS_DEADLINE)
        rig.sweep(NON_CAS_DEADLINE + dt.timedelta(seconds=1))
        assert rig.account.calls == ["cancel:STOP", "place:SQUAREOFF:SELL:40"]

    def test_a_short_cancel_wait_still_exits_in_the_same_sweep(self) -> None:
        rig = Rig()
        rig.position()
        rig.account.cancel_pending_reads = 2
        report = rig.sweep(NON_CAS_DEADLINE)
        assert report.exited == ["INFY"]

    def test_a_stop_already_filling_is_the_exit(self) -> None:
        rig = Rig()
        position = rig.position(stop=False)
        rig.account.seed_stop(position, filled=15)
        report = rig.sweep(NON_CAS_DEADLINE)
        assert rig.account.calls == [], "a triggered stop is a market order already"
        assert report.waiting == ["INFY"]
        assert Outcome.STOP_FILLING.value in rig.outcomes()

    def test_a_stop_that_fills_as_it_is_cancelled_is_the_exit(self) -> None:
        rig = Rig()
        rig.position()
        rig.account.fill_on_cancel = True
        rig.sweep(NON_CAS_DEADLINE)
        assert rig.account.calls == ["cancel:STOP"], "no exit after the stop filled"
        # The REASON, not only the result: a filled stop also reads as live, so
        # "no exit" alone passed with the filling check deleted (mutation run).
        assert Outcome.STOP_FILLING.value in rig.outcomes()
        assert Outcome.CANCELLING.value not in rig.outcomes()

    def test_a_stop_cancelled_part_filled_is_not_the_exit(self) -> None:
        """Terminal with 15 of 40 filled: it sells nothing more, and the 25
        still held are unprotected. Exit what the broker holds - and halt,
        because the book cannot yet be reduced (E15-S08)."""
        rig = Rig()
        position = rig.position(stop=False, held=25)
        rig.account.seed_stop(position, filled=15, status=OrderStatus.CANCELLED)
        report = rig.sweep(NON_CAS_DEADLINE)
        assert rig.account.calls == ["place:SQUAREOFF:SELL:25"]
        assert report.exited == ["INFY"]
        assert rig.halter.armed == [HaltReason.POSITION_NOT_HELD]
        assert Outcome.STOP_FILLING.value not in rig.outcomes()

    def test_an_unverified_position_with_no_stop_is_exited(self) -> None:
        rig = Rig()
        rig.position(kind="unverified", stop=False)
        report = rig.sweep(NON_CAS_DEADLINE)
        assert rig.account.calls == ["place:SQUAREOFF:SELL:40"]
        assert report.exited == ["INFY"]

    def test_an_exiting_position_is_left_to_reconciliation(self) -> None:
        rig = Rig()
        rig.position(kind="exiting")
        rig.sweep(NON_CAS_DEADLINE)
        assert rig.account.calls == []

    def test_a_second_sweep_sends_nothing_more(self) -> None:
        rig = Rig()
        rig.position()
        rig.sweep(NON_CAS_DEADLINE)
        rig.sweep(NON_CAS_DEADLINE + dt.timedelta(seconds=1))
        assert rig.account.calls == ["cancel:STOP", "place:SQUAREOFF:SELL:40"]

    def test_each_position_goes_at_its_own_deadline(self) -> None:
        """Per-stock, never global: a CAS stock's deadline is ten minutes earlier."""
        rig = Rig()
        rig.position("TCS", deadline=CAS_DEADLINE)
        rig.position("INFY", deadline=NON_CAS_DEADLINE)
        first = rig.sweep(CAS_DEADLINE)
        assert first.exited == ["TCS"]
        assert isinstance(rig.manager.tracked("INFY"), ProtectedPosition)
        second = rig.sweep(NON_CAS_DEADLINE)
        assert second.exited == ["INFY"]

    def test_a_short_is_squared_off_with_a_buy(self) -> None:
        rig = Rig()
        rig.position(direction=Direction.SHORT)
        rig.sweep(NON_CAS_DEADLINE)
        assert rig.account.calls[-1] == "place:SQUAREOFF:BUY:40"

    def test_a_halt_never_stops_the_time_exit(self) -> None:
        """A halt stops new risk. It must never stop risk from being taken OFF."""
        rig = Rig()
        rig.position()
        rig.halter.armed.append(HaltReason.MANUAL)
        assert rig.sweep(NON_CAS_DEADLINE).exited == ["INFY"]

    def test_the_time_exit_is_counted_and_audited_once(self) -> None:
        rig = Rig()
        rig.position()
        rig.sweep(NON_CAS_DEADLINE)
        rig.sweep(NON_CAS_DEADLINE + dt.timedelta(seconds=1))
        assert rig.outcomes().count(Outcome.TIME_EXIT.value) == 1
        assert get_metrics().squareoff_exits_total._value.get() == 1


# ---------------------------------------------------------------------------
# Never exit more than the broker holds
# ---------------------------------------------------------------------------


class TestOnlyWhatTheBrokerHoldsIsExited:
    def test_a_position_the_broker_does_not_hold_sends_nothing_and_halts(self) -> None:
        """A row left OPEN after the broker closed the position outside our
        view. Exiting it would SELL 40 shares we do not own."""
        rig = Rig()
        rig.position(held=0)
        report = rig.sweep(NON_CAS_DEADLINE)
        assert rig.account.calls == []
        assert report.not_held == ["INFY"]
        assert rig.halter.armed == [HaltReason.POSITION_NOT_HELD]
        assert Outcome.NOT_HELD.value in rig.outcomes()

    def test_it_is_not_re_asked_every_second(self) -> None:
        """Halted and nothing to send: re-reading the broker each sweep would
        spend the data budget reconciliation needs."""
        rig = Rig()
        rig.position(held=0)
        rig.sweep(NON_CAS_DEADLINE)
        reads = rig.account.position_reads
        rig.sweep(NON_CAS_DEADLINE + dt.timedelta(seconds=1))
        assert rig.account.position_reads == reads
        assert rig.halter.armed == [HaltReason.POSITION_NOT_HELD], "armed once here"

    def test_a_partial_holding_is_exited_for_what_is_held_and_halts(self) -> None:
        rig = Rig()
        rig.position(held=25)
        rig.sweep(NON_CAS_DEADLINE)
        assert rig.account.calls == ["cancel:STOP", "place:SQUAREOFF:SELL:25"]
        assert rig.halter.armed == [HaltReason.POSITION_NOT_HELD]

    def test_a_stop_that_filled_before_the_deadline_is_not_a_missing_position(self) -> None:
        """The control for the order of the checks: the stop filled, the
        broker is flat, and reconciliation has not closed the record yet.
        That is an ordinary stop-out, not a reason to halt the day."""
        rig = Rig()
        position = rig.position(held=0, stop=False)
        rig.account.seed_stop(position, filled=40)
        report = rig.sweep(NON_CAS_DEADLINE)
        assert rig.halter.armed == []
        assert report.not_held == []
        assert Outcome.STOP_FILLING.value in rig.outcomes()

    def test_the_owners_delivery_shares_are_not_ours_to_exit(self) -> None:
        rig = Rig()
        rig.position(held=0)
        rig.account.hold("INFY", 40, product="CNC")
        rig.sweep(NON_CAS_DEADLINE)
        assert rig.account.calls == []
        assert rig.halter.armed == [HaltReason.POSITION_NOT_HELD]


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


class TestWhenSomethingFails:
    def test_unreadable_positions_send_nothing_and_keep_the_stop(self) -> None:
        rig = Rig()
        rig.position()
        rig.account.fail_positions = True
        report = rig.sweep(NON_CAS_DEADLINE)
        assert rig.account.calls == [], "the stop must not be cancelled blind"
        assert report.errors
        assert Outcome.UNREADABLE.value in rig.outcomes()

    def test_a_failed_cancel_leaves_the_stop_and_retries_next_sweep(self) -> None:
        rig = Rig()
        rig.position()
        rig.account.fail_cancel = True
        report = rig.sweep(NON_CAS_DEADLINE)
        assert rig.account.calls == []
        assert isinstance(rig.manager.tracked("INFY"), ProtectedPosition)
        assert report.errors
        rig.account.fail_cancel = False
        assert rig.sweep(NON_CAS_DEADLINE + dt.timedelta(seconds=1)).exited == ["INFY"]

    def test_an_exit_the_broker_does_not_list_takes_the_emergency_path(self) -> None:
        rig = Rig()
        rig.position()
        rig.account.hide_exits = True
        rig.sweep(NON_CAS_DEADLINE)
        assert [t.symbol for t, _ in rig.emergency.exits] == ["INFY"]
        assert Outcome.EXIT_FAILED.value in rig.outcomes()
        tracked = rig.manager.tracked("INFY")
        assert isinstance(tracked, ExitingPosition) and tracked.reason is ExitReason.TIME

    def test_an_emergency_exit_that_fails_leaves_the_label_alone(self) -> None:
        """exit_now raises having halted: there is no live exit to claim."""
        rig = Rig()
        rig.position()
        rig.account.hide_exits = True
        rig.emergency.raises = RuntimeError("exit refused")
        report = rig.sweep(NON_CAS_DEADLINE)
        assert report.errors
        assert isinstance(rig.manager.tracked("INFY"), ProtectedPosition)

    def test_one_failure_does_not_stop_the_other_positions(self) -> None:
        rig = Rig()
        rig.position("TCS", deadline=CAS_DEADLINE)
        rig.position("INFY", deadline=CAS_DEADLINE)
        rig.account.hide_exits = True
        rig.emergency.raises = RuntimeError("exit refused")
        report = rig.sweep(CAS_DEADLINE)
        assert sorted(t.symbol for t, _ in rig.emergency.exits) == ["INFY", "TCS"]
        assert len(report.errors) == 2

    def test_a_naive_clock_is_refused(self) -> None:
        with pytest.raises(ValueError, match="naive"):
            Rig().sweep(dt.datetime(2026, 8, 25, 9, 45))


# ---------------------------------------------------------------------------
# Task 5 - alert on a position that survives its deadline
# ---------------------------------------------------------------------------


class TestAPositionPastItsDeadlineIsAlerted:
    def test_a_minute_past_the_deadline_it_is_alerted_once(self) -> None:
        rig = Rig()
        rig.position(kind="exiting")
        rig.sweep(NON_CAS_DEADLINE + dt.timedelta(seconds=61))
        rig.sweep(NON_CAS_DEADLINE + dt.timedelta(seconds=62))
        assert rig.outcomes().count(Outcome.OVERDUE.value) == 1
        assert get_metrics().positions_overdue_total._value.get() == 1

    def test_inside_the_minute_it_is_not(self) -> None:
        rig = Rig()
        rig.position(kind="exiting")
        report = rig.sweep(NON_CAS_DEADLINE + dt.timedelta(seconds=59))
        assert report.overdue == []

    def test_a_position_that_closed_in_the_sweep_is_not_overdue(self) -> None:
        rig = Rig()
        rig.position(held=0, stop=False)
        rig.manager._book.clear()
        assert rig.sweep(NON_CAS_DEADLINE + dt.timedelta(minutes=5)).overdue == []

    def test_its_settings_are_validated(self) -> None:
        with pytest.raises(ValueError, match="overdue_after_seconds"):
            SquareOffTimer(
                gateway=None,  # type: ignore[arg-type]
                broker=None,  # type: ignore[arg-type]
                manager=None,  # type: ignore[arg-type]
                protector=None,  # type: ignore[arg-type]
                halter=None,  # type: ignore[arg-type]
                overdue_after_seconds=0,
            )
        with pytest.raises(ValueError, match="cancel_settle_attempts"):
            SquareOffTimer(
                gateway=None,  # type: ignore[arg-type]
                broker=None,  # type: ignore[arg-type]
                manager=None,  # type: ignore[arg-type]
                protector=None,  # type: ignore[arg-type]
                halter=None,  # type: ignore[arg-type]
                cancel_settle_attempts=0,
            )


class TestTheAuditRows:
    def test_every_label_fits_the_table(self) -> None:
        from algotrader.common.db.models import DecisionLog

        width = DecisionLog.__table__.c.outcome.type.length
        assert all(len(o.value) <= width for o in Outcome)

    def test_a_failing_audit_sink_does_not_stop_the_exit(self) -> None:
        rig = Rig()
        rig.position()

        async def broken(entry: AuditEntry) -> None:
            raise RuntimeError("audit database down")

        rig.timer._audit = broken
        assert rig.sweep(NON_CAS_DEADLINE).exited == ["INFY"]


class TestItsMemoryIsBounded:
    def test_what_a_closed_position_recorded_is_forgotten(self) -> None:
        """A process that runs for months must not keep every day's records."""
        rig = Rig()
        rig.position(kind="exiting")
        rig.sweep(NON_CAS_DEADLINE + dt.timedelta(seconds=61))
        assert rig.timer._recorded
        rig.manager._book.clear()
        rig.sweep(NON_CAS_DEADLINE + dt.timedelta(seconds=62))
        assert rig.timer._recorded == set()
