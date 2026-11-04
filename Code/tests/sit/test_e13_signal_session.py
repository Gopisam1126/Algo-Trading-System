"""SIT-27: a signal session, assembled the way production assembles it (E13-S01).

The component and integration tests prove each criterion once. This asks what
only the assembled thing can: **walk a morning of ticks through the real bar
builder, across the real streams, into a signal side built from Postgres - and
does every planned symbol get evaluated exactly once per bar, does the one
with a feed hole stop, does nothing outside the plan move, does every trigger
leave with its bar's state already published, and does a crash and restart in
the middle change no decision at all?**

* Producer: ticks -> ``MultiTimeframeBuilder`` (5m/15m/1h sealing together)
  -> ``publish_sealed_bars`` -> Redis.
* Signal side: locked plan, symbol facts, strategies (the registry, as
  ``algotrader_app``) and warm-up history all read from Postgres; indicator
  state published to Redis; the PAPER route a sink that asks execution's own
  ``PublishedATR`` what it would size from; the SHADOW route the real recorder
  writing ``shadow_signal``.
* The restart: the first side dies holding an unacknowledged batch; the second
  warms from Postgres, replays today's bars from the streams, and is handed
  the dead side's backlog.
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
    BarRepository,
    DailyStatusRepository,
    InstrumentRepository,
    PlanRepository,
    PositionRepository,
    ShadowSignalRepository,
    StrategyRepository,
)
from algotrader.common.enums import Regime, Timeframe
from algotrader.common.events import stream as streams
from algotrader.common.models.market import Bar, Tick
from algotrader.common.models.trading import Trigger
from algotrader.execution.live_inputs import PublishedATR, strategy_timeframes
from algotrader.indicators.engine import IndicatorEngine, warm_up_symbols
from algotrader.indicators.publish import publish_indicator_state
from algotrader.ingest.bars import MultiTimeframeBuilder, publish_sealed_bars
from algotrader.signals.loop import (
    BarOutcome,
    BarReport,
    MarketFacts,
    ShadowRecorder,
    SignalLoop,
    StrategyOutcome,
    load_session_plan,
    load_symbol_facts,
)
from algotrader.signals.source import GROUP, RedisBarSource, deliver, replay_today
from algotrader.strategy.dsl import load_strategy_yaml
from algotrader.strategy.registry import RunnableStrategy, StrategyRegistry

pytestmark = [pytest.mark.integration]

DAY = dt.date(2026, 8, 20)  # an ordinary Thursday
YESTERDAY = dt.date(2026, 8, 19)
CAL = MarketCalendar()
PLANNED = ("SITAAA", "SITBBB")
VETOED = "SITCCC"
TIMEFRAMES = (Timeframe.M5, Timeframe.M15, Timeframe.H1)
#: SITBBB's feed drops for a quarter of an hour.
HOLE = ((10, 30), (10, 46))
RESTART_AT = (11, 0)
SESSION_END = (12, 0)


def ist(day: dt.date, hour: int, minute: int, second: int = 0) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, minute, second), tzinfo=IST).astimezone(dt.UTC)


STRATEGY = """
id: {sid}
name: SIT-27 breakout {sid}
origin: USER_AUTHORED
created_at: 2026-08-01T00:00:00+00:00
created_by: qa
direction: LONG
hypothesis:
  mechanism: >-
    Break the opening range high while holding above the twenty-period average:
    enough to exercise the opening range built from streamed bars, a warmed
    average and an ATR stop together across a whole morning.
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
PAPER_DOC = load_strategy_yaml(STRATEGY.format(sid="sit27_paper", r="2.0"))
SHADOW_DOC = load_strategy_yaml(STRATEGY.format(sid="sit27_shadow", r="3.0"))


def _owner_dsn(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://")


def _wipe(conn: psycopg.Connection) -> None:
    conn.execute("DELETE FROM shadow_signal")
    conn.execute("DELETE FROM plan_candidate")
    conn.execute("DELETE FROM daily_plan")
    conn.execute("DELETE FROM strategy")
    symbols = [*PLANNED, VETOED]
    conn.execute(
        "DELETE FROM ohlcv WHERE symbol_id IN"
        " (SELECT id FROM instruments WHERE tradingsymbol = ANY(%s))",
        (symbols,),
    )
    conn.execute("DELETE FROM instruments WHERE tradingsymbol = ANY(%s)", (symbols,))


@pytest.fixture
def owner(migrated_database: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(_owner_dsn(migrated_database), autocommit=True) as conn:
        _wipe(conn)
        yield conn
        _wipe(conn)


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
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    eng = db_engine.create_engine_from_url(migrated_database)
    yield eng
    await eng.dispose()


@pytest.fixture
def factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return db_engine.create_session_factory(engine)


async def _as_app(session: AsyncSession) -> AsyncSession:
    await session.execute(text("SET LOCAL ROLE algotrader_app"))
    return session


# ---------------------------------------------------------------------------
# The morning before the open: instruments, history, the plan, the strategies
# ---------------------------------------------------------------------------


async def _premarket(owner: psycopg.Connection, factory: async_sessionmaker[AsyncSession]) -> None:
    for i, symbol in enumerate([*PLANNED, VETOED]):
        owner.execute(
            "INSERT INTO instruments (tradingsymbol, exchange, broker_token, tick_size, sector)"
            " VALUES (%s, 'NSE', %s, 0.05, 'IT')",
            (symbol, f"sit27{i}{uuid.uuid4().hex[:6]}"),
        )
    # 260 bars per timeframe ending at yesterday's close - warm-up's source.
    async with db_engine.unit_of_work(factory) as session:
        instruments = InstrumentRepository(session)
        await instruments.refresh_cache()
        bars = BarRepository(session, instruments)
        last = {
            Timeframe.M5: ist(YESTERDAY, 15, 25),
            Timeframe.M15: ist(YESTERDAY, 15, 15),
            Timeframe.H1: ist(YESTERDAY, 15, 15),
        }
        for symbol in [*PLANNED, VETOED]:
            sid = await instruments.symbol_id(symbol)
            rng = random.Random(symbol)
            for tf in TIMEFRAMES:
                price, rows = 900.0, []
                for i in range(260, 0, -1):
                    price *= 1 + rng.uniform(-0.002, 0.0026)
                    p = Decimal(f"{price:.2f}")
                    rows.append(
                        {
                            "symbol_id": sid,
                            "timeframe": tf.value,
                            "ts": last[tf] - dt.timedelta(seconds=tf.seconds * (i - 1)),
                            "open": p,
                            "high": p + 1,
                            "low": p - 1,
                            "close": p,
                            "volume": 10_000,
                        }
                    )
                await bars.bulk_upsert(rows)
    plan = owner.execute(
        "INSERT INTO daily_plan (trade_date, generated_at, locked_at, config_hash, market_thesis,"
        " macro_snapshot) VALUES (%s, %s, %s, 'sit27', %s, %s) RETURNING id",
        (DAY, ist(DAY, 9, 0), ist(DAY, 9, 12), Jsonb({}), Jsonb({})),
    ).fetchone()
    assert plan is not None
    for rank, (symbol, status) in enumerate(
        [("SITAAA", "ACTIVE"), ("SITBBB", "ACTIVE"), (VETOED, "AI_VETOED")], start=1
    ):
        owner.execute(
            "INSERT INTO plan_candidate (plan_id, symbol_id, rank, tradeability_score,"
            " score_breakdown, direction_bias, status) SELECT %s, id, %s, 70, %s, 'LONG', %s"
            " FROM instruments WHERE tradingsymbol = %s",
            (plan[0], rank, Jsonb({}), status, symbol),
        )
    async with db_engine.unit_of_work(factory) as session:
        registry = StrategyRegistry(StrategyRepository(await _as_app(session)))
        await registry.register(PAPER_DOC, now=ist(YESTERDAY, 18, 0))
        await registry.register(SHADOW_DOC, now=ist(YESTERDAY, 18, 1))
    owner.execute("UPDATE strategy SET state = 'PAPER' WHERE id = 'sit27_paper'")
    owner.execute("UPDATE strategy SET state = 'SHADOW' WHERE id = 'sit27_shadow'")


# ---------------------------------------------------------------------------
# The producer: ticks through the real bar builder onto the streams
# ---------------------------------------------------------------------------


def _ticks() -> list[Tick]:
    """Every 15s from 09:15 to noon for every symbol. Quiet through the
    opening range, then rising; SITBBB silent through HOLE."""
    out: list[Tick] = []
    for symbol in [*PLANNED, VETOED]:
        rng = random.Random(f"ticks-{symbol}")
        moment, price, volume = ist(DAY, 9, 15), 1000.0, 0
        while moment < ist(DAY, *SESSION_END):
            hour, minute = moment.astimezone(IST).hour, moment.astimezone(IST).minute
            price += rng.uniform(-0.2, 0.2) if (hour, minute) < (9, 30) else rng.uniform(-0.1, 0.45)
            volume += rng.randint(200, 900)
            silent = symbol == "SITBBB" and HOLE[0] <= (hour, minute) < HOLE[1]
            if not silent:
                out.append(
                    Tick(
                        symbol=symbol,
                        exchange_ts=moment,
                        received_ts=moment,
                        ltp=Decimal(f"{price:.2f}"),
                        volume=volume,
                    )
                )
            moment += dt.timedelta(seconds=15)
    return sorted(out, key=lambda t: (t.exchange_ts, t.symbol))


class Producer:
    def __init__(self, r: aioredis.Redis) -> None:
        self._r = r
        self._builders = {
            s: MultiTimeframeBuilder(symbol=s, calendar=CAL, timeframes=TIMEFRAMES)
            for s in [*PLANNED, VETOED]
        }

    async def tick(self, tick: Tick) -> bool:
        sealed = self._builders[tick.symbol].add(tick)
        if sealed:
            await publish_sealed_bars(self._r, sealed, now=tick.exchange_ts)
        return bool(sealed)


# ---------------------------------------------------------------------------
# The signal side, built the way a service start would build it
# ---------------------------------------------------------------------------


class _Trending:
    async def facts(self, now: dt.datetime) -> MarketFacts:
        return MarketFacts(regime=Regime.TRENDING)


class _Facts:
    """InstrumentRepository + DailyStatusRepository, as load_symbol_facts reads them."""

    def __init__(self, session: AsyncSession, instruments: InstrumentRepository) -> None:
        self._instruments = instruments
        self._status = DailyStatusRepository(session, instruments)

    def tick_size(self, symbol: str) -> Decimal:
        return self._instruments.tick_size(symbol)

    async def profile(self, symbol: str) -> dict[str, Any] | None:
        return await self._instruments.profile(symbol)

    async def daily_status(self, symbol: str, trade_date: dt.date) -> dict[str, Any] | None:
        return await self._status.for_symbol(symbol, trade_date)


class _Entries:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def entries_on(self, trade_date: dt.date, strategy_id: str, symbol: str) -> int:
        async with db_engine.read_only(self._factory) as session:
            return await PositionRepository(await _as_app(session), None).entries_on(  # type: ignore[arg-type]
                trade_date, strategy_id, symbol
            )


class _ShadowRows:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def record_shadow_signal(self, row: dict[str, Any]) -> int:
        async with db_engine.unit_of_work(self._factory) as session:
            return await ShadowSignalRepository(await _as_app(session)).record_shadow_signal(row)


class PaperDesk:
    """The PAPER route, asking execution's reader what it would size from."""

    def __init__(self, r: aioredis.Redis) -> None:
        self._reader = PublishedATR(r, timeframes=strategy_timeframes([PAPER_DOC, SHADOW_DOC]))
        self.arrivals: list[tuple[Trigger, Decimal | None]] = []

    async def submit(self, trigger: Trigger, strategy: RunnableStrategy) -> None:
        atr = await self._reader.atr(
            trigger.symbol,
            strategy_id=trigger.strategy_id,
            now=trigger.fired_at + dt.timedelta(seconds=2),
        )
        self.arrivals.append((trigger, atr))


class _NoLive:
    async def submit(self, trigger: Trigger, strategy: RunnableStrategy) -> None:
        raise AssertionError(f"nothing is ACTIVE today, and {trigger.strategy_id} routed live")


class Side:
    """One process's worth of signal side: built at start, gone at a crash."""

    def __init__(self, loop: SignalLoop, source: RedisBarSource, paper: PaperDesk) -> None:
        self.loop, self.source, self.paper = loop, source, paper
        self.reports: list[BarReport] = []

    @classmethod
    async def start(
        cls, r: aioredis.Redis, factory: async_sessionmaker[AsyncSession], *, consumer: str
    ) -> Side:
        async with db_engine.read_only(factory) as session:
            await _as_app(session)
            plan = await load_session_plan(PlanRepository(session), DAY)
            assert plan is not None
            instruments = InstrumentRepository(session)
            await instruments.refresh_cache()
            facts, refused = await load_symbol_facts(
                plan, _Facts(session, instruments), CAL, buffer_minutes=5
            )
            assert refused == {}
            loaded = await StrategyRegistry(StrategyRepository(session)).load()
            assert dict(loaded.rejected) == {}
            engine = IndicatorEngine()
            history: dict[tuple[str, Timeframe], list[Bar]] = {}
            store = BarRepository(session, instruments)
            for tf in TIMEFRAMES:
                for symbol, rows in (
                    await store.warm_up_batch(sorted(plan.symbols), tf, 260)
                ).items():
                    history[(symbol, tf)] = [Bar(**row) for row in rows]
            assert warm_up_symbols(engine, history).ok

        async def publish(indicator_set: Any) -> Any:
            return await publish_indicator_state(r, indicator_set)

        paper = PaperDesk(r)
        clock = {"now": ist(DAY, 9, 15)}
        loop = SignalLoop(
            plan=plan,
            facts=facts,
            strategies=loaded.runnable,
            engine=engine,
            publish=publish,
            calendar=CAL,
            entries=_Entries(factory),
            live=_NoLive(),
            paper=paper,
            shadow=ShadowRecorder(_ShadowRows(factory)),
            market=_Trending(),
            clock=lambda: clock["now"],
        )
        side = cls(loop, RedisBarSource(r, TIMEFRAMES, consumer=consumer, block_ms=20), paper)
        side._clock = clock
        await side.source.start()
        return side

    _clock: dict[str, dt.datetime]

    async def drain(self, now: dt.datetime) -> None:
        self._clock["now"] = now
        while batch := await self.source.next_batch():
            for delivery in batch:
                report = await deliver(self.loop, self.source, delivery)
                assert report is not None, "a bar was dead-lettered"
                self.reports.append(report)


def _routed(
    reports: list[BarReport],
) -> list[tuple[str, str, dt.datetime, uuid.UUID, Decimal, Decimal]]:
    return [
        (
            d.strategy_id,
            r.symbol,
            r.open_ts,
            d.trigger.correlation_id,
            d.trigger.trigger_price,
            d.trigger.suggested_stop,
        )
        for r in reports
        for d in r.decisions
        if d.outcome is StrategyOutcome.ROUTED and d.trigger is not None
    ]


async def _session(
    r: aioredis.Redis, factory: async_sessionmaker[AsyncSession], *, restart: bool
) -> tuple[
    list[BarReport],
    list[tuple[Trigger, Decimal | None]],
    dict[BarOutcome, int],
    list[tuple[str, Timeframe, dt.datetime]],
]:
    producer = Producer(r)
    side = await Side.start(r, factory, consumer="signal-engine-sit27")
    reports: list[BarReport] = []
    arrivals: list[tuple[Trigger, Decimal | None]] = []
    replayed: dict[BarOutcome, int] = {}
    stranded: list[tuple[str, Timeframe, dt.datetime]] = []
    restarted = False
    for tick in _ticks():
        if restart and not restarted and tick.exchange_ts >= ist(DAY, *RESTART_AT):
            # The process dies holding a batch it read and never acknowledged.
            await producer.tick(tick)
            unacked = await side.source.next_batch()
            assert unacked, "the crash should strand at least one bar"
            stranded = [(d.bar.symbol, d.bar.timeframe, d.bar.open_ts) for d in unacked]
            reports += side.reports
            arrivals += side.paper.arrivals
            side = await Side.start(r, factory, consumer="signal-engine-sit27")
            replayed = await replay_today(side.loop, r)
            restarted = True
            await side.drain(tick.exchange_ts + dt.timedelta(seconds=1))
            continue
        if await producer.tick(tick):
            await side.drain(tick.exchange_ts + dt.timedelta(seconds=1))
    reports += side.reports
    arrivals += side.paper.arrivals
    return reports, arrivals, replayed, stranded


class TestSit27ASignalSession:
    async def test_a_morning_of_bars_is_evaluated_once_each_and_routed_by_state(
        self,
        owner: psycopg.Connection,
        r: aioredis.Redis,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _premarket(owner, factory)
        reports, arrivals, _, _ = await _session(r, factory, restart=False)

        # Nothing outside the plan moved.
        assert {x.outcome for x in reports if x.symbol == VETOED} == {BarOutcome.NOT_IN_PLAN}
        # Every planned bar was evaluated exactly once (no duplicates in a clean run).
        planned = [x for x in reports if x.symbol in PLANNED]
        keys = [(x.symbol, x.timeframe, x.open_ts) for x in planned]
        assert len(keys) == len(set(keys))
        assert BarOutcome.DUPLICATE not in {x.outcome for x in planned}

        # SITBBB's feed hole stopped it for the rest of the morning; SITAAA ran on.
        bbb = [x for x in planned if x.symbol == "SITBBB"]
        gap_at = min(x.open_ts for x in bbb if x.outcome is BarOutcome.GAP)
        assert gap_at.astimezone(IST).time() >= dt.time(10, 45)
        assert {x.outcome for x in bbb if x.open_ts > gap_at} <= {
            BarOutcome.NOT_READY,
            BarOutcome.GAP,
        }
        assert not [d for x in bbb if x.open_ts >= gap_at for d in x.decisions]
        aaa_5m = [x for x in planned if x.symbol == "SITAAA" and x.timeframe is Timeframe.M5]
        assert {x.outcome for x in aaa_5m} == {BarOutcome.EVALUATED}

        # Both strategies fired; each reached its own route and only that.
        routed = _routed(reports)
        assert {sid for sid, *_ in routed} == {"sit27_paper", "sit27_shadow"}
        paper_ids = {t.correlation_id for t, _ in arrivals}
        assert paper_ids == {cid for sid, _, _, cid, _, _ in routed if sid == "sit27_paper"}
        shadow_rows = owner.execute(
            "SELECT count(*) FROM shadow_signal WHERE strategy_id = 'sit27_shadow'"
        ).fetchone()
        assert shadow_rows == (len([1 for sid, *_ in routed if sid == "sit27_shadow"]),)

        # Every paper trigger left with its bar's ATR already readable by execution.
        assert arrivals and all(atr is not None and atr > 0 for _, atr in arrivals)
        # Nothing stranded.
        for tf in TIMEFRAMES:
            assert await streams.pending_count(r, f"stream:bars:{tf.value}", GROUP) == 0

    async def test_a_crash_and_restart_mid_morning_changes_no_decision(
        self,
        owner: psycopg.Connection,
        r: aioredis.Redis,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _premarket(owner, factory)
        clean, clean_arrivals, _, _ = await _session(r, factory, restart=False)
        await r.flushall()
        owner.execute("DELETE FROM shadow_signal")

        crashed, crashed_arrivals, replayed, stranded = await _session(r, factory, restart=True)

        assert replayed.get(BarOutcome.REPLAYED, 0) > 0
        assert BarOutcome.EVALUATED not in replayed, "a replayed bar was evaluated"
        # The batch the dead process held came to the new one and was EVALUATED
        # there, once - not replayed past (SIT-009) and not counted twice.
        handled = {(x.symbol, x.timeframe, x.open_ts): x.outcome for x in crashed}
        keys = [(x.symbol, x.timeframe, x.open_ts) for x in crashed]
        for key in stranded:
            assert keys.count(key) == 1, key
            if key[0] == "SITAAA":
                assert handled[key] is BarOutcome.EVALUATED, (key, handled[key])
        assert BarOutcome.DUPLICATE not in set(handled.values())
        # Same decisions, same signals, same ids - before and after the restart.
        assert _routed(crashed) == _routed(clean)
        assert [t.correlation_id for t, _ in crashed_arrivals] == [
            t.correlation_id for t, _ in clean_arrivals
        ]
        assert owner.execute("SELECT count(*) FROM shadow_signal").fetchone() == (
            len([1 for sid, *_ in _routed(clean) if sid == "sit27_shadow"]),
        )
