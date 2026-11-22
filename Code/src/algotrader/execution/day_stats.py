"""What the day did, trade by trade (E15-S15, task 2).

## The design record

**The decision.** The day's statistics are built on the same
:class:`~algotrader.execution.day_ledger.RoundTrip` the daily loss limit
counts in, and the day's totals ARE the loss limit's
:class:`~algotrader.execution.day_ledger.DayLedger` - held, not recomputed.
A trade is one round trip; its R-multiple divides its NET result by the risk
it was opened with, read off its closed ``positions`` row.

**The alternatives rejected.**

* *Statistics from the closed positions alone.* What a journal would
  naturally read, and it misses exactly the trades that matter on a bad day:
  a fill exited before it was ever booked (an unprotected fill, a restart)
  leaves no positions row - RSK-004's hole in the loss limit, reopened in the
  report. The positions rows give each trade its risk; the orders give every
  trade.
* *A "scratch" band in R (say, within 0.1 R of break-even).* A tunable number
  nobody has chosen, and one that would disagree with the losing streak:
  ``day_ledger`` counts any trade that lost money net of charges as a loss,
  and the consecutive-loss latch halts on that count. A report saying
  "0 losses, 3 scratches" on the day that latch armed is SIT-001 in a
  report - the label contradicting the control. So WIN, LOSS and SCRATCH are
  the sign of the net, the streak's own definition. A trade exited at its
  entry price shows its gross of zero beside a net LOSS; the charges are what
  it cost.
* *Gross R.* The sizer budgets risk as price distance, so gross is the
  textbook R. Net is what the trade did to the account, and the figure the
  owner compares across days; gross stays visible per trade.

**The failure it prevents.** A day report that disagrees with the number the
loss limit acted on, and a trade that vanishes from the count because it was
never booked.

**What would make this wrong.** A trade whose orders carry two correlation
ids (a re-entry under the original id would merge two trades into one), or a
stop moved after entry: ``stop_price`` is anchored at the fill and never
updated today, which is what makes it the initial risk. A trailing stop that
rewrote the column would turn every R into "result over the last risk".

## What unknown becomes

``None``. No statistics when a fill has no price (``round_trips`` says so);
no day net when an open quantity has no fresh price (``DayLedger.net``); no R
for a trade with no closed position row to say what it risked.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Any, Final
from uuid import UUID

from algotrader.execution.day_ledger import DayLedger, RoundTrip, day_ledger, round_trips

__all__ = ["DayStatistics", "TradeOutcome", "TradeResult", "day_statistics"]

#: R is reported to the hundredth: finer is noise against a fill's slippage.
R_PLACES: Final = Decimal("0.01")


class TradeOutcome(StrEnum):
    """The sign of a trade's NET result - the losing streak's own definition."""

    WIN = "WIN"
    LOSS = "LOSS"
    SCRATCH = "SCRATCH"


@dataclass(frozen=True)
class TradeResult:
    """One finished round trip."""

    correlation_id: UUID | str
    symbol: str
    #: Shares bought (= sold: the trip is flat).
    quantity: int
    gross: Decimal
    charges: Decimal
    #: The closed positions row this trade was booked as, or ``None`` - the
    #: fill was exited without ever entering the book.
    position_id: int | None
    exit_reason: str | None
    #: ``net / initial risk``, or ``None`` with no row to read the risk from.
    r_multiple: Decimal | None

    @property
    def net(self) -> Decimal:
        return self.gross - self.charges

    @property
    def outcome(self) -> TradeOutcome:
        if self.net > 0:
            return TradeOutcome.WIN
        if self.net < 0:
            return TradeOutcome.LOSS
        return TradeOutcome.SCRATCH


@dataclass(frozen=True)
class DayStatistics:
    """The day, from today's orders and today's closed positions."""

    trade_date: dt.date
    #: Finished round trips, in the order they finished.
    trades: tuple[TradeResult, ...]
    #: Symbols whose round trip is still open - on a flat day, none.
    still_open: tuple[str, ...]
    #: Closed positions rows no finished round trip explains: a position closed
    #: by something other than an order this system placed (a hand edit, an
    #: operator closing a POSITION_NOT_HELD row). Always a discrepancy.
    unexplained_closes: tuple[int, ...]
    #: The daily loss limit's own figure for the same orders (AC2).
    ledger: DayLedger

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.outcome is TradeOutcome.WIN)

    @property
    def losses(self) -> int:
        return sum(1 for t in self.trades if t.outcome is TradeOutcome.LOSS)

    @property
    def scratches(self) -> int:
        return sum(1 for t in self.trades if t.outcome is TradeOutcome.SCRATCH)

    @property
    def booked(self) -> int:
        """Trades that were positions in the book. Equals the closed rows that
        explain a trade: one row, one trade."""
        return sum(1 for t in self.trades if t.position_id is not None)

    @property
    def unbooked(self) -> tuple[TradeResult, ...]:
        """Trades with no positions row - a fill exited before it was booked."""
        return tuple(t for t in self.trades if t.position_id is None)

    @property
    def net(self) -> Decimal | None:
        return self.ledger.net

    @property
    def gross(self) -> Decimal | None:
        if self.ledger.realised is None or self.ledger.unrealised is None:
            return None
        return self.ledger.realised + self.ledger.unrealised

    @property
    def reconciles(self) -> bool:
        """Every closed position is a trade here, and every trade is flat.

        An unbooked trade does not break this: it is a real trade that is
        counted and named, not a disagreement between the two records.
        """
        return not self.unexplained_closes and not self.still_open


def _r_multiple(net: Decimal, row: Mapping[str, Any]) -> Decimal | None:
    entry, stop, quantity = row.get("entry_price"), row.get("stop_price"), row.get("quantity")
    if entry is None or stop is None or not quantity:
        return None
    risk = abs(Decimal(entry) - Decimal(stop)) * int(quantity)
    if risk <= 0:
        # Position refuses a stop at the entry; a row that has one was not
        # written by this system, and a ratio over it means nothing.
        return None
    return (net / risk).quantize(R_PLACES, rounding=ROUND_HALF_EVEN)


def _finished_order(trip: RoundTrip) -> dt.datetime:
    return trip.last_fill or dt.datetime.min.replace(tzinfo=dt.UTC)


def day_statistics(
    *,
    trade_date: dt.date,
    orders: Iterable[Mapping[str, Any]],
    closed_positions: Sequence[Mapping[str, Any]],
    last_price: Callable[[str], Decimal | None],
) -> DayStatistics | None:
    """The day's statistics, or ``None`` when a fill cannot be priced.

    ``orders`` are today's, any status - the rows ``day_ledger`` reads.
    ``closed_positions`` are the rows closed today. ``last_price`` marks a
    round trip still open, exactly as for the loss limit.
    """
    orders = list(orders)
    trips = round_trips(orders)
    if trips is None:
        return None
    rows: dict[str, Mapping[str, Any]] = {str(r["correlation_id"]): r for r in closed_positions}

    trades: list[TradeResult] = []
    still_open: list[str] = []
    explained: set[str] = set()
    for trip in sorted(trips, key=_finished_order):
        if trip.open_qty != 0:
            still_open.append(trip.symbol)
            continue
        cid = str(trip.correlation_id)
        row = rows.get(cid)
        if row is not None:
            explained.add(cid)
        net = trip.cash - trip.charges
        trades.append(
            TradeResult(
                correlation_id=trip.correlation_id,
                symbol=trip.symbol,
                quantity=trip.bought_qty,
                gross=trip.cash,
                charges=trip.charges,
                position_id=None if row is None else int(row["position_id"]),
                exit_reason=None if row is None else str(row.get("exit_reason")),
                r_multiple=None if row is None else _r_multiple(net, row),
            )
        )
    unexplained = tuple(int(r["position_id"]) for cid, r in rows.items() if cid not in explained)
    return DayStatistics(
        trade_date=trade_date,
        trades=tuple(trades),
        still_open=tuple(still_open),
        unexplained_closes=unexplained,
        ledger=day_ledger(orders, last_price),
    )
