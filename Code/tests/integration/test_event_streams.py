"""E01-S04 — stream publish, consume, recovery and dead-lettering.

The acceptance criterion this file exists for:

    publish 100 -> consume 50 -> kill the consumer -> restart -> the remaining
    50 plus the unacked are processed, none lost, none duplicated beyond
    at-least-once.

That is ``test_crash_mid_stream_loses_nothing`` below. It is the test that
catches the single easiest mistake in this component — forgetting to read the
consumer's own pending backlog with id ``'0'`` on restart, which silently
strands every message in flight at the moment of a crash.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import redis.asyncio as aioredis

from algotrader.common.events import Envelope, stream
from algotrader.common.redis import keys

pytestmark = [pytest.mark.integration]

STREAM = "stream:signals"
GROUP = "test-group"


@pytest.fixture
async def r(redis_url: str) -> AsyncIterator[aioredis.Redis]:
    client = aioredis.from_url(redis_url, decode_responses=True)
    await client.flushall()
    try:
        yield client
    finally:
        await client.flushall()
        await client.aclose()


def make_envelope(n: int = 0, correlation_id: uuid.UUID | None = None) -> Envelope:
    return Envelope(
        correlation_id=correlation_id or uuid.uuid4(),
        emitted_at=dt.datetime.now(dt.UTC),
        emitted_by="test",
        payload={"n": n, "price": "1234.5678"},
    )


class TestMaxlenIsMandatory:
    """Under noeviction an unbounded stream stops ALL writes system-wide."""

    async def test_publish_without_maxlen_is_a_type_error(self, r: aioredis.Redis) -> None:
        """The signature must refuse it, not a runtime check."""
        with pytest.raises(TypeError):
            await stream.publish(r, STREAM, make_envelope())  # type: ignore[call-arg]

    async def test_zero_maxlen_is_rejected(self, r: aioredis.Redis) -> None:
        with pytest.raises(stream.StreamError):
            await stream.publish(r, STREAM, make_envelope(), maxlen=0)

    async def test_negative_maxlen_is_rejected(self, r: aioredis.Redis) -> None:
        with pytest.raises(stream.StreamError):
            await stream.publish(r, STREAM, make_envelope(), maxlen=-5)

    async def test_valid_maxlen_is_accepted(self, r: aioredis.Redis) -> None:
        """The control."""
        assert await stream.publish(r, STREAM, make_envelope(), maxlen=100)

    async def test_trimming_actually_bounds_the_stream(self, r: aioredis.Redis) -> None:
        """The cap must be real, not merely accepted as an argument.

        Approximate trimming means the length is bounded, not exact — asserting
        equality here would be asserting an implementation detail of Redis's
        macro-node trimming rather than the property we need.
        """
        for i in range(500):
            await stream.publish(r, STREAM, make_envelope(i), maxlen=50)
        assert await stream.stream_length(r, STREAM) < 500

    def test_documented_caps_match_the_capacity_analysis(self) -> None:
        assert stream.default_maxlen("stream:ticks") == 100_000
        assert stream.default_maxlen("stream:audit") == 50_000
        assert stream.default_maxlen("stream:bars:5m") == 10_000
        assert stream.default_maxlen("stream:unknown:thing") > 0


class TestEnvelope:
    def test_round_trip_preserves_the_payload(self) -> None:
        env = make_envelope(7)
        assert Envelope.from_fields(env.to_fields()).payload == env.payload

    def test_round_trip_preserves_ids_and_time(self) -> None:
        env = make_envelope()
        back = Envelope.from_fields(env.to_fields())
        assert back.message_id == env.message_id
        assert back.correlation_id == env.correlation_id
        assert back.emitted_at == env.emitted_at

    def test_schema_version_is_present_from_message_one(self) -> None:
        assert make_envelope().schema_version == 1

    def test_naive_timestamp_is_rejected(self) -> None:
        """A naive timestamp is a 5.5-hour error in this market, not a rounding one."""
        with pytest.raises(ValueError, match="timezone-aware"):
            Envelope(
                correlation_id=uuid.uuid4(),
                emitted_at=dt.datetime(2026, 8, 6, 9, 15),
                emitted_by="test",
                payload={},
            )

    def test_envelope_is_immutable(self) -> None:
        env = make_envelope()
        with pytest.raises(Exception):  # noqa: B017 - pydantic raises ValidationError
            env.emitted_by = "someone-else"  # type: ignore[misc]

    def test_malformed_entry_raises_valueerror_not_something_exotic(self) -> None:
        """Consumers catch ValueError to dead-letter; anything else kills the loop."""
        with pytest.raises(ValueError):
            Envelope.from_fields({"message_id": "not-a-uuid"})

    def test_decimal_survives_as_a_string_not_a_float(self) -> None:
        """Money must not silently become float across a stream hop."""
        env = Envelope(
            correlation_id=uuid.uuid4(),
            emitted_at=dt.datetime.now(dt.UTC),
            emitted_by="t",
            payload={"price": str(Decimal("1234.5678"))},
        )
        back = Envelope.from_fields(env.to_fields())
        assert Decimal(back.payload["price"]) == Decimal("1234.5678")


class TestConsumerGroups:
    async def test_group_creation_is_idempotent(self, r: aioredis.Redis) -> None:
        """Every service calls this at boot; BUSYGROUP is success, not an error."""
        await stream.ensure_group(r, STREAM, GROUP)
        await stream.ensure_group(r, STREAM, GROUP)

    async def test_group_can_be_created_before_the_producer_exists(self, r: aioredis.Redis) -> None:
        """mkstream — a consumer booting first is the normal case."""
        await stream.ensure_group(r, "stream:not:yet", GROUP)
        assert await r.exists("stream:not:yet")

    async def test_published_messages_are_delivered(self, r: aioredis.Redis) -> None:
        await stream.ensure_group(r, STREAM, GROUP)
        for i in range(5):
            await stream.publish(r, STREAM, make_envelope(i), maxlen=1000)
        got = await stream.consume(r, STREAM, GROUP, "c1", count=10, block_ms=100)
        assert [d.envelope.payload["n"] for d in got] == [0, 1, 2, 3, 4]

    async def test_unacked_messages_stay_pending(self, r: aioredis.Redis) -> None:
        await stream.ensure_group(r, STREAM, GROUP)
        await stream.publish(r, STREAM, make_envelope(), maxlen=1000)
        await stream.consume(r, STREAM, GROUP, "c1", block_ms=100)
        assert await stream.pending_count(r, STREAM, GROUP) == 1

    async def test_acknowledged_messages_clear(self, r: aioredis.Redis) -> None:
        await stream.ensure_group(r, STREAM, GROUP)
        await stream.publish(r, STREAM, make_envelope(), maxlen=1000)
        got = await stream.consume(r, STREAM, GROUP, "c1", block_ms=100)
        await stream.acknowledge(r, STREAM, GROUP, *[d.entry_id for d in got])
        assert await stream.pending_count(r, STREAM, GROUP) == 0

    async def test_each_process_gets_a_distinct_consumer_name(self) -> None:
        """Sharing a name means sharing a pending list — one can ack the other's work."""
        assert stream.new_consumer_name("svc") != stream.new_consumer_name("svc")


class TestCrashRecovery:
    async def test_crash_mid_stream_loses_nothing(self, r: aioredis.Redis) -> None:
        """THE acceptance criterion.

        Publish 100, consume and ack 50, then "crash" (stop consuming without
        acking the next batch) and restart. Everything must eventually be
        processed: none lost, none duplicated beyond at-least-once.
        """
        await stream.ensure_group(r, STREAM, GROUP)
        for i in range(100):
            await stream.publish(r, STREAM, make_envelope(i), maxlen=1000)

        seen: list[int] = []

        # First 50: consumed and acked cleanly.
        first = await stream.consume(r, STREAM, GROUP, "worker-1", count=50, block_ms=200)
        assert len(first) == 50
        seen += [d.envelope.payload["n"] for d in first]
        await stream.acknowledge(r, STREAM, GROUP, *[d.entry_id for d in first])

        # Next 10: delivered but NOT acked — this is the crash.
        stranded = await stream.consume(r, STREAM, GROUP, "worker-1", count=10, block_ms=200)
        assert len(stranded) == 10
        assert await stream.pending_count(r, STREAM, GROUP) == 10

        # Restart under the SAME consumer name. The backlog read at id '0' is
        # what makes the stranded entries visible again; without it they are
        # invisible to a '>' read and lost forever.
        recovered: list[int] = []
        for _ in range(20):
            batch = await stream.consume(r, STREAM, GROUP, "worker-1", count=25, block_ms=100)
            if not batch:
                break
            recovered += [d.envelope.payload["n"] for d in batch]
            await stream.acknowledge(r, STREAM, GROUP, *[d.entry_id for d in batch])

        processed = set(seen) | set(recovered)
        assert processed == set(range(100)), (
            f"lost {sorted(set(range(100)) - processed)} — the backlog read is missing"
        )
        assert await stream.pending_count(r, STREAM, GROUP) == 0

    async def test_backlog_is_drained_before_new_messages(self, r: aioredis.Redis) -> None:
        """Processing new work ahead of older unacked work reorders the stream."""
        await stream.ensure_group(r, STREAM, GROUP)
        await stream.publish(r, STREAM, make_envelope(1), maxlen=1000)
        await stream.consume(r, STREAM, GROUP, "w", block_ms=100)  # leave unacked
        await stream.publish(r, STREAM, make_envelope(2), maxlen=1000)

        nxt = await stream.consume(r, STREAM, GROUP, "w", block_ms=100)
        assert [d.envelope.payload["n"] for d in nxt] == [1], "new message jumped the backlog"

    async def test_a_dead_consumers_work_can_be_reclaimed(self, r: aioredis.Redis) -> None:
        """Entries held by a consumer that never returns must not be stranded."""
        await stream.ensure_group(r, STREAM, GROUP)
        await stream.publish(r, STREAM, make_envelope(42), maxlen=1000)
        await stream.consume(r, STREAM, GROUP, "doomed", block_ms=100)

        reclaimed = await stream.reclaim_stalled(r, STREAM, GROUP, "survivor", min_idle_ms=0)
        assert [d.envelope.payload["n"] for d in reclaimed] == [42]

    async def test_fresh_entries_are_not_stolen_from_a_live_consumer(
        self, r: aioredis.Redis
    ) -> None:
        """min_idle_ms is what stops reclaim from racing a healthy worker."""
        await stream.ensure_group(r, STREAM, GROUP)
        await stream.publish(r, STREAM, make_envelope(), maxlen=1000)
        await stream.consume(r, STREAM, GROUP, "busy", block_ms=100)

        assert await stream.reclaim_stalled(r, STREAM, GROUP, "thief", min_idle_ms=60_000) == []


class TestDeadLettering:
    async def test_dead_letter_moves_and_acks(self, r: aioredis.Redis) -> None:
        await stream.ensure_group(r, STREAM, GROUP)
        await stream.publish(r, STREAM, make_envelope(9), maxlen=1000)
        [delivery] = await stream.consume(r, STREAM, GROUP, "w", block_ms=100)

        await stream.dead_letter(r, STREAM, GROUP, delivery, "handler raised three times")

        assert await stream.pending_count(r, STREAM, GROUP) == 0, "original not acked"
        assert await stream.stream_length(r, keys.stream_dlq(STREAM)) == 1

    async def test_dead_letter_preserves_correlation_and_the_reason(
        self, r: aioredis.Redis
    ) -> None:
        """The DLQ entry has to be diagnosable — it is the record of a bug."""
        cid = uuid.uuid4()
        await stream.ensure_group(r, STREAM, GROUP)
        await stream.publish(r, STREAM, make_envelope(1, cid), maxlen=1000)
        [delivery] = await stream.consume(r, STREAM, GROUP, "w", block_ms=100)
        await stream.dead_letter(r, STREAM, GROUP, delivery, "boom")

        entries = await r.xrange(keys.stream_dlq(STREAM))
        env = Envelope.from_fields(entries[0][1])
        assert env.correlation_id == cid
        assert env.payload["reason"] == "boom"
        assert env.payload["original_stream"] == STREAM
        assert env.payload["payload"]["n"] == 1

    async def test_delivery_attempts_are_counted(self, r: aioredis.Redis) -> None:
        """The counter that decides when to give up and dead-letter."""
        await stream.ensure_group(r, STREAM, GROUP)
        await stream.publish(r, STREAM, make_envelope(), maxlen=1000)
        [d] = await stream.consume(r, STREAM, GROUP, "w", block_ms=100)
        assert await stream.delivery_attempts(r, STREAM, GROUP, d.entry_id) == 1

        await stream.reclaim_stalled(r, STREAM, GROUP, "w2", min_idle_ms=0)
        assert await stream.delivery_attempts(r, STREAM, GROUP, d.entry_id) >= 2


class TestPoisonedMessages:
    async def test_one_bad_entry_does_not_stop_the_stream(self, r: aioredis.Redis) -> None:
        """A message that cannot be parsed must not take the consumer down with it."""
        await stream.ensure_group(r, STREAM, GROUP)
        await r.xadd(STREAM, {"garbage": "not an envelope"}, maxlen=1000)
        await stream.publish(r, STREAM, make_envelope(7), maxlen=1000)

        got = await stream.consume(r, STREAM, GROUP, "w", count=10, block_ms=200)
        assert [d.envelope.payload["n"] for d in got] == [7]
