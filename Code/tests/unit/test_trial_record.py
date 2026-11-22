"""E12-S04: a TrialRecord is something the registry can store, and says something
coherent - refused at construction, not at the INSERT."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest

from algotrader.common.db.repositories import PRE_REGISTRY_TRIALS, TrialRecord
from algotrader.common.enums import StrategyOrigin, TrialOutcome

HASH = "ab" * 32


def record(**overrides: Any) -> TrialRecord:
    base: dict[str, Any] = {
        "strategy_id": "orb_classic",
        "strategy_hash": HASH,
        "origin": StrategyOrigin.USER_AUTHORED,
        "outcome": TrialOutcome.REJECTED,
        "failed_check": "G6",
        "report": {"gauntlet": "probe"},
    }
    base.update(overrides)
    return TrialRecord(**base)


def test_a_coherent_rejection_and_pass_construct() -> None:
    """Control: everything below refuses for its own reason."""
    assert record().outcome is TrialOutcome.REJECTED
    passed = record(
        outcome=TrialOutcome.PASSED,
        failed_check=None,
        observed_sharpe=Decimal("1.9"),
        deflated_sharpe=Decimal("0.4"),
        pbo=Decimal("0.12"),
        trade_count=140,
        generation_batch_id=uuid.uuid4(),
    )
    assert passed.failed_check is None


def test_string_enums_are_normalised() -> None:
    made = record(origin="AI_PROPOSED_JOURNAL", outcome="REJECTED")
    assert made.origin is StrategyOrigin.AI_PROPOSED_JOURNAL
    assert made.outcome is TrialOutcome.REJECTED


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        (
            {"strategy_hash": "AB" * 32},
            "content hash",
        ),  # uppercase is not what content_hash() writes
        ({"strategy_hash": "ab" * 31}, "content hash"),
        ({"strategy_id": ""}, "strategy_id"),
        ({"strategy_id": "x" * 65}, "strategy_id"),
        ({"failed_check": None}, "names the check"),
        ({"failed_check": "G13"}, "names the check"),
        ({"failed_check": "G0"}, "names the check"),
        ({"outcome": TrialOutcome.PASSED}, "PASSED trial failed no check"),  # still names G6
        ({"observed_sharpe": Decimal("NaN")}, "observed_sharpe"),
        ({"deflated_sharpe": Decimal("10000")}, "deflated_sharpe"),  # NUMERIC(8,4) overflows
        ({"pbo": Decimal("1.01")}, "probability"),
        ({"pbo": Decimal("-0.01")}, "probability"),
        ({"trade_count": -1}, "trade_count"),
        ({"report": {}}, "report"),
    ],
)
def test_an_incoherent_trial_is_refused(overrides: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        record(**overrides)


def test_an_unknown_origin_or_outcome_is_refused() -> None:
    with pytest.raises(ValueError):
        record(origin="SOMETHING_ELSE")
    with pytest.raises(ValueError):
        record(outcome="MAYBE")


def test_the_pre_registry_count_is_a_recorded_decision() -> None:
    """Zero because no backtest has ever run in this system (E12-S04). Changing
    it is a decision with a reason, made where the constant is documented."""
    assert PRE_REGISTRY_TRIALS == 0
