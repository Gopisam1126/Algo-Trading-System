"""E12-S01 - the strategy registry against PostgreSQL, under the application's role.

The unit tests hold the registry to a store that keeps two promises. These
hold the TABLE to them, and to the rest of migration ``a986f25d3838``:

* the application role can register and load - checked under ``SET ROLE``,
  because every other test connects as the owner and cannot see a grant;
* it cannot UPDATE any column but the five lifecycle ones, and cannot DELETE;
* a duplicate behaviour, a misspelt state, an unapproved DEGRADED and a
  broken lineage are refused by the database itself;
* a document edited behind the application - by the owner, who can - is
  refused when the registry loads it;
* two writers racing for one id, or one behaviour, end with one row.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from algotrader.common.db import engine as db_engine
from algotrader.common.db.models import LIFECYCLE_COLUMNS, Strategy
from algotrader.common.db.repositories import StrategyRepository
from algotrader.common.enums import StrategyState
from algotrader.strategy.dsl import StrategyDocument, load_strategy_yaml
from algotrader.strategy.registry import (
    RegistrationOutcome,
    RegistrationRefusedError,
    StrategyRegistry,
    row_for,
)

pytestmark = [pytest.mark.integration]

SEED = Path(__file__).resolve().parents[2] / "config" / "strategies" / "orb_classic.yaml"
T0 = dt.datetime(2026, 11, 3, 3, 0, tzinfo=dt.UTC)


def _runnable_seed() -> StrategyDocument:
    """orb_classic, asking for agreement on timeframes this engine carries.

    The shipped file asks for 1h/1d/1w and the engine carries 5m/15m/1h, so
    the shipped document is refused as "cannot run here" (see the unit tests).
    """
    data = load_strategy_yaml(SEED.read_text(encoding="utf-8")).model_dump(mode="json")
    data["entry"]["all_of"][2]["params"]["of"] = "5m,15m,1h"
    return StrategyDocument.model_validate(data)


def _variant(doc: StrategyDocument, new_id: str, threshold: float) -> StrategyDocument:
    data = doc.model_dump(mode="json")
    data["id"] = new_id
    data["entry"]["all_of"][1]["params"]["threshold"] = threshold
    return StrategyDocument.model_validate(data)


def _owner_dsn(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://")


@pytest.fixture(autouse=True)
def _no_strategies_survive(migrated_database: str) -> Iterator[None]:
    """These tests COMMIT - the race needs two real transactions - so the
    table is emptied before and after, as the owner (the application role
    cannot delete, which is the point)."""

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
    with psycopg.connect(_owner_dsn(migrated_database), autocommit=True) as conn:
        yield conn


@pytest.fixture
def app(migrated_database: str) -> Iterator[psycopg.Connection]:
    """The production role, rolled back after each test."""
    with psycopg.connect(_owner_dsn(migrated_database)) as conn:
        conn.execute("SET ROLE algotrader_app")
        yield conn
        conn.rollback()


async def _as_app(session: AsyncSession) -> StrategyRegistry:
    await session.execute(text("SET LOCAL ROLE algotrader_app"))
    return StrategyRegistry(StrategyRepository(session))


async def _register(
    factory: async_sessionmaker[AsyncSession], doc: StrategyDocument, now: dt.datetime = T0
) -> RegistrationOutcome:
    async with db_engine.unit_of_work(factory) as session:
        return await (await _as_app(session)).register(doc, now=now)


async def _load(factory: async_sessionmaker[AsyncSession]) -> Any:
    async with db_engine.read_only(factory) as session:
        return await (await _as_app(session)).load()


def _insert(conn: psycopg.Connection, row: dict[str, Any]) -> None:
    values = {k: Jsonb(v) if isinstance(v, dict) else v for k, v in row.items()}
    cols = ", ".join(values)
    marks = ", ".join(["%s"] * len(values))
    conn.execute(f"INSERT INTO strategy ({cols}) VALUES ({marks})", tuple(values.values()))


class TestTheApplicationRoleRegistersAndLoads:
    async def test_a_registered_document_round_trips_through_the_table(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        doc = _runnable_seed()
        assert await _register(factory, doc) is RegistrationOutcome.REGISTERED

        loaded = await _load(factory)
        record = loaded.records["orb_classic"]
        assert record.document == doc, "JSONB changed the document"
        assert record.state is StrategyState.DRAFT and record.registered_at == T0
        assert record.dsl_hash == doc.content_hash()
        assert loaded.runnable == () and dict(loaded.rejected) == {}

    async def test_registering_it_again_is_recognised_after_the_round_trip(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Equality survives JSONB: a numeric re-spelling on the way back
        would turn every restart's seeding into an "immutable" refusal."""
        doc = _runnable_seed()
        await _register(factory, doc)
        later = T0 + dt.timedelta(days=1)
        assert await _register(factory, doc, later) is RegistrationOutcome.ALREADY_REGISTERED

    async def test_a_state_moved_through_the_lifecycle_columns_comes_back_runnable(
        self, factory: async_sessionmaker[AsyncSession], app: psycopg.Connection
    ) -> None:
        await _register(factory, _runnable_seed())
        app.execute(
            "UPDATE strategy SET state = 'PAPER', state_changed_at = now() WHERE id = 'orb_classic'"
        )
        app.commit()

        loaded = await _load(factory)
        [runnable] = loaded.runnable
        assert runnable.strategy_id == "orb_classic" and runnable.state is StrategyState.PAPER
        assert runnable.evaluator.strategy_id == "orb_classic"


class TestOneRowPerBehaviour:
    async def test_a_twin_is_refused_by_the_registry_and_by_the_table(
        self, factory: async_sessionmaker[AsyncSession], owner: psycopg.Connection
    ) -> None:
        doc = _runnable_seed()
        await _register(factory, doc)
        twin = _variant(doc, "orb_twin_v1", 1.5)  # 1.5 is the seed's own threshold
        with pytest.raises(RegistrationRefusedError, match="'orb_classic'"):
            await _register(factory, twin)

        async with db_engine.unit_of_work(factory) as session:
            await session.execute(text("SET LOCAL ROLE algotrader_app"))
            assert await StrategyRepository(session).insert_strategy(row_for(twin, now=T0)) is False

        with pytest.raises(psycopg.errors.UniqueViolation, match="uq_strategy_dsl_hash"):
            _insert(owner, row_for(twin, now=T0))

    async def test_two_writers_racing_for_one_id_leave_one_row(
        self, factory: async_sessionmaker[AsyncSession], owner: psycopg.Connection
    ) -> None:
        doc = _runnable_seed()
        outcomes = await _race(factory, doc, doc)
        assert sorted(str(o) for o in outcomes) == ["ALREADY_REGISTERED", "REGISTERED"]
        assert owner.execute("SELECT count(*) FROM strategy").fetchone() == (1,)

    async def test_two_writers_racing_with_one_behaviour_leave_one_row(
        self, factory: async_sessionmaker[AsyncSession], owner: psycopg.Connection
    ) -> None:
        doc = _runnable_seed()
        twin = _variant(doc, "orb_twin_v1", 1.5)
        outcomes = await _race(factory, doc, twin)
        registered = [o for o in outcomes if o is RegistrationOutcome.REGISTERED]
        refused = [o for o in outcomes if isinstance(o, RegistrationRefusedError)]
        assert len(registered) == 1 and len(refused) == 1, outcomes
        assert "behaves identically" in str(refused[0])
        assert owner.execute("SELECT count(*) FROM strategy").fetchone() == (1,)


async def _race(
    factory: async_sessionmaker[AsyncSession], first: StrategyDocument, second: StrategyDocument
) -> list[Any]:
    """The first writer holds its transaction open after inserting; the
    second reads before that commit and inserts into the conflict."""

    async def holder() -> RegistrationOutcome:
        async with db_engine.unit_of_work(factory) as session:
            outcome = await (await _as_app(session)).register(first, now=T0)
            await asyncio.sleep(0.6)
            return outcome

    async def contender() -> RegistrationOutcome:
        await asyncio.sleep(0.1)
        return await _register(factory, second)

    return list(await asyncio.gather(holder(), contender(), return_exceptions=True))


class TestTheDocumentCannotChange:
    @pytest.mark.parametrize(
        "column",
        sorted(
            c.name
            for c in Strategy.__table__.columns
            if c.name not in LIFECYCLE_COLUMNS["strategy"]
        ),
    )
    async def test_the_application_cannot_update_a_document_column(
        self, factory: async_sessionmaker[AsyncSession], app: psycopg.Connection, column: str
    ) -> None:
        await _register(factory, _runnable_seed())
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute(f"UPDATE strategy SET {column} = {column} WHERE id = 'orb_classic'")

    async def test_the_application_cannot_delete_a_strategy(
        self, factory: async_sessionmaker[AsyncSession], app: psycopg.Connection
    ) -> None:
        await _register(factory, _runnable_seed())
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute("DELETE FROM strategy WHERE id = 'orb_classic'")

    @pytest.mark.parametrize("column", LIFECYCLE_COLUMNS["strategy"])
    async def test_the_lifecycle_columns_are_still_writable(
        self, factory: async_sessionmaker[AsyncSession], app: psycopg.Connection, column: str
    ) -> None:
        """The control: E12-S02 must be able to move a strategy."""
        await _register(factory, _runnable_seed())
        app.execute(f"UPDATE strategy SET {column} = {column} WHERE id = 'orb_classic'")

    def test_the_grants_are_exactly_insert_select_and_the_lifecycle_columns(
        self, owner: psycopg.Connection
    ) -> None:
        table = {
            r[0]
            for r in owner.execute(
                "SELECT privilege_type FROM information_schema.role_table_grants "
                "WHERE table_name = 'strategy' AND grantee = 'algotrader_app'"
            ).fetchall()
        }
        updatable = {
            r[0]
            for r in owner.execute(
                "SELECT column_name FROM information_schema.column_privileges "
                "WHERE table_name = 'strategy' AND grantee = 'algotrader_app' "
                "AND privilege_type = 'UPDATE'"
            ).fetchall()
        }
        assert table == {"SELECT", "INSERT"}
        assert updatable == set(LIFECYCLE_COLUMNS["strategy"])

    async def test_a_document_edited_behind_the_application_is_refused_at_load(
        self, factory: async_sessionmaker[AsyncSession], owner: psycopg.Connection
    ) -> None:
        """The owner CAN edit it. The load is what stands behind the grant."""
        doc = _runnable_seed()
        await _register(factory, doc)
        await _register(factory, _variant(doc, "orb_other_v1", 1.8), T0 + dt.timedelta(minutes=1))
        owner.execute("UPDATE strategy SET state = 'PAPER'")
        owner.execute(
            "UPDATE strategy SET dsl = jsonb_set(dsl, '{entry,all_of,1,params,threshold}', '0.2') "
            "WHERE id = 'orb_classic'"
        )

        loaded = await _load(factory)
        assert "hashes to" in loaded.rejected["orb_classic"]
        assert [r.strategy_id for r in loaded.runnable] == ["orb_other_v1"]


class TestTheTableRefusesWhatTheRegistryWouldNever:
    def _row(self, **overrides: Any) -> dict[str, Any]:
        row = row_for(_runnable_seed(), now=T0)
        row["id"] = f"probe_{uuid.uuid4().hex[:8]}"
        row["dsl_hash"] = uuid.uuid4().hex * 2
        row.update(overrides)
        return row

    def test_degraded_needs_an_approval_like_active(self, app: psycopg.Connection) -> None:
        """The BR-5 hole this story closed: DEGRADED places live orders."""
        with pytest.raises(psycopg.errors.CheckViolation, match="live_states_need_approval"):
            _insert(app, self._row(state="DEGRADED"))

    def test_an_update_to_degraded_needs_one_too(self, app: psycopg.Connection) -> None:
        row = self._row()
        _insert(app, row)
        with pytest.raises(psycopg.errors.CheckViolation, match="live_states_need_approval"):
            app.execute("UPDATE strategy SET state = 'DEGRADED' WHERE id = %s", (row["id"],))

    @pytest.mark.parametrize("state", list(StrategyState))
    def test_every_strategy_state_is_a_state_the_table_accepts(
        self, app: psycopg.Connection, state: StrategyState
    ) -> None:
        """Holds the migration's frozen list to the enum, in both directions
        with the test below."""
        approval = (
            {"approved_by": "operator", "approved_at": T0} if state.places_live_orders else {}
        )
        _insert(app, self._row(state=state.value, **approval))

    @pytest.mark.parametrize("state", ["active", "LIVE", "", "ACTIVE "])
    def test_a_state_that_is_not_one_is_refused(self, app: psycopg.Connection, state: str) -> None:
        with pytest.raises(psycopg.errors.CheckViolation, match="known_state"):
            _insert(app, self._row(state=state))

    @pytest.mark.parametrize(
        ("overrides", "constraint"),
        [
            pytest.param({"dsl_hash": "not-a-sha256"}, "dsl_hash_is_sha256", id="hash-format"),
            pytest.param({"dsl_hash": "A" * 64}, "dsl_hash_is_sha256", id="hash-uppercase"),
            pytest.param({"version": 2}, "root_is_version_one", id="root-v2"),
        ],
    )
    def test_a_malformed_row_is_refused(
        self, app: psycopg.Connection, overrides: dict[str, Any], constraint: str
    ) -> None:
        with pytest.raises(psycopg.errors.CheckViolation, match=constraint):
            _insert(app, self._row(**overrides))

    def test_version_zero_is_refused_even_with_a_parent(self, app: psycopg.Connection) -> None:
        """Given a parent, so ``root_is_version_one`` cannot be what refuses
        it - a root at version 0 breaks both, and PostgreSQL names only one."""
        parent = self._row()
        _insert(app, parent)
        with pytest.raises(psycopg.errors.CheckViolation, match="version_positive"):
            _insert(app, self._row(version=0, parent_id=parent["id"]))

    def test_a_strategy_cannot_be_its_own_parent(self, app: psycopg.Connection) -> None:
        """The foreign key accepts it: the row references itself and exists."""
        row = self._row(version=2)
        row["parent_id"] = row["id"]
        with pytest.raises(psycopg.errors.CheckViolation, match="not_its_own_parent"):
            _insert(app, row)
