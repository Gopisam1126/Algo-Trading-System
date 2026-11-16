"""Transaction charges on an executed intraday equity order (audit RSK-005).

The daily loss limit counted GROSS realised P&L, so the account was always
worse than the figure the limit read - by brokerage, STT, exchange charges,
SEBI fees, stamp duty and GST on every round trip. This module prices them.

## The schedule, and where it comes from

Zerodha's published charges for **equity intraday**, read from
https://zerodha.com/charges on **16 Nov 2026** (the page shows no effective
date). Verbatim:

* Brokerage - "0.03% or Rs. 20/executed order whichever is lower"
* STT/CTT - "0.025% on the sell side"
* Transaction charges - "NSE: 0.00307%", "BSE: 0.00375%"
* GST - "18% on (brokerage + SEBI charges + transaction charges)"
* SEBI charges - "₹10 / crore"
* Stamp charges - "0.003% or ₹300 / crore on buy side"

Not modelled: NSE's IPFT (₹0.01 per crore, below a paisa on any order this
system places), the ₹20 debit-balance surcharge and the ₹50 dealer-assisted
order charge (neither applies to an API order on a funded account).

**These are a broker's and an exchange's numbers and they change** - NSE
revised its transaction charge in 2024, and SEBI revises STT in budgets.
``SCHEDULE_RETRIEVED`` dates them; re-read the page and update both together.
An estimate that is a little high only halts a little early, so where the page
rounds, this rounds UP.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import ROUND_UP, Decimal
from typing import Final

from algotrader.common.enums import Side

__all__ = [
    "SCHEDULE",
    "SCHEDULE_RETRIEVED",
    "SCHEDULE_SOURCE",
    "ChargeSchedule",
    "order_charges",
]

SCHEDULE_SOURCE: Final = "https://zerodha.com/charges (equity intraday)"
SCHEDULE_RETRIEVED: Final = dt.date(2026, 11, 16)

_PAISA: Final = Decimal("0.01")
_PCT: Final = Decimal(100)


@dataclass(frozen=True)
class ChargeSchedule:
    """Rates as fractions of turnover, except the brokerage cap in rupees."""

    brokerage_rate: Decimal
    brokerage_cap: Decimal
    stt_sell_rate: Decimal
    transaction_rate: dict[str, Decimal]
    sebi_rate: Decimal
    stamp_buy_rate: Decimal
    gst_rate: Decimal

    def __post_init__(self) -> None:
        for name in (
            "brokerage_rate",
            "brokerage_cap",
            "stt_sell_rate",
            "sebi_rate",
            "stamp_buy_rate",
            "gst_rate",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"ChargeSchedule.{name} must be a finite Decimal >= 0")


SCHEDULE: Final = ChargeSchedule(
    brokerage_rate=Decimal("0.03") / _PCT,
    brokerage_cap=Decimal("20"),
    stt_sell_rate=Decimal("0.025") / _PCT,
    transaction_rate={"NSE": Decimal("0.00307") / _PCT, "BSE": Decimal("0.00375") / _PCT},
    # Rs 10 per crore = 10 / 1,00,00,000.
    sebi_rate=Decimal(10) / Decimal(10_000_000),
    stamp_buy_rate=Decimal("0.003") / _PCT,
    gst_rate=Decimal("18") / _PCT,
)


def order_charges(
    side: Side,
    price: Decimal,
    quantity: int,
    *,
    exchange: str = "NSE",
    schedule: ChargeSchedule = SCHEDULE,
) -> Decimal:
    """All charges on ONE executed order, in rupees, rounded up to the paisa.

    Raises on an exchange the schedule does not know: an unknown venue's
    charges are not zero, and guessing them low would loosen the loss limit.
    """
    if quantity < 0 or not price.is_finite() or price < 0:
        raise ValueError(f"cannot price an order of {quantity} at {price}")
    if quantity == 0:
        return Decimal(0)
    try:
        transaction_rate = schedule.transaction_rate[exchange]
    except KeyError:
        raise ValueError(f"no transaction-charge rate for exchange {exchange!r}") from None
    turnover = price * quantity
    brokerage = min(turnover * schedule.brokerage_rate, schedule.brokerage_cap)
    transaction = turnover * transaction_rate
    sebi = turnover * schedule.sebi_rate
    stt = turnover * schedule.stt_sell_rate if side is Side.SELL else Decimal(0)
    stamp = turnover * schedule.stamp_buy_rate if side is Side.BUY else Decimal(0)
    gst = (brokerage + sebi + transaction) * schedule.gst_rate
    total = brokerage + transaction + sebi + stt + stamp + gst
    return total.quantize(_PAISA, rounding=ROUND_UP)
