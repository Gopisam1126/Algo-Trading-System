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
and ``uq_open_symbol``. (Audit entries do not travel by stream at all - see
:data:`NOT_PUBLISHABLE`.)
Exactly-once would need distributed transactions across Redis and Postgres for
no practical gain.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import uuid
from collections.abc import Sequence
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
}

#: Streams nothing may publish to, and why. ``stream:audit`` was planned as a
#: Redis hop drained to Postgres by a ``db-writer``; what was built writes
#: straight to Postgres through ``AuditWriter``, whose disk buffer survives an
#: outage. No drainer exists, and a capped stream TRIMS - approximately - so an
#: audit entry published here would be silently discarded once 50,000 newer
#: ones arrived (audit NR-010, 17 Nov 2026). Refused rather than capped.
NOT_PUBLISHABLE: Final[dict[str, str]] = {
    "stream:audit": (
        "audit entries go to Postgres through common.audit.AuditWriter, never through "
        "a capped stream that trims what nothing drains"
    ),
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

    **One call can append twice.** The client retries a command whose reply was
    lost (``common/redis/client.py``), so the same envelope can sit under two
    entry ids. Consumers de-duplicate on the envelope's content - its bar, its
    correlation id - never on the entry id (audit NR-007).
    """
    if maxlen <= 0:
        raise StreamError(
            f"maxlen must be positive, got {maxlen}. Under noeviction an unbounded "
            f"stream fills memory and Redis then refuses ALL writes, system-wide."
        )
    if stream in NOT_PUBLISHABLE:
        raise StreamError(f"{stream} is not publishable: {NOT_PUBLISHABLE[stream]}")
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
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
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

    An entry that is not an Envelope is dead-lettered and acknowledged here,
    never returned and never left pending (audit ENG-013); so is a backlog
    entry delivered more than ``max_attempts`` times.
    """
    if include_backlog:
        pending = await client.xreadgroup(
            groupname=group, consumername=consumer, streams={stream: "0"}, count=count
        )
        entries = _decode_tagged(pending)
        if entries:
            # Drain the backlog fully before taking anything new - even when
            # every entry in this read was unreadable and is now dead-lettered:
            # reading '>' next would put new work ahead of older pending work.
            kept = await _deliverable(client, group, entries, max_attempts=max_attempts)
            return [e.delivery for e in kept if e.delivery is not None]

    fresh = await client.xreadgroup(
        groupname=group,
        consumername=consumer,
        streams={stream: ">"},
        count=count,
        block=block_ms,
    )
    kept = await _deliverable(client, group, _decode_tagged(fresh), max_attempts=None)
    return [e.delivery for e in kept if e.delivery is not None]


async def _deliverable(
    client: aioredis.Redis,
    group: str,
    entries: list[StreamEntry],
    *,
    max_attempts: int | None,
) -> list[StreamEntry]:
    """The entries worth handing on; the rest dead-lettered on the way.

    Unreadable entries always go. With ``max_attempts`` - a BACKLOG read,
    i.e. redeliveries - so does an entry delivered more times than that: a
    message that took its consumer down repeatedly would otherwise be handed
    back after every restart, forever (ENG-013). A first delivery is never
    counted, so this costs nothing in normal running, when the backlog is
    empty.
    """
    out: list[StreamEntry] = []
    for entry in entries:
        if entry.delivery is None:
            await _dead_letter_unreadable(client, entry, group)
            continue
        if max_attempts is not None:
            attempts = await delivery_attempts(client, entry.stream, group, entry.entry_id)
            if attempts > max_attempts:
                await dead_letter(
                    client,
                    entry.stream,
                    group,
                    entry.delivery,
                    f"delivered {attempts} times without being acknowledged",
                )
                continue
        out.append(entry)
    return out


@dataclass(frozen=True, slots=True)
class StreamEntry:
    """One entry read from a stream: where it came from, and what it held.

    ``delivery`` is ``None`` when the entry is not an Envelope, and ``fields``
    is what it held instead (``None`` for an entry trimmed from the stream
    while pending). Readers dead-letter such an entry where they read it
    (audit ENG-013): one that is merely skipped stays pending, comes back on
    every backlog read, and with ``count`` of them at the head of the pending
    list, nothing behind them is ever returned.
    """

    stream: str
    entry_id: str
    delivery: Delivery | None
    fields: dict[Any, Any] | None = None


async def consume_many(
    client: aioredis.Redis,
    streams: Sequence[str],
    group: str,
    consumer: str,
    *,
    count: int = 10,
    block_ms: int = 1_000,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> list[StreamEntry]:
    """Read several streams in ONE ``XREADGROUP``, backlog first.

    One command is one moment: an entry written to any of the streams before
    the read is in its reply. Three separate reads are three moments, and a
    producer writing between them is seen by some and not others - which is
    the difference between "every bar that closed at 10:15" and "some of
    them" (E13-S01). ``count`` applies per stream.

    Every returned entry carries a delivery: an unreadable one, or one
    redelivered more than ``max_attempts`` times, is dead-lettered here.
    """
    names = list(dict.fromkeys(streams))
    if not names:
        raise StreamError("consume_many needs at least one stream")
    pending = await client.xreadgroup(
        groupname=group, consumername=consumer, streams=dict.fromkeys(names, "0"), count=count
    )
    entries = _decode_tagged(pending)
    if entries:
        return await _deliverable(client, group, entries, max_attempts=max_attempts)
    fresh = await client.xreadgroup(
        groupname=group,
        consumername=consumer,
        streams=dict.fromkeys(names, ">"),
        count=count,
        block=block_ms,
    )
    return await _deliverable(client, group, _decode_tagged(fresh), max_attempts=None)


def _decode_tagged(raw: Any) -> list[StreamEntry]:
    """Turn an XREADGROUP reply into entries. Never raises for one bad entry:
    one malformed message must not stop the stream."""
    out: list[StreamEntry] = []
    for stream_name, entries in raw or []:
        name = stream_name.decode() if isinstance(stream_name, bytes) else str(stream_name)
        for entry_id, fields in entries:
            try:
                delivery: Delivery | None = Delivery(
                    entry_id=str(entry_id), envelope=Envelope.from_fields(fields)
                )
            except (ValueError, TypeError):
                # TypeError: a pending entry trimmed from the stream comes back
                # with no fields at all.
                delivery = None
            out.append(
                StreamEntry(
                    stream=name,
                    entry_id=str(entry_id),
                    delivery=delivery,
                    fields=None if delivery is not None else fields,
                )
            )
    return out


#: The correlation id a dead-lettered entry gets when it carried none that reads.
NO_CORRELATION: Final = uuid.UUID(int=0)


async def _dead_letter_unreadable(client: aioredis.Redis, entry: StreamEntry, group: str) -> None:
    """Dead-letter an entry that is not an Envelope, then acknowledge it.

    No retry can make malformed bytes parse, so it goes at once - not after
    attempts it would only fail again (audit ENG-013). What it held is kept,
    bounded, so the DLQ entry is still the record of a bug.
    """
    fields = entry.fields
    if fields is None:
        reason = "trimmed from the stream before it was processed"
    else:
        reason = "not an envelope: it does not parse"
    held = {str(k)[:64]: str(v)[:512] for k, v in list((fields or {}).items())[:16]}
    try:
        correlation = uuid.UUID(str((fields or {}).get("correlation_id")))
    except ValueError:
        correlation = NO_CORRELATION
    dlq = keys.stream_dlq(entry.stream)
    record = Envelope(
        correlation_id=correlation,
        emitted_at=dt.datetime.now(dt.UTC),
        emitted_by="dlq",
        payload={
            "original_stream": entry.stream,
            "original_entry_id": entry.entry_id,
            "reason": reason,
            "fields": held,
        },
    )
    await publish(client, dlq, record, maxlen=default_maxlen(dlq))
    await acknowledge(client, entry.stream, group, entry.entry_id)
    log.error("dead-lettered unreadable entry %s from %s: %s", entry.entry_id, entry.stream, reason)


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
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    max_pages: int = 100,
) -> list[Delivery]:
    """Take over entries abandoned by a dead consumer, into ``consumer``'s pending list.

    Without this, a consumer that dies holding pending entries strands them: they
    belong to a consumer name that will never return, and no other consumer will
    ever be offered them. Consumers call it at ``start()`` (audit ENG-013 - until
    then nothing did), so a process that comes back under a NEW name, or a
    consumer renamed by a deployment, still gets the work its predecessor held:
    the claimed entries are returned AND sit in this consumer's pending list,
    where its next backlog read finds them.

    Walks every page of the pending list (``count`` per page, at most
    ``max_pages``). An entry that does not parse, or that has now been
    delivered more than ``max_attempts`` times - it has killed a consumer
    before, more than once - is dead-lettered instead of returned.
    """
    out: list[Delivery] = []
    cursor = "0-0"
    for _ in range(max_pages):
        result = await client.xautoclaim(
            name=stream,
            groupname=group,
            consumername=consumer,
            min_idle_time=min_idle_ms,
            start_id=cursor,
            count=count,
        )
        # xautoclaim returns (next_cursor, entries) or (next_cursor, entries, deleted)
        cursor = str(result[0].decode() if isinstance(result[0], bytes) else result[0])
        raw = result[1] if len(result) >= 2 else []
        for entry in _decode_tagged([(stream, raw)]):
            if entry.delivery is None:
                await _dead_letter_unreadable(client, entry, group)
                continue
            attempts = await delivery_attempts(client, stream, group, entry.entry_id)
            if attempts > max_attempts:
                await dead_letter(
                    client,
                    stream,
                    group,
                    entry.delivery,
                    f"delivered {attempts} times without being acknowledged",
                )
                continue
            out.append(entry.delivery)
        if cursor in {"0-0", "0"}:
            break
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


#: Which instance of a service this process is. One per service is the
#: deployment (docker-compose runs one container each); a second instance
#: needs its own value, or the two would share a pending list.
INSTANCE_ENV: Final = "ALGOTRADER_INSTANCE"

_SAFE_INSTANCE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")


def consumer_name(service: str, *, instance: str | None = None) -> str:
    """The consumer name for ``service`` - the SAME across restarts.

    It used to carry a fresh uuid per process (``new_consumer_name``), so a
    restarted process read an empty pending list of its own and the entries
    its predecessor held were stranded under a name nobody would use again:
    at-least-once became at-most-once across exactly the event it exists for.
    The tests passed because they restarted under the same name by hand
    (audit ENG-013, 17 Nov 2026). A stable name makes the restart read its
    predecessor's pending entries first.

    Two LIVE processes must not share a name - one could acknowledge the
    other's in-flight work - so a second instance sets :data:`INSTANCE_ENV`.
    """
    chosen = instance if instance is not None else os.environ.get(INSTANCE_ENV, "1")
    if _SAFE_INSTANCE.fullmatch(chosen) is None:
        raise StreamError(
            f"{INSTANCE_ENV} must be 1-32 of a-z 0-9 -, starting alphanumeric; got {chosen[:40]!r}"
        )
    return f"{service}-{chosen}"
