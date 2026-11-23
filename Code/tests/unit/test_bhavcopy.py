"""E03-S01: NSE's bhavcopy, read strictly, stored as daily bars.

``tests/fixtures/bhavcopy/`` holds the real UDiFF file for 20 Nov 2026,
downloaded from nsearchives.nseindia.com on 23 Nov 2026. Expected values are
read off that file by hand (``unzip`` and ``grep``), and the VWAP worked out
by hand: INFY traded 13,749,744 shares for Rs 15,158,082,633.00, which is
Rs 1,102.42653... a share - 1102.4265 to four places. Synthetic files, for
the refusals, are built from the same header by ``_zip``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import urllib.error
import urllib.request
import zipfile
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from algotrader.common.calendar import HolidayDataError, MarketCalendar, load_holidays_with_status
from algotrader.common.enums import Timeframe
from algotrader.premarket import bhavcopy as bc
from algotrader.premarket.bhavcopy import (
    HEADER,
    BhavcopyArchive,
    BhavcopyError,
    BhavcopyLoader,
    BhavcopyMissingError,
    DayOutcome,
    fetch_bhavcopy,
    parse_bhavcopy,
    to_daily_bars,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
REAL_DAY = dt.date(2026, 11, 20)
REAL_ZIP = (FIXTURES / "bhavcopy" / "BhavCopy_NSE_CM_0_0_0_20261120_F_0000.csv.zip").read_bytes()
MASTER = frozenset(
    line
    for line in (FIXTURES / "universe" / "nse_eq_symbols_20261122.txt")
    .read_text("utf-8")
    .splitlines()
    if line and not line.startswith("#")
)


@pytest.fixture(scope="module")
def calendar() -> MarketCalendar:
    status = load_holidays_with_status(
        str(Path(__file__).resolve().parents[2] / "config" / "nse_holidays.yaml")
    )
    return MarketCalendar(status.dates, covers_years=status.covers_years)


def _row(day: dt.date, symbol: str, series: str = "EQ", **over: str) -> list[str]:
    values = dict.fromkeys(HEADER, "")
    values.update(
        TradDt=day.isoformat(),
        BizDt=day.isoformat(),
        Sgmt="CM",
        Src="NSE",
        FinInstrmTp="STK",
        FinInstrmId="1",
        ISIN="INE000A01010",
        TckrSymb=symbol,
        SctySrs=series,
        FinInstrmNm=f"{symbol} LTD",
        OpnPric="100.00",
        HghPric="110.00",
        LwPric="95.00",
        ClsPric="105.00",
        LastPric="105.00",
        PrvsClsgPric="99.00",
        TtlTradgVol="1000",
        TtlTrfVal="102000.00",
        TtlNbOfTxsExctd="50",
        SsnId="F1",
        NewBrdLotQty="1",
    )
    values.update(over)
    return [values[h] for h in HEADER]


FILLER = [f"FILL{i:04d}" for i in range(bc.MIN_ROWS)]


def _csv(day: dt.date, rows: list[list[str]], *, filler: bool = True, header=HEADER) -> str:
    lines = [",".join(header)]
    lines += [",".join(r) for r in rows]
    if filler:
        lines += [",".join(_row(day, s)) for s in FILLER]
    return "\n".join(lines) + "\n"


def _zip(day: dt.date, text: str, *, name: str | None = None, extra: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name or bc.MEMBER.format(date=day.strftime("%Y%m%d")), text)
        if extra is not None:
            archive.writestr(extra, "x")
    return buffer.getvalue()


def _file(day: dt.date, rows: list[list[str]] | None = None, **kw: Any) -> bytes:
    return _zip(day, _csv(day, rows or [], **kw))


# ---------------------------------------------------------------------------
# AC1 - the real file
# ---------------------------------------------------------------------------


class TestTheRealFile:
    def test_every_row_parses(self) -> None:
        assert len(parse_bhavcopy(REAL_ZIP, day=REAL_DAY)) == 3742

    def test_a_row_carries_the_exchanges_numbers(self) -> None:
        infy = next(r for r in parse_bhavcopy(REAL_ZIP, day=REAL_DAY) if r.symbol == "INFY")
        assert (infy.open, infy.high, infy.low, infy.close) == (
            Decimal("1087.00"),
            Decimal("1118.80"),
            Decimal("1081.70"),
            Decimal("1098.00"),
        )
        assert (infy.volume, infy.trades, infy.token) == (13749744, 197822, 1594)
        assert infy.turnover == Decimal("15158082633.00")

    def test_series_map_to_kites_symbols(self) -> None:
        names = {r.tradingsymbol for r in parse_bhavcopy(REAL_ZIP, day=REAL_DAY)}
        assert {"INFY", "HFCL-BE", "EMBASSY-RR"} <= names

    def test_it_becomes_daily_bars_at_the_session_open(self, calendar) -> None:
        daily = to_daily_bars(
            parse_bhavcopy(REAL_ZIP, day=REAL_DAY), day=REAL_DAY, master=MASTER, calendar=calendar
        )
        assert (len(daily.bars), len(daily.unresolved), len(daily.untraded)) == (3722, 20, 0)
        infy = next(b for b in daily.bars if b.symbol == "INFY")
        assert infy.timeframe is Timeframe.D1
        assert infy.open_ts == dt.datetime(2026, 11, 20, 3, 45, tzinfo=dt.UTC)
        assert infy.vwap == Decimal("1102.4265")
        assert infy.trade_count == 197822

    def test_the_file_dates_itself(self) -> None:
        with pytest.raises(BhavcopyError, match="not exactly"):
            parse_bhavcopy(REAL_ZIP, day=dt.date(2026, 11, 19))


# ---------------------------------------------------------------------------
# AC2 - refused whole, never partly loaded
# ---------------------------------------------------------------------------


DAY = REAL_DAY


class TestRefusals:
    def test_an_html_page(self) -> None:
        with pytest.raises(BhavcopyError, match="not a zip"):
            parse_bhavcopy(b"<!DOCTYPE html><html>404</html>", day=DAY)

    def test_another_header(self) -> None:
        header = ("TradDate", *HEADER[1:])
        with pytest.raises(BhavcopyError, match="header"):
            parse_bhavcopy(_zip(DAY, _csv(DAY, [], header=header)), day=DAY)

    def test_a_stale_file_under_todays_name(self) -> None:
        """Yesterday's rows in a file named for today: every row's own date is read."""
        rows = [_row(dt.date(2026, 11, 19), "AAA")]
        with pytest.raises(BhavcopyError, match="stale or wrong"):
            parse_bhavcopy(_file(DAY, rows), day=DAY)

    def test_a_zip_with_two_files(self) -> None:
        raw = _zip(DAY, _csv(DAY, []), extra="readme.txt")
        with pytest.raises(BhavcopyError, match="not exactly"):
            parse_bhavcopy(raw, day=DAY)

    def test_a_zip_whose_file_is_misnamed(self) -> None:
        with pytest.raises(BhavcopyError, match="not exactly"):
            parse_bhavcopy(_zip(DAY, _csv(DAY, []), name="../evil.csv"), day=DAY)

    def test_a_file_that_expands_past_the_bound(self, monkeypatch) -> None:
        monkeypatch.setattr(bc, "MAX_CSV_BYTES", 1024)
        with pytest.raises(BhavcopyError, match="expands"):
            parse_bhavcopy(_file(DAY), day=DAY)

    def test_a_response_too_large(self) -> None:
        with pytest.raises(BhavcopyError, match="bytes"):
            parse_bhavcopy(b"PK" + b"x" * bc.MAX_ZIP_BYTES, day=DAY)

    def test_a_truncated_file(self) -> None:
        rows = [_row(DAY, s) for s in FILLER[: bc.MIN_ROWS - 1]]
        with pytest.raises(BhavcopyError, match="truncated"):
            parse_bhavcopy(_file(DAY, rows, filler=False), day=DAY)

    def test_control_exactly_the_floor_is_accepted(self) -> None:
        rows = [_row(DAY, s) for s in FILLER]
        assert len(parse_bhavcopy(_file(DAY, rows, filler=False), day=DAY)) == bc.MIN_ROWS

    def test_a_symbol_twice(self) -> None:
        with pytest.raises(BhavcopyError, match="twice"):
            parse_bhavcopy(_file(DAY, [_row(DAY, "AAA"), _row(DAY, "AAA")]), day=DAY)

    def test_the_same_symbol_in_two_series_is_two_instruments(self) -> None:
        rows = parse_bhavcopy(_file(DAY, [_row(DAY, "AAA"), _row(DAY, "AAA", "BE")]), day=DAY)
        assert {"AAA", "AAA-BE"} <= {r.tradingsymbol for r in rows}

    def test_a_symbol_that_could_forge_a_log_line(self) -> None:
        with pytest.raises(BhavcopyError, match="symbol"):
            parse_bhavcopy(_file(DAY, [_row(DAY, '"AAA\nCRITICAL forged"')]), day=DAY)

    @pytest.mark.parametrize("series", ["E", "EQUITY", "e1", ""])
    def test_a_series_that_is_not_one(self, series: str) -> None:
        with pytest.raises(BhavcopyError, match="series"):
            parse_bhavcopy(_file(DAY, [_row(DAY, "AAA", series)]), day=DAY)

    @pytest.mark.parametrize(
        ("column", "value"),
        [("ClsPric", "abc"), ("ClsPric", ""), ("ClsPric", "-5.00"), ("TtlTradgVol", "1.5")],
    )
    def test_a_number_that_is_not_one(self, column: str, value: str) -> None:
        with pytest.raises(BhavcopyError, match=column):
            parse_bhavcopy(_file(DAY, [_row(DAY, "AAA", **{column: value})]), day=DAY)

    def test_another_segment(self) -> None:
        with pytest.raises(BhavcopyError, match="capital market"):
            parse_bhavcopy(_file(DAY, [_row(DAY, "AAA", Sgmt="FO")]), day=DAY)

    def test_a_row_of_the_wrong_width(self) -> None:
        text = _csv(DAY, []) + ",".join(_row(DAY, "AAA")[:-1]) + "\n"
        with pytest.raises(BhavcopyError, match="fields"):
            parse_bhavcopy(_zip(DAY, text), day=DAY)

    def test_an_incoherent_bar_refuses_the_file(self, calendar) -> None:
        rows = parse_bhavcopy(
            _file(DAY, [_row(DAY, "AAA", HghPric="90.00", LwPric="95.00")]), day=DAY
        )
        with pytest.raises(BhavcopyError, match="not a coherent bar"):
            to_daily_bars(rows, day=DAY, master={"AAA", *FILLER}, calendar=calendar)

    def test_a_stale_master(self, calendar) -> None:
        rows = parse_bhavcopy(_file(DAY), day=DAY)
        with pytest.raises(BhavcopyError, match="stale"):
            to_daily_bars(rows, day=DAY, master=set(FILLER[:100]), calendar=calendar)

    def test_an_empty_master(self, calendar) -> None:
        rows = parse_bhavcopy(_file(DAY), day=DAY)
        with pytest.raises(BhavcopyError, match="empty"):
            to_daily_bars(rows, day=DAY, master=set(), calendar=calendar)

    def test_a_file_with_no_eq_rows(self, calendar) -> None:
        rows = [_row(DAY, s, "BE") for s in FILLER]
        parsed = parse_bhavcopy(_file(DAY, rows, filler=False), day=DAY)
        with pytest.raises(BhavcopyError, match="EQ rows"):
            to_daily_bars(parsed, day=DAY, master={f"{s}-BE" for s in FILLER}, calendar=calendar)


class TestWhatIsKeptAndWhatIsNot:
    def test_a_security_that_did_not_trade_is_named_not_barred(self, calendar) -> None:
        rows = parse_bhavcopy(_file(DAY, [_row(DAY, "QUIET", TtlTradgVol="0")]), day=DAY)
        daily = to_daily_bars(rows, day=DAY, master={"QUIET", *FILLER}, calendar=calendar)
        assert daily.untraded == ("QUIET",)
        assert "QUIET" not in {b.symbol for b in daily.bars}

    def test_a_blank_trade_count_is_unknown_not_zero(self, calendar) -> None:
        rows = parse_bhavcopy(_file(DAY, [_row(DAY, "AAA", TtlNbOfTxsExctd="")]), day=DAY)
        daily = to_daily_bars(rows, day=DAY, master={"AAA", *FILLER}, calendar=calendar)
        assert next(b for b in daily.bars if b.symbol == "AAA").trade_count is None

    def test_a_vwap_outside_the_range_is_unknown(self, calendar) -> None:
        """Turnover 200,000 for 1,000 shares is Rs 200 a share on a 95-110 day."""
        rows = parse_bhavcopy(_file(DAY, [_row(DAY, "AAA", TtlTrfVal="200000.00")]), day=DAY)
        daily = to_daily_bars(rows, day=DAY, master={"AAA", *FILLER}, calendar=calendar)
        assert next(b for b in daily.bars if b.symbol == "AAA").vwap is None

    def test_control_a_sane_vwap_is_kept(self, calendar) -> None:
        rows = parse_bhavcopy(_file(DAY, [_row(DAY, "AAA")]), day=DAY)
        daily = to_daily_bars(rows, day=DAY, master={"AAA", *FILLER}, calendar=calendar)
        assert next(b for b in daily.bars if b.symbol == "AAA").vwap == Decimal("102.0000")

    def test_a_row_the_master_does_not_know_is_counted_not_stored(self, calendar) -> None:
        rows = parse_bhavcopy(_file(DAY, [_row(DAY, "SMEONLY", "SM")]), day=DAY)
        daily = to_daily_bars(rows, day=DAY, master=set(FILLER), calendar=calendar)
        assert daily.unresolved == ("SMEONLY-SM",)


# ---------------------------------------------------------------------------
# The fetch and the archive
# ---------------------------------------------------------------------------


class _Response(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200) -> None:
        super().__init__(body)
        self.status = status

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://nsearchives.nseindia.com/x", code, "x", {}, None)  # type: ignore[arg-type]


class TestTheFetch:
    def test_the_dated_url_on_nses_archive(self) -> None:
        asked: list[str] = []

        def opener(request, timeout):
            asked.append(request.full_url)
            return _Response(REAL_ZIP)

        assert fetch_bhavcopy(REAL_DAY, opener=opener) == REAL_ZIP
        assert asked == [
            "https://nsearchives.nseindia.com/content/cm/"
            "BhavCopy_NSE_CM_0_0_0_20261120_F_0000.csv.zip"
        ]

    def test_a_404_is_missing_not_refused(self) -> None:
        def opener(request, timeout):
            raise _http_error(404)

        with pytest.raises(BhavcopyMissingError):
            fetch_bhavcopy(REAL_DAY, opener=opener)

    def test_another_http_error_is_a_refusal(self) -> None:
        def opener(request, timeout):
            raise _http_error(503)

        with pytest.raises(BhavcopyError, match="503") as caught:
            fetch_bhavcopy(REAL_DAY, opener=opener)
        assert not isinstance(caught.value, BhavcopyMissingError)

    def test_an_unreachable_archive(self) -> None:
        def opener(request, timeout):
            raise urllib.error.URLError("no route")

        with pytest.raises(BhavcopyError, match="URLError"):
            fetch_bhavcopy(REAL_DAY, opener=opener)

    def test_a_200_that_is_a_page(self) -> None:
        with pytest.raises(BhavcopyError, match="not a zip"):
            fetch_bhavcopy(REAL_DAY, opener=lambda r, t: _Response(b"<html>maintenance</html>"))

    def test_an_oversized_response(self) -> None:
        body = b"PK" + b"x" * bc.MAX_ZIP_BYTES
        with pytest.raises(BhavcopyError, match="exceeds"):
            fetch_bhavcopy(REAL_DAY, opener=lambda r, t: _Response(body))


class TestTheOpenerRefuses:
    """The executed check behind the ``nosec B310`` on ``_open``."""

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "http://nsearchives.nseindia.com/content/cm/x.zip",
            "https://nsearchives.nseindia.com.evil.example/x.zip",
            "https://www.nseindia.com/content/cm/x.zip",
        ],
    )
    def test_anything_but_nses_archive(self, url: str) -> None:
        with pytest.raises(BhavcopyError, match="refusing to open"):
            bc._open(urllib.request.Request(url), 1.0)

    def test_the_source_is_on_that_origin(self) -> None:
        assert bc.SOURCE.startswith(bc._ORIGIN)


class TestTheArchive:
    def test_one_file_per_session_under_its_year(self, tmp_path: Path) -> None:
        archive = BhavcopyArchive(tmp_path)
        path = archive.write(REAL_DAY, REAL_ZIP)
        assert path == tmp_path / "2026" / "BhavCopy_NSE_CM_0_0_0_20261120_F_0000.csv.zip"
        assert archive.read(REAL_DAY) == REAL_ZIP
        assert not list(tmp_path.rglob("*.partial"))

    def test_an_absent_day_reads_none(self, tmp_path: Path) -> None:
        assert BhavcopyArchive(tmp_path).read(REAL_DAY) is None


# ---------------------------------------------------------------------------
# The loader - every outcome, with a store and a network that record calls
# ---------------------------------------------------------------------------


class Store:
    def __init__(self, master: frozenset[str] = MASTER) -> None:
        self.master = master
        self.stored: list[int] = []

    async def active_symbols(self) -> frozenset[str]:
        return self.master

    async def store(self, bars) -> int:
        self.stored.append(len(bars))
        return len(bars)


class Network:
    def __init__(self, files: dict[dt.date, bytes | Exception]) -> None:
        self.files = files
        self.asked: list[dt.date] = []

    def __call__(self, day: dt.date) -> bytes:
        self.asked.append(day)
        answer = self.files.get(day, BhavcopyMissingError(f"no file for {day}"))
        if isinstance(answer, Exception):
            raise answer
        return answer


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _loader(calendar, tmp_path, network, *, store=None, offline=False, clock=None):
    clock = clock or Clock()
    return BhavcopyLoader(
        calendar=calendar,
        archive=BhavcopyArchive(tmp_path),
        store=store or Store(),
        fetch=network,
        offline=offline,
        clock=clock,
        sleep=clock.sleep,
    )


class TestTheLoader:
    def test_a_day_without_a_session_asks_nothing(self, calendar, tmp_path) -> None:
        network = Network({})
        saturday = dt.date(2026, 11, 21)
        result = asyncio.run(_loader(calendar, tmp_path, network).sync_day(saturday))
        assert result.outcome is DayOutcome.NOT_A_TRADING_DAY and network.asked == []

    def test_a_holiday_asks_nothing(self, calendar, tmp_path) -> None:
        network = Network({})
        gandhi_jayanti = dt.date(2026, 10, 2)
        result = asyncio.run(_loader(calendar, tmp_path, network).sync_day(gandhi_jayanti))
        assert result.outcome is DayOutcome.NOT_A_TRADING_DAY and network.asked == []

    def test_the_muhurat_session_is_stood_down_like_the_rest_of_the_system(
        self, calendar, tmp_path
    ) -> None:
        """NSE publishes a file for 8 Nov 2026's Muhurat session; the calendar
        says no session for this system, so nothing is fetched."""
        network = Network({dt.date(2026, 11, 8): REAL_ZIP})
        result = asyncio.run(_loader(calendar, tmp_path, network).sync_day(dt.date(2026, 11, 8)))
        assert result.outcome is DayOutcome.NOT_A_TRADING_DAY and network.asked == []

    def test_a_session_from_the_network_is_stored_and_archived(self, calendar, tmp_path) -> None:
        network, store = Network({REAL_DAY: REAL_ZIP}), Store()
        result = asyncio.run(_loader(calendar, tmp_path, network, store=store).sync_day(REAL_DAY))
        assert (result.outcome, result.source, result.bars) == (DayOutcome.STORED, "network", 3722)
        assert store.stored == [3722]
        assert BhavcopyArchive(tmp_path).read(REAL_DAY) == REAL_ZIP

    def test_an_archived_session_is_not_fetched_again(self, calendar, tmp_path) -> None:
        BhavcopyArchive(tmp_path).write(REAL_DAY, REAL_ZIP)
        network = Network({})
        result = asyncio.run(_loader(calendar, tmp_path, network).sync_day(REAL_DAY))
        assert (result.outcome, result.source) == (DayOutcome.STORED, "archive")
        assert network.asked == []

    def test_a_refused_file_is_neither_stored_nor_archived(self, calendar, tmp_path) -> None:
        network, store = Network({REAL_DAY: b"<html>oops</html>"}), Store()
        result = asyncio.run(_loader(calendar, tmp_path, network, store=store).sync_day(REAL_DAY))
        assert result.outcome is DayOutcome.REFUSED and "not a zip" in result.detail
        assert store.stored == [] and BhavcopyArchive(tmp_path).read(REAL_DAY) is None

    def test_a_missing_session_is_reported_not_skipped(self, calendar, tmp_path) -> None:
        network, store = Network({}), Store()
        result = asyncio.run(_loader(calendar, tmp_path, network, store=store).sync_day(REAL_DAY))
        assert result.outcome is DayOutcome.MISSING and store.stored == []

    def test_offline_never_touches_the_network(self, calendar, tmp_path) -> None:
        network = Network({REAL_DAY: REAL_ZIP})
        result = asyncio.run(_loader(calendar, tmp_path, network, offline=True).sync_day(REAL_DAY))
        assert result.outcome is DayOutcome.MISSING and network.asked == []

    def test_fetches_are_spaced_a_second_apart(self, calendar, tmp_path) -> None:
        clock = Clock()
        days = [dt.date(2026, 11, 18), dt.date(2026, 11, 19), REAL_DAY]
        network = Network({d: _file(d) for d in days})
        loader = _loader(calendar, tmp_path, network, store=Store(frozenset(FILLER)), clock=clock)
        for d in days:
            asyncio.run(loader.sync_day(d))
        assert network.asked == days
        assert clock.slept == [1.0, 1.0]

    def test_a_faster_client_is_refused_at_construction(self, calendar, tmp_path) -> None:
        with pytest.raises(ValueError, match="courtesy"):
            BhavcopyLoader(
                calendar=calendar,
                archive=BhavcopyArchive(tmp_path),
                store=Store(),
                min_interval=0.5,
            )


class TestTheBackfill:
    def test_only_sessions_are_asked_for_and_gaps_are_named(self, calendar, tmp_path) -> None:
        """16-23 Nov 2026: five sessions (the 21st and 22nd are a weekend); the
        19th has no file."""
        sessions = [dt.date(2026, 11, d) for d in (16, 17, 18, 20, 23)]
        network = Network({d: _file(d) for d in sessions})
        loader = _loader(calendar, tmp_path, network, store=Store(frozenset(FILLER)))
        report = asyncio.run(loader.backfill(dt.date(2026, 11, 16), dt.date(2026, 11, 23)))
        assert network.asked == sorted([*sessions, dt.date(2026, 11, 19)])
        assert [r.day for r in report.of(DayOutcome.MISSING)] == [dt.date(2026, 11, 19)]
        assert len(report.of(DayOutcome.STORED)) == 5
        assert not report.ok

    def test_a_rerun_reads_the_archive_and_fetches_nothing(self, calendar, tmp_path) -> None:
        sessions = [dt.date(2026, 11, d) for d in (16, 17, 18)]
        store = Store(frozenset(FILLER))
        first = _loader(calendar, tmp_path, Network({d: _file(d) for d in sessions}), store=store)
        assert asyncio.run(first.backfill(sessions[0], sessions[-1])).ok
        network = Network({})
        again = _loader(calendar, tmp_path, network, store=store, offline=True)
        report = asyncio.run(again.backfill(sessions[0], sessions[-1]))
        assert report.ok and network.asked == []
        assert {r.source for r in report.results} == {"archive"}

    def test_a_year_the_calendar_does_not_cover_is_refused_before_any_request(
        self, calendar, tmp_path
    ) -> None:
        network = Network({})
        loader = _loader(calendar, tmp_path, network)
        with pytest.raises(HolidayDataError):
            asyncio.run(loader.backfill(dt.date(2025, 12, 1), dt.date(2026, 1, 9)))
        assert network.asked == []

    def test_an_inverted_range_is_refused(self, calendar, tmp_path) -> None:
        loader = _loader(calendar, tmp_path, Network({}))
        with pytest.raises(ValueError):
            asyncio.run(loader.backfill(dt.date(2026, 11, 20), dt.date(2026, 11, 16)))


@pytest.fixture(scope="module")
def script():
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "scripts" / "backfill_bhavcopy.py"
    spec = importlib.util.spec_from_file_location("backfill_bhavcopy", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTheCommand:
    def test_no_database_is_a_usage_error(self, script, monkeypatch) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(SystemExit) as caught:
            script.main(["--start", "2026-11-16", "--end", "2026-11-20"])
        assert caught.value.code == 2

    def test_an_inverted_range_is_a_usage_error(self, script) -> None:
        with pytest.raises(SystemExit) as caught:
            script.main(["--start", "2026-11-20", "--end", "2026-11-16", "--database-url", "x"])
        assert caught.value.code == 2

    def test_a_date_that_is_not_one(self, script) -> None:
        with pytest.raises(SystemExit):
            script.main(["--start", "20-11-2026", "--end", "2026-11-20", "--database-url", "x"])

    def test_a_password_on_the_command_line_is_refused(self, script, capsys) -> None:
        """A command line is readable by every process on the host. Assembled at
        runtime so no scanner reads the fixture as a committed credential."""
        secret = "s3cret" + "-for-test"
        url = "postgresql+psycopg://svc:" + secret + "@localhost/algotrader"
        with pytest.raises(SystemExit) as caught:
            script.main(["--start", "2026-11-16", "--end", "2026-11-20", "--database-url", url])
        assert caught.value.code == 2
        err = capsys.readouterr().err
        assert "DATABASE_URL" in err and secret not in err

    def test_control_a_url_without_a_password_passes_the_guard(self, script, capsys) -> None:
        url = "postgresql+psycopg://svc@localhost/algotrader"
        with pytest.raises(SystemExit):
            script.main(["--start", "2026-11-20", "--end", "2026-11-16", "--database-url", url])
        err = capsys.readouterr().err
        assert "carries a password" not in err and "before --start" in err
