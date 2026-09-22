"""E15-S05: the book, tested one acceptance criterion at a time.

The doubles here record what the manager *did* rather than answering with the
manager's own helpers. That is deliberate and it is QA-E15-12's lesson from
E15-S04: a double that shares an implementation with the system under test
agrees with it by construction and proves nothing.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from algotrader.common.enums import (
    Direction,
    ExitReason,
    OrderIntent,
    OrderStatus,
    OrderType,
    PositionStatus,
    Product,
    Side,
)
from algotrader.common.metrics import get_metrics, reset_metrics_for_testing
from algotrader.common.models.trading import Order, Position, SizingResult
from algotrader.execution.positions import (
    ContradictoryFillError,
    ExitingPosition,
    Marks,
    NotFilledError,
    PositionManager,
    PositionSnapshot,
    ProtectedPosition,
    UnprotectableFillError,
    UnverifiedPosition,
    confirm_fill,
)
from algotrader.execution.protective_stop import (
    EstablishedPosition,
    NakedPositionError,
    StopNotEstablishedError,
)

TRADING_DAY = dt.date(2026, 8, 25)
DEADLINE = dt.datetime(2026, 8, 25, 9, 45, tzinfo=dt.UTC)
NOW = dt.datetime(2026, 8, 25, 5, 0, tzinfo=dt.UTC)
CID = uuid4()


@pytest.fixture(autouse=True)
def _fresh_metrics() -> None:
    reset_metrics_for_testing()


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class RecordingStore:
    """The positions table, as the manager is allowed to see it."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.inserts: list[dict[str, Any]] = []
        self.closes: list[dict[str, Any]] = []
        self.rows = rows or []
        self.next_id = 41

    async def open_position(self, position: dict[str, Any]) -> int:
        self.inserts.append(dict(position))
        self.next_id += 1
        return self.next_id

    async def open_positions(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.rows]

    async def close_position(self, position_id: int, **kwargs: Any) -> None:
        self.closes.append({"position_id": position_id, **kwargs})


class RecordingMirror:
    def __init__(self) -> None:
        self.writes: list[tuple[str, PositionSnapshot]] = []
        self.clears: list[str] = []

    async def write(self, symbol: str, snapshot: PositionSnapshot) -> None:
        self.writes.append((symbol, snapshot))

    async def clear(self, symbol: str) -> None:
        self.clears.append(symbol)

    def last(self, symbol: str) -> PositionSnapshot:
        return [s for sym, s in self.writes if sym == symbol][-1]


class FakeProtector:
    """Stands in for ``ProtectiveStop``. Records, and fails when told to."""

    def __init__(self, raises: Exception | None = None) -> None:
        self.attached: list[Position] = []
        self.exited: list[Any] = []
        self.raises = raises
        self.order: list[str] = []

    async def attach(
        self, position: Position, *, trade_date: dt.date, now: dt.datetime
    ) -> EstablishedPosition:
        self.attached.append(position)
        self.order.append("attach")
        if self.raises is not None:
            raise self.raises
        return EstablishedPosition(
            position=position,
            stop_client_order_id="STOPCID",
            stop_broker_order_id="BROKER-1",
            established_at=now,
        )

    async def exit_now(
        self, target: Any, *, trade_date: dt.date, now: dt.datetime, because: str
    ) -> None:
        self.exited.append((target, because))


class StubCalendar:
    """Records the flags it was asked with; the deadline maths is E04's."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def squareoff_deadline(self, trade_date: dt.date, **kwargs: Any) -> dt.datetime:
        self.calls.append({"trade_date": trade_date, **kwargs})
        return DEADLINE


def _manager(
    *,
    protector: FakeProtector | None = None,
    store: RecordingStore | None = None,
    mirror: RecordingMirror | None = None,
    calendar: StubCalendar | None = None,
    max_quote_age_seconds: float = 60.0,
) -> tuple[PositionManager, FakeProtector, RecordingStore, RecordingMirror, StubCalendar]:
    protector = protector or FakeProtector()
    store = store or RecordingStore()
    mirror = mirror or RecordingMirror()
    calendar = calendar or StubCalendar()
    manager = PositionManager(
        protector=protector,  # type: ignore[arg-type]
        store=store,
        mirror=mirror,
        calendar=calendar,
        exit_buffer_minutes=5,
        max_quote_age_seconds=max_quote_age_seconds,
        metrics=get_metrics(),
    )
    return manager, protector, store, mirror, calendar


def _order(
    *,
    quantity: int = 100,
    filled: int = 100,
    price: str | None = "1200.0000",
    status: OrderStatus = OrderStatus.FILLED,
    side: Side = Side.BUY,
    intent: OrderIntent = OrderIntent.ENTRY,
    symbol: str = "INFY",
) -> Order:
    return Order(
        client_order_id="CID123",
        broker_order_id="BROKER-9",
        correlation_id=CID,
        symbol=symbol,
        side=side,
        order_type=OrderType.MARKET,
        product=Product.MIS,
        quantity=quantity,
        status=status,
        filled_quantity=filled,
        average_price=Decimal(price) if price is not None else None,
        intent=intent,
        placed_at=NOW,
        last_update_at=NOW,
    )


def _sizing(stop: str = "1186.4500", target: str | None = "1230.0000") -> SizingResult:
    return SizingResult(
        quantity=100,
        entry_price=Decimal("1200.0000"),
        stop_price=Decimal(stop),
        target_price=Decimal(target) if target else None,
        capital_at_risk=Decimal("1355.00"),
        binding_constraint="risk_per_trade",
    )


def _position(
    *, quantity: int = 100, entry: str = "1200.0000", stop: str = "1186.4500"
) -> Position:
    return Position(
        position_id=42,
        correlation_id=CID,
        symbol="INFY",
        slot_index=0,
        direction=Direction.LONG,
        quantity=quantity,
        entry_price=Decimal(entry),
        stop_price=Decimal(stop),
        opened_at=NOW,
        squareoff_deadline=DEADLINE,
    )


async def _open(manager: PositionManager, order: Order, sizing: SizingResult) -> Any:
    return await manager.open_from_fill(
        order,
        sizing=sizing,
        slot_index=0,
        trade_date=TRADING_DAY,
        now=NOW,
        is_cas_stock=False,
    )


# ---------------------------------------------------------------------------
# AC1 — a position is opened for what FILLED
# ---------------------------------------------------------------------------


class TestAc1TheQuantityThatFilled:
    @pytest.mark.asyncio
    async def test_a_partial_fill_opens_the_position_that_exists(self) -> None:
        """The defect this story exists to prevent, stated as its test.

        A position recorded as 100 when 40 filled gets a 100-share stop, and
        that stop sells 60 shares that do not exist when it triggers.
        """
        manager, protector, store, _mirror, _cal = _manager()
        tracked = await _open(
            manager, _order(quantity=100, filled=40, price="1201.3000"), _sizing()
        )
        assert tracked.position.quantity == 40
        assert tracked.position.entry_price == Decimal("1201.3000")
        assert protector.attached[0].quantity == 40, "the STOP was sized for the wrong holding"
        assert store.inserts[0]["quantity"] == 40

    @pytest.mark.asyncio
    async def test_a_complete_fill_opens_the_full_quantity(self) -> None:
        """The control. Without it, a manager that always opened 40 would pass
        the test above and nothing else would notice."""
        manager, protector, _s, _m, _c = _manager()
        tracked = await _open(manager, _order(quantity=100, filled=100), _sizing())
        assert tracked.position.quantity == 100
        assert protector.attached[0].quantity == 100

    @pytest.mark.asyncio
    async def test_an_unfilled_entry_opens_nothing_at_all(self) -> None:
        manager, protector, store, mirror, _c = _manager()
        with pytest.raises(NotFilledError):
            await _open(manager, _order(filled=0, status=OrderStatus.CANCELLED), _sizing())
        assert manager.open_positions() == []
        assert store.inserts == []
        assert mirror.writes == []
        assert protector.attached == []

    @pytest.mark.asyncio
    async def test_a_cancelled_order_that_partly_filled_is_still_a_position(self) -> None:
        """§8.2's documented case: ``PARTIAL -> CANCELLED`` at square-off, and
        the filled quantity is real stock. A status-driven implementation opens
        nothing here and is wrong by a whole position."""
        manager, _p, _s, _m, _c = _manager()
        tracked = await _open(
            manager, _order(quantity=100, filled=30, status=OrderStatus.CANCELLED), _sizing()
        )
        assert tracked.position.quantity == 30

    @pytest.mark.asyncio
    async def test_an_open_order_that_partly_filled_is_still_a_position(self) -> None:
        """Kite reports OPEN for a partially filled order - its own mapping says
        so - which is why status cannot be the gate."""
        manager, _p, _s, _m, _c = _manager()
        tracked = await _open(
            manager, _order(quantity=100, filled=55, status=OrderStatus.OPEN), _sizing()
        )
        assert tracked.position.quantity == 55

    def test_a_fill_larger_than_the_order_is_refused(self) -> None:
        with pytest.raises(ContradictoryFillError, match="cannot fill more"):
            confirm_fill(_order(quantity=10, filled=11))

    def test_a_fill_with_no_price_is_refused(self) -> None:
        """A position that cannot be priced cannot be risked, sized or marked."""
        with pytest.raises(ContradictoryFillError, match="no average price"):
            confirm_fill(_order(filled=40, price=None))

    def test_a_complete_fill_is_reported_complete(self) -> None:
        assert confirm_fill(_order(quantity=10, filled=10)).complete is True
        assert confirm_fill(_order(quantity=10, filled=4)).complete is False

    @pytest.mark.asyncio
    async def test_a_short_entry_becomes_a_short_position(self) -> None:
        manager, _p, _s, _m, _c = _manager()
        tracked = await _open(
            manager, _order(side=Side.SELL), _sizing(stop="1214.0000", target="1170.0000")
        )
        assert tracked.position.direction is Direction.SHORT

    @pytest.mark.asyncio
    async def test_the_deadline_is_asked_for_with_this_stocks_flags(self) -> None:
        """The seam that would silently give every stock the non-CAS deadline."""
        manager, _p, _s, _m, calendar = _manager()
        await manager.open_from_fill(
            _order(),
            sizing=_sizing(),
            slot_index=3,
            trade_date=TRADING_DAY,
            now=NOW,
            is_cas_stock=True,
            is_fno=False,
        )
        assert calendar.calls == [
            {
                "trade_date": TRADING_DAY,
                "is_cas_stock": True,
                "is_fno": False,
                "buffer_minutes": 5,
            }
        ]

    @pytest.mark.asyncio
    async def test_a_naive_timestamp_is_refused(self) -> None:
        manager, _p, _s, _m, _c = _manager()
        with pytest.raises(ValueError, match="naive"):
            await manager.open_from_fill(
                _order(),
                sizing=_sizing(),
                slot_index=0,
                trade_date=TRADING_DAY,
                now=dt.datetime(2026, 8, 25, 5, 0),
                is_cas_stock=False,
            )


# ---------------------------------------------------------------------------
# AC2 — nothing is protected without a verified live stop
# ---------------------------------------------------------------------------


class TestAc2ProtectionIsProved:
    @pytest.mark.asyncio
    async def test_a_failed_stop_produces_no_protected_position(self) -> None:
        manager, _p, _s, mirror, _c = _manager(
            protector=FakeProtector(raises=StopNotEstablishedError("stop refused"))
        )
        with pytest.raises(StopNotEstablishedError):
            await _open(manager, _order(), _sizing())
        tracked = manager.tracked("INFY")
        assert isinstance(tracked, ExitingPosition)
        assert not isinstance(tracked, ProtectedPosition)
        assert mirror.last("INFY").protection == "ExitingPosition"
        assert mirror.last("INFY").status is PositionStatus.CLOSING

    def test_the_protected_type_cannot_be_built_from_a_position(self) -> None:
        """Structure, not discipline: there is no constructor that takes a row.

        This is the test that would fail the moment someone 'simplifies'
        ProtectedPosition to hold a Position and a boolean.
        """
        with pytest.raises(TypeError):
            ProtectedPosition(_position())  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_the_row_is_written_before_the_stop_is_attached(self) -> None:
        """Ordering, and it is not cosmetic. Dying after the insert costs one
        position; dying after the attach with no row is an unknown position at
        the broker, which §8.3 says trips the kill switch."""
        order_seen: list[str] = []

        class OrderedStore(RecordingStore):
            async def open_position(self, position: dict[str, Any]) -> int:
                order_seen.append("insert")
                return await super().open_position(position)

        protector = FakeProtector()
        protector.order = order_seen
        manager, _p, _s, _m, _c = _manager(protector=protector, store=OrderedStore())
        await _open(manager, _order(), _sizing())
        assert order_seen == ["insert", "attach"]

    @pytest.mark.asyncio
    async def test_the_position_carries_the_id_the_database_gave_it(self) -> None:
        manager, protector, store, _m, _c = _manager()
        tracked = await _open(manager, _order(), _sizing())
        assert tracked.position.position_id == store.next_id
        assert protector.attached[0].position_id == store.next_id, (
            "the stop was attached to a position the database had not identified"
        )

    @pytest.mark.asyncio
    async def test_a_naked_position_error_propagates_untouched(self) -> None:
        """The session is already halted by the time this arrives. The manager
        must not swallow it into 'exiting' - there is no live exit."""
        manager, _p, _s, mirror, _c = _manager(
            protector=FakeProtector(raises=NakedPositionError("could not exit"))
        )
        with pytest.raises(NakedPositionError):
            await _open(manager, _order(), _sizing())
        assert manager.tracked("INFY") is None
        assert mirror.writes == []


# ---------------------------------------------------------------------------
# AC3 — an exiting position stays held until its exit FILLS
# ---------------------------------------------------------------------------


class TestAc3TheExitMustFill:
    async def _exiting(self) -> tuple[PositionManager, RecordingStore, RecordingMirror]:
        manager, _p, store, mirror, _c = _manager(
            protector=FakeProtector(raises=StopNotEstablishedError("stop refused"))
        )
        with pytest.raises(StopNotEstablishedError):
            await _open(manager, _order(), _sizing())
        return manager, store, mirror

    @pytest.mark.asyncio
    async def test_the_position_is_still_held_before_the_exit_fills(self) -> None:
        manager, store, _m = await self._exiting()
        assert isinstance(manager.tracked("INFY"), ExitingPosition)
        assert store.closes == [], "closed on the strength of an exit being placed"

    @pytest.mark.asyncio
    async def test_a_filled_exit_closes_the_position(self) -> None:
        manager, store, mirror = await self._exiting()
        result = await manager.confirm_exit(
            _order(
                quantity=100,
                filled=100,
                price="1195.0000",
                side=Side.SELL,
                intent=OrderIntent.SQUAREOFF,
            ),
            now=NOW,
        )
        assert result is None
        assert manager.tracked("INFY") is None
        assert mirror.clears == ["INFY"]
        (close,) = store.closes
        assert close["exit_price"] == Decimal("1195.0000")
        assert close["exit_reason"] == ExitReason.UNPROTECTED.value
        assert close["realized_pnl"] == Decimal("-500.0000")

    @pytest.mark.asyncio
    async def test_a_partially_filled_exit_does_not_close_the_position(self) -> None:
        """A half-exited position is still a position. Calling it closed is the
        same mistake as opening one for the quantity ordered."""
        manager, store, mirror = await self._exiting()
        result = await manager.confirm_exit(
            _order(quantity=100, filled=60, price="1195.0000", side=Side.SELL), now=NOW
        )
        assert isinstance(result, ExitingPosition)
        assert store.closes == []
        assert mirror.clears == []
        assert manager.tracked("INFY") is not None

    @pytest.mark.asyncio
    async def test_the_exit_reason_is_its_own_member(self) -> None:
        """Not STOP (no stop existed), not MANUAL (no human), not KILLSWITCH
        (the session was not halted). Each would send a reader somewhere wrong."""
        manager, store, _m = await self._exiting()
        await manager.confirm_exit(_order(side=Side.SELL, price="1195.0000"), now=NOW)
        assert store.closes[0]["exit_reason"] == "UNPROTECTED"

    @pytest.mark.asyncio
    async def test_an_exit_for_something_we_do_not_hold_is_ignored(self) -> None:
        manager, _p, store, _m, _c = _manager()
        assert await manager.confirm_exit(_order(symbol="WIPRO", side=Side.SELL), now=NOW) is None
        assert store.closes == []

    @pytest.mark.asyncio
    async def test_the_excursions_are_persisted_on_close(self) -> None:
        """The columns existed since the first migration and nothing wrote them."""
        manager, store, _m = await self._exiting()
        await manager.mark(_quote("1240.0000"), now=NOW)
        await manager.mark(_quote("1150.0000"), now=NOW)
        await manager.confirm_exit(_order(side=Side.SELL, price="1195.0000"), now=NOW)
        close = store.closes[0]
        assert close["max_favourable_excursion"] == Decimal("4000.0000")
        assert close["max_adverse_excursion"] == Decimal("-5000.0000")


# ---------------------------------------------------------------------------
# AC4 — marking to market
# ---------------------------------------------------------------------------


class _Quote:
    def __init__(self, ltp: str, *, symbol: str = "INFY", as_of: dt.datetime = NOW) -> None:
        self.symbol = symbol
        self.ltp = Decimal(ltp)
        self.as_of = as_of


def _quote(ltp: str, **kwargs: Any) -> _Quote:
    return _Quote(ltp, **kwargs)


class TestAc4Marks:
    @pytest.mark.asyncio
    async def test_an_unmarked_position_reports_none_rather_than_zero(self) -> None:
        """'We have not priced this' and 'this is flat' are different facts."""
        manager, _p, _s, _m, _c = _manager()
        tracked = await _open(manager, _order(), _sizing())
        assert tracked.marks.unrealised_pnl is None
        assert tracked.marks.max_favourable_excursion == Decimal("0")

    @pytest.mark.asyncio
    async def test_marking_updates_pnl_and_the_mirror(self) -> None:
        manager, _p, _s, mirror, _c = _manager()
        await _open(manager, _order(), _sizing())
        tracked = await manager.mark(_quote("1210.0000"), now=NOW)
        assert tracked is not None
        assert tracked.marks.unrealised_pnl == Decimal("1000.0000")
        assert mirror.last("INFY").unrealised_pnl == Decimal("1000.0000")

    @pytest.mark.asyncio
    async def test_a_stale_quote_does_not_mark(self) -> None:
        """A stale mark is worse than no mark: the daily-loss halt reads this."""
        manager, _p, _s, mirror, _c = _manager(max_quote_age_seconds=30.0)
        await _open(manager, _order(), _sizing())
        writes_before = len(mirror.writes)
        tracked = await manager.mark(
            _quote("1210.0000", as_of=NOW - dt.timedelta(seconds=90)), now=NOW
        )
        assert tracked is not None
        assert tracked.marks.unrealised_pnl is None
        assert len(mirror.writes) == writes_before

    @pytest.mark.asyncio
    async def test_marking_a_symbol_we_do_not_hold_is_a_no_op(self) -> None:
        manager, _p, _s, _m, _c = _manager()
        assert await manager.mark(_quote("1210.0000", symbol="WIPRO"), now=NOW) is None

    @pytest.mark.asyncio
    async def test_the_book_total_refuses_when_anything_is_unpriced(self) -> None:
        """Fail closed: a total that omits the position nobody could price is
        wrong in the direction of continuing to trade."""
        manager, _p, _s, _m, _c = _manager()
        await _open(manager, _order(), _sizing())
        assert manager.unrealised_pnl is None
        await manager.mark(_quote("1210.0000"), now=NOW)
        assert manager.unrealised_pnl == Decimal("1000.0000")

    def test_an_empty_book_is_flat_not_unknown(self) -> None:
        manager, _p, _s, _m, _c = _manager()
        assert manager.unrealised_pnl == Decimal("0")

    @settings(max_examples=60, deadline=None)
    @given(
        prices=st.lists(
            st.decimals(min_value=800, max_value=1600, places=2), min_size=1, max_size=25
        )
    )
    def test_mfe_and_mae_are_the_extremes_of_unrealised_pnl(self, prices: list[Decimal]) -> None:
        """The property, over sequences longer than anyone would hand-write."""
        position = _position()
        marks = Marks()
        for price in prices:
            marks = marks.marked(position, price, now=NOW)
        pnls = [position.unrealized_pnl(p) for p in prices]
        assert marks.max_favourable_excursion == max(max(pnls), Decimal("0"))
        assert marks.max_adverse_excursion == min(min(pnls), Decimal("0"))
        assert marks.unrealised_pnl == pnls[-1]

    @settings(max_examples=60, deadline=None)
    @given(
        prices=st.lists(
            st.decimals(min_value=800, max_value=1600, places=2), min_size=2, max_size=25
        )
    )
    def test_the_extremes_never_move_the_wrong_way(self, prices: list[Decimal]) -> None:
        position = _position()
        marks = Marks()
        for price in prices:
            nxt = marks.marked(position, price, now=NOW)
            assert nxt.max_favourable_excursion >= marks.max_favourable_excursion
            assert nxt.max_adverse_excursion <= marks.max_adverse_excursion
            marks = nxt


# ---------------------------------------------------------------------------
# AC5 — a restored position is never presumed protected
# ---------------------------------------------------------------------------


def _row(**over: Any) -> dict[str, Any]:
    row = {
        "position_id": 7,
        "correlation_id": CID,
        "symbol": "INFY",
        "strategy_id": "orb_long_v1",
        "slot_index": 1,
        "direction": "LONG",
        "quantity": 50,
        "entry_price": Decimal("1200.0000"),
        "stop_price": Decimal("1186.4500"),
        "target_price": None,
        "opened_at": NOW,
        "squareoff_deadline": DEADLINE,
        "status": "OPEN",
        "closed_at": None,
        "exit_price": None,
        "exit_reason": None,
        "realized_pnl": None,
        "max_favourable_excursion": Decimal("2400.0000"),
        "max_adverse_excursion": Decimal("-800.0000"),
    }
    row.update(over)
    return row


class TestAc5RestoreNeverPresumesProtection:
    @pytest.mark.asyncio
    async def test_every_restored_position_is_unverified(self) -> None:
        """``stop_price`` is NOT NULL on every row, so a row can say nothing
        about whether an ORDER is protecting anything."""
        manager, _p, _s, _m, _c = _manager(store=RecordingStore(rows=[_row(), _row(symbol="TCS")]))
        restored = await manager.restore()
        assert len(restored) == 2
        assert all(isinstance(r, UnverifiedPosition) for r in restored)
        assert not any(isinstance(r, ProtectedPosition) for r in manager.open_positions())

    @pytest.mark.asyncio
    async def test_restore_brings_back_the_excursions(self) -> None:
        """Without the repository fix these came back as zero, silently."""
        manager, _p, _s, _m, _c = _manager(store=RecordingStore(rows=[_row()]))
        (restored,) = await manager.restore()
        assert restored.marks.max_favourable_excursion == Decimal("2400.0000")
        assert restored.marks.max_adverse_excursion == Decimal("-800.0000")

    @pytest.mark.asyncio
    async def test_a_row_without_excursions_restores_to_zero_not_none(self) -> None:
        manager, _p, _s, _m, _c = _manager(
            store=RecordingStore(
                rows=[_row(max_favourable_excursion=None, max_adverse_excursion=None)]
            )
        )
        (restored,) = await manager.restore()
        assert restored.marks.max_favourable_excursion == Decimal("0")

    @pytest.mark.asyncio
    async def test_a_restored_position_can_be_marked(self) -> None:
        """The control: restoring into a state nothing can use would satisfy
        every assertion above."""
        manager, _p, _s, _m, _c = _manager(store=RecordingStore(rows=[_row()]))
        await manager.restore()
        tracked = await manager.mark(_quote("1210.0000"), now=NOW)
        assert tracked is not None
        assert tracked.marks.unrealised_pnl == Decimal("500.0000")

    @pytest.mark.asyncio
    async def test_restoring_keeps_the_database_identity(self) -> None:
        manager, _p, _s, _m, _c = _manager(store=RecordingStore(rows=[_row()]))
        (restored,) = await manager.restore()
        assert restored.position.position_id == 7


# ---------------------------------------------------------------------------
# The fill that lands through its own stop
# ---------------------------------------------------------------------------


class TestAFillThroughItsOwnStop:
    @pytest.mark.asyncio
    async def test_it_is_exited_rather_than_kept(self) -> None:
        """A long that fills BELOW its approved stop is already past it. No stop
        can be placed that would not trigger instantly."""
        manager, protector, _s, _m, _c = _manager()
        with pytest.raises(UnprotectableFillError):
            await _open(manager, _order(price="1180.0000"), _sizing(stop="1186.4500"))
        assert len(protector.exited) == 1
        target, because = protector.exited[0]
        assert target.symbol == "INFY"
        assert target.quantity == 100
        assert target.direction is Direction.LONG
        assert "stop is already breached" in because
        assert manager.open_positions() == []

    @pytest.mark.asyncio
    async def test_nothing_is_persisted_for_it(self) -> None:
        """A row claiming a stop that is not on the protective side of entry
        would be a false record. The exit order carries the correlation_id."""
        manager, _p, store, mirror, _c = _manager()
        with pytest.raises(UnprotectableFillError):
            await _open(manager, _order(price="1180.0000"), _sizing(stop="1186.4500"))
        assert store.inserts == []
        assert mirror.writes == []

    @pytest.mark.asyncio
    async def test_the_stop_is_never_attached_for_it(self) -> None:
        manager, protector, _s, _m, _c = _manager()
        with pytest.raises(UnprotectableFillError):
            await _open(manager, _order(price="1186.4500"), _sizing(stop="1186.4500"))
        assert protector.attached == [], "a stop was attached at the entry price"

    @pytest.mark.asyncio
    async def test_a_short_that_fills_above_its_stop_is_also_exited(self) -> None:
        manager, protector, _s, _m, _c = _manager()
        with pytest.raises(UnprotectableFillError):
            await _open(
                manager,
                _order(side=Side.SELL, price="1220.0000"),
                _sizing(stop="1214.0000", target="1170.0000"),
            )
        assert protector.exited[0][0].direction is Direction.SHORT

    @pytest.mark.asyncio
    async def test_a_fill_just_inside_the_stop_is_kept(self) -> None:
        """The control, and it is the one that matters: a manager that exited
        every fill would pass all four tests above."""
        manager, protector, _s, _m, _c = _manager()
        tracked = await _open(manager, _order(price="1186.4600"), _sizing(stop="1186.4500"))
        assert isinstance(tracked, ProtectedPosition)
        assert protector.exited == []


# ---------------------------------------------------------------------------
# Counters, against the real registry (QA-E15-13)
# ---------------------------------------------------------------------------


class TestTheCountersExistOnTheRealMetrics:
    def _names(self) -> set[str]:
        return {
            sample.name for metric in get_metrics().registry.collect() for sample in metric.samples
        }

    def test_every_counter_this_module_increments_is_registered(self) -> None:
        """QA-E15-13 was two counters read by ``getattr`` and registered
        nowhere - satisfied by a double and by nothing else."""
        assert {
            "positions_opened_total",
            "partial_fills_total",
            "unprotectable_fills_total",
        } <= self._names()

    @pytest.mark.asyncio
    async def test_a_partial_fill_is_counted(self) -> None:
        manager, _p, _s, _m, _c = _manager()
        await _open(manager, _order(quantity=100, filled=40), _sizing())
        assert _counter("partial_fills_total") == 1
        assert _counter("positions_opened_total") == 1

    @pytest.mark.asyncio
    async def test_an_unprotectable_fill_is_counted_separately(self) -> None:
        manager, _p, _s, _m, _c = _manager()
        with pytest.raises(UnprotectableFillError):
            await _open(manager, _order(price="1180.0000"), _sizing())
        assert _counter("unprotectable_fills_total") == 1
        assert _counter("positions_opened_total") == 0


def _counter(name: str) -> float:
    for metric in get_metrics().registry.collect():
        for sample in metric.samples:
            if sample.name == name:
                return sample.value
    raise AssertionError(f"{name} is registered nowhere")
