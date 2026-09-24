"""Broker interface.

Every broker-specific detail lives behind this protocol, so swapping brokers
touches one module rather than the whole system.  **No module outside
``algotrader.broker`` imports a broker SDK.**

Two adapter flavours exist, and the split is a security boundary, not an
organizational one (LOW_LEVEL_ARCHITECTURE.md §10.3):

* :class:`MarketDataAdapter` — read-only.  Given to ``market-ingest``,
  ``premarket-job`` and anything else that needs prices.  Its credentials are
  data-scoped where the broker supports it.
* :class:`TradingAdapter` — adds order placement.  Instantiated by
  ``execution-svc`` **only**.  Compromising any other service therefore
  cannot place an order.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from algotrader.common.enums import Timeframe
from algotrader.common.models.market import Bar, Instrument, Tick
from algotrader.common.models.trading import Order, OrderRequest


class BrokerSession(BaseModel):
    """An authenticated broker session.

    Note there is no token field here — tokens are held as ``SecretString``
    inside the adapter and never surface in a model that might be logged or
    serialized.
    """

    model_config = ConfigDict(frozen=True)

    broker: str
    client_id: str
    authenticated_at: datetime
    expires_at: datetime

    @property
    def is_expired(self) -> bool:
        from datetime import UTC

        return datetime.now(UTC) >= self.expires_at


class MarginSnapshot(BaseModel):
    """Live margin from the broker.

    Position sizing uses THIS, never an assumed leverage multiple — SEBI's
    peak margin rules mean intraday leverage is not something to guess at.
    """

    model_config = ConfigDict(frozen=True)

    available_cash: Decimal = Field(ge=0)
    available_margin: Decimal = Field(ge=0)
    used_margin: Decimal = Field(ge=0)
    fetched_at: datetime


class BrokerError(Exception):
    """Base for broker failures."""


class AuthenticationError(BrokerError):
    """Login or session refresh failed.  Fails the trading day closed."""


class RateLimitError(BrokerError):
    """Broker rate limit hit.  Back off; never spin."""


class OrderRejectedError(BrokerError):
    """Broker rejected the order outright.  Do NOT retry blindly."""

    def __init__(self, message: str, *, reason_code: str | None = None) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class AmbiguousOrderError(BrokerError):
    """The order may or may not have been placed (timeout, 5xx, connection drop).

    This is the dangerous case.  The recovery path is to **query the
    orderbook by client_order_id**, never to retry — a blind retry after a
    timeout is how duplicate positions happen.
    See LOW_LEVEL_ARCHITECTURE.md §8.2.
    """


class DuplicateBrokerOrderError(BrokerError):
    """More than one live broker order carries our idempotency key.

    This is the failure idempotency exists to prevent, so it must never be
    resolved by picking one. Returning the first match silently would hand the
    recovery path a stale rejected order and invite a resubmission on top of a
    live one. Halt and escalate instead.
    """

    def __init__(self, client_order_id: str, broker_order_ids: list[str]) -> None:
        super().__init__(
            f"{len(broker_order_ids)} broker orders carry client_order_id "
            f"{client_order_id}: {broker_order_ids}. A duplicate exists — do not "
            f"place anything else for this id until it is resolved by hand."
        )
        self.client_order_id = client_order_id
        self.broker_order_ids = broker_order_ids


@dataclass(frozen=True)
class BrokerPosition:
    """One row of the broker's net positions, and only what the broker knows.

    ``quantity`` is SIGNED: negative is short. Kite's portfolio documentation
    says "Quantity held", and its own example shows ``-3``. Rows for positions
    opened and fully closed during the day remain in the list at zero, so a
    reader that treated every row as an open position would see a book full of
    positions that are not there.

    ``product`` and ``exchange`` stay strings rather than enums, deliberately.
    The account is also a human's, and it can hold products this system never
    trades (CNC, NRML, MTF). Mapping them through a closed enum would make one
    delivery holding the owner bought by hand fail every reconciliation read -
    and a read that always fails is, after three cycles, a halt.

    The day's cumulative buy and sell quantities are carried because they are
    MONOTONE, which net quantity is not: see ``execution/reconciliation.py``
    for why that is the difference between a race-free unknown-position check
    and one that halts on its own exits.
    """

    symbol: str
    exchange: str
    product: str
    quantity: int
    day_buy_quantity: int
    day_sell_quantity: int
    average_price: Decimal | None = None


@runtime_checkable
class MarketDataAdapter(Protocol):
    """Read-only broker surface."""

    async def authenticate(self) -> BrokerSession: ...

    async def is_session_valid(self) -> bool: ...

    async def fetch_instruments(self) -> list[Instrument]: ...

    def subscribe(self, tokens: list[str]) -> AsyncIterator[Tick]:
        """Stream live ticks.

        A plain ``def`` returning an async iterator, not an ``async def``. The
        protocol said ``async def`` until E15-S09, which types the method as a
        coroutine that must be awaited before iterating - and an async
        generator cannot be awaited, so a caller written against the protocol
        would have raised ``TypeError`` against every real implementation.
        Nothing noticed because nothing checked that the Kite adapter
        conforms; ``broker/kite/trading.py`` now asserts it.

        Implementations must reconnect with backoff on drop, and must signal
        the gap so downstream can mark indicator state stale rather than
        silently resuming with a hole in the series.
        """
        ...

    async def fetch_historical(
        self,
        token: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]: ...


@runtime_checkable
class TradingAdapter(MarketDataAdapter, Protocol):
    """Adds order placement.  ``execution-svc`` only."""

    async def fetch_margins(self) -> MarginSnapshot: ...

    async def place_order(self, request: OrderRequest) -> str:
        """Submit an order; returns the broker order id.

        Raises:
            OrderRejectedError: broker refused — do not retry.
            AmbiguousOrderError: outcome unknown — reconcile, do not retry.
            RateLimitError: back off.
        """
        ...

    async def modify_order(
        self,
        broker_order_id: str,
        *,
        quantity: int | None = None,
        limit_price: Decimal | None = None,
        trigger_price: Decimal | None = None,
    ) -> None: ...

    async def cancel_order(self, broker_order_id: str) -> None: ...

    async def fetch_orderbook(self) -> list[Order]: ...

    async def fetch_positions(self) -> list[BrokerPosition]:
        """The broker's NET positions, as the broker describes them.

        Declared ``-> list[Position]`` until E15-S09 while the Kite adapter
        returned raw dicts - and the adapter was right. A ``Position`` requires
        a stop price, a correlation id, a slot and a square-off deadline, and a
        broker has none of them; "mapping" into it would mean inventing them.
        The first consumer of this method, the reconciliation loop, would have
        been typed against fields that do not exist.
        """
        ...

    def order_key(self, client_order_id: str) -> str:
        """How the broker stores OUR idempotency key on an order.

        Kite truncates it to a 20-character tag. The rule is the broker's, so it
        lives here rather than in every caller that needs to match an orderbook
        row back to one of our orders.
        """
        ...

    async def find_by_client_order_id(self, client_order_id: str) -> Order | None:
        """Look up an order by OUR idempotency key.

        This is the recovery path after an :class:`AmbiguousOrderError`:
        query, adopt the broker's answer if present, and only resubmit if
        genuinely absent.
        """
        ...


class ReadOnlyGuard:
    """Mixin that makes trading methods raise.

    Defence in depth: even if a read-only adapter is accidentally handed to a
    service that tries to trade, the attempt fails loudly instead of quietly
    succeeding.
    """

    async def place_order(self, request: OrderRequest) -> str:
        raise PermissionError("this adapter is read-only; only execution-svc may place orders")

    async def modify_order(self, *args: object, **kwargs: object) -> None:
        raise PermissionError("this adapter is read-only")

    async def cancel_order(self, broker_order_id: str) -> None:
        raise PermissionError("this adapter is read-only")
