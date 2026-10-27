"""Tests for the strategy DSL.

The most important assertions here are the negative ones: that a strategy
*cannot* express something dangerous.  A strategy without a stop, without a
time exit, or referencing an unknown primitive must fail to parse — that is
what makes AI-generated strategies survivable.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from algotrader.strategy import primitives  # noqa: F401 — populates REGISTRY
from algotrader.strategy.dsl import (
    REGISTRY,
    CompilationError,
    ConditionGroup,
    StrategyDocument,
    compile_strategy,
    load_strategy_yaml,
)

SEED = Path(__file__).parents[2] / "config" / "strategies" / "orb_classic.yaml"


@pytest.fixture
def seed_doc() -> StrategyDocument:
    return load_strategy_yaml(SEED.read_text(encoding="utf-8"))


class TestSeedStrategy:
    def test_seed_parses(self, seed_doc: StrategyDocument) -> None:
        assert seed_doc.id == "orb_classic"

    def test_seed_compiles(self, seed_doc: StrategyDocument) -> None:
        assert compile_strategy(seed_doc) is seed_doc

    def test_content_hash_is_stable(self, seed_doc: StrategyDocument) -> None:
        assert seed_doc.content_hash() == seed_doc.content_hash()

    def test_hash_ignores_metadata_but_not_behaviour(self, seed_doc: StrategyDocument) -> None:
        """Renaming a strategy is not a new trial; changing its logic is."""
        renamed = seed_doc.model_copy(update={"name": "Totally Different Name"})
        assert renamed.content_hash() == seed_doc.content_hash()

        retuned = seed_doc.model_copy(
            update={
                "constraints": seed_doc.constraints.model_copy(update={"max_entries_per_day": 3})
            }
        )
        assert retuned.content_hash() != seed_doc.content_hash()


class TestPrimitiveRegistry:
    def test_registry_is_populated(self) -> None:
        assert len(REGISTRY.names()) >= 20

    def test_unknown_primitive_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown primitive"):
            REGISTRY.get("execute_arbitrary_python")

    def test_param_out_of_bounds_rejected(self) -> None:
        """Bounds are part of the safety story — no absurd stop widths."""
        spec = REGISTRY.get("atr_stop")
        with pytest.raises(ValueError, match="above maximum"):
            spec.validate_params({"multiplier": 500})

    def test_unknown_param_rejected(self) -> None:
        spec = REGISTRY.get("atr_stop")
        with pytest.raises(ValueError, match="unknown parameter"):
            spec.validate_params({"multiplier": 1.5, "shell_command": "rm -rf /"})


class TestMandatoryExits:
    """A strategy must not be able to express 'no stop' or 'hold forever'."""

    def _base(self) -> dict:
        return {
            "id": "test_strat",
            "name": "Test Strategy",
            "origin": "USER_AUTHORED",
            "created_at": "2026-08-04T00:00:00Z",
            "created_by": "test",
            "hypothesis": {
                "mechanism": "A" * 100,
                "why_it_should_persist": "B" * 80,
                "expected_failure_mode": "C" * 60,
            },
            "applicability": {"regimes": ["TRENDING"], "timeframe": "5m"},
            "direction": "LONG",
            "entry": {"all_of": [{"primitive": "price_above_ma", "params": {"period": 20}}]},
        }

    def test_missing_stop_is_rejected(self) -> None:
        doc = self._base()
        doc["exit"] = {"time": {"primitive": "squareoff_deadline"}}
        with pytest.raises(ValidationError):
            StrategyDocument.model_validate(doc)

    def test_missing_time_exit_is_rejected(self) -> None:
        doc = self._base()
        doc["exit"] = {"stop": {"primitive": "atr_stop", "params": {"multiplier": 1.5}}}
        with pytest.raises(ValidationError):
            StrategyDocument.model_validate(doc)

    def test_wrong_time_exit_is_rejected(self) -> None:
        doc = self._base()
        doc["exit"] = {
            "stop": {"primitive": "atr_stop", "params": {"multiplier": 1.5}},
            "time": {"primitive": "within_window", "params": {"start": "09:15", "end": "23:59"}},
        }
        parsed = StrategyDocument.model_validate(doc)
        with pytest.raises(CompilationError, match="squareoff_deadline"):
            compile_strategy(parsed)

    def test_valid_exits_accepted(self) -> None:
        doc = self._base()
        doc["exit"] = {
            "stop": {"primitive": "atr_stop", "params": {"multiplier": 1.5}},
            "time": {"primitive": "squareoff_deadline"},
        }
        assert compile_strategy(StrategyDocument.model_validate(doc))


class TestHypothesisRequired:
    """Hypothesis-before-results is the cheapest anti-data-mining control."""

    def _with_hypothesis(self, h: dict) -> dict:
        return {
            "id": "test_strat",
            "name": "Test Strategy",
            "origin": "AI_PROPOSED_JOURNAL",
            "created_at": "2026-08-04T00:00:00Z",
            "created_by": "claude-opus-5",
            "hypothesis": h,
            "applicability": {"regimes": ["TRENDING"], "timeframe": "5m"},
            "direction": "LONG",
            "entry": {"all_of": [{"primitive": "price_above_ma", "params": {"period": 20}}]},
            "exit": {
                "stop": {"primitive": "atr_stop", "params": {"multiplier": 1.5}},
                "time": {"primitive": "squareoff_deadline"},
            },
        }

    def test_boilerplate_hypothesis_rejected(self) -> None:
        with pytest.raises(ValidationError):
            StrategyDocument.model_validate(
                self._with_hypothesis(
                    {
                        "mechanism": "n/a",
                        "why_it_should_persist": "n/a",
                        "expected_failure_mode": "n/a",
                    }
                )
            )

    def test_too_short_hypothesis_rejected(self) -> None:
        with pytest.raises(ValidationError):
            StrategyDocument.model_validate(
                self._with_hypothesis(
                    {
                        "mechanism": "it works",
                        "why_it_should_persist": "because",
                        "expected_failure_mode": "dunno",
                    }
                )
            )

    def test_substantive_hypothesis_accepted(self) -> None:
        doc = StrategyDocument.model_validate(
            self._with_hypothesis(
                {
                    "mechanism": "M" * 100,
                    "why_it_should_persist": "P" * 80,
                    "expected_failure_mode": "F" * 60,
                }
            )
        )
        assert doc.hypothesis.mechanism


class TestOverfittingGuards:
    def test_more_than_twelve_in_one_group_is_refused_while_parsing(self) -> None:
        """27 Oct 2026: the cap is now a parse-time bound per group, so a
        document with a million conditions is never built in memory first."""
        conditions = [
            {"primitive": "price_above_ma", "params": {"period": 5 + i}} for i in range(15)
        ]
        with pytest.raises(ValidationError, match="at most 12"):
            ConditionGroup.model_validate({"all_of": conditions})

    def test_too_many_entry_conditions_rejected(self) -> None:
        """Excessive degrees of freedom is an overfitting signal. Fifteen
        spread across the three groups - each group within its own cap - is
        still fifteen, and the compile-time guard counts the total."""
        conditions = [
            {"primitive": "price_above_ma", "params": {"period": 5 + i}} for i in range(15)
        ]
        doc = StrategyDocument.model_validate(
            {
                "id": "overfit_strat",
                "name": "Overfit Strategy",
                "origin": "AI_PROPOSED_OBSERVATION",
                "created_at": "2026-08-04T00:00:00Z",
                "created_by": "claude-opus-5",
                "hypothesis": {
                    "mechanism": "M" * 100,
                    "why_it_should_persist": "P" * 80,
                    "expected_failure_mode": "F" * 60,
                },
                "applicability": {"regimes": ["TRENDING"], "timeframe": "5m"},
                "direction": "LONG",
                "entry": {
                    "all_of": conditions[:5],
                    "any_of": conditions[5:10],
                    "none_of": conditions[10:],
                },
                "exit": {
                    "stop": {"primitive": "atr_stop", "params": {"multiplier": 1.5}},
                    "time": {"primitive": "squareoff_deadline"},
                },
            }
        )
        with pytest.raises(CompilationError, match="overfitting signal"):
            compile_strategy(doc)


class TestNoCodeExecution:
    """The DSL must offer no path to executing arbitrary code."""

    @pytest.mark.parametrize(
        "tag",
        [
            "!!python/object/apply:os.system ['echo pwned']",
            "{id: !!python/object/apply:os.system ['echo pwned']}",
            "!!python/name:os.system",
        ],
        ids=["top-level", "nested-value", "name-tag"],
    )
    def test_yaml_tags_do_not_construct_objects(self, tag, monkeypatch) -> None:
        """The executed check behind the ``nosec B506`` on the loader.

        The loader is a ``SafeLoader`` subclass, so a python tag must be
        refused by the CONSTRUCTOR - asserted by type, because the old version
        accepted any ``Exception`` and would have passed on a typo. And the
        call it names must never run.
        """
        import os

        import yaml

        def tripwire(*_args, **_kwargs):
            raise AssertionError("os.system was reached from a strategy document")

        monkeypatch.setattr(os, "system", tripwire)
        with pytest.raises(yaml.constructor.ConstructorError, match="python"):
            load_strategy_yaml(tag + "\n")

    def test_the_loader_is_a_safe_loader(self) -> None:
        """Structural half: whatever the loader adds, it inherits SafeLoader's
        constructor table and nothing broader."""
        import yaml

        captured: list[type] = []
        real_load = yaml.load

        def spy(text, Loader):  # noqa: N803 - PyYAML's own parameter name
            captured.append(Loader)
            return real_load(text, Loader=Loader)

        try:
            yaml.load = spy
            load_strategy_yaml("{}")
        except (CompilationError, ValidationError):
            pass  # "{}" is not a valid document; only the loader type matters here
        finally:
            yaml.load = real_load
        assert captured and issubclass(captured[0], yaml.SafeLoader)
        assert not issubclass(captured[0], yaml.UnsafeLoader)

    def test_strategy_document_forbids_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            StrategyDocument.model_validate(
                {
                    "id": "x_strat",
                    "name": "X",
                    "origin": "USER_AUTHORED",
                    "created_at": "2026-08-04T00:00:00Z",
                    "created_by": "test",
                    "hypothesis": {
                        "mechanism": "M" * 100,
                        "why_it_should_persist": "P" * 80,
                        "expected_failure_mode": "F" * 60,
                    },
                    "applicability": {"regimes": ["TRENDING"], "timeframe": "5m"},
                    "direction": "LONG",
                    "entry": {
                        "all_of": [{"primitive": "price_above_ma", "params": {"period": 20}}]
                    },
                    "exit": {
                        "stop": {"primitive": "atr_stop", "params": {"multiplier": 1.5}},
                        "time": {"primitive": "squareoff_deadline"},
                    },
                    "on_signal_exec": "import os; os.system('id')",  # <- must be rejected
                }
            )


def _doc(**overrides: object) -> dict:
    """A minimal valid document; overrides replace top-level keys."""
    base: dict = {
        "id": "probe_strategy",
        "name": "Probe Strategy",
        "origin": "USER_AUTHORED",
        "created_at": "2026-08-04T00:00:00Z",
        "created_by": "test",
        "hypothesis": {
            "mechanism": "A" * 100,
            "why_it_should_persist": "B" * 80,
            "expected_failure_mode": "C" * 60,
        },
        "applicability": {"regimes": ["TRENDING"], "timeframe": "5m"},
        "direction": "LONG",
        "entry": {"all_of": [{"primitive": "price_above_ma", "params": {"period": 20}}]},
        "exit": {
            "stop": {"primitive": "atr_stop", "params": {"multiplier": 1.5}},
            "time": {"primitive": "squareoff_deadline"},
        },
    }
    base.update(overrides)
    return base


def _compiles(doc: dict) -> StrategyDocument:
    return compile_strategy(StrategyDocument.model_validate(doc))


class TestEveryExitSlotTakesItsOwnKindOfExit:
    """`exit.stop` only had to be "an exit primitive", and a profit target, a
    trailing rule and the square-off deadline all are. So ``stop:
    r_multiple_target`` compiled - a strategy with no stop loss, written in
    the one language whose documentation says that cannot be expressed."""

    @pytest.mark.parametrize(
        "not_a_stop",
        [
            {"primitive": "r_multiple_target", "params": {"r": 2}},
            {"primitive": "trail_after_r", "params": {"activate_at_r": 1, "atr_mult": 1}},
            {"primitive": "squareoff_deadline"},
        ],
        ids=["a-target", "a-trail", "the-time-exit"],
    )
    def test_the_stop_slot_refuses_every_exit_that_is_not_a_stop(self, not_a_stop) -> None:
        doc = _doc(exit={"stop": not_a_stop, "time": {"primitive": "squareoff_deadline"}})
        with pytest.raises(CompilationError, match=r"exit\.stop must be a stop primitive"):
            _compiles(doc)

    def test_the_target_and_trail_slots_are_typed_too(self) -> None:
        doc = _doc()
        doc["exit"]["target"] = {"primitive": "atr_stop", "params": {"multiplier": 1.5}}
        with pytest.raises(CompilationError, match=r"exit\.target must be a target"):
            _compiles(doc)
        doc = _doc()
        doc["exit"]["trail"] = {"primitive": "r_multiple_target", "params": {"r": 2}}
        with pytest.raises(CompilationError, match=r"exit\.trail must be a trail"):
            _compiles(doc)

    def test_an_exit_primitive_cannot_be_an_entry_condition(self) -> None:
        doc = _doc(entry={"all_of": [{"primitive": "atr_stop", "params": {"multiplier": 1.5}}]})
        with pytest.raises(CompilationError, match="exit primitive"):
            _compiles(doc)

    def test_every_slot_correctly_filled_compiles(self) -> None:
        """The control."""
        doc = _doc()
        doc["exit"]["target"] = {"primitive": "r_multiple_target", "params": {"r": 2}}
        doc["exit"]["trail"] = {
            "primitive": "trail_after_r",
            "params": {"activate_at_r": 1, "atr_mult": 1},
        }
        assert _compiles(doc).exit.target is not None

    def test_the_registry_cannot_declare_an_exit_without_a_role(self) -> None:
        from algotrader.strategy.dsl import PrimitiveSpec

        with pytest.raises(ValidationError, match="exit_role"):
            PrimitiveSpec(name="loose_exit", category="exit", description="x")
        with pytest.raises(ValidationError, match="exit_role"):
            PrimitiveSpec(
                name="entry_with_role", category="trend", description="x", exit_role="stop"
            )


class TestParametersAreWhatTheyClaim:
    """Hostile or sloppy input from the AI, refused at compile time."""

    @pytest.mark.parametrize(
        "period",
        [14.7, 14.0, True, "14"],
        ids=["fraction", "integral-float", "bool", "string"],
    )
    def test_an_int_parameter_takes_an_int(self, period) -> None:
        """14.7 used to compile and run as 14 while the content hash kept
        14.7: two identical strategies counted as two trials."""
        doc = _doc(
            entry={"all_of": [{"primitive": "price_above_ma", "params": {"period": period}}]}
        )
        with pytest.raises(CompilationError, match=r"whole number|must be numeric"):
            _compiles(doc)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_a_non_finite_number_is_a_compilation_error_not_a_crash(self, bad) -> None:
        """NaN against a bound raised InvalidOperation, which is not a
        ValueError, so compile_strategy let it escape as a crash."""
        doc = _doc()
        doc["exit"]["stop"] = {"primitive": "atr_stop", "params": {"multiplier": bad}}
        with pytest.raises(CompilationError, match="finite"):
            _compiles(doc)

    def test_an_enum_takes_a_string_from_its_choices(self) -> None:
        doc = _doc()
        doc["exit"]["stop"] = {"primitive": "structure_stop", "params": {"level": ["swing_low"]}}
        with pytest.raises(CompilationError, match="not in"):
            _compiles(doc)

    def test_a_string_parameter_is_typed_and_bounded(self) -> None:
        from algotrader.strategy.dsl import ParamSpec, PrimitiveSpec

        spec = PrimitiveSpec(
            name="probe_str",
            category="context",
            description="x",
            params=[ParamSpec(name="tag", type="str")],
        )
        for bad in (123, "", "x" * 65):
            with pytest.raises(ValueError):
                spec.validate_params({"tag": bad})
        spec.validate_params({"tag": "fine"})

    def test_a_condition_with_too_many_parameters_is_refused_while_parsing(self) -> None:
        from algotrader.strategy.dsl import Condition

        with pytest.raises(ValidationError, match="at most 16"):
            Condition.model_validate({"primitive": "x", "params": {f"p{i}": i for i in range(17)}})


class TestDeclaredConstraintsAreWellFormed:
    @pytest.mark.parametrize(
        "window",
        [("25:00", "26:00"), ("9:30", "11:00"), ("11:00", "10:00"), ("10:00", "10:00")],
        ids=["no-such-hour", "not-zero-padded", "inverted", "empty"],
    )
    def test_an_entry_window_that_cannot_match_is_refused(self, window) -> None:
        with pytest.raises(ValidationError, match="entry_window"):
            StrategyDocument.model_validate(_doc(constraints={"entry_window": window}))

    def test_a_real_window_parses(self) -> None:
        doc = StrategyDocument.model_validate(
            _doc(constraints={"entry_window": ("09:30", "11:00")})
        )
        window = doc.constraints.window_times()
        assert window is not None
        assert (window[0].hour, window[1].hour) == (9, 11)

    @pytest.mark.parametrize(
        "applicability",
        [
            {"regimes": ["TRENDING"], "timeframe": "5m", "sectors": ["  "]},
            {"regimes": ["TRENDING"], "timeframe": "5m", "sectors": []},
            {"regimes": ["TRENDING"], "timeframe": "5m", "min_price": "NaN"},
        ],
        ids=["blank-sector", "empty-sector-list", "nan-floor"],
    )
    def test_applicability_that_reads_as_a_restriction_but_is_not_is_refused(
        self, applicability
    ) -> None:
        with pytest.raises(ValidationError):
            StrategyDocument.model_validate(_doc(applicability=applicability))


class TestTheYamlLoaderIsBounded:
    BOMB = (
        "a: &a [x, x, x, x, x, x, x, x, x]\n"
        "b: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a]\n"
        "c: &c [*b, *b, *b, *b, *b, *b, *b, *b, *b]\n"
        "d: [*c, *c, *c, *c, *c, *c, *c, *c, *c]\n"
    )

    def test_anchors_and_aliases_are_refused(self) -> None:
        """A few lines of nested aliases expand into billions of nodes once
        anything walks the result; safe_load stops code execution, not this."""
        with pytest.raises(CompilationError, match=r"anchors|aliases"):
            load_strategy_yaml(self.BOMB)

    def test_an_alias_alone_is_refused_as_a_compilation_error(self) -> None:
        """Without an anchor the alias cannot resolve, and PyYAML raises its own
        ComposerError - not CompilationError, which is what every caller of
        the loader handles. The alias check is what keeps this a refusal the
        pipeline understands rather than a stray parser exception. Found when
        mutation M32 (disable the alias check) survived the anchor-based test."""
        with pytest.raises(CompilationError, match="aliases"):
            load_strategy_yaml("id: *nowhere")

    def test_an_oversized_document_is_refused_before_parsing(self) -> None:
        with pytest.raises(CompilationError, match="larger than"):
            load_strategy_yaml("id: x\n" + "# padding\n" * 10_000)

    def test_the_shipped_strategy_still_loads(self, seed_doc: StrategyDocument) -> None:
        """The control: the bounds must not refuse a real document."""
        assert compile_strategy(seed_doc) is seed_doc
