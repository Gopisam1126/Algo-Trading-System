"""E15-S15 task 1: after the books close, is anything left - and does it stop
the next session?

The pure verdict (``judge``) one kind of leftover at a time, each with its
control; the run's sequence (one cycle, THEN a fresh read, orderbook first);
and every way the run can fail to see the book, each of which must read as
NOT flat. The real reconciler is in
``tests/integration/test_execution_service_wiring.py`` (AC1).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from algotrader.broker.adapter import BrokerPosition
from algotrader.common.audit import AuditEntry
from algotrader.common.calendar import IST, MarketCalendar, load_holidays_with_status
from algotrader.common.enums import (
    Direction,
    OrderIntent,
    OrderStatus,
    OrderType,
    Product,
    Side,
)
from algotrader.common.models.trading import Order, Position
from algotrader.execution import end_of_day as eod
from algotrader.execution.end_of_day import (
    MAX_NAMED,
    SETTLED,
    EndOfDay,
    FlatVerdict,
    Leftover,
    LeftoverKind,
    books_close,
    judge,
)
from algotrader.execution.halt import HaltReason
from algotrader.execution.positions import UnverifiedPosition
from algotrader.execution.reconciliation import CycleReport

DAY = dt.date(2026, 8, 25)  # a Tuesday, CAS in force (auction to 15:35)


def _ist(day: dt.date, hh: int, mm: int, ss: int = 0) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hh, mm, ss), tzinfo=IST).astimezone(dt.UTC)


AFTER = _ist(DAY, 15, 40)


@pytest.fixture(scope="module")
def calendar() -> MarketCalendar:
    path = Path(__file__).resolve().parents[2] / "config" / "nse_holidays.yaml"
    status = load_holidays_with_status(str(path))
    return MarketCalendar(status.dates, covers_years=status.covers_years)


def _position(symbol: str = "INFY") -> Position:
    return Position(
        position_id=7,
        correlation_id=uuid4(),
        symbol=symbol,
        slot_index=0,
        direction=Direction.LONG,
        quantity=40,
        entry_price=Decimal("1200.0000"),
        stop_price=Decimal("1186.4500"),
        opened_at=_ist(DAY, 10, 30),
        squareoff_deadline=_ist(DAY, 15, 15),
    )


def _held(
    symbol: str = "INFY", quantity: int = 40, *, product: str = "MIS", exchange: str = "NSE"
) -> BrokerPosition:
    return BrokerPosition(
        symbol=symbol,
        exchange=exchange,
        product=product,
        quantity=quantity,
        day_buy_quantity=max(quantity, 0),
        day_sell_quantity=0,
    )


def _order(
    status: OrderStatus = OrderStatus.OPEN,
    *,
    symbol: str = "INFY",
    product: Product = Product.MIS,
    filled: int = 0,
) -> Order:
    return Order(
        client_order_id="k-stop-1",
        broker_order_id="B-1",
        correlation_id=uuid4(),
        symbol=symbol,
        side=Side.SELL,
        order_type=OrderType.SLM,
        product=product,
        quantity=40,
        trigger_price=Decimal("1186.40"),
        status=status,
        filled_quantity=filled,
        intent=OrderIntent.STOP,
        placed_at=_ist(DAY, 10, 31),
        last_update_at=_ist(DAY, 10, 31),
    )


def _ours(status: str, *, symbol: str = "INFY") -> dict[str, Any]:
    return {
        "client_order_id": f"k-{status.lower()}",
        "symbol": symbol,
        "status": status,
        "intent": "STOP",
        "side": "SELL",
        "filled_quantity": 0,
        "average_price": None,
        "correlation_id": uuid4(),
    }


def _judge(**parts: Any) -> tuple[Leftover, ...]:
    base: dict[str, Any] = {"book": (), "records": (), "positions": (), "orderbook": (), "ours": ()}
    return judge(**(base | parts))


# ---------------------------------------------------------------------------
# When the books close - against NSE's own schedule, written by hand
# ---------------------------------------------------------------------------


class TestBooksClose:
    def test_before_the_closing_auction_existed_it_is_the_close(self) -> None:
        assert books_close(dt.date(2026, 7, 31)) == _ist(dt.date(2026, 7, 31), 15, 30)

    def test_from_3_aug_2026_the_auction_runs_to_1535(self) -> None:
        assert books_close(DAY) == _ist(DAY, 15, 35)

    def test_from_7_dec_2026_to_1540(self) -> None:
        """NSE's change of 6 Nov 2026: CAS 15:30-15:40. The runbook's 15:35
        would judge while the auction can still execute."""
        assert books_close(dt.date(2026, 12, 7)) == _ist(dt.date(2026, 12, 7), 15, 40)


# ---------------------------------------------------------------------------
# judge - one leftover at a time, each with its control
# ---------------------------------------------------------------------------


class TestJudge:
    def test_nothing_left_is_flat(self) -> None:
        assert _judge() == ()

    def test_a_position_the_book_still_tracks(self) -> None:
        (item,) = _judge(book=[UnverifiedPosition(position=_position())])
        assert item.kind is LeftoverKind.BOOK_POSITION and item.symbol == "INFY"
        assert "UnverifiedPosition" in item.detail and "40" in item.detail

    def test_a_positions_row_still_open(self) -> None:
        (item,) = _judge(records=[{"symbol": "TCS", "position_id": 12, "status": "OPEN"}])
        assert (item.kind, item.symbol) == (LeftoverKind.OPEN_RECORD, "TCS")
        assert "12" in item.detail

    def test_mis_quantity_at_the_broker(self) -> None:
        (item,) = _judge(positions=[_held(quantity=-25)])
        assert item.kind is LeftoverKind.BROKER_POSITION and "-25" in item.detail

    def test_control_a_round_trip_kite_keeps_at_zero_is_not_a_position(self) -> None:
        assert _judge(positions=[_held(quantity=0)]) == ()

    @pytest.mark.parametrize("product", ["CNC", "NRML", "MTF"])
    def test_control_the_owners_other_products_are_not_ours_to_judge(self, product) -> None:
        assert _judge(positions=[_held(product=product)]) == ()

    def test_mis_on_any_exchange_counts(self) -> None:
        (item,) = _judge(positions=[_held(exchange="BSE")])
        assert "BSE" in item.detail

    @pytest.mark.parametrize(
        "status",
        [s for s in OrderStatus if not s.is_terminal],
    )
    def test_any_order_the_broker_lists_unfinished_is_working(self, status) -> None:
        (item,) = _judge(orderbook=[_order(status)])
        assert item.kind is LeftoverKind.WORKING_ORDER and status.value in item.detail

    @pytest.mark.parametrize("status", [s for s in OrderStatus if s.is_terminal])
    def test_control_a_finished_order_is_not(self, status) -> None:
        assert (
            _judge(orderbook=[_order(status, filled=40 if status is OrderStatus.FILLED else 0)])
            == ()
        )

    def test_control_a_working_delivery_order_is_not_ours(self) -> None:
        assert _judge(orderbook=[_order(product=Product.CNC)]) == ()

    @pytest.mark.parametrize("status", sorted(s.value for s in OrderStatus if s not in SETTLED))
    def test_every_unsettled_record_is_named(self, status: str) -> None:
        (item,) = _judge(ours=[_ours(status)])
        assert item.kind is LeftoverKind.UNSETTLED_ORDER and status in item.detail

    @pytest.mark.parametrize("status", sorted(s.value for s in SETTLED))
    def test_control_a_settled_record_is_not(self, status: str) -> None:
        assert _judge(ours=[_ours(status)]) == ()

    def test_settled_is_exactly_the_finished_statuses_and_submit_failed(self) -> None:
        assert SETTLED == {s for s in OrderStatus if s.is_terminal} | {OrderStatus.SUBMIT_FAILED}

    def test_a_status_nobody_listed_is_unsettled(self) -> None:
        """An allowlist: a value the enum does not know is not finished."""
        (item,) = _judge(ours=[_ours("PARKED")])
        assert item.kind is LeftoverKind.UNSETTLED_ORDER

    def test_each_kind_is_reported_together(self) -> None:
        items = _judge(
            book=[UnverifiedPosition(position=_position())],
            records=[{"symbol": "INFY", "position_id": 7, "status": "OPEN"}],
            positions=[_held()],
            orderbook=[_order()],
            ours=[_ours("OPEN")],
        )
        assert [i.kind for i in items] == [
            LeftoverKind.BOOK_POSITION,
            LeftoverKind.OPEN_RECORD,
            LeftoverKind.BROKER_POSITION,
            LeftoverKind.WORKING_ORDER,
            LeftoverKind.UNSETTLED_ORDER,
        ]

    def test_a_broker_symbol_cannot_forge_a_log_line(self) -> None:
        (item,) = _judge(positions=[_held(symbol="INFY\nCRITICAL kill switch disarmed")])
        assert "\n" not in item.describe() and "\\n" in item.describe()


class TestTheVerdictIsBounded:
    def test_it_names_at_most_max_named_and_counts_the_rest(self) -> None:
        items = tuple(
            Leftover.of(LeftoverKind.BROKER_POSITION, f"S{i}", "x") for i in range(MAX_NAMED + 5)
        )
        text = FlatVerdict(trade_date=DAY, at=AFTER, leftovers=items).describe()
        named = [f"S{i}" for i in range(MAX_NAMED + 5) if f" S{i}:" in text]
        assert len(named) == MAX_NAMED
        assert f"{MAX_NAMED + 5} left" in text and "and 5 more" in text

    def test_control_a_short_list_names_everything_and_no_tail(self) -> None:
        items = (Leftover.of(LeftoverKind.BROKER_POSITION, "S0", "x"),)
        text = FlatVerdict(trade_date=DAY, at=AFTER, leftovers=items).describe()
        assert "S0" in text and "more" not in text

    def test_flat_means_no_leftovers_at_all(self) -> None:
        assert FlatVerdict(trade_date=DAY, at=AFTER, leftovers=()).flat
        unseen = (Leftover.of(LeftoverKind.UNVERIFIED, "-", "could not read"),)
        assert not FlatVerdict(trade_date=DAY, at=AFTER, leftovers=unseen).flat


# ---------------------------------------------------------------------------
# The run - doubles that record what happened, and in what order
# ---------------------------------------------------------------------------


class Broker:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.positions: list[BrokerPosition] = []
        self.orderbook: list[Order] = []
        self.fail: Exception | None = None
        #: Called after the FIRST read, whichever it is - for the race test.
        self.between: Any = None

    def _read(self, name: str) -> None:
        self.log.append(name)
        if self.fail is not None:
            raise self.fail
        if self.between is not None:
            between, self.between = self.between, None
            between()

    async def fetch_orderbook(self) -> list[Order]:
        snapshot = list(self.orderbook)
        self._read("orderbook")
        return snapshot

    async def fetch_positions(self) -> list[BrokerPosition]:
        snapshot = list(self.positions)
        self._read("positions")
        return snapshot

    def order_key(self, client_order_id: str) -> str:
        return client_order_id[:20]


class Reconciler:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.broker = Broker(log)
        self.fail: Exception | None = None
        self.errors: list[str] = []

    async def run_cycle(self, *, now: dt.datetime) -> CycleReport:
        self.log.append("cycle")
        if self.fail is not None:
            raise self.fail
        return CycleReport(cycle_id=uuid4(), at=now, errors=list(self.errors))


class Records:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.orders: list[dict[str, Any]] = []
        self.closed: list[dict[str, Any]] = []
        self.open_rows: list[dict[str, Any]] = []
        self.fail = False

    async def orders_since(self, since: dt.datetime) -> list[dict[str, Any]]:
        self.log.append("orders")
        if self.fail:
            raise RuntimeError("database unavailable")
        return list(self.orders)

    async def closed_since(self, since: dt.datetime) -> list[dict[str, Any]]:
        self.log.append("closed")
        return list(self.closed)

    async def open_position_rows(self) -> list[dict[str, Any]]:
        self.log.append("open_rows")
        return list(self.open_rows)


class Book:
    def __init__(self) -> None:
        self.held: list[Any] = []

    def open_positions(self) -> list[Any]:
        return list(self.held)


class Halter:
    def __init__(self) -> None:
        self.armed: list[tuple[HaltReason, str, str]] = []
        self.fail = False

    async def arm(self, reason, *, detail: str, armed_by: str, now: dt.datetime) -> object:
        if self.fail:
            raise ConnectionError("redis down")
        self.armed.append((reason, detail, armed_by))
        return None


class Prices:
    def __init__(self) -> None:
        self.asked: list[str] = []
        self.price: Decimal | None = None
        self.fail = False

    async def last_price(self, symbol: str, *, now: dt.datetime) -> Decimal | None:
        self.asked.append(symbol)
        if self.fail:
            raise TimeoutError("quote read")
        return self.price


class Harness:
    def __init__(self, calendar: MarketCalendar) -> None:
        self.log: list[str] = []
        self.reconciler = Reconciler(self.log)
        self.broker = self.reconciler.broker
        self.records = Records(self.log)
        self.book = Book()
        self.halter = Halter()
        self.prices = Prices()
        self.audited: list[AuditEntry] = []

        async def audit(entry: AuditEntry) -> None:
            self.audited.append(entry)

        self.eod = EndOfDay(
            reconciler=self.reconciler,
            records=self.records,
            book=self.book,
            halter=self.halter,
            calendar=calendar,
            prices=self.prices,
            audit=audit,
        )

    async def run(self, now: dt.datetime = AFTER) -> eod.DayReport:
        return await self.eod.run(now=now)


@pytest.fixture
def h(calendar: MarketCalendar) -> Harness:
    return Harness(calendar)


class TestWhenItMayRun:
    async def test_refused_before_the_books_close(self, h: Harness) -> None:
        """At 15:34 the closing auction can still execute: every position
        about to be squared off would be a leftover, and tomorrow halted."""
        with pytest.raises(ValueError, match="books closed"):
            await h.run(_ist(DAY, 15, 34, 59))
        assert h.log == [] and h.halter.armed == []

    async def test_control_from_the_moment_they_close(self, h: Harness) -> None:
        report = await h.run(_ist(DAY, 15, 35))
        assert report.verdict.flat

    async def test_refused_on_a_day_with_no_session(self, h: Harness) -> None:
        saturday = dt.date(2026, 8, 29)
        with pytest.raises(ValueError):
            await h.run(_ist(saturday, 16, 0))
        assert h.eod.due(_ist(saturday, 16, 0)) is None

    async def test_refused_on_a_naive_instant(self, h: Harness) -> None:
        with pytest.raises(ValueError, match="naive"):
            await h.run(dt.datetime(2026, 8, 25, 16, 0))

    def test_due_names_the_ist_trading_day(self, h: Harness) -> None:
        assert h.eod.due(_ist(DAY, 23, 59)) == DAY
        assert h.eod.due(_ist(DAY, 15, 30)) is None


class TestTheSequence:
    async def test_one_cycle_then_the_orderbook_then_positions(self, h: Harness) -> None:
        await h.run()
        assert h.log[:3] == ["cycle", "orderbook", "positions"]
        assert set(h.log[3:]) == {"orders", "open_rows", "closed"}
        assert h.log.count("cycle") == 1

    async def test_an_order_that_fills_between_the_reads_is_not_missed(self, h: Harness) -> None:
        """The design record's race. At the first read the stop is WORKING and
        the broker flat; by the second it has filled and the broker holds the
        short it opened. Read orderbook first and the working order is named;
        read positions first and both reads look finished - a false FLAT."""
        h.broker.orderbook = [_order(OrderStatus.OPEN)]

        def fill() -> None:
            h.broker.orderbook = [_order(OrderStatus.FILLED, filled=40)]
            h.broker.positions = [_held(quantity=-40)]

        h.broker.between = fill
        report = await h.run()
        kinds = [i.kind for i in report.verdict.leftovers]
        assert LeftoverKind.WORKING_ORDER in kinds
        assert not report.verdict.flat


class TestAFlatDay:
    async def test_flat_no_halt_and_the_audit_row_says_so(self, h: Harness) -> None:
        cid = uuid4()
        h.records.orders = [
            {
                **_ours("FILLED"),
                "correlation_id": cid,
                "side": "BUY",
                "filled_quantity": 100,
                "average_price": Decimal("1000"),
            },
            {
                **_ours("FILLED"),
                "correlation_id": cid,
                "side": "SELL",
                "filled_quantity": 100,
                "average_price": Decimal("1010"),
            },
        ]
        report = await h.run()
        assert report.verdict.flat and h.halter.armed == []
        assert report.statistics is not None and report.statistics.net == Decimal("917.02")
        (entry,) = h.audited
        assert (entry.stage, entry.outcome, entry.service) == ("END_OF_DAY", "FLAT", "end-of-day")
        assert entry.payload["flat"] is True
        assert entry.payload["statistics"]["net"] == "917.02"
        assert entry.payload["statistics"]["per_trade"][0]["r"] is None

    async def test_no_price_is_asked_for_on_a_flat_day_of_closed_trips(self, h: Harness) -> None:
        """Prices are asked for symbols with fills, and a closed trip never
        reads one: the net is known whatever the quote feed says after 15:30."""
        h.prices.fail = True
        cid = uuid4()
        h.records.orders = [
            {
                **_ours("FILLED"),
                "correlation_id": cid,
                "side": "BUY",
                "filled_quantity": 1,
                "average_price": Decimal("100"),
            },
            {
                **_ours("FILLED"),
                "correlation_id": cid,
                "side": "SELL",
                "filled_quantity": 1,
                "average_price": Decimal("100"),
            },
        ]
        report = await h.run()
        assert report.statistics is not None and report.statistics.net is not None

    async def test_the_day_and_its_correlation_are_the_same_on_a_rerun(self, h: Harness) -> None:
        await h.run()
        await h.run(AFTER + dt.timedelta(hours=1))
        first, second = h.audited
        assert first.correlation_id == second.correlation_id


class TestANotFlatDay:
    async def test_a_leftover_arms_book_not_flat_naming_it(self, h: Harness) -> None:
        h.broker.positions = [_held("TCS", 25)]
        report = await h.run()
        assert not report.verdict.flat
        ((reason, detail, armed_by),) = h.halter.armed
        assert reason is HaltReason.BOOK_NOT_FLAT and armed_by == "end-of-day"
        assert "TCS" in detail and "BROKER_POSITION" in detail
        (entry,) = h.audited
        assert entry.outcome == "NOT_FLAT" and entry.payload["leftover_count"] == 1

    async def test_the_final_cycle_failing_is_not_flat(self, h: Harness) -> None:
        h.reconciler.fail = ConnectionError("broker down")
        report = await h.run()
        assert report.cycle is None
        assert [i.kind for i in report.verdict.leftovers] == [LeftoverKind.UNVERIFIED]
        assert h.halter.armed and "ConnectionError" in h.halter.armed[0][1]

    async def test_a_cycle_that_did_not_finish_is_not_flat(self, h: Harness) -> None:
        h.reconciler.errors = ["order k-1: boom"]
        report = await h.run()
        assert [i.kind for i in report.verdict.leftovers] == [LeftoverKind.UNVERIFIED]
        assert "k-1" in report.verdict.leftovers[0].detail

    async def test_a_failed_read_is_not_flat_and_still_names_the_book(self, h: Harness) -> None:
        h.broker.fail = ConnectionError("timeout")
        h.book.held = [UnverifiedPosition(position=_position("SBIN"))]
        report = await h.run()
        assert [i.kind for i in report.verdict.leftovers] == [
            LeftoverKind.UNVERIFIED,
            LeftoverKind.BOOK_POSITION,
        ]
        assert report.statistics is None
        assert h.halter.armed

    async def test_unreadable_records_are_not_flat(self, h: Harness) -> None:
        h.records.fail = True
        report = await h.run()
        assert not report.verdict.flat and h.halter.armed

    async def test_a_halt_that_cannot_be_armed_raises_so_it_is_retried(self, h: Harness) -> None:
        h.broker.positions = [_held()]
        h.halter.fail = True
        with pytest.raises(ConnectionError):
            await h.run()

    async def test_an_audit_that_raises_does_not_undo_the_halt(self, h: Harness) -> None:
        async def broken(_entry: AuditEntry) -> None:
            raise RuntimeError("audit down")

        h.eod._audit = broken
        h.broker.positions = [_held()]
        report = await h.run()
        assert not report.verdict.flat and h.halter.armed

    async def test_an_open_trip_with_no_price_leaves_the_net_unknown_not_zero(
        self, h: Harness
    ) -> None:
        h.records.orders = [
            {
                **_ours("FILLED"),
                "side": "BUY",
                "filled_quantity": 40,
                "average_price": Decimal("1200"),
            },
        ]
        h.broker.positions = [_held()]
        report = await h.run()
        assert h.prices.asked == ["INFY"]
        assert report.statistics is not None and report.statistics.net is None
        assert report.statistics.still_open == ("INFY",)


class TestNothingSecretReachesTheSinksTheLogFilterNeverSees:
    #: Assembled at runtime so no scanner reads it as a committed credential
    #: (the convention in test_log_redaction.py).
    TOKEN = "AbCd" + "Ef0123456789xyzQRST"

    async def test_a_cycle_errors_credential_is_redacted_in_the_halt_and_the_audit(
        self, h: Harness
    ) -> None:
        """A cycle error carries an exception's text, and a broker exception
        can carry a request line. The halt record (Redis) and the audit row
        (decision_log) never pass the logging filter (REC-003)."""
        h.reconciler.errors = ["order k-1: GET /orders?" + "access_" + "token=" + self.TOKEN]
        report = await h.run()
        ((_reason, detail, _by),) = h.halter.armed
        assert self.TOKEN not in detail
        assert self.TOKEN not in str(h.audited[0].payload)
        assert self.TOKEN not in report.verdict.describe()
        assert "k-1" in detail, "the rest of the error survives"
