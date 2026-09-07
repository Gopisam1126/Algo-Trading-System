"""E14-S08's acceptance criterion, against a real Redis and a real Postgres.

    🔴 Concurrent signals cannot over-allocate slots.

That claim cannot be proved with doubles. Its three layers live in three
different places — a Redis ``SET NX PX``, a partial unique index on
``positions``, and reconciliation between them — and the failure it guards
against is a race between them. ``tests/unit/test_slots.py`` covers the pieces;
this covers the property.

Both stores are genuine here on purpose: the unit suite's Redis double
implements the release script's compare-and-delete by hand, and a double that
got that subtly wrong would let every unit test pass while the real system
deleted another holder's lock.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from algotrader.common.db import engine as db_engine
from algotrader.common.db.repositories import InstrumentRepository, PositionRepository
from algotrader.common.redis import keys, locks
from algotrader.execution.slots import SlotManager, is_slot_taken

pytestmark = [pytest.mark.integration]

TOTAL_SLOTS = 5


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
async def engine(migrated_database: str) -> AsyncIterator[object]:
    eng = db_engine.create_engine_from_url(migrated_database)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: object) -> AsyncIterator[AsyncSession]:
    factory = db_engine.create_session_factory(engine)  # type: ignore[arg-type]
    async with factory() as s:
        yield s
        await s.rollback()


@pytest.fixture
async def instruments(session: AsyncSession) -> InstrumentRepository:
    repo = InstrumentRepository(session)
    await repo.upsert(
        [
            {
                "tradingsymbol": symbol,
                "exchange": "NSE",
                "broker_token": f"slot-tok-{index}",
                "tick_size": Decimal("0.05"),
                "lot_size": 1,
                "instrument_type": "EQ",
            }
            for index, symbol in enumerate(["INFY", "TCS", "WIPRO", "HDFCBANK", "SBIN", "ITC"])
        ]
    )
    await session.flush()
    return repo


@pytest.fixture
async def positions(session: AsyncSession, instruments: InstrumentRepository) -> PositionRepository:
    return PositionRepository(session, instruments)


def _position(symbol: str, slot_index: int) -> dict[str, Any]:
    return {
        "correlation_id": uuid.uuid4(),
        "symbol": symbol,
        "slot_index": slot_index,
        "direction": "LONG",
        "quantity": 10,
        "entry_price": Decimal("100"),
        "stop_price": Decimal("95"),
        "opened_at": dt.datetime.now(dt.UTC),
        "squareoff_deadline": dt.datetime.now(dt.UTC) + dt.timedelta(hours=5),
    }


class TestTheUniqueViolationIsRecognisable:
    """``EPIC01_TECHNICAL_SPEC.md §8.4`` instructs callers to catch the unique
    violation and treat it as *"slot taken"*. That instruction is only
    followable if the exception can be identified, and the existing test that
    was meant to establish this —
    ``test_repositories.py::test_slot_collision_raises_and_is_catchable`` —
    asserts ``pytest.raises((UniqueViolation, Exception))``. With ``Exception``
    in the tuple that matches anything at all, including a typo, so it could
    not fail and never established what its name claims.

    These pin the shape ``is_slot_taken`` depends on.
    """

    async def test_a_second_position_in_one_slot_is_refused(
        self, session: AsyncSession, positions: PositionRepository
    ) -> None:
        await positions.open_position(_position("INFY", 2))
        await session.flush()
        with pytest.raises(Exception) as excinfo:
            await positions.open_position(_position("TCS", 2))
            await session.flush()
        assert is_slot_taken(excinfo.value), (
            f"a slot collision surfaced as {type(excinfo.value).__name__}, which "
            f"is_slot_taken does not recognise. Callers following 8.4 would crash "
            f"on an outcome the spec calls normal."
        )

    async def test_it_is_recognised_as_the_slot_constraint_specifically(
        self, session: AsyncSession, positions: PositionRepository
    ) -> None:
        """Not merely "some unique violation". Reading a symbol collision as a
        slot collision would retry against a different slot and open a second
        position in a name already held."""
        await positions.open_position(_position("INFY", 1))
        await session.flush()
        with pytest.raises(Exception) as excinfo:
            await positions.open_position(_position("TCS", 1))
            await session.flush()
        assert is_slot_taken(excinfo.value, constraints=["uq_open_slot"])

    async def test_an_ordinary_insert_raises_nothing(
        self, session: AsyncSession, positions: PositionRepository
    ) -> None:
        """The control. A test that only ever asserts failure passes against a
        repository that can never insert anything."""
        position_id = await positions.open_position(_position("INFY", 0))
        await session.flush()
        assert position_id > 0
        assert await positions.occupied_slots() == {0}


class TestConcurrentSignalsCannotOverAllocate:
    """🔴 The acceptance criterion.

    **Each concurrent claimer gets its own session.** Not a detail: an
    ``AsyncSession`` wraps one connection and is not safe to use from two
    coroutines at once, so sharing one here would fail on SQLAlchemy's own
    concurrency guard rather than on the slot race — a red test that proves
    nothing about slots. It is also what the real system does, where two
    signals are two units of work.
    """

    @staticmethod
    async def _claim_concurrently(
        engine: object,
        client: aioredis.Redis,
        instruments: InstrumentRepository,
        count: int,
    ) -> list[int | None]:
        factory = db_engine.create_session_factory(engine)  # type: ignore[arg-type]
        handed: list[int | None] = []

        async def claim() -> None:
            async with factory() as own_session:
                repo = PositionRepository(own_session, InstrumentRepository(own_session))
                manager = SlotManager(client, repo, total_slots=TOTAL_SLOTS)
                async with manager.claiming("INFY") as index:
                    # A real await point inside the block: without one, the
                    # coroutines would run to completion one at a time and the
                    # race this exists to test would never occur.
                    await asyncio.sleep(0.01)
                    handed.append(index)

        await asyncio.gather(*(claim() for _ in range(count)))
        return handed

    async def test_two_claimers_on_an_empty_book_get_different_slots(
        self, engine: object, r: aioredis.Redis, instruments: InstrumentRepository
    ) -> None:
        """The narrow race: both read an empty book, both look at slot 0."""
        handed = await self._claim_concurrently(engine, r, instruments, 2)
        assert len(set(handed)) == 2, f"one slot was handed to both claimers: {handed}"

    async def test_more_claimers_than_slots_never_duplicates(
        self, engine: object, r: aioredis.Redis, instruments: InstrumentRepository
    ) -> None:
        handed = await self._claim_concurrently(engine, r, instruments, 12)
        allocated = [i for i in handed if i is not None]
        assert len(allocated) == len(set(allocated)), f"a slot was reused: {handed}"
        assert len(allocated) == TOTAL_SLOTS
        assert set(allocated) == set(range(TOTAL_SLOTS))
        assert handed.count(None) == 12 - TOTAL_SLOTS

    async def test_the_database_refuses_what_the_lock_somehow_lets_through(
        self, session: AsyncSession, positions: PositionRepository
    ) -> None:
        """Layer 2, exercised with layer 1 deliberately bypassed.

        The lock is the fast path and can be lost — to an expiry, a partition,
        or a future refactor. This asserts the guarantee underneath it still
        holds when it is, which is the entire reason §8.4 has three layers
        instead of one.
        """
        await positions.open_position(_position("INFY", 3))
        await session.flush()
        with pytest.raises(Exception) as excinfo:
            await positions.open_position(_position("TCS", 3))
            await session.flush()
        assert is_slot_taken(excinfo.value)

    async def test_slots_held_by_open_positions_are_never_offered(
        self, session: AsyncSession, r: aioredis.Redis, positions: PositionRepository
    ) -> None:
        await positions.open_position(_position("INFY", 0))
        await positions.open_position(_position("TCS", 1))
        await session.flush()
        manager = SlotManager(r, positions, total_slots=TOTAL_SLOTS)
        async with manager.claiming("WIPRO") as index:
            assert index == 2

    async def test_a_full_book_hands_out_nothing(
        self, session: AsyncSession, r: aioredis.Redis, positions: PositionRepository
    ) -> None:
        for index, symbol in enumerate(["INFY", "TCS", "WIPRO", "HDFCBANK", "SBIN"]):
            await positions.open_position(_position(symbol, index))
        await session.flush()
        manager = SlotManager(r, positions, total_slots=TOTAL_SLOTS)
        async with manager.claiming("ITC") as index:
            assert index is None


class TestRecyclingAndReconciliationAgainstRealStores:
    async def test_a_closed_position_frees_its_slot(
        self, session: AsyncSession, r: aioredis.Redis, positions: PositionRepository
    ) -> None:
        """Task 3. Recycling is not a mechanism of its own — the slot becomes
        allocatable the moment the row stops being OPEN, because that is what
        ``occupied_slots`` reads."""
        position_id = await positions.open_position(_position("INFY", 0))
        await session.flush()
        manager = SlotManager(r, positions, total_slots=TOTAL_SLOTS)
        await manager.record(0, "INFY")

        async with manager.claiming("TCS") as index:
            assert index == 1

        await positions.close_position(
            position_id,
            exit_price=Decimal("101"),
            exit_reason="TARGET",
            realized_pnl=Decimal("10"),
        )
        await session.flush()
        await manager.release(0)

        async with manager.claiming("TCS") as index:
            assert index == 0
        assert (await manager.reconcile()).is_clean

    async def test_reconciliation_sees_a_real_leaked_lock(
        self, r: aioredis.Redis, positions: PositionRepository
    ) -> None:
        """A genuine ``SET NX PX`` key, not a fake one — the point of running
        this tier at all."""
        manager = SlotManager(r, positions, total_slots=TOTAL_SLOTS)
        await locks.acquire_lock(r, keys.slot_lock(4), "a-dead-process", 60_000)
        drift = await manager.reconcile()
        assert drift.leaked_locks == (4,)
        assert not drift.is_clean

    async def test_reconciliation_sees_a_record_with_no_position(
        self, r: aioredis.Redis, positions: PositionRepository
    ) -> None:
        """The category that never heals on its own: this hash carries no TTL,
        so the slot reads as occupied for the rest of the session."""
        manager = SlotManager(r, positions, total_slots=TOTAL_SLOTS)
        await manager.record(2, "INFY")
        drift = await manager.reconcile()
        assert drift.stale_records == (2,)

    async def test_reconciliation_sees_a_position_with_no_record(
        self, session: AsyncSession, r: aioredis.Redis, positions: PositionRepository
    ) -> None:
        """The dangerous direction — occupancy understated."""
        await positions.open_position(_position("INFY", 1))
        await session.flush()
        manager = SlotManager(r, positions, total_slots=TOTAL_SLOTS)
        drift = await manager.reconcile()
        assert drift.missing_records == (1,)

    async def test_a_consistent_pair_of_stores_reconciles_clean(
        self, session: AsyncSession, r: aioredis.Redis, positions: PositionRepository
    ) -> None:
        """The control. Every assertion above is satisfied by a reconcile() that
        always reports drift."""
        await positions.open_position(_position("INFY", 0))
        await session.flush()
        manager = SlotManager(r, positions, total_slots=TOTAL_SLOTS)
        await manager.record(0, "INFY")
        drift = await manager.reconcile()
        assert drift.is_clean, drift.describe()
