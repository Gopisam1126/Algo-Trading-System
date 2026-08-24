"""Walking a validated strategy tree.

This closes the implementation half of **E00-S08** — "strategy DSL with vetted
primitive registry", which is marked Closed and delivered only the declarations
— and unblocks **E12-S03** (whose acceptance criterion is that the *real*
strategy code runs in backtest) and **E13-S01** (which must "evaluate runnable
strategies"). It is not itself E13-S01: that story is the signal LOOP, which
also needs the plan symbols from E07 and the strategy registry from E12-S01.


This is what ``compile_strategy``'s docstring has always promised: *"the runtime
evaluator walks the validated tree; there is no code generation step at any
point."* The walk is an ordinary recursive descent over frozen Pydantic models.
There is no ``eval``, no ``exec``, no dynamic import, and no place to add one —
a primitive name is looked up in a dict of hand-written functions, and a name
that is not in that dict is an error rather than an opportunity.

Two decisions carry most of the safety here.

**Three-valued logic all the way to the top.** Conditions answer True, False or
UNKNOWN, and UNKNOWN propagates. An entry fires only on a definite True. The
alternative — collapsing UNKNOWN to False — is wrong in a specific and
dangerous way: ``none_of`` would read a condition that could not be computed as
a condition that is absent, so missing data would grant permission. That is a
fail-open in a system whose first invariant is fail-closed.

**A strategy that cannot be evaluated cannot be constructed.** The DSL lets a
strategy ask for ``ema_33`` or a daily timeframe; the engine carries neither.
Checking that at evaluation time would surface the mismatch at 09:20 with a
position forming. :class:`StrategyEvaluator` verifies capability in
``__init__``, so the failure happens when the strategy is loaded — and cannot
be skipped, because evaluating requires constructing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

from algotrader.common.enums import Direction, Timeframe
from algotrader.common.models.trading import Trigger
from algotrader.indicators.engine import HISTORICAL_INDICATORS, IndicatorEngine, default_indicators
from algotrader.strategy.context import EvalContext
from algotrader.strategy.dsl import REGISTRY, Condition, ConditionGroup, StrategyDocument
from algotrader.strategy.primitives.evaluators import (
    CONDITION_EVALUATORS,
    DEFERRED_TO_POSITION_MANAGER,
    STOP_EVALUATORS,
    PrimitiveError,
    directional_agreement,
    parse_regimes,
    parse_timeframe,
    r_multiple_target,
)

log = logging.getLogger(__name__)

#: UNKNOWN. Named so the composition below reads as tri-state rather than as
#: an accident of Optional[bool].
UNKNOWN: None = None


class UnevaluableStrategyError(ValueError):
    """The strategy asks for data this deployment does not produce.

    Raised at load time, never at decision time. A strategy referencing an
    indicator period the engine does not compute is not a runtime hiccup — it
    is a strategy that can never fire, and discovering that mid-session means
    discovering it from an absence, which is the hardest kind of bug to see.
    """


# ---------------------------------------------------------------------------
# What this deployment can actually answer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Capabilities:
    """The indicators and timeframes actually produced by the engine.

    Derived from :func:`default_indicators` rather than restated, so adding an
    indicator to the engine widens what strategies may ask for without anyone
    remembering to update a second list.
    """

    indicators: frozenset[str]
    timeframes: frozenset[Timeframe]
    historical: frozenset[str]

    @classmethod
    def from_engine(cls, engine: IndicatorEngine | None = None) -> Capabilities:
        names = set(default_indicators())
        # values() derives these from the composite indicators; they are
        # readable by name even though no Indicator object is registered.
        names |= {"macd_signal", "macd_histogram", "bb_upper", "bb_lower", "bb_width_pct"}
        timeframes = (engine or IndicatorEngine()).timeframes
        return cls(
            indicators=frozenset(names),
            timeframes=frozenset(timeframes),
            historical=frozenset(HISTORICAL_INDICATORS),
        )

    def periods_for(self, prefix: str) -> list[int]:
        out = []
        for name in self.indicators:
            if name.startswith(prefix):
                suffix = name[len(prefix) :]
                if suffix.isdigit():
                    out.append(int(suffix))
        return sorted(out)


DEFAULT_CAPABILITIES = Capabilities.from_engine()


# ---------------------------------------------------------------------------
# Capability verification
# ---------------------------------------------------------------------------


def _require_indicator(name: str, caps: Capabilities, primitive: str, param: str) -> None:
    if name in caps.indicators:
        return
    prefix = name.rstrip("0123456789")
    available = caps.periods_for(prefix)
    hint = (
        f" This deployment computes {prefix}{{{', '.join(map(str, available))}}}."
        if available
        else ""
    )
    raise UnevaluableStrategyError(
        f"{primitive}: {param} resolves to indicator {name!r}, which this "
        f"deployment does not compute, so the condition could never be "
        f"evaluated.{hint}"
    )


def _require_timeframe(timeframe: Timeframe, caps: Capabilities, primitive: str) -> None:
    if timeframe not in caps.timeframes:
        raise UnevaluableStrategyError(
            f"{primitive}: timeframe {timeframe.value!r} is not carried by the "
            f"indicator engine (it carries "
            f"{sorted(t.value for t in caps.timeframes)}), so the condition "
            f"could never be evaluated."
        )


def verify_condition(condition: Condition, caps: Capabilities = DEFAULT_CAPABILITIES) -> None:
    """Check one condition can be answered here. Raises, or returns silently."""
    name, params = condition.primitive, condition.params

    if name not in CONDITION_EVALUATORS and name not in STOP_EVALUATORS:
        if name in DEFERRED_TO_POSITION_MANAGER:
            return
        raise UnevaluableStrategyError(
            f"primitive {name!r} is registered in the DSL but has no evaluator. "
            f"A strategy using it would validate and then never fire."
        )

    # Re-validate against the registry's declared bounds. compile_strategy
    # already does this, but nothing STRUCTURALLY guarantees a document reached
    # the evaluator through compilation — and the bounds are what stop a
    # threshold of -1e999 from making an "above this" gate pass
    # unconditionally. Checking here means holding an evaluator is proof the
    # parameters are in range, not just that the primitive names exist.
    try:
        REGISTRY.get(name).validate_params(dict(params))
    except ValueError as exc:
        raise UnevaluableStrategyError(f"{name}: {exc}") from exc

    def period(param: str, default: int | None = None) -> int | None:
        value = params.get(param, default)
        return None if value is None else int(value)

    if name in ("price_above_ma", "ma_slope_positive"):
        p = period("period")
        if p is not None:
            _require_indicator(f"ema_{p}", caps, name, "period")
    elif name == "ma_crossover":
        for param in ("fast", "slow"):
            p = period(param)
            if p is not None:
                _require_indicator(f"ema_{p}", caps, name, param)
        fast, slow = period("fast"), period("slow")
        if fast is not None and slow is not None and fast >= slow:
            raise UnevaluableStrategyError(
                f"ma_crossover: fast={fast} is not faster than slow={slow}; a "
                f"crossover of a moving average with a slower one is the only "
                f"reading that makes sense."
            )
    elif name == "rsi_between":
        _require_indicator(f"rsi_{period('period', 14)}", caps, name, "period")
        lo, hi = params.get("min"), params.get("max")
        if lo is not None and hi is not None and Decimal(str(lo)) > Decimal(str(hi)):
            raise UnevaluableStrategyError(
                f"rsi_between: min={lo} exceeds max={hi}, so the band is empty "
                f"and the condition can never be true."
            )
    elif name == "atr_pct_between":
        _require_indicator(f"atr_{period('period', 14)}", caps, name, "period")
    elif name == "volume_ratio_above":
        _require_indicator(f"volume_ratio_{period('window', 20)}", caps, name, "window")
    elif name == "higher_tf_trend_is":
        _require_timeframe(parse_timeframe(str(params["timeframe"])), caps, name)
    elif name == "timeframe_agreement_at_least":
        raw = str(params.get("of", "5m,15m,1h"))
        timeframes = [parse_timeframe(t) for t in raw.split(",") if t.strip()]
        for timeframe in timeframes:
            _require_timeframe(timeframe, caps, name)
        count = int(params.get("count", 1))
        if count > len(timeframes):
            raise UnevaluableStrategyError(
                f"timeframe_agreement_at_least: count={count} exceeds the "
                f"{len(timeframes)} timeframes listed, so it can never be met."
            )
    elif name == "regime_is":
        parse_regimes(str(params["regimes"]))
    elif name == "price_within_pct_of_level":
        level = str(params["level"])
        if level.startswith("ema_"):
            _require_indicator(level, caps, name, "level")


def verify_strategy(doc: StrategyDocument, caps: Capabilities = DEFAULT_CAPABILITIES) -> None:
    """Every condition in the document, entry and exit alike."""
    for condition in doc.entry.all_conditions():
        verify_condition(condition, caps)
    exits: tuple[Condition | None, ...] = (
        doc.exit.stop,
        doc.exit.time,
        doc.exit.target,
        doc.exit.trail,
    )
    for exit_condition in exits:
        if exit_condition is not None:
            verify_condition(exit_condition, caps)
    if doc.applicability.timeframe not in caps.timeframes:
        raise UnevaluableStrategyError(
            f"strategy timeframe {doc.applicability.timeframe.value!r} is not "
            f"carried by the indicator engine "
            f"({sorted(t.value for t in caps.timeframes)})."
        )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConditionResult:
    """One condition's answer, with enough detail to explain a non-entry.

    E07-S05 wants score explainability and E13 wants to know why a strategy did
    not fire. "Entry conditions not met" is not an answer anybody can act on;
    which condition, and whether it was false or unreadable, is.
    """

    primitive: str
    outcome: bool | None
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def is_unknown(self) -> bool:
        return self.outcome is None

    def describe(self) -> str:
        state = "UNKNOWN" if self.outcome is None else str(self.outcome)
        return f"{self.primitive}={state}"


@dataclass(frozen=True)
class EntryDecision:
    """Whether the entry group fired, and everything behind that."""

    fired: bool
    outcome: bool | None
    results: tuple[ConditionResult, ...]

    @property
    def unknowns(self) -> tuple[ConditionResult, ...]:
        return tuple(r for r in self.results if r.is_unknown)

    def reason(self) -> str:
        if self.fired:
            return "all entry conditions met"
        if self.outcome is None:
            missing = ", ".join(r.primitive for r in self.unknowns)
            return f"UNKNOWN — could not evaluate: {missing}"
        failed = ", ".join(r.primitive for r in self.results if r.outcome is False)
        return f"not met: {failed}" if failed else "not met"


def evaluate_condition(condition: Condition, ctx: EvalContext) -> ConditionResult:
    evaluator = CONDITION_EVALUATORS.get(condition.primitive)
    if evaluator is None:
        raise UnevaluableStrategyError(f"no evaluator for primitive {condition.primitive!r}")
    outcome = evaluator(ctx, condition.params)
    return ConditionResult(
        primitive=condition.primitive, outcome=outcome, params=dict(condition.params)
    )


def _all_of(outcomes: list[bool | None]) -> bool | None:
    """False beats UNKNOWN: a definite failure is a definite non-entry."""
    if any(o is False for o in outcomes):
        return False
    if any(o is None for o in outcomes):
        return UNKNOWN
    return True


def _any_of(outcomes: list[bool | None]) -> bool | None:
    """True beats UNKNOWN: one satisfied alternative is enough."""
    if any(o is True for o in outcomes):
        return True
    if any(o is None for o in outcomes):
        return UNKNOWN
    return False


def _none_of(outcomes: list[bool | None]) -> bool | None:
    """The subtle one, and the reason tri-state exists at all.

    ``none_of`` asserts an absence. A condition that could not be evaluated has
    NOT been shown to be absent, so it must yield UNKNOWN — collapsing it to
    True here would mean an unreadable news feed or a cold indicator satisfied
    a guard clause, which is precisely the fail-open the invariants forbid.
    """
    if any(o is True for o in outcomes):
        return False
    if any(o is None for o in outcomes):
        return UNKNOWN
    return True


def evaluate_group(group: ConditionGroup, ctx: EvalContext) -> EntryDecision:
    results: list[ConditionResult] = []

    def run(conditions: list[Condition]) -> list[bool | None]:
        outcomes: list[bool | None] = []
        for condition in conditions:
            result = evaluate_condition(condition, ctx)
            results.append(result)
            outcomes.append(result.outcome)
        return outcomes

    parts: list[bool | None] = []
    if group.all_of:
        parts.append(_all_of(run(group.all_of)))
    if group.any_of:
        parts.append(_any_of(run(group.any_of)))
    if group.none_of:
        parts.append(_none_of(run(group.none_of)))

    outcome = _all_of(parts)
    return EntryDecision(fired=outcome is True, outcome=outcome, results=tuple(results))


def evaluate_stop(doc: StrategyDocument, ctx: EvalContext) -> Decimal | None:
    """The stop price the strategy's exit rules imply, or None if uncomputable."""
    evaluator = STOP_EVALUATORS.get(doc.exit.stop.primitive)
    if evaluator is None:
        raise UnevaluableStrategyError(f"exit stop {doc.exit.stop.primitive!r} has no evaluator")
    return evaluator(ctx, doc.exit.stop.params)


def evaluate_target(
    doc: StrategyDocument,
    entry: Decimal,
    stop: Decimal,
    tick: Decimal = Decimal("0.05"),
) -> Decimal | None:
    target = doc.exit.target
    if target is None or target.primitive != "r_multiple_target":
        return None
    return r_multiple_target(entry, stop, doc.direction, target.params, tick)


# ---------------------------------------------------------------------------
# The evaluator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApplicabilityCheck:
    """Whether a strategy is even in scope for this symbol right now."""

    applies: bool
    reason: str | None = None


@dataclass
class StrategyEvaluator:
    """One strategy, verified against this deployment, ready to run.

    Construction verifies capability, so possessing an evaluator is proof the
    strategy can actually be answered here.
    """

    document: StrategyDocument
    capabilities: Capabilities = DEFAULT_CAPABILITIES

    def __post_init__(self) -> None:
        verify_strategy(self.document, self.capabilities)

    @property
    def strategy_id(self) -> str:
        return self.document.id

    def applies_to(self, ctx: EvalContext) -> ApplicabilityCheck:
        """Is this strategy in scope at all, before any condition is read?

        ``Applicability`` was parsed, hashed into the content hash, and then
        consulted by nothing — a strategy declaring "TRENDING only, above 100
        rupees" had both ignored, so a trend strategy would fire freely in a
        rangebound market. This is the one place that block is enforced.

        An UNKNOWN regime does not apply. The strategy was validated against a
        named set of regimes; running it when the regime cannot be established
        is running it outside the conditions its backtest covers.
        """
        applicability = self.document.applicability

        if ctx.timeframe is not applicability.timeframe:
            return ApplicabilityCheck(
                False,
                f"strategy is for {applicability.timeframe.value}, "
                f"evaluated on {ctx.timeframe.value}",
            )
        if ctx.last_price < applicability.min_price:
            return ApplicabilityCheck(
                False,
                f"price {ctx.last_price} is below the strategy's floor {applicability.min_price}",
            )
        if ctx.regime is None:
            return ApplicabilityCheck(
                False,
                "regime is unknown; the strategy is only validated for "
                f"{[r.value for r in applicability.regimes]}",
            )
        if ctx.regime not in applicability.regimes:
            return ApplicabilityCheck(
                False,
                f"regime {ctx.regime.value} is not in the strategy's "
                f"{[r.value for r in applicability.regimes]}",
            )
        return ApplicabilityCheck(True)

    def evaluate(self, ctx: EvalContext) -> EntryDecision:
        """Entry conditions only. Does not decide whether to trade."""
        if ctx.direction is not self.document.direction:
            raise UnevaluableStrategyError(
                f"context direction {ctx.direction.value} does not match strategy "
                f"direction {self.document.direction.value}; a long strategy "
                f"evaluated with short context would read every directional "
                f"primitive backwards"
            )
        return evaluate_group(self.document.entry, ctx)

    def fire(self, ctx: EvalContext, *, correlation_id: UUID) -> Trigger | None:
        """A full firing: conditions, stop, and the resulting :class:`Trigger`.

        ``correlation_id`` is REQUIRED rather than generated here. Minting a
        UUID inside the evaluator would put randomness in the strategy path,
        which the conventions forbid for a concrete reason: replaying the same
        context twice must produce the same answer, and a backtest that cannot
        compare two Trigger objects for equality cannot assert that the
        decision sequence matched. The caller owns the identity; the evaluator
        stays a pure function.

        Returns None when the indicators are not ready, when the strategy is
        out of its declared applicability, when it did not fire, OR when it
        fired but the stop could not be computed. The second case deserves the same answer as
        the first: an entry whose protective stop is unknown is exactly what
        the "every position has a stop" invariant exists to prevent, and a
        Trigger without a valid stop would not construct anyway.
        """
        if not ctx.snapshot.all_ready:
            # E13-S01 criterion 3, and "data stale -> block entries". This is
            # the gate a feed gap acts through: mark_stale sets IndicatorSet
            # .stale, which clears is_ready, which clears all_ready. Without
            # this check the evaluator happily traded through a twelve-minute
            # hole in the feed on indicators computed from the wrong bars —
            # and a strategy reading only a warm ema_20 would never notice
            # that the 200-EMA beside it was built from forty bars.
            log.warning(
                "%s: refusing to evaluate %s — indicators are not ready: %s",
                ctx.symbol,
                self.strategy_id,
                {tf.value: names for tf, names in ctx.snapshot.not_ready.items()},
            )
            return None

        scope = self.applies_to(ctx)
        if not scope.applies:
            log.debug("%s: %s does not apply — %s", ctx.symbol, self.strategy_id, scope.reason)
            return None

        decision = self.evaluate(ctx)
        if not decision.fired:
            return None

        stop = evaluate_stop(self.document, ctx)
        if stop is None:
            log.warning(
                "%s: %s entry conditions met but the stop could not be computed "
                "(%s); refusing to fire — an entry without a stop is not a trade "
                "this system may take",
                ctx.symbol,
                self.strategy_id,
                self.document.exit.stop.primitive,
            )
            return None

        if not self._stop_is_on_the_right_side(stop, ctx):
            log.error(
                "%s: %s produced a %s stop at %s against a price of %s — that is "
                "on the wrong side of the entry and would be an immediate exit. "
                "Refusing to fire.",
                ctx.symbol,
                self.strategy_id,
                self.document.direction.value,
                stop,
                ctx.last_price,
            )
            return None

        return Trigger(
            correlation_id=correlation_id,
            symbol=ctx.symbol,
            strategy_id=self.strategy_id,
            direction=self.document.direction,
            trigger_price=ctx.last_price,
            suggested_stop=stop,
            timeframe_agreement=directional_agreement(ctx),
            fired_at=ctx.now,
        )

    def _stop_is_on_the_right_side(self, stop: Decimal, ctx: EvalContext) -> bool:
        """``Trigger`` enforces this too, by raising. Checking first turns a
        strategy bug into a logged refusal rather than an exception in the
        signal loop."""
        if stop <= 0:
            return False
        if self.document.direction is Direction.LONG:
            return stop < ctx.last_price
        return stop > ctx.last_price


def load_evaluators(
    documents: list[StrategyDocument], capabilities: Capabilities = DEFAULT_CAPABILITIES
) -> tuple[list[StrategyEvaluator], dict[str, str]]:
    """Build evaluators for every strategy, reporting the ones that cannot run.

    Returns ``(usable, rejected)``. One unevaluable strategy must not stop the
    other nineteen from trading, but it must be visible rather than absent —
    the same shape as :func:`~algotrader.indicators.engine.warm_up_symbols`.
    """
    usable: list[StrategyEvaluator] = []
    rejected: dict[str, str] = {}
    for doc in documents:
        try:
            usable.append(StrategyEvaluator(doc, capabilities))
        except (UnevaluableStrategyError, PrimitiveError) as exc:
            rejected[doc.id] = str(exc)
            log.error("strategy %s cannot run in this deployment: %s", doc.id, exc)
    return usable, rejected


__all__ = [
    "DEFAULT_CAPABILITIES",
    "Capabilities",
    "ConditionResult",
    "EntryDecision",
    "StrategyEvaluator",
    "UnevaluableStrategyError",
    "evaluate_condition",
    "evaluate_group",
    "evaluate_stop",
    "evaluate_target",
    "load_evaluators",
    "verify_condition",
    "verify_strategy",
]
