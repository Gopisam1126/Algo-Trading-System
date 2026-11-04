"""E13-S01 - the signal loop against its real neighbours.

* **The bar streams** (Redis): what ``publish_sealed_bars`` writes is what
  ``RedisBarSource`` reads, in the order a decision needs; a bar that is not
  a bar is dead-lettered; a bar delivered twice is folded once.
* **The stores** (PostgreSQL, under the application role): the locked plan,
  today's entries per strategy and symbol, and the shadow-signal rows.
* **Execution's reader** (E15-S14): at the moment a trigger leaves the loop,
  ``PublishedATR`` - the reader execution sizes from - already sees the state
  of the bar that produced it.
* **The registry** (E12-S01): runnable strategies loaded from the table,
  evaluated by the real evaluator over bars that came through the stream.
"""

from __future__ import annotations

import datetime as dt
import random
import uuid
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from typing import Any

import psycopg
import pytest
import redis.asyncio as aioredis
from psycopg.types.json import Jsonb
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from algotrader.common.calendar import IST, MarketCalendar
from algotrader.common.db import engine as db_engine
from algotrader.common.db.repositories import (
    PlanRepository,
    PositionRepository,
    ShadowSignalRepository,
    StrategyRepository,
    UnknownSymbolError,
)
from algotrader.common.enums import Regime, StrategyState, Timeframe
from algotrader.common.events import stream as streams
from algotrader.common.events.envelope import Envelope
from algotrader.common.models.market import Bar
from algotrader.common.models.trading import Trigger
from algotrader.common.redis import keys
from algotrader.execution.live_inputs import PublishedATR, strategy_timeframes
from algotrader.indicators.engine import IndicatorEngine
from algotrader.indicators.publish import publish_indicator_state
from algotrader.ingest.bars import bar_id, publication_order, publish_sealed_bars
from algotrader.signals.loop import (
    BarOutcome,
    MarketFacts,
    SessionPlan,
    ShadowRecorder,
    SignalLoop,
    StrategyOutcome,
    SymbolFacts,
    load_session_plan,
)
from algotrader.signals.source import (
    GROUP,
    RedisBarSource,
    bars_since,
    deliver,
    processed_since,
)
from algotrader.strategy.dsl import load_strategy_yaml
from algotrader.strategy.registry import RunnableStrategy, StrategyRegistry

pytestmark = [pytest.mark.integration]

DAY = dt.date(2026, 8, 20)
YESTERDAY = dt.date(2026, 8, 19)
CAL = MarketCalendar()
SYMBOLS = ("SLAAA", "SLBBB", "SLCCC")


def ist(day: dt.date, hour: int, minute: int) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=IST).astimezone(dt.UTC)


def _bar(symbol: str, tf: Timeframe, open_ts: dt.datetime, price: float, **kw: Any) -> Bar:
    p = Decimal(f"{price:.2f}")
    return Bar(
        symbol=symbol,
        timeframe=tf,
        open_ts=open_ts,
        open=p,
        high=kw.pop("high", p + Decimal("1")),
        low=kw.pop("low", p - Decimal("1")),
        close=p,
        volume=kw.pop("volume", 10_000),
        **kw,
    )


def warm(engine: IndicatorEngine, symbol: str, *, end_price: float = 1000.0) -> None:
    rng = random.Random(symbol)
    last = {
        Timeframe.M5: ist(YESTERDAY, 15, 25),
        Timeframe.M15: ist(YESTERDAY, 15, 15),
        Timeframe.H1: ist(YESTERDAY, 15, 15),
    }
    for tf in engine.timeframes:
        price = end_price * 0.9
        bars = []
        for i in range(260, 0, -1):
            price *= 1 + rng.uniform(-0.002, 0.0026)
            bars.append(
                _bar(symbol, tf, last[tf] - dt.timedelta(seconds=tf.seconds * (i - 1)), price)
            )
        engine.set_for(symbol, tf).warm_up(bars)


def _owner_dsn(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://")


@pytest.fixture
async def r(redis_url: str) -> AsyncIterator[aioredis.Redis]:
    client = aioredis.from_url(redis_url, decode_responses=True)
    await client.flushall()
    try:
        yield client
    finally:
        await client.flushall()
        await client.aclose()


@pytest.fixture
def owner(migrated_database: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(_owner_dsn(migrated_database), autocommit=True) as conn:
        _wipe(conn)
        for i, symbol in enumerate(SYMBOLS):
            conn.execute(
                "INSERT INTO instruments (tradingsymbol, exchange, broker_token, tick_size, sector)"
                " VALUES (%s, 'NSE', %s, 0.05, 'IT')",
                (symbol, f"slt{i}{uuid.uuid4().hex[:6]}"),
            )
        yield conn
        _wipe(conn)


def _wipe(conn: psycopg.Connection) -> None:
    conn.execute("DELETE FROM shadow_signal")
    conn.execute(
        "DELETE FROM positions WHERE symbol_id IN"
        " (SELECT id FROM instruments WHERE tradingsymbol = ANY(%s))",
        (list(SYMBOLS),),
    )
    conn.execute("DELETE FROM plan_candidate")
    conn.execute("DELETE FROM daily_plan")
    conn.execute("DELETE FROM strategy")
    conn.execute("DELETE FROM instruments WHERE tradingsymbol = ANY(%s)", (list(SYMBOLS),))


@pytest.fixture
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    eng = db_engine.create_engine_from_url(migrated_database)
    yield eng
    await eng.dispose()


@pytest.fixture
def factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return db_engine.create_session_factory(engine)


# ---------------------------------------------------------------------------
# The bar streams
# ---------------------------------------------------------------------------


class TestBarsCrossTheStreamsInTheOrderADecisionNeeds:
    def _tick_at_1015(self, symbol: str = "SLAAA") -> list[Bar]:
        """What one tick at 10:15:01 seals, in the order a builder might list them."""
        return [
            _bar(symbol, Timeframe.M5, ist(DAY, 10, 10), 1010),
            _bar(symbol, Timeframe.M15, ist(DAY, 10, 0), 1009),
            _bar(symbol, Timeframe.H1, ist(DAY, 9, 15), 1005),
        ]

    def test_publication_order_is_close_time_then_longest_first(self) -> None:
        ordered = publication_order(self._tick_at_1015())
        assert [b.timeframe for b in ordered] == [Timeframe.H1, Timeframe.M15, Timeframe.M5]
        end_of_day = [
            _bar("SLAAA", Timeframe.M5, ist(DAY, 15, 25), 1000),
            _bar("SLAAA", Timeframe.H1, ist(DAY, 15, 15), 1000),
            _bar("SLAAA", Timeframe.M15, ist(DAY, 15, 15), 1000),
        ]
        # The 15:15 hourly bar closes at 15:30 with the market, not at 16:15.
        assert [b.timeframe for b in publication_order(end_of_day)] == [
            Timeframe.H1,
            Timeframe.M15,
            Timeframe.M5,
        ]

    async def test_one_read_returns_the_bars_that_close_together_longest_first(
        self, r: aioredis.Redis
    ) -> None:
        source = RedisBarSource(r, (Timeframe.M5, Timeframe.M15, Timeframe.H1), block_ms=50)
        await source.start()
        await publish_sealed_bars(r, self._tick_at_1015(), now=ist(DAY, 10, 15))
        batch = await source.next_batch()
        assert [d.bar.timeframe for d in batch] == [Timeframe.H1, Timeframe.M15, Timeframe.M5]
        assert [d.stream for d in batch] == ["stream:bars:1h", "stream:bars:15m", "stream:bars:5m"]

    async def test_a_forming_bar_is_never_published(self, r: aioredis.Redis) -> None:
        forming = _bar("SLAAA", Timeframe.M5, ist(DAY, 10, 10), 1010, is_final=False)
        with pytest.raises(ValueError, match="forming"):
            await publish_sealed_bars(r, [forming], now=ist(DAY, 10, 15))
        assert await r.exists("stream:bars:5m") == 0

    @pytest.mark.parametrize(
        ("payload", "correlation", "stream", "reason"),
        [
            pytest.param({"not": "a bar"}, None, "stream:bars:5m", "not a Bar", id="not-a-bar"),
            pytest.param(None, uuid.uuid4(), "stream:bars:5m", "disagree", id="wrong-identity"),
            pytest.param(
                None, None, "stream:bars:15m", "a 5m bar on stream:bars:15m", id="wrong-stream"
            ),
        ],
    )
    async def test_an_entry_that_is_not_the_bar_it_claims_is_dead_lettered(
        self, r: aioredis.Redis, payload: Any, correlation: Any, stream: str, reason: str
    ) -> None:
        bar = _bar("SLAAA", Timeframe.M5, ist(DAY, 10, 10), 1010)
        envelope = Envelope(
            correlation_id=correlation or bar_id(bar),
            emitted_at=ist(DAY, 10, 15),
            emitted_by="market-ingest",
            payload=payload if payload is not None else bar.model_dump(mode="json"),
        )
        source = RedisBarSource(r, (Timeframe.M5, Timeframe.M15), block_ms=50)
        await source.start()
        await streams.publish(r, stream, envelope, maxlen=1000)
        assert await source.next_batch() == []
        [(_, fields)] = await r.xrange(keys.stream_dlq(stream))
        assert reason in Envelope.from_fields(fields).payload["reason"]
        assert await streams.pending_count(r, stream, GROUP) == 0

    async def test_an_entry_that_is_not_an_envelope_is_acknowledged_not_reread(
        self, r: aioredis.Redis
    ) -> None:
        """Nothing to dead-letter it as - but left pending, the backlog read
        would hand it back on every call (mutation 'an unreadable entry is
        left pending' survived until this test). The bar after it reads as a
        gap, which stops the symbol rather than guessing."""
        source = RedisBarSource(r, (Timeframe.M5,), consumer="c1", block_ms=50)
        await source.start()
        await r.xadd("stream:bars:5m", {"garbage": "not an envelope"})
        assert await source.next_batch() == []
        assert await streams.pending_count(r, "stream:bars:5m", GROUP) == 0
        assert await source.next_batch() == []

    async def test_a_bar_redelivered_after_a_crash_is_folded_once(self, r: aioredis.Redis) -> None:
        """Read, not acknowledged, the process dies; the same consumer comes
        back and is handed its backlog. The loop recognises the bar."""
        desk = _Desk(strategies=[])
        bars = [_bar("SLAAA", Timeframe.M5, ist(DAY, 9, 15), 1010)]
        crashed = RedisBarSource(r, (Timeframe.M5,), consumer="signal-engine-1", block_ms=50)
        await crashed.start()
        await publish_sealed_bars(r, bars, now=ist(DAY, 9, 20))
        [delivery] = await crashed.next_batch()
        await desk.loop.on_bar(delivery.bar)  # folded; the process dies before the ack

        restarted = RedisBarSource(r, (Timeframe.M5,), consumer="signal-engine-1", block_ms=50)
        [again] = await restarted.next_batch()
        report = await deliver(desk.loop, restarted, again)
        assert report is not None and report.outcome is BarOutcome.DUPLICATE
        assert desk.engine.set_for("SLAAA", Timeframe.M5).bars_seen == 261
        assert await streams.pending_count(r, "stream:bars:5m", GROUP) == 0

    async def test_a_replay_folds_only_what_the_group_finished_with(
        self, r: aioredis.Redis
    ) -> None:
        """SIT-009: acknowledged is replayed; pending and undelivered are not -
        those come through the normal path and are evaluated there."""
        bars = [_bar("SLAAA", Timeframe.M5, ist(DAY, 9, 15 + 5 * i), 1000 + i) for i in range(3)]
        source = RedisBarSource(r, (Timeframe.M5,), consumer="c1", count=1, block_ms=50)
        await source.start()
        assert await processed_since(r, (Timeframe.M5,), ist(DAY, 9, 15)) == []
        for bar in bars:
            await publish_sealed_bars(r, [bar], now=bar.open_ts + dt.timedelta(minutes=5))
        [first] = await source.next_batch()
        await source.done(first)  # acknowledged
        [second] = await source.next_batch()  # delivered, never acknowledged
        assert (first.bar, second.bar) == (bars[0], bars[1])
        # bars[2] was never delivered at all.
        assert await processed_since(r, (Timeframe.M5,), ist(DAY, 9, 15)) == [bars[0]]
        # A stream nobody reads has nothing finished on it.
        assert await processed_since(r, (Timeframe.M15,), ist(DAY, 9, 15)) == []

    async def test_bars_since_reads_todays_bars_back_in_publication_order(
        self, r: aioredis.Redis
    ) -> None:
        await publish_sealed_bars(r, self._tick_at_1015(), now=ist(DAY, 10, 15))
        back = await bars_since(r, (Timeframe.M5, Timeframe.M15, Timeframe.H1), ist(DAY, 9, 15))
        assert back == publication_order(self._tick_at_1015())
        assert (
            await bars_since(r, (Timeframe.M5,), dt.datetime.now(dt.UTC) + dt.timedelta(hours=1))
            == []
        )


# ---------------------------------------------------------------------------
# The stores
# ---------------------------------------------------------------------------


def _plan(owner: psycopg.Connection, *, locked: bool, statuses: dict[str, str]) -> None:
    row = owner.execute(
        "INSERT INTO daily_plan (trade_date, generated_at, locked_at, config_hash, market_thesis,"
        " macro_snapshot) VALUES (%s, %s, %s, 'cfg', %s, %s) RETURNING id",
        (DAY, ist(DAY, 9, 0), ist(DAY, 9, 12) if locked else None, Jsonb({}), Jsonb({})),
    ).fetchone()
    assert row is not None
    for rank, (symbol, status) in enumerate(statuses.items(), start=1):
        owner.execute(
            "INSERT INTO plan_candidate (plan_id, symbol_id, rank, tradeability_score,"
            " score_breakdown, direction_bias, status) SELECT %s, id, %s, 70, %s, 'LONG', %s"
            " FROM instruments WHERE tradingsymbol = %s",
            (row[0], rank, Jsonb({}), status, symbol),
        )


async def _as_app(session: AsyncSession) -> AsyncSession:
    await session.execute(text("SET LOCAL ROLE algotrader_app"))
    return session


class TestTheStoresTheLoopReads:
    async def test_the_locked_plan_is_read_with_its_candidates(
        self, owner: psycopg.Connection, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        _plan(
            owner,
            locked=True,
            statuses={"SLAAA": "ACTIVE", "SLBBB": "AI_VETOED", "SLCCC": "TRADED"},
        )
        async with db_engine.read_only(factory) as session:
            plan = await load_session_plan(PlanRepository(await _as_app(session)), DAY)
        assert plan is not None and plan.symbols == frozenset({"SLAAA", "SLCCC"})
        assert plan.locked_at == ist(DAY, 9, 12)

    async def test_an_unlocked_plan_is_no_plan(
        self, owner: psycopg.Connection, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        _plan(owner, locked=False, statuses={"SLAAA": "ACTIVE"})
        async with db_engine.read_only(factory) as session:
            assert await load_session_plan(PlanRepository(await _as_app(session)), DAY) is None

    def _position(
        self,
        owner: psycopg.Connection,
        symbol: str,
        strategy_id: str,
        opened: dt.datetime,
        slot: int,
    ) -> None:
        owner.execute(
            "INSERT INTO positions (correlation_id, symbol_id, slot_index, direction, quantity,"
            " entry_price, stop_price, opened_at, squareoff_deadline, status, strategy_id,"
            " closed_at, exit_price, exit_reason)"
            " SELECT %s, id, %s, 'LONG', 10, 100, 95, %s, %s, 'CLOSED', %s, %s, 96, 'STOP'"
            " FROM instruments WHERE tradingsymbol = %s",
            (
                uuid.uuid4(),
                slot,
                opened,
                opened + dt.timedelta(hours=5),
                strategy_id,
                opened + dt.timedelta(minutes=30),
                symbol,
            ),
        )

    async def test_entries_are_counted_per_strategy_symbol_and_ist_day(
        self, owner: psycopg.Connection, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._position(owner, "SLAAA", "orb_a", ist(DAY, 9, 20), 0)  # counts
        self._position(owner, "SLAAA", "orb_a", ist(DAY, 0, 5), 1)  # 00:05 IST is today: counts
        self._position(owner, "SLAAA", "orb_a", ist(YESTERDAY, 23, 55), 2)  # yesterday, IST
        self._position(owner, "SLAAA", "orb_b", ist(DAY, 10, 0), 3)  # another strategy
        self._position(owner, "SLBBB", "orb_a", ist(DAY, 10, 0), 4)  # another symbol
        async with db_engine.read_only(factory) as session:
            repo = PositionRepository(await _as_app(session), None)  # type: ignore[arg-type]
            assert await repo.entries_on(DAY, "orb_a", "SLAAA") == 2
            assert await repo.entries_on(DAY, "orb_a", "SLCCC") == 0
            with pytest.raises(UnknownSymbolError):
                await repo.entries_on(DAY, "orb_a", "NOTLISTED")

    async def test_a_shadow_signal_is_a_row_under_the_application_role(
        self, owner: psycopg.Connection, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        doc = load_strategy_yaml(_STRATEGY.format(sid="sl_shadow", r="2.0"))
        async with db_engine.unit_of_work(factory) as session:
            await StrategyRegistry(StrategyRepository(await _as_app(session))).register(
                doc, now=ist(YESTERDAY, 9, 0)
            )
        trigger = Trigger(
            correlation_id=uuid.uuid4(),
            symbol="SLAAA",
            strategy_id="sl_shadow",
            direction=doc.direction,
            trigger_price=Decimal("1010.50"),
            suggested_stop=Decimal("1001.25"),
            timeframe_agreement=2,
            fired_at=ist(DAY, 9, 35),
        )

        class UnitOfWork:
            async def record_shadow_signal(self, row: dict[str, Any]) -> int:
                async with db_engine.unit_of_work(factory) as session:
                    return await ShadowSignalRepository(
                        await _as_app(session)
                    ).record_shadow_signal(row)

        await ShadowRecorder(UnitOfWork()).record(trigger, _FakeRunnable("sl_shadow"))  # type: ignore[arg-type]
        rows = owner.execute(
            "SELECT s.strategy_id, i.tradingsymbol, s.signalled_at, s.direction, s.price_at_signal,"
            " s.hypothetical_stop, s.hypothetical_outcome FROM shadow_signal s"
            " JOIN instruments i ON i.id = s.symbol_id"
        ).fetchall()
        assert rows == [
            (
                "sl_shadow",
                "SLAAA",
                ist(DAY, 9, 35),
                "LONG",
                Decimal("1010.5000"),
                Decimal("1001.2500"),
                None,
            )
        ]


class _FakeRunnable:
    def __init__(self, strategy_id: str) -> None:
        self.strategy_id = strategy_id


# ---------------------------------------------------------------------------
# Execution's reader sees the state before the trigger leaves
# ---------------------------------------------------------------------------


_STRATEGY = """
id: {sid}
name: Signal loop wiring {sid}
origin: USER_AUTHORED
created_at: 2026-08-01T00:00:00+00:00
created_by: qa
direction: LONG
hypothesis:
  mechanism: >-
    A probe for the signal loop's wiring tests: break the opening range high
    while holding above the twenty-period average, enough to exercise levels,
    indicators and the stop together on bars that came through the stream.
  why_it_should_persist: >-
    It is a test fixture rather than a traded strategy, so persistence is not
    a property it is claimed to have.
  expected_failure_mode: >-
    It fires on any sufficiently strong upward move, including noise.
applicability: {{regimes: [TRENDING], timeframe: 5m}}
entry:
  all_of:
    - {{primitive: price_breaks_level, params: {{level: opening_range_high, direction: above}}}}
    - {{primitive: price_above_ma, params: {{period: 20}}}}
exit:
  stop: {{primitive: atr_stop, params: {{multiplier: 1.5, period: 14}}}}
  time: {{primitive: squareoff_deadline}}
  target: {{primitive: r_multiple_target, params: {{r: {r}}}}}
"""


class _Market:
    async def facts(self, now: dt.datetime) -> MarketFacts:
        return MarketFacts(regime=Regime.TRENDING)


class _NoEntries:
    async def entries_on(self, trade_date: dt.date, strategy_id: str, symbol: str) -> int:
        return 0


class _Capture:
    def __init__(self, on_submit: Any = None) -> None:
        self.received: list[Trigger] = []
        self._on_submit = on_submit

    async def submit(self, trigger: Trigger, strategy: RunnableStrategy) -> None:
        if self._on_submit is not None:
            await self._on_submit(trigger, strategy)
        self.received.append(trigger)


class _Rows:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def record_shadow_signal(self, row: dict[str, Any]) -> int:
        self.rows.append(row)
        return len(self.rows)


class _Desk:
    def __init__(
        self,
        strategies: list[RunnableStrategy],
        *,
        publish: Any = None,
        live: _Capture | None = None,
        paper: _Capture | None = None,
        shadow: Any = None,
        symbols: frozenset[str] = frozenset({"SLAAA"}),
    ) -> None:
        self.engine = IndicatorEngine()
        for s in symbols:
            warm(self.engine, s)
        self.live, self.paper = live or _Capture(), paper or _Capture()
        self.shadow_rows = _Rows()
        self.loop = SignalLoop(
            plan=SessionPlan(DAY, ist(DAY, 9, 12), symbols),
            facts={
                s: SymbolFacts(s, Decimal("0.05"), CAL.deadline_for(DAY, is_cas_stock=None), "IT")
                for s in symbols
            },
            strategies=strategies,
            engine=self.engine,
            publish=publish or self._nowhere,
            calendar=CAL,
            entries=_NoEntries(),
            live=self.live,
            paper=self.paper,
            shadow=shadow or ShadowRecorder(self.shadow_rows),
            market=_Market(),
            clock=lambda: self.now,
        )
        self.now = ist(DAY, 9, 20)

    @staticmethod
    async def _nowhere(_set: Any) -> None:
        return None


def _rising_session(symbol: str, until: tuple[int, int]) -> list[Bar]:
    """09:15 onward, quiet for the opening range, then a strong rise through it."""
    bars, moment, price = [], ist(DAY, 9, 15), 1000.0
    while moment < ist(DAY, *until):
        before_seal = moment < ist(DAY, 9, 30)
        price += 0.2 if before_seal else 2.5
        bars.append(_bar(symbol, Timeframe.M5, moment, price))
        moment += dt.timedelta(minutes=5)
    return bars


class TestTheStateIsPublishedBeforeTheTriggerLeaves:
    async def test_execution_s_reader_sees_this_bar_s_atr_when_the_trigger_arrives(
        self,
        r: aioredis.Redis,
        owner: psycopg.Connection,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        doc = load_strategy_yaml(_STRATEGY.format(sid="sl_live", r="2.0"))
        reader = PublishedATR(r, timeframes=strategy_timeframes([doc]))
        seen: list[tuple[Trigger, Decimal | None]] = []

        async def read_at_arrival(trigger: Trigger, strategy: RunnableStrategy) -> None:
            atr = await reader.atr(
                trigger.symbol,
                strategy_id=trigger.strategy_id,
                now=trigger.fired_at + dt.timedelta(seconds=2),
            )
            seen.append((trigger, atr))

        runnable = await _registered(factory, owner, doc, StrategyState.ACTIVE)

        async def publish(indicator_set: Any) -> Any:
            return await publish_indicator_state(r, indicator_set)

        desk = _Desk([runnable], publish=publish, live=_Capture(read_at_arrival))
        for bar in _rising_session("SLAAA", (10, 0)):
            desk.now = bar.open_ts + dt.timedelta(minutes=5, seconds=2)
            await desk.loop.on_bar(bar)

        assert seen, "the strategy never fired on a rising session"
        engine_atr = desk.engine.set_for("SLAAA", Timeframe.M5).indicators["atr_14"]
        _, last_atr = seen[-1]
        assert last_atr is not None and last_atr == engine_atr.as_decimal()
        assert all(atr is not None for _, atr in seen)

    async def test_without_publishing_the_same_trigger_finds_no_atr(
        self,
        r: aioredis.Redis,
        owner: psycopg.Connection,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The control: the reader is not seeing something else."""
        doc = load_strategy_yaml(_STRATEGY.format(sid="sl_live", r="2.0"))
        reader = PublishedATR(r, timeframes=strategy_timeframes([doc]))
        seen: list[Decimal | None] = []

        async def read_at_arrival(trigger: Trigger, strategy: RunnableStrategy) -> None:
            seen.append(
                await reader.atr(
                    trigger.symbol,
                    strategy_id=trigger.strategy_id,
                    now=trigger.fired_at + dt.timedelta(seconds=2),
                )
            )

        runnable = await _registered(factory, owner, doc, StrategyState.ACTIVE)
        desk = _Desk([runnable], live=_Capture(read_at_arrival))
        for bar in _rising_session("SLAAA", (10, 0)):
            desk.now = bar.open_ts + dt.timedelta(minutes=5, seconds=2)
            await desk.loop.on_bar(bar)
        assert seen and all(atr is None for atr in seen)


async def _registered(
    factory: async_sessionmaker[AsyncSession],
    owner: psycopg.Connection,
    doc: Any,
    state: StrategyState,
) -> RunnableStrategy:
    async with db_engine.unit_of_work(factory) as session:
        await StrategyRegistry(StrategyRepository(await _as_app(session))).register(
            doc, now=ist(YESTERDAY, 9, 0)
        )
    approval = ", approved_by = 'operator', approved_at = now()" if state.places_live_orders else ""
    owner.execute(f"UPDATE strategy SET state = %s{approval} WHERE id = %s", (state.value, doc.id))
    async with db_engine.read_only(factory) as session:
        loaded = await StrategyRegistry(StrategyRepository(await _as_app(session))).load()
    [runnable] = [s for s in loaded.runnable if s.strategy_id == doc.id]
    return runnable


# ---------------------------------------------------------------------------
# The whole loop over the real stores
# ---------------------------------------------------------------------------


class TestTheLoopOverTheRealStores:
    async def test_bars_from_the_stream_reach_the_routes_their_strategies_name(
        self,
        r: aioredis.Redis,
        owner: psycopg.Connection,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        paper_doc = load_strategy_yaml(_STRATEGY.format(sid="sl_paper", r="2.0"))
        shadow_doc = load_strategy_yaml(_STRATEGY.format(sid="sl_shadow", r="3.0"))
        paper = await _registered(factory, owner, paper_doc, StrategyState.PAPER)
        shadow = await _registered(factory, owner, shadow_doc, StrategyState.SHADOW)
        _plan(owner, locked=True, statuses={"SLAAA": "ACTIVE"})
        async with db_engine.read_only(factory) as session:
            plan = await load_session_plan(PlanRepository(await _as_app(session)), DAY)
        assert plan is not None

        class UnitOfWork:
            async def record_shadow_signal(self, row: dict[str, Any]) -> int:
                async with db_engine.unit_of_work(factory) as session:
                    return await ShadowSignalRepository(
                        await _as_app(session)
                    ).record_shadow_signal(row)

        desk = _Desk(
            [paper, shadow],
            shadow=ShadowRecorder(UnitOfWork()),
            symbols=frozenset({"SLAAA", "SLBBB"}),
        )
        desk.loop._plan = plan  # the plan as Postgres says it, SLBBB not in it
        source = RedisBarSource(r, (Timeframe.M5,), block_ms=50)
        await source.start()

        session_bars = _rising_session("SLAAA", (10, 0)) + _rising_session("SLBBB", (10, 0))
        outcomes = []
        for bar in sorted(session_bars, key=lambda b: b.open_ts):
            await publish_sealed_bars(r, [bar], now=bar.open_ts + dt.timedelta(minutes=5))
            desk.now = bar.open_ts + dt.timedelta(minutes=5, seconds=2)
            for delivery in await source.next_batch():
                report = await deliver(desk.loop, source, delivery)
                assert report is not None
                outcomes.append((delivery.bar.symbol, report))

        assert {rep.outcome for sym, rep in outcomes if sym == "SLBBB"} == {BarOutcome.NOT_IN_PLAN}
        assert desk.paper.received and desk.live.received == []
        assert {t.strategy_id for t in desk.paper.received} == {"sl_paper"}
        shadow_rows = owner.execute("SELECT strategy_id FROM shadow_signal").fetchall()
        assert shadow_rows and {row[0] for row in shadow_rows} == {"sl_shadow"}
        routed = [
            d for _, rep in outcomes for d in rep.decisions if d.outcome is StrategyOutcome.ROUTED
        ]
        assert len(shadow_rows) == len([d for d in routed if d.strategy_id == "sl_shadow"])
        assert await streams.pending_count(r, "stream:bars:5m", GROUP) == 0
