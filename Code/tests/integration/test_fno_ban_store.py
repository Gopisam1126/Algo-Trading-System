"""E04-S03 against the real schema: NSE's F&O ban list into
``instrument_daily_status``, beside the surveillance roster, read back by the
reader execution consults before every entry.

The unit tests prove the parse and the sync's decisions. This proves the seams:
the ban writing ``is_fno_ban`` - and only it - for every active instrument; the
roster and the ban each keeping the other's columns whichever writes first;
``restrictions_from`` blocking the banned stock and, for the first time,
clearing a stock every source reported clean; a refused list leaving the flag
unknown; and the command, run as an operator runs it, under the services' login.

The instruments and rows this writes are committed (the stores open their own
transactions, as production does), so the fixture removes what it added, at
both ends (QA-E15-28). The ban writes a row for EVERY active instrument, so its
days are cleared for every instrument, not only the ones seeded here.
"""

from __future__ import annotations

import datetime as dt
import gzip
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from algotrader.common.calendar import MarketCalendar, load_holidays_with_status
from algotrader.common.db import engine as db_engine
from algotrader.common.db.repositories import DailyStatusRepository, InstrumentRepository
from algotrader.execution.session_context import restrictions_from
from algotrader.premarket.fno_ban import (
    BanListMissingError,
    FnoBanArchive,
    FnoBanSync,
    RepositoryBanStore,
)
from algotrader.premarket.surveillance import (
    Fetched,
    RepositoryStatusStore,
    RosterArchive,
    RosterMissingError,
    SurveillanceSync,
    SyncOutcome,
    parse_roster,
)

pytestmark = [pytest.mark.integration]

CODE = Path(__file__).resolve().parents[2]
FIXTURES = CODE / "tests" / "fixtures"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
JAN30, NOV23, NOV24, NOV25 = (
    dt.date(2026, 1, 30),
    dt.date(2026, 11, 23),
    dt.date(2026, 11, 24),
    dt.date(2026, 11, 25),
)
DAYS = [JAN30, NOV25]
AFTER = dt.datetime(2026, 12, 1, tzinfo=dt.UTC)


def _ban(day: dt.date) -> bytes:
    return gzip.decompress((FIXTURES / "fno_ban" / f"fo_secban_{day:%d%m%Y}.csv.gz").read_bytes())


#: NSE's lists, and when it wrote them (Last-Modified, read 27 Nov 2026).
BANS = {
    NOV25: Fetched(_ban(NOV25), dt.datetime(2026, 11, 23, 18, 38, 4, tzinfo=IST)),
    JAN30: Fetched(_ban(JAN30), dt.datetime(2026, 1, 29, 19, 1, 4, tzinfo=IST)),
}
ROSTER_23 = gzip.decompress((FIXTURES / "surveillance" / "REG1_IND231126.csv.gz").read_bytes())
MASTER = {
    line
    for line in (FIXTURES / "universe" / "nse_eq_symbols_20261122.txt")
    .read_text("utf-8")
    .splitlines()
    if line and not line.startswith("#")
}
#: The 23 Nov roster's securities that Kite's list knows - the master seeded.
SYMBOLS = sorted({r.tradingsymbol for r in parse_roster(ROSTER_23, session=NOV23)} & MASTER)
#: In ban on 25 Nov 2026, and clean on the roster that governs that day.
BANNED_25 = "MANAPPURAM"


def _owner(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://")


def _wipe(url: str, before: set[str]) -> None:
    with psycopg.connect(_owner(url), autocommit=True) as conn:
        conn.execute("DELETE FROM instrument_daily_status WHERE trade_date = ANY(%s)", (DAYS,))
        added = [s for s in SYMBOLS if s not in before]
        conn.execute("DELETE FROM instruments WHERE tradingsymbol = ANY(%s)", (added,))


def _seed(url: str) -> set[str]:
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
                [(s, f"fb-{i}") for i, s in enumerate(SYMBOLS)],
            )
    return before


def _active(url: str) -> int:
    with psycopg.connect(_owner(url)) as conn:
        row = conn.execute("SELECT count(*) FROM instruments WHERE is_active").fetchone()
        assert row is not None
        return int(row[0])


def _ban_column(url: str, day: dt.date) -> tuple[int, int, int]:
    """(rows, banned, unknown) for ``day``'s ``is_fno_ban``."""
    with psycopg.connect(_owner(url)) as conn:
        row = conn.execute(
            "SELECT count(*), count(*) FILTER (WHERE is_fno_ban), "
            "count(*) FILTER (WHERE is_fno_ban IS NULL) "
            "FROM instrument_daily_status WHERE trade_date = %s",
            (day,),
        ).fetchone()
        assert row is not None
        return int(row[0]), int(row[1]), int(row[2])


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


@pytest.fixture
def calendar() -> MarketCalendar:
    status = load_holidays_with_status(str(CODE / "config" / "nse_holidays.yaml"))
    return MarketCalendar(status.dates, covers_years=status.covers_years)


def _serve(by_day: dict[dt.date, Fetched]) -> Any:
    def fetch(day: dt.date) -> Fetched:
        if day not in by_day:
            raise BanListMissingError(f"NSE has no F&O ban list for {day} (HTTP 404)")
        return by_day[day]

    return fetch


def _ban_sync(
    calendar: MarketCalendar,
    factory: async_sessionmaker[AsyncSession],
    archive: Path,
    served: dict[dt.date, Fetched] = BANS,
) -> FnoBanSync:
    return FnoBanSync(
        calendar=calendar,
        archive=FnoBanArchive(archive),
        store=RepositoryBanStore(factory),
        fetch=_serve(served),
    )


def _roster_sync(
    calendar: MarketCalendar, factory: async_sessionmaker[AsyncSession], archive: Path
) -> SurveillanceSync:
    def fetch(session: dt.date) -> Fetched:
        if session != NOV23:
            raise RosterMissingError(f"NSE has no surveillance roster for {session} (HTTP 404)")
        return Fetched(ROSTER_23, dt.datetime(2026, 11, 23, 14, 40, 33, tzinfo=dt.UTC))

    return SurveillanceSync(
        calendar=calendar,
        archive=RosterArchive(archive),
        store=RepositoryStatusStore(factory),
        fetch=fetch,
        now=lambda: AFTER,
    )


async def _status(
    factory: async_sessionmaker[AsyncSession], symbol: str, day: dt.date
) -> dict[str, Any] | None:
    async with factory() as session:
        instruments = InstrumentRepository(session)
        await instruments.refresh_cache()
        return await DailyStatusRepository(session, instruments).for_symbol(symbol, day)


class TestTheBanList:
    async def test_every_active_instrument_is_written_and_only_the_banned_true(
        self, seeded: str, factory, calendar, tmp_path
    ) -> None:
        result = await _ban_sync(calendar, factory, tmp_path).sync_day(NOV25)
        assert result.outcome is SyncOutcome.STORED and result.banned == (BANNED_25,)
        active = _active(seeded)
        assert result.written == active
        assert _ban_column(seeded, NOV25) == (active, 1, 0)
        status = await _status(factory, BANNED_25, NOV25)
        assert status is not None and status["is_fno_ban"] is True

    async def test_a_nil_day_writes_every_instrument_clean(
        self, seeded: str, factory, calendar, tmp_path
    ) -> None:
        result = await _ban_sync(calendar, factory, tmp_path).sync_day(JAN30)
        assert result.outcome is SyncOutcome.STORED and result.banned == ()
        assert _ban_column(seeded, JAN30) == (_active(seeded), 0, 0)

    async def test_a_rerun_writes_the_same_values(
        self, seeded: str, factory, calendar, tmp_path
    ) -> None:
        sync = _ban_sync(calendar, factory, tmp_path)
        first = await sync.sync_day(NOV25)
        again = await sync.sync_day(NOV25)  # from the archive the first run filled
        assert again.source == "archive" and again.written == first.written
        assert _ban_column(seeded, NOV25) == (_active(seeded), 1, 0)

    async def test_a_refused_list_leaves_the_flag_unknown(
        self, seeded: str, factory, calendar, tmp_path
    ) -> None:
        """25 Nov's roster reports; 25 Nov's ban list is the 30 Jan file - the
        wrong day, refused. Every instrument's ban flag stays NULL, so even a
        stock the roster reports clean is refused."""
        await _roster_sync(calendar, factory, tmp_path / "roster").sync_day(NOV25)
        wrong = {NOV25: Fetched(BANS[JAN30].body, BANS[NOV25].last_modified)}
        result = await _ban_sync(calendar, factory, tmp_path / "ban", wrong).sync_day(NOV25)
        assert result.outcome is SyncOutcome.REFUSED and "not 2026-11-25" in result.detail
        rows, banned, unknown = _ban_column(seeded, NOV25)
        assert rows > 0 and banned == 0 and unknown == rows
        assert restrictions_from(await _status(factory, "INFY", NOV25)) == ("FNO_BAN",)


class TestBesideTheRoster:
    @pytest.mark.parametrize("ban_first", [False, True])
    async def test_each_source_keeps_the_others_columns_whichever_writes_first(
        self, seeded: str, factory, calendar, tmp_path, ban_first: bool
    ) -> None:
        roster = _roster_sync(calendar, factory, tmp_path / "roster")
        ban = _ban_sync(calendar, factory, tmp_path / "ban")
        order = (ban, roster) if ban_first else (roster, ban)
        for sync in order:
            assert (await sync.sync_day(NOV25)).outcome is SyncOutcome.STORED
        banned = await _status(factory, BANNED_25, NOV25)
        assert banned is not None
        assert (banned["is_t2t"], banned["is_asm"], banned["is_gsm"]) == (False, False, False)
        assert banned["is_fno_ban"] is True
        # A stock under surveillance keeps its measure, and is not banned.
        flagged = next(
            r.tradingsymbol
            for r in parse_roster(ROSTER_23, session=NOV23)
            if r.is_asm and r.tradingsymbol in MASTER
        )
        status = await _status(factory, flagged, NOV25)
        assert status is not None and status["is_asm"] is True and status["is_fno_ban"] is False

    async def test_restrictions_from_blocks_the_ban_and_clears_a_stock_all_report_clean(
        self, seeded: str, factory, calendar, tmp_path
    ) -> None:
        """Until this story every symbol carried FNO_BAN, because nothing
        reported the flag. With the roster and the ban both in, a stock every
        source reports clean has no restriction - the control - and the
        banned one has exactly one."""
        await _roster_sync(calendar, factory, tmp_path / "roster").sync_day(NOV25)
        await _ban_sync(calendar, factory, tmp_path / "ban").sync_day(NOV25)
        assert restrictions_from(await _status(factory, BANNED_25, NOV25)) == ("FNO_BAN",)
        for clean in ("INFY", "TCS", "APLAPOLLO"):  # APLAPOLLO entered the ban on the 26th
            assert restrictions_from(await _status(factory, clean, NOV25)) == (), clean

    async def test_the_ban_alone_does_not_clear_a_stock(
        self, seeded: str, factory, calendar, tmp_path
    ) -> None:
        """Without the roster the other flags are still unknown."""
        await _ban_sync(calendar, factory, tmp_path).sync_day(NOV25)
        assert restrictions_from(await _status(factory, "INFY", NOV25)) == ("T2T", "ASM", "GSM")


async def test_the_store_reads_the_master_from_the_table(factory) -> None:
    assert {"INFY", BANNED_25} <= await RepositoryBanStore(factory).active_symbols()


# ---------------------------------------------------------------------------
# The command, as an operator runs it
# ---------------------------------------------------------------------------


@pytest.fixture
def deployed_seeded(deployed_database: dict[str, str]) -> Iterator[dict[str, str]]:
    before = _seed(deployed_database["owner"])
    yield deployed_database
    _wipe(deployed_database["owner"], before)


class TestTheCommand:
    """``scripts/sync_fno_ban.py`` end to end: a process of its own (the test
    process sets the Windows event-loop policy for itself - SIT-011), under the
    services' login the real first-init script creates."""

    @staticmethod
    def _run(url: str, archive: Path) -> subprocess.CompletedProcess[str]:
        script = CODE / "scripts" / "sync_fno_ban.py"
        argv = ["--date", "2026-11-25", "--archive", str(archive), "--offline"]
        return subprocess.run(  # a fixed command: this interpreter, this repository's script
            [sys.executable, str(script), *argv],
            env={**os.environ, "DATABASE_URL": url},
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )

    def test_the_services_login_writes_the_day(self, deployed_seeded, tmp_path) -> None:
        FnoBanArchive(tmp_path).write(NOV25, BANS[NOV25].body)
        done = self._run(deployed_seeded["svc"], tmp_path)
        assert done.returncode == 0, done.stderr[-2000:]
        active = _active(deployed_seeded["owner"])
        assert _ban_column(deployed_seeded["owner"], NOV25) == (active, 1, 0)
        assert f"1 stored, 0 missing, 0 refused; {active} status row(s) written" in done.stdout
        assert f"banned=1 flagged=1 ({BANNED_25})" in done.stdout

    def test_a_login_that_could_rewrite_the_record_is_refused(
        self, deployed_seeded, tmp_path
    ) -> None:
        FnoBanArchive(tmp_path).write(NOV25, BANS[NOV25].body)
        done = self._run(deployed_seeded["owner"], tmp_path)
        assert done.returncode == 2, done.stderr[-2000:]
        assert _ban_column(deployed_seeded["owner"], NOV25)[0] == 0
        assert "refused:" in done.stderr and "algotrader_svc" in done.stderr
        assert "test_only_not_a_real_secret" not in done.stderr  # it names no secret

    def test_a_missing_list_exits_1_and_writes_nothing(self, deployed_seeded, tmp_path) -> None:
        done = self._run(deployed_seeded["svc"], tmp_path)  # an empty archive, offline
        assert done.returncode == 1, done.stderr[-2000:]
        assert "0 stored, 1 missing" in done.stdout
        assert _ban_column(deployed_seeded["owner"], NOV25)[0] == 0

    def test_a_tampered_archive_exits_1_and_writes_nothing(self, deployed_seeded, tmp_path) -> None:
        FnoBanArchive(tmp_path).write(NOV25, BANS[JAN30].body)  # another day's list
        done = self._run(deployed_seeded["svc"], tmp_path)
        assert done.returncode == 1, done.stderr[-2000:]
        assert "0 stored, 0 missing, 1 refused" in done.stdout
        assert _ban_column(deployed_seeded["owner"], NOV25)[0] == 0
