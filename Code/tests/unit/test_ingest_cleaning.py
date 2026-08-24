"""Tick cleaning (E05-S02..S05).

One bad print corrupts an EMA permanently — the indicator carries the poisoned
value for as many periods as its window, and nothing downstream notices. So the
acceptance criterion for E05-S04 is absolute: a single injected bad print must
not change any indicator value. These tests are that criterion, one layer up.

The case worth reading is the cold start. The outlier bound is
``max(5 x ATR%, 2%)``, and ATR does not exist at 09:15 — exactly when prints
are most erratic. If the missing term resolved to "no limit" the filter would be
inert precisely when it matters.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from algotrader.ingest import cleaning
from algotrader.ingest.kite_protocol import DepthLevel, Mode, RawTick

TOKEN = 408065
NOW = dt.datetime(2026, 8, 20, 4, 0, 0, tzinfo=dt.UTC)


def _raw(
    price: str = "2500.00",
    *,
    volume: int | None = 1000,
    ts: dt.datetime | None = NOW,
    token: int = TOKEN,
    last_quantity: int | None = 5,
) -> RawTick:
    return RawTick(
        instrument_token=token,
        mode=Mode.QUOTE,
        last_price=Decimal(price),
        volume=volume,
        exchange_timestamp=ts,
        last_quantity=last_quantity,
    )


class TestValidation:
    def test_a_good_tick_passes(self) -> None:
        assert cleaning.TickValidator().check(_raw(), now=NOW) is None

    def test_a_zero_price_is_refused(self) -> None:
        assert cleaning.TickValidator().check(_raw("0"), now=NOW) == cleaning.NULL_PRICE

    def test_a_negative_price_is_refused(self) -> None:
        assert cleaning.TickValidator().check(_raw("-10"), now=NOW) == cleaning.NEGATIVE_PRICE

    def test_a_negative_volume_is_refused(self) -> None:
        v = cleaning.TickValidator()
        assert v.check(_raw(volume=-5), now=NOW) == cleaning.NEGATIVE_VOLUME

    def test_volume_going_backwards_is_refused(self) -> None:
        """Session volume is cumulative and cannot fall. A decrease means a
        replayed or out-of-order packet, and accepting it makes every
        volume-delta negative."""
        v = cleaning.TickValidator()
        assert v.check(_raw(volume=1000), now=NOW) is None
        assert v.check(_raw(volume=900), now=NOW) == cleaning.VOLUME_WENT_BACKWARDS

    def test_volume_holding_steady_is_fine(self) -> None:
        """Equal volume just means no trade since the last tick."""
        v = cleaning.TickValidator()
        assert v.check(_raw(volume=1000), now=NOW) is None
        assert v.check(_raw(volume=1000), now=NOW) is None

    def test_a_timestamp_far_from_our_clock_is_refused(self) -> None:
        """Catches a stuck or replayed feed presenting itself as live."""
        stale = NOW - dt.timedelta(seconds=60)
        assert cleaning.TickValidator().check(_raw(ts=stale), now=NOW) == cleaning.CLOCK_SKEW

    def test_ordinary_drift_is_tolerated(self) -> None:
        near = NOW - dt.timedelta(seconds=2)
        assert cleaning.TickValidator().check(_raw(ts=near), now=NOW) is None

    def test_a_missing_timestamp_is_not_a_skew_failure(self) -> None:
        """An index in quote mode genuinely sends none."""
        assert cleaning.TickValidator().check(_raw(ts=None), now=NOW) is None

    def test_rejections_are_counted_and_sampled(self) -> None:
        v = cleaning.TickValidator()
        for _ in range(20):
            v.check(_raw("0"), now=NOW)
        assert v.rejections.counts[cleaning.NULL_PRICE] == 20
        assert len(v.rejections.samples[cleaning.NULL_PRICE]) <= v.rejections.sample_limit


class TestDeduplication:
    def test_an_identical_tick_is_a_duplicate(self) -> None:
        d = cleaning.Deduplicator()
        tick = _raw()
        assert not d.is_duplicate(tick)
        assert d.is_duplicate(tick)

    def test_a_different_price_is_not_a_duplicate(self) -> None:
        d = cleaning.Deduplicator()
        d.is_duplicate(_raw("2500.00"))
        assert not d.is_duplicate(_raw("2500.05"))

    def test_the_window_is_bounded(self) -> None:
        """An unbounded set grows for the whole session; 200 symbols over six
        hours is a lot of tuples, and the memory is needed elsewhere."""
        d = cleaning.Deduplicator(capacity_per_instrument=4)
        for i in range(10):
            d.is_duplicate(_raw(volume=1000 + i))
        assert len(d._seen[TOKEN]) <= 4

    def test_one_busy_symbol_cannot_evict_another(self) -> None:
        """Per-instrument windows: a high-frequency name must not push a quiet
        one out of its own dedup history."""
        d = cleaning.Deduplicator(capacity_per_instrument=3)
        quiet = _raw(token=999, volume=1)
        d.is_duplicate(quiet)
        for i in range(20):
            d.is_duplicate(_raw(token=TOKEN, volume=1000 + i))
        assert d.is_duplicate(quiet), "the quiet symbol's history was evicted"


class TestOutlierFilterColdStart:
    """The bound is max(5 x ATR%, 2%). ATR does not exist at 09:15."""

    def test_before_atr_is_known_the_filter_reports_cold_start(self) -> None:
        assert cleaning.OutlierFilter().cold_start(TOKEN) is True

    def test_the_cold_start_bound_is_the_two_percent_floor_not_unlimited(self) -> None:
        """The failure this guards: a missing ATR resolving to no limit, leaving
        the filter inert exactly when prints are most erratic."""
        f = cleaning.OutlierFilter()
        assert f.threshold_pct(TOKEN) == cleaning.COLD_START_MOVE_PCT

    def test_a_wild_move_is_rejected_during_cold_start(self) -> None:
        f = cleaning.OutlierFilter()
        assert f.check(_raw("2500")) is None
        assert f.check(_raw("3000")) == cleaning.OUTLIER  # +20%

    def test_a_normal_move_passes_during_cold_start(self) -> None:
        f = cleaning.OutlierFilter()
        assert f.check(_raw("2500")) is None
        assert f.check(_raw("2520")) is None  # +0.8%

    def test_once_atr_arrives_the_bound_widens_for_a_volatile_name(self) -> None:
        f = cleaning.OutlierFilter()
        f.set_atr_pct(TOKEN, Decimal("1.5"))
        assert not f.cold_start(TOKEN)
        assert f.threshold_pct(TOKEN) == Decimal("7.5")

    def test_the_floor_still_applies_to_a_very_quiet_name(self) -> None:
        """5 x 0.1% is 0.5%, which would reject ordinary noise. The 2% floor is
        a floor, not just a cold-start default."""
        f = cleaning.OutlierFilter()
        f.set_atr_pct(TOKEN, Decimal("0.1"))
        assert f.threshold_pct(TOKEN) == cleaning.COLD_START_MOVE_PCT

    def test_a_nonsensical_atr_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            cleaning.OutlierFilter().set_atr_pct(TOKEN, Decimal(0))


class TestOutlierFilterCircuitBand:
    def test_a_price_outside_the_band_is_impossible_not_merely_unlikely(self) -> None:
        """The exchange would not have printed it, so it is a data error by
        definition rather than a judgement call."""
        f = cleaning.OutlierFilter()
        f.set_circuit(TOKEN, Decimal("2400"), Decimal("2600"))
        assert f.check(_raw("2700")) == cleaning.CROSSED_CIRCUIT

    def test_a_price_inside_the_band_is_allowed(self) -> None:
        f = cleaning.OutlierFilter()
        f.set_circuit(TOKEN, Decimal("2400"), Decimal("2600"))
        assert f.check(_raw("2550")) is None

    def test_the_band_check_applies_to_the_very_first_tick(self) -> None:
        """There is no previous price to compare against, so the band is the
        only check that can run — and it must."""
        f = cleaning.OutlierFilter()
        f.set_circuit(TOKEN, Decimal("2400"), Decimal("2600"))
        assert f.check(_raw("9999")) == cleaning.CROSSED_CIRCUIT

    def test_an_inverted_band_is_refused_at_configuration_time(self) -> None:
        with pytest.raises(ValueError, match="inverted"):
            cleaning.OutlierFilter().set_circuit(TOKEN, Decimal("2600"), Decimal("2400"))

    def test_a_rejected_outlier_does_not_move_the_baseline(self) -> None:
        """If a rejected price updated the reference, the NEXT tick would be
        measured from a price that never happened — and a second bad print
        would then look reasonable."""
        f = cleaning.OutlierFilter()
        assert f.check(_raw("2500")) is None
        assert f.check(_raw("3000")) == cleaning.OUTLIER
        assert f._last_price[TOKEN] == Decimal("2500")
        assert f.check(_raw("2510")) is None


class TestNormalisation:
    def test_a_raw_tick_becomes_a_domain_tick(self) -> None:
        tick = cleaning.normalise(_raw(), "INFY", received_ts=NOW)
        assert tick.symbol == "INFY"
        assert tick.ltp == Decimal("2500.00")
        assert tick.exchange_ts.tzinfo is dt.UTC

    def test_depth_becomes_bid_and_ask(self) -> None:
        raw = RawTick(
            instrument_token=TOKEN,
            mode=Mode.FULL,
            last_price=Decimal("2500"),
            volume=10,
            exchange_timestamp=NOW,
            bids=(DepthLevel(quantity=50, price=Decimal("2499.95"), orders=3),),
            asks=(DepthLevel(quantity=60, price=Decimal("2500.05"), orders=4),),
        )
        tick = cleaning.normalise(raw, "INFY", received_ts=NOW)
        assert tick.bid == Decimal("2499.95")
        assert tick.ask == Decimal("2500.05")
        assert tick.bid_qty == 50 and tick.ask_qty == 60

    def test_a_missing_exchange_timestamp_falls_back_to_arrival(self) -> None:
        tick = cleaning.normalise(_raw(ts=None), "INFY", received_ts=NOW)
        assert tick.exchange_ts == NOW


class TestThePipelineAsAWhole:
    def test_a_clean_tick_comes_out_the_other_side(self) -> None:
        p = cleaning.CleaningPipeline()
        assert p.process(_raw(), "INFY", now=NOW) is not None
        assert p.accepted == 1

    def test_a_bad_print_never_becomes_a_domain_tick(self) -> None:
        """E05-S04's acceptance criterion, one layer up: the bad print does not
        reach anything that could compute an indicator from it."""
        p = cleaning.CleaningPipeline()
        p.process(_raw("2500"), "INFY", now=NOW)
        assert p.process(_raw("5000"), "INFY", now=NOW) is None

    def test_deduplication_runs_before_the_outlier_baseline_moves(self) -> None:
        """Order matters. If a duplicate updated the last-price reference, a
        genuine outlier immediately afterwards would be measured against a
        stale baseline and could slip through."""
        p = cleaning.CleaningPipeline()
        tick = _raw("2500")
        assert p.process(tick, "INFY", now=NOW) is not None
        assert p.process(tick, "INFY", now=NOW) is None  # duplicate
        assert p.rejection_summary.get(cleaning.DUPLICATE) == 1

    def test_the_summary_merges_every_stage(self) -> None:
        p = cleaning.CleaningPipeline()
        p.process(_raw("0"), "INFY", now=NOW)
        p.process(_raw(volume=-1), "INFY", now=NOW)
        assert set(p.rejection_summary) >= {cleaning.NULL_PRICE, cleaning.NEGATIVE_VOLUME}

    def test_a_mostly_rejected_feed_is_reported_unhealthy(self) -> None:
        """A feed rejecting a quarter of what it sends is not a feed with some
        bad prints — it is one this system has misunderstood."""
        p = cleaning.CleaningPipeline()
        for _ in range(200):
            p.process(_raw("0"), "INFY", now=NOW)
        assert p.looks_unhealthy()

    def test_a_healthy_feed_is_not_flagged(self) -> None:
        p = cleaning.CleaningPipeline()
        for i in range(200):
            p.process(_raw(str(2500 + i * 0.05), volume=1000 + i), "INFY", now=NOW)
        assert not p.looks_unhealthy()
