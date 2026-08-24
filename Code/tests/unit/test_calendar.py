"""Tests for the NSE calendar — particularly the per-stock square-off logic.

Constraint C5 says every intraday position must close on OUR schedule, before
the broker's auto square-off.  Since NSE's Closing Auction Session went live
on 2026-08-03 the broker's deadline differs per stock, so these tests exist to
catch a regression that would otherwise show up as unexplained daily slippage.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
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


class TestTradingDaySearchIsBounded:
    """Bad holiday data must fail loudly, not answer confidently.

    An unbounded search does not hang — it returns a wrong date. With a
    malformed list covering six months, ``previous_trading_day(2026-06-15)``
    returned 2025-12-31: a caller fetching "yesterday's close" would silently
    get a price from another season.

    This is a live risk rather than a theoretical one. ``config/nse_holidays.yaml``
    is hand-transcribed from the NSE circular and is currently flagged incomplete
    (blocker B3), so a paste or date-format error is exactly the kind of mistake
    waiting to happen.
    """

    @staticmethod
    def _calendar_with_a_broken_list() -> MarketCalendar:
        from datetime import date as _d
        from datetime import timedelta as _td

        return MarketCalendar(frozenset({_d(2026, 1, 1) + _td(days=i) for i in range(400)}))

    def test_previous_trading_day_refuses_to_walk_months_back(self) -> None:
        import datetime as _dt

        from algotrader.common.calendar import HolidayDataError

        with pytest.raises(HolidayDataError, match="almost certainly wrong"):
            self._calendar_with_a_broken_list().previous_trading_day(_dt.date(2026, 6, 15))

    def test_next_trading_day_refuses_to_walk_months_forward(self) -> None:
        import datetime as _dt

        from algotrader.common.calendar import HolidayDataError

        with pytest.raises(HolidayDataError, match="almost certainly wrong"):
            self._calendar_with_a_broken_list().next_trading_day(_dt.date(2026, 6, 15))

    def test_the_error_names_the_file_to_look_at(self) -> None:
        import datetime as _dt

        from algotrader.common.calendar import HolidayDataError

        with pytest.raises(HolidayDataError) as exc:
            self._calendar_with_a_broken_list().previous_trading_day(_dt.date(2026, 6, 15))
        assert "nse_holidays.yaml" in str(exc.value), "the error must say where to look"

    def test_a_normal_long_weekend_still_resolves(self) -> None:
        """The bound must not break real holiday clusters.

        Thu + Fri holidays either side of a weekend is four consecutive
        non-trading days, which NSE genuinely does.
        """
        import datetime as _dt

        holidays = frozenset(
            {_dt.date(2026, 3, 19), _dt.date(2026, 3, 20)}  # Thursday, Friday
        )
        cal = MarketCalendar(holidays)
        # From Saturday, the previous trading day is Wednesday the 18th.
        assert cal.previous_trading_day(_dt.date(2026, 3, 21)) == _dt.date(2026, 3, 18)
        # From Friday, the next is Monday the 23rd.
        assert cal.next_trading_day(_dt.date(2026, 3, 20)) == _dt.date(2026, 3, 23)


class TestTradingDaysBetweenRejectsAnInvertedRange:
    def test_start_after_end_raises(self) -> None:
        """Silently returning [] would make a reversed backfill a no-op.

        A backfill that reports success having fetched nothing leaves a
        permanent gap — market data cannot be re-derived.
        """
        import datetime as _dt

        cal = MarketCalendar(frozenset())
        with pytest.raises(ValueError, match="inverted range"):
            cal.trading_days_between(_dt.date(2026, 6, 15), _dt.date(2026, 6, 1))

    def test_a_single_day_range_is_still_valid(self) -> None:
        import datetime as _dt

        cal = MarketCalendar(frozenset())
        assert cal.trading_days_between(_dt.date(2026, 6, 15), _dt.date(2026, 6, 15)) == [
            _dt.date(2026, 6, 15)
        ]

    def test_a_normal_range_excludes_weekends_and_holidays(self) -> None:
        import datetime as _dt

        cal = MarketCalendar(frozenset({_dt.date(2026, 6, 17)}))
        days = cal.trading_days_between(_dt.date(2026, 6, 15), _dt.date(2026, 6, 21))
        assert days == [
            _dt.date(2026, 6, 15),
            _dt.date(2026, 6, 16),
            _dt.date(2026, 6, 18),
            _dt.date(2026, 6, 19),
        ]


class TestTheHolidayListIsNowVerified:
    """Blocker B3, closed 24 Aug 2026.

    The list was fixed-date entries only, so every lunar-calendar festival —
    Holi, Diwali, Bakri Id, Guru Nanak Jayanti — read as a normal trading day.
    Transcribing it needed three independent publications of the circular
    because the first two disagreed: one omitted 24 Nov (Guru Nanak Jayanti),
    the other omitted 15 Jan (Maharashtra municipal elections, a separate
    special closure rather than part of the annual circular). Both are real.
    """

    def _status(self):
        from algotrader.common.calendar import load_holidays_with_status

        path = Path(__file__).resolve().parents[2] / "config" / "nse_holidays.yaml"
        return load_holidays_with_status(str(path))

    def test_the_file_declares_itself_verified(self) -> None:
        assert self._status().is_trustworthy

    def test_the_lunar_festivals_are_present(self) -> None:
        """The whole point of B3 — these are the ones a fixed-date list misses."""
        dates = self._status().dates
        for day, name in [
            (date(2026, 3, 3), "Holi"),
            (date(2026, 3, 26), "Ram Navami"),
            (date(2026, 5, 28), "Bakri Id"),
            (date(2026, 6, 26), "Muharram"),
            (date(2026, 9, 14), "Ganesh Chaturthi"),
            (date(2026, 10, 20), "Dussehra"),
            (date(2026, 11, 10), "Diwali Balipratipada"),
            (date(2026, 11, 24), "Guru Nanak Jayanti"),
        ]:
            assert day in dates, f"{name} ({day}) missing from the holiday list"

    def test_the_two_contested_dates_are_both_included(self) -> None:
        dates = self._status().dates
        assert date(2026, 1, 15) in dates, "Maharashtra election closure"
        assert date(2026, 11, 24) in dates, "Guru Nanak Jayanti"

    def test_the_trading_day_count_is_plausible(self) -> None:
        """A sanity bound rather than an exact figure: NSE runs roughly 245-250
        sessions a year, and a list that produced 260 or 200 would be wrong in
        a way no individual date check would catch."""
        from algotrader.common.calendar import MarketCalendar

        status = self._status()
        cal = MarketCalendar(status.dates, covers_years=status.covers_years)
        trading = sum(
            1 for i in range(365) if cal.is_trading_day(date(2026, 1, 1) + timedelta(days=i))
        )
        assert 240 <= trading <= 252, f"{trading} trading days is implausible for NSE"

    def test_muhurat_sunday_is_not_a_normal_trading_day(self) -> None:
        """Muhurat trading is a real session on a SUNDAY. The default is to
        stand down — a one-hour ceremonial session has different liquidity and
        spreads from the sessions every strategy was validated against."""
        from algotrader.common.calendar import MarketCalendar

        status = self._status()
        cal = MarketCalendar(status.dates, covers_years=status.covers_years)
        assert not cal.is_trading_day(date(2026, 11, 8))


class TestAnUncoveredYearIsRefusedNotAnswered:
    """A holiday list is published one year at a time.

    A 2026 file knows nothing about 2027, and answering "no holidays in 2027"
    is a calendar that looks entirely healthy right up to the moment it
    schedules a pre-market run on Republic Day.
    """

    def _calendar(self):
        from algotrader.common.calendar import MarketCalendar

        return MarketCalendar(frozenset({date(2026, 1, 26)}), covers_years=frozenset({2026}))

    def test_a_covered_year_is_answered(self) -> None:
        assert self._calendar().is_trading_day(date(2026, 8, 20))

    def test_an_uncovered_year_raises(self) -> None:
        from algotrader.common.calendar import HolidayDataError

        with pytest.raises(HolidayDataError, match="2027"):
            self._calendar().is_trading_day(date(2027, 1, 5))

    def test_the_error_says_what_to_do(self) -> None:
        from algotrader.common.calendar import HolidayDataError

        with pytest.raises(HolidayDataError, match=r"nse_holidays\.yaml"):
            self._calendar().is_trading_day(date(2027, 1, 5))

    def test_a_calendar_with_no_declared_coverage_still_answers(self) -> None:
        """Hand-built calendars in tests declare no coverage and must keep
        working — the guard is for the loaded file, not for every instance."""
        from algotrader.common.calendar import MarketCalendar

        assert MarketCalendar(frozenset()).is_trading_day(date(2030, 8, 20))

    def test_coverage_is_inferred_when_the_file_forgets_to_declare_it(self) -> None:
        from algotrader.common.calendar import HolidayCalendarStatus

        status = HolidayCalendarStatus(
            frozenset({date(2026, 1, 26)}), verified=True, source="t", path=None
        )
        assert status.covers(date(2026, 5, 1))
