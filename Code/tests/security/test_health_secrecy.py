"""A heartbeat's detail never carries a credential into the audit (E20-S05).

The health roll-up's answer becomes the health gate's rejection detail, which
is written to ``decision_log`` - a sink the logging filter never sees
(REC-003). A service reporting its own status could put a connection string or
an exception message there. These probes feed real-shaped secrets through both
paths - the writer, and a heartbeat written by something else - and grep the
answer.

The fake secrets are assembled at runtime, as in ``test_log_redaction.py``:
written as one literal, gitleaks flags the test file itself.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

from algotrader.common.health import HeartbeatHealth, HeartbeatWriter, ServiceState, why_unhealthy
from algotrader.common.redis import keys

NOW = dt.datetime(2026, 8, 25, 5, 0, tzinfo=dt.UTC)
SECRET = "hunter2" + "Sup3rS3cret"  # assembled: see the module docstring

SHAPES = [
    "postgresql+asyncpg://trader:" + SECRET + "@timescaledb:5432/algotrader",
    "redis://:" + SECRET + "@redis:6379/0",
    "pass" + "word=" + SECRET,
    "api_" + "key=" + SECRET,
    '{"access_' + 'token": "' + SECRET + '"}',
]


class FakeRedis:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}

    async def set(self, key: str, value: str, **_: Any) -> bool:
        self.strings[key] = value
        return True

    async def mget(self, names: list[str]) -> list[str | None]:
        return [self.strings.get(n) for n in names]


@pytest.mark.parametrize("shape", SHAPES)
async def test_the_writer_stores_no_secret(shape: str) -> None:
    redis = FakeRedis()
    await HeartbeatWriter(redis, service="market-ingest").beat(
        ServiceState.DEGRADED, now=NOW, detail=f"cannot reach the store: {shape}"
    )
    assert SECRET not in redis.strings[keys.health("market-ingest")]


async def test_a_secret_past_the_cut_is_still_masked() -> None:
    """Redact first, then cut: a URI severed before its '@' would no longer
    match the pattern that masks its password."""
    redis = FakeRedis()
    detail = "x" * 160 + " " + SHAPES[0]
    await HeartbeatWriter(redis, service="market-ingest").beat(
        ServiceState.DEGRADED, now=NOW, detail=detail
    )
    stored = redis.strings[keys.health("market-ingest")]
    assert SECRET not in stored
    assert SECRET[:6] not in stored, "a severed password is still a leaked prefix"


@pytest.mark.parametrize("shape", SHAPES)
def test_a_foreign_heartbeat_does_not_carry_one_into_the_answer(shape: str) -> None:
    """Written by something other than HeartbeatWriter, so never redacted."""
    raw = json.dumps(
        {
            "service": "market-ingest",
            "state": "FAILED",
            "beat_at": NOW.isoformat(),
            "detail": shape[:200],
        }
    )
    reason = why_unhealthy("market-ingest", raw, now=NOW)
    assert reason is not None and reason.startswith("FAILED")
    assert SECRET not in reason and SECRET[:6] not in reason


async def test_the_roll_up_answer_is_clean_end_to_end() -> None:
    redis = FakeRedis()
    await HeartbeatWriter(redis, service="notifier").beat(
        ServiceState.FAILED, now=NOW, detail=SHAPES[1]
    )
    answer = await HeartbeatHealth(redis).unhealthy_services(now=NOW)
    assert answer is not None
    assert all(SECRET not in s for s in answer)
    assert any(s.startswith("notifier (FAILED") for s in answer)
