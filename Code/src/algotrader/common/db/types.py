"""Shared column types.

Defined once so that "money is NUMERIC(14,4)" is a fact with one definition
rather than a convention repeated across fifteen tables — the kind of thing
that stays consistent right up until the one column somebody types by hand.

The precisions are business decisions, not arbitrary:

- ``Money`` — ``NUMERIC(14,4)``. Four decimal places because NSE tick sizes go
  to ₹0.01 and VWAP/average-fill prices carry more precision than that; ten
  integer digits because ₹99,99,99,99,99 is comfortably beyond any position
  this system will hold. **Never float** — float error accumulates in P&L and
  breaks tick-size equality comparison (BR-10).
- ``Charge`` — ``NUMERIC(10,2)``. Brokerage, STT, GST and stamp duty are all
  billed to the paisa, and are itemised per fill for tax reconciliation (BR-13).
- ``Ts`` — ``TIMESTAMP(timezone=True)`` everywhere, never naive (BR-9). A naive
  timestamp in a market with a fixed session is a recurring, silent bug class.
"""

from __future__ import annotations

from sqlalchemy import Numeric
from sqlalchemy.dialects.postgresql import TIMESTAMP

#: Prices, and any rupee amount that participates in arithmetic.
Money = Numeric(14, 4)

#: Itemised transaction charges (BR-13).
Charge = Numeric(10, 2)

#: Realised P&L — two places is enough, and matches contract-note rounding.
Pnl = Numeric(14, 2)

#: Percentages: circuit bands, drawdown, gap.
Pct5 = Numeric(5, 2)
Pct6 = Numeric(6, 3)

#: Statistical measures — Sharpe, deflated Sharpe.
Stat = Numeric(8, 4)

#: Probabilities in [0, 1] — PBO, AI confidence.
Prob = Numeric(5, 4)

#: Corporate action adjustment factor. Ten decimal places because factors
#: multiply: a symbol with several splits accumulates a product, and rounding
#: each one to four places would drift the earliest prices measurably.
AdjFactor = Numeric(18, 10)

#: Timezone-aware timestamp. The only kind this system stores.
Ts = TIMESTAMP(timezone=True)

#: SHA-256 hex digest, fixed width.
HASH_LEN = 64

__all__ = [
    "HASH_LEN",
    "AdjFactor",
    "Charge",
    "Money",
    "Pct5",
    "Pct6",
    "Pnl",
    "Prob",
    "Stat",
    "Ts",
]
