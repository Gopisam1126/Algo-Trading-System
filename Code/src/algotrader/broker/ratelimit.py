"""Broker rate limiting (E02-S05).

Zerodha enforces **10 orders/sec account-wide** — confirmed on their developer
forum: *"10 OPS is account-specific, regardless of the number of apps."* That
has two consequences the naive design misses.

First, the bucket key is account-scoped, not app-scoped or process-scoped, so
every service shares one Redis key. Second, the budget is shared with anything
else placing orders on the same account — including a human tapping the Kite
mobile app. Running at 3/sec against a cap of 10 leaves that headroom
deliberately.

**Refusal, not delay.** When the bucket is empty the caller is rejected. Queuing
an order until capacity frees up sounds kinder and is worse: the market has
moved, the signal that justified the order has expired, and the position gets
opened against conditions that no longer hold. A rejected signal is a missed
trade; a delayed order is a trade taken on stale reasoning.

Orders and data have separate buckets so a historical backfill cannot starve an
exit order.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import redis.asyncio as aioredis

from algotrader.common.redis import keys
from algotrader.common.redis.primitives import take_token

log = logging.getLogger(__name__)

#: SEBI's registration threshold. Above this a strategy must be registered with
#: the exchange, so the system stays below it by construction, not by config.
SEBI_ORDER_RATE_THRESHOLD = 10


class RateLimitExceededError(RuntimeError):
    """The caller must surface this, not swallow and retry.

    Retrying inside the limiter would turn backpressure into a busy-wait and
    hide the condition from the metric that is supposed to reveal it.
    """

    def __init__(self, what: str, *, tokens_left: float) -> None:
        super().__init__(
            f"{what} rate limit reached; refused rather than queued. "
            f"{tokens_left:.2f} tokens remaining."
        )
        self.tokens_left = tokens_left


@dataclass(frozen=True, slots=True)
class RateLimitConfig:
    """Effective limits. Both must stay under what the broker permits."""

    orders_per_second: int = 3
    order_burst: int = 3
    data_per_second: int = 3
    data_burst: int = 10

    def __post_init__(self) -> None:
        if self.orders_per_second >= SEBI_ORDER_RATE_THRESHOLD:
            raise ValueError(
                f"orders_per_second={self.orders_per_second} is at or above SEBI's "
                f"{SEBI_ORDER_RATE_THRESHOLD}/sec registration threshold. Staying "
                f"below it is what keeps this a self-developed personal algo."
            )
        if self.order_burst < 1 or self.data_burst < 1:
            raise ValueError("burst capacity must be at least 1")


class BrokerRateLimiter:
    """Gate in front of every broker call. Account-wide, Redis-backed."""

    def __init__(self, client: aioredis.Redis, config: RateLimitConfig | None = None) -> None:
        self._client = client
        self._config = config or RateLimitConfig()

    @property
    def config(self) -> RateLimitConfig:
        return self._config

    async def acquire_order(self) -> float:
        """Take one order token or raise. Returns tokens remaining."""
        allowed, left = await take_token(
            self._client,
            keys.order_rate_limit(),
            capacity=self._config.order_burst,
            refill_per_second=float(self._config.orders_per_second),
        )
        if not allowed:
            log.warning("order refused by rate limiter; %.2f tokens left", left)
            raise RateLimitExceededError("order", tokens_left=left)
        return left

    async def acquire_data(self) -> float:
        """Take one data token or raise. Separate budget from orders."""
        allowed, left = await take_token(
            self._client,
            keys.data_rate_limit(),
            capacity=self._config.data_burst,
            refill_per_second=float(self._config.data_per_second),
        )
        if not allowed:
            log.info("data call refused by rate limiter; %.2f tokens left", left)
            raise RateLimitExceededError("data", tokens_left=left)
        return left
