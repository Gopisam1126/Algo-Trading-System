"""E01-S06 — archive, verify, restore, purge.

The tests that matter here all guard the same thing: **market data cannot be
re-derived.** A gap is permanent and silently corrupts every future backtest
over that window, so every path that deletes is tested for its refusals, not
just its happy case.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from algotrader.common.db import engine as db_engine
from algotrader.common.db import retention
from algotrader.common.db.repositories import BarRepository, InstrumentRepository

pytestmark = [pytest.mark.integration]

BASE = dt.datetime(2026, 8, 6, 3, 45, tzinfo=dt.UTC)
LATER = BASE + dt.timedelta(days=30)


@pytest.fixture
async def session(migrated_database: str) -> AsyncIterator[AsyncSession]:
    eng = db_engine.create_engine_from_url(migrated_database)
    factory = db_engine.create_session_factory(eng)
    async with factory() as s:
        await s.execute(text("DELETE FROM ohlcv"))
        await s.commit()
        yield s
        await s.rollback()
    await eng.dispose()


@pytest.fixture
async def seeded(session: AsyncSession) -> int:
    """20 bars for one instrument, committed."""
    repo = InstrumentRepository(session)
    await repo.upsert(
        [
            {
                "tradingsymbol": "ARCHIVE1",
                "exchange": "NSE",
                "broker_token": "arch1",
                "tick_size": Decimal("0.05"),
            }
        ]
    )
    await session.flush()
    sid = await repo.symbol_id("ARCHIVE1")

    bars = BarRepository(session, repo)
    await bars.bulk_upsert(
        [
            {
                "symbol_id": sid,
                "timeframe": "5m",
                "ts": BASE + dt.timedelta(minutes=5 * i),
                "open": Decimal("100.1234"),
                "high": Decimal("110.5678"),
                "low": Decimal("95.4321"),
                "close": Decimal("103.8765"),
                "volume": 1000 + i,
            }
            for i in range(20)
        ]
    )
    await session.commit()
    return sid


class TestArchive:
    async def test_archive_writes_and_verifies(
        self, session: AsyncSession, seeded: int, tmp_path: Path
    ) -> None:
        result = await retention.archive_bars(session, tmp_path, start=BASE, end=LATER)
        assert result.verified
        assert result.rows_written == 20
        assert result.path.exists()

    async def test_decimals_survive_the_round_trip_exactly(
        self, session: AsyncSession, seeded: int, tmp_path: Path
    ) -> None:
        """A float round-trip would silently corrupt every archived price.

        This is the single most important property of the archive: it exists to
        preserve data faithfully, and a price that comes back as 103.87650000001
        is a corrupted archive that reports success.
        """
        result = await retention.archive_bars(session, tmp_path, start=BASE, end=LATER)
        rows = retention.read_archive(result.path)
        assert Decimal(rows[0]["close"]) == Decimal("103.8765")
        assert Decimal(rows[0]["open"]) == Decimal("100.1234")

    async def test_empty_range_is_refused(
        self, session: AsyncSession, seeded: int, tmp_path: Path
    ) -> None:
        """An empty archive would later look like a verified archive of nothing
        and could authorise a purge of data it never held."""
        far = BASE + dt.timedelta(days=365)
        with pytest.raises(retention.RetentionError, match="no bars"):
            await retention.archive_bars(
                session, tmp_path, start=far, end=far + dt.timedelta(days=1)
            )

    async def test_archive_path_is_deterministic(self, tmp_path: Path) -> None:
        """A re-run must overwrite, not accumulate files nobody can tell apart."""
        assert retention.archive_path(tmp_path, BASE, LATER) == retention.archive_path(
            tmp_path, BASE, LATER
        )


class TestRestore:
    async def test_restore_brings_everything_back(
        self, session: AsyncSession, seeded: int, tmp_path: Path
    ) -> None:
        result = await retention.archive_bars(session, tmp_path, start=BASE, end=LATER)
        await session.execute(text("DELETE FROM ohlcv"))
        await session.flush()

        assert await retention.restore_bars(session, result.path) == 20
        await session.flush()
        count = (await session.execute(text("SELECT count(*) FROM ohlcv"))).scalar_one()
        assert count == 20

    async def test_restore_preserves_prices_exactly(
        self, session: AsyncSession, seeded: int, tmp_path: Path
    ) -> None:
        result = await retention.archive_bars(session, tmp_path, start=BASE, end=LATER)
        await session.execute(text("DELETE FROM ohlcv"))
        await session.flush()
        await retention.restore_bars(session, result.path)
        await session.flush()

        close = (
            await session.execute(text("SELECT close FROM ohlcv ORDER BY ts LIMIT 1"))
        ).scalar_one()
        assert close == Decimal("103.8765")

    async def test_restore_never_overwrites_live_data(
        self, session: AsyncSession, seeded: int, tmp_path: Path
    ) -> None:
        """ON CONFLICT DO NOTHING — the live copy is authoritative.

        The archive is older by definition, so a restore that overwrote would
        roll back corrections that arrived after the archive was taken.
        """
        result = await retention.archive_bars(session, tmp_path, start=BASE, end=LATER)
        await session.execute(
            text("UPDATE ohlcv SET close = 109.9999 WHERE ts = :ts"), {"ts": BASE}
        )
        await session.flush()

        await retention.restore_bars(session, result.path)
        await session.flush()

        close = (
            await session.execute(text("SELECT close FROM ohlcv WHERE ts = :ts"), {"ts": BASE})
        ).scalar_one()
        assert close == Decimal("109.9999"), "restore clobbered a live correction"

    async def test_restore_is_idempotent(
        self, session: AsyncSession, seeded: int, tmp_path: Path
    ) -> None:
        result = await retention.archive_bars(session, tmp_path, start=BASE, end=LATER)
        await retention.restore_bars(session, result.path)
        await retention.restore_bars(session, result.path)
        await session.flush()
        count = (await session.execute(text("SELECT count(*) FROM ohlcv"))).scalar_one()
        assert count == 20

    async def test_restore_heals_adjustments_it_missed_while_archived(
        self, session: AsyncSession, seeded: int, tmp_path: Path
    ) -> None:
        """A split announced while the bars were archived must still reach them.

        Archived bars are not in ``ohlcv``, so the recompute that followed the
        split never saw them and their stored factors are stale. Restoring them
        unchanged would splice raw prices onto an adjusted series — no error, no
        failing query, just a fabricated 80% gap in the history.
        """
        from algotrader.common.db.corporate_actions import CorporateActionRepository

        await session.execute(text("DELETE FROM corporate_action"))
        result = await retention.archive_bars(session, tmp_path, start=BASE, end=LATER)
        await retention.purge_archived_bars(session, tmp_path, start=BASE, end=LATER)
        await session.commit()

        actions = CorporateActionRepository(session)
        await actions.upsert(
            [
                {
                    "symbol_id": seeded,
                    "action_type": "SPLIT",
                    "ex_date": (BASE + dt.timedelta(days=10)).date(),
                    "ratio_from": Decimal(1),
                    "ratio_to": Decimal(5),
                    "source": "test",
                }
            ]
        )
        await session.flush()

        await retention.restore_bars(session, result.path)
        await session.flush()

        factors = (
            await session.execute(
                text(
                    "SELECT DISTINCT price_adj_factor FROM ohlcv "
                    "WHERE symbol_id = :s AND ts < CAST(:d AS date)"
                ),
                {"s": seeded, "d": (BASE + dt.timedelta(days=10)).date()},
            )
        ).all()
        assert {Decimal(f[0]) for f in factors} == {Decimal("0.2")}, (
            "restored bars kept their pre-archive factors and missed the split"
        )


class TestPurgeRefusesWithoutAVerifiedArchive:
    """The only destructive path. Every refusal is tested."""

    async def test_purge_without_an_archive_is_refused(
        self, session: AsyncSession, seeded: int, tmp_path: Path
    ) -> None:
        with pytest.raises(retention.RetentionError, match="no archive"):
            await retention.purge_archived_bars(session, tmp_path, start=BASE, end=LATER)

        count = (await session.execute(text("SELECT count(*) FROM ohlcv"))).scalar_one()
        assert count == 20, "data was deleted despite the refusal"

    async def test_purge_is_refused_if_the_archive_is_short(
        self, session: AsyncSession, seeded: int, tmp_path: Path
    ) -> None:
        """Archive, then add more bars. The archive no longer covers the range."""
        await retention.archive_bars(session, tmp_path, start=BASE, end=LATER)

        repo = InstrumentRepository(session)
        await repo.refresh_cache()
        bars = BarRepository(session, repo)
        await bars.bulk_upsert(
            [
                {
                    "symbol_id": seeded,
                    "timeframe": "5m",
                    "ts": BASE + dt.timedelta(minutes=5 * i),
                    "open": Decimal("100"),
                    "high": Decimal("110"),
                    "low": Decimal("95"),
                    "close": Decimal("103"),
                    "volume": 1,
                }
                for i in range(20, 30)
            ]
        )
        await session.flush()

        with pytest.raises(retention.RetentionError, match="Re-archive"):
            await retention.purge_archived_bars(session, tmp_path, start=BASE, end=LATER)

        count = (await session.execute(text("SELECT count(*) FROM ohlcv"))).scalar_one()
        assert count == 30, "data was deleted despite the refusal"

    async def test_purge_proceeds_when_the_archive_is_complete(
        self, session: AsyncSession, seeded: int, tmp_path: Path
    ) -> None:
        """The control — the refusals above must be targeted, not blanket."""
        await retention.archive_bars(session, tmp_path, start=BASE, end=LATER)
        assert await retention.purge_archived_bars(session, tmp_path, start=BASE, end=LATER) == 20
        await session.flush()
        count = (await session.execute(text("SELECT count(*) FROM ohlcv"))).scalar_one()
        assert count == 0

    async def test_archived_data_is_recoverable_after_a_purge(
        self, session: AsyncSession, seeded: int, tmp_path: Path
    ) -> None:
        """The full cycle. An archive nobody has restored from is a hypothesis."""
        result = await retention.archive_bars(session, tmp_path, start=BASE, end=LATER)
        await retention.purge_archived_bars(session, tmp_path, start=BASE, end=LATER)
        await session.flush()

        assert await retention.restore_bars(session, result.path) == 20
        await session.flush()
        close = (
            await session.execute(text("SELECT close FROM ohlcv ORDER BY ts LIMIT 1"))
        ).scalar_one()
        assert close == Decimal("103.8765")


class TestCompressionPolicyHealth:
    async def test_policy_is_scheduled_for_ohlcv(self, session: AsyncSession) -> None:
        """A policy that silently stops converting produces NO error.

        The first symptom is the disk filling weeks later, by which time the
        cause is far away — hence a nightly check.
        """
        status = await retention.compression_status(session, "ohlcv")
        assert status.policy_scheduled, "the compression policy is missing"
        assert status.healthy

    async def test_policy_is_scheduled_for_decision_log(self, session: AsyncSession) -> None:
        status = await retention.compression_status(session, "decision_log")
        assert status.policy_scheduled
        assert status.healthy

    async def test_a_table_with_no_policy_reports_unhealthy(self, session: AsyncSession) -> None:
        """The control — the checks above must be able to fail."""
        status = await retention.compression_status(session, "does_not_exist")
        assert not status.policy_scheduled
        assert not status.healthy

    async def test_zero_compressed_chunks_is_not_an_error_on_a_young_database(
        self, session: AsyncSession
    ) -> None:
        """Nothing is 90 days old yet. That is expected, not a failure."""
        status = await retention.compression_status(session, "ohlcv")
        assert status.compressed_chunks == 0
        assert status.healthy
