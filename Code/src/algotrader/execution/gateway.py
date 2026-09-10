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
market protection — rejected outright by Zerodha since 1 April 2026; at a
price off the tick grid — rejected by the exchange; or faster than the rate
limit, which is throttling below 10/sec and a change of regulatory regime at
it (see ``config.SEBI_ALGO_REGISTRATION_OPS``).

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
from typing import Any, Final, Protocol

from algotrader.broker.adapter import AmbiguousOrderError
from algotrader.common.enums import Direction, OrderIntent, OrderType, Product, Side
from algotrader.common.models.trading import (
    Order,
    OrderRequest,
    Recommendation,
    RiskDecision,
)
from algotrader.common.text import one_safe_line

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
    """The broker capabilities this needs.

    A Protocol rather than an import of ``KiteTradingAdapter``: the gateway is
    testable without a broker, and the dependency reads as a capability rather
    than as a vendor.

    ``find_by_client_order_id`` belongs on the SAME protocol as
    ``place_order`` deliberately (E15-S02). Splitting the query into its own
    protocol would let a caller wire a placer and a finder that talk to
    different brokers, or to the same broker through different credentials —
    and the recovery path would then ask one venue about an order sent to
    another, conclude "absent", and resubmit. One object, one venue, one
    answer.
    """

    async def place_order(self, request: OrderRequest) -> str: ...

    async def find_by_client_order_id(self, client_order_id: str) -> Order | None: ...


class OrderStore(Protocol):
    """Order persistence, as the gateway needs it.

    Structurally identical to the subset of
    ``common.db.repositories.OrderRepositoryProtocol`` used here, and declared
    locally for the same reason ``OrderPlacer`` is: ``execution`` does not
    import ``common.db``, and a capability is a better dependency than a
    module.

    ``insert_submitting`` writes the row with status ``SUBMITTING`` and no
    ``broker_order_id``. That intermediate state is not bookkeeping — it is
    the entire recovery mechanism, and §8.2 is unimplementable without it.
    """

    async def insert_submitting(self, order: dict[str, Any]) -> int: ...

    async def attach_broker_id(self, client_order_id: str, broker_order_id: str) -> None: ...

    async def find_by_client_order_id(self, client_order_id: str) -> dict[str, Any] | None: ...


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


class OrderNeverLandedError(GatewayError):
    """An ambiguous submission was queried, and the order genuinely is not there.

    The gateway raises this rather than resubmitting on its own initiative.
    §8.2 says such an order "may be resubmitted" — permission, not obligation,
    and the difference matters: if the absence determination is ever wrong,
    an automatic resubmit turns it into two real positions, which §8.2 calls
    "the single most expensive bug possible in a trading system". Resubmission
    stays an explicit act by a caller that can see this error.

    Distinct from ``AmbiguousOrderError``, which means *unknown*. This one
    means *known absent*, and only the query can tell them apart.
    """


class OrderStateUnknownError(GatewayError):
    """The recovery query answered, but not with enough to adopt.

    The broker returned a record for our ``client_order_id`` that carries no
    broker order id. Neither "it landed" nor "it did not" is supportable, so
    neither is claimed: this fails closed rather than resubmitting on a
    half-answer.
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

    ``algo_id`` is ``str | None`` and ``None`` is the EXPECTED value at this
    system's operating profile, not a placeholder waiting on paperwork.

    That reasoning was corrected on 9 Sep 2026. It previously read: absent
    until Zerodha assigns one at registration (blocker B1), and safe only
    because ``AppConfig`` refuses to load in LIVE mode without one. The second
    half is no longer true and the first half was never true. SEBI requires
    registration for a self-developed algo **only above** 10 orders/sec
    (circular of 4 Feb 2025, clause I(c)); below it the broker tags the order
    with a generic identifier and there is no id for the developer to supply.
    ``AppConfig`` now demands one only at or above that threshold, which
    ``MAX_ORDERS_PER_SECOND = 5`` structurally prevents this system reaching.

    So a ``None`` here is carried through to the adapter, which omits the
    parameter entirely — ``kiteconnect`` documents ``algo_id`` as optional and
    defaults it to ``None``. What still binds unconditionally is the static
    IP, which is checked at config load and is blocker B6.
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
        store: OrderStore,
        limiter: RateLimiter | None = None,
    ) -> None:
        self._placer = placer
        self._policy = policy
        #: REQUIRED, not optional (E15-S02). An optional store would mean
        #: ``None`` silently skips the pre-submission record - the exact shape
        #: of defect this codebase keeps finding, where an absent capability
        #: reads as a satisfied one. A gateway that cannot record an order
        #: cannot be constructed, so there is no configuration in which orders
        #: are placed untracked.
        self._store = store
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
        cid = request.client_order_id

        # Layer 1 of the pattern EPIC01 8.4 uses for slots, applied to orders:
        # the cheap check that catches the ordinary case. The UNIQUE
        # constraint on ``client_order_id`` (BR-2) remains the guarantee
        # behind it, and reconciliation (8.3) is the backstop behind that.
        # Execution is single-threaded and strictly serialised (9.1), so there
        # is no concurrent submitter for this check to race with.
        already = await self._store.find_by_client_order_id(cid)
        if already is not None:
            return await self._resume(cid, already)

        # BEFORE the record, so a refusal leaves nothing behind. A SUBMITTING
        # row for an order that was never sent would send reconciliation
        # hunting an order the broker has never heard of, every 30 seconds,
        # for the rest of the day.
        if self._limiter is not None and not await self._limiter.allow():
            raise RateLimitedError(
                f"the order-rate limiter refused {cid}. Nothing was sent, so "
                f"there is nothing to reconcile — but the signal is stale by "
                f"definition and must not be queued behind the bucket."
            )

        # TX1. If this raises, no broker call happens: a fact we cannot record
        # is a fact we must not create.
        await self._store.insert_submitting(
            {
                "client_order_id": cid,
                "correlation_id": request.correlation_id,
                "symbol": request.symbol,
                "side": request.side.value,
                "order_type": request.order_type.value,
                "product": request.product.value,
                "quantity": request.quantity,
                "intent": request.intent.value,
                "limit_price": request.limit_price,
                "trigger_price": request.trigger_price,
                "market_protection": request.market_protection,
                "algo_id": request.algo_id,
            }
        )

        try:
            broker_order_id = await self._placer.place_order(request)
        except AmbiguousOrderError:
            # The one branch this story exists for. Query, never retry.
            broker_order_id = await self._recover(cid)

        await self._attach(cid, broker_order_id)
        log.info("submitted %s for %s as broker order %s", cid, request.symbol, broker_order_id)
        return broker_order_id

    async def _resume(self, cid: str, recorded: dict[str, Any]) -> str:
        """A row already exists for this decision. Decide without sending.

        Two shapes reach here. A row carrying a broker id is a completed
        submission - the caller is retrying something that already succeeded,
        and the answer is the id it got last time. A row still in
        ``SUBMITTING`` is the dangerous one: the previous attempt died
        somewhere between recording the intent and recording the outcome, and
        only the broker knows which side of the call it died on.
        """
        broker_order_id = recorded.get("broker_order_id")
        if broker_order_id:
            log.info("%s was already submitted as broker order %s", cid, broker_order_id)
            return str(broker_order_id)

        log.warning(
            "%s is recorded as %s with no broker order id - a previous attempt "
            "did not finish. Querying the broker rather than resubmitting.",
            cid,
            one_safe_line(str(recorded.get("status", "unknown"))),
        )
        broker_order_id = await self._recover(cid)
        await self._attach(cid, broker_order_id)
        return broker_order_id

    async def _recover(self, cid: str) -> str:
        """Query, don't retry. Section 8.2's recovery path, and the whole point.

        Every outcome here is either an adopted broker id or an exception.
        There is deliberately no path that returns "probably fine".
        """
        found = await self._placer.find_by_client_order_id(cid)
        if found is None:
            raise OrderNeverLandedError(
                f"{cid} is not in the broker's orderbook, so it never landed. "
                f"Resubmitting is safe but is NOT done here: if this absence "
                f"determination is ever wrong, an automatic retry becomes a "
                f"second real position. Resubmit deliberately, or let "
                f"reconciliation adopt it."
            )
        if not found.broker_order_id:
            raise OrderStateUnknownError(
                f"the broker returned a record for {cid} with no broker order "
                f"id. That supports neither 'it landed' nor 'it did not', so "
                f"neither is assumed."
            )
        log.warning(
            "adopted broker order %s for %s after an ambiguous submission",
            found.broker_order_id,
            cid,
        )
        return found.broker_order_id

    async def _attach(self, cid: str, broker_order_id: str) -> None:
        """TX2. A failure here is logged, never raised.

        The order is real by this point. Raising would tell the caller the
        submission failed, and the natural response to that is to submit
        again - which is precisely the duplicate this story exists to prevent.
        A stale row is recoverable by reconciliation (8.3); a duplicate
        position is not.
        """
        try:
            await self._store.attach_broker_id(cid, broker_order_id)
        # Deliberately total: every persistence failure mode is less bad than
        # the resubmission a raise would invite. See the docstring.
        except Exception as exc:
            log.error(
                "order %s LANDED as broker order %s but could not be recorded: %s. "
                "The order is live and the local row is stale - reconciliation "
                "must adopt it. Not raising, because a raise here reads as "
                "'submission failed' and invites a duplicate.",
                cid,
                broker_order_id,
                one_safe_line(str(exc)),
            )

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
