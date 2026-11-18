"""The strategy path as an attack surface (E13-S01).

This is the layer an AI writes into. CLAUDE.md's third invariant — no ``eval``,
``exec`` or dynamic import in the strategy path, ever — exists because a
prompt-injected model composing a strategy must not be able to reach code
execution. Until the evaluator was written that invariant was easy to hold and
untested, because nothing executed anything at all.

Now something does execute, so the invariant needs probes rather than
assurances. The threat model is a fully compromised strategy author: assume the
YAML is adversarial and ask what the worst outcome is. It should be a
bad-but-valid strategy that still faces the whole risk gauntlet — never code
execution, never a position without a stop, never an unbounded loop.
"""

from __future__ import annotations

import ast
import datetime as dt
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from algotrader.common.enums import Direction, Timeframe
from algotrader.indicators.engine import MultiTimeframeSnapshot
from algotrader.strategy import runtime
from algotrader.strategy.context import EvalContext
from algotrader.strategy.dsl import (
    REGISTRY,
    CompilationError,
    Condition,
    ConditionGroup,
    compile_strategy,
    load_strategy_yaml,
)
from algotrader.strategy.primitives import evaluators
from algotrader.strategy.primitives import registry as primitive_registry
from algotrader.strategy.runtime import (
    StrategyEvaluator,
    UnevaluableStrategyError,
    evaluate_condition,
    evaluate_group,
)

primitive_registry.install()

NOW = dt.datetime(2026, 8, 20, 5, 0, tzinfo=dt.UTC)


STRATEGY_PACKAGE = Path(runtime.__file__).resolve().parent

#: Calls that turn data into code, by the name they are called as.
_DYNAMIC_CALLS = frozenset({"eval", "exec", "compile", "__import__"})


def _dynamic_execution(root: Path) -> list[str]:
    """Every dynamic-execution site under ``root``, as ``file:line: what``."""
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            what = None
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                what = node.func.id if node.func.id in _DYNAMIC_CALLS else None
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "import_module":
                    what = "importlib.import_module"
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
                if any(n.split(".")[0] == "importlib" for n in names):
                    what = "import importlib"
            if what:
                found.append(f"{path.name}:{getattr(node, 'lineno', 0)}: {what}")
    return found


def _snapshot() -> MultiTimeframeSnapshot:
    return MultiTimeframeSnapshot(
        symbol="INFY",
        per_timeframe={
            Timeframe.M5: {"ema_20": 100.0, "ema_50": 99.0, "atr_14": 2.0, "rsi_14": 55.0}
        },
        all_ready=True,
    )


def _ctx(**kw) -> EvalContext:
    base: dict = {
        "symbol": "INFY",
        "now": NOW,
        "timeframe": Timeframe.M5,
        "direction": Direction.LONG,
        "last_price": Decimal("100"),
        "snapshot": _snapshot(),
        "entries_today": 0,
    }
    base.update(kw)
    return EvalContext(**base)


class TestNoCodeExecutionPathExists:
    """Invariant 3, probed rather than asserted."""

    def test_no_module_in_the_strategy_package_executes_dynamically(self) -> None:
        """EVERY module under strategy/, by AST (audit ENG-047): the first
        version scanned two modules' text, so an ``eval`` in dsl.py or the
        registry passed - and a text scan cannot tell the builtin ``compile``
        from ``re.compile``."""
        offenders = _dynamic_execution(STRATEGY_PACKAGE)
        assert len(list(STRATEGY_PACKAGE.rglob("*.py"))) >= 6, "the scan is not seeing the package"
        assert offenders == []

    def test_the_scan_would_see_each_spelling(self, tmp_path) -> None:
        """The control - and the case a text scan got wrong: re.compile is fine."""
        (tmp_path / "a.py").write_text(
            "import re\nPAT = re.compile('x')\nimport importlib\ndef f(s):\n"
            "    eval(s)\n    exec(s)\n    compile(s, 'f', 'exec')\n    __import__(s)\n"
            "    importlib.import_module(s)\n",
            encoding="utf-8",
        )
        found = {line.split(": ", 1)[1] for line in _dynamic_execution(tmp_path)}
        assert found == {
            "import importlib",
            "eval",
            "exec",
            "compile",
            "__import__",
            "importlib.import_module",
        }

    def test_a_primitive_name_is_a_dict_lookup_not_a_resolution(self) -> None:
        """The whole defence: an unknown name finds nothing rather than
        resolving to something."""
        unknown = Condition(primitive="os.system", params={})
        with pytest.raises(UnevaluableStrategyError):
            evaluate_condition(unknown, _ctx())

    def test_a_dunder_name_resolves_to_nothing(self) -> None:
        for hostile in ("__builtins__", "__class__", "__subclasses__", "system", "popen"):
            with pytest.raises(UnevaluableStrategyError):
                evaluate_condition(Condition(primitive=hostile, params={}), _ctx())

    def test_the_registry_refuses_an_unvetted_primitive_at_compile_time(self) -> None:
        with pytest.raises(ValueError, match="unknown primitive"):
            REGISTRY.get("subprocess_run")

    def test_params_are_data_and_never_reach_a_name_lookup(self) -> None:
        """A parameter value must not be able to select code. ``level`` is a
        string that indexes a hand-written if-chain, so a hostile value falls
        through to None rather than resolving."""
        hostile = Condition(
            primitive="price_breaks_level",
            params={"level": "__class__", "direction": "above"},
        )
        assert evaluate_condition(hostile, _ctx()).outcome is None


class TestAdversarialParametersFailClosed:
    """The strategy author is assumed hostile. Nothing here may produce a
    trade, hang, or crash the signal loop in a way that skips other symbols."""

    @pytest.mark.parametrize(
        "params",
        [
            {"level": "opening_range_high", "direction": "sideways"},
            {"level": "opening_range_high", "direction": ""},
            {"level": "opening_range_high", "direction": None},
        ],
        ids=["nonsense", "empty", "null"],
    )
    def test_a_bad_direction_raises_rather_than_guessing(self, params: dict) -> None:
        with pytest.raises(evaluators.PrimitiveError):
            evaluate_condition(Condition(primitive="price_breaks_level", params=params), _ctx())

    def test_a_non_numeric_threshold_is_refused(self) -> None:
        with pytest.raises(evaluators.PrimitiveError):
            evaluate_condition(
                Condition(primitive="news_score_above", params={"threshold": "0; DROP TABLE"}),
                _ctx(news_score=0.9),
            )

    def test_an_enormous_lookback_does_not_hang(self) -> None:
        """Bounded history means a huge lookback is answered instantly with
        UNKNOWN rather than walked."""
        result = evaluate_condition(
            Condition(
                primitive="ma_slope_positive",
                params={"period": 20, "lookback": 10**9, "positive": True},
            ),
            _ctx(),
        )
        assert result.outcome is None

    def test_a_negative_lookback_is_refused(self) -> None:
        with pytest.raises(ValueError, match="bars must be >= 0"):
            _ctx().indicator_ago("ema_20", -5)

    def test_an_absurd_atr_multiplier_still_produces_a_valid_stop(self) -> None:
        """A 5x ATR stop is legal and terrible. It must remain a placeable
        price on the correct side, so the risk engine gets to reject it on
        SIZE rather than the evaluator failing on arithmetic."""
        stop = evaluators.atr_stop(_ctx(), {"multiplier": Decimal("5"), "period": 14})
        assert stop is not None and stop < Decimal("100")
        assert stop % Decimal("0.05") == 0

    def test_a_stop_that_would_land_below_zero_is_rejected_not_clamped(self) -> None:
        """Clamping to zero would produce a stop that can never trigger, which
        looks like a stop and is not one."""
        ctx = _ctx(last_price=Decimal("1.00"))
        stop = evaluators.atr_stop(ctx, {"multiplier": Decimal("5"), "period": 14})
        assert stop is not None and stop <= 0
        # The helper refuses it. That fire() consults the helper - the path
        # that matters - is tested through fire() itself:
        # tests/unit/test_strategy_runtime.py::test_a_stop_below_zero_means_fire_returns_nothing
        doc = compile_strategy(load_strategy_yaml(_MINIMAL_STRATEGY))
        assert StrategyEvaluator(doc)._stop_is_on_the_right_side(stop, ctx) is False

    def test_a_hostile_regime_string_cannot_silently_never_match(self) -> None:
        with pytest.raises(evaluators.PrimitiveError, match="not a regime"):
            evaluate_condition(
                Condition(primitive="regime_is", params={"regimes": "'; DROP TABLE strategy;--"}),
                _ctx(),
            )

    def test_a_malformed_time_window_is_refused(self) -> None:
        for bad in ("25:00", "9", "aa:bb", "", "09:00:00:00"):
            with pytest.raises(evaluators.PrimitiveError):
                evaluate_condition(
                    Condition(primitive="within_window", params={"start": bad, "end": "15:00"}),
                    _ctx(),
                )


class TestTheDslBoundsStillHold:
    """The registry's declared bounds are the first gate. The evaluator must
    not become a way around them."""

    def test_an_out_of_range_parameter_is_refused_at_compile_time(self) -> None:
        hostile = _MINIMAL_STRATEGY.replace("multiplier: 1.5", "multiplier: 500")
        with pytest.raises(CompilationError, match="above maximum"):
            compile_strategy(load_strategy_yaml(hostile))

    def test_an_unknown_parameter_is_refused(self) -> None:
        hostile = _MINIMAL_STRATEGY.replace(
            "params: {multiplier: 1.5, period: 14}",
            "params: {multiplier: 1.5, period: 14, shell: true}",
        )
        with pytest.raises(CompilationError, match="unknown parameter"):
            compile_strategy(load_strategy_yaml(hostile))

    def test_a_strategy_without_a_stop_does_not_parse(self) -> None:
        """Invariant 5 at the type level: the DSL has no way to say 'no stop'."""
        hostile = _MINIMAL_STRATEGY.replace(
            "  stop: {primitive: atr_stop, params: {multiplier: 1.5, period: 14}}\n", ""
        )
        with pytest.raises(ValueError):
            load_strategy_yaml(hostile)

    def test_a_non_exit_primitive_cannot_be_used_as_a_stop(self) -> None:
        hostile = _MINIMAL_STRATEGY.replace(
            "stop: {primitive: atr_stop, params: {multiplier: 1.5, period: 14}}",
            "stop: {primitive: rsi_between, params: {period: 14, min: 0, max: 100}}",
        )
        with pytest.raises((ValueError, CompilationError)):
            compile_strategy(load_strategy_yaml(hostile))

    def test_the_time_exit_must_be_the_squareoff_deadline(self) -> None:
        hostile = _MINIMAL_STRATEGY.replace(
            "time: {primitive: squareoff_deadline}",
            "time: {primitive: min_bars_since_open, params: {bars: 1}}",
        )
        with pytest.raises((ValueError, CompilationError), match="squareoff"):
            compile_strategy(load_strategy_yaml(hostile))


class TestMissingDataNeverBecomesPermission:
    """The fail-closed property, stated as a security claim rather than a
    logic one: an attacker who can degrade an input must not thereby unlock a
    trade."""

    def test_killing_the_news_feed_does_not_satisfy_a_news_guard(self) -> None:
        """CHANGED 16 Nov 2026: the guard is placed as a strategy may place it -
        under all_of. Under none_of it is a double negative, now refused at
        compile (audit TRD-010); a dead feed must satisfy it in neither."""
        for group in (
            ConditionGroup(
                all_of=[Condition(primitive="no_material_news", params={"lookback_hours": 24})]
            ),
            ConditionGroup(
                none_of=[Condition(primitive="no_material_news", params={"lookback_hours": 24})]
            ),
        ):
            assert evaluate_group(group, _ctx(hours_since_material_news=None)).fired is False

    def test_killing_the_macro_feed_does_not_satisfy_a_vix_gate(self) -> None:
        group = ConditionGroup(
            all_of=[Condition(primitive="india_vix_between", params={"min": 0, "max": 15})]
        )
        assert evaluate_group(group, _ctx(india_vix=None)).fired is False

    def test_an_empty_snapshot_fires_nothing(self) -> None:
        empty = MultiTimeframeSnapshot(symbol="INFY", per_timeframe={}, all_ready=False)
        doc = compile_strategy(load_strategy_yaml(_MINIMAL_STRATEGY))
        assert StrategyEvaluator(doc).fire(_ctx(snapshot=empty), correlation_id=uuid4()) is None

    def test_every_condition_unknown_means_no_trade(self) -> None:
        group = ConditionGroup(
            all_of=[
                Condition(primitive="india_vix_between", params={"min": 0, "max": 99}),
                Condition(primitive="sector_rank_top_n", params={"n": 5}),
                Condition(primitive="news_score_above", params={"threshold": -1}),
            ]
        )
        decision = evaluate_group(group, _ctx())
        assert decision.fired is False and decision.outcome is None
        assert len(decision.unknowns) == 3


class TestNoInformationLeaksThroughErrors:
    def test_a_primitive_error_does_not_echo_unbounded_input(self) -> None:
        """Error strings reach logs. A multi-kilobyte parameter echoed into an
        exception message becomes a log-flooding primitive."""
        huge = "A" * 10_000
        with pytest.raises(evaluators.PrimitiveError) as caught:
            evaluate_condition(Condition(primitive="regime_is", params={"regimes": huge}), _ctx())
        assert len(str(caught.value)) < 500, "error message echoes unbounded input"


_MINIMAL_STRATEGY = """
id: probe_strategy
name: Probe
origin: USER_AUTHORED
created_at: 2026-08-20T04:00:00+00:00
created_by: security-test
direction: LONG
hypothesis:
  mechanism: >-
    A deliberately minimal strategy used to probe the execution path rather
    than to trade; it exists so the security tests have something valid to
    mutate into something hostile.
  why_it_should_persist: >-
    It is a test fixture and is never promoted, so persistence is not a
    property it needs to have.
  expected_failure_mode: >-
    It should never be activated in any environment.
applicability: {regimes: [TRENDING], timeframe: 5m}
entry:
  all_of:
    - {primitive: price_above_ma, params: {period: 20}}
exit:
  stop: {primitive: atr_stop, params: {multiplier: 1.5, period: 14}}
  time: {primitive: squareoff_deadline}
"""
