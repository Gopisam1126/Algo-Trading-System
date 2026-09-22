"""The book meets the real gateway, adapter, database and Redis (E15-S05).

`tests/unit/test_positions.py` proves the POLICY — open what filled, protect
before tracking, never presume protection on a restart. It proves none of it
against a real row, because its store is a dictionary that accepts whatever it
is handed.

Three seams only exist here, and each has a way of failing that no double can
show:

* **The excursions round-tripping through Postgres.** The columns existed since
  the first migration with nothing writing them, and `_position_to_dict` did
  not return them. A double that echoes back what it was given cannot tell you
  whether the column names line up.
* **The row coming back as a `Position`.** `_position_to_dict` hands out
  `direction` and `status` as strings and adds `strategy_id`, which the model
  does not have. Field-by-field agreement between a repository and a Pydantic
  model is exactly what integration is for.
* **The stop being sized from the FILL.** The unit tests assert the quantity on
  the position; only here does a real `OrderGateway` build the real stop order
  and a real adapter send it, so only here can you see the number that would
  reach a broker.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from algotrader.broker.kite.trading import KiteTradingAdapter
from algotrader.common.calendar import MarketCalendar, load_holidays_with_status
from algotrader.common.db import engine as db_engine
from algotrader.common.db.repositories import (
    InstrumentRepository,
    OrderRepository,
    PositionRepository,
)
from algotrader.common.enums import OrderIntent, OrderStatus, OrderType, Product, Side
from algotrader.common.models.trading import Order, SizingResult
from algotrader.common.redis import keys, state
from algotrader.execution.gateway import GatewayPolicy, OrderGateway
from algotrader.execution.positions import (
    PositionManager,
    PositionSnapshot,
    ProtectedPosition,
    RedisMirror,
    UnprotectableFillError,
    UnverifiedPosition,
)
from algotrader.execution.protective_stop import ProtectiveStop

pytestmark = [pytest.mark.integration]

TRADE_DATE = dt.date(2026, 8, 25)
NOW = dt.datetime(2026, 8, 25, 6, 30, tzinfo=dt.UTC)
TICKS = {"INFY": Decimal("0.05")}


@pytest.fixture
async def engine(migrated_database: str) -> AsyncIterator[object]:
    eng = db_engine.create_engine_from_url(migrated_database)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: object) -> AsyncIterator[AsyncSession]:
    factory = db_engine.create_session_factory(engine)  # type: ignore[arg-type]
    async with factory() as s:
        yield s
        await s.rollback()


@pytest.fixture
async def instruments(session: AsyncSession) -> InstrumentRepository:
    repo = InstrumentRepository(session)
    await repo.upsert(
        [
            {
                "tradingsymbol": symbol,
                "exchange": "NSE",
                "broker_token": f"pm-tok-{index}",
                "tick_size": tick,
                "lot_size": 1,
            }
            for index, (symbol, tick) in enumerate(TICKS.items())
        ]
    )
    await session.flush()
    await repo.refresh_cache()
    return repo


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[aioredis.Redis]:
    client = aioredis.from_url(redis_url, decode_responses=True)
    await client.flushall()
    yield client
    await client.aclose()


@pytest.fixture
def calendar() -> MarketCalendar:
    """The real shipped holiday list, and the real deadline maths with it."""
    path = Path(__file__).resolve().parents[2] / "config" / "nse_holidays.yaml"
    status = load_holidays_with_status(str(path))
    return MarketCalendar(status.dates, covers_years=status.covers_years)


class _RecordingKite:
    """Records what the adapter sends and lists it back as an orderbook."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.book: list[dict[str, Any]] = []

    def place_order(self, **params: Any) -> str:
        self.calls.append(params)
        self.book.append(
            {
                "order_id": f"2608250009{len(self.calls):02d}",
                "tag": params.get("tag"),
                "status": "OPEN" if params["order_type"] == "MARKET" else "TRIGGER PENDING",
                "tradingsymbol": params["tradingsymbol"],
                "transaction_type": params["transaction_type"],
                "order_type": params["order_type"],
                "product": params["product"],
                "quantity": params["quantity"],
                "filled_quantity": 0,
                "order_timestamp": "2026-08-25 12:00:00",
                "exchange": "NSE",
            }
        )
        return self.book[-1]["order_id"]

    def orders(self) -> list[dict[str, Any]]:
        return list(self.book)

    def of_type(self, order_type: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["order_type"] == order_type]


class _Halter:
    def __init__(self) -> None:
        self.armed: list[tuple] = []

    async def arm(self, reason, *, detail, armed_by, now):
        self.armed.append((reason, detail, armed_by))
        return object()


def _entry(*, quantity: int = 100, filled: int = 100, price: str = "1200.0000") -> Order:
    return Order(
        client_order_id=uuid.uuid4().hex[:32],
        broker_order_id="BROKER-ENTRY",
        correlation_id=uuid.uuid4(),
        symbol="INFY",
        side=Side.BUY,
        order_type=OrderType.MARKET,
        product=Product.MIS,
        quantity=quantity,
        status=OrderStatus.FILLED if filled == quantity else OrderStatus.OPEN,
        filled_quantity=filled,
        average_price=Decimal(price),
        intent=OrderIntent.ENTRY,
        placed_at=NOW,
        last_update_at=NOW,
    )


def _exit(correlation_id, *, price: str = "1195.0000", filled: int = 100, quantity: int = 100):
    """A genuine SQUAREOFF order. Using an ENTRY here would still 'work',
    because confirm_exit keys on symbol and fill - which is exactly why the
    test would then be asserting nothing about exits."""
    return Order(
        client_order_id=uuid.uuid4().hex[:32],
        correlation_id=correlation_id,
        symbol="INFY",
        side=Side.SELL,
        order_type=OrderType.MARKET,
        product=Product.MIS,
        quantity=quantity,
        status=OrderStatus.FILLED if filled == quantity else OrderStatus.OPEN,
        filled_quantity=filled,
        average_price=Decimal(price),
        intent=OrderIntent.SQUAREOFF,
        placed_at=NOW,
        last_update_at=NOW,
    )


def _sizing(stop: str = "1186.4500") -> SizingResult:
    return SizingResult(
        quantity=100,
        entry_price=Decimal("1200.0000"),
        stop_price=Decimal(stop),
        target_price=None,
        capital_at_risk=Decimal("1355.00"),
        binding_constraint="risk_per_trade",
    )


class _Quote:
    def __init__(self, ltp: str, *, symbol: str = "INFY") -> None:
        self.symbol = symbol
        self.ltp = Decimal(ltp)
        self.as_of = NOW


def _manager(instruments, session, redis, calendar, client) -> tuple[PositionManager, Any]:
    gateway = OrderGateway(
        KiteTradingAdapter(
            auth=None,
            client=client,
            algo_id="",
            tick_size_for=instruments.tick_size,
        ),
        policy=GatewayPolicy(algo_id=None, market_protection=Decimal("-1")),
        store=OrderRepository(session, instruments),
    )
    protector = ProtectiveStop(gateway, halter=_Halter())
    repo = PositionRepository(session, instruments)
    manager = PositionManager(
        protector=protector,
        store=repo,
        mirror=RedisMirror(redis),
        calendar=calendar,
    )
    return manager, repo


async def _open(manager, order, sizing, *, slot_index: int = 0):
    return await manager.open_from_fill(
        order,
        sizing=sizing,
        slot_index=slot_index,
        trade_date=TRADE_DATE,
        now=NOW,
        is_cas_stock=False,
    )


class TestWhatReachesTheDatabase:
    async def test_the_row_records_the_quantity_that_filled(
        self, instruments, session, redis, calendar
    ) -> None:
        """The story's first acceptance criterion, against a real row."""
        manager, _repo = _manager(instruments, session, redis, calendar, _RecordingKite())
        tracked = await _open(
            manager, _entry(quantity=100, filled=40, price="1201.3000"), _sizing()
        )
        await session.flush()

        row = (
            await session.execute(
                text("SELECT quantity, entry_price, stop_price FROM positions WHERE id = :i"),
                {"i": tracked.position.position_id},
            )
        ).one()
        assert row.quantity == 40
        assert Decimal(str(row.entry_price)) == Decimal("1201.3000")

    async def test_the_stop_that_reaches_the_broker_is_for_the_filled_quantity(
        self, instruments, session, redis, calendar
    ) -> None:
        """The number that would actually be sent. A stop for 100 against a
        40-share holding sells 60 shares that do not exist when it triggers."""
        client = _RecordingKite()
        manager, _repo = _manager(instruments, session, redis, calendar, client)
        await _open(manager, _entry(quantity=100, filled=40), _sizing())

        (stop,) = client.of_type("SL-M")
        assert stop["quantity"] == 40
        assert stop["transaction_type"] == "SELL"

    async def test_the_excursions_survive_the_database(
        self, instruments, session, redis, calendar
    ) -> None:
        """The columns have existed since the first migration with no writer.

        A double cannot show this: it echoes back whatever it was handed,
        column names and all.
        """
        manager, _repo = _manager(instruments, session, redis, calendar, _RecordingKite())
        tracked = await _open(manager, _entry(), _sizing())
        await manager.mark(_Quote("1240.0000"), now=NOW)
        await manager.mark(_Quote("1150.0000"), now=NOW)
        await manager.confirm_exit(_exit(tracked.position.correlation_id), now=NOW)
        await session.flush()

        row = (
            await session.execute(
                text(
                    "SELECT status, realized_pnl, max_favourable_excursion, "
                    "max_adverse_excursion, exit_reason FROM positions WHERE id = :i"
                ),
                {"i": tracked.position.position_id},
            )
        ).one()
        assert row.status == "CLOSED"
        assert Decimal(str(row.max_favourable_excursion)) == Decimal("4000.0000")
        assert Decimal(str(row.max_adverse_excursion)) == Decimal("-5000.0000")
        assert Decimal(str(row.realized_pnl)) == Decimal("-500.00")
        assert row.exit_reason == "UNPROTECTED"


class TestRestartAgainstRealRows:
    async def test_a_new_manager_reads_back_what_the_old_one_wrote(
        self, instruments, session, redis, calendar
    ) -> None:
        """The field-by-field seam between the repository and the model.

        ``_position_to_dict`` hands out ``direction`` and ``status`` as strings
        and adds ``strategy_id``, which ``Position`` does not have. Agreement
        there is not provable against a dictionary that was never a row.
        """
        manager, _repo = _manager(instruments, session, redis, calendar, _RecordingKite())
        tracked = await _open(manager, _entry(quantity=100, filled=75), _sizing())
        await manager.mark(_Quote("1230.0000"), now=NOW)
        await session.flush()
        assert isinstance(manager.tracked("INFY"), ProtectedPosition)

        restarted, _r = _manager(instruments, session, redis, calendar, _RecordingKite())
        restored = await restarted.restore()

        assert [type(r) for r in restored] == [UnverifiedPosition]
        assert restored[0].position.quantity == 75
        assert restored[0].position.position_id == tracked.position.position_id
        assert restored[0].position.direction is tracked.position.direction

    async def test_restored_positions_carry_their_excursions(
        self, instruments, session, redis, calendar
    ) -> None:
        manager, _repo = _manager(instruments, session, redis, calendar, _RecordingKite())
        tracked = await _open(manager, _entry(), _sizing())
        await session.flush()
        await session.execute(
            text(
                "UPDATE positions SET max_favourable_excursion = 3300.0000, "
                "max_adverse_excursion = -1200.0000 WHERE id = :i"
            ),
            {"i": tracked.position.position_id},
        )

        restarted, _r = _manager(instruments, session, redis, calendar, _RecordingKite())
        (restored,) = await restarted.restore()
        assert restored.marks.max_favourable_excursion == Decimal("3300.0000")
        assert restored.marks.max_adverse_excursion == Decimal("-1200.0000")
        assert restored.position.symbol == "INFY"
        assert restored.position.position_id == tracked.position.position_id

    async def test_a_restored_position_can_be_marked(
        self, instruments, session, redis, calendar
    ) -> None:
        """The control: restoring into an unusable state would satisfy the rest."""
        manager, _repo = _manager(instruments, session, redis, calendar, _RecordingKite())
        await _open(manager, _entry(quantity=100, filled=50), _sizing())
        await session.flush()

        restarted, _r = _manager(instruments, session, redis, calendar, _RecordingKite())
        await restarted.restore()
        marked = await restarted.mark(_Quote("1210.0000"), now=NOW)
        assert marked is not None
        assert marked.marks.unrealised_pnl == Decimal("500.0000")


class TestTheMirrorInRealRedis:
    async def test_the_snapshot_round_trips_with_exact_decimals(
        self, instruments, session, redis, calendar
    ) -> None:
        """Money is never a float. JSON via ``model_dump_json`` is why the key
        is a string and not the HASH the spec still describes."""
        manager, _repo = _manager(instruments, session, redis, calendar, _RecordingKite())
        await _open(manager, _entry(quantity=100, filled=40, price="1201.3000"), _sizing())
        await manager.mark(_Quote("1210.5500"), now=NOW)

        back = await state.get_state(redis, keys.position_state("INFY"), PositionSnapshot)
        assert back is not None
        assert back.entry_price == Decimal("1201.3000")
        assert back.quantity == 40
        assert back.unrealised_pnl == Decimal("370.0000")
        assert back.protection == "ProtectedPosition"

    async def test_closing_removes_the_mirror(self, instruments, session, redis, calendar) -> None:
        """A live position must never vanish from the fast path while held, and
        a closed one must never linger there."""
        manager, _repo = _manager(instruments, session, redis, calendar, _RecordingKite())
        await _open(manager, _entry(), _sizing())
        assert await redis.get(keys.position_state("INFY")) is not None

        tracked = manager.tracked("INFY")
        assert tracked is not None
        await manager.confirm_exit(_exit(tracked.position.correlation_id), now=NOW)
        assert await redis.get(keys.position_state("INFY")) is None


class TestTheFillThatLandsThroughItsOwnStop:
    async def test_a_real_market_exit_reaches_the_broker(
        self, instruments, session, redis, calendar
    ) -> None:
        """The case that arrives as a Pydantic ValidationError while holding
        live stock. It must leave through the gateway, not up the stack."""
        client = _RecordingKite()
        manager, _repo = _manager(instruments, session, redis, calendar, client)
        with pytest.raises(UnprotectableFillError):
            await _open(manager, _entry(price="1180.0000"), _sizing(stop="1186.4500"))

        (exit_order,) = client.of_type("MARKET")
        assert exit_order["transaction_type"] == "SELL"
        assert exit_order["quantity"] == 100
        assert client.of_type("SL-M") == [], "a stop was placed through its own trigger"

    async def test_no_position_row_is_written_for_it(
        self, instruments, session, redis, calendar
    ) -> None:
        manager, _repo = _manager(instruments, session, redis, calendar, _RecordingKite())
        with pytest.raises(UnprotectableFillError):
            await _open(manager, _entry(price="1180.0000"), _sizing(stop="1186.4500"))
        await session.flush()

        count = (await session.execute(text("SELECT count(*) FROM positions"))).scalar_one()
        assert count == 0
        assert await redis.get(keys.position_state("INFY")) is None

    async def test_the_exit_is_recorded_in_the_orders_table(
        self, instruments, session, redis, calendar
    ) -> None:
        """Nothing is written to ``positions``, so the order row is the only
        audit trail this holding leaves. It must carry the correlation_id."""
        manager, _repo = _manager(instruments, session, redis, calendar, _RecordingKite())
        entry = _entry(price="1180.0000")
        with pytest.raises(UnprotectableFillError):
            await _open(manager, entry, _sizing(stop="1186.4500"))
        await session.flush()

        row = (
            await session.execute(text("SELECT intent, quantity, correlation_id FROM orders"))
        ).one()
        assert row.intent == "SQUAREOFF"
        assert row.quantity == 100
        assert row.correlation_id == entry.correlation_id


class TestTheHealthyPath:
    async def test_a_clean_fill_is_protected_recorded_and_mirrored(
        self, instruments, session, redis, calendar
    ) -> None:
        """The control for the whole file. Every test above asserts a refusal or
        a failure; without this one a manager that never opened anything would
        pass them all."""
        client = _RecordingKite()
        manager, _repo = _manager(instruments, session, redis, calendar, client)
        tracked = await _open(manager, _entry(), _sizing())
        await session.flush()

        assert isinstance(tracked, ProtectedPosition)
        assert tracked.established.stop_broker_order_id
        assert len(client.of_type("SL-M")) == 1
        assert client.of_type("MARKET") == []
        count = (await session.execute(text("SELECT count(*) FROM positions"))).scalar_one()
        assert count == 1
        assert await redis.get(keys.position_state("INFY")) is not None

    async def test_the_deadline_stored_is_the_calendars_answer(
        self, instruments, session, redis, calendar
    ) -> None:
        """The real calendar, not a stub: a per-stock deadline computed wrong is
        a position the broker force-closes at an arbitrary price."""
        manager, _repo = _manager(instruments, session, redis, calendar, _RecordingKite())
        tracked = await _open(manager, _entry(), _sizing())
        assert tracked.position.squareoff_deadline == calendar.squareoff_deadline(
            TRADE_DATE, is_cas_stock=False, is_fno=False, buffer_minutes=5
        )
