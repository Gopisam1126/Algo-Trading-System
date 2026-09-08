"""Capital slot allocation and reconciliation (E14-S08).

A slot is a unit of deployable capital. ``position_slots`` of them at
``capital_per_slot_pct`` each is how the book is bounded, and handing the same
slot to two signals is how a 5-slot book becomes six positions carrying 120% of
capital — with every per-trade control still reporting success.

## The design record

**The decision.** ``SlotManager`` composes three layers that already existed
separately, in ``EPIC01_TECHNICAL_SPEC.md §8.4``'s order of cost:

1. the **database** is asked which slots hold an open position;
2. a short-lived **Redis lock** is taken on a free index, held only across the
   caller's insert;
3. ``uq_open_slot`` — a partial unique index on ``status = 'OPEN'`` — is the
   guarantee, and the caller must treat its violation as *"slot taken"* rather
   than as an error.

Reconciliation is the third layer of §8.4 and lives here too, because the drift
it looks for is between exactly the two stores this class writes.

**The alternative rejected.** Holding the Redis lock for the life of the
position, which is the reading the name ``lock:slot:{index}`` invites. It would
have been simpler to write and wrong: positions live for hours, ``MAX_TTL_MS``
is 300 seconds, so such a lock must either expire mid-position — making it
meaningless as a claim while still looking like one — or be renewed by a
heartbeat that goes quiet exactly when the process dies, which is the moment
the claim matters. **The database row is what holds a slot. The lock only
guards the window between choosing an index and inserting the row.**

**The failure it prevents.** Two signals firing on the same cycle both see slot
3 free. Without the lock, both proceed to insert and one crashes the execution
service with an unhandled unique violation at the moment a position is being
opened. Without the index, both succeed.

**What would make this decision wrong.** If allocation ever moved off the
single-threaded execution service (``LOW_LEVEL_ARCHITECTURE.md §9.1``), the
read-then-lock sequence here would need to become a single atomic operation.
And if slots stopped being a small fixed integer range, scanning them one at a
time would stop being reasonable.

## What is deliberately not here

**The priority queue.** ``E14-S08``'s task 2 asks for one, ordering candidates
by score and then expectancy when signals exceed free slots. Nothing in this
system computes an expectancy: ``paper_min_expectancy_r`` is a configured
*threshold* with no producer, and the ``signals`` package is empty. Ordering
candidates by a number that does not exist would mean inventing one, and a
made-up ranking over real money is worse than the honest first-come behaviour
here. Raised as its own story, blocked on E12 and E13.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

import redis.asyncio as aioredis

from algotrader.common.config import MAX_POSITION_SLOTS
from algotrader.common.redis import keys, locks

log = logging.getLogger(__name__)

#: How long the allocation lock lives. The module docstring explains why this is
#: short: it spans one insert, not one position. Generous against a slow
#: database, still far under ``locks.MAX_TTL_MS``.
DEFAULT_CLAIM_TTL_MS = 60_000

#: How many drifting slots an error message will name. Bounding the OUTPUT, not
#: just the count — QA-SEC-29 produced an 80,054-character detail from a line
#: that capped the count and nothing else.
MAX_SLOTS_NAMED = 8


class SlotsProvider(Protocol):
    """The one thing this needs from persistence.

    A Protocol rather than an import of ``PositionRepository`` so that the
    allocator can be tested without a database, and so the dependency is stated
    as a capability rather than as a class.
    """

    async def occupied_slots(self) -> set[int]:
        """Slot indices currently holding an OPEN position."""
        ...


@dataclass(frozen=True)
class SlotDrift:
    """What reconciliation found between Redis and the database.

    Three categories, because they have three different causes and three
    different costs. Collapsing them into one "drift" count would report the
    dangerous case and the self-healing one identically.
    """

    #: A lock is held but no position is open. Usually an allocation in flight,
    #: which is normal and transient; if it persists past the TTL it is a leaked
    #: lock. Self-healing either way, because every lock carries a TTL.
    leaked_locks: tuple[int, ...] = ()

    #: ``state:slots`` names a symbol in a slot the database says is free. The
    #: hash carries **no TTL**, so unlike a lock this never heals on its own: the
    #: slot reads as occupied for the rest of the session and the book quietly
    #: runs one position short.
    stale_records: tuple[int, ...] = ()

    #: An open position whose slot is missing from ``state:slots``. The
    #: dangerous direction — anything reading the hash to count occupancy would
    #: understate it, and understating occupancy is how a slot gets handed out
    #: twice. Allocation here reads the database precisely so that this cannot
    #: cause over-allocation, but it still means the readable view is lying.
    missing_records: tuple[int, ...] = ()

    @property
    def is_clean(self) -> bool:
        return not (self.leaked_locks or self.stale_records or self.missing_records)

    def describe(self) -> str:
        """One bounded line for a log or an alert."""
        if self.is_clean:
            return "slot state is consistent between Redis and the database"
        parts = []
        for label, values in (
            ("locks held with no open position", self.leaked_locks),
            ("recorded but not open", self.stale_records),
            ("open but not recorded", self.missing_records),
        ):
            if values:
                shown = ", ".join(str(v) for v in sorted(values)[:MAX_SLOTS_NAMED])
                if len(values) > MAX_SLOTS_NAMED:
                    shown += f", and {len(values) - MAX_SLOTS_NAMED} more"
                parts.append(f"{label}: {shown}")
        return "; ".join(parts)


class SlotManager:
    """Allocates capital slots, records them, and reconciles the two stores."""

    def __init__(
        self,
        client: aioredis.Redis,
        positions: SlotsProvider,
        *,
        total_slots: int,
        ttl_ms: int = DEFAULT_CLAIM_TTL_MS,
    ) -> None:
        if not isinstance(total_slots, int) or isinstance(total_slots, bool):
            raise ValueError(f"total_slots must be an int, got {total_slots!r}")
        if total_slots < 1:
            raise ValueError(
                f"total_slots is {total_slots}. A book with no slots cannot trade, and "
                f"expressing that as a slot count rather than as a halt would look like "
                f"a configuration value rather than the stop it actually is."
            )
        if total_slots > MAX_POSITION_SLOTS:
            raise ValueError(
                f"total_slots {total_slots} exceeds the hard bound of "
                f"{MAX_POSITION_SLOTS}. That bound is code, not configuration."
            )
        self._client = client
        self._positions = positions
        self._total = total_slots
        self._ttl_ms = ttl_ms

    @property
    def total_slots(self) -> int:
        return self._total

    @asynccontextmanager
    async def claiming(self, symbol: str) -> AsyncIterator[int | None]:
        """Hold a free slot index for the duration of the block, or ``None``.

        Yields rather than raising when the book is full, because a full book is
        an ordinary outcome and the caller has a reason code for it —
        ``NO_SLOT_AVAILABLE`` — not an exception to handle. That mirrors
        :func:`algotrader.common.redis.locks.lock`, which yields a bool for the
        same reason.

        Usage, and the ordering matters::

            async with manager.claiming(symbol) as index:
                if index is None:
                    return reject(RejectReason.NO_SLOT_AVAILABLE)
                position_id = await positions.open_position({..., "slot_index": index})
                await manager.record(index, symbol)

        The insert happens **inside** the block. Outside it the lock is gone and
        two signals can interleave between the check and the write, which is the
        exact race §8.4 exists to describe.
        """
        occupied = await self._positions.occupied_slots()
        holder = uuid.uuid4().hex
        claimed: int | None = None
        for candidate in range(self._total):
            if candidate in occupied:
                continue
            if await locks.acquire_lock(
                self._client, keys.slot_lock(candidate), holder, self._ttl_ms
            ):
                claimed = candidate
                break
        try:
            yield claimed
        finally:
            if claimed is not None:
                try:
                    await locks.release_lock(self._client, keys.slot_lock(claimed), holder)
                except Exception:
                    # Best-effort, and never masking an error from the block —
                    # the same reasoning as locks.lock(). A lock that outlives
                    # this is bounded by its TTL and shows up in reconcile().
                    log.warning(
                        "could not release the claim on slot %d; it expires in %d ms",
                        claimed,
                        self._ttl_ms,
                        exc_info=True,
                    )

    async def record(self, index: int, symbol: str) -> None:
        """Note in ``state:slots`` that ``index`` holds ``symbol``.

        Called after a successful insert, never instead of one. This hash is the
        readable view — the database is the record — which is why nothing in
        allocation consults it.
        """
        await self._client.hset(keys.slots(), str(self._validated(index)), symbol)

    async def release(self, index: int) -> None:
        """Clear a slot's record when its position closes.

        Task 3's "recycling" is this and nothing more: the slot becomes
        allocatable the moment the position's row stops being OPEN, because that
        is what ``occupied_slots`` reads. Forgetting this call does not leak the
        slot — it leaks the *readable view*, which reconcile() reports as a
        stale record.
        """
        await self._client.hdel(keys.slots(), str(self._validated(index)))

    async def recorded(self) -> dict[int, str]:
        """The Redis view: slot index to symbol."""
        raw: dict[Any, Any] = await self._client.hgetall(keys.slots())
        out: dict[int, str] = {}
        for key, value in raw.items():
            index = key.decode() if isinstance(key, bytes) else key
            symbol = value.decode() if isinstance(value, bytes) else value
            try:
                out[int(index)] = symbol
            except (TypeError, ValueError):
                # A non-integer field in the slot hash is corruption, not a slot.
                # Skipped rather than raised so one bad field cannot stop
                # reconciliation from reporting everything else.
                log.warning("ignoring non-integer field %r in %s", index, keys.slots())
        return out

    async def reconcile(self) -> SlotDrift:
        """Compare the two stores. Task 4, and §8.4's third layer.

        Reports rather than repairs. An automatic repair would have to decide
        which store is wrong, and the honest answer differs by category: a stale
        record should be cleared, but a *missing* record means an open position
        the readable view does not know about, and writing the view from the
        database would paper over however that happened.
        """
        open_slots = await self._positions.occupied_slots()
        recorded = await self.recorded()

        leaked: list[int] = []
        for index in range(self._total):
            if index in open_slots:
                continue
            if await self._client.exists(keys.slot_lock(index)):
                leaked.append(index)

        drift = SlotDrift(
            leaked_locks=tuple(sorted(leaked)),
            stale_records=tuple(sorted(set(recorded) - open_slots)),
            missing_records=tuple(sorted(open_slots - set(recorded))),
        )
        if not drift.is_clean:
            log.warning("slot reconciliation found drift: %s", drift.describe())
        return drift

    def _validated(self, index: int) -> int:
        if not isinstance(index, int) or isinstance(index, bool):
            raise ValueError(f"slot index must be an int, got {index!r}")
        if not 0 <= index < self._total:
            raise ValueError(
                f"slot index {index} is outside 0..{self._total - 1}. Recording a "
                f"slot the book does not have would put a position in the readable "
                f"view that no allocation could ever have produced."
            )
        return index


def is_slot_taken(exc: BaseException, *, constraints: Sequence[str] = ()) -> bool:
    """Is this exception the database saying *"that slot is already open"*?

    §8.4 requires callers to treat a unique violation as a normal outcome, and
    that instruction is only followable if the exception can be recognised.
    Recognising it is less obvious than it looks: SQLAlchemy wraps every DBAPI
    error, so the ``psycopg.errors.UniqueViolation`` the spec names arrives as
    ``sqlalchemy.exc.IntegrityError`` with the original hanging off ``.orig``.
    Catching the psycopg type alone would miss every real occurrence and turn
    the expected outcome into an unhandled crash — during position opening,
    which is the worst moment for one.

    **This is checked against the exception in hand rather than asserted from
    the type hierarchy**, because the existing test that was supposed to pin the
    type down — ``test_slot_collision_raises_and_is_catchable`` — asserted
    ``pytest.raises((UniqueViolation, Exception))``, and ``Exception`` in that
    tuple matches anything at all. It could not fail, so it never established
    what it was named for.

    ``constraints`` narrows the match to named indexes when supplied, so that a
    violation of some unrelated constraint is not silently read as a busy slot.
    """
    seen: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in seen:
        seen.append(current)
        name = type(current).__name__
        if name in {"UniqueViolation", "IntegrityError"}:
            if not constraints:
                return True
            text = str(current)
            if any(c in text for c in constraints):
                return True
        current = getattr(current, "orig", None) or current.__cause__
    return False
