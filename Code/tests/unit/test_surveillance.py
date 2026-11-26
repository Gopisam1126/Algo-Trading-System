"""E04-S01 and S02: NSE's surveillance roster, parsed strictly and synced per
trade date - ASM, GSM and T2T.

Every refusal is tested against the REAL roster with one thing changed, so the
check under test is the only thing that can refuse (the real file passing is
each test's control). The circular-backed facts - which stocks NSE put under
ASM w.e.f. 23 and 25 Nov 2026, and w.e.f. 2 Feb after the Budget session - are
asserted against the real files, as are the two series moves NSE made for
9 Nov in the roster it wrote after Muhurat.
"""

from __future__ import annotations

import datetime as dt
import gzip
import io
import logging
import urllib.error
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from algotrader.common.calendar import HolidayDataError, MarketCalendar, load_holidays_with_status
from algotrader.common.enums import SurveillanceCategory
from algotrader.premarket import surveillance as sv
from algotrader.premarket.surveillance import (
    Fetched,
    RosterArchive,
    RosterError,
    RosterMissingError,
    RosterRow,
    SurveillanceSync,
    SyncOutcome,
    check_publication,
    fetch_roster,
    parse_roster,
    resolve_roster,
)

CODE = Path(__file__).resolve().parents[2]
FIXTURES = CODE / "tests" / "fixtures"
NOV20, NOV23, NOV25 = dt.date(2026, 11, 20), dt.date(2026, 11, 23), dt.date(2026, 11, 25)
NOV24 = dt.date(2026, 11, 24)  # Guru Nanak Jayanti: no session
#: Fri 30 Jan, the Budget session on Sun 1 Feb (NSE/CMTR/72349), Mon 2 Feb.
JAN30, FEB01, FEB02 = dt.date(2026, 1, 30), dt.date(2026, 2, 1), dt.date(2026, 2, 2)
#: Fri 6 Nov, Muhurat on Sun 8 Nov, Mon 9 Nov.
NOV06, NOV08, NOV09 = dt.date(2026, 11, 6), dt.date(2026, 11, 8), dt.date(2026, 11, 9)


def _real(name: str) -> bytes:
    return gzip.decompress((FIXTURES / "surveillance" / name).read_bytes())


ROSTER_20 = _real("REG1_IND201126.csv.gz")
ROSTER_23 = _real("REG1_IND231126.csv.gz")
ROSTER_JAN30 = _real("REG1_IND300126.csv.gz")
ROSTER_FEB01 = _real("REG1_IND010226.csv.gz")
ROSTER_NOV06 = _real("REG1_IND061126.csv.gz")
ROSTER_NOV08 = _real("REG1_IND081126.csv.gz")
#: When NSE wrote them (their Last-Modified headers, read 24 and 25 Nov 2026).
WRITTEN_20 = dt.datetime(2026, 11, 20, 13, 54, 4, tzinfo=dt.UTC)
WRITTEN_23 = dt.datetime(2026, 11, 23, 14, 40, 33, tzinfo=dt.UTC)
WRITTEN_JAN30 = dt.datetime(2026, 1, 30, 14, 55, 4, tzinfo=dt.UTC)
WRITTEN_FEB01 = dt.datetime(2026, 2, 1, 13, 20, 33, tzinfo=dt.UTC)  # 18:50 IST, a Sunday
WRITTEN_NOV06 = dt.datetime(2026, 11, 6, 14, 35, 5, tzinfo=dt.UTC)
WRITTEN_NOV08 = dt.datetime(2026, 11, 8, 13, 39, 3, tzinfo=dt.UTC)  # 19:09 IST, a Sunday
#: Every roster NSE wrote around those days, as its archive serves them.
NSE_2026 = {
    JAN30: Fetched(ROSTER_JAN30, WRITTEN_JAN30),
    FEB01: Fetched(ROSTER_FEB01, WRITTEN_FEB01),
    NOV06: Fetched(ROSTER_NOV06, WRITTEN_NOV06),
    NOV08: Fetched(ROSTER_NOV08, WRITTEN_NOV08),
    NOV20: Fetched(ROSTER_20, WRITTEN_20),
    NOV23: Fetched(ROSTER_23, WRITTEN_23),
}
#: A wall clock after every day these tests sync: each closed day is over.
AFTER = dt.datetime(2026, 12, 1, tzinfo=dt.UTC)
MASTER = frozenset(
    line
    for line in (FIXTURES / "universe" / "nse_eq_symbols_20261122.txt")
    .read_text("utf-8")
    .splitlines()
    if line and not line.startswith("#")
)


@pytest.fixture(scope="module")
def calendar() -> MarketCalendar:
    status = load_holidays_with_status(str(CODE / "config" / "nse_holidays.yaml"))
    return MarketCalendar(status.dates, covers_years=status.covers_years)


def _edit(raw: bytes, *, header: Any = None, row: Any = None, symbol: str = "INFY") -> bytes:
    """The real roster with its header, or one named symbol's row, changed."""
    lines = raw.decode("utf-8").split("\n")
    fields = lines[0].split(",")
    if header is not None:
        lines[0] = ",".join(header(fields))
    if row is not None:
        at = next(i for i, ln in enumerate(lines) if ln.split(",")[1:2] == [symbol])
        cells = lines[at].split(",")
        lines[at] = ",".join(row(dict(zip(fields, cells, strict=True)), cells, fields))
    return "\n".join(lines).encode("utf-8")


def _set(column: str, value: str) -> Any:
    def change(named: dict[str, str], cells: list[str], fields: list[str]) -> list[str]:
        cells = list(cells)
        cells[fields.index(column)] = value
        return cells

    return change


def _by_symbol(rows: Sequence[RosterRow]) -> dict[str, RosterRow]:
    return {r.symbol: r for r in rows}


# ---------------------------------------------------------------------------
# The real rosters
# ---------------------------------------------------------------------------


class TestTheRealRosters:
    def test_the_23_nov_roster_parses_whole(self) -> None:
        rows = parse_roster(ROSTER_23, session=NOV23)
        assert len(rows) == 3590
        assert sum(r.is_asm for r in rows) == 168
        assert sum(r.is_gsm for r in rows) == 73

    def test_what_nse_put_under_asm_from_25_nov_is_in_the_23_nov_roster(self) -> None:
        """NSE/SURV/77570 and 77569 (23 Nov 2026): ASPIRE and HINDADH into
        ST-ASM, EMPOWER into LT-ASM, JYOTISTRUC, ORCHASP, SOTAC and SPCENET out
        - all "w.e.f. November 25". The 23 Nov roster carries all of it."""
        rows = _by_symbol(parse_roster(ROSTER_23, session=NOV23))
        assert rows["ASPIRE"].st_asm_stage == 1
        assert rows["ASPIRE"].asm_category is SurveillanceCategory.SHORT_TERM
        assert rows["HINDADH"].st_asm_stage == 1
        assert rows["EMPOWER"].lt_asm_stage == 1
        assert rows["EMPOWER"].asm_category is SurveillanceCategory.LONG_TERM
        for gone in ("JYOTISTRUC", "ORCHASP", "SOTAC", "SPCENET"):
            assert not rows[gone].is_asm, gone

    def test_the_20_nov_roster_governs_23_nov_not_25(self) -> None:
        """NSE/SURV/77539 (20 Nov 2026) put APEXECO, CADSYS and QLINE into
        ST-ASM w.e.f. 23 Nov: the 20 Nov roster has them, and does not yet have
        what applies from 25 Nov."""
        rows = _by_symbol(parse_roster(ROSTER_20, session=NOV20))
        for added in ("APEXECO", "CADSYS", "QLINE"):
            assert rows[added].st_asm_stage == 1, added
        for later in ("ASPIRE", "HINDADH", "EMPOWER"):
            assert not rows[later].is_asm, later
        assert rows["JYOTISTRUC"].is_asm  # it leaves on 25 Nov, not before

    def test_gsm_stage_0_is_a_stage_not_an_absence(self) -> None:
        """Stage 0 is "shortlisted under GSM". A truthiness test on the stage
        would read it as not in GSM - 67 stocks on 23 Nov 2026."""
        rows = parse_roster(ROSTER_23, session=NOV23)
        zero = [r for r in rows if r.gsm_stage == 0]
        assert len(zero) == 67
        assert all(r.is_gsm and r.flagged for r in zero)

    def test_a_stock_under_two_measures_carries_both(self) -> None:
        """AQYLON is LT-ASM stage I and GSM stage 0; NSE's ASM report omitted it."""
        aqylon = _by_symbol(parse_roster(ROSTER_23, session=NOV23))["AQYLON"]
        assert (aqylon.lt_asm_stage, aqylon.gsm_stage) == (1, 0)

    def test_a_clean_stock_is_clean(self) -> None:
        infy = _by_symbol(parse_roster(ROSTER_23, session=NOV23))["INFY"]
        assert not infy.flagged and infy.asm_category is None and infy.gsm_stage is None

    def test_the_kite_name_carries_the_series(self) -> None:
        rows = _by_symbol(parse_roster(ROSTER_23, session=NOV23))
        assert rows["INFY"].tradingsymbol == "INFY"
        assert rows["ASPIRE"].tradingsymbol == "ASPIRE-SM"


# ---------------------------------------------------------------------------
# Strictness: one thing changed in the real file, and it is refused
# ---------------------------------------------------------------------------


class TestTheHeader:
    def test_a_repurposed_filler_column_is_accepted(self) -> None:
        """NSE renamed Filler12 to an ETF alert during 2026; the columns read
        are asserted by name, the rest are not."""
        raw = _edit(
            ROSTER_23, header=lambda f: ["Something New" if c == "Filler4" else c for c in f]
        )
        assert len(parse_roster(raw, session=NOV23)) == 3590

    @pytest.mark.parametrize("column", sv.REQUIRED)
    def test_a_renamed_column_that_is_read_refuses(self, column: str) -> None:
        raw = _edit(ROSTER_23, header=lambda f: [c + "X" if c == column else c for c in f])
        with pytest.raises(RosterError, match="0 times, not once"):
            parse_roster(raw, session=NOV23)

    def test_a_column_read_twice_refuses(self) -> None:
        raw = _edit(ROSTER_23, header=lambda f: [sv.GSM if c == "Filler4" else c for c in f])
        with pytest.raises(RosterError, match="2 times, not once"):
            parse_roster(raw, session=NOV23)


class TestTheRows:
    @pytest.mark.parametrize(
        ("column", "value"),
        [
            (sv.GSM, "7"),
            (sv.GSM, "99"),
            (sv.GSM, "-1"),
            (sv.GSM, ""),
            (sv.LT_ASM, "0"),
            (sv.LT_ASM, "5"),
            (sv.ST_ASM, "3"),
            (sv.ST_ASM, "1.0"),
        ],
    )
    def test_a_stage_this_code_does_not_know_refuses(self, column: str, value: str) -> None:
        with pytest.raises(RosterError, match="not a stage this code knows"):
            parse_roster(_edit(ROSTER_23, row=_set(column, value)), session=NOV23)

    @pytest.mark.parametrize(
        ("column", "value"), [(sv.GSM, "6"), (sv.LT_ASM, "4"), (sv.ST_ASM, "2")]
    )
    def test_the_highest_known_stage_is_accepted(self, column: str, value: str) -> None:
        infy = _by_symbol(parse_roster(_edit(ROSTER_23, row=_set(column, value)), session=NOV23))
        assert infy["INFY"].flagged

    def test_an_unknown_status_refuses(self) -> None:
        with pytest.raises(RosterError, match="status 'X'"):
            parse_roster(_edit(ROSTER_23, row=_set(sv.STATUS, "X")), session=NOV23)

    def test_a_suspended_security_parses(self) -> None:
        assert _by_symbol(parse_roster(ROSTER_23, session=NOV23))["AURIGROW"].status == "S"

    def test_an_inactive_security_parses(self) -> None:
        """NSE's file structure lists A, S and I; refusing I would refuse a file
        the publisher calls well-formed - and stop every trade that day."""
        raw = _edit(ROSTER_23, row=_set(sv.STATUS, "I"))
        assert _by_symbol(parse_roster(raw, session=NOV23))["INFY"].status == "I"

    def test_a_bad_series_refuses(self) -> None:
        with pytest.raises(RosterError, match="not a series code"):
            parse_roster(_edit(ROSTER_23, row=_set(sv.SERIES, "eq")), session=NOV23)

    def test_a_short_row_refuses(self) -> None:
        raw = _edit(ROSTER_23, row=lambda named, cells, fields: cells[:-1])
        with pytest.raises(RosterError, match="62 fields, the header 63"):
            parse_roster(raw, session=NOV23)

    def test_a_symbol_listed_twice_refuses(self) -> None:
        raw = _edit(ROSTER_23, row=_set(sv.SYMBOL, "TCS"))
        with pytest.raises(RosterError, match="'TCS' is listed twice"):
            parse_roster(raw, session=NOV23)

    def test_a_symbol_that_could_forge_a_log_line_refuses_without_echoing_it(self) -> None:
        raw = _edit(ROSTER_23, row=_set(sv.SYMBOL, '"INFY\nCRITICAL kill switch disarmed"'))
        with pytest.raises(RosterError) as caught:
            parse_roster(raw, session=NOV23)
        assert "\n" not in str(caught.value)

    def test_a_blank_line_refuses(self) -> None:
        raw = ROSTER_23.replace(b"\n", b"\n\n", 1)
        with pytest.raises(RosterError, match="0 fields"):
            parse_roster(raw, session=NOV23)

    def test_a_truncated_roster_refuses(self) -> None:
        cut = b"\n".join(ROSTER_23.split(b"\n")[: sv.MIN_ROWS])  # header + MIN_ROWS - 1
        with pytest.raises(RosterError, match=f"lists {sv.MIN_ROWS - 1} securities"):
            parse_roster(cut, session=NOV23)

    def test_exactly_the_floor_is_accepted(self) -> None:
        cut = b"\n".join(ROSTER_23.split(b"\n")[: sv.MIN_ROWS + 1])
        assert len(parse_roster(cut, session=NOV23)) == sv.MIN_ROWS


def _keep_only(raw: bytes, column: str, keep: int) -> bytes:
    """The real roster with all but ``keep`` of ``column``'s flags cleared."""
    lines = raw.decode("utf-8").split("\n")
    at = lines[0].split(",").index(column)
    kept = 0
    for i, line in enumerate(lines[1:], start=1):
        cells = line.split(",")
        if len(cells) > at and cells[at] != sv.NOT_APPLICABLE:
            if kept < keep:
                kept += 1
            else:
                cells[at] = sv.NOT_APPLICABLE
                lines[i] = ",".join(cells)
    return "\n".join(lines).encode("utf-8")


class TestAnEmptyDenyList:
    """SPRINT02_TECHNICAL_SPEC 5.1: an empty deny list reads as "no stock is
    under surveillance today" - so a roster naming (almost) nobody under one of
    its measures is refused, not written clean."""

    @pytest.mark.parametrize("column", [sv.LT_ASM, sv.ST_ASM, sv.GSM])
    def test_a_list_that_names_fewer_than_a_handful_refuses(self, column: str) -> None:
        raw = _keep_only(ROSTER_23, column, sv.MIN_FLAGGED - 1)
        with pytest.raises(RosterError, match="a deny list that names nobody"):
            parse_roster(raw, session=NOV23)

    @pytest.mark.parametrize("column", [sv.LT_ASM, sv.ST_ASM, sv.GSM])
    def test_exactly_a_handful_is_accepted(self, column: str) -> None:
        assert parse_roster(_keep_only(ROSTER_23, column, sv.MIN_FLAGGED), session=NOV23)


# ---------------------------------------------------------------------------
# T2T (E04-S02): the series a security trades in from the next session
# ---------------------------------------------------------------------------


def _relabel(raw: bytes, series: str, keep: int, to: str) -> bytes:
    """The real roster with all but ``keep`` of ``series``'s rows moved to ``to``."""
    lines = raw.decode("utf-8").split("\n")
    at = lines[0].split(",").index(sv.SERIES)
    kept = 0
    for i, line in enumerate(lines[1:], start=1):
        cells = line.split(",")
        if len(cells) > at and cells[at] == series:
            if kept < keep:
                kept += 1
            else:
                cells[at] = to
                lines[i] = ",".join(cells)
    return "\n".join(lines).encode("utf-8")


class TestTradeForTrade:
    def test_every_series_the_real_roster_carries_is_classified(self) -> None:
        """NSE's legend of series: EQ and SM settle rolling; BE, BZ, ST and SZ
        trade for trade. The 23 Nov roster carries those six and nothing else."""
        rows = parse_roster(ROSTER_23, session=NOV23)
        assert {r.series for r in rows if r.is_t2t is True} == {"BE", "BZ", "ST", "SZ"}
        assert {r.series for r in rows if r.is_t2t is False} == {"EQ", "SM"}
        assert all(r.is_t2t is not None for r in rows)
        assert sum(r.is_t2t is True for r in rows) == 184 + 41 + 102 + 2

    @pytest.mark.parametrize(
        ("symbol", "series", "kite"),
        [
            ("ABAN", "BE", "ABAN-BE"),
            ("ANSALAPI", "BZ", "ANSALAPI-BZ"),
            ("AARADHYA", "ST", "AARADHYA-ST"),
            ("SECL", "SZ", "SECL-SZ"),  # suspended - and still trade-for-trade
        ],
    )
    def test_a_trade_for_trade_stock_is_t2t_under_its_kite_name(
        self, symbol: str, series: str, kite: str
    ) -> None:
        row = _by_symbol(parse_roster(ROSTER_23, session=NOV23))[symbol]
        assert (row.series, row.tradingsymbol, row.is_t2t) == (series, kite, True)

    @pytest.mark.parametrize(("symbol", "kite"), [("INFY", "INFY"), ("ASPIRE", "ASPIRE-SM")])
    def test_a_rolling_stock_is_not_t2t(self, symbol: str, kite: str) -> None:
        row = _by_symbol(parse_roster(ROSTER_23, session=NOV23))[symbol]
        assert (row.tradingsymbol, row.is_t2t) == (kite, False)

    def test_a_series_this_code_does_not_know_is_unknown_not_clean(self) -> None:
        """RR (a REIT's series) never appears in the roster; if it did, the
        flag would be NULL - refused by every reader - not written False."""
        rows = _by_symbol(parse_roster(_edit(ROSTER_23, row=_set(sv.SERIES, "RR")), session=NOV23))
        assert rows["INFY"].is_t2t is None

    @pytest.mark.parametrize(("series", "to"), [("BE", "EQ"), ("BZ", "EQ"), ("ST", "SM")])
    def test_a_trade_for_trade_list_that_names_fewer_than_a_handful_refuses(
        self, series: str, to: str
    ) -> None:
        """SPRINT02 5.1 again: a roster whose BE list has (all but) vanished into
        EQ would write every one of those stocks clean."""
        raw = _relabel(ROSTER_23, series, sv.MIN_FLAGGED - 1, to)
        with pytest.raises(RosterError, match=f"the trade-for-trade series {series}, under"):
            parse_roster(raw, session=NOV23)

    @pytest.mark.parametrize(("series", "to"), [("BE", "EQ"), ("BZ", "EQ"), ("ST", "SM")])
    def test_exactly_a_handful_is_accepted(self, series: str, to: str) -> None:
        raw = _relabel(ROSTER_23, series, sv.MIN_FLAGGED, to)
        rows = parse_roster(raw, session=NOV23)
        assert sum(r.series == series for r in rows) == sv.MIN_FLAGGED

    def test_sz_carries_no_floor(self) -> None:
        """SZ named one or two securities all through 2026: a floor would
        refuse real files."""
        rows = parse_roster(_relabel(ROSTER_23, "SZ", 0, "SM"), session=NOV23)
        assert not any(r.series == "SZ" for r in rows)


class TestTheFileItself:
    def test_an_oversized_response_refuses(self) -> None:
        with pytest.raises(RosterError, match="exceeds"):
            parse_roster(b"x" * (sv.MAX_FILE_BYTES + 1), session=NOV23)

    def test_text_that_is_not_utf8_refuses(self) -> None:
        with pytest.raises(RosterError, match="not UTF-8"):
            parse_roster(b"\xff\xfe" + ROSTER_23, session=NOV23)

    def test_an_empty_response_refuses(self) -> None:
        with pytest.raises(RosterError, match="empty"):
            parse_roster(b"", session=NOV23)

    def test_an_html_page_refuses(self) -> None:
        with pytest.raises(RosterError, match="0 times, not once"):
            parse_roster(b"<html><body>Access Denied</body></html>\n", session=NOV23)

    def test_a_byte_order_mark_is_tolerated(self) -> None:
        assert len(parse_roster(b"\xef\xbb\xbf" + ROSTER_23, session=NOV23)) == 3590


# ---------------------------------------------------------------------------
# Resolution against the master
# ---------------------------------------------------------------------------


class TestResolution:
    def test_the_real_roster_resolves_against_kites_list(self) -> None:
        resolved = resolve_roster(parse_roster(ROSTER_23, session=NOV23), master=MASTER)
        assert len(resolved.rows) > 3500
        assert "INFY" in {r.tradingsymbol for r in resolved.rows}
        assert set(resolved.unresolved).isdisjoint(MASTER)

    def test_a_master_that_knows_too_few_eq_rows_refuses(self) -> None:
        rows = parse_roster(ROSTER_23, session=NOV23)
        eq = sorted(r.tradingsymbol for r in rows if r.series == "EQ")
        keep = frozenset(eq[: int(len(eq) * 0.79)])
        with pytest.raises(RosterError, match="master is stale or the file is wrong"):
            resolve_roster(rows, master=keep)

    def test_the_floor_itself_is_accepted(self) -> None:
        rows = parse_roster(ROSTER_23, session=NOV23)
        eq = sorted(r.tradingsymbol for r in rows if r.series == "EQ")
        keep = frozenset(eq[: -(-len(eq) * 8 // 10)])  # ceil(0.8 * n)
        assert resolve_roster(rows, master=keep).rows

    def test_a_roster_with_no_eq_rows_refuses(self) -> None:
        rows = [r for r in parse_roster(ROSTER_23, session=NOV23) if r.series != "EQ"]
        with pytest.raises(RosterError, match="only 0 of 0 EQ rows"):
            resolve_roster(rows, master=MASTER)


# ---------------------------------------------------------------------------
# When NSE wrote it
# ---------------------------------------------------------------------------


class TestThePublicationWindow:
    def test_the_real_evening_files_are_in_their_window(self) -> None:
        check_publication(WRITTEN_20, session=NOV20, governs=NOV23)
        check_publication(WRITTEN_23, session=NOV23, governs=NOV25)

    def test_no_last_modified_refuses(self) -> None:
        with pytest.raises(RosterError, match="age is unknown"):
            check_publication(None, session=NOV23, governs=NOV25)

    def test_a_file_written_before_the_session_closed_refuses(self) -> None:
        """A morning file under the evening file's name would miss the
        evening's notices - the overnight additions this story exists for."""
        at_close = dt.datetime(2026, 11, 23, 10, 0, tzinfo=dt.UTC)  # 15:30 IST
        with pytest.raises(RosterError, match="not the evening file"):
            check_publication(at_close, session=NOV23, governs=NOV25)
        check_publication(at_close + dt.timedelta(seconds=1), session=NOV23, governs=NOV25)

    def test_a_file_written_after_the_session_it_governs_opened_refuses(self) -> None:
        at_open = dt.datetime(2026, 11, 25, 3, 45, tzinfo=dt.UTC)  # 09:15 IST
        with pytest.raises(RosterError, match="not the evening file"):
            check_publication(at_open, session=NOV23, governs=NOV25)
        check_publication(at_open - dt.timedelta(seconds=1), session=NOV23, governs=NOV25)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, body: bytes, status: int = 200, modified: str | None = None) -> None:
        self._body = body
        self.status = status
        self.headers = {"Last-Modified": modified} if modified else {}

    def read(self, limit: int) -> bytes:
        return self._body[:limit]

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class TestFetching:
    def test_the_url_names_the_session_and_last_modified_is_read(self) -> None:
        seen: list[str] = []

        def opener(request: Any, timeout: float) -> _Response:
            seen.append(request.full_url)
            return _Response(ROSTER_23, modified="Mon, 23 Nov 2026 14:40:33 GMT")

        fetched = fetch_roster(NOV23, opener=opener)
        assert seen == ["https://nsearchives.nseindia.com/content/cm/REG1_IND231126.csv"]
        assert fetched.body == ROSTER_23 and fetched.last_modified == WRITTEN_23

    def test_a_404_is_missing(self) -> None:
        def opener(request: Any, timeout: float) -> _Response:
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", None, None)  # type: ignore[arg-type]

        with pytest.raises(RosterMissingError, match="HTTP 404"):
            fetch_roster(NOV23, opener=opener)

    def test_another_http_error_is_refused_not_missing(self) -> None:
        def opener(request: Any, timeout: float) -> _Response:
            raise urllib.error.HTTPError(request.full_url, 503, "Busy", None, None)  # type: ignore[arg-type]

        with pytest.raises(RosterError, match="HTTP 503") as caught:
            fetch_roster(NOV23, opener=opener)
        assert not isinstance(caught.value, RosterMissingError)

    def test_a_network_failure_is_refused(self) -> None:
        def opener(request: Any, timeout: float) -> _Response:
            raise urllib.error.URLError("unreachable")

        with pytest.raises(RosterError, match="could not be fetched"):
            fetch_roster(NOV23, opener=opener)

    def test_an_oversized_body_is_refused(self) -> None:
        def opener(request: Any, timeout: float) -> _Response:
            return _Response(b"x" * (sv.MAX_FILE_BYTES + 10))

        with pytest.raises(RosterError, match="exceeds"):
            fetch_roster(NOV23, opener=opener)

    def test_an_unparseable_last_modified_reads_as_none(self) -> None:
        def opener(request: Any, timeout: float) -> _Response:
            return _Response(ROSTER_23, modified="yesterday-ish")

        assert fetch_roster(NOV23, opener=opener).last_modified is None

    def test_the_opener_refuses_another_origin(self) -> None:
        import urllib.request

        with pytest.raises(RosterError, match="not https://nsearchives"):
            sv._open(urllib.request.Request("https://example.com/REG1_IND231126.csv"), 1.0)


# ---------------------------------------------------------------------------
# The sync: one trade date at a time
# ---------------------------------------------------------------------------


class _Store:
    def __init__(self, master: frozenset[str] = MASTER) -> None:
        self.master = master
        self.writes: list[tuple[dt.date, tuple[RosterRow, ...]]] = []

    async def active_symbols(self) -> frozenset[str]:
        return self.master

    async def write(self, trade_date: dt.date, rows: Sequence[RosterRow]) -> int:
        self.writes.append((trade_date, tuple(rows)))
        return len(rows)


class _Fetch:
    """Serves the real rosters by session, as NSE does, and records each ask."""

    def __init__(self, by_session: dict[dt.date, Fetched] | None = None) -> None:
        self.by_session = (
            by_session
            if by_session is not None
            else {NOV20: Fetched(ROSTER_20, WRITTEN_20), NOV23: Fetched(ROSTER_23, WRITTEN_23)}
        )
        self.asked: list[dt.date] = []

    def __call__(self, session: dt.date) -> Fetched:
        self.asked.append(session)
        if session not in self.by_session:
            raise RosterMissingError(f"NSE has no surveillance roster for {session} (HTTP 404)")
        return self.by_session[session]


async def _no_sleep(seconds: float) -> None:
    return None


def _sync(calendar: MarketCalendar, tmp_path: Path, **kw: Any) -> SurveillanceSync:
    kw.setdefault("store", _Store())
    kw.setdefault("fetch", _Fetch())
    kw.setdefault("sleep", _no_sleep)
    kw.setdefault("now", lambda: AFTER)
    return SurveillanceSync(calendar=calendar, archive=RosterArchive(tmp_path), **kw)


class TestOneTradeDate:
    async def test_a_trade_date_reads_the_roster_of_the_session_before_it(
        self, calendar: MarketCalendar, tmp_path: Path
    ) -> None:
        """25 Nov 2026 follows a holiday. NSE could have held a session on the
        24th (it did on two Sundays this year), so the 24th is asked first; it
        has no roster, so the 23rd's governs."""
        fetch, store = _Fetch(), _Store()
        result = await _sync(calendar, tmp_path, fetch=fetch, store=store).sync_day(NOV25)
        assert fetch.asked == [NOV24, NOV23]
        assert result.outcome is SyncOutcome.STORED and result.roster_session == NOV23
        written = {r.tradingsymbol: r for r in store.writes[0][1]}
        assert store.writes[0][0] == NOV25
        assert written["EMPOWER"].is_asm and written["ASPIRE-SM"].is_asm
        assert not written["INFY"].flagged
        # Counts are of what was WRITTEN - the instruments the master knows.
        assert result.written == len(written)
        assert result.asm == sum(r.is_asm for r in written.values())
        assert result.gsm == sum(r.is_gsm for r in written.values())
        assert result.unresolved == 3590 - len(written)

    async def test_what_applies_from_25_nov_is_not_written_for_23_nov(
        self, calendar: MarketCalendar, tmp_path: Path
    ) -> None:
        store = _Store()
        await _sync(calendar, tmp_path, store=store).sync_day(NOV23)
        written = {r.tradingsymbol: r for r in store.writes[0][1]}
        assert not written["EMPOWER"].is_asm and written["CADSYS-SM"].is_asm

    async def test_a_day_with_no_session_requests_nothing(
        self, calendar: MarketCalendar, tmp_path: Path
    ) -> None:
        fetch, store = _Fetch(), _Store()
        holiday = dt.date(2026, 11, 24)
        result = await _sync(calendar, tmp_path, fetch=fetch, store=store).sync_day(holiday)
        assert result.outcome is SyncOutcome.NOT_A_TRADING_DAY
        assert fetch.asked == [] and store.writes == []

    async def test_a_missing_roster_writes_nothing_and_archives_nothing(
        self, calendar: MarketCalendar, tmp_path: Path
    ) -> None:
        """AC2: no roster, no write - so the day's flags stay unknown and
        every reader refuses every symbol, rather than reading them clean."""
        store = _Store()
        result = await _sync(calendar, tmp_path, fetch=_Fetch({}), store=store).sync_day(NOV25)
        assert result.outcome is SyncOutcome.MISSING and "HTTP 404" in result.detail
        assert store.writes == [] and RosterArchive(tmp_path).read(NOV23) is None

    async def test_a_roster_written_at_the_wrong_time_is_refused_and_not_archived(
        self, calendar: MarketCalendar, tmp_path: Path
    ) -> None:
        morning = dt.datetime(2026, 11, 23, 2, 0, tzinfo=dt.UTC)  # 07:30 IST on the 23rd
        store = _Store()
        fetch = _Fetch({NOV23: Fetched(ROSTER_23, morning)})
        result = await _sync(calendar, tmp_path, fetch=fetch, store=store).sync_day(NOV25)
        assert result.outcome is SyncOutcome.REFUSED and "not the evening file" in result.detail
        assert store.writes == [] and RosterArchive(tmp_path).read(NOV23) is None

    async def test_a_roster_that_does_not_parse_is_refused_and_not_archived(
        self, calendar: MarketCalendar, tmp_path: Path
    ) -> None:
        store = _Store()
        bad = Fetched(_edit(ROSTER_23, row=_set(sv.GSM, "9")), WRITTEN_23)
        result = await _sync(calendar, tmp_path, fetch=_Fetch({NOV23: bad}), store=store).sync_day(
            NOV25
        )
        assert result.outcome is SyncOutcome.REFUSED and "not a stage" in result.detail
        assert store.writes == [] and RosterArchive(tmp_path).read(NOV23) is None

    async def test_a_stale_master_is_refused(
        self, calendar: MarketCalendar, tmp_path: Path
    ) -> None:
        store = _Store(master=frozenset({"INFY", "TCS"}))
        result = await _sync(calendar, tmp_path, store=store).sync_day(NOV25)
        assert result.outcome is SyncOutcome.REFUSED and "master is stale" in result.detail
        assert store.writes == []

    async def test_an_accepted_roster_is_archived_and_a_rerun_reads_the_archive(
        self, calendar: MarketCalendar, tmp_path: Path
    ) -> None:
        fetch = _Fetch()
        await _sync(calendar, tmp_path, fetch=fetch).sync_day(NOV25)
        assert RosterArchive(tmp_path).read(NOV23) == ROSTER_23
        again = _Fetch()
        result = await _sync(calendar, tmp_path, fetch=again).sync_day(NOV25)
        assert again.asked == [] and result.source == "archive"

    async def test_offline_reads_only_the_archive(
        self, calendar: MarketCalendar, tmp_path: Path
    ) -> None:
        fetch = _Fetch()
        result = await _sync(calendar, tmp_path, fetch=fetch, offline=True).sync_day(NOV25)
        assert result.outcome is SyncOutcome.MISSING and "offline" in result.detail
        assert fetch.asked == []

    async def test_a_held_position_put_under_surveillance_is_named_and_logged(
        self, calendar: MarketCalendar, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.ERROR, logger="algotrader.premarket.surveillance")
        result = await _sync(calendar, tmp_path).sync_day(NOV25, held=["EMPOWER", "INFY"])
        assert result.held_flagged == ("EMPOWER",)
        assert [r.getMessage() for r in caplog.records] == [
            "held position EMPOWER is under ASM/GSM surveillance from 2026-11-25 "
            "(roster of 2026-11-23)"
        ]

    async def test_a_clean_holding_raises_no_alarm(
        self, calendar: MarketCalendar, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.ERROR, logger="algotrader.premarket.surveillance")
        result = await _sync(calendar, tmp_path).sync_day(NOV25, held=["INFY"])
        assert result.held_flagged == () and caplog.records == []


def _everything(raw: bytes, session: dt.date) -> _Store:
    """A store whose master knows every name the roster lists - so a fact is
    asserted about the roster, not about how far today's master reaches back."""
    return _Store(master=frozenset(r.tradingsymbol for r in parse_roster(raw, session=session)))


class TestTheGoverningRoster:
    """E04-S02, QA-E04-07: T reads the LAST roster NSE wrote before T opened.
    NSE held sessions on Sun 1 Feb (the Budget) and Sun 8 Nov 2026 (Muhurat)
    that the calendar does not trade, and wrote a roster after each."""

    async def test_2_feb_reads_the_roster_of_the_budget_session(
        self, calendar: MarketCalendar, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """NSE's notice of 1 Feb 2026 put COOLCAPS, ESSENTIA, MANORAMA, RCDL and
        SYSTMTXC into ST-ASM for 2 Feb and VIVIMEDLAB into stage II. Read the
        calendar's way, 2 Feb took 30 Jan's roster and wrote all five clean."""
        caplog.set_level(logging.WARNING, logger="algotrader.premarket.surveillance")
        fetch, store = _Fetch(NSE_2026), _everything(ROSTER_FEB01, FEB01)
        result = await _sync(calendar, tmp_path, fetch=fetch, store=store).sync_day(FEB02)
        assert result.outcome is SyncOutcome.STORED and result.roster_session == FEB01
        assert fetch.asked == [FEB01]  # the latest closed day first, and it answered
        written = _by_symbol(store.writes[0][1])
        for added in ("COOLCAPS", "ESSENTIA", "MANORAMA", "RCDL", "SYSTMTXC"):
            assert written[added].st_asm_stage == 1, added
        assert written["VIVIMEDLAB"].st_asm_stage == 2
        for gone in ("BAWEJA", "GANESHIN", "MIRCELECTR", "ORIENTTECH"):
            assert not written[gone].is_asm, gone
        assert [r.getMessage() for r in caplog.records] == [
            "NSE wrote a roster for 2026-02-01, a day the calendar has no session on; "
            "it governs 2026-02-02"
        ]

    def test_the_roster_before_it_had_them_clean(self) -> None:
        """The control: what E04-S01 wrote for 2 Feb."""
        before = _by_symbol(parse_roster(ROSTER_JAN30, session=JAN30))
        for added in ("COOLCAPS", "ESSENTIA", "MANORAMA", "RCDL", "SYSTMTXC"):
            assert before[added].st_asm_stage is None, added

    async def test_9_nov_reads_the_muhurat_roster_and_its_series(
        self, calendar: MarketCalendar, tmp_path: Path
    ) -> None:
        """GLOBAL and OPTIFIN traded EQ on 6 Nov and BE on 9 Nov: the 6 Nov
        roster says EQ (for Muhurat), the 8 Nov roster BE."""
        fetch, store = _Fetch(NSE_2026), _everything(ROSTER_NOV08, NOV08)
        result = await _sync(calendar, tmp_path, fetch=fetch, store=store).sync_day(NOV09)
        assert result.roster_session == NOV08 and fetch.asked == [NOV08]
        written = _by_symbol(store.writes[0][1])
        before = _by_symbol(parse_roster(ROSTER_NOV06, session=NOV06))
        for moved in ("GLOBAL", "OPTIFIN"):
            assert (written[moved].tradingsymbol, written[moved].is_t2t) == (f"{moved}-BE", True)
            assert (before[moved].tradingsymbol, before[moved].is_t2t) == (moved, False)
        assert result.t2t == sum(1 for r in written.values() if r.is_t2t)

    async def test_a_plain_weekend_is_asked_about_and_recorded_absent(
        self, calendar: MarketCalendar, tmp_path: Path
    ) -> None:
        fetch = _Fetch()
        result = await _sync(calendar, tmp_path, fetch=fetch).sync_day(NOV23)
        sat, sun = dt.date(2026, 11, 21), dt.date(2026, 11, 22)
        assert fetch.asked == [sun, sat, NOV20] and result.roster_session == NOV20
        archive = RosterArchive(tmp_path)
        assert archive.is_absent(sun) and archive.is_absent(sat)
        assert not archive.is_absent(NOV20) and archive.read(NOV20) == ROSTER_20

    async def test_a_recorded_absence_is_not_asked_again(
        self, calendar: MarketCalendar, tmp_path: Path
    ) -> None:
        await _sync(calendar, tmp_path).sync_day(NOV25)
        again = _Fetch()
        result = await _sync(calendar, tmp_path, fetch=again).sync_day(NOV25)
        assert again.asked == [] and result.outcome is SyncOutcome.STORED

    @pytest.mark.parametrize(
        ("asked_at", "outcome"),
        [
            # 23:59:59.999999 IST on the 24th: NSE may still write the 24th's roster.
            (dt.datetime(2026, 11, 24, 18, 29, 59, 999999, tzinfo=dt.UTC), SyncOutcome.MISSING),
            # 00:00 IST on the 25th: the 24th is over, and had none.
            (dt.datetime(2026, 11, 24, 18, 30, tzinfo=dt.UTC), SyncOutcome.STORED),
        ],
    )
    async def test_a_closed_days_404_is_absence_only_once_the_day_is_over(
        self, calendar: MarketCalendar, tmp_path: Path, asked_at: dt.datetime, outcome: SyncOutcome
    ) -> None:
        fetch, store = _Fetch(), _Store()
        sync = _sync(calendar, tmp_path, fetch=fetch, store=store, now=lambda: asked_at)
        result = await sync.sync_day(NOV25)
        assert result.outcome is outcome
        if outcome is SyncOutcome.MISSING:
            assert "that day is not over" in result.detail and result.roster_session == NOV24
            assert fetch.asked == [NOV24] and store.writes == []
            assert not RosterArchive(tmp_path).is_absent(NOV24)
        else:
            assert fetch.asked == [NOV24, NOV23] and RosterArchive(tmp_path).is_absent(NOV24)

    async def test_a_closed_day_that_cannot_be_asked_about_refuses(
        self, calendar: MarketCalendar, tmp_path: Path
    ) -> None:
        """A failure that is not a 404 says nothing about whether NSE held a
        session - so the older roster is not read in its place."""

        def unreachable(session: dt.date) -> Fetched:
            fetch.asked.append(session)
            raise RosterError("surveillance roster refused: could not be fetched (URLError)")

        fetch, store = _Fetch(), _Store()
        result = await _sync(calendar, tmp_path, fetch=unreachable, store=store).sync_day(NOV25)
        assert result.outcome is SyncOutcome.REFUSED and "could not be fetched" in result.detail
        assert fetch.asked == [NOV24] and store.writes == []
        assert not RosterArchive(tmp_path).is_absent(NOV24)

    async def test_a_closed_days_roster_written_at_the_wrong_time_refuses(
        self, calendar: MarketCalendar, tmp_path: Path
    ) -> None:
        morning = dt.datetime(2026, 11, 24, 4, 0, tzinfo=dt.UTC)  # 09:30 IST on the 24th
        fetch = _Fetch({NOV24: Fetched(ROSTER_23, morning), NOV23: Fetched(ROSTER_23, WRITTEN_23)})
        store = _Store()
        result = await _sync(calendar, tmp_path, fetch=fetch, store=store).sync_day(NOV25)
        assert result.outcome is SyncOutcome.REFUSED and "not the evening file" in result.detail
        assert fetch.asked == [NOV24] and store.writes == []
        assert RosterArchive(tmp_path).read(NOV24) is None

    async def test_offline_must_know_every_closed_day(
        self, calendar: MarketCalendar, tmp_path: Path
    ) -> None:
        """An archive that holds the 23rd's roster but says nothing about the
        24th cannot tell "no session" from "never asked" - so it refuses. Its
        controls: the absence recorded, and a roster recorded."""
        archive = RosterArchive(tmp_path)
        archive.write(NOV23, ROSTER_23)
        offline = _sync(calendar, tmp_path, offline=True)
        result = await offline.sync_day(NOV25)
        assert result.outcome is SyncOutcome.MISSING
        assert "does not record whether NSE wrote a roster for 2026-11-24" in result.detail
        archive.mark_absent(NOV24, at=AFTER)
        result = await offline.sync_day(NOV25)
        assert result.outcome is SyncOutcome.STORED and result.roster_session == NOV23
        archive.write(NOV24, ROSTER_23)
        result = await offline.sync_day(NOV25)
        assert result.outcome is SyncOutcome.STORED and result.roster_session == NOV24


class TestPoliteness:
    def test_an_interval_under_a_second_is_refused(
        self, calendar: MarketCalendar, tmp_path: Path
    ) -> None:
        with pytest.raises(ValueError, match="public courtesy"):
            _sync(calendar, tmp_path, min_interval=0.5)

    async def test_consecutive_fetches_wait_out_the_interval(
        self, calendar: MarketCalendar, tmp_path: Path
    ) -> None:
        slept: list[float] = []

        async def sleep(seconds: float) -> None:
            slept.append(seconds)

        fetch = _Fetch()
        sync = _sync(calendar, tmp_path, fetch=fetch, sleep=sleep, clock=lambda: 100.0)
        await sync.sync_day(NOV23)  # asks 22, 21 (a weekend), then 20 Nov
        await sync.sync_day(NOV25)  # asks 24 (the holiday), then 23 Nov
        assert fetch.asked == [dt.date(2026, 11, 22), dt.date(2026, 11, 21), NOV20, NOV24, NOV23]
        # Every request after the first waited out the interval - probes too.
        assert slept == [sv.FETCH_INTERVAL_SECONDS] * (len(fetch.asked) - 1)


class TestARange:
    async def test_every_session_is_synced_and_a_gap_is_reported(
        self, calendar: MarketCalendar, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """23 to 26 Nov: the 23rd reads the 20th's roster, the 24th is a
        holiday, the 25th reads the 23rd's, and the 26th's (the 25th's
        roster) does not exist in this fetcher - a gap, named."""
        caplog.set_level(logging.ERROR, logger="algotrader.premarket.surveillance")
        seen: list[SyncOutcome] = []
        report = await _sync(calendar, tmp_path).backfill(
            NOV23, dt.date(2026, 11, 26), progress=lambda r: seen.append(r.outcome)
        )
        assert [r.trade_date for r in report.results] == [NOV23, NOV25, dt.date(2026, 11, 26)]
        assert seen == [SyncOutcome.STORED, SyncOutcome.STORED, SyncOutcome.MISSING]
        assert not report.ok
        assert len(caplog.records) == 1 and "2026-11-26: MISSING" in caplog.records[0].getMessage()

    async def test_a_range_whose_first_roster_is_in_an_uncovered_year_refuses_first(
        self, calendar: MarketCalendar, tmp_path: Path
    ) -> None:
        """The year's first session reads the previous year's last roster,
        and the calendar does not know 2025 (E04-S09). Refused before any
        request, not on the first day."""
        fetch = _Fetch()
        first = calendar.trading_days_between(dt.date(2026, 1, 1), dt.date(2026, 1, 9))[0]
        with pytest.raises(HolidayDataError):
            await _sync(calendar, tmp_path, fetch=fetch).backfill(first, dt.date(2026, 1, 9))
        assert fetch.asked == []


class TestTheArchive:
    def test_a_roster_is_filed_by_the_session_it_was_written_after(self, tmp_path: Path) -> None:
        path = RosterArchive(tmp_path).write(NOV23, ROSTER_23)
        assert path == tmp_path / "2026" / "REG1_IND231126.csv"
        assert RosterArchive(tmp_path).read(NOV23) == ROSTER_23
        assert not list((tmp_path / "2026").glob("*.partial"))

    def test_an_absence_is_filed_beside_the_rosters_and_says_when(self, tmp_path: Path) -> None:
        archive = RosterArchive(tmp_path)
        path = archive.mark_absent(NOV24, at=AFTER)
        assert path == tmp_path / "2026" / "REG1_IND241126.absent"
        assert path.read_text("utf-8") == "HTTP 404 at 2026-12-01T00:00:00+00:00\n"
        assert archive.is_absent(NOV24) and archive.read(NOV24) is None
        assert not archive.is_absent(NOV23)
        assert not list((tmp_path / "2026").glob("*.partial"))


def test_the_parse_reads_only_bytes_and_never_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """The parser is pure: a refusal can be reproduced from the archived file."""
    import urllib.request

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("parse_roster opened a URL")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert parse_roster(io.BytesIO(ROSTER_23).getvalue(), session=NOV23)


# ---------------------------------------------------------------------------
# The command's guards - everything that happens before it touches a database
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def script() -> Any:
    import importlib.util

    path = CODE / "scripts" / "sync_surveillance.py"
    spec = importlib.util.spec_from_file_location("sync_surveillance", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTheCommand:
    def test_a_password_on_the_command_line_is_refused(self, script: Any, capsys: Any) -> None:
        """Assembled at runtime so no scanner reads the fixture as a credential."""
        secret = "s3cret" + "-for-test"
        url = "postgresql+psycopg://svc:" + secret + "@localhost/algotrader"
        with pytest.raises(SystemExit) as caught:
            script.main(["--date", "2026-11-25", "--database-url", url])
        assert caught.value.code == 2
        err = capsys.readouterr().err
        assert "DATABASE_URL" in err and secret not in err

    def test_control_a_url_without_a_password_passes_the_guard(
        self, script: Any, capsys: Any
    ) -> None:
        url = "postgresql+psycopg://svc@localhost/algotrader"
        with pytest.raises(SystemExit):
            script.main(["--start", "2026-11-25", "--end", "2026-11-23", "--database-url", url])
        err = capsys.readouterr().err
        assert "carries a password" not in err and "before --start" in err

    @pytest.mark.parametrize(
        "argv",
        [
            ["--date", "2026-11-25", "--start", "2026-11-23"],
            ["--start", "2026-11-23"],
            ["--end", "2026-11-25"],
            [],
        ],
    )
    def test_a_date_or_a_whole_range_is_required(self, script: Any, argv: list[str]) -> None:
        with pytest.raises(SystemExit) as caught:
            script.main([*argv, "--database-url", "x"])
        assert caught.value.code == 2

    def test_no_database_is_a_usage_error(self, script: Any, monkeypatch: Any) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(SystemExit) as caught:
            script.main(["--date", "2026-11-25"])
        assert caught.value.code == 2

    def test_a_date_that_is_not_one(self, script: Any) -> None:
        with pytest.raises(SystemExit):
            script.main(["--date", "25-11-2026", "--database-url", "x"])
