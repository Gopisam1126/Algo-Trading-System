"""SIT: a whole trading session driven by the execution service (E15-S13).

The integration tests prove each acceptance criterion once. This asks what
only a session can: **walk the clock through a day - signals arriving, the
broker filling, reconciliation running every minute, a restart, a kill switch
- and does every signal end exactly one explainable way, with every position
it opens protected?**

Real Postgres, Redis, Kite adapter mapping, gateway, protective stop, position
manager, reconciler and the factory-built engine. The account is Kite's payload
shapes with one behaviour added: a MARKET order fills at the next settle. The
three risk inputs nothing produces yet are answered (``Facts``) so the session
can approve; ``NoProducerYet`` is what production wires today, and refuses all.
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
from algotrader.common.audit import AuditWriter
from algotrader.common.calendar import IST, MarketCalendar, load_holidays_with_status
from algotrader.common.config import AppConfig
from algotrader.common.db import engine as db_engine
from algotrader.common.db.repositories import (
    DailyStatusRepository,
    InstrumentRepository,
    OrderRepository,
    PositionRepository,
)
from algotrader.common.enums import AIVerdict, Direction, RejectReason
from algotrader.common.events import stream as streams
from algotrader.common.events.envelope import Envelope
from algotrader.common.models.trading import Recommendation
from algotrader.common.redis import keys
from algotrader.execution.gateway import GatewayPolicy, OrderGateway
from algotrader.execution.halt import HaltController, HaltReason
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

    async def atr(self, symbol: str, *, now: dt.datetime) -> Decimal | None:
        return Decimal("12.5")

    async def margin_per_share(self, symbol, price, *, now):
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
        self, session, instruments, redis, audit, calendar, *, account=None, clock=None
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
            halts=self.halts,
            margins=adapter,
            store=RepositoryContextStore(
                positions=positions,
                instruments=instruments,
                daily_status=DailyStatusRepository(session, instruments),
            ),
            orders=orders,
            redis=redis,
            facts=Facts(),
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

        # Every entry the broker received is protected, with one stop each.
        entries = [p for p in day.account.placed if p["order_type"] == "MARKET"]
        assert sorted(p["tradingsymbol"] for p in entries) == ["INFY", "TCS"]
        for symbol in ("INFY", "TCS"):
            assert isinstance(day.manager.tracked(symbol), ProtectedPosition), symbol
        assert _stops(day.account) == {p["tradingsymbol"]: p["quantity"] for p in entries}
        assert not any(
            p["transaction_type"] == "SELL" and p["order_type"] == "MARKET"
            for p in day.account.placed
        ), "something was exited"

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
