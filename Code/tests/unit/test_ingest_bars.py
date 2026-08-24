"""Bar construction (E05-S06, E05-S07).

E05-S06's acceptance criteria are that bar boundaries match the calendar exactly
across a full session, and that consumers can tell a final bar from a forming
one. Both are here.

The rule with the sharpest consequence is that a sealed bar never mutates. The
opening range is sealed at 09:30 and every level derived from it is used to size
and place stops; if a late tick could reopen that bar, those levels would shift
underneath positions already built on them.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from algotrader.common.calendar import IST, MarketCalendar
from algotrader.common.enums import Timeframe
from algotrader.common.models.market import Tick
from algotrader.ingest.bars import BarBuilder, BarError, MultiTimeframeBuilder

SESSION_DAY = dt.date(2026, 8, 20)  # a Thursday


def _at(hour: int, minute: int, second: int = 0) -> dt.datetime:
    """An instant in the session, expressed in IST and stored as UTC."""
    return dt.datetime.combine(SESSION_DAY, dt.time(hour, minute, second), tzinfo=IST).astimezone(
        dt.UTC
    )


def _tick(moment: dt.datetime, price: str, volume: int = 1000) -> Tick:
    return Tick(
        symbol="INFY",
        exchange_ts=moment,
        received_ts=moment,
        ltp=Decimal(price),
        volume=volume,
    )


@pytest.fixture
def calendar() -> MarketCalendar:
    return MarketCalendar(frozenset())


class TestBarsAlignToTheSessionNotTheClock:
    """A 15-minute bar runs 09:15-09:30, never 09:00-09:15."""

    def test_the_first_fifteen_minute_bar_opens_at_the_session_open(
        self, calendar: MarketCalendar
    ) -> None:
        b = BarBuilder(symbol="INFY", timeframe=Timeframe.M15, calendar=calendar)
        assert b.bar_open_for(_at(9, 20)).astimezone(IST).time() == dt.time(9, 15)

    def test_a_tick_at_nine_twenty_nine_is_still_the_first_bar(
        self, calendar: MarketCalendar
    ) -> None:
        b = BarBuilder(symbol="INFY", timeframe=Timeframe.M15, calendar=calendar)
        assert b.bar_open_for(_at(9, 29, 59)).astimezone(IST).time() == dt.time(9, 15)

    def test_a_tick_at_nine_thirty_starts_the_second_bar(self, calendar: MarketCalendar) -> None:
        b = BarBuilder(symbol="INFY", timeframe=Timeframe.M15, calendar=calendar)
        assert b.bar_open_for(_at(9, 30)).astimezone(IST).time() == dt.time(9, 30)

    def test_hourly_bars_also_start_at_the_session_open(self, calendar: MarketCalendar) -> None:
        """Wall-clock hours would put a boundary at 10:00 and split the opening
        hour across two candles."""
        b = BarBuilder(symbol="INFY", timeframe=Timeframe.H1, calendar=calendar)
        assert b.bar_open_for(_at(10, 10)).astimezone(IST).time() == dt.time(9, 15)
        assert b.bar_open_for(_at(10, 20)).astimezone(IST).time() == dt.time(10, 15)

    def test_weekly_cannot_be_built_from_ticks(self, calendar: MarketCalendar) -> None:
        """A week is not a fixed number of seconds once holidays exist."""
        with pytest.raises(BarError, match="aggregated from daily"):
            BarBuilder(symbol="INFY", timeframe=Timeframe.W1, calendar=calendar)


class TestSealingAndOhlc:
    def test_a_bar_seals_when_a_tick_crosses_the_boundary(self, calendar: MarketCalendar) -> None:
        b = BarBuilder(symbol="INFY", timeframe=Timeframe.M5, calendar=calendar)
        assert b.add(_tick(_at(9, 16), "100")) is None
        assert b.add(_tick(_at(9, 18), "101")) is None
        sealed = b.add(_tick(_at(9, 21), "102"))
        assert sealed is not None
        assert sealed.open_ts.astimezone(IST).time() == dt.time(9, 15)

    def test_ohlc_reflects_the_ticks_in_the_interval(self, calendar: MarketCalendar) -> None:
        b = BarBuilder(symbol="INFY", timeframe=Timeframe.M5, calendar=calendar)
        for minute, price in ((16, "100"), (17, "105"), (18, "98"), (19, "103")):
            b.add(_tick(_at(9, minute), price))
        sealed = b.add(_tick(_at(9, 21), "110"))
        assert sealed is not None
        assert (sealed.open, sealed.high, sealed.low, sealed.close) == (
            Decimal("100"),
            Decimal("105"),
            Decimal("98"),
            Decimal("103"),
        )

    def test_bar_volume_is_the_delta_not_the_session_total(self, calendar: MarketCalendar) -> None:
        """The feed reports a running session total. Using it directly would
        make every bar's volume the whole day's."""
        b = BarBuilder(symbol="INFY", timeframe=Timeframe.M5, calendar=calendar)
        b.add(_tick(_at(9, 16), "100", volume=10_000))
        b.add(_tick(_at(9, 18), "101", volume=10_450))
        sealed = b.add(_tick(_at(9, 21), "102", volume=10_500))
        assert sealed is not None
        assert sealed.volume == 450

    def test_a_sealed_bar_is_marked_final(self, calendar: MarketCalendar) -> None:
        b = BarBuilder(symbol="INFY", timeframe=Timeframe.M5, calendar=calendar)
        b.add(_tick(_at(9, 16), "100"))
        sealed = b.add(_tick(_at(9, 21), "102"))
        assert sealed is not None and sealed.is_final

    def test_a_snapshot_is_marked_not_final(self, calendar: MarketCalendar) -> None:
        """Acting on a forming bar is look-ahead bias in live trading: the bar
        can still move against you before it closes."""
        b = BarBuilder(symbol="INFY", timeframe=Timeframe.M5, calendar=calendar)
        b.add(_tick(_at(9, 16), "100"))
        snap = b.snapshot()
        assert snap is not None and snap.is_final is False


class TestASealedBarNeverMutates:
    """The rule the opening range depends on.

    Levels derived from the 09:15-09:30 range are used to size positions and
    place stops. A late tick reopening that bar would shift those levels
    underneath positions already built on them.
    """

    def test_a_late_tick_is_dropped_not_applied(self, calendar: MarketCalendar) -> None:
        b = BarBuilder(symbol="INFY", timeframe=Timeframe.M5, calendar=calendar)
        b.add(_tick(_at(9, 16), "100"))
        sealed = b.add(_tick(_at(9, 21), "102"))
        assert sealed is not None
        high_before = sealed.high

        # A tick whose EXCHANGE timestamp falls inside the sealed bar, arriving
        # afterwards — reordering by the feed, not a clock problem.
        assert b.add(_tick(_at(9, 17), "999")) is None
        assert b.late_ticks == 1
        assert sealed.high == high_before

    def test_late_ticks_are_counted_for_the_health_panel(self, calendar: MarketCalendar) -> None:
        """A rising count means the feed is delivering out of order, and every
        boundary becomes approximate."""
        b = BarBuilder(symbol="INFY", timeframe=Timeframe.M5, calendar=calendar)
        b.add(_tick(_at(9, 16), "100"))
        b.add(_tick(_at(9, 21), "102"))
        for _ in range(3):
            b.add(_tick(_at(9, 17), "99"))
        assert b.late_ticks == 3

    def test_a_tick_in_the_current_bar_still_applies(self, calendar: MarketCalendar) -> None:
        """The control: only ticks for ALREADY-SEALED bars are dropped."""
        b = BarBuilder(symbol="INFY", timeframe=Timeframe.M5, calendar=calendar)
        b.add(_tick(_at(9, 16), "100"))
        b.add(_tick(_at(9, 21), "102"))
        b.add(_tick(_at(9, 22), "108"))
        snap = b.snapshot()
        assert snap is not None and snap.high == Decimal("108")


class TestSyntheticBars:
    def test_a_carry_forward_bar_is_flat_and_flagged(self, calendar: MarketCalendar) -> None:
        """An illiquid symbol printing identical bars looks like a volatility
        collapse to any indicator that cannot tell 'did not move' from 'did not
        trade'."""
        b = BarBuilder(symbol="INFY", timeframe=Timeframe.M5, calendar=calendar)
        b.add(_tick(_at(9, 16), "100"))
        sealed = b.add(_tick(_at(9, 21), "102"))
        assert sealed is not None
        b.remember_close(sealed.close)
        b.force_seal()

        synthetic = b.carry_forward(_at(9, 25))
        assert synthetic is not None
        assert synthetic.synthetic is True
        assert synthetic.open == synthetic.high == synthetic.low == synthetic.close
        assert synthetic.volume == 0

    def test_no_synthetic_bar_without_a_previous_close(self, calendar: MarketCalendar) -> None:
        """Nothing to carry forward means nothing is invented."""
        b = BarBuilder(symbol="INFY", timeframe=Timeframe.M5, calendar=calendar)
        assert b.carry_forward(_at(9, 25)) is None


class TestTimeframesAreBuiltIndependently:
    """Cascading 1m -> 5m -> 15m means one bad 1m bar propagates silently into
    every higher timeframe with nothing to catch it."""

    def test_one_tick_feeds_every_timeframe(self, calendar: MarketCalendar) -> None:
        m = MultiTimeframeBuilder(symbol="INFY", calendar=calendar)
        m.add(_tick(_at(9, 16), "100"))
        snaps = m.snapshots()
        assert set(snaps) == {Timeframe.M1, Timeframe.M5, Timeframe.M15, Timeframe.H1}

    def test_the_minute_bar_seals_first(self, calendar: MarketCalendar) -> None:
        m = MultiTimeframeBuilder(symbol="INFY", calendar=calendar)
        m.add(_tick(_at(9, 16), "100"))
        sealed = m.add(_tick(_at(9, 17, 30), "101"))
        assert [b.timeframe for b in sealed] == [Timeframe.M1]

    def test_higher_timeframes_seal_on_their_own_boundaries(self, calendar: MarketCalendar) -> None:
        m = MultiTimeframeBuilder(symbol="INFY", calendar=calendar)
        m.add(_tick(_at(9, 16), "100"))
        sealed = m.add(_tick(_at(9, 21), "101"))
        assert {b.timeframe for b in sealed} == {Timeframe.M1, Timeframe.M5}

    def test_each_timeframe_sees_the_raw_ticks_not_a_lower_bar(
        self, calendar: MarketCalendar
    ) -> None:
        """Independence means the 15m high is computed from ticks, so a wrong
        1m bar could not corrupt it even if one existed."""
        m = MultiTimeframeBuilder(symbol="INFY", calendar=calendar)
        for minute, price in ((16, "100"), (17, "130"), (18, "95"), (19, "110")):
            m.add(_tick(_at(9, minute), price))
        snaps = m.snapshots()
        assert snaps[Timeframe.M15].high == Decimal("130")
        assert snaps[Timeframe.M15].low == Decimal("95")

    def test_force_seal_closes_everything_at_the_end_of_a_session(
        self, calendar: MarketCalendar
    ) -> None:
        m = MultiTimeframeBuilder(symbol="INFY", calendar=calendar)
        m.add(_tick(_at(15, 25), "100"))
        assert len(m.force_seal_all()) == 4


class TestTheClosingAuctionIsNotContinuousTrading:
    """For CAS-scope stocks, 15:15-15:30 is a call auction. Prints there are not
    comparable to intraday prints."""

    def test_a_cas_stock_marks_bars_covering_the_auction(self, calendar: MarketCalendar) -> None:
        b = BarBuilder(symbol="INFY", timeframe=Timeframe.M5, calendar=calendar)
        b.add(_tick(_at(15, 20), "100"), is_cas_stock=True)
        assert b._forming is not None
        assert b._forming.covers_call_auction is True

    def test_a_non_cas_stock_at_the_same_time_does_not(self, calendar: MarketCalendar) -> None:
        """Non-CAS stocks trade continuously until 15:30, so the same clock time
        means something different for them."""
        b = BarBuilder(symbol="INFY", timeframe=Timeframe.M5, calendar=calendar)
        b.add(_tick(_at(15, 20), "100"), is_cas_stock=False)
        assert b._forming is not None
        assert b._forming.covers_call_auction is False

    def test_earlier_bars_are_unaffected(self, calendar: MarketCalendar) -> None:
        b = BarBuilder(symbol="INFY", timeframe=Timeframe.M5, calendar=calendar)
        b.add(_tick(_at(11, 0), "100"), is_cas_stock=True)
        assert b._forming is not None
        assert b._forming.covers_call_auction is False
