"""The broker account the system tests share: Kite's payloads and Kite's rules.

Audit ENG-008 (E24-S07, E24-S08): every broker fake in the system tests was
more permissive than Kite - any price grid, any keyword, any tag, every MARKET
order filled in full at one price - so a system test could prove a capability
the venue does not have. This is the ONE Kite-client double the E15 SIT and the
execution-service integration tests run the real ``KiteTradingAdapter`` over.
A second permissive copy is the defect this module exists to prevent.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from kiteconnect import KiteConnect
from kiteconnect import exceptions as kx

__all__ = ["PLACE_ORDER_KEYWORDS", "StrictKiteAccount", "check_place_order"]

#: The keywords the INSTALLED SDK's ``KiteConnect.place_order`` takes - read
#: from it, not listed by hand (a hand list here once named ``autoslice``,
#: which 5.2.1 does not accept). A keyword outside it is a TypeError there.
PLACE_ORDER_KEYWORDS = frozenset(inspect.signature(KiteConnect.place_order).parameters) - {"self"}
TAG = re.compile(r"[A-Za-z0-9]{1,20}")


def check_place_order(params: dict[str, Any]) -> None:
    """What any Kite-client double must refuse, whatever else it models.

    For the component doubles that record what the adapter sends (E24-S08):
    a keyword the SDK does not take, and a tag that is not "alphanumeric, max
    20 chars" (kite.trade/docs/connect/v3/orders, read 20 Nov 2026). Tick, lot
    and market protection need the venue's rules and live in
    :class:`StrictKiteAccount`.
    """
    unknown = set(params) - PLACE_ORDER_KEYWORDS
    if unknown:
        raise kx.InputException(
            f"place_order() got unexpected keywords {sorted(unknown)}", code=400
        )
    tag = params.get("tag")
    if tag is not None and not TAG.fullmatch(str(tag)):
        raise kx.InputException("tag must be alphanumeric, at most 20 characters", code=400)


class StrictKiteAccount:
    """Kite's payloads, and the order rules Kite and the exchange apply (audit ENG-008).

    Until 20 Nov 2026 this accepted anything: any price grid, any keyword, any
    tag, and filled every MARKET order in full at one constant price, so a
    regression in tick snapping or order shape on the money path passed every
    SIT that used it. It now refuses what the venue refuses, each rule with its
    source:

    * **Tick** - a trigger or price off the instrument's grid. The exchange's
      rule (NSE/CMTR/67133 sets the grid by price band); the grid is the
      instrument master's, the same resolver the adapter snaps to.
    * **Lot** - a quantity that is not a positive whole number of lots (one
      share for NSE equity).
    * **Tag** - "alphanumeric, max 20 chars" (kite.trade/docs/connect/v3/orders,
      read 20 Nov 2026).
    * **Market protection** - "-1 (auto protection)" or "greater than 0 and up
      to 100" for MARKET and SL-M (same page). The page does not say it is
      mandatory; requiring it is THIS system's rule (the gateway refuses 0 and
      always sends it), held here so that dropping it fails a SIT.
    * **Order types** - the system sends MARKET and SL-M only; LIMIT and SL are
      refused as unmodelled, never mis-filled (PaperKite's rule too).

    A refusal at placement raises ``InputException``, Kite's type for "bad
    values for parameters" (kite.trade/docs/connect/v3/exceptions). Whether
    Kite refuses an off-tick order synchronously or lists it REJECTED is not
    documented; both reach the gateway as a refusal.

    And tests can configure what a real session does: a fill price per symbol
    that is not the trigger, a partial fill per settle, an order the RMS
    rejects AFTER accepting it (as the documented "Insufficient funds" example
    does - the order exists, then reads REJECTED with a status_message), and a
    stop that triggers when the price crosses it.
    """

    MODELLED_ORDER_TYPES = frozenset({"MARKET", "SL-M"})

    def __init__(
        self,
        *,
        tick_size: Callable[[str], Decimal],
        lot_size: Callable[[str], int] = lambda _symbol: 1,
        fill_price: Decimal = Decimal("1200.50"),
    ) -> None:
        self.book: list[dict[str, Any]] = []
        self.net: dict[str, dict[str, Any]] = {}
        self.placed: list[dict[str, Any]] = []
        self.refused: list[tuple[dict[str, Any], str]] = []
        self._tick = tick_size
        self._lot = lot_size
        #: Where each symbol's MARKET orders fill; ``fill_price`` otherwise.
        self.fill_prices: dict[str, Decimal] = {}
        self._fill_price = fill_price
        #: (symbol, transaction_type, order_type) the RMS rejects after
        #: accepting, with the status_message it gives.
        self.rejects: dict[tuple[str, str, str], str] = {}
        #: The most of a symbol's MARKET order that fills per settle.
        self.fill_at_most: dict[str, int] = {}

    # -- placement -----------------------------------------------------------

    def _refuse(self, params: dict[str, Any], why: str) -> None:
        self.refused.append((params, why))
        raise kx.InputException(why, code=400)

    def _on_grid(self, symbol: str, value: Any) -> bool:
        tick = self._tick(symbol)
        return Decimal(str(value)) % tick == 0

    def place_order(self, **params: Any) -> str:
        try:
            check_place_order(params)
        except kx.InputException as exc:
            self._refuse(params, str(exc))
        symbol, order_type = params["tradingsymbol"], params["order_type"]
        if order_type not in self.MODELLED_ORDER_TYPES:
            self._refuse(params, f"{order_type} orders are not modelled")
        quantity = params["quantity"]
        lot = self._lot(symbol)
        if not isinstance(quantity, int) or quantity <= 0 or quantity % lot:
            self._refuse(params, f"quantity {quantity!r} is not a whole number of {lot}-lots")
        protection = params.get("market_protection")
        if protection is None:
            self._refuse(params, f"a {order_type} order without market_protection")
        if not (Decimal(str(protection)) == -1 or 0 < Decimal(str(protection)) <= 100):
            self._refuse(params, f"market_protection {protection!r} is not -1 or in (0, 100]")
        if order_type == "SL-M":
            trigger = params.get("trigger_price")
            if trigger is None or Decimal(str(trigger)) <= 0:
                self._refuse(params, "an SL-M order needs a positive trigger_price")
            if not self._on_grid(symbol, trigger):
                self._refuse(
                    params,
                    f"trigger_price {trigger} is not a multiple of tick {self._tick(symbol)}",
                )
        if params.get("price") not in (None, 0) and not self._on_grid(symbol, params["price"]):
            self._refuse(params, f"price {params['price']} is off the {self._tick(symbol)} grid")
        self.placed.append(params)
        order_id = f"26082500{len(self.placed):04d}"
        self.book.append(
            {
                "order_id": order_id,
                "tag": params.get("tag"),
                "status": "OPEN" if order_type == "MARKET" else "TRIGGER PENDING",
                "tradingsymbol": symbol,
                "exchange": params.get("exchange", "NSE"),
                "transaction_type": params["transaction_type"],
                "order_type": order_type,
                "product": params["product"],
                "quantity": quantity,
                "filled_quantity": 0,
                "trigger_price": params.get("trigger_price"),
                "order_timestamp": "2026-08-25 10:00:00",
            }
        )
        return order_id

    def orders(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.book]

    def cancel_order(self, variety: str, order_id: str, **_kwargs: Any) -> str:
        for row in self.book:
            if row["order_id"] == order_id:
                row["status"] = "CANCELLED"
        return order_id

    def positions(self) -> dict[str, list[dict[str, Any]]]:
        return {"net": [dict(r) for r in self.net.values()], "day": []}

    def margins(self, segment: str) -> dict[str, Any]:
        return {
            "available": {"cash": 400000.0, "live_balance": 400000.0},
            "utilised": {"debits": 0.0},
        }

    # -- what happens between reads -----------------------------------------

    def trigger(self, symbol: str, ltp: Decimal) -> None:
        """The price reaches ``ltp``: every working stop it crosses fills there."""
        for row in self.book:
            if row["tradingsymbol"] != symbol or row["status"] != "TRIGGER PENDING":
                continue
            stop = Decimal(str(row["trigger_price"]))
            crossed = ltp <= stop if row["transaction_type"] == "SELL" else ltp >= stop
            if crossed:
                self._fill(row, row["quantity"], ltp)

    def settle(self) -> None:
        for row in self.book:
            key = (row["tradingsymbol"], row["transaction_type"], row["order_type"])
            if row["status"] in ("OPEN", "TRIGGER PENDING") and key in self.rejects:
                if row["filled_quantity"] == 0:
                    row.update(status="REJECTED", status_message=self.rejects[key])
                    continue
            if row["order_type"] != "MARKET" or row["status"] != "OPEN":
                continue
            remaining = row["quantity"] - row["filled_quantity"]
            now = min(remaining, self.fill_at_most.get(row["tradingsymbol"], remaining))
            price = self.fill_prices.get(row["tradingsymbol"], self._fill_price)
            self._fill(row, now, price)

    def _fill(self, row: dict[str, Any], quantity: int, price: Decimal) -> None:
        if quantity <= 0:
            return
        before = row["filled_quantity"]
        row["filled_quantity"] = before + quantity
        previous = Decimal(str(row.get("average_price") or 0))
        row["average_price"] = str((previous * before + price * quantity) / row["filled_quantity"])
        if row["filled_quantity"] == row["quantity"]:
            row["status"] = "COMPLETE"
        held = self.net.setdefault(
            row["tradingsymbol"],
            {
                "tradingsymbol": row["tradingsymbol"],
                "exchange": "NSE",
                "product": "MIS",
                "quantity": 0,
                "day_buy_quantity": 0,
                "day_sell_quantity": 0,
                "average_price": float(price),
                "pnl": 0,
            },
        )
        if row["transaction_type"] == "BUY":
            held["quantity"] += quantity
            held["day_buy_quantity"] += quantity
        else:
            held["quantity"] -= quantity
            held["day_sell_quantity"] += quantity
