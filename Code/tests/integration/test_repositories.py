"""E01-S02 — repository layer against a real database.

The two acceptance criteria:

1. ``grep -r "import sqlalchemy" src/algotrader/{ingest,signals,execution,premarket}``
   returns nothing — the ORM does not leak past this layer.
2. Bulk insert of 100k bars completes in under 10 s, **measured in the test**
   rather than asserted in a comment.

Plus the one that matters most operationally: ``warm_up_batch`` must issue a
constant number of queries regardless of how many symbols it is given. A
per-symbol loop passes every correctness test and then misses the pre-market
deadline in production, so the query count is asserted directly.
"""

from __future__ import annotations

import datetime as dt
import subprocess
import time
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from algotrader.common.db import engine as db_engine
from algotrader.common.db.repositories import (
    BarRepository,
    DailyStatusRepository,
    DecisionLogRepository,
    InstrumentRepository,
    OrderRepository,
    PositionRepository,
    UnknownSymbolError,
)
from algotrader.common.enums import Timeframe

pytestmark = [pytest.mark.integration]

CODE_ROOT = Path(__file__).resolve().parents[2]
BASE_TS = dt.datetime(2026, 8, 6, 3, 45, tzinfo=dt.UTC)


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
                "tradingsymbol": sym,
                "exchange": "NSE",
                "broker_token": f"tok{i}",
                "tick_size": Decimal("0.05"),
                "lot_size": 1,
            }
            for i, sym in enumerate(["INFY", "TCS", "RELIANCE", "HDFCBANK"])
        ]
    )
    await session.flush()
    await repo.refresh_cache()
    return repo


def _bar(sid: int, minutes: int, close: str = "100") -> dict[str, object]:
    return {
        "symbol_id": sid,
        "timeframe": "5m",
        "ts": BASE_TS + dt.timedelta(minutes=minutes),
        "open": Decimal("100"),
        "high": Decimal("110"),
        "low": Decimal("95"),
        "close": Decimal(close),
        "volume": 1000,
    }


class TestOrmDoesNotLeak:
    def test_services_do_not_import_sqlalchemy(self) -> None:
        """The scope boundary, asserted rather than trusted.

        If the ORM leaks into a service, that service can no longer be unit
        tested against a fake, and detached-instance lazy loads start raising
        MissingGreenlet inside business logic.
        """
        targets = [
            CODE_ROOT / "src" / "algotrader" / name
            for name in ("ingest", "signals", "execution", "premarket")
        ]
        existing = [str(p) for p in targets if p.exists()]
        if not existing:
            pytest.skip("service packages not implemented yet (Phase 1+)")

        result = subprocess.run(
            ["grep", "-rn", "import sqlalchemy", *existing],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0, f"ORM leaked into a service:\n{result.stdout}"

    async def test_repositories_return_plain_dicts(
        self, session: AsyncSession, instruments: InstrumentRepository
    ) -> None:
        """Not ORM instances — those carry session affinity and lazy loaders."""
        bars = BarRepository(session, instruments)
        sid = await instruments.symbol_id("INFY")
        await bars.bulk_upsert([_bar(sid, 0)])
        await session.flush()

        [row] = await bars.latest_n("INFY", Timeframe.M5, 1)
        assert isinstance(row, dict)
        assert row["symbol"] == "INFY"


class TestSymbolTranslation:
    """The impedance mismatch this layer exists to own (spec §2.3 E)."""

    async def test_round_trip(self, instruments: InstrumentRepository) -> None:
        sid = await instruments.symbol_id("INFY")
        assert await instruments.symbol_name(sid) == "INFY"

    async def test_unknown_symbol_raises_rather_than_returning_none(
        self, instruments: InstrumentRepository
    ) -> None:
        """Silently skipping an unknown symbol loses market data with no error."""
        with pytest.raises(UnknownSymbolError):
            await instruments.symbol_id("NOTLISTED")

    async def test_cache_avoids_repeat_queries(
        self, session: AsyncSession, engine: object, instruments: InstrumentRepository
    ) -> None:
        """A query per lookup would issue one per bar during ingestion."""
        counter = _QueryCounter(engine)
        with counter:
            for _ in range(50):
                await instruments.symbol_id("INFY")
        assert counter.count == 0, f"cache missed — {counter.count} queries for 50 lookups"

    async def test_symbol_added_mid_session_is_picked_up(
        self, session: AsyncSession, instruments: InstrumentRepository
    ) -> None:
        """A new listing must not require a restart."""
        await instruments.upsert(
            [
                {
                    "tradingsymbol": "NEWLIST",
                    "exchange": "NSE",
                    "broker_token": "tok999",
                    "tick_size": Decimal("0.05"),
                }
            ]
        )
        await session.flush()
        assert await instruments.symbol_id("NEWLIST") > 0


class TestBarUpsert:
    async def test_duplicate_bars_do_not_fail_the_batch(
        self, session: AsyncSession, instruments: InstrumentRepository
    ) -> None:
        """Ingestion is at-least-once — a reconnect replays recent bars."""
        bars = BarRepository(session, instruments)
        sid = await instruments.symbol_id("INFY")
        await bars.bulk_upsert([_bar(sid, 0)])
        await bars.bulk_upsert([_bar(sid, 0)])
        await session.flush()
        assert await bars.count("INFY", Timeframe.M5) == 1

    async def test_a_revised_bar_overwrites_rather_than_being_ignored(
        self, session: AsyncSession, instruments: InstrumentRepository
    ) -> None:
        """A late trade report changes volume and vwap; DO NOTHING would pin stale data."""
        bars = BarRepository(session, instruments)
        sid = await instruments.symbol_id("INFY")
        await bars.bulk_upsert([_bar(sid, 0, close="100")])
        await bars.bulk_upsert([_bar(sid, 0, close="107")])
        await session.flush()
        [row] = await bars.latest_n("INFY", Timeframe.M5, 1)
        assert row["close"] == Decimal("107")

    async def test_bars_come_back_oldest_first(
        self, session: AsyncSession, instruments: InstrumentRepository
    ) -> None:
        """Indicators consume chronologically; sorting at call sites gets forgotten."""
        bars = BarRepository(session, instruments)
        sid = await instruments.symbol_id("INFY")
        await bars.bulk_upsert([_bar(sid, m) for m in (0, 5, 10, 15)])
        await session.flush()
        rows = await bars.latest_n("INFY", Timeframe.M5, 3)
        assert [r["open_ts"] for r in rows] == sorted(r["open_ts"] for r in rows)

    async def test_prices_come_back_as_decimal(
        self, session: AsyncSession, instruments: InstrumentRepository
    ) -> None:
        """Never float — error accumulates in P&L and breaks tick-size equality.

        The close must stay inside [low, high]: the ohlc_coherent CHECK rejects
        an incoherent bar, which an earlier version of this test discovered by
        picking a decimal outside the range. That rejection is the schema
        working, so the fixture uses four decimal places WITHIN the band.
        """
        bars = BarRepository(session, instruments)
        sid = await instruments.symbol_id("INFY")
        await bars.bulk_upsert([_bar(sid, 0, close="103.4567")])
        await session.flush()
        [row] = await bars.latest_n("INFY", Timeframe.M5, 1)
        assert isinstance(row["close"], Decimal)
        assert row["close"] == Decimal("103.4567")


class TestWarmUpBatchIsOneQuery:
    """BP-2's workhorse. The performance property, not just correctness."""

    async def test_returns_correct_bars_per_symbol(
        self, session: AsyncSession, instruments: InstrumentRepository
    ) -> None:
        bars = BarRepository(session, instruments)
        symbols = ["INFY", "TCS", "RELIANCE"]
        for sym in symbols:
            sid = await instruments.symbol_id(sym)
            await bars.bulk_upsert([_bar(sid, m) for m in range(0, 50, 5)])
        await session.flush()

        out = await bars.warm_up_batch(symbols, Timeframe.M5, bars_each=4)
        assert set(out) == set(symbols)
        assert all(len(v) == 4 for v in out.values())

    async def test_query_count_is_constant_regardless_of_symbol_count(
        self, session: AsyncSession, engine: object, instruments: InstrumentRepository
    ) -> None:
        """THE test for this method.

        A per-symbol loop is correct and passes every other test in this class,
        then misses the 45-minute pre-market deadline in production because it
        issues 450 round trips. Asserting the query count is what makes that
        refactor fail here instead of at 08:45 on a trading morning.
        """
        bars = BarRepository(session, instruments)
        symbols = ["INFY", "TCS", "RELIANCE", "HDFCBANK"]
        for sym in symbols:
            sid = await instruments.symbol_id(sym)
            await bars.bulk_upsert([_bar(sid, m) for m in range(0, 20, 5)])
        await session.flush()

        one = _QueryCounter(engine)
        with one:
            await bars.warm_up_batch(symbols[:1], Timeframe.M5, bars_each=3)
        many = _QueryCounter(engine)
        with many:
            await bars.warm_up_batch(symbols, Timeframe.M5, bars_each=3)

        assert many.count == one.count, (
            f"{one.count} queries for 1 symbol but {many.count} for {len(symbols)} — "
            f"this is a per-symbol loop and it will miss the BP-2 deadline"
        )

    async def test_empty_input_is_not_a_query(
        self, session: AsyncSession, instruments: InstrumentRepository
    ) -> None:
        bars = BarRepository(session, instruments)
        assert await bars.warm_up_batch([], Timeframe.M5, bars_each=5) == {}


class TestCopyPath:
    """Batches above the threshold take a different code path — prove it behaves."""

    async def test_copy_path_produces_the_same_result_as_the_insert_path(
        self, session: AsyncSession, instruments: InstrumentRepository
    ) -> None:
        bars = BarRepository(session, instruments)
        sid = await instruments.symbol_id("INFY")
        rows = [_bar(sid, m) for m in range(6_000)]  # above _COPY_THRESHOLD
        await bars.bulk_upsert(rows)
        await session.flush()
        assert await bars.count("INFY", Timeframe.M5) == 6_000

    async def test_copy_path_tolerates_duplicates_within_one_batch(
        self, session: AsyncSession, instruments: InstrumentRepository
    ) -> None:
        """ON CONFLICT raises "cannot affect row a second time" if the SOURCE
        holds two rows with the same key — which a batch spanning a reconnect
        legitimately can. The DISTINCT ON in the COPY path is what prevents it,
        and the last occurrence must win, matching the INSERT path.
        """
        bars = BarRepository(session, instruments)
        sid = await instruments.symbol_id("INFY")
        rows = [_bar(sid, m) for m in range(6_000)]
        rows.append(_bar(sid, 0, close="109"))  # duplicate key, later value

        await bars.bulk_upsert(rows)
        await session.flush()

        assert await bars.count("INFY", Timeframe.M5) == 6_000
        first = await bars.range("INFY", Timeframe.M5, BASE_TS, BASE_TS + dt.timedelta(minutes=1))
        assert first[0]["close"] == Decimal("109"), "last occurrence did not win"

    async def test_copy_path_is_idempotent_across_calls(
        self, session: AsyncSession, instruments: InstrumentRepository
    ) -> None:
        """Repeated calls in ONE transaction reuse the temp table — the bug that
        made the second chunk of a backfill fail with 'relation already exists'."""
        bars = BarRepository(session, instruments)
        sid = await instruments.symbol_id("INFY")
        rows = [_bar(sid, m) for m in range(6_000)]
        await bars.bulk_upsert(rows)
        await bars.bulk_upsert(rows)
        await session.flush()
        assert await bars.count("INFY", Timeframe.M5) == 6_000


class TestBulkInsertPerformance:
    @pytest.mark.slow
    async def test_100k_bars_in_under_10_seconds(
        self, session: AsyncSession, instruments: InstrumentRepository
    ) -> None:
        """Measured, not asserted in a comment. This is the backfill path."""
        bars = BarRepository(session, instruments)
        sid = await instruments.symbol_id("INFY")
        rows = [_bar(sid, m) for m in range(100_000)]

        started = time.perf_counter()
        for chunk in range(0, len(rows), 10_000):
            await bars.bulk_upsert(rows[chunk : chunk + 10_000])
        await session.flush()
        elapsed = time.perf_counter() - started

        assert elapsed < 10.0, f"bulk insert of 100k bars took {elapsed:.1f}s"


class TestOrderRecoveryPath:
    """The two-transaction submission rule and what makes recovery possible."""

    async def test_submitting_row_exists_before_the_broker_is_called(
        self, session: AsyncSession, instruments: InstrumentRepository
    ) -> None:
        """The intermediate state IS the recovery mechanism."""
        orders = OrderRepository(session, instruments)
        cid = f"cid-{uuid.uuid4().hex[:12]}"
        await orders.insert_submitting(
            {
                "client_order_id": cid,
                "correlation_id": uuid.uuid4(),
                "symbol": "INFY",
                "side": "BUY",
                "order_type": "LIMIT",
                "product": "MIS",
                "quantity": 10,
                "intent": "ENTRY",
            }
        )
        await session.flush()
        found = await orders.find_by_client_order_id(cid)
        assert found is not None
        assert found["status"] == "SUBMITTING"
        assert found["broker_order_id"] is None

    async def test_lookup_by_client_order_id_is_what_prevents_double_submission(
        self, session: AsyncSession, instruments: InstrumentRepository
    ) -> None:
        """After an ambiguous timeout: query, adopt the answer, do not resubmit."""
        orders = OrderRepository(session, instruments)
        cid = f"cid-{uuid.uuid4().hex[:12]}"
        await orders.insert_submitting(
            {
                "client_order_id": cid,
                "correlation_id": uuid.uuid4(),
                "symbol": "INFY",
                "side": "BUY",
                "order_type": "MARKET",
                "product": "MIS",
                "quantity": 5,
                "intent": "ENTRY",
            }
        )
        await session.flush()
        await orders.attach_broker_id(cid, "BROKER-123")
        await session.flush()

        found = await orders.find_by_client_order_id(cid)
        assert found is not None
        assert found["broker_order_id"] == "BROKER-123"
        assert found["status"] == "SUBMITTED"
        assert found["symbol"] == "INFY", "symbol_id was not translated back"

    async def test_unknown_client_order_id_returns_none(
        self, session: AsyncSession, instruments: InstrumentRepository
    ) -> None:
        orders = OrderRepository(session, instruments)
        assert await orders.find_by_client_order_id("never-existed") is None

    async def test_open_orders_excludes_terminal_states(
        self, session: AsyncSession, instruments: InstrumentRepository
    ) -> None:
        orders = OrderRepository(session, instruments)
        for status in ("OPEN", "FILLED", "CANCELLED", "REJECTED", "SUBMITTED"):
            await orders.insert_submitting(
                {
                    "client_order_id": f"cid-{uuid.uuid4().hex[:12]}",
                    "correlation_id": uuid.uuid4(),
                    "symbol": "INFY",
                    "side": "BUY",
                    "order_type": "LIMIT",
                    "product": "MIS",
                    "quantity": 1,
                    "intent": "ENTRY",
                    "status": status,
                }
            )
        await session.flush()
        statuses = {o["status"] for o in await orders.open_orders()}
        assert statuses == {"OPEN", "SUBMITTED"}


class TestPositionSlots:
    async def test_slot_collision_raises_and_is_catchable(
        self, session: AsyncSession, instruments: InstrumentRepository
    ) -> None:
        """A normal outcome under concurrency, not an error to surface."""
        import psycopg

        positions = PositionRepository(session, instruments)
        base = {
            "correlation_id": uuid.uuid4(),
            "slot_index": 2,
            "direction": "LONG",
            "quantity": 10,
            "entry_price": Decimal("100"),
            "stop_price": Decimal("95"),
            "opened_at": dt.datetime.now(dt.UTC),
            "squareoff_deadline": dt.datetime.now(dt.UTC) + dt.timedelta(hours=5),
        }
        await positions.open_position({**base, "symbol": "INFY"})
        await session.flush()

        with pytest.raises((psycopg.errors.UniqueViolation, Exception)):
            await positions.open_position({**base, "symbol": "TCS"})
            await session.flush()

    async def test_occupied_slots_reports_only_open_positions(
        self, session: AsyncSession, instruments: InstrumentRepository
    ) -> None:
        positions = PositionRepository(session, instruments)
        pid = await positions.open_position(
            {
                "correlation_id": uuid.uuid4(),
                "symbol": "INFY",
                "slot_index": 4,
                "direction": "LONG",
                "quantity": 10,
                "entry_price": Decimal("100"),
                "stop_price": Decimal("95"),
                "opened_at": dt.datetime.now(dt.UTC),
                "squareoff_deadline": dt.datetime.now(dt.UTC) + dt.timedelta(hours=5),
            }
        )
        await session.flush()
        assert await positions.occupied_slots() == {4}

        await positions.close_position(
            pid, exit_price=Decimal("102"), exit_reason="TARGET", realized_pnl=Decimal("20")
        )
        await session.flush()
        assert await positions.occupied_slots() == set()


class TestDailyStatus:
    async def test_upsert_is_idempotent_per_symbol_per_day(
        self, session: AsyncSession, instruments: InstrumentRepository
    ) -> None:
        repo = DailyStatusRepository(session, instruments)
        sid = await instruments.symbol_id("INFY")
        row = {"symbol_id": sid, "trade_date": dt.date(2026, 8, 6), "is_t2t": False}
        await repo.upsert([row])
        await repo.upsert([{**row, "is_t2t": True}])
        await session.flush()

        got = await repo.for_symbol("INFY", dt.date(2026, 8, 6))
        assert got is not None and got["is_t2t"] is True


class TestDecisionLogReads:
    async def test_max_seq_starts_at_zero(self, session: AsyncSession) -> None:
        """The audit writer continues from here; an empty chain must be 0, not None."""
        assert await DecisionLogRepository(session).max_seq() == 0


class _QueryCounter:
    """Counts SQL statements issued on a session, for the N+1 assertions."""

    def __init__(self, engine: object) -> None:
        self._sync_engine = engine.sync_engine  # type: ignore[attr-defined]
        self.count = 0

    def _on_execute(self, *_args: object, **_kw: object) -> None:
        self.count += 1

    def __enter__(self) -> _QueryCounter:
        self.count = 0
        event.listen(self._sync_engine, "before_cursor_execute", self._on_execute)
        return self

    def __exit__(self, *_exc: object) -> None:
        event.remove(self._sync_engine, "before_cursor_execute", self._on_execute)
