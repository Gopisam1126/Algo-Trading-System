"""The end of the day: is the book flat, and what did the day do (E15-S15).

``LOW_LEVEL_ARCHITECTURE.md §11.4``'s runbook line - *"EOD reconciliation,
journal write, performance report"* - without the journal, which waits for a
regime producer (E15-S11). This module answers two questions once the books
have closed: is anything left, and what did today's trades come to.

## The design record

**The decision.** Once nothing can trade any more - the close, or the end of
the closing auction where one runs later (``books_close``) - run ONE final
reconciliation cycle, then read the account afresh and judge it: no position
in the book, no positions row still open, no MIS quantity at the broker, no
MIS order the broker lists as working, and none of our order records
unsettled. Anything else, including a cycle or a read that failed, is a
``NOT_FLAT`` verdict that names each leftover and arms the kill switch with
``BOOK_NOT_FLAT``. The latch has no TTL, so the next session refuses every
signal until a named operator clears it (``scripts/halt.py clear``).

**The alternatives rejected.**

* *Judge from the final cycle's own snapshot.* The cycle acts - books a fill,
  closes a position, sends an exit - after it reads, so its snapshot is of the
  account BEFORE its actions. The verdict must be about the account after
  them, so it reads again.
* *Read positions first, as the cycle does.* The cycle reads positions first
  because its question is "is anything unexplained?" and the race it avoids
  is a false halt. This question is "is anything left?", and its race runs the
  other way: positions read flat, then an order that was working fills, and
  the orderbook - read second - shows it FILLED, not working. A false FLAT.
  So the ORDERBOOK is read first: anything working at that read is named, and
  anything that filled after it is in the positions read that follows. A
  false FLAT now needs an order PLACED between the two reads - no different
  from one placed a second after the verdict.
* *A verdict that says "unknown" without halting.* Fail closed: a book that
  could not be seen is not a book anyone can vouch for, and the cost of the
  halt is one operator check before 09:15.
* *Judge at a fixed 15:35* (the runbook's time). The closing auction moved to
  15:30-15:40 on 7 Dec 2026 (``CAS_SCHEDULE``); at 15:35 an auction order can
  still execute. ``books_close`` reads the schedule for the day.

**The failure it prevents.** A position or a working order left after
square-off - the timer failed, a stop was never cancelled, the broker could
not square off a stock locked at its circuit - found at 09:15 the next
morning, if at all, instead of the evening it happened.

**What would make this wrong.** A broker that settles MIS positions into
delivery overnight (Zerodha converts an MIS position it could not square off
to CNC): the verdict names it tonight as MIS, and tomorrow it is a CNC holding
that reconciliation - correctly - ignores. And a broker API that fails often
enough at the close that ``UNVERIFIED`` halts become routine: then the run
should retry its reads before concluding, rather than halt on the first.

## A verdict before the close is refused

Run at 14:00, every open position is a leftover, and the halt would stop
tomorrow on a book that was about to be squared off. ``run`` raises before
the books close and on a day with no session, rather than return a verdict
that is wrong by construction.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, Protocol

from algotrader.broker.adapter import BrokerPosition
from algotrader.common.audit import AuditEntry
from algotrader.common.calendar import IST, MARKET_CLOSE, MarketCalendar, cas_session_on
from algotrader.common.db.models import DecisionLog
from algotrader.common.enums import OrderStatus, Product
from algotrader.common.logging import redact
from algotrader.common.models.trading import Order
from algotrader.common.text import one_safe_line
from algotrader.execution.day_stats import DayStatistics, day_statistics
from algotrader.execution.halt import HaltReason
from algotrader.execution.positions import TrackedPosition
from algotrader.execution.reconciliation import TRADED_PRODUCT, BrokerView, CycleReport

log = logging.getLogger(__name__)

__all__ = [
    "SETTLED",
    "DayReport",
    "EndOfDay",
    "FlatVerdict",
    "Leftover",
    "LeftoverKind",
    "books_close",
    "judge",
]

STAGE: Final = "END_OF_DAY"
SERVICE: Final = "end-of-day"
FLAT: Final = "FLAT"
NOT_FLAT: Final = "NOT_FLAT"

#: How many leftovers a verdict names before "and N more". Bounds what reaches
#: the log, the halt record (Redis, 2 KB) and the audit payload - the thing
#: that reaches the sink, not the count of things found (QA-SEC-29).
MAX_NAMED: Final = 8
#: How many trades the audit row carries. A day holds at most a few dozen; the
#: bound is for the day something has gone wrong.
MAX_TRADES_RECORDED: Final = 100

#: Our order records that are finished. An ALLOWLIST: a status nobody listed
#: here - RECONCILE_REQUIRED above all - is unsettled, and so not flat.
#: SUBMIT_FAILED is settled: the broker never listed the order, and the final
#: cycle would have moved it to RECONCILE_REQUIRED had it appeared since.
SETTLED: Final = frozenset(
    {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.SUBMIT_FAILED}
)

#: Every verdict for one trade date correlates in the audit chain.
_NAMESPACE: Final = uuid.UUID("6b1c0e8e-4f7a-5d0e-9a51-e15515e0d0f1")


def _assert_fits_audit_columns() -> None:
    """Refuse to load if what this module writes would not fit the table."""
    columns = DecisionLog.__table__.c
    for name, values in (("stage", [STAGE]), ("outcome", [FLAT, NOT_FLAT]), ("service", [SERVICE])):
        width = getattr(columns[name].type, "length", None)
        too_long = [v for v in values if width is None or len(v) > width]
        if too_long:
            raise RuntimeError(f"decision_log.{name} is String({width}); {too_long} would not fit")


_assert_fits_audit_columns()


# ---------------------------------------------------------------------------
# What is left
# ---------------------------------------------------------------------------


class LeftoverKind(StrEnum):
    """What kind of thing the day left behind."""

    #: The position manager still tracks it.
    BOOK_POSITION = "BOOK_POSITION"
    #: A ``positions`` row that is not CLOSED - what tomorrow's restore reads.
    OPEN_RECORD = "OPEN_RECORD"
    #: The broker holds MIS quantity in it.
    BROKER_POSITION = "BROKER_POSITION"
    #: The broker lists an MIS order in it that is not finished.
    WORKING_ORDER = "WORKING_ORDER"
    #: One of our order records is not settled.
    UNSETTLED_ORDER = "UNSETTLED_ORDER"
    #: The final cycle or a read failed: the book could not be seen.
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True)
class Leftover:
    kind: LeftoverKind
    symbol: str
    detail: str

    @classmethod
    def of(cls, kind: LeftoverKind, symbol: object, detail: str) -> Leftover:
        """Built through here: escaped, so a broker's symbol cannot forge a log
        line, and REDACTED first, because a detail reaches two sinks the
        logging filter never sees - the halt record in Redis and the audit
        payload - and a cycle error carries an exception's text (REC-003).
        Redacted before it is cut, so a credential cut short still matches."""
        return cls(
            kind=kind,
            symbol=one_safe_line(redact(str(symbol))),
            detail=one_safe_line(redact(detail)),
        )

    def describe(self) -> str:
        return f"{self.kind.value} {self.symbol}: {self.detail}"


@dataclass(frozen=True)
class FlatVerdict:
    trade_date: dt.date
    at: dt.datetime
    leftovers: tuple[Leftover, ...]

    @property
    def flat(self) -> bool:
        """Flat only with NOTHING left - including nothing unseen."""
        return not self.leftovers

    def describe(self) -> str:
        if self.flat:
            return f"{self.trade_date}: the book is flat"
        named = "; ".join(item.describe() for item in self.leftovers[:MAX_NAMED])
        more = len(self.leftovers) - MAX_NAMED
        tail = f"; and {more} more" if more > 0 else ""
        return f"{self.trade_date}: NOT FLAT - {len(self.leftovers)} left: {named}{tail}"


def books_close(day: dt.date) -> dt.datetime:
    """The last moment anything can execute on ``day``, in UTC.

    The close, or the end of that day's closing auction if it runs later
    (from 3 Aug 2026 to 15:35, from 7 Dec 2026 to 15:40).
    """
    cas = cas_session_on(day)
    end = MARKET_CLOSE if cas is None else max(MARKET_CLOSE, cas.auction_end)
    return dt.datetime.combine(day, end, tzinfo=IST).astimezone(dt.UTC)


def _status(raw: object) -> OrderStatus | None:
    try:
        return OrderStatus(str(raw))
    except ValueError:
        return None


def judge(
    *,
    book: Sequence[TrackedPosition],
    records: Sequence[Mapping[str, Any]],
    positions: Sequence[BrokerPosition],
    orderbook: Sequence[Order],
    ours: Sequence[Mapping[str, Any]],
) -> tuple[Leftover, ...]:
    """Everything left after the books closed. Empty means flat. Pure."""
    found: list[Leftover] = []
    for tracked in book:
        p = tracked.position
        found.append(
            Leftover.of(
                LeftoverKind.BOOK_POSITION,
                p.symbol,
                f"the book tracks {p.direction.value} {p.quantity} as {type(tracked).__name__}",
            )
        )
    for row in records:
        found.append(
            Leftover.of(
                LeftoverKind.OPEN_RECORD,
                row.get("symbol"),
                f"positions row {row.get('position_id')} is {row.get('status')}",
            )
        )
    for held in positions:
        if held.product == TRADED_PRODUCT and held.quantity != 0:
            found.append(
                Leftover.of(
                    LeftoverKind.BROKER_POSITION,
                    held.symbol,
                    f"the broker holds {held.quantity:+d} {held.product} on {held.exchange}",
                )
            )
    for order in orderbook:
        # A status the adapter could not map arrives as RECONCILE_REQUIRED,
        # which is not terminal: an order we cannot read is not a finished one.
        if order.product is Product.MIS and not order.status.is_terminal:
            found.append(
                Leftover.of(
                    LeftoverKind.WORKING_ORDER,
                    order.symbol,
                    f"{order.side.value} {order.order_type.value} {order.quantity} "
                    f"({order.filled_quantity} filled) is {order.status.value} at the broker, "
                    f"key {order.client_order_id}",
                )
            )
    for row in ours:
        status = _status(row.get("status"))
        if status not in SETTLED:
            found.append(
                Leftover.of(
                    LeftoverKind.UNSETTLED_ORDER,
                    row.get("symbol"),
                    f"our order {row.get('client_order_id')} ({row.get('intent')}) "
                    f"is recorded {row.get('status')}",
                )
            )
    return tuple(found)


# ---------------------------------------------------------------------------
# What it reads, as shapes
# ---------------------------------------------------------------------------


class CycleRunner(Protocol):
    """The reconciler: one cycle, and the account it reads."""

    @property
    def broker(self) -> BrokerView: ...

    async def run_cycle(self, *, now: dt.datetime) -> CycleReport: ...


class DayRecords(Protocol):
    async def orders_since(self, since: dt.datetime) -> list[dict[str, Any]]: ...

    async def closed_since(self, since: dt.datetime) -> list[dict[str, Any]]: ...

    async def open_position_rows(self) -> list[dict[str, Any]]: ...


class Book(Protocol):
    def open_positions(self) -> list[TrackedPosition]: ...


class PriceReader(Protocol):
    async def last_price(self, symbol: str, *, now: dt.datetime) -> Decimal | None: ...


class Halter(Protocol):
    async def arm(
        self, reason: HaltReason, *, detail: str, armed_by: str, now: dt.datetime
    ) -> object: ...


AuditSink = Callable[[AuditEntry], Awaitable[object]]


@dataclass(frozen=True)
class DayReport:
    verdict: FlatVerdict
    #: ``None`` when today's records could not be read, or a fill has no price.
    statistics: DayStatistics | None
    #: The final cycle, or ``None`` if it failed.
    cycle: CycleReport | None


def _money(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _payload(verdict: FlatVerdict, stats: DayStatistics | None) -> dict[str, Any]:
    """JSON-safe and bounded: Decimals as strings, every list cut."""
    payload: dict[str, Any] = {
        "trade_date": verdict.trade_date.isoformat(),
        "flat": verdict.flat,
        "leftover_count": len(verdict.leftovers),
        "leftovers": [item.describe() for item in verdict.leftovers[:MAX_NAMED]],
        "statistics": None,
    }
    if stats is None:
        return payload
    payload["statistics"] = {
        "trades": len(stats.trades),
        "wins": stats.wins,
        "losses": stats.losses,
        "scratches": stats.scratches,
        "booked": stats.booked,
        "unbooked": len(stats.unbooked),
        "unexplained_closes": list(stats.unexplained_closes[:MAX_NAMED]),
        "still_open": [one_safe_line(s) for s in stats.still_open[:MAX_NAMED]],
        "gross": _money(stats.gross),
        "charges": _money(stats.ledger.charges),
        "net": _money(stats.net),
        "per_trade": [
            {
                "symbol": one_safe_line(t.symbol),
                "net": str(t.net),
                "outcome": t.outcome.value,
                "r": _money(t.r_multiple),
            }
            for t in stats.trades[:MAX_TRADES_RECORDED]
        ],
    }
    return payload


class EndOfDay:
    """One verdict and one report for a trading day, after its books close."""

    def __init__(
        self,
        *,
        reconciler: CycleRunner,
        records: DayRecords,
        book: Book,
        halter: Halter,
        calendar: MarketCalendar,
        prices: PriceReader,
        audit: AuditSink | None = None,
    ) -> None:
        self._reconciler = reconciler
        self._records = records
        self._book = book
        #: REQUIRED: a verdict that found leftovers and could not stop the next
        #: session would be a report, not a control.
        self._halter = halter
        self._calendar = calendar
        self._prices = prices
        self._audit = audit

    def due(self, now: dt.datetime) -> dt.date | None:
        """The trading day ``now`` may judge, or ``None`` - before its books
        close, or on a day with no session."""
        if now.tzinfo is None:
            raise ValueError(f"end-of-day timestamp {now!r} is naive")
        day = now.astimezone(IST).date()
        if not self._calendar.is_trading_day(day) or now < books_close(day):
            return None
        return day

    async def run(self, *, now: dt.datetime) -> DayReport:
        """Final cycle, fresh read, verdict, statistics - then halt if not flat.

        Raises before the books close: a verdict then would halt tomorrow on
        positions about to be squared off. Raises if the halt cannot be armed,
        so the caller tries again: a NOT_FLAT day must not end unlatched.
        """
        day = self.due(now)
        if day is None:
            raise ValueError(
                f"{now.isoformat()} is not after the books closed on a trading day; "
                f"a verdict now would name every position still to be squared off"
            )
        leftovers: list[Leftover] = []

        # -- 1. one final cycle: book fills, close what filled, exit the rest -
        cycle: CycleReport | None = None
        try:
            cycle = await self._reconciler.run_cycle(now=now)
        except Exception as exc:
            log.error("the final reconciliation cycle failed", exc_info=True)
            leftovers.append(
                Leftover.of(
                    LeftoverKind.UNVERIFIED,
                    "-",
                    f"the final reconciliation cycle failed ({type(exc).__name__})",
                )
            )
        else:
            for error in cycle.errors[:MAX_NAMED]:
                leftovers.append(
                    Leftover.of(
                        LeftoverKind.UNVERIFIED, "-", f"the final cycle did not finish: {error}"
                    )
                )

        # -- 2. read again: the ORDERBOOK first (see the design record) -------
        stats: DayStatistics | None = None
        since = dt.datetime.combine(day, dt.time(0), tzinfo=IST).astimezone(dt.UTC)
        try:
            broker = self._reconciler.broker
            orderbook = await broker.fetch_orderbook()
            positions = await broker.fetch_positions()
            ours = await self._records.orders_since(since)
            records = await self._records.open_position_rows()
            closed = await self._records.closed_since(since)
        except Exception as exc:
            log.error("the end-of-day read failed", exc_info=True)
            leftovers.append(
                Leftover.of(
                    LeftoverKind.UNVERIFIED,
                    "-",
                    f"the account could not be read after the final cycle ({type(exc).__name__})",
                )
            )
            # The in-memory book needs no read, and is still worth naming.
            leftovers.extend(
                judge(
                    book=self._book.open_positions(),
                    records=(),
                    positions=(),
                    orderbook=(),
                    ours=(),
                )
            )
        else:
            leftovers.extend(
                judge(
                    book=self._book.open_positions(),
                    records=records,
                    positions=positions,
                    orderbook=orderbook,
                    ours=ours,
                )
            )
            stats = await self._statistics(day, ours, closed, now=now)

        verdict = FlatVerdict(trade_date=day, at=now, leftovers=tuple(leftovers))

        # -- 3. act: a NOT FLAT day stops the next one -------------------------
        if not verdict.flat:
            await self._halter.arm(
                HaltReason.BOOK_NOT_FLAT,
                detail=verdict.describe(),
                armed_by=SERVICE,
                now=now,
            )
            log.critical("END OF DAY - %s", verdict.describe())
        else:
            log.info("END OF DAY - %s; %s", verdict.describe(), _summary(stats))
        await self._record(verdict, stats)
        return DayReport(verdict=verdict, statistics=stats, cycle=cycle)

    async def _statistics(
        self,
        day: dt.date,
        ours: Sequence[Mapping[str, Any]],
        closed: Sequence[Mapping[str, Any]],
        *,
        now: dt.datetime,
    ) -> DayStatistics | None:
        """Prices are asked for only where something is still open."""
        prices: dict[str, Decimal | None] = {}
        for symbol in sorted({str(o["symbol"]) for o in ours if o.get("filled_quantity")}):
            try:
                prices[symbol] = await self._prices.last_price(symbol, now=now)
            except Exception:
                log.warning("no price for %s at the end of day", one_safe_line(symbol))
                prices[symbol] = None
        try:
            return day_statistics(
                trade_date=day, orders=ours, closed_positions=closed, last_price=prices.get
            )
        except Exception:
            # A report that cannot be computed must not take the verdict with it.
            log.exception("the day's statistics could not be computed")
            return None

    async def _record(self, verdict: FlatVerdict, stats: DayStatistics | None) -> None:
        if self._audit is None:
            return
        entry = AuditEntry(
            correlation_id=uuid.uuid5(_NAMESPACE, verdict.trade_date.isoformat()),
            stage=STAGE,
            outcome=FLAT if verdict.flat else NOT_FLAT,
            service=SERVICE,
            payload=_payload(verdict, stats),
            ts=verdict.at,
        )
        try:
            await self._audit(entry)
        except Exception:
            # The audit writer buffers to disk on an outage; anything that
            # still escapes is logged. The halt above is already armed.
            log.exception("the end-of-day audit record could not be written")


def _summary(stats: DayStatistics | None) -> str:
    if stats is None:
        return "statistics unknown"
    return (
        f"{len(stats.trades)} trade(s): {stats.wins} won, {stats.losses} lost, "
        f"{stats.scratches} scratched; net {stats.net}"
    )
