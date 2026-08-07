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
    def test_too_many_entry_conditions_rejected(self) -> None:
        """Excessive degrees of freedom is an overfitting signal."""
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
                "entry": {"all_of": conditions},
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

    def test_yaml_tags_do_not_construct_objects(self) -> None:
        """safe_load blocks the !!python/object construction vector."""
        malicious = "!!python/object/apply:os.system ['echo pwned']\n"
        with pytest.raises((CompilationError, Exception)):
            load_strategy_yaml(malicious)

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
