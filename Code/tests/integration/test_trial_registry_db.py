"""E12-S04: the trial registry counts every search, honestly, against the real schema.

Run as the APPLICATION role (``SET LOCAL ROLE algotrader_app``): the registry
must work with exactly the INSERT and SELECT it is granted. That it cannot
UPDATE or DELETE is held by ``test_service_login.py`` (connecting AS the service
login) and ``test_schema_business_rules.py::test_br4_strategy_trials_cannot_be_deleted``.
The rows these tests write are removed as the table OWNER, before and after -
the application role cannot, which is the point.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from algotrader.common.db import engine as db_engine
from algotrader.common.db.repositories import PRE_REGISTRY_TRIALS, TrialRecord, TrialRepository
from algotrader.common.enums import StrategyOrigin, TrialOutcome

# The DSL's REGISTRY is populated by importing this package (dsl.REGISTRY says
# so). Imported explicitly: without it this file passes only when another
# test file happens to import it first, and fails alone.
from algotrader.strategy import primitives  # noqa: F401
from algotrader.strategy.dsl import StrategyDocument, load_strategy_yaml

pytestmark = [pytest.mark.integration]

SEED = Path(__file__).resolve().parents[2] / "config" / "strategies" / "orb_classic.yaml"


def _seed() -> StrategyDocument:
    return load_strategy_yaml(SEED.read_text(encoding="utf-8"))


def _setting(threshold: float) -> StrategyDocument:
    """One point of a parameter sweep over the seed's volume threshold."""
    data = _seed().model_dump(mode="json")
    data["entry"]["all_of"][1]["params"]["threshold"] = threshold
    return StrategyDocument.model_validate(data)


def _trial(doc: StrategyDocument, *, outcome: TrialOutcome = TrialOutcome.REJECTED) -> TrialRecord:
    return TrialRecord(
        strategy_id=doc.id,
        strategy_hash=doc.content_hash(),
        origin=StrategyOrigin.USER_AUTHORED,
        outcome=outcome,
        failed_check="G6" if outcome is TrialOutcome.REJECTED else None,
        report={"gauntlet": "probe", "checks_run": ["G1", "G6"]},
    )


def _owner_dsn(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://")


@pytest.fixture(autouse=True)
def _no_trials_survive(migrated_database: str) -> Iterator[None]:
    def wipe() -> None:
        with psycopg.connect(_owner_dsn(migrated_database), autocommit=True) as conn:
            conn.execute("DELETE FROM strategy_trial")

    wipe()
    yield
    wipe()


@pytest.fixture
async def factory(migrated_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine: AsyncEngine = db_engine.create_engine_from_url(migrated_database)
    yield db_engine.create_session_factory(engine)
    await engine.dispose()


async def _record(
    factory: async_sessionmaker[AsyncSession], trials: list[TrialRecord]
) -> list[int]:
    async with factory() as session, session.begin():
        await session.execute(text("SET LOCAL ROLE algotrader_app"))
        repo = TrialRepository(session)
        return [await repo.record(t) for t in trials]


async def _count(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session, session.begin():
        await session.execute(text("SET LOCAL ROLE algotrader_app"))
        return await TrialRepository(session).effective_trial_count()


def _rows(migrated_database: str) -> int:
    with psycopg.connect(_owner_dsn(migrated_database)) as conn:
        row = conn.execute("SELECT count(*) FROM strategy_trial").fetchone()
        assert row is not None
        return int(row[0])


async def test_an_empty_registry_counts_only_the_pre_registry_trials(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    assert await _count(factory) == PRE_REGISTRY_TRIALS


async def test_the_application_role_can_record_a_trial(
    factory: async_sessionmaker[AsyncSession], migrated_database: str
) -> None:
    ids = await _record(factory, [_trial(_seed(), outcome=TrialOutcome.PASSED)])
    assert len(ids) == 1 and ids[0] > 0
    assert _rows(migrated_database) == 1
    assert await _count(factory) == PRE_REGISTRY_TRIALS + 1


async def test_a_sweep_of_twenty_settings_records_twenty_trials(
    factory: async_sessionmaker[AsyncSession], migrated_database: str
) -> None:
    """AC2. Each setting is a different behaviour, so a different content hash:
    twenty rows, twenty trials - never 'one strategy tried once'."""
    sweep = [_setting(1.0 + i / 10) for i in range(20)]
    assert len({doc.content_hash() for doc in sweep}) == 20  # control: the sweep really varies
    await _record(factory, [_trial(doc) for doc in sweep])
    assert _rows(migrated_database) == 20
    assert await _count(factory) == PRE_REGISTRY_TRIALS + 20


async def test_an_identical_rerun_adds_a_row_but_not_a_trial(
    factory: async_sessionmaker[AsyncSession], migrated_database: str
) -> None:
    """Every run is recorded; only a distinct behaviour is a new trial."""
    doc = _setting(1.7)
    await _record(factory, [_trial(doc), _trial(doc), _trial(_setting(1.7))])
    assert _rows(migrated_database) == 3
    assert await _count(factory) == PRE_REGISTRY_TRIALS + 1


async def test_a_respelt_strategy_is_the_same_trial(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The hash is of behaviour: a stated default and an omitted one, or 2 and
    2.0, are one strategy, so re-spelling cannot inflate - or deflate - the
    count (E12-S01's content hash)."""
    await _record(factory, [_trial(_setting(2)), _trial(_setting(2.0))])
    assert await _count(factory) == PRE_REGISTRY_TRIALS + 1


async def test_rejections_count_exactly_as_passes_do(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await _record(
        factory,
        [
            _trial(_setting(1.1), outcome=TrialOutcome.PASSED),
            _trial(_setting(1.2), outcome=TrialOutcome.REJECTED),
        ],
    )
    assert await _count(factory) == PRE_REGISTRY_TRIALS + 2


async def test_the_application_role_still_cannot_delete_what_it_recorded(
    factory: async_sessionmaker[AsyncSession], migrated_database: str
) -> None:
    """AC1, at the point it matters: a row this repository wrote, refused for
    deletion by the same role that wrote it."""
    await _record(factory, [_trial(_setting(1.3))])
    with psycopg.connect(_owner_dsn(migrated_database)) as conn:
        conn.execute("SET ROLE algotrader_app")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM strategy_trial")
        conn.rollback()
    assert _rows(migrated_database) == 1
