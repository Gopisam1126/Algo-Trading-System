"""E15-S13: the one production way to build a risk engine.

QA-VER-01: ``RiskEngine(checks=[])`` is a legitimate test shape and, in
production, an engine that approves everything. The factory assembles all
fourteen checks and refuses to return anything else.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from algotrader.common.calendar import MarketCalendar, load_holidays_with_status
from algotrader.common.config import AppConfig
from algotrader.execution.risk import factory
from algotrader.execution.risk.checks import all_check_ids
from algotrader.execution.risk.framework import RiskCheckError


def _calendar() -> MarketCalendar:
    path = Path(__file__).resolve().parents[2] / "config" / "nse_holidays.yaml"
    status = load_holidays_with_status(str(path))
    return MarketCalendar(status.dates, covers_years=status.covers_years)


class TestTheFactory:
    def test_it_builds_all_fourteen_checks_in_canonical_order(self) -> None:
        engine = factory.build_risk_engine(AppConfig(), calendar=_calendar())
        assert engine.check_ids == all_check_ids()
        assert len(engine.check_ids) == 14

    def test_it_binds_a_sizer(self) -> None:
        """An engine without one can only reject; the factory never returns that."""
        assert factory.build_risk_engine(AppConfig(), calendar=_calendar()).sizer is not None

    def test_it_refuses_an_engine_missing_a_check(self, monkeypatch) -> None:
        real = factory.build_loss_checks

        def one_short(**kwargs):
            return real(**kwargs)[:1]

        monkeypatch.setattr(factory, "build_loss_checks", one_short)
        with pytest.raises(RiskCheckError, match="must run all 14 checks"):
            factory.build_risk_engine(AppConfig(), calendar=_calendar())

    def test_it_refuses_the_checks_out_of_order(self, monkeypatch) -> None:
        real = factory.build_loss_checks

        def swapped(**kwargs):
            return tuple(reversed(real(**kwargs)))

        monkeypatch.setattr(factory, "build_loss_checks", swapped)
        with pytest.raises(RiskCheckError, match="canonical order"):
            factory.build_risk_engine(AppConfig(), calendar=_calendar())

    def test_the_sizing_policy_is_read_from_config(self) -> None:
        config = AppConfig(
            risk={
                "capital_per_slot_pct": "15",
                "per_trade": {"risk_pct": "0.5", "atr_multiplier_stop": "2.0"},
                "portfolio": {"max_sector_exposure_pct": "30"},
            }
        )
        policy = factory.sizing_policy(config)
        assert (policy.risk_pct, policy.atr_multiplier_stop) == (0.5, 2)
        assert policy.capital_per_slot_pct == 15
        assert policy.max_sector_exposure_pct == 30

    def test_the_engine_writes_to_the_audit_it_is_given(self) -> None:
        seen: list[dict] = []
        engine = factory.build_risk_engine(AppConfig(), calendar=_calendar(), audit=seen.append)
        assert engine.audit is not None
        engine.audit({"probe": True})
        assert seen == [{"probe": True}]
