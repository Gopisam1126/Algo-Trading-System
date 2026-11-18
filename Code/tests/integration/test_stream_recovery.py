"""A restarted consumer gets its predecessor's work; a poisoned entry gets the DLQ.

Audit ENG-013 (17 Nov 2026). Three defects, each the library's:

- Both consumers named themselves with a fresh uuid per process, so a
  restarted process read an EMPTY pending list of its own: the entries its
  predecessor held were stranded under a name nothing would use again. The
  crash-recovery tests passed because they restarted under the same name by
  hand - the production naming scheme was untested.
- An entry that did not parse was skipped and left pending, so it came back on
  every backlog read; with ``count`` of them at the head of the pending list,
  nothing behind them was ever returned. The docstring promised the reclaim
  path would dead-letter it, and nothing called the reclaim path.
- An entry that took its consumer down was redelivered after every restart,
  forever: ``DEFAULT_MAX_ATTEMPTS`` was read by nothing.

These run the real consumers (``RedisSignalSource``, ``RedisBarSource``) with
their DEFAULT names, which is what production runs.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator

import pytest
import redis.asyncio as aioredis

from algotrader.common.events import Envelope, stream
from algotrader.common.redis import keys
from algotrader.execution.service import GROUP as EXECUTION_GROUP
from algotrader.execution.service import RedisSignalSource

pytestmark = [pytest.mark.integration]

STREAM = "stream:recovery"
GROUP = "recovery-group"


@pytest.fixture
async def r(redis_url: str) -> AsyncIterator[aioredis.Redis]:
    client = aioredis.from_url(redis_url, decode_responses=True)
    await client.flushall()
    try:
        yield client
    finally:
        await client.flushall()
        await client.aclose()


def envelope(n: int) -> Envelope:
    return Envelope(
        correlation_id=uuid.uuid4(),
        emitted_at=dt.datetime.now(dt.UTC),
        emitted_by="test",
        payload={"n": n},
    )


async def dlq_reasons(r: aioredis.Redis, name: str) -> list[str]:
    return [
        Envelope.from_fields(fields).payload["reason"]
        for _, fields in await r.xrange(keys.stream_dlq(name))
    ]


class TestTheRestartedServiceGetsWhatItsPredecessorHeld:
    async def test_the_execution_source_restarted_with_its_default_name(
        self, r: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(stream.INSTANCE_ENV, raising=False)
        signals = keys.stream_signals()
        crashed = RedisSignalSource(r, block_ms=50)
        await crashed.start()
        await stream.publish(r, signals, envelope(1), maxlen=1000)
        held = await crashed.next_batch()
        assert [d.envelope.payload["n"] for d in held] == [1]
        # The process dies here: read, never acknowledged.

        restarted = RedisSignalSource(r, block_ms=50)
        await restarted.start()
        again = await restarted.next_batch()
        assert [d.entry_id for d in again] == [held[0].entry_id], (
            "the restarted service never saw the signal its predecessor held"
        )

    async def test_work_held_under_an_old_style_name_is_reclaimed_at_start(
        self, r: aioredis.Redis
    ) -> None:
        """A consumer from before this fix carried a uuid; nothing will ever
        read its pending list again. start() reclaims it."""
        signals = keys.stream_signals()
        await stream.ensure_group(r, signals, EXECUTION_GROUP)
        await stream.publish(r, signals, envelope(2), maxlen=1000)
        await stream.consume(r, signals, EXECUTION_GROUP, "execution-svc-1a2b3c4d", block_ms=50)

        source = RedisSignalSource(r, block_ms=50, reclaim_idle_ms=0)
        await source.start()
        got = await source.next_batch()
        assert [d.envelope.payload["n"] for d in got] == [2]

    async def test_a_live_consumers_fresh_work_is_not_taken(self, r: aioredis.Redis) -> None:
        """The control: the reclaim waits out min_idle, so it cannot race a
        healthy worker."""
        signals = keys.stream_signals()
        await stream.ensure_group(r, signals, EXECUTION_GROUP)
        await stream.publish(r, signals, envelope(3), maxlen=1000)
        await stream.consume(r, signals, EXECUTION_GROUP, "execution-svc-2", block_ms=50)

        source = RedisSignalSource(r, block_ms=50)  # the default 60 s idle
        await source.start()
        assert await source.next_batch() == []


class TestAnUnreadableEntryIsDeadLetteredWhereItIsRead:
    async def test_consume_dead_letters_it_and_leaves_nothing_pending(
        self, r: aioredis.Redis
    ) -> None:
        await stream.ensure_group(r, STREAM, GROUP)
        await r.xadd(STREAM, {"garbage": "not an envelope"}, maxlen=1000)
        assert await stream.consume(r, STREAM, GROUP, "w", block_ms=50) == []
        assert await stream.pending_count(r, STREAM, GROUP) == 0, "left pending"
        assert await dlq_reasons(r, STREAM) == ["not an envelope: it does not parse"]

    async def test_a_head_of_poison_does_not_hide_the_good_entries_behind_it(
        self, r: aioredis.Redis
    ) -> None:
        """With `count` unreadable entries at the head of the pending list, the
        backlog read returned nothing - and the valid entries behind them were
        never returned at all."""
        await stream.ensure_group(r, STREAM, GROUP)
        for _ in range(3):
            await r.xadd(STREAM, {"garbage": "x"}, maxlen=1000)
        for n in range(2):
            await stream.publish(r, STREAM, envelope(n), maxlen=1000)
        # Delivered once, not acknowledged: all five now sit in the backlog.
        await r.xreadgroup(GROUP, "w", {STREAM: ">"}, count=10)

        seen: list[int] = []
        for _ in range(5):
            batch = await stream.consume(r, STREAM, GROUP, "w", count=3, block_ms=50)
            seen += [d.envelope.payload["n"] for d in batch]
            await stream.acknowledge(r, STREAM, GROUP, *[d.entry_id for d in batch])
        assert seen == [0, 1]
        assert len(await dlq_reasons(r, STREAM)) == 3

    async def test_consume_many_dead_letters_it_too(self, r: aioredis.Redis) -> None:
        await stream.ensure_group(r, STREAM, GROUP)
        await r.xadd(STREAM, {"garbage": "x"}, maxlen=1000)
        await stream.publish(r, STREAM, envelope(5), maxlen=1000)
        got = await stream.consume_many(r, [STREAM], GROUP, "w", block_ms=50)
        assert [e.delivery.envelope.payload["n"] for e in got if e.delivery] == [5]
        assert all(e.delivery is not None for e in got)
        assert len(await dlq_reasons(r, STREAM)) == 1


class TestAnEntryThatKeepsKillingItsConsumerIsDeadLettered:
    async def test_the_backlog_read_gives_up_after_max_attempts(self, r: aioredis.Redis) -> None:
        await stream.ensure_group(r, STREAM, GROUP)
        await stream.publish(r, STREAM, envelope(9), maxlen=1000)
        await stream.consume(r, STREAM, GROUP, "w", block_ms=50)  # delivery 1
        for attempt in range(2, stream.DEFAULT_MAX_ATTEMPTS + 1):  # each a "restart"
            got = await stream.consume(r, STREAM, GROUP, "w", block_ms=50)
            assert len(got) == 1, f"delivery {attempt} should still be handed on"
        assert await stream.consume(r, STREAM, GROUP, "w", block_ms=50) == []
        assert await stream.pending_count(r, STREAM, GROUP) == 0
        (reason,) = await dlq_reasons(r, STREAM)
        assert "delivered 4 times" in reason

    async def test_a_first_delivery_is_never_counted_against_it(self, r: aioredis.Redis) -> None:
        """The control: a fresh read hands everything on, whatever max_attempts."""
        await stream.ensure_group(r, STREAM, GROUP)
        await stream.publish(r, STREAM, envelope(1), maxlen=1000)
        got = await stream.consume(r, STREAM, GROUP, "w", block_ms=50, max_attempts=0)
        assert len(got) == 1


class TestReclaimWalksEveryPage:
    async def test_more_than_one_page_is_reclaimed(self, r: aioredis.Redis) -> None:
        await stream.ensure_group(r, STREAM, GROUP)
        for n in range(7):
            await stream.publish(r, STREAM, envelope(n), maxlen=1000)
        await stream.consume(r, STREAM, GROUP, "gone", count=10, block_ms=50)
        claimed = await stream.reclaim_stalled(r, STREAM, GROUP, "heir", min_idle_ms=0, count=2)
        assert sorted(d.envelope.payload["n"] for d in claimed) == list(range(7))

    async def test_an_unreadable_reclaimed_entry_is_dead_lettered(self, r: aioredis.Redis) -> None:
        await stream.ensure_group(r, STREAM, GROUP)
        await r.xadd(STREAM, {"garbage": "x"}, maxlen=1000)
        await r.xreadgroup(GROUP, "gone", {STREAM: ">"}, count=10)
        assert await stream.reclaim_stalled(r, STREAM, GROUP, "heir", min_idle_ms=0) == []
        assert await stream.pending_count(r, STREAM, GROUP) == 0
        assert len(await dlq_reasons(r, STREAM)) == 1
