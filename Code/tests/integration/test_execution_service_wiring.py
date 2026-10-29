"""E15-S13 end to end: a signal on the stream becomes a protected position.

Everything real except the broker's HTTP: the Kite adapter maps Kite's own
payload shapes, the orders and positions tables are Postgres, the stream, the
slot locks and the halt latches are Redis, and every decision lands in the
audit chain. The one double besides the account is ``Facts`` - the three live
inputs nothing produces yet (health, ATR, per-share margin) - because with
``NoProducerYet`` the engine rightly refuses everything, and AC1 needs an
approval.

`tests/unit/test_execution_service.py` proves the decisions. This proves the
seams: the stream's wire format, the CAS flag read from the real table, the
real reconciler booking the fill the service registered a plan for, and a
restart that restores the real table before the real cycle.
"""

from __future__ import annotations

import datetime as dt
import math
import random
import uuid
from collections.abc import AsyncIterator
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

import pytest
import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from algotrader.broker.kite.trading import KiteTradingAdapter
from algotrader.common.audit import AuditWriter
from algotrader.common.calendar import MarketCalendar, load_holidays_with_status
from algotrader.common.config import AppConfig
from algotrader.common.db import engine as db_engine
from algotrader.common.db.repositories import (
    BarRepository,
    DailyStatusRepository,
    InstrumentRepository,
    OrderRepository,
    PositionRepository,
)
from algotrader.common.enums import (
    AIVerdict,
    Direction,
    OrderIntent,
    OrderType,
    Product,
    Side,
    Timeframe,
)
from algotrader.common.events import stream as streams
from algotrader.common.events.envelope import Envelope
from algotrader.common.models.market import Bar
from algotrader.common.models.trading import OrderRequest, Position, Recommendation
from algotrader.common.redis import keys
from algotrader.execution.gateway import GatewayPolicy, OrderGateway, client_order_id
from algotrader.execution.halt import HaltController, HaltReason
from algotrader.execution.live_inputs import (
    BrokerMarginPerShare,
    DailyCloseCorrelations,
    LiveSessionFacts,
    PublishedATR,
    strategy_timeframes,
)
from algotrader.execution.positions import (
    PositionManager,
    ProtectedPosition,
    RedisMirror,
)
from algotrader.execution.protective_stop import ProtectiveStop
from algotrader.execution.reconciliation import Drift, Reconciler
from algotrader.execution.service import GROUP, ExecutionService, Outcome, RedisSignalSource
from algotrader.execution.session_context import RepositoryContextStore
from algotrader.indicators.engine import IndicatorSet
from algotrader.indicators.publish import publish_indicator_state
from algotrader.strategy.dsl import load_strategy_yaml

pytestmark = [pytest.mark.integration]

TRADE_DATE = dt.date(2026, 8, 25)
NOW = dt.datetime(2026, 8, 25, 5, 0, tzinfo=dt.UTC)  # 10:30 IST
INSTRUMENTS = {"INFY": "IT", "TCS": "IT", "SBIN": "BANKS"}


@pytest.fixture
async def engine(migrated_database: str) -> AsyncIterator[object]:
    eng = db_engine.create_engine_from_url(migrated_database)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: object) -> AsyncIterator[AsyncSession]:
    factory = db_engine.create_session_factory(engine)  # type: ignore[arg-type]
    async with factory() as s:
        await s.execute(text("DELETE FROM decision_log"))
        await s.commit()
        yield s
        await s.rollback()
        # The audit writer commits in its own session; see the reconciliation
        # wiring test for the leak cleaning only at the start once caused.
        await s.execute(text("DELETE FROM decision_log"))
        await s.commit()


@pytest.fixture
def audit(engine: object, tmp_path: Path) -> AuditWriter:
    return AuditWriter(
        db_engine.create_session_factory(engine),  # type: ignore[arg-type]
        buffer_dir=tmp_path / "audit_buffer",
    )


@pytest.fixture
async def instruments(session: AsyncSession) -> InstrumentRepository:
    repo = InstrumentRepository(session)
    await repo.upsert(
        [
            {
                "tradingsymbol": symbol,
                "exchange": "NSE",
                "broker_token": f"svc-tok-{i}",
                "tick_size": Decimal("0.05"),
                "lot_size": 1,
                "sector": sector,
            }
            for i, (symbol, sector) in enumerate(INSTRUMENTS.items())
        ]
    )
    await session.flush()
    await repo.refresh_cache()
    daily = DailyStatusRepository(session, repo)
    await daily.upsert(
        [
            {
                "symbol_id": await repo.symbol_id(symbol),
                "trade_date": TRADE_DATE,
                "is_t2t": False,
                "is_asm": False,
                "is_gsm": False,
                "is_fno_ban": False,
                "is_cas_stock": symbol == "TCS",
            }
            for symbol in ("INFY", "TCS")  # SBIN deliberately has no row today
        ]
    )
    await session.flush()
    return repo


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[aioredis.Redis]:
    client = aioredis.from_url(redis_url, decode_responses=True)
    await client.flushall()
    yield client
    await client.aclose()


@pytest.fixture
def calendar() -> MarketCalendar:
    path = Path(__file__).resolve().parents[2] / "config" / "nse_holidays.yaml"
    status = load_holidays_with_status(str(path))
    return MarketCalendar(status.dates, covers_years=status.covers_years)


class KiteAccount:
    """Kite's payloads for orders, positions and margins (the fields we read)."""

    def __init__(self) -> None:
        self.book: list[dict[str, Any]] = []
        self.net: list[dict[str, Any]] = []
        self.placed: list[dict[str, Any]] = []
        #: E15-S14: what one share of each symbol costs, and a way to fail.
        self.margin_per_share: dict[str, float] = {}
        self.margin_error: Exception | None = None
        self.margin_calls: list[list[dict[str, Any]]] = []

    def place_order(self, **params: Any) -> str:
        self.placed.append(params)
        order_id = f"26082500{len(self.placed):04d}"
        self.book.append(
            {
                "order_id": order_id,
                "tag": params.get("tag"),
                "status": "OPEN" if params["order_type"] == "MARKET" else "TRIGGER PENDING",
                "tradingsymbol": params["tradingsymbol"],
                "exchange": params.get("exchange", "NSE"),
                "transaction_type": params["transaction_type"],
                "order_type": params["order_type"],
                "product": params["product"],
                "quantity": params["quantity"],
                "filled_quantity": 0,
                "order_timestamp": "2026-08-25 10:00:00",
            }
        )
        return order_id

    def orders(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.book]

    def cancel_order(self, variety: str, order_id: str, **_kwargs: Any) -> str:
        for row in self.book:
            if row["order_id"] == order_id:
                row["status"] = "CANCELLED"
        return order_id

    def positions(self) -> dict[str, list[dict[str, Any]]]:
        return {"net": [dict(r) for r in self.net], "day": []}

    def margins(self, segment: str) -> dict[str, Any]:
        return {
            "available": {"cash": 400000.0, "live_balance": 400000.0},
            "utilised": {"debits": 0.0},
        }

    def order_margins(self, params: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """E15-S14: ``/margins/orders``, as Kite answers it."""
        self.margin_calls.append(params)
        if self.margin_error is not None:
            raise self.margin_error
        symbol = params[0]["tradingsymbol"]
        total = self.margin_per_share.get(symbol, 240.0) * params[0]["quantity"]
        return [{"type": "equity", "tradingsymbol": symbol, "total": total}]

    def fill(self, tag: str, quantity: int, price: str) -> None:
        for row in self.book:
            if row["tag"] == tag:
                row.update(status="COMPLETE", filled_quantity=quantity, average_price=price)
                return
        raise AssertionError(f"no order tagged {tag}")

    def hold(self, symbol: str, *, quantity: int) -> None:
        self.net.append(
            {
                "tradingsymbol": symbol,
                "exchange": "NSE",
                "product": "MIS",
                "quantity": quantity,
                "day_buy_quantity": quantity,
                "day_sell_quantity": 0,
                "average_price": 1200.0,
                "pnl": 0,
            }
        )


class Facts:
    """The three inputs with no producer yet, answered - so AC1 can approve."""

    async def unhealthy_services(self, *, now: dt.datetime) -> tuple[str, ...] | None:
        return ()

    async def atr(self, symbol: str, *, strategy_id: str, now: dt.datetime) -> Decimal | None:
        return Decimal("12.5")

    async def margin_per_share(self, symbol, price, *, direction, now):
        return Decimal("240")

    async def correlations(self, symbol, against, *, now):
        return {s: Decimal("0.1") for s in against}


class Stack:
    """The real execution stack over one Kite account."""

    def __init__(
        self, session, instruments, redis, audit, calendar, account=None, clock=None, facts=None
    ) -> None:
        self.clock = clock or (lambda: NOW)
        self.redis = redis
        self.account = account or KiteAccount()
        self.adapter = KiteTradingAdapter(
            auth=None, client=self.account, algo_id="", tick_size_for=instruments.tick_size
        )
        self.orders = OrderRepository(session, instruments)
        self.gateway = OrderGateway(
            self.adapter,
            policy=GatewayPolicy(algo_id=None, market_protection=Decimal("-1")),
            store=self.orders,
        )
        self.halts = HaltController(redis)
        self.protector = ProtectiveStop(self.gateway, halter=self.halts)
        self.positions = PositionRepository(session, instruments)
        self.manager = PositionManager(
            protector=self.protector,
            store=self.positions,
            mirror=RedisMirror(redis),
            calendar=calendar,
        )
        self.reconciler = Reconciler(
            broker=self.adapter,
            ledger=self.orders,
            manager=self.manager,
            protector=self.protector,
            halter=self.halts,
            canceller=self.gateway,
            audit=audit.write,
        )
        self.service = ExecutionService(
            config=AppConfig(),
            calendar=calendar,
            gateway=self.gateway,
            manager=self.manager,
            reconciler=self.reconciler,
            protector=self.protector,
            broker=self.adapter,
            halts=self.halts,
            margins=self.adapter,
            store=RepositoryContextStore(
                positions=self.positions,
                instruments=instruments,
                daily_status=DailyStatusRepository(session, instruments),
            ),
            orders=self.orders,
            redis=redis,
            # E15-S14: a callable, because the live producers read this stack's
            # own adapter and tables.
            facts=facts(self) if facts is not None else Facts(),
            clock=self.clock,
            audit=audit.write,
        )
        self.source = RedisSignalSource(redis, block_ms=50)

    async def publish(self, rec: Recommendation) -> None:
        await streams.publish(
            self.redis,
            keys.stream_signals(),
            Envelope(
                correlation_id=rec.correlation_id,
                emitted_at=NOW,
                emitted_by="signal-svc",
                payload=rec.model_dump(mode="json"),
            ),
            maxlen=streams.default_maxlen(keys.stream_signals()),
        )

    async def consume_one(self):
        await self.source.start()
        (delivery,) = await self.source.next_batch()
        return await self.service.deliver(self.source, delivery)


@pytest.fixture
def stack(session, instruments, redis, audit, calendar) -> Stack:
    return Stack(session, instruments, redis, audit, calendar)


def _rec(symbol: str = "INFY") -> Recommendation:
    return Recommendation(
        correlation_id=uuid.uuid4(),
        symbol=symbol,
        strategy_id="orb_long_v1",
        direction=Direction.LONG,
        trigger_price=Decimal("1200.00"),
        suggested_stop=Decimal("1186.45"),
        timeframe_agreement=3,
        ai_confidence=Decimal("0.82"),
        ai_verdict=AIVerdict.CONFIRM,
        ai_rationale="integration",
        emitted_at=NOW,
    )


async def _audit_rows(session: AsyncSession) -> list[tuple[str, str]]:
    rows = (
        await session.execute(text("SELECT stage, outcome FROM decision_log ORDER BY seq"))
    ).all()
    return [(r.stage, r.outcome) for r in rows]


# ---------------------------------------------------------------------------
# AC1 - an approved signal becomes a ProtectedPosition, end to end
# ---------------------------------------------------------------------------


class TestAc1SignalToProtectedPosition:
    async def test_a_signal_on_the_stream_ends_as_a_protected_position(
        self, stack: Stack, session: AsyncSession
    ) -> None:
        await stack.service.start()
        rec = _rec("INFY")
        await stack.publish(rec)

        result = await stack.consume_one()
        assert result is not None and result.outcome is Outcome.SUBMITTED, result
        (entry,) = stack.account.placed
        assert (entry["tradingsymbol"], entry["transaction_type"]) == ("INFY", "BUY")

        stack.account.fill(entry["tag"], entry["quantity"], "1200.50")
        stack.account.hold("INFY", quantity=entry["quantity"])
        report = await stack.service.tick()

        assert report is not None and report.booked == ["INFY"], report
        assert report.exited == [], "the fill was exited instead of protected"
        tracked = stack.manager.tracked("INFY")
        assert isinstance(tracked, ProtectedPosition)
        assert tracked.position.correlation_id == rec.correlation_id
        stops = [p for p in stack.account.placed if p["order_type"] == "SL-M"]
        assert [(s["transaction_type"], s["quantity"]) for s in stops] == [
            ("SELL", entry["quantity"])
        ]
        assert len(stack.service.pending) == 0, "booked, so no longer pending"

        rows = await _audit_rows(session)
        assert ("risk_approved", "approved") in rows
        assert ("EXECUTION", "SUBMITTED") in rows
        assert ("RECONCILIATION_DRIFT", "BOOKED") in rows

    async def test_the_stream_entry_is_acknowledged(self, stack: Stack) -> None:
        await stack.service.start()
        await stack.publish(_rec())
        await stack.consume_one()
        assert await streams.pending_count(stack.redis, keys.stream_signals(), GROUP) == 0

    async def test_the_booked_slot_and_cas_deadline_come_from_the_real_tables(
        self, stack: Stack, session: AsyncSession
    ) -> None:
        """TCS is a CAS stock in instrument_daily_status: 15:10 minus 5 minutes."""
        await stack.service.start()
        await stack.publish(_rec("TCS"))
        result = await stack.consume_one()
        (entry,) = stack.account.placed
        stack.account.fill(entry["tag"], entry["quantity"], "1200.50")
        stack.account.hold("TCS", quantity=entry["quantity"])
        await stack.service.tick()

        row = (
            await session.execute(
                text("SELECT slot_index, squareoff_deadline FROM positions WHERE status = 'OPEN'")
            )
        ).one()
        assert row.slot_index == result.slot_index
        assert row.squareoff_deadline == dt.datetime(2026, 8, 25, 9, 35, tzinfo=dt.UTC)

    async def test_a_second_signal_while_the_first_is_in_flight_is_refused(
        self, stack: Stack
    ) -> None:
        await stack.service.start()
        await stack.publish(_rec("INFY"))
        await stack.consume_one()
        await stack.publish(_rec("INFY"))
        second = await stack.consume_one()
        assert second is not None and second.outcome is Outcome.REJECTED
        assert len(stack.account.placed) == 1


# ---------------------------------------------------------------------------
# AC2 - a rejected signal places nothing
# ---------------------------------------------------------------------------


class TestAc2ARejectedSignalPlacesNothing:
    async def test_the_kill_switch_in_redis_stops_it_and_the_table_says_why(
        self, stack: Stack, session: AsyncSession
    ) -> None:
        await stack.service.start()
        await stack.halts.arm(HaltReason.MANUAL, detail="drill", armed_by="test", now=NOW)
        await stack.publish(_rec())
        result = await stack.consume_one()

        assert result is not None and result.outcome is Outcome.REJECTED
        assert stack.account.placed == []
        assert (await session.execute(text("SELECT count(*) FROM orders"))).scalar_one() == 0
        assert ("kill_switch", "rejected") in await _audit_rows(session)

    async def test_a_symbol_with_no_row_today_places_nothing(
        self, stack: Stack, session: AsyncSession
    ) -> None:
        """SBIN has no instrument_daily_status row: eligibility unknown."""
        await stack.service.start()
        await stack.publish(_rec("SBIN"))
        result = await stack.consume_one()
        assert result is not None and result.outcome is Outcome.REJECTED
        assert result.reason is not None and result.reason.value == "RISK_ENGINE_FAULT"
        assert stack.account.placed == []

    async def test_a_malformed_entry_is_dead_lettered_and_acknowledged(self, stack: Stack) -> None:
        await stack.service.start()
        payload = _rec().model_dump(mode="json") | {"quantity": 500}
        await streams.publish(
            stack.redis,
            keys.stream_signals(),
            Envelope(
                correlation_id=uuid.uuid4(),
                emitted_at=NOW,
                emitted_by="signal-svc",
                payload=payload,
            ),
            maxlen=100,
        )
        assert await stack.consume_one() is None
        assert stack.account.placed == []
        dlq = keys.stream_dlq(keys.stream_signals())
        assert await streams.stream_length(stack.redis, dlq) == 1
        assert await streams.pending_count(stack.redis, keys.stream_signals(), GROUP) == 0


# ---------------------------------------------------------------------------
# AC3 - a restart restores the book before the first cycle
# ---------------------------------------------------------------------------


async def _held_and_protected(stack: Stack, *, stop: bool = True) -> uuid.UUID:
    """A position this system opened before a restart: the entry it sent and
    that filled, the positions row, the holding, and (unless not) a live stop."""
    corr = uuid.uuid4()
    entry_key = client_order_id(
        correlation_id=corr,
        symbol="INFY",
        side=Side.BUY,
        intent=OrderIntent.ENTRY,
        trade_date=TRADE_DATE,
    )
    await stack.gateway.submit(
        OrderRequest(
            client_order_id=entry_key,
            correlation_id=corr,
            symbol="INFY",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            product=Product.MIS,
            quantity=40,
            intent=OrderIntent.ENTRY,
            algo_id=None,
            market_protection=Decimal("-1"),
        )
    )
    stack.account.fill(stack.adapter.order_key(entry_key), 40, "1200.00")
    position = Position(
        correlation_id=corr,
        symbol="INFY",
        slot_index=0,
        direction=Direction.LONG,
        quantity=40,
        entry_price=Decimal("1200.0000"),
        stop_price=Decimal("1186.4500"),
        opened_at=NOW,
        squareoff_deadline=dt.datetime(2026, 8, 25, 9, 45, tzinfo=dt.UTC),
    )
    await stack.positions.open_position(
        {
            "correlation_id": corr,
            "symbol": "INFY",
            "slot_index": 0,
            "direction": "LONG",
            "quantity": 40,
            "entry_price": position.entry_price,
            "stop_price": position.stop_price,
            "opened_at": position.opened_at,
            "squareoff_deadline": position.squareoff_deadline,
        }
    )
    stack.account.hold("INFY", quantity=40)
    if stop:
        await stack.gateway.submit_stop(position, trade_date=TRADE_DATE)
    return corr


class TestAc3RestartRestoresBeforeTheFirstCycle:
    async def test_a_restarted_service_keeps_what_it_held(
        self, session, instruments, redis, audit, calendar
    ) -> None:
        before = Stack(session, instruments, redis, audit, calendar)
        await _held_and_protected(before)
        # The process restarts: a new stack over the same account and tables.
        after = Stack(session, instruments, redis, audit, calendar, account=before.account)
        report = await after.service.start()

        assert report is not None and not report.halted
        assert report.exited == [], "a held position was exited on restart"
        assert isinstance(after.manager.tracked("INFY"), ProtectedPosition)
        exits = [p for p in after.account.placed if p["transaction_type"] == "SELL"]
        assert [p["order_type"] for p in exits] == ["SL-M"], "only the original stop"
        assert after.service.ready

    async def test_the_control_without_a_restore_it_never_enters_the_book(
        self, session, instruments, redis, audit, calendar
    ) -> None:
        """Reconcile WITHOUT restoring: the stop keeps the holding from being
        exited, but nothing puts it in the book - so no time exit walks it and
        no risk context counts it as held. start() is what makes the difference."""
        before = Stack(session, instruments, redis, audit, calendar)
        await _held_and_protected(before)
        after = Stack(session, instruments, redis, audit, calendar, account=before.account)
        report = await after.service.on_reconnect()  # skips start(): no restore

        assert report is not None and report.promoted == []
        assert after.manager.tracked("INFY") is None
        assert not after.service.ready, "reconciled, but never restored"

    async def test_the_control_an_unprotected_holding_on_an_empty_book_is_exited(
        self, session, instruments, redis, audit, calendar
    ) -> None:
        """S12 constraint 2 shown happening: no restore, no live stop, and the
        position this system opened is read as an unbooked fill and exited."""
        before = Stack(session, instruments, redis, audit, calendar)
        await _held_and_protected(before, stop=False)
        after = Stack(session, instruments, redis, audit, calendar, account=before.account)
        report = await after.service.on_reconnect()

        assert report is not None and report.exited == ["INFY"]
        assert Drift.UNBOOKED in [d.kind for d in report.drifts]

    async def test_restored_first_the_same_holding_is_not_treated_as_unbooked(
        self, session, instruments, redis, audit, calendar
    ) -> None:
        before = Stack(session, instruments, redis, audit, calendar)
        await _held_and_protected(before, stop=False)
        after = Stack(session, instruments, redis, audit, calendar, account=before.account)
        report = await after.service.start()

        assert report is not None
        assert Drift.UNBOOKED not in [d.kind for d in report.drifts]
        assert after.manager.tracked("INFY") is not None

    async def test_a_restored_position_holds_its_slot_for_new_signals(
        self, session, instruments, redis, audit, calendar
    ) -> None:
        before = Stack(session, instruments, redis, audit, calendar)
        await _held_and_protected(before)
        after = Stack(session, instruments, redis, audit, calendar, account=before.account)
        await after.service.start()
        await after.publish(_rec("TCS"))
        result = await after.consume_one()
        assert result is not None and result.outcome is Outcome.SUBMITTED, result
        assert result.slot_index == 1, "slot 0 is held by the restored position"


# ---------------------------------------------------------------------------
# The two repository reads this story added
# ---------------------------------------------------------------------------


class TestTheNewRepositoryReads:
    async def test_profile_reads_sector_and_lot_size(self, instruments) -> None:
        assert await instruments.profile("SBIN") == {"sector": "BANKS", "lot_size": 1}
        assert await instruments.profile("NOSUCH") is None

    async def test_closed_since_returns_todays_closes_oldest_first(
        self, stack: Stack, session: AsyncSession
    ) -> None:
        ids = []
        for symbol, slot in (("INFY", 0), ("TCS", 1)):
            ids.append(
                await stack.positions.open_position(
                    {
                        "correlation_id": uuid.uuid4(),
                        "symbol": symbol,
                        "slot_index": slot,
                        "direction": "LONG",
                        "quantity": 10,
                        "entry_price": Decimal("1200.0000"),
                        "stop_price": Decimal("1186.4500"),
                        "opened_at": NOW,
                        "squareoff_deadline": dt.datetime(2026, 8, 25, 9, 45, tzinfo=dt.UTC),
                    }
                )
            )
        for position_id, minutes, pnl in ((ids[1], 10, "-50"), (ids[0], 20, "80")):
            await stack.positions.close_position(
                position_id,
                exit_price=Decimal("1195.0000"),
                exit_reason="STOP_HIT",
                realized_pnl=Decimal(pnl),
                closed_at=NOW + dt.timedelta(minutes=minutes),
            )
        closed = await stack.positions.closed_since(NOW)
        assert [r["symbol"] for r in closed] == ["TCS", "INFY"]
        assert [r["realized_pnl"] for r in closed] == [Decimal("-50"), Decimal("80")]
        assert await stack.positions.closed_since(NOW + dt.timedelta(minutes=15)) == closed[1:]

    async def test_closed_since_refuses_a_naive_instant(self, stack: Stack) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            await stack.positions.closed_since(dt.datetime(2026, 8, 25, 5, 0))


# ===========================================================================
# E15-S06 - the square-off timer against the real adapter, tables and halt
# ===========================================================================


def _at_ist(day: dt.date, hour: int, minute: int, second: int = 0) -> dt.datetime:
    from algotrader.common.calendar import IST

    return dt.datetime.combine(day, dt.time(hour, minute, second), tzinfo=IST).astimezone(dt.UTC)


class TestS06TheTimeExitForReal:
    async def test_at_its_deadline_a_position_is_squared_off_and_closed_as_time(
        self, session, instruments, redis, audit, calendar
    ) -> None:
        clock = {"now": NOW}
        stack = Stack(session, instruments, redis, audit, calendar, clock=lambda: clock["now"])
        await stack.service.start()
        await stack.publish(_rec("INFY"))
        await stack.consume_one()
        (entry,) = stack.account.placed
        stack.account.fill(entry["tag"], entry["quantity"], "1200.50")
        stack.account.hold("INFY", quantity=entry["quantity"])
        await stack.service.tick()
        assert isinstance(stack.manager.tracked("INFY"), ProtectedPosition)

        clock["now"] = _at_ist(TRADE_DATE, 15, 15)  # 15:20 minus the buffer
        await stack.service.sweep()

        stop = next(p for p in stack.account.placed if p["order_type"] == "SL-M")
        stop_row = next(r for r in stack.account.book if r["tag"] == stop["tag"])
        assert stop_row["status"] == "CANCELLED", "the stop went first"
        exit_ = stack.account.placed[-1]
        assert (exit_["transaction_type"], exit_["order_type"], exit_["quantity"]) == (
            "SELL",
            "MARKET",
            entry["quantity"],
        )
        assert exit_["market_protection"] == -1.0, "Zerodha rejects an unprotected MARKET"

        stack.account.fill(exit_["tag"], exit_["quantity"], "1210.00")
        stack.account.net[0]["quantity"] = 0
        stack.account.net[0]["day_sell_quantity"] = entry["quantity"]
        clock["now"] = _at_ist(TRADE_DATE, 15, 15, 30)
        await stack.service.on_reconnect()

        assert stack.manager.open_positions() == []
        row = (await session.execute(text("SELECT status, exit_reason FROM positions"))).one()
        assert (row.status, row.exit_reason) == ("CLOSED", "TIME")
        rows = await _audit_rows(session)
        assert ("SQUAREOFF", "TIME_EXIT") in rows
        assert ("RECONCILIATION_DRIFT", "CLOSED") in rows
        assert not (await stack.halts.state()).any_halted

    async def test_yesterdays_open_row_is_not_sold_this_morning(
        self, session, instruments, redis, audit, calendar
    ) -> None:
        """The defect found designing this story: a row left OPEN after the
        broker's own square-off. Restored the next morning with no live stop,
        both reconciliation and the timer would have SOLD 40 shares the account
        does not hold. Now nothing is sent and the session halts."""
        yesterday = dt.date(2026, 8, 24)
        morning = _at_ist(TRADE_DATE, 9, 30)
        stack = Stack(session, instruments, redis, audit, calendar, clock=lambda: morning)
        await stack.positions.open_position(
            {
                "correlation_id": uuid.uuid4(),
                "symbol": "INFY",
                "slot_index": 0,
                "direction": "LONG",
                "quantity": 40,
                "entry_price": Decimal("1200.0000"),
                "stop_price": Decimal("1186.4500"),
                "opened_at": _at_ist(yesterday, 10, 0),
                "squareoff_deadline": _at_ist(yesterday, 15, 15),
            }
        )
        await stack.service.start()

        assert stack.account.placed == [], "an order here sells shares we do not own"
        state = await stack.halts.state()
        assert state.kill_switch
        assert state.records["kill_switch"].reason is HaltReason.POSITION_NOT_HELD
        assert ("RECONCILIATION_DRIFT", "NOT_HELD") in await _audit_rows(session)


# ===========================================================================
# E15-S14 - the live risk inputs, produced for real
# ===========================================================================

_CONFIG = Path(__file__).resolve().parents[2] / "config"
#: The shipped strategy, so the ATR's timeframe comes from a real document.
_ORB = load_strategy_yaml((_CONFIG / "strategies" / "orb_classic.yaml").read_text())


class _Healthy:
    """E20-S05's roll-up does not exist; these tests answer it so the three
    inputs this story produces are the only thing under test."""

    async def unhealthy_services(self, *, now: dt.datetime) -> tuple[str, ...] | None:
        return ()


def _live(session, instruments, calendar):
    def facts(stack: Stack) -> LiveSessionFacts:
        return LiveSessionFacts(
            health=_Healthy(),
            atr=PublishedATR(stack.redis, timeframes=strategy_timeframes([_ORB])),
            margin=BrokerMarginPerShare(stack.adapter),
            correlations=DailyCloseCorrelations(
                BarRepository(session, instruments), calendar=calendar
            ),
        )

    return facts


async def _publish_atr(
    redis: aioredis.Redis,
    symbol: str,
    *,
    true_range: str = "60",
    last_open: dt.datetime = NOW - dt.timedelta(minutes=5),
) -> Decimal:
    """Warm a real IndicatorSet on 5m bars and publish it the real way."""
    width = Decimal(true_range) / 2
    indicator_set = IndicatorSet(symbol=symbol, timeframe=Timeframe.M5)
    indicator_set.warm_up(
        [
            Bar(
                symbol=symbol,
                timeframe=Timeframe.M5,
                open_ts=last_open - dt.timedelta(minutes=5 * (259 - i)),
                open=Decimal(1200 + i % 7),
                high=Decimal(1200 + i % 7) + width,
                low=Decimal(1200 + i % 7) - width,
                close=Decimal(1200 + i % 7),
                volume=10_000,
            )
            for i in range(260)
        ]
    )
    snapshot = await publish_indicator_state(redis, indicator_set)
    assert snapshot is not None and snapshot.ready and snapshot.atr_14 is not None
    return snapshot.atr_14


async def _daily_history(session, instruments, calendar, *symbols: str, sessions: int = 75):
    """Independent random walks of daily closes, ending yesterday."""
    days = [calendar.previous_trading_day(TRADE_DATE)]
    while len(days) < sessions:
        days.append(calendar.previous_trading_day(days[-1]))
    rows = []
    for seed, symbol in enumerate(symbols):
        rng, price = random.Random(seed), 1000.0
        sid = await instruments.symbol_id(symbol)
        for day in reversed(days):
            price *= math.exp(rng.gauss(0, 0.012))
            close = Decimal(f"{price:.2f}")
            rows.append(
                {
                    "symbol_id": sid,
                    "timeframe": "1d",
                    "ts": _at_ist(day, 9, 15),
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": 100_000,
                }
            )
    await BarRepository(session, instruments).bulk_upsert(rows)
    await session.flush()


def _orb(symbol: str) -> Recommendation:
    return _rec(symbol).model_copy(update={"strategy_id": "orb_classic"})


@pytest.fixture
def live_stack(session, instruments, redis, audit, calendar) -> Stack:
    return Stack(
        session, instruments, redis, audit, calendar, facts=_live(session, instruments, calendar)
    )


class TestS14Ac1AnApprovalSizedFromTheLiveInputs:
    async def test_the_quantity_is_the_risk_budget_over_the_published_atr(
        self, live_stack: Stack
    ) -> None:
        atr = await _publish_atr(live_stack.redis, "INFY")
        await live_stack.service.start()
        await live_stack.publish(_orb("INFY"))
        result = await live_stack.consume_one()

        assert result is not None and result.outcome is Outcome.SUBMITTED, result
        (entry,) = live_stack.account.placed
        stop_distance = (atr * Decimal("1.5")).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
        # Rs 5,000 of risk (1% of 5 lakh) over the ATR stop: the binding clamp,
        # well inside the 83-share position cap and the 1,666-share margin cap.
        assert entry["quantity"] == int(Decimal(5000) / stop_distance)
        assert entry["quantity"] < 83
        (asked,) = live_stack.account.margin_calls
        assert [(a["tradingsymbol"], a["transaction_type"], a["product"]) for a in asked] == [
            ("INFY", "BUY", "MIS")
        ]

    async def test_a_dear_margin_binds_instead(self, live_stack: Stack) -> None:
        """Rs 8,000 a share against Rs 4 lakh available: 50 shares, below the
        ATR's answer - so the broker's number is the one that sized it."""
        atr = await _publish_atr(live_stack.redis, "INFY")
        live_stack.account.margin_per_share["INFY"] = 8000.0
        await live_stack.service.start()
        await live_stack.publish(_orb("INFY"))
        result = await live_stack.consume_one()

        assert result is not None and result.outcome is Outcome.SUBMITTED, result
        (entry,) = live_stack.account.placed
        assert entry["quantity"] == 50
        assert int(Decimal(5000) / (atr * Decimal("1.5"))) > 50, "the ATR alone allowed more"


class TestS14Ac2EachMissingInputRejectsByName:
    async def _rejected(self, stack: Stack, symbol: str = "INFY"):
        await stack.service.start()
        await stack.publish(_orb(symbol))
        result = await stack.consume_one()
        assert result is not None and result.outcome is Outcome.REJECTED, result
        assert stack.account.placed == [], "a rejected signal placed an order"
        return result

    async def test_no_published_atr(self, live_stack: Stack) -> None:
        result = await self._rejected(live_stack)
        assert "ATR for INFY" in result.detail

    async def test_a_stale_atr(self, live_stack: Stack) -> None:
        """Published half an hour ago: six 5m bars have closed since."""
        await _publish_atr(live_stack.redis, "INFY", last_open=NOW - dt.timedelta(minutes=35))
        result = await self._rejected(live_stack)
        assert "ATR for INFY" in result.detail

    async def test_a_strategy_this_service_was_not_given_has_no_atr(
        self, live_stack: Stack
    ) -> None:
        """The ATR is published on orb_classic's timeframe; a signal from a
        strategy the service does not know is not sized from it."""
        await _publish_atr(live_stack.redis, "INFY")
        await live_stack.service.start()
        await live_stack.publish(_rec("INFY"))  # strategy_id orb_long_v1
        result = await live_stack.consume_one()
        assert result is not None and result.outcome is Outcome.REJECTED
        assert "ATR for INFY" in result.detail
        assert live_stack.account.placed == []

    async def test_the_margin_call_fails(self, live_stack: Stack) -> None:
        await _publish_atr(live_stack.redis, "INFY")
        live_stack.account.margin_error = ConnectionError("Kite /margins/orders timed out")
        result = await self._rejected(live_stack)
        assert "per-share margin requirement for INFY" in result.detail

    async def test_the_broker_answers_zero(self, live_stack: Stack) -> None:
        await _publish_atr(live_stack.redis, "INFY")
        live_stack.account.margin_per_share["INFY"] = 0.0
        result = await self._rejected(live_stack)
        assert "per-share margin requirement for INFY" in result.detail


class TestS14Ac3AnUnknownCorrelationRefusesASecondPosition:
    async def _hold_tcs(self, stack: Stack) -> None:
        await _publish_atr(stack.redis, "TCS")
        await _publish_atr(stack.redis, "INFY")
        await stack.service.start()
        await stack.publish(_orb("TCS"))
        first = await stack.consume_one()
        assert first is not None and first.outcome is Outcome.SUBMITTED, first
        (entry,) = stack.account.placed
        stack.account.fill(entry["tag"], entry["quantity"], "1200.50")
        stack.account.hold("TCS", quantity=entry["quantity"])
        await stack.service.tick()
        assert isinstance(stack.manager.tracked("TCS"), ProtectedPosition)

    async def test_a_held_name_with_no_daily_history(self, live_stack: Stack) -> None:
        await self._hold_tcs(live_stack)
        placed = len(live_stack.account.placed)
        await live_stack.publish(_orb("INFY"))
        result = await live_stack.consume_one()

        assert result is not None and result.outcome is Outcome.REJECTED, result
        assert result.reason is not None and result.reason.value == "RISK_ENGINE_FAULT"
        assert "no correlation available" in result.detail and "TCS" in result.detail
        assert len(live_stack.account.placed) == placed

    async def test_the_control_with_history_the_second_position_is_taken(
        self, live_stack: Stack, session, instruments, calendar
    ) -> None:
        """Same book, daily closes in Postgres for both: computable, low, and
        the second entry goes - so the refusal above was the missing history."""
        await _daily_history(session, instruments, calendar, "INFY", "TCS")
        await self._hold_tcs(live_stack)
        await live_stack.publish(_orb("INFY"))
        result = await live_stack.consume_one()
        assert result is not None and result.outcome is Outcome.SUBMITTED, result

    async def test_history_that_stopped_before_yesterday_is_unknown(
        self, live_stack: Stack, session, instruments, calendar
    ) -> None:
        await _daily_history(session, instruments, calendar, "INFY", "TCS")
        await session.execute(
            text("DELETE FROM ohlcv WHERE timeframe = '1d' AND ts >= :since"),
            {"since": _at_ist(calendar.previous_trading_day(TRADE_DATE), 0, 0)},
        )
        await self._hold_tcs(live_stack)
        await live_stack.publish(_orb("INFY"))
        result = await live_stack.consume_one()
        assert result is not None and result.outcome is Outcome.REJECTED
        assert "no correlation available" in result.detail
