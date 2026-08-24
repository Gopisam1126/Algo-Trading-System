"""Kite WebSocket client (E05-S01) — connection, reconnection, and the gap signal.

Built on ``websockets`` rather than ``KiteTicker``. See ``kite_protocol`` for
why: it keeps a Twisted reactor out of an asyncio process. It does NOT remove
autobahn from the process — importing ``kiteconnect`` loads it regardless — and
that CVE was closed by upgrading the package instead.

**The reconnect is the story, not the connect.** A feed that drops and silently
resumes is worse than one that stays down, because the indicators keep updating
across a hole they cannot see. Every reconnection therefore emits a
:class:`FeedGap` *before* any tick that follows it, and the consumer is expected
to mark indicator state stale rather than carry on.

Two failure modes this is shaped around:

- **Connection leak on reconnect.** Kite caps concurrent connections (three).
  Reconnecting without closing the old socket exhausts that cap after a few
  drops, and the feed then stays down for the rest of the session. Every
  connection is opened inside ``async with``, so the previous one is closed
  before the next is attempted.
- **A reconnect storm.** Backoff is exponential and capped, and a connection
  that survives long enough to be useful resets it — otherwise a single bad
  hour leaves the backoff pinned at its maximum for the rest of the day.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import random
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field

import websockets

from algotrader.ingest.kite_protocol import (
    Mode,
    ProtocolError,
    RawTick,
    mode_message,
    parse_frame,
    stream_url,
    subscribe_message,
)

log = logging.getLogger(__name__)

KITE_WS_URL = "wss://ws.kite.trade"

#: Kite permits three concurrent WebSocket connections per api_key.
MAX_CONCURRENT_CONNECTIONS = 3

#: Instruments per connection. A Nifty 200 universe fits comfortably; this
#: exists so exceeding it fails here rather than as a silent partial
#: subscription in which some symbols simply never tick.
MAX_TOKENS_PER_CONNECTION = 3000


@dataclass(frozen=True, slots=True)
class FeedGap:
    """Emitted when the stream was interrupted. Consumers must act on it.

    Carries the outage window so downstream can decide what to invalidate. It
    is a value rather than a callback because it travels the same channel as
    the ticks — ordering between "the gap" and "the ticks after it" is then a
    property of the stream rather than of two racing notifications.
    """

    disconnected_at: dt.datetime
    reconnected_at: dt.datetime
    attempts: int

    @property
    def duration_seconds(self) -> float:
        return (self.reconnected_at - self.disconnected_at).total_seconds()


@dataclass
class BackoffPolicy:
    """Exponential backoff with jitter and a ceiling."""

    initial_seconds: float = 1.0
    max_seconds: float = 30.0
    multiplier: float = 2.0
    #: Jitter stops every reconnecting client in a fleet retrying in lockstep.
    #: There is one client here, but the exchange sees many, and a thundering
    #: herd at 09:15 is a real way to be rate-limited on reconnect.
    jitter: float = 0.25

    def delay(self, attempt: int) -> float:
        raw = min(self.initial_seconds * (self.multiplier ** max(0, attempt - 1)), self.max_seconds)
        spread = raw * self.jitter
        return max(0.0, raw + random.uniform(-spread, spread))  # noqa: S311 - not cryptographic


@dataclass
class FeedStats:
    """What the health gate reads. Cheap to keep, expensive to lack."""

    connects: int = 0
    reconnects: int = 0
    frames: int = 0
    ticks: int = 0
    protocol_errors: int = 0
    last_tick_at: dt.datetime | None = None
    connected_since: dt.datetime | None = None

    def seconds_since_last_tick(self, *, now: dt.datetime | None = None) -> float | None:
        if self.last_tick_at is None:
            return None
        return ((now or dt.datetime.now(dt.UTC)) - self.last_tick_at).total_seconds()


@dataclass
class KiteFeed:
    """Streams ticks, and says so when it could not.

    ``stream()`` yields ``RawTick`` and ``FeedGap`` on one channel. A consumer
    that ignores ``FeedGap`` gets ticks across a hole with no indication — which
    is exactly the failure this class exists to make impossible to miss.
    """

    api_key: str
    access_token: str
    tokens: list[int]
    mode: Mode = Mode.FULL
    url: str = KITE_WS_URL
    backoff: BackoffPolicy = field(default_factory=BackoffPolicy)
    #: A connection alive at least this long is considered good, and resets the
    #: backoff. Without it, one bad hour pins the delay at its ceiling all day.
    healthy_after_seconds: float = 60.0
    #: No frame for this long means the connection is dead even though the
    #: socket is open — the case a TCP-level check never notices.
    idle_timeout_seconds: float = 45.0
    max_attempts: int | None = None
    stats: FeedStats = field(default_factory=FeedStats)

    def __post_init__(self) -> None:
        if not self.tokens:
            raise ValueError("no instrument tokens to subscribe to")
        if len(self.tokens) > MAX_TOKENS_PER_CONNECTION:
            raise ValueError(
                f"{len(self.tokens)} tokens exceeds Kite's {MAX_TOKENS_PER_CONNECTION} per "
                f"connection. Split across connections (max "
                f"{MAX_CONCURRENT_CONNECTIONS}) rather than letting the excess silently "
                f"never tick."
            )

    # -- the stream ----------------------------------------------------------

    async def stream(self) -> AsyncGenerator[RawTick | FeedGap, None]:
        """Yield ticks forever, with a :class:`FeedGap` after every interruption."""
        attempt = 0
        disconnected_at: dt.datetime | None = None

        while True:
            if self.max_attempts is not None and attempt >= self.max_attempts:
                log.error("giving up after %d connection attempts", attempt)
                return

            attempt += 1
            opened_at: dt.datetime | None = None
            try:
                async with websockets.connect(
                    stream_url(self.url, self.api_key, self.access_token),
                    open_timeout=10,
                    close_timeout=5,
                    ping_interval=20,
                    ping_timeout=10,
                    max_size=2**22,
                ) as socket:
                    opened_at = dt.datetime.now(dt.UTC)
                    self.stats.connects += 1
                    self.stats.connected_since = opened_at
                    # The URL carries the credential, so it must never be logged.
                    log.info("market feed connected; subscribing to %d tokens", len(self.tokens))

                    await socket.send(subscribe_message(self.tokens))
                    await socket.send(mode_message(self.mode, self.tokens))

                    if disconnected_at is not None:
                        gap = FeedGap(
                            disconnected_at=disconnected_at,
                            reconnected_at=opened_at,
                            attempts=attempt,
                        )
                        self.stats.reconnects += 1
                        log.warning(
                            "feed resumed after %.1fs down (%d attempts) — downstream must "
                            "mark indicator state STALE rather than continue",
                            gap.duration_seconds,
                            attempt,
                        )
                        yield gap
                        disconnected_at = None

                    attempt = 0
                    async for item in self._read(socket):
                        yield item

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Any failure is a disconnection. The socket is already closed by
                # the context manager, so the connection cannot leak into the
                # next attempt and eat the concurrent-connection cap.
                if disconnected_at is None:
                    disconnected_at = dt.datetime.now(dt.UTC)
                self.stats.connected_since = None

                if opened_at is not None:
                    alive = (dt.datetime.now(dt.UTC) - opened_at).total_seconds()
                    if alive >= self.healthy_after_seconds:
                        attempt = 1  # the connection was good; do not inherit old backoff
                delay = self.backoff.delay(attempt)
                log.warning(
                    "market feed dropped (%s: %s); reconnecting in %.1fs",
                    type(exc).__name__,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)

    async def _read(self, socket: object) -> AsyncIterator[RawTick | FeedGap]:
        """Read frames until the socket goes quiet or fails.

        A silent socket is treated as dead. Kite sends a heartbeat, so a gap
        longer than ``idle_timeout_seconds`` means the connection is open at the
        TCP level and useless at the application level — the failure a
        connectivity check reports as healthy.
        """
        while True:
            try:
                message = await asyncio.wait_for(
                    socket.recv(),  # type: ignore[attr-defined]
                    timeout=self.idle_timeout_seconds,
                )
            except TimeoutError:
                raise ConnectionError(
                    f"no frame for {self.idle_timeout_seconds:.0f}s; treating the connection "
                    f"as dead even though the socket is open"
                ) from None

            if isinstance(message, str):
                self._handle_text(message)
                continue

            if len(message) <= 2:
                # Kite's heartbeat: a one-byte frame. Proof of life, not data.
                self.stats.last_tick_at = dt.datetime.now(dt.UTC)
                continue

            self.stats.frames += 1
            try:
                ticks = parse_frame(message)
            except ProtocolError as exc:
                # Loud, and counted. A rising rate means the protocol moved, and
                # the honest response is to stop trusting the feed rather than
                # to parse around it.
                self.stats.protocol_errors += 1
                log.error("unparseable frame (%d bytes): %s", len(message), exc)
                continue

            self.stats.ticks += len(ticks)
            self.stats.last_tick_at = dt.datetime.now(dt.UTC)
            for tick in ticks:
                yield tick

    @staticmethod
    def _handle_text(message: str) -> None:
        """Text frames are JSON control messages: errors and order postbacks."""
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            log.warning("non-JSON text frame from the feed: %.120s", message)
            return
        kind = payload.get("type")
        if kind == "error":
            log.error("broker feed reported an error: %s", payload.get("data"))
        elif kind == "order":
            log.info("order postback received on the market feed")
        else:
            log.debug("feed control message: %s", kind)


@contextlib.asynccontextmanager
async def open_feed(
    feed: KiteFeed,
) -> AsyncIterator[AsyncGenerator[RawTick | FeedGap, None]]:
    """Bracket the stream so cancellation always closes the socket.

    Kite allows three concurrent connections; a task cancelled without closing
    its socket burns one until the server times it out. After a few restarts
    the feed simply refuses to connect, which presents as a broker problem
    rather than as our own leak.

    Typed as ``AsyncGenerator`` rather than ``AsyncIterator`` on purpose: only a
    generator has ``aclose()``, and that method is the entire point of this
    wrapper.
    """
    generator: AsyncGenerator[RawTick | FeedGap, None] = feed.stream()
    try:
        yield generator
    finally:
        await generator.aclose()
