"""The day's P&L, from the orders this system executed (audit RSK-004, RSK-005).

## The design record

**The decision.** The daily loss limit reads the account's change for the
day as THIS SYSTEM'S ORDERS describe it: for every correlation id with fills
today, cash out minus cash in, plus any open quantity marked at the live price,
minus the charges on every executed order (and on the order that would close
what is still open). The losing streak counts completed round trips, NET of
charges, in the order they finished.

**The alternatives rejected.**

* *Closed ``positions`` rows* - what was there. Two holes. A holding exited
  without ever being booked (a fill through its stop; an unbooked fill
  reconciliation exited after a restart) leaves no positions row, so its loss
  never reached the limit (RSK-004). And the figure was gross and realised
  only, so open losers and every rupee of charges were outside it (RSK-005):
  three losers at -12,000 and two open positions at -6,000 read as -2.4%
  on an account at -3.6%.
* *The broker's day P&L.* The right number in principle, and it would include
  trades this system did not place - which reconciliation already halts on as
  unknown positions. The orders table holds every order this system placed,
  booked or not, with the broker's fill price and quantity written back by
  reconciliation; it is the ledger of what this system did.

**The failure it prevents.** Trading on past the configured loss because the
number the limit reads leaves out the losses that matter most on a bad day.

**What would make this wrong.** An exit placed outside this system (a manual
square-off in the Kite app). It is not in the orders table, so the round trip
reads as open and is marked at the live price - which is the right figure for
a holding still on, and for a closed one is conservative only if the price
has moved against it since. Reconciliation halts on the holding it can no
longer find (POSITION_NOT_HELD), so the session stops either way.

## What unknown becomes

``None``, never zero. A filled order with no average price, or an open
quantity with no fresh live price, makes the day's figure unknown, and the
daily loss check refuses on unknown (``RiskContext.require``).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from algotrader.common.enums import Side
from algotrader.execution.charges import order_charges

__all__ = ["DayLedger", "RoundTrip", "day_ledger", "round_trips"]


@dataclass(frozen=True)
class DayLedger:
    """The day's money, in rupees. ``None`` fields are unknown, not zero."""

    #: Cash out minus cash in, over round trips that are flat (gross).
    realised: Decimal | None
    #: Open quantity marked at the live price (gross).
    unrealised: Decimal | None
    #: Charges on every executed order, and on the order that would close
    #: each open quantity at the live price.
    charges: Decimal | None
    #: Completed round trips that lost money net of charges, counted back from
    #: the most recent until one made money. Break-even neither extends nor
    #: ends it.
    streak: int | None

    @property
    def net(self) -> Decimal | None:
        """The day's change: realised + unrealised - charges. None if unknown."""
        if self.realised is None or self.unrealised is None or self.charges is None:
            return None
        return self.realised + self.unrealised - self.charges


UNKNOWN = DayLedger(realised=None, unrealised=None, charges=None, streak=None)


@dataclass(frozen=True)
class RoundTrip:
    """One correlation id's fills today: an entry and whatever closed it.

    The unit both the loss limit and the end-of-day statistics count in
    (E15-S15). One definition, so the two cannot disagree about what a trade
    made: the report's net is the loss limit's net by construction.
    """

    correlation_id: UUID | str
    symbol: str
    bought_qty: int
    bought_value: Decimal
    sold_qty: int
    sold_value: Decimal
    #: Charges on the executed orders only - not on any order still to close.
    charges: Decimal
    last_fill: dt.datetime | None

    @property
    def open_qty(self) -> int:
        return self.bought_qty - self.sold_qty

    @property
    def cash(self) -> Decimal:
        """Cash in minus cash out, gross: the realised P&L once ``open_qty`` is 0."""
        return self.sold_value - self.bought_value


@dataclass
class _Trip:
    symbol: str
    bought_qty: int = 0
    bought_value: Decimal = Decimal(0)
    sold_qty: int = 0
    sold_value: Decimal = Decimal(0)
    charges: Decimal = Decimal(0)
    last_fill: dt.datetime | None = None


def round_trips(orders: Iterable[Mapping[str, Any]]) -> tuple[RoundTrip, ...] | None:
    """Every correlation id with fills in ``orders``, in the order first seen.

    ``None`` - not an empty tuple - when a filled order has no average price:
    a fill we cannot price is a fill we cannot account for.
    """
    trips: dict[UUID | str, _Trip] = {}
    for order in orders:
        filled = int(order.get("filled_quantity") or 0)
        if filled <= 0:
            continue
        price = order.get("average_price")
        if price is None:
            return None
        price = Decimal(price)
        side = Side(order["side"])
        trip = trips.setdefault(order["correlation_id"], _Trip(symbol=str(order["symbol"])))
        if side is Side.BUY:
            trip.bought_qty += filled
            trip.bought_value += price * filled
        else:
            trip.sold_qty += filled
            trip.sold_value += price * filled
        trip.charges += order_charges(side, price, filled)
        updated = order.get("last_update_at")
        if isinstance(updated, dt.datetime) and (
            trip.last_fill is None or updated > trip.last_fill
        ):
            trip.last_fill = updated
    return tuple(
        RoundTrip(
            correlation_id=cid,
            symbol=t.symbol,
            bought_qty=t.bought_qty,
            bought_value=t.bought_value,
            sold_qty=t.sold_qty,
            sold_value=t.sold_value,
            charges=t.charges,
            last_fill=t.last_fill,
        )
        for cid, t in trips.items()
    )


def day_ledger(
    orders: Iterable[Mapping[str, Any]],
    last_price: Callable[[str], Decimal | None],
) -> DayLedger:
    """The ledger of ``orders`` (today's, any status), marking open quantity.

    ``last_price(symbol)`` is the live price, or ``None`` when no fresh one
    exists. It is asked only for symbols with open quantity.
    """
    trips = round_trips(orders)
    if trips is None:
        return UNKNOWN

    realised = Decimal(0)
    unrealised: Decimal | None = Decimal(0)
    charges = Decimal(0)
    finished: list[tuple[dt.datetime, Decimal]] = []
    for trip in trips:
        cash = trip.cash
        charges += trip.charges
        if trip.open_qty == 0:
            realised += cash
            when = trip.last_fill or dt.datetime.min.replace(tzinfo=dt.UTC)
            finished.append((when, cash - trip.charges))
            continue
        ltp = last_price(trip.symbol)
        if ltp is None or unrealised is None:
            unrealised = None
            continue
        unrealised += cash + trip.open_qty * ltp
        closing_side = Side.SELL if trip.open_qty > 0 else Side.BUY
        charges += order_charges(closing_side, ltp, abs(trip.open_qty))

    streak = 0
    for _, net in sorted(finished, key=lambda item: item[0], reverse=True):
        if net > 0:
            break
        if net < 0:
            streak += 1
    return DayLedger(
        realised=realised,
        unrealised=unrealised,
        charges=charges if unrealised is not None else None,
        streak=streak,
    )
