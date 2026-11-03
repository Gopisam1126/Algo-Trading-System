"""Service heartbeats, and the health roll-up the risk engine gates on (E20-S05).

Every service writes ``control:health:{service}`` - a :class:`Heartbeat` as
JSON - every :data:`HEARTBEAT_INTERVAL_SECONDS`, with a
:data:`HEARTBEAT_TTL_SECONDS` expiry. :class:`HeartbeatHealth` reads them all
in one round trip and answers ``SessionFacts.unhealthy_services``: the risk
engine's health gate (check 2) refuses every entry while any name is in it,
and refuses on an unknown answer too.

## The design record

**The decision.** The roster of services that must be healthy for an entry is
:data:`TRADING_SERVICES`, in code - every service the architecture runs while
entries are possible (``LOW_LEVEL_ARCHITECTURE.md`` §2.2) - and a deployment
can ADD to it but never remove from it. A service is healthy only if its
heartbeat exists, parses, names itself, is not dated in the future, is no
older than the TTL, and says ``READY``.

**The alternative rejected.** A configured list of "critical" services. It
reads as flexibility and is a switch that turns the gate off one name at a
time: drop ``notifier`` from it and the system keeps opening positions with
nobody able to hear about a naked one; drop ``api-server`` and the phone kill
switch is gone while trading continues. Invariant 2 says hard bounds are code;
this is one. The risk check already refuses to choose a quorum
(``check_health_gate``), and the roster cannot reintroduce one beneath it.

**The failure it prevents.** New risk while a component is down - invariant 6.
Concretely: a market-ingest that died at 11:00 leaves indicator state ageing
and the last quote standing; an orchestrator that died takes the watchdog and
the order-rate kill switch with it. Either way the remaining services would
keep trading on a system that is no longer whole.

**What would make this wrong.** A service that legitimately stops during the
session (a batch job). ``premarket-job`` (05:30-09:15) and ``strategy-svc``
(weekends) are excluded for exactly that reason. A service added to the
architecture must be added here, or its death will not block entries - and
nothing but this file says so. And a paper session run with fewer services
than production would need its own, explicitly smaller roster: a business
decision, not something to configure.

## Why the age is checked as well as the TTL

The TTL is the liveness mechanism (``keys.health``): a dead service's key
expires. But a heartbeat written WITHOUT a TTL - by a bug, or by hand while
debugging - would read as alive forever. The reader checks ``beat_at`` against
its own clock, so that mistake reads as stale within 30 seconds instead.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable, Sequence
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from algotrader.common.logging import redact
from algotrader.common.redis import keys
from algotrader.common.redis import state as redis_state
from algotrader.common.text import one_safe_line

log = logging.getLogger(__name__)

__all__ = [
    "HEARTBEAT_INTERVAL_SECONDS",
    "HEARTBEAT_TTL_SECONDS",
    "TRADING_SERVICES",
    "Heartbeat",
    "HeartbeatHealth",
    "HeartbeatWriter",
    "ServiceState",
    "why_unhealthy",
]

#: The heartbeat key's expiry (``keys.health``). A service silent this long is down.
HEARTBEAT_TTL_SECONDS: Final = 30
#: How often a service beats: three chances inside one TTL, so one slow write
#: or one dropped connection does not read as a dead service.
HEARTBEAT_INTERVAL_SECONDS: Final = 10.0
#: A heartbeat dated further ahead than this is a clock disagreement between
#: hosts, which the architecture treats as a health problem in its own right.
MAX_BEAT_SKEW: Final = dt.timedelta(seconds=5)
#: How much of a heartbeat's own detail reaches the roll-up's answer, which
#: becomes part of an audit row.
_DETAIL_SHOWN: Final = 80

#: Every service that runs while entries are possible - ``LOW_LEVEL_ARCHITECTURE.md``
#: §2.2's "Runs" column, by docker-compose service name. ``premarket-job`` and
#: ``strategy-svc`` are batch work and absent on purpose. See the module docstring.
TRADING_SERVICES: Final = frozenset(
    {
        "orchestrator",
        "market-ingest",
        "ti-engine",
        "signal-engine",
        "macro-svc",
        "execution-svc",
        "api-server",
        "notifier",
    }
)


class ServiceState(StrEnum):
    """``LOW_LEVEL_ARCHITECTURE.md`` §13.3's four states. Only READY is healthy."""

    STARTING = "STARTING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


class Heartbeat(BaseModel):
    """What a service says about itself, and when it said it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    service: str = Field(min_length=1, max_length=64)
    state: ServiceState
    beat_at: dt.datetime
    detail: str | None = Field(default=None, max_length=200)

    @field_validator("beat_at")
    @classmethod
    def _aware(cls, v: dt.datetime) -> dt.datetime:
        if v.tzinfo is None:
            raise ValueError("beat_at must be timezone-aware")
        return v


class HeartbeatWriter:
    """One service's heartbeat. The key is validated at construction."""

    def __init__(
        self, redis: Any, *, service: str, ttl_seconds: int = HEARTBEAT_TTL_SECONDS
    ) -> None:
        if not 1 <= ttl_seconds <= HEARTBEAT_TTL_SECONDS:
            # Longer than the reader's bound would read as stale while alive;
            # the reader's bound is the TTL the architecture fixes.
            raise ValueError(
                f"ttl_seconds must be between 1 and {HEARTBEAT_TTL_SECONDS}, not {ttl_seconds}"
            )
        self._redis = redis
        self._key = keys.health(service)
        self._service = service
        self._ttl = ttl_seconds

    @property
    def service(self) -> str:
        return self._service

    async def beat(
        self, state: ServiceState, *, now: dt.datetime, detail: str | None = None
    ) -> Heartbeat:
        # Redacted BEFORE it is cut: truncating first can sever a connection
        # string before its '@', and the pattern that would mask it no longer
        # matches what is left.
        heartbeat = Heartbeat(
            service=self._service,
            state=state,
            beat_at=now,
            detail=one_safe_line(redact(detail))[:200] if detail else None,
        )
        await redis_state.set_state(self._redis, self._key, heartbeat, ttl_seconds=self._ttl)
        return heartbeat


def why_unhealthy(service: str, raw: str | None, *, now: dt.datetime) -> str | None:
    """Why ``service`` is not healthy, from its stored heartbeat - or ``None``.

    Pure, and in the order each question assumes the last: there is a
    heartbeat; it parses; it is this service's; it is not from the future; it
    is no older than the TTL; it says READY.
    """
    if raw is None:
        return "no heartbeat"
    try:
        beat = Heartbeat.model_validate_json(raw)
    except ValidationError:
        return "unreadable heartbeat"
    if beat.service != service:
        return f"the heartbeat names {one_safe_line(redact(beat.service))[:64]}"
    if beat.beat_at - now > MAX_BEAT_SKEW:
        return "heartbeat dated in the future"
    age = (now - beat.beat_at).total_seconds()
    if age > HEARTBEAT_TTL_SECONDS:
        return f"heartbeat {int(age)}s old"
    if beat.state is not ServiceState.READY:
        # Redacted again here: the answer becomes part of a decision_log row,
        # a sink the logging filter never sees, and the heartbeat may have been
        # written by something other than HeartbeatWriter.
        detail = one_safe_line(redact(beat.detail))[:_DETAIL_SHOWN] if beat.detail else ""
        return f"{beat.state.value}: {detail}" if detail else beat.state.value
    return None


class HeartbeatHealth:
    """``SessionFacts.unhealthy_services``: every required heartbeat, in one read.

    ``also`` adds services to :data:`TRADING_SERVICES`; nothing removes one.
    A failed read RAISES - the context builder turns that into ``None``, the
    unknown answer the health gate refuses on. It never becomes ``()``.
    """

    def __init__(self, redis: Any, *, also: Iterable[str] = ()) -> None:
        self._redis = redis
        self._services: tuple[str, ...] = tuple(sorted(TRADING_SERVICES | set(also)))
        self._keys = [keys.health(s) for s in self._services]  # validates every name

    @property
    def services(self) -> tuple[str, ...]:
        return self._services

    async def unhealthy_services(self, *, now: dt.datetime) -> tuple[str, ...] | None:
        raw: Sequence[str | None] = await self._redis.mget(self._keys)
        if len(raw) != len(self._services):
            raise RuntimeError(
                f"asked for {len(self._services)} heartbeats and got {len(raw)} answers"
            )
        out: list[str] = []
        for service, payload in zip(self._services, raw, strict=True):
            reason = why_unhealthy(service, payload, now=now)
            if reason is not None:
                out.append(f"{service} ({reason})")
        if out:
            log.warning("unhealthy: %s", "; ".join(out))
        return tuple(out)
