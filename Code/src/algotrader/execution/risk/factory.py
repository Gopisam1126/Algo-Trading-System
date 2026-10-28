"""The one production way to build a risk engine (E15-S13).

## The design record

**Decision.** ``build_risk_engine`` assembles all fourteen checks from config,
in canonical order, binds the ATR sizer, and refuses to return an engine whose
check ids are not exactly :func:`all_check_ids`.

**Alternative rejected.** Letting the execution service construct a
``RiskEngine`` itself. ``RiskEngine(checks=[], sizer=...)`` is a legitimate
TEST shape - forty-odd tests use it to exercise sizing through the engine -
and in production it approves everything. A caller assembling the list by hand
is one forgotten builder away from an engine missing the kill switch, and
nothing about that engine would look wrong: it evaluates, audits and approves.

**The failure it prevents.** A production engine with fewer than fourteen
gates. The 27 Oct 2026 verification recorded it as a hard constraint on this
story (QA-VER-01).

**What would make this wrong.** A legitimate production configuration that
runs a subset of the checks - say, a paper mode that skips the margin check.
That would need a second, named factory with its own assertion, never a
parameter that removes gates from this one.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from algotrader.execution.risk.checks import (
    all_check_ids,
    build_eligibility_checks,
    build_exposure_checks,
    build_loss_checks,
    build_margin_timing_checks,
    build_precondition_checks,
)
from algotrader.execution.risk.framework import RiskCheckError, RiskEngine
from algotrader.execution.sizer import SizingPolicy, build_sizer

if TYPE_CHECKING:
    from algotrader.common.calendar import MarketCalendar
    from algotrader.common.config import AppConfig


def sizing_policy(config: AppConfig) -> SizingPolicy:
    """The sizer's numbers, read from config in one place."""
    risk = config.risk
    return SizingPolicy(
        risk_pct=risk.per_trade.risk_pct,
        atr_multiplier_stop=risk.per_trade.atr_multiplier_stop,
        max_position_pct=risk.per_trade.max_position_pct,
        capital_per_slot_pct=risk.capital_per_slot_pct,
        target_r_multiple=risk.per_trade.target_r_multiple,
        max_sector_exposure_pct=risk.portfolio.max_sector_exposure_pct,
        max_net_directional_exposure_pct=risk.portfolio.max_net_directional_exposure_pct,
    )


def build_risk_engine(
    config: AppConfig,
    *,
    calendar: MarketCalendar,
    audit: Callable[[dict[str, object]], None] | None = None,
) -> RiskEngine:
    """All fourteen checks, in order, with the sizer. Or a refusal."""
    risk = config.risk
    checks = (
        *build_precondition_checks(calendar, config.execution.no_trade_windows),
        *build_eligibility_checks(),
        *build_exposure_checks(
            max_correlated_positions=risk.portfolio.max_correlated_positions,
            correlation_threshold=risk.portfolio.correlation_threshold,
            max_sector_exposure_pct=risk.portfolio.max_sector_exposure_pct,
            max_net_directional_exposure_pct=risk.portfolio.max_net_directional_exposure_pct,
        ),
        *build_loss_checks(
            max_daily_loss_pct=risk.portfolio.max_daily_loss_pct,
            consecutive_loss_halt=risk.portfolio.consecutive_loss_halt,
        ),
        *build_margin_timing_checks(min_minutes_to_squareoff=risk.min_minutes_to_squareoff),
    )
    built = tuple(check.id for check in checks)
    expected = all_check_ids()
    if built != expected:
        raise RiskCheckError(
            f"the production risk engine must run all {len(expected)} checks in "
            f"canonical order; built {built}, expected {expected}"
        )
    return RiskEngine(checks=checks, sizer=build_sizer(sizing_policy(config)), audit=audit)
