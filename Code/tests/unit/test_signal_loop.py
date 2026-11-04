"""E13-S01 - the strategy evaluation loop, one acceptance criterion at a time.

Real indicator engine, real calendar, real strategy records. The evaluator is
a spy where a test must control whether a strategy fires and see the context
it was given; the integration tests and SIT-27 run the real evaluator.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import random
from decimal import Decimal
from typing import Any

import pytest

from algotrader.common.calendar import IST, MarketCalendar
from algotrader.common.enums import Direction, Regime, StrategyState, Timeframe
from algotrader.common.models.market import Bar
from algotrader.common.models.trading import Trigger
from algotrader.indicators.engine import IndicatorEngine, IndicatorSet
from algotrader.signals import loop as loop_module
from algotrader.signals.loop import (
    IN_PLAN_STATUSES,
    MAX_EVALUATION_LAG,
    ROUTE_BY_STATE,
    BarOutcome,
    MarketFacts,
    Route,
    SessionPlan,
    ShadowRecorder,
    SignalLoop,
    StrategyOutcome,
    SymbolFacts,
    load_session_plan,
    load_symbol_facts,
    signal_id,
)
from algotrader.strategy.dsl import load_strategy_yaml
from algotrader.strategy.registry import RunnableStrategy, row_for, verify_row

DAY = dt.date(2026, 8, 20)  # a Thursday
YESTERDAY = dt.date(2026, 8, 19)
CAL = MarketCalendar()


def ist(day: dt.date, hour: int, minute: int) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=IST).astimezone(dt.UTC)


def _bar(symbol: str, tf: Timeframe, open_ts: dt.datetime, price: float, **kw: Any) -> Bar:
    p = Decimal(f"{price:.2f}")
    return Bar(
        symbol=symbol,
        timeframe=tf,
        open_ts=open_ts,
        open=kw.pop("open", p),
        high=kw.pop("high", p + Decimal("1")),
        low=kw.pop("low", p - Decimal("1")),
        close=kw.pop("close", p),
        volume=kw.pop("volume", 10_000),
        **kw,
    )


def warm(engine: IndicatorEngine, symbol: str) -> None:
    """260 bars per timeframe ending at yesterday's last slot - enough for
    every indicator, and a last bar from a previous day, so today's first
    expected bar is the 09:15 slot."""
    rng = random.Random(symbol)
    last_slot = {
        Timeframe.M5: ist(YESTERDAY, 15, 25),
        Timeframe.M15: ist(YESTERDAY, 15, 15),
        Timeframe.H1: ist(YESTERDAY, 15, 15),
    }
    for tf in engine.timeframes:
        price = 1000.0
        bars = []
        for i in range(260, 0, -1):
            price *= 1 + rng.uniform(-0.002, 0.0025)
            bars.append(
                _bar(symbol, tf, last_slot[tf] - dt.timedelta(seconds=tf.seconds * (i - 1)), price)
            )
        engine.set_for(symbol, tf).warm_up(bars)
    assert engine.snapshot(symbol).all_ready


STRATEGY_YAML = """
id: {sid}
name: Loop probe {sid}
origin: USER_AUTHORED
created_at: 2026-08-01T00:00:00+00:00
created_by: qa
direction: LONG
hypothesis:
  mechanism: >-
    A probe strategy for the signal loop's tests: the evaluator is replaced by
    a spy, so this document only has to be a valid, registrable strategy.
  why_it_should_persist: >-
    It is a test fixture rather than a traded strategy, so persistence is not
    a property it is claimed to have.
  expected_failure_mode: >-
    It is never traded; it fails whenever a test says it does.
applicability: {{regimes: [TRENDING], timeframe: {tf}}}
entry:
  all_of:
    - {{primitive: volume_ratio_above, params: {{threshold: {threshold}}}}}
exit:
  stop: {{primitive: atr_stop, params: {{multiplier: 1.5}}}}
  time: {{primitive: squareoff_deadline}}
"""


class SpyEvaluator:
    """Fires when told to, and keeps every context it was handed."""

    def __init__(self, strategy_id: str, fires: bool = True) -> None:
        self.strategy_id = strategy_id
        self.fires = fires
        self.contexts: list[Any] = []
        self.ids: list[Any] = []

    def fire(self, ctx: Any, *, correlation_id: Any) -> Trigger | None:
        self.contexts.append(ctx)
        self.ids.append(correlation_id)
        if not self.fires:
            return None
        return Trigger(
            correlation_id=correlation_id,
            symbol=ctx.symbol,
            strategy_id=self.strategy_id,
            direction=Direction.LONG,
            trigger_price=ctx.last_price,
            suggested_stop=ctx.last_price - Decimal("5"),
            timeframe_agreement=2,
            fired_at=ctx.now,
        )


def strategy(
    sid: str, state: StrategyState, *, tf: str = "5m", fires: bool = True, threshold: float = 1.5
) -> RunnableStrategy:
    doc = load_strategy_yaml(STRATEGY_YAML.format(sid=sid, tf=tf, threshold=threshold))
    record = dataclasses.replace(
        verify_row(row_for(doc, now=ist(YESTERDAY, 9, 0))),
        state=state,
        approved_by="operator" if state.places_live_orders else None,
        approved_at=ist(YESTERDAY, 9, 0) if state.places_live_orders else None,
    )
    return RunnableStrategy(record=record, evaluator=SpyEvaluator(sid, fires))  # type: ignore[arg-type]


class Sink:
    def __init__(self, name: str, log: list[str], *, fails: bool = False) -> None:
        self.name, self.log, self.fails = name, log, fails
        self.received: list[Trigger] = []

    async def submit(self, trigger: Trigger, strategy: RunnableStrategy) -> None:
        self.log.append(f"{self.name}:{trigger.symbol}")
        if self.fails:
            raise RuntimeError("downstream refused")
        self.received.append(trigger)


class ShadowRows:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.rows: list[dict[str, Any]] = []

    async def record_shadow_signal(self, row: dict[str, Any]) -> int:
        self.log.append(f"shadow:{row['symbol']}")
        self.rows.append(row)
        return len(self.rows)


class Entries:
    def __init__(self, count: int | Exception = 0) -> None:
        self.count = count

    async def entries_on(self, trade_date: dt.date, strategy_id: str, symbol: str) -> int:
        if isinstance(self.count, Exception):
            raise self.count
        return self.count


class Market:
    def __init__(self, facts: MarketFacts | Exception | None) -> None:
        self._facts = facts

    async def facts(self, now: dt.datetime) -> MarketFacts | None:
        if isinstance(self._facts, Exception):
            raise self._facts
        return self._facts


TRENDING = MarketFacts(regime=Regime.TRENDING, india_vix=Decimal("13.5"))


class Desk:
    """A loop over INFY and TCS, with every collaborator observable."""

    def __init__(
        self,
        strategies: list[RunnableStrategy],
        *,
        symbols: frozenset[str] = frozenset({"INFY", "TCS"}),
        clock: dt.datetime | None = None,
        publish_fails: bool = False,
        entries: Entries | None = None,
        market: Market | None = None,
        live_fails: bool = False,
    ) -> None:
        self.events: list[str] = []
        self.engine = IndicatorEngine()
        for s in symbols:
            warm(self.engine, s)
        self.live = Sink("live", self.events, fails=live_fails)
        self.paper = Sink("paper", self.events)
        self.shadow_rows = ShadowRows(self.events)
        self.published: list[tuple[str, Timeframe, bool]] = []
        self.now = clock
        self.publish_fails = publish_fails
        self.facts = {
            s: SymbolFacts(
                symbol=s,
                tick_size=Decimal("0.05"),
                squareoff_deadline=CAL.deadline_for(DAY, is_cas_stock=None),
                sector="IT",
            )
            for s in symbols
        }
        self.loop = SignalLoop(
            plan=SessionPlan(trade_date=DAY, locked_at=ist(DAY, 9, 12), symbols=symbols),
            facts=self.facts,
            strategies=strategies,
            engine=self.engine,
            publish=self._publish,
            calendar=CAL,
            entries=entries or Entries(0),
            live=self.live,
            paper=self.paper,
            shadow=ShadowRecorder(self.shadow_rows),
            market=market or Market(TRENDING),
            clock=self._clock,
        )

    def _clock(self) -> dt.datetime:
        return self.now if self.now is not None else self._latest + dt.timedelta(seconds=2)

    _latest = ist(DAY, 9, 15)

    async def _publish(self, indicator_set: IndicatorSet) -> None:
        self.events.append(f"publish:{indicator_set.symbol}:{indicator_set.timeframe.value}")
        if self.publish_fails:
            raise ConnectionError("redis is down")
        self.published.append(
            (indicator_set.symbol, indicator_set.timeframe, indicator_set.is_ready)
        )

    async def feed(self, bar: Bar):  # type: ignore[no-untyped-def]
        self._latest = bar.open_ts + dt.timedelta(seconds=bar.timeframe.seconds)
        return await self.loop.on_bar(bar)

    async def session(self, symbol: str, until: tuple[int, int], *, start: float = 1010.0):  # type: ignore[no-untyped-def]
        """Contiguous 5m bars from 09:15 up to (not including) ``until``."""
        reports = []
        moment, price = ist(DAY, 9, 15), start
        while moment < ist(DAY, *until):
            reports.append(await self.feed(_bar(symbol, Timeframe.M5, moment, price)))
            moment += dt.timedelta(minutes=5)
            price += 0.5
        return reports


# ---------------------------------------------------------------------------
# The routing table
# ---------------------------------------------------------------------------


class TestTheRoutingTable:
    def test_it_covers_exactly_the_runnable_states(self) -> None:
        assert set(ROUTE_BY_STATE) == {s for s in StrategyState if s.is_runnable}

    def test_only_live_order_states_route_live_and_shadow_routes_to_shadow(self) -> None:
        assert ROUTE_BY_STATE[StrategyState.SHADOW] is Route.SHADOW
        assert ROUTE_BY_STATE[StrategyState.PAPER] is Route.PAPER
        assert {s for s, r in ROUTE_BY_STATE.items() if r is Route.LIVE} == {
            s for s in StrategyState if s.places_live_orders
        }

    @pytest.mark.parametrize(
        "table",
        [
            pytest.param(
                {
                    StrategyState.PAPER: Route.PAPER,
                    StrategyState.ACTIVE: Route.LIVE,
                    StrategyState.DEGRADED: Route.LIVE,
                },
                id="a-runnable-state-missing",
            ),
            pytest.param(
                {
                    StrategyState.SHADOW: Route.LIVE,
                    StrategyState.PAPER: Route.PAPER,
                    StrategyState.ACTIVE: Route.LIVE,
                    StrategyState.DEGRADED: Route.LIVE,
                },
                id="shadow-routed-live",
            ),
            pytest.param(
                {
                    StrategyState.SHADOW: Route.SHADOW,
                    StrategyState.PAPER: Route.PAPER,
                    StrategyState.ACTIVE: Route.PAPER,
                    StrategyState.DEGRADED: Route.LIVE,
                },
                id="active-routed-to-paper",
            ),
        ],
    )
    def test_the_import_time_check_refuses_a_wrong_table(
        self, monkeypatch: pytest.MonkeyPatch, table: dict[StrategyState, Route]
    ) -> None:
        monkeypatch.setattr(loop_module, "ROUTE_BY_STATE", table)
        with pytest.raises(RuntimeError):
            loop_module._check_routes()


# ---------------------------------------------------------------------------
# AC1 - only today's plan
# ---------------------------------------------------------------------------


class PlanRows:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row

    async def locked_plan(self, trade_date: dt.date) -> dict[str, Any] | None:
        return self.row


class TestOnlyTodaysPlan:
    async def test_a_symbol_not_in_the_plan_is_neither_folded_nor_evaluated(self) -> None:
        spy = strategy("probe_live", StrategyState.ACTIVE)
        desk = Desk([spy])
        report = await desk.feed(_bar("WIPRO", Timeframe.M5, ist(DAY, 9, 15), 500))
        assert report.outcome is BarOutcome.NOT_IN_PLAN
        assert ("WIPRO", Timeframe.M5) not in desk.engine._sets, "the bar was folded"
        assert desk.events == [] and spy.evaluator.contexts == []  # type: ignore[attr-defined]

    async def test_a_bar_from_another_day_is_refused(self) -> None:
        desk = Desk([strategy("probe_live", StrategyState.ACTIVE)])
        before = desk.engine.set_for("INFY", Timeframe.M5).bars_seen
        report = await desk.feed(_bar("INFY", Timeframe.M5, ist(dt.date(2026, 8, 21), 9, 15), 1000))
        assert report.outcome is BarOutcome.WRONG_DAY
        assert desk.engine.set_for("INFY", Timeframe.M5).bars_seen == before

    async def test_a_forming_bar_is_refused(self) -> None:
        desk = Desk([strategy("probe_live", StrategyState.ACTIVE)])
        report = await desk.feed(_bar("INFY", Timeframe.M5, ist(DAY, 9, 15), 1000, is_final=False))
        assert report.outcome is BarOutcome.NOT_FINAL

    async def test_a_planned_symbol_without_facts_is_not_evaluated(self) -> None:
        desk = Desk([strategy("probe_live", StrategyState.ACTIVE)])
        del desk.loop._facts["TCS"]
        report = await desk.feed(_bar("TCS", Timeframe.M5, ist(DAY, 9, 15), 1000))
        assert report.outcome is BarOutcome.NO_FACTS and desk.events == []

    def _row(self, **candidates: str) -> dict[str, Any]:
        return {
            "trade_date": DAY,
            "locked_at": ist(DAY, 9, 12),
            "candidates": [{"symbol": s, "status": st} for s, st in candidates.items()],
        }

    async def test_the_plan_is_its_active_and_traded_candidates(self) -> None:
        row = self._row(INFY="ACTIVE", TCS="TRADED", SBIN="GAP_INVALIDATED", WIPRO="AI_VETOED")
        plan = await load_session_plan(PlanRows(row), DAY)
        assert plan is not None and plan.symbols == frozenset({"INFY", "TCS"})
        assert frozenset({"ACTIVE", "TRADED"}) == IN_PLAN_STATUSES

    async def test_an_unknown_status_is_out_of_the_plan(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        plan = await load_session_plan(PlanRows(self._row(INFY="ACTIVE", TCS="active\n")), DAY)
        assert plan is not None and plan.symbols == frozenset({"INFY"})
        assert any("does not know" in r.getMessage() for r in caplog.records)
        assert all("\n" not in r.getMessage() for r in caplog.records)

    async def test_no_locked_plan_means_no_plan(self) -> None:
        assert await load_session_plan(PlanRows(None), DAY) is None

    async def test_a_plan_for_another_day_is_no_plan(self) -> None:
        row = self._row(INFY="ACTIVE") | {"trade_date": YESTERDAY}
        assert await load_session_plan(PlanRows(row), DAY) is None


# ---------------------------------------------------------------------------
# AC2 - routed by state; SHADOW never reaches an order
# ---------------------------------------------------------------------------


class TestRoutedByState:
    @pytest.mark.parametrize(
        ("state", "destination"),
        [
            (StrategyState.ACTIVE, "live"),
            (StrategyState.DEGRADED, "live"),
            (StrategyState.PAPER, "paper"),
            (StrategyState.SHADOW, "shadow"),
        ],
    )
    async def test_a_trigger_goes_where_its_state_says_and_nowhere_else(
        self, state: StrategyState, destination: str
    ) -> None:
        desk = Desk([strategy("probe", state)])
        report = await desk.feed(_bar("INFY", Timeframe.M5, ist(DAY, 9, 15), 1010))
        [decision] = report.decisions
        assert decision.outcome is StrategyOutcome.ROUTED
        assert [e.split(":")[0] for e in desk.events if not e.startswith("publish")] == [
            destination
        ]

    async def test_a_shadow_trigger_is_recorded_as_a_shadow_signal(self) -> None:
        desk = Desk([strategy("probe_shadow", StrategyState.SHADOW)])
        await desk.feed(_bar("INFY", Timeframe.M5, ist(DAY, 9, 15), 1010))
        [row] = desk.shadow_rows.rows
        assert row == {
            "strategy_id": "probe_shadow",
            "symbol": "INFY",
            "signalled_at": ist(DAY, 9, 20),
            "direction": "LONG",
            "price_at_signal": Decimal("1010.00"),
            "hypothetical_stop": Decimal("1005.00"),
        }
        assert desk.live.received == [] and desk.paper.received == []

    def test_the_shadow_route_must_be_a_shadow_recorder(self) -> None:
        desk = Desk([])
        with pytest.raises(TypeError, match="ShadowRecorder"):
            SignalLoop(
                plan=desk.loop.plan,
                facts=desk.facts,
                strategies=[],
                engine=desk.engine,
                publish=desk._publish,
                calendar=CAL,
                entries=Entries(),
                live=desk.live,
                paper=desk.paper,
                shadow=desk.live,  # type: ignore[arg-type]
            )

    @pytest.mark.parametrize(
        "state", [s for s in StrategyState if not s.is_runnable], ids=lambda s: s.value
    )
    def test_a_strategy_that_is_not_runnable_is_refused(self, state: StrategyState) -> None:
        with pytest.raises(ValueError, match="not runnable"):
            Desk([strategy("probe", state)])

    async def test_a_failed_route_is_reported_and_the_rest_still_run(self) -> None:
        live = strategy("probe_live", StrategyState.ACTIVE)
        paper = strategy("probe_paper", StrategyState.PAPER)
        desk = Desk([live, paper], live_fails=True)
        report = await desk.feed(_bar("INFY", Timeframe.M5, ist(DAY, 9, 15), 1010))
        outcomes = {d.strategy_id: d.outcome for d in report.decisions}
        assert outcomes == {
            "probe_live": StrategyOutcome.ROUTE_FAILED,
            "probe_paper": StrategyOutcome.ROUTED,
        }

    async def test_a_strategy_that_raises_does_not_stop_the_others(self) -> None:
        broken = strategy("probe_broken", StrategyState.ACTIVE)

        def explode(ctx: Any, *, correlation_id: Any) -> None:
            raise ValueError("primitive blew up")

        broken.evaluator.fire = explode  # type: ignore[method-assign]
        fine = strategy("probe_fine", StrategyState.PAPER)
        desk = Desk([broken, fine])
        report = await desk.feed(_bar("INFY", Timeframe.M5, ist(DAY, 9, 15), 1010))
        outcomes = {d.strategy_id: d.outcome for d in report.decisions}
        assert outcomes == {
            "probe_broken": StrategyOutcome.ERROR,
            "probe_fine": StrategyOutcome.ROUTED,
        }


# ---------------------------------------------------------------------------
# AC3 - every bar folded once, and a hole stops the symbol
# ---------------------------------------------------------------------------


class TestEachBarOnce:
    async def test_a_redelivered_bar_is_not_folded_again_or_evaluated(self) -> None:
        spy = strategy("probe", StrategyState.PAPER)
        desk = Desk([spy])
        bar = _bar("INFY", Timeframe.M5, ist(DAY, 9, 15), 1010)
        await desk.feed(bar)
        indicator_set = desk.engine.set_for("INFY", Timeframe.M5)
        seen, atr = indicator_set.bars_seen, indicator_set.indicators["atr_14"].value
        again = await desk.feed(bar)
        assert again.outcome is BarOutcome.DUPLICATE
        assert indicator_set.bars_seen == seen and indicator_set.indicators["atr_14"].value == atr
        assert len(desk.paper.received) == 1

    async def test_an_older_bar_is_not_folded(self) -> None:
        desk = Desk([strategy("probe", StrategyState.PAPER)])
        await desk.session("INFY", (9, 30))
        late = await desk.feed(_bar("INFY", Timeframe.M5, ist(DAY, 9, 20), 999))
        assert late.outcome is BarOutcome.DUPLICATE

    async def test_a_skipped_interval_stops_the_symbol_for_the_day(self) -> None:
        desk = Desk([strategy("probe", StrategyState.PAPER)])
        await desk.session("INFY", (9, 30))
        gap = await desk.feed(_bar("INFY", Timeframe.M5, ist(DAY, 9, 35), 1012))  # 09:30 missing
        assert gap.outcome is BarOutcome.GAP
        assert "expected 2026-08-20T04:00:00+00:00" in (gap.detail or "")  # 09:30 IST
        assert not desk.engine.snapshot("INFY").all_ready
        received = len(desk.paper.received)
        after = await desk.feed(_bar("INFY", Timeframe.M5, ist(DAY, 9, 40), 1013))
        assert after.outcome is BarOutcome.NOT_READY
        assert len(desk.paper.received) == received
        # Every timeframe of the symbol is stale, and the published state says so.
        assert desk.published[-1] == ("INFY", Timeframe.M5, False)

    async def test_a_day_that_does_not_start_at_the_open_is_a_gap(self) -> None:
        desk = Desk([strategy("probe", StrategyState.PAPER)])
        first = await desk.feed(_bar("INFY", Timeframe.M5, ist(DAY, 9, 20), 1010))
        assert first.outcome is BarOutcome.GAP

    async def test_a_gap_in_one_symbol_does_not_stop_another(self) -> None:
        desk = Desk([strategy("probe", StrategyState.PAPER)])
        await desk.feed(_bar("INFY", Timeframe.M5, ist(DAY, 9, 20), 1010))  # INFY gap
        tcs = await desk.feed(_bar("TCS", Timeframe.M5, ist(DAY, 9, 15), 1010))
        assert tcs.outcome is BarOutcome.EVALUATED

    async def test_contiguous_bars_across_the_night_are_not_a_gap(self) -> None:
        """The control: warm-up ends yesterday 15:25; today opens 09:15."""
        desk = Desk([strategy("probe", StrategyState.PAPER)])
        reports = await desk.session("INFY", (10, 0))
        assert {r.outcome for r in reports} == {BarOutcome.EVALUATED}


# ---------------------------------------------------------------------------
# AC4 - published before evaluated; not ready is not evaluated
# ---------------------------------------------------------------------------


class TestPublishedBeforeEvaluated:
    async def test_the_state_is_published_before_the_trigger_is_routed(self) -> None:
        desk = Desk([strategy("probe", StrategyState.ACTIVE)])
        await desk.feed(_bar("INFY", Timeframe.M5, ist(DAY, 9, 15), 1010))
        assert desk.events == ["publish:INFY:5m", "live:INFY"]

    async def test_a_failed_publish_evaluates_nothing(self) -> None:
        spy = strategy("probe", StrategyState.ACTIVE)
        desk = Desk([spy], publish_fails=True)
        report = await desk.feed(_bar("INFY", Timeframe.M5, ist(DAY, 9, 15), 1010))
        assert report.outcome is BarOutcome.PUBLISH_FAILED
        assert spy.evaluator.contexts == [] and desk.live.received == []  # type: ignore[attr-defined]

    async def test_a_symbol_that_is_not_warm_is_not_evaluated(self) -> None:
        spy = strategy("probe", StrategyState.ACTIVE)
        desk = Desk([spy])
        desk.engine.mark_stale("INFY", "test")
        report = await desk.feed(_bar("INFY", Timeframe.M5, ist(DAY, 9, 15), 1010))
        assert report.outcome is BarOutcome.NOT_READY
        assert spy.evaluator.contexts == []  # type: ignore[attr-defined]

    async def test_a_bar_handled_late_is_folded_but_not_evaluated(self) -> None:
        spy = strategy("probe", StrategyState.ACTIVE)
        closes = ist(DAY, 9, 20)
        desk = Desk([spy], clock=closes + MAX_EVALUATION_LAG + dt.timedelta(seconds=1))
        report = await desk.feed(_bar("INFY", Timeframe.M5, ist(DAY, 9, 15), 1010))
        assert report.outcome is BarOutcome.LATE
        assert desk.engine.set_for("INFY", Timeframe.M5).last_bar_open == ist(DAY, 9, 15)
        assert spy.evaluator.contexts == []  # type: ignore[attr-defined]

    async def test_a_bar_handled_just_in_time_is_evaluated(self) -> None:
        """The control for the lag bound."""
        desk = Desk(
            [strategy("probe", StrategyState.ACTIVE)], clock=ist(DAY, 9, 20) + MAX_EVALUATION_LAG
        )
        report = await desk.feed(_bar("INFY", Timeframe.M5, ist(DAY, 9, 15), 1010))
        assert report.outcome is BarOutcome.EVALUATED


# ---------------------------------------------------------------------------
# AC5 - the context each strategy is given
# ---------------------------------------------------------------------------


class TestTheContext:
    async def test_only_strategies_on_the_bars_timeframe_are_evaluated(self) -> None:
        five = strategy("probe_5m", StrategyState.PAPER, tf="5m")
        fifteen = strategy("probe_15m", StrategyState.PAPER, tf="15m", threshold=1.6)
        desk = Desk([five, fifteen])
        report = await desk.feed(_bar("INFY", Timeframe.M5, ist(DAY, 9, 15), 1010))
        assert [d.strategy_id for d in report.decisions] == ["probe_5m"]
        report = await desk.feed(_bar("INFY", Timeframe.M15, ist(DAY, 9, 15), 1010))
        assert [d.strategy_id for d in report.decisions] == ["probe_15m"]

    async def test_the_context_carries_the_session_and_the_symbols_facts(self) -> None:
        spy = strategy("probe", StrategyState.PAPER)
        desk = Desk([spy], entries=Entries(1))
        await desk.session("INFY", (9, 35))
        ctx = spy.evaluator.contexts[-1]  # type: ignore[attr-defined]
        assert ctx.now == ist(DAY, 9, 35) and ctx.last_price == Decimal("1011.50")
        assert ctx.direction is Direction.LONG and ctx.timeframe is Timeframe.M5
        assert ctx.tick_size == Decimal("0.05") and ctx.sector == "IT"
        assert ctx.bars_since_open == 4
        # Our deadline for an unknown CAS flag: 15:10 - 5 = 15:05 IST; 15:05-09:35 = 66 bars.
        assert ctx.bars_until_squareoff == 66
        assert ctx.entries_today == 1 and ctx.regime is Regime.TRENDING
        assert ctx.india_vix == Decimal("13.5")
        assert ctx.day_open == Decimal("1010.00") and ctx.day_high == Decimal("1012.50")
        assert ctx.prev_close is None and ctx.levels is None

    async def test_the_opening_range_seals_at_0930_from_real_5m_bars(self) -> None:
        spy = strategy("probe", StrategyState.PAPER)
        desk = Desk([spy])
        reports = await desk.session("INFY", (9, 25))
        assert spy.evaluator.contexts[-1].opening_range is None  # type: ignore[attr-defined]
        await desk.feed(_bar("INFY", Timeframe.M5, ist(DAY, 9, 25), 1030, high=Decimal("1040")))
        sealed = spy.evaluator.contexts[-1].opening_range  # type: ignore[attr-defined]
        assert (
            sealed.is_usable and sealed.high == Decimal("1040") and sealed.low == Decimal("1009.00")
        )
        await desk.feed(_bar("INFY", Timeframe.M5, ist(DAY, 9, 30), 1100, high=Decimal("1200")))
        assert spy.evaluator.contexts[-1].opening_range.high == Decimal("1040"), "the range moved"  # type: ignore[attr-defined]
        assert len(reports) == 2

    async def test_a_synthetic_bar_says_nothing_about_the_days_range(self) -> None:
        spy = strategy("probe", StrategyState.PAPER)
        desk = Desk([spy])
        await desk.feed(_bar("INFY", Timeframe.M5, ist(DAY, 9, 15), 1010))
        await desk.feed(_bar("INFY", Timeframe.M5, ist(DAY, 9, 20), 900, synthetic=True, volume=0))
        ctx = spy.evaluator.contexts[-1]  # type: ignore[attr-defined]
        assert ctx.day_low == Decimal("1009.00"), "a no-trade bar moved the day's low"

    async def test_an_uncountable_entry_count_reaches_the_evaluator_as_unknown(self) -> None:
        spy = strategy("probe", StrategyState.PAPER)
        desk = Desk([spy], entries=Entries(ConnectionError("db down")))
        await desk.feed(_bar("INFY", Timeframe.M5, ist(DAY, 9, 15), 1010))
        assert spy.evaluator.contexts[-1].entries_today is None  # type: ignore[attr-defined]

    @pytest.mark.parametrize("market", [None, RuntimeError("feed down")], ids=["absent", "raises"])
    async def test_no_market_context_is_an_unknown_regime(self, market: Any) -> None:
        spy = strategy("probe", StrategyState.PAPER)
        desk = Desk([spy], market=Market(market))
        await desk.feed(_bar("INFY", Timeframe.M5, ist(DAY, 9, 15), 1010))
        ctx = spy.evaluator.contexts[-1]  # type: ignore[attr-defined]
        assert ctx.regime is None and ctx.india_vix is None and ctx.index_change_pct == {}

    async def test_the_correlation_id_is_the_signal_not_the_moment(self) -> None:
        spy = strategy("probe", StrategyState.PAPER)
        desk = Desk([spy])
        bar = _bar("INFY", Timeframe.M5, ist(DAY, 9, 15), 1010)
        await desk.feed(bar)
        assert spy.evaluator.ids == [signal_id("probe", bar)]  # type: ignore[attr-defined]
        assert signal_id("probe", bar) == signal_id("probe", bar.model_copy())
        assert signal_id("probe", bar) != signal_id("other", bar)
        later = bar.model_copy(update={"open_ts": ist(DAY, 9, 20)})
        assert signal_id("probe", bar) != signal_id("probe", later)


class TestReplayAfterARestart:
    async def test_a_replayed_bar_is_folded_and_not_evaluated(self) -> None:
        spy = strategy("probe", StrategyState.ACTIVE)
        desk = Desk([spy])
        report = await desk.loop.replay(_bar("INFY", Timeframe.M5, ist(DAY, 9, 15), 1010))
        assert report.outcome is BarOutcome.REPLAYED
        assert desk.engine.set_for("INFY", Timeframe.M5).last_bar_open == ist(DAY, 9, 15)
        assert spy.evaluator.contexts == [] and desk.live.received == []  # type: ignore[attr-defined]
        assert desk.events == ["publish:INFY:5m"]

    async def test_after_replay_the_next_bar_is_evaluated_as_if_nothing_happened(self) -> None:
        spy = strategy("probe", StrategyState.ACTIVE)
        desk = Desk([spy])
        for minute in (15, 20, 25):
            await desk.loop.replay(_bar("INFY", Timeframe.M5, ist(DAY, 9, minute), 1010 + minute))
        report = await desk.feed(_bar("INFY", Timeframe.M5, ist(DAY, 9, 30), 1050))
        assert report.outcome is BarOutcome.EVALUATED
        assert spy.evaluator.contexts[-1].opening_range.is_usable  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Symbol facts
# ---------------------------------------------------------------------------


class FactsRows:
    def __init__(
        self, *, profiles: dict[str, Any], statuses: dict[str, Any], ticks: dict[str, Any]
    ) -> None:
        self.profiles, self.statuses, self.ticks = profiles, statuses, ticks

    def tick_size(self, symbol: str) -> Decimal:
        value = self.ticks[symbol]
        if isinstance(value, Exception):
            raise value
        return value

    async def profile(self, symbol: str) -> dict[str, Any] | None:
        return self.profiles.get(symbol)

    async def daily_status(self, symbol: str, trade_date: dt.date) -> dict[str, Any] | None:
        return self.statuses.get(symbol)


class TestSymbolFacts:
    async def test_each_symbol_gets_its_tick_sector_and_execution_s_deadline(self) -> None:
        plan = SessionPlan(DAY, ist(DAY, 9, 12), frozenset({"INFY", "TCS", "SBIN"}))
        store = FactsRows(
            profiles={
                "INFY": {"sector": "IT"},
                "TCS": {"sector": None},
                "SBIN": {"sector": "BANKS"},
            },
            statuses={"INFY": {"is_cas_stock": False}, "TCS": {"is_cas_stock": True}},
            ticks={"INFY": Decimal("0.05"), "TCS": Decimal("0.10"), "SBIN": Decimal("0.05")},
        )
        facts, refused = await load_symbol_facts(plan, store, CAL, buffer_minutes=5)
        assert refused == {}
        assert facts["INFY"].squareoff_deadline == CAL.deadline_for(DAY, is_cas_stock=False)
        assert facts["TCS"].squareoff_deadline == CAL.deadline_for(DAY, is_cas_stock=True)
        # No status row: the EARLIEST deadline, as execution uses.
        assert facts["SBIN"].squareoff_deadline == CAL.deadline_for(DAY, is_cas_stock=True)
        assert facts["TCS"].tick_size == Decimal("0.10") and facts["TCS"].sector is None

    async def test_a_symbol_whose_facts_cannot_be_read_is_refused(self) -> None:
        plan = SessionPlan(DAY, ist(DAY, 9, 12), frozenset({"INFY", "GHOST", "TCS"}))
        """GHOST HAS a tick size, so only the master check can refuse it: the
        first version gave it none, the tick read refused it first, and
        mutation 'a symbol missing from the master accepted' survived."""
        store = FactsRows(
            profiles={"INFY": {"sector": "IT"}, "TCS": {"sector": "IT"}},
            statuses={},
            ticks={
                "INFY": Decimal("0.05"),
                "GHOST": Decimal("0.05"),
                "TCS": KeyError("no tick size"),
            },
        )
        facts, refused = await load_symbol_facts(plan, store, CAL, buffer_minutes=5)
        assert set(facts) == {"INFY"}
        assert refused["GHOST"] == "not in the instrument master"
        assert "could not be read" in refused["TCS"]
