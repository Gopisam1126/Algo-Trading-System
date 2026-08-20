"""Kite payloads <-> domain models, and the two conversions that are easy to get wrong.

**Prices arrive as paise.** Kite sends integers; dividing by 100 as ``Decimal``
is exact, dividing as ``float`` is not. Every price crossing this boundary goes
through :func:`paise_to_rupees`.

**Our order id does not fit the broker's field.** ``client_order_id`` is up to
64 characters; Kite's ``tag`` is *"alphanumeric, max 20 chars"* and is the only
carrier the broker offers. Since the id is a SHA-256 hex digest, its first 20
characters are alphanumeric and carry ~80 bits — enough that a collision within
one trading day is not a real risk. :func:`broker_tag` does that truncation in
one place so the recovery path and the submission path cannot disagree about it.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from algotrader.common.enums import Exchange, OrderStatus, OrderType, Product, Side

#: Kite: "An optional tag to apply to an order to identify it
#: (alphanumeric, max 20 chars)".
BROKER_TAG_MAX = 20

_ALNUM = re.compile(r"[A-Za-z0-9]+")

#: Kite quotes equities in paise.
_PAISE = Decimal(100)


class MappingError(ValueError):
    """A payload could not be mapped. Never guessed around."""


def paise_to_rupees(paise: int | str | Decimal) -> Decimal:
    """Exact paise -> rupees. ``Decimal`` throughout; never float."""
    return Decimal(paise) / _PAISE


def broker_tag(client_order_id: str) -> str:
    """The 20-character alphanumeric tag carrying our idempotency key.

    Refuses a non-alphanumeric id rather than silently mangling it. The broker
    would strip or reject the offending characters, and the resulting tag would
    no longer match what :meth:`find_by_client_order_id` searches for — turning
    the idempotency guarantee into a guarantee-shaped comment.
    """
    if not client_order_id:
        raise MappingError("client_order_id is empty; nothing to tag the order with")
    match = _ALNUM.fullmatch(client_order_id)
    if match is None:
        raise MappingError(
            f"client_order_id {client_order_id[:24]!r} is not alphanumeric. Kite's tag "
            f"field accepts alphanumeric only, so this id cannot round-trip and the "
            f"recovery path would not find the order."
        )
    return client_order_id[:BROKER_TAG_MAX]


def round_to_tick(price: Decimal, tick: Decimal, *, side: Side) -> Decimal:
    """Snap a limit price to the tick grid, **away from crossing the spread**.

    Direction is not cosmetic. Rounding a BUY *up* and a SELL *down* pays the
    spread on every order; doing it consistently one way biases fills. Rounding
    each side conservatively — buy down, sell up — never pays more than
    intended, at the cost of occasionally not filling. For a system whose
    position sizing is derived from an exact risk budget, paying more than
    intended is the worse failure.

    ``Instrument.round_to_tick`` uses banker's rounding and stays as-is for
    display and analysis; this is the one used before submission.
    """
    if tick <= 0:
        raise MappingError(f"tick size must be positive, got {tick}")
    mode = ROUND_DOWN if side is Side.BUY else ROUND_UP
    return (price / tick).quantize(Decimal("1"), rounding=mode) * tick


# ---------------------------------------------------------------------------
# Enum translation. Explicit tables, no getattr tricks — an unmapped value must
# raise here rather than reach the broker as something plausible-looking.
# ---------------------------------------------------------------------------

_ORDER_TYPE_OUT: dict[OrderType, str] = {
    OrderType.MARKET: "MARKET",
    OrderType.LIMIT: "LIMIT",
    OrderType.SL: "SL",
    OrderType.SLM: "SL-M",
}
_ORDER_TYPE_IN = {v: k for k, v in _ORDER_TYPE_OUT.items()}

_PRODUCT_OUT: dict[Product, str] = {
    Product.MIS: "MIS",
    Product.CNC: "CNC",
    Product.NRML: "NRML",
}
_PRODUCT_IN = {v: k for k, v in _PRODUCT_OUT.items()}

_SIDE_OUT: dict[Side, str] = {Side.BUY: "BUY", Side.SELL: "SELL"}
_SIDE_IN = {v: k for k, v in _SIDE_OUT.items()}

#: Kite status -> ours. Kite's vocabulary is smaller and its OPEN covers both
#: working and partially-filled; the fill quantity disambiguates downstream.
_STATUS_IN: dict[str, OrderStatus] = {
    "PUT ORDER REQ RECEIVED": OrderStatus.SUBMITTED,
    "VALIDATION PENDING": OrderStatus.SUBMITTED,
    "OPEN PENDING": OrderStatus.SUBMITTED,
    "MODIFY PENDING": OrderStatus.OPEN,
    "TRIGGER PENDING": OrderStatus.OPEN,
    "OPEN": OrderStatus.OPEN,
    "COMPLETE": OrderStatus.FILLED,
    "CANCELLED": OrderStatus.CANCELLED,
    "CANCELLED AMO": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
}


def order_type_out(value: OrderType) -> str:
    try:
        return _ORDER_TYPE_OUT[value]
    except KeyError:
        raise MappingError(f"no Kite order type for {value!r}") from None


def product_out(value: Product) -> str:
    try:
        return _PRODUCT_OUT[value]
    except KeyError:
        raise MappingError(f"no Kite product for {value!r}") from None


def side_out(value: Side) -> str:
    try:
        return _SIDE_OUT[value]
    except KeyError:
        raise MappingError(f"no Kite transaction type for {value!r}") from None


def status_in(value: str) -> OrderStatus:
    """Kite status -> ours.

    An unknown status becomes ``RECONCILE_REQUIRED`` rather than a guess. A
    status we do not understand on a live order is exactly the condition the
    reconciliation loop exists to surface, and silently calling it OPEN or
    REJECTED would either strand a position or fabricate one.
    """
    mapped = _STATUS_IN.get(str(value).upper().strip())
    if mapped is None:
        return OrderStatus.RECONCILE_REQUIRED
    return mapped


def exchange_in(value: str) -> Exchange:
    try:
        return Exchange(str(value).upper().strip())
    except ValueError:
        raise MappingError(f"unknown exchange {value!r}") from None


def parse_broker_timestamp(value: object) -> dt.datetime:
    """Kite returns naive IST datetimes. Attach IST, then convert to UTC.

    Treating a naive broker timestamp as UTC shifts every order 5h30m into the
    past, which silently corrupts latency measurement and any time-ordered
    reconstruction of a trade.
    """
    from algotrader.common.calendar import IST

    if isinstance(value, dt.datetime):
        stamp = value
    else:
        try:
            stamp = dt.datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            raise MappingError(f"unparseable broker timestamp {value!r}") from None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=IST)
    return stamp.astimezone(dt.UTC)
