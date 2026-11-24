"""QA-SEC-45: a CSV the ``csv`` module cannot read is refused, never a crash.

``csv`` raises ``csv.Error`` - not a ``ValueError`` - for an unterminated quote
or a field over its 128 KiB limit. Three parsers of files from the internet
(the bhavcopy, the index constituents, the surveillance roster) each caught
only their own refusal type, so such a file escaped as an exception: a
backfill aborted where it should have named one day REFUSED. Found by E04-S01's
STRIDE pass. Every CSV is now read through ``common.text.csv_rows``.
"""

from __future__ import annotations

import datetime as dt
import gzip
import io
import zipfile
from pathlib import Path

import pytest

from algotrader.common.text import MalformedCsvError, csv_rows
from algotrader.premarket.bhavcopy import BhavcopyError, parse_bhavcopy
from algotrader.premarket.surveillance import RosterError, parse_roster
from algotrader.premarket.universe import UniverseSourceError, parse_constituents

CODE = Path(__file__).resolve().parents[2]
SRC = CODE / "src" / "algotrader"
FIXTURES = CODE / "tests" / "fixtures"
#: One field past csv's 131,072-character limit, as a damaged quote produces.
OVERSIZED = b'"' + b"A" * 140_000 + b'"'


def test_every_csv_in_src_is_read_through_the_one_helper() -> None:
    readers = [
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if "csv.reader(" in (text := path.read_text(encoding="utf-8")) or "csv.DictReader(" in text
    ]
    assert readers == ["common/text.py"], (
        f"read CSV through common.text.csv_rows, which refuses what csv cannot read: {readers}"
    )


def test_the_helper_reads_csv_and_refuses_what_it_cannot() -> None:
    assert csv_rows('a,b\r\n"c,d",e\n') == [["a", "b"], ["c,d", "e"]]
    with pytest.raises(MalformedCsvError, match="field larger than field limit"):
        csv_rows(OVERSIZED.decode())


def test_the_refusal_does_not_echo_the_content() -> None:
    with pytest.raises(MalformedCsvError) as caught:
        csv_rows('"' + "secret\n" * 30_000)
    assert "secret" not in str(caught.value)


def test_the_bhavcopy_refuses_it() -> None:
    raw = (FIXTURES / "bhavcopy" / "BhavCopy_NSE_CM_0_0_0_20261120_F_0000.csv.zip").read_bytes()
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        name = zf.namelist()[0]
        body = zf.read(name)
    damaged = io.BytesIO()
    with zipfile.ZipFile(damaged, "w") as zf:
        zf.writestr(name, body + OVERSIZED + b"\n")
    with pytest.raises(BhavcopyError, match="not well-formed CSV"):
        parse_bhavcopy(damaged.getvalue(), day=dt.date(2026, 11, 20))


def test_the_index_constituents_refuse_it() -> None:
    raw = b"Company Name,Industry,Symbol,Series,ISIN Code\n" + OVERSIZED + b"\n"
    with pytest.raises(UniverseSourceError, match="not well-formed CSV"):
        parse_constituents(raw, base="nifty50")


def test_the_surveillance_roster_refuses_it() -> None:
    raw = gzip.decompress((FIXTURES / "surveillance" / "REG1_IND231126.csv.gz").read_bytes())
    with pytest.raises(RosterError, match="not well-formed CSV"):
        parse_roster(raw + OVERSIZED + b"\n", session=dt.date(2026, 11, 23))
