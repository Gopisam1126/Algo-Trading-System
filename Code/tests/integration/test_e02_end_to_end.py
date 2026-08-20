"""E02 end to end — the broker layer as one system, against real datastores.

Each test below is a sequence a real trading morning actually performs, not an
isolated unit. The interesting failures in this layer are all at the seams:
between the rate limiter and the order path, between an ambiguous failure and
the recovery that follows it, between the instrument dump and the symbols the
rest of the system will address.

Uses the real Redis token bucket and the real instrument repository. Only the
broker HTTP client is faked, because that is the one thing that needs
credentials — and faking it is the point: what is under test is this system's
handling of the broker's behaviour, including its bad days.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any, ClassVar

import kiteconnect.exceptions as kx
import pytest
import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from algotrader.broker.adapter import (
    AmbiguousOrderError,
    DuplicateBrokerOrderError,
    OrderRejectedError,
)
from algotrader.broker.kite import mapping
from algotrader.broker.kite.auth import KiteAuthManager
from algotrader.broker.kite.instruments import InstrumentSync
from algotrader.broker.kite.market_data import KiteMarketDataAdapter
from algotrader.broker.kite.trading import KiteTradingAdapter
from algotrader.broker.ratelimit import (
    BrokerRateLimiter,
    RateLimitConfig,
    RateLimitExceededError,
)
from algotrader.common.db import engine as db_engine
from algotrader.common.db.repositories import InstrumentRepository
from algotrader.common.enums import OrderIntent, OrderStatus, OrderType, Product, Side
from algotrader.common.redis import keys
from algotrader.common.secrets import SecretString

pytestmark = [pytest.mark.integration]

CLIENT_ORDER_ID = "f3a9c1d0e5b74826a1c9d3f5e7b90246"  # sha256-shaped, alphanumeric


@pytest.fixture
async def r(redis_url: str) -> AsyncIterator[aioredis.Redis]:
    client = aioredis.from_url(redis_url, decode_responses=True)
    await client.delete(keys.order_rate_limit(), keys.data_rate_limit())
    yield client
    await client.aclose()


@pytest.fixture
async def session(migrated_database: str) -> AsyncIterator[AsyncSession]:
    """A rolled-back session. Deliberately does NOT clear ``instruments``.

    An earlier version wiped the table for a clean count and passed alone, then
    failed in the full suite: other tests hang bars and positions off those
    rows, so the DELETE hit a foreign key. Wiping a shared table to make an
    assertion easy is the wrong trade — these tests use their own symbol prefix
    and assert only on that, so they hold whatever else the suite has left
    behind.
    """
    eng = db_engine.create_engine_from_url(migrated_database)
    factory = db_engine.create_session_factory(eng)
    async with factory() as s:
        yield s
        await s.rollback()
    await eng.dispose()


def _auth() -> KiteAuthManager:
    auth = KiteAuthManager(
        api_key="pubkey", api_secret=SecretString("api_secret_value"), client_id="AB1234"
    )
    auth.adopt_token("access_token_value")
    return auth


class ScriptedKite:
    """A broker that behaves badly on cue.

    ``fail_next`` makes the next call raise, which is how the ambiguous-failure
    path gets exercised without waiting for a real network to misbehave.
    """

    def __init__(self, **canned: Any) -> None:
        self.canned = canned
        self.placed: list[dict[str, Any]] = []
        self.fail_next: Exception | None = None
        self._book: list[dict[str, Any]] = list(canned.get("orders", []))

    def place_order(self, **kwargs: Any) -> str:
        if self.fail_next is not None:
            failure, self.fail_next = self.fail_next, None
            # The order DID reach the exchange before the connection dropped —
            # the whole point of an ambiguous failure.
            self._record(kwargs)
            raise failure
        self._record(kwargs)
        return str(self.placed[-1]["order_id"])

    def _record(self, kwargs: dict[str, Any]) -> None:
        order_id = f"2408190000000{len(self.placed) + 1:02d}"
        row = {
            "order_id": order_id,
            "tag": kwargs.get("tag", ""),
            "tradingsymbol": kwargs["tradingsymbol"],
            "transaction_type": kwargs["transaction_type"],
            "order_type": kwargs["order_type"],
            "product": kwargs["product"],
            "quantity": kwargs["quantity"],
            "price": kwargs.get("price", 0),
            "trigger_price": kwargs.get("trigger_price", 0),
            "status": "OPEN",
            "filled_quantity": 0,
            "average_price": None,
            "order_timestamp": dt.datetime(2026, 8, 19, 9, 20, 15),
            "status_message": None,
        }
        self.placed.append(row)
        self._book.append(row)

    def orders(self) -> list[dict[str, Any]]:
        return list(self._book)

    def instruments(self, exchange: str) -> list[dict[str, Any]]:
        return self.canned.get("instruments", [])

    def margins(self, segment: str) -> dict[str, Any]:
        return self.canned.get(
            "margins",
            {
                "available": {"cash": "500000", "live_balance": "500000"},
                "utilised": {"debits": "0"},
            },
        )

    def positions(self) -> dict[str, Any]:
        return {"net": []}


def _request(**over: Any) -> Any:
    from algotrader.common.models.trading import OrderRequest

    base: dict[str, Any] = {
        "client_order_id": CLIENT_ORDER_ID,
        "correlation_id": uuid.uuid4(),
        "symbol": "INFY",
        "side": Side.BUY,
        "order_type": OrderType.MARKET,
        "product": Product.MIS,
        "quantity": 10,
        "intent": OrderIntent.ENTRY,
        "market_protection": Decimal(-1),
    }
    base.update(over)
    return OrderRequest(**base)


class TestTheAmbiguousFailureRecoveryLoop:
    """The most expensive bug in a trading system, exercised end to end.

    A timeout after the exchange accepted the order looks identical to a
    timeout before it. Retrying produces two positions. The recovery path must
    query by our tag, find the order the broker already has, and adopt it.
    """

    async def test_a_timeout_then_recovery_finds_exactly_one_order(self, r: aioredis.Redis) -> None:
        client = ScriptedKite()
        limiter = BrokerRateLimiter(r, RateLimitConfig())
        adapter = KiteTradingAdapter(auth=_auth(), client=client, limiter=limiter)

        client.fail_next = kx.NetworkException("connection reset")
        with pytest.raises(AmbiguousOrderError):
            await adapter.place_order(_request())

        # The order exists at the broker despite the exception.
        recovered = await adapter.find_by_client_order_id(CLIENT_ORDER_ID)
        assert recovered is not None, (
            "the order reached the exchange but recovery could not find it — "
            "this is the state in which a caller wrongly resubmits"
        )
        assert recovered.status is OrderStatus.OPEN
        assert len(client.placed) == 1, "exactly one order should exist at the broker"

    async def test_recovery_does_not_place_a_second_order(self, r: aioredis.Redis) -> None:
        """The whole point: querying is not placing."""
        client = ScriptedKite()
        adapter = KiteTradingAdapter(
            auth=_auth(), client=client, limiter=BrokerRateLimiter(r, RateLimitConfig())
        )
        client.fail_next = TimeoutError("read timeout")
        with pytest.raises(AmbiguousOrderError):
            await adapter.place_order(_request())

        for _ in range(3):
            await adapter.find_by_client_order_id(CLIENT_ORDER_ID)
        assert len(client.placed) == 1

    async def test_a_genuinely_absent_order_reports_absent(self, r: aioredis.Redis) -> None:
        """Absent is a real answer — it is what authorises a resubmission."""
        adapter = KiteTradingAdapter(
            auth=_auth(), client=ScriptedKite(), limiter=BrokerRateLimiter(r, RateLimitConfig())
        )
        assert await adapter.find_by_client_order_id(CLIENT_ORDER_ID) is None

    async def test_a_duplicate_halts_instead_of_guessing(self, r: aioredis.Redis) -> None:
        """If a duplicate already exists, picking one of them hides the failure
        idempotency was meant to prevent."""
        client = ScriptedKite()
        adapter = KiteTradingAdapter(
            auth=_auth(), client=client, limiter=BrokerRateLimiter(r, RateLimitConfig())
        )
        await adapter.place_order(_request())
        await adapter.place_order(_request())  # same id — the bug we defend against
        with pytest.raises(DuplicateBrokerOrderError):
            await adapter.find_by_client_order_id(CLIENT_ORDER_ID)


class TestTheRateLimiterGatesTheRealOrderPath:
    async def test_a_burst_of_orders_is_capped_at_the_broker(self, r: aioredis.Redis) -> None:
        """Not the limiter in isolation — the limiter as wired into place_order."""
        client = ScriptedKite()
        adapter = KiteTradingAdapter(
            auth=_auth(),
            client=client,
            limiter=BrokerRateLimiter(r, RateLimitConfig(orders_per_second=3, order_burst=3)),
        )

        async def attempt(n: int) -> bool:
            try:
                await adapter.place_order(_request(client_order_id=f"{n:032x}"))
            except RateLimitExceededError:
                return False
            return True

        loop = asyncio.get_running_loop()
        started = loop.time()
        results = await asyncio.gather(*(attempt(n) for n in range(60)))
        elapsed = loop.time() - started

        ceiling = 3 + 3 * elapsed + 1
        assert len(client.placed) == sum(results)
        assert len(client.placed) <= ceiling, (
            f"{len(client.placed)} orders reached the broker in {elapsed:.2f}s; "
            f"burst plus honest refill allows at most {ceiling:.1f}"
        )

    async def test_a_refused_order_never_reaches_the_broker(self, r: aioredis.Redis) -> None:
        """Refusal must happen BEFORE the call, not after."""
        client = ScriptedKite()
        adapter = KiteTradingAdapter(
            auth=_auth(),
            client=client,
            limiter=BrokerRateLimiter(r, RateLimitConfig(orders_per_second=1, order_burst=1)),
        )
        await adapter.place_order(_request(client_order_id="a" * 32))
        with pytest.raises(RateLimitExceededError):
            await adapter.place_order(_request(client_order_id="b" * 32))
        assert len(client.placed) == 1

    async def test_reads_do_not_consume_the_order_budget(self, r: aioredis.Redis) -> None:
        """An exit order must not be starved by a reconciliation sweep."""
        client = ScriptedKite()
        adapter = KiteTradingAdapter(
            auth=_auth(),
            client=client,
            limiter=BrokerRateLimiter(r, RateLimitConfig(orders_per_second=2, order_burst=2)),
        )
        for _ in range(10):
            await adapter.fetch_orderbook()
        # The order budget is untouched by all that reading.
        await adapter.place_order(_request(client_order_id="c" * 32))
        await adapter.place_order(_request(client_order_id="d" * 32))
        assert len(client.placed) == 2


class TestInstrumentSyncAgainstTheRealRepository:
    #: Own symbols, so the assertions do not depend on what else the suite has
    #: put in the table. "E02X3" carries the non-standard tick.
    _DUMP: ClassVar[list[dict[str, Any]]] = [
        {"tradingsymbol": "E02X1", "instrument_token": 90000001, "tick_size": 0.05, "lot_size": 1},
        {"tradingsymbol": "E02X2", "instrument_token": 90000002, "tick_size": 0.05, "lot_size": 1},
        {"tradingsymbol": "E02X3", "instrument_token": 90000003, "tick_size": 0.01, "lot_size": 1},
    ]

    async def test_the_dump_lands_in_the_database_and_resolves(
        self, session: AsyncSession, r: aioredis.Redis
    ) -> None:
        adapter = KiteMarketDataAdapter(
            auth=_auth(),
            client=ScriptedKite(instruments=self._DUMP),
            limiter=BrokerRateLimiter(r, RateLimitConfig()),
        )
        repo = InstrumentRepository(session)
        result = await InstrumentSync(adapter, repo).run()
        await session.flush()

        assert result.fetched == 3
        await repo.refresh_cache()
        assert await repo.symbol_id("E02X1") > 0, "the symbol must be addressable afterwards"

    async def test_a_rerun_is_idempotent(self, session: AsyncSession) -> None:
        """A retry after a network blip is normal and must not duplicate rows."""
        adapter = KiteMarketDataAdapter(auth=_auth(), client=ScriptedKite(instruments=self._DUMP))
        repo = InstrumentRepository(session)
        sync = InstrumentSync(adapter, repo)
        await sync.run()
        await session.flush()
        await sync.run()
        await session.flush()

        count = (
            await session.execute(
                text("SELECT count(*) FROM instruments WHERE tradingsymbol LIKE 'E02X%'")
            )
        ).scalar_one()
        assert count == 3, "a re-run must update in place, never duplicate"

    async def test_the_non_standard_tick_survives_the_round_trip(
        self, session: AsyncSession
    ) -> None:
        """A hardcoded 0.05 would put every order in this symbol off-grid."""
        adapter = KiteMarketDataAdapter(auth=_auth(), client=ScriptedKite(instruments=self._DUMP))
        repo = InstrumentRepository(session)
        await InstrumentSync(adapter, repo).run()
        await session.flush()

        tick = (
            await session.execute(
                text("SELECT tick_size FROM instruments WHERE tradingsymbol = 'E02X3'")
            )
        ).scalar_one()
        assert Decimal(str(tick)) == Decimal("0.01")

    async def test_a_price_rounded_for_that_tick_is_on_the_grid(
        self, session: AsyncSession
    ) -> None:
        """The full path: dump -> stored tick -> side-aware rounding -> valid price."""
        from algotrader.broker.kite.instruments import tick_grid_is_respected

        adapter = KiteMarketDataAdapter(auth=_auth(), client=ScriptedKite(instruments=self._DUMP))
        await InstrumentSync(adapter, InstrumentRepository(session)).run()
        await session.flush()

        tick = Decimal(
            str(
                (
                    await session.execute(
                        text("SELECT tick_size FROM instruments WHERE tradingsymbol = 'E02X3'")
                    )
                ).scalar_one()
            )
        )
        buy = mapping.round_to_tick(Decimal("13.4567"), tick, side=Side.BUY)
        assert tick_grid_is_respected(buy, tick)
        assert buy <= Decimal("13.4567"), "a buy limit must never be rounded UP"


class TestForeignOrdersDoNotBlindReconciliation:
    """A personal Kite account is also traded by hand.

    An order type this system does not model — an iceberg placed in the app, a
    GTT firing — must not be able to empty the read the reconciliation loop
    depends on.
    """

    async def test_a_foreign_order_does_not_hide_our_own(self, r: aioredis.Redis) -> None:
        client = ScriptedKite()
        adapter = KiteTradingAdapter(
            auth=_auth(), client=client, limiter=BrokerRateLimiter(r, RateLimitConfig())
        )
        await adapter.place_order(_request())
        # A human places an iceberg from the Kite app.
        client._book.append(
            {
                "order_id": "HUMAN1",
                "tag": "",
                "tradingsymbol": "RELIANCE",
                "transaction_type": "BUY",
                "order_type": "ICEBERG",
                "product": "MIS",
                "quantity": 100,
                "price": 0,
                "trigger_price": 0,
                "status": "OPEN",
                "filled_quantity": 0,
                "average_price": None,
                "order_timestamp": dt.datetime(2026, 8, 19, 10, 0, 0),
                "status_message": None,
            }
        )

        modelled = await adapter.fetch_orderbook()
        assert len(modelled) == 1, "our order must still be visible"

        raw = await adapter.fetch_raw_orders()
        assert len(raw) == 2, "reconciliation must still see the foreign order"
        assert "HUMAN1" in {str(row["order_id"]) for row in raw}

    async def test_recovery_still_works_alongside_a_foreign_order(self, r: aioredis.Redis) -> None:
        client = ScriptedKite()
        adapter = KiteTradingAdapter(
            auth=_auth(), client=client, limiter=BrokerRateLimiter(r, RateLimitConfig())
        )
        client.fail_next = kx.NetworkException("reset")
        with pytest.raises(AmbiguousOrderError):
            await adapter.place_order(_request())
        client._book.insert(
            0,
            {
                "order_id": "HUMAN1",
                "tag": "",
                "tradingsymbol": "RELIANCE",
                "transaction_type": "SELL",
                "order_type": "ICEBERG",
                "product": "CNC",
                "quantity": 5,
                "price": 0,
                "trigger_price": 0,
                "status": "OPEN",
                "filled_quantity": 0,
                "average_price": None,
                "order_timestamp": dt.datetime(2026, 8, 19, 10, 0, 0),
                "status_message": None,
            },
        )
        assert await adapter.find_by_client_order_id(CLIENT_ORDER_ID) is not None


class TestTheSessionGatesEverything:
    async def test_a_rejected_order_is_not_ambiguous(self, r: aioredis.Redis) -> None:
        """A definitive rejection must not send the caller into reconciliation."""
        client = ScriptedKite()
        adapter = KiteTradingAdapter(
            auth=_auth(), client=client, limiter=BrokerRateLimiter(r, RateLimitConfig())
        )
        client.fail_next = kx.OrderException("insufficient funds", code=400)
        with pytest.raises(OrderRejectedError):
            await adapter.place_order(_request())

    async def test_an_expired_session_refuses_before_any_broker_call(self) -> None:
        auth = KiteAuthManager(api_key="k", api_secret=SecretString("s_value_123"), client_id="AB1")
        adapter = KiteMarketDataAdapter(auth=auth, client=ScriptedKite())
        assert not await adapter.is_session_valid()
        notice = auth.notice_if_reauth_needed()
        assert notice is not None
        assert "http" not in notice.message()
