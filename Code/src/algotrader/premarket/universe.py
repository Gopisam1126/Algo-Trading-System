"""The base universe: the symbols the day starts from (E07-S01).

``INDIA_FEATURES_AND_CONFIG.md §5.1``: a Nifty index (50, 100, 200 - the
recommended default - or 500), the F&O universe, or a custom list. This module
turns ``universe.base`` into a list of instrument-master symbols, before any
hard filter (E07-S02) or score (E07-S03) is applied. It runs in the pre-market
pipeline's 06:30 stage (``LOW_LEVEL_ARCHITECTURE.md §5.5``), after the 05:30
instrument sync.

## The design record

**The decision.** The base universe is RESOLVED against the instrument master
and every symbol that does not resolve is named; an implausible answer - a
file of the wrong shape, a count far from the index's size, an F&O list that
lost its stocks, a churn no reconstitution produces - is refused, never loaded.

**The alternatives rejected.**

* *Take the index file's symbols as they are.* A constituent in the BE
  (trade-to-trade) series is ``HFCL`` in the file and ``HFCL-BE`` at Kite, and
  a REIT is ``EMBASSY`` in the file and ``EMBASSY-RR`` at Kite (both read on
  22 Nov 2026). Unresolved, the first would be a symbol no order can name;
  "resolved" by the bare symbol, it would silently match nothing, or the wrong
  line. The series is carried and the Kite symbol derived from it.
* *A hand-kept list of index underlyings to drop from the F&O universe.*
  NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50 and NIFTYFPI on 22 Nov
  2026, and the next index NSE adds would enter the universe as a "stock". The
  rule instead is the master: an underlying that is not an NSE equity there is
  not a stock.
* *Treat the dummy constituent as unresolved.* On a demerger NSE Indices adds
  a placeholder for the demerged company until it lists - on 22 Nov 2026 the
  Nifty 500 file carried "Dummy HEG Ltd.", ``DUMMYHEG``, ISIN ``DUM545A01024``,
  which is why it listed 501 stocks. It is not a stale master and not a
  tradable name; it is reported as a placeholder, apart from both.

**The failure it prevents.** The 401-symbol HTML error page, the truncated
download and the stale master all parse to a *smaller* universe, and a
smaller universe trades - just fewer names, with nothing to say why. Same
class as E02-S06's truncated instrument dump and E04's empty hazard lists
(BR-17): the danger is a successful-looking short answer.

**What would make this wrong.** NSE Indices changing the file's columns (the
header is asserted exactly, so that refuses loudly), renaming the dummy ISIN
convention, or Kite changing its series suffix. And a base drawn from the F&O
list counts on the instrument master holding only NSE equities, which E02-S06
guarantees today (``TRADED_EXCHANGE``).

## What is not here

Hard filters (T2T, surveillance, bans, circuit bands) are E07-S02 - a BE-series
constituent is resolved here and excluded there. Persisting the day's list for
tomorrow's reconstitution check is the pipeline checkpoint (E11-S01): this
module compares two lists it is given. The index file's ``Industry`` column is
returned per symbol (``industries``) because nothing yet fills
``instruments.sector`` (E07-S07).
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import io
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from algotrader.common.config import UniverseConfig
from algotrader.common.models.trading import _validate_symbol
from algotrader.common.text import one_safe_line

__all__ = [
    "INDEX_BASES",
    "BaseUniverse",
    "Constituent",
    "Reconstitution",
    "UniverseSourceError",
    "build_base_universe",
    "fetch_index_file",
    "fno_underlyings",
    "kite_tradingsymbol",
    "load_base_universe",
    "parse_constituents",
    "reconstitution",
]

IndexBase = Literal["nifty50", "nifty100", "nifty200", "nifty500"]
Base = Literal["nifty50", "nifty100", "nifty200", "nifty500", "fno", "custom"]

#: How many stocks each index holds by its methodology.
INDEX_BASES: Final[dict[str, int]] = {
    "nifty50": 50,
    "nifty100": 100,
    "nifty200": 200,
    "nifty500": 500,
}

#: Where a constituent file is fetched, in order: NSE Indices (the publisher),
#: then NSE's archive, which serves the same bytes. TWO, because on 22 Nov 2026
#: niftyindices.com answered HTTP 200 with an HTML page for every file after
#: three requests in a row, while the archive kept serving the CSV (found in
#: SIT). Each answer goes through the same strict parse; the universe records
#: which one served it. Fixed, https only: no URL is ever built from input.
SOURCES: Final = (
    "https://www.niftyindices.com/IndexConstituent/ind_{base}list.csv",
    "https://nsearchives.nseindia.com/content/indices/ind_{base}list.csv",
)

#: The file's header, exactly as published on 22 Nov 2026.
HEADER: Final = ("Company Name", "Industry", "Symbol", "Series", "ISIN Code")

#: A constituent file is a few tens of KB (Nifty 500: 32,765 bytes). The bound
#: is for the response that is not one.
MAX_FILE_BYTES: Final = 512 * 1024

#: The count a file may hold, relative to the index's size. Fewer than 95% is a
#: truncated download; a placeholder or two above it is a demerger (Nifty 500:
#: 501 on 22 Nov 2026), and ten above it is not an index file.
MIN_SHARE: Final = 0.95
MAX_EXTRA: Final = 10

#: Constituents that may fail to resolve before the master is judged stale: a
#: day-old listing change is normal, a tenth of the index is not.
MAX_UNRESOLVED_SHARE: Final = 0.05

#: The F&O stock universe was 218 names on 22 Nov 2026. Below this the list
#: lost its stocks, or the master did.
MIN_FNO_STOCKS: Final = 100

#: A reconstitution changes a handful of names (NSE's semi-annual reviews, the
#: odd mid-period replacement). Above this share of the list, in a day, the
#: input is wrong, not the index.
MAX_CHURN_SHARE: Final = 0.20

_SERIES = re.compile(r"[A-Z0-9]{2}")
#: An Indian ISIN: "IN", nine alphanumerics, a check digit.
_ISIN = re.compile(r"IN[A-Z0-9]{9}[0-9]")
#: NSE Indices' placeholder for a demerged company that has not yet listed.
_PLACEHOLDER_ISIN = re.compile(r"DUM[A-Z0-9]{9}")


def kite_tradingsymbol(symbol: str, series: str) -> str:
    """NSE's (symbol, series) as Kite names it: ``INFY`` for EQ, ``HFCL-BE`` otherwise.

    One definition for every NSE file this system reads - the index lists here,
    the bhavcopy (``premarket/bhavcopy.py``) - so two readers cannot disagree on
    which instrument a row is (verified against Kite's NSE list, 22 Nov 2026).
    """
    return symbol if series == "EQ" else f"{symbol}-{series}"


class UniverseSourceError(ValueError):
    """The input to the universe is not one that can be trusted. Never partial."""


@dataclass(frozen=True)
class Constituent:
    symbol: str
    series: str
    industry: str
    isin: str
    company: str

    @property
    def tradingsymbol(self) -> str:
        """Kite's symbol: the bare symbol for EQ, ``SYMBOL-SERIES`` otherwise."""
        return kite_tradingsymbol(self.symbol, self.series)

    @property
    def is_placeholder(self) -> bool:
        return bool(_PLACEHOLDER_ISIN.fullmatch(self.isin))


def _refuse(why: str) -> UniverseSourceError:
    return UniverseSourceError(one_safe_line(why))


def parse_constituents(raw: bytes, *, base: str) -> tuple[Constituent, ...]:
    """One index's published constituents, or a refusal saying why not."""
    if base not in INDEX_BASES:
        raise _refuse(f"{base!r} is not an index base")
    if len(raw) > MAX_FILE_BYTES:
        raise _refuse(f"the {base} file is {len(raw)} bytes; a constituent file is not")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _refuse(f"the {base} file is not UTF-8 text") from None
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or tuple(cell.strip() for cell in rows[0]) != HEADER:
        # The first few cells, each cut short: an HTML page's first "row" is
        # its whole head, and the reason only needs to show it is not a CSV.
        shown = [cell.strip()[:24] for cell in rows[0][:5]] if rows else []
        raise _refuse(f"the {base} file's header is {shown!r}, not {list(HEADER)!r}")

    found: list[Constituent] = []
    seen: set[str] = set()
    for line, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue
        if len(row) != len(HEADER):
            raise _refuse(f"the {base} file's line {line} has {len(row)} fields")
        company, industry, symbol, series, isin = (cell.strip() for cell in row)
        try:
            symbol = _validate_symbol(symbol)
        except ValueError:
            raise _refuse(f"the {base} file's line {line} has an unusable symbol") from None
        if not _SERIES.fullmatch(series):
            raise _refuse(f"the {base} file's line {line} has series {series[:8]!r}")
        if not (_ISIN.fullmatch(isin) or _PLACEHOLDER_ISIN.fullmatch(isin)):
            raise _refuse(f"the {base} file's line {line} has ISIN {isin[:16]!r}")
        if not industry:
            raise _refuse(f"the {base} file's line {line} names no industry")
        constituent = Constituent(
            symbol=symbol,
            series=series,
            industry=one_safe_line(industry)[:64],
            isin=isin,
            company=one_safe_line(company)[:128],
        )
        if constituent.tradingsymbol in seen:
            raise _refuse(f"the {base} file lists {constituent.tradingsymbol} twice")
        seen.add(constituent.tradingsymbol)
        found.append(constituent)

    size = INDEX_BASES[base]
    if not size * MIN_SHARE <= len(found) <= size + MAX_EXTRA:
        raise _refuse(
            f"the {base} file lists {len(found)} constituents; the index holds {size}, "
            f"so this is a truncated or wrong file"
        )
    return tuple(found)


def _as_date(value: object) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str) and value:
        try:
            return dt.date.fromisoformat(value)
        except ValueError:
            return None
    return None


def fno_underlyings(rows: Iterable[Mapping[str, Any]], *, as_of: dt.date) -> frozenset[str]:
    """Underlyings with a futures contract still trading on ``as_of``.

    ``rows`` are the broker's NFO instruments (``KiteConnect.instruments("NFO")``
    or its CSV). Indices are among the names; :func:`build_base_universe`
    removes them by resolving against the master. A contract with no readable
    expiry is not counted: "still trading" is the point of the filter.
    """
    names: set[str] = set()
    for row in rows:
        if row.get("segment") != "NFO-FUT" or row.get("instrument_type") != "FUT":
            continue
        expiry = _as_date(row.get("expiry"))
        name = str(row.get("name") or "").strip()
        if expiry is None or expiry < as_of or not name:
            continue
        names.add(name)
    return frozenset(names)


@dataclass(frozen=True)
class BaseUniverse:
    base: str
    as_of: dt.date
    #: Instrument-master symbols, sorted. What every later stage works on.
    symbols: tuple[str, ...]
    #: In the source and not in the master - named, never dropped silently.
    unresolved: tuple[str, ...]
    #: NSE Indices' placeholders for a demerged company not yet listed.
    placeholders: tuple[str, ...]
    #: The index file's industry per resolved symbol; empty for F&O and custom.
    industries: Mapping[str, str]
    #: Where the list came from: the file's URL (which of ``SOURCES`` served
    #: it), or empty when built directly from rows already in hand.
    source: str = ""

    def __len__(self) -> int:
        return len(self.symbols)


def _master(master: Iterable[str]) -> frozenset[str]:
    symbols = frozenset(master)
    if not symbols:
        raise _refuse("the instrument master is empty; nothing can be resolved")
    return symbols


def build_base_universe(
    *,
    base: Base,
    as_of: dt.date,
    master: Iterable[str],
    constituents: Iterable[Constituent] | None = None,
    fno_rows: Iterable[Mapping[str, Any]] | None = None,
    custom: Iterable[str] = (),
) -> BaseUniverse:
    """``base`` resolved against the instrument master's active symbols."""
    known = _master(master)
    if base in INDEX_BASES:
        if constituents is None:
            raise _refuse(f"base {base} needs its constituent file")
        members = tuple(constituents)
        placeholders = tuple(sorted(c.tradingsymbol for c in members if c.is_placeholder))
        real = [c for c in members if not c.is_placeholder]
        resolved = {c.tradingsymbol: c.industry for c in real if c.tradingsymbol in known}
        unresolved = tuple(sorted(c.tradingsymbol for c in real if c.tradingsymbol not in known))
        if len(unresolved) > max(2, int(len(real) * MAX_UNRESOLVED_SHARE)):
            raise _refuse(
                f"{len(unresolved)} of {len(real)} {base} constituents are not in the "
                f"instrument master (first: {unresolved[0]}); the master is stale or wrong"
            )
        return BaseUniverse(
            base=base,
            as_of=as_of,
            symbols=tuple(sorted(resolved)),
            unresolved=unresolved,
            placeholders=placeholders,
            industries=resolved,
        )
    if base == "fno":
        if fno_rows is None:
            raise _refuse("base fno needs the broker's NFO instruments")
        names = fno_underlyings(fno_rows, as_of=as_of)
        stocks = tuple(sorted(names & known))
        if len(stocks) < MIN_FNO_STOCKS:
            raise _refuse(
                f"only {len(stocks)} F&O underlyings are NSE equities in the master; "
                f"the F&O list or the master is incomplete"
            )
        return BaseUniverse(
            base=base,
            as_of=as_of,
            symbols=stocks,
            # Indices, by the rule in the design record - named so that a
            # stock wrongly among them is visible.
            unresolved=tuple(sorted(names - known)),
            placeholders=(),
            industries={},
        )
    if base == "custom":
        wanted = tuple(dict.fromkeys(custom))
        if not wanted:
            raise _refuse("base custom with no custom_symbols is an empty universe")
        missing = [s for s in wanted if s not in known]
        if missing:
            # A typo is not a smaller universe.
            raise _refuse(
                f"{len(missing)} custom symbol(s) are not in the instrument master "
                f"(first: {missing[0][:32]!r})"
            )
        return BaseUniverse(
            base=base,
            as_of=as_of,
            symbols=tuple(sorted(wanted)),
            unresolved=(),
            placeholders=(),
            industries={},
        )
    raise _refuse(f"{base!r} is not a base universe")


@dataclass(frozen=True)
class Reconstitution:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    previous_size: int

    @property
    def looks_implausible(self) -> bool:
        changed = len(self.added) + len(self.removed)
        return changed > max(4, int(self.previous_size * MAX_CHURN_SHARE))


def reconstitution(previous: Iterable[str], current: BaseUniverse) -> Reconstitution:
    """What changed since ``previous`` (yesterday's list, from the checkpoint)."""
    before = frozenset(previous)
    now = frozenset(current.symbols)
    return Reconstitution(
        added=tuple(sorted(now - before)),
        removed=tuple(sorted(before - now)),
        previous_size=len(before),
    )


Opener = Callable[[urllib.request.Request, float], Any]


#: The only origins the opener will contact - those of ``SOURCES``.
_ORIGINS: Final = ("https://www.niftyindices.com/", "https://nsearchives.nseindia.com/")


def _open(request: urllib.request.Request, timeout: float) -> Any:
    """``urlopen``, for the two https origins and nothing else.

    Bandit's B310 is about ``urlopen`` following ``file:`` or a custom scheme.
    The URLs here are constants, and this check makes that a property of the
    function rather than of its callers: anything else is refused before a
    socket opens (``test_universe.py::TestTheOpenerRefuses``).
    """
    if not request.full_url.startswith(_ORIGINS):
        raise _refuse(f"refusing to open {request.full_url[:48]!r}: not one of {_ORIGINS}")
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310  # nosec B310 - origin checked above


def fetch_index_file(
    base: IndexBase,
    source: str = SOURCES[0],
    *,
    opener: Opener = _open,
    timeout: float = 20.0,
) -> bytes:
    """Download one index's constituent file from one of ``SOURCES``.

    Raises rather than return a part.
    """
    if base not in INDEX_BASES:
        raise _refuse(f"{base!r} is not an index base")
    if source not in SOURCES:
        raise _refuse("not a constituent-file source")
    url = source.format(base=base)
    request = urllib.request.Request(  # noqa: S310 - fixed https URL
        url, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/csv"}
    )
    try:
        with opener(request, timeout) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise _refuse(f"the {base} file answered HTTP {status}")
            body: bytes = response.read(MAX_FILE_BYTES + 1)
    except urllib.error.URLError as exc:
        raise _refuse(f"the {base} file could not be fetched ({type(exc).__name__})") from exc
    if len(body) > MAX_FILE_BYTES:
        raise _refuse(f"the {base} file exceeds {MAX_FILE_BYTES} bytes")
    return body


def load_base_universe(
    config: UniverseConfig,
    *,
    as_of: dt.date,
    master: Iterable[str],
    fetch: Callable[[IndexBase, str], bytes] = fetch_index_file,
    nfo_rows: Callable[[], Iterable[Mapping[str, Any]]] | None = None,
) -> BaseUniverse:
    """The configured base universe for ``as_of``: ``config.base`` read here.

    ``nfo_rows`` is the read-only broker's NFO instrument list, asked for only
    when the base is ``fno``. ``master`` is the instrument master's ACTIVE
    symbols (``InstrumentRepository.active_symbols``).
    """
    base = config.base
    if base == "fno":
        if nfo_rows is None:
            raise _refuse("base fno needs the broker's NFO instruments")
        return build_base_universe(base=base, as_of=as_of, master=master, fno_rows=nfo_rows())
    if base == "custom":
        return build_base_universe(
            base=base, as_of=as_of, master=master, custom=config.custom_symbols
        )
    refusals: list[str] = []
    for source in SOURCES:
        try:
            constituents = parse_constituents(fetch(base, source), base=base)
        except UniverseSourceError as exc:
            # The next source is tried only on a REFUSAL - an answer that is
            # not the file. Each is held to the same parse, so the mirror can
            # never be a looser path than the publisher.
            refusals.append(f"{source.split('/')[2]}: {exc}")
            continue
        universe = build_base_universe(
            base=base, as_of=as_of, master=master, constituents=constituents
        )
        return dataclasses.replace(universe, source=source.format(base=base))
    raise _refuse(f"no source served the {base} file - " + "; ".join(refusals))
