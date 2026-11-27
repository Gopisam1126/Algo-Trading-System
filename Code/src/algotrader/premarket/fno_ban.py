"""NSE's F&O ban list: ``is_fno_ban`` per symbol per day (E04-S03).

A stock is in its F&O *ban period* when the open interest in its derivatives
nears the market-wide position limit. No one may then add to a position in
those derivatives - only reduce it - and an increase costs 1% of its value,
minimum Rs 5,000 and maximum Rs 1,00,000 per entity per stock per day (NSE
Clearing, NCL/CMPT/70085, 2.5). The stock itself still trades.
``restrictions_from`` refuses an entry in every such stock (a recorded,
conservative assumption); this module is what tells it which ones they are.

## The design record

**The decision.** ``is_fno_ban`` for trade date T comes from NSE's
``fo_secban_<DDMMYYYY>.csv`` under ``nsearchives.nseindia.com/archives/fo/sec_ban/``
- the ``FO-SEC-BAN`` entry of NSE's daily-reports index, "F&O-Security in ban
period for F&O segment". Unlike the surveillance roster it is named for the
trade date it GOVERNS, and says so in its first line: ``Securities in Ban For
Trade Date 27-NOV-2026:``. NSE writes it the evening of the session before -
19:26 IST on 26 Nov 2026 for 27 Nov, 18:20 on 25 Nov for 26 Nov; the index
files it under that earlier session's date - from that session's closing open
interest. NSE Clearing's SOP (NCL/CMPT/70085, 8 Sep 2025, in force from
1 Oct 2025) puts a stock whose Future-Equivalent open interest exceeds 95% of
its market-wide position limit at the end of a day "in the ban period from
next day onwards" (2.1.1), and takes it out only when that falls to 80%
(2.1.2). The list then holds for the whole of T: NSE's intraday monitoring
(NSE/SURV/71090, from 3 Nov 2025) takes four random snapshots a day and
broadcasts an alert above 60%. It bans nothing. Checked 27 Nov 2026 for every
2026 session (221 trade dates): each list names exactly the stocks NSE
Clearing's combined open-interest file for the session before marks "No Fresh
Positions", and every stock over 95% of its limit there is in the next list.

**Every instrument gets an answer.** The list is exhaustive - "all stocks
placed under ban for the next trading day" (2.1.3) - so a security it does not
name is not in ban, and one with no derivatives cannot be. Every active
instrument the master knows is written: ``True`` for the Kite names of a
security the list names (``S``, ``S-BE`` or ``S-BZ``: the main-board series,
because SME stocks have no derivatives), and ``False`` for every other.

**Which file governs T: its own.** QA-E04-07 asks of each list whether the
file NSE writes after a session the calendar does not trade governs the next
day. This one does, and is named for that day. ``fo_secban_02022026`` was
written at 18:17 IST on Sunday 1 Feb 2026, after the Budget session, and
``fo_secban_09112026`` at 17:42 on 8 Nov, after Muhurat (derivatives
14:20-15:40, NSE/FAOP/76940). Each of those sessions has its own file too,
written the evening before it. So T reads ``fo_secban_<T>`` and nothing else.

**What is checked. Any failure refuses the whole file.**

* The first line names T. A day with no ban is NSE's explicit ``: NIL`` on
  that line (30 Jan 2026). A list with no rows and no NIL is a failed fetch
  (SPRINT02 5.1: an empty deny list), and a NIL with rows is neither.
* The rows are numbered 1..n in order, and each is a distinct, valid symbol.
* The body is as long as ``Content-Length`` says. In a deny list a row that
  is not there reads as clean, so a cut-off response is the fail-open case.
  The roster is the opposite: a row missing there leaves its instrument
  unknown. ``HTTPResponse.read(n)`` returns a short body without raising, and
  without the header a body cut at a line boundary looks whole.
* ``Last-Modified`` falls after the calendar's previous session ended and
  before T opens.
* Every security named is in the master. A name the master does not know may
  be a security it holds under another name (a rename, or a stale master),
  and that instrument would otherwise be written clean.

**The alternatives rejected.**

* *NSE Clearing's combined open-interest file* (``combineoi_<DDMMYYYY>.csv``)
  says the same in its ``Limit for Next Day`` column - ``No Fresh Positions``
  - for the session it is named after. There it is a word among numbers in
  one column of an OI report, and a change in what that column means would
  read as no bans. It is the SIT's independent check instead.
* *``False`` only for the F&O stocks.* Every other stock would stay NULL and
  be refused for ever, though it cannot be in ban.
* *Probing the closed days before T* (E04-S02's ``_find``). T's file is named
  for T, so the wrong file is never read. The calendar's previous session
  only bounds when it was written - see what would make this wrong.
* *Lenient parsing.* A list that loses a row still "parses", and the lost
  stock reads as clean.

**The failure it prevents.** An entry in a stock NSE put in ban overnight;
a truncated, empty or stale list read as fewer bans; a fetch failure read as
none - a failed day writes nothing, so the flag stays unknown and every
symbol is refused.

**What would make this wrong.** NSE rewording the first line (refused) or
writing the list after T opens (refused). An intraday ban: none today, but a
circular introducing one would leave the morning's list incomplete for part
of T. NSE writing T's file before a session it held after the calendar's
previous one: the window opens at that earlier session's end, so such a file
would pass. Both such sessions of 2026 were written after, and E04-S11 will
let the window open at the exchange's own last session. A banned security
trading outside EQ, BE and BZ: none of its names is in the master, so the
file is refused - never written clean.

## What is deliberately not here

* **F&O orders.** This system places none. NSE's rule - no new position that
  increases Future-Equivalent open interest - binds only the derivatives.
* **Whether to trade the stock itself.** ``restrictions_from`` refuses it, a
  recorded, conservative assumption. ``v_eligible_today`` flags it without
  excluding it. ``hard_filters.exclude_fno_ban`` (default ``true``) belongs
  to E07-S02, which must either honour ``false`` at execution or refuse it.
* **Held positions.** The ban restricts no holding of the stock itself, so
  nothing is alerted.
* **Scheduling** - the data-sync stage's (E11-S02).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import email.utils
import http.client
import logging
import re
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

from algotrader.common.calendar import (
    IST,
    MARKET_CLOSE,
    MARKET_OPEN,
    MarketCalendar,
    cas_session_on,
)
from algotrader.common.models.trading import _validate_symbol
from algotrader.common.text import MalformedCsvError, csv_rows, one_safe_line
from algotrader.premarket.bhavcopy import BhavcopyArchive
from algotrader.premarket.surveillance import Fetched, SyncOutcome
from algotrader.premarket.universe import kite_tradingsymbol

log = logging.getLogger(__name__)

__all__ = [
    "MAIN_BOARD_SERIES",
    "SOURCE",
    "BanListError",
    "BanListMissingError",
    "BanStore",
    "FnoBanArchive",
    "FnoBanReport",
    "FnoBanResult",
    "FnoBanSync",
    "RepositoryBanStore",
    "check_written",
    "fetch_ban_list",
    "parse_ban_list",
    "resolve_ban_list",
]

#: One list per trade date, named for the date it governs.
SOURCE: Final = "https://nsearchives.nseindia.com/archives/fo/sec_ban/fo_secban_{date}.csv"
_ORIGIN: Final = "https://nsearchives.nseindia.com/"

#: The first line exactly as NSE writes it (read 27 Nov 2026): the trade date
#: as DD-MON-YYYY, a colon, and `` NIL`` when no security is in ban.
_HEADER: Final = re.compile(r"Securities in Ban For Trade Date (\d{2})-([A-Z]{3})-(\d{4}):( NIL)?")
#: English month abbreviations, as NSE writes them - not ``%b``, which is the
#: process locale's.
_MONTHS: Final = {
    name: number
    for number, name in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"),
        start=1,
    )
}
#: Where a security with derivatives can trade: the main board, settling
#: rolling in EQ or trade for trade in BE and BZ (NSE's legend of series).
#: SME stocks (SM, ST, SZ) have no derivatives.
MAIN_BOARD_SERIES: Final = ("EQ", "BE", "BZ")
#: 50 to 75 bytes on the days read; all ~220 F&O stocks at once would be
#: ~4 KB. The bound is for a response that is not a ban list.
MAX_FILE_BYTES: Final = 64 * 1024
#: NSE's archive is a public courtesy: at most one request a second.
FETCH_INTERVAL_SECONDS: Final = 1.0


class BanListError(ValueError):
    """A ban list this system will not use; the message says why."""


class BanListMissingError(BanListError):
    """NSE has no ban list for that trade date (HTTP 404) - yet, or at all."""


def _refuse(why: str) -> BanListError:
    """One line, escaped and bounded: the detail reaches the log and stdout,
    and parts of it come from the file (QA-SEC-30's rule)."""
    return BanListError(one_safe_line(f"F&O ban list refused: {why}"))


def _shown(row: Sequence[str]) -> str:
    """A row from the file, short and quoted, for a refusal message."""
    return repr(",".join(row)[:48])


def _trade_date(day: str, month: str, year: str) -> dt.date:
    number = _MONTHS.get(month)
    if number is None:
        raise _refuse(f"the first line names the month {month!r}, which is not one")
    try:
        return dt.date(int(year), number, int(day))
    except ValueError:
        raise _refuse(f"the first line names {day}-{month}-{year}, which is not a date") from None


def parse_ban_list(raw: bytes, *, trade_date: dt.date) -> tuple[str, ...]:
    """The symbols NSE lists in ban for ``trade_date``, in its order, or
    :class:`BanListError` for the whole file. Empty only for NSE's NIL."""
    if len(raw) > MAX_FILE_BYTES:
        raise _refuse(f"the ban list for {trade_date} exceeds {MAX_FILE_BYTES} bytes")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _refuse(f"the ban list for {trade_date} is not UTF-8 text") from None
    try:
        table = csv_rows(text)
    except MalformedCsvError as exc:
        raise _refuse(f"the ban list for {trade_date} is {exc}") from None
    while table and not table[-1]:
        # Blank lines at the END carry nothing; one between rows is refused
        # below, because it could be where a second list begins.
        table.pop()
    if not table:
        raise _refuse(f"the ban list for {trade_date} is empty")
    first = table[0]
    match = _HEADER.fullmatch(first[0]) if len(first) == 1 else None
    if match is None:
        raise _refuse(
            f"the ban list for {trade_date} does not begin with NSE's line naming its "
            f"trade date: {_shown(first)}"
        )
    day, month, year, nil = match.groups()
    named = _trade_date(day, month, year)
    if named != trade_date:
        raise _refuse(f"the file is the ban list for {named}, not {trade_date}")
    rows = table[1:]
    if nil:
        if rows:
            raise _refuse(
                f"the ban list for {trade_date} says NIL and then lists {len(rows)} row(s)"
            )
        return ()
    if not rows:
        raise _refuse(
            f"the ban list for {trade_date} names no security and does not say NIL: "
            f"a deny list that names nobody is a failed fetch"
        )
    symbols: list[str] = []
    for serial, row in enumerate(rows, start=1):
        line = serial + 1
        if len(row) != 2:
            raise _refuse(f"line {line} is {_shown(row)}, not a number and a symbol")
        number, symbol = row
        if number != str(serial):
            raise _refuse(f"line {line} is numbered {number[:8]!r}, not {serial}")
        try:
            _validate_symbol(symbol)
        except ValueError:
            raise _refuse(f"line {line}: {symbol[:24]!r} is not a symbol") from None
        if symbol in symbols:
            raise _refuse(f"line {line}: {symbol!r} is listed twice")
        symbols.append(symbol)
    return tuple(symbols)


def resolve_ban_list(banned: Sequence[str], *, master: frozenset[str]) -> frozenset[str]:
    """The master's names for the banned securities - every main-board series
    each one has there - or :class:`BanListError` naming any it has none for."""
    flagged: set[str] = set()
    unknown: list[str] = []
    for symbol in banned:
        names = {kite_tradingsymbol(symbol, series) for series in MAIN_BOARD_SERIES} & master
        if not names:
            unknown.append(symbol)
        flagged |= names
    if unknown:
        raise _refuse(
            f"the instrument master has no instrument for {len(unknown)} banned "
            f"security(ies) ({', '.join(unknown[:8])}): a stale master, or a renamed "
            f"security, whose instrument would be written clean"
        )
    return frozenset(flagged)


# ---------------------------------------------------------------------------
# Fetching and archiving
# ---------------------------------------------------------------------------

Opener = Callable[[urllib.request.Request, float], Any]


def _open(request: urllib.request.Request, timeout: float) -> Any:
    """``urlopen`` for NSE's archive origin and nothing else (bandit B310)."""
    if not request.full_url.startswith(_ORIGIN):
        raise _refuse(f"refusing to open {request.full_url[:48]!r}: not {_ORIGIN}")
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310  # nosec B310 - origin checked above


def _http_date(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _check_length(headers: Any, body: bytes, trade_date: dt.date) -> None:
    """The whole body or a refusal. ``Content-Length`` must match; a chunked
    body is checked by ``http.client`` itself (a cut chunk is ``IncompleteRead``);
    a body that ends only when the connection does cannot be told whole from cut."""
    declared = headers.get("Content-Length")
    if declared is not None:
        if not declared.isdigit() or int(declared) != len(body):
            raise _refuse(
                f"NSE declared {declared[:12]!r} bytes for the {trade_date} ban list and sent "
                f"{len(body)}: a cut-off list reads as fewer bans"
            )
        return
    if "chunked" not in str(headers.get("Transfer-Encoding", "")).lower():
        raise _refuse(
            f"NSE sent the {trade_date} ban list with no length and unchunked, so a "
            f"cut-off list cannot be told from a whole one"
        )


def fetch_ban_list(
    trade_date: dt.date, *, opener: Opener = _open, timeout: float = 30.0
) -> Fetched:
    """The list for ``trade_date``. A 404 is :class:`BanListMissingError`."""
    url = SOURCE.format(date=trade_date.strftime("%d%m%Y"))
    request = urllib.request.Request(  # noqa: S310 - fixed https URL
        url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept": "*/*"}
    )
    try:
        with opener(request, timeout) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise _refuse(f"NSE answered HTTP {status} for the {trade_date} ban list")
            body: bytes = response.read(MAX_FILE_BYTES + 1)
            if len(body) > MAX_FILE_BYTES:
                raise _refuse(f"the response for {trade_date} exceeds {MAX_FILE_BYTES} bytes")
            _check_length(response.headers, body, trade_date)
            modified = _http_date(response.headers.get("Last-Modified"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise BanListMissingError(
                f"NSE has no F&O ban list for {trade_date} (HTTP 404)"
            ) from None
        raise _refuse(f"NSE answered HTTP {exc.code} for the {trade_date} ban list") from None
    except (OSError, http.client.HTTPException) as exc:
        # URLError is an OSError, raised by the open. A failure while READING
        # the body - a timeout, a reset, a short chunk - is a bare OSError or
        # HTTPException that urllib does not wrap (QA-E04-12).
        raise _refuse(
            f"the ban list for {trade_date} could not be fetched ({type(exc).__name__})"
        ) from exc
    return Fetched(body=body, last_modified=modified)


def _session_end(day: dt.date) -> dt.datetime:
    """The last moment anything executes on ``day``: the close, or that day's
    closing-auction end if later (15:35 from 3 Aug 2026, 15:40 from 7 Dec)."""
    cas = cas_session_on(day)
    end = MARKET_CLOSE if cas is None else max(MARKET_CLOSE, cas.auction_end)
    return dt.datetime.combine(day, end, tzinfo=IST)


def check_written(last_modified: dt.datetime | None, *, after: dt.date, governs: dt.date) -> None:
    """NSE must have written ``governs``' list after the session ``after``
    ended - its closing open interest is what the list is made from - and
    before ``governs`` opened."""
    if last_modified is None:
        raise _refuse(
            f"NSE gave no Last-Modified for the {governs} ban list, so when it was written is "
            f"unknown"
        )
    ended = _session_end(after)
    opens = dt.datetime.combine(governs, MARKET_OPEN, tzinfo=IST)
    if not ended < last_modified < opens:
        written = last_modified.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")
        raise _refuse(
            f"the {governs} ban list was written {written}, outside {ended:%Y-%m-%d %H:%M} - "
            f"{opens:%Y-%m-%d %H:%M} IST: it is not the list made after {after}'s session"
        )


class FnoBanArchive(BhavcopyArchive):
    """The raw lists under ``root/YYYY/``, written - atomically, as the
    bhavcopy's are - only once a list has been accepted."""

    def path(self, day: dt.date) -> Path:
        return self._root / f"{day.year:04d}" / f"fo_secban_{day:%d%m%Y}.csv"


# ---------------------------------------------------------------------------
# One trade date, or a range of them
# ---------------------------------------------------------------------------


class BanStore(Protocol):
    """Where the day's flags go (``RepositoryBanStore`` over the real tables)."""

    async def active_symbols(self) -> frozenset[str]: ...

    async def write(self, trade_date: dt.date, flags: Mapping[str, bool]) -> int: ...


@dataclass(frozen=True)
class FnoBanResult:
    trade_date: dt.date
    outcome: SyncOutcome
    detail: str = ""
    #: ``archive`` or ``network``.
    source: str = ""
    written: int = 0
    #: The securities NSE lists in ban for ``trade_date``, as it names them.
    banned: tuple[str, ...] = ()
    #: The instruments written ``True``: every master name of those securities.
    flagged: int = 0


@dataclass
class FnoBanReport:
    results: list[FnoBanResult] = field(default_factory=list)

    def of(self, outcome: SyncOutcome) -> list[FnoBanResult]:
        return [r for r in self.results if r.outcome is outcome]

    @property
    def ok(self) -> bool:
        """Every session's flags written: nothing missing, nothing refused."""
        return not self.of(SyncOutcome.MISSING) and not self.of(SyncOutcome.REFUSED)


class FnoBanSync:
    """Fetches (politely), checks, parses, archives and writes the ban lists."""

    def __init__(
        self,
        *,
        calendar: MarketCalendar,
        archive: FnoBanArchive,
        store: BanStore,
        fetch: Callable[[dt.date], Fetched] = fetch_ban_list,
        offline: bool = False,
        min_interval: float = FETCH_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        if min_interval < FETCH_INTERVAL_SECONDS:
            raise ValueError(
                f"min_interval {min_interval} is below {FETCH_INTERVAL_SECONDS}s: NSE's archive "
                f"is a public courtesy, and an impolite client is how a host gets refused"
            )
        self._calendar = calendar
        self._archive = archive
        self._store = store
        self._fetch = fetch
        #: Archive only: a re-run must not touch the network.
        self._offline = offline
        self._interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._last_fetch: float | None = None

    async def _polite_fetch(self, trade_date: dt.date) -> Fetched:
        if self._last_fetch is not None:
            wait = self._interval - (self._clock() - self._last_fetch)
            if wait > 0:
                await self._sleep(wait)
        try:
            return await asyncio.to_thread(self._fetch, trade_date)
        finally:
            self._last_fetch = self._clock()

    async def sync_day(
        self, trade_date: dt.date, *, master: frozenset[str] | None = None
    ) -> FnoBanResult:
        """Trade date T's flags, from the list NSE wrote for T. Never raises for
        a bad list - the result says what happened, and a result that is not
        STORED wrote nothing. Raises ``HolidayDataError`` when the session
        before T is in a year the calendar does not cover."""
        if not self._calendar.is_trading_day(trade_date):
            return FnoBanResult(trade_date, SyncOutcome.NOT_A_TRADING_DAY)
        previous = self._calendar.previous_trading_day(trade_date)
        raw = self._archive.read(trade_date)
        source = "archive"
        if raw is None:
            if self._offline:
                return FnoBanResult(
                    trade_date,
                    SyncOutcome.MISSING,
                    f"the {trade_date} ban list is not in the archive (offline run)",
                )
            source = "network"
            try:
                fetched = await self._polite_fetch(trade_date)
                check_written(fetched.last_modified, after=previous, governs=trade_date)
            except BanListMissingError as exc:
                return FnoBanResult(trade_date, SyncOutcome.MISSING, str(exc), source)
            except BanListError as exc:
                return FnoBanResult(trade_date, SyncOutcome.REFUSED, str(exc), source)
            raw = fetched.body
        try:
            banned = parse_ban_list(raw, trade_date=trade_date)
            symbols = master if master is not None else await self._store.active_symbols()
            if not symbols:
                raise _refuse("the instrument master is empty, so there is nothing to flag")
            flagged = resolve_ban_list(banned, master=symbols)
        except BanListError as exc:
            return FnoBanResult(trade_date, SyncOutcome.REFUSED, str(exc), source)
        if source == "network":
            self._archive.write(trade_date, raw)
        written = await self._store.write(trade_date, {name: name in flagged for name in symbols})
        return FnoBanResult(
            trade_date,
            SyncOutcome.STORED,
            source=source,
            written=written,
            banned=banned,
            flagged=len(flagged),
        )

    async def backfill(
        self,
        start: dt.date,
        end: dt.date,
        *,
        progress: Callable[[FnoBanResult], object] | None = None,
    ) -> FnoBanReport:
        """Every trading day in ``[start, end]``. Resumable: an archived list is
        read from disk, and a written day is rewritten to the same values.

        Raises before any request if the range is inverted or reaches a year
        the calendar does not cover - including the year before ``start`` when
        ``start`` is a year's first session, whose list was written after the
        previous year's last.
        """
        days = self._calendar.trading_days_between(start, end)
        master = await self._store.active_symbols()
        report = FnoBanReport()
        for day in days:
            result = await self.sync_day(day, master=master)
            report.results.append(result)
            if result.outcome not in (SyncOutcome.STORED, SyncOutcome.NOT_A_TRADING_DAY):
                log.error(
                    "F&O ban %s: %s - %s; its flags stay unknown and nothing trades on it",
                    day,
                    result.outcome.value,
                    result.detail,
                )
            if progress is not None:
                progress(result)
        return report


class RepositoryBanStore:
    """:class:`BanStore` over the real tables: one transaction per day."""

    def __init__(self, factory: Any) -> None:
        self._factory = factory

    async def active_symbols(self) -> frozenset[str]:
        from algotrader.common.db.engine import read_only
        from algotrader.common.db.repositories import InstrumentRepository

        async with read_only(self._factory) as session:
            return await InstrumentRepository(session).active_symbols()

    async def write(self, trade_date: dt.date, flags: Mapping[str, bool]) -> int:
        from algotrader.common.db.engine import unit_of_work
        from algotrader.common.db.repositories import DailyStatusRepository, InstrumentRepository

        if not flags:
            return 0
        async with unit_of_work(self._factory) as session:
            instruments = InstrumentRepository(session)
            await instruments.refresh_cache()
            records = [
                {
                    "symbol_id": await instruments.symbol_id(name),
                    "trade_date": trade_date,
                    "is_fno_ban": banned,
                }
                for name, banned in sorted(flags.items())
            ]
            return await DailyStatusRepository(session, instruments).upsert(records)
