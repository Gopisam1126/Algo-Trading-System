"""E03-S01 against the real schema: NSE's 20 Nov 2026 bhavcopy into ``ohlcv``.

The unit tests prove the parse. This proves the seam: the instrument master's
ids, ``BarRepository.bulk_upsert``'s idempotency and its refusal to change a
stored price (BR-15), and the bars read back through the repository the daily
correlations (E15-S14) read.

The instruments and bars this writes are committed (the store opens its own
transaction per session, as production does), so the fixture removes exactly
what it added - at both ends, because a shared table left dirty fails some
other file's count (QA-E15-28).
"""

from __future__ import annotations

import datetime as dt
import io
import os
import subprocess
import sys
import zipfile
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from algotrader.common.calendar import MarketCalendar, load_holidays_with_status
from algotrader.common.db import engine as db_engine
from algotrader.common.db.repositories import BarRepository, InstrumentRepository
from algotrader.common.enums import Timeframe
from algotrader.premarket.bhavcopy import (
    BhavcopyArchive,
    BhavcopyLoader,
    DayOutcome,
    RepositoryBarStore,
    parse_bhavcopy,
)

pytestmark = [pytest.mark.integration]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DAY = dt.date(2026, 11, 20)
TS = dt.datetime(2026, 11, 20, 3, 45, tzinfo=dt.UTC)
ZIP = (FIXTURES / "bhavcopy" / "BhavCopy_NSE_CM_0_0_0_20261120_F_0000.csv.zip").read_bytes()
MASTER = {
    line
    for line in (FIXTURES / "universe" / "nse_eq_symbols_20261122.txt")
    .read_text("utf-8")
    .splitlines()
    if line and not line.startswith("#")
}
#: Every symbol of the day's file that Kite's list knows - what the master holds.
SYMBOLS = sorted(
    {r.tradingsymbol for r in parse_bhavcopy(ZIP, day=DAY)} & MASTER
)  # 3,722 (20 of the file's 3,742 are not in Kite's list)


def _owner(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://")


def _wipe(url: str, before: set[str]) -> None:
    with psycopg.connect(_owner(url), autocommit=True) as conn:
        conn.execute(
            "DELETE FROM ohlcv WHERE timeframe = '1d' AND ts = %s AND symbol_id IN "
            "(SELECT id FROM instruments WHERE tradingsymbol = ANY(%s))",
            (TS, SYMBOLS),
        )
        added = [s for s in SYMBOLS if s not in before]
        conn.execute("DELETE FROM instruments WHERE tradingsymbol = ANY(%s)", (added,))


@pytest.fixture
def calendar() -> MarketCalendar:
    status = load_holidays_with_status(
        str(Path(__file__).resolve().parents[2] / "config" / "nse_holidays.yaml")
    )
    return MarketCalendar(status.dates, covers_years=status.covers_years)


def _seed(url: str) -> set[str]:
    """Add the day's instruments; return what was there before, for ``_wipe``."""
    with psycopg.connect(_owner(url)) as conn:
        before = {r[0] for r in conn.execute("SELECT tradingsymbol FROM instruments").fetchall()}
    _wipe(url, before)
    with psycopg.connect(_owner(url), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO instruments "
                "(tradingsymbol, exchange, broker_token, tick_size, lot_size) "
                "VALUES (%s, 'NSE', %s, 0.01, 1) "
                "ON CONFLICT (exchange, tradingsymbol) DO NOTHING",
                [(s, f"bc-{i}") for i, s in enumerate(SYMBOLS)],
            )
    return before


@pytest.fixture
def seeded(migrated_database: str) -> Iterator[str]:
    before = _seed(migrated_database)
    yield migrated_database
    _wipe(migrated_database, before)


@pytest.fixture
async def factory(seeded: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine: AsyncEngine = db_engine.create_engine_from_url(seeded)
    yield db_engine.create_session_factory(engine)
    await engine.dispose()


def _count(url: str) -> int:
    with psycopg.connect(_owner(url)) as conn:
        row = conn.execute(
            "SELECT count(*) FROM ohlcv WHERE timeframe = '1d' AND ts = %s", (TS,)
        ).fetchone()
        assert row is not None
        return int(row[0])


def _close(url: str, symbol: str) -> Decimal:
    with psycopg.connect(_owner(url)) as conn:
        row = conn.execute(
            "SELECT o.close FROM ohlcv o JOIN instruments i ON i.id = o.symbol_id "
            "WHERE i.tradingsymbol = %s AND o.timeframe = '1d' AND o.ts = %s",
            (symbol, TS),
        ).fetchone()
        assert row is not None
        return Decimal(row[0])


def _loader(calendar, archive_root: Path, factory) -> BhavcopyLoader:
    def no_network(day: dt.date) -> bytes:
        raise AssertionError(f"the network was asked for {day}")

    return BhavcopyLoader(
        calendar=calendar,
        archive=BhavcopyArchive(archive_root),
        store=RepositoryBarStore(factory),
        fetch=no_network,
    )


def _revised(close: str) -> bytes:
    """The same file with INFY's close changed - a 'revision' NSE never made."""
    with zipfile.ZipFile(io.BytesIO(ZIP)) as source:
        (member,) = source.infolist()
        text = source.read(member).decode("utf-8")
    old = "INFY,EQ,,,,,INFOSYS LIMITED,1087.00,1118.80,1081.70,1098.00,"
    assert text.count(old) == 1
    text = text.replace(old, old.replace("1098.00,", f"{close},"))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as out:
        out.writestr(member.filename, text)
    return buffer.getvalue()


async def test_the_real_day_lands_as_daily_bars(calendar, tmp_path, factory, seeded) -> None:
    BhavcopyArchive(tmp_path).write(DAY, ZIP)
    result = await _loader(calendar, tmp_path, factory).sync_day(DAY)
    assert (result.outcome, result.source, result.bars) == (DayOutcome.STORED, "archive", 3722)
    assert _count(seeded) == len(SYMBOLS) == 3722
    assert _close(seeded, "INFY") == Decimal("1098.00")
    assert _close(seeded, "HFCL-BE") == Decimal("188.00")


async def test_a_rerun_adds_no_rows_and_changes_no_price(calendar, tmp_path, factory, seeded):
    BhavcopyArchive(tmp_path).write(DAY, ZIP)
    loader = _loader(calendar, tmp_path, factory)
    await loader.sync_day(DAY)
    again = await loader.sync_day(DAY)
    assert again.outcome is DayOutcome.STORED
    assert _count(seeded) == 3722
    assert _close(seeded, "INFY") == Decimal("1098.00")


async def test_a_revised_price_does_not_overwrite_the_stored_one(
    calendar, tmp_path, factory, seeded
) -> None:
    """BR-15: raw prices are immutable. A file disagreeing with a stored bar is
    counted and logged by bulk_upsert, and the stored bar stands."""
    BhavcopyArchive(tmp_path).write(DAY, ZIP)
    await _loader(calendar, tmp_path, factory).sync_day(DAY)
    other = tmp_path / "revised"
    BhavcopyArchive(other).write(DAY, _revised("1111.00"))
    result = await _loader(calendar, other, factory).sync_day(DAY)
    assert result.outcome is DayOutcome.STORED and result.bars == 3721
    assert _close(seeded, "INFY") == Decimal("1098.00")


async def test_the_bars_read_back_through_the_repository(calendar, tmp_path, factory) -> None:
    """The seam the daily correlations and the pre-market warm-up read."""
    BhavcopyArchive(tmp_path).write(DAY, ZIP)
    await _loader(calendar, tmp_path, factory).sync_day(DAY)
    async with db_engine.read_only(factory) as session:
        instruments = InstrumentRepository(session)
        await instruments.refresh_cache()
        (bar,) = await BarRepository(session, instruments).latest_n("INFY", Timeframe.D1, 1)
    assert bar["open_ts"] == TS
    assert Decimal(bar["close"]) == Decimal("1098.00")
    assert int(bar["volume"]) == 13749744


async def test_the_master_is_read_from_the_table(factory) -> None:
    master = await RepositoryBarStore(factory).active_symbols()
    assert {"INFY", "HFCL-BE", "EMBASSY-RR"} <= master


@pytest.fixture
def deployed_seeded(deployed_database: dict[str, str]) -> Iterator[dict[str, str]]:
    before = _seed(deployed_database["owner"])
    yield deployed_database
    _wipe(deployed_database["owner"], before)


class TestTheCommand:
    """``scripts/backfill_bhavcopy.py`` end to end, as an operator runs it: a
    process of its own, under the login production runs it as - the services'
    login, created by the real first-init script - not ``SET ROLE`` on the
    owner's connection, which proves grants and not what a login gets (audit
    SEC-002).

    A process of its own because this one is not neutral: ``tests/conftest.py``
    sets the Windows event-loop policy for the whole session. Run in-process,
    the command passed here while, run on its own, it crashed at its first
    query - it had never set the policy itself (SIT-011)."""

    @staticmethod
    def _run(url: str, archive: Path) -> subprocess.CompletedProcess[str]:
        script = Path(__file__).resolve().parents[2] / "scripts" / "backfill_bhavcopy.py"
        argv = ["--start", "2026-11-20", "--end", "2026-11-20", "--archive", str(archive)]
        return subprocess.run(  # a fixed command: this interpreter, this repository's script
            [sys.executable, str(script), *argv, "--offline"],
            env={**os.environ, "DATABASE_URL": url},
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )

    def test_the_services_login_stores_the_day(self, deployed_seeded, tmp_path) -> None:
        BhavcopyArchive(tmp_path).write(DAY, ZIP)
        done = self._run(deployed_seeded["svc"], tmp_path)
        assert done.returncode == 0, done.stderr[-2000:]
        assert _count(deployed_seeded["owner"]) == 3722
        assert "1 stored, 0 missing, 0 refused; 3722 bar(s) written" in done.stdout

    def test_a_login_that_could_rewrite_the_record_is_refused(
        self, deployed_seeded, tmp_path
    ) -> None:
        """The owner runs the migrations and may UPDATE the audit trail; a
        command that writes what it reads from the internet does not run as it."""
        BhavcopyArchive(tmp_path).write(DAY, ZIP)
        done = self._run(deployed_seeded["owner"], tmp_path)
        assert done.returncode == 2, done.stderr[-2000:]
        assert _count(deployed_seeded["owner"]) == 0
        assert "refused:" in done.stderr and "algotrader_svc" in done.stderr
        assert "test_only_not_a_real_secret" not in done.stderr  # it names no secret
