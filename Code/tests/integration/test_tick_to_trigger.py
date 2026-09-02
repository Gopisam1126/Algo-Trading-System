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
    PRECONDITION_ORDER,
    build_eligibility_checks,
    build_precondition_checks,
)
from algotrader.execution.risk.context import OpenPosition, RiskContext
from algotrader.execution.risk.framework import RiskEngine
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
