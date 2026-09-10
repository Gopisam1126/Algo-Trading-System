"""Protective stop attachment — no position survives a cycle unprotected (E15-S04).

``LOW_LEVEL_ARCHITECTURE.md §5.7``: *"Places the protective stop immediately
after entry fill confirmation. If the stop order fails to place, the position is
closed at market immediately — a naked position is never acceptable."* This is
that sentence, and the invariant it serves is the fifth in ``CLAUDE.md``: every
position has a stop and a time exit.

## The design record

**The decision.** One object owns the sequence *place → verify → on any failure
close → on a failed close halt*, and the only way to obtain an
:class:`EstablishedPosition` is to come out of the good end of it. Holding one
is therefore proof that a stop was live at the broker, not merely submitted.

**The alternative rejected.** Returning a boolean, or a position object with an
``is_protected`` flag. Both let a caller ignore the answer, and the caller that
ignores it is the one that opens the next position. A flag would also have to
be *checked* everywhere the position is used, which is discipline where
structure is available: an unprotected position simply has no
``EstablishedPosition`` to hand on.

**The failure it prevents.** A filled entry whose stop was refused, leaving a
position with no exit but the square-off deadline hours away. On a 2% adverse
move that is the entire day's loss budget on one name, and nothing would have
raised: the entry succeeded, the row exists, ``stop_price`` is populated
because BR-1 makes it NOT NULL — the *price* is recorded whether or not any
*order* is protecting it. Those are different facts and this module is where
they stop being conflated.

**What would make this decision wrong.** If the broker ever offers a true
bracket order that places entry and stop atomically, this sequence becomes a
worse version of one API call. Zerodha withdrew Bracket Orders in 2020 and has
not reinstated them, so the two-step is currently the only shape available.

## Why verification is separate from submission

A broker accepting an order is not the same as an order being live, and the gap
between them is where this story's build concern lives: *"the stop being
ACCEPTED and then rejected asynchronously, which is not the same as a
synchronous placement failure."*

So there are two checks, doing different jobs:

* :meth:`ProtectiveStop.attach` verifies **synchronously**, immediately after
  submission, and treats an order the broker will not list as not live.
* :meth:`ProtectiveStop.is_protected` is the **predicate reconciliation calls**
  every 30 seconds (§8.3). It is what catches a stop that was accepted and was
  rejected a second later, which no synchronous check can see.

Only the first is wired here. The loop that calls the second is E15-S09, and
the constraint is recorded on that story rather than left to be discovered.

## What counts as live

Present in the broker's orderbook, and not ``REJECTED`` or ``CANCELLED``.

``SUBMITTED`` — Kite's validation-pending states — counts as live *at this
moment*, and that is deliberate rather than lax. An order still validating has
not been refused, and treating it as a failure would close a healthy position
on a normal broker delay. The window it leaves open is exactly the window
:meth:`is_protected` exists to close.

``FILLED`` also counts: a stop that has already triggered did its job, and the
position it protected is gone.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Protocol

from algotrader.common.enums import OrderStatus
from algotrader.common.models.trading import Order, Position
from algotrader.common.text import one_safe_line
from algotrader.execution.gateway import client_order_id
from algotrader.execution.halt import HaltReason

log = logging.getLogger(__name__)

__all__ = [
    "EstablishedPosition",
    "NakedPositionError",
    "ProtectiveStop",
    "StopFailureError",
    "StopNotEstablishedError",
    "stop_client_order_id",
]

#: Statuses that mean the stop is NOT protecting anything. Everything else —
#: including the validation-pending states — counts as live; see the module
#: docstring for why, and for what closes the window that leaves.
_NOT_LIVE = frozenset({OrderStatus.REJECTED, OrderStatus.CANCELLED})


class StopFailureError(RuntimeError):
    """Base: a protective stop could not be established."""


class StopNotEstablishedError(StopFailureError):
    """The stop failed and an exit was submitted AND verified at the broker.

    Bad, and contained: an exit exists for the whole position. Note the
    precision — "verified at the broker", not "filled". Confirming the FILL is
    the position manager's job (E15-S05); what is established here is that the
    exit is live and will fill, which is the strongest claim available without
    a fill feed.
    """


class NakedPositionError(StopFailureError):
    """The stop failed AND the emergency close failed. The session is halted.

    The worst state this system can reach: a real position, no protection, and
    no demonstrated ability to exit it. Halting does not fix it — a human must
    — but it stops the system opening a second one while the first is
    unattended.
    """


class Halter(Protocol):
    """The halt capability, as a shape rather than an import of Redis."""

    async def arm(
        self, reason: HaltReason, *, detail: str, armed_by: str, now: dt.datetime
    ) -> object: ...


class StopGateway(Protocol):
    """The gateway capabilities this needs. §5.7 keeps it the only path out."""

    async def submit_stop(self, position: Position, *, trade_date: dt.date) -> str: ...

    async def submit_exit(self, position: Position, *, trade_date: dt.date) -> str: ...

    async def find_order(self, client_order_id_: str) -> Order | None: ...


@dataclass(frozen=True)
class EstablishedPosition:
    """A position whose protective stop was verified live at the broker.

    Constructible only by :meth:`ProtectiveStop.attach`, on the path where the
    verification passed. That is the point: a caller cannot hold one of these
    for a position that is unprotected, so "is this position safe to keep?"
    stops being a question anyone has to remember to ask.
    """

    position: Position
    stop_client_order_id: str
    stop_broker_order_id: str
    established_at: dt.datetime


def _exit_cid(position: Position, trade_date: dt.date) -> str:
    """The emergency exit's idempotency key, derived like the stop's."""
    from algotrader.common.enums import Direction, OrderIntent, Side

    side = Side.SELL if position.direction is Direction.LONG else Side.BUY
    return client_order_id(
        correlation_id=position.correlation_id,
        symbol=position.symbol,
        side=side,
        intent=OrderIntent.SQUAREOFF,
        trade_date=trade_date,
    )


def stop_client_order_id(position: Position, *, trade_date: dt.date) -> str:
    """The idempotency key of this position's protective stop.

    Derivable from the position alone, which is what makes a schema change
    unnecessary: ``positions`` has no column naming the stop order, and does
    not need one. §8.2's hash over
    ``(correlation_id, symbol, side, intent, trade_date)`` with
    ``intent=STOP`` names exactly one order, computable by anyone holding the
    position — including a reconciliation loop that has never seen it placed.
    """
    from algotrader.common.enums import Direction, OrderIntent, Side

    side = Side.SELL if position.direction is Direction.LONG else Side.BUY
    return client_order_id(
        correlation_id=position.correlation_id,
        symbol=position.symbol,
        side=side,
        intent=OrderIntent.STOP,
        trade_date=trade_date,
    )


def is_live(order: Order | None) -> bool:
    """Whether this order is protecting anything.

    ``None`` — the broker does not list it — is NOT live. Acceptance is not
    liveness, and an order the broker will not show us is one we cannot claim
    is working.
    """
    return order is not None and order.status not in _NOT_LIVE


class ProtectiveStop:
    """Places the stop, verifies it, and closes the position if it cannot."""

    def __init__(
        self,
        gateway: StopGateway,
        *,
        halter: Halter,
        metrics: object | None = None,
    ) -> None:
        self._gateway = gateway
        #: REQUIRED. The escalation for a naked position that will not close is
        #: not optional, and an attacher constructed without one would fail
        #: silently in exactly the case that matters most.
        self._halter = halter
        self._metrics = metrics

    async def attach(
        self, position: Position, *, trade_date: dt.date, now: dt.datetime
    ) -> EstablishedPosition:
        """Protect a filled position, or close it, or halt.

        Returns only on the path where a stop is verified live. Every other
        path raises, because there is no return value that a caller could
        safely ignore.
        """
        if now.tzinfo is None:
            raise ValueError(
                f"attach timestamp {now!r} is naive. Which session a naked "
                f"position belongs to must be unambiguous."
            )

        cid = stop_client_order_id(position, trade_date=trade_date)
        try:
            broker_order_id = await self._gateway.submit_stop(position, trade_date=trade_date)
        except Exception as exc:
            # Deliberately total. Every failure to submit a stop has the same
            # response, and enumerating broker exception types here would mean
            # a new one silently became "protected".
            await self._emergency_close(
                position,
                trade_date=trade_date,
                now=now,
                because=f"the stop could not be submitted: {one_safe_line(str(exc))}",
            )
            raise StopNotEstablishedError(
                f"stop for {position.symbol} could not be submitted; the position "
                f"was exited at market. Cause: {one_safe_line(str(exc))}"
            ) from exc

        # Submitted is not protected. Ask the broker.
        order = await self._gateway.find_order(cid)
        if not is_live(order):
            observed = "absent from the orderbook" if order is None else order.status.value
            await self._emergency_close(
                position,
                trade_date=trade_date,
                now=now,
                because=f"the stop was submitted but is {observed}",
            )
            raise StopNotEstablishedError(
                f"stop {cid} for {position.symbol} was submitted as broker order "
                f"{broker_order_id} but is {observed}. The position was exited at "
                f"market: an accepted stop is not a live one."
            )

        log.info(
            "position %s protected by stop %s (broker %s) at %s",
            position.symbol,
            cid,
            broker_order_id,
            position.stop_price,
        )
        return EstablishedPosition(
            position=position,
            stop_client_order_id=cid,
            stop_broker_order_id=broker_order_id,
            established_at=now,
        )

    async def is_protected(self, position: Position, *, trade_date: dt.date) -> bool:
        """The predicate the reconciliation loop calls (E15-S09).

        Recomputes the stop's idempotency key from the position and asks the
        broker. This is what catches a stop that was accepted at attach time
        and rejected afterwards — the case the story's build concern names, and
        the one no synchronous check can reach.
        """
        cid = stop_client_order_id(position, trade_date=trade_date)
        return is_live(await self._gateway.find_order(cid))

    async def _emergency_close(
        self, position: Position, *, trade_date: dt.date, now: dt.datetime, because: str
    ) -> None:
        """Close at market now. If that fails too, halt the session."""
        self._count("stop_attach_failures_total")
        log.critical(
            "NAKED POSITION: %s %s x%s has no protective stop - %s. Closing at market.",
            position.direction.value,
            position.symbol,
            position.quantity,
            one_safe_line(because),
        )
        try:
            exit_id = await self._gateway.submit_exit(position, trade_date=trade_date)
            # Acceptance is not liveness — the claim this whole module rests
            # on, and it applies to the exit exactly as it applies to the
            # stop. Trusting the exit's acceptance while distrusting the
            # stop's would be indefensible: the exit is the ONLY thing
            # standing between a naked position and the square-off deadline.
            # Found by the STRIDE pass against this story's own diff
            # (QA-E15-11); the first version logged "closed at market" having
            # verified nothing.
            if not is_live(await self._gateway.find_order(_exit_cid(position, trade_date))):
                raise StopFailureError(
                    f"the exit for {position.symbol} was submitted as {exit_id} but "
                    f"the broker does not list it as live"
                )
        except Exception as exc:
            self._count("naked_positions_total")
            log.critical(
                "NAKED POSITION COULD NOT BE CLOSED: %s %s x%s - %s. Halting.",
                position.direction.value,
                position.symbol,
                position.quantity,
                one_safe_line(str(exc)),
            )
            # Armed BEFORE raising, so the halt survives a caller that swallows
            # the exception. The order matters: a raise-then-arm would leave the
            # system trading if anything upstream caught this.
            await self._halter.arm(
                HaltReason.NAKED_POSITION,
                detail=one_safe_line(
                    f"{position.symbol} x{position.quantity} is unprotected and could "
                    f"not be closed: {exc}"
                ),
                armed_by="protective-stop",
                now=now,
            )
            raise NakedPositionError(
                f"{position.symbol} x{position.quantity} has no stop and could not be "
                f"exited at market ({one_safe_line(str(exc))}). The session is halted; "
                f"this position needs a human."
            ) from exc

        log.critical(
            "unprotected position %s has a LIVE market exit, broker order %s. The "
            "fill is not confirmed here - that is the position manager's job.",
            position.symbol,
            exit_id,
        )

    def _count(self, name: str) -> None:
        """Increment a counter if metrics are wired, and never fail on them.

        A metrics backend that is down must not turn a contained failure into
        an uncontained one — this is called from the path whose whole job is to
        close a naked position.
        """
        counter = getattr(self._metrics, name, None)
        if counter is None:
            return
        try:
            counter.inc()
        except Exception:  # pragma: no cover - defensive; see docstring
            log.warning("could not increment %s", name)
