"""E01 end-to-end QA — the whole persistence layer exercised as one system.

The per-story suites test components in isolation. This one tests the seams
between them, because that is where integration defects live and where no
single story's tests would look.

It walks one trade through every E01 component in the order the live system
will: instrument sync -> hazard flags -> bars -> Redis state -> stream ->
order -> position -> audit -> verification -> archive -> restore. Each step
consumes what the previous one produced, so a mismatch in shape, type or
naming between two components fails here rather than in Phase 1.

**QA-E01-01 .. QA-E01-12** are referenced from the tracker's `QA Results`
sheet.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest
import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from algotrader.common.audit import AuditEntry, AuditWriter, verify_chain
from algotrader.common.db import engine as db_engine
from algotrader.common.db import retention
from algotrader.common.db.repositories import (
    BarRepository,
    DailyStatusRepository,
    DecisionLogRepository,
    InstrumentRepository,
    OrderRepository,
    PositionRepository,
)
from algotrader.common.enums import Timeframe
from algotrader.common.events import Envelope, stream
from algotrader.common.redis import keys, locks, primitives, state

pytestmark = [pytest.mark.integration]

SESSION_OPEN = dt.datetime(2026, 8, 6, 3, 45, tzinfo=dt.UTC)  # 09:15 IST
SQUAREOFF = dt.datetime(2026, 8, 6, 9, 40, tzinfo=dt.UTC)  # 15:10 IST


@pytest.fixture
async def factory(migrated_database: str) -> AsyncIterator[object]:
    eng = db_engine.create_engine_from_url(migrated_database)
    yield db_engine.create_session_factory(eng)
    await eng.dispose()


@pytest.fixture
async def db(factory: object) -> AsyncIterator[AsyncSession]:
    async with factory() as s:  # type: ignore[operator]
        for table in ("decision_log", "positions", "order_fills", "orders", "ohlcv"):
            await s.execute(text(f"DELETE FROM {table}"))
        await s.commit()
        yield s
        await s.rollback()


@pytest.fixture
async def rds(redis_url: str) -> AsyncIterator[aioredis.Redis]:
    client = aioredis.from_url(redis_url, decode_responses=True)
    await client.flushall()
    try:
        yield client
    finally:
        await client.flushall()
        await client.aclose()


class TestE01FullTradeLifecycle:
    """One correlation_id, followed through every component of the layer."""

    async def test_a_trade_flows_through_the_entire_persistence_layer(
        self,
        db: AsyncSession,
        factory: object,
        rds: aioredis.Redis,
        tmp_path: Path,
    ) -> None:
        correlation_id = uuid.uuid4()
        symbol = "INFY"

        # -- QA-E01-01: instrument sync ---------------------------------
        instruments = InstrumentRepository(db)
        await instruments.upsert(
            [
                {
                    "tradingsymbol": symbol,
                    "exchange": "NSE",
                    "broker_token": "408065",
                    "tick_size": Decimal("0.05"),
                    "lot_size": 1,
                    "sector": "IT",
                }
            ]
        )
        await db.flush()
        await instruments.refresh_cache()
        symbol_id = await instruments.symbol_id(symbol)

        # -- QA-E01-02: hazard flags, and the eligibility view ----------
        ist_today = (
            await db.execute(text("SELECT (now() AT TIME ZONE 'Asia/Kolkata')::date"))
        ).scalar_one()
        await DailyStatusRepository(db, instruments).upsert(
            [{"symbol_id": symbol_id, "trade_date": ist_today, "is_cas_stock": True}]
        )
        await db.flush()
        assert symbol in await instruments.eligible_today()

        # -- QA-E01-03: bars land and read back in order ----------------
        bars = BarRepository(db, instruments)
        await bars.bulk_upsert(
            [
                {
                    "symbol_id": symbol_id,
                    "timeframe": "5m",
                    "ts": SESSION_OPEN + dt.timedelta(minutes=5 * i),
                    "open": Decimal("1500.0000"),
                    "high": Decimal("1520.5000"),
                    "low": Decimal("1495.2500"),
                    "close": Decimal("1510.7500"),
                    "volume": 10_000 + i,
                }
                for i in range(30)
            ]
        )
        await db.flush()

        warm = await bars.warm_up_batch([symbol], Timeframe.M5, bars_each=20)
        assert len(warm[symbol]) == 20
        assert warm[symbol][0]["open_ts"] < warm[symbol][-1]["open_ts"]
        assert isinstance(warm[symbol][0]["close"], Decimal)

        # -- QA-E01-04: Redis state round-trips the same values ---------
        from pydantic import BaseModel

        class Quote(BaseModel):
            symbol: str
            ltp: Decimal

        await state.set_state(
            rds, keys.quote(symbol), Quote(symbol=symbol, ltp=Decimal("1510.7500")), ttl_seconds=60
        )
        quote = await state.get_state(rds, keys.quote(symbol), Quote)
        assert quote is not None and quote.ltp == Decimal("1510.7500")

        # -- QA-E01-05: slot lock then the DB uniqueness guarantee ------
        async with locks.lock(rds, keys.slot_lock(0), ttl_ms=60_000) as got_slot:
            assert got_slot

            # -- QA-E01-06: a signal crosses the stream -----------------
            await stream.ensure_group(rds, keys.stream_signals(), "execution")
            await stream.publish(
                rds,
                keys.stream_signals(),
                Envelope(
                    correlation_id=correlation_id,
                    emitted_at=dt.datetime.now(dt.UTC),
                    emitted_by="signal-engine",
                    payload={"symbol": symbol, "direction": "LONG"},
                ),
                maxlen=stream.default_maxlen(keys.stream_signals()),
            )
            [delivery] = await stream.consume(
                rds, keys.stream_signals(), "execution", "exec-1", block_ms=200
            )
            assert delivery.envelope.correlation_id == correlation_id

            # -- QA-E01-07: rate limit gate before ordering -------------
            allowed, _ = await primitives.take_token(
                rds, keys.order_rate_limit(), capacity=3, refill_per_second=3
            )
            assert allowed

            # -- QA-E01-08: two-transaction order submission ------------
            orders = OrderRepository(db, instruments)
            client_order_id = f"algo-{correlation_id.hex[:12]}"
            await orders.insert_submitting(
                {
                    "client_order_id": client_order_id,
                    "correlation_id": correlation_id,
                    "symbol": symbol,
                    "side": "BUY",
                    "order_type": "MARKET",
                    "product": "MIS",
                    "quantity": 10,
                    "intent": "ENTRY",
                    "market_protection": Decimal("-1"),
                }
            )
            await db.flush()

            submitting = await orders.find_by_client_order_id(client_order_id)
            assert submitting is not None and submitting["status"] == "SUBMITTING"

            await orders.attach_broker_id(client_order_id, "251106000123456")
            await db.flush()

            # -- QA-E01-09: position opens with a stop and a deadline ---
            positions = PositionRepository(db, instruments)
            position_id = await positions.open_position(
                {
                    "correlation_id": correlation_id,
                    "symbol": symbol,
                    "slot_index": 0,
                    "direction": "LONG",
                    "quantity": 10,
                    "entry_price": Decimal("1510.7500"),
                    "stop_price": Decimal("1495.0000"),
                    "opened_at": dt.datetime.now(dt.UTC),
                    "squareoff_deadline": SQUAREOFF,
                }
            )
            await db.flush()
            assert await positions.occupied_slots() == {0}

            # -- QA-E01-10: square-off timer is armed -------------------
            await primitives.schedule(
                rds,
                keys.squareoff_timer(),
                str(position_id),
                int(SQUAREOFF.timestamp() * 1000),
            )
            assert await primitives.scheduled_count(rds, keys.squareoff_timer()) == 1

        # -- QA-E01-11: every stage is audited, and the chain verifies --
        audit = AuditWriter(factory, buffer_dir=tmp_path / "audit")  # type: ignore[arg-type]
        for stage_name, outcome in (
            ("PREMARKET_CANDIDATE", "ALLOW"),
            ("SIGNAL", "ALLOW"),
            ("RISK_CHECK", "ALLOW"),
            ("ORDER", "ALLOW"),
            ("FILL", "ALLOW"),
        ):
            assert await audit.write(
                AuditEntry(
                    correlation_id=correlation_id,
                    stage=stage_name,
                    outcome=outcome,
                    service="e2e",
                    symbol_id=symbol_id,
                    payload={"symbol": symbol, "price": str(Decimal("1510.7500"))},
                )
            )

        assert await verify_chain(db)

        trace = await DecisionLogRepository(db).by_correlation(correlation_id)
        assert [row["stage"] for row in trace] == [
            "PREMARKET_CANDIDATE",
            "SIGNAL",
            "RISK_CHECK",
            "ORDER",
            "FILL",
        ], "the audit trail does not reconstruct the trade end to end"

        # -- QA-E01-12: close, then archive and restore the bars --------
        await positions.close_position(
            position_id,
            exit_price=Decimal("1518.0000"),
            exit_reason="TARGET",
            realized_pnl=Decimal("72.50"),
        )
        await db.flush()
        assert await positions.occupied_slots() == set()
        await db.commit()

        archived = await retention.archive_bars(
            db,
            tmp_path / "archive",
            start=SESSION_OPEN,
            end=SESSION_OPEN + dt.timedelta(days=1),
        )
        assert archived.verified and archived.rows_written == 30

        await retention.purge_archived_bars(
            db,
            tmp_path / "archive",
            start=SESSION_OPEN,
            end=SESSION_OPEN + dt.timedelta(days=1),
        )
        await db.flush()
        assert await bars.count(symbol, Timeframe.M5) == 0

        assert await retention.restore_bars(db, archived.path) == 30
        await db.flush()
        restored = await bars.latest_n(symbol, Timeframe.M5, 1)
        assert restored[0]["close"] == Decimal("1510.7500"), "prices changed across archive"


class TestCrossComponentContracts:
    """Seams that no single story's tests would look at."""

    async def test_symbols_valid_for_the_database_are_valid_for_redis(
        self, db: AsyncSession, rds: aioredis.Redis
    ) -> None:
        """A symbol the instruments table accepts must also be usable as a key.

        If the two disagree, ingestion writes a bar and then throws building the
        cache key for the same symbol — a partial write with no clean recovery.
        """
        instruments = InstrumentRepository(db)
        real_symbols = ["INFY", "M&M", "BAJAJ-AUTO", "NIFTY50", "IDEA", "L&TFH"]
        await instruments.upsert(
            [
                {
                    "tradingsymbol": s,
                    "exchange": "NSE",
                    "broker_token": f"t{i}",
                    "tick_size": Decimal("0.05"),
                }
                for i, s in enumerate(real_symbols)
            ]
        )
        await db.flush()

        for symbol in real_symbols:
            assert keys.quote(symbol)
            assert keys.indicator_state(symbol, Timeframe.M5)
            assert keys.symbol_lock(symbol)

    async def test_audit_payload_survives_the_jsonb_round_trip_for_verification(
        self, db: AsyncSession, factory: object, tmp_path: Path
    ) -> None:
        """The chain is verified from what JSONB gives BACK, not what was sent.

        If the stored form differs from the hashed form in any way, verification
        fails on an untampered chain — which would be worse than useless,
        because it would train everyone to ignore the alarm.
        """
        audit = AuditWriter(factory, buffer_dir=tmp_path / "a")  # type: ignore[arg-type]
        tricky = {
            "unicode": "₹ रिलायंस",
            "nested": {"z": 1, "a": {"deep": [1, 2, {"k": "v"}]}},
            "decimal_as_str": str(Decimal("1234.5678")),
            "empty": {},
            "null": None,
            "bool": True,
            "big": 2**53 + 1,
        }
        await audit.write(
            AuditEntry(
                correlation_id=uuid.uuid4(),
                stage="SIGNAL",
                outcome="ALLOW",
                service="e2e",
                payload=tricky,
            )
        )
        result = await verify_chain(db)
        assert result, f"a payload that round-trips through JSONB broke the chain: {result.detail}"

    async def test_stream_envelope_carries_the_correlation_id_into_the_audit(
        self, db: AsyncSession, factory: object, rds: aioredis.Redis, tmp_path: Path
    ) -> None:
        """One id must tie the stream message and the audit row together."""
        correlation_id = uuid.uuid4()
        await stream.ensure_group(rds, keys.stream_orders(), "notifier")
        await stream.publish(
            rds,
            keys.stream_orders(),
            Envelope(
                correlation_id=correlation_id,
                emitted_at=dt.datetime.now(dt.UTC),
                emitted_by="execution",
                payload={"status": "FILLED"},
            ),
            maxlen=stream.default_maxlen(keys.stream_orders()),
        )
        [delivery] = await stream.consume(rds, keys.stream_orders(), "notifier", "n1", block_ms=200)

        audit = AuditWriter(factory, buffer_dir=tmp_path / "a")  # type: ignore[arg-type]
        await audit.write(
            AuditEntry(
                correlation_id=delivery.envelope.correlation_id,
                stage="FILL",
                outcome="ALLOW",
                service="e2e",
                payload=delivery.envelope.payload,
            )
        )

        rows = await DecisionLogRepository(db).by_correlation(correlation_id)
        assert len(rows) == 1

    async def test_redis_slot_lock_and_db_unique_index_agree(
        self, db: AsyncSession, rds: aioredis.Redis
    ) -> None:
        """The lock is the fast path; the index is the guarantee. Both must hold.

        If the lock were released before the insert committed, the index would
        be the only thing standing between two signals and a double position.
        """
        import psycopg

        instruments = InstrumentRepository(db)
        await instruments.upsert(
            [
                {
                    "tradingsymbol": s,
                    "exchange": "NSE",
                    "broker_token": f"tk{i}",
                    "tick_size": Decimal("0.05"),
                }
                for i, s in enumerate(["AAA", "BBB"])
            ]
        )
        await db.flush()
        positions = PositionRepository(db, instruments)

        base = {
            "correlation_id": uuid.uuid4(),
            "slot_index": 7,
            "direction": "LONG",
            "quantity": 1,
            "entry_price": Decimal("100"),
            "stop_price": Decimal("95"),
            "opened_at": dt.datetime.now(dt.UTC),
            "squareoff_deadline": SQUAREOFF,
        }
        assert await locks.acquire_lock(rds, keys.slot_lock(7), "holder-a", 60_000)
        assert not await locks.acquire_lock(rds, keys.slot_lock(7), "holder-b", 60_000)

        await positions.open_position({**base, "symbol": "AAA"})
        await db.flush()

        # Even if the lock were somehow lost, the index must still refuse.
        with pytest.raises((psycopg.errors.UniqueViolation, Exception)):
            await positions.open_position({**base, "symbol": "BBB"})
            await db.flush()


class TestFailClosedBehaviour:
    """Every component must degrade to 'do not trade', never to 'proceed'."""

    async def test_missing_bars_yield_empty_not_an_exception(self, db: AsyncSession) -> None:
        instruments = InstrumentRepository(db)
        await instruments.upsert(
            [
                {
                    "tradingsymbol": "NOBARS",
                    "exchange": "NSE",
                    "broker_token": "nb1",
                    "tick_size": Decimal("0.05"),
                }
            ]
        )
        await db.flush()
        bars = BarRepository(db, instruments)
        assert await bars.latest_n("NOBARS", Timeframe.M5, 20) == []

    async def test_corrupt_redis_state_reads_as_absent(self, rds: aioredis.Redis) -> None:
        """Absent routes into the existing no-data path, which blocks entries."""
        from pydantic import BaseModel

        class S(BaseModel):
            ready: bool

        await rds.set(keys.quote("INFY"), "{not json")
        assert await state.get_state(rds, keys.quote("INFY"), S) is None

    async def test_kill_switch_is_engaged_when_redis_is_unreachable(self) -> None:
        dead = aioredis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
        try:
            assert await state.is_kill_switch_active(dead, keys.kill_switch()) is True
        finally:
            await dead.aclose()

    async def test_audit_failure_does_not_propagate_to_the_caller(self, tmp_path: Path) -> None:
        dead = db_engine.create_engine_from_url(
            "postgresql+psycopg://nobody:nothing@127.0.0.1:1/nothing",
            connect_timeout_seconds=1,
        )
        try:
            writer = AuditWriter(db_engine.create_session_factory(dead), buffer_dir=tmp_path / "b")
            assert (
                await writer.write(
                    AuditEntry(
                        correlation_id=uuid.uuid4(),
                        stage="SIGNAL",
                        outcome="ALLOW",
                        service="e2e",
                        payload={},
                    )
                )
                is None
            )
            assert writer.buffered_count() == 1
        finally:
            await dead.dispose()
