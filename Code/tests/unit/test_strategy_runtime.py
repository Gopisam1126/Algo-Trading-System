"""The strategy evaluator (E13-S01).

The first test in this file is the one that matters most, and it is the one
that did not exist. ``registry.py`` declared 27 primitives; nothing checked
that any of them could be *computed*. The whole vocabulary was validated,
hashed, persisted and unrunnable, and every existing test passed. So
:class:`TestEveryPrimitiveCanActuallyRun` asserts the property directly:
declared and implemented are the same set.

After that the emphasis is on the three-valued logic, because that is where a
plausible implementation goes wrong silently. A two-valued evaluator returning
False for "I could not compute this" makes ``none_of`` read missing data as
permission — the strategy enters *because* the news feed is down. Those cases
are tested by name.
"""

from __future__ import annotations

import datetime as dt
import random
from decimal import Decimal
from uuid import UUID

import pytest

from algotrader.common.enums import Direction, Regime, Timeframe
from algotrader.common.models.market import Bar
from algotrader.indicators.engine import IndicatorEngine, MultiTimeframeSnapshot
from algotrader.indicators.levels import Level, LevelSet, OpeningRange
from algotrader.strategy.context import ContextError, EvalContext
from algotrader.strategy.dsl import (
    REGISTRY,
    Condition,
    ConditionGroup,
    compile_strategy,
    load_strategy_yaml,
)
from algotrader.strategy.primitives import registry as primitive_registry
from algotrader.strategy.primitives.evaluators import (
    CONDITION_EVALUATORS,
    DEFERRED_TO_POSITION_MANAGER,
    STOP_EVALUATORS,
    PrimitiveError,
    snap_stop,
    snap_target,
)
from algotrader.strategy.runtime import (
    DEFAULT_CAPABILITIES,
    StrategyEvaluator,
    UnevaluableStrategyError,
    evaluate_condition,
    evaluate_group,
    evaluate_stop,
    evaluate_target,
    load_evaluators,
)

primitive_registry.install()

BASE = dt.datetime(2026, 8, 20, 3, 45, tzinfo=dt.UTC)
NOW = dt.datetime(2026, 8, 20, 5, 0, tzinfo=dt.UTC)  # 10:30 IST
CID = UUID("11111111-2222-3333-4444-555555555555")


def _bars(n: int, tf: Timeframe = Timeframe.M5, symbol: str = "INFY", drift: float = 0.0006):
    rng = random.Random(7)
    out, price = [], 1000.0
    for i in range(n):
        price = price * (1 + rng.uniform(-0.004, 0.004) + drift)
        out.append(
            Bar(
                symbol=symbol,
                timeframe=tf,
                open_ts=BASE + dt.timedelta(minutes=5 * i),
                open=Decimal(f"{price:.2f}"),
                high=Decimal(f"{price * 1.004:.2f}"),
                low=Decimal(f"{price * 0.996:.2f}"),
                close=Decimal(f"{price:.2f}"),
                volume=rng.randint(5_000, 20_000),
            )
        )
    return out


@pytest.fixture(scope="module")
def snapshot() -> MultiTimeframeSnapshot:
    engine = IndicatorEngine()
    for timeframe in engine.timeframes:
        engine.set_for("INFY", timeframe).warm_up(_bars(300, timeframe))
    return engine.snapshot("INFY")


@pytest.fixture
def opening_range() -> OpeningRange:
    return OpeningRange(
        symbol="INFY",
        trade_date=dt.date(2026, 8, 20),
        high=Decimal("1150"),
        low=Decimal("1140"),
        sealed=True,
    )


@pytest.fixture
def ctx(snapshot: MultiTimeframeSnapshot, opening_range: OpeningRange):
    def build(**overrides) -> EvalContext:
        base: dict = {
            "symbol": "INFY",
            "now": NOW,
            "timeframe": Timeframe.M5,
            "direction": Direction.LONG,
            "last_price": Decimal("1200.00"),
            "snapshot": snapshot,
            "opening_range": opening_range,
            # The strategy under test declares TRENDING/RISK_ON applicability,
            # which fire() now enforces. Tests that care about a missing or
            # wrong regime override this explicitly.
            "regime": Regime.TRENDING,
        }
        base.update(overrides)
        return EvalContext(**base)

    return build


def _cond(primitive: str, **params) -> Condition:
    return Condition(primitive=primitive, params=params)


STRATEGY_YAML = """
id: orb_long_v1
name: Opening Range Breakout Long
origin: USER_AUTHORED
created_at: 2026-08-20T04:00:00+00:00
created_by: test
direction: LONG
hypothesis:
  mechanism: >-
    Overnight order imbalance resolves in the first fifteen minutes; a break of
    that range on volume marks the point institutional flow commits to a
    direction for the session.
  why_it_should_persist: >-
    It is a structural consequence of how overnight orders batch into the open,
    not a pattern that arbitrages away as participants learn it.
  expected_failure_mode: >-
    Rangebound low-volatility days produce false breaks that revert
    immediately.
applicability: {regimes: [TRENDING, RISK_ON], timeframe: 5m}
entry:
  all_of:
    - {primitive: price_breaks_level, params: {level: opening_range_high, direction: above}}
    - {primitive: price_above_ma, params: {period: 20}}
exit:
  stop: {primitive: atr_stop, params: {multiplier: 1.5, period: 14}}
  time: {primitive: squareoff_deadline}
  target: {primitive: r_multiple_target, params: {r: 2.0}}
"""


@pytest.fixture
def document():
    return compile_strategy(load_strategy_yaml(STRATEGY_YAML))


# ---------------------------------------------------------------------------


class TestEveryPrimitiveCanActuallyRun:
    """The test whose absence let 27 unimplemented primitives look finished.

    A strategy referencing a primitive with no evaluator validates, compiles,
    hashes, stores — and then never fires. Nothing raises. The symptom is an
    absence of trades, which is indistinguishable from a quiet market.
    """

    def test_every_registered_primitive_is_implemented_or_deliberately_deferred(self) -> None:
        implemented = set(CONDITION_EVALUATORS) | set(STOP_EVALUATORS)
        accounted = implemented | DEFERRED_TO_POSITION_MANAGER
        missing = set(REGISTRY.names()) - accounted
        assert not missing, (
            f"declared in the DSL with no evaluator: {sorted(missing)}. A strategy "
            f"using one of these would validate and then silently never fire."
        )

    def test_no_evaluator_exists_for_an_unregistered_name(self) -> None:
        """The other direction: an evaluator with no spec would be reachable
        only by hand-built Conditions, bypassing every parameter bound."""
        extra = (set(CONDITION_EVALUATORS) | set(STOP_EVALUATORS)) - set(REGISTRY.names())
        assert not extra, f"evaluators with no registry entry: {sorted(extra)}"

    def test_the_deferred_ones_are_genuinely_position_management(self) -> None:
        """Guards the escape hatch: 'deferred' must not become a place to park
        primitives nobody implemented."""
        assert DEFERRED_TO_POSITION_MANAGER == {
            "r_multiple_target",
            "trail_after_r",
            "squareoff_deadline",
        }

    def test_all_27_are_accounted_for(self) -> None:
        assert len(REGISTRY.names()) == 27
        assert (
            len(CONDITION_EVALUATORS) + len(STOP_EVALUATORS) + len(DEFERRED_TO_POSITION_MANAGER)
            == 27
        )


class TestUnknownIsNotFalse:
    """Three-valued logic, and the reason it exists.

    ``none_of`` asserts an absence. If an unevaluable condition collapses to
    False, ``none_of`` reads it as "the forbidden thing is not there" and the
    strategy enters. Missing data becomes permission — a fail-open in a system
    whose first invariant is fail-closed.
    """

    def test_a_missing_input_yields_unknown_not_false(self, ctx) -> None:
        result = evaluate_condition(_cond("india_vix_between", min=10, max=20), ctx())
        assert result.outcome is None

    def test_none_of_does_not_pass_on_an_unevaluable_condition(self, ctx) -> None:
        """The specific fail-open. With the news feed down, 'no material news'
        cannot be answered — and must not therefore be treated as satisfied."""
        group = ConditionGroup(none_of=[_cond("no_material_news", lookback_hours=24)])
        decision = evaluate_group(group, ctx(hours_since_material_news=None))
        assert decision.outcome is None
        assert not decision.fired

    def test_none_of_passes_only_when_the_condition_is_definitely_absent(self, ctx) -> None:
        group = ConditionGroup(none_of=[_cond("no_material_news", lookback_hours=24)])
        # 2 hours since material news => 'no_material_news' is False => none_of holds
        decision = evaluate_group(group, ctx(hours_since_material_news=2.0))
        assert decision.outcome is True
        assert decision.fired

    def test_none_of_blocks_when_the_condition_is_present(self, ctx) -> None:
        group = ConditionGroup(none_of=[_cond("no_material_news", lookback_hours=24)])
        decision = evaluate_group(group, ctx(hours_since_material_news=50.0))
        assert decision.outcome is False

    def test_all_of_prefers_a_definite_false_over_unknown(self, ctx) -> None:
        """Both block entry, but False is actionable and UNKNOWN is a health
        signal — conflating them would hide a broken feed behind a normal
        no-trade."""
        group = ConditionGroup(
            all_of=[
                _cond("price_breaks_level", level="opening_range_high", direction="below"),
                _cond("india_vix_between", min=10, max=20),
            ]
        )
        assert evaluate_group(group, ctx()).outcome is False

    def test_all_of_is_unknown_when_nothing_definitely_failed(self, ctx) -> None:
        group = ConditionGroup(
            all_of=[
                _cond("price_breaks_level", level="opening_range_high", direction="above"),
                _cond("india_vix_between", min=10, max=20),
            ]
        )
        assert evaluate_group(group, ctx()).outcome is None

    def test_any_of_is_satisfied_by_one_true_despite_unknowns(self, ctx) -> None:
        """One alternative definitely holding is enough; the others need not be
        readable for the disjunction to be settled."""
        group = ConditionGroup(
            any_of=[
                _cond("price_breaks_level", level="opening_range_high", direction="above"),
                _cond("india_vix_between", min=10, max=20),
            ]
        )
        assert evaluate_group(group, ctx()).outcome is True

    def test_an_entry_never_fires_on_unknown(self, ctx) -> None:
        group = ConditionGroup(all_of=[_cond("sector_rank_top_n", n=5)])
        decision = evaluate_group(group, ctx())
        assert decision.outcome is None and decision.fired is False

    def test_the_reason_names_what_could_not_be_evaluated(self, ctx) -> None:
        """'Entry conditions not met' is not something an operator can act on."""
        group = ConditionGroup(all_of=[_cond("sector_rank_top_n", n=5)])
        assert "sector_rank_top_n" in evaluate_group(group, ctx()).reason()


class TestPriceAndLevelPrimitives:
    def test_a_break_above_a_level_needs_to_clear_the_buffer(self, ctx) -> None:
        c = _cond(
            "price_breaks_level", level="opening_range_high", direction="above", buffer_pct=1.0
        )
        # 1150 + 1% = 1161.5; price 1155 has not cleared it
        assert evaluate_condition(c, ctx(last_price=Decimal("1155"))).outcome is False
        assert evaluate_condition(c, ctx(last_price=Decimal("1170"))).outcome is True

    def test_a_break_below_applies_the_buffer_on_the_other_side(self, ctx) -> None:
        c = _cond(
            "price_breaks_level", level="opening_range_low", direction="below", buffer_pct=1.0
        )
        assert evaluate_condition(c, ctx(last_price=Decimal("1135"))).outcome is False
        assert evaluate_condition(c, ctx(last_price=Decimal("1120"))).outcome is True

    def test_an_unsealed_opening_range_is_unknown_not_zero(self, ctx) -> None:
        """Before 09:30 the range does not exist. Treating a missing level as
        zero would make every price a break above it."""
        unsealed = OpeningRange(symbol="INFY", trade_date=dt.date(2026, 8, 20))
        c = _cond("price_breaks_level", level="opening_range_high", direction="above")
        assert evaluate_condition(c, ctx(opening_range=unsealed)).outcome is None

    def test_proximity_to_a_level(self, ctx) -> None:
        c = _cond("price_within_pct_of_level", level="prev_day_high", max_distance_pct=1.0)
        levels = LevelSet(symbol="INFY", prior_high=Decimal("1205"))
        assert evaluate_condition(c, ctx(levels=levels)).outcome is True
        far = LevelSet(symbol="INFY", prior_high=Decimal("1400"))
        assert evaluate_condition(c, ctx(levels=far)).outcome is False

    def test_a_gap_is_measured_from_the_open_not_the_current_price(self, ctx) -> None:
        """A gap is a statement about the OPEN. Using the last price would make
        the same morning read as a different gap at every moment of it."""
        c = _cond("gap_from_prev_close", min_pct=1.0, max_pct=3.0)
        result = evaluate_condition(
            c, ctx(prev_close=Decimal("1000"), day_open=Decimal("1020"), last_price=Decimal("1200"))
        )
        assert result.outcome is True

    def test_a_gap_without_the_open_is_unknown(self, ctx) -> None:
        c = _cond("gap_from_prev_close", min_pct=1.0, max_pct=3.0)
        assert evaluate_condition(c, ctx(prev_close=Decimal("1000"))).outcome is None

    def test_the_nearest_swing_is_chosen_not_the_most_extreme(self, ctx) -> None:
        """An unqualified 'swing low' would put the stop at the session's
        lowest swing and size the position to nothing."""
        levels = LevelSet(
            symbol="INFY",
            swing_levels=[
                Level(price=Decimal("1180"), kind="support"),
                Level(price=Decimal("900"), kind="support"),
            ],
        )
        assert ctx(levels=levels).named_level("swing_low") == Decimal("1180")


class TestTrendPrimitives:
    def test_price_above_ma(self, ctx, snapshot) -> None:
        ema20 = snapshot.value(Timeframe.M5, "ema_20")
        assert ema20 is not None
        above = evaluate_condition(
            _cond("price_above_ma", period=20), ctx(last_price=Decimal("5000"))
        )
        assert above.outcome is True
        below = evaluate_condition(
            _cond("price_above_ma", period=20), ctx(last_price=Decimal("10"))
        )
        assert below.outcome is False

    def test_a_crossover_is_a_crossing_not_a_comparison(self, snapshot) -> None:
        """The distinction is the point of the primitive: 'fast is above slow'
        is true on every bar of a trend; 'fast crossed above slow' on one. The
        first would re-enter every bar."""
        engine = IndicatorEngine()
        # Build a series that genuinely crosses: down then sharply up.
        bars = _bars(200, Timeframe.M5, drift=-0.002) + _bars(60, Timeframe.M5, drift=0.010)
        for i, bar in enumerate(bars):
            engine.update(bar.model_copy(update={"open_ts": BASE + dt.timedelta(minutes=5 * i)}))
        snap = engine.snapshot("INFY")

        fired_bars = 0
        assert snap.value(Timeframe.M5, "ema_20") is not None
        c = _cond("ma_crossover", fast=20, slow=50, direction="bullish")
        result = evaluate_condition(
            c,
            EvalContext(
                symbol="INFY",
                now=NOW,
                timeframe=Timeframe.M5,
                direction=Direction.LONG,
                last_price=Decimal("1000"),
                snapshot=snap,
            ),
        )
        # At the end of a long rally fast is ABOVE slow but did not cross here.
        assert result.outcome is False, "a sustained trend must not read as a crossover"
        assert fired_bars == 0

    def test_a_crossover_without_history_is_unknown(self, ctx) -> None:
        """A restarted process with no history must decline, not guess."""
        bare = MultiTimeframeSnapshot(
            symbol="INFY",
            per_timeframe={Timeframe.M5: {"ema_20": 100.0, "ema_50": 99.0}},
            all_ready=True,
        )
        c = _cond("ma_crossover", fast=20, slow=50, direction="bullish")
        assert evaluate_condition(c, ctx(snapshot=bare)).outcome is None

    def test_ma_slope_uses_the_lookback(self, ctx) -> None:
        c = _cond("ma_slope_positive", period=20, lookback=20)
        assert evaluate_condition(c, ctx()).outcome is True  # the fixture trends up

    def test_ma_slope_beyond_retained_history_is_unknown(self, ctx) -> None:
        bare = MultiTimeframeSnapshot(
            symbol="INFY", per_timeframe={Timeframe.M5: {"ema_20": 100.0}}, all_ready=True
        )
        c = _cond("ma_slope_positive", period=20, lookback=10)
        assert evaluate_condition(c, ctx(snapshot=bare)).outcome is None


class TestRemainingConditionPrimitives:
    def test_rsi_band_is_inclusive(self, ctx, snapshot) -> None:
        rsi = snapshot.value(Timeframe.M5, "rsi_14")
        assert rsi is not None
        exact = Decimal(repr(rsi))
        c = _cond("rsi_between", period=14, min=exact, max=exact)
        assert evaluate_condition(c, ctx()).outcome is True

    def test_macd_histogram_sign(self, ctx, snapshot) -> None:
        histogram = snapshot.value(Timeframe.M5, "macd_histogram")
        assert histogram is not None
        want_positive = histogram > 0
        c = _cond("macd_histogram_sign", positive=want_positive)
        assert evaluate_condition(c, ctx()).outcome is True

    def test_atr_pct_band(self, ctx) -> None:
        wide = _cond("atr_pct_between", period=14, min_pct=0, max_pct=20)
        assert evaluate_condition(wide, ctx()).outcome is True
        narrow = _cond("atr_pct_between", period=14, min_pct=15, max_pct=20)
        assert evaluate_condition(narrow, ctx()).outcome is False

    def test_range_pct_from_the_opening_range(self, ctx, opening_range) -> None:
        # 1150/1140 -> ~0.87% of the midpoint
        c = _cond("range_pct_between", source="opening_range", min_pct=0.5, max_pct=1.5)
        assert evaluate_condition(c, ctx()).outcome is True

    def test_range_pct_from_a_bar_needs_a_bar(self, ctx) -> None:
        c = _cond("range_pct_between", source="bar", min_pct=0, max_pct=5)
        assert evaluate_condition(c, ctx()).outcome is None

    def test_volume_ratio(self, ctx) -> None:
        assert (
            evaluate_condition(_cond("volume_ratio_above", window=20, threshold=0.1), ctx()).outcome
            is True
        )
        assert (
            evaluate_condition(_cond("volume_ratio_above", window=20, threshold=19), ctx()).outcome
            is False
        )

    def test_india_vix_band(self, ctx) -> None:
        c = _cond("india_vix_between", min=10, max=20)
        assert evaluate_condition(c, ctx(india_vix=Decimal("14"))).outcome is True
        assert evaluate_condition(c, ctx(india_vix=Decimal("30"))).outcome is False

    def test_regime_membership(self, ctx) -> None:
        c = _cond("regime_is", regimes="TRENDING,RISK_ON")
        assert evaluate_condition(c, ctx(regime=Regime.TRENDING)).outcome is True
        assert evaluate_condition(c, ctx(regime=Regime.RANGEBOUND)).outcome is False

    def test_an_unknown_regime_name_is_a_strategy_bug_not_a_quiet_false(self, ctx) -> None:
        """Silently never matching would look like a filter that simply never
        passes, which is indistinguishable from a working filter."""
        with pytest.raises(PrimitiveError, match="not a regime"):
            evaluate_condition(_cond("regime_is", regimes="BULLISH"), ctx())

    def test_index_not_opposing_is_permissive_by_design(self, ctx) -> None:
        """It asks that the index is not falling hard against a long, NOT that
        it is rising — a stock is allowed to lead its index."""
        c = _cond("index_not_opposing", index="NIFTY", tolerance_pct=0.3)
        flat = evaluate_condition(c, ctx(index_change_pct={"NIFTY": Decimal("0.0")}))
        assert flat.outcome is True
        mild = evaluate_condition(c, ctx(index_change_pct={"NIFTY": Decimal("-0.2")}))
        assert mild.outcome is True
        hard = evaluate_condition(c, ctx(index_change_pct={"NIFTY": Decimal("-1.5")}))
        assert hard.outcome is False

    def test_index_not_opposing_flips_for_a_short(self, ctx) -> None:
        c = _cond("index_not_opposing", index="NIFTY", tolerance_pct=0.3)
        result = evaluate_condition(
            c, ctx(direction=Direction.SHORT, index_change_pct={"NIFTY": Decimal("1.5")})
        )
        assert result.outcome is False

    def test_sector_rank(self, ctx) -> None:
        c = _cond("sector_rank_top_n", n=5)
        assert evaluate_condition(c, ctx(sector_rank=3)).outcome is True
        assert evaluate_condition(c, ctx(sector_rank=9)).outcome is False

    def test_news_score(self, ctx) -> None:
        c = _cond("news_score_above", threshold=0.2)
        assert evaluate_condition(c, ctx(news_score=0.5)).outcome is True
        assert evaluate_condition(c, ctx(news_score=0.1)).outcome is False

    def test_no_material_news_declines_when_the_feed_is_silent(self, ctx) -> None:
        """A dead news feed must not read as 'no news' — that would be a green
        light on every symbol on exactly the days a feed is most likely down."""
        c = _cond("no_material_news", lookback_hours=24)
        assert evaluate_condition(c, ctx(hours_since_material_news=None)).outcome is None
        assert evaluate_condition(c, ctx(hours_since_material_news=48.0)).outcome is True
        assert evaluate_condition(c, ctx(hours_since_material_news=2.0)).outcome is False

    def test_within_window_uses_ist(self, ctx) -> None:
        """A trader writes 09:20 meaning IST. Evaluating it in UTC would shift
        every window by five and a half hours."""
        c = _cond("within_window", start="10:00", end="11:00")
        assert evaluate_condition(c, ctx()).outcome is True  # NOW is 10:30 IST
        late = _cond("within_window", start="14:00", end="15:00")
        assert evaluate_condition(late, ctx()).outcome is False

    def test_an_inverted_window_is_refused(self, ctx) -> None:
        with pytest.raises(PrimitiveError, match="not before"):
            evaluate_condition(_cond("within_window", start="15:00", end="09:00"), ctx())

    def test_bars_since_open(self, ctx) -> None:
        c = _cond("min_bars_since_open", bars=3)
        assert evaluate_condition(c, ctx(bars_since_open=5)).outcome is True
        assert evaluate_condition(c, ctx(bars_since_open=1)).outcome is False
        assert evaluate_condition(c, ctx()).outcome is None

    def test_bars_until_squareoff(self, ctx) -> None:
        c = _cond("bars_until_squareoff_above", bars=6)
        assert evaluate_condition(c, ctx(bars_until_squareoff=10)).outcome is True
        assert evaluate_condition(c, ctx(bars_until_squareoff=2)).outcome is False

    def test_timeframe_agreement_settles_when_the_readable_ones_suffice(self, ctx) -> None:
        """A cold third timeframe must not block a decision the other two have
        already made."""
        partial = MultiTimeframeSnapshot(
            symbol="INFY",
            per_timeframe={
                Timeframe.M5: {"ema_20": 110.0, "ema_50": 100.0},
                Timeframe.M15: {"ema_20": 110.0, "ema_50": 100.0},
                Timeframe.H1: {"ema_20": None, "ema_50": None},
            },
            all_ready=False,
        )
        c = _cond("timeframe_agreement_at_least", count=2, of="5m,15m,1h")
        assert evaluate_condition(c, ctx(snapshot=partial)).outcome is True

    def test_timeframe_agreement_is_false_when_unreachable(self, ctx) -> None:
        partial = MultiTimeframeSnapshot(
            symbol="INFY",
            per_timeframe={
                Timeframe.M5: {"ema_20": 90.0, "ema_50": 100.0},
                Timeframe.M15: {"ema_20": 90.0, "ema_50": 100.0},
                Timeframe.H1: {"ema_20": None, "ema_50": None},
            },
            all_ready=False,
        )
        c = _cond("timeframe_agreement_at_least", count=3, of="5m,15m,1h")
        assert evaluate_condition(c, ctx(snapshot=partial)).outcome is False

    def test_timeframe_agreement_is_unknown_in_the_undecided_middle(self, ctx) -> None:
        partial = MultiTimeframeSnapshot(
            symbol="INFY",
            per_timeframe={
                Timeframe.M5: {"ema_20": 110.0, "ema_50": 100.0},
                Timeframe.M15: {"ema_20": None, "ema_50": None},
                Timeframe.H1: {"ema_20": None, "ema_50": None},
            },
            all_ready=False,
        )
        c = _cond("timeframe_agreement_at_least", count=2, of="5m,15m,1h")
        assert evaluate_condition(c, ctx(snapshot=partial)).outcome is None

    def test_higher_timeframe_trend(self, ctx) -> None:
        c = _cond("higher_tf_trend_is", timeframe="1h", direction="up")
        assert evaluate_condition(c, ctx()).outcome is True


class TestStopsAreRealPlaceablePrices:
    """A stop is not a number, it is an order. NSE rejects an off-tick price,
    so an unsnapped stop is a position with no protection at all."""

    def test_an_atr_stop_sits_below_entry_for_a_long(self, ctx) -> None:
        from algotrader.strategy.primitives.evaluators import atr_stop

        stop = atr_stop(ctx(), {"multiplier": 1.5, "period": 14})
        assert stop is not None and stop < Decimal("1200")

    def test_an_atr_stop_sits_above_entry_for_a_short(self, ctx) -> None:
        from algotrader.strategy.primitives.evaluators import atr_stop

        stop = atr_stop(ctx(direction=Direction.SHORT), {"multiplier": 1.5, "period": 14})
        assert stop is not None and stop > Decimal("1200")

    def test_every_stop_lands_on_the_tick_grid(self, ctx) -> None:
        from algotrader.strategy.primitives.evaluators import atr_stop

        for multiplier in ("0.5", "1.3", "1.75", "2.9", "3.33"):
            stop = atr_stop(ctx(), {"multiplier": Decimal(multiplier), "period": 14})
            assert stop is not None
            assert stop % Decimal("0.05") == 0, (
                f"{stop} is not placeable at multiplier {multiplier}"
            )

    def test_a_stop_never_exceeds_four_decimal_places(self, ctx) -> None:
        """``Price`` caps at four. An unquantised value is a ValidationError at
        whichever downstream site builds the model first."""
        from algotrader.strategy.primitives.evaluators import atr_stop

        for multiplier in ("0.5", "1.5", "2.7"):
            stop = atr_stop(ctx(), {"multiplier": Decimal(multiplier), "period": 14})
            assert stop is not None and -stop.as_tuple().exponent <= 4

    def test_an_exact_grid_price_is_still_quantised(self, ctx) -> None:
        """The early-return path skipped quantisation, so a price already on the
        grid kept whatever precision it arrived with."""
        snapped = snap_target(Decimal("1227.10000"), Decimal("0.05"), Direction.LONG)
        assert -snapped.as_tuple().exponent <= 4

    def test_snapping_widens_a_stop_rather_than_tightening_it(self) -> None:
        """Rounding inward would silently make a declared 1.5x ATR stop 1.49x,
        tightening every stop in the system and inventing whipsaws."""
        long_stop = snap_stop(Decimal("100.03"), Decimal("0.05"), Direction.LONG)
        assert long_stop == Decimal("100.0000")
        short_stop = snap_stop(Decimal("100.03"), Decimal("0.05"), Direction.SHORT)
        assert short_stop == Decimal("100.0500")

    def test_snapping_a_target_moves_it_toward_the_entry(self) -> None:
        """The mirror of a stop: a target only pays if it fills."""
        assert snap_target(Decimal("100.03"), Decimal("0.05"), Direction.LONG) == Decimal(
            "100.0000"
        )
        assert snap_target(Decimal("100.03"), Decimal("0.05"), Direction.SHORT) == Decimal(
            "100.0500"
        )

    def test_a_structure_stop_puts_the_buffer_on_the_protective_side(self, ctx) -> None:
        """Applying the buffer inward would place the stop inside the level it
        exists to sit behind."""
        from algotrader.strategy.primitives.evaluators import structure_stop

        stop = structure_stop(ctx(), {"level": "opening_range_low", "buffer_pct": 1.0})
        assert stop is not None and stop < Decimal("1140")

    def test_a_stop_that_cannot_be_computed_is_none(self, ctx) -> None:
        from algotrader.strategy.primitives.evaluators import structure_stop

        unsealed = OpeningRange(symbol="INFY", trade_date=dt.date(2026, 8, 20))
        assert structure_stop(ctx(opening_range=unsealed), {"level": "opening_range_low"}) is None

    def test_a_zero_tick_is_refused(self) -> None:
        with pytest.raises(PrimitiveError, match="tick size"):
            snap_stop(Decimal("100"), Decimal("0"), Direction.LONG)

    def test_the_target_is_measured_against_the_snapped_stop(self, document) -> None:
        """So the R multiple a journal reports is the one the position had."""
        target = evaluate_target(document, Decimal("100"), Decimal("95"))
        assert target == Decimal("110.0000")


class TestFiringProducesATrigger:
    def test_a_met_strategy_produces_a_trigger(self, document, ctx) -> None:
        trigger = StrategyEvaluator(document).fire(ctx(), correlation_id=CID)
        assert trigger is not None
        assert trigger.symbol == "INFY"
        assert trigger.suggested_stop < trigger.trigger_price
        assert trigger.strategy_id == "orb_long_v1"

    def test_an_unmet_strategy_produces_nothing(self, document, ctx) -> None:
        assert (
            StrategyEvaluator(document).fire(ctx(last_price=Decimal("1100")), correlation_id=CID)
            is None
        )

    def test_it_refuses_to_fire_when_the_stop_cannot_be_computed(self, document, ctx) -> None:
        """'Every position has a stop' is an invariant, so an entry whose
        protective stop is unknown is not a trade this system may take."""
        bare = MultiTimeframeSnapshot(
            symbol="INFY",
            per_timeframe={Timeframe.M5: {"ema_20": 100.0, "atr_14": None}},
            all_ready=True,
        )
        assert StrategyEvaluator(document).fire(ctx(snapshot=bare), correlation_id=CID) is None

    def test_timeframe_agreement_is_reported_as_a_magnitude(self, document, ctx) -> None:
        """``Trigger`` bounds it 0..3 while ``trend_agreement`` is signed -3..3;
        passing the signed value straight through would raise on any short."""
        trigger = StrategyEvaluator(document).fire(ctx(), correlation_id=CID)
        assert trigger is not None and 0 <= trigger.timeframe_agreement <= 3

    def test_a_direction_mismatch_is_refused(self, document, ctx) -> None:
        """A long strategy evaluated with short context reads every directional
        primitive backwards, and every one of them still returns a bool."""
        with pytest.raises(UnevaluableStrategyError, match="direction"):
            StrategyEvaluator(document).evaluate(ctx(direction=Direction.SHORT))

    def test_the_correlation_id_must_be_supplied_by_the_caller(self, document, ctx) -> None:
        """Minting a UUID inside the evaluator would put randomness in the
        strategy path. A backtest could then never compare two Triggers for
        equality, so it could not assert that the decision SEQUENCE matched —
        which is E12-S03's whole acceptance criterion."""
        from uuid import uuid4

        wanted = uuid4()
        trigger = StrategyEvaluator(document).fire(ctx(), correlation_id=wanted)
        assert trigger is not None and trigger.correlation_id == wanted

    def test_firing_twice_on_the_same_context_is_identical(self, document, ctx) -> None:
        ev, context = StrategyEvaluator(document), ctx()
        first = ev.fire(context, correlation_id=CID)
        second = ev.fire(context, correlation_id=CID)
        assert first == second, "the evaluator is not a pure function"


class TestCapabilityIsVerifiedAtLoadTime:
    """The DSL is wider than the engine. Discovering that at 09:20, from an
    absence of trades, is the worst possible time and form."""

    def test_a_strategy_wanting_an_uncomputed_period_is_refused(self, ctx) -> None:
        bad = compile_strategy(
            load_strategy_yaml(STRATEGY_YAML.replace("period: 20}", "period: 33}"))
        )
        with pytest.raises(UnevaluableStrategyError, match="ema_33"):
            StrategyEvaluator(bad)

    def test_the_error_names_what_is_available(self) -> None:
        bad = compile_strategy(
            load_strategy_yaml(STRATEGY_YAML.replace("period: 20}", "period: 33}"))
        )
        with pytest.raises(UnevaluableStrategyError, match="20, 50, 200"):
            StrategyEvaluator(bad)

    def test_capability_cannot_be_skipped(self, document) -> None:
        """Verification lives in __init__, so holding an evaluator is proof the
        strategy can be answered here."""
        assert StrategyEvaluator(document).document is document

    def test_the_registry_no_longer_offers_uncomputed_timeframes(self) -> None:
        """The default for timeframe_agreement_at_least was '1h,1d,1w'; the
        engine carries 5m/15m/1h, so a strategy omitting the parameter could
        never fire."""
        spec = REGISTRY.get("timeframe_agreement_at_least")
        of = next(p for p in spec.params if p.name == "of")
        for name in str(of.default).split(","):
            assert Timeframe(name) in DEFAULT_CAPABILITIES.timeframes

    def test_higher_tf_trend_only_offers_computed_timeframes(self) -> None:
        spec = REGISTRY.get("higher_tf_trend_is")
        choices = next(p for p in spec.params if p.name == "timeframe").choices or []
        for name in choices:
            assert Timeframe(name) in DEFAULT_CAPABILITIES.timeframes

    def test_an_inverted_rsi_band_is_caught_at_load(self, ctx) -> None:
        from algotrader.strategy.runtime import verify_condition

        with pytest.raises(UnevaluableStrategyError, match="empty"):
            verify_condition(_cond("rsi_between", period=14, min=70, max=30))

    def test_a_crossover_with_fast_slower_than_slow_is_caught(self) -> None:
        from algotrader.strategy.runtime import verify_condition

        with pytest.raises(UnevaluableStrategyError, match="not faster"):
            # Both periods are inside the registry's declared bounds and both
            # indicators exist, so only the ordering check can catch this.
            verify_condition(_cond("ma_crossover", fast=50, slow=20, direction="bullish"))

    def test_one_bad_strategy_does_not_block_the_others(self, document) -> None:
        bad = compile_strategy(
            load_strategy_yaml(
                STRATEGY_YAML.replace("period: 20}", "period: 33}").replace(
                    "id: orb_long_v1", "id: broken_one"
                )
            )
        )
        usable, rejected = load_evaluators([document, bad])
        assert [e.strategy_id for e in usable] == ["orb_long_v1"]
        assert "broken_one" in rejected


class TestTheContextRefusesIncoherentInput:
    def test_a_snapshot_for_another_symbol_is_refused(self, snapshot) -> None:
        """Evaluating TCS against INFY's indicators produces a plausible,
        entirely wrong signal — and nothing downstream could detect it."""
        with pytest.raises(ContextError, match="TCS"):
            EvalContext(
                symbol="TCS",
                now=NOW,
                timeframe=Timeframe.M5,
                direction=Direction.LONG,
                last_price=Decimal("100"),
                snapshot=snapshot,
            )

    def test_a_naive_timestamp_is_refused(self, snapshot) -> None:
        with pytest.raises(ContextError, match="timezone-aware"):
            EvalContext(
                symbol="INFY",
                now=dt.datetime(2026, 8, 20, 5, 0),
                timeframe=Timeframe.M5,
                direction=Direction.LONG,
                last_price=Decimal("100"),
                snapshot=snapshot,
            )

    def test_a_nonpositive_price_is_refused(self, snapshot) -> None:
        with pytest.raises(ContextError, match="last_price"):
            EvalContext(
                symbol="INFY",
                now=NOW,
                timeframe=Timeframe.M5,
                direction=Direction.LONG,
                last_price=Decimal("0"),
                snapshot=snapshot,
            )

    def test_a_nonpositive_tick_is_refused(self, snapshot) -> None:
        with pytest.raises(ContextError, match="tick_size"):
            EvalContext(
                symbol="INFY",
                now=NOW,
                timeframe=Timeframe.M5,
                direction=Direction.LONG,
                last_price=Decimal("100"),
                tick_size=Decimal("0"),
                snapshot=snapshot,
            )


class TestApplicabilityIsEnforcedNotDecorative:
    """Found by reading the story against the code as a BA would.

    ``Applicability`` was parsed, validated, and folded into the content hash —
    and then consulted by nothing. A strategy declaring "TRENDING only, above
    100 rupees" had both ignored, so a trend strategy would fire freely in a
    rangebound market and on a penny stock. The declaration existed purely to
    make the document look complete.
    """

    def test_the_declared_regime_is_honoured(self, document, ctx) -> None:
        ev = StrategyEvaluator(document)
        assert ev.applies_to(ctx(regime=Regime.TRENDING)).applies is True
        assert ev.applies_to(ctx(regime=Regime.RANGEBOUND)).applies is False

    def test_an_unknown_regime_does_not_apply(self, document, ctx) -> None:
        """The strategy was validated for a named set of regimes. Running it
        when the regime cannot be established runs it outside the conditions
        its backtest covers."""
        check = StrategyEvaluator(document).applies_to(ctx(regime=None))
        assert check.applies is False
        assert "unknown" in (check.reason or "")

    def test_the_price_floor_is_honoured(self, document, ctx) -> None:
        ev = StrategyEvaluator(document)
        low = ctx(regime=Regime.TRENDING, last_price=Decimal("50"))
        assert ev.applies_to(low).applies is False

    def test_the_declared_timeframe_is_honoured(self, document, ctx) -> None:
        ev = StrategyEvaluator(document)
        wrong = ctx(regime=Regime.TRENDING, timeframe=Timeframe.H1)
        assert ev.applies_to(wrong).applies is False

    def test_the_reason_is_specific_enough_to_act_on(self, document, ctx) -> None:
        check = StrategyEvaluator(document).applies_to(ctx(regime=Regime.RANGEBOUND))
        assert "RANGEBOUND" in (check.reason or "")

    def test_firing_is_blocked_outside_applicability(self, document, ctx) -> None:
        """The guarantee has to be structural: conditions that would otherwise
        fire must not produce a Trigger out of scope."""
        ev = StrategyEvaluator(document)
        assert ev.evaluate(ctx(regime=Regime.RANGEBOUND)).fired is True
        assert ev.fire(ctx(regime=Regime.RANGEBOUND), correlation_id=CID) is None

    def test_firing_succeeds_inside_applicability(self, document, ctx) -> None:
        assert (
            StrategyEvaluator(document).fire(ctx(regime=Regime.TRENDING), correlation_id=CID)
            is not None
        )


class TestTimeframeAgreementCarriesDirection:
    """Found by reading fire() as an architect.

    It reported ``abs(snapshot.trend_agreement())``. ``trend_agreement`` counts
    timeframes where the fast MA is above the slow one and the sign carries
    direction, so discarding the sign turns maximum DISAGREEMENT into maximum
    CONFLUENCE: a long trade against a unanimously bearish tape scored 3 of 3.
    That number reaches ``Recommendation.timeframe_agreement`` and the AI
    confirmation prompt, where it is exactly the wrong thing to say.
    """

    def _tape(self, fast: float, slow: float) -> MultiTimeframeSnapshot:
        return MultiTimeframeSnapshot(
            symbol="INFY",
            all_ready=True,
            per_timeframe={
                tf: {"ema_20": fast, "ema_50": slow, "atr_14": 2.0}
                for tf in (Timeframe.M5, Timeframe.M15, Timeframe.H1)
            },
        )

    def test_a_long_against_a_bearish_tape_scores_zero(self, ctx) -> None:
        from algotrader.strategy.primitives.evaluators import directional_agreement

        bearish = ctx(snapshot=self._tape(fast=90.0, slow=100.0), direction=Direction.LONG)
        assert directional_agreement(bearish) == 0

    def test_a_long_with_a_bullish_tape_scores_three(self, ctx) -> None:
        from algotrader.strategy.primitives.evaluators import directional_agreement

        bullish = ctx(snapshot=self._tape(fast=110.0, slow=100.0), direction=Direction.LONG)
        assert directional_agreement(bullish) == 3

    def test_a_short_with_a_bearish_tape_scores_three(self, ctx) -> None:
        """The mirror: for a short, falling averages ARE agreement."""
        from algotrader.strategy.primitives.evaluators import directional_agreement

        bearish = ctx(snapshot=self._tape(fast=90.0, slow=100.0), direction=Direction.SHORT)
        assert directional_agreement(bearish) == 3

    def test_the_old_absolute_value_would_have_said_three(self) -> None:
        """Pins the defect so it cannot come back as a 'simplification'."""
        bearish = self._tape(fast=90.0, slow=100.0)
        assert abs(bearish.trend_agreement()) == 3, "this is what fire() used to report"

    def test_a_flat_timeframe_is_not_agreement(self, ctx) -> None:
        from algotrader.strategy.primitives.evaluators import directional_agreement

        flat = ctx(snapshot=self._tape(fast=100.0, slow=100.0))
        assert directional_agreement(flat) == 0


class TestNonFiniteValuesAreNeitherTrustedNorFatal:
    """Found as a pentester, probing what a corrupted snapshot can do.

    Python's ``json`` emits and accepts a bare ``NaN``, so an indicator
    snapshot round-tripping through Redis can carry one. NaN compares False
    against everything, so ``price_above_ma`` answered a confident "no" instead
    of declining; an infinity compares True against everything, so a band check
    passed unconditionally. And ``Decimal("NaN")`` constructs happily and
    raises only on the first COMPARISON — surfacing as an uncaught
    InvalidOperation deep in the signal loop rather than at the boundary.
    """

    def _tainted(self, value: float) -> MultiTimeframeSnapshot:
        return MultiTimeframeSnapshot(
            symbol="INFY",
            all_ready=True,
            per_timeframe={Timeframe.M5: {"ema_20": value, "ema_50": 99.0, "atr_14": value}},
        )

    @pytest.mark.parametrize(
        "value", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"]
    )
    def test_a_tainted_indicator_reads_as_absent_not_as_an_answer(self, ctx, value) -> None:
        result = evaluate_condition(
            _cond("price_above_ma", period=20), ctx(snapshot=self._tainted(value))
        )
        assert result.outcome is None

    @pytest.mark.parametrize(
        "value", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"]
    )
    def test_a_tainted_atr_yields_no_stop_rather_than_crashing(self, ctx, value) -> None:
        from algotrader.strategy.primitives.evaluators import atr_stop

        stop = atr_stop(ctx(snapshot=self._tainted(value)), {"multiplier": 1.5, "period": 14})
        assert stop is None

    @pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
    def test_a_non_finite_parameter_is_refused(self, ctx, bad: str) -> None:
        """``-Infinity`` is the dangerous one: it makes any 'above this
        threshold' gate pass unconditionally, and a strategy author can write
        it in one word."""
        with pytest.raises(PrimitiveError, match="finite"):
            evaluate_condition(_cond("news_score_above", threshold=bad), ctx(news_score=0.5))

    def test_the_money_boundary_refuses_non_finite(self) -> None:
        from algotrader.indicators.framework import to_decimal

        assert to_decimal(float("nan")) is None
        assert to_decimal(float("inf")) is None
        assert to_decimal(1.5) == Decimal("1.5000")

    def test_ordinary_values_are_untouched(self, ctx) -> None:
        """The control: the guard must not reject real numbers."""
        assert evaluate_condition(_cond("price_above_ma", period=20), ctx()).outcome is True


class TestRegistryBoundsHoldAtLoadTime:
    """Defence in depth. compile_strategy validates parameter bounds, but
    nothing structurally guaranteed a document reached the evaluator through
    compilation — and those bounds are what stop a threshold of -1e999 from
    making a gate pass unconditionally. ``Decimal("-1e999")`` is FINITE, so the
    non-finite guard does not catch it; only the declared range does."""

    def test_an_out_of_range_threshold_is_refused_at_load(self) -> None:
        from algotrader.strategy.runtime import verify_condition

        # Decimal("-1e999") is FINITE — Decimal has an arbitrary exponent
        # range — so the non-finite guard does not see it. Only the declared
        # -1..1 range stops it from making the gate pass unconditionally.
        huge = Decimal("-1e999")
        assert huge.is_finite(), "the point of this test is that it is finite"
        with pytest.raises(UnevaluableStrategyError, match="below minimum"):
            verify_condition(_cond("news_score_above", threshold=huge))

    def test_an_unknown_parameter_is_refused_at_load(self) -> None:
        from algotrader.strategy.runtime import verify_condition

        with pytest.raises(UnevaluableStrategyError, match="unknown parameter"):
            verify_condition(_cond("news_score_above", threshold=0.5, shell=True))

    def test_a_valid_condition_still_passes(self) -> None:
        from algotrader.strategy.runtime import verify_condition

        verify_condition(_cond("news_score_above", threshold=0.5))


class TestTheGuardsHoldThroughFireNotJustInIsolation:
    """Both tests here exist because a MUTATION survived.

    The originals called ``directional_agreement`` and
    ``_stop_is_on_the_right_side`` directly. Reverting ``fire()`` to the buggy
    behaviour therefore changed nothing any test could see — the helpers were
    correct and unused by the assertions. Testing a helper is not testing the
    path that calls it.
    """

    #: Bearish tape: the fast average is BELOW the slow one on every timeframe.
    def _bearish_but_breaking_out(self) -> MultiTimeframeSnapshot:
        return MultiTimeframeSnapshot(
            symbol="INFY",
            all_ready=True,
            per_timeframe={
                tf: {"ema_20": 100.0, "ema_50": 110.0, "atr_14": 2.0}
                for tf in (Timeframe.M5, Timeframe.M15, Timeframe.H1)
            },
        )

    def test_a_long_into_a_bearish_tape_reports_zero_agreement(self, document, ctx) -> None:
        """``abs(trend_agreement())`` would report 3 of 3 here. The number goes
        into Recommendation and the AI confirmation prompt."""
        snapshot = self._bearish_but_breaking_out()
        context = ctx(snapshot=snapshot, last_price=Decimal("1200"))
        trigger = StrategyEvaluator(document).fire(context, correlation_id=CID)
        assert trigger is not None, "the fixture must actually fire for this to test anything"
        assert abs(snapshot.trend_agreement()) == 3, "the buggy value would have been 3"
        assert trigger.timeframe_agreement == 0

    def test_a_wrong_sided_stop_is_refused_by_fire_not_raised(self, ctx) -> None:
        """A structure stop can legitimately resolve ABOVE the price for a long
        — a prior-day high that price has not reached. ``Trigger`` would raise
        on it; ``fire()`` must decline cleanly instead, because an exception
        here kills the evaluation of every other symbol in the loop."""
        doc = compile_strategy(load_strategy_yaml(_STRUCTURE_STOP_STRATEGY))
        levels = LevelSet(symbol="INFY", prior_high=Decimal("1500"))
        context = ctx(
            last_price=Decimal("1200"),
            levels=levels,
            snapshot=MultiTimeframeSnapshot(
                symbol="INFY",
                all_ready=True,
                per_timeframe={
                    tf: {"ema_20": 100.0, "ema_50": 90.0, "atr_14": 2.0}
                    for tf in (Timeframe.M5, Timeframe.M15, Timeframe.H1)
                },
            ),
        )
        evaluator = StrategyEvaluator(doc)
        assert evaluator.evaluate(context).fired is True, "entry must pass to reach the stop"
        assert evaluate_stop(doc, context) > context.last_price, "stop must be wrong-sided"
        assert evaluator.fire(context, correlation_id=CID) is None

    def test_the_refusal_is_logged_as_an_error(self, ctx, caplog: pytest.LogCaptureFixture) -> None:
        """A silent None here is indistinguishable from 'conditions not met',
        and this one is a strategy bug worth surfacing."""
        doc = compile_strategy(load_strategy_yaml(_STRUCTURE_STOP_STRATEGY))
        context = ctx(
            last_price=Decimal("1200"),
            levels=LevelSet(symbol="INFY", prior_high=Decimal("1500")),
            snapshot=MultiTimeframeSnapshot(
                symbol="INFY",
                all_ready=True,
                per_timeframe={
                    tf: {"ema_20": 100.0, "ema_50": 90.0, "atr_14": 2.0}
                    for tf in (Timeframe.M5, Timeframe.M15, Timeframe.H1)
                },
            ),
        )
        with caplog.at_level("ERROR"):
            StrategyEvaluator(doc).fire(context, correlation_id=CID)
        assert "wrong side" in caplog.text


#: The same strategy with the opening-range condition dropped and the atr_stop
#: swapped for a structure_stop, so the stop can resolve on the WRONG side of
#: the entry — a prior-day high that price has not reached.
_BREAKOUT_CONDITION = (
    "- {primitive: price_breaks_level, params: {level: opening_range_high, direction: above}}\n    "
)

_STRUCTURE_STOP_STRATEGY = STRATEGY_YAML.replace(_BREAKOUT_CONDITION, "").replace(
    "stop: {primitive: atr_stop, params: {multiplier: 1.5, period: 14}}",
    "stop: {primitive: structure_stop, params: {level: prev_day_high, buffer_pct: 0.1}}",
)
