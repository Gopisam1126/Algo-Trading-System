"""Redis Streams: publish, consume, recover, dead-letter.

**`maxlen` is a required argument on `publish`, and that is the point of this
module.**

Redis runs with ``maxmemory 2gb`` and ``maxmemory-policy noeviction``. That
policy is correct — trading state must never be silently evicted — but it has a
consequence that is easy to miss: **an untrimmed stream does not lose old
entries, it fills memory and then Redis REFUSES ALL WRITES.** Not just to that
stream. To everything. The system stops mid-session with write errors rather
than degrading.

``stream:ticks`` alone is ~10 MB/day. Left untrimmed for a month that is 300 MB;
for a year, past the cap. So a stream that can be published to without a bound
should not be constructible, and here it is not.

Delivery is **at-least-once** and consumers must be idempotent. That is the
correct choice rather than a limitation to route around: processing a bar twice
yields the same indicator state; a duplicate signal is caught by ``lock:symbol``
and ``uq_open_symbol``; a duplicate audit event is caught by ``message_id``.
Exactly-once would need distributed transactions across Redis and Postgres for
no practical gain.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Final, cast

import redis.asyncio as aioredis
from redis.exceptions import ResponseError

from algotrader.common.events.envelope import Envelope
from algotrader.common.redis import keys

log = logging.getLogger(__name__)

#: Per-stream caps from EPIC01_TECHNICAL_SPEC.md §4.3. Approximate trimming
#: (``approximate=True`` -> ``MAXLEN ~``) is far cheaper than exact and entirely
#: adequate: the guarantee needed is "bounded", not "exactly N".
DEFAULT_MAXLEN: Final[dict[str, int]] = {
    "stream:ticks": 100_000,  # ~2 hours; the archive is the durable path
    "stream:snapshots": 10_000,
    "stream:signals": 10_000,
    "stream:orders": 10_000,
    "stream:audit": 50_000,  # drained to Postgres continuously
}

#: Entries pending longer than this are assumed abandoned and reclaimed.
DEFAULT_MIN_IDLE_MS: Final = 60_000

#: After this many delivery attempts an entry is dead-lettered. A message that
#: cannot be processed three times is a bug, not a transient.
DEFAULT_MAX_ATTEMPTS: Final = 3


class StreamError(RuntimeError):
    """Raised when a stream operation is used incorrectly."""


@dataclass(frozen=True, slots=True)
class Delivery:
    """One delivered entry, with the id needed to acknowledge it."""

    entry_id: str
    envelope: Envelope
    attempt: int = 1


def default_maxlen(stream: str) -> int:
    """The documented cap for a stream, or a conservative default.

    Bar streams are per-timeframe (``stream:bars:5m``) so they are matched by
    prefix rather than needing an entry each.
    """
    if stream in DEFAULT_MAXLEN:
        return DEFAULT_MAXLEN[stream]
    if stream.startswith("stream:bars:"):
        return 10_000
    if stream.startswith("stream:dlq:"):
        return 10_000
    return 10_000


async def publish(
    client: aioredis.Redis,
    stream: str,
    envelope: Envelope,
    *,
    maxlen: int,
) -> str:
    """Append an envelope. Returns the Redis entry id.

    ``maxlen`` is REQUIRED and has no default — see the module docstring. Use
    :func:`default_maxlen` to get the documented cap for a stream rather than
    inventing a number at the call site.
    """
    if maxlen <= 0:
        raise StreamError(
            f"maxlen must be positive, got {maxlen}. Under noeviction an unbounded "
            f"stream fills memory and Redis then refuses ALL writes, system-wide."
        )
    # cast: redis-py types the field map as accepting bytes|str|int|float keys
    # AND values; dict[str, str] is a valid subset but mypy will not narrow the
    # invariant dict type. The values are all str by construction (to_fields).
    fields = cast("dict[Any, Any]", envelope.to_fields())
    entry_id = await client.xadd(stream, fields, maxlen=maxlen, approximate=True)
    return str(entry_id)


async def ensure_group(client: aioredis.Redis, stream: str, group: str) -> None:
    """Create the consumer group if absent. Idempotent.

    ``mkstream=True`` creates the stream too, so a consumer can start before its
    producer has ever run — which is the normal case at boot. ``BUSYGROUP`` means
    another process created it first; that is success, not an error.
    """
    try:
        await client.xgroup_create(stream, group, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def consume(
    client: aioredis.Redis,
    stream: str,
    group: str,
    consumer: str,
    *,
    count: int = 10,
    block_ms: int = 1_000,
    include_backlog: bool = True,
) -> list[Delivery]:
    """Read entries for this consumer.

    **The backlog read is the step that is easy to miss and the one the
    acceptance criterion tests.** On restart, a consumer has entries that were
    delivered to it but never acknowledged — they are invisible to a normal
    ``'>'`` read, which only returns *new* messages. Reading id ``'0'`` first
    returns this consumer's own pending entries. Skip it and every message
    in-flight at the moment of a crash is silently stranded forever.

    Order matters: backlog first, then new. Processing new messages while older
    unacked ones sit pending reorders the stream from the consumer's point of
    view.
    """
    deliveries: list[Delivery] = []

    if include_backlog:
        pending = await client.xreadgroup(
            groupname=group, consumername=consumer, streams={stream: "0"}, count=count
        )
        deliveries.extend(_decode(client, stream, pending))
        if deliveries:
            # Drain the backlog fully before taking anything new.
            return deliveries

    fresh = await client.xreadgroup(
        groupname=group,
        consumername=consumer,
        streams={stream: ">"},
        count=count,
        block=block_ms,
    )
    deliveries.extend(_decode(client, stream, fresh))
    return deliveries


def _decode(client: aioredis.Redis, stream: str, raw: Any) -> list[Delivery]:
    """Turn the raw XREADGROUP reply into deliveries.

    An entry that will not parse is **not** raised here. One malformed message
    must not stop the stream, so it is logged and skipped; the reclaim path
    dead-letters it once its attempt count is exhausted.
    """
    out: list[Delivery] = []
    if not raw:
        return out
    for _stream_name, entries in raw:
        for entry_id, fields in entries:
            try:
                out.append(Delivery(entry_id=str(entry_id), envelope=Envelope.from_fields(fields)))
            except ValueError:
                log.error(
                    "unparseable entry %s on %s — skipping; it will be dead-lettered "
                    "once its attempts are exhausted",
                    entry_id,
                    stream,
                    exc_info=True,
                )
    return out


async def acknowledge(client: aioredis.Redis, stream: str, group: str, *entry_ids: str) -> int:
    """Acknowledge entries. Call this **only after successful processing.**

    Acking on receipt turns at-least-once into at-most-once and loses messages
    on a crash mid-processing.
    """
    if not entry_ids:
        return 0
    return int(await client.xack(stream, group, *entry_ids))


async def reclaim_stalled(
    client: aioredis.Redis,
    stream: str,
    group: str,
    consumer: str,
    *,
    min_idle_ms: int = DEFAULT_MIN_IDLE_MS,
    count: int = 50,
) -> list[Delivery]:
    """Take over entries abandoned by a dead consumer.

    Without this, a consumer that dies holding pending entries strands them: they
    belong to a consumer name that will never return, and no other consumer will
    ever be offered them.
    """
    result = await client.xautoclaim(
        name=stream,
        groupname=group,
        consumername=consumer,
        min_idle_time=min_idle_ms,
        count=count,
    )
    # xautoclaim returns (next_cursor, entries) or (next_cursor, entries, deleted)
    entries = result[1] if len(result) >= 2 else []
    out: list[Delivery] = []
    for entry_id, fields in entries:
        try:
            out.append(Delivery(entry_id=str(entry_id), envelope=Envelope.from_fields(fields)))
        except ValueError:
            log.error("unparseable reclaimed entry %s on %s", entry_id, stream, exc_info=True)
    return out


async def delivery_attempts(client: aioredis.Redis, stream: str, group: str, entry_id: str) -> int:
    """How many times this entry has been delivered. 0 if not pending."""
    rows = await client.xpending_range(stream, group, min=entry_id, max=entry_id, count=1)
    return int(rows[0]["times_delivered"]) if rows else 0


async def dead_letter(
    client: aioredis.Redis,
    stream: str,
    group: str,
    delivery: Delivery,
    reason: str,
) -> str:
    """Move an entry to the DLQ and acknowledge the original.

    The original is acked **after** the DLQ write, never before: if the process
    dies between the two, the entry stays pending and is retried, which is
    recoverable. Acking first would lose it entirely.

    Every DLQ write deserves an alert. An entry that failed three times is a bug
    to investigate, not a transient to absorb quietly.
    """
    dlq = keys.stream_dlq(stream)
    failed = Envelope(
        message_id=delivery.envelope.message_id,
        correlation_id=delivery.envelope.correlation_id,
        schema_version=delivery.envelope.schema_version,
        emitted_at=dt.datetime.now(dt.UTC),
        emitted_by="dlq",
        payload={
            "original_stream": stream,
            "original_entry_id": delivery.entry_id,
            "original_emitted_by": delivery.envelope.emitted_by,
            "reason": reason,
            "payload": delivery.envelope.payload,
        },
    )
    dlq_id = await publish(client, dlq, failed, maxlen=default_maxlen(dlq))
    await acknowledge(client, stream, group, delivery.entry_id)

    log.error(
        "dead-lettered %s from %s to %s: %s",
        delivery.entry_id,
        stream,
        dlq,
        reason,
        extra={"correlation_id": str(delivery.envelope.correlation_id)},
    )
    return dlq_id


async def stream_length(client: aioredis.Redis, stream: str) -> int:
    return int(await client.xlen(stream))


async def pending_count(client: aioredis.Redis, stream: str, group: str) -> int:
    """How many entries are delivered-but-unacked. A steadily rising number is
    the signal that a consumer is failing silently."""
    try:
        summary = await client.xpending(stream, group)
    except ResponseError:
        return 0
    return int(summary["pending"]) if summary else 0


def new_consumer_name(service: str) -> str:
    """A unique consumer name per process.

    Two processes sharing a name share a pending list, so one can acknowledge
    the other's in-flight work. The uuid suffix makes that impossible.
    """
    return f"{service}-{uuid.uuid4().hex[:8]}"
