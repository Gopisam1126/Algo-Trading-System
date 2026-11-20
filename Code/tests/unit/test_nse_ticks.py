"""The NSE tick schedule the tests use is the one NSE published (audit NR-006).

``tests/doubles.nse_equity_tick`` is a market constant written down once, so
its band edges are pinned here against the published table rather than
against itself: each row is a literal from NSE/CMTR/67133 (via Zerodha's
bulletin 408151), not a value the helper computed.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.doubles import nse_equity_tick

# (reference close, tick) - the edge of every band, on both sides.
PUBLISHED = [
    ("0.05", "0.01"),
    ("249.99", "0.01"),
    ("250", "0.05"),  # ">= 250"
    ("1000", "0.05"),  # "to 1000"
    ("1000.05", "0.10"),  # "> 1000"
    ("1200", "0.10"),  # the price level of the SIT and integration fixtures
    ("5000", "0.10"),
    ("5000.50", "0.50"),
    ("10000", "0.50"),
    ("10001", "1"),
    ("20000", "1"),
    ("20000.05", "5"),
    ("150000", "5"),
]


@pytest.mark.parametrize(("close", "tick"), PUBLISHED)
def test_the_band_edges_match_the_published_table(close: str, tick: str) -> None:
    assert nse_equity_tick(Decimal(close)) == Decimal(tick)


@pytest.mark.parametrize("close", ["0", "-1"])
def test_a_non_positive_close_is_refused(close: str) -> None:
    with pytest.raises(ValueError, match="positive"):
        nse_equity_tick(Decimal(close))


def test_a_rs_1200_stock_is_not_on_the_old_flat_tick() -> None:
    """The control for NR-006: the flat 0.05 the fixtures used is wrong here."""
    assert nse_equity_tick(Decimal("1200")) != Decimal("0.05")
