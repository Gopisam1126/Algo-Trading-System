"""Every shipped strategy compiles AND can run here (audit TRD-014, TRD-010).

validate_strategies.py and the registry's seeding compiled orb_classic and
reported it OK, while it asked for daily and weekly agreement the indicator
engine does not carry, so every load refused it. And its news guard sat under
``none_of``, so it could enter only on news days. These pin both, against the
file itself.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest

from algotrader.common.enums import Direction, Regime, Timeframe
from algotrader.indicators.engine import MultiTimeframeSnapshot
from algotrader.indicators.levels import OpeningRange
from algotrader.strategy import primitives  # noqa: F401 - populates REGISTRY
from algotrader.strategy.context import EvalContext
from algotrader.strategy.dsl import compile_strategy, load_strategy_yaml
from algotrader.strategy.runtime import StrategyEvaluator
from tests.doubles import nse_equity_tick

CODE = Path(__file__).resolve().parents[2]
STRATEGIES = CODE / "config" / "strategies"
SHIPPED = sorted(STRATEGIES.glob("*.yaml"))


@pytest.mark.parametrize("path", SHIPPED, ids=lambda p: p.name)
def test_every_shipped_strategy_can_be_evaluated_here(path: Path) -> None:
    StrategyEvaluator(compile_strategy(load_strategy_yaml(path.read_text(encoding="utf-8"))))


def _validator():
    spec = importlib.util.spec_from_file_location(
        "validate_strategies", CODE / "scripts" / "validate_strategies.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_validator_passes_the_shipped_files() -> None:
    assert _validator().main() == 0


def test_the_validator_fails_a_strategy_the_engine_cannot_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acceptance criterion: the pre-fix orb_classic exits non-zero."""
    module = _validator()
    strategies = tmp_path / "config" / "strategies"
    strategies.mkdir(parents=True)
    text = (STRATEGIES / "orb_classic.yaml").read_text(encoding="utf-8")
    (strategies / "old.yaml").write_text(
        text.replace('of: "15m,1h"', 'of: "1h,1d,1w"'), encoding="utf-8"
    )
    monkeypatch.setattr(module, "__file__", str(tmp_path / "scripts" / "validate_strategies.py"))
    assert module.main() == 1


# -- orb_classic enters on a quiet day, not a news day -------------------------

NOW = dt.datetime(2026, 8, 20, 5, 0, tzinfo=dt.UTC)  # 10:30 IST
_TREND = {"ema_20": 1010.0, "ema_50": 1000.0}


def _ctx(hours_since_material_news: float | None) -> EvalContext:
    snapshot = MultiTimeframeSnapshot(
        symbol="INFY",
        per_timeframe={
            Timeframe.M5: {**_TREND, "volume_ratio_20": 2.0},
            Timeframe.M15: dict(_TREND),
            Timeframe.H1: dict(_TREND),
        },
        all_ready=True,
    )
    return EvalContext(
        symbol="INFY",
        now=NOW,
        timeframe=Timeframe.M5,
        direction=Direction.LONG,
        last_price=Decimal("1160"),
        tick_size=nse_equity_tick(Decimal("1160")),
        previous_bar_close=Decimal("1148"),
        snapshot=snapshot,
        opening_range=OpeningRange(
            symbol="INFY",
            trade_date=dt.date(2026, 8, 20),
            high=Decimal("1150"),
            low=Decimal("1140"),
            sealed=True,
        ),
        index_change_pct={"NIFTY": Decimal("0.4")},
        bars_since_open=15,
        bars_until_squareoff=50,
        regime=Regime.TRENDING,
        entries_today=0,
        hours_since_material_news=hours_since_material_news,
    )


@pytest.fixture(scope="module")
def orb() -> StrategyEvaluator:
    text = (STRATEGIES / "orb_classic.yaml").read_text(encoding="utf-8")
    return StrategyEvaluator(compile_strategy(load_strategy_yaml(text)))


def test_orb_classic_enters_when_no_material_news_landed(orb: StrategyEvaluator) -> None:
    decision = orb.evaluate(_ctx(hours_since_material_news=100.0))
    assert decision.fired, [r.describe() for r in decision.results]


def test_orb_classic_does_not_enter_on_a_news_day(orb: StrategyEvaluator) -> None:
    decision = orb.evaluate(_ctx(hours_since_material_news=1.0))
    assert not decision.fired and decision.outcome is False


def test_orb_classic_does_not_enter_when_nobody_reported_the_news(orb: StrategyEvaluator) -> None:
    """No news source is wired yet: UNKNOWN, and a strategy never fires on it."""
    decision = orb.evaluate(_ctx(hours_since_material_news=None))
    assert not decision.fired and decision.outcome is None
