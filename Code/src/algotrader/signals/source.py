"""Bars from the stream into the signal loop, and back after a restart (E13-S01).

Every bar stream the engine carries is read in ONE ``XREADGROUP``
(:func:`~algotrader.common.events.stream.consume_many`), and each batch is
handed to the loop in :func:`~algotrader.ingest.bars.publication_order` - by
close time, the longest timeframe first. The producer writes in the same
order, so a batch holding a 5m bar holds every longer bar closing with it.

An entry is acknowledged only after the loop has finished with it. A bar
that is not a bar, or whose envelope disagrees with it about which bar it
is, is dead-lettered at once: no retry can fix it, and the bar after it then
reads as a gap, which stops the symbol rather than guessing.

**After a restart** the engine is warm only through yesterday - live bars are
not in Postgres during the session (LLA §4.1). :func:`replay_today` folds
the bars the group had FINISHED with (delivered and acknowledged) from the
streams, evaluating none of them, so the indicators and the opening range are
what they were; the bars the dead process held, and the undelivered ones,
then come through the normal path and are evaluated - or refused as LATE.
Without it the first bar after a mid-session restart is a gap, and the
symbol is stale for the rest of the day: safe, and a lost day.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from pydantic import ValidationError

from algotrader.common.enums import Timeframe
from algotrader.common.events import stream as streams
from algotrader.common.events.envelope import Envelope
from algotrader.common.events.stream import Delivery
from algotrader.common.models.market import Bar
from algotrader.common.redis import keys
from algotrader.ingest.bars import bar_close, bar_id, publication_order
from algotrader.signals.loop import BarOutcome, BarReport, SignalLoop

if TYPE_CHECKING:
    import redis.asyncio as aioredis

log = logging.getLogger(__name__)

#: The consumer group, named for the service (TRADING_SERVICES).
GROUP = "signal-engine"


@dataclass(frozen=True)
class BarDelivery:
    stream: str
    delivery: Delivery
    bar: Bar


def _read_bar(name: str, envelope: Envelope) -> Bar:
    """The bar an entry carries, or ValueError saying why it is not one."""
    try:
        bar = Bar.model_validate(envelope.payload)
    except ValidationError as exc:
        raise ValueError(f"not a Bar ({exc.error_count()} validation error(s))") from exc
    if envelope.correlation_id != bar_id(bar):
        raise ValueError("the envelope and the bar disagree about which bar this is")
    if keys.stream_bars(bar.timeframe) != name:
        raise ValueError(f"a {bar.timeframe.value} bar on {name}")
    return bar


class RedisBarSource:
    def __init__(
        self,
        client: aioredis.Redis,
        timeframes: Sequence[Timeframe],
        *,
        consumer: str | None = None,
        count: int = 50,
        block_ms: int = 1_000,
    ) -> None:
        if not timeframes:
            raise ValueError("a bar source needs at least one timeframe")
        self._client = client
        self._streams = [keys.stream_bars(tf) for tf in timeframes]
        self._consumer = consumer or streams.new_consumer_name(GROUP)
        self._count = count
        self._block_ms = block_ms

    async def start(self) -> None:
        for name in self._streams:
            await streams.ensure_group(self._client, name, GROUP)

    async def next_batch(self) -> list[BarDelivery]:
        entries = await streams.consume_many(
            self._client,
            self._streams,
            GROUP,
            self._consumer,
            count=self._count,
            block_ms=self._block_ms,
        )
        out: list[BarDelivery] = []
        for entry in entries:
            if entry.delivery is None:
                # Not an envelope at all: nothing to dead-letter it as. Acked so
                # the backlog read does not return it forever; the missing bar
                # reads as a gap at the next one.
                await streams.acknowledge(self._client, entry.stream, GROUP, entry.entry_id)
                continue
            try:
                bar = _read_bar(entry.stream, entry.delivery.envelope)
            except ValueError as exc:
                await streams.dead_letter(
                    self._client, entry.stream, GROUP, entry.delivery, str(exc)
                )
                continue
            out.append(BarDelivery(stream=entry.stream, delivery=entry.delivery, bar=bar))
        return sorted(out, key=lambda d: (bar_close(d.bar), -d.bar.timeframe.seconds))

    async def done(self, delivery: BarDelivery) -> None:
        await streams.acknowledge(self._client, delivery.stream, GROUP, delivery.delivery.entry_id)

    async def poison(self, delivery: BarDelivery, reason: str) -> None:
        await streams.dead_letter(self._client, delivery.stream, GROUP, delivery.delivery, reason)


async def deliver(
    loop: SignalLoop, source: RedisBarSource, delivery: BarDelivery
) -> BarReport | None:
    """Hand one bar to the loop, then acknowledge it - or dead-letter it."""
    try:
        report = await loop.on_bar(delivery.bar)
    except Exception as exc:
        # on_bar returns an outcome for everything it anticipates; this is a
        # defect. Retrying repeats it. Dead-lettered, the bar is missing, and
        # the next one on its series reads as a gap.
        log.exception("evaluating %s raised; dead-lettering it", delivery.bar.symbol)
        await source.poison(delivery, f"evaluating raised {type(exc).__name__}")
        return None
    await source.done(delivery)
    return report


async def run(loop: SignalLoop, source: RedisBarSource, stop: asyncio.Event) -> None:
    await source.start()
    while not stop.is_set():
        for delivery in await source.next_batch():
            await deliver(loop, source, delivery)


async def bars_since(
    client: aioredis.Redis, timeframes: Sequence[Timeframe], since: dt.datetime
) -> list[Bar]:
    """Every readable bar published to the streams at or after ``since``, in
    publication order. Entry ids are publication times, so this is a range read."""
    if since.tzinfo is None:
        raise ValueError("since must be timezone-aware")
    start = f"{int(since.timestamp() * 1000)}-0"
    bars: list[Bar] = []
    for timeframe in timeframes:
        name = keys.stream_bars(timeframe)
        for entry_id, fields in await client.xrange(name, min=start, max="+") or []:
            try:
                bars.append(_read_bar(name, Envelope.from_fields(cast("dict[str, str]", fields))))
            except (ValueError, TypeError) as exc:
                log.error("replay: skipping unreadable entry %s on %s: %s", entry_id, name, exc)
    return publication_order(bars)


#: The pending list is at most one batch per stream (``RedisBarSource.count``);
#: this bounds the read of it without being a limit anything reaches.
_MAX_PENDING_READ = 10_000


async def processed_since(
    client: aioredis.Redis, timeframes: Sequence[Timeframe], since: dt.datetime
) -> list[Bar]:
    """The bars at or after ``since`` that this consumer group has FINISHED
    with - delivered and acknowledged - in publication order.

    Not every bar on the stream (SIT-009, 3 Nov 2026). A restart's replay that
    folded everything also folded the bars the dead process had read and not
    yet acknowledged, and the ones nobody had been handed yet; the backlog
    then redelivered them as duplicates, so a bar in flight at the crash was
    never evaluated, even when the restart came well inside its evaluation
    window. A pending bar and an undelivered bar belong to the normal path,
    where each is evaluated - or refused as LATE - like any other.
    """
    if since.tzinfo is None:
        raise ValueError("since must be timezone-aware")
    start = f"{int(since.timestamp() * 1000)}-0"
    bars: list[Bar] = []
    for timeframe in timeframes:
        name = keys.stream_bars(timeframe)
        if not await client.exists(name):
            continue
        groups = await client.xinfo_groups(name)
        group = next((g for g in groups if g.get("name") == GROUP), None)
        if group is None:
            continue  # no reader has finished with anything on this stream
        last = str(group.get("last-delivered-id") or "0-0")
        if last == "0-0":
            continue
        pending = {
            str(row["message_id"])
            for row in await client.xpending_range(
                name, GROUP, min="-", max="+", count=_MAX_PENDING_READ
            )
        }
        for entry_id, fields in await client.xrange(name, min=start, max=last) or []:
            if str(entry_id) in pending:
                continue
            try:
                bars.append(_read_bar(name, Envelope.from_fields(cast("dict[str, str]", fields))))
            except (ValueError, TypeError) as exc:
                log.error("replay: skipping unreadable entry %s on %s: %s", entry_id, name, exc)
    return publication_order(bars)


async def replay_today(loop: SignalLoop, client: aioredis.Redis) -> dict[BarOutcome, int]:
    """Fold today's FINISHED bars back into a freshly warmed loop, evaluating none.

    Only what the group acknowledged (:func:`processed_since`): the bars the
    dead process was still holding, and the ones not yet delivered, come next
    through the normal path and are evaluated there. Returns how many bars
    ended each way; a GAP here means a bar is missing from the stream itself,
    and the symbol stays stale - correctly.
    """
    session_open, _ = loop.calendar.session_bounds(loop.plan.trade_date)
    counts: dict[BarOutcome, int] = {}
    for bar in await processed_since(client, loop.timeframes, session_open):
        outcome = (await loop.replay(bar)).outcome
        counts[outcome] = counts.get(outcome, 0) + 1
    log.info("replayed today's bars: %s", {k.value: v for k, v in counts.items()})
    return counts


__all__ = [
    "GROUP",
    "BarDelivery",
    "RedisBarSource",
    "bars_since",
    "deliver",
    "processed_since",
    "replay_today",
    "run",
]
