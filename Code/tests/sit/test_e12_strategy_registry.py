"""SIT-26: the strategy registry across starts, a session and an overnight (E12-S01).

The unit and integration tests prove the registry's contract once each. This
asks what only the assembled thing can: **is what the signal side runs after
a start exactly what was registered - the same strategies, the same
decisions on the same session - and does every start refuse, by name, what
it must not run, while running the rest?**

Real PostgreSQL migrated to head, every write and read under the
application's own role; the operator's lifecycle moves are the UPDATEs that
role is allowed (E12-S02's state machine will make them); the session is
``test_tick_to_trigger``'s tick-to-bar pipeline - cleaning, bar building,
indicators, opening range - feeding the evaluators the registry loaded.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from algotrader.common.db import engine as db_engine
from algotrader.common.db.repositories import StrategyRepository
from algotrader.common.enums import StrategyState
from algotrader.strategy.dsl import compile_strategy, load_strategy_yaml
from algotrader.strategy.registry import (
    LoadedStrategies,
    StrategyRegistry,
    register_directory,
)
from algotrader.strategy.runtime import StrategyEvaluator
from tests.integration.test_tick_to_trigger import STRATEGY, Pipeline, _session_ticks

pytestmark = [pytest.mark.integration]

SHIPPED = Path(__file__).resolve().parents[2] / "config" / "strategies"
CID = UUID("26262626-2626-2626-2626-262626262626")
START_1 = dt.datetime(2026, 8, 20, 2, 30, tzinfo=dt.UTC)  # 08:00 IST
START_2 = START_1 + dt.timedelta(days=1)

#: Registered by the operator alongside the shipped seed. ``e2e_breakout``
#: fires on the pipeline's rising session; ``e2e_quiet`` asks for a volume
#: surge that session never has, so it records and never fires.
BREAKOUT = STRATEGY
QUIET = STRATEGY.replace("id: e2e_breakout", "id: e2e_quiet").replace(
    "    - {primitive: price_above_ma, params: {period: 20}}",
    "    - {primitive: price_above_ma, params: {period: 20}}\n"
    "    - {primitive: volume_ratio_above, params: {threshold: 20}}",
)


def _owner_dsn(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://")


@pytest.fixture(autouse=True)
def _empty_registry(migrated_database: str) -> Iterator[None]:
    def wipe() -> None:
        with psycopg.connect(_owner_dsn(migrated_database), autocommit=True) as conn:
            conn.execute("DELETE FROM strategy")

    wipe()
    yield
    wipe()


@pytest.fixture
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    eng = db_engine.create_engine_from_url(migrated_database)
    yield eng
    await eng.dispose()


@pytest.fixture
def factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return db_engine.create_session_factory(engine)


@pytest.fixture
def owner(migrated_database: str) -> Iterator[psycopg.Connection]:
    """The table owner: can do what the application cannot - the overnight edit."""
    with psycopg.connect(_owner_dsn(migrated_database), autocommit=True) as conn:
        yield conn


@pytest.fixture
def operator(migrated_database: str) -> Iterator[psycopg.Connection]:
    """The application role moving a strategy through its lifecycle columns."""
    with psycopg.connect(_owner_dsn(migrated_database), autocommit=True) as conn:
        conn.execute("SET ROLE algotrader_app")
        yield conn


def _move(operator: psycopg.Connection, strategy_id: str, state: StrategyState) -> None:
    operator.execute(
        "UPDATE strategy SET state = %s, state_changed_at = now() WHERE id = %s",
        (state.value, strategy_id),
    )


async def _start(
    factory: async_sessionmaker[AsyncSession], now: dt.datetime, *authored: str
) -> tuple[dict[str, str], LoadedStrategies]:
    """One service start: seed the shipped files, register anything newly
    authored, commit; then load what may run, in a separate read."""
    async with db_engine.unit_of_work(factory) as session:
        await session.execute(text("SET LOCAL ROLE algotrader_app"))
        registry = StrategyRegistry(StrategyRepository(session))
        seeded = await register_directory(registry, SHIPPED, now=now)
        for yaml_text in authored:
            await registry.register(load_strategy_yaml(yaml_text), now=now)
    async with db_engine.read_only(factory) as session:
        await session.execute(text("SET LOCAL ROLE algotrader_app"))
        return seeded, await StrategyRegistry(StrategyRepository(session)).load()


def _session_decisions(evaluators: dict[str, StrategyEvaluator]) -> dict[str, list[Any]]:
    """Walk one session's ticks and ask every evaluator at every sealed bar."""
    pipeline = Pipeline()
    pipeline.warm_up()
    decisions: dict[str, list[Any]] = {name: [] for name in evaluators}
    seen = 0
    for i, raw in enumerate(_session_ticks()):
        if i == 180:  # 15 minutes in
            pipeline.seal_opening_range()
        pipeline.feed(raw)
        if pipeline.bars_built == seen or pipeline.last_price is None:
            continue
        seen = pipeline.bars_built
        ctx = pipeline.context()
        for name, evaluator in evaluators.items():
            trigger = evaluator.fire(ctx, correlation_id=CID)
            decisions[name].append(
                None
                if trigger is None
                else (trigger.trigger_price, trigger.suggested_stop, trigger.timeframe_agreement)
            )
    return decisions


class TestSit26TheRegistryAcrossStartsAndASession:
    async def test_what_runs_after_each_start_is_what_was_registered(
        self, factory: async_sessionmaker[AsyncSession], operator: psycopg.Connection
    ) -> None:
        # -- start 1: seed, author, and nothing yet runs ----------------------
        seeded, loaded = await _start(factory, START_1, BREAKOUT, QUIET)
        assert seeded == {"orb_classic.yaml": "REGISTERED"}
        assert set(loaded.records) == {"orb_classic", "e2e_breakout", "e2e_quiet"}
        assert loaded.runnable == (), "registration armed something"
        assert dict(loaded.rejected) == {}

        # -- the operator puts two of them on paper and in shadow -------------
        _move(operator, "e2e_breakout", StrategyState.PAPER)
        _move(operator, "e2e_quiet", StrategyState.SHADOW)

        # -- start 2: the same seed changes nothing; the two now run ----------
        seeded, loaded = await _start(factory, START_2)
        assert seeded == {"orb_classic.yaml": "ALREADY_REGISTERED"}
        assert [(r.strategy_id, r.state) for r in loaded.runnable] == [
            ("e2e_breakout", StrategyState.PAPER),
            ("e2e_quiet", StrategyState.SHADOW),
        ]
        assert loaded.records["orb_classic"].state is StrategyState.DRAFT
        assert dict(loaded.rejected) == {}

        # -- the session: what was loaded decides exactly as what was written --
        authored = {
            doc.id: StrategyEvaluator(doc)
            for doc in (compile_strategy(load_strategy_yaml(t)) for t in (BREAKOUT, QUIET))
        }
        from_registry = {r.strategy_id: r.evaluator for r in loaded.runnable}
        live = _session_decisions(from_registry)
        source = _session_decisions(authored)
        assert live == source
        assert any(d is not None for d in live["e2e_breakout"]), "the session never fired"
        assert all(d is None for d in live["e2e_quiet"])

        # -- start 3: a restart reloads the same set, hash for hash -----------
        _, again = await _start(factory, START_2 + dt.timedelta(hours=1))
        assert [(r.strategy_id, r.record.dsl_hash) for r in again.runnable] == [
            (r.strategy_id, r.record.dsl_hash) for r in loaded.runnable
        ]
        assert all(
            again.records[sid].document == loaded.records[sid].document for sid in loaded.records
        )

    async def test_an_overnight_edit_is_refused_at_the_next_start_and_the_rest_run(
        self,
        factory: async_sessionmaker[AsyncSession],
        operator: psycopg.Connection,
        owner: psycopg.Connection,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await _start(factory, START_1, BREAKOUT, QUIET)
        _move(operator, "e2e_breakout", StrategyState.PAPER)
        _move(operator, "e2e_quiet", StrategyState.SHADOW)

        # Overnight, someone with the owner's credentials loosens the breakout
        # - a shorter average, so it fires earlier - and leaves the hash alone.
        owner.execute(
            "UPDATE strategy SET dsl = jsonb_set(dsl, '{entry,all_of,1,params,period}', '5') "
            "WHERE id = 'e2e_breakout'"
        )
        with caplog.at_level(logging.ERROR, logger="algotrader.strategy.registry"):
            _, loaded = await _start(factory, START_2)

        assert [r.strategy_id for r in loaded.runnable] == ["e2e_quiet"]
        assert "hashes to" in loaded.rejected["e2e_breakout"]
        assert any("e2e_breakout" in r.getMessage() for r in caplog.records)

    async def test_a_retired_strategy_stays_retired_and_the_seed_cannot_run_here(
        self, factory: async_sessionmaker[AsyncSession], operator: psycopg.Connection
    ) -> None:
        """Two operator moves a start must answer honestly: one strategy
        retired (re-seeding must not revive it), and the shipped seed put in
        shadow - which this engine cannot evaluate (1d/1w agreement), so it is
        refused by name while the other still runs."""
        await _start(factory, START_1, BREAKOUT)
        _move(operator, "e2e_breakout", StrategyState.PAPER)
        _move(operator, "orb_classic", StrategyState.SHADOW)

        _, loaded = await _start(factory, START_2)
        assert [r.strategy_id for r in loaded.runnable] == ["e2e_breakout"]
        assert "cannot run here" in loaded.rejected["orb_classic"]

        _move(operator, "e2e_breakout", StrategyState.RETIRED)
        _move(operator, "orb_classic", StrategyState.RETIRED)
        seeded, loaded = await _start(factory, START_2 + dt.timedelta(days=1))
        assert seeded == {"orb_classic.yaml": "ALREADY_REGISTERED"}
        assert loaded.runnable == ()
        assert {r.state for r in loaded.records.values()} == {StrategyState.RETIRED}

    async def test_a_document_respelled_under_a_new_name_does_not_come_back(
        self, factory: async_sessionmaker[AsyncSession], operator: psycopg.Connection
    ) -> None:
        """The search-until-it-passes loop, across days: rejected on Monday,
        resubmitted on Tuesday under a new id with its numbers respelled."""
        await _start(factory, START_1, BREAKOUT)
        _move(operator, "e2e_breakout", StrategyState.REJECTED)

        respelled = (
            BREAKOUT.replace("id: e2e_breakout", "id: e2e_breakout_revisited")
            .replace("{r: 2.0}", "{r: 2}")
            .replace("{multiplier: 1.5, period: 14}", "{multiplier: 1.50}")
        )
        async with db_engine.unit_of_work(factory) as session:
            await session.execute(text("SET LOCAL ROLE algotrader_app"))
            registry = StrategyRegistry(StrategyRepository(session))
            with pytest.raises(ValueError, match=r"identically.*'e2e_breakout' \(REJECTED\)"):
                await registry.register(load_strategy_yaml(respelled), now=START_2)

        _, loaded = await _start(factory, START_2)
        assert set(loaded.records) == {"orb_classic", "e2e_breakout"}

    async def test_a_database_that_is_down_at_start_stops_the_load(self) -> None:
        """Degraded dependency: no answer is not "no strategies". The load
        raises; it does not return an empty set the signal side would read
        as a quiet day."""
        dead = db_engine.create_engine_from_url(
            "postgresql+psycopg://nobody:nothing@127.0.0.1:1/none", connect_timeout_seconds=2
        )
        try:
            with pytest.raises(OperationalError):
                async with db_engine.read_only(db_engine.create_session_factory(dead)) as session:
                    await StrategyRegistry(StrategyRepository(session)).load()
        finally:
            await dead.dispose()


def test_the_two_authored_strategies_differ() -> None:
    """Guards the scenario: were QUIET's replacement to miss, both would be
    the same behaviour and the second registration would be refused."""
    assert QUIET != BREAKOUT and "volume_ratio_above" in QUIET
    breakout, quiet = (load_strategy_yaml(t) for t in (BREAKOUT, QUIET))
    assert breakout.content_hash() != quiet.content_hash()
