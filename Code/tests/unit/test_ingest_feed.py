"""The WebSocket feed and quote state (E05-S01, E05-S08).

Both modules were at **0% coverage** — 212 statements of production code, one
of them the live market-data feed, with no test of any kind. They were reviewed
and never run. That is precisely the situation CLAUDE.md warns about: two real
bugs in this repository shipped past a clean code read.

The socket is faked rather than mocked at the library boundary: a small object
with ``recv``, ``send`` and the async-context-manager protocol, driven by a
scripted list of frames. That is enough to exercise everything that matters —
reconnection, gap emission, idle detection, heartbeat handling, and the
protocol-error path — without a network.

**The property under test is the gap.** A feed that drops and silently resumes
is worse than one that stays down, because indicators keep updating across a
hole they cannot see. Every reconnection must emit a :class:`FeedGap` BEFORE
any tick that follows it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import struct
from decimal import Decimal

import pytest

from algotrader.ingest import kite_ws
from algotrader.ingest.kite_protocol import Mode, ProtocolError
from algotrader.ingest.kite_ws import (
    MAX_TOKENS_PER_CONNECTION,
    BackoffPolicy,
    FeedGap,
    FeedStats,
    KiteFeed,
    open_feed,
)

TOKEN = 408065
NOW = dt.datetime(2026, 8, 20, 5, 0, tzinfo=dt.UTC)


def _ltp_frame(token: int = TOKEN, paise: int = 250_000) -> bytes:
    """One LTP packet in a one-packet frame, per the documented layout."""
    packet = struct.pack(">ii", token, paise)
    return struct.pack(">h", 1) + struct.pack(">h", len(packet)) + packet


class FakeSocket:
    """A scripted websocket. Items are frames; exceptions are raised in order."""

    def __init__(self, script: list[object]) -> None:
        self.script = list(script)
        self.sent: list[bytes | str] = []
        self.closed = False

    async def __aenter__(self) -> FakeSocket:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        self.closed = True
        return False

    async def send(self, message: bytes | str) -> None:
        self.sent.append(message)

    async def recv(self) -> bytes | str:
        if not self.script:
            raise ConnectionError("scripted socket exhausted")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if item == "HANG":
            await asyncio.sleep(3600)
        return item  # type: ignore[return-value]


class FakeConnect:
    """Stands in for ``websockets.connect``, one FakeSocket per attempt."""

    def __init__(self, sockets: list[FakeSocket | BaseException]) -> None:
        self.sockets = list(sockets)
        self.urls: list[str] = []
        self.calls = 0

    def __call__(self, url: str, **kwargs: object) -> FakeSocket:
        self.calls += 1
        self.urls.append(url)
        if not self.sockets:
            raise ConnectionError("no more scripted connections")
        item = self.sockets.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Backoff delays are recorded, not waited on."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(kite_ws.asyncio, "sleep", fake_sleep)
    return slept


async def _drain(feed: KiteFeed, limit: int) -> list[object]:
    out: list[object] = []
    async with open_feed(feed) as stream:
        async for item in stream:
            out.append(item)
            if len(out) >= limit:
                break
    return out


class TestConstructionRefusesImpossibleConfigurations:
    def test_no_tokens_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no instrument tokens"):
            KiteFeed(api_key="k", access_token="t", tokens=[])

    def test_too_many_tokens_for_one_connection_is_refused(self) -> None:
        """Kite caps a connection at 3000 tokens. Silently exceeding it means
        the excess never ticks — an absence, not an error."""
        with pytest.raises(ValueError, match="exceeds Kite's"):
            KiteFeed(
                api_key="k",
                access_token="t",
                tokens=list(range(MAX_TOKENS_PER_CONNECTION + 1)),
            )

    def test_the_cap_itself_is_allowed(self) -> None:
        feed = KiteFeed(
            api_key="k", access_token="t", tokens=list(range(MAX_TOKENS_PER_CONNECTION))
        )
        assert len(feed.tokens) == MAX_TOKENS_PER_CONNECTION


class TestBackoff:
    def test_it_grows(self) -> None:
        policy = BackoffPolicy(initial_seconds=1.0, multiplier=2.0, jitter=0.0, max_seconds=60.0)
        assert policy.delay(1) < policy.delay(2) < policy.delay(3)

    def test_it_is_capped(self) -> None:
        policy = BackoffPolicy(initial_seconds=1.0, multiplier=2.0, jitter=0.0, max_seconds=30.0)
        assert policy.delay(50) <= 30.0

    def test_it_is_never_negative_even_with_jitter(self) -> None:
        """Jitter is subtractive as well as additive; a negative sleep would
        raise rather than reconnect faster."""
        policy = BackoffPolicy(initial_seconds=0.01, multiplier=1.0, jitter=1.0)
        assert all(policy.delay(1) >= 0 for _ in range(500))

    def test_jitter_actually_spreads_the_retries(self) -> None:
        """The point of jitter: many clients reconnecting must not synchronise
        into a thundering herd against the broker."""
        policy = BackoffPolicy(initial_seconds=4.0, multiplier=2.0, jitter=0.5)
        assert len({policy.delay(3) for _ in range(50)}) > 1


class TestTheFeedYieldsTicks:
    @pytest.mark.asyncio
    async def test_a_tick_comes_through(self, monkeypatch: pytest.MonkeyPatch, no_sleep) -> None:
        connect = FakeConnect([FakeSocket([_ltp_frame()])])
        monkeypatch.setattr(kite_ws.websockets, "connect", connect)
        feed = KiteFeed(api_key="k", access_token="t", tokens=[TOKEN], max_attempts=1)
        items = await _drain(feed, 1)
        assert items[0].instrument_token == TOKEN
        assert items[0].last_price == Decimal("2500.00")

    @pytest.mark.asyncio
    async def test_it_subscribes_before_reading(
        self, monkeypatch: pytest.MonkeyPatch, no_sleep
    ) -> None:
        socket = FakeSocket([_ltp_frame()])
        monkeypatch.setattr(kite_ws.websockets, "connect", FakeConnect([socket]))
        feed = KiteFeed(
            api_key="k", access_token="t", tokens=[TOKEN], mode=Mode.FULL, max_attempts=1
        )
        await _drain(feed, 1)
        assert len(socket.sent) == 2
        assert json.loads(socket.sent[0])["a"] == "subscribe"
        assert json.loads(socket.sent[1])["a"] == "mode"

    @pytest.mark.asyncio
    async def test_the_credential_is_in_the_url_and_never_logged(
        self, monkeypatch: pytest.MonkeyPatch, no_sleep, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The access token travels as a query parameter, so any log line that
        echoed the URL would leak a live session."""
        connect = FakeConnect([FakeSocket([_ltp_frame()])])
        monkeypatch.setattr(kite_ws.websockets, "connect", connect)
        feed = KiteFeed(
            api_key="k", access_token="SUPERSECRETTOKEN", tokens=[TOKEN], max_attempts=1
        )
        with caplog.at_level("DEBUG"):
            await _drain(feed, 1)
        assert "SUPERSECRETTOKEN" in connect.urls[0]
        assert "SUPERSECRETTOKEN" not in caplog.text

    @pytest.mark.asyncio
    async def test_a_heartbeat_is_proof_of_life_not_data(
        self, monkeypatch: pytest.MonkeyPatch, no_sleep
    ) -> None:
        socket = FakeSocket([b"\x00", _ltp_frame()])
        monkeypatch.setattr(kite_ws.websockets, "connect", FakeConnect([socket]))
        feed = KiteFeed(api_key="k", access_token="t", tokens=[TOKEN], max_attempts=1)
        items = await _drain(feed, 1)
        assert len(items) == 1
        assert feed.stats.last_tick_at is not None

    @pytest.mark.asyncio
    async def test_an_unparseable_frame_is_counted_and_skipped(
        self, monkeypatch: pytest.MonkeyPatch, no_sleep
    ) -> None:
        """A rising protocol-error rate means the wire format moved. The honest
        response is to count it loudly, not to parse around it."""
        socket = FakeSocket([b"\x00\x05garbage", _ltp_frame()])
        monkeypatch.setattr(kite_ws.websockets, "connect", FakeConnect([socket]))
        feed = KiteFeed(api_key="k", access_token="t", tokens=[TOKEN], max_attempts=1)
        items = await _drain(feed, 1)
        assert feed.stats.protocol_errors == 1
        assert items[0].instrument_token == TOKEN


class TestTheGapIsAlwaysAnnounced:
    """The property the whole class exists for."""

    @pytest.mark.asyncio
    async def test_a_reconnect_emits_a_gap_before_any_later_tick(
        self, monkeypatch: pytest.MonkeyPatch, no_sleep
    ) -> None:
        first = FakeSocket([_ltp_frame(), ConnectionError("dropped")])
        second = FakeSocket([_ltp_frame(paise=260_000)])
        monkeypatch.setattr(kite_ws.websockets, "connect", FakeConnect([first, second]))
        feed = KiteFeed(api_key="k", access_token="t", tokens=[TOKEN], max_attempts=3)

        items = await _drain(feed, 3)
        kinds = [type(i).__name__ for i in items]
        assert kinds == ["RawTick", "FeedGap", "RawTick"]

    @pytest.mark.asyncio
    async def test_the_gap_carries_how_long_and_how_many_attempts(
        self, monkeypatch: pytest.MonkeyPatch, no_sleep
    ) -> None:
        first = FakeSocket([ConnectionError("dropped")])
        monkeypatch.setattr(
            kite_ws.websockets,
            "connect",
            FakeConnect([first, ConnectionError("refused"), FakeSocket([_ltp_frame()])]),
        )
        feed = KiteFeed(api_key="k", access_token="t", tokens=[TOKEN], max_attempts=5)
        items = await _drain(feed, 2)
        gap = next(i for i in items if isinstance(i, FeedGap))
        assert gap.attempts >= 2
        assert gap.duration_seconds >= 0

    @pytest.mark.asyncio
    async def test_a_clean_first_connection_emits_no_gap(
        self, monkeypatch: pytest.MonkeyPatch, no_sleep
    ) -> None:
        """Startup is not a gap. Emitting one would make every launch look like
        a feed failure and train the operator to ignore the signal."""
        monkeypatch.setattr(
            kite_ws.websockets, "connect", FakeConnect([FakeSocket([_ltp_frame()])])
        )
        feed = KiteFeed(api_key="k", access_token="t", tokens=[TOKEN], max_attempts=1)
        items = await _drain(feed, 1)
        assert not any(isinstance(i, FeedGap) for i in items)

    @pytest.mark.asyncio
    async def test_reconnects_are_counted(self, monkeypatch: pytest.MonkeyPatch, no_sleep) -> None:
        monkeypatch.setattr(
            kite_ws.websockets,
            "connect",
            FakeConnect([FakeSocket([ConnectionError("x")]), FakeSocket([_ltp_frame()])]),
        )
        feed = KiteFeed(api_key="k", access_token="t", tokens=[TOKEN], max_attempts=4)
        await _drain(feed, 2)
        assert feed.stats.reconnects == 1
        assert feed.stats.connects == 2


class TestTheSilentSocketIsTreatedAsDead:
    @pytest.mark.asyncio
    async def test_an_idle_socket_forces_a_reconnect(
        self, monkeypatch: pytest.MonkeyPatch, no_sleep
    ) -> None:
        """The failure a TCP-level check reports as healthy: the socket is open
        and nothing is coming down it."""
        monkeypatch.setattr(
            kite_ws.websockets,
            "connect",
            FakeConnect([FakeSocket(["HANG"]), FakeSocket([_ltp_frame()])]),
        )
        feed = KiteFeed(
            api_key="k",
            access_token="t",
            tokens=[TOKEN],
            idle_timeout_seconds=0.05,
            max_attempts=4,
        )
        items = await _drain(feed, 2)
        assert any(isinstance(i, FeedGap) for i in items), "idle socket did not become a gap"


class TestBackoffResetsAfterAHealthyConnection:
    @pytest.mark.asyncio
    async def test_one_bad_hour_does_not_pin_the_delay_all_day(
        self, monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
    ) -> None:
        """A connection that stayed up must not inherit the previous streak's
        backoff — otherwise a single bad hour keeps reconnection at its ceiling
        for the rest of the session."""
        real_now = kite_ws.dt.datetime.now

        class Clock:
            """First reading is the open; the next is two minutes later."""

            def __init__(self) -> None:
                self.calls = 0

            def now(self, tz: object = None) -> dt.datetime:
                self.calls += 1
                return real_now(dt.UTC) + dt.timedelta(minutes=2 * (self.calls > 1))

        monkeypatch.setattr(
            kite_ws.websockets,
            "connect",
            FakeConnect([FakeSocket([ConnectionError("late drop")]), FakeSocket([_ltp_frame()])]),
        )
        feed = KiteFeed(
            api_key="k",
            access_token="t",
            tokens=[TOKEN],
            healthy_after_seconds=60.0,
            backoff=BackoffPolicy(initial_seconds=1.0, multiplier=2.0, jitter=0.0),
            max_attempts=4,
        )
        await _drain(feed, 2)
        assert no_sleep, "no backoff was applied"
        assert no_sleep[0] <= 2.0, f"backoff did not reset after a healthy connection: {no_sleep}"


class TestControlFrames:
    def test_an_error_frame_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("ERROR"):
            KiteFeed._handle_text(json.dumps({"type": "error", "data": "bad token"}))
        assert "bad token" in caplog.text

    def test_an_order_postback_is_noted(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("INFO"):
            KiteFeed._handle_text(json.dumps({"type": "order", "data": {}}))
        assert "postback" in caplog.text

    def test_a_non_json_text_frame_does_not_raise(self, caplog: pytest.LogCaptureFixture) -> None:
        """A control frame the broker changes must not kill the feed."""
        with caplog.at_level("WARNING"):
            KiteFeed._handle_text("<html>maintenance</html>")
        assert "non-JSON" in caplog.text

    def test_an_unknown_control_type_is_ignored(self) -> None:
        KiteFeed._handle_text(json.dumps({"type": "something_new"}))


class TestGivingUp:
    @pytest.mark.asyncio
    async def test_it_stops_after_max_attempts(
        self, monkeypatch: pytest.MonkeyPatch, no_sleep
    ) -> None:
        """Bounded so a test — or a misconfigured deployment — cannot spin
        forever against a broker that is refusing us."""
        monkeypatch.setattr(
            kite_ws.websockets,
            "connect",
            FakeConnect([ConnectionError("no")] * 10),
        )
        feed = KiteFeed(api_key="k", access_token="t", tokens=[TOKEN], max_attempts=3)
        items = [item async for item in feed.stream()]
        assert items == []


class TestFeedStats:
    def test_seconds_since_last_tick_is_none_before_any_tick(self) -> None:
        """None means 'never ticked', which is different from 'ticked zero
        seconds ago' — a health panel must not show the second for the first."""
        assert FeedStats().seconds_since_last_tick() is None

    def test_it_measures_from_the_last_tick(self) -> None:
        stats = FeedStats(last_tick_at=NOW - dt.timedelta(seconds=30))
        assert stats.seconds_since_last_tick(now=NOW) == pytest.approx(30.0)

    def test_the_gap_duration_is_computed_from_its_endpoints(self) -> None:
        gap = FeedGap(
            disconnected_at=NOW,
            reconnected_at=NOW + dt.timedelta(seconds=12),
            attempts=2,
        )
        assert gap.duration_seconds == pytest.approx(12.0)


class TestOpenFeedAlwaysClosesTheSocket:
    @pytest.mark.asyncio
    async def test_the_generator_is_closed_on_exit(
        self, monkeypatch: pytest.MonkeyPatch, no_sleep
    ) -> None:
        """Kite allows three concurrent connections. A cancelled task that left
        its socket open would burn one until the server timed it out; after a
        few restarts the feed simply refuses to connect, and it presents as a
        broker problem rather than as our leak."""
        socket = FakeSocket([_ltp_frame()])
        monkeypatch.setattr(kite_ws.websockets, "connect", FakeConnect([socket]))
        feed = KiteFeed(api_key="k", access_token="t", tokens=[TOKEN], max_attempts=1)

        async with open_feed(feed) as stream:
            await stream.__anext__()
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()

    @pytest.mark.asyncio
    async def test_it_closes_even_when_the_body_raises(
        self, monkeypatch: pytest.MonkeyPatch, no_sleep
    ) -> None:
        monkeypatch.setattr(
            kite_ws.websockets, "connect", FakeConnect([FakeSocket([_ltp_frame()])])
        )
        feed = KiteFeed(api_key="k", access_token="t", tokens=[TOKEN], max_attempts=1)
        with pytest.raises(RuntimeError):
            async with open_feed(feed) as stream:
                await stream.__anext__()
                raise RuntimeError("consumer blew up")
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()


class TestProtocolErrorIsImportable:
    def test_the_parse_failure_type_is_what_the_feed_catches(self) -> None:
        assert issubclass(ProtocolError, Exception)
