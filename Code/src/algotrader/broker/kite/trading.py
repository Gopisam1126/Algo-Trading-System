"""Trading adapter (E02-S04, E02-S07) — the only class that can move money.

Instantiated by ``execution-svc`` and nowhere else. Four rules are enforced
here rather than trusted to callers:

**Market protection is mandatory.** Zerodha rejects MARKET and SL-M orders sent
without it, and rejects a value of ``0`` outright: *"Market orders without
market protection are not allowed via API."* ``OrderRequest`` already refuses to
construct such an order; this is the second gate, because the order that
matters most is the forced square-off at the deadline, and a rejection there is
the one you cannot afford.

**Every order carries our idempotency key.** ``client_order_id`` goes into
Kite's ``tag``, which is *"alphanumeric, max 20 chars"* — shorter than our id,
so :func:`mapping.broker_tag` truncates deterministically in one place. The tag
comes back in the orderbook, which is what makes :meth:`find_by_client_order_id`
a real recovery path rather than a hopeful one.

**A timeout is never a retry.** Every mutating call is classified with
``mutating=True``, so anything unrecognised becomes :class:`AmbiguousOrderError`
and routes to query-by-tag. Blind retry after a timeout is the most expensive
bug available in a trading system.

**Margin is read live, and its age is bounded.** Sizing from a cached margin is
a time-of-check/time-of-use gap: margin falls after every fill.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from algotrader.broker.adapter import (
    AmbiguousOrderError,
    DuplicateBrokerOrderError,
    MarginSnapshot,
    OrderRejectedError,
)
from algotrader.broker.kite import mapping
from algotrader.broker.kite.errors import classify
from algotrader.broker.kite.market_data import KiteReads
from algotrader.broker.ratelimit import BrokerRateLimiter
from algotrader.common.enums import OrderType, Side
from algotrader.common.models.trading import Order, OrderRequest

log = logging.getLogger(__name__)

#: Order types Zerodha requires market protection on.
_NEEDS_PROTECTION = frozenset({OrderType.MARKET, OrderType.SLM})

#: How stale a margin snapshot may be before sizing must refuse it. Short,
#: because margin moves on every fill and an overstated balance sizes a
#: position the account cannot actually carry.
DEFAULT_MARGIN_TTL_SECONDS = 20.0


class StaleMarginError(RuntimeError):
    """The cached margin is too old to size against. Fetch again."""


class KiteTradingAdapter(KiteReads):
    """Adds order placement to the read surface.

    Inherits :class:`KiteReads` and NOT ``ReadOnlyGuard``: execution needs
    quotes to sanity-check a price before submitting, and duplicating the read
    methods would risk the two paths drifting apart. Composing the reads
    without the guard means 'may trade' is stated positively rather than
    achieved by overriding a refusal.
    """

    def __init__(
        self,
        *,
        auth: Any,
        client: Any,
        limiter: BrokerRateLimiter | None = None,
        algo_id: str = "",
        margin_ttl_seconds: float = DEFAULT_MARGIN_TTL_SECONDS,
        tick_size_for: Callable[[str], Decimal] | None = None,
    ) -> None:
        super().__init__(auth=auth, client=client, limiter=limiter)
        self._algo_id = algo_id
        self._margin_ttl = margin_ttl_seconds
        self._tick_size_for = tick_size_for
        self._margin: MarginSnapshot | None = None

    # -- the write path ------------------------------------------------------

    async def _mutate(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Every state-changing broker call goes through here.

        Takes an ORDER token, not a data token — the two budgets are separate
        so a backfill cannot starve an exit. Classified with ``mutating=True``
        so an unrecognised failure fails closed.
        """
        if self._limiter is not None:
            await self._limiter.acquire_order()
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except Exception as exc:
            raise classify(exc, mutating=True) from None

    def _snap(self, price: Decimal | None, symbol: str, side: Side) -> Decimal | None:
        """Round a price onto the instrument's tick grid, or refuse.

        E02-S06's acceptance criterion is that a limit price is ALWAYS snapped
        before submission, and the exchange rejects anything off-grid. This is
        not a nicety: a stop derived from ATR is essentially never on a 0.05
        grid by accident, so without this every computed stop would be rejected
        by the broker.

        Refusing when no tick resolver is wired is deliberate. Falling back to a
        hardcoded 0.05 would be right for most of the market and silently wrong
        for the rest — and "silently wrong for some symbols" is exactly the
        failure mode that only appears once real money is on it.
        """
        if price is None:
            return None
        if self._tick_size_for is None:
            raise OrderRejectedError(
                f"cannot submit a priced order for {symbol}: no tick-size resolver is "
                f"wired, so the price cannot be snapped to the instrument's grid and "
                f"the exchange would reject it. Construct the adapter with "
                f"tick_size_for=...",
                reason_code="NO_TICK_RESOLVER",
            )
        tick = self._tick_size_for(symbol)
        snapped = mapping.round_to_tick(price, tick, side=side)
        if snapped != price:
            log.info(
                "snapped %s %s price %s -> %s on a %s tick",
                symbol,
                side.value,
                price,
                snapped,
                tick,
            )
        return snapped

    def _build_params(self, request: OrderRequest) -> dict[str, Any]:
        """Translate an OrderRequest into Kite's argument names.

        Raises rather than guessing on anything it cannot map. A silently
        dropped field here becomes an order with the wrong shape at the
        exchange.
        """
        if request.order_type in _NEEDS_PROTECTION:
            if request.market_protection is None:
                raise OrderRejectedError(
                    f"{request.order_type.value} requires market_protection; Zerodha "
                    f"rejects unprotected market orders via API",
                    reason_code="NO_MARKET_PROTECTION",
                )
            if request.market_protection == 0:
                raise OrderRejectedError(
                    "market_protection of 0 is rejected by the broker; use -1 for "
                    "broker-calculated protection or a positive percentage",
                    reason_code="ZERO_MARKET_PROTECTION",
                )

        params: dict[str, Any] = {
            "variety": "regular",
            "exchange": "NSE",
            "tradingsymbol": request.symbol,
            "transaction_type": mapping.side_out(request.side),
            "quantity": request.quantity,
            "product": mapping.product_out(request.product),
            "order_type": mapping.order_type_out(request.order_type),
            "tag": mapping.broker_tag(request.client_order_id),
        }
        limit_price = self._snap(request.limit_price, request.symbol, request.side)
        trigger_price = self._snap(request.trigger_price, request.symbol, request.side)
        if limit_price is not None:
            params["price"] = float(limit_price)
        if trigger_price is not None:
            params["trigger_price"] = float(trigger_price)
        if request.market_protection is not None:
            params["market_protection"] = float(request.market_protection)

        # Algo-ID: sent only when configured. SEBI's framework has the BROKER
        # tag strategies for self-developed algos under 10 orders/sec, and
        # Zerodha's own compliance guidance never asks the developer to supply
        # one — so an empty value means "let the broker tag it", not "forgot".
        algo_id = request.algo_id or self._algo_id
        if algo_id:
            params["algo_id"] = algo_id
        return params

    async def place_order(self, request: OrderRequest) -> str:
        """Submit an order and return the broker order id.

        Raises:
            OrderRejectedError: refused outright — do not retry.
            AmbiguousOrderError: outcome unknown — reconcile by tag, never retry.
            RateLimitError: refused before reaching the exchange — safe to retry.
        """
        params = self._build_params(request)
        # Logged before the call, without prices, so an ambiguous outcome still
        # leaves a record that this id was attempted.
        log.info(
            "placing %s %s x%d for %s (tag=%s)",
            params["transaction_type"],
            params["order_type"],
            request.quantity,
            request.symbol,
            params["tag"],
        )
        result = await self._mutate(self._client.place_order, **params)
        broker_order_id = str(result)
        if not broker_order_id:
            raise AmbiguousOrderError(
                f"broker returned no order id for tag {params['tag']}; the order may "
                f"exist. Reconcile by tag before doing anything else."
            )
        return broker_order_id

    async def modify_order(
        self,
        broker_order_id: str,
        *,
        quantity: int | None = None,
        limit_price: Decimal | None = None,
        trigger_price: Decimal | None = None,
    ) -> None:
        params: dict[str, Any] = {"variety": "regular", "order_id": broker_order_id}
        if quantity is not None:
            params["quantity"] = quantity
        if limit_price is not None:
            params["price"] = float(limit_price)
        if trigger_price is not None:
            params["trigger_price"] = float(trigger_price)
        if len(params) == 2:
            raise ValueError("modify_order called with nothing to modify")
        await self._mutate(self._client.modify_order, **params)

    async def cancel_order(self, broker_order_id: str) -> None:
        await self._mutate(self._client.cancel_order, variety="regular", order_id=broker_order_id)

    # -- reads that execution needs -----------------------------------------

    async def fetch_raw_orders(self) -> list[dict[str, Any]]:
        """The orderbook as the broker states it, unmapped.

        Reconciliation diffs on IDENTITY — which broker order ids exist, and do
        we have a record of each. That question needs no enum mapping, and
        insisting on one would make an order type this system does not model
        (an iceberg placed by hand, a GTT firing) able to break the very loop
        that is supposed to notice it.
        """
        return list(await self._call(self._client.orders))

    async def fetch_orderbook(self) -> list[Order]:
        """Orders this system can model, skipping those it cannot.

        A personal Kite account is also used by a human, so foreign order types
        are expected rather than exotic. Letting one unmappable row raise would
        return NOTHING from the read the 30-second reconciliation loop depends
        on — losing sight of every other order, including a genuinely unknown
        position, which is the exact condition the kill switch exists for.

        Skipped rows are logged at ERROR with their id, and
        :meth:`fetch_raw_orders` still sees them, so nothing disappears.
        """
        raw = await self._call(self._client.orders)
        out: list[Order] = []
        for row in raw:
            try:
                out.append(self._to_order(row))
            except (mapping.MappingError, KeyError, ValueError) as exc:
                log.error(
                    "broker order %s could not be mapped (%s) — it is NOT in the "
                    "modelled orderbook. Reconcile it from fetch_raw_orders.",
                    row.get("order_id"),
                    exc,
                )
        return out

    async def find_by_client_order_id(self, client_order_id: str) -> Order | None:
        """The recovery path after an ambiguous failure.

        Matches on the truncated tag, because that is what the broker actually
        stored. Comparing against the full id would never match and would make
        every ambiguous order look absent — precisely the condition under which
        a caller would wrongly resubmit.

        Two or more matches raise. That state means a duplicate already exists,
        which is the failure idempotency is for; returning the first match would
        hand back whichever the broker happened to list first — observed to be a
        stale REJECTED order sitting in front of the live OPEN one, which reads
        as "not placed" and invites a second submission on top of a real
        position.
        """
        wanted = mapping.broker_tag(client_order_id)
        matches = [
            row
            for row in await self._call(self._client.orders)
            if str(row.get("tag") or "") == wanted
        ]
        if not matches:
            return None
        if len(matches) > 1:
            raise DuplicateBrokerOrderError(
                client_order_id, [str(m.get("order_id")) for m in matches]
            )
        return self._to_order(matches[0])

    async def fetch_positions(self) -> list[dict[str, Any]]:
        """Raw broker positions.

        Returned as dicts on purpose: the broker's position shape is its own,
        and mapping it into our ``Position`` model would invent fields the
        broker does not have (correlation id, slot, stop price). Reconciliation
        compares identities and quantities, not our richer model.
        """
        raw = await self._call(self._client.positions)
        return list(raw.get("net", []) if isinstance(raw, dict) else raw)

    async def fetch_margins(self) -> MarginSnapshot:
        """Live margin. Always a fresh call; caching is the caller's decision."""
        raw = await self._call(self._client.margins, "equity")
        available = raw.get("available", {}) if isinstance(raw, dict) else {}
        utilised = raw.get("utilised", {}) if isinstance(raw, dict) else {}
        snapshot = MarginSnapshot(
            available_cash=Decimal(str(available.get("cash", 0) or 0)),
            available_margin=Decimal(
                str(available.get("live_balance", available.get("cash", 0)) or 0)
            ),
            used_margin=Decimal(str(utilised.get("debits", 0) or 0)),
            fetched_at=dt.datetime.now(dt.UTC),
        )
        self._margin = snapshot
        return snapshot

    def margin_for_sizing(self, *, now: dt.datetime | None = None) -> MarginSnapshot:
        """The margin snapshot, refused if it has aged out.

        Sizing against a stale margin is a time-of-check/time-of-use gap:
        available margin drops after every fill, so a snapshot taken before the
        previous entry can authorise a position the account cannot carry. The
        staleness bound turns that into a loud failure instead of an
        over-leveraged position.
        """
        if self._margin is None:
            raise StaleMarginError("no margin fetched yet; call fetch_margins first")
        now = now or dt.datetime.now(dt.UTC)
        age = (now - self._margin.fetched_at).total_seconds()
        if age > self._margin_ttl:
            raise StaleMarginError(
                f"margin snapshot is {age:.1f}s old (limit {self._margin_ttl:.0f}s); "
                f"re-fetch before sizing"
            )
        return self._margin

    # -- mapping -------------------------------------------------------------

    @staticmethod
    def _to_order(row: dict[str, Any]) -> Order:
        from uuid import UUID, uuid5

        tag = str(row.get("tag") or "")
        # The broker does not carry our correlation id, so derive a stable one
        # from the tag rather than inventing a random one that would break
        # audit threading on every reconciliation pass.
        namespace = UUID("00000000-0000-0000-0000-000000000000")
        correlation = uuid5(namespace, tag or str(row.get("order_id")))
        return Order(
            client_order_id=tag or str(row.get("order_id")),
            broker_order_id=str(row.get("order_id")),
            correlation_id=correlation,
            symbol=str(row.get("tradingsymbol")),
            side=mapping.side_in(str(row["transaction_type"])),
            order_type=mapping.order_type_in(str(row["order_type"])),
            product=mapping.product_in(str(row["product"])),
            quantity=int(row.get("quantity") or 0),
            limit_price=Decimal(str(row["price"])) if row.get("price") else None,
            trigger_price=Decimal(str(row["trigger_price"])) if row.get("trigger_price") else None,
            status=mapping.status_in(str(row.get("status") or "")),
            filled_quantity=int(row.get("filled_quantity") or 0),
            average_price=Decimal(str(row["average_price"])) if row.get("average_price") else None,
            intent=_intent_from_tag(tag),
            placed_at=mapping.parse_broker_timestamp(row.get("order_timestamp")),
            last_update_at=mapping.parse_broker_timestamp(
                row.get("exchange_update_timestamp") or row.get("order_timestamp")
            ),
            rejection_reason=str(row["status_message"]) if row.get("status_message") else None,
        )


def _intent_from_tag(_tag: str) -> Any:
    """Intent is ours, not the broker's.

    Kite has no field for it, and the tag is a hash with no room to encode one.
    Reconciliation reads intent from our own order row, keyed by the tag; this
    default only fills the model when adopting an order we have no record of —
    which is itself an alert condition.
    """
    from algotrader.common.enums import OrderIntent

    return OrderIntent.ENTRY
