"""Paper trading: a simulated Kite Connect CLIENT behind the real adapter (E15-S10).

## The design record

**The decision.** Paper mode simulates the broker at the lowest layer that
still lets every line of the live path run: the Kite SDK client.
:class:`PaperKite` answers the handful of ``kiteconnect.KiteConnect`` calls
:class:`~algotrader.broker.kite.trading.KiteTradingAdapter` makes, with Kite's
own payload shapes, statuses and exception types. The adapter, the gateway,
the risk engine, the protective stop, the position manager, reconciliation and
the square-off timer are the SAME objects in paper and live; the one thing
that differs is the client handed to the adapter, chosen in exactly one
place - :func:`trading_client`.

**The alternative rejected.** A ``PaperBroker`` implementing the adapter's
protocol. It is the obvious shape and it would skip precisely the code most
likely to differ at the exchange: tick snapping, the market-protection gate,
the 20-character tag truncation, Kite status mapping, ``require_field`` on
every payload, and the classification of broker exceptions into retryable,
rejected and ambiguous. A paper session that passes around those would
validate a system that is not the one that trades.

*Kite's own sandbox* (``sandbox.kite.trade``) was rejected too, on its
documentation (``kite.trade/docs/connect/v3/agent-setup/sandbox-prompt.md``,
read 3 Nov 2026): orders are "LIMIT only", the ``MARKET`` order type and
``order_margins()`` "return backend errors", and it runs on shared demo
credentials "provided by the session organisers". This system enters and
exits at MARKET, stops with SL-M and sizes from ``order_margins``, so the
sandbox cannot run its order path at all.

**The failure it prevents.** Paper results that are unrepresentative of live
- the build concern on this story: "one stray ``if mode == PAPER`` in the
risk path makes every paper result unrepresentative". And the converse, worse
one: a PAPER process that can reach Kite's order endpoints. ``trading_client``
returns the live client for ``SystemMode.LIVE`` and for nothing else, and
never so much as calls the live factory otherwise; a structural test confines
every ``SystemMode`` branch in the code to config, the service's mode refusal
and that function.

**What would make this wrong.** A Kite behaviour the simulation does not
model and the live path depends on. Recorded, not modelled: the transient
CANCEL PENDING state (cancels land at once here); market protection as a
price band (a protected MARKET order always fills here); circuit limits;
LIMIT and SL orders (the system sends neither - they are refused, never
mis-filled); auction sessions. And state lives in memory: a restart loses the
paper book unless the composition persists it through ``on_change`` - without
that, a restart mid-session reads every open position as not held and halts,
which is safe.

## How the simulation behaves

* **Fills happen on the next read**, as a real broker's arrive between the
  submission and the next poll. ``orders()``, ``positions()``, ``margins()``
  and ``order_margins()`` first match every working order against the
  current quote.
* **Price.** A buy fills at the ask (the LTP if there is none), a sell at the
  bid, then ``slippage_bps`` further against the order, rounded to the tick
  against the order too. Never a guess: a MARKET order with no quote younger
  than ``max_quote_age_seconds`` is REJECTED ("PAPER: no fresh quote"), and an
  SL-M stop with no fresh quote is left pending - nobody can know whether it
  triggered.
* **Partial fills.** One pass fills no more than the visible depth on the far
  side (``ask_qty`` for a buy); the rest stays OPEN for the next pass.
* **Stops.** An SL-M triggers when the LTP crosses its trigger (a sell stop
  at or below, a buy stop at or above) and then fills like a MARKET order.
* **Margin.** An order that increases exposure is REJECTED by "RMS" when its
  margin (quantity x price x ``margin_rate``) exceeds what is available -
  asynchronously, as Kite does: the order exists, then reads REJECTED with a
  status message. Exits are never margin-checked.
* **Charges.** Zerodha's published NSE equity-intraday schedule
  (:data:`ZERODHA_NSE_INTRADAY`), per executed order, out of paper cash.
"""

from __future__ import annotations

import copy
import datetime as dt
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal
from typing import Any, Final, Protocol

from kiteconnect import exceptions as kx

from algotrader.common.calendar import IST
from algotrader.common.enums import SystemMode

log = logging.getLogger(__name__)

__all__ = [
    "ZERODHA_NSE_INTRADAY",
    "ChargeSchedule",
    "PaperConfig",
    "PaperKite",
    "trading_client",
]

_BUY: Final = "BUY"
_SELL: Final = "SELL"
_MARKET: Final = "MARKET"
_SLM: Final = "SL-M"
_WORKING: Final = frozenset({"OPEN", "TRIGGER PENDING"})
_TAG: Final = re.compile(r"[A-Za-z0-9]{1,20}")
#: NSE trading symbols: letters, digits and the '&' and '-' they contain (M&M,
#: BAJAJ-AUTO). Nothing else becomes part of a simulated order.
_SYMBOL: Final = re.compile(r"[A-Z0-9&-]{1,32}")
_PAISE: Final = Decimal("0.01")


# ---------------------------------------------------------------------------
# The one place paper and live differ
# ---------------------------------------------------------------------------


def trading_client(mode: SystemMode, *, live: Callable[[], Any], paper: Callable[[], Any]) -> Any:
    """The Kite client the trading adapter is built over (E15-S10 AC1).

    LIVE gets ``live()``; every other mode gets ``paper()``, and ``live`` is
    not called - no Kite client, no session, nothing that could reach an
    order endpoint is constructed. ``alert_only`` and ``approval`` place no
    orders (the execution service refuses them), so the paper client is the
    safe thing for anything they might read.
    """
    if mode is SystemMode.LIVE:
        return live()
    return paper()


# ---------------------------------------------------------------------------
# Charges and configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChargeSchedule:
    """Per-executed-order charges, as rates of traded value unless stated.

    Defaults are Zerodha's NSE equity-intraday schedule, read from
    zerodha.com/charges on 3 Nov 2026: brokerage 0.03% or Rs 20 per executed
    order, whichever is lower; STT 0.025% on the sell side; exchange
    transaction 0.00307% (NSE's 0.00297% plus the IPFT levy, which NSE's
    circular NSE/FA/73061 of 27 Feb 2026 restructured with "no change in the
    overall outflow"); SEBI Rs 10 per crore; stamp 0.003% on the buy side;
    GST 18% on brokerage + SEBI + transaction. Rates change; re-read the page
    before trusting a paper P&L to the rupee.
    """

    brokerage_rate: Decimal = Decimal("0.0003")
    brokerage_cap: Decimal = Decimal("20")
    stt_sell: Decimal = Decimal("0.00025")
    transaction: Decimal = Decimal("0.0000307")
    sebi: Decimal = Decimal("0.000001")
    stamp_buy: Decimal = Decimal("0.00003")
    gst: Decimal = Decimal("0.18")

    def for_order(self, side: str, value: Decimal) -> Decimal:
        """Total charges for one executed order of ``value`` traded."""
        if value <= 0:
            return Decimal(0)
        brokerage = min(value * self.brokerage_rate, self.brokerage_cap)
        transaction = value * self.transaction
        sebi = value * self.sebi
        stt = value * self.stt_sell if side == _SELL else Decimal(0)
        stamp = value * self.stamp_buy if side == _BUY else Decimal(0)
        gst = (brokerage + transaction + sebi) * self.gst
        total = brokerage + transaction + sebi + stt + stamp + gst
        return total.quantize(_PAISE, rounding=ROUND_HALF_UP)


ZERODHA_NSE_INTRADAY: Final = ChargeSchedule()


@dataclass(frozen=True)
class PaperConfig:
    """How the simulated account behaves. See the module docstring."""

    opening_cash: Decimal = Decimal("500000")
    #: MIS margin as a fraction of order value. 0.20 is 5x, the most SEBI's
    #: peak-margin framework allows intraday for equity (VaR + ELM at least
    #: 20%); real per-stock margins are often higher, which makes this the
    #: optimistic end - a paper fill can be one a live account would refuse.
    margin_rate: Decimal = Decimal("0.20")
    slippage_bps: Decimal = Decimal("5")
    max_quote_age_seconds: float = 10.0
    charges: ChargeSchedule = ZERODHA_NSE_INTRADAY

    def __post_init__(self) -> None:
        if self.opening_cash <= 0:
            raise ValueError("opening_cash must be positive")
        if not 0 < self.margin_rate <= 1:
            raise ValueError(f"margin_rate must be in (0, 1], not {self.margin_rate}")
        if self.slippage_bps < 0:
            raise ValueError("slippage_bps cannot be negative: slippage only ever costs")
        if self.max_quote_age_seconds <= 0:
            raise ValueError("max_quote_age_seconds must be positive")


class Quote(Protocol):
    """What a fill needs from a quote - ``ingest.quotes.QuoteState`` has it."""

    @property
    def ltp(self) -> Decimal: ...
    @property
    def bid(self) -> Decimal | None: ...
    @property
    def ask(self) -> Decimal | None: ...
    @property
    def bid_qty(self) -> int | None: ...
    @property
    def ask_qty(self) -> int | None: ...
    @property
    def as_of(self) -> dt.datetime: ...


# ---------------------------------------------------------------------------
# The simulated book
# ---------------------------------------------------------------------------


@dataclass
class _Order:
    seq: int
    order_id: str
    tag: str
    symbol: str
    side: str
    order_type: str
    quantity: int
    trigger_price: Decimal | None
    placed_at: dt.datetime
    status: str
    updated_at: dt.datetime
    filled: int = 0
    fill_value: Decimal = Decimal(0)
    charges: Decimal = Decimal(0)
    message: str | None = None
    #: The quote this order last filled against: its depth is spent.
    last_quote_at: dt.datetime | None = None

    @property
    def pending(self) -> int:
        return self.quantity - self.filled

    @property
    def average_price(self) -> Decimal | None:
        if self.filled == 0:
            return None
        return (self.fill_value / self.filled).quantize(_PAISE, rounding=ROUND_HALF_UP)

    def row(self) -> dict[str, Any]:
        """Kite's orderbook row, with the fields the adapter reads."""
        average = self.average_price
        return {
            "order_id": self.order_id,
            "tag": self.tag,
            "status": self.status,
            "tradingsymbol": self.symbol,
            "exchange": "NSE",
            "transaction_type": self.side,
            "order_type": self.order_type,
            "product": "MIS",
            "variety": "regular",
            "quantity": self.quantity,
            "filled_quantity": self.filled,
            "pending_quantity": self.pending if self.status in _WORKING else 0,
            "price": 0.0,
            "trigger_price": float(self.trigger_price) if self.trigger_price is not None else 0.0,
            "average_price": float(average) if average is not None else 0.0,
            "order_timestamp": self.placed_at.astimezone(IST).replace(tzinfo=None),
            "exchange_update_timestamp": self.updated_at.astimezone(IST).replace(tzinfo=None),
            "status_message": self.message,
        }


@dataclass
class _Holding:
    symbol: str
    buy_qty: int = 0
    buy_value: Decimal = Decimal(0)
    sell_qty: int = 0
    sell_value: Decimal = Decimal(0)

    @property
    def net(self) -> int:
        return self.buy_qty - self.sell_qty

    @property
    def average_buy(self) -> Decimal:
        return self.buy_value / self.buy_qty if self.buy_qty else Decimal(0)

    @property
    def average_sell(self) -> Decimal:
        return self.sell_value / self.sell_qty if self.sell_qty else Decimal(0)

    @property
    def average_open(self) -> Decimal:
        if self.net > 0:
            return self.average_buy
        if self.net < 0:
            return self.average_sell
        return Decimal(0)

    @property
    def realised(self) -> Decimal:
        """P&L on the quantity both bought and sold today."""
        closed = min(self.buy_qty, self.sell_qty)
        return (self.average_sell - self.average_buy) * closed if closed else Decimal(0)


@dataclass
class _Account:
    next_seq: int = 1
    orders: list[_Order] = field(default_factory=list)
    holdings: dict[str, _Holding] = field(default_factory=dict)
    charges: Decimal = Decimal(0)


class PaperKite:
    """The ``kiteconnect.KiteConnect`` surface the trading adapter calls, simulated.

    Synchronous and thread-safe, like the SDK it stands in for: the adapter
    calls it through ``asyncio.to_thread``. It holds no API key, no access
    token and no HTTP session - there is nothing in it that could reach the
    broker.
    """

    def __init__(
        self,
        *,
        quotes: Callable[[str], Quote | None],
        clock: Callable[[], dt.datetime],
        config: PaperConfig | None = None,
        tick_size: Callable[[str], Decimal] | None = None,
        on_change: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._quotes = quotes
        self._clock = clock
        self._config = config or PaperConfig()
        self._tick = tick_size or (lambda _symbol: Decimal("0.05"))
        self._on_change = on_change
        self._lock = threading.Lock()
        self._account = _Account()

    # -- the Kite calls --------------------------------------------------------

    def place_order(
        self,
        variety: str,
        exchange: str,
        tradingsymbol: str,
        transaction_type: str,
        quantity: int,
        product: str,
        order_type: str,
        *,
        price: float | None = None,
        trigger_price: float | None = None,
        tag: str | None = None,
        market_protection: float | None = None,
        **_: Any,
    ) -> str:
        """Accept an order, as Kite does - refusing only what Kite refuses outright."""
        if variety != "regular" or exchange != "NSE" or product != "MIS":
            raise kx.InputException(
                f"PAPER models regular NSE MIS orders only, not {variety}/{exchange}/{product}",
                code=400,
            )
        if order_type not in {_MARKET, _SLM}:
            # The system sends MARKET and SL-M only. Filling a LIMIT order here
            # would be a guess dressed as a simulation.
            raise kx.InputException(f"PAPER does not model {order_type} orders", code=400)
        if transaction_type not in {_BUY, _SELL}:
            raise kx.InputException(f"invalid transaction_type {transaction_type!r}", code=400)
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
            raise kx.InputException(f"invalid quantity {quantity!r}", code=400)
        if market_protection is None or market_protection == 0:
            raise kx.InputException(
                "Market orders without market protection are not allowed via API.", code=400
            )
        if order_type == _SLM and (trigger_price is None or trigger_price <= 0):
            raise kx.InputException("an SL-M order needs a trigger price", code=400)
        if tag is None or not _TAG.fullmatch(tag):
            raise kx.InputException("tag must be alphanumeric, at most 20 characters", code=400)
        if not _SYMBOL.fullmatch(tradingsymbol):
            raise kx.InputException(f"invalid tradingsymbol {tradingsymbol!r}", code=400)
        now = self._clock()
        with self._lock:
            account = self._account
            order = _Order(
                seq=account.next_seq,
                order_id=f"PAPER-{account.next_seq:06d}",
                tag=tag,
                symbol=tradingsymbol,
                side=transaction_type,
                order_type=order_type,
                quantity=quantity,
                trigger_price=Decimal(str(trigger_price)) if trigger_price is not None else None,
                placed_at=now,
                status="TRIGGER PENDING" if order_type == _SLM else "OPEN",
                updated_at=now,
            )
            account.next_seq += 1
            account.orders.append(order)
            self._changed()
            return order.order_id

    def modify_order(self, variety: str, order_id: str, **params: Any) -> str:
        with self._lock:
            order = self._working(order_id, "modified")
            if "quantity" in params and params["quantity"] is not None:
                quantity = int(params["quantity"])
                if quantity < max(order.filled, 1):
                    raise kx.InputException("quantity below what has filled", code=400)
                order.quantity = quantity
            if "trigger_price" in params and params["trigger_price"] is not None:
                order.trigger_price = Decimal(str(params["trigger_price"]))
            order.updated_at = self._clock()
            self._changed()
            return order_id

    def cancel_order(self, variety: str, order_id: str, **_: Any) -> str:
        with self._lock:
            order = self._working(order_id, "cancelled")
            order.status = "CANCELLED"
            order.updated_at = self._clock()
            self._changed()
            return order_id

    def orders(self) -> list[dict[str, Any]]:
        with self._lock:
            self._match()
            return [o.row() for o in self._account.orders]

    def positions(self) -> dict[str, list[dict[str, Any]]]:
        with self._lock:
            self._match()
            net = []
            for h in self._account.holdings.values():
                quote = self._fresh(h.symbol)
                last = quote.ltp if quote is not None else h.average_open
                unrealised = (last - h.average_open) * h.net if h.net else Decimal(0)
                net.append(
                    {
                        "tradingsymbol": h.symbol,
                        "exchange": "NSE",
                        "product": "MIS",
                        "quantity": h.net,
                        "day_buy_quantity": h.buy_qty,
                        "day_sell_quantity": h.sell_qty,
                        "average_price": float(h.average_open),
                        "last_price": float(last),
                        "pnl": float((h.realised + unrealised).quantize(_PAISE)),
                    }
                )
            return {"net": net, "day": copy.deepcopy(net)}

    def margins(self, segment: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._match()
            cash = self._cash()
            used = self._used()
            return {
                "enabled": True,
                "available": {
                    "cash": float(cash),
                    "opening_balance": float(self._config.opening_cash),
                    "live_balance": float(cash - used),
                },
                "utilised": {"debits": float(used)},
            }

    def order_margins(self, params: list[dict[str, Any]]) -> list[dict[str, Any]]:
        with self._lock:
            self._match()
            out = []
            for p in params:
                symbol = str(p["tradingsymbol"])
                quote = self._fresh(symbol)
                if quote is None:
                    raise kx.DataException(f"PAPER: no fresh quote for {symbol}; margin unknown")
                total = Decimal(int(p["quantity"])) * quote.ltp * self._config.margin_rate
                out.append(
                    {
                        "type": "equity",
                        "tradingsymbol": symbol,
                        "exchange": "NSE",
                        "total": float(total.quantize(_PAISE, rounding=ROUND_UP)),
                    }
                )
            return out

    # -- what the composition needs -------------------------------------------

    @property
    def charges_total(self) -> Decimal:
        with self._lock:
            return self._account.charges

    def snapshot(self) -> dict[str, Any]:
        """The whole paper account, JSON-able, for ``on_change`` to persist."""
        with self._lock:
            return self._snapshot()

    def restore(self, state: dict[str, Any]) -> None:
        with self._lock:
            account = _Account(next_seq=int(state["next_seq"]), charges=Decimal(state["charges"]))
            for o in state["orders"]:
                account.orders.append(
                    _Order(
                        seq=int(o["seq"]),
                        order_id=o["order_id"],
                        tag=o["tag"],
                        symbol=o["symbol"],
                        side=o["side"],
                        order_type=o["order_type"],
                        quantity=int(o["quantity"]),
                        trigger_price=(
                            Decimal(o["trigger_price"]) if o["trigger_price"] is not None else None
                        ),
                        placed_at=dt.datetime.fromisoformat(o["placed_at"]),
                        status=o["status"],
                        updated_at=dt.datetime.fromisoformat(o["updated_at"]),
                        filled=int(o["filled"]),
                        fill_value=Decimal(o["fill_value"]),
                        charges=Decimal(o["charges"]),
                        message=o["message"],
                        last_quote_at=(
                            dt.datetime.fromisoformat(o["last_quote_at"])
                            if o.get("last_quote_at")
                            else None
                        ),
                    )
                )
            for h in state["holdings"]:
                account.holdings[h["symbol"]] = _Holding(
                    symbol=h["symbol"],
                    buy_qty=int(h["buy_qty"]),
                    buy_value=Decimal(h["buy_value"]),
                    sell_qty=int(h["sell_qty"]),
                    sell_value=Decimal(h["sell_value"]),
                )
            self._account = account

    # -- the simulation ---------------------------------------------------------

    def _working(self, order_id: str, verb: str) -> _Order:
        for order in self._account.orders:
            if order.order_id == order_id:
                if order.status not in _WORKING:
                    raise kx.InputException(
                        f"Order cannot be {verb} as it is {order.status.lower()}", code=400
                    )
                return order
        raise kx.InputException(f"no order {order_id}", code=400)

    def _fresh(self, symbol: str) -> Quote | None:
        quote = self._quotes(symbol)
        if quote is None or quote.ltp <= 0:
            return None
        age = (self._clock() - quote.as_of).total_seconds()
        if age > self._config.max_quote_age_seconds or age < -5:
            return None
        return quote

    def _fill_price(self, order: _Order, quote: Quote) -> Decimal:
        if order.side == _BUY:
            base = quote.ask if quote.ask is not None and quote.ask > 0 else quote.ltp
            price = base * (1 + self._config.slippage_bps / Decimal(10000))
            rounding = ROUND_UP
        else:
            base = quote.bid if quote.bid is not None and quote.bid > 0 else quote.ltp
            price = base * (1 - self._config.slippage_bps / Decimal(10000))
            rounding = ROUND_DOWN
        tick = self._tick(order.symbol)
        return (price / tick).quantize(Decimal(1), rounding=rounding) * tick

    def _holding(self, symbol: str) -> _Holding:
        return self._account.holdings.setdefault(symbol, _Holding(symbol=symbol))

    def _increase(self, order: _Order, quantity: int) -> int:
        """How much of ``quantity`` adds exposure rather than closing it."""
        net = self._holding(order.symbol).net
        closing = min(quantity, -net) if order.side == _BUY and net < 0 else 0
        if order.side == _SELL and net > 0:
            closing = min(quantity, net)
        return quantity - closing

    def _cash(self) -> Decimal:
        realised = sum((h.realised for h in self._account.holdings.values()), Decimal(0))
        return self._config.opening_cash + realised - self._account.charges

    def _used(self) -> Decimal:
        return sum(
            (
                (abs(h.net) * h.average_open * self._config.margin_rate)
                for h in self._account.holdings.values()
            ),
            Decimal(0),
        )

    def _reject(self, order: _Order, message: str, now: dt.datetime) -> None:
        order.status = "REJECTED"
        order.message = message
        order.updated_at = now
        log.info("PAPER rejected %s %s %s: %s", order.order_id, order.side, order.symbol, message)

    def _match(self) -> None:
        now = self._clock()
        changed = False
        for order in sorted(self._account.orders, key=lambda o: o.seq):
            if order.status not in _WORKING:
                continue
            quote = self._fresh(order.symbol)
            if order.status == "TRIGGER PENDING":
                if quote is None:
                    continue  # nobody can know whether it triggered
                trigger = order.trigger_price or Decimal(0)
                crossed = quote.ltp <= trigger if order.side == _SELL else quote.ltp >= trigger
                if not crossed:
                    continue
                order.status = "OPEN"
                order.updated_at = now
                changed = True
            if quote is None:
                if order.filled == 0:
                    self._reject(order, f"PAPER: no fresh quote for {order.symbol}", now)
                    changed = True
                continue
            if order.last_quote_at is not None and quote.as_of <= order.last_quote_at:
                # This quote's depth has already been taken by this order. One
                # reconciliation cycle reads the broker several times; matching
                # on each read would fill the same visible depth three times
                # over at one instant (QA-E15-42). New liquidity is a new quote.
                continue
            depth = quote.ask_qty if order.side == _BUY else quote.bid_qty
            quantity = order.pending if not depth or depth <= 0 else min(order.pending, depth)
            price = self._fill_price(order, quote)
            # Margin is blocked for the WHOLE order when it is accepted, as
            # Kite's RMS does - not for the slice this pass can fill.
            adds = self._increase(order, order.pending)
            if adds and order.filled == 0:
                required = adds * price * self._config.margin_rate
                available = self._cash() - self._used()
                if required > available:
                    self._reject(
                        order,
                        f"RMS:Margin Exceeds, Required:{required.quantize(_PAISE)}, "
                        f"Available:{available.quantize(_PAISE)} (PAPER)",
                        now,
                    )
                    changed = True
                    continue
            self._fill(order, quantity, price, now)
            order.last_quote_at = quote.as_of
            changed = True
        if changed:
            self._changed()

    def _fill(self, order: _Order, quantity: int, price: Decimal, now: dt.datetime) -> None:
        holding = self._holding(order.symbol)
        value = price * quantity
        if order.side == _BUY:
            holding.buy_qty += quantity
            holding.buy_value += value
        else:
            holding.sell_qty += quantity
            holding.sell_value += value
        order.filled += quantity
        order.fill_value += value
        # Charges are per EXECUTED ORDER, so a partially filled order is
        # charged on its running total, never once per fill.
        charges = self._config.charges.for_order(order.side, order.fill_value)
        self._account.charges += charges - order.charges
        order.charges = charges
        order.updated_at = now
        if order.pending == 0:
            order.status = "COMPLETE"

    def _changed(self) -> None:
        if self._on_change is not None:
            self._on_change(self._snapshot())

    def _snapshot(self) -> dict[str, Any]:
        account = self._account
        return {
            "next_seq": account.next_seq,
            "charges": str(account.charges),
            "orders": [
                {
                    "seq": o.seq,
                    "order_id": o.order_id,
                    "tag": o.tag,
                    "symbol": o.symbol,
                    "side": o.side,
                    "order_type": o.order_type,
                    "quantity": o.quantity,
                    "trigger_price": str(o.trigger_price) if o.trigger_price is not None else None,
                    "placed_at": o.placed_at.isoformat(),
                    "status": o.status,
                    "updated_at": o.updated_at.isoformat(),
                    "filled": o.filled,
                    "fill_value": str(o.fill_value),
                    "charges": str(o.charges),
                    "message": o.message,
                    "last_quote_at": (
                        o.last_quote_at.isoformat() if o.last_quote_at is not None else None
                    ),
                }
                for o in account.orders
            ],
            "holdings": [
                {
                    "symbol": h.symbol,
                    "buy_qty": h.buy_qty,
                    "buy_value": str(h.buy_value),
                    "sell_qty": h.sell_qty,
                    "sell_value": str(h.sell_value),
                }
                for h in account.holdings.values()
            ],
        }
