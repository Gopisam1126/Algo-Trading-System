"""Tests for the NSE calendar — particularly the per-stock square-off logic.

Constraint C5 says every intraday position must close on OUR schedule, before
the broker's auto square-off.  Since NSE's Closing Auction Session went live
on 2026-08-03 the broker's deadline differs per stock, so these tests exist to
catch a regression that would otherwise show up as unexplained daily slippage.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from algotrader.common.calendar import (
    IST,
    SQUAREOFF_CAS_STOCKS,
    SQUAREOFF_FNO,
    SQUAREOFF_NON_CAS,
    MarketCalendar,
)


@pytest.fixture
def cal() -> MarketCalendar:
    return MarketCalendar(holidays=frozenset({date(2026, 8, 15)}))  # Independence Day


def ist_at(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=IST)


class TestTradingDays:
    def test_weekday_is_trading_day(self, cal: MarketCalendar) -> None:
        assert cal.is_trading_day(date(2026, 8, 4))  # Tuesday

    def test_saturday_is_not(self, cal: MarketCalendar) -> None:
        assert not cal.is_trading_day(date(2026, 8, 8))  # Saturday

    def test_holiday_is_not(self, cal: MarketCalendar) -> None:
        assert not cal.is_trading_day(date(2026, 8, 15))

    def test_next_trading_day_skips_weekend(self, cal: MarketCalendar) -> None:
        assert cal.next_trading_day(date(2026, 8, 7)) == date(2026, 8, 10)  # Fri -> Mon


class TestSessionState:
    def test_pre_open_window(self, cal: MarketCalendar) -> None:
        assert cal.is_pre_open(ist_at(2026, 8, 4, 9, 5))
        assert not cal.is_pre_open(ist_at(2026, 8, 4, 9, 20))

    def test_market_open_window(self, cal: MarketCalendar) -> None:
        assert cal.is_market_open(ist_at(2026, 8, 4, 11, 0))
        assert not cal.is_market_open(ist_at(2026, 8, 4, 8, 0))
        assert not cal.is_market_open(ist_at(2026, 8, 4, 16, 0))

    def test_cas_stock_continuous_ends_earlier(self, cal: MarketCalendar) -> None:
        """CAS-scope stocks stop continuous trading at 15:15, not 15:30."""
        at_1520 = ist_at(2026, 8, 4, 15, 20)
        assert not cal.is_continuous_for(at_1520, is_cas_stock=True)
        assert cal.is_continuous_for(at_1520, is_cas_stock=False)

    def test_naive_datetime_rejected(self, cal: MarketCalendar) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            cal.is_market_open(datetime(2026, 8, 4, 11, 0))


class TestSquareOffDeadlines:
    """The per-stock deadline is the whole point of this section."""

    def test_broker_times_differ_by_stock_class(self, cal: MarketCalendar) -> None:
        assert cal.broker_squareoff_time(is_cas_stock=True) == SQUAREOFF_CAS_STOCKS
        assert cal.broker_squareoff_time(is_cas_stock=False) == SQUAREOFF_NON_CAS
        assert cal.broker_squareoff_time(is_cas_stock=False, is_fno=True) == SQUAREOFF_FNO

    def test_cas_deadline_is_earlier_than_non_cas(self, cal: MarketCalendar) -> None:
        d = date(2026, 8, 4)
        cas = cal.squareoff_deadline(d, is_cas_stock=True)
        non_cas = cal.squareoff_deadline(d, is_cas_stock=False)
        assert cas < non_cas, "CAS stocks must square off earlier"

    @pytest.mark.parametrize("is_cas,is_fno", [(True, False), (False, False), (False, True)])
    def test_our_deadline_always_precedes_brokers(
        self, cal: MarketCalendar, is_cas: bool, is_fno: bool
    ) -> None:
        """Constraint C5, stated directly.

        We must never be the one being force-closed.
        """
        d = date(2026, 8, 4)
        ours = cal.squareoff_deadline(d, is_cas_stock=is_cas, is_fno=is_fno, buffer_minutes=5)
        broker_time = cal.broker_squareoff_time(is_cas_stock=is_cas, is_fno=is_fno)
        theirs = datetime.combine(d, broker_time, tzinfo=IST).astimezone(UTC)
        assert ours < theirs

    def test_deadline_is_utc(self, cal: MarketCalendar) -> None:
        deadline = cal.squareoff_deadline(date(2026, 8, 4), is_cas_stock=True)
        assert deadline.tzinfo is not None
        assert deadline.utcoffset() == datetime.now(UTC).utcoffset()

    def test_minutes_to_squareoff_counts_down(self, cal: MarketCalendar) -> None:
        moment = ist_at(2026, 8, 4, 14, 0)
        mins = cal.minutes_to_squareoff(moment, is_cas_stock=True, buffer_minutes=5)
        assert 60 < mins < 70  # 14:00 -> 15:05 is 65 minutes


class TestBarAlignment:
    """Bars align to the session start (09:15), not to wall-clock hours."""

    def test_15m_bar_aligns_to_session_start(self, cal: MarketCalendar) -> None:
        aligned = cal.bar_open_time(ist_at(2026, 8, 4, 9, 37), 900)
        assert aligned.astimezone(IST).time() == time(9, 30)

    def test_first_bar_of_session(self, cal: MarketCalendar) -> None:
        aligned = cal.bar_open_time(ist_at(2026, 8, 4, 9, 20), 900)
        assert aligned.astimezone(IST).time() == time(9, 15)

    def test_hourly_bar_is_not_wall_clock_aligned(self, cal: MarketCalendar) -> None:
        """An hourly bar runs 09:15-10:15, NOT 09:00-10:00."""
        aligned = cal.bar_open_time(ist_at(2026, 8, 4, 10, 5), 3600)
        assert aligned.astimezone(IST).time() == time(9, 15)

    def test_before_open_clamps_to_open(self, cal: MarketCalendar) -> None:
        aligned = cal.bar_open_time(ist_at(2026, 8, 4, 8, 30), 300)
        assert aligned.astimezone(IST).time() == time(9, 15)


class TestTimezoneDiscipline:
    def test_ist_offset(self) -> None:
        offset = datetime(2026, 8, 4, tzinfo=ZoneInfo("Asia/Kolkata")).utcoffset()
        assert offset is not None
        assert offset.total_seconds() == 5.5 * 3600


# ---------------------------------------------------------------------------
# Regression: OHLC coherence
#
# A field_validator on `high` only sees fields declared before it, so checks
# against `low` and `close` were silently no-ops and `close > high` reached
# the indicator engine as a corrupt bar. Fixed by moving to a model_validator.
# ---------------------------------------------------------------------------


class TestBarOHLCCoherence:
    @staticmethod
    def _bar(o: str, h: str, low: str, c: str):
        from decimal import Decimal

        from algotrader.common.enums import Timeframe
        from algotrader.common.models.market import Bar

        return Bar(
            symbol="TEST",
            timeframe=Timeframe.M5,
            open_ts=datetime.now(UTC),
            open=Decimal(o),
            high=Decimal(h),
            low=Decimal(low),
            close=Decimal(c),
            volume=100,
        )

    @pytest.mark.parametrize(
        "name,o,h,low,c",
        [
            ("close above high", "100", "105", "98", "110"),
            ("close below low", "100", "105", "98", "95"),
            ("open above high", "100", "99.5", "98", "99"),
            ("open below low", "100", "105", "101", "102"),
            ("high below low", "100", "97", "99", "98"),
        ],
    )
    def test_incoherent_bars_rejected(self, name, o, h, low, c) -> None:
        with pytest.raises(ValidationError):
            self._bar(o, h, low, c)

    def test_coherent_bar_accepted(self) -> None:
        bar = self._bar("100", "105", "98", "102")
        assert bar.high >= bar.close >= bar.low
