"""NSE's surveillance roster: ASM, GSM and T2T per symbol per day (E04-S01, S02).

A stock under the Additional (ASM) or Graded (GSM) Surveillance Measure trades
with 100% margin, a narrowed band, and at the higher stages trade-for-trade or
a weekly call auction with no upward movement. A stock in a trade-for-trade
(T2T) series must be delivered: an intraday order in it is refused, or becomes
a delivery obligation. ``restrictions_from`` blocks every such symbol; this
module is what tells it which ones they are.

## The design record

**The decision (E04-S01).** The ASM and GSM flags for trade date T come from
one file: NSE's *Surveillance Indicator* roster, ``REG1_IND<DDMMYY>.csv`` under
``nsearchives.nseindia.com/content/cm/`` (the
``CM-SURVEILLANCE-INDICATOR-NEW-CSV`` entry of NSE's daily-reports index).
NSE writes it the evening of each session - ``Last-Modified`` 19:24 IST on
20 Nov 2026, 20:10 IST on 23 Nov, 19:26 IST on 2 Jan - and it carries the
measures in force **from the next session**. Verified 24 Nov 2026 against the
circulars: the 20 Nov roster already lists the ST-ASM entries NSE/SURV/77539
(20 Nov) made "w.e.f. November 23" (APEXECO, CADSYS, QLINE) and drops its
exits; the 23 Nov roster carries NSE/SURV/77569 and 77570's changes "w.e.f.
November 25" (EMPOWER, ASPIRE and HINDADH in; JYOTISTRUC, ORCHASP, SOTAC and
SPCENET out). Both FAQs say a measure is announced "after market hours
typically one trading day before the effective date" - which is this file.
It is parsed strictly and written for every instrument it names.

**The decision (E04-S02): T2T is the roster's ``Series``.** The roster lists
every security with the series it trades in from the next session - checked
25 Nov 2026 against every 2026 bhavcopy: of 606,785 roster rows whose series
did not change, all agree with the next session's trading, and of the 1,783
that did, 1,781 carry the NEW series (the other two are the next decision's).
NSE's legend of series (updated 19 Sep 2024) settles which are which: equity
and ETFs settle rolling in EQ and trade for trade in BE and BZ; SME equity
rolling in SM and trade for trade in ST and SZ - the six series every 2026
roster carries. Any other series is unknown (NULL, refused). The Kite name
carries the series (``HFCL-BE``), so a security whose series the roster had
wrong would land on an instrument with no row - refused - never on a clean
one.

**The decision (E04-S02): T reads the LAST roster NSE wrote before T opened -
which is not always the calendar's previous session.** NSE held live sessions
on Sunday 1 Feb 2026 (the Union Budget, NSE/CMTR/72349, normal hours) and
Sunday 8 Nov 2026 (Muhurat), wrote a roster after each, and the calendar
trades neither. Read the calendar's way, 2 Feb took 30 Jan's roster and wrote
COOLCAPS, ESSENTIA, MANORAMA, RCDL and SYSTMTXC clean on the first day of
their short-term ASM (NSE's notice of 1 Feb 2026) - a fail-open E04-S01
shipped (QA-E04-07). So every closed day since the previous trading day is
asked for, latest first, and the first roster found governs T. A 404 for a
closed day means "no session" only once that day is over (the rosters read
were written 18:50 to 20:10 IST); it is recorded in the archive, so an
offline re-run knows a day it never asked about from a day with no roster.

**The alternatives rejected.**

* *NSE's ``api/reportASM`` and ``api/reportGSM``.* They carry no date to
  assert: on 24 Nov 2026 - a holiday - every ASM row said ``asmTime
  24-Nov-2026``, the date of the request, and GSM's ``gsmTime`` is a morning
  batch time. Their ``gsmStage`` is not the stage but the roman numeral of the
  surveillance code (``LXII`` is code 62, "IBC and GSM stage 0"). The ASM
  report leaves out a security under two measures (AQYLON and KEEPLEARN appear
  only as GSM composites) and listed LGHL, which neither the roster nor the
  consolidated list in NSE/SURV/77569 carries.
* *The margin date.* The circulars apply the margin from T+2 (ST-ASM) or T+3
  (LT-ASM); the roster flags a stock from the next session. The earlier date is
  the one a cautious system uses, and the one the exchange itself shows.
* *Lenient parsing.* A roster that loses a column or a stage value still
  "parses" and lists fewer flagged stocks - and a missing flag reads as clean.
  One bad row refuses the file.
* *``False`` for an instrument the roster does not name.* The roster lists
  the ~3,600 securities the capital-market segment trades; the master holds
  ~10,400 instruments (bonds, indices, the suspended). An instrument the roster
  does not name gets no write, so its flags stay NULL - unknown, refused - and
  only a named, clean row is ever written ``False``.
* *A separate T2T file* - ``sec_list_<DDMMYYYY>.csv``, the price-band list,
  also carries a series. A second source would need its own dates verified;
  the roster's series are verified against a year of trading and arrive in a
  file already fetched, checked and archived. (The band itself is E04-S04's.)
* *The calendar's special sessions.* The holiday file lists Muhurat and not
  the Budget session; a list someone must remember to extend is how 2 Feb 2026
  read five new ST-ASM names as clean. The exchange's own files say which
  days it held a session.

**The failure it prevents.** Trading a stock NSE moved into surveillance or
into a trade-for-trade series overnight; proceeding on an older file (the
file's own date is the request, and its ``Last-Modified`` must fall after that
session closed and before the session it governs opens) - including the file
before a session the system stands down on; a truncated or reshaped roster read
as a shorter list of flagged names; a fetch failure read as "nothing flagged" -
a failed day writes nothing, so every symbol stays unknown and is refused (AC2).

**What would make this wrong.** NSE moving the roster's publication to the
morning of the session it governs, or changing what it describes - the
``Last-Modified`` window refuses the first; nothing here can detect the
second, so re-verify it against a day's circulars if NSE announces a change to
its daily reports. NSE writing a closed day's roster after that day ends (its
404 would already have been taken as "no session"). A new series code (read as
unknown, so refused - but say what it is before trading it). NSE renaming a
column read here (refused: they are asserted by name). NSE repurposes its
``Filler`` columns (column 23 was ``Filler12`` on 2 Jan 2026 and an ETF alert
by 20 Nov), so the others are deliberately not asserted.

## What is deliberately not here

* **The roster's other measures** - ESM (198 stage I and 21 stage II on
  23 Nov 2026), insolvency (IRP), inter-creditor agreements, unsolicited SMS
  and the rest. The schema has no column for them. ESM's trade-for-trade
  stocks are T2T here already - on 23 Nov 2026 all 21 at ESM II and 195 of
  the 198 at ESM I were in BE, BZ or ST - and ESM I's 5% band is E04-S04's
  filter. Raised as E04-S10.
* **The universe filter.** E07-S02 applies the hard filters; this writes the
  day's flags, which execution already refuses on.
* **Scheduling** - the data-sync stage's (E11-S02).
* **History keyed by its own names.** A stock whose series has changed since
  (21STCENMGM was EQ in January and is BE now) resolves against today's master
  under its new name only: 2,413 of 2,770 rows of the 2 Jan roster resolve
  today, 3,575 of 3,590 of the 23 Nov one - the bhavcopy has the same limit.
* **A holding whose series moved.** The held-position alert matches Kite
  names, so a position in ``XYZ`` is not matched when ``XYZ`` moves to BE
  (``XYZ-BE``); an intraday book holds nothing at the pre-market.
* **Sessions the calendar does not know.** The bhavcopy backfill still asks
  the calendar which days were sessions, so 1 Feb 2026 has no bars (E04-S11).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import email.utils
import http.client
import logging
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Protocol

from algotrader.common.calendar import IST, MARKET_CLOSE, MARKET_OPEN, MarketCalendar
from algotrader.common.enums import SurveillanceCategory
from algotrader.common.models.trading import _validate_symbol
from algotrader.common.text import MalformedCsvError, csv_rows
from algotrader.premarket.bhavcopy import BhavcopyArchive
from algotrader.premarket.universe import kite_tradingsymbol

log = logging.getLogger(__name__)

__all__ = [
    "SOURCE",
    "Fetched",
    "RepositoryStatusStore",
    "Resolved",
    "RosterArchive",
    "RosterError",
    "RosterMissingError",
    "RosterRow",
    "StatusStore",
    "SurveillanceReport",
    "SurveillanceResult",
    "SurveillanceSync",
    "SyncOutcome",
    "check_publication",
    "fetch_roster",
    "parse_roster",
    "resolve_roster",
]

#: One roster per session, named for the session it was written after.
SOURCE: Final = "https://nsearchives.nseindia.com/content/cm/REG1_IND{date}.csv"
_ORIGIN: Final = "https://nsearchives.nseindia.com/"

#: The columns read, by their exact published names (read 24 Nov 2026).
SYMBOL: Final = "Symbol"
SERIES: Final = "Series"
STATUS: Final = "Status"
GSM: Final = "GSM"
LT_ASM: Final = "Long_Term_Additional_Surveillance_Measure (Long Term ASM)"
ST_ASM: Final = "Short_Term_Additional_Surveillance_Measure (Short Term ASM)"
REQUIRED: Final = (SYMBOL, SERIES, STATUS, GSM, LT_ASM, ST_ASM)

#: The roster's "this measure does not apply".
NOT_APPLICABLE: Final = "100"
#: Stage values per measure. GSM 0 is "shortlisted"; NSE's own code table
#: (GSM FAQ v1.1, 12 Jun 2026) defines stages up to VI, the rosters read carry
#: 0 to 4. LT-ASM has four stages, ST-ASM two. Anything else is a roster this
#: code does not understand, so it is refused rather than guessed at.
GSM_STAGES: Final = frozenset(range(7))
LT_ASM_STAGES: Final = frozenset({1, 2, 3, 4})
ST_ASM_STAGES: Final = frozenset({1, 2})
#: Active, Suspended and Inactive - NSE's file structure for the indicator file
#: (``REG_INDDDMMYY File Structure``) lists these three; the 2026 rosters carry
#: A and S. A fourth value is a file this code does not understand.
STATUSES: Final = frozenset({"A", "S", "I"})

#: NSE's legend of series (nseindia.com, "Legend of series", updated
#: 19 Sep 2024): fully paid equity and ETFs settle rolling in EQ and trade for
#: trade in BE and BZ; SME equity rolling in SM and trade for trade in ST and
#: SZ. These six are every series the 219 rosters of 2026 carry; any other is
#: unknown - neither written clean nor written T2T.
ROLLING_SERIES: Final = frozenset({"EQ", "SM"})
TRADE_FOR_TRADE_SERIES: Final = frozenset({"BE", "BZ", "ST", "SZ"})
#: The trade-for-trade lists the deny-list floor holds: at their smallest in
#: 2026, BE named 93 (13 Apr), BZ 28 (20 Apr) and ST 65 (2 Apr). SZ named one
#: or two all year - too few for a floor to mean anything.
FLOORED_SERIES: Final = ("BE", "BZ", "ST")

#: 2,770 rows on 2 Jan 2026, 3,590 on 23 Nov. Fewer is a truncated file.
MIN_ROWS: Final = 2000
#: A deny list that names (almost) nobody is a failed fetch, not a clean
#: market (SPRINT02_TECHNICAL_SPEC 5.1): a roster whose ASM columns all read
#: "not applicable" would otherwise write every stock clean - the most
#: permissive answer there is. Each list named dozens on the days read:
#: long-term ASM, short-term ASM and GSM were 124, 44 and 73 on 23 Nov 2026
#: and 57, 35 and 64 on 2 Jan 2026.
MIN_FLAGGED: Final = 5
#: ~700 KB today; the bound is for a response that is not a roster.
MAX_FILE_BYTES: Final = 4 * 1024 * 1024
#: The share of the roster's EQ rows the master must know - the bhavcopy's
#: floor. 2,781 of 2,782 resolved for 23 Nov 2026 and 1,963 of 2,086 for 2 Jan.
#: An unresolved row is written nowhere (unknown, refused), so this does not
#: protect a flag; it makes a stale master or a reshaped file loud.
MIN_EQ_RESOLVED_SHARE: Final = 0.8
#: NSE's archive is a public courtesy: at most one request a second.
FETCH_INTERVAL_SECONDS: Final = 1.0

_SERIES_CODE: Final = re.compile(r"[A-Z0-9]{2}")


class RosterError(ValueError):
    """A roster this system will not use; the message says why."""


class RosterMissingError(RosterError):
    """NSE has no roster at that name (HTTP 404) - yet, or for that day."""


def _refuse(why: str) -> RosterError:
    return RosterError(f"surveillance roster refused: {why}")


@dataclass(frozen=True)
class RosterRow:
    """One security as the roster lists it. A stage is ``None`` where the
    measure does not apply."""

    symbol: str
    series: str
    status: str
    gsm_stage: int | None
    lt_asm_stage: int | None
    st_asm_stage: int | None

    @property
    def tradingsymbol(self) -> str:
        return kite_tradingsymbol(self.symbol, self.series)

    @property
    def is_gsm(self) -> bool:
        return self.gsm_stage is not None

    @property
    def is_asm(self) -> bool:
        return self.lt_asm_stage is not None or self.st_asm_stage is not None

    @property
    def asm_category(self) -> SurveillanceCategory | None:
        """The schema keeps one category. A stock in both lists (none on the
        days read) is recorded LONG_TERM; both block, so this only changes
        the answer to "which list"."""
        if self.lt_asm_stage is not None:
            return SurveillanceCategory.LONG_TERM
        if self.st_asm_stage is not None:
            return SurveillanceCategory.SHORT_TERM
        return None

    @property
    def is_t2t(self) -> bool | None:
        """Trade for trade on the session the roster governs; ``None`` for a
        series this code does not know - unknown, which every reader refuses."""
        if self.series in TRADE_FOR_TRADE_SERIES:
            return True
        if self.series in ROLLING_SERIES:
            return False
        return None

    @property
    def flagged(self) -> bool:
        """Under ASM or GSM - what the held-position alert names."""
        return self.is_asm or self.is_gsm


def _stage(value: str, allowed: frozenset[int], line: int, column: str) -> int | None:
    if value == NOT_APPLICABLE:
        return None
    if value.isdigit() and len(value) <= 2 and int(value) in allowed:
        return int(value)
    raise _refuse(f"line {line}: {column[:24]!r} is {value[:16]!r}, not a stage this code knows")


def parse_roster(raw: bytes, *, session: dt.date) -> tuple[RosterRow, ...]:
    """Every row of one roster, or :class:`RosterError` for the whole file."""
    if len(raw) > MAX_FILE_BYTES:
        raise _refuse(f"the roster for {session} exceeds {MAX_FILE_BYTES} bytes")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _refuse(f"the roster for {session} is not UTF-8 text") from None
    try:
        table = csv_rows(text)
    except MalformedCsvError as exc:
        raise _refuse(f"the roster for {session} is {exc}") from None
    if not table:
        raise _refuse(f"the roster for {session} is empty")
    header = table[0]
    index: dict[str, int] = {}
    for name in REQUIRED:
        if header.count(name) != 1:
            raise _refuse(f"the header names {name[:40]!r} {header.count(name)} times, not once")
        index[name] = header.index(name)
    width = len(header)
    rows: list[RosterRow] = []
    seen: set[str] = set()
    for line, row in enumerate(table[1:], start=2):
        if len(row) != width:
            raise _refuse(f"line {line} has {len(row)} fields, the header {width}")
        symbol = row[index[SYMBOL]]
        try:
            _validate_symbol(symbol)
        except ValueError:
            raise _refuse(f"line {line}: {symbol[:24]!r} is not a symbol") from None
        if symbol in seen:
            raise _refuse(f"line {line}: {symbol!r} is listed twice")
        seen.add(symbol)
        series = row[index[SERIES]]
        if _SERIES_CODE.fullmatch(series) is None:
            raise _refuse(f"line {line}: series {series[:8]!r} is not a series code")
        status = row[index[STATUS]]
        if status not in STATUSES:
            raise _refuse(f"line {line}: status {status[:8]!r} is not one of {sorted(STATUSES)}")
        rows.append(
            RosterRow(
                symbol=symbol,
                series=series,
                status=status,
                gsm_stage=_stage(row[index[GSM]], GSM_STAGES, line, GSM),
                lt_asm_stage=_stage(row[index[LT_ASM]], LT_ASM_STAGES, line, LT_ASM),
                st_asm_stage=_stage(row[index[ST_ASM]], ST_ASM_STAGES, line, ST_ASM),
            )
        )
    if len(rows) < MIN_ROWS:
        raise _refuse(f"the roster for {session} lists {len(rows)} securities, under {MIN_ROWS}")
    for label, named in (
        ("long-term ASM", sum(r.lt_asm_stage is not None for r in rows)),
        ("short-term ASM", sum(r.st_asm_stage is not None for r in rows)),
        ("GSM", sum(r.gsm_stage is not None for r in rows)),
        *(
            (f"the trade-for-trade series {code}", sum(r.series == code for r in rows))
            for code in FLOORED_SERIES
        ),
    ):
        if named < MIN_FLAGGED:
            raise _refuse(
                f"the roster for {session} puts {named} securities under {label}, under "
                f"{MIN_FLAGGED}: a deny list that names nobody is a failed fetch"
            )
    return tuple(rows)


@dataclass(frozen=True)
class Resolved:
    """The roster rows the master knows, and the names it does not."""

    rows: tuple[RosterRow, ...]
    unresolved: tuple[str, ...]


def resolve_roster(
    rows: Sequence[RosterRow],
    *,
    master: frozenset[str],
    min_eq_resolved_share: float = MIN_EQ_RESOLVED_SHARE,
) -> Resolved:
    """Keep the rows whose Kite name is in the master; refuse a roster most of
    whose EQ rows the master does not know."""
    known = [r for r in rows if r.tradingsymbol in master]
    eq = [r for r in rows if r.series == "EQ"]
    eq_known = sum(1 for r in eq if r.tradingsymbol in master)
    # No EQ rows at all is a reshaped file, not a quiet day: refusing on the
    # share alone would divide by zero, and skipping the check would let it in.
    if not eq or eq_known / len(eq) < min_eq_resolved_share:
        raise _refuse(
            f"only {eq_known} of {len(eq)} EQ rows are in the instrument master; "
            f"the master is stale or the file is wrong"
        )
    return Resolved(
        rows=tuple(known),
        unresolved=tuple(r.tradingsymbol for r in rows if r.tradingsymbol not in master),
    )


# ---------------------------------------------------------------------------
# Fetching and archiving
# ---------------------------------------------------------------------------

Opener = Callable[[urllib.request.Request, float], Any]


def _open(request: urllib.request.Request, timeout: float) -> Any:
    """``urlopen`` for NSE's archive origin and nothing else (bandit B310)."""
    if not request.full_url.startswith(_ORIGIN):
        raise _refuse(f"refusing to open {request.full_url[:48]!r}: not {_ORIGIN}")
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310  # nosec B310 - origin checked above


@dataclass(frozen=True)
class Fetched:
    body: bytes
    #: When NSE wrote the file, from ``Last-Modified``; ``None`` if absent.
    last_modified: dt.datetime | None


def _http_date(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def fetch_roster(session: dt.date, *, opener: Opener = _open, timeout: float = 30.0) -> Fetched:
    """The roster written after ``session``. A 404 is :class:`RosterMissingError`."""
    url = SOURCE.format(date=session.strftime("%d%m%y"))
    request = urllib.request.Request(  # noqa: S310 - fixed https URL
        url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept": "*/*"}
    )
    try:
        with opener(request, timeout) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise _refuse(f"NSE answered HTTP {status} for {session}")
            body: bytes = response.read(MAX_FILE_BYTES + 1)
            modified = _http_date(response.headers.get("Last-Modified"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RosterMissingError(
                f"NSE has no surveillance roster for {session} (HTTP 404)"
            ) from None
        raise _refuse(f"NSE answered HTTP {exc.code} for {session}") from None
    except (OSError, http.client.HTTPException) as exc:
        # URLError is an OSError, raised by the open. A failure while READING
        # the body - a timeout, a reset, a short read - is a bare OSError or
        # HTTPException that urllib does not wrap, and escaped (QA-E04-12).
        raise _refuse(
            f"the roster for {session} could not be fetched ({type(exc).__name__})"
        ) from exc
    if len(body) > MAX_FILE_BYTES:
        raise _refuse(f"the response for {session} exceeds {MAX_FILE_BYTES} bytes")
    return Fetched(body=body, last_modified=modified)


def check_publication(
    last_modified: dt.datetime | None, *, session: dt.date, governs: dt.date
) -> None:
    """The roster must have been written after ``session`` closed and before
    the session it governs opened - the evening file, not a morning one."""
    if last_modified is None:
        raise _refuse(f"NSE gave no Last-Modified for the {session} roster, so its age is unknown")
    closed = dt.datetime.combine(session, MARKET_CLOSE, tzinfo=IST)
    opens = dt.datetime.combine(governs, MARKET_OPEN, tzinfo=IST)
    if not closed < last_modified < opens:
        written = last_modified.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")
        raise _refuse(
            f"the {session} roster was written {written}, outside {closed:%Y-%m-%d %H:%M} - "
            f"{opens:%Y-%m-%d %H:%M} IST: it is not the evening file for {governs}"
        )


class RosterArchive(BhavcopyArchive):
    """The raw rosters under ``root/YYYY/``, written - atomically, as the
    bhavcopy's are - only once a roster has been accepted."""

    def path(self, day: dt.date) -> Path:
        return self._root / f"{day.year:04d}" / f"REG1_IND{day:%d%m%y}.csv"

    def absent_path(self, day: dt.date) -> Path:
        return self.path(day).with_suffix(".absent")

    def mark_absent(self, day: dt.date, *, at: dt.datetime) -> Path:
        """Record that NSE had no roster for the closed day ``day`` once it was
        over - so an offline re-run can tell that from a day nobody asked about."""
        path = self.absent_path(day)
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + ".partial")
        partial.write_text(f"HTTP 404 at {at.isoformat()}\n", encoding="utf-8")
        os.replace(partial, path)
        return path

    def is_absent(self, day: dt.date) -> bool:
        return self.absent_path(day).is_file()


# ---------------------------------------------------------------------------
# One trade date, or a range of them
# ---------------------------------------------------------------------------


class StatusStore(Protocol):
    """Where the day's flags go (``RepositoryStatusStore`` over the real tables)."""

    async def active_symbols(self) -> frozenset[str]: ...

    async def write(self, trade_date: dt.date, rows: Sequence[RosterRow]) -> int: ...


class SyncOutcome(StrEnum):
    STORED = "STORED"
    #: The calendar says T has no session; nothing was requested.
    NOT_A_TRADING_DAY = "NOT_A_TRADING_DAY"
    #: A session, and no file for it (a roster, or the F&O ban list of
    #: ``fno_ban.py``, which reports in these terms too): T's flags stay unknown.
    MISSING = "MISSING"
    #: A file this system will not use: T's flags stay unknown.
    REFUSED = "REFUSED"


@dataclass(frozen=True)
class SurveillanceResult:
    trade_date: dt.date
    outcome: SyncOutcome
    detail: str = ""
    #: The session whose evening roster governs ``trade_date`` - the last one
    #: NSE held before it, a session the calendar does not trade included.
    roster_session: dt.date | None = None
    #: ``archive`` or ``network``.
    source: str = ""
    written: int = 0
    asm: int = 0
    gsm: int = 0
    t2t: int = 0
    unresolved: int = 0
    #: Held positions the roster puts under surveillance for ``trade_date``.
    held_flagged: tuple[str, ...] = ()


@dataclass
class SurveillanceReport:
    results: list[SurveillanceResult] = field(default_factory=list)

    def of(self, outcome: SyncOutcome) -> list[SurveillanceResult]:
        return [r for r in self.results if r.outcome is outcome]

    @property
    def ok(self) -> bool:
        """Every session's flags written: nothing missing, nothing refused."""
        return not self.of(SyncOutcome.MISSING) and not self.of(SyncOutcome.REFUSED)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass(frozen=True)
class _Found:
    """The roster that governs a trade date, and where it came from."""

    session: dt.date
    raw: bytes
    source: str


class SurveillanceSync:
    """Fetches (politely), checks, parses, archives and writes the rosters."""

    def __init__(
        self,
        *,
        calendar: MarketCalendar,
        archive: RosterArchive,
        store: StatusStore,
        fetch: Callable[[dt.date], Fetched] = fetch_roster,
        offline: bool = False,
        min_interval: float = FETCH_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
        min_eq_resolved_share: float = MIN_EQ_RESOLVED_SHARE,
        now: Callable[[], dt.datetime] = _utc_now,
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
        #: The wall clock: whether a closed day is over yet.
        self._now = now
        self._last_fetch: float | None = None

    async def _polite_fetch(self, session: dt.date) -> Fetched:
        if self._last_fetch is not None:
            wait = self._interval - (self._clock() - self._last_fetch)
            if wait > 0:
                await self._sleep(wait)
        try:
            return await asyncio.to_thread(self._fetch, session)
        finally:
            self._last_fetch = self._clock()

    async def sync_day(
        self,
        trade_date: dt.date,
        *,
        master: frozenset[str] | None = None,
        held: Iterable[str] = (),
    ) -> SurveillanceResult:
        """Trade date T's flags, from the last roster NSE wrote before T opened.
        Never raises for a bad roster - the result says what happened, and a
        result that is not STORED wrote nothing. Raises ``HolidayDataError``
        when the session before T is in a year the calendar does not cover."""
        if not self._calendar.is_trading_day(trade_date):
            return SurveillanceResult(trade_date, SyncOutcome.NOT_A_TRADING_DAY)
        found = await self._find(trade_date)
        if isinstance(found, SurveillanceResult):
            return found
        session, raw, source = found.session, found.raw, found.source
        if not self._calendar.is_trading_day(session):
            log.warning(
                "NSE wrote a roster for %s, a day the calendar has no session on; it governs %s",
                session,
                trade_date,
            )
        try:
            rows = parse_roster(raw, session=session)
            resolved = resolve_roster(
                rows,
                master=master if master is not None else await self._store.active_symbols(),
                min_eq_resolved_share=self._share,
            )
        except RosterError as exc:
            return SurveillanceResult(trade_date, SyncOutcome.REFUSED, str(exc), session, source)
        if source == "network":
            self._archive.write(session, raw)
        written = await self._store.write(trade_date, resolved.rows)
        flagged = {r.tradingsymbol for r in resolved.rows if r.flagged}
        held_flagged = tuple(sorted(set(held) & flagged))
        for symbol in held_flagged:
            # An intraday book holds nothing at the pre-market; a holding here
            # is a leftover, and from today it carries the measure's margin.
            log.error(
                "held position %s is under ASM/GSM surveillance from %s (roster of %s)",
                symbol,
                trade_date,
                session,
            )
        return SurveillanceResult(
            trade_date,
            SyncOutcome.STORED,
            roster_session=session,
            source=source,
            written=written,
            asm=sum(1 for r in resolved.rows if r.is_asm),
            gsm=sum(1 for r in resolved.rows if r.is_gsm),
            t2t=sum(1 for r in resolved.rows if r.is_t2t),
            unresolved=len(resolved.unresolved),
            held_flagged=held_flagged,
        )

    async def _find(self, trade_date: dt.date) -> _Found | SurveillanceResult:
        """The roster that governs ``trade_date``: the latest NSE wrote before it
        opened. NSE holds sessions the calendar does not trade (1 Feb 2026, the
        Budget; 8 Nov 2026, Muhurat) and writes a roster after each, so every
        closed day since the previous trading day is asked for, latest first,
        before that day's own. A result instead of a roster says why there is
        none to use - and nothing is written for the day."""
        regular = self._calendar.previous_trading_day(trade_date)
        between = (trade_date - regular).days - 1
        closed = [regular + dt.timedelta(days=n) for n in range(between, 0, -1)]
        for day in (*closed, regular):
            is_closed = day != regular
            raw = self._archive.read(day)
            if raw is not None:
                return _Found(day, raw, "archive")
            if is_closed and self._archive.is_absent(day):
                continue
            if self._offline:
                return SurveillanceResult(
                    trade_date,
                    SyncOutcome.MISSING,
                    (
                        f"the archive does not record whether NSE wrote a roster for {day} "
                        f"(offline run)"
                        if is_closed
                        else f"the {day} roster is not in the archive (offline run)"
                    ),
                    roster_session=day,
                )
            try:
                fetched = await self._polite_fetch(day)
                check_publication(fetched.last_modified, session=day, governs=trade_date)
            except RosterMissingError as exc:
                if not is_closed:
                    return SurveillanceResult(
                        trade_date, SyncOutcome.MISSING, str(exc), day, "network"
                    )
                ended = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0, 0), tzinfo=IST)
                asked = self._now()
                if asked < ended:
                    # NSE writes a session's roster that evening: until the day
                    # is over, a 404 cannot say there was no session.
                    return SurveillanceResult(
                        trade_date,
                        SyncOutcome.MISSING,
                        f"NSE has no roster for {day} yet and that day is not over, so whether "
                        f"it held a session - whose roster would govern {trade_date} - is unknown",
                        day,
                        "network",
                    )
                self._archive.mark_absent(day, at=asked)
                continue
            except RosterError as exc:
                return SurveillanceResult(trade_date, SyncOutcome.REFUSED, str(exc), day, "network")
            return _Found(day, fetched.body, "network")
        # The loop's last day is the previous trading day, and it always returns.
        raise AssertionError("unreachable")  # pragma: no cover

    async def backfill(
        self,
        start: dt.date,
        end: dt.date,
        *,
        held: Iterable[str] = (),
        progress: Callable[[SurveillanceResult], object] | None = None,
    ) -> SurveillanceReport:
        """Every trading day in ``[start, end]``. Resumable: an archived roster
        is read from disk, and a written day is rewritten to the same values.

        Raises before any request if the range is inverted or reaches a year
        the calendar does not cover - including the year before ``start`` when
        ``start`` is a year's first session, whose roster is the previous
        year's last.
        """
        days = self._calendar.trading_days_between(start, end)
        master = await self._store.active_symbols()
        holding = tuple(held)
        report = SurveillanceReport()
        for day in days:
            result = await self.sync_day(day, master=master, held=holding)
            report.results.append(result)
            if result.outcome not in (SyncOutcome.STORED, SyncOutcome.NOT_A_TRADING_DAY):
                log.error(
                    "surveillance %s: %s - %s; its flags stay unknown and nothing trades on it",
                    day,
                    result.outcome.value,
                    result.detail,
                )
            if progress is not None:
                progress(result)
        return report


class RepositoryStatusStore:
    """:class:`StatusStore` over the real tables: one transaction per day."""

    def __init__(self, factory: Any) -> None:
        self._factory = factory

    async def active_symbols(self) -> frozenset[str]:
        from algotrader.common.db.engine import read_only
        from algotrader.common.db.repositories import InstrumentRepository

        async with read_only(self._factory) as session:
            return await InstrumentRepository(session).active_symbols()

    async def write(self, trade_date: dt.date, rows: Sequence[RosterRow]) -> int:
        from algotrader.common.db.engine import unit_of_work
        from algotrader.common.db.repositories import DailyStatusRepository, InstrumentRepository

        if not rows:
            return 0
        async with unit_of_work(self._factory) as session:
            instruments = InstrumentRepository(session)
            await instruments.refresh_cache()
            records = [
                {
                    "symbol_id": await instruments.symbol_id(row.tradingsymbol),
                    "trade_date": trade_date,
                    "is_asm": row.is_asm,
                    "asm_category": row.asm_category,
                    "is_gsm": row.is_gsm,
                    "gsm_stage": row.gsm_stage,
                    "is_t2t": row.is_t2t,
                }
                for row in rows
            ]
            return await DailyStatusRepository(session, instruments).upsert(records)
