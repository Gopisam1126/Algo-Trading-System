"""E07-S01: the base universe, against the files as NSE Indices and Kite publish them.

Fixtures in ``tests/fixtures/universe/`` were downloaded on 22 Nov 2026:
the four constituent files from niftyindices.com, the futures rows of Kite's
public NFO instrument list, and the tradingsymbols of Kite's NSE equity list
(the instrument master's source). Every expected number below was counted
from those files by hand (``wc``, ``awk``), not by this module.
"""

from __future__ import annotations

import datetime as dt
import io
import urllib.error
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from algotrader.common.config import UniverseConfig
from algotrader.premarket import universe as u
from algotrader.premarket.universe import (
    BaseUniverse,
    UniverseSourceError,
    build_base_universe,
    fetch_index_file,
    fno_underlyings,
    load_base_universe,
    parse_constituents,
    reconstitution,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "universe"
AS_OF = dt.date(2026, 11, 22)
INDICES = {"BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTY", "NIFTYFPI", "NIFTYNXT50"}


def _file(base: str) -> bytes:
    return (FIXTURES / f"ind_{base}list.csv").read_bytes()


def _by_base(base: str, _source: str) -> bytes:
    return _file(base)


def _master() -> frozenset[str]:
    lines = (FIXTURES / "nse_eq_symbols_20261122.txt").read_text(encoding="utf-8").splitlines()
    return frozenset(line for line in lines if line and not line.startswith("#"))


def _nfo() -> list[dict[str, Any]]:
    import csv

    with (FIXTURES / "nfo_futures_20261122.csv").open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _lines(base: str) -> list[str]:
    return _file(base).decode("utf-8").splitlines()


def _bytes(lines: list[str]) -> bytes:
    return ("\n".join(lines) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# AC1 - the published files parse; anything else is refused with a reason
# ---------------------------------------------------------------------------


class TestThePublishedFiles:
    @pytest.mark.parametrize(
        ("base", "count"),
        [("nifty50", 50), ("nifty100", 100), ("nifty200", 200), ("nifty500", 501)],
    )
    def test_each_index_parses_to_its_constituents(self, base: str, count: int) -> None:
        assert len(parse_constituents(_file(base), base=base)) == count

    def test_a_trade_to_trade_stock_is_named_as_kite_names_it(self) -> None:
        by = {c.symbol: c for c in parse_constituents(_file("nifty500"), base="nifty500")}
        assert (by["HFCL"].series, by["HFCL"].tradingsymbol) == ("BE", "HFCL-BE")
        assert by["EMBASSY"].tradingsymbol == "EMBASSY-RR", "a REIT carries its series"
        assert by["RELIANCE"].tradingsymbol == "RELIANCE"

    def test_the_demerger_placeholder_is_recognised(self) -> None:
        """Nifty 500 listed 501 on 22 Nov 2026 because of 'Dummy HEG Ltd.'."""
        found = parse_constituents(_file("nifty500"), base="nifty500")
        assert [c.symbol for c in found if c.is_placeholder] == ["DUMMYHEG"]

    def test_the_industry_is_kept(self) -> None:
        by = {c.symbol: c for c in parse_constituents(_file("nifty50"), base="nifty50")}
        assert by["ULTRACEMCO"].industry == "Construction Materials"

    def test_a_byte_order_mark_is_accepted(self) -> None:
        assert len(parse_constituents(b"\xef\xbb\xbf" + _file("nifty50"), base="nifty50")) == 50


class TestWhatIsRefused:
    def test_an_html_error_page(self) -> None:
        with pytest.raises(UniverseSourceError, match="header"):
            parse_constituents(b"<html><body>Access Denied</body></html>", base="nifty50")

    def test_a_different_header(self) -> None:
        lines = _lines("nifty50")
        lines[0] = "Company Name,Industry,Symbol,Series,ISIN"
        with pytest.raises(UniverseSourceError, match="header"):
            parse_constituents(_bytes(lines), base="nifty50")

    def test_a_truncated_download(self) -> None:
        """40 of 50 is below 95%."""
        with pytest.raises(UniverseSourceError, match="truncated"):
            parse_constituents(_bytes(_lines("nifty50")[:41]), base="nifty50")

    def test_control_two_short_is_a_mid_period_change_not_a_truncation(self) -> None:
        """48 of 50 is 96%: accepted."""
        assert len(parse_constituents(_bytes(_lines("nifty50")[:49]), base="nifty50")) == 48

    def test_another_indexs_file(self) -> None:
        """The Nifty 200 file offered as Nifty 50: 200 is not 50 plus a few."""
        with pytest.raises(UniverseSourceError, match="lists 200"):
            parse_constituents(_file("nifty200"), base="nifty50")

    def test_a_duplicated_constituent(self) -> None:
        lines = _lines("nifty50")
        with pytest.raises(UniverseSourceError, match="twice"):
            parse_constituents(_bytes([*lines, lines[1]]), base="nifty50")

    def test_a_symbol_that_could_forge_a_log_line(self) -> None:
        lines = _lines("nifty50")
        lines[1] = 'Evil Ltd.,Services,"ADANI\nENT",EQ,INE423A01024'
        with pytest.raises(UniverseSourceError, match="symbol"):
            parse_constituents(_bytes(lines), base="nifty50")

    @pytest.mark.parametrize("isin", ["US0378331005", "INE423A0102", "", "INE423A0102X"])
    def test_an_isin_that_is_not_indian_or_a_placeholder(self, isin: str) -> None:
        lines = _lines("nifty50")
        lines[1] = f"Adani Enterprises Ltd.,Metals & Mining,ADANIENT,EQ,{isin}"
        with pytest.raises(UniverseSourceError, match="ISIN"):
            parse_constituents(_bytes(lines), base="nifty50")

    def test_a_series_that_is_not_one(self) -> None:
        lines = _lines("nifty50")
        lines[1] = "Adani Enterprises Ltd.,Metals & Mining,ADANIENT,EQUITY,INE423A01024"
        with pytest.raises(UniverseSourceError, match="series"):
            parse_constituents(_bytes(lines), base="nifty50")

    def test_a_row_with_no_industry(self) -> None:
        lines = _lines("nifty50")
        lines[1] = "Adani Enterprises Ltd.,,ADANIENT,EQ,INE423A01024"
        with pytest.raises(UniverseSourceError, match="industry"):
            parse_constituents(_bytes(lines), base="nifty50")

    def test_a_row_with_the_wrong_number_of_fields(self) -> None:
        lines = _lines("nifty50")
        lines[1] += ",extra"
        with pytest.raises(UniverseSourceError, match="fields"):
            parse_constituents(_bytes(lines), base="nifty50")

    def test_a_response_too_large_to_be_a_constituent_file(self) -> None:
        with pytest.raises(UniverseSourceError, match="bytes"):
            parse_constituents(b"x" * (u.MAX_FILE_BYTES + 1), base="nifty50")

    def test_bytes_that_are_not_text(self) -> None:
        with pytest.raises(UniverseSourceError, match="UTF-8"):
            parse_constituents(b"\xff\xfe\x00\x00", base="nifty50")

    def test_a_base_that_is_not_an_index(self) -> None:
        with pytest.raises(UniverseSourceError):
            parse_constituents(_file("nifty50"), base="fno")


# ---------------------------------------------------------------------------
# AC4 - resolved against the instrument master; what does not resolve is named
# ---------------------------------------------------------------------------


class TestResolution:
    def test_every_nifty500_constituent_resolves_but_the_placeholder(self) -> None:
        got = build_base_universe(
            base="nifty500",
            as_of=AS_OF,
            master=_master(),
            constituents=parse_constituents(_file("nifty500"), base="nifty500"),
        )
        assert len(got) == 500 and got.unresolved == ()
        assert got.placeholders == ("DUMMYHEG",)
        assert "HFCL-BE" in got.symbols and "HFCL" not in got.symbols
        assert got.industries["RELIANCE"] == "Oil Gas & Consumable Fuels"
        assert list(got.symbols) == sorted(got.symbols)

    def test_a_few_missing_from_the_master_are_named_not_dropped(self) -> None:
        master = _master() - {"TRENT", "ULTRACEMCO"}
        got = build_base_universe(
            base="nifty50",
            as_of=AS_OF,
            master=master,
            constituents=parse_constituents(_file("nifty50"), base="nifty50"),
        )
        assert len(got) == 48 and got.unresolved == ("TRENT", "ULTRACEMCO")

    def test_a_stale_master_is_refused(self) -> None:
        members = parse_constituents(_file("nifty200"), base="nifty200")
        master = _master() - {c.tradingsymbol for c in members[:20]}
        with pytest.raises(UniverseSourceError, match="stale"):
            build_base_universe(base="nifty200", as_of=AS_OF, master=master, constituents=members)

    def test_an_empty_master_is_refused(self) -> None:
        with pytest.raises(UniverseSourceError, match="empty"):
            build_base_universe(
                base="nifty50",
                as_of=AS_OF,
                master=[],
                constituents=parse_constituents(_file("nifty50"), base="nifty50"),
            )

    def test_an_index_base_without_its_file_is_refused(self) -> None:
        with pytest.raises(UniverseSourceError, match="constituent"):
            build_base_universe(base="nifty50", as_of=AS_OF, master=_master())


# ---------------------------------------------------------------------------
# AC2 - the F&O universe: stock underlyings with a live future
# ---------------------------------------------------------------------------


class TestTheFnoUniverse:
    def test_the_real_list_is_218_stocks_and_the_six_indices_are_named(self) -> None:
        got = build_base_universe(base="fno", as_of=AS_OF, master=_master(), fno_rows=_nfo())
        assert len(got) == 218
        assert set(got.unresolved) == INDICES
        assert "RELIANCE" in got.symbols and "NIFTY" not in got.symbols

    def test_every_underlying_is_one_or_the_other(self) -> None:
        names = fno_underlyings(_nfo(), as_of=AS_OF)
        assert len(names) == 224

    def test_expired_contracts_do_not_count(self) -> None:
        """After the last listed expiry (25 Jan 2027) nothing is still trading."""
        assert fno_underlyings(_nfo(), as_of=dt.date(2027, 1, 26)) == frozenset()
        with pytest.raises(UniverseSourceError, match="F&O"):
            build_base_universe(
                base="fno", as_of=dt.date(2027, 1, 26), master=_master(), fno_rows=_nfo()
            )

    def test_control_the_last_expiry_day_still_counts(self) -> None:
        assert len(fno_underlyings(_nfo(), as_of=dt.date(2027, 1, 25))) > 200

    def test_options_and_unreadable_expiries_are_not_futures(self) -> None:
        rows = [
            {"segment": "NFO-OPT", "instrument_type": "CE", "name": "TCS", "expiry": "2026-12-29"},
            {"segment": "NFO-FUT", "instrument_type": "FUT", "name": "INFY", "expiry": ""},
            {"segment": "NFO-FUT", "instrument_type": "FUT", "name": "", "expiry": "2026-12-29"},
            {
                "segment": "NFO-FUT",
                "instrument_type": "FUT",
                "name": "SBIN",
                "expiry": dt.date(2026, 12, 29),
            },
        ]
        assert fno_underlyings(rows, as_of=AS_OF) == {"SBIN"}

    def test_a_master_that_lost_its_stocks_is_refused(self) -> None:
        master = frozenset(list(_master())[:50])
        with pytest.raises(UniverseSourceError, match="incomplete"):
            build_base_universe(base="fno", as_of=AS_OF, master=master, fno_rows=_nfo())


# ---------------------------------------------------------------------------
# AC3 - a custom list: every symbol real, or no universe
# ---------------------------------------------------------------------------


class TestACustomList:
    def test_resolved_and_sorted(self) -> None:
        got = build_base_universe(
            base="custom", as_of=AS_OF, master=_master(), custom=["TCS", "INFY", "TCS"]
        )
        assert got.symbols == ("INFY", "TCS")

    def test_a_typo_is_not_a_smaller_universe(self) -> None:
        with pytest.raises(UniverseSourceError, match="not in the instrument master"):
            build_base_universe(base="custom", as_of=AS_OF, master=_master(), custom=["INFYY"])

    def test_an_empty_list(self) -> None:
        with pytest.raises(UniverseSourceError, match="empty"):
            build_base_universe(base="custom", as_of=AS_OF, master=_master(), custom=[])


class TestTheConfig:
    def test_the_default_is_nifty200(self) -> None:
        assert UniverseConfig().base == "nifty200"

    @pytest.mark.parametrize("base", ["nifty50", "nifty100", "nifty200", "nifty500", "fno"])
    def test_each_built_base_is_accepted(self, base: str) -> None:
        assert UniverseConfig(base=base).base == base

    def test_a_base_nothing_builds_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            UniverseConfig(base="nifty_midcap_150")

    def test_custom_needs_its_symbols(self) -> None:
        with pytest.raises(ValidationError, match="custom_symbols"):
            UniverseConfig(base="custom")

    def test_symbols_under_another_base_would_be_read_by_nothing(self) -> None:
        with pytest.raises(ValidationError, match="only when"):
            UniverseConfig(base="nifty200", custom_symbols=["TCS"])

    def test_a_custom_symbol_is_held_to_the_one_symbol_rule(self) -> None:
        with pytest.raises(ValidationError):
            UniverseConfig(base="custom", custom_symbols=["TCS\nCRITICAL forged"])

    def test_a_custom_symbol_twice(self) -> None:
        with pytest.raises(ValidationError, match="twice"):
            UniverseConfig(base="custom", custom_symbols=["TCS", "TCS"])

    def test_control_a_custom_list(self) -> None:
        assert UniverseConfig(base="custom", custom_symbols=["TCS"]).custom_symbols == ["TCS"]


# ---------------------------------------------------------------------------
# AC5 - reconstitution: what changed, and a churn no index produces
# ---------------------------------------------------------------------------


def _universe(symbols: list[str]) -> BaseUniverse:
    return BaseUniverse(
        base="custom",
        as_of=AS_OF,
        symbols=tuple(sorted(symbols)),
        unresolved=(),
        placeholders=(),
        industries={},
    )


class TestReconstitution:
    def test_additions_and_removals_are_named(self) -> None:
        yesterday = [f"S{i}" for i in range(50)]
        today = _universe([*yesterday[1:], "NEW"])
        change = reconstitution(yesterday, today)
        assert (change.added, change.removed) == (("NEW",), ("S0",))
        assert not change.looks_implausible

    def test_a_churn_no_reconstitution_produces(self) -> None:
        yesterday = [f"S{i}" for i in range(50)]
        today = _universe([f"T{i}" for i in range(50)])
        assert reconstitution(yesterday, today).looks_implausible

    def test_control_a_semi_annual_review_is_plausible(self) -> None:
        """Ten of fifty swapped is 20% - at the bound, allowed."""
        yesterday = [f"S{i}" for i in range(50)]
        today = _universe([*yesterday[5:], *[f"N{i}" for i in range(5)]])
        assert not reconstitution(yesterday, today).looks_implausible

    def test_one_past_the_bound_is_not(self) -> None:
        yesterday = [f"S{i}" for i in range(50)]
        today = _universe([*yesterday[6:], *[f"N{i}" for i in range(5)]])
        assert reconstitution(yesterday, today).looks_implausible


# ---------------------------------------------------------------------------
# The fetch and the configured load
# ---------------------------------------------------------------------------


class _Response(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200) -> None:
        super().__init__(body)
        self.status = status

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class TestTheFetch:
    def test_the_fixed_https_source_for_the_base(self) -> None:
        asked: list[str] = []

        def opener(request, timeout):
            asked.append(request.full_url)
            return _Response(_file("nifty50"))

        assert fetch_index_file("nifty50", opener=opener) == _file("nifty50")
        assert asked == ["https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv"]

    def test_an_http_error_is_a_refusal(self) -> None:
        with pytest.raises(UniverseSourceError, match="HTTP 403"):
            fetch_index_file("nifty50", opener=lambda r, t: _Response(b"", status=403))

    def test_an_unreachable_source_is_a_refusal(self) -> None:
        def opener(request, timeout):
            raise urllib.error.URLError("no route")

        with pytest.raises(UniverseSourceError, match="URLError"):
            fetch_index_file("nifty50", opener=opener)

    def test_an_oversized_body_is_refused_without_reading_it_all(self) -> None:
        with pytest.raises(UniverseSourceError, match="exceeds"):
            fetch_index_file(
                "nifty50", opener=lambda r, t: _Response(b"x" * (u.MAX_FILE_BYTES + 50))
            )

    def test_only_an_index_has_a_file(self) -> None:
        with pytest.raises(UniverseSourceError):
            fetch_index_file("fno")  # type: ignore[arg-type]


class TestTheConfiguredLoad:
    def test_an_index_base_fetches_its_file(self) -> None:
        got = load_base_universe(
            UniverseConfig(base="nifty100"), as_of=AS_OF, master=_master(), fetch=_by_base
        )
        assert len(got) == 100 and got.base == "nifty100"

    def test_fno_asks_the_broker_only_then(self) -> None:
        asked: list[str] = []

        def rows() -> list[dict[str, Any]]:
            asked.append("nfo")
            return _nfo()

        load_base_universe(
            UniverseConfig(), as_of=AS_OF, master=_master(), fetch=_by_base, nfo_rows=rows
        )
        assert asked == []
        got = load_base_universe(
            UniverseConfig(base="fno"), as_of=AS_OF, master=_master(), nfo_rows=rows
        )
        assert asked == ["nfo"] and len(got) == 218

    def test_fno_without_the_broker_is_refused(self) -> None:
        with pytest.raises(UniverseSourceError, match="NFO"):
            load_base_universe(UniverseConfig(base="fno"), as_of=AS_OF, master=_master())

    def test_custom_reads_the_configured_symbols(self) -> None:
        got = load_base_universe(
            UniverseConfig(base="custom", custom_symbols=["SBIN", "TCS"]),
            as_of=AS_OF,
            master=_master(),
        )
        assert got.symbols == ("SBIN", "TCS")


class TestTheOpenerRefuses:
    """The executed check behind the ``nosec B310`` on ``_open``."""

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "http://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv",
            "https://www.niftyindices.com.evil.example/x.csv",
            "https://evil.example/ind_nifty50list.csv",
            "ftp://www.niftyindices.com/x.csv",
        ],
    )
    def test_anything_but_the_one_https_origin(self, url: str) -> None:
        import urllib.request

        with pytest.raises(UniverseSourceError, match="refusing to open"):
            u._open(urllib.request.Request(url), 1.0)

    def test_every_source_is_on_an_allowed_origin(self) -> None:
        for source in u.SOURCES:
            assert source.format(base="nifty50").startswith(u._ORIGINS)
        assert len(u._ORIGINS) == len(u.SOURCES) == 2

    def test_a_look_alike_of_the_mirror(self) -> None:
        import urllib.request

        with pytest.raises(UniverseSourceError, match="refusing to open"):
            u._open(urllib.request.Request("https://nsearchives.nseindia.com.evil.example/x"), 1.0)


class TestTwoSources:
    """SIT, 22 Nov 2026: niftyindices.com answered 200 with an HTML page after
    three requests in a row; NSE's archive kept serving the CSV."""

    HTML = b"<!DOCTYPE html><html><head><title>NSE Indices</title></head></html>"

    def test_the_mirror_serves_when_the_publisher_answers_html(self) -> None:
        asked: list[str] = []

        def fetch(base: str, source: str) -> bytes:
            asked.append(source.split("/")[2])
            return self.HTML if "niftyindices" in source else _file(base)

        got = load_base_universe(
            UniverseConfig(base="nifty50"), as_of=AS_OF, master=_master(), fetch=fetch
        )
        assert asked == ["www.niftyindices.com", "nsearchives.nseindia.com"]
        assert len(got) == 50
        assert got.source == "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv"

    def test_control_the_publisher_first_and_the_mirror_never_asked(self) -> None:
        asked: list[str] = []

        def fetch(base: str, source: str) -> bytes:
            asked.append(source.split("/")[2])
            return _file(base)

        got = load_base_universe(
            UniverseConfig(base="nifty50"), as_of=AS_OF, master=_master(), fetch=fetch
        )
        assert asked == ["www.niftyindices.com"]
        assert got.source.startswith("https://www.niftyindices.com/")

    def test_both_refusing_is_one_refusal_naming_each(self) -> None:
        with pytest.raises(UniverseSourceError, match="no source") as caught:
            load_base_universe(
                UniverseConfig(base="nifty50"),
                as_of=AS_OF,
                master=_master(),
                fetch=lambda _b, _s: self.HTML,
            )
        assert "www.niftyindices.com" in str(caught.value)
        assert "nsearchives.nseindia.com" in str(caught.value)

    def test_the_mirror_is_held_to_the_same_parse(self) -> None:
        """A truncated mirror is refused exactly as a truncated publisher is."""
        short = _bytes(_lines("nifty50")[:41])
        with pytest.raises(UniverseSourceError, match="truncated"):
            load_base_universe(
                UniverseConfig(base="nifty50"),
                as_of=AS_OF,
                master=_master(),
                fetch=lambda _b, s: self.HTML if "niftyindices" in s else short,
            )

    def test_only_a_listed_source_is_fetched(self) -> None:
        with pytest.raises(UniverseSourceError, match="source"):
            fetch_index_file("nifty50", "https://evil.example/{base}.csv")

    def test_the_mirror_url_is_fetched_as_listed(self) -> None:
        asked: list[str] = []

        def opener(request, timeout):
            asked.append(request.full_url)
            return _Response(_file("nifty50"))

        fetch_index_file("nifty50", u.SOURCES[1], opener=opener)
        assert asked == ["https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv"]
