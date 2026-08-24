"""Redis access layer — keyspace, state, locks, rate limiting, timers.

Scope boundary: **no other module may build a Redis key string or construct a
client.** Keys come from :mod:`~algotrader.common.redis.keys`, clients from
:func:`~algotrader.common.redis.client.build_client`. A mistyped key does not
raise — it reads ``None``, which in this system reads as "no position", "no
plan", "kill switch not set". Centralising both is what makes that class of
bug impossible rather than merely unlikely.

Note on the package name: this shadows the third-party ``redis`` package by
name only. Python 3 uses absolute imports, so ``import redis.asyncio`` inside
these modules resolves to the installed library, not to this package.
"""

from algotrader.common.redis import keys
from algotrader.common.redis.client import (
    assert_noeviction,
    build_client,
    build_pool,
    close,
    ping,
)
from algotrader.common.redis.locks import (
    MAX_TTL_MS,
    LockError,
    acquire_lock,
    extend_lock,
    lock,
    release_lock,
)
from algotrader.common.redis.primitives import (
    RateLimiterConfigError,
    cancel,
    peek_next,
    pop_due,
    schedule,
    scheduled_count,
    take_token,
)
from algotrader.common.redis.state import (
    delete_state,
    exists,
    get_flag,
    get_many,
    get_state,
    is_kill_switch_active,
    set_flag,
    set_state,
)

__all__ = [
    "MAX_TTL_MS",
    "LockError",
    "RateLimiterConfigError",
    "acquire_lock",
    "assert_noeviction",
    "build_client",
    "build_pool",
    "cancel",
    "close",
    "delete_state",
    "exists",
    "extend_lock",
    "get_flag",
    "get_many",
    "get_state",
    "is_kill_switch_active",
    "keys",
    "lock",
    "peek_next",
    "ping",
    "pop_due",
    "release_lock",
    "schedule",
    "scheduled_count",
    "set_flag",
    "set_state",
    "take_token",
]
