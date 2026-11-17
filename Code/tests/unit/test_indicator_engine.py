"""Indicator sets, warm-up and the multi-timeframe snapshot (E06-S03, E06-S04).

Two acceptance criteria live here, and both are about refusing to answer:

- E06-S03: a symbol with insufficient history is EXCLUDED, not traded on bad
  values. A 200-EMA built from forty bars is present, plausible and wrong.
- E06-S04: ``all_ready`` gates evaluation.

The third property is not in the story and matters as much: restoring state
must equal re-warming from history. A mid-session restart that produced even
slightly different values would change signals with nothing visible to show for
it — and that equivalence is exactly what quietly stops holding when a new
indicator is added and its ``snapshot`` forgets a field.
"""

from __future__ import annotations

import datetime as dt
import json
import random
from decimal import Decimal

import pytest

from algotrader.common.enums import Timeframe
from algotrader.common.models.market import Bar
from algotrader.indicators.engine import (
    IndicatorEngine,
    IndicatorSet,
    warm_up_symbols,
)

BASE = dt.datetime(2026, 8, 20, 3, 45, tzinfo=dt.UTC)


def _bars(n: int, symbol: str = "INFY", timeframe: Timeframe = Timeframe.M5) -> list[Bar]:
    rng = random.Random(11)
    out: list[Bar] = []
    price = 1000.0
    for i in range(n):
        price = max(10.0, price * (1 + rng.uniform(-0.01, 0.01)))
        out.append(
            Bar(
                symbol=symbol,
                timeframe=timeframe,
                open_ts=BASE + dt.timedelta(minutes=5 * i),
                open=Decimal(f"{price:.4f}"),
                high=Decimal(f"{price * 1.004:.4f}"),
                low=Decimal(f"{price * 0.996:.4f}"),
                close=Decimal(f"{price:.4f}"),
                volume=rng.randint(1_000, 20_000),
            )
        )
    return out


class TestReadinessGatesEvaluation:
    def test_a_fresh_set_is_not_ready(self) -> None:
        assert not IndicatorSet(symbol="INFY", timeframe=Timeframe.M5).is_ready

    def test_forty_bars_is_not_enough_for_a_two_hundred_ema(self) -> None:
        """The bug this prevents: a number that is present, plausible and wrong."""
        s = IndicatorSet(symbol="INFY", timeframe=Timeframe.M5)
        s.warm_up(_bars(40))
        assert not s.is_ready
        assert "ema_200" in s.not_ready()

    def test_enough_history_makes_it_ready(self) -> None:
        s = IndicatorSet(symbol="INFY", timeframe=Timeframe.M5)
        s.warm_up(_bars(260))
        assert s.is_ready, f"still warming: {s.not_ready()}"

    def test_not_ready_names_what_is_missing(self) -> None:
        """A health panel needs to say WHICH indicator, not just 'not ready'."""
        s = IndicatorSet(symbol="INFY", timeframe=Timeframe.M5)
        s.warm_up(_bars(60))
        pending = s.not_ready()
        assert "ema_200" in pending
        assert "rsi_14" not in pending


class TestStalenessSurvivesTheNextBar:
    """One bar after a fifteen-minute hole does not make a 200-EMA correct."""

    def test_a_gap_marks_the_set_stale(self) -> None:
        s = IndicatorSet(symbol="INFY", timeframe=Timeframe.M5)
        s.warm_up(_bars(260))
        assert s.is_ready
        s.mark_stale("feed gap 12m")
        assert not s.is_ready

    def test_a_further_bar_does_not_clear_staleness(self) -> None:
        s = IndicatorSet(symbol="INFY", timeframe=Timeframe.M5)
        s.warm_up(_bars(260))
        s.mark_stale("feed gap")
        s.update(_bars(261)[-1])
        assert not s.is_ready, "a single bar must not undo a gap"

    def test_only_an_explicit_rewarm_clears_it(self) -> None:
        s = IndicatorSet(symbol="INFY", timeframe=Timeframe.M5)
        s.warm_up(_bars(260))
        s.mark_stale("feed gap")
        s.clear_stale()
        assert s.is_ready

    def test_a_gap_invalidates_every_timeframe_for_that_symbol(self) -> None:
        """A 15-minute hole is a missing 5m bar AND a corrupted hourly bar. The
        hourly one is the harder to notice."""
        engine = IndicatorEngine()
        for timeframe in engine.timeframes:
            engine.set_for("INFY", timeframe).warm_up(_bars(260, timeframe=timeframe))
        engine.mark_stale("INFY", "feed gap")
        assert all(engine.set_for("INFY", tf).stale for tf in engine.timeframes)

    def test_another_symbol_is_untouched(self) -> None:
        engine = IndicatorEngine()
        engine.set_for("INFY", Timeframe.M5).warm_up(_bars(260))
        engine.set_for("TCS", Timeframe.M5).warm_up(_bars(260, symbol="TCS"))
        engine.mark_stale("INFY", "feed gap")
        assert not engine.set_for("TCS", Timeframe.M5).stale


class TestRestoreEqualsRewarm:
    """The property that makes a mid-session restart safe."""

    def test_a_restored_set_matches_a_rewarmed_one_exactly(self) -> None:
        bars = _bars(300)
        live = IndicatorSet(symbol="INFY", timeframe=Timeframe.M5)
        live.warm_up(bars)

        restored = IndicatorSet(symbol="INFY", timeframe=Timeframe.M5)
        restored.restore(json.loads(json.dumps(live.snapshot())))

        for name, value in live.values().items():
            other = restored.values()[name]
            if value is None:
                assert other is None, f"{name}: live None, restored {other}"
            else:
                assert other is not None and abs(value - other) < 1e-9, name

    def test_they_stay_equal_for_the_rest_of_the_session(self) -> None:
        """A snapshot capturing the value but not the window would pass a
        same-instant check and diverge on the very next bar."""
        bars = _bars(320)
        live = IndicatorSet(symbol="INFY", timeframe=Timeframe.M5)
        live.warm_up(bars[:300])
        restored = IndicatorSet(symbol="INFY", timeframe=Timeframe.M5)
        restored.restore(live.snapshot())

        for bar in bars[300:]:
            live.update(bar)
            restored.update(bar)
        for name, value in live.values().items():
            other = restored.values()[name]
            if value is not None:
                assert other is not None and abs(value - other) < 1e-9, name

    def test_a_snapshot_from_a_different_build_is_refused(self) -> None:
        """Silently ignoring a mismatch leaves a set that looks restored and is
        partly empty."""
        live = IndicatorSet(symbol="INFY", timeframe=Timeframe.M5)
        live.warm_up(_bars(260))
        state = live.snapshot()
        del state["indicators"]["rsi_14"]

        target = IndicatorSet(symbol="INFY", timeframe=Timeframe.M5)
        with pytest.raises(ValueError, match="does not match this build"):
            target.restore(state)

    def test_the_whole_snapshot_is_json_serialisable(self) -> None:
        live = IndicatorSet(symbol="INFY", timeframe=Timeframe.M5)
        live.warm_up(_bars(260))
        assert json.loads(json.dumps(live.snapshot()))["bars_seen"] == 260


class TestTheMultiTimeframeSnapshot:
    def _warm_engine(self, symbol: str = "INFY", bars: int = 260) -> IndicatorEngine:
        engine = IndicatorEngine()
        for timeframe in engine.timeframes:
            engine.set_for(symbol, timeframe).warm_up(
                _bars(bars, symbol=symbol, timeframe=timeframe)
            )
        return engine

    def test_all_ready_is_true_only_when_every_timeframe_is(self) -> None:
        engine = self._warm_engine()
        assert engine.snapshot("INFY").all_ready

    def test_one_cold_timeframe_blocks_the_whole_symbol(self) -> None:
        engine = IndicatorEngine()
        engine.set_for("INFY", Timeframe.M5).warm_up(_bars(260))
        engine.set_for("INFY", Timeframe.M15).warm_up(_bars(30, timeframe=Timeframe.M15))
        snapshot = engine.snapshot("INFY")
        assert not snapshot.all_ready
        assert Timeframe.M15 in snapshot.not_ready

    def test_an_unknown_symbol_is_not_ready_rather_than_an_error(self) -> None:
        """The signal engine asks about symbols that may have no bars yet."""
        snapshot = IndicatorEngine().snapshot("NOSUCH")
        assert not snapshot.all_ready

    def test_values_are_exposed_even_when_not_ready(self) -> None:
        """A health panel needs to show what is missing, not just be refused."""
        engine = IndicatorEngine()
        engine.set_for("INFY", Timeframe.M5).warm_up(_bars(60))
        snapshot = engine.snapshot("INFY")
        assert not snapshot.all_ready
        assert snapshot.value(Timeframe.M5, "rsi_14") is not None

    def test_trend_agreement_carries_direction_in_its_sign(self) -> None:
        engine = self._warm_engine()
        score = engine.snapshot("INFY").trend_agreement()
        assert -len(engine.timeframes) <= score <= len(engine.timeframes)

    def test_a_timeframe_with_no_opinion_contributes_nothing(self) -> None:
        """'No opinion' and 'flat' are different, and averaging them together
        would let a cold timeframe look like disagreement."""
        engine = IndicatorEngine()
        engine.set_for("INFY", Timeframe.M5).warm_up(_bars(260))
        score = engine.snapshot("INFY").trend_agreement()
        assert abs(score) == 1, "only the warm timeframe should have voted"

    def test_ready_symbols_lists_only_the_tradable_ones(self) -> None:
        engine = self._warm_engine("INFY")
        engine.set_for("TCS", Timeframe.M5).warm_up(_bars(20, symbol="TCS"))
        assert engine.ready_symbols() == ["INFY"]


class TestWarmUpExcludesRatherThanDegrades:
    """E06-S03's acceptance criterion."""

    def test_a_symbol_with_enough_history_is_warmed(self) -> None:
        engine = IndicatorEngine()
        history = {("INFY", tf): _bars(260, timeframe=tf) for tf in engine.timeframes}
        report = warm_up_symbols(engine, history)
        assert report.warmed == ["INFY"]
        assert report.ok

    def test_a_symbol_with_too_little_history_is_excluded_not_traded(self) -> None:
        engine = IndicatorEngine()
        history = {("THIN", tf): _bars(30, symbol="THIN", timeframe=tf) for tf in engine.timeframes}
        report = warm_up_symbols(engine, history)
        assert report.warmed == []
        assert "THIN" in report.insufficient
        assert not report.ok

    def test_the_report_names_what_was_missing(self) -> None:
        """The operator needs to know WHY a symbol dropped out of the universe."""
        engine = IndicatorEngine()
        history = {("THIN", tf): _bars(30, symbol="THIN", timeframe=tf) for tf in engine.timeframes}
        report = warm_up_symbols(engine, history)
        assert "ema_200" in report.insufficient["THIN"]

    def test_one_thin_symbol_does_not_block_a_good_one(self) -> None:
        engine = IndicatorEngine()
        history: dict = {}
        for tf in engine.timeframes:
            history[("INFY", tf)] = _bars(260, timeframe=tf)
            history[("THIN", tf)] = _bars(20, symbol="THIN", timeframe=tf)
        report = warm_up_symbols(engine, history)
        assert report.warmed == ["INFY"]
        assert list(report.insufficient) == ["THIN"]

    def test_warm_up_uses_the_same_path_as_live_updates(self) -> None:
        """A separate batch warm-up would let the two disagree, and the
        disagreement would appear as a discontinuity at the moment the system
        switched between them — mid-session."""
        bars = _bars(260)
        warmed = IndicatorSet(symbol="INFY", timeframe=Timeframe.M5)
        warmed.warm_up(bars)

        streamed = IndicatorSet(symbol="INFY", timeframe=Timeframe.M5)
        for bar in bars:
            streamed.update(bar)

        for name, value in warmed.values().items():
            other = streamed.values()[name]
            if value is not None:
                assert other is not None and abs(value - other) < 1e-12, name


# ---------------------------------------------------------------------------
# Audit 16 Nov 2026 - TRD-003 (VWAP per session), ENG-022 (one bar close)
# ---------------------------------------------------------------------------


def _session_bars(day: dt.date, n: int, seed: int) -> list[Bar]:
    """``n`` 5m bars from 09:15 IST on ``day`` - real session bars, no night."""
    from algotrader.common.calendar import IST

    start = dt.datetime.combine(day, dt.time(9, 15), tzinfo=IST).astimezone(dt.UTC)
    rng = random.Random(seed)
    out: list[Bar] = []
    price = 1000.0 + seed
    for i in range(n):
        price = max(10.0, price * (1 + rng.uniform(-0.01, 0.01)))
        out.append(
            Bar(
                symbol="INFY",
                timeframe=Timeframe.M5,
                open_ts=start + dt.timedelta(minutes=5 * i),
                open=Decimal(f"{price:.4f}"),
                high=Decimal(f"{price * 1.004:.4f}"),
                low=Decimal(f"{price * 0.996:.4f}"),
                close=Decimal(f"{price:.4f}"),
                volume=rng.randint(1_000, 20_000),
            )
        )
    return out


class TestVwapIsTheSessionsInsideTheSet:
    """Audit TRD-003's acceptance: two sessions folded into one IndicatorSet;
    right after session 2's first bar, vwap is that bar's typical price."""

    def test_the_new_session_starts_from_its_first_bar(self) -> None:
        day_one = _session_bars(dt.date(2026, 8, 20), 75, seed=1)
        day_two = _session_bars(dt.date(2026, 8, 21), 45, seed=2)
        s = IndicatorSet(symbol="INFY", timeframe=Timeframe.M5)
        s.warm_up(day_one)
        s.update(day_two[0])
        first = day_two[0]
        typical = (float(first.high) + float(first.low) + float(first.close)) / 3.0
        assert s.values()["vwap"] == pytest.approx(typical, abs=1e-9)

    def test_restore_then_continue_gives_the_same_session_vwap(self) -> None:
        day_one = _session_bars(dt.date(2026, 8, 20), 75, seed=1)
        day_two = _session_bars(dt.date(2026, 8, 21), 45, seed=2)
        live = IndicatorSet(symbol="INFY", timeframe=Timeframe.M5)
        live.warm_up(day_one)
        restored = IndicatorSet(symbol="INFY", timeframe=Timeframe.M5)
        restored.restore(json.loads(json.dumps(live.snapshot())))
        for bar in day_two:
            live.update(bar)
            restored.update(bar)
        reference = IndicatorSet(symbol="INFY", timeframe=Timeframe.M5)
        reference.warm_up(day_two)
        assert restored.values()["vwap"] == live.values()["vwap"]
        assert live.values()["vwap"] == pytest.approx(reference.values()["vwap"], abs=1e-9)


class TestThePublishedTimeIsTheBarsClose:
    """Audit ENG-022: the 15:15 hourly bar was published as of 16:15, and the
    execution reader refused its ATR as 'from the future' for 45 minutes."""

    def test_the_last_hourly_bar_is_published_as_of_the_market_close(self) -> None:
        from algotrader.common.calendar import IST

        opened = dt.datetime(2026, 8, 20, 15, 15, tzinfo=IST).astimezone(dt.UTC)
        bar = Bar(
            symbol="INFY",
            timeframe=Timeframe.H1,
            open_ts=opened,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=1_000,
        )
        s = IndicatorSet(symbol="INFY", timeframe=Timeframe.H1)
        s.update(bar)
        published = s.published()
        assert published is not None
        expected = dt.datetime(2026, 8, 20, 15, 30, tzinfo=IST)
        assert published.as_of == bar.close_ts == expected
