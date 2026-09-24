"""Reconciliation: the broker is the legal record, and this is how we find out
we disagree with it (E15-S09).

``LOW_LEVEL_ARCHITECTURE.md §8.3``: every 30 seconds during market hours and on
every reconnect, fetch the broker's orderbook and positions, diff them against
local state, let the broker win, record every difference, and halt on a
position we have no record of.

## The design record

**The decision.** One cycle is a fixed sequence - *read everything, then act* -
and each step's inputs come from a single broker snapshot. Nothing acts on a
partial view: if any read fails, the cycle takes no action at all and counts a
failure; three in a row halt the day (``BROKER_DISCONNECTED``), because a book
nobody can see is not a book anybody is watching.

**The alternative rejected.** Reacting per event - reconcile an order when it
changes, check a position when a quote arrives. It is lower latency and it has
no single moment at which the system's view is complete, so "is there anything
at the broker we cannot explain?" has no answer. That question is the one this
module exists for.

**The failure it prevents.** Three, in order of cost. A position opened outside
this system - a bug, or someone else - running unwatched with no stop and no
deadline. A protective stop verified live at attach time and rejected a second
later, leaving a position naked until square-off; nothing but a loop can see
that. A restart that leaves every position's protection unknown indefinitely.

**What would make this decision wrong.** A broker push feed for order updates.
The per-order half of this module would become an event handler; the
unknown-position check would not, because "nothing unexplained exists" is a
statement about a snapshot and cannot be assembled from events.

## Why unknown positions are found from the day's BUYS and SELLS, not net

The obvious check - does the broker's net quantity equal what our orders net to?
- is not race-free, and the way it fails is a false halt. The two broker reads
are separate calls. If a fill lands between them, one read sees it and the other
does not. Read positions first and an ENTRY fill in the gap looks benign (our
orders explain more than the broker holds) - but an EXIT fill in the same gap
looks like the broker holding shares nobody ordered, and the day halts on its
own square-off.

Net quantity is not monotone: entries add, exits subtract. The day's cumulative
buy quantity and sell quantity each only ever grow. So positions are read
FIRST and the orderbook SECOND, and each side is compared separately: every fill
the positions read reflects already happened before the orderbook read, so our
explained buys and sells are each at least what the broker shows for our
orders. Anything beyond that, on either side, did not come from us. Race-free in
both directions, without a second read.

## What counts as unknown, and what does not

Scope is product ``MIS``. The account is also a human's, and delivery holdings
(``CNC``), carry-forward (``NRML``) and margin-funded (``MTF``) positions are
outside anything this system trades. Halting on them would halt every day.

Within MIS, **any fill our orders do not explain arms the kill switch** - an
open position we never ordered, extra shares on a symbol we also hold, and a
foreign fill that nets against ours. The last is the dangerous one: our book
says long 40, someone sells 40 by hand, the broker is flat, and our stop, when
it triggers, sells 40 shares that are no longer there. The owner trading MIS by
hand on the same account while this runs will therefore halt it. That is §8.3's
policy ("either a bug or an unauthorized order, and both warrant stopping"),
stated here so it is a choice rather than a surprise.

The loop does **not** trade an unknown position. It is not ours, it may be the
owner's, and we know nothing of its intent. It halts, and it alerts separately
from the halt: "an unknown position exists" needs a human to look at a position
now; "the system halted" needs a human to decide about the rest of the day.
Those are different responses on different clocks, which is this story's build
concern.

## A holding we explain but do not book is unprotected

Our own fill, explained by our orders, with no position in the book, has no
stop by construction - nothing attached one. It is exited at market through the
same path a failed stop takes. Until E15-S12 books fills into the position
manager, that is every fill: the honest behaviour of a half-built pipeline,
not a defect. E15-S12's booking step must run before this one in the cycle.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, Protocol, assert_never

from algotrader.broker.adapter import BrokerPosition
from algotrader.common.audit import AuditEntry
from algotrader.common.calendar import IST
from algotrader.common.db.models import DecisionLog
from algotrader.common.enums import Direction, OrderIntent, OrderStatus, Side
from algotrader.common.models.trading import Order
from algotrader.common.text import one_safe_line
from algotrader.execution.halt import HaltReason
from algotrader.execution.order_state import is_legal
from algotrader.execution.positions import (
    ExitingPosition,
    PositionManager,
    ProtectedPosition,
    UnverifiedPosition,
)
from algotrader.execution.protective_stop import ProtectiveStop

log = logging.getLogger(__name__)

__all__ = [
    "CycleReport",
    "Drift",
    "DriftEvent",
    "Reconciler",
    "ReconciliationLoop",
    "ReconciliationReadError",
    "UnknownPosition",
    "broker_target",
]

#: The audit stage every drift event is written under (§8.3 names it).
STAGE: Final = "RECONCILIATION_DRIFT"
SERVICE: Final = "reconciler"
#: This system places MIS orders on NSE and nothing else (``gateway`` and the
#: Kite adapter both hardcode it). A position anywhere else is not ours.
TRADED_PRODUCT: Final = "MIS"
TRADED_EXCHANGE: Final = "NSE"


class Drift(StrEnum):
    """What kind of difference was found. Each value is an audit ``outcome``."""

    TRANSITION = "TRANSITION"
    FILL_UPDATE = "FILL_UPDATE"
    ADOPTED = "ADOPTED"
    REFUSED = "REFUSED"
    SUBMIT_FAIL = "SUBMIT_FAIL"
    DUPLICATE = "DUPLICATE"
    MISSING = "MISSING"
    UNKNOWN_POS = "UNKNOWN_POS"
    QTY_SHORT = "QTY_SHORT"
    UNBOOKED = "UNBOOKED"
    UNPROTECTED = "UNPROTECTED"
    PROTECTED = "PROTECTED"


def _assert_fits_audit_columns() -> None:
    """Refuse to load if anything this module writes would not fit the table.

    The widths are read off the real model rather than restated. The lesson is
    ``decision_log.stage``: three risk-check names in the design document were
    longer than ``String(28)`` and would have failed the audit insert at the
    moment a rejection happened - precisely when the record is wanted. Here the
    obvious outcome label, ``UNKNOWN_POSITION``, is 16 characters against a
    ``String(12)`` column, and would have failed at the moment an unknown
    position was found. A load-time refusal costs one failed start; the
    alternative costs the record of the incident.
    """
    columns = DecisionLog.__table__.c

    def width_of(name: str) -> int | None:
        # ``length`` lives on String, not on TypeEngine. A column that stopped
        # being a sized string returns None, which refuses below: a width we
        # cannot read is not one we can promise to fit.
        return getattr(columns[name].type, "length", None)

    limits = {
        "stage": (width_of("stage"), [STAGE]),
        "outcome": (width_of("outcome"), [d.value for d in Drift]),
        "service": (width_of("service"), [SERVICE]),
    }
    for column, (width, values) in limits.items():
        too_long = [v for v in values if width is None or len(v) > width]
        if too_long:
            raise RuntimeError(
                f"decision_log.{column} is String({width}); {too_long} would not fit. "
                f"The audit insert would fail at the moment the drift happened."
            )


_assert_fits_audit_columns()


class ReconciliationReadError(RuntimeError):
    """A broker or database read failed, so this cycle took no action at all."""


# ---------------------------------------------------------------------------
# What we consume, as shapes
# ---------------------------------------------------------------------------


class BrokerView(Protocol):
    """The three things reconciliation needs from the broker, and no more."""

    async def fetch_positions(self) -> list[BrokerPosition]: ...

    async def fetch_orderbook(self) -> list[Order]: ...

    def order_key(self, client_order_id: str) -> str: ...


class OrderLedger(Protocol):
    """Our order records, as the capability rather than the repository."""

    async def orders_placed_since(self, since: dt.datetime) -> list[dict[str, Any]]: ...

    async def apply_broker_state(
        self,
        client_order_id: str,
        *,
        status: str,
        filled_quantity: int,
        average_price: Decimal | None,
        broker_order_id: str | None,
    ) -> None: ...


class Halter(Protocol):
    async def arm(
        self, reason: HaltReason, *, detail: str, armed_by: str, now: dt.datetime
    ) -> object: ...


AuditSink = Callable[[AuditEntry], Awaitable[object]]


# ---------------------------------------------------------------------------
# What a cycle finds
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DriftEvent:
    """One difference between us and the broker, as it reaches the audit log."""

    kind: Drift
    correlation_id: uuid.UUID
    symbol: str
    detail: str

    def to_audit(self, *, at: dt.datetime) -> AuditEntry:
        return AuditEntry(
            correlation_id=self.correlation_id,
            stage=STAGE,
            outcome=self.kind.value,
            service=SERVICE,
            payload={"symbol": self.symbol, "detail": one_safe_line(self.detail)},
            ts=at,
        )


@dataclass(frozen=True)
class UnknownPosition:
    """MIS activity at the broker that none of our orders explains."""

    symbol: str
    exchange: str
    broker_quantity: int
    foreign_buys: int
    foreign_sells: int

    @property
    def is_open(self) -> bool:
        return self.broker_quantity != 0

    def describe(self) -> str:
        shape = (
            f"net {self.broker_quantity:+d} OPEN at the broker"
            if self.is_open
            else "a round trip now flat at the broker"
        )
        return (
            f"{self.exchange}:{self.symbol} {shape}; "
            f"{self.foreign_buys} bought and {self.foreign_sells} sold by orders "
            f"this system did not place"
        )


@dataclass
class CycleReport:
    """What one cycle saw and did. Every field is an assertion somewhere."""

    cycle_id: uuid.UUID
    at: dt.datetime
    drifts: list[DriftEvent] = field(default_factory=list)
    unknown: list[UnknownPosition] = field(default_factory=list)
    halted: bool = False
    exited: list[str] = field(default_factory=list)
    promoted: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.drifts or self.unknown or self.exited or self.errors)


@dataclass(frozen=True)
class UnbookedHolding:
    """Our own fill, with no position in the book. Satisfies ``Closable``."""

    correlation_id: uuid.UUID
    symbol: str
    direction: Direction
    quantity: int


# ---------------------------------------------------------------------------
# Pure decisions
# ---------------------------------------------------------------------------


def broker_target(order: Order) -> OrderStatus:
    """What the broker's report means in OUR vocabulary.

    **Consult the fill, not only the status.** Kite reports ``OPEN`` for a
    working order and for a partially filled one alike; its own mapping says
    so. Taken at face value, a poll of a PARTIAL order would try to move it
    back to OPEN, which the state machine refuses because going backwards
    discards the knowledge that a fill occurred. So an OPEN report with a
    fill in it IS a partial fill, and is recorded as one. (E15-S03's
    constraint 1, discharged here rather than worked around.)
    """
    if order.status is OrderStatus.OPEN and 0 < order.filled_quantity < order.quantity:
        return OrderStatus.PARTIAL
    return order.status


@dataclass(frozen=True)
class OrderWrite:
    status: OrderStatus
    filled_quantity: int
    average_price: Decimal | None
    broker_order_id: str | None


def decide_order(
    row: dict[str, Any],
    matches: Sequence[Order],
    *,
    now: dt.datetime,
    submitting_grace: dt.timedelta,
) -> tuple[OrderWrite | None, DriftEvent | None]:
    """What to do about one of our non-terminal orders. Pure; no I/O.

    Returns what to write (if anything) and what to record (if anything). The
    rule that matters most is at the bottom: **a refused transition is recorded
    once**. A row already in ``RECONCILE_REQUIRED`` is left alone, because
    retrying a refused transition every 30 seconds turns a fail-closed guard
    into a continuous alarm, and an alarm that is always on is not read.
    """
    cid = str(row["client_order_id"])
    correlation = row["correlation_id"]
    symbol = str(row["symbol"])
    recorded_fill = int(row.get("filled_quantity") or 0)

    def drift(kind: Drift, detail: str) -> DriftEvent:
        return DriftEvent(kind=kind, correlation_id=correlation, symbol=symbol, detail=detail)

    try:
        current = OrderStatus(str(row["status"]))
    except ValueError:
        # No legal move can be computed from a state we cannot read, so nothing
        # is written. Recording it is the whole of the response.
        return None, drift(
            Drift.REFUSED, f"{cid} carries unreadable status {row['status']!r}; not touched"
        )

    reconcile = OrderStatus.RECONCILE_REQUIRED

    if len(matches) > 1:
        # Bookkeeping only. The DUPLICATE drift and its halt are raised by the
        # cycle, which sees every key - including ones whose row is already
        # FILLED and never reaches this function. Reporting it here as well was
        # the first version, and it missed exactly the terminal-row case.
        if current is reconcile:
            return None, None
        return OrderWrite(reconcile, recorded_fill, row.get("average_price"), None), None

    if not matches:
        if current is OrderStatus.SUBMITTING:
            placed_at = row.get("placed_at")
            if placed_at is None or now - placed_at < submitting_grace:
                return None, None
            # The request never reached the broker, or reached it and was
            # never recorded there. SUBMIT_FAILED is modelled for exactly this
            # and nothing wrote it before this story (SIT-003's constraint).
            return (
                OrderWrite(OrderStatus.SUBMIT_FAILED, recorded_fill, None, None),
                drift(
                    Drift.SUBMIT_FAIL,
                    f"{cid} has been SUBMITTING since {placed_at.isoformat()} and the "
                    f"broker does not list it",
                ),
            )
        if current in (OrderStatus.SUBMIT_FAILED, reconcile):
            return None, None
        return (
            OrderWrite(reconcile, recorded_fill, row.get("average_price"), None),
            drift(Drift.MISSING, f"{cid} is {current.value} here and absent at the broker"),
        )

    broker = matches[0]
    target = broker_target(broker)
    write = OrderWrite(target, broker.filled_quantity, broker.average_price, broker.broker_order_id)

    if broker.filled_quantity < recorded_fill:
        # Fills never shrink. The broker reporting less than we recorded is a
        # contradiction, and the higher number is the one we must not lose - the
        # same reason PARTIAL -> OPEN is refused.
        if current is reconcile:
            return None, None
        return (
            OrderWrite(reconcile, recorded_fill, row.get("average_price"), None),
            drift(
                Drift.REFUSED,
                f"{cid}: broker reports {broker.filled_quantity} filled, we recorded "
                f"{recorded_fill}; keeping the higher",
            ),
        )

    if target is reconcile:
        if current is reconcile:
            return None, None
        return (
            OrderWrite(reconcile, broker.filled_quantity, broker.average_price, None),
            drift(Drift.REFUSED, f"{cid}: the broker's status is not one this system models"),
        )

    if current is OrderStatus.SUBMIT_FAILED:
        # It landed after all. SUBMIT_FAILED can only move to RECONCILE_REQUIRED;
        # the next cycle carries it the rest of the way.
        return (
            OrderWrite(reconcile, broker.filled_quantity, broker.average_price, None),
            drift(Drift.ADOPTED, f"{cid} was recorded SUBMIT_FAILED and the broker lists it"),
        )

    unchanged = (
        target is current
        and broker.filled_quantity == recorded_fill
        and broker.average_price == row.get("average_price")
        and (broker.broker_order_id == row.get("broker_order_id") or not broker.broker_order_id)
    )
    if unchanged:
        return None, None

    if target is current:
        return write, drift(
            Drift.FILL_UPDATE,
            f"{cid} {current.value}: filled {recorded_fill} -> {broker.filled_quantity}",
        )

    if is_legal(current, target):
        kind = Drift.ADOPTED if current is OrderStatus.SUBMITTING else Drift.TRANSITION
        return write, drift(kind, f"{cid} {current.value} -> {target.value}")

    if current is reconcile:
        return None, None
    return (
        OrderWrite(reconcile, broker.filled_quantity, broker.average_price, None),
        drift(
            Drift.REFUSED,
            f"{cid}: {current.value} -> {target.value} is not a legal move; recorded "
            f"RECONCILE_REQUIRED instead of forcing it",
        ),
    )


def _signed(side: object) -> int:
    return 1 if str(getattr(side, "value", side)) == Side.BUY.value else -1


def find_unknown(
    positions: Sequence[BrokerPosition],
    our_fills: dict[str, tuple[int, int]],
) -> list[UnknownPosition]:
    """MIS activity at the broker that our orders do not explain. Pure.

    ``our_fills`` maps symbol -> (bought, sold) by OUR orders on NSE, as the
    orderbook read reports them. Because that read happens AFTER the positions
    read and both sides only grow, our numbers are each at least the broker's
    share of them - so any excess on either side came from somewhere else. See
    the module docstring for why net quantity cannot be used.
    """
    unknown: list[UnknownPosition] = []
    for p in positions:
        if p.product != TRADED_PRODUCT:
            continue
        ours = our_fills.get(p.symbol, (0, 0)) if p.exchange == TRADED_EXCHANGE else (0, 0)
        foreign_buys = max(0, p.day_buy_quantity - ours[0])
        foreign_sells = max(0, p.day_sell_quantity - ours[1])
        if foreign_buys or foreign_sells:
            unknown.append(
                UnknownPosition(
                    symbol=p.symbol,
                    exchange=p.exchange,
                    broker_quantity=p.quantity,
                    foreign_buys=foreign_buys,
                    foreign_sells=foreign_sells,
                )
            )
    return unknown


# ---------------------------------------------------------------------------
# The cycle
# ---------------------------------------------------------------------------


class Reconciler:
    """One reconciliation cycle: read everything, then act, in a fixed order."""

    def __init__(
        self,
        *,
        broker: BrokerView,
        ledger: OrderLedger,
        manager: PositionManager,
        protector: ProtectiveStop,
        halter: Halter,
        audit: AuditSink | None = None,
        submitting_grace_seconds: float = 60.0,
        metrics: object | None = None,
    ) -> None:
        if submitting_grace_seconds <= 0:
            raise ValueError(
                "submitting_grace_seconds must be positive; zero would mark an order "
                "SUBMIT_FAILED while its request is still in flight"
            )
        self._broker = broker
        self._ledger = ledger
        self._manager = manager
        self._protector = protector
        #: REQUIRED. The whole point of the unknown-position check is the halt;
        #: a reconciler that could be built without one would find the position
        #: and do nothing about it.
        self._halter = halter
        self._audit = audit
        self._grace = dt.timedelta(seconds=submitting_grace_seconds)
        self._metrics = metrics
        #: Keys already reported as duplicated. The halt is a latch, so arming it
        #: again is harmless, but a drift event every 30 seconds for the rest of
        #: the day is an alarm nobody reads. A restart reports each once more.
        self._reported_duplicates: set[str] = set()
        #: What each unknown position looked like when it was last ALERTED on.
        #: Detection runs every cycle; the alert runs when a stranger is new or
        #: has changed size (SIT-005). The first version alerted 30 times for
        #: one position, and a counter documented as counting positions read 30.
        self._reported_unknown: dict[tuple[str, str], tuple[int, int]] = {}

    async def run_cycle(self, *, now: dt.datetime) -> CycleReport:
        """Read, then act. Raises :class:`ReconciliationReadError` having done
        nothing if any read fails."""
        if now.tzinfo is None:
            raise ValueError(f"cycle timestamp {now!r} is naive")
        report = CycleReport(cycle_id=uuid.uuid4(), at=now)
        trade_date = now.astimezone(IST).date()

        # -- 1. read: positions FIRST, then the orderbook, then our records ----
        try:
            positions = await self._broker.fetch_positions()
            orderbook = await self._broker.fetch_orderbook()
            since = dt.datetime.combine(trade_date, dt.time(0), tzinfo=IST)
            ours = await self._ledger.orders_placed_since(since)
        except Exception as exc:
            self._count("reconciliation_read_failures_total")
            raise ReconciliationReadError(
                f"reconciliation read failed ({type(exc).__name__}: "
                f"{one_safe_line(str(exc))}); no action taken on a partial view"
            ) from exc
        self._count("reconciliation_cycles_total")

        by_key: dict[str, list[Order]] = {}
        for order in orderbook:
            by_key.setdefault(order.client_order_id, []).append(order)
        matched: dict[str, list[Order]] = {
            str(row["client_order_id"]): by_key.get(
                self._broker.order_key(str(row["client_order_id"])), []
            )
            for row in ours
        }

        # -- 2. unknown positions: first, because it is the one that halts ----
        await self._check_unknown(positions, ours, matched, report, now=now)

        # -- 2b. one key, several broker orders: attribution is in doubt ------
        doubted = await self._check_duplicates(ours, matched, report, now=now)

        # -- 3. our orders: the broker wins, through the state machine --------
        for row in ours:
            try:
                await self._reconcile_order(row, matched[str(row["client_order_id"])], report, now)
            except Exception as exc:
                report.errors.append(f"order {row.get('client_order_id')}: {exc}")
                log.error("reconciling order %s failed", row.get("client_order_id"), exc_info=True)

        # -- 4. every booked position has a live stop, or leaves -------------
        await self._check_protection(report, trade_date=trade_date, now=now)

        # -- 5. our fills the book does not hold: unprotected by definition ---
        # E15-S12's booking step belongs BEFORE this one.
        await self._exit_unbooked(
            ours, matched, report, doubted=doubted, trade_date=trade_date, now=now
        )

        for event in report.drifts:
            await self._record(event, at=now)
        return report

    # -- step 2 ---------------------------------------------------------------

    async def _check_unknown(
        self,
        positions: list[BrokerPosition],
        ours: list[dict[str, Any]],
        matched: dict[str, list[Order]],
        report: CycleReport,
        *,
        now: dt.datetime,
    ) -> None:
        fills: dict[str, tuple[int, int]] = {}
        for row in ours:
            # Every match counts, duplicates included: they carry our tag, so
            # their fills are ours. A duplicate is flagged by step 3; calling its
            # shares unknown here would misname a failed idempotency as a stranger.
            for order in matched[str(row["client_order_id"])]:
                bought, sold = fills.get(order.symbol, (0, 0))
                if _signed(order.side) > 0:
                    fills[order.symbol] = (bought + order.filled_quantity, sold)
                else:
                    fills[order.symbol] = (bought, sold + order.filled_quantity)

        for position in positions:
            if position.product != TRADED_PRODUCT or position.exchange != TRADED_EXCHANGE:
                continue
            bought, sold = fills.get(position.symbol, (0, 0))
            if bought > position.day_buy_quantity or sold > position.day_sell_quantity:
                # The benign direction: our orders already show a fill the
                # positions read did not. Recorded, never halted on - this is
                # the race the read order exists to make harmless.
                report.drifts.append(
                    DriftEvent(
                        kind=Drift.QTY_SHORT,
                        correlation_id=report.cycle_id,
                        symbol=position.symbol,
                        detail=(
                            f"our orders show {bought} bought / {sold} sold, the broker's "
                            f"positions {position.day_buy_quantity} / "
                            f"{position.day_sell_quantity}"
                        ),
                    )
                )

        report.unknown = find_unknown(positions, fills)
        if not report.unknown:
            return

        fresh = [
            u
            for u in report.unknown
            if self._reported_unknown.get((u.exchange, u.symbol))
            != (u.foreign_buys, u.foreign_sells)
        ]
        for u in fresh:
            self._reported_unknown[(u.exchange, u.symbol)] = (u.foreign_buys, u.foreign_sells)
            self._count("unknown_positions_total")
            # The alert that is NOT the halt. Its own line, its own counter:
            # someone must look at this position now, which is a different job
            # from deciding what to do about the rest of the day.
            log.critical("UNKNOWN POSITION: %s. It is not ours and it is not traded.", u.describe())
            report.drifts.append(
                DriftEvent(
                    kind=Drift.UNKNOWN_POS,
                    correlation_id=report.cycle_id,
                    symbol=u.symbol,
                    detail=u.describe(),
                )
            )
        # Re-armed EVERY cycle while a stranger remains, alerted or not: an
        # operator who clears the latch while an unknown position still exists
        # is halted again by the next cycle. ``arm`` is idempotent and keeps the
        # first reason, so this costs one read.
        detail = "; ".join(u.describe() for u in report.unknown)
        await self._halter.arm(
            HaltReason.UNKNOWN_POSITION,
            detail=one_safe_line(detail),
            armed_by="reconciler",
            now=now,
        )
        report.halted = True
        if fresh:
            log.critical(
                "SESSION HALTED by reconciliation: %d unknown position(s)", len(report.unknown)
            )

    # -- step 2b --------------------------------------------------------------

    async def _check_duplicates(
        self,
        ours: list[dict[str, Any]],
        matched: dict[str, list[Order]],
        report: CycleReport,
        *,
        now: dt.datetime,
    ) -> set[uuid.UUID]:
        """Halt on any of our keys found on more than one broker order.

        Found by the STRIDE pass against this story's own attribution rule. A
        broker order carrying our tag is counted as ours, and a tag is readable
        by anyone with access to the account: copy one onto a foreign order and
        its fill is "explained". The first version then (a) raised no unknown
        position, (b) never examined the duplicate at all, because our row was
        already FILLED and terminal rows skip order reconciliation, and (c) sent
        an exit for the whole holding - trading 100 shares that were not ours.

        A duplicated key is either our idempotency failing or a copied tag. The
        orderbook cannot tell them apart, and both warrant stopping. Returns the
        correlations whose attribution is now in doubt, which step 5 must not
        trade.
        """
        doubted: set[uuid.UUID] = set()
        fresh: list[str] = []
        for row in ours:
            key = str(row["client_order_id"])
            found = matched[key]
            if len(found) < 2:
                continue
            doubted.add(row["correlation_id"])
            if key in self._reported_duplicates:
                continue
            self._reported_duplicates.add(key)
            ids = ", ".join(str(o.broker_order_id) for o in found)
            detail = (
                f"{key} is on {len(found)} broker orders ({ids}); either our "
                f"idempotency failed or someone copied our tag"
            )
            fresh.append(f"{row['symbol']}: {detail}")
            log.critical("DUPLICATE ORDER: %s %s", row["symbol"], detail)
            report.drifts.append(
                DriftEvent(
                    kind=Drift.DUPLICATE,
                    correlation_id=row["correlation_id"],
                    symbol=str(row["symbol"]),
                    detail=detail,
                )
            )
        if fresh:
            await self._halter.arm(
                HaltReason.DUPLICATE_ORDER,
                detail=one_safe_line("; ".join(fresh)),
                armed_by="reconciler",
                now=now,
            )
            report.halted = True
        return doubted

    # -- step 3 ---------------------------------------------------------------

    async def _reconcile_order(
        self,
        row: dict[str, Any],
        matches: list[Order],
        report: CycleReport,
        now: dt.datetime,
    ) -> None:
        try:
            status = OrderStatus(str(row["status"]))
        except ValueError:
            status = None
        if status is not None and status.is_terminal:
            return
        write, event = decide_order(row, matches, now=now, submitting_grace=self._grace)
        if write is not None:
            await self._ledger.apply_broker_state(
                str(row["client_order_id"]),
                status=write.status.value,
                filled_quantity=write.filled_quantity,
                average_price=write.average_price,
                broker_order_id=write.broker_order_id,
            )
        if event is not None:
            report.drifts.append(event)

    # -- step 4 ---------------------------------------------------------------

    async def _check_protection(
        self, report: CycleReport, *, trade_date: dt.date, now: dt.datetime
    ) -> None:
        """Every booked position, every cycle (E15-S04's constraint 4).

        This is the only thing that catches a stop accepted and verified live
        at attach time and rejected afterwards. Protection is asked of the
        broker - never read from the Redis mirror or a row (E15-S05's
        constraint 9).
        """
        for tracked in list(self._manager.open_positions()):
            position = tracked.position
            try:
                if isinstance(tracked, UnverifiedPosition):
                    proof = await self._protector.adopt(position, trade_date=trade_date, now=now)
                    if proof is not None:
                        await self._manager.promote(proof)
                        report.promoted.append(position.symbol)
                        report.drifts.append(
                            DriftEvent(
                                kind=Drift.PROTECTED,
                                correlation_id=position.correlation_id,
                                symbol=position.symbol,
                                detail="restored position: broker confirms a live stop",
                            )
                        )
                        continue
                    because = "restored position has no live stop at the broker"
                elif isinstance(tracked, ProtectedPosition):
                    if await self._protector.is_protected(position, trade_date=trade_date):
                        continue
                    because = "its stop is no longer live at the broker"
                elif isinstance(tracked, ExitingPosition):
                    if await self._protector.exit_is_live(position, trade_date=trade_date):
                        continue
                    because = "its emergency exit is no longer live at the broker"
                else:
                    # A fourth kind of tracked position would fail type-checking
                    # here rather than be skipped silently by a loop whose job
                    # is to look at every one of them.
                    assert_never(tracked)
                await self._exit(position, because, report, trade_date=trade_date, now=now)
                # The exit is live (exit_now verifies it, and raises having
                # halted if it is not). The book must now SAY so (SIT-006).
                await self._manager.mark_exiting(position.symbol, because=because)
            except Exception as exc:
                report.errors.append(f"protection {position.symbol}: {exc}")
                log.error("protection check for %s failed", position.symbol, exc_info=True)

    # -- step 5 ---------------------------------------------------------------

    async def _exit_unbooked(
        self,
        ours: list[dict[str, Any]],
        matched: dict[str, list[Order]],
        report: CycleReport,
        *,
        doubted: set[uuid.UUID],
        trade_date: dt.date,
        now: dt.datetime,
    ) -> None:
        booked = {t.position.correlation_id for t in self._manager.open_positions()}
        groups: dict[uuid.UUID, list[tuple[dict[str, Any], Order]]] = {}
        for row in ours:
            for order in matched[str(row["client_order_id"])]:
                groups.setdefault(row["correlation_id"], []).append((row, order))

        for correlation, members in groups.items():
            if correlation in booked:
                continue
            if correlation in doubted:
                # Some of these shares may not be ours. The loop does not trade
                # a holding it cannot attribute - the session is already halted
                # and a human decides.
                continue
            entries = [(r, o) for r, o in members if str(r["intent"]) == OrderIntent.ENTRY.value]
            if not entries:
                continue
            entry_row, entry = entries[0]
            direction = Direction.LONG if _signed(entry.side) > 0 else Direction.SHORT
            opened = sum(o.filled_quantity for r, o in entries)
            closed = sum(
                o.filled_quantity for r, o in members if str(r["intent"]) != OrderIntent.ENTRY.value
            )
            open_qty = opened - closed
            if open_qty <= 0:
                continue
            holding = UnbookedHolding(
                correlation_id=correlation,
                symbol=entry.symbol,
                direction=direction,
                quantity=open_qty,
            )
            try:
                if await self._protector.exit_is_live(holding, trade_date=trade_date):
                    continue
                report.drifts.append(
                    DriftEvent(
                        kind=Drift.UNBOOKED,
                        correlation_id=correlation,
                        symbol=entry.symbol,
                        detail=(
                            f"{open_qty} filled by our entry {entry_row['client_order_id']} "
                            f"and not in the book, so no stop protects it"
                        ),
                    )
                )
                await self._exit(
                    holding,
                    "our fill is not in the book, so nothing attached a stop",
                    report,
                    trade_date=trade_date,
                    now=now,
                    record=False,
                )
            except Exception as exc:
                report.errors.append(f"unbooked {entry.symbol}: {exc}")
                log.error("exiting unbooked holding %s failed", entry.symbol, exc_info=True)

    async def _exit(
        self,
        target: Any,
        because: str,
        report: CycleReport,
        *,
        trade_date: dt.date,
        now: dt.datetime,
        record: bool = True,
    ) -> None:
        """The SAME path a failed stop takes at attach time (constraint 5).

        ``exit_now`` exits at market and verifies the exit; if the exit cannot
        be established it arms the halt BEFORE raising. So a failure here has
        already stopped the session by the time it propagates - it is recorded
        and the cycle carries on with the next position.
        """
        if record:
            report.drifts.append(
                DriftEvent(
                    kind=Drift.UNPROTECTED,
                    correlation_id=target.correlation_id,
                    symbol=target.symbol,
                    detail=because,
                )
            )
        report.exited.append(target.symbol)
        await self._protector.exit_now(target, trade_date=trade_date, now=now, because=because)

    # -- plumbing -------------------------------------------------------------

    async def _record(self, event: DriftEvent, *, at: dt.datetime) -> None:
        """Write one drift event. A failed audit write never stops the cycle.

        ``AuditWriter.write`` already promises not to raise; an injected sink
        might not. Losing a drift record is bad, and losing the exits and halts
        that follow it would be worse.
        """
        self._count_drift(event.kind)
        if self._audit is None:
            return
        try:
            await self._audit(event.to_audit(at=at))
        except Exception:
            log.error(
                "could not record %s drift for %s", event.kind.value, event.symbol, exc_info=True
            )

    def _count(self, name: str) -> None:
        counter = getattr(self._metrics, name, None)
        if counter is None:
            return
        try:
            counter.inc()
        except Exception:  # pragma: no cover - defensive
            log.warning("could not increment %s", name)

    def _count_drift(self, kind: Drift) -> None:
        counter = getattr(self._metrics, "reconciliation_drift_total", None)
        if counter is None:
            return
        try:
            counter.labels(kind=kind.value).inc()
        except Exception:  # pragma: no cover - defensive
            log.warning("could not count %s drift", kind.value)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


class ReconciliationLoop:
    """Every 30 seconds during market hours, and once on every reconnect.

    A cycle that fails does not stop the loop, and it does not pass either:
    failures are counted, and ``max_consecutive_failures`` in a row arm
    ``BROKER_DISCONNECTED``. Three is 90 seconds of an unwatched book at the
    default cadence - long enough that one dropped request does not stop the
    day, short enough that a real outage does.
    """

    def __init__(
        self,
        reconciler: Reconciler,
        *,
        calendar: Any,
        halter: Halter,
        clock: Callable[[], dt.datetime],
        interval_seconds: float = 30.0,
        max_consecutive_failures: int = 3,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if max_consecutive_failures < 1:
            raise ValueError(
                "max_consecutive_failures below 1 would halt on no failures at all, "
                "or never; neither is a policy"
            )
        self._reconciler = reconciler
        self._calendar = calendar
        self._halter = halter
        self._clock = clock
        self._interval = interval_seconds
        self._max_failures = max_consecutive_failures
        self._sleep = sleep
        self.consecutive_failures = 0

    async def tick(self) -> CycleReport | None:
        """One scheduled iteration. Outside market hours it does nothing."""
        now = self._clock()
        if not self._calendar.is_market_open(now):
            return None
        return await self._run(now)

    async def on_reconnect(self) -> CycleReport | None:
        """Run immediately after a reconnect, whatever the clock says.

        A reconnect is the moment the view was last known to be stale, so it is
        not gated on market hours: an extra look outside them costs one read.
        """
        return await self._run(self._clock())

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.tick()
            await self._sleep(self._interval)

    async def _run(self, now: dt.datetime) -> CycleReport | None:
        try:
            report = await self._reconciler.run_cycle(now=now)
        except Exception as exc:
            self.consecutive_failures += 1
            log.error(
                "reconciliation cycle failed (%d in a row): %s",
                self.consecutive_failures,
                one_safe_line(str(exc)),
                exc_info=not isinstance(exc, ReconciliationReadError),
            )
            if self.consecutive_failures >= self._max_failures:
                # The error's TYPE, never its text. The text is a broker's
                # exception message, and the halt record is written to Redis -
                # a sink the logging layer's redaction never sees. The full
                # message is in the ERROR line above, which is redacted. Keeping
                # it out of this sink is one rung above scrubbing it on the way in.
                cause = exc.__cause__ if exc.__cause__ is not None else exc
                await self._halter.arm(
                    HaltReason.BROKER_DISCONNECTED,
                    detail=(
                        f"{self.consecutive_failures} consecutive reconciliation cycles "
                        f"failed ({type(cause).__name__}); the book cannot be seen"
                    ),
                    armed_by="reconciler",
                    now=now,
                )
            return None
        self.consecutive_failures = 0
        return report
