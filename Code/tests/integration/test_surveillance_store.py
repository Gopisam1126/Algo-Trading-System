"""E04-S01 and S02 against the real schema: NSE's surveillance rosters into
``instrument_daily_status``, read back by the readers execution uses.

The unit tests prove the parse and the sync's decisions. This proves the seams:
the migration that makes an unreported flag NULL (unknown) instead of false
(clean); the repository writing only a source's own columns, so a second
source cannot reset the first's flags; the real roster's names landing on the
right trade date, T2T among them; and ``restrictions_from`` - what the
execution service consults before every entry - and ``v_eligible_today``
refusing what the roster flags and what nobody has reported.

The instruments and rows this writes are committed (the store opens its own
transaction, as production does), so the fixture removes exactly what it added,
at both ends (QA-E15-28).
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
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from algotrader.common.calendar import MarketCalendar, load_holidays_with_status
from algotrader.common.db import engine as db_engine
from algotrader.common.db import repositories
from algotrader.common.db.repositories import DailyStatusRepository, InstrumentRepository
from algotrader.common.enums import SurveillanceCategory
from algotrader.execution.session_context import restrictions_from
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
NOV20, NOV23, NOV24, NOV25, NOV26 = (dt.date(2026, 11, d) for d in (20, 23, 24, 25, 26))
#: A wall clock after every day synced here: each closed day is over.
AFTER = dt.datetime(2026, 12, 1, tzinfo=dt.UTC)
#: Trade-for-trade on 25 Nov 2026 (BE, BZ, ST, SZ) and under no other measure.
T2T_ONLY = ("ABAN-BE", "ANSALAPI-BZ", "AARADHYA-ST", "GLOBAL-BE")
ROSTER_20 = gzip.decompress((FIXTURES / "surveillance" / "REG1_IND201126.csv.gz").read_bytes())
ROSTER_23 = gzip.decompress((FIXTURES / "surveillance" / "REG1_IND231126.csv.gz").read_bytes())
WRITTEN = {
    NOV20: dt.datetime(2026, 11, 20, 13, 54, 4, tzinfo=dt.UTC),
    NOV23: dt.datetime(2026, 11, 23, 14, 40, 33, tzinfo=dt.UTC),
}
MASTER = {
    line
    for line in (FIXTURES / "universe" / "nse_eq_symbols_20261122.txt")
    .read_text("utf-8")
    .splitlines()
    if line and not line.startswith("#")
}
#: Every security either roster names that Kite's list knows - what the master holds.
SYMBOLS = sorted(
    (
        {r.tradingsymbol for r in parse_roster(ROSTER_20, session=NOV20)}
        | {r.tradingsymbol for r in parse_roster(ROSTER_23, session=NOV23)}
    )
    & MASTER
)
#: 23 Nov's roster as the master knows it - what a 25 Nov sync writes.
WRITTEN_25 = len({r.tradingsymbol for r in parse_roster(ROSTER_23, session=NOV23)} & MASTER)
#: ... and how many of those trade for trade on 25 Nov.
T2T_25 = len({r.tradingsymbol for r in parse_roster(ROSTER_23, session=NOV23) if r.is_t2t} & MASTER)
#: Named by the 20 Nov roster and not the 23 Nov one: nothing reports their 25 Nov series.
UNNAMED_25 = sorted(
    set(SYMBOLS) - {r.tradingsymbol for r in parse_roster(ROSTER_23, session=NOV23)}
)


def _owner(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://")


def _ist_today(url: str) -> dt.date:
    with psycopg.connect(_owner(url)) as conn:
        row = conn.execute("SELECT (now() AT TIME ZONE 'Asia/Kolkata')::date").fetchone()
        assert row is not None
        return row[0]  # type: ignore[no-any-return]


def _wipe(url: str, before: set[str]) -> None:
    days = [NOV23, NOV25, NOV26, _ist_today(url)]
    with psycopg.connect(_owner(url), autocommit=True) as conn:
        conn.execute(
            "DELETE FROM instrument_daily_status WHERE trade_date = ANY(%s) AND symbol_id IN "
            "(SELECT id FROM instruments WHERE tradingsymbol = ANY(%s))",
            (days, SYMBOLS),
        )
        added = [s for s in SYMBOLS if s not in before]
        conn.execute("DELETE FROM instruments WHERE tradingsymbol = ANY(%s)", (added,))


def _seed(url: str) -> set[str]:
    """Add the rosters' instruments; return what was there before, for ``_wipe``."""
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
                [(s, f"sv-{i}") for i, s in enumerate(SYMBOLS)],
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


@pytest.fixture
def calendar() -> MarketCalendar:
    status = load_holidays_with_status(str(CODE / "config" / "nse_holidays.yaml"))
    return MarketCalendar(status.dates, covers_years=status.covers_years)


def _rows_on(url: str, day: dt.date) -> int:
    with psycopg.connect(_owner(url)) as conn:
        row = conn.execute(
            "SELECT count(*) FROM instrument_daily_status WHERE trade_date = %s", (day,)
        ).fetchone()
        assert row is not None
        return int(row[0])


async def _status(
    factory: async_sessionmaker[AsyncSession], symbol: str, day: dt.date
) -> dict[str, Any] | None:
    async with factory() as session:
        instruments = InstrumentRepository(session)
        await instruments.refresh_cache()
        return await DailyStatusRepository(session, instruments).for_symbol(symbol, day)


async def _report(
    factory: async_sessionmaker[AsyncSession], day: dt.date, columns: dict[str, Any]
) -> None:
    """Another hazard source (T2T, the F&O ban) reporting its flags for every
    instrument - what E04-S02 and S03 will do."""
    async with factory() as session, session.begin():
        instruments = InstrumentRepository(session)
        await instruments.refresh_cache()
        rows = [
            {"symbol_id": await instruments.symbol_id(s), "trade_date": day, **columns}
            for s in SYMBOLS
        ]
        await DailyStatusRepository(session, instruments).upsert(rows)


def _fetch(session: dt.date) -> Fetched:
    raw = {NOV20: ROSTER_20, NOV23: ROSTER_23}.get(session)
    if raw is None:
        raise RosterMissingError(f"NSE has no surveillance roster for {session} (HTTP 404)")
    return Fetched(raw, WRITTEN[session])


def _sync(
    calendar: MarketCalendar, factory: async_sessionmaker[AsyncSession], archive: Path
) -> SurveillanceSync:
    return SurveillanceSync(
        calendar=calendar,
        archive=RosterArchive(archive),
        store=RepositoryStatusStore(factory),
        fetch=_fetch,
        now=lambda: AFTER,
    )


# ---------------------------------------------------------------------------
# The schema: a flag nobody reported is unknown, and unknown is not eligible
# ---------------------------------------------------------------------------


class TestUnknownIsNotClean:
    def test_a_row_written_with_only_its_key_has_every_flag_unknown(self, seeded: str) -> None:
        with psycopg.connect(_owner(seeded), autocommit=True) as conn:
            conn.execute(
                "INSERT INTO instrument_daily_status (symbol_id, trade_date) "
                "SELECT id, %s FROM instruments WHERE tradingsymbol = 'INFY'",
                (NOV26,),
            )
            row = conn.execute(
                "SELECT is_t2t, is_asm, is_gsm, is_fno_ban, is_cas_stock, has_earnings_today "
                "FROM instrument_daily_status s JOIN instruments i ON i.id = s.symbol_id "
                "WHERE i.tradingsymbol = 'INFY' AND s.trade_date = %s",
                (NOV26,),
            ).fetchone()
        assert row == (None, None, None, None, None, None)

    def test_the_eligibility_view_refuses_an_unreported_flag(self, seeded: str) -> None:
        """``v_eligible_today`` reads ``NOT s.is_t2t``: for an unknown flag that
        is NULL, not true, so the row drops out. Its control: once every flag
        it reads is reported clean, the symbol is eligible."""
        today = _ist_today(seeded)
        with psycopg.connect(_owner(seeded), autocommit=True) as conn:
            conn.execute(
                "INSERT INTO instrument_daily_status (symbol_id, trade_date, is_asm, is_gsm) "
                "SELECT id, %s, false, false FROM instruments WHERE tradingsymbol = 'INFY'",
                (today,),
            )

            def eligible() -> bool:
                row = conn.execute(
                    "SELECT count(*) FROM v_eligible_today WHERE tradingsymbol = 'INFY'"
                ).fetchone()
                return row is not None and row[0] == 1

            assert not eligible()  # is_t2t is unknown
            conn.execute(
                "UPDATE instrument_daily_status SET is_t2t = false WHERE trade_date = %s "
                "AND symbol_id = (SELECT id FROM instruments WHERE tradingsymbol = 'INFY')",
                (today,),
            )
            assert eligible()


# ---------------------------------------------------------------------------
# The repository: each source writes its own columns and nothing else
# ---------------------------------------------------------------------------


class TestEachSourceWritesItsOwnColumns:
    async def test_a_second_source_leaves_the_first_sources_flags_alone(self, factory) -> None:
        """The defect this story found: the upsert rewrote every column from the
        row it was given, so the T2T list - written after the roster - would
        have reset a surveillance flag to the default."""
        async with factory() as session, session.begin():
            instruments = InstrumentRepository(session)
            await instruments.refresh_cache()
            sid = await instruments.symbol_id("EMPOWER")
            repo = DailyStatusRepository(session, instruments)
            await repo.upsert(
                [
                    {
                        "symbol_id": sid,
                        "trade_date": NOV25,
                        "is_asm": True,
                        "asm_category": SurveillanceCategory.LONG_TERM,
                    }
                ]
            )
            await repo.upsert([{"symbol_id": sid, "trade_date": NOV25, "is_t2t": False}])
        status = await _status(factory, "EMPOWER", NOV25)
        assert status is not None
        assert (status["is_asm"], status["asm_category"], status["is_t2t"]) == (
            True,
            "LONG_TERM",
            False,
        )
        assert status["is_gsm"] is None  # nobody reported it

    async def test_rows_that_disagree_on_their_columns_are_refused(self, factory) -> None:
        async with factory() as session, session.begin():
            instruments = InstrumentRepository(session)
            await instruments.refresh_cache()
            a, b = await instruments.symbol_id("INFY"), await instruments.symbol_id("TCS")
            repo = DailyStatusRepository(session, instruments)
            with pytest.raises(ValueError, match="same columns"):
                await repo.upsert(
                    [
                        {"symbol_id": a, "trade_date": NOV25, "is_t2t": False},
                        {"symbol_id": b, "trade_date": NOV25, "is_t2t": False, "is_asm": False},
                    ]
                )

    @pytest.mark.parametrize("column", ["fetched_at", "symbol", "is_t2t; DROP TABLE x"])
    async def test_a_column_that_is_not_a_status_column_is_refused(self, factory, column) -> None:
        async with factory() as session, session.begin():
            instruments = InstrumentRepository(session)
            await instruments.refresh_cache()
            sid = await instruments.symbol_id("INFY")
            with pytest.raises(ValueError, match="status columns only"):
                await DailyStatusRepository(session, instruments).upsert(
                    [{"symbol_id": sid, "trade_date": NOV25, column: False}]
                )

    async def test_a_write_wider_than_one_statement_is_chunked_and_counted(
        self, factory, seeded, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(repositories, "_PG_MAX_BIND_PARAMS", 30)  # 5 rows of 6 a statement
        engine = factory.kw["bind"].sync_engine
        inserts: list[str] = []

        def count(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
            if statement.startswith("INSERT INTO instrument_daily_status"):
                inserts.append(statement)

        event.listen(engine, "before_cursor_execute", count)
        try:
            await self._write_23(factory)
        finally:
            event.remove(engine, "before_cursor_execute", count)
        assert len(inserts) == 5  # ceil(23 / 5): one statement could not have held them
        assert _rows_on(seeded, NOV26) == 23

    @staticmethod
    async def _write_23(factory: async_sessionmaker[AsyncSession]) -> None:
        async with factory() as session, session.begin():
            instruments = InstrumentRepository(session)
            await instruments.refresh_cache()
            rows = [
                {
                    "symbol_id": await instruments.symbol_id(s),
                    "trade_date": NOV26,
                    "is_asm": False,
                    "asm_category": None,
                    "is_gsm": False,
                    "gsm_stage": None,
                }
                for s in SYMBOLS[:23]
            ]
            written = await DailyStatusRepository(session, instruments).upsert(rows)
        assert written == 23  # counted, not the driver's -1


# ---------------------------------------------------------------------------
# The real rosters, through the real store, read by the real reader
# ---------------------------------------------------------------------------


class TestTheRealRosters:
    async def test_25_nov_carries_what_nse_applied_from_25_nov(
        self, calendar, factory, tmp_path
    ) -> None:
        result = await _sync(calendar, factory, tmp_path).sync_day(NOV25)
        assert result.outcome is SyncOutcome.STORED and result.roster_session == NOV23
        assert result.written == WRITTEN_25
        empower = await _status(factory, "EMPOWER", NOV25)
        aspire = await _status(factory, "ASPIRE-SM", NOV25)
        stage0 = await _status(factory, "ASIANTNE", NOV25)
        infy = await _status(factory, "INFY", NOV25)
        assert empower and (empower["is_asm"], empower["asm_category"]) == (True, "LONG_TERM")
        assert aspire and (aspire["is_asm"], aspire["asm_category"]) == (True, "SHORT_TERM")
        assert stage0 and (stage0["is_gsm"], stage0["gsm_stage"]) == (True, 0)
        assert infy and (infy["is_asm"], infy["is_gsm"], infy["gsm_stage"]) == (False, False, None)
        assert infy["is_t2t"] is False  # EQ: rolling (E04-S02)
        aban = await _status(factory, "ABAN-BE", NOV25)
        assert aban and (aban["is_t2t"], aban["is_asm"], aban["is_gsm"]) == (True, False, False)
        # Only the roster's columns: what no source has reported stays unknown.
        assert (infy["is_fno_ban"], infy["is_cas_stock"], infy["has_earnings_today"]) == (
            None,
            None,
            None,
        )

    async def test_a_stock_added_overnight_is_refused_on_the_day_it_applies(
        self, calendar, factory, tmp_path
    ) -> None:
        """AC1, through the reader execution consults before every entry. NSE
        put EMPOWER into LT-ASM w.e.f. 25 Nov (NSE/SURV/77569, 23 Nov): with the
        other sources reporting it clean, it may be traded on the 23rd and is
        refused on the 25th - and a clean stock is refused on neither."""
        sync = _sync(calendar, factory, tmp_path)
        for day in (NOV23, NOV25):
            assert (await sync.sync_day(day)).outcome is SyncOutcome.STORED
            await _report(factory, day, {"is_fno_ban": False})
        assert restrictions_from(await _status(factory, "EMPOWER", NOV23)) == ()
        assert restrictions_from(await _status(factory, "EMPOWER", NOV25)) == ("ASM:LONG_TERM",)
        assert restrictions_from(await _status(factory, "INFY", NOV25)) == ()
        assert restrictions_from(await _status(factory, "ASIANTNE", NOV25)) == ("GSM:0",)

    async def test_until_every_source_reports_a_clean_stock_is_still_refused(
        self, calendar, factory, tmp_path
    ) -> None:
        await _sync(calendar, factory, tmp_path).sync_day(NOV25)
        assert restrictions_from(await _status(factory, "INFY", NOV25)) == ("FNO_BAN",)

    async def test_a_day_whose_roster_is_missing_leaves_every_symbol_refused(
        self, calendar, factory, tmp_path, seeded
    ) -> None:
        """AC2: the 26th reads the 25th's roster, which does not exist here.
        Nothing is written, so every symbol has no row and is refused."""
        result = await _sync(calendar, factory, tmp_path).sync_day(NOV26)
        assert result.outcome is SyncOutcome.MISSING
        assert _rows_on(seeded, NOV26) == 0
        for symbol in ("INFY", "EMPOWER"):
            assert restrictions_from(await _status(factory, symbol, NOV26)) is None

    async def test_a_rerun_from_the_archive_writes_the_same_day_again(
        self, calendar, factory, tmp_path, seeded
    ) -> None:
        await _sync(calendar, factory, tmp_path).sync_day(NOV25)
        before = _rows_on(seeded, NOV25)
        again = await _sync(calendar, factory, tmp_path).sync_day(NOV25)
        assert again.source == "archive" and again.written == before == WRITTEN_25
        assert _rows_on(seeded, NOV25) == before


class TestTradeForTrade:
    """E04-S02: the roster's series as ``is_t2t``, through the real store and
    both readers - ``restrictions_from`` and the eligibility view."""

    async def test_a_t2t_stock_is_refused_by_the_reader_execution_consults(
        self, calendar, factory, tmp_path, seeded
    ) -> None:
        """AC1 as far as it is built (the watchlist's hard filter is E07-S02):
        with the F&O ban reported clean, a trade-for-trade stock under no
        other measure is refused for T2T alone, and a rolling one is not. A
        security the governing roster does not name stays unknown - refused -
        however clean the other sources say it is."""
        result = await _sync(calendar, factory, tmp_path).sync_day(NOV25)
        await _report(factory, NOV25, {"is_fno_ban": False})
        for symbol in T2T_ONLY:
            assert restrictions_from(await _status(factory, symbol, NOV25)) == ("T2T",), symbol
        assert restrictions_from(await _status(factory, "INFY", NOV25)) == ()
        hfcl = restrictions_from(await _status(factory, "HFCL-BE", NOV25))  # Nifty 500, BE
        assert hfcl is not None and hfcl[0] == "T2T" and len(hfcl) > 1  # and under ASM
        assert UNNAMED_25, "the two rosters name the same securities: the control proves nothing"
        for symbol in UNNAMED_25:
            restricted = restrictions_from(await _status(factory, symbol, NOV25))
            assert restricted == ("T2T", "ASM", "GSM"), symbol
        with psycopg.connect(_owner(seeded)) as conn:
            counts = conn.execute(
                "SELECT count(*) FILTER (WHERE is_t2t), count(*) FILTER (WHERE is_t2t IS NULL) "
                "FROM instrument_daily_status WHERE trade_date = %s",
                (NOV25,),
            ).fetchone()
        assert counts == (result.t2t, len(UNNAMED_25)) and result.t2t == T2T_25

    async def test_no_t2t_stock_is_eligible_even_with_every_other_flag_reported_clean(
        self, factory, seeded
    ) -> None:
        """SPRINT02 8: no T2T symbol may reach the eligible list, even if every
        other source downstream of the roster says it is clean. The control:
        rolling stocks are listed once they are."""
        today = _ist_today(seeded)
        rows = [r for r in parse_roster(ROSTER_23, session=NOV23) if r.tradingsymbol in MASTER]
        await RepositoryStatusStore(factory).write(today, rows)
        with psycopg.connect(_owner(seeded), autocommit=True) as conn:
            conn.execute(
                "UPDATE instrument_daily_status SET is_asm = false, is_gsm = false, "
                "is_fno_ban = false, is_cas_stock = false WHERE trade_date = %s AND symbol_id IN "
                "(SELECT id FROM instruments WHERE tradingsymbol = ANY(%s))",
                (today, SYMBOLS),
            )
            listed = {
                r[0]
                for r in conn.execute(
                    "SELECT tradingsymbol FROM v_eligible_today WHERE tradingsymbol = ANY(%s)",
                    (SYMBOLS,),
                ).fetchall()
            }
        t2t = {r.tradingsymbol for r in rows if r.is_t2t}
        assert len(t2t) == T2T_25 and not t2t & listed
        assert {"INFY", "EMPOWER", "ASPIRE-SM"} <= listed


# ---------------------------------------------------------------------------
# The command, as an operator runs it
# ---------------------------------------------------------------------------


@pytest.fixture
def deployed_seeded(deployed_database: dict[str, str]) -> Iterator[dict[str, str]]:
    before = _seed(deployed_database["owner"])
    yield deployed_database
    _wipe(deployed_database["owner"], before)


class TestTheCommand:
    """``scripts/sync_surveillance.py`` end to end: a process of its own (the
    test process sets the Windows event-loop policy for itself - SIT-011),
    under the services' login the real first-init script creates."""

    @staticmethod
    def _run(url: str, archive: Path) -> subprocess.CompletedProcess[str]:
        script = CODE / "scripts" / "sync_surveillance.py"
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
        archive = RosterArchive(tmp_path)
        archive.write(NOV23, ROSTER_23)
        archive.mark_absent(NOV24, at=AFTER)  # as the online run that filled it recorded
        done = self._run(deployed_seeded["svc"], tmp_path)
        assert done.returncode == 0, done.stderr[-2000:]
        assert _rows_on(deployed_seeded["owner"], NOV25) == WRITTEN_25
        assert f"1 stored, 0 missing, 0 refused; {WRITTEN_25} status row(s) written" in done.stdout
        assert "roster=2026-11-23" in done.stdout and f" t2t={T2T_25} " in done.stdout

    def test_an_archive_silent_about_a_closed_day_exits_1_offline(
        self, deployed_seeded, tmp_path
    ) -> None:
        """The 23rd's roster alone cannot govern the 25th offline: nothing says
        NSE held no session on the 24th."""
        RosterArchive(tmp_path).write(NOV23, ROSTER_23)
        done = self._run(deployed_seeded["svc"], tmp_path)
        assert done.returncode == 1, done.stderr[-2000:]
        assert "0 stored, 1 missing" in done.stdout and "2026-11-24" in done.stdout
        assert _rows_on(deployed_seeded["owner"], NOV25) == 0

    def test_a_login_that_could_rewrite_the_record_is_refused(
        self, deployed_seeded, tmp_path
    ) -> None:
        RosterArchive(tmp_path).write(NOV23, ROSTER_23)
        done = self._run(deployed_seeded["owner"], tmp_path)
        assert done.returncode == 2, done.stderr[-2000:]
        assert _rows_on(deployed_seeded["owner"], NOV25) == 0
        assert "refused:" in done.stderr and "algotrader_svc" in done.stderr
        assert "test_only_not_a_real_secret" not in done.stderr  # it names no secret

    def test_a_missing_roster_exits_1_and_writes_nothing(self, deployed_seeded, tmp_path) -> None:
        done = self._run(deployed_seeded["svc"], tmp_path)  # an empty archive, offline
        assert done.returncode == 1, done.stderr[-2000:]
        assert "0 stored, 1 missing" in done.stdout
        assert _rows_on(deployed_seeded["owner"], NOV25) == 0


async def test_the_store_reads_the_master_from_the_table(factory) -> None:
    assert {"INFY", "EMPOWER", "ASPIRE-SM"} <= await RepositoryStatusStore(factory).active_symbols()


async def test_the_session_sees_the_rows_the_store_committed(factory, calendar, tmp_path) -> None:
    """The store commits in its own transaction; a later reader sees it."""
    await _sync(calendar, factory, tmp_path).sync_day(NOV25)
    async with factory() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM instrument_daily_status WHERE trade_date = :d"),
                {"d": NOV25},
            )
        ).scalar_one()
    assert count == WRITTEN_25
