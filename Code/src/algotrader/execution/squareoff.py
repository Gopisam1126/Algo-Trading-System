"""The square-off timer: every position closed on our schedule (E15-S06).

Invariant 5 is *every position has a stop and a time exit*. E15-S04 gave every
position its stop; this is the time exit. Without it a position stays open
until the broker's own forced square-off - at whatever price the market gives
in the minutes when every intraday account is being closed at once, with the
broker's square-off charge on top.

## The design record

**The decision.** One sweep of the BOOK, run every second, acts on each
position's stored ``squareoff_deadline``: it cancels the protective stop,
waits until the broker shows the stop is no longer working, then sends a
MARKET exit with market protection and marks the position EXITING with reason
``TIME``. Reconciliation confirms the fill and closes the record, as it does
for every other exit.

**The alternatives rejected.**

* *The Redis sorted set ``timer:squareoff`` that LOW_LEVEL_ARCHITECTURE.md
  §5.8 and the story's task 2 describe.* It is a second copy of a deadline the
  position already carries (``positions.squareoff_deadline`` is NOT NULL and
  the position cannot be built without it - E15-S05's constraint 1), written
  when a position opens and removed when it closes, by different code paths. A
  missed removal leaves a phantom; a missed ADD leaves a position with no time
  exit at all, which is exactly the failure this story exists to prevent. The
  book holds at most twenty positions, so scanning it each second costs
  nothing. The key stays defined in ``common/redis/keys.py`` and unused.
* *Sending the exit while the stop is still working.* Two working exits for
  one position: if both fill, our sells outrun our buys and the reconciler's
  OVER_EXITED guard halts the day with the account short (E15-S12's
  constraint 5). So the exit waits until the stop reads terminal. Kite reports
  CANCEL PENDING as OPEN, which counts as live, so the reconciler cannot add an
  emergency exit in the gap either.
* *A new order intent for the time exit (E15-S09's constraint 4).* The timer
  shares the emergency exit's SQUAREOFF key. That key is unused for every
  position the timer acts on: a position becomes EXITING the moment it is
  used, and the timer never exits an EXITING position. A new intent would need
  a migration of ``order_intent_enum`` and give each position a second exit
  key - a way to send a second exit - for no case this design reaches.

**The failure it prevents.** A position the broker force-closes; a position
exited twice; and - found while designing this - a position exited that the
broker no longer holds, which SELLS shares we do not own. Every exit here is
sized ``min(book, broker)``; a holding the broker does not have halts and
sends nothing.

**What would make this wrong.** A book that grows past what a one-second scan
can handle, or a broker whose cancel is not visible to the next read. Kite's
untriggered SL-M cancel is; a broker where it is not would need the exit to
wait on a cancel CONFIRMATION, not on a read.

## Liveness (the story's build concern 1)

A single loop is a single point of failure at the most time-critical moment
of the day. The execution service runs this sweep from its own one-second
loop AND from the reconciliation path after every cycle, so a dead timer loop
leaves positions exited thirty seconds late, not left for the broker; and the
reconciliation path reports the dedicated loop going quiet (see
``ExecutionService``). A loop that CRASHES stops the service's TaskGroup, and
the process restarts, restores the book and sweeps again.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, Protocol, TypeGuard

from algotrader.broker.adapter import BrokerPosition
from algotrader.common.audit import AuditEntry
from algotrader.common.calendar import IST
from algotrader.common.db.models import DecisionLog
from algotrader.common.enums import ExitReason
from algotrader.common.models.trading import Order
from algotrader.common.text import one_safe_line
from algotrader.execution.halt import HaltReason
from algotrader.execution.positions import ExitingPosition, PositionManager, TrackedPosition
from algotrader.execution.protective_stop import (
    exit_client_order_id,
    is_live,
    stop_client_order_id,
)
from algotrader.execution.reconciliation import UnbookedHolding, held_by_broker

log = logging.getLogger(__name__)

__all__ = ["Outcome", "SquareOffTimer", "TimerReport"]

#: The audit stage every square-off event is written under.
STAGE: Final = "SQUAREOFF"
SERVICE: Final = "squareoff-timer"


class Outcome(StrEnum):
    """What the timer did about one due position. Each value is an audit outcome."""

    #: The stop is cancelled and the MARKET exit is live.
    TIME_EXIT = "TIME_EXIT"
    #: The stop has triggered and is filling at market: it IS the exit.
    STOP_FILLING = "STOP_FILLING"
    #: The stop's cancel is not yet visible; the exit waits for the next sweep.
    CANCELLING = "CANCELLING"
    #: The book holds more than the broker. Halted; only what is held is exited.
    NOT_HELD = "NOT_HELD"
    #: Still in the book a minute past its deadline.
    OVERDUE = "OVERDUE"
    #: The time exit could not be established; the emergency path took over.
    EXIT_FAILED = "EXIT_FAILED"
    #: The broker's positions could not be read, so nothing was sent.
    UNREADABLE = "UNREADABLE"


def _assert_fits_audit_columns() -> None:
    """Refuse to load if a label this module writes would not fit the table."""
    columns = DecisionLog.__table__.c
    for column, values in (
        ("stage", [STAGE]),
        ("outcome", [o.value for o in Outcome]),
        ("service", [SERVICE]),
    ):
        width = getattr(columns[column].type, "length", None)
        too_long = [v for v in values if width is None or len(v) > width]
        if too_long:
            raise RuntimeError(f"decision_log.{column} is String({width}); {too_long} won't fit")


_assert_fits_audit_columns()


# ---------------------------------------------------------------------------
# Seams
# ---------------------------------------------------------------------------


class TimerGateway(Protocol):
    """The gateway calls the timer makes. §5.7 keeps the gateway the only path."""

    async def cancel(self, client_order_id_: str) -> bool: ...

    async def submit_exit(self, position: Any, *, trade_date: dt.date) -> str: ...

    async def find_order(self, client_order_id_: str) -> Order | None: ...


class PositionsReader(Protocol):
    async def fetch_positions(self) -> list[BrokerPosition]: ...


class EmergencyExit(Protocol):
    """``ProtectiveStop.exit_now``: exit at market, verify, halt if it fails."""

    async def exit_now(
        self, target: Any, *, trade_date: dt.date, now: dt.datetime, because: str
    ) -> None: ...


class Halter(Protocol):
    async def arm(
        self, reason: HaltReason, *, detail: str, armed_by: str, now: dt.datetime
    ) -> object: ...


AuditSink = Callable[[AuditEntry], Awaitable[object]]


@dataclass
class TimerReport:
    """One sweep, for the caller and for tests."""

    at: dt.datetime
    exited: list[str] = field(default_factory=list)
    waiting: list[str] = field(default_factory=list)
    not_held: list[str] = field(default_factory=list)
    overdue: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The timer
# ---------------------------------------------------------------------------


def _is_the_exit(stop: Order | None) -> TypeGuard[Order]:
    """Is this stop already taking the position off? Filled, AND still live.

    Mutation testing found the first version's rule - "any fill means the stop
    is the exit" - wrong for one case: a stop that part-filled and was then
    cancelled (the exchange does that when market protection cannot be met).
    It is terminal, it will sell nothing more, and the rest of the position is
    unprotected. Read as "the exit", the timer left that remainder for
    reconciliation to find a cycle later. ``is_live`` counts FILLED, so a stop
    that filled completely is still the exit.
    """
    return stop is not None and stop.filled_quantity > 0 and is_live(stop)


class SquareOffTimer:
    """Exits every position at its own deadline. Run it under the service's lock."""

    def __init__(
        self,
        *,
        gateway: TimerGateway,
        broker: PositionsReader,
        manager: PositionManager,
        protector: EmergencyExit,
        halter: Halter,
        audit: AuditSink | None = None,
        metrics: object | None = None,
        overdue_after_seconds: float = 60.0,
        cancel_settle_attempts: int = 5,
        cancel_settle_seconds: float = 0.2,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        if overdue_after_seconds <= 0:
            raise ValueError("overdue_after_seconds must be positive")
        if cancel_settle_attempts < 1:
            raise ValueError("cancel_settle_attempts must be at least 1")
        self._gateway = gateway
        self._broker = broker
        self._manager = manager
        self._protector = protector
        #: REQUIRED: a holding the broker does not have must stop the session,
        #: and a timer that could be built without a halter would only log it.
        self._halter = halter
        self._audit = audit
        self._metrics = metrics
        self._overdue_after = dt.timedelta(seconds=overdue_after_seconds)
        self._settle_attempts = cancel_settle_attempts
        self._settle_seconds = cancel_settle_seconds
        self._sleep = sleep
        #: (correlation, outcome) pairs already written. Each kind of event is
        #: recorded once per position: a sweep runs every second, and a
        #: CANCELLING row sixty times a minute is an alarm nobody reads.
        self._recorded: set[tuple[uuid.UUID, Outcome]] = set()
        self.last_tick_at: dt.datetime | None = None

    async def tick(self, now: dt.datetime) -> TimerReport:
        """One sweep of the book. Never raises for a single position."""
        if now.tzinfo is None:
            raise ValueError(f"sweep timestamp {now!r} is naive")
        self.last_tick_at = now
        report = TimerReport(at=now)
        book = self._manager.open_positions()
        # Bounded by the book, not by how long the process has lived: what a
        # closed position recorded can never be recorded again.
        held = {t.position.correlation_id for t in book}
        self._recorded = {key for key in self._recorded if key[0] in held}
        due = [t for t in book if t.position.squareoff_deadline <= now]
        if not due:
            return report

        trade_date = now.astimezone(IST).date()
        positions: Sequence[BrokerPosition] | None = None
        read_failed = False
        for tracked in due:
            if isinstance(tracked, ExitingPosition):
                continue  # already leaving at market; reconciliation confirms it
            if (tracked.position.correlation_id, Outcome.NOT_HELD) in self._recorded:
                # Halted, and nothing will be sent for it. Re-asking the broker
                # every second would spend the data budget reconciliation needs
                # (QA-VER-01 constraint 4); reconciliation re-arms the halt.
                continue
            if positions is None and not read_failed:
                try:
                    positions = await self._broker.fetch_positions()
                except Exception as exc:
                    read_failed = True
                    log.error(
                        "square-off: the broker's positions could not be read (%s); "
                        "nothing is sent until they can be",
                        one_safe_line(str(exc)),
                    )
            if positions is None:
                report.errors.append(f"{tracked.position.symbol}: positions unreadable")
                await self._record(tracked, Outcome.UNREADABLE, "broker positions unreadable", now)
                continue
            try:
                await self._square_off(tracked, positions, report, trade_date=trade_date, now=now)
            except Exception as exc:
                report.errors.append(f"{tracked.position.symbol}: {type(exc).__name__}")
                log.error("square-off of %s failed", tracked.position.symbol, exc_info=True)

        for tracked in due:
            await self._check_overdue(tracked, report, now=now)
        return report

    async def _square_off(
        self,
        tracked: TrackedPosition,
        positions: Sequence[BrokerPosition],
        report: TimerReport,
        *,
        trade_date: dt.date,
        now: dt.datetime,
    ) -> None:
        position = tracked.position
        stop_key = stop_client_order_id(position, trade_date=trade_date)
        stop = await self._gateway.find_order(stop_key)
        # The stop FIRST, then the broker's holding. A stop that filled just
        # before the deadline leaves the broker flat while the book still holds
        # the position - reconciliation closes it within a cycle - and checking
        # the holding first would halt the day on an ordinary stop-out.
        if _is_the_exit(stop):
            await self._stop_filling(tracked, stop, report, now=now)
            return
        held = held_by_broker(positions, position)
        if held == 0:
            await self._not_held(tracked, held, report, now=now)
            return
        if is_live(stop):
            # Cancel first, and do not send anything until the broker shows the
            # stop is no longer working. A cancel that raises leaves the stop
            # in place - the position is still protected - and the next sweep
            # tries again.
            await self._gateway.cancel(stop_key)
            stop = await self._settled(stop_key)
            if _is_the_exit(stop):
                await self._stop_filling(tracked, stop, report, now=now)
                return
            if is_live(stop):
                report.waiting.append(position.symbol)
                await self._record(
                    tracked, Outcome.CANCELLING, "the stop's cancel is not yet visible", now
                )
                return

        quantity = min(held, position.quantity)
        target: Any = position
        if quantity < position.quantity:
            target = UnbookedHolding(
                correlation_id=position.correlation_id,
                symbol=position.symbol,
                direction=position.direction,
                quantity=quantity,
            )
        deadline = position.squareoff_deadline.astimezone(IST).strftime("%H:%M")
        because = f"square-off deadline {deadline} IST"
        try:
            await self._gateway.submit_exit(target, trade_date=trade_date)
            exit_order = await self._gateway.find_order(
                exit_client_order_id(position, trade_date=trade_date)
            )
            if not is_live(exit_order):
                raise RuntimeError("the time exit was sent but the broker does not list it live")
        except Exception as exc:
            # The stop is gone and the time exit did not establish: that IS the
            # naked case, and it has one owner. exit_now retries under the same
            # key (so nothing is sent twice) and arms the halt if it fails too.
            await self._record(tracked, Outcome.EXIT_FAILED, type(exc).__name__, now)
            log.error(
                "square-off exit for %s failed (%s); taking the emergency path",
                position.symbol,
                one_safe_line(str(exc)),
            )
            await self._protector.exit_now(target, trade_date=trade_date, now=now, because=because)

        await self._manager.mark_exiting(position.symbol, because=because, reason=ExitReason.TIME)
        report.exited.append(position.symbol)
        self._count("squareoff_exits_total")
        await self._record(tracked, Outcome.TIME_EXIT, f"{quantity} at market; {because}", now)
        log.warning("square-off: %s x%s exiting at market (%s)", position.symbol, quantity, because)
        if quantity < position.quantity:
            await self._not_held(tracked, held, report, now=now)

    async def _settled(self, stop_key: str) -> Order | None:
        """Re-read the stop until it is no longer working, or give up for now."""
        order: Order | None = None
        for attempt in range(self._settle_attempts):
            order = await self._gateway.find_order(stop_key)
            if order is None or not is_live(order) or order.filled_quantity > 0:
                return order
            if attempt < self._settle_attempts - 1:
                await self._sleep(self._settle_seconds)
        return order

    async def _stop_filling(
        self, tracked: TrackedPosition, stop: Order, report: TimerReport, *, now: dt.datetime
    ) -> None:
        """The stop has triggered: it is a market order already, and the exit."""
        report.waiting.append(tracked.position.symbol)
        await self._record(
            tracked,
            Outcome.STOP_FILLING,
            f"the stop has filled {stop.filled_quantity} of {stop.quantity}; it is the exit",
            now,
        )

    async def _not_held(
        self, tracked: TrackedPosition, held: int, report: TimerReport, *, now: dt.datetime
    ) -> None:
        position = tracked.position
        report.not_held.append(position.symbol)
        detail = (
            f"the book holds {position.quantity} and the broker {held}; only what the "
            f"broker holds is exited, and a human must close the record"
        )
        if (position.correlation_id, Outcome.NOT_HELD) in self._recorded:
            return
        self._count("positions_not_held_total")
        log.critical("POSITION NOT HELD %s at square-off: %s", position.symbol, detail)
        await self._record(tracked, Outcome.NOT_HELD, detail, now)
        # Armed once here; reconciliation re-arms it every cycle while the book
        # and the broker disagree, so a latch cleared by hand comes back.
        await self._halter.arm(
            HaltReason.POSITION_NOT_HELD,
            detail=one_safe_line(f"{position.symbol}: {detail}"),
            armed_by="squareoff-timer",
            now=now,
        )

    async def _check_overdue(
        self, tracked: TrackedPosition, report: TimerReport, *, now: dt.datetime
    ) -> None:
        """Still in the book a minute past its deadline: alert, once."""
        position = tracked.position
        if self._manager.tracked(position.symbol) is None:
            return  # closed during this sweep
        late = now - position.squareoff_deadline
        if late < self._overdue_after:
            return
        report.overdue.append(position.symbol)
        if (position.correlation_id, Outcome.OVERDUE) in self._recorded:
            return
        self._count("positions_overdue_total")
        log.critical(
            "POSITION OVERDUE %s: still held %ss past its square-off deadline (%s)",
            position.symbol,
            int(late.total_seconds()),
            type(tracked).__name__,
        )
        await self._record(
            tracked,
            Outcome.OVERDUE,
            f"held {int(late.total_seconds())}s past the deadline as {type(tracked).__name__}",
            now,
        )

    async def _record(
        self, tracked: TrackedPosition, outcome: Outcome, detail: str, now: dt.datetime
    ) -> None:
        """Write one event, once per position and kind. Never raises."""
        key = (tracked.position.correlation_id, outcome)
        if key in self._recorded:
            return
        self._recorded.add(key)
        if self._audit is None:
            return
        try:
            await self._audit(
                AuditEntry(
                    correlation_id=tracked.position.correlation_id,
                    stage=STAGE,
                    outcome=outcome.value,
                    service=SERVICE,
                    payload={"symbol": tracked.position.symbol, "detail": one_safe_line(detail)},
                    ts=now,
                )
            )
        except Exception:
            log.exception("could not write the %s audit row", outcome.value)

    def _count(self, name: str) -> None:
        counter = getattr(self._metrics, name, None)
        if counter is None:
            return
        try:
            counter.inc()
        except Exception:  # pragma: no cover - defensive, as in positions.py
            log.warning("could not increment %s", name)
