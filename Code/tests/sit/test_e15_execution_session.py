"""SIT: a whole trading session driven by the execution service (E15-S13).

The integration tests prove each acceptance criterion once. This asks what
only a session can: **walk the clock through a day - signals arriving, the
broker filling, reconciliation running every minute, a restart, a kill switch
- and does every signal end exactly one explainable way, with every position
it opens protected?**

Real Postgres, Redis, Kite adapter mapping, gateway, protective stop, position
manager, reconciler and the factory-built engine. The account is Kite's payload
shapes with one behaviour added: a MARKET order fills at the next settle. The
live risk inputs are answered (``Facts``) so the session can approve - except
in SIT-23, which produces them for real (E15-S14): ATR from the state the
strategy side publishes, margin from the broker, correlations from daily
closes in Postgres.
"""

from __future__ import annotations

import datetime as dt
import math
import random
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
from algotrader.common.audit import AuditWriter
from algotrader.common.calendar import IST, MarketCalendar, load_holidays_with_status
from algotrader.common.config import AppConfig
from algotrader.common.db import engine as db_engine
from algotrader.common.db.repositories import (
    BarRepository,
    DailyStatusRepository,
    InstrumentRepository,
    OrderRepository,
    PositionRepository,
)
from algotrader.common.enums import AIVerdict, Direction, RejectReason, Timeframe
from algotrader.common.events import stream as streams
from algotrader.common.events.envelope import Envelope
from algotrader.common.models.market import Bar
from algotrader.common.models.trading import Recommendation
from algotrader.common.redis import keys
from algotrader.execution.gateway import GatewayPolicy, OrderGateway
from algotrader.execution.halt import HaltController, HaltReason
from algotrader.execution.live_inputs import (
    BrokerMarginPerShare,
    DailyCloseCorrelations,
    LiveSessionFacts,
    PublishedATR,
    strategy_timeframes,
)
from algotrader.execution.positions import PositionManager, ProtectedPosition, RedisMirror
from algotrader.execution.protective_stop import ProtectiveStop
from algotrader.execution.reconciliation import Reconciler
from algotrader.execution.service import (
    GROUP,
    ExecutionService,
    Outcome,
    RedisSignalSource,
    SignalResult,
)
from algotrader.execution.session_context import RepositoryContextStore
from algotrader.indicators.engine import IndicatorSet
from algotrader.indicators.publish import publish_indicator_state
from algotrader.strategy.dsl import load_strategy_yaml

pytestmark = [pytest.mark.integration]

DAY = dt.date(2026, 8, 25)  # an ordinary Tuesday
SECTORS = {"INFY": "IT", "TCS": "IT", "SBIN": "BANKS", "HDFCBANK": "BANKS", "WIPRO": "IT"}
NO_ROW_TODAY = {"SBIN"}
CAS = {"TCS"}


def _ist(hour: int, minute: int) -> dt.datetime:
    return dt.datetime.combine(DAY, dt.time(hour, minute), tzinfo=IST).astimezone(dt.UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
                "broker_token": f"sit21-tok-{i}",
                "tick_size": Decimal("0.05"),
                "lot_size": 1,
                "sector": sector,
            }
            for i, (symbol, sector) in enumerate(SECTORS.items())
        ]
    )
    await session.flush()
    await repo.refresh_cache()
    await DailyStatusRepository(session, repo).upsert(
        [
            {
                "symbol_id": await repo.symbol_id(symbol),
                "trade_date": DAY,
                "is_t2t": False,
                "is_asm": False,
                "is_gsm": False,
                "is_fno_ban": False,
                "is_cas_stock": symbol in CAS,
            }
            for symbol in SECTORS
            if symbol not in NO_ROW_TODAY
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


# ---------------------------------------------------------------------------
# The account and the stack
# ---------------------------------------------------------------------------


class SettlingAccount:
    """Kite's payloads; a MARKET order fills in full at the next settle()."""

    def __init__(self) -> None:
        self.book: list[dict[str, Any]] = []
        self.net: dict[str, dict[str, Any]] = {}
        self.placed: list[dict[str, Any]] = []

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
        return {"net": [dict(r) for r in self.net.values()], "day": []}

    def margins(self, segment: str) -> dict[str, Any]:
        return {
            "available": {"cash": 400000.0, "live_balance": 400000.0},
            "utilised": {"debits": 0.0},
        }

    def settle(self) -> None:
        for row in self.book:
            if row["order_type"] != "MARKET" or row["status"] != "OPEN":
                continue
            row.update(status="COMPLETE", filled_quantity=row["quantity"], average_price="1200.50")
            held = self.net.setdefault(
                row["tradingsymbol"],
                {
                    "tradingsymbol": row["tradingsymbol"],
                    "exchange": "NSE",
                    "product": "MIS",
                    "quantity": 0,
                    "day_buy_quantity": 0,
                    "day_sell_quantity": 0,
                    "average_price": 1200.5,
                    "pnl": 0,
                },
            )
            if row["transaction_type"] == "BUY":
                held["quantity"] += row["quantity"]
                held["day_buy_quantity"] += row["quantity"]
            else:
                held["quantity"] -= row["quantity"]
                held["day_sell_quantity"] += row["quantity"]


class Facts:
    async def unhealthy_services(self, *, now: dt.datetime) -> tuple[str, ...] | None:
        return ()

    async def atr(self, symbol: str, *, strategy_id: str, now: dt.datetime) -> Decimal | None:
        return Decimal("12.5")

    async def margin_per_share(self, symbol, price, *, direction, now):
        return Decimal("240")

    async def correlations(self, symbol, against, *, now):
        return {s: Decimal("0.1") for s in against}


class Clock:
    def __init__(self, now: dt.datetime) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


class Day:
    """The real stack over one account, one clock and one set of tables."""

    def __init__(
        self,
        session,
        instruments,
        redis,
        audit,
        calendar,
        *,
        account=None,
        clock=None,
        facts=None,
    ) -> None:
        self.account = account or SettlingAccount()
        self.clock = clock or Clock(_ist(9, 10))
        self.redis = redis
        adapter = KiteTradingAdapter(
            auth=None, client=self.account, algo_id="", tick_size_for=instruments.tick_size
        )
        orders = OrderRepository(session, instruments)
        gateway = OrderGateway(
            adapter,
            policy=GatewayPolicy(algo_id=None, market_protection=Decimal("-1")),
            store=orders,
        )
        self.halts = HaltController(redis)
        protector = ProtectiveStop(gateway, halter=self.halts)
        positions = PositionRepository(session, instruments)
        self.manager = PositionManager(
            protector=protector, store=positions, mirror=RedisMirror(redis), calendar=calendar
        )
        reconciler = Reconciler(
            broker=adapter,
            ledger=orders,
            manager=self.manager,
            protector=protector,
            halter=self.halts,
            canceller=gateway,
            audit=audit.write,
        )
        config = AppConfig(
            execution={
                "no_trade_windows": [
                    (dt.time(9, 15), dt.time(9, 20)),
                    (dt.time(15, 0), dt.time(15, 30)),
                ]
            }
        )
        self.service = ExecutionService(
            config=config,
            calendar=calendar,
            gateway=gateway,
            manager=self.manager,
            reconciler=reconciler,
            protector=protector,
            broker=adapter,
            halts=self.halts,
            margins=adapter,
            store=RepositoryContextStore(
                positions=positions,
                instruments=instruments,
                daily_status=DailyStatusRepository(session, instruments),
            ),
            orders=orders,
            redis=redis,
            # SIT-23 passes the live producers, built over this day's adapter.
            facts=facts(adapter) if facts is not None else Facts(),
            clock=self.clock,
            audit=audit.write,
        )
        self.source = RedisSignalSource(redis, block_ms=20)
        self.results: dict[uuid.UUID, list[SignalResult]] = {}

    async def publish(self, rec: Recommendation) -> None:
        await streams.publish(
            self.redis,
            keys.stream_signals(),
            Envelope(
                correlation_id=rec.correlation_id,
                emitted_at=self.clock.now,
                emitted_by="signal-svc",
                payload=rec.model_dump(mode="json"),
            ),
            maxlen=streams.default_maxlen(keys.stream_signals()),
        )

    async def drain(self) -> None:
        await self.source.start()
        while batch := await self.source.next_batch():
            for delivery in batch:
                result = await self.service.deliver(self.source, delivery)
                if result is not None:
                    self.results.setdefault(delivery.envelope.correlation_id, []).append(result)

    async def walk(
        self,
        until: dt.datetime,
        signals: dict[dt.datetime, list[Recommendation]] | None = None,
        *,
        at_each_minute=None,
    ) -> None:
        """Minute by minute: the broker settles, a cycle runs, signals arrive."""
        signals = signals or {}
        while self.clock.now <= until:
            if at_each_minute is not None:
                await at_each_minute(self.clock.now)
            self.account.settle()
            await self.service.tick()
            for rec in signals.get(self.clock.now, []):
                await self.publish(rec)
            await self.drain()
            self.clock.now += dt.timedelta(minutes=1)


def _rec(
    symbol: str,
    at: dt.datetime,
    *,
    verdict: AIVerdict = AIVerdict.CONFIRM,
    emitted_at: dt.datetime | None = None,
) -> Recommendation:
    return Recommendation(
        correlation_id=uuid.uuid4(),
        symbol=symbol,
        strategy_id="orb_long_v1",
        direction=Direction.LONG,
        trigger_price=Decimal("1200.00"),
        suggested_stop=Decimal("1186.45"),
        timeframe_agreement=3,
        ai_confidence=Decimal("0.82"),
        ai_verdict=verdict,
        ai_rationale="sit",
        emitted_at=emitted_at or at,
    )


async def _audited(session: AsyncSession) -> dict[str, list[tuple[str, str]]]:
    rows = (
        await session.execute(
            text("SELECT correlation_id, stage, outcome FROM decision_log ORDER BY seq")
        )
    ).all()
    out: dict[str, list[tuple[str, str]]] = {}
    for r in rows:
        out.setdefault(str(r.correlation_id), []).append((r.stage, r.outcome))
    return out


def _stops(account: SettlingAccount) -> dict[str, int]:
    return {p["tradingsymbol"]: p["quantity"] for p in account.placed if p["order_type"] == "SL-M"}


# ---------------------------------------------------------------------------
# SIT-21
# ---------------------------------------------------------------------------


class TestSit21ASessionDrivenByTheExecutionService:
    async def test_every_signal_ends_one_explainable_way_and_every_entry_is_protected(
        self, session, instruments, redis, audit, calendar
    ) -> None:
        day = Day(session, instruments, redis, audit, calendar)
        await day.service.start()

        infy = _rec("INFY", _ist(10, 0))
        signals = {
            _ist(9, 12): [_rec("INFY", _ist(9, 12))],  # pre-open
            _ist(9, 17): [_rec("TCS", _ist(9, 17))],  # opening blackout
            _ist(10, 0): [infy, infy],  # and its redelivery
            _ist(10, 5): [_rec("TCS", _ist(10, 5))],
            _ist(11, 0): [_rec("SBIN", _ist(11, 0))],  # no daily row
            _ist(11, 30): [_rec("HDFCBANK", _ist(11, 30), verdict=AIVerdict.VETO)],
            _ist(12, 0): [_rec("HDFCBANK", _ist(12, 0), emitted_at=_ist(11, 55))],  # stale
            _ist(13, 0): [_rec("INFY", _ist(13, 0))],  # already held
            _ist(15, 5): [_rec("WIPRO", _ist(15, 5))],  # closing blackout
        }
        published = [rec for recs in signals.values() for rec in recs]
        await day.walk(_ist(15, 30), signals)

        outcomes = {
            rec.correlation_id: [(r.outcome, r.reason) for r in day.results[rec.correlation_id]]
            for rec in published
        }
        assert outcomes[infy.correlation_id] == [
            (Outcome.SUBMITTED, None),
            (Outcome.DUPLICATE, None),
        ]
        by_symbol_time = {(r.symbol, r.emitted_at): outcomes[r.correlation_id] for r in published}
        assert by_symbol_time[("INFY", _ist(9, 12))] == [
            (Outcome.REJECTED, RejectReason.OUTSIDE_TRADING_WINDOW)
        ]
        assert by_symbol_time[("TCS", _ist(9, 17))] == [
            (Outcome.REJECTED, RejectReason.NO_TRADE_WINDOW)
        ]
        assert by_symbol_time[("TCS", _ist(10, 5))] == [(Outcome.SUBMITTED, None)]
        assert by_symbol_time[("SBIN", _ist(11, 0))] == [
            (Outcome.REJECTED, RejectReason.RISK_ENGINE_FAULT)
        ]
        assert by_symbol_time[("HDFCBANK", _ist(11, 30))] == [
            (Outcome.REFUSED, RejectReason.AI_VETO)
        ]
        assert by_symbol_time[("HDFCBANK", _ist(11, 55))] == [
            (Outcome.REFUSED, RejectReason.STALE_DATA)
        ]
        assert by_symbol_time[("INFY", _ist(13, 0))] == [
            (Outcome.REJECTED, RejectReason.ALREADY_HOLDING)
        ]
        assert by_symbol_time[("WIPRO", _ist(15, 5))] == [
            (Outcome.REJECTED, RejectReason.NO_TRADE_WINDOW)
        ]

        # Every entry the broker received was protected by one stop, and then
        # - since E15-S06 - squared off at its own deadline and closed as TIME.
        entries = [p for p in day.account.placed if p["transaction_type"] == "BUY"]
        assert sorted(p["tradingsymbol"] for p in entries) == ["INFY", "TCS"]
        assert _stops(day.account) == {p["tradingsymbol"]: p["quantity"] for p in entries}
        exits = [
            p
            for p in day.account.placed
            if p["transaction_type"] == "SELL" and p["order_type"] == "MARKET"
        ]
        assert {p["tradingsymbol"]: p["quantity"] for p in exits} == {
            p["tradingsymbol"]: p["quantity"] for p in entries
        }, "each position is exited once, for what it held"
        assert day.manager.open_positions() == [], "a position survived its deadline"
        closed = (
            await session.execute(
                text("SELECT status, exit_reason FROM positions ORDER BY slot_index")
            )
        ).all()
        assert [(r.status, r.exit_reason) for r in closed] == [("CLOSED", "TIME")] * 2

        # Every signal is explainable from the audit table alone.
        audited = await _audited(session)
        for rec in published:
            assert str(rec.correlation_id) in audited, f"{rec.symbol} left no audit row"
        assert not (await day.halts.state()).any_halted
        assert len(day.service.pending) == 0
        assert await streams.pending_count(redis, keys.stream_signals(), GROUP) == 0

    async def test_a_restart_mid_session_keeps_the_book_and_carries_on(
        self, session, instruments, redis, audit, calendar
    ) -> None:
        clock = Clock(_ist(9, 30))
        first = Day(session, instruments, redis, audit, calendar, clock=clock)
        await first.service.start()
        await first.walk(
            _ist(10, 30),
            {_ist(10, 0): [_rec("INFY", _ist(10, 0))], _ist(10, 5): [_rec("TCS", _ist(10, 5))]},
        )
        assert {p["tradingsymbol"] for p in first.account.placed} == {"INFY", "TCS"}
        placed_before = len(first.account.placed)

        # The process dies and comes back on the same account, tables and Redis.
        second = Day(
            session, instruments, redis, audit, calendar, account=first.account, clock=clock
        )
        report = await second.service.start()
        assert report is not None and report.exited == [] and not report.halted
        for symbol in ("INFY", "TCS"):
            assert isinstance(second.manager.tracked(symbol), ProtectedPosition), symbol
        assert len(second.account.placed) == placed_before, "the restart placed orders"

        hdfc = _rec("HDFCBANK", _ist(11, 0))
        await second.walk(_ist(11, 30), {_ist(11, 0): [hdfc]})
        (result,) = second.results[hdfc.correlation_id]
        assert result.outcome is Outcome.SUBMITTED
        assert result.slot_index == 2, "slots 0 and 1 are the restored positions'"
        assert isinstance(second.manager.tracked("HDFCBANK"), ProtectedPosition)
        assert not (await second.halts.state()).any_halted

    async def test_a_kill_switch_stops_new_entries_and_leaves_protection_alone(
        self, session, instruments, redis, audit, calendar
    ) -> None:
        day = Day(session, instruments, redis, audit, calendar, clock=Clock(_ist(9, 30)))
        await day.service.start()

        async def operator(now: dt.datetime) -> None:
            if now == _ist(11, 0):
                await day.halts.arm(HaltReason.MANUAL, detail="drill", armed_by="sit", now=now)

        late = _rec("TCS", _ist(11, 15))
        await day.walk(
            _ist(12, 0),
            {_ist(10, 0): [_rec("INFY", _ist(10, 0))], _ist(11, 15): [late]},
            at_each_minute=operator,
        )
        (result,) = day.results[late.correlation_id]
        assert (result.outcome, result.reason) == (
            Outcome.REJECTED,
            RejectReason.KILL_SWITCH_ACTIVE,
        )
        assert {p["tradingsymbol"] for p in day.account.placed} == {"INFY"}
        assert isinstance(day.manager.tracked("INFY"), ProtectedPosition)
        assert not any(
            p["transaction_type"] == "SELL" and p["order_type"] == "MARKET"
            for p in day.account.placed
        ), "a halt must never disable protection"


# ---------------------------------------------------------------------------
# SIT-22 - the square-off timer across a session (E15-S06)
# ---------------------------------------------------------------------------


def _working_sells(account: SettlingAccount, symbol: str) -> int:
    """Sell orders for this symbol that could still fill: a working stop or exit."""
    return sum(
        1
        for row in account.book
        if row["tradingsymbol"] == symbol
        and row["transaction_type"] == "SELL"
        and row["status"] in ("OPEN", "TRIGGER PENDING")
    )


class TestSit22NoPositionSurvivesItsDeadline:
    async def test_every_position_leaves_at_its_own_deadline_and_never_twice(
        self, session, instruments, redis, audit, calendar
    ) -> None:
        day = Day(session, instruments, redis, audit, calendar, clock=Clock(_ist(9, 30)))
        await day.service.start()
        exits_at: dict[str, dt.datetime] = {}
        worst: dict[str, int] = {}

        async def watch(now: dt.datetime) -> None:
            for row in day.account.placed:
                key = row["tradingsymbol"]
                if row["transaction_type"] == "SELL" and row["order_type"] == "MARKET":
                    exits_at.setdefault(key, now)
            for symbol in ("INFY", "TCS", "HDFCBANK"):
                worst[symbol] = max(worst.get(symbol, 0), _working_sells(day.account, symbol))

        await day.walk(
            _ist(15, 30),
            {
                _ist(10, 0): [_rec("INFY", _ist(10, 0))],  # non-CAS: 15:15
                _ist(10, 5): [_rec("TCS", _ist(10, 5))],  # CAS: 15:05
                _ist(14, 0): [_rec("HDFCBANK", _ist(14, 0))],  # non-CAS, late entry
            },
            at_each_minute=watch,
        )
        # The exit is placed IN the deadline's minute and seen by the next one.
        assert exits_at == {
            "TCS": _ist(15, 6),
            "INFY": _ist(15, 16),
            "HDFCBANK": _ist(15, 16),
        }
        assert worst == {"INFY": 1, "TCS": 1, "HDFCBANK": 1}, "a stop and an exit both working"
        assert day.manager.open_positions() == []
        rows = (
            await session.execute(text("SELECT exit_reason FROM positions ORDER BY slot_index"))
        ).all()
        assert [r.exit_reason for r in rows] == ["TIME", "TIME", "TIME"]
        assert all(held["quantity"] == 0 for held in day.account.net.values()), "flat at close"
        assert not (await day.halts.state()).any_halted

    async def test_a_restart_after_the_deadline_still_squares_off_before_the_broker(
        self, session, instruments, redis, audit, calendar
    ) -> None:
        clock = Clock(_ist(9, 30))
        first = Day(session, instruments, redis, audit, calendar, clock=clock)
        await first.service.start()
        await first.walk(_ist(15, 10), {_ist(10, 0): [_rec("INFY", _ist(10, 0))]})
        assert isinstance(first.manager.tracked("INFY"), ProtectedPosition)

        # Down across the deadline; back at 15:17, three minutes before Zerodha.
        clock.now = _ist(15, 17)
        second = Day(
            session, instruments, redis, audit, calendar, account=first.account, clock=clock
        )
        await second.service.start()
        exits = [
            p
            for p in second.account.placed
            if p["transaction_type"] == "SELL" and p["order_type"] == "MARKET"
        ]
        (entry,) = [p for p in second.account.placed if p["transaction_type"] == "BUY"]
        assert [(p["tradingsymbol"], p["quantity"]) for p in exits] == [
            ("INFY", entry["quantity"])
        ], "the restart squared off at once, once, for what was held"
        await second.walk(_ist(15, 20))
        assert second.manager.open_positions() == []
        (row,) = (await session.execute(text("SELECT exit_reason FROM positions"))).all()
        assert row.exit_reason == "TIME"

    async def test_a_kill_switch_does_not_stop_the_time_exits(
        self, session, instruments, redis, audit, calendar
    ) -> None:
        day = Day(session, instruments, redis, audit, calendar, clock=Clock(_ist(9, 30)))
        await day.service.start()

        async def operator(now: dt.datetime) -> None:
            if now == _ist(14, 0):
                await day.halts.arm(HaltReason.MANUAL, detail="drill", armed_by="sit", now=now)

        await day.walk(
            _ist(15, 30),
            {_ist(10, 0): [_rec("INFY", _ist(10, 0))]},
            at_each_minute=operator,
        )
        assert (await day.halts.state()).kill_switch
        assert day.manager.open_positions() == [], "a halt must never disable the exit path"
        (row,) = (await session.execute(text("SELECT exit_reason FROM positions"))).all()
        assert row.exit_reason == "TIME"

    async def test_yesterdays_open_row_halts_the_morning_and_nothing_is_sold_all_day(
        self, session, instruments, redis, audit, calendar
    ) -> None:
        """The defect found designing this story, as a session: the broker
        squared yesterday's position off while we were down, and the row stayed
        OPEN. Before the fix the first cycle of the morning SOLD 40 shares the
        account did not hold."""
        yesterday = dt.date(2026, 8, 24)
        day = Day(session, instruments, redis, audit, calendar, clock=Clock(_ist(9, 10)))
        positions = PositionRepository(session, instruments)
        await positions.open_position(
            {
                "correlation_id": uuid.uuid4(),
                "symbol": "INFY",
                "slot_index": 0,
                "direction": "LONG",
                "quantity": 40,
                "entry_price": Decimal("1200.0000"),
                "stop_price": Decimal("1186.4500"),
                "opened_at": dt.datetime.combine(yesterday, dt.time(10, 0), tzinfo=IST),
                "squareoff_deadline": dt.datetime.combine(yesterday, dt.time(15, 15), tzinfo=IST),
            }
        )
        await day.service.start()
        late = _rec("TCS", _ist(11, 0))
        await day.walk(_ist(15, 30), {_ist(11, 0): [late]})

        assert day.account.placed == [], "nothing was sent all day"
        state = await day.halts.state()
        assert state.records["kill_switch"].reason is HaltReason.POSITION_NOT_HELD
        (result,) = day.results[late.correlation_id]
        assert result.reason is RejectReason.KILL_SWITCH_ACTIVE


# ---------------------------------------------------------------------------
# SIT-23 - a session sized from the live inputs (E15-S14)
# ---------------------------------------------------------------------------

_ORB = load_strategy_yaml(
    (Path(__file__).resolve().parents[2] / "config" / "strategies" / "orb_classic.yaml").read_text()
)


class _Healthy:
    """E20-S05's roll-up does not exist yet; answered so the three inputs this
    story produces are what the session exercises."""

    async def unhealthy_services(self, *, now: dt.datetime) -> tuple[str, ...] | None:
        return ()


class StrategySide:
    """What E13-S01's loop will do with each sealed 5m bar: fold it into the
    symbol's indicator set and publish the state BEFORE any signal on it.

    ``quiet`` symbols get no bars (a feed gap); a symbol coming back from one
    is re-warmed from history, as the engine requires after a gap.
    """

    def __init__(self, redis: aioredis.Redis, symbols: list[str]) -> None:
        self.redis = redis
        self.quiet: set[str] = set()
        self._sets: dict[str, IndicatorSet] = {}
        self._gapped: set[str] = set()
        for symbol in symbols:
            self._sets[symbol] = self._warmed(symbol, through=_ist(9, 10))

    @staticmethod
    def _bar(symbol: str, open_ts: dt.datetime) -> Bar:
        mid = Decimal(1200 + (open_ts.minute // 5) % 7)
        return Bar(
            symbol=symbol,
            timeframe=Timeframe.M5,
            open_ts=open_ts,
            open=mid,
            high=mid + 30,
            low=mid - 30,
            close=mid,
            volume=10_000,
        )

    def _warmed(self, symbol: str, *, through: dt.datetime) -> IndicatorSet:
        s = IndicatorSet(symbol=symbol, timeframe=Timeframe.M5)
        s.warm_up(
            [self._bar(symbol, through - dt.timedelta(minutes=5 * k)) for k in range(259, -1, -1)]
        )
        return s

    async def __call__(self, now: dt.datetime) -> None:
        open_ = dt.datetime.combine(DAY, dt.time(9, 15), tzinfo=IST).astimezone(dt.UTC)
        if now <= open_ or int((now - open_).total_seconds()) % 300:
            return  # not a 5m boundary: no bar has just closed
        for symbol, indicator_set in self._sets.items():
            if symbol in self.quiet:
                self._gapped.add(symbol)
                continue
            if symbol in self._gapped:
                self._gapped.discard(symbol)
                indicator_set = self._sets[symbol] = self._warmed(
                    symbol, through=now - dt.timedelta(minutes=10)
                )
            indicator_set.update(self._bar(symbol, now - dt.timedelta(minutes=5)))
            await publish_indicator_state(self.redis, indicator_set)


class MarginDesk:
    """Kite's ``/margins/orders`` for a SettlingAccount, with an outage switch."""

    def __init__(self) -> None:
        self.down = False
        self.calls = 0

    def order_margins(self, params: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.calls += 1
        if self.down:
            raise ConnectionError("Kite /margins/orders is not answering")
        return [{"tradingsymbol": params[0]["tradingsymbol"], "total": 240.0}]


async def _daily_closes(session, instruments, calendar, *symbols: str) -> None:
    days = [calendar.previous_trading_day(DAY)]
    while len(days) < 75:
        days.append(calendar.previous_trading_day(days[-1]))
    rows = []
    for seed, symbol in enumerate(symbols):
        rng, price = random.Random(100 + seed), 1000.0
        sid = await instruments.symbol_id(symbol)
        for day in reversed(days):
            price *= math.exp(rng.gauss(0, 0.012))
            close = Decimal(f"{price:.2f}")
            rows.append(
                {
                    "symbol_id": sid,
                    "timeframe": "1d",
                    "ts": dt.datetime.combine(day, dt.time(9, 15), tzinfo=IST).astimezone(dt.UTC),
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": 100_000,
                }
            )
    await BarRepository(session, instruments).bulk_upsert(rows)
    await session.flush()


class TestSit23ASessionSizedFromTheLiveInputs:
    async def test_every_live_input_gap_refuses_by_name_and_the_rest_trade(
        self, session, instruments, redis, audit, calendar
    ) -> None:
        # Daily history for three IT names; HDFCBANK has none.
        await _daily_closes(session, instruments, calendar, "INFY", "TCS", "WIPRO")
        strategy = StrategySide(redis, ["INFY", "TCS", "WIPRO", "HDFCBANK"])
        desk = MarginDesk()

        def live(adapter: KiteTradingAdapter) -> LiveSessionFacts:
            return LiveSessionFacts(
                health=_Healthy(),
                atr=PublishedATR(redis, timeframes=strategy_timeframes([_ORB])),
                margin=BrokerMarginPerShare(adapter),
                correlations=DailyCloseCorrelations(
                    BarRepository(session, instruments), calendar=calendar
                ),
            )

        account = SettlingAccount()
        account.order_margins = desk.order_margins  # type: ignore[attr-defined]
        day = Day(session, instruments, redis, audit, calendar, account=account, facts=live)
        await day.service.start()

        def orb(symbol: str, at: dt.datetime) -> Recommendation:
            return _rec(symbol, at).model_copy(update={"strategy_id": "orb_classic"})

        signals = {
            _ist(10, 1): [orb("INFY", _ist(10, 1))],  # sized from the live ATR
            _ist(10, 31): [orb("HDFCBANK", _ist(10, 31))],  # no history: correlation unknown
            _ist(11, 21): [orb("TCS", _ist(11, 21))],  # TCS feed quiet since 11:00
            _ist(12, 11): [orb("TCS", _ist(12, 11))],  # re-warmed and publishing again
            _ist(13, 6): [orb("WIPRO", _ist(13, 6))],  # the margin desk is down
            _ist(13, 31): [orb("WIPRO", _ist(13, 31))],  # and back
        }
        published = [rec for recs in signals.values() for rec in recs]

        async def each_minute(now: dt.datetime) -> None:
            if now == _ist(11, 0):
                strategy.quiet.add("TCS")
            if now == _ist(12, 0):
                strategy.quiet.discard("TCS")
            desk.down = _ist(13, 0) <= now < _ist(13, 10)
            await strategy(now)

        await day.walk(_ist(15, 30), signals, at_each_minute=each_minute)

        def ended(rec: Recommendation) -> tuple[Outcome, RejectReason | None, str]:
            (result,) = day.results[rec.correlation_id]
            return result.outcome, result.reason, result.detail

        infy, hdfc, tcs_quiet, tcs_back, wipro_down, wipro_back = published
        assert ended(infy)[0] is Outcome.SUBMITTED
        assert ended(tcs_back)[0] is Outcome.SUBMITTED
        assert ended(wipro_back)[0] is Outcome.SUBMITTED

        outcome, reason, detail = ended(hdfc)
        assert (outcome, reason) == (Outcome.REJECTED, RejectReason.RISK_ENGINE_FAULT)
        assert "no correlation available" in detail and "INFY" in detail
        outcome, reason, detail = ended(tcs_quiet)
        assert outcome is Outcome.REJECTED and "ATR for TCS" in detail
        outcome, reason, detail = ended(wipro_down)
        assert outcome is Outcome.REJECTED and "per-share margin requirement for WIPRO" in detail

        # The first entry was sized from the ATR the strategy side published:
        # Rs 5,000 of risk over a 1.5 x ATR stop, inside every other clamp.
        entries = {
            p["tradingsymbol"]: p for p in day.account.placed if p["transaction_type"] == "BUY"
        }
        assert sorted(entries) == ["INFY", "TCS", "WIPRO"]
        assert 0 < entries["INFY"]["quantity"] < 83

        # Nothing a refusal touched reached the broker; everything that did was
        # protected, then left at its own deadline, once.
        assert _stops(day.account) == {s: p["quantity"] for s, p in entries.items()}
        exits = {
            p["tradingsymbol"]: p["quantity"]
            for p in day.account.placed
            if p["transaction_type"] == "SELL" and p["order_type"] == "MARKET"
        }
        assert exits == {s: p["quantity"] for s, p in entries.items()}
        assert day.manager.open_positions() == []
        closed = (await session.execute(text("SELECT exit_reason FROM positions"))).all()
        assert [r.exit_reason for r in closed] == ["TIME"] * 3

        audited = await _audited(session)
        for rec in published:
            assert str(rec.correlation_id) in audited, f"{rec.symbol} left no audit row"
        assert not (await day.halts.state()).any_halted
        assert desk.calls >= 4, "every sized signal asked the broker"
