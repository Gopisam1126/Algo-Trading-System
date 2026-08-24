"""Pivots, support/resistance and the opening range (E06-S05, E06-S06).

Levels are where stops go, so an error here is a stop in the wrong place on a
real position rather than merely a worse signal.

E06-S06's acceptance criterion is that the opening range is sealed exactly at
09:30 and never mutates afterwards. By 09:31 that range has already sized a
position and placed its stop, so a late tick reopening it would move levels
underneath a trade that is already on.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from algotrader.common.calendar import IST
from algotrader.common.enums import Timeframe
from algotrader.common.models.market import Bar, Tick
from algotrader.indicators.levels import (
    LevelError,
    LevelSet,
    OpeningRange,
    classic_pivots,
    cluster_levels,
    find_swings,
)

DAY = dt.date(2026, 8, 20)


def _at(hour: int, minute: int, second: int = 0) -> dt.datetime:
    return dt.datetime.combine(DAY, dt.time(hour, minute, second), tzinfo=IST).astimezone(dt.UTC)


def _tick(moment: dt.datetime, price: str) -> Tick:
    return Tick(
        symbol="INFY",
        exchange_ts=moment,
        received_ts=moment,
        ltp=Decimal(price),
        volume=1000,
    )


def _bar(i: int, high: str, low: str, close: str | None = None) -> Bar:
    return Bar(
        symbol="INFY",
        timeframe=Timeframe.M5,
        open_ts=_at(9, 15) + dt.timedelta(minutes=5 * i),
        open=Decimal(low),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close or high),
        volume=1000,
    )


class TestTheOpeningRangeWindow:
    def test_ticks_inside_the_window_build_the_range(self) -> None:
        r = OpeningRange(symbol="INFY", trade_date=DAY)
        for minute, price in ((16, "100"), (20, "105"), (25, "98")):
            assert r.update(_tick(_at(9, minute), price))
        assert r.high == Decimal("105")
        assert r.low == Decimal("98")

    def test_a_tick_before_the_open_is_ignored(self) -> None:
        """Pre-open auction prints are not continuous trading."""
        r = OpeningRange(symbol="INFY", trade_date=DAY)
        assert not r.update(_tick(_at(9, 5), "50"))
        assert r.high is None

    def test_a_tick_at_exactly_nine_thirty_is_outside(self) -> None:
        """The window is half-open: 09:15 inclusive, 09:30 exclusive. A tick at
        the boundary belongs to the next bar, not to the range."""
        r = OpeningRange(symbol="INFY", trade_date=DAY)
        r.update(_tick(_at(9, 16), "100"))
        assert not r.update(_tick(_at(9, 30), "999"))
        assert r.high == Decimal("100")

    def test_a_tick_from_another_day_is_ignored(self) -> None:
        r = OpeningRange(symbol="INFY", trade_date=DAY)
        yesterday = dt.datetime.combine(
            DAY - dt.timedelta(days=1), dt.time(9, 20), tzinfo=IST
        ).astimezone(dt.UTC)
        assert not r.update(_tick(yesterday, "999"))
        assert r.high is None


class TestTheSealedRangeNeverMoves:
    """The criterion with teeth."""

    def _sealed(self) -> OpeningRange:
        r = OpeningRange(symbol="INFY", trade_date=DAY)
        r.update(_tick(_at(9, 16), "100"))
        r.update(_tick(_at(9, 25), "104"))
        r.seal()
        return r

    def test_a_late_tick_does_not_change_the_high(self) -> None:
        r = self._sealed()
        assert not r.update(_tick(_at(9, 20), "999"))
        assert r.high == Decimal("104")

    def test_late_ticks_are_counted_rather_than_silently_dropped(self) -> None:
        """A silent no-op would hide a feed delivering out of order — which
        means every bar boundary that morning is approximate."""
        r = self._sealed()
        for _ in range(3):
            r.update(_tick(_at(9, 20), "999"))
        assert r.ticks_after_seal == 3

    def test_a_late_tick_outside_the_window_is_not_counted(self) -> None:
        """Most of the day is outside the window; that is not an anomaly."""
        r = self._sealed()
        r.update(_tick(_at(11, 0), "120"))
        assert r.ticks_after_seal == 0

    def test_sealing_twice_is_harmless(self) -> None:
        r = self._sealed()
        r.seal()
        assert r.high == Decimal("104")

    def test_an_empty_range_is_sealed_but_not_usable(self) -> None:
        """A symbol that did not trade in the first fifteen minutes has no
        range. That is different from a narrow one, and must not read as one."""
        r = OpeningRange(symbol="INFY", trade_date=DAY)
        r.seal()
        assert r.sealed
        assert not r.is_usable
        assert r.range_pct is None


class TestBreakoutRefusesToAnswerEarly:
    def test_no_direction_before_sealing(self) -> None:
        """Answering early trades a breakout of a range that is still forming —
        which is not a breakout, it is just the current high."""
        r = OpeningRange(symbol="INFY", trade_date=DAY)
        r.update(_tick(_at(9, 16), "100"))
        assert r.breakout_direction(Decimal("110")) is None

    def test_up_and_down_after_sealing(self) -> None:
        r = OpeningRange(symbol="INFY", trade_date=DAY)
        r.update(_tick(_at(9, 16), "100"))
        r.update(_tick(_at(9, 25), "104"))
        r.seal()
        assert r.breakout_direction(Decimal("105")) == "up"
        assert r.breakout_direction(Decimal("99")) == "down"
        assert r.breakout_direction(Decimal("102")) is None

    def test_range_percent_uses_the_midpoint(self) -> None:
        """The open sits at one edge on a gap-and-reverse morning, which would
        make the same range look wider or narrower depending on direction."""
        r = OpeningRange(symbol="INFY", trade_date=DAY)
        r.update(_tick(_at(9, 16), "99"))
        r.update(_tick(_at(9, 25), "101"))
        r.seal()
        assert r.range_pct == Decimal(2)  # 2 wide over a midpoint of 100


class TestClassicPivots:
    def test_the_pivot_is_the_average_of_high_low_close(self) -> None:
        p = classic_pivots(Decimal(110), Decimal(90), Decimal(100))
        assert p.pivot == Decimal(100)

    def test_resistances_sit_above_and_supports_below(self) -> None:
        p = classic_pivots(Decimal(110), Decimal(90), Decimal(100))
        assert p.s3 < p.s2 < p.s1 < p.pivot < p.r1 < p.r2 < p.r3

    def test_an_inverted_bar_is_refused(self) -> None:
        with pytest.raises(LevelError, match="below low"):
            classic_pivots(Decimal(90), Decimal(110), Decimal(100))

    def test_nearest_names_the_level(self) -> None:
        p = classic_pivots(Decimal(110), Decimal(90), Decimal(100))
        name, level = p.nearest(p.r1 + Decimal("0.5"))
        assert name == "r1" and level == p.r1


class TestSwingDetection:
    def test_a_peak_is_found(self) -> None:
        bars = [
            _bar(0, "100", "99"),
            _bar(1, "101", "100"),
            _bar(2, "110", "105"),  # the peak
            _bar(3, "102", "101"),
            _bar(4, "100", "99"),
        ]
        highs, _ = find_swings(bars, lookback=2)
        assert Decimal("110") in highs

    def test_a_trough_is_found(self) -> None:
        bars = [
            _bar(0, "110", "105"),
            _bar(1, "108", "104"),
            _bar(2, "102", "90"),  # the trough
            _bar(3, "107", "103"),
            _bar(4, "109", "105"),
        ]
        _, lows = find_swings(bars, lookback=2)
        assert Decimal("90") in lows

    def test_a_flat_series_has_no_swings(self) -> None:
        """Every bar identical means no bar stands out — reporting swings there
        would manufacture levels from nothing."""
        bars = [_bar(i, "100", "99") for i in range(9)]
        highs, lows = find_swings(bars, lookback=2)
        assert highs == [] and lows == []

    def test_a_series_shorter_than_the_window_yields_nothing(self) -> None:
        assert find_swings([_bar(0, "100", "99")], lookback=2) == ([], [])

    def test_a_nonsensical_lookback_is_refused(self) -> None:
        with pytest.raises(LevelError):
            find_swings([], lookback=0)


class TestClusteringStopsDoubleCounting:
    def test_nearby_levels_merge_into_one(self) -> None:
        """Five swing highs within a rupee are one level price visited five
        times — not five independent levels."""
        levels = cluster_levels(
            [Decimal("100.00"), Decimal("100.05"), Decimal("100.10")], "resistance"
        )
        assert len(levels) == 1
        assert levels[0].touches == 3

    def test_distant_levels_stay_separate(self) -> None:
        levels = cluster_levels([Decimal("100"), Decimal("120")], "resistance")
        assert len(levels) == 2

    def test_touches_become_strength_but_are_capped(self) -> None:
        """A level touched twenty times is not twice as strong as one touched
        ten."""
        many = [Decimal("100") + Decimal("0.001") * i for i in range(20)]
        level = cluster_levels(many, "resistance")[0]
        assert level.touches == 20
        assert level.strength == 5

    def test_a_cluster_cannot_grow_arbitrarily_wide_by_chaining(self) -> None:
        """Merging against the FIRST member, not the previous one.

        Chaining — "within 0.15% of the last price I added" — would let a
        gently-rising staircase of prices merge into one 'level' spanning the
        whole day's range, which is not a level at all. Anchoring bounds the
        cluster to the tolerance, so a long staircase becomes several honest
        levels rather than one dishonest one.
        """
        staircase = [Decimal("100") + Decimal("0.01") * i for i in range(40)]
        clusters = cluster_levels(staircase, "resistance")
        assert len(clusters) > 1, "a 0.4%-wide staircase collapsed into one level"
        widest = max(c.touches for c in clusters)
        assert widest <= 16, "one cluster spans more than the merge tolerance allows"

    def test_an_empty_input_yields_no_levels(self) -> None:
        assert cluster_levels([], "resistance") == []


class TestTheLevelSet:
    def _set(self) -> LevelSet:
        ls = LevelSet(symbol="INFY")
        ls.from_prior_session(Decimal(110), Decimal(90), Decimal(100))
        return ls

    def test_prior_session_values_become_levels(self) -> None:
        kinds = {lv.kind for lv in self._set().all_levels()}
        assert {"prior_high", "prior_low", "prior_close"} <= kinds

    def test_nearest_above_and_below_bracket_the_price(self) -> None:
        ls = self._set()
        above = ls.nearest_above(Decimal(100))
        below = ls.nearest_below(Decimal(100))
        assert above is not None and above.price > Decimal(100)
        assert below is not None and below.price < Decimal(100)

    def test_proximity_feeds_the_tradeability_score(self) -> None:
        """A setup right on top of resistance has worse expectancy than the same
        setup with room to run. This is how that becomes a number."""
        ls = self._set()
        assert ls.proximity_pct(Decimal(100)) == Decimal(0)  # sits on the pivot
        assert ls.proximity_pct(Decimal(105)) is not None

    def test_an_empty_set_has_no_proximity_rather_than_zero(self) -> None:
        """Zero would read as 'right on a level', which is the opposite of
        'we have no levels'."""
        assert LevelSet(symbol="INFY").proximity_pct(Decimal(100)) is None

    def test_swings_from_bars_join_the_set(self) -> None:
        ls = LevelSet(symbol="INFY")
        ls.from_bars(
            [
                _bar(0, "100", "99"),
                _bar(1, "101", "100"),
                _bar(2, "110", "105"),
                _bar(3, "102", "101"),
                _bar(4, "100", "99"),
            ]
        )
        assert any(lv.kind == "resistance" for lv in ls.all_levels())
