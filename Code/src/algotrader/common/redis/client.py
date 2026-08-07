"""Async Redis connection management.

One pool per process, built from :class:`RedisConfig`. Nothing else in the
codebase may construct a client — that keeps timeouts, retry policy and
decoding consistent, and means there is one place to change them.

**`decode_responses=True` is deliberate and load-bearing.** Every value this
system stores is either JSON from ``model_dump_json()`` or a short scalar, and
none of it is binary. Without it, every read returns ``bytes`` and every
comparison against a string silently fails — ``b"1" != "1"``, so a kill-switch
check would read as "not set" and trading would continue. That is the failure
mode this flag exists to prevent.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import redis.asyncio as aioredis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

if TYPE_CHECKING:
    from algotrader.common.config import RedisConfig

log = logging.getLogger(__name__)

#: Retried on connection-level failures only. A command that fails for a
#: semantic reason (wrong type, bad script) is a bug and must surface, not be
#: retried into a slower bug.
_RETRY_ON = (RedisConnectionError, RedisTimeoutError)


def build_pool(config: RedisConfig, password: str | None = None) -> aioredis.ConnectionPool:
    """Create the connection pool.

    The DSN is resolved by :meth:`RedisConfig.dsn`, so the ``REDIS_URL``
    override applies here exactly as it does everywhere else — containers reach
    ``redis`` by service name, tests reach an ephemeral container, and
    ``make doctor`` reports which source is in effect.
    """
    return aioredis.ConnectionPool.from_url(
        config.dsn(password),
        max_connections=config.max_connections,
        socket_timeout=config.socket_timeout_seconds,
        socket_connect_timeout=config.socket_connect_timeout_seconds,
        socket_keepalive=True,
        decode_responses=True,  # see module docstring — not cosmetic
        retry=Retry(ExponentialBackoff(base=0.05, cap=1.0), retries=3),
        retry_on_error=list(_RETRY_ON),
        health_check_interval=30,
    )


def build_client(config: RedisConfig, password: str | None = None) -> aioredis.Redis:
    """The single entry point for obtaining a Redis client."""
    return aioredis.Redis(connection_pool=build_pool(config, password))


async def ping(client: aioredis.Redis) -> bool:
    """True if Redis is reachable. Never raises — callers decide what down means."""
    try:
        return bool(await client.ping())
    except Exception:
        log.warning("redis ping failed", exc_info=True)
        return False


async def assert_noeviction(client: aioredis.Redis) -> None:
    """Fail loudly at startup if Redis is not configured ``noeviction``.

    This is a **safety check, not a preference.** Under any eviction policy,
    Redis silently discards keys when memory fills — and the keys it discards
    are chosen by the policy, not by importance. Losing ``state:position:*`` or
    ``control:killswitch`` while the system keeps trading is the worst failure
    this component can have, and it produces no error at the point of loss.

    ``noeviction`` converts that silent data loss into a loud write failure,
    which is the correct trade for trading state. The cost is that an untrimmed
    stream can fill memory and stop writes entirely — which is why ``maxlen`` is
    a required argument on stream publication (E01-S04).
    """
    settings: Any = await client.config_get("maxmemory-policy")
    policy = str(settings.get("maxmemory-policy", "unknown"))
    if policy != "noeviction":
        raise RuntimeError(
            f"Redis is running maxmemory-policy={policy!r}, but this system requires "
            f"'noeviction'. Under an eviction policy Redis will silently discard "
            f"trading state — positions, the kill switch — when memory fills, with no "
            f"error at the point of loss. Set it in ops/docker-compose.yml."
        )


async def close(client: aioredis.Redis) -> None:
    """Release the pool. Safe to call more than once."""
    try:
        await client.aclose()
    except Exception:  # pragma: no cover - shutdown path
        log.debug("error closing redis client", exc_info=True)
