"""The order gateway — the only path from a decision to a broker (E15-S01).

``LOW_LEVEL_ARCHITECTURE.md §5.7`` gives this component four jobs: build the
broker payload with the SEBI ``algo_id`` attached, generate the deterministic
``client_order_id`` (§8.2), enforce the order-rate token bucket, and place the
protective stop after entry fill. The fourth is E15-S04 and not here; the first
three are.

## The design record

**The decision.** One object turns an approved :class:`RiskDecision` into a
submitted order, and nothing else may. It refuses anything that is not
approved, computes the idempotency key from the decision's own identity, and
takes a token before every submission.

**The alternative rejected.** Letting the risk engine submit directly. It would
save a hop and cost the property that makes the engine worth having: the engine
is a pure function of (recommendation, context) and is replayable because of
it. Giving it a broker client would put I/O inside the thing whose determinism
every SIT scenario depends on. §5.7 also calls the gateway *"the only path to
an order"*, and a second path is how a control gets bypassed.

**The failure it prevents.** An approved decision reaching the broker without
an ``algo_id`` — a SEBI violation on an algorithmic order; without market
protection — rejected outright by Zerodha since 1 April 2026; at a price off
the tick grid — rejected by the exchange; or faster than the rate limit, which
is throttling below 10/sec and a regulatory threshold above it.

**What would make this decision wrong.** If submission ever needed to be
concurrent. It is deliberately serialised (§9.1: *"execution-svc is
single-threaded, strictly serialized... concurrency here buys nothing and risks
double-entry races. This is a deliberate performance sacrifice for
correctness"*), and that is the assumption the token bucket's accounting rests
on.

## Wiring the tick resolver, which is E15-S01's recorded loose end

E02 left ``KiteTradingAdapter`` taking a ``tick_size_for`` callable and
**refusing every priced order when it is absent** — fail-closed, with
``reason_code=NO_TICK_RESOLVER``, so nothing slipped through silently while the
resolver did not exist. Connecting it was left to this story because the
instrument repository was in scope here and not there.

The resolver is :meth:`InstrumentRepository.tick_size`, added for this story.
The *column* has been populated by the instrument sync since E02-S06 and
nothing ever read it; the repository cached only ``id <-> tradingsymbol``. The
new accessor is deliberately **synchronous**, because the adapter's callable is
a plain one and making it async would change a contract that is already under
test — which is why ``refresh_cache`` now loads tick sizes in the same query.

The gateway itself does not take the resolver: its entry order is MARKET and
needs no price. It is the *adapter* that needs it, so the wiring is
``KiteTradingAdapter(..., tick_size_for=instruments.tick_size)``, and an
integration test asserts a priced order now reaches the tick grid instead of
NO_TICK_RESOLVER. Holding an unused resolver here would have looked like
wiring while being none.

## The idempotency key, and the re-entry question §8.2 asks

§8.2 specifies::

    client_order_id = sha256(correlation_id|symbol|side|intent|trade_date)[:32]

E15-S02's build concern raises the obvious worry: *"a deliberate SECOND entry
into the same symbol, same side, same intent, on the same day collides with the
first and is silently suppressed. Decide now, not after the first suppressed
re-entry."*

**Decided: the discriminator already exists, and it is ``correlation_id``.**
That field is the identity of one *signal* — it is minted per recommendation
and threads a single trade through pre-market candidacy, signal, AI review,
risk decision, order, fill and exit, which is what makes one trade a single log
query. Two entries into the same symbol on the same day are two signals with
two correlation ids, so they do not collide. A *retry* of one decision reuses
that decision's correlation id, so it does collide — which is exactly the
idempotency the key is for.

The whole guarantee therefore rests on correlation_id being per-signal rather
than per-symbol-day, so that is asserted directly in the tests rather than
assumed here. **If correlation ids are ever reused across signals, this key
silently suppresses real orders** — which is the residual risk, recorded at the
place it would bite.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Protocol

from algotrader.common.enums import Direction, OrderIntent, OrderType, Product, Side
from algotrader.common.models.trading import (
    OrderRequest,
    Recommendation,
    RiskDecision,
)

log = logging.getLogger(__name__)

#: §8.2's truncation. 32 hex characters of sha256, which also fits
#: ``OrderRequest.client_order_id``'s 8..64 bound and stays alphanumeric so
#: ``mapping.broker_tag`` can carry the first 20 into Kite's tag field.
CLIENT_ORDER_ID_LENGTH: Final = 32

#: The separator in the hashed material. A character that cannot appear in any
#: component, so that two different tuples cannot produce one string —
#: ("AB", "C") and ("A", "BC") must not hash alike.
_SEPARATOR: Final = "|"


class OrderPlacer(Protocol):
    """The one broker capability this needs.

    A Protocol rather than an import of ``KiteTradingAdapter``: the gateway is
    testable without a broker, and the dependency reads as a capability rather
    than as a vendor.
    """

    async def place_order(self, request: OrderRequest) -> str: ...


class RateLimiter(Protocol):
    """Permission to send one order now, or not."""

    async def allow(self) -> bool: ...


class GatewayError(RuntimeError):
    """The gateway refused to build or submit an order."""


class NotApprovedError(GatewayError):
    """A decision that was not approved was offered for submission."""


class RateLimitedError(GatewayError):
    """The order-rate bucket refused this submission.

    Distinct from the broker's own ``RateLimitError``: this one never reached
    the broker, so nothing is in flight and there is nothing to reconcile.
    """


def client_order_id(
    *,
    correlation_id: object,
    symbol: str,
    side: Side,
    intent: OrderIntent,
    trade_date: dt.date,
) -> str:
    """The deterministic idempotency key of §8.2.

    Keyword-only on purpose. Five string-ish components in a fixed order, where
    swapping two would still hash successfully and produce an id that is wrong
    in a way nothing downstream could detect.
    """
    # Checked BEFORE the join it protects. The join is what creates the
    # ambiguity — ("AB", "C") and ("A", "BC") produce the same string — so
    # validating afterwards would read as a guard while guarding nothing that
    # had not already happened.
    for part in (symbol, side.value, intent.value):
        if _SEPARATOR in part:
            raise GatewayError(
                f"{part!r} contains the {_SEPARATOR!r} separator, so two different "
                f"orders could hash to one id and the second would be suppressed as "
                f"a duplicate of the first."
            )
    material = _SEPARATOR.join(
        (
            str(correlation_id),
            symbol,
            side.value,
            intent.value,
            trade_date.isoformat(),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:CLIENT_ORDER_ID_LENGTH]


@dataclass(frozen=True)
class GatewayPolicy:
    """The configured values every order carries.

    ``algo_id`` is ``str | None`` because Zerodha assigns it at algo
    registration (blocker B1) and it is genuinely absent until then. That is
    safe here and not a hole: ``AppConfig`` already refuses to load in LIVE
    mode without one, so an order can only be built without an algo_id in a
    mode that cannot reach the exchange.
    """

    algo_id: str | None
    #: ``-1`` requests Zerodha's default protection band. Zero is refused at
    #: config load; it would mean "no protection" while looking configured.
    market_protection: Decimal
    product: Product = Product.MIS

    def __post_init__(self) -> None:
        if self.market_protection == 0:
            raise GatewayError(
                "market_protection of 0 is not protection. Zerodha rejects "
                "unprotected market orders, and a zero band would read as "
                "configured while protecting nothing."
            )


class OrderGateway:
    """Turns an approved decision into a submitted order. The only path."""

    def __init__(
        self,
        placer: OrderPlacer,
        *,
        policy: GatewayPolicy,
        limiter: RateLimiter | None = None,
    ) -> None:
        self._placer = placer
        self._policy = policy
        self._limiter = limiter

    async def submit_entry(
        self,
        decision: RiskDecision,
        recommendation: Recommendation,
        *,
        trade_date: dt.date,
    ) -> str:
        """Submit the entry order for an approved decision.

        Returns the broker order id. Raises rather than returning a sentinel:
        every failure here means no position was opened, and a caller that
        forgot to check a returned ``None`` would believe otherwise.
        """
        request = self.build_entry(decision, recommendation, trade_date=trade_date)
        if self._limiter is not None and not await self._limiter.allow():
            raise RateLimitedError(
                f"the order-rate limiter refused {request.client_order_id}. Nothing "
                f"was sent, so there is nothing to reconcile — but the signal is "
                f"stale by definition and must not be queued behind the bucket."
            )
        broker_order_id = await self._placer.place_order(request)
        log.info(
            "submitted %s for %s as broker order %s",
            request.client_order_id,
            request.symbol,
            broker_order_id,
        )
        return broker_order_id

    def build_entry(
        self,
        decision: RiskDecision,
        recommendation: Recommendation,
        *,
        trade_date: dt.date,
    ) -> OrderRequest:
        """Build the entry order. No I/O, so it is testable on its own.

        Separate from :meth:`submit_entry` because everything that can be wrong
        about an order is wrong here, and a caller reviewing what would be sent
        should not have to send it to find out.
        """
        if not decision.approved:
            raise NotApprovedError(
                f"decision for {recommendation.symbol} was not approved "
                f"({decision.reason.value if decision.reason else 'no reason'}); "
                f"the gateway is the only path to an order and it does not "
                f"second-guess the risk engine."
            )
        sizing = decision.sizing
        if sizing is None:
            raise NotApprovedError(
                f"decision for {recommendation.symbol} is approved but carries no "
                f"sizing, so there is no quantity to send. Approving without a "
                f"quantity is a risk-engine fault, not something to default."
            )
        if sizing.quantity <= 0:
            raise NotApprovedError(
                f"decision for {recommendation.symbol} is approved with quantity "
                f"{sizing.quantity}. A zero-quantity order is not an order."
            )

        side = Side.BUY if recommendation.direction is Direction.LONG else Side.SELL
        return OrderRequest(
            client_order_id=client_order_id(
                correlation_id=recommendation.correlation_id,
                symbol=recommendation.symbol,
                side=side,
                intent=OrderIntent.ENTRY,
                trade_date=trade_date,
            ),
            correlation_id=recommendation.correlation_id,
            symbol=recommendation.symbol,
            side=side,
            # MARKET for the entry: the decision was made on a trigger price
            # that has already been reached, and a limit that does not fill
            # leaves the risk budget committed to a position that does not
            # exist. Market protection is what bounds the slippage.
            order_type=OrderType.MARKET,
            product=self._policy.product,
            quantity=sizing.quantity,
            intent=OrderIntent.ENTRY,
            algo_id=self._policy.algo_id,
            market_protection=self._policy.market_protection,
        )
