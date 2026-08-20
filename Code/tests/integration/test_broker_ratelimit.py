"""Order rate limiting against real Redis (E02-S05).

Zerodha enforces 10 orders/sec **account-wide** — their developer forum is
explicit: *"10 OPS is account-specific, regardless of the number of apps."* So
the bucket must be shared across every process, which is why it lives in Redis
rather than in the adapter.

The behaviour that matters under load is the refusal. Queuing an order until
capacity frees up sounds kinder and is worse: the market has moved and the
signal that justified the order has expired, so the position gets opened
against conditions that no longer hold.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import redis.asyncio as aioredis

from algotrader.broker.ratelimit import (
    SEBI_ORDER_RATE_THRESHOLD,
    BrokerRateLimiter,
    RateLimitConfig,
    RateLimitExceededError,
)
from algotrader.common.redis import keys

pytestmark = [pytest.mark.integration]


@pytest.fixture
async def r(redis_url: str) -> AsyncIterator[aioredis.Redis]:
    client = aioredis.from_url(redis_url, decode_responses=True)
    await client.delete(keys.order_rate_limit(), keys.data_rate_limit())
    yield client
    await client.aclose()


class TestTheOrderBudgetIsEnforced:
    async def test_a_burst_beyond_capacity_is_refused(self, r: aioredis.Redis) -> None:
        limiter = BrokerRateLimiter(r, RateLimitConfig(orders_per_second=3, order_burst=3))
        for _ in range(3):
            await limiter.acquire_order()
        with pytest.raises(RateLimitExceededError):
            await limiter.acquire_order()

    async def test_a_flood_is_bounded_by_burst_plus_honest_refill(self, r: aioredis.Redis) -> None:
        """E02-S05's acceptance criterion, stated so it is actually checkable.

        "100 orders/sec results in at most 3/sec reaching the broker" cannot mean
        "at most 3 in total": the bucket legitimately refills while the flood is
        running, and a flood taking 0.7 s at 3/sec regenerates a further two
        tokens. The real contract is burst + refill x elapsed, so that is what is
        asserted — with a small allowance for the clock read straddling the
        window boundary.

        This bound is what caught the client-clock defect in `take_token`: out-of
        -order timestamps let 11 of 100 through here (56 on a loaded host)
        against a ceiling of about 5.
        """
        rate, burst = 3, 3
        limiter = BrokerRateLimiter(r, RateLimitConfig(orders_per_second=rate, order_burst=burst))

        async def attempt() -> bool:
            try:
                await limiter.acquire_order()
            except RateLimitExceededError:
                return False
            return True

        loop = asyncio.get_running_loop()
        started = loop.time()
        results = await asyncio.gather(*(attempt() for _ in range(100)))
        elapsed = loop.time() - started

        allowed = sum(results)
        ceiling = burst + rate * elapsed + 1
        assert allowed <= ceiling, (
            f"{allowed} orders passed in {elapsed:.2f}s; burst {burst} plus honest "
            f"refill allows at most {ceiling:.1f}. The limiter is granting tokens "
            f"that no elapsed time produced."
        )
        assert allowed >= 1, "the limiter refused everything, including the first"
        assert allowed < 100, "the limiter let the entire flood through"

    async def test_refusal_is_immediate_rather_than_a_delay(self, r: aioredis.Redis) -> None:
        """A limiter that sleeps turns backpressure into a silent latency spike
        at exactly the moment the system is under stress."""
        limiter = BrokerRateLimiter(r, RateLimitConfig(orders_per_second=1, order_burst=1))
        await limiter.acquire_order()

        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(RateLimitExceededError):
            await limiter.acquire_order()
        assert loop.time() - started < 0.5, "the limiter blocked instead of refusing"

    async def test_the_bucket_refills(self, r: aioredis.Redis) -> None:
        limiter = BrokerRateLimiter(r, RateLimitConfig(orders_per_second=9, order_burst=1))
        await limiter.acquire_order()
        with pytest.raises(RateLimitExceededError):
            await limiter.acquire_order()
        await asyncio.sleep(0.3)
        await limiter.acquire_order()  # refilled; must not raise


class TestOrdersAndDataDoNotShareABudget:
    async def test_exhausting_data_leaves_orders_untouched(self, r: aioredis.Redis) -> None:
        """A multi-hour historical backfill must never be able to starve an exit
        order. Separate keys are what guarantees that."""
        limiter = BrokerRateLimiter(
            r, RateLimitConfig(orders_per_second=3, order_burst=3, data_per_second=1, data_burst=1)
        )
        await limiter.acquire_data()
        with pytest.raises(RateLimitExceededError):
            await limiter.acquire_data()

        # The order budget is entirely unaffected.
        for _ in range(3):
            await limiter.acquire_order()

    async def test_they_use_different_keys(self) -> None:
        assert keys.order_rate_limit() != keys.data_rate_limit()


class TestTheBudgetIsSharedAcrossProcesses:
    async def test_two_limiters_draw_from_one_bucket(self, r: aioredis.Redis) -> None:
        """The limit is account-wide, so two services must not each get a full
        allowance. This is why the bucket is in Redis and not in the adapter."""
        config = RateLimitConfig(orders_per_second=2, order_burst=2)
        first = BrokerRateLimiter(r, config)
        second = BrokerRateLimiter(r, config)

        await first.acquire_order()
        await second.acquire_order()
        with pytest.raises(RateLimitExceededError):
            await first.acquire_order()


class TestTheConfigCannotBreachTheSebiThreshold:
    def test_ten_per_second_is_refused(self) -> None:
        """Staying under 10/sec is what keeps this a self-developed personal
        algo needing no exchange registration. It is a code bound, not a
        preference."""
        with pytest.raises(ValueError, match="registration threshold"):
            RateLimitConfig(orders_per_second=SEBI_ORDER_RATE_THRESHOLD)

    def test_above_ten_is_refused(self) -> None:
        with pytest.raises(ValueError, match="registration threshold"):
            RateLimitConfig(orders_per_second=25)

    def test_the_shipped_default_is_well_under_it(self) -> None:
        assert RateLimitConfig().orders_per_second < SEBI_ORDER_RATE_THRESHOLD

    def test_a_zero_burst_is_refused(self) -> None:
        """A burst of zero would refuse every order forever, silently."""
        with pytest.raises(ValueError, match="burst"):
            RateLimitConfig(order_burst=0)
