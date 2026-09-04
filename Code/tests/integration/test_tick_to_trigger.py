"""Raw ticks all the way to a Trigger — the first test of the SYSTEM.

Every other test in this repository tests a component. This one runs the whole
deterministic path a real session takes:

    RawTick -> CleaningPipeline -> BarBuilder -> IndicatorEngine
            -> MultiTimeframeSnapshot -> EvalContext -> StrategyEvaluator
            -> Trigger

That chain had never been assembled. The audit's headline finding was that six
service packages are empty and no module imports both ``ingest`` and
``indicators``, so every "it works" claim was a claim about a part. Assembling
it here does not build the orchestrator, but it does prove the parts fit — and
it is where interface mismatches surface, because a component tested against
its own fixtures agrees with itself by construction.

The cases that matter are the ones spanning a boundary: a bad print that the
cleaner rejects must not reach a bar; a feed gap must invalidate indicators
that a later bar would otherwise appear to refresh; and a stop derived from an
ATR computed on cleaned ticks must still be a placeable NSE price.
"""

from __future__ import annotations

# --- E14-S02: the chain continues into the risk engine ---------------------
import datetime as _dt
import datetime as dt
import random
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from algotrader.common.calendar import (
    MarketCalendar,
    load_holidays_with_status,
)
from algotrader.common.enums import AIVerdict, Direction, Regime, RejectReason, Timeframe
from algotrader.common.models.market import Bar
from algotrader.common.models.trading import AIReview, Recommendation
from algotrader.execution.risk.checks import (
    ELIGIBILITY_ORDER,
    EXPOSURE_ORDER,
    LOSS_ORDER,
    PRECONDITION_ORDER,
    all_check_ids,
    build_eligibility_checks,
    build_exposure_checks,
    build_loss_checks,
    build_margin_timing_checks,
    build_precondition_checks,
)
from algotrader.execution.risk.context import OpenPosition, RiskContext
from algotrader.execution.risk.correlation import correlations_against
from algotrader.execution.risk.framework import RiskEngine
from algotrader.execution.sizer import SizingPolicy, build_sizer
from algotrader.indicators.engine import IndicatorEngine, warm_up_symbols
from algotrader.indicators.levels import OpeningRange
from algotrader.ingest import cleaning
from algotrader.ingest.bars import MultiTimeframeBuilder
from algotrader.ingest.kite_protocol import Mode, RawTick
from algotrader.strategy.context import EvalContext
from algotrader.strategy.dsl import compile_strategy, load_strategy_yaml
from algotrader.strategy.primitives import registry as primitive_registry
from algotrader.strategy.runtime import StrategyEvaluator

#: What system.yaml configures.
NO_TRADE_WINDOWS = (
    (_dt.time(9, 15), _dt.time(9, 20)),
    (_dt.time(15, 0), _dt.time(15, 30)),
)

primitive_registry.install()

TOKEN = 408065
SYMBOL = "INFY"
CID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

#: 09:15 IST on a Thursday, in UTC.
SESSION_OPEN = dt.datetime(2026, 8, 20, 3, 45, tzinfo=dt.UTC)

STRATEGY = """
id: e2e_breakout
name: End-to-end breakout probe
origin: USER_AUTHORED
created_at: 2026-08-20T03:00:00+00:00
created_by: qa
direction: LONG
hypothesis:
  mechanism: >-
    A probe strategy used to drive the full tick-to-trigger path in an
    integration test; it breaks the opening range while holding above the
    twenty-period average, which is enough to exercise levels, indicators and
    the stop calculation together.
  why_it_should_persist: >-
    It is a test fixture rather than a traded strategy, so persistence is not
    a property it is claimed to have.
  expected_failure_mode: >-
    It fires on any sufficiently strong upward move, including noise.
applicability: {regimes: [TRENDING], timeframe: 5m}
entry:
  all_of:
    - {primitive: price_breaks_level, params: {level: opening_range_high, direction: above}}
    - {primitive: price_above_ma, params: {period: 20}}
exit:
  stop: {primitive: atr_stop, params: {multiplier: 1.5, period: 14}}
  time: {primitive: squareoff_deadline}
  target: {primitive: r_multiple_target, params: {r: 2.0}}
"""


def _tick(price: str, *, offset_s: int, volume: int, token: int = TOKEN) -> RawTick:
    moment = SESSION_OPEN + dt.timedelta(seconds=offset_s)
    return RawTick(
        instrument_token=token,
        mode=Mode.QUOTE,
        last_price=Decimal(price),
        volume=volume,
        exchange_timestamp=moment,
        last_quantity=10,
    )


def _session_ticks(count: int = 2400, seed: int = 5) -> list[RawTick]:
    """A rising session, one tick every five seconds."""
    rng = random.Random(seed)
    out: list[RawTick] = []
    price, volume = 1000.0, 0
    for i in range(count):
        price = max(1.0, price * (1 + rng.uniform(-0.0008, 0.0008) + 0.00035))
        volume += rng.randint(50, 400)
        out.append(_tick(f"{price:.2f}", offset_s=i * 5, volume=volume))
    return out


def _history(n: int, timeframe: Timeframe, seed: int = 9) -> list[Bar]:
    """Prior-session bars, as E06-S03 warm-up would load them from ohlcv.

    A 200-period EMA on 5-minute bars needs sixteen hours of trading. No single
    session can warm it from ticks, which is exactly why warm_up_symbols
    exists — and why an end-to-end test that skipped it would be testing a
    system that can never be ready.
    """
    rng = random.Random(seed)
    out, price = [], 900.0
    start = SESSION_OPEN - dt.timedelta(days=5)
    for i in range(n):
        price = price * (1 + rng.uniform(-0.003, 0.003) + 0.0004)
        out.append(
            Bar(
                symbol=SYMBOL,
                timeframe=timeframe,
                open_ts=start + dt.timedelta(minutes=5 * i),
                open=Decimal(f"{price:.2f}"),
                high=Decimal(f"{price * 1.003:.2f}"),
                low=Decimal(f"{price * 0.997:.2f}"),
                close=Decimal(f"{price:.2f}"),
                volume=rng.randint(5_000, 30_000),
            )
        )
    return out


class Pipeline:
    """The assembly under test, wired the way a real service would wire it."""

    def __init__(self) -> None:
        self.cleaner = cleaning.CleaningPipeline()
        # MultiTimeframeBuilder is one of the components the audit found with
        # zero callers anywhere in src/. Using it here is the point: a
        # component whose only exercise is its own unit test agrees with its
        # own fixtures by construction.
        self.builder = MultiTimeframeBuilder(
            symbol=SYMBOL,
            calendar=MarketCalendar(),
            timeframes=(Timeframe.M5, Timeframe.M15, Timeframe.H1),
        )
        self.bars_built = 0
        self.engine = IndicatorEngine()
        self.opening_range = OpeningRange(symbol=SYMBOL, trade_date=dt.date(2026, 8, 20))
        self.rejected = 0
        self.last_price: Decimal | None = None

    def warm_up(self) -> object:
        """E06-S03: load history before the feed connects.

        Also the point at which the E05/E06 dependency cycle is resolved in
        practice — the ATR the outlier filter wants is the PREVIOUS session's,
        established here, not one derived from today's ticks.
        """
        history = {
            (SYMBOL, timeframe): _history(260, timeframe) for timeframe in self.engine.timeframes
        }
        report = warm_up_symbols(self.engine, history)
        atr = self.engine.set_for(SYMBOL, Timeframe.M5).indicators["atr_14"]
        if atr.value is not None:
            last_close = history[(SYMBOL, Timeframe.M5)][-1].close
            self.cleaner.outliers.set_atr_pct(
                TOKEN, Decimal(repr(atr.value)) / last_close * Decimal(100)
            )
        return report

    def feed(self, raw: RawTick) -> None:
        tick = self.cleaner.process(raw, SYMBOL, now=raw.exchange_timestamp)
        if tick is None:
            self.rejected += 1
            return
        self.last_price = tick.ltp
        self.opening_range.update(tick)
        for bar in self.builder.add(tick):
            self.bars_built += 1
            self.engine.update(bar)

    def seal_opening_range(self) -> None:
        self.opening_range.seal()

    def context(self, **overrides) -> EvalContext:
        assert self.last_price is not None
        base: dict = {
            "symbol": SYMBOL,
            "now": SESSION_OPEN + dt.timedelta(hours=2),
            "timeframe": Timeframe.M5,
            "direction": Direction.LONG,
            "last_price": self.last_price,
            "snapshot": self.engine.snapshot(SYMBOL),
            "opening_range": self.opening_range,
            "regime": Regime.TRENDING,
        }
        base.update(overrides)
        return EvalContext(**base)


@pytest.fixture(scope="module")
def evaluator() -> StrategyEvaluator:
    return StrategyEvaluator(compile_strategy(load_strategy_yaml(STRATEGY)))


@pytest.fixture(scope="module")
def run() -> Pipeline:
    pipeline = Pipeline()
    pipeline.warm_up()
    for i, raw in enumerate(_session_ticks()):
        if i == 180:  # 15 minutes in
            pipeline.seal_opening_range()
        pipeline.feed(raw)
    return pipeline


class TestTheChainHoldsTogether:
    def test_ticks_became_bars(self, run: Pipeline) -> None:
        assert run.bars_built > 0

    def test_bars_reached_the_indicators(self, run: Pipeline) -> None:
        assert run.engine.set_for(SYMBOL, Timeframe.M5).bars_seen > 0

    def test_the_opening_range_sealed_and_is_usable(self, run: Pipeline) -> None:
        assert run.opening_range.is_usable
        assert run.opening_range.high is not None and run.opening_range.low is not None

    def test_the_snapshot_carries_history_for_the_crossover_primitives(self, run: Pipeline) -> None:
        """The interface added for ``ma_crossover`` has to survive the real
        path, not just a hand-built snapshot."""
        snapshot = run.engine.snapshot(SYMBOL)
        assert snapshot.value_ago(Timeframe.M5, "ema_20", 1) is not None

    def test_a_context_builds_from_real_pipeline_output(self, run: Pipeline) -> None:
        assert run.context().last_price > 0


class TestATriggerComesOutOfTheOtherEnd:
    def test_the_strategy_fires_on_a_rising_session(
        self, run: Pipeline, evaluator: StrategyEvaluator
    ) -> None:
        trigger = evaluator.fire(run.context(), correlation_id=CID)
        assert trigger is not None, evaluator.evaluate(run.context()).reason()
        assert trigger.symbol == SYMBOL
        assert trigger.strategy_id == "e2e_breakout"

    def test_the_stop_is_a_placeable_nse_price(
        self, run: Pipeline, evaluator: StrategyEvaluator
    ) -> None:
        """The whole point of tick snapping, verified against an ATR that came
        from cleaned ticks rather than from a fixture."""
        trigger = evaluator.fire(run.context(), correlation_id=CID)
        assert trigger is not None
        assert trigger.suggested_stop % Decimal("0.05") == 0
        assert -trigger.suggested_stop.as_tuple().exponent <= 4

    def test_the_stop_is_below_the_entry(self, run: Pipeline, evaluator: StrategyEvaluator) -> None:
        trigger = evaluator.fire(run.context(), correlation_id=CID)
        assert trigger is not None and trigger.suggested_stop < trigger.trigger_price

    def test_the_risk_per_share_is_sane(self, run: Pipeline, evaluator: StrategyEvaluator) -> None:
        """A stop 40% away is arithmetically valid and means something upstream
        is wrong — the kind of thing only an end-to-end run shows."""
        trigger = evaluator.fire(run.context(), correlation_id=CID)
        assert trigger is not None
        risk_pct = trigger.stop_distance / trigger.trigger_price * 100
        assert Decimal("0.05") < risk_pct < Decimal("10"), f"stop is {risk_pct}% away"

    def test_the_same_session_replays_identically(
        self, run: Pipeline, evaluator: StrategyEvaluator
    ) -> None:
        """Determinism across the whole chain, which is what makes a backtest a
        replay rather than an approximation."""
        first = evaluator.fire(run.context(), correlation_id=CID)
        second = evaluator.fire(run.context(), correlation_id=CID)
        assert first == second


class TestBadDataDoesNotReachTheStrategy:
    def test_a_poison_print_is_rejected_before_it_can_move_a_bar(self) -> None:
        """E05-S04's criterion, checked one layer up: a single injected bad
        print must not change any indicator value."""
        clean, dirty = Pipeline(), Pipeline()
        clean.warm_up()
        dirty.warm_up()
        ticks = _session_ticks(600)
        for i, raw in enumerate(ticks):
            if i == 180:
                clean.seal_opening_range()
                dirty.seal_opening_range()
            clean.feed(raw)
            dirty.feed(raw)
            if i == 300:
                # A 50x spike, the classic bad print.
                dirty.feed(_tick("50000.00", offset_s=i * 5 + 1, volume=raw.volume + 10))

        assert dirty.rejected > clean.rejected, "the spike was not rejected"
        for name in ("ema_20", "ema_50", "atr_14", "rsi_14"):
            assert clean.engine.snapshot(SYMBOL).value(Timeframe.M5, name) == dirty.engine.snapshot(
                SYMBOL
            ).value(Timeframe.M5, name), f"{name} moved because of one bad print"

    def test_a_feed_gap_stops_the_strategy_firing(
        self, run: Pipeline, evaluator: StrategyEvaluator
    ) -> None:
        """Found by running this chain for the first time: the evaluator TRADED
        through a twelve-minute feed gap.

        mark_stale sets IndicatorSet.stale, which clears is_ready, which clears
        all_ready — and nothing consulted all_ready. A strategy reading only a
        warm ema_20 never noticed that every indicator beside it was computed
        across a hole in the data. That is the "data stale -> block entries"
        invariant, and it was not held.
        """
        assert evaluator.fire(run.context(), correlation_id=CID) is not None

        run.engine.mark_stale(SYMBOL, "feed gap 12m")
        try:
            assert not run.engine.snapshot(SYMBOL).all_ready
            assert evaluator.fire(run.context(), correlation_id=CID) is None
        finally:
            for timeframe in run.engine.timeframes:
                run.engine.set_for(SYMBOL, timeframe).clear_stale()

        assert evaluator.fire(run.context(), correlation_id=CID) is not None

    def test_warm_up_made_the_symbol_ready(self, run: Pipeline) -> None:
        """A 200-EMA on 5-minute bars needs sixteen hours of trading, so no
        session warms it from ticks. Without warm-up the symbol is never ready
        and the all_ready gate blocks every trade — correctly."""
        assert run.engine.snapshot(SYMBOL).all_ready

    def test_without_warm_up_nothing_can_fire(self, evaluator: StrategyEvaluator) -> None:
        cold = Pipeline()  # deliberately NOT warmed
        for i, raw in enumerate(_session_ticks(600)):
            if i == 180:
                cold.seal_opening_range()
            cold.feed(raw)
        assert not cold.engine.snapshot(SYMBOL).all_ready
        assert evaluator.fire(cold.context(), correlation_id=CID) is None

    def test_an_unsealed_opening_range_blocks_the_breakout(
        self, run: Pipeline, evaluator: StrategyEvaluator
    ) -> None:
        """Before 09:30 the level does not exist, so the condition is UNKNOWN
        and the entry cannot fire — rather than treating a missing level as
        zero, which every price breaks."""
        unsealed = OpeningRange(symbol=SYMBOL, trade_date=dt.date(2026, 8, 20))
        decision = evaluator.evaluate(run.context(opening_range=unsealed))
        assert decision.outcome is None
        assert not decision.fired

    def test_a_wrong_regime_blocks_the_trade_end_to_end(
        self, run: Pipeline, evaluator: StrategyEvaluator
    ) -> None:
        assert evaluator.fire(run.context(regime=Regime.RANGEBOUND), correlation_id=CID) is None

    def test_cold_indicators_cannot_produce_a_trigger(self, evaluator: StrategyEvaluator) -> None:
        """A symbol that has only just started ticking has no EMA, so the
        condition is UNKNOWN and no Trigger is produced."""
        cold = Pipeline()
        for raw in _session_ticks(30):
            cold.feed(raw)
        cold.seal_opening_range()
        assert evaluator.fire(cold.context(), correlation_id=CID) is None


# ---------------------------------------------------------------------------
# E14-S02 — the chain now continues into the risk engine
# ---------------------------------------------------------------------------


class TestTheTriggerReachesARiskDecision:
    """QA-2 for E14-S02: the pre-condition checks against a REAL Trigger.

    The unit tests build a Recommendation by hand. This one takes the trigger
    that came out of the actual pipeline -- cleaned ticks, real bars, warmed
    indicators, a compiled strategy -- and puts it through the engine with the
    real calendar and the real configured windows.

    That matters because the seam has two sides. A hand-built recommendation
    agrees with whatever the test author imagined; this one carries the
    timestamps, prices and correlation id the system actually produced, and a
    mismatch in any of them shows up here rather than in production.
    """

    #: The AI review is constructed directly -- E10 does not exist yet, and
    #: E13-S03 is what will produce one for real. Everything else on this path
    #: is the genuine article.
    @staticmethod
    def _review() -> AIReview:
        return AIReview(
            verdict=AIVerdict.CONFIRM,
            confidence=Decimal("0.80"),
            timeframe_agreement=3,
            thesis_alignment="aligned",
            rationale="integration probe",
            model_used="test-harness",
            latency_ms=0,
        )

    def _recommendation(self, run, evaluator) -> Recommendation:
        trigger = evaluator.fire(run.context(), correlation_id=CID)
        assert trigger is not None, "the pipeline must fire for this seam to be testable"
        return Recommendation.build(trigger, self._review(), now=run.context().now)

    @staticmethod
    def _calendar() -> MarketCalendar:
        status = load_holidays_with_status(
            str(Path(__file__).resolve().parents[2] / "config" / "nse_holidays.yaml")
        )
        return MarketCalendar(status.dates, covers_years=status.covers_years)

    def _engine(self, audit=None) -> RiskEngine:
        return RiskEngine(
            checks=build_precondition_checks(self._calendar(), NO_TRADE_WINDOWS),
            audit=audit,
        )

    @staticmethod
    def _risk_ctx(**overrides) -> RiskContext:
        # 2026-08-20 is a Thursday and not a holiday. 05:00 UTC is 10:30 IST --
        # mid-session, clear of both configured blackouts.
        base: dict = {
            "now": _dt.datetime(2026, 8, 20, 5, 0, tzinfo=_dt.UTC),
            "squareoff_deadline": _dt.datetime(2026, 8, 20, 9, 40, tzinfo=_dt.UTC),
            "capital": Decimal("500000"),
            "slots_total": 5,
            "slots_used": 0,
            # E14-S03. `()` means "checked, and clean" -- NOT None, which means
            # eligibility was never established and must reject.
            "symbol_restrictions": (),
        }
        base.update(overrides)
        return RiskContext(**base)

    def test_a_real_trigger_becomes_a_recommendation(self, run, evaluator) -> None:
        rec = self._recommendation(run, evaluator)
        assert rec.symbol == SYMBOL
        assert rec.correlation_id == CID

    def test_the_recommendation_still_carries_no_sizing(self, run, evaluator) -> None:
        """Invariant 1, checked at the seam rather than on the type. Sizing
        arrives downstream in E14-S07; if it ever appeared here the
        AI/deterministic boundary would have moved without anyone deciding to."""
        rec = self._recommendation(run, evaluator)
        banned = {"quantity", "capital_at_risk", "stop_price", "notional"}
        assert not (set(type(rec).model_fields) & banned)

    def test_a_clean_session_clears_all_four_preconditions(self, run, evaluator) -> None:
        """The control for the whole seam."""
        decision = self._engine().evaluate(self._recommendation(run, evaluator), self._risk_ctx())
        assert decision.checks_passed == list(PRECONDITION_ORDER)
        # No sizer configured, so the engine correctly refuses at the last gate
        # rather than approving. That IS the fail-closed behaviour.
        assert not decision.approved
        assert "no sizer" in (decision.detail or "")

    def test_the_kill_switch_stops_a_real_trigger(self, run, evaluator) -> None:
        decision = self._engine().evaluate(
            self._recommendation(run, evaluator), self._risk_ctx(kill_switch_active=True)
        )
        assert decision.reason is RejectReason.KILL_SWITCH_ACTIVE
        assert decision.checks_passed == []

    def test_a_dead_feed_service_stops_a_real_trigger(self, run, evaluator) -> None:
        """The degraded-dependency case the strategy layer cannot see: the
        evaluator fired happily, and the risk layer refuses because a component
        it depends on is down."""
        decision = self._engine().evaluate(
            self._recommendation(run, evaluator),
            self._risk_ctx(unhealthy_services=("ingest-svc",)),
        )
        assert decision.reason is RejectReason.HEALTH_GATE_FAILED
        assert "ingest-svc" in (decision.detail or "")

    def test_the_same_trigger_is_refused_after_hours(self, run, evaluator) -> None:
        """Same recommendation, different clock. The strategy has no opinion
        about the session; the risk layer does."""
        decision = self._engine().evaluate(
            self._recommendation(run, evaluator),
            self._risk_ctx(now=_dt.datetime(2026, 8, 20, 11, 0, tzinfo=_dt.UTC)),
        )
        assert decision.reason is RejectReason.OUTSIDE_TRADING_WINDOW

    def test_the_same_trigger_is_refused_in_the_closing_blackout(self, run, evaluator) -> None:
        decision = self._engine().evaluate(
            self._recommendation(run, evaluator),
            self._risk_ctx(now=_dt.datetime(2026, 8, 20, 9, 40, tzinfo=_dt.UTC)),
        )
        assert decision.reason is RejectReason.NO_TRADE_WINDOW
        assert "15:00-15:30" in (decision.detail or "")

    def test_every_decision_is_audited_with_the_correlation_id(self, run, evaluator) -> None:
        """The audit chain has to survive the whole way. Without the id the risk
        decision cannot be joined to the tick sequence that caused it."""
        written: list[dict] = []
        engine = self._engine(audit=written.append)
        engine.evaluate(
            self._recommendation(run, evaluator), self._risk_ctx(kill_switch_active=True)
        )
        assert len(written) == 1
        assert written[0]["correlation_id"] == CID
        assert written[0]["stage"] == "kill_switch"


class TestTheTriggerMeetsSymbolEligibility:
    """QA-2 for E14-S03: checks 5-7 against a REAL Trigger, wired behind the
    real pre-conditions.

    The seam this exercises is the one the unit tests structurally cannot see:
    the symbol the risk engine tests for eligibility is the symbol the
    *pipeline* produced, carried through cleaning, bars, indicators, the
    strategy runtime and `Recommendation.build`. A mismatch anywhere in that
    chain -- a renamed field, a symbol normalised on one side and not the
    other -- makes `ctx.holds(rec.symbol)` quietly answer False forever, and
    the already-held gate stops being a gate while every unit test still
    passes.
    """

    @staticmethod
    def _review() -> AIReview:
        return AIReview(
            verdict=AIVerdict.CONFIRM,
            confidence=Decimal("0.80"),
            timeframe_agreement=3,
            thesis_alignment="aligned",
            rationale="eligibility seam probe",
            model_used="test-harness",
            latency_ms=0,
        )

    def _recommendation(self, run, evaluator) -> Recommendation:
        trigger = evaluator.fire(run.context(), correlation_id=CID)
        assert trigger is not None, "the pipeline must fire for this seam to be testable"
        return Recommendation.build(trigger, self._review(), now=run.context().now)

    @staticmethod
    def _calendar() -> MarketCalendar:
        status = load_holidays_with_status(
            str(Path(__file__).resolve().parents[2] / "config" / "nse_holidays.yaml")
        )
        return MarketCalendar(status.dates, covers_years=status.covers_years)

    def _engine(self, audit=None) -> RiskEngine:
        """All seven built checks, in their real order."""
        return RiskEngine(
            checks=[
                *build_precondition_checks(self._calendar(), NO_TRADE_WINDOWS),
                *build_eligibility_checks(),
            ],
            audit=audit,
        )

    @staticmethod
    def _risk_ctx(**overrides) -> RiskContext:
        base: dict = {
            "now": _dt.datetime(2026, 8, 20, 5, 0, tzinfo=_dt.UTC),
            "squareoff_deadline": _dt.datetime(2026, 8, 20, 9, 40, tzinfo=_dt.UTC),
            "capital": Decimal("500000"),
            "slots_total": 5,
            "slots_used": 0,
            "symbol_restrictions": (),
        }
        base.update(overrides)
        return RiskContext(**base)

    def test_a_clean_real_trigger_clears_all_seven(self, run, evaluator) -> None:
        decision = self._engine().evaluate(self._recommendation(run, evaluator), self._risk_ctx())
        assert decision.checks_passed == [*PRECONDITION_ORDER, *ELIGIBILITY_ORDER]

    def test_a_restricted_symbol_stops_a_real_trigger(self, run, evaluator) -> None:
        decision = self._engine().evaluate(
            self._recommendation(run, evaluator),
            self._risk_ctx(symbol_restrictions=("T2T",)),
        )
        assert decision.reason is RejectReason.SYMBOL_NOT_TRADABLE
        assert "T2T" in (decision.detail or "")

    def test_unverified_eligibility_stops_a_real_trigger(self, run, evaluator) -> None:
        """The state an unwired E04 leaves. The pipeline fired, the strategy is
        happy, and the risk layer refuses because nobody established whether
        this instrument may be traded today."""
        decision = self._engine().evaluate(
            self._recommendation(run, evaluator),
            self._risk_ctx(symbol_restrictions=None),
        )
        assert not decision.approved
        assert decision.reason is RejectReason.RISK_ENGINE_FAULT

    def test_a_full_book_stops_a_real_trigger(self, run, evaluator) -> None:
        decision = self._engine().evaluate(
            self._recommendation(run, evaluator),
            self._risk_ctx(slots_total=5, slots_used=5),
        )
        assert decision.reason is RejectReason.NO_SLOT_AVAILABLE

    def test_the_symbol_the_pipeline_produced_is_the_one_matched_as_held(
        self, run, evaluator
    ) -> None:
        """The real point of doing this at the seam.

        The held position is built from `rec.symbol` -- whatever the pipeline
        actually emitted -- rather than from the SYMBOL constant. If the two
        ever diverge, this still holds, whereas a test that hardcoded the
        constant would pass while the gate silently matched nothing.
        """
        rec = self._recommendation(run, evaluator)
        held = OpenPosition(
            symbol=rec.symbol,
            direction=Direction.LONG,
            quantity=40,
            entry_price=rec.trigger_price,
            stop_price=rec.suggested_stop,
        )
        decision = self._engine().evaluate(rec, self._risk_ctx(open_positions=(held,)))
        assert decision.reason is RejectReason.ALREADY_HOLDING
        assert rec.symbol in (decision.detail or "")

    def test_holding_a_different_name_does_not_block_this_one(self, run, evaluator) -> None:
        """The control. A gate matching on the wrong thing -- or on nothing --
        would reject here too, and the test above alone could not tell."""
        rec = self._recommendation(run, evaluator)
        other = OpenPosition(
            symbol="SOMETHINGELSE",
            direction=Direction.LONG,
            quantity=10,
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
        )
        decision = self._engine().evaluate(
            rec, self._risk_ctx(slots_used=1, open_positions=(other,))
        )
        assert decision.checks_passed == [*PRECONDITION_ORDER, *ELIGIBILITY_ORDER]

    def test_a_precondition_still_wins_over_an_eligibility_failure(self, run, evaluator) -> None:
        """Order across the two groups, at the seam. With both a blackout and a
        banned symbol, an operator must see the blackout -- the more
        fundamental reason, and the one that explains every other candidate
        being refused at the same moment."""
        decision = self._engine().evaluate(
            self._recommendation(run, evaluator),
            self._risk_ctx(
                now=_dt.datetime(2026, 8, 20, 9, 40, tzinfo=_dt.UTC),
                symbol_restrictions=("T2T",),
            ),
        )
        assert decision.reason is RejectReason.NO_TRADE_WINDOW

    def test_the_audit_records_which_eligibility_check_stopped_it(self, run, evaluator) -> None:
        written: list[dict] = []
        self._engine(audit=written.append).evaluate(
            self._recommendation(run, evaluator),
            self._risk_ctx(symbol_restrictions=("GSM_3",)),
        )
        assert len(written) == 1
        assert written[0]["stage"] == "symbol_tradable"
        assert written[0]["correlation_id"] == CID
        assert written[0]["payload"]["checks_passed"] == list(PRECONDITION_ORDER)


class TestTheTriggerMeetsPortfolioExposure:
    """QA-2 for E14-S04: checks 8-10 behind the six that precede them, against
    a REAL Trigger.

    The seam that only exists here: `ctx.correlations` and
    `ctx.open_positions` are keyed by SYMBOL, and the symbol the guard looks up
    is the one the pipeline emitted. The unit tests build both sides from the
    same string, so they agree by construction. If the pipeline ever emitted a
    symbol in a different form than the pre-market matrix keys, every lookup
    would miss -- and a miss is 'unknown', which now refuses, so the failure
    would be loud rather than silent. This asserts that loudness.
    """

    @staticmethod
    def _review() -> AIReview:
        return AIReview(
            verdict=AIVerdict.CONFIRM,
            confidence=Decimal("0.80"),
            timeframe_agreement=3,
            thesis_alignment="aligned",
            rationale="exposure seam probe",
            model_used="test-harness",
            latency_ms=0,
        )

    def _recommendation(self, run, evaluator) -> Recommendation:
        trigger = evaluator.fire(run.context(), correlation_id=CID)
        assert trigger is not None, "the pipeline must fire for this seam to be testable"
        return Recommendation.build(trigger, self._review(), now=run.context().now)

    @staticmethod
    def _calendar() -> MarketCalendar:
        status = load_holidays_with_status(
            str(Path(__file__).resolve().parents[2] / "config" / "nse_holidays.yaml")
        )
        return MarketCalendar(status.dates, covers_years=status.covers_years)

    def _engine(self, audit=None) -> RiskEngine:
        """All ten built checks, in their real order."""
        return RiskEngine(
            checks=[
                *build_precondition_checks(self._calendar(), NO_TRADE_WINDOWS),
                *build_eligibility_checks(),
                *build_exposure_checks(
                    max_correlated_positions=2,
                    correlation_threshold=Decimal("0.7"),
                    max_sector_exposure_pct=Decimal("40"),
                    max_net_directional_exposure_pct=Decimal("60"),
                ),
            ],
            audit=audit,
        )

    @staticmethod
    def _risk_ctx(**overrides) -> RiskContext:
        base: dict = {
            "now": _dt.datetime(2026, 8, 20, 5, 0, tzinfo=_dt.UTC),
            "squareoff_deadline": _dt.datetime(2026, 8, 20, 9, 40, tzinfo=_dt.UTC),
            "capital": Decimal("500000"),
            "slots_total": 5,
            "slots_used": 0,
            "symbol_restrictions": (),
            "symbol_sector": "IT",
        }
        base.update(overrides)
        return RiskContext(**base)

    @staticmethod
    def _held(symbol: str, notional: str, sector: str, direction=Direction.LONG):
        price = Decimal("100")
        return OpenPosition(
            symbol=symbol,
            direction=direction,
            quantity=int(Decimal(notional) / price),
            entry_price=price,
            stop_price=Decimal("95") if direction is Direction.LONG else Decimal("105"),
            sector=sector,
        )

    def test_a_clean_real_trigger_clears_all_ten(self, run, evaluator) -> None:
        decision = self._engine().evaluate(self._recommendation(run, evaluator), self._risk_ctx())
        assert decision.checks_passed == [
            *PRECONDITION_ORDER,
            *ELIGIBILITY_ORDER,
            *EXPOSURE_ORDER,
        ]

    def test_a_correlated_book_stops_a_real_trigger(self, run, evaluator) -> None:
        rec = self._recommendation(run, evaluator)
        ctx = self._risk_ctx(
            slots_used=2,
            open_positions=(
                self._held("PNB", "50000", "PSU_BANK"),
                self._held("CANBK", "50000", "PSU_BANK"),
            ),
            correlations={"PNB": Decimal("0.91"), "CANBK": Decimal("0.88")},
        )
        decision = self._engine().evaluate(rec, ctx)
        assert decision.reason is RejectReason.CORRELATION_LIMIT

    def test_a_missing_correlation_for_a_held_name_stops_a_real_trigger(
        self, run, evaluator
    ) -> None:
        """The state a failed pre-market matrix job leaves. It must refuse, and
        it must refuse as a FAULT rather than as a business rejection."""
        rec = self._recommendation(run, evaluator)
        ctx = self._risk_ctx(
            slots_used=1,
            open_positions=(self._held("PNB", "50000", "PSU_BANK"),),
            correlations={},
        )
        decision = self._engine().evaluate(rec, ctx)
        assert not decision.approved
        assert decision.reason is RejectReason.RISK_ENGINE_FAULT

    def test_a_saturated_sector_stops_a_real_trigger(self, run, evaluator) -> None:
        rec = self._recommendation(run, evaluator)
        ctx = self._risk_ctx(
            symbol_sector="IT",
            slots_used=1,
            open_positions=(self._held("TCS", "200000", "IT"),),
            correlations={"TCS": Decimal("0.2")},
        )
        decision = self._engine().evaluate(rec, ctx)
        assert decision.reason is RejectReason.SECTOR_EXPOSURE_LIMIT

    def test_an_unclassified_held_position_stops_a_real_trigger(self, run, evaluator) -> None:
        """The hole from the far side: a position with no sector contributes
        nothing to any sector total, so the cap would never bind."""
        rec = self._recommendation(run, evaluator)
        ctx = self._risk_ctx(
            slots_used=1,
            open_positions=(self._held("MYSTERY", "400000", None),),  # type: ignore[arg-type]
            correlations={"MYSTERY": Decimal("0.1")},
        )
        decision = self._engine().evaluate(rec, ctx)
        assert not decision.approved
        assert decision.reason is RejectReason.RISK_ENGINE_FAULT

    def test_a_one_sided_book_stops_a_real_trigger(self, run, evaluator) -> None:
        rec = self._recommendation(run, evaluator)
        ctx = self._risk_ctx(
            symbol_sector="PHARMA",
            slots_used=2,
            open_positions=(
                self._held("TCS", "160000", "IT"),
                self._held("RELIANCE", "160000", "ENERGY"),
            ),
            correlations={"TCS": Decimal("0.1"), "RELIANCE": Decimal("0.2")},
        )
        decision = self._engine().evaluate(rec, ctx)
        assert decision.reason is RejectReason.NET_EXPOSURE_LIMIT

    def test_a_hedged_book_of_the_same_size_does_not(self, run, evaluator) -> None:
        """The control for the test above. Same gross exposure, opposite sides:
        that is not a directional bet, and the check that measures direction
        must not refuse it."""
        rec = self._recommendation(run, evaluator)
        ctx = self._risk_ctx(
            symbol_sector="PHARMA",
            slots_used=2,
            open_positions=(
                self._held("TCS", "160000", "IT"),
                self._held("RELIANCE", "160000", "ENERGY", Direction.SHORT),
            ),
            correlations={"TCS": Decimal("0.1"), "RELIANCE": Decimal("0.2")},
        )
        decision = self._engine().evaluate(rec, ctx)
        assert decision.checks_passed == [
            *PRECONDITION_ORDER,
            *ELIGIBILITY_ORDER,
            *EXPOSURE_ORDER,
        ]

    def test_an_earlier_group_still_wins(self, run, evaluator) -> None:
        """Order across all three groups. With a blackout, a banned symbol AND
        a saturated sector, an operator must see the blackout."""
        rec = self._recommendation(run, evaluator)
        ctx = self._risk_ctx(
            now=_dt.datetime(2026, 8, 20, 9, 40, tzinfo=_dt.UTC),
            symbol_restrictions=("T2T",),
            symbol_sector="IT",
            slots_used=1,
            open_positions=(self._held("TCS", "300000", "IT"),),
            correlations={"TCS": Decimal("0.95")},
        )
        assert self._engine().evaluate(rec, ctx).reason is RejectReason.NO_TRADE_WINDOW

    def test_eligibility_still_wins_over_exposure(self, run, evaluator) -> None:
        rec = self._recommendation(run, evaluator)
        ctx = self._risk_ctx(
            symbol_restrictions=("GSM_3",),
            symbol_sector="IT",
            slots_used=1,
            open_positions=(self._held("TCS", "300000", "IT"),),
            correlations={"TCS": Decimal("0.95")},
        )
        assert self._engine().evaluate(rec, ctx).reason is RejectReason.SYMBOL_NOT_TRADABLE

    def test_the_audit_records_which_exposure_check_stopped_it(self, run, evaluator) -> None:
        written: list[dict] = []
        self._engine(audit=written.append).evaluate(
            self._recommendation(run, evaluator),
            self._risk_ctx(
                symbol_sector="IT",
                slots_used=1,
                open_positions=(self._held("TCS", "250000", "IT"),),
                correlations={"TCS": Decimal("0.2")},
            ),
        )
        assert len(written) == 1
        assert written[0]["stage"] == "sector_exposure"
        assert written[0]["correlation_id"] == CID
        assert written[0]["payload"]["checks_passed"] == [
            *PRECONDITION_ORDER,
            *ELIGIBILITY_ORDER,
            "correlation",
        ]

    def test_the_matrix_computed_from_real_bars_feeds_the_guard(self, run, evaluator) -> None:
        """The other seam, and the one that closes the loop: the correlation
        module's OUTPUT is the guard's INPUT. Built here from the pipeline's
        own bars rather than from hand-written numbers, so the two halves meet
        the way they will in production.
        """
        rec = self._recommendation(run, evaluator)
        # The warm-up history the indicators were built from -- real bars from
        # this test's own pipeline, not numbers written to make the test pass.
        closes = [b.close for b in _history(260, Timeframe.M5)]
        book = {rec.symbol: closes, "TWIN": closes}
        matrix = correlations_against(rec.symbol, book, against=["TWIN"])
        assert matrix["TWIN"] == pytest.approx(Decimal("1"), abs=Decimal("0.0001")), (
            "a symbol against a copy of itself must correlate at 1"
        )
        ctx = self._risk_ctx(
            slots_used=1,
            open_positions=(self._held("TWIN", "50000", "IT"),),
            correlations=matrix,
        )
        # One correlated name, limit is 2 -> allowed, and that is the point:
        # the values flow through and mean what the guard thinks they mean.
        assert self._engine().evaluate(rec, ctx).checks_passed[-1] == "net_exposure"


class TestTheTriggerMeetsTheLossLimits:
    """QA-2 for E14-S05: checks 11-12 behind the ten that precede them, against
    a REAL Trigger.

    The seam these add is different in kind from the earlier ones. Checks 1-10
    ask about the market, the instrument and the book; these ask about the
    SESSION'S HISTORY. They are the first checks whose answer depends on what
    the system already did today, which means they are the first that can be
    wrong in a way a single evaluation cannot reveal.
    """

    @staticmethod
    def _review() -> AIReview:
        return AIReview(
            verdict=AIVerdict.CONFIRM,
            confidence=Decimal("0.80"),
            timeframe_agreement=3,
            thesis_alignment="aligned",
            rationale="loss limit seam probe",
            model_used="test-harness",
            latency_ms=0,
        )

    def _recommendation(self, run, evaluator) -> Recommendation:
        trigger = evaluator.fire(run.context(), correlation_id=CID)
        assert trigger is not None, "the pipeline must fire for this seam to be testable"
        return Recommendation.build(trigger, self._review(), now=run.context().now)

    @staticmethod
    def _calendar() -> MarketCalendar:
        status = load_holidays_with_status(
            str(Path(__file__).resolve().parents[2] / "config" / "nse_holidays.yaml")
        )
        return MarketCalendar(status.dates, covers_years=status.covers_years)

    def _engine(self, audit=None) -> RiskEngine:
        """All TWELVE built checks, in their real order."""
        return RiskEngine(
            checks=[
                *build_precondition_checks(self._calendar(), NO_TRADE_WINDOWS),
                *build_eligibility_checks(),
                *build_exposure_checks(
                    max_correlated_positions=2,
                    correlation_threshold=Decimal("0.7"),
                    max_sector_exposure_pct=Decimal("40"),
                    max_net_directional_exposure_pct=Decimal("60"),
                ),
                *build_loss_checks(
                    max_daily_loss_pct=Decimal("3.0"),
                    consecutive_loss_halt=3,
                ),
            ],
            audit=audit,
        )

    @staticmethod
    def _risk_ctx(**overrides) -> RiskContext:
        base: dict = {
            "now": _dt.datetime(2026, 8, 20, 5, 0, tzinfo=_dt.UTC),
            "squareoff_deadline": _dt.datetime(2026, 8, 20, 9, 40, tzinfo=_dt.UTC),
            "capital": Decimal("500000"),
            "slots_total": 5,
            "slots_used": 0,
            "symbol_restrictions": (),
            "symbol_sector": "IT",
        }
        base.update(overrides)
        return RiskContext(**base)

    def test_a_clean_real_trigger_clears_all_twelve(self, run, evaluator) -> None:
        decision = self._engine().evaluate(self._recommendation(run, evaluator), self._risk_ctx())
        assert decision.checks_passed == [
            *PRECONDITION_ORDER,
            *ELIGIBILITY_ORDER,
            *EXPOSURE_ORDER,
            *LOSS_ORDER,
        ]

    def test_a_breached_daily_loss_stops_a_real_trigger(self, run, evaluator) -> None:
        decision = self._engine().evaluate(
            self._recommendation(run, evaluator),
            self._risk_ctx(realised_pnl_today=Decimal("-16000")),
        )
        assert decision.reason is RejectReason.DAILY_LOSS_LIMIT

    def test_a_profitable_day_of_the_same_size_does_not(self, run, evaluator) -> None:
        """The control, at the seam. A sign inversion would halt the system on
        its best days, and every one of the loss tests above would still pass."""
        decision = self._engine().evaluate(
            self._recommendation(run, evaluator),
            self._risk_ctx(realised_pnl_today=Decimal("16000")),
        )
        assert decision.checks_passed[-1] == "consecutive_loss"

    def test_a_latched_halt_stops_a_real_trigger_that_would_otherwise_pass(
        self, run, evaluator
    ) -> None:
        """Everything else about this candidate is fine and the live P&L has
        recovered. Only the latch stands between it and an entry on a day a
        risk limit already fired."""
        decision = self._engine().evaluate(
            self._recommendation(run, evaluator),
            self._risk_ctx(realised_pnl_today=Decimal("-500"), daily_loss_halted=True),
        )
        assert decision.reason is RejectReason.DAILY_LOSS_LIMIT
        assert "already" in (decision.detail or "")

    def test_a_consecutive_streak_stops_a_real_trigger(self, run, evaluator) -> None:
        decision = self._engine().evaluate(
            self._recommendation(run, evaluator), self._risk_ctx(consecutive_losses=3)
        )
        assert decision.reason is RejectReason.CONSECUTIVE_LOSS_LIMIT

    def test_the_loss_checks_run_last(self, run, evaluator) -> None:
        """Position in the pipeline. A blocked symbol on a losing day must
        report the symbol -- the more specific reason -- not the loss."""
        decision = self._engine().evaluate(
            self._recommendation(run, evaluator),
            self._risk_ctx(
                symbol_restrictions=("T2T",),
                realised_pnl_today=Decimal("-20000"),
                consecutive_losses=5,
            ),
        )
        assert decision.reason is RejectReason.SYMBOL_NOT_TRADABLE

    def test_the_audit_records_which_loss_check_stopped_it(self, run, evaluator) -> None:
        written: list[dict] = []
        self._engine(audit=written.append).evaluate(
            self._recommendation(run, evaluator),
            self._risk_ctx(consecutive_losses=4),
        )
        assert len(written) == 1
        assert written[0]["stage"] == "consecutive_loss"
        assert written[0]["correlation_id"] == CID
        assert written[0]["payload"]["checks_passed"] == [
            *PRECONDITION_ORDER,
            *ELIGIBILITY_ORDER,
            *EXPOSURE_ORDER,
            "daily_loss",
        ]

    def test_the_loss_limits_never_look_at_the_candidate(self, run, evaluator) -> None:
        """These are session-wide conditions. Whatever the pipeline produced,
        a halted day halts it -- and the rejection must not name the symbol as
        if the symbol were the problem."""
        rec = self._recommendation(run, evaluator)
        decision = self._engine().evaluate(
            rec, self._risk_ctx(realised_pnl_today=Decimal("-16000"))
        )
        assert rec.symbol not in (decision.detail or "")


class TestTheTriggerMeetsAllFourteenChecks:
    """QA-2 for E14-S06, and the first time the WHOLE risk pipeline has existed.

    Every previous seam class ran a partial engine because the remaining checks
    were unwritten. This one runs all fourteen against the Trigger the pipeline
    actually produced, which makes it the first test that can answer: does a
    real candidate survive the complete gauntlet, and in the right order?
    """

    @staticmethod
    def _review() -> AIReview:
        return AIReview(
            verdict=AIVerdict.CONFIRM,
            confidence=Decimal("0.80"),
            timeframe_agreement=3,
            thesis_alignment="aligned",
            rationale="full pipeline probe",
            model_used="test-harness",
            latency_ms=0,
        )

    def _recommendation(self, run, evaluator) -> Recommendation:
        trigger = evaluator.fire(run.context(), correlation_id=CID)
        assert trigger is not None, "the pipeline must fire for this seam to be testable"
        return Recommendation.build(trigger, self._review(), now=run.context().now)

    @staticmethod
    def _calendar() -> MarketCalendar:
        status = load_holidays_with_status(
            str(Path(__file__).resolve().parents[2] / "config" / "nse_holidays.yaml")
        )
        return MarketCalendar(status.dates, covers_years=status.covers_years)

    def _engine(self, audit=None) -> RiskEngine:
        """All FOURTEEN checks, in LOW_LEVEL_ARCHITECTURE 5.7's order."""
        return RiskEngine(
            checks=[
                *build_precondition_checks(self._calendar(), NO_TRADE_WINDOWS),
                *build_eligibility_checks(),
                *build_exposure_checks(
                    max_correlated_positions=2,
                    correlation_threshold=Decimal("0.7"),
                    max_sector_exposure_pct=Decimal("40"),
                    max_net_directional_exposure_pct=Decimal("60"),
                ),
                *build_loss_checks(max_daily_loss_pct=Decimal("3.0"), consecutive_loss_halt=3),
                *build_margin_timing_checks(min_minutes_to_squareoff=30),
            ],
            audit=audit,
        )

    @staticmethod
    def _risk_ctx(**overrides) -> RiskContext:
        base: dict = {
            "now": _dt.datetime(2026, 8, 20, 5, 0, tzinfo=_dt.UTC),  # 10:30 IST
            "squareoff_deadline": _dt.datetime(2026, 8, 20, 9, 35, tzinfo=_dt.UTC),
            "capital": Decimal("500000"),
            "slots_total": 5,
            "slots_used": 0,
            "symbol_restrictions": (),
            "symbol_sector": "IT",
            "available_margin": Decimal("250000"),
            "margin_per_share": Decimal("240"),
        }
        base.update(overrides)
        return RiskContext(**base)

    def test_a_clean_real_trigger_clears_all_fourteen(self, run, evaluator) -> None:
        """The claim the whole epic has been building toward: a candidate the
        strategy produced from real ticks survives every risk gate."""
        decision = self._engine().evaluate(self._recommendation(run, evaluator), self._risk_ctx())
        assert decision.checks_passed == list(all_check_ids())
        assert len(decision.checks_passed) == 14

    def test_it_is_still_not_approved_because_there_is_no_sizer(self, run, evaluator) -> None:
        """Clearing fourteen checks is not an approval. The honest state of the
        system: E14-S07 does not exist, so the engine refuses rather than
        defaulting a quantity -- and it refuses as a FAULT, not as a business
        rejection, because a missing sizer is a broken engine."""
        decision = self._engine().evaluate(self._recommendation(run, evaluator), self._risk_ctx())
        assert not decision.approved
        assert decision.reason is RejectReason.RISK_ENGINE_FAULT
        assert "no sizer" in (decision.detail or "")

    def test_unknown_margin_stops_a_real_trigger(self, run, evaluator) -> None:
        decision = self._engine().evaluate(
            self._recommendation(run, evaluator), self._risk_ctx(available_margin=None)
        )
        assert not decision.approved
        assert decision.reason is RejectReason.RISK_ENGINE_FAULT

    def test_an_account_that_cannot_afford_one_share_stops_a_real_trigger(
        self, run, evaluator
    ) -> None:
        decision = self._engine().evaluate(
            self._recommendation(run, evaluator),
            self._risk_ctx(available_margin=Decimal("10"), margin_per_share=Decimal("240")),
        )
        assert decision.reason is RejectReason.INSUFFICIENT_MARGIN

    def test_too_little_runway_stops_a_real_trigger(self, run, evaluator) -> None:
        """14:45 IST against a 15:05 deadline: twenty minutes, inside the
        thirty-minute minimum, and still inside the tradable session."""
        decision = self._engine().evaluate(
            self._recommendation(run, evaluator),
            self._risk_ctx(now=_dt.datetime(2026, 8, 20, 9, 15, tzinfo=_dt.UTC)),
        )
        assert decision.reason is RejectReason.TOO_CLOSE_TO_SQUAREOFF

    def test_the_runway_check_catches_what_the_no_trade_window_does_not(
        self, run, evaluator
    ) -> None:
        """The case that justifies check 14 existing alongside check 4. At
        14:59 IST the blackout has not started -- so the no-trade window
        passes -- and a CAS name has six minutes of runway."""
        at_1459 = _dt.datetime(2026, 8, 20, 9, 29, tzinfo=_dt.UTC)
        decision = self._engine().evaluate(
            self._recommendation(run, evaluator), self._risk_ctx(now=at_1459)
        )
        assert decision.reason is RejectReason.TOO_CLOSE_TO_SQUAREOFF
        assert "no_trade_window" in decision.checks_passed, (
            "the blackout had not started, so check 4 must have passed -- "
            "otherwise this test is not exercising what it claims"
        )

    def test_an_earlier_group_still_wins_over_the_last_two(self, run, evaluator) -> None:
        """Order across all five groups. Everything is wrong at once; the
        operator must see the kill switch."""
        decision = self._engine().evaluate(
            self._recommendation(run, evaluator),
            self._risk_ctx(
                kill_switch_active=True,
                symbol_restrictions=("T2T",),
                realised_pnl_today=Decimal("-20000"),
                available_margin=Decimal("1"),
                now=_dt.datetime(2026, 8, 20, 9, 30, tzinfo=_dt.UTC),
            ),
        )
        assert decision.reason is RejectReason.KILL_SWITCH_ACTIVE
        assert decision.checks_passed == []

    def test_the_audit_records_the_last_check_when_it_is_the_one_that_stops_it(
        self, run, evaluator
    ) -> None:
        written: list[dict] = []
        self._engine(audit=written.append).evaluate(
            self._recommendation(run, evaluator),
            self._risk_ctx(now=_dt.datetime(2026, 8, 20, 9, 29, tzinfo=_dt.UTC)),
        )
        assert len(written) == 1
        assert written[0]["stage"] == "time_to_squareoff"
        assert written[0]["correlation_id"] == CID
        # Thirteen cleared before the last one refused.
        assert len(written[0]["payload"]["checks_passed"]) == 13

    def test_every_stage_the_engine_can_write_fits_the_audit_column(self, run, evaluator) -> None:
        """All fourteen ids against decision_log.stage's String(28), asserted
        against the ASSEMBLED engine rather than the constants -- so a check
        registered with an id that does not match its group constant would
        still be caught."""
        for check in self._engine().checks:
            assert len(check.id) <= 28, check.id


class TestTheTriggerBecomesAnApprovedOrder:
    """QA-2 for E14-S07: the first time this pipeline produces an APPROVAL.

    Every earlier seam class ended in a refusal, because there was no sizer and
    the engine correctly refuses rather than defaulting a quantity. This one
    closes the chain: ticks -> cleaning -> bars -> indicators -> strategy ->
    Trigger -> Recommendation -> fourteen checks -> a quantity and a stop.

    The seam that only exists here is the ATR one. The sizer divides the risk
    budget by an ATR the INDICATOR ENGINE produced, through
    `ATR.as_decimal()` -- not by a number a test author chose. If that boundary
    ever returned a float, or the wrong scale, the quantity would be wrong in a
    way no unit test using a hand-written Decimal could see.
    """

    @staticmethod
    def _review() -> AIReview:
        return AIReview(
            verdict=AIVerdict.CONFIRM,
            confidence=Decimal("0.80"),
            timeframe_agreement=3,
            thesis_alignment="aligned",
            rationale="sizing seam probe",
            model_used="test-harness",
            latency_ms=0,
        )

    def _recommendation(self, run, evaluator) -> Recommendation:
        trigger = evaluator.fire(run.context(), correlation_id=CID)
        assert trigger is not None, "the pipeline must fire for this seam to be testable"
        return Recommendation.build(trigger, self._review(), now=run.context().now)

    @staticmethod
    def _calendar() -> MarketCalendar:
        status = load_holidays_with_status(
            str(Path(__file__).resolve().parents[2] / "config" / "nse_holidays.yaml")
        )
        return MarketCalendar(status.dates, covers_years=status.covers_years)

    @staticmethod
    def _policy() -> SizingPolicy:
        return SizingPolicy(
            risk_pct=Decimal("1.0"),
            atr_multiplier_stop=Decimal("1.5"),
            max_position_pct=Decimal("20"),
            capital_per_slot_pct=Decimal("20"),
            target_r_multiple=Decimal("2.0"),
        )

    def _engine(self, audit=None) -> RiskEngine:
        """The complete engine: fourteen checks AND a sizer."""
        return RiskEngine(
            checks=[
                *build_precondition_checks(self._calendar(), NO_TRADE_WINDOWS),
                *build_eligibility_checks(),
                *build_exposure_checks(
                    max_correlated_positions=2,
                    correlation_threshold=Decimal("0.7"),
                    max_sector_exposure_pct=Decimal("40"),
                    max_net_directional_exposure_pct=Decimal("60"),
                ),
                *build_loss_checks(max_daily_loss_pct=Decimal("3.0"), consecutive_loss_halt=3),
                *build_margin_timing_checks(min_minutes_to_squareoff=30),
            ],
            sizer=build_sizer(self._policy()),
            audit=audit,
        )

    @staticmethod
    def _atr_from_the_engine(run) -> Decimal:
        """The ATR the INDICATOR ENGINE computed, through the money-path
        boundary. Not a number chosen to make the test pass."""
        atr = run.engine.set_for(SYMBOL, Timeframe.M5).indicators["atr_14"]
        value = atr.as_decimal()
        assert value is not None, "the pipeline must have a warmed ATR"
        return value

    def _risk_ctx(self, run, **overrides) -> RiskContext:
        base: dict = {
            "now": _dt.datetime(2026, 8, 20, 5, 0, tzinfo=_dt.UTC),
            "squareoff_deadline": _dt.datetime(2026, 8, 20, 9, 35, tzinfo=_dt.UTC),
            "capital": Decimal("500000"),
            "slots_total": 5,
            "slots_used": 0,
            "symbol_restrictions": (),
            "symbol_sector": "IT",
            "available_margin": Decimal("400000"),
            "margin_per_share": Decimal("240"),
            "atr": self._atr_from_the_engine(run),
            "lot_size": 1,
        }
        base.update(overrides)
        return RiskContext(**base)

    def test_a_real_trigger_becomes_an_approved_order(self, run, evaluator) -> None:
        """The chain closes. Nothing in this system had ever produced an
        approval before this story."""
        decision = self._engine().evaluate(
            self._recommendation(run, evaluator), self._risk_ctx(run)
        )
        assert decision.approved, f"refused: {decision.reason} — {decision.detail}"
        assert decision.checks_passed == list(all_check_ids())
        assert decision.sizing is not None
        assert decision.sizing.quantity > 0

    def test_the_risk_bound_holds_on_a_real_atr(self, run, evaluator) -> None:
        """AC1 against the indicator engine's own number rather than a chosen
        one. A units error at the `as_decimal()` boundary -- percent instead of
        rupees, say -- would blow the bound here and nowhere else."""
        decision = self._engine().evaluate(
            self._recommendation(run, evaluator), self._risk_ctx(run)
        )
        assert decision.sizing is not None
        budget = Decimal("500000") * Decimal("1.0") / 100
        assert decision.sizing.capital_at_risk <= budget

    def test_the_stop_is_a_placeable_price_on_the_losing_side(self, run, evaluator) -> None:
        rec = self._recommendation(run, evaluator)
        decision = self._engine().evaluate(rec, self._risk_ctx(run))
        assert decision.sizing is not None
        sizing = decision.sizing
        assert sizing.stop_price > 0
        assert sizing.stop_price < sizing.entry_price, "a long's stop must sit below entry"
        assert -sizing.stop_price.as_tuple().exponent <= 4, "not a valid Price"

    def test_the_entry_is_the_price_the_strategy_triggered_at(self, run, evaluator) -> None:
        """The seam in the other direction: the sizer's entry price is the
        Trigger's, carried through Recommendation unchanged."""
        rec = self._recommendation(run, evaluator)
        decision = self._engine().evaluate(rec, self._risk_ctx(run))
        assert decision.sizing is not None
        assert decision.sizing.entry_price == rec.trigger_price

    def test_the_approval_carries_a_binding_constraint(self, run, evaluator) -> None:
        decision = self._engine().evaluate(
            self._recommendation(run, evaluator), self._risk_ctx(run)
        )
        assert decision.sizing is not None
        assert decision.sizing.binding_constraint

    def test_a_failed_check_still_produces_no_sizing(self, run, evaluator) -> None:
        """The sizer runs only after every gate passes. A rejected candidate
        must carry no quantity at all -- not a quantity that something
        downstream might read."""
        decision = self._engine().evaluate(
            self._recommendation(run, evaluator),
            self._risk_ctx(run, kill_switch_active=True),
        )
        assert not decision.approved
        assert decision.sizing is None

    def test_an_account_that_cannot_afford_one_share_is_refused_by_check_13(
        self, run, evaluator
    ) -> None:
        """Refused before sizing ever runs. The gate exists so the sizer is
        never asked to divide a budget the account cannot fund."""
        decision = self._engine().evaluate(
            self._recommendation(run, evaluator),
            self._risk_ctx(run, available_margin=Decimal("100"), margin_per_share=Decimal("240")),
        )
        assert not decision.approved
        assert decision.reason is RejectReason.INSUFFICIENT_MARGIN
        assert decision.sizing is None

    def test_an_account_that_affords_exactly_one_share_gets_one(self, run, evaluator) -> None:
        """Recorded because it surprised me: 300 of margin at 240 per share is
        1.25, which floors to a ONE-share position and is APPROVED. That is
        correct — the risk bound holds and the margin cap is respected — and it
        is the honest consequence of flooring. Whether one share is worth the
        brokerage is a different question, and not this story's."""
        decision = self._engine().evaluate(
            self._recommendation(run, evaluator),
            self._risk_ctx(run, available_margin=Decimal("300"), margin_per_share=Decimal("240")),
        )
        assert decision.approved
        assert decision.sizing is not None
        assert decision.sizing.quantity == 1
        assert decision.sizing.binding_constraint == "margin_cap"

    def test_a_lot_too_large_to_fill_is_refused_as_a_size_problem(self, run, evaluator) -> None:
        """The sizer's own zero path, reached with margin to spare — so the
        rejection must NOT say insufficient margin."""
        decision = self._engine().evaluate(
            self._recommendation(run, evaluator), self._risk_ctx(run, lot_size=100000)
        )
        assert not decision.approved
        assert decision.reason is RejectReason.POSITION_TOO_SMALL
        assert "lot_rounding" in (decision.detail or "")

    def test_the_audit_records_the_approval_with_the_quantity(self, run, evaluator) -> None:
        """The audit chain has to carry the number that becomes an order."""
        written: list[dict] = []
        self._engine(audit=written.append).evaluate(
            self._recommendation(run, evaluator), self._risk_ctx(run)
        )
        assert len(written) == 1
        entry = written[0]
        assert entry["outcome"] == "approved"
        assert entry["stage"] == "risk_approved"
        assert entry["correlation_id"] == CID
        assert entry["payload"]["quantity"] > 0
        assert entry["payload"]["binding_constraint"]

    def test_a_volatile_session_sizes_smaller_than_a_quiet_one(self, run, evaluator) -> None:
        """The whole point of ATR sizing, at the seam: the same risk budget
        buys fewer shares when the instrument moves more. Both are measured
        against the SAME budget."""
        rec = self._recommendation(run, evaluator)
        real_atr = self._atr_from_the_engine(run)
        quiet = self._engine().evaluate(rec, self._risk_ctx(run, atr=real_atr / 4))
        volatile = self._engine().evaluate(rec, self._risk_ctx(run, atr=real_atr * 4))
        assert quiet.sizing is not None and volatile.sizing is not None
        assert quiet.sizing.quantity > volatile.sizing.quantity
        budget = Decimal("500000") * Decimal("1.0") / 100
        assert quiet.sizing.capital_at_risk <= budget
        assert volatile.sizing.capital_at_risk <= budget
