"""E01-S03 — every claim the Redis layer makes, probed against a real Redis.

Per DEVELOPMENT_PROCEDURE.md §4.1: a claim gets a test that fails when the claim
is false. §4.2: every rejection test is paired with a control, so it cannot pass
because the feature is simply absent.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import redis.asyncio as aioredis
from pydantic import BaseModel

from algotrader.common.enums import Timeframe
from algotrader.common.redis import keys, locks, primitives, state

pytestmark = [pytest.mark.integration]


@pytest.fixture
async def r(redis_url: str) -> AsyncIterator[aioredis.Redis]:
    client = aioredis.from_url(redis_url, decode_responses=True)
    await client.flushall()
    try:
        yield client
    finally:
        await client.flushall()
        await client.aclose()


class Snapshot(BaseModel):
    symbol: str
    price: Decimal
    ready: bool


# ---------------------------------------------------------------------------


class TestKeyBuilders:
    def test_every_documented_key_has_a_builder(self) -> None:
        """§9 of the spec must be fully covered — a missing builder invites a literal."""
        missing = [n for n in keys.ALL_BUILDERS if not callable(getattr(keys, n, None))]
        assert not missing, f"declared but not implemented: {missing}"

    def test_keys_are_namespaced_by_lifecycle(self) -> None:
        assert keys.indicator_state("INFY", Timeframe.M5) == "state:indicator:INFY:5m"
        assert keys.slot_lock(3) == "lock:slot:3"
        assert keys.health("ti-engine") == "control:health:ti-engine"
        assert keys.stream_bars(Timeframe.M15) == "stream:bars:15m"

    def test_timeframe_enum_and_string_agree(self) -> None:
        """Callers pass either; a divergence would split one logical key in two."""
        assert keys.indicator_state("X", Timeframe.M5) == keys.indicator_state("X", "5m")

    def test_dlq_key_derives_from_the_full_stream_key(self) -> None:
        assert keys.stream_dlq(keys.stream_signals()) == "stream:dlq:signals"

    def test_dates_in_keys_are_iso(self) -> None:
        assert keys.plan(dt.date(2026, 8, 6)) == "plan:2026-08-06"


class TestLockTtlIsMandatory:
    """The story's real content: a lock without a TTL deadlocks the session."""

    def test_acquire_without_ttl_is_a_type_error(self) -> None:
        """Not a runtime check — the signature itself must refuse it."""
        with pytest.raises(TypeError):
            locks.acquire_lock(None, "lock:x", "holder")  # type: ignore[call-arg]

    async def test_zero_ttl_is_rejected(self, r: aioredis.Redis) -> None:
        with pytest.raises(locks.LockError):
            await locks.acquire_lock(r, "lock:x", "h", 0)

    async def test_negative_ttl_is_rejected(self, r: aioredis.Redis) -> None:
        with pytest.raises(locks.LockError):
            await locks.acquire_lock(r, "lock:x", "h", -1)

    async def test_absurd_ttl_is_rejected(self, r: aioredis.Redis) -> None:
        """Catches seconds-passed-as-milliseconds, the likely real mistake."""
        with pytest.raises(locks.LockError):
            await locks.acquire_lock(r, "lock:x", "h", locks.MAX_TTL_MS + 1)

    async def test_valid_ttl_is_accepted(self, r: aioredis.Redis) -> None:
        """The control."""
        assert await locks.acquire_lock(r, "lock:x", "h", 5_000) is True

    async def test_acquired_lock_actually_expires(self, r: aioredis.Redis) -> None:
        """The TTL must be real, not merely accepted as an argument."""
        await locks.acquire_lock(r, "lock:ttl", "h", 150)
        assert await r.exists("lock:ttl")
        await asyncio.sleep(0.35)
        assert not await r.exists("lock:ttl"), "the lock outlived its TTL"


class TestLockMutualExclusion:
    async def test_second_holder_is_refused(self, r: aioredis.Redis) -> None:
        assert await locks.acquire_lock(r, "lock:slot:1", "first", 10_000) is True
        assert await locks.acquire_lock(r, "lock:slot:1", "second", 10_000) is False

    async def test_release_frees_it(self, r: aioredis.Redis) -> None:
        await locks.acquire_lock(r, "lock:slot:1", "first", 10_000)
        assert await locks.release_lock(r, "lock:slot:1", "first") is True
        assert await locks.acquire_lock(r, "lock:slot:1", "second", 10_000) is True

    async def test_a_non_holder_cannot_release_someone_elses_lock(self, r: aioredis.Redis) -> None:
        """The race this prevents: expire, someone else acquires, you delete theirs."""
        await locks.acquire_lock(r, "lock:slot:1", "first", 10_000)
        assert await locks.release_lock(r, "lock:slot:1", "impostor") is False
        assert await r.get("lock:slot:1") == "first", "the impostor deleted the lock"

    async def test_extend_only_works_for_the_holder(self, r: aioredis.Redis) -> None:
        await locks.acquire_lock(r, "lock:e", "first", 1_000)
        assert await locks.extend_lock(r, "lock:e", "first", 20_000) is True
        assert await locks.extend_lock(r, "lock:e", "impostor", 20_000) is False

    async def test_context_manager_yields_contention_rather_than_raising(
        self, r: aioredis.Redis
    ) -> None:
        """Contention is normal here — two signals on one cycle — not exceptional."""
        async with locks.lock(r, "lock:cm", 10_000, holder="a") as first:
            assert first is True
            async with locks.lock(r, "lock:cm", 10_000, holder="b") as second:
                assert second is False

    async def test_context_manager_releases_on_exception(self, r: aioredis.Redis) -> None:
        with pytest.raises(ValueError, match="boom"):
            async with locks.lock(r, "lock:err", 10_000, holder="a") as got:
                assert got
                raise ValueError("boom")
        assert not await r.exists("lock:err"), "lock leaked when the body raised"


class TestTokenBucket:
    async def test_burst_is_capped_at_capacity(self, r: aioredis.Redis) -> None:
        key = keys.order_rate_limit()
        allowed = [
            (await primitives.take_token(r, key, capacity=3, refill_per_second=1))[0]
            for _ in range(5)
        ]
        assert allowed == [True, True, True, False, False]

    async def test_tokens_refill_over_time(self, r: aioredis.Redis) -> None:
        key = keys.order_rate_limit()
        for _ in range(2):
            await primitives.take_token(r, key, capacity=2, refill_per_second=20)
        ok, _ = await primitives.take_token(r, key, capacity=2, refill_per_second=20)
        assert ok is False
        await asyncio.sleep(0.25)
        ok, _ = await primitives.take_token(r, key, capacity=2, refill_per_second=20)
        assert ok is True, "bucket never refilled"

    async def test_concurrent_callers_cannot_exceed_capacity(self, r: aioredis.Redis) -> None:
        """The reason this is Lua.

        A Python-side get/compute/set lets two callers see the same count and
        both proceed — which for the order limiter means breaching the broker
        cap and, above 10/sec, the SEBI threshold.
        """
        key = keys.order_rate_limit()
        results = await asyncio.gather(
            *[primitives.take_token(r, key, capacity=5, refill_per_second=0.001) for _ in range(25)]
        )
        assert sum(1 for ok, _ in results if ok) == 5

    async def test_impossible_request_is_rejected_not_looped(self, r: aioredis.Redis) -> None:
        """Asking for more than capacity can never succeed — fail fast, not spin."""
        with pytest.raises(primitives.RateLimitError):
            await primitives.take_token(r, "ratelimit:x", capacity=3, refill_per_second=1, count=4)

    async def test_zero_refill_is_rejected(self, r: aioredis.Redis) -> None:
        with pytest.raises(primitives.RateLimitError):
            await primitives.take_token(r, "ratelimit:x", capacity=3, refill_per_second=0)


class TestTimerQueue:
    async def test_only_due_items_pop(self, r: aioredis.Redis) -> None:
        key = keys.squareoff_timer()
        now = int(time.time() * 1000)
        await primitives.schedule(r, key, "pos:1", now - 1_000)
        await primitives.schedule(r, key, "pos:2", now + 60_000)
        assert await primitives.pop_due(r, key, now_ms=now) == ["pos:1"]
        assert await primitives.scheduled_count(r, key) == 1

    async def test_pop_is_atomic_across_workers(self, r: aioredis.Redis) -> None:
        """Two workers must not both act on one square-off — that is a duplicate exit."""
        key = keys.squareoff_timer()
        now = int(time.time() * 1000)
        for i in range(20):
            await primitives.schedule(r, key, f"pos:{i}", now - 1_000)

        batches = await asyncio.gather(*[primitives.pop_due(r, key, now_ms=now) for _ in range(6)])
        popped = [m for b in batches for m in b]
        assert len(popped) == len(set(popped)) == 20, "an item was popped twice"

    async def test_rescheduling_moves_the_deadline_rather_than_duplicating(
        self, r: aioredis.Redis
    ) -> None:
        key = keys.squareoff_timer()
        now = int(time.time() * 1000)
        await primitives.schedule(r, key, "pos:1", now + 1_000)
        await primitives.schedule(r, key, "pos:1", now + 90_000)
        assert await primitives.scheduled_count(r, key) == 1
        nxt = await primitives.peek_next(r, key)
        assert nxt is not None and nxt[1] == now + 90_000

    async def test_cancel_removes_it(self, r: aioredis.Redis) -> None:
        key = keys.squareoff_timer()
        await primitives.schedule(r, key, "pos:1", int(time.time() * 1000))
        assert await primitives.cancel(r, key, "pos:1") is True
        assert await primitives.cancel(r, key, "pos:1") is False


class TestTypedState:
    async def test_round_trip_preserves_decimal_exactly(self, r: aioredis.Redis) -> None:
        """Money is never float — a JSON round trip must not quietly make it one."""
        snap = Snapshot(symbol="INFY", price=Decimal("1234.5678"), ready=True)
        await state.set_state(r, "state:x", snap)
        back = await state.get_state(r, "state:x", Snapshot)
        assert back is not None
        assert back.price == Decimal("1234.5678")
        assert isinstance(back.price, Decimal)

    async def test_absent_key_returns_none(self, r: aioredis.Redis) -> None:
        assert await state.get_state(r, "state:nothing", Snapshot) is None

    async def test_invalid_payload_is_treated_as_absent(self, r: aioredis.Redis) -> None:
        """Half-valid trading state is worse than none — it keeps being acted on."""
        await r.set("state:bad", '{"symbol": "INFY"}')  # missing required fields
        assert await state.get_state(r, "state:bad", Snapshot) is None

    async def test_non_json_is_treated_as_absent(self, r: aioredis.Redis) -> None:
        await r.set("state:junk", "not json at all")
        assert await state.get_state(r, "state:junk", Snapshot) is None

    async def test_ttl_is_applied(self, r: aioredis.Redis) -> None:
        snap = Snapshot(symbol="X", price=Decimal("1"), ready=False)
        await state.set_state(r, "state:ttl", snap, ttl_seconds=60)
        assert 0 < await r.ttl("state:ttl") <= 60

    async def test_get_many_is_one_round_trip_and_skips_bad_entries(
        self, r: aioredis.Redis
    ) -> None:
        good = Snapshot(symbol="A", price=Decimal("1"), ready=True)
        await state.set_state(r, "state:a", good)
        await r.set("state:b", "corrupt")
        out = await state.get_many(r, ["state:a", "state:b", "state:missing"], Snapshot)
        assert set(out) == {"state:a"}


class TestKillSwitchFailsClosed:
    """The asymmetry: a false halt costs a trade, a false all-clear costs a position."""

    async def test_absent_means_not_halted(self, r: aioredis.Redis) -> None:
        assert await state.is_kill_switch_active(r, keys.kill_switch()) is False

    async def test_present_means_halted(self, r: aioredis.Redis) -> None:
        await r.set(keys.kill_switch(), "1")
        assert await state.is_kill_switch_active(r, keys.kill_switch()) is True

    async def test_unreachable_redis_means_halted(self, redis_url: str) -> None:
        """Fail closed. Reading 'not halted' from a broken lookup keeps trading
        exactly when the coordination layer is known to be broken."""
        dead = aioredis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
        try:
            assert await state.is_kill_switch_active(dead, keys.kill_switch()) is True
        finally:
            await dead.aclose()
