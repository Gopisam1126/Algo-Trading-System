"""Service heartbeats and the health roll-up (E20-S05).

The health gate (check 2) refuses every entry while any service is named, and
refuses on an unknown answer. So the roll-up's job is narrow and unforgiving:
name every service that is not demonstrably READY right now, say why, and
never answer "all healthy" when it could not look.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from algotrader.common.health import (
    HEARTBEAT_TTL_SECONDS,
    MAX_BEAT_SKEW,
    TRADING_SERVICES,
    Heartbeat,
    HeartbeatHealth,
    HeartbeatWriter,
    ServiceState,
    why_unhealthy,
)
from algotrader.common.redis import keys
from algotrader.common.redis.keys import UnsafeKeyError

NOW = dt.datetime(2026, 8, 25, 5, 0, tzinfo=dt.UTC)
COMPOSE = Path(__file__).resolve().parents[2] / "ops" / "docker-compose.yml"


class FakeRedis:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}
        self.mgets: list[list[str]] = []
        self.fail = False

    async def set(self, key: str, value: str, ex: int | None = None, **_: Any) -> bool:
        if self.fail:
            raise ConnectionError("redis down")
        self.strings[key] = value
        self.ttls[key] = ex
        return True

    async def mget(self, names: list[str]) -> list[str | None]:
        if self.fail:
            raise ConnectionError("redis down")
        self.mgets.append(list(names))
        return [self.strings.get(n) for n in names]


def _raw(service: str = "notifier", **fields: Any) -> str:
    beat = {"service": service, "state": "READY", "beat_at": NOW.isoformat()}
    beat.update(fields)
    return json.dumps(beat)


async def _all_ready(redis: FakeRedis, services=TRADING_SERVICES, at=NOW) -> None:
    for service in services:
        await HeartbeatWriter(redis, service=service).beat(ServiceState.READY, now=at)


# ---------------------------------------------------------------------------
# why_unhealthy - the pure judgement
# ---------------------------------------------------------------------------


class TestWhyUnhealthy:
    def test_a_fresh_ready_heartbeat_is_healthy(self) -> None:
        assert why_unhealthy("notifier", _raw(), now=NOW) is None

    def test_no_heartbeat(self) -> None:
        assert why_unhealthy("notifier", None, now=NOW) == "no heartbeat"

    @pytest.mark.parametrize(
        "raw",
        [
            "not json",
            '{"service": "notifier"}',
            _raw(state="SLEEPING"),
            _raw(beat_at="2026-08-25T05:00:00"),  # naive
            _raw(extra="field"),
        ],
        ids=["garbage", "partial", "unknown-state", "naive-time", "extra-field"],
    )
    def test_an_unreadable_heartbeat(self, raw: str) -> None:
        assert why_unhealthy("notifier", raw, now=NOW) == "unreadable heartbeat"

    def test_a_heartbeat_that_names_another_service(self) -> None:
        assert "names api-server" in str(why_unhealthy("notifier", _raw("api-server"), now=NOW))

    def test_a_heartbeat_from_the_future(self) -> None:
        late = (NOW + MAX_BEAT_SKEW + dt.timedelta(seconds=1)).isoformat()
        assert why_unhealthy("notifier", _raw(beat_at=late), now=NOW) == (
            "heartbeat dated in the future"
        )
        within = (NOW + MAX_BEAT_SKEW).isoformat()
        assert why_unhealthy("notifier", _raw(beat_at=within), now=NOW) is None

    def test_a_heartbeat_older_than_the_ttl_is_stale_even_if_the_key_survived(self) -> None:
        """A heartbeat written without a TTL would never expire on its own."""
        at_limit = (NOW - dt.timedelta(seconds=HEARTBEAT_TTL_SECONDS)).isoformat()
        assert why_unhealthy("notifier", _raw(beat_at=at_limit), now=NOW) is None
        old = (NOW - dt.timedelta(seconds=HEARTBEAT_TTL_SECONDS + 1)).isoformat()
        assert why_unhealthy("notifier", _raw(beat_at=old), now=NOW) == "heartbeat 31s old"

    @pytest.mark.parametrize(
        "state", [ServiceState.STARTING, ServiceState.DEGRADED, ServiceState.FAILED]
    )
    def test_only_ready_is_healthy(self, state: ServiceState) -> None:
        assert why_unhealthy("notifier", _raw(state=state.value), now=NOW) == state.value

    def test_the_services_own_detail_is_carried_short(self) -> None:
        reason = why_unhealthy(
            "market-ingest",
            _raw("market-ingest", state="DEGRADED", detail="feed stale " * 15),  # 165 chars
            now=NOW,
        )
        assert reason is not None and reason.startswith("DEGRADED: feed stale")
        assert len(reason) <= len("DEGRADED: ") + 80

    def test_a_detail_longer_than_the_writer_allows_is_unreadable(self) -> None:
        """The writer caps detail at 200; a longer one was written by something
        else, and is not trusted as READY or as anything."""
        raw = _raw("market-ingest", detail="x" * 201)
        assert why_unhealthy("market-ingest", raw, now=NOW) == "unreadable heartbeat"

    @given(
        state=st.sampled_from(list(ServiceState)),
        age=st.integers(min_value=-60, max_value=120),
    )
    def test_healthy_exactly_when_ready_fresh_and_not_from_the_future(
        self, state: ServiceState, age: int
    ) -> None:
        beat_at = (NOW - dt.timedelta(seconds=age)).isoformat()
        healthy = why_unhealthy("notifier", _raw(state=state.value, beat_at=beat_at), now=NOW)
        expected = (
            state is ServiceState.READY
            and -age <= MAX_BEAT_SKEW.total_seconds()
            and age <= HEARTBEAT_TTL_SECONDS
        )
        assert (healthy is None) is expected


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------


class TestHeartbeatWriter:
    async def test_it_writes_the_health_key_with_the_ttl(self) -> None:
        redis = FakeRedis()
        beat = await HeartbeatWriter(redis, service="notifier").beat(ServiceState.READY, now=NOW)
        key = keys.health("notifier")
        assert key == "control:health:notifier"
        assert Heartbeat.model_validate_json(redis.strings[key]) == beat
        assert redis.ttls[key] == HEARTBEAT_TTL_SECONDS

    async def test_what_it_writes_the_reader_reads_as_healthy(self) -> None:
        redis = FakeRedis()
        await HeartbeatWriter(redis, service="notifier").beat(ServiceState.READY, now=NOW)
        assert why_unhealthy("notifier", redis.strings[keys.health("notifier")], now=NOW) is None

    async def test_a_detail_is_made_one_safe_line(self) -> None:
        redis = FakeRedis()
        beat = await HeartbeatWriter(redis, service="notifier").beat(
            ServiceState.DEGRADED, now=NOW, detail="line one\nline two\x00" + "x" * 500
        )
        assert beat.detail is not None
        assert "\n" not in beat.detail and "\x00" not in beat.detail
        assert len(beat.detail) <= 200

    def test_an_unsafe_service_name_is_refused_at_construction(self) -> None:
        with pytest.raises(UnsafeKeyError):
            HeartbeatWriter(FakeRedis(), service="notifier:x")

    @pytest.mark.parametrize("ttl", [0, HEARTBEAT_TTL_SECONDS + 1])
    def test_a_ttl_the_reader_would_misread_is_refused(self, ttl: int) -> None:
        with pytest.raises(ValueError, match="ttl_seconds"):
            HeartbeatWriter(FakeRedis(), service="notifier", ttl_seconds=ttl)


# ---------------------------------------------------------------------------
# The roll-up
# ---------------------------------------------------------------------------


class TestHeartbeatHealth:
    async def test_every_trading_service_ready_is_an_empty_answer(self) -> None:
        redis = FakeRedis()
        await _all_ready(redis)
        assert await HeartbeatHealth(redis).unhealthy_services(now=NOW) == ()

    async def test_nothing_heartbeating_names_everything(self) -> None:
        answer = await HeartbeatHealth(FakeRedis()).unhealthy_services(now=NOW)
        assert answer == tuple(f"{s} (no heartbeat)" for s in sorted(TRADING_SERVICES))

    async def test_one_service_down_is_named_with_why(self) -> None:
        redis = FakeRedis()
        await _all_ready(redis, TRADING_SERVICES - {"market-ingest"})
        await HeartbeatWriter(redis, service="market-ingest").beat(
            ServiceState.DEGRADED, now=NOW, detail="feed stale 75s"
        )
        assert await HeartbeatHealth(redis).unhealthy_services(now=NOW) == (
            "market-ingest (DEGRADED: feed stale 75s)",
        )

    async def test_a_heartbeat_that_has_aged_out_is_named(self) -> None:
        redis = FakeRedis()
        await _all_ready(redis)
        later = NOW + dt.timedelta(seconds=HEARTBEAT_TTL_SECONDS + 5)
        answer = await HeartbeatHealth(redis).unhealthy_services(now=later)
        assert answer is not None and len(answer) == len(TRADING_SERVICES)

    async def test_one_round_trip_for_every_service(self) -> None:
        redis = FakeRedis()
        await HeartbeatHealth(redis).unhealthy_services(now=NOW)
        assert redis.mgets == [[keys.health(s) for s in sorted(TRADING_SERVICES)]]

    async def test_a_failed_read_raises_never_answers_healthy(self) -> None:
        redis = FakeRedis()
        await _all_ready(redis)
        redis.fail = True
        with pytest.raises(ConnectionError):
            await HeartbeatHealth(redis).unhealthy_services(now=NOW)

    async def test_a_short_answer_from_redis_raises(self) -> None:
        class Short(FakeRedis):
            async def mget(self, names):
                return [None]

        with pytest.raises(RuntimeError, match="answers"):
            await HeartbeatHealth(Short()).unhealthy_services(now=NOW)

    def test_the_roster_can_grow_and_cannot_shrink(self) -> None:
        """There is no parameter that removes a trading service."""
        assert set(HeartbeatHealth(FakeRedis()).services) == TRADING_SERVICES
        grown = HeartbeatHealth(FakeRedis(), also=["premarket-job"])
        assert set(grown.services) == TRADING_SERVICES | {"premarket-job"}

    async def test_an_added_service_is_gated_too(self) -> None:
        redis = FakeRedis()
        await _all_ready(redis)
        answer = await HeartbeatHealth(redis, also=["broker-proxy"]).unhealthy_services(now=NOW)
        assert answer == ("broker-proxy (no heartbeat)",)

    def test_an_unsafe_added_name_is_refused(self) -> None:
        with pytest.raises(UnsafeKeyError):
            HeartbeatHealth(FakeRedis(), also=["*"])


class TestTheRosterMatchesTheDeployment:
    def test_every_compose_service_is_gated_or_a_named_batch_job(self) -> None:
        """A service added to docker-compose and not to TRADING_SERVICES would
        be able to die without blocking a single entry. This makes that a red
        test instead of a sentence in a docstring."""
        text = COMPOSE.read_text(encoding="utf-8")
        services = set(
            re.findall(
                r"^  ([a-z][a-z-]+):\n(?:    .*\n)*?    command: python -m algotrader", text, re.M
            )
        )
        assert services, "no algotrader services found in docker-compose"
        batch = {"premarket-job", "strategy-svc"}
        assert services == TRADING_SERVICES | batch
