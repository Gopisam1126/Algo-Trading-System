"""E13-S01 - the signal engine, attacked.

LLA §2.2: exactly one service holds broker write credentials and can place
orders, and it is not this one. Checked structurally, with controls:

1. **The signals package cannot reach an order.** It imports nothing from
   ``algotrader.execution`` or ``algotrader.broker``, no broker SDK, and no
   ORM - asserted on the AST, so an import inside a function counts.
2. **A SHADOW trigger has one destination.** The recorder holds a store that
   can insert shadow signals and nothing else, and the loop will not accept
   anything else as the SHADOW route.
3. **Absent context refuses.** Until E09 publishes a market context, no
   strategy - each declares its validated regimes - can fire.
4. **Hostile volume makes no state.** A flood of bars for symbols outside the
   plan folds nothing and remembers nothing.
"""

from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import inspect
import random
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from algotrader.common.calendar import IST, MarketCalendar
from algotrader.common.enums import Regime, StrategyState, Timeframe
from algotrader.common.models.market import Bar
from algotrader.indicators.engine import IndicatorEngine
from algotrader.signals.loop import (
    BarOutcome,
    MarketFacts,
    NoMarketContext,
    SessionPlan,
    ShadowRecorder,
    ShadowStore,
    SignalLoop,
    StrategyOutcome,
    SymbolFacts,
    signal_id,
)
from algotrader.strategy.dsl import load_strategy_yaml
from algotrader.strategy.registry import RunnableStrategy, row_for, verify_row
from algotrader.strategy.runtime import StrategyEvaluator

SIGNALS = Path(__file__).resolve().parents[2] / "src" / "algotrader" / "signals"
DAY = dt.date(2026, 8, 20)
CAL = MarketCalendar()

#: What the signal engine may not import. ``algotrader.common.db`` is the ORM
#: boundary (E01-S02); the rest is the order path.
FORBIDDEN = (
    "algotrader.execution",
    "algotrader.broker",
    "kiteconnect",
    "sqlalchemy",
    "algotrader.common.db",
)


def ist(hour: int, minute: int, day: dt.date = DAY) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=IST).astimezone(dt.UTC)


def _imports(source: str) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


class TestTheSignalsPackageCannotReachAnOrder:
    def test_nothing_in_it_imports_the_order_path_or_the_orm(self) -> None:
        files = sorted(SIGNALS.rglob("*.py"))
        assert len(files) >= 3, "the probe is not looking at the package"
        offenders = [
            f"{path.name}: {name}"
            for path in files
            for name in _imports(path.read_text(encoding="utf-8"))
            if any(name == f or name.startswith(f + ".") for f in FORBIDDEN)
        ]
        assert offenders == []

    def test_the_walk_would_see_one(self) -> None:
        """The control: an import tucked inside a function is still found."""
        names = _imports("def f():\n    from algotrader.execution.gateway import OrderGateway\n")
        assert any(n.startswith("algotrader.execution") for n in names)


class TestAShadowTriggerHasOneDestination:
    def test_the_recorder_holds_a_store_and_nothing_else(self) -> None:
        recorder = ShadowRecorder(object())  # type: ignore[arg-type]
        assert list(vars(recorder)) == ["_store"]
        methods = {
            n
            for n, _ in inspect.getmembers(ShadowStore, inspect.isfunction)
            if not n.startswith("_")
        }
        assert methods == {"record_shadow_signal"}

    def test_the_loop_takes_only_a_recorder_as_the_shadow_route(self) -> None:
        class LooksLikeOne:
            async def record(self, trigger: Any, strategy: Any) -> None:
                return None

        with pytest.raises(TypeError, match="ShadowRecorder"):
            _loop([], shadow=LooksLikeOne())


_FIRES_ON_ANY_RISE = """
id: sec_probe
name: Security probe
origin: AI_PROPOSED_JOURNAL
created_at: 2026-08-01T00:00:00+00:00
created_by: claude
direction: LONG
hypothesis:
  mechanism: >-
    Fires whenever price is above its twenty-period average, which a rising
    session satisfies on every bar - so the only thing that can stop it here
    is the absence of the context it was validated in.
  why_it_should_persist: >-
    It is a test fixture rather than a traded strategy, so persistence is not
    a property it is claimed to have.
  expected_failure_mode: >-
    It fires on any sufficiently strong upward move, including noise.
applicability:
  regimes: [TRENDING, RISK_ON, RISK_OFF, NEUTRAL, HIGH_VOL, LOW_VOL, RANGEBOUND]
  timeframe: 5m
entry:
  all_of:
    - {primitive: price_above_ma, params: {period: 20}}
exit:
  stop: {primitive: atr_stop, params: {multiplier: 1.5}}
  time: {primitive: squareoff_deadline}
"""


class _Rows:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def record_shadow_signal(self, row: dict[str, Any]) -> int:
        self.rows.append(row)
        return len(self.rows)


class _Sink:
    def __init__(self) -> None:
        self.received: list[Any] = []

    async def submit(self, trigger: Any, strategy: Any) -> None:
        self.received.append(trigger)


class _NoEntries:
    async def entries_on(self, *_: Any) -> int:
        return 0


def _warm(engine: IndicatorEngine, symbol: str) -> None:
    rng = random.Random(7)
    last = {
        Timeframe.M5: ist(15, 25, dt.date(2026, 8, 19)),
        Timeframe.M15: ist(15, 15, dt.date(2026, 8, 19)),
        Timeframe.H1: ist(15, 15, dt.date(2026, 8, 19)),
    }
    for tf in engine.timeframes:
        price, bars = 900.0, []
        for i in range(260, 0, -1):
            price *= 1 + rng.uniform(-0.002, 0.0026)
            p = Decimal(f"{price:.2f}")
            bars.append(
                Bar(
                    symbol=symbol,
                    timeframe=tf,
                    open=p,
                    high=p + 1,
                    low=p - 1,
                    close=p,
                    volume=10_000,
                    open_ts=last[tf] - dt.timedelta(seconds=tf.seconds * (i - 1)),
                )
            )
        engine.set_for(symbol, tf).warm_up(bars)


def _loop(
    strategies: list[RunnableStrategy],
    *,
    shadow: Any = None,
    live: Any = None,
    paper: Any = None,
    now: dt.datetime | None = None,
) -> SignalLoop:
    engine = IndicatorEngine()
    _warm(engine, "INFY")
    return SignalLoop(
        plan=SessionPlan(DAY, ist(9, 12), frozenset({"INFY"})),
        facts={
            "INFY": SymbolFacts("INFY", Decimal("0.05"), CAL.deadline_for(DAY, is_cas_stock=None))
        },
        strategies=strategies,
        engine=engine,
        publish=_nowhere,
        calendar=CAL,
        entries=_NoEntries(),
        live=live or _Sink(),
        paper=paper or _Sink(),
        shadow=shadow if shadow is not None else ShadowRecorder(_Rows()),
        market=NoMarketContext(),
        clock=lambda: now or ist(15, 0),
    )


async def _nowhere(_set: Any) -> None:
    return None


def _runnable(state: StrategyState) -> RunnableStrategy:
    doc = load_strategy_yaml(_FIRES_ON_ANY_RISE)
    record = verify_row(row_for(doc, now=ist(9, 0, dt.date(2026, 8, 19))))
    record = dataclasses.replace(
        record,
        state=state,
        approved_by="operator" if state.places_live_orders else None,
        approved_at=ist(9, 0, dt.date(2026, 8, 19)) if state.places_live_orders else None,
    )
    return RunnableStrategy(record=record, evaluator=StrategyEvaluator(doc))


class _Trending:
    async def facts(self, now: dt.datetime) -> MarketFacts:
        return MarketFacts(regime=Regime.TRENDING)


async def _rising_session(loop: SignalLoop) -> list[StrategyOutcome]:
    moment, price, decisions = ist(9, 15), 1000.0, []
    while moment < ist(11, 0):
        p = Decimal(f"{price:.2f}")
        bar = Bar(
            symbol="INFY",
            timeframe=Timeframe.M5,
            open_ts=moment,
            open=p,
            high=p + 1,
            low=p - 1,
            close=p,
            volume=20_000,
        )
        loop._clock = lambda m=moment: m + dt.timedelta(minutes=5, seconds=1)  # type: ignore[method-assign]
        report = await loop.on_bar(bar)
        assert report.outcome is BarOutcome.EVALUATED
        decisions += [d.outcome for d in report.decisions]
        moment += dt.timedelta(minutes=5)
        price += 3
    return decisions


class TestAbsentContextRefuses:
    @pytest.mark.parametrize(
        "state", [s for s in StrategyState if s.is_runnable], ids=lambda s: s.value
    )
    async def test_with_no_market_context_nothing_fires_in_any_state(
        self, state: StrategyState
    ) -> None:
        live, paper, rows = _Sink(), _Sink(), _Rows()
        loop = _loop([_runnable(state)], live=live, paper=paper, shadow=ShadowRecorder(rows))
        decisions = await _rising_session(loop)
        assert decisions and set(decisions) == {StrategyOutcome.NO_SIGNAL}
        assert live.received == paper.received == rows.rows == []

    async def test_the_control_with_a_regime_the_same_session_fires(self) -> None:
        """Without this the test above could pass on a strategy that never fires."""
        paper = _Sink()
        loop = _loop([_runnable(StrategyState.PAPER)], paper=paper)
        loop._market = _Trending()  # type: ignore[assignment]
        decisions = await _rising_session(loop)
        assert StrategyOutcome.ROUTED in decisions and paper.received


class TestHostileVolumeMakesNoState:
    async def test_a_flood_of_unplanned_symbols_folds_and_remembers_nothing(self) -> None:
        loop = _loop([])
        sets_before = set(loop._engine._sets)
        rng = random.Random(1)
        for i in range(2_000):
            symbol = "X" + "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(6))
            bar = Bar(
                symbol=symbol,
                timeframe=Timeframe.M5,
                open_ts=ist(9, 15) + dt.timedelta(minutes=5 * (i % 60)),
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=1,
            )
            assert (await loop.on_bar(bar)).outcome is BarOutcome.NOT_IN_PLAN
        assert set(loop._engine._sets) == sets_before
        assert loop._days == {}


class TestTheSignalIdentity:
    @settings(max_examples=80, deadline=None)
    @given(
        minutes=st.integers(min_value=0, max_value=74),
        other=st.integers(min_value=0, max_value=74),
        sid=st.from_regex(r"[a-z0-9_]{3,20}", fullmatch=True),
    )
    def test_one_signal_per_strategy_per_bar(self, minutes: int, other: int, sid: str) -> None:
        def bar(m: int) -> Bar:
            return Bar(
                symbol="INFY",
                timeframe=Timeframe.M5,
                open_ts=ist(9, 15) + dt.timedelta(minutes=5 * m),
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=1,
            )

        assert signal_id(sid, bar(minutes)) == signal_id(sid, bar(minutes).model_copy())
        assert (signal_id(sid, bar(minutes)) == signal_id(sid, bar(other))) == (minutes == other)
        assert signal_id(sid, bar(minutes)) != signal_id(sid + "x", bar(minutes))
