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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from algotrader.common.db import engine as db_engine
from algotrader.common.db.repositories import InstrumentRepository, PositionRepository
from algotrader.common.redis import keys, locks
from algotrader.execution.slots import SlotManager, is_slot_taken

pytestmark = [pytest.mark.integration]

TOTAL_SLOTS = 5

#: One name per concurrent claimer. ``uq_open_symbol`` is a partial unique
#: index too, so twelve claimers all bidding for "INFY" would collide on the
#: SYMBOL and never reach the slot question this file is about.
SYMBOLS = [
    "INFY",
    "TCS",
    "WIPRO",
    "HDFCBANK",
    "SBIN",
    "ITC",
    "RELIANCE",
    "AXISBANK",
    "LT",
    "MARUTI",
    "TITAN",
    "NESTLEIND",
]


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
            }
            for index, symbol in enumerate(SYMBOLS)
        ]
    )
    await session.flush()
    await repo.refresh_cache()
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


@pytest.fixture
async def clean_positions(session: AsyncSession) -> AsyncIterator[None]:
    """Empty the positions table around a test that COMMITS.

    The ordinary ``session`` fixture rolls back, which isolates every other
    test in this file for free. The concurrency tests cannot use that: their
    claimers each need their own session and must commit, or no other claimer
    would see the row — and seeing it is the entire point. So the rows are
    real and have to be cleaned up explicitly.
    """
    await session.execute(text("DELETE FROM positions"))
    await session.commit()
    yield
    await session.execute(text("DELETE FROM positions"))
    await session.commit()


class TestConcurrentSignalsCannotOverAllocate:
    """🔴 The acceptance criterion.

    **Each claimer opens a real position and commits it.** The first draft only
    held the lock across a sleep, and slot 0 came back three times — correctly,
    because ``claiming`` releases on exit and nothing had claimed the slot in
    the database. That draft was asserting a property the design never had. The
    lock guards the *insert*; it is the committed row that holds the slot
    afterwards, so a test that never inserts is testing nothing about
    over-allocation.

    Each claimer also gets its own session: an ``AsyncSession`` wraps one
    connection and is not safe to share across coroutines, and two signals are
    two units of work in the real system anyway.
    """

    @staticmethod
    async def _open_concurrently(
        engine: object, client: aioredis.Redis, count: int
    ) -> tuple[list[tuple[str, int]], list[str]]:
        factory = db_engine.create_session_factory(engine)  # type: ignore[arg-type]
        opened: list[tuple[str, int]] = []
        refused: list[str] = []

        async def claim_and_open(symbol: str) -> None:
            async with factory() as own_session:
                instruments = InstrumentRepository(own_session)
                await instruments.refresh_cache()
                repo = PositionRepository(own_session, instruments)
                manager = SlotManager(client, repo, total_slots=TOTAL_SLOTS)
                async with manager.claiming(symbol) as index:
                    if index is None:
                        refused.append(symbol)
                        return
                    # A real await point inside the block, so the coroutines
                    # actually interleave rather than running to completion one
                    # at a time — without it the race never happens.
                    await asyncio.sleep(0.01)
                    try:
                        await repo.open_position(_position(symbol, index))
                        await own_session.commit()
                    except Exception as exc:
                        # §8.4: a unique violation here is a NORMAL outcome
                        # under concurrency, not an error to surface.
                        if not is_slot_taken(exc):
                            raise
                        await own_session.rollback()
                        refused.append(symbol)
                        return
                    opened.append((symbol, index))

        await asyncio.gather(*(claim_and_open(s) for s in SYMBOLS[:count]))
        return opened, refused

    async def test_two_claimers_on_an_empty_book_open_different_slots(
        self,
        engine: object,
        r: aioredis.Redis,
        instruments: InstrumentRepository,
        clean_positions: None,
    ) -> None:
        """The narrow race: both read an empty book, both look at slot 0."""
        opened, _ = await self._open_concurrently(engine, r, 2)
        slots = [index for _, index in opened]
        assert len(slots) == 2, f"a claimer was refused on an empty book: {opened}"
        assert len(set(slots)) == 2, f"one slot was opened twice: {opened}"

    async def test_twelve_claimers_never_exceed_five_positions(
        self,
        engine: object,
        r: aioredis.Redis,
        instruments: InstrumentRepository,
        clean_positions: None,
        session: AsyncSession,
    ) -> None:
        """The criterion itself: more signals than slots, and the book must not
        grow past its slot count."""
        opened, refused = await self._open_concurrently(engine, r, 12)
        slots = [index for _, index in opened]
        assert len(slots) == len(set(slots)), f"a slot was opened twice: {opened}"
        assert len(opened) == TOTAL_SLOTS, f"opened {len(opened)} of {TOTAL_SLOTS}"
        assert set(slots) == set(range(TOTAL_SLOTS))
        assert len(refused) == 12 - TOTAL_SLOTS

        # The database is the arbiter, so ask it rather than trusting the tally.
        positions = PositionRepository(session, InstrumentRepository(session))
        assert await positions.occupied_slots() == set(range(TOTAL_SLOTS))

    async def test_no_claimer_crashes_on_a_lost_race(
        self,
        engine: object,
        r: aioredis.Redis,
        instruments: InstrumentRepository,
        clean_positions: None,
    ) -> None:
        """Losing is ordinary. §8.4 calls the unique violation a normal outcome,
        and `_open_concurrently` re-raises anything `is_slot_taken` does not
        recognise — so reaching this assertion at all is the claim."""
        opened, refused = await self._open_concurrently(engine, r, 12)
        assert len(opened) + len(refused) == 12, "a claimer vanished"

    async def test_the_database_refuses_what_the_lock_somehow_lets_through(
        self, session: AsyncSession, positions: PositionRepository
    ) -> None:
        """Layer 2, with layer 1 deliberately bypassed.

        The lock is the fast path and can be lost — to an expiry, a partition,
        or a future refactor. This asserts the guarantee underneath still holds
        when it is, which is why §8.4 has three layers rather than one.
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
        for index, symbol in enumerate(SYMBOLS[:TOTAL_SLOTS]):
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
