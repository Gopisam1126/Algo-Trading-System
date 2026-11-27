"""E04-S03: NSE's F&O ban list, parsed strictly and synced per trade date.

Every refusal is tested against a REAL list with one thing changed, so the
check under test is the only thing that can refuse (the real file passing is
each test's control). The facts the design rests on are asserted against the
real files: each is named for the trade date it governs; a day with no ban is
NSE's explicit NIL; and the lists for the days after the two sessions the
calendar does not trade (2 Feb and 9 Nov 2026) were written after them.
"""

from __future__ import annotations

import datetime as dt
import gzip
import http.client
import logging
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from algotrader.common.calendar import HolidayDataError, MarketCalendar, load_holidays_with_status
from algotrader.premarket import fno_ban as fb
from algotrader.premarket.fno_ban import (
    BanListError,
    BanListMissingError,
    FnoBanArchive,
    FnoBanSync,
    check_written,
    fetch_ban_list,
    parse_ban_list,
    resolve_ban_list,
)
from algotrader.premarket.surveillance import Fetched, SyncOutcome

CODE = Path(__file__).resolve().parents[2]
FIXTURES = CODE / "tests" / "fixtures"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

NOV23, NOV24, NOV25 = dt.date(2026, 11, 23), dt.date(2026, 11, 24), dt.date(2026, 11, 25)
NOV26, NOV27 = dt.date(2026, 11, 26), dt.date(2026, 11, 27)
#: Fri 30 Jan, the Budget session on Sun 1 Feb (NSE/CMTR/72349), Mon 2 Feb.
JAN29, JAN30, FEB02 = dt.date(2026, 1, 29), dt.date(2026, 1, 30), dt.date(2026, 2, 2)
#: Fri 6 Nov, Muhurat on Sun 8 Nov, Mon 9 Nov.
NOV06, NOV09 = dt.date(2026, 11, 6), dt.date(2026, 11, 9)


def _real(day: dt.date) -> bytes:
    return gzip.decompress((FIXTURES / "fno_ban" / f"fo_secban_{day:%d%m%Y}.csv.gz").read_bytes())


#: The lists NSE serves, and when it wrote them (Last-Modified, read 27 Nov 2026).
NSE_2026 = {
    JAN30: Fetched(_real(JAN30), dt.datetime(2026, 1, 29, 19, 1, 4, tzinfo=IST)),
    FEB02: Fetched(_real(FEB02), dt.datetime(2026, 2, 1, 18, 17, 3, tzinfo=IST)),  # Sunday
    NOV09: Fetched(_real(NOV09), dt.datetime(2026, 11, 8, 17, 42, 4, tzinfo=IST)),  # Sunday
    NOV25: Fetched(_real(NOV25), dt.datetime(2026, 11, 23, 18, 38, 4, tzinfo=IST)),
    NOV26: Fetched(_real(NOV26), dt.datetime(2026, 11, 25, 18, 20, 34, tzinfo=IST)),
    NOV27: Fetched(_real(NOV27), dt.datetime(2026, 11, 26, 19, 26, 4, tzinfo=IST)),
}
LIST_27 = NSE_2026[NOV27].body
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


def _lines(raw: bytes) -> list[str]:
    return raw.decode("ascii").split("\n")


def _join(lines: list[str]) -> bytes:
    return "\n".join(lines).encode("ascii")


# ---------------------------------------------------------------------------
# The real lists
# ---------------------------------------------------------------------------


class TestTheRealLists:
    @pytest.mark.parametrize(
        ("day", "banned"),
        [
            (NOV27, ("APLAPOLLO", "MANAPPURAM")),
            (NOV26, ("APLAPOLLO", "MANAPPURAM")),
            (NOV25, ("MANAPPURAM",)),
            (NOV09, ("KAYNES",)),
            (FEB02, ("SAMMAANCAP",)),
        ],
    )
    def test_each_list_names_its_trade_date_and_its_securities(
        self, day: dt.date, banned: tuple[str, ...]
    ) -> None:
        assert parse_ban_list(NSE_2026[day].body, trade_date=day) == banned

    def test_a_day_with_no_ban_is_nses_explicit_nil(self) -> None:
        assert _lines(NSE_2026[JAN30].body)[0].endswith(": NIL")
        assert parse_ban_list(NSE_2026[JAN30].body, trade_date=JAN30) == ()

    def test_each_was_written_after_the_calendars_previous_session_and_before_it_opened(
        self, calendar: MarketCalendar
    ) -> None:
        """Including 2 Feb and 9 Nov, whose lists NSE wrote after the Budget
        and Muhurat sessions - days the calendar does not trade."""
        for day, fetched in NSE_2026.items():
            check_written(
                fetched.last_modified, after=calendar.previous_trading_day(day), governs=day
            )

    def test_the_lists_after_the_two_untraded_sessions_were_written_on_those_sundays(
        self,
    ) -> None:
        """The fact QA-E04-07 asks of every list: is the file NSE writes after a
        session the calendar does not trade the one that governs the next day?
        Here it is, and it is named for that day."""
        for day, sunday in ((FEB02, dt.date(2026, 2, 1)), (NOV09, dt.date(2026, 11, 8))):
            written = NSE_2026[day].last_modified
            assert written is not None and written.astimezone(IST).date() == sunday

    def test_the_file_for_another_day_is_refused(self) -> None:
        with pytest.raises(BanListError, match="ban list for 2026-11-27, not 2026-11-26"):
            parse_ban_list(LIST_27, trade_date=NOV26)

    def test_crlf_line_ends_read_the_same(self) -> None:
        crlf = LIST_27.replace(b"\n", b"\r\n")
        assert parse_ban_list(crlf, trade_date=NOV27) == ("APLAPOLLO", "MANAPPURAM")


# ---------------------------------------------------------------------------
# The first line
# ---------------------------------------------------------------------------


class TestTheFirstLine:
    @pytest.mark.parametrize(
        "first",
        [
            "Securities in ban for Trade Date 27-NOV-2026:",  # NSE's wording, re-cased
            "Securities in Ban For Trade Date 27-NOV-2026",  # the colon lost
            "Securities in Ban For Trade Date 27-11-2026:",  # another date format
            "Securities in Ban For Trade Date 27-NOV-2026: ",  # trailing space
            "Securities in Ban For Trade Date 27-NOV-2026:,x",  # a second field
            "1,APLAPOLLO",  # the line itself lost
            "",
        ],
    )
    def test_anything_but_nses_line_is_refused(self, first: str) -> None:
        lines = _lines(LIST_27)
        lines[0] = first
        with pytest.raises(BanListError, match="does not begin with NSE's line"):
            parse_ban_list(_join(lines), trade_date=NOV27)

    @pytest.mark.parametrize(
        ("date", "why"),
        [("27-NOX-2026", "not one"), ("31-FEB-2026", "not a date"), ("00-NOV-2026", "not a date")],
    )
    def test_a_date_that_is_not_one_is_refused(self, date: str, why: str) -> None:
        lines = _lines(LIST_27)
        lines[0] = f"Securities in Ban For Trade Date {date}:"
        with pytest.raises(BanListError, match=why):
            parse_ban_list(_join(lines), trade_date=NOV27)

    def test_the_months_are_english_whatever_the_locale(self) -> None:
        assert fb._MONTHS["NOV"] == 11 and len(fb._MONTHS) == 12


# ---------------------------------------------------------------------------
# An empty deny list (SPRINT02 5.1)
# ---------------------------------------------------------------------------


class TestAnEmptyDenyList:
    def test_no_rows_and_no_nil_is_a_failed_fetch(self) -> None:
        """The probe: the real list's first line with its rows cut off. Read
        leniently it would say nothing is banned - the most permissive answer."""
        cut = _join(_lines(LIST_27)[:1]) + b"\n"
        with pytest.raises(BanListError, match="does not say NIL"):
            parse_ban_list(cut, trade_date=NOV27)

    def test_nil_with_rows_is_refused(self) -> None:
        lines = _lines(LIST_27)
        lines[0] += " NIL"
        with pytest.raises(BanListError, match="says NIL and then lists 2"):
            parse_ban_list(_join(lines), trade_date=NOV27)

    @pytest.mark.parametrize("raw", [b"", b"\n", b"\n\n"])
    def test_an_empty_file_is_refused(self, raw: bytes) -> None:
        with pytest.raises(BanListError, match="is empty"):
            parse_ban_list(raw, trade_date=NOV27)


# ---------------------------------------------------------------------------
# The rows
# ---------------------------------------------------------------------------


class TestTheRows:
    @pytest.mark.parametrize(
        ("rows", "why"),
        [
            (["2,APLAPOLLO", "1,MANAPPURAM"], "numbered '2', not 1"),
            (["1,APLAPOLLO", "3,MANAPPURAM"], "numbered '3', not 2"),
            (["01,APLAPOLLO", "02,MANAPPURAM"], "numbered '01', not 1"),
            (["1,APLAPOLLO", "1,MANAPPURAM"], "numbered '1', not 2"),
            (["1,APLAPOLLO", "2,APLAPOLLO"], "listed twice"),
            (["1,APLAPOLLO", "2, MANAPPURAM"], "not a symbol"),
            (["1,APLAPOLLO", "2,MANAPPURAM,X"], "not a number and a symbol"),
            (["1,APLAPOLLO", "MANAPPURAM"], "not a number and a symbol"),
            (["1,APLAPOLLO", "", "2,MANAPPURAM"], "not a number and a symbol"),
            (["1,APLAPOLLO", "2,"], "not a symbol"),
            (["1,APLAPOLLO", "2,MANAPPURAM\x1bOK"], "not a symbol"),
        ],
    )
    def test_one_bad_row_refuses_the_whole_list(self, rows: list[str], why: str) -> None:
        with pytest.raises(BanListError, match=why):
            parse_ban_list(_join([_lines(LIST_27)[0], *rows]), trade_date=NOV27)

    def test_blank_lines_at_the_end_carry_nothing(self) -> None:
        assert parse_ban_list(LIST_27 + b"\n\n", trade_date=NOV27) == ("APLAPOLLO", "MANAPPURAM")

    @pytest.mark.parametrize("separator", ["\N{LINE SEPARATOR}", "\x85", "\x1b[2J"])
    def test_a_refusal_naming_the_file_stays_one_line(self, separator: str) -> None:
        """Parts of a refusal come from the file and reach the log (QA-SEC-30):
        a line separator in the first line must not forge a second log line."""
        forged = f"Securities{separator}CRITICAL kill switch disarmed by operator"
        raw = f"{forged}\n1,APLAPOLLO\n".encode()
        with pytest.raises(BanListError) as caught:
            parse_ban_list(raw, trade_date=NOV27)
        assert str(caught.value).count("\n") == 0 and separator not in str(caught.value)
        assert str(caught.value).startswith("F&O ban list refused: ")


class TestTheFileItself:
    def test_an_oversized_file_is_refused(self) -> None:
        with pytest.raises(BanListError, match="exceeds"):
            parse_ban_list(LIST_27 + b"9" * fb.MAX_FILE_BYTES, trade_date=NOV27)

    def test_a_file_that_is_not_text_is_refused(self) -> None:
        with pytest.raises(BanListError, match="not UTF-8"):
            parse_ban_list(b"\xff\xfe" + LIST_27, trade_date=NOV27)

    def test_a_malformed_csv_is_refused_not_raised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """QA-SEC-45: csv.Error is not a ValueError. The one csv.Error a file
        can still cause - a field over 128 KiB - cannot get past the 64 KiB
        cap here, so the helper's refusal is what is driven."""
        from algotrader.common.text import MalformedCsvError

        def malformed(text: str) -> list[list[str]]:
            raise MalformedCsvError("not well-formed CSV (field larger than field limit)")

        monkeypatch.setattr(fb, "csv_rows", malformed)
        with pytest.raises(BanListError, match="not well-formed CSV"):
            parse_ban_list(LIST_27, trade_date=NOV27)

    def test_an_unterminated_quote_reads_as_a_symbol_that_is_not_one(self) -> None:
        with pytest.raises(BanListError, match="not a symbol"):
            parse_ban_list(LIST_27 + b'3,"UNTERMINATED\n', trade_date=NOV27)

    def test_a_byte_order_mark_is_not_a_format_change(self) -> None:
        assert parse_ban_list(b"\xef\xbb\xbf" + LIST_27, trade_date=NOV27) == (
            "APLAPOLLO",
            "MANAPPURAM",
        )


_SYMBOL = st.from_regex(r"[A-Z][A-Z0-9&\-]{0,14}", fullmatch=True)


@given(st.lists(_SYMBOL, min_size=1, max_size=40, unique=True))
def test_any_list_nse_could_write_reads_back_exactly(symbols: list[str]) -> None:
    """The round trip, in NSE's format: every name, in order, none added."""
    body = [f"{n},{s}" for n, s in enumerate(symbols, start=1)]
    raw = _join(["Securities in Ban For Trade Date 27-NOV-2026:", *body]) + b"\n"
    assert parse_ban_list(raw, trade_date=NOV27) == tuple(symbols)


@given(st.lists(_SYMBOL, min_size=2, max_size=40, unique=True), st.data())
def test_any_list_with_a_row_taken_out_of_the_middle_is_refused(
    symbols: list[str], data: st.DataObject
) -> None:
    """A row lost from the middle leaves a gap in the numbering. (One lost
    from the END is a shorter valid list - which is what Content-Length is
    for: ``TestFetching``.)"""
    body = [f"{n},{s}" for n, s in enumerate(symbols, start=1)]
    del body[data.draw(st.integers(0, len(body) - 2))]
    raw = _join(["Securities in Ban For Trade Date 27-NOV-2026:", *body])
    with pytest.raises(BanListError, match="numbered"):
        parse_ban_list(raw, trade_date=NOV27)


# ---------------------------------------------------------------------------
# Resolution against the instrument master
# ---------------------------------------------------------------------------


class TestResolution:
    def test_a_banned_equity_is_its_kite_name(self) -> None:
        assert resolve_ban_list(("APLAPOLLO", "MANAPPURAM"), master=MASTER) == {
            "APLAPOLLO",
            "MANAPPURAM",
        }

    def test_every_main_board_series_of_a_banned_security_is_flagged(self) -> None:
        master = frozenset({"XYZ", "XYZ-BE", "XYZ-BZ", "XYZ-SM", "XYZ-N1", "OTHER"})
        assert resolve_ban_list(("XYZ",), master=master) == {"XYZ", "XYZ-BE", "XYZ-BZ"}

    def test_a_security_trading_only_in_be_is_still_found(self) -> None:
        assert resolve_ban_list(("XYZ",), master=frozenset({"XYZ-BE", "OTHER"})) == {"XYZ-BE"}

    def test_a_banned_security_the_master_does_not_know_refuses_the_list(self) -> None:
        """It may be a security the master holds under another name, which
        would otherwise be written clean."""
        with pytest.raises(BanListError, match=r"no instrument for 1 banned .*\(GONE\)"):
            resolve_ban_list(("APLAPOLLO", "GONE"), master=MASTER)

    def test_an_sme_or_bond_name_is_not_the_security(self) -> None:
        with pytest.raises(BanListError, match="no instrument"):
            resolve_ban_list(("XYZ",), master=frozenset({"XYZ-SM", "XYZ-N1"}))

    def test_nil_flags_nothing(self) -> None:
        assert resolve_ban_list((), master=MASTER) == frozenset()


# ---------------------------------------------------------------------------
# When NSE wrote it
# ---------------------------------------------------------------------------


class TestThePublicationWindow:
    def test_written_inside_the_window_is_accepted(self) -> None:
        check_written(NSE_2026[NOV27].last_modified, after=NOV26, governs=NOV27)

    @pytest.mark.parametrize(
        "written",
        [
            dt.datetime(2026, 11, 26, 15, 35, tzinfo=IST),  # the day's last moment, not after it
            dt.datetime(2026, 11, 26, 12, 0, tzinfo=IST),  # during the session
            dt.datetime(2026, 11, 27, 9, 15, tzinfo=IST),  # at the open
            dt.datetime(2026, 11, 27, 11, 0, tzinfo=IST),  # during T
            dt.datetime(2026, 11, 25, 19, 0, tzinfo=IST),  # a day early: the 26th's list
        ],
    )
    def test_written_outside_it_is_refused(self, written: dt.datetime) -> None:
        with pytest.raises(BanListError, match="not the list made after 2026-11-26"):
            check_written(written, after=NOV26, governs=NOV27)

    def test_no_last_modified_is_refused(self) -> None:
        with pytest.raises(BanListError, match="no Last-Modified"):
            check_written(None, after=NOV26, governs=NOV27)

    @pytest.mark.parametrize(
        ("after", "governs", "too_early", "late_enough"),
        [
            # Before CAS (3 Aug 2026): the session ends at the 15:30 close.
            (JAN29, JAN30, (15, 30), (15, 31)),
            # From 3 Aug 2026 the closing auction runs to 15:35.
            (NOV26, NOV27, (15, 35), (15, 36)),
            # From 7 Dec 2026, to 15:40 (E04-S08: session rules are dated).
            (dt.date(2026, 12, 7), dt.date(2026, 12, 8), (15, 40), (15, 41)),
        ],
    )
    def test_the_window_opens_at_that_days_own_last_moment(
        self,
        after: dt.date,
        governs: dt.date,
        too_early: tuple[int, int],
        late_enough: tuple[int, int],
    ) -> None:
        early = dt.datetime.combine(after, dt.time(*too_early), tzinfo=IST)
        with pytest.raises(BanListError, match="outside"):
            check_written(early, after=after, governs=governs)
        check_written(
            dt.datetime.combine(after, dt.time(*late_enough), tzinfo=IST),
            after=after,
            governs=governs,
        )


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


class _Response:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        modified: str | None = "Thu, 26 Nov 2026 13:56:04 GMT",
        length: object = ...,
        chunked: bool = False,
        fail: BaseException | None = None,
    ) -> None:
        self._body = body
        self._fail = fail
        self.status = status
        self.headers: dict[str, str] = {}
        if modified:
            self.headers["Last-Modified"] = modified
        if length is ...:
            length = str(len(body))
        if isinstance(length, str):
            self.headers["Content-Length"] = length
        if chunked:
            self.headers["Transfer-Encoding"] = "chunked"

    def read(self, limit: int) -> bytes:
        if self._fail is not None:
            raise self._fail
        return self._body[:limit]

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _serving(response: _Response, seen: list[str] | None = None) -> Any:
    def opener(request: Any, timeout: float) -> _Response:
        if seen is not None:
            seen.append(request.full_url)
        return response

    return opener


class TestFetching:
    def test_the_url_names_the_trade_date_and_last_modified_is_read(self) -> None:
        seen: list[str] = []
        fetched = fetch_ban_list(NOV27, opener=_serving(_Response(LIST_27), seen))
        assert seen == [
            "https://nsearchives.nseindia.com/archives/fo/sec_ban/fo_secban_27112026.csv"
        ]
        assert fetched.body == LIST_27
        assert fetched.last_modified == NSE_2026[NOV27].last_modified

    def test_a_body_shorter_than_its_content_length_is_refused(self) -> None:
        """The fail-open probe: the real list cut after its first row is a
        valid, shorter list - MANAPPURAM would read as clean."""
        cut = _join(_lines(LIST_27)[:2]) + b"\n"
        assert parse_ban_list(cut, trade_date=NOV27) == ("APLAPOLLO",)  # why it matters
        response = _Response(cut, length=str(len(LIST_27)))
        with pytest.raises(BanListError, match=r"declared '71' bytes .* sent 58"):
            fetch_ban_list(NOV27, opener=_serving(response))

    @pytest.mark.parametrize("length", ["70", "72", "-1", "71 ", "abc", ""])
    def test_a_content_length_that_does_not_match_is_refused(self, length: str) -> None:
        with pytest.raises(BanListError, match="declared"):
            fetch_ban_list(NOV27, opener=_serving(_Response(LIST_27, length=length)))

    def test_a_chunked_body_is_left_to_http_client(self) -> None:
        """http.client raises IncompleteRead for a cut chunk (below)."""
        response = _Response(LIST_27, length=None, chunked=True)
        assert fetch_ban_list(NOV27, opener=_serving(response)).body == LIST_27

    def test_a_body_with_no_length_and_no_chunking_is_refused(self) -> None:
        with pytest.raises(BanListError, match="no length and unchunked"):
            fetch_ban_list(NOV27, opener=_serving(_Response(LIST_27, length=None)))

    @pytest.mark.parametrize(
        "failure",
        [
            TimeoutError("timed out"),
            ConnectionResetError(10054, "reset"),
            http.client.IncompleteRead(b"Securities", 61),
            urllib.error.URLError("unreachable"),
        ],
    )
    def test_a_failure_opening_or_reading_is_refused(self, failure: BaseException) -> None:
        with pytest.raises(BanListError, match="could not be fetched"):
            fetch_ban_list(NOV27, opener=_serving(_Response(LIST_27, fail=failure)))

    def test_a_failure_to_connect_is_refused(self) -> None:
        def opener(request: Any, timeout: float) -> _Response:
            raise urllib.error.URLError("unreachable")

        with pytest.raises(BanListError, match=r"could not be fetched \(URLError\)"):
            fetch_ban_list(NOV27, opener=opener)

    def test_a_404_is_missing(self) -> None:
        def opener(request: Any, timeout: float) -> _Response:
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", None, None)  # type: ignore[arg-type]

        with pytest.raises(BanListMissingError, match="HTTP 404"):
            fetch_ban_list(NOV27, opener=opener)

    def test_another_http_error_is_refused_not_missing(self) -> None:
        def opener(request: Any, timeout: float) -> _Response:
            raise urllib.error.HTTPError(request.full_url, 503, "Busy", None, None)  # type: ignore[arg-type]

        with pytest.raises(BanListError, match="HTTP 503") as caught:
            fetch_ban_list(NOV27, opener=opener)
        assert not isinstance(caught.value, BanListMissingError)

    def test_a_non_200_answer_is_refused(self) -> None:
        with pytest.raises(BanListError, match="HTTP 203"):
            fetch_ban_list(NOV27, opener=_serving(_Response(LIST_27, status=203)))

    def test_an_oversized_body_is_refused(self) -> None:
        body = b"x" * (fb.MAX_FILE_BYTES + 10)
        with pytest.raises(BanListError, match="exceeds"):
            fetch_ban_list(NOV27, opener=_serving(_Response(body)))

    def test_an_unparseable_last_modified_reads_as_none(self) -> None:
        response = _Response(LIST_27, modified="yesterday-ish")
        assert fetch_ban_list(NOV27, opener=_serving(response)).last_modified is None

    def test_the_opener_refuses_another_origin(self) -> None:
        with pytest.raises(BanListError, match="not https://nsearchives"):
            fb._open(urllib.request.Request("https://example.com/fo_secban_27112026.csv"), 1.0)


# ---------------------------------------------------------------------------
# The sync: one trade date at a time
# ---------------------------------------------------------------------------


class _Store:
    def __init__(self, master: frozenset[str] = MASTER) -> None:
        self.master = master
        self.writes: list[tuple[dt.date, dict[str, bool]]] = []

    async def active_symbols(self) -> frozenset[str]:
        return self.master

    async def write(self, trade_date: dt.date, flags: Mapping[str, bool]) -> int:
        self.writes.append((trade_date, dict(flags)))
        return len(flags)


class _Fetch:
    """Serves the real lists by trade date, as NSE does, and records each ask."""

    def __init__(self, by_day: dict[dt.date, Fetched] | None = None) -> None:
        self.by_day = by_day if by_day is not None else dict(NSE_2026)
        self.asked: list[dt.date] = []

    def __call__(self, day: dt.date) -> Fetched:
        self.asked.append(day)
        if day not in self.by_day:
            raise BanListMissingError(f"NSE has no F&O ban list for {day} (HTTP 404)")
        return self.by_day[day]


async def _no_sleep(seconds: float) -> None:
    return None


def _sync(calendar: MarketCalendar, tmp_path: Path, **kw: Any) -> FnoBanSync:
    kw.setdefault("store", _Store())
    kw.setdefault("fetch", _Fetch())
    kw.setdefault("sleep", _no_sleep)
    return FnoBanSync(calendar=calendar, archive=FnoBanArchive(tmp_path), **kw)


class TestOneTradeDate:
    async def test_every_instrument_is_written_true_only_if_banned(
        self, calendar: MarketCalendar, tmp_path: Path
    ) -> None:
        store, fetch = _Store(), _Fetch()
        result = await _sync(calendar, tmp_path, store=store, fetch=fetch).sync_day(NOV27)
        assert result.outcome is SyncOutcome.STORED and result.source == "network"
        assert result.banned == ("APLAPOLLO", "MANAPPURAM") and result.flagged == 2
        assert fetch.asked == [NOV27]  # its own file, and nothing else
        [(day, flags)] = store.writes
        assert day == NOV27 and set(flags) == MASTER  # every instrument has an answer
        assert {name for name, banned in flags.items() if banned} == {"APLAPOLLO", "MANAPPURAM"}
        assert result.written == len(MASTER)
        assert FnoBanArchive(tmp_path).read(NOV27) == LIST_27

    async def test_a_nil_day_writes_every_instrument_clean(
        self, calendar: MarketCalendar, tmp_path: Path
    ) -> None:
        store = _Store()
        result = await _sync(calendar, tmp_path, store=store).sync_day(JAN30)
        assert result.outcome is SyncOutcome.STORED and result.banned == ()
        [(_, flags)] = store.writes
        assert set(flags) == MASTER and not any(flags.values())

    @pytest.mark.parametrize(("day", "banned"), [(FEB02, "SAMMAANCAP"), (NOV09, "KAYNES")])
    async def test_the_day_after_an_untraded_session_reads_its_own_list(
        self, calendar: MarketCalendar, tmp_path: Path, day: dt.date, banned: str
    ) -> None:
        """2 Feb reads the list NSE wrote after the Budget session, 9 Nov the
        one written after Muhurat - and asks for no other file."""
        fetch, store = _Fetch(), _Store()
        result = await _sync(calendar, tmp_path, fetch=fetch, store=store).sync_day(day)
        assert result.outcome is SyncOutcome.STORED and result.banned == (banned,)
        assert fetch.asked == [day]

    async def test_a_day_after_a_holiday_is_bounded_by_the_session_before_it(
        self, calendar: MarketCalendar, tmp_path: Path
    ) -> None:
        """24 Nov was a holiday: NSE wrote the 25th's list on the 23rd."""
        result = await _sync(calendar, tmp_path).sync_day(NOV25)
        assert result.outcome is SyncOutcome.STORED and result.banned == ("MANAPPURAM",)

    async def test_an_archived_list_is_read_from_disk_and_not_fetched(
        self, calendar: MarketCalendar, tmp_path: Path
    ) -> None:
        FnoBanArchive(tmp_path).write(NOV27, LIST_27)
        fetch, store = _Fetch(), _Store()
        result = await _sync(calendar, tmp_path, fetch=fetch, store=store).sync_day(NOV27)
        assert result.outcome is SyncOutcome.STORED and result.source == "archive"
        assert fetch.asked == [] and len(store.writes) == 1

    async def test_offline_without_the_list_is_missing_and_writes_nothing(
        self, calendar: MarketCalendar, tmp_path: Path
    ) -> None:
        fetch, store = _Fetch(), _Store()
        sync = _sync(calendar, tmp_path, fetch=fetch, store=store, offline=True)
        result = await sync.sync_day(NOV27)
        assert result.outcome is SyncOutcome.MISSING and "offline" in result.detail
        assert fetch.asked == [] and store.writes == []

    async def test_a_404_is_missing_and_writes_nothing(
        self, calendar: MarketCalendar, tmp_path: Path
    ) -> None:
        store = _Store()
        result = await _sync(calendar, tmp_path, store=store, fetch=_Fetch({})).sync_day(NOV27)
        assert result.outcome is SyncOutcome.MISSING and "HTTP 404" in result.detail
        assert store.writes == [] and FnoBanArchive(tmp_path).read(NOV27) is None

    @pytest.mark.parametrize(
        ("served", "why"),
        [
            (Fetched(LIST_27, NSE_2026[NOV26].last_modified), "outside"),  # yesterday's write
            (Fetched(LIST_27, None), "no Last-Modified"),
            (Fetched(NSE_2026[NOV26].body, NSE_2026[NOV27].last_modified), "not 2026-11-27"),
            (Fetched(_join(_lines(LIST_27)[:1]), NSE_2026[NOV27].last_modified), "NIL"),
        ],
    )
    async def test_a_list_this_system_will_not_use_is_refused_unarchived_and_unwritten(
        self, calendar: MarketCalendar, tmp_path: Path, served: Fetched, why: str
    ) -> None:
        store = _Store()
        sync = _sync(calendar, tmp_path, store=store, fetch=_Fetch({NOV27: served}))
        result = await sync.sync_day(NOV27)
        assert result.outcome is SyncOutcome.REFUSED and why in result.detail
        assert store.writes == [] and FnoBanArchive(tmp_path).read(NOV27) is None

    async def test_a_banned_name_the_master_does_not_know_refuses_the_day(
        self, calendar: MarketCalendar, tmp_path: Path
    ) -> None:
        store = _Store(master=MASTER - {"MANAPPURAM"})
        result = await _sync(calendar, tmp_path, store=store).sync_day(NOV27)
        assert result.outcome is SyncOutcome.REFUSED and "MANAPPURAM" in result.detail
        assert store.writes == []

    async def test_an_empty_master_is_refused_not_stored_with_nothing_written(
        self, calendar: MarketCalendar, tmp_path: Path
    ) -> None:
        store = _Store(master=frozenset())
        result = await _sync(calendar, tmp_path, store=store).sync_day(JAN30)
        assert result.outcome is SyncOutcome.REFUSED and "master is empty" in result.detail
        assert store.writes == []

    async def test_a_day_with_no_session_asks_for_nothing(
        self, calendar: MarketCalendar, tmp_path: Path
    ) -> None:
        fetch, store = _Fetch(), _Store()
        sync = _sync(calendar, tmp_path, fetch=fetch, store=store)
        for day in (NOV24, dt.date(2026, 11, 28), dt.date(2026, 11, 8)):
            result = await sync.sync_day(day)
            assert result.outcome is SyncOutcome.NOT_A_TRADING_DAY
        assert fetch.asked == [] and store.writes == []

    async def test_a_tampered_archive_is_refused_not_trusted(
        self, calendar: MarketCalendar, tmp_path: Path
    ) -> None:
        """The archive is re-parsed on every read: a file swapped for another
        day's is refused, not written."""
        FnoBanArchive(tmp_path).write(NOV27, NSE_2026[NOV26].body)
        store = _Store()
        result = await _sync(calendar, tmp_path, store=store, offline=True).sync_day(NOV27)
        assert result.outcome is SyncOutcome.REFUSED and store.writes == []


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
        await sync.sync_day(NOV26)
        await sync.sync_day(NOV27)
        assert fetch.asked == [NOV26, NOV27] and slept == [fb.FETCH_INTERVAL_SECONDS]


class TestARange:
    async def test_every_session_is_synced_and_a_gap_is_reported(
        self, calendar: MarketCalendar, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """23 to 27 Nov: the 23rd's list is not served here (a gap, named), the
        24th is a holiday, the 25th to 27th are stored - read from the master
        once for the whole range."""
        caplog.set_level(logging.ERROR, logger="algotrader.premarket.fno_ban")
        seen: list[SyncOutcome] = []
        report = await _sync(calendar, tmp_path).backfill(
            NOV23, NOV27, progress=lambda r: seen.append(r.outcome)
        )
        assert [r.trade_date for r in report.results] == [NOV23, NOV25, NOV26, NOV27]
        assert seen == [SyncOutcome.MISSING, *[SyncOutcome.STORED] * 3]
        assert not report.ok and len(report.of(SyncOutcome.STORED)) == 3
        assert len(caplog.records) == 1 and "2026-11-23: MISSING" in caplog.records[0].getMessage()

    async def test_a_whole_range_stored_is_ok(
        self, calendar: MarketCalendar, tmp_path: Path
    ) -> None:
        report = await _sync(calendar, tmp_path).backfill(NOV25, NOV27)
        assert report.ok and len(report.results) == 3

    async def test_a_range_whose_first_list_follows_an_uncovered_year_refuses_first(
        self, calendar: MarketCalendar, tmp_path: Path
    ) -> None:
        """The year's first session follows the previous year's last, which the
        calendar does not know (E04-S09). Refused before any request."""
        fetch = _Fetch()
        first = calendar.trading_days_between(dt.date(2026, 1, 1), dt.date(2026, 1, 9))[0]
        with pytest.raises(HolidayDataError):
            await _sync(calendar, tmp_path, fetch=fetch).backfill(first, dt.date(2026, 1, 9))
        assert fetch.asked == []


class TestTheArchive:
    def test_a_list_is_filed_by_the_trade_date_it_governs(self, tmp_path: Path) -> None:
        path = FnoBanArchive(tmp_path).write(NOV27, LIST_27)
        assert path == tmp_path / "2026" / "fo_secban_27112026.csv"
        assert FnoBanArchive(tmp_path).read(NOV27) == LIST_27
        assert not list((tmp_path / "2026").glob("*.partial"))


def test_the_parse_reads_only_bytes_and_never_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """The parser is pure: a refusal can be reproduced from the archived file."""

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("parse_ban_list opened a URL")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert parse_ban_list(LIST_27, trade_date=NOV27)


# ---------------------------------------------------------------------------
# The command's guards - everything that happens before it touches a database
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def script() -> Any:
    import importlib.util

    path = CODE / "scripts" / "sync_fno_ban.py"
    spec = importlib.util.spec_from_file_location("sync_fno_ban", path)
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
            script.main(["--date", "2026-11-27", "--database-url", url])
        assert caught.value.code == 2
        err = capsys.readouterr().err
        assert "DATABASE_URL" in err and secret not in err

    def test_control_a_url_without_a_password_passes_the_guard(
        self, script: Any, capsys: Any
    ) -> None:
        url = "postgresql+psycopg://svc@localhost/algotrader"
        with pytest.raises(SystemExit):
            script.main(["--start", "2026-11-27", "--end", "2026-11-25", "--database-url", url])
        err = capsys.readouterr().err
        assert "carries a password" not in err and "before --start" in err

    @pytest.mark.parametrize(
        "argv",
        [
            ["--date", "2026-11-27", "--start", "2026-11-25"],
            ["--start", "2026-11-25"],
            ["--end", "2026-11-27"],
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
            script.main(["--date", "2026-11-27"])
        assert caught.value.code == 2

    def test_a_date_that_is_not_one(self, script: Any) -> None:
        with pytest.raises(SystemExit):
            script.main(["--date", "27-11-2026", "--database-url", "x"])
