"""The NSE bhavcopy: each session's official end-of-day prices, stored as daily bars (E03-S01).

The bhavcopy is the exchange's own record of a session - open, high, low,
close, volume, turnover and trade count for every security in the capital
market segment. Stored locally as ``1d`` bars, it is what the pre-market
analysis, the daily correlations (E15-S14) and every backtest read, so none of
them depends on a rate-limited broker API each morning.

## The design record

**The decision.** One file per trading day - the UDiFF capital-market
bhavcopy from ``nsearchives.nseindia.com`` - parsed strictly (the exact
header, the file's OWN trade date, every row a coherent bar, a plausible row
count) into daily bars for the instruments in the master, stored through
``BarRepository.bulk_upsert``. The raw zip is archived on disk once it has
parsed, so a backfill resumes from disk and a re-run fetches nothing.

**The alternatives rejected.**

* *Kite's historical API for daily candles.* It needs the owner's paid Connect
  plan, it is rate-limited, and a broker's candles are not the exchange's
  record. The store is raw prices plus adjustment factors (BR-15); the
  bhavcopy IS the raw price.
* *The legacy bhavcopy path* (``cmDDMONYYYYbhav.csv.zip``). NSE moved to the
  UDiFF format in July 2024; a fetcher against the old path 404s, or worse
  reads a stale mirror. The UDiFF file exists for 22 Nov 2024 (checked
  23 Nov 2026), so the format covers any backfill this system needs.
* *Lenient parsing* - skipping a bad row and storing the rest. A partial day
  looks complete: a missing close reads as "no data" for one stock, nothing
  says the file was damaged, and the gap is permanent. One bad row refuses the
  file (SPRINT02_TECHNICAL_SPEC 5.1: "a parse failure must raise").
* *Inferring holidays from missing files.* NSE serves an HTML page with a 404
  for a holiday and for a file it has not published, and the two cannot be
  told apart from the response. The calendar decides what is a trading day;
  a 404 on one is a MISSING day, reported, never skipped.

**The failure it prevents.** A stale file stored as today's prices (a mirror
serving yesterday's file under today's name - caught by the file's own
``TradDt``); a truncated or wrong download stored as a quiet partial day
(caught by the header, the row floor and the master's resolution share); a
holiday treated as a gap, or a gap treated as a holiday.

**What would make this wrong.** NSE changing the UDiFF header or URL (both
refuse loudly - update ``HEADER`` / ``SOURCE`` and the recorded fixture); NSE
revising a published file after the fact (``bulk_upsert`` keeps the first
stored prices and logs the disagreement; ``revise=True`` is the deliberate
correction, not used here); an instrument missing from the master (its rows
are counted as unresolved, so its daily series has gaps, which every consumer
refuses on rather than guessing through).

## What is deliberately not here

* **Special sessions.** NSE publishes a bhavcopy for the Diwali Muhurat
  session (8 Nov 2026 is one); the calendar stands the system down that day,
  so no request is made and no bar stored - an hour-long session would distort
  the daily ranges ATR is built from.
* **Scheduling at 05:30** (task 6) is the data-sync stage's (E11-S02).
* **History before the calendar's first covered year.** The calendar refuses an
  uncovered year (HolidayDataError) rather than guess its holidays, so the
  backfill reaches back only as far as ``config/nse_holidays.yaml`` covers.
* **Corporate actions.** The bhavcopy is UNADJUSTED; ``bulk_upsert`` recomputes
  the factors of any symbol with a later ex-date in the same transaction, and
  the actions themselves are E04-S06's.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import logging
import os
import re
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Protocol

from pydantic import ValidationError

from algotrader.common.calendar import MarketCalendar
from algotrader.common.enums import Timeframe
from algotrader.common.models.market import Bar
from algotrader.common.models.trading import _validate_symbol
from algotrader.common.text import MalformedCsvError, csv_rows, one_safe_line
from algotrader.premarket.universe import kite_tradingsymbol

log = logging.getLogger(__name__)

__all__ = [
    "HEADER",
    "SOURCE",
    "BackfillReport",
    "BhavcopyArchive",
    "BhavcopyError",
    "BhavcopyLoader",
    "BhavcopyMissingError",
    "BhavcopyRow",
    "DailyBars",
    "DayOutcome",
    "DayResult",
    "fetch_bhavcopy",
    "parse_bhavcopy",
    "to_daily_bars",
]

#: The capital-market UDiFF bhavcopy, one per session. Fixed, https only.
SOURCE: Final = (
    "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{date}_F_0000.csv.zip"
)
#: The one file inside the zip.
MEMBER: Final = "BhavCopy_NSE_CM_0_0_0_{date}_F_0000.csv"
_ORIGIN: Final = "https://nsearchives.nseindia.com/"

#: The UDiFF header, exactly as published (read 23 Nov 2026 from the 20 Nov file).
HEADER: Final = (
    "TradDt", "BizDt", "Sgmt", "Src", "FinInstrmTp", "FinInstrmId", "ISIN", "TckrSymb",
    "SctySrs", "XpryDt", "FininstrmActlXpryDt", "StrkPric", "OptnTp", "FinInstrmNm",
    "OpnPric", "HghPric", "LwPric", "ClsPric", "LastPric", "PrvsClsgPric", "UndrlygPric",
    "SttlmPric", "OpnIntrst", "ChngInOpnIntrst", "TtlTradgVol", "TtlTrfVal",
    "TtlNbOfTxsExctd", "SsnId", "NewBrdLotQty", "Rmks", "Rsvd1", "Rsvd2", "Rsvd3", "Rsvd4",
)  # fmt: skip
_COL: Final = {name: i for i, name in enumerate(HEADER)}

#: A real day is ~210 KB zipped and ~650 KB unzipped. The bounds are for the
#: response that is not a bhavcopy, and for a zip that expands without limit.
MAX_ZIP_BYTES: Final = 4 * 1024 * 1024
MAX_CSV_BYTES: Final = 16 * 1024 * 1024

#: A real day lists ~3,700 securities (3,742 on 20 Nov 2026). Fewer than this is
#: a truncated file, not a quiet market.
MIN_ROWS: Final = 1000
#: The share of the file's EQ-series rows the master must know. 2,759 of 2,760
#: resolved on 20 Nov 2026; below this the master is stale or the file is wrong.
MIN_EQ_RESOLVED_SHARE: Final = 0.8
#: NSE's archive is a public courtesy: at most one request a second.
FETCH_INTERVAL_SECONDS: Final = 1.0

_PRICE: Final = re.compile(r"\d{1,9}(?:\.\d{1,4})?")
#: Turnover runs to eleven integer digits on a liquid stock (RELIANCE traded
#: Rs 17,998,974,119.90 on 20 Nov 2026) - a price pattern would refuse it.
_AMOUNT: Final = re.compile(r"\d{1,18}(?:\.\d{1,4})?")
_COUNT: Final = re.compile(r"\d{1,15}")
_SERIES: Final = re.compile(r"[A-Z0-9]{2}")
_VWAP_PLACES: Final = Decimal("0.0001")


class BhavcopyError(ValueError):
    """The file is not a bhavcopy this system will store. Never partial."""


class BhavcopyMissingError(BhavcopyError):
    """NSE has no file for the date (HTTP 404) - on a trading day, a gap."""


def _refuse(why: str) -> BhavcopyError:
    return BhavcopyError(one_safe_line(why))


@dataclass(frozen=True)
class BhavcopyRow:
    symbol: str
    series: str
    isin: str
    #: The exchange token (``FinInstrmId``) - the same number Kite lists.
    token: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    turnover: Decimal
    #: ``None`` when the file leaves the trade count blank - unknown, not zero.
    trades: int | None

    @property
    def tradingsymbol(self) -> str:
        return kite_tradingsymbol(self.symbol, self.series)

    @property
    def traded(self) -> bool:
        return self.volume > 0 and min(self.open, self.high, self.low, self.close) > 0


def _decimal(text: str, line: int, column: str, pattern: re.Pattern[str] = _PRICE) -> Decimal:
    text = text.strip()
    if not pattern.fullmatch(text):
        raise _refuse(f"line {line}: {column} is {text[:16]!r}, not a number this file carries")
    try:
        return Decimal(text)
    except InvalidOperation:  # pragma: no cover - the pattern admits only digits
        raise _refuse(f"line {line}: {column} is not a number") from None


def _count(text: str, line: int, column: str) -> int:
    text = text.strip()
    if not _COUNT.fullmatch(text):
        raise _refuse(f"line {line}: {column} is {text[:16]!r}, not a count")
    return int(text)


def _the_csv(raw: bytes, day: dt.date) -> str:
    if len(raw) > MAX_ZIP_BYTES:
        raise _refuse(f"the file is {len(raw)} bytes; a bhavcopy is not")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise _refuse("the file is not a zip (an HTML page answers for a missing file)") from None
    with archive:
        members = archive.infolist()
        expected = MEMBER.format(date=day.strftime("%Y%m%d"))
        if [m.filename for m in members] != [expected]:
            names = [m.filename[:48] for m in members[:3]]
            raise _refuse(f"the zip holds {names}, not exactly {expected!r}")
        member = members[0]
        if member.file_size > MAX_CSV_BYTES:
            raise _refuse(f"the zip's file expands to {member.file_size} bytes")
        with archive.open(member) as handle:
            body = handle.read(MAX_CSV_BYTES + 1)
    if len(body) > MAX_CSV_BYTES:
        raise _refuse("the zip's file is larger than it declares")
    try:
        return body.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _refuse("the bhavcopy is not UTF-8 text") from None


def parse_bhavcopy(raw: bytes, *, day: dt.date) -> tuple[BhavcopyRow, ...]:
    """Every row of the zipped bhavcopy for ``day``, or a refusal saying why.

    The file must be ``day``'s: every row's ``TradDt`` is checked, because a
    mirror serving yesterday's file under today's name is a plausible, silent
    failure. A row that is not well-formed refuses the whole file.
    """
    try:
        rows = csv_rows(_the_csv(raw, day))
    except MalformedCsvError as exc:
        raise _refuse(f"the bhavcopy for {day} is {exc}") from None
    if not rows or tuple(cell.strip() for cell in rows[0]) != HEADER:
        shown = [cell.strip()[:16] for cell in rows[0][:4]] if rows else []
        raise _refuse(f"the header begins {shown!r}, not the UDiFF bhavcopy's {list(HEADER[:4])!r}")
    iso = day.isoformat()
    found: list[BhavcopyRow] = []
    seen: set[str] = set()
    for line, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue
        if len(row) != len(HEADER):
            raise _refuse(f"line {line} has {len(row)} fields, not {len(HEADER)}")

        def cell(name: str, row: list[str] = row) -> str:
            return row[_COL[name]].strip()

        if cell("TradDt") != iso:
            raise _refuse(
                f"line {line} is for {cell('TradDt')[:12]!r}, not {iso}: a stale or wrong file"
            )
        if cell("Sgmt") != "CM" or cell("Src") != "NSE":
            raise _refuse(
                f"line {line} is {cell('Sgmt')[:6]}/{cell('Src')[:6]}, not NSE's capital market"
            )
        try:
            symbol = _validate_symbol(cell("TckrSymb"))
        except ValueError:
            raise _refuse(f"line {line} has an unusable symbol") from None
        series = cell("SctySrs")
        if not _SERIES.fullmatch(series):
            raise _refuse(f"line {line} has series {series[:8]!r}")
        parsed = BhavcopyRow(
            symbol=symbol,
            series=series,
            isin=cell("ISIN")[:12],
            token=_count(cell("FinInstrmId"), line, "FinInstrmId"),
            open=_decimal(cell("OpnPric"), line, "OpnPric"),
            high=_decimal(cell("HghPric"), line, "HghPric"),
            low=_decimal(cell("LwPric"), line, "LwPric"),
            close=_decimal(cell("ClsPric"), line, "ClsPric"),
            volume=_count(cell("TtlTradgVol"), line, "TtlTradgVol"),
            turnover=_decimal(cell("TtlTrfVal"), line, "TtlTrfVal", _AMOUNT),
            # Unknown, not zero, when the file leaves it blank: a trade count of
            # 0 beside a non-zero volume would be a fact this file did not state.
            trades=(
                _count(cell("TtlNbOfTxsExctd"), line, "TtlNbOfTxsExctd")
                if cell("TtlNbOfTxsExctd")
                else None
            ),
        )
        if parsed.tradingsymbol in seen:
            raise _refuse(f"the file lists {parsed.tradingsymbol} twice")
        seen.add(parsed.tradingsymbol)
        found.append(parsed)
    if len(found) < MIN_ROWS:
        raise _refuse(
            f"the file lists {len(found)} securities; a session lists thousands - truncated"
        )
    return tuple(found)


@dataclass(frozen=True)
class DailyBars:
    day: dt.date
    #: One validated ``1d`` bar per traded instrument the master knows.
    bars: tuple[Bar, ...]
    #: In the master, and did not trade that session - no bar, by design.
    untraded: tuple[str, ...]
    #: In the file, not in the master - counted, never stored.
    unresolved: tuple[str, ...]


def _vwap(row: BhavcopyRow) -> Decimal | None:
    """Turnover over volume, or ``None`` when it cannot be trusted.

    ``None`` - not a guess - when there was no volume, or when the quotient
    falls outside the day's range, which only a damaged turnover can produce.
    """
    if row.volume <= 0:
        return None
    vwap = (row.turnover / row.volume).quantize(_VWAP_PLACES, rounding=ROUND_HALF_EVEN)
    return vwap if row.low <= vwap <= row.high else None


def to_daily_bars(
    rows: Iterable[BhavcopyRow],
    *,
    day: dt.date,
    master: Iterable[str],
    calendar: MarketCalendar,
    min_eq_resolved_share: float = MIN_EQ_RESOLVED_SHARE,
) -> DailyBars:
    """The session's ``1d`` bars, timestamped at its 09:15 IST open.

    A row the ``Bar`` model refuses (high below low, a close outside the range)
    refuses the file: the exchange's official record does not contain one.
    """
    known = frozenset(master)
    if not known:
        raise _refuse("the instrument master is empty; nothing can be resolved")
    open_ts, _close = calendar.session_bounds(day)
    rows = tuple(rows)
    bars: list[Bar] = []
    untraded: list[str] = []
    unresolved: list[str] = []
    eq_rows = eq_known = 0
    for row in rows:
        name = row.tradingsymbol
        if row.series == "EQ":
            eq_rows += 1
            eq_known += name in known
        if name not in known:
            unresolved.append(name)
            continue
        if not row.traded:
            untraded.append(name)
            continue
        try:
            bars.append(
                Bar(
                    symbol=name,
                    timeframe=Timeframe.D1,
                    open_ts=open_ts,
                    open=row.open,
                    high=row.high,
                    low=row.low,
                    close=row.close,
                    volume=row.volume,
                    trade_count=row.trades,
                    vwap=_vwap(row),
                )
            )
        except ValidationError as exc:
            reason = exc.errors()[0]["msg"] if exc.errors() else "invalid"
            raise _refuse(
                f"{name}'s row is not a coherent bar ({one_safe_line(str(reason))[:80]})"
            ) from None
    # A file with no EQ rows at all is not a session's bhavcopy; skipping the
    # check when there are none would let exactly that file through.
    if eq_rows == 0 or eq_known / eq_rows < min_eq_resolved_share:
        raise _refuse(
            f"only {eq_known} of {eq_rows} EQ rows are in the instrument master; "
            f"the master is stale or the file is wrong"
        )
    return DailyBars(
        day=day, bars=tuple(bars), untraded=tuple(untraded), unresolved=tuple(unresolved)
    )


# ---------------------------------------------------------------------------
# Fetching and archiving
# ---------------------------------------------------------------------------

Opener = Callable[[urllib.request.Request, float], Any]


def _open(request: urllib.request.Request, timeout: float) -> Any:
    """``urlopen`` for NSE's archive origin and nothing else (bandit B310).

    The URL is a constant; this makes that a property of the function rather
    than of its callers (``test_bhavcopy.py::TestTheOpenerRefuses``).
    """
    if not request.full_url.startswith(_ORIGIN):
        raise _refuse(f"refusing to open {request.full_url[:48]!r}: not {_ORIGIN}")
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310  # nosec B310 - origin checked above


def fetch_bhavcopy(day: dt.date, *, opener: Opener = _open, timeout: float = 30.0) -> bytes:
    """The zipped bhavcopy for ``day``. A 404 is :class:`BhavcopyMissingError`."""
    url = SOURCE.format(date=day.strftime("%Y%m%d"))
    request = urllib.request.Request(  # noqa: S310 - fixed https URL
        url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept": "*/*"}
    )
    try:
        with opener(request, timeout) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise _refuse(f"NSE answered HTTP {status} for {day}")
            body: bytes = response.read(MAX_ZIP_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise BhavcopyMissingError(f"NSE has no bhavcopy for {day} (HTTP 404)") from None
        raise _refuse(f"NSE answered HTTP {exc.code} for {day}") from None
    except urllib.error.URLError as exc:
        raise _refuse(
            f"the bhavcopy for {day} could not be fetched ({type(exc).__name__})"
        ) from exc
    if len(body) > MAX_ZIP_BYTES:
        raise _refuse(f"the response for {day} exceeds {MAX_ZIP_BYTES} bytes")
    if not body.startswith(b"PK"):
        raise _refuse(
            f"the response for {day} is not a zip (NSE answers a page for a missing file)"
        )
    return body


class BhavcopyArchive:
    """The raw zips, one per session, under ``root/YYYY/``. Written only after a
    file has parsed, so the archive never holds one this system refused."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    def path(self, day: dt.date) -> Path:
        name = Path(SOURCE.format(date=day.strftime("%Y%m%d"))).name
        return self._root / f"{day.year:04d}" / name

    def read(self, day: dt.date) -> bytes | None:
        path = self.path(day)
        return path.read_bytes() if path.is_file() else None

    def write(self, day: dt.date, raw: bytes) -> Path:
        """Atomically: a reader sees the whole file or none of it."""
        path = self.path(day)
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + ".partial")
        partial.write_bytes(raw)
        os.replace(partial, path)
        return path


# ---------------------------------------------------------------------------
# Loading: one session, or a range of them
# ---------------------------------------------------------------------------


class BarStore(Protocol):
    """Where daily bars go (``RepositoryBarStore`` over the real tables)."""

    async def active_symbols(self) -> frozenset[str]: ...

    async def store(self, bars: Sequence[Bar]) -> int: ...


class DayOutcome(StrEnum):
    STORED = "STORED"
    #: The calendar says there was no session; nothing was requested.
    NOT_A_TRADING_DAY = "NOT_A_TRADING_DAY"
    #: A session, and no file - a gap in the history, reported.
    MISSING = "MISSING"
    #: A file this system will not store; the reason says why.
    REFUSED = "REFUSED"


@dataclass(frozen=True)
class DayResult:
    day: dt.date
    outcome: DayOutcome
    detail: str = ""
    bars: int = 0
    untraded: int = 0
    unresolved: int = 0
    #: ``archive`` or ``network`` - where the file came from.
    source: str = ""


@dataclass
class BackfillReport:
    results: list[DayResult] = field(default_factory=list)

    def of(self, outcome: DayOutcome) -> list[DayResult]:
        return [r for r in self.results if r.outcome is outcome]

    @property
    def ok(self) -> bool:
        """Every session stored: nothing missing, nothing refused."""
        return not self.of(DayOutcome.MISSING) and not self.of(DayOutcome.REFUSED)

    @property
    def bars(self) -> int:
        return sum(r.bars for r in self.results)


class BhavcopyLoader:
    """Fetches (politely), parses, archives and stores bhavcopies."""

    def __init__(
        self,
        *,
        calendar: MarketCalendar,
        archive: BhavcopyArchive,
        store: BarStore,
        fetch: Callable[[dt.date], bytes] = fetch_bhavcopy,
        offline: bool = False,
        min_interval: float = FETCH_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
        min_eq_resolved_share: float = MIN_EQ_RESOLVED_SHARE,
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
        self._share = min_eq_resolved_share
        self._last_fetch: float | None = None

    async def _polite_fetch(self, day: dt.date) -> bytes:
        if self._last_fetch is not None:
            wait = self._interval - (self._clock() - self._last_fetch)
            if wait > 0:
                await self._sleep(wait)
        try:
            return await asyncio.to_thread(self._fetch, day)
        finally:
            self._last_fetch = self._clock()

    async def sync_day(self, day: dt.date, *, master: frozenset[str] | None = None) -> DayResult:
        """One session: archive or network, parse, then store. Never raises for
        a bad file - the result says what happened."""
        if not self._calendar.is_trading_day(day):
            return DayResult(day, DayOutcome.NOT_A_TRADING_DAY)
        raw = self._archive.read(day)
        source = "archive"
        if raw is None:
            if self._offline:
                return DayResult(day, DayOutcome.MISSING, "not in the archive (offline run)")
            source = "network"
            try:
                raw = await self._polite_fetch(day)
            except BhavcopyMissingError as exc:
                return DayResult(day, DayOutcome.MISSING, str(exc), source=source)
            except BhavcopyError as exc:
                return DayResult(day, DayOutcome.REFUSED, str(exc), source=source)
        try:
            rows = parse_bhavcopy(raw, day=day)
            daily = to_daily_bars(
                rows,
                day=day,
                master=master if master is not None else await self._store.active_symbols(),
                calendar=self._calendar,
                min_eq_resolved_share=self._share,
            )
        except BhavcopyError as exc:
            return DayResult(day, DayOutcome.REFUSED, str(exc), source=source)
        if source == "network":
            self._archive.write(day, raw)
        written = await self._store.store(daily.bars)
        return DayResult(
            day,
            DayOutcome.STORED,
            bars=written,
            untraded=len(daily.untraded),
            unresolved=len(daily.unresolved),
            source=source,
        )

    async def backfill(
        self,
        start: dt.date,
        end: dt.date,
        *,
        progress: Callable[[DayResult], object] | None = None,
    ) -> BackfillReport:
        """Every trading day in ``[start, end]``. Resumable: an archived day is
        read from disk; a stored day is upserted to the same rows (idempotent).

        Raises before any request if the range is inverted or reaches a year the
        holiday calendar does not cover - a backfill that guessed holidays would
        record gaps as holidays, permanently.
        """
        days = self._calendar.trading_days_between(start, end)
        master = await self._store.active_symbols()
        report = BackfillReport()
        for day in days:
            result = await self.sync_day(day, master=master)
            report.results.append(result)
            if result.outcome is not DayOutcome.STORED:
                log.warning("bhavcopy %s: %s - %s", day, result.outcome.value, result.detail)
            if progress is not None:
                progress(result)
        return report


class RepositoryBarStore:
    """:class:`BarStore` over the real tables: one transaction per session."""

    def __init__(self, factory: Any) -> None:
        self._factory = factory

    async def active_symbols(self) -> frozenset[str]:
        from algotrader.common.db.engine import read_only
        from algotrader.common.db.repositories import InstrumentRepository

        async with read_only(self._factory) as session:
            return await InstrumentRepository(session).active_symbols()

    async def store(self, bars: Sequence[Bar]) -> int:
        from algotrader.common.db.engine import unit_of_work
        from algotrader.common.db.repositories import BarRepository, InstrumentRepository

        if not bars:
            return 0
        async with unit_of_work(self._factory) as session:
            instruments = InstrumentRepository(session)
            await instruments.refresh_cache()
            rows = [
                {
                    "symbol_id": await instruments.symbol_id(bar.symbol),
                    "timeframe": bar.timeframe.value,
                    "ts": bar.open_ts,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                    "trade_count": bar.trade_count,
                    "vwap": bar.vwap,
                }
                for bar in bars
            ]
            return await BarRepository(session, instruments).bulk_upsert(rows)
