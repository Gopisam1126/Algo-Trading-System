"""Capital slot allocation — E14-S08.

The acceptance criterion ("concurrent signals cannot over-allocate slots") is
about a race between two processes and a database, so the proof of it lives in
``tests/integration/test_slot_allocation.py`` against a real Redis and a real
Postgres. What is testable here is everything the race is built out of: which
index gets picked, what happens when the book is full, whether the lock is
released, and whether reconciliation names the right drift.

**The Redis double is deliberately thin and exercises the REAL lock code.**
``SlotManager`` never calls Redis directly for locking — it calls
``locks.acquire_lock`` and ``locks.release_lock``, which are the tested,
Lua-backed compare-and-delete implementations. Faking the *client* rather than
the *lock module* keeps that logic under test here; faking the lock module
would have tested a stub and proved nothing about contention.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from algotrader.common.redis import keys, locks
from algotrader.execution.slots import (
    DEFAULT_CLAIM_TTL_MS,
    MAX_SLOTS_NAMED,
    SlotDrift,
    SlotManager,
    is_slot_taken,
)

#: Applied per-class rather than module-wide: two classes below are
#: synchronous, and an asyncio mark on a sync test is a warning on every
#: run that trains the reader to ignore warnings.
_ASYNC = pytest.mark.asyncio


class FakeRedis:
    """The six operations ``SlotManager`` and ``locks`` actually use.

    ``eval`` recognises the two Lua scripts from ``locks`` by their content and
    implements exactly what they do. That is a real risk — a double that got
    compare-and-delete wrong would let every test here pass while the system
    deleted another holder's lock — so the compare is implemented, not assumed,
    and the integration test runs the genuine scripts against a genuine Redis.
    """

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        #: Set to raise on the next call, to test the fail-closed paths.
        self.fail_on: str | None = None

    def _maybe_fail(self, op: str) -> None:
        if self.fail_on == op:
            raise ConnectionError(f"redis is unreachable ({op})")

    async def set(
        self, key: str, value: str, *, nx: bool = False, px: int | None = None
    ) -> bool | None:
        self._maybe_fail("set")
        if nx and key in self.strings:
            return None
        self.strings[key] = value
        return True

    async def eval(self, script: str, numkeys: int, *args: Any) -> int:
        self._maybe_fail("eval")
        key, holder = str(args[0]), str(args[1])
        if "DEL" in script:  # the release script
            if self.strings.get(key) == holder:
                del self.strings[key]
                return 1
            return 0
        if "PEXPIRE" in script:  # the extend script
            return 1 if self.strings.get(key) == holder else 0
        raise AssertionError(f"unrecognised script: {script[:40]}")

    async def exists(self, key: str) -> int:
        self._maybe_fail("exists")
        return 1 if key in self.strings else 0

    async def hset(self, key: str, field: str, value: str) -> int:
        self._maybe_fail("hset")
        self.hashes.setdefault(key, {})[field] = value
        return 1

    async def hdel(self, key: str, field: str) -> int:
        self._maybe_fail("hdel")
        return 1 if self.hashes.setdefault(key, {}).pop(field, None) is not None else 0

    async def hgetall(self, key: str) -> dict[str, str]:
        self._maybe_fail("hgetall")
        return dict(self.hashes.get(key, {}))


class FakePositions:
    """The single capability ``SlotsProvider`` declares."""

    def __init__(self, occupied: set[int] | None = None) -> None:
        self._occupied = set(occupied or ())
        self.fail = False

    async def occupied_slots(self) -> set[int]:
        if self.fail:
            raise ConnectionError("the database is unreachable")
        return set(self._occupied)

    def open(self, index: int) -> None:
        self._occupied.add(index)

    def close(self, index: int) -> None:
        self._occupied.discard(index)


def _manager(
    client: FakeRedis | None = None,
    positions: FakePositions | None = None,
    *,
    total_slots: int = 5,
) -> tuple[SlotManager, FakeRedis, FakePositions]:
    redis = client or FakeRedis()
    repo = positions or FakePositions()
    return (
        SlotManager(redis, repo, total_slots=total_slots),  # type: ignore[arg-type]
        redis,
        repo,
    )


@_ASYNC
class TestAllocationPicksAFreeSlot:
    async def test_an_empty_book_yields_the_first_slot(self) -> None:
        manager, _, _ = _manager()
        async with manager.claiming("INFY") as index:
            assert index == 0

    async def test_slots_open_in_the_database_are_skipped(self) -> None:
        """The database is truth. A slot holding an open position must never be
        offered, whatever Redis happens to say about it."""
        manager, _, _ = _manager(positions=FakePositions({0, 1, 3}))
        async with manager.claiming("INFY") as index:
            assert index == 2

    async def test_a_full_book_yields_none_rather_than_raising(self) -> None:
        """A full book is an ordinary outcome with a reason code
        (NO_SLOT_AVAILABLE), not an exception. Mirrors locks.lock(), which
        yields a bool for the same reason."""
        manager, _, _ = _manager(positions=FakePositions({0, 1, 2, 3, 4}))
        async with manager.claiming("INFY") as index:
            assert index is None

    async def test_a_slot_locked_by_someone_else_is_skipped(self) -> None:
        """The race, in miniature. The database says slot 0 is free because the
        other signal has not inserted yet — the LOCK is what carries that
        information, and it is the whole reason the lock exists."""
        manager, redis, _ = _manager()
        await locks.acquire_lock(redis, keys.slot_lock(0), "other-signal", 60_000)  # type: ignore[arg-type]
        async with manager.claiming("INFY") as index:
            assert index == 1

    async def test_the_lock_is_held_for_the_whole_block(self) -> None:
        """The insert happens inside the block. If the lock were released on
        yield, two signals could interleave between the check and the write —
        which is the exact race EPIC01 8.4 describes."""
        manager, redis, _ = _manager()
        async with manager.claiming("INFY") as index:
            assert index is not None
            assert await redis.exists(keys.slot_lock(index)) == 1

    async def test_the_lock_is_released_on_exit(self) -> None:
        manager, redis, _ = _manager()
        async with manager.claiming("INFY") as index:
            assert index is not None
        assert await redis.exists(keys.slot_lock(0)) == 0

    async def test_the_lock_is_released_even_when_the_block_raises(self) -> None:
        """A failed insert must not cost the slot for the rest of the session."""
        manager, redis, _ = _manager()
        with pytest.raises(RuntimeError):
            async with manager.claiming("INFY"):
                raise RuntimeError("the insert failed")
        assert await redis.exists(keys.slot_lock(0)) == 0

    async def test_the_original_error_survives_a_failing_release(self) -> None:
        """If the block raised, THAT is what the caller needs to see — not a
        secondary failure from releasing a lock. Same contract as locks.lock()."""
        manager, redis, _ = _manager()
        with pytest.raises(RuntimeError, match="the insert failed"):
            async with manager.claiming("INFY"):
                redis.fail_on = "eval"
                raise RuntimeError("the insert failed")

    async def test_two_sequential_claims_take_different_slots(self) -> None:
        """Nested, so the first lock is still held when the second is taken —
        the single-process shape of the concurrency the AC is about."""
        manager, _, _ = _manager()
        async with manager.claiming("INFY") as first, manager.claiming("TCS") as second:
            assert (first, second) == (0, 1)

    async def test_a_released_slot_is_offered_again(self) -> None:
        """Task 3. Recycling is not a separate mechanism: the slot becomes
        allocatable the moment its row stops being OPEN."""
        positions = FakePositions({0})
        manager, _, _ = _manager(positions=positions)
        async with manager.claiming("INFY") as index:
            assert index == 1
        positions.close(0)
        async with manager.claiming("TCS") as index:
            assert index == 0


@_ASYNC
class TestAllocationFailsClosed:
    """Every path where an input is unavailable must refuse, not guess. A slot
    handed out on a guess is a position the book did not budget for."""

    async def test_an_unreachable_database_refuses_rather_than_assuming_empty(
        self,
    ) -> None:
        """The dangerous default: treating an unreadable book as an empty one
        would offer slot 0 while five positions are open."""
        positions = FakePositions({0, 1})
        positions.fail = True
        manager, _, _ = _manager(positions=positions)
        with pytest.raises(ConnectionError):
            async with manager.claiming("INFY"):
                pass

    async def test_an_unreachable_redis_refuses(self) -> None:
        manager, redis, _ = _manager()
        redis.fail_on = "set"
        with pytest.raises(ConnectionError):
            async with manager.claiming("INFY"):
                pass


@_ASYNC
class TestTheSlotCountIsBounded:
    async def test_zero_slots_is_refused(self) -> None:
        with pytest.raises(ValueError, match="cannot trade"):
            _manager(total_slots=0)

    async def test_a_negative_slot_count_is_refused(self) -> None:
        with pytest.raises(ValueError):
            _manager(total_slots=-1)

    async def test_more_slots_than_the_hard_bound_is_refused(self) -> None:
        """MAX_POSITION_SLOTS is code, not configuration — invariant 2."""
        with pytest.raises(ValueError, match="hard bound"):
            _manager(total_slots=21)

    async def test_a_bool_is_not_a_slot_count(self) -> None:
        """`True` is an int in Python and would silently mean one slot."""
        with pytest.raises(ValueError, match="must be an int"):
            _manager(total_slots=True)  # type: ignore[arg-type]

    async def test_the_bound_itself_is_allowed(self) -> None:
        """The control. A validator that rejects everything passes every
        hostile-input test above."""
        manager, _, _ = _manager(total_slots=20)
        assert manager.total_slots == 20


@_ASYNC
class TestTheReadableView:
    async def test_recording_and_reading_round_trip(self) -> None:
        manager, _, _ = _manager()
        await manager.record(2, "INFY")
        assert await manager.recorded() == {2: "INFY"}

    async def test_releasing_clears_the_record(self) -> None:
        manager, _, _ = _manager()
        await manager.record(2, "INFY")
        await manager.release(2)
        assert await manager.recorded() == {}

    async def test_a_slot_outside_the_book_cannot_be_recorded(self) -> None:
        """A record no allocation could have produced would make the view lie in
        the direction that reads as MORE capacity used, not less."""
        manager, _, _ = _manager(total_slots=3)
        with pytest.raises(ValueError, match="outside"):
            await manager.record(3, "INFY")

    async def test_a_corrupt_field_does_not_stop_the_view(self) -> None:
        """One unparseable field must not hide every other slot — reconciliation
        is most needed exactly when something has gone wrong."""
        manager, redis, _ = _manager()
        await manager.record(1, "INFY")
        redis.hashes[keys.slots()]["not-an-index"] = "GARBAGE"
        assert await manager.recorded() == {1: "INFY"}


@_ASYNC
class TestReconciliationNamesTheRightDrift:
    """Three categories because they have three causes and three costs.
    Collapsing them would report the self-healing case and the dangerous one
    identically."""

    async def test_a_consistent_pair_of_stores_is_clean(self) -> None:
        """The control."""
        manager, _, _ = _manager(positions=FakePositions({0, 1}))
        await manager.record(0, "INFY")
        await manager.record(1, "TCS")
        drift = await manager.reconcile()
        assert drift.is_clean
        assert "consistent" in drift.describe()

    async def test_a_lock_with_no_open_position_is_a_leaked_lock(self) -> None:
        manager, redis, _ = _manager()
        await locks.acquire_lock(redis, keys.slot_lock(3), "gone", 60_000)  # type: ignore[arg-type]
        drift = await manager.reconcile()
        assert drift.leaked_locks == (3,)
        assert not drift.is_clean

    async def test_a_record_with_no_open_position_is_stale(self) -> None:
        """The one that never heals on its own. Locks carry a TTL; this hash
        does not, so the slot reads as occupied for the rest of the session."""
        manager, _, _ = _manager()
        await manager.record(2, "INFY")
        drift = await manager.reconcile()
        assert drift.stale_records == (2,)

    async def test_an_open_position_with_no_record_is_missing(self) -> None:
        """The dangerous direction: anything counting occupancy from the hash
        would understate it, and understating occupancy is how a slot gets
        handed out twice."""
        manager, _, _ = _manager(positions=FakePositions({4}))
        drift = await manager.reconcile()
        assert drift.missing_records == (4,)

    async def test_the_three_categories_are_reported_separately(self) -> None:
        manager, redis, _ = _manager(positions=FakePositions({1}))
        await manager.record(0, "STALE")
        await locks.acquire_lock(redis, keys.slot_lock(3), "gone", 60_000)  # type: ignore[arg-type]
        drift = await manager.reconcile()
        assert drift.stale_records == (0,)
        assert drift.missing_records == (1,)
        assert drift.leaked_locks == (3,)

    async def test_a_lock_on_an_open_slot_is_not_drift(self) -> None:
        """An allocation in flight holds a lock on a slot that is about to be
        open. Reporting that as a leak would make reconciliation cry wolf on
        every normal entry."""
        manager, redis, _ = _manager(positions=FakePositions({2}))
        await locks.acquire_lock(redis, keys.slot_lock(2), "inserting", 60_000)  # type: ignore[arg-type]
        await manager.record(2, "INFY")
        drift = await manager.reconcile()
        assert drift.leaked_locks == ()
        assert drift.is_clean

    async def test_the_description_names_at_most_the_cap(self) -> None:
        """Assert the COUNT of named values, not the length of the string.

        The first version of this measured `len(describe()) < 300`, and a
        mutation that removed the cap survived it: twenty slot indices are
        short enough to fit under 300 characters, so the assertion could not
        tell a bounded description from an unbounded one. Slot indices are
        small today; the property this protects is that the cap is applied,
        which stays true if the values ever get longer.
        """
        manager, _, _ = _manager(positions=FakePositions(set(range(20))), total_slots=20)
        drift = await manager.reconcile()
        assert len(drift.missing_records) == 20

        description = drift.describe()
        named = description.split("open but not recorded: ")[1].split(", and ")[0]
        assert len([v for v in named.split(", ") if v]) == MAX_SLOTS_NAMED
        assert "and 12 more" in description

    async def test_a_short_list_is_named_in_full(self) -> None:
        """The control on the cap: under the limit, nothing is elided and no
        "and N more" is claimed."""
        manager, _, _ = _manager(positions=FakePositions({1, 2}))
        description = (await manager.reconcile()).describe()
        assert "1, 2" in description
        assert "more" not in description


class TestRecognisingATakenSlot:
    """`is_slot_taken` exists because EPIC01 8.4 tells callers to treat a unique
    violation as a normal outcome, and that is only followable if the exception
    can be recognised."""

    # N818 wants an `Error` suffix. These two must carry the EXACT names
    # `is_slot_taken` matches on — renaming them to satisfy the linter would
    # leave the tests passing while testing nothing, since the match is by
    # `type(exc).__name__`. The suppression is backed by
    # `test_a_wrapped_one_is_recognised_through_orig`, which fails if the
    # names stop lining up with psycopg's and SQLAlchemy's.
    class UniqueViolation(Exception):  # noqa: N818
        """Stands in for psycopg.errors.UniqueViolation, matched by name."""

    class IntegrityError(Exception):
        """Stands in for sqlalchemy.exc.IntegrityError, which wraps it."""

        def __init__(self, message: str, orig: Exception | None = None) -> None:
            super().__init__(message)
            self.orig = orig

    def test_a_bare_unique_violation_is_recognised(self) -> None:
        assert is_slot_taken(self.UniqueViolation("duplicate key"))

    def test_a_wrapped_one_is_recognised_through_orig(self) -> None:
        """The case that matters. SQLAlchemy wraps every DBAPI error, so
        catching the psycopg type alone would miss every real occurrence and
        turn the expected outcome into a crash during position opening."""
        inner = self.UniqueViolation('duplicate key value violates "uq_open_slot"')
        assert is_slot_taken(self.IntegrityError("wrapped", orig=inner))

    def test_it_is_recognised_through_orig_when_the_wrapper_is_unnamed(self) -> None:
        """The `.orig` walk, isolated.

        Added because a mutation that removed the `.orig` step survived: the
        test above wraps in a class called `IntegrityError`, which matches on
        the FIRST iteration by name, so `.orig` was never reached and the
        branch was untested. Here the wrapper is a `StatementError` — a real
        SQLAlchemy type that is not in the match set — so the only way to
        recognise the violation is to follow `.orig`.
        """

        class StatementError(Exception):
            def __init__(self, orig: Exception) -> None:
                super().__init__("statement failed")
                self.orig = orig

        assert is_slot_taken(StatementError(self.UniqueViolation("duplicate key")))

    def test_it_is_recognised_through_a_cause_chain(self) -> None:
        inner = self.UniqueViolation("duplicate key")
        outer = RuntimeError("while opening a position")
        outer.__cause__ = inner
        assert is_slot_taken(outer)

    def test_an_unrelated_error_is_not_a_taken_slot(self) -> None:
        """The control, and the one that matters most: reading a connection
        failure as "slot taken" would silently skip the slot and eventually
        report a full book while the database was simply down."""
        assert not is_slot_taken(ConnectionError("the database is unreachable"))
        assert not is_slot_taken(ValueError("bad input"))

    def test_naming_a_constraint_narrows_the_match(self) -> None:
        slot = self.UniqueViolation('violates unique constraint "uq_open_slot"')
        other = self.UniqueViolation('violates unique constraint "uq_strategy_hash"')
        assert is_slot_taken(slot, constraints=["uq_open_slot"])
        assert not is_slot_taken(other, constraints=["uq_open_slot"])

    def test_a_self_referential_cause_chain_terminates(self) -> None:
        """A malformed exception chain must not hang the caller. Cheap to
        assert, and an infinite loop here would stop the execution service."""
        exc = RuntimeError("loop")
        exc.__cause__ = exc
        assert not is_slot_taken(exc)


class TestTheDriftValueObject:
    def test_an_empty_drift_is_clean(self) -> None:
        assert SlotDrift().is_clean

    def test_the_default_ttl_is_within_the_lock_ceiling(self) -> None:
        """A claim TTL above locks.MAX_TTL_MS would raise on every single
        allocation — a defect that only shows up at runtime, on the first
        trade of the day."""
        assert 0 < DEFAULT_CLAIM_TTL_MS <= locks.MAX_TTL_MS


@_ASYNC
class TestAllocationUnderConcurrency:
    """The AC's shape, as far as one process can express it. The real proof is
    the integration test: two connections, one Redis, one Postgres, one index.
    """

    async def test_concurrent_claims_never_collide(self) -> None:
        """Ten coroutines, five slots. Every claim that succeeds must be
        distinct, and the surplus must come back None rather than duplicating."""
        manager, _, _ = _manager()
        handed: list[int | None] = []

        async def claim() -> None:
            async with manager.claiming("INFY") as index:
                handed.append(index)
                await asyncio.sleep(0)  # force interleaving inside the block

        await asyncio.gather(*(claim() for _ in range(10)))
        allocated = [i for i in handed if i is not None]
        assert len(allocated) == len(set(allocated)), f"a slot was handed out twice: {handed}"
        assert len(allocated) == 5
        assert handed.count(None) == 5


@_ASYNC
class TestNothingHereForgesALogLine:
    """Phase 7 on this diff. Log forgery has been found five times in this
    project, so a new module that writes untrusted strings into Redis and then
    logs about them gets checked rather than assumed.
    """

    RAW_NEWLINE = chr(10)

    async def test_a_hostile_symbol_never_reaches_the_drift_description(self) -> None:
        """`describe()` names slot INDICES, never symbols. That is what keeps
        the untrusted value away from the log — asserted, because "it happens
        not to be interpolated today" is exactly how the previous five
        occurrences were introduced."""
        manager, _, _ = _manager(positions=FakePositions({0}))
        await manager.record(0, "INFY" + chr(10) + "CRITICAL kill switch disarmed by operator")
        description = (await manager.reconcile()).describe()
        assert self.RAW_NEWLINE not in description
        assert "kill switch" not in description

    async def test_a_hostile_hash_field_is_logged_escaped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The one place a value from Redis IS interpolated into a log call.
        `%r` is doing the escaping; a switch to `%s` would forge a line."""
        import logging

        manager, redis, _ = _manager()
        redis.hashes[keys.slots()] = {"3" + chr(10) + "CRITICAL kill switch disarmed": "INFY"}
        with caplog.at_level(logging.WARNING, logger="algotrader.execution.slots"):
            await manager.recorded()
        assert caplog.records, "the corrupt field was not reported at all"
        for record in caplog.records:
            assert self.RAW_NEWLINE not in record.getMessage()

    async def test_an_ordinary_symbol_round_trips_intact(self) -> None:
        """The control. Containment must not have cost the readable view its
        actual content."""
        manager, _, _ = _manager()
        await manager.record(0, "HDFCBANK")
        assert (await manager.recorded())[0] == "HDFCBANK"
