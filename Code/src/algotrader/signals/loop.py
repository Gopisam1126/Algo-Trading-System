"""The strategy evaluation loop (E13-S01).

Each sealed bar of a planned symbol is folded into the indicator engine once,
its state published, and every runnable strategy on that timeframe evaluated;
what fires is routed by the strategy's state. STRATEGY_ENGINE.md §9.1 and
LOW_LEVEL_ARCHITECTURE.md §5.6 describe the shape; this is the deterministic
half of it. AI confirmation and Recommendation emission (E13-S03/S04) sit
behind the LIVE and PAPER routes, which this module hands a Trigger to and
nothing more.

Design record
-------------
**Decision.** The loop owns the indicator engine (E15-S14's recorded choice:
"the indicator engine runs inside the strategy side and nowhere else") and is
the gate every bar passes before anything reads it: in the plan, today, not
seen before, not after a hole, folded, published - and only then evaluated.

**Alternatives rejected.**

* *Fold whatever arrives.* Delivery is at-least-once, so a crash between fold
  and acknowledge redelivers the bar; ``IndicatorSet.update`` folds it again
  and every EMA, RSI and the ATR that sizes positions moves by one bar's worth
  with nothing to show for it. A bar not newer than the last one folded for
  its (symbol, timeframe) is refused here.
* *Trust the producer to announce gaps.* A missing bar - a feed drop, or an
  interval the producer never filled - leaves indicators computed on an
  irregular series. The loop checks continuity itself, against the session's
  bar slots, and marks the symbol stale: nothing is evaluated for it until a
  re-warm (``IndicatorSet.stale`` is cleared by nothing else).
* *Read the three bar streams separately.* One XREADGROUP is one moment;
  three reads are three. See :func:`algotrader.ingest.bars.publication_order`.

**The failures it prevents.** A symbol outside the plan traded; a SHADOW
strategy reaching an order; a redelivered bar double-counted into the ATR; a
strategy evaluated through a feed gap; a signal that overtakes its own bar's
published state and is refused at execution; a decision about a bar that
closed minutes ago.

**What would make this wrong.** A second process folding bars (the separate
``ti-engine`` in LLA §2.2 - superseded by the E15-S14 decision), or a bar
producer that fills no-trade intervals by NOT emitting a bar: the continuity
check would then read every quiet interval as a gap.

Routing is a table, total over the runnable states and checked at import:
SHADOW goes to the shadow recorder and only there. The signals package
imports nothing that can place an order (``tests/security``).
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol

from algotrader.common.calendar import IST, MarketCalendar
from algotrader.common.enums import Regime, StrategyState, Timeframe
from algotrader.common.models.market import Bar
from algotrader.common.models.trading import Trigger
from algotrader.common.text import one_safe_line
from algotrader.indicators.engine import IndicatorEngine, IndicatorSet, MultiTimeframeSnapshot
from algotrader.indicators.levels import OPENING_RANGE_END, LevelSet, OpeningRange
from algotrader.ingest.bars import bar_close
from algotrader.strategy.context import ContextError, EvalContext
from algotrader.strategy.registry import RunnableStrategy

log = logging.getLogger(__name__)

#: A bar evaluated later than this after it closed is a decision about a
#: market that has moved. It is still folded - the indicators need it - but
#: nothing is evaluated on it. Execution refuses indicator state older than
#: one bar + 30s (E15-S14); a minute keeps every signal this lets through
#: well inside that.
MAX_EVALUATION_LAG = dt.timedelta(seconds=60)

#: The opening range and the day's high/low are built from these bars. The
#: shortest bar the engine carries that tiles 09:15-09:30 exactly.
DAY_TIMEFRAME = Timeframe.M5

#: Plan candidate statuses that are IN the plan. ``GAP_INVALIDATED`` and
#: ``AI_VETOED`` were taken out of it before the open; any status this
#: module does not know is also out - a plan the loop cannot read is not a
#: plan it trades.
IN_PLAN_STATUSES: frozenset[str] = frozenset({"ACTIVE", "TRADED"})

#: Signal identity: the same strategy on the same bar is the same signal, so
#: a bar delivered twice cannot become two.
SIGNAL_NAMESPACE = uuid.UUID("e13a5001-0f1e-4c2b-9b6e-5160a1100e13")


class Route(StrEnum):
    #: ACTIVE and DEGRADED: the full path - AI review, then a Recommendation.
    LIVE = "LIVE"
    #: The paper execution path.
    PAPER = "PAPER"
    #: Recorded to ``shadow_signal``. No path to any order.
    SHADOW = "SHADOW"


ROUTE_BY_STATE: Mapping[StrategyState, Route] = MappingProxyType(
    {
        StrategyState.SHADOW: Route.SHADOW,
        StrategyState.PAPER: Route.PAPER,
        StrategyState.ACTIVE: Route.LIVE,
        StrategyState.DEGRADED: Route.LIVE,
    }
)


def _check_routes() -> None:
    """Total over the runnable states, LIVE for exactly the live-order ones."""
    runnable = {s for s in StrategyState if s.is_runnable}
    if set(ROUTE_BY_STATE) != runnable:
        raise RuntimeError(
            f"ROUTE_BY_STATE covers {sorted(ROUTE_BY_STATE)}, the runnable states are "
            f"{sorted(runnable)}: a runnable state with no route would be evaluated and "
            f"its triggers dropped, or routed by a default nobody chose"
        )
    for state, route in ROUTE_BY_STATE.items():
        if (route is Route.LIVE) != state.places_live_orders:
            raise RuntimeError(f"{state} routes to {route}; LIVE is for live-order states only")


_check_routes()


def signal_id(strategy_id: str, bar: Bar) -> uuid.UUID:
    """The correlation id of the signal ``strategy_id`` raises on ``bar``."""
    return uuid.uuid5(
        SIGNAL_NAMESPACE,
        f"{strategy_id}|{bar.symbol}|{bar.timeframe.value}|"
        f"{bar.open_ts.astimezone(dt.UTC).isoformat()}",
    )


# ---------------------------------------------------------------------------
# What the loop reads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionPlan:
    """The day's locked plan, as the loop needs it: which symbols, which day."""

    trade_date: dt.date
    locked_at: dt.datetime
    symbols: frozenset[str]

    def __post_init__(self) -> None:
        if self.locked_at.tzinfo is None:
            raise ValueError("locked_at must be timezone-aware")


class PlanStore(Protocol):
    async def locked_plan(self, trade_date: dt.date) -> dict[str, Any] | None: ...


async def load_session_plan(store: PlanStore, trade_date: dt.date) -> SessionPlan | None:
    """Today's locked plan, or ``None`` - and ``None`` means nothing is evaluated.

    A plan for another date is refused as surely as no plan: yesterday's
    shortlist is not today's.
    """
    row = await store.locked_plan(trade_date)
    if row is None:
        log.warning("no locked plan for %s: the signal loop evaluates nothing today", trade_date)
        return None
    if row.get("trade_date") != trade_date:
        log.error("the plan read for %s is for %s; refusing it", trade_date, row.get("trade_date"))
        return None
    symbols = set()
    for candidate in row.get("candidates") or []:
        status = candidate.get("status")
        if status in IN_PLAN_STATUSES:
            symbols.add(str(candidate["symbol"]))
        elif status not in {"GAP_INVALIDATED", "AI_VETOED"}:
            log.error(
                "plan candidate %s has status %s, which the loop does not know; it is "
                "NOT in the plan",
                one_safe_line(str(candidate.get("symbol"))),
                one_safe_line(str(status)),
            )
    return SessionPlan(
        trade_date=trade_date, locked_at=row["locked_at"], symbols=frozenset(symbols)
    )


@dataclass(frozen=True)
class SymbolFacts:
    """What does not change about a planned symbol during the session."""

    symbol: str
    tick_size: Decimal
    #: OUR square-off deadline - ``MarketCalendar.deadline_for``, the instant
    #: execution puts on the position.
    squareoff_deadline: dt.datetime
    sector: str | None = None
    #: Prior-session structure (E06-S05). ``None`` until daily bars exist (E03):
    #: every level primitive then answers UNKNOWN.
    levels: LevelSet | None = None

    def __post_init__(self) -> None:
        if self.tick_size <= 0:
            raise ValueError(f"{self.symbol}: tick size must be positive")
        if self.squareoff_deadline.tzinfo is None:
            raise ValueError(f"{self.symbol}: the square-off deadline must be timezone-aware")


class FactsStore(Protocol):
    """``InstrumentRepository`` and ``DailyStatusRepository``, as the loop reads them."""

    def tick_size(self, symbol: str) -> Decimal: ...
    async def profile(self, symbol: str) -> dict[str, Any] | None: ...
    async def daily_status(self, symbol: str, trade_date: dt.date) -> dict[str, Any] | None: ...


async def load_symbol_facts(
    plan: SessionPlan,
    store: FactsStore,
    calendar: MarketCalendar,
    *,
    buffer_minutes: int,
    levels: Mapping[str, LevelSet] | None = None,
) -> tuple[dict[str, SymbolFacts], dict[str, str]]:
    """Facts for every planned symbol, and the symbols refused with why.

    A planned symbol the master does not know, or whose tick size cannot be
    read, is refused: its stop could not be snapped to a placeable price. An
    unknown CAS flag is not a refusal - the deadline is the earliest one.
    """
    facts: dict[str, SymbolFacts] = {}
    refused: dict[str, str] = {}
    for symbol in sorted(plan.symbols):
        try:
            profile = await store.profile(symbol)
            if profile is None:
                refused[symbol] = "not in the instrument master"
                continue
            tick = store.tick_size(symbol)
            status = await store.daily_status(symbol, plan.trade_date)
        except Exception as exc:  # an unreadable fact refuses the symbol, never defaults it
            refused[symbol] = one_safe_line(f"its facts could not be read: {exc}")
            continue
        is_cas = status.get("is_cas_stock") if status is not None else None
        facts[symbol] = SymbolFacts(
            symbol=symbol,
            tick_size=tick,
            squareoff_deadline=calendar.deadline_for(
                plan.trade_date,
                is_cas_stock=is_cas if isinstance(is_cas, bool) else None,
                buffer_minutes=buffer_minutes,
            ),
            sector=profile.get("sector"),
            levels=(levels or {}).get(symbol),
        )
    for symbol, why in refused.items():
        log.error("%s is planned but will not be evaluated: %s", symbol, why)
    return facts, refused


@dataclass(frozen=True)
class MarketFacts:
    """The market context a strategy may read (E09)."""

    regime: Regime | None = None
    india_vix: Decimal | None = None
    index_change_pct: Mapping[str, Decimal] = field(default_factory=dict)


class MarketContextSource(Protocol):
    async def facts(self, now: dt.datetime) -> MarketFacts | None: ...


class NoMarketContext:
    """What runs until E09 publishes a market context: no regime, so every
    strategy - each declares the regimes it was validated in - does not apply."""

    async def facts(self, now: dt.datetime) -> MarketFacts | None:
        return None


class EntryCounter(Protocol):
    """``PositionRepository.entries_on``, as the loop reads it."""

    async def entries_on(self, trade_date: dt.date, strategy_id: str, symbol: str) -> int: ...


class CandidateSink(Protocol):
    """Where a LIVE or PAPER trigger goes next: AI review (E13-S03)."""

    async def submit(self, trigger: Trigger, strategy: RunnableStrategy) -> None: ...


class ShadowStore(Protocol):
    async def record_shadow_signal(self, row: dict[str, Any]) -> int: ...


class ShadowRecorder:
    """The SHADOW route's only destination: a ``shadow_signal`` row.

    It holds a store that can insert shadow signals and nothing else - no
    candidate sink, no stream, no broker - so a SHADOW strategy's trigger has
    no path to an order by construction, not by convention.
    """

    def __init__(self, store: ShadowStore) -> None:
        self._store = store

    async def record(self, trigger: Trigger, strategy: RunnableStrategy) -> None:
        await self._store.record_shadow_signal(
            {
                "strategy_id": strategy.strategy_id,
                "symbol": trigger.symbol,
                "signalled_at": trigger.fired_at,
                "direction": trigger.direction.value,
                "price_at_signal": trigger.trigger_price,
                "hypothetical_stop": trigger.suggested_stop,
            }
        )


# ---------------------------------------------------------------------------
# What the loop reports
# ---------------------------------------------------------------------------


class BarOutcome(StrEnum):
    NOT_IN_PLAN = "NOT_IN_PLAN"
    NOT_FINAL = "NOT_FINAL"
    NOT_CARRIED = "NOT_CARRIED"
    WRONG_DAY = "WRONG_DAY"
    NO_FACTS = "NO_FACTS"
    DUPLICATE = "DUPLICATE"
    GAP = "GAP"
    PUBLISH_FAILED = "PUBLISH_FAILED"
    LATE = "LATE"
    REPLAYED = "REPLAYED"
    NOT_READY = "NOT_READY"
    EVALUATED = "EVALUATED"


class StrategyOutcome(StrEnum):
    #: Fired, and the trigger reached its route.
    ROUTED = "ROUTED"
    #: Evaluated; did not fire (or was out of its declared applicability).
    NO_SIGNAL = "NO_SIGNAL"
    #: Could not be evaluated: an unbuildable context, or the evaluator raised.
    ERROR = "ERROR"
    #: Fired, and the route refused or failed. The signal is lost, not retried.
    ROUTE_FAILED = "ROUTE_FAILED"


@dataclass(frozen=True)
class StrategyDecision:
    strategy_id: str
    state: StrategyState
    outcome: StrategyOutcome
    route: Route | None = None
    trigger: Trigger | None = None
    detail: str | None = None


@dataclass(frozen=True)
class BarReport:
    symbol: str
    timeframe: Timeframe
    open_ts: dt.datetime
    outcome: BarOutcome
    decisions: tuple[StrategyDecision, ...] = ()
    detail: str | None = None


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


@dataclass
class _Day:
    """Today's opening range and high/low for one symbol, from real 5m bars."""

    symbol: str
    trade_date: dt.date
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    range_high: Decimal | None = None
    range_low: Decimal | None = None
    opening_range: OpeningRange | None = None

    def fold(self, bar: Bar) -> None:
        if bar.synthetic:
            # Flat at the previous close with no trades: it says nothing about
            # where today traded, and would put yesterday's close into the range.
            return
        self.open = bar.open if self.open is None else self.open
        self.high = bar.high if self.high is None else max(self.high, bar.high)
        self.low = bar.low if self.low is None else min(self.low, bar.low)
        if bar.open_ts.astimezone(IST).time() < OPENING_RANGE_END:
            self.range_high = (
                bar.high if self.range_high is None else max(self.range_high, bar.high)
            )
            self.range_low = bar.low if self.range_low is None else min(self.range_low, bar.low)

    def seal_if_due(self, closed_at: dt.datetime) -> None:
        if self.opening_range is not None:
            return
        if closed_at.astimezone(IST).time() >= OPENING_RANGE_END:
            self.opening_range = OpeningRange(
                symbol=self.symbol,
                trade_date=self.trade_date,
                high=self.range_high,
                low=self.range_low,
                sealed=True,
            )


PublishState = Callable[[IndicatorSet], Awaitable[object]]


class SignalLoop:
    """One session's evaluation of the planned symbols.

    Built for ONE trading day: the plan, the symbol facts and the opening
    ranges are that day's. A new day is a new loop.
    """

    def __init__(
        self,
        *,
        plan: SessionPlan,
        facts: Mapping[str, SymbolFacts],
        strategies: Sequence[RunnableStrategy],
        engine: IndicatorEngine,
        publish: PublishState,
        calendar: MarketCalendar,
        entries: EntryCounter,
        live: CandidateSink,
        paper: CandidateSink,
        shadow: ShadowRecorder,
        market: MarketContextSource | None = None,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        if not isinstance(shadow, ShadowRecorder):
            raise TypeError(
                "the SHADOW route must be a ShadowRecorder: anything else could reach an order"
            )
        for strategy in strategies:
            if not strategy.state.is_runnable:
                raise ValueError(f"{strategy.strategy_id} is {strategy.state}, not runnable")
        self._plan = plan
        self._facts = dict(facts)
        self._strategies = tuple(strategies)
        self._engine = engine
        self._publish = publish
        self._calendar = calendar
        self._entries = entries
        self._live = live
        self._paper = paper
        self._shadow = shadow
        self._market = market or NoMarketContext()
        self._clock = clock
        self._days: dict[str, _Day] = {}
        session_open, session_close = calendar.session_bounds(plan.trade_date)
        self._session_open = session_open
        self._session_close = session_close

    @property
    def plan(self) -> SessionPlan:
        return self._plan

    @property
    def calendar(self) -> MarketCalendar:
        return self._calendar

    @property
    def timeframes(self) -> tuple[Timeframe, ...]:
        return tuple(self._engine.timeframes)

    # -- admission ------------------------------------------------------------

    def _admit(self, bar: Bar) -> tuple[BarOutcome | None, str | None]:
        """Whether ``bar`` may be folded, and if not, why."""
        if bar.symbol not in self._plan.symbols:
            return BarOutcome.NOT_IN_PLAN, None
        if not bar.is_final:
            return BarOutcome.NOT_FINAL, "a forming bar is look-ahead"
        if bar.open_ts.astimezone(IST).date() != self._plan.trade_date:
            return BarOutcome.WRONG_DAY, f"the bar is not on {self._plan.trade_date}"
        if bar.symbol not in self._facts:
            return BarOutcome.NO_FACTS, None
        if bar.timeframe not in self._engine.timeframes:
            return BarOutcome.NOT_CARRIED, f"the engine does not carry {bar.timeframe.value}"
        last = self._engine.set_for(bar.symbol, bar.timeframe).last_bar_open
        if last is not None and bar.open_ts <= last:
            return BarOutcome.DUPLICATE, f"already folded through {last.isoformat()}"
        return None, None

    def _expected_open(self, last: dt.datetime, timeframe: Timeframe) -> dt.datetime:
        """The bar that follows one opening at ``last``: the next slot in its
        session, or the first slot of the session it belongs to."""
        if last.astimezone(IST).date() != self._plan.trade_date:
            return self._session_open
        return last + dt.timedelta(seconds=timeframe.seconds)

    # -- one bar ----------------------------------------------------------------

    async def replay(self, bar: Bar) -> BarReport:
        """Fold a bar seen before a restart, without evaluating it.

        After a restart the engine is warm only through yesterday; today's
        bars come back from the stream and are folded here, so the indicators
        and the opening range are what they were. Nothing is evaluated: a
        signal on a replayed bar is a decision about the past.
        """
        return await self._step(bar, evaluate=False)

    async def on_bar(self, bar: Bar) -> BarReport:
        return await self._step(bar, evaluate=True)

    async def _step(self, bar: Bar, *, evaluate: bool) -> BarReport:
        def report(
            outcome: BarOutcome,
            detail: str | None = None,
            decisions: Sequence[StrategyDecision] = (),
        ) -> BarReport:
            return BarReport(
                symbol=bar.symbol,
                timeframe=bar.timeframe,
                open_ts=bar.open_ts,
                outcome=outcome,
                decisions=tuple(decisions),
                detail=detail,
            )

        refused, why = self._admit(bar)
        if refused is not None:
            if refused is not BarOutcome.NOT_IN_PLAN:
                log.info(
                    "%s %s %s: %s %s",
                    bar.symbol,
                    bar.timeframe.value,
                    bar.open_ts,
                    refused,
                    why or "",
                )
            return report(refused, why)

        indicator_set = self._engine.set_for(bar.symbol, bar.timeframe)
        gap: str | None = None
        last = indicator_set.last_bar_open
        if last is not None:
            expected = self._expected_open(last, bar.timeframe)
            if bar.open_ts != expected:
                gap = (
                    f"{bar.timeframe.value} bar opening {bar.open_ts.isoformat()} follows "
                    f"{last.isoformat()}; expected {expected.isoformat()}"
                )
                self._engine.mark_stale(bar.symbol, gap)

        indicator_set.update(bar)
        closed_at = bar_close(bar)
        if bar.timeframe is DAY_TIMEFRAME:
            day = self._days.setdefault(bar.symbol, _Day(bar.symbol, self._plan.trade_date))
            day.fold(bar)
            day.seal_if_due(closed_at)

        # Publish BEFORE anything is evaluated on this bar: execution sizes from
        # this state and refuses one older than the signal (E15-S14).
        try:
            await self._publish(indicator_set)
        except Exception as exc:  # nothing is evaluated on a bar whose state is unpublished
            log.error("%s: publishing %s state failed: %s", bar.symbol, bar.timeframe.value, exc)
            return report(BarOutcome.PUBLISH_FAILED, one_safe_line(str(exc)))

        if gap is not None:
            return report(BarOutcome.GAP, gap)
        if not evaluate:
            return report(BarOutcome.REPLAYED)
        lag = self._clock() - closed_at
        if lag > MAX_EVALUATION_LAG:
            return report(BarOutcome.LATE, f"closed {lag.total_seconds():.0f}s ago")

        snapshot = self._engine.snapshot(bar.symbol)
        if not snapshot.all_ready:
            return report(
                BarOutcome.NOT_READY,
                one_safe_line(str({tf.value: n for tf, n in snapshot.not_ready.items()})),
            )

        candidates = [
            s
            for s in self._strategies
            if s.record.document.applicability.timeframe is bar.timeframe
        ]
        if not candidates:
            return report(BarOutcome.EVALUATED)
        market = await self._market_facts(closed_at)
        decisions = [
            await self._evaluate(strategy, bar, closed_at, snapshot, market)
            for strategy in candidates
        ]
        return report(BarOutcome.EVALUATED, decisions=decisions)

    async def _market_facts(self, now: dt.datetime) -> MarketFacts | None:
        try:
            return await self._market.facts(now)
        except Exception as exc:  # no context is unknown context: nothing regime-gated runs
            log.error("market context unreadable: %s", exc)
            return None

    async def _entries_today(self, strategy_id: str, symbol: str) -> int | None:
        try:
            return int(await self._entries.entries_on(self._plan.trade_date, strategy_id, symbol))
        except Exception as exc:  # uncounted is None, and the evaluator then refuses
            log.error("entries today for %s/%s unreadable: %s", strategy_id, symbol, exc)
            return None

    async def _evaluate(
        self,
        strategy: RunnableStrategy,
        bar: Bar,
        closed_at: dt.datetime,
        snapshot: MultiTimeframeSnapshot,
        market: MarketFacts | None,
    ) -> StrategyDecision:
        doc = strategy.record.document
        facts = self._facts[bar.symbol]
        day = self._days.get(bar.symbol)
        seconds = bar.timeframe.seconds
        since_open = int((closed_at - self._session_open).total_seconds() // seconds)
        until_squareoff = max(
            0, int((facts.squareoff_deadline - closed_at).total_seconds() // seconds)
        )
        levels = facts.levels

        def decided(outcome: StrategyOutcome, **kw: Any) -> StrategyDecision:
            return StrategyDecision(strategy.strategy_id, strategy.state, outcome, **kw)

        try:
            ctx = EvalContext(
                symbol=bar.symbol,
                now=closed_at,
                timeframe=bar.timeframe,
                direction=doc.direction,
                last_price=bar.close,
                snapshot=snapshot,
                tick_size=facts.tick_size,
                bar=bar,
                day_open=day.open if day else None,
                day_high=day.high if day else None,
                day_low=day.low if day else None,
                prev_close=levels.prior_close if levels is not None else None,
                levels=levels,
                opening_range=day.opening_range if day else None,
                bars_since_open=since_open,
                bars_until_squareoff=until_squareoff,
                regime=market.regime if market else None,
                india_vix=market.india_vix if market else None,
                index_change_pct=dict(market.index_change_pct) if market else {},
                sector=facts.sector,
                entries_today=await self._entries_today(strategy.strategy_id, bar.symbol),
            )
            trigger = strategy.evaluator.fire(
                ctx, correlation_id=signal_id(strategy.strategy_id, bar)
            )
        except (ContextError, ValueError) as exc:
            # One strategy that cannot be evaluated must not stop the others.
            log.error("%s on %s: not evaluable: %s", strategy.strategy_id, bar.symbol, exc)
            return decided(StrategyOutcome.ERROR, detail=one_safe_line(str(exc)))
        if trigger is None:
            return decided(StrategyOutcome.NO_SIGNAL)

        route = ROUTE_BY_STATE[strategy.state]
        try:
            if route is Route.SHADOW:
                await self._shadow.record(trigger, strategy)
            elif route is Route.PAPER:
                await self._paper.submit(trigger, strategy)
            else:
                await self._live.submit(trigger, strategy)
        except Exception as exc:  # lost, logged, reported - never retried into a second signal
            log.error(
                "%s on %s fired and its %s route failed: %s",
                strategy.strategy_id,
                bar.symbol,
                route,
                exc,
            )
            return decided(
                StrategyOutcome.ROUTE_FAILED,
                route=route,
                trigger=trigger,
                detail=one_safe_line(str(exc)),
            )
        log.info(
            "%s fired on %s %s at %s -> %s",
            strategy.strategy_id,
            bar.symbol,
            bar.timeframe.value,
            trigger.trigger_price,
            route,
        )
        return decided(StrategyOutcome.ROUTED, route=route, trigger=trigger)


__all__ = [
    "DAY_TIMEFRAME",
    "IN_PLAN_STATUSES",
    "MAX_EVALUATION_LAG",
    "ROUTE_BY_STATE",
    "BarOutcome",
    "BarReport",
    "CandidateSink",
    "EntryCounter",
    "FactsStore",
    "MarketContextSource",
    "MarketFacts",
    "NoMarketContext",
    "PlanStore",
    "Route",
    "SessionPlan",
    "ShadowRecorder",
    "ShadowStore",
    "SignalLoop",
    "StrategyDecision",
    "StrategyOutcome",
    "SymbolFacts",
    "load_session_plan",
    "load_symbol_facts",
    "signal_id",
]
