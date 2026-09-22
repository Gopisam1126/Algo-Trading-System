"""The book: what this system believes it is holding, and why (E15-S05).

``LOW_LEVEL_ARCHITECTURE.md §5.8``. Every other safety mechanism reasons about
the book — the exposure checks size against it, the daily-loss halt is computed
from it, the square-off timer (E15-S06) walks it, reconciliation (E15-S09)
diffs it against the broker. So a book that is subtly wrong is not one bug; it
is every downstream decision being made about a portfolio that does not exist.

## The design record

**The decision.** A tracked position is one of three things, and they are
different *types* rather than a status field: :class:`ProtectedPosition` (holds
an :class:`~algotrader.execution.protective_stop.EstablishedPosition`, so the
stop was verified live at the broker), :class:`ExitingPosition` (the stop could
not be established, a market exit is live, and the fill has not been confirmed
yet) and :class:`UnverifiedPosition` (restored from the database, protection
unknown until something asks the broker).

**The alternative rejected.** One class with ``is_protected: bool``. It reads
the same and permits the state this module exists to prevent: a position
claimed protected because a column said so. A boolean can be set; an
``EstablishedPosition`` can only be *obtained*, and only from the good end of
:meth:`ProtectiveStop.attach`. That is E15-S04's build concern 3 discharged
structurally instead of by discipline.

**The failure it prevents.** Two, and the first is the one that would cost
most: a position opened for the quantity that was *ordered* rather than the
quantity that *filled*. A 100-share entry that fills 40 and is recorded as 100
gets a protective stop for 100 shares — and when that stop triggers it sells 60
shares that do not exist, turning the protective mechanism into a naked short
at the worst possible moment. The second: a restart that rebuilds the book from
``positions`` rows and presumes every one of them protected, because
``stop_price`` is NOT NULL and therefore always populated. BR-1 guarantees the
*price*, never the *order*.

**What would make this decision wrong.** A broker fill feed. Everything here
confirms fills by *asking* — the status and ``filled_quantity`` on an order we
fetched. With a push feed (Kite postbacks, or the order-update WebSocket) the
confirmation becomes an event and the polling caller in E15-S09 becomes
unnecessary; the three states would survive, the triggering would not.

## Consult filled_quantity, not status

``order_state.py`` records this as an inherited constraint and it is load
bearing here: Kite reports ``OPEN`` for an order that is *partially filled*,
so status alone cannot tell you whether you are holding anything. Worse,
``PARTIAL -> CANCELLED`` is a real and legal transition — an order cancelled at
square-off having partly filled — and that cancelled order **is a real
position**. So this module gates on ``filled_quantity`` and treats status as
corroboration only. A status-driven version would have opened no position for
the cancelled case and a full-size one for the partial.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

from algotrader.common.enums import Direction, ExitReason, PositionStatus
from algotrader.common.models.trading import Order, Position, SizingResult
from algotrader.common.text import one_safe_line
from algotrader.execution.protective_stop import (
    EstablishedPosition,
    ProtectiveStop,
    StopNotEstablishedError,
)

log = logging.getLogger(__name__)

__all__ = [
    "ExitingPosition",
    "FilledEntry",
    "Marks",
    "NotFilledError",
    "PositionManager",
    "PositionSnapshot",
    "ProtectedPosition",
    "RedisMirror",
    "TrackedPosition",
    "UnprotectableFillError",
    "UnverifiedPosition",
    "confirm_fill",
]


class PositionError(RuntimeError):
    """Base: something is wrong with what we believe we are holding."""


class NotFilledError(PositionError):
    """The order did not fill, so there is no position to open.

    Not an error condition in the ordinary sense — an entry that did not fill
    is a normal outcome. It is an exception rather than a ``None`` because the
    caller that forgets to check a ``None`` opens nothing and *believes it
    opened something*, which is the direction of mistake this module cannot
    afford.
    """


class ContradictoryFillError(PositionError):
    """The broker's account of this order cannot be true.

    Filled quantity above the ordered quantity, or a fill with no price.
    Refused rather than repaired: there is no safe way to guess which half of a
    contradiction is the real one, and both repairs open a position on a
    number nobody can vouch for.
    """


class UnprotectableFillError(PositionError):
    """The fill price puts the approved stop on the wrong side of entry.

    A long that fills *below* its intended stop is already past it. No stop can
    be placed that would not trigger instantly, so the holding is exited at
    market and never becomes a position. Rare, real, and exactly the case a
    naive implementation meets as a Pydantic ``ValidationError`` while holding
    live stock.
    """


# ---------------------------------------------------------------------------
# What we consume, as shapes rather than imports
# ---------------------------------------------------------------------------


class Quoted(Protocol):
    """A price and when it was true.

    A protocol rather than an import of ``ingest.quotes.QuoteState``:
    ``execution`` does not depend on ``ingest`` anywhere else, and marking a
    book to market does not justify being the first. ``QuoteState`` satisfies
    this structurally.
    """

    @property
    def symbol(self) -> str: ...

    @property
    def ltp(self) -> Decimal: ...

    @property
    def as_of(self) -> dt.datetime: ...


class PositionStore(Protocol):
    """Position persistence, as the capability rather than the repository."""

    async def open_position(self, position: dict[str, Any]) -> int: ...

    async def open_positions(self) -> list[dict[str, Any]]: ...

    async def close_position(
        self,
        position_id: int,
        *,
        exit_price: Decimal,
        exit_reason: str,
        realized_pnl: Decimal,
        closed_at: dt.datetime | None = None,
        max_favourable_excursion: Decimal | None = None,
        max_adverse_excursion: Decimal | None = None,
    ) -> None: ...


class Mirror(Protocol):
    """The fast-read copy of the book (``state:position:{symbol}``)."""

    async def write(self, symbol: str, snapshot: PositionSnapshot) -> None: ...

    async def clear(self, symbol: str) -> None: ...


@dataclass(frozen=True)
class UnprotectableFill:
    """A holding that cannot be expressed as a :class:`Position`.

    Satisfies ``gateway.Closable``, which is the whole reason that protocol
    exists: this is a real quantity of real stock that must be exited, and the
    only reason it is not a ``Position`` is that its stop would be invalid.
    """

    correlation_id: UUID
    symbol: str
    direction: Direction
    quantity: int


# ---------------------------------------------------------------------------
# Fill confirmation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FilledEntry:
    """How much actually filled, and at what price."""

    quantity: int
    price: Decimal
    complete: bool


def confirm_fill(order: Order) -> FilledEntry:
    """What this order actually achieved, or refuse to say.

    Gates on ``filled_quantity``, never on status — see the module docstring
    for why that distinction is the difference between a correct book and a
    stop sized for shares nobody owns.
    """
    if order.filled_quantity > order.quantity:
        raise ContradictoryFillError(
            f"order {order.client_order_id} reports {order.filled_quantity} filled "
            f"against {order.quantity} ordered. A broker cannot fill more than was "
            f"asked, so one of these numbers is wrong and neither can be trusted."
        )
    if order.filled_quantity == 0:
        raise NotFilledError(
            f"order {order.client_order_id} ({order.status.value}) has filled "
            f"nothing. There is no position here."
        )
    if order.average_price is None:
        raise ContradictoryFillError(
            f"order {order.client_order_id} reports {order.filled_quantity} filled "
            f"with no average price. A position that cannot be priced cannot be "
            f"risked, sized or marked, so it is refused rather than guessed at."
        )
    return FilledEntry(
        quantity=order.filled_quantity,
        price=order.average_price,
        complete=order.filled_quantity == order.quantity,
    )


# ---------------------------------------------------------------------------
# Marks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Marks:
    """Live P&L and the running extremes, in rupees.

    **Money, not price.** The columns are ``Numeric(14, 4)``, which is this
    schema's *price* type, and that is a leftover from an earlier intent —
    ``realized_pnl`` uses ``Numeric(14, 2)``. P&L is chosen because it is what
    makes these comparable with the realised figure and with the daily loss
    budget, and because R-multiple divides by ``initial_risk``, which is money.
    The extra two decimals are harmless.

    ``unrealised_pnl`` is ``None`` when no usable quote has arrived. Not zero:
    "we have not priced this" and "this is flat" are different facts, and
    defaulting the first to the second is how an unmarked book reads as a
    breakeven one.
    """

    unrealised_pnl: Decimal | None = None
    #: Both start at zero, which is the true excursion of a position that has
    #: never been marked — not a placeholder.
    max_favourable_excursion: Decimal = Decimal("0")
    max_adverse_excursion: Decimal = Decimal("0")
    last_price: Decimal | None = None
    marked_at: dt.datetime | None = None

    def marked(self, position: Position, ltp: Decimal, *, now: dt.datetime) -> Marks:
        """The marks after observing ``ltp``."""
        pnl = position.unrealized_pnl(ltp)
        return Marks(
            unrealised_pnl=pnl,
            max_favourable_excursion=max(self.max_favourable_excursion, pnl),
            max_adverse_excursion=min(self.max_adverse_excursion, pnl),
            last_price=ltp,
            marked_at=now,
        )


# ---------------------------------------------------------------------------
# The three states
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProtectedPosition:
    """Open, and a stop was verified live at the broker.

    Constructible only from an ``EstablishedPosition``, which is itself
    constructible only by :meth:`ProtectiveStop.attach` on its success path.
    There is deliberately no route here from a database row.
    """

    established: EstablishedPosition
    marks: Marks = Marks()

    def __post_init__(self) -> None:
        # The annotation alone was doing the work of a guarantee, and a
        # dataclass does not check types at runtime: ``ProtectedPosition(a_row)``
        # constructed happily and produced exactly the object this class exists
        # to make impossible. mypy would catch it inside ``src`` and nowhere
        # else. One rung up the ladder, from "a type checker complains" to "the
        # dangerous state cannot be represented".
        if not isinstance(self.established, EstablishedPosition):
            raise TypeError(
                f"ProtectedPosition requires an EstablishedPosition, which only "
                f"ProtectiveStop.attach can produce - got "
                f"{type(self.established).__name__}. A position is protected "
                f"because the broker confirmed a live stop, never because a "
                f"field said so."
            )

    @property
    def position(self) -> Position:
        return self.established.position

    @property
    def status(self) -> PositionStatus:
        return PositionStatus.OPEN


@dataclass(frozen=True)
class ExitingPosition:
    """The stop failed; a market exit is live and its fill is not confirmed.

    E15-S04 proves the exit is *live*. Proving it *filled* is this story's job,
    and until :meth:`PositionManager.confirm_exit` says so the position is
    still held. Reading the CRITICAL log line as a flat book is the mistake
    that constraint exists to prevent.
    """

    position: Position
    because: str
    marks: Marks = Marks()

    @property
    def status(self) -> PositionStatus:
        return PositionStatus.CLOSING


@dataclass(frozen=True)
class UnverifiedPosition:
    """Restored from the database. Whether a stop protects it is unknown.

    ``stop_price`` is populated on every row because BR-1 makes the column NOT
    NULL, so the row can say nothing about whether an *order* is protecting
    anything. Only the broker can, via ``ProtectiveStop.is_protected`` —
    E15-S09's loop.
    """

    position: Position
    marks: Marks = Marks()

    @property
    def status(self) -> PositionStatus:
        return PositionStatus.OPEN


TrackedPosition = ProtectedPosition | ExitingPosition | UnverifiedPosition


class PositionSnapshot(BaseModel):
    """The fast-read mirror written to ``state:position:{symbol}``.

    Stored as JSON, despite ``§4.3`` and ``keys.position_state`` both calling
    it a HASH. Every other ``state:*`` key in this codebase is already JSON via
    ``state.set_state`` — the quote key says HASH and is a string — because
    ``model_dump_json`` round-trips ``Decimal`` exactly and a hash of
    stringified decimals would re-introduce parsing at every read. The
    docstrings are corrected to match the code rather than the reverse.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    correlation_id: UUID
    position_id: int | None
    slot_index: int
    direction: Direction
    quantity: int
    entry_price: Decimal
    stop_price: Decimal
    squareoff_deadline: dt.datetime
    status: PositionStatus
    #: What this process believed when it wrote the snapshot - and a CACHE, not
    #: an authority.
    #:
    #: **Never treat this as proof of protection.** It is the same mistake as
    #: reading ``stop_price`` off a row, one store along: anything with Redis
    #: access can write this key, the value can be stale by a tick, and a
    #: reader that trusted it would have laundered "we thought so a moment ago"
    #: into "the broker confirms a live stop". The only thing that can answer
    #: that question is ``ProtectiveStop.is_protected``, which asks the broker.
    #: Recorded as a constraint on E15-S09, whose loop is the first real
    #: consumer.
    protection: str
    unrealised_pnl: Decimal | None
    max_favourable_excursion: Decimal
    max_adverse_excursion: Decimal
    last_price: Decimal | None
    marked_at: dt.datetime | None

    @classmethod
    def of(cls, tracked: TrackedPosition) -> PositionSnapshot:
        p = tracked.position
        m = tracked.marks
        return cls(
            symbol=p.symbol,
            correlation_id=p.correlation_id,
            position_id=p.position_id,
            slot_index=p.slot_index,
            direction=p.direction,
            quantity=p.quantity,
            entry_price=p.entry_price,
            stop_price=p.stop_price,
            squareoff_deadline=p.squareoff_deadline,
            status=tracked.status,
            protection=type(tracked).__name__,
            unrealised_pnl=m.unrealised_pnl,
            max_favourable_excursion=m.max_favourable_excursion,
            max_adverse_excursion=m.max_adverse_excursion,
            last_price=m.last_price,
            marked_at=m.marked_at,
        )


# ---------------------------------------------------------------------------
# The manager
# ---------------------------------------------------------------------------


class RedisMirror:
    """The real mirror: ``state:position:{symbol}``, written as JSON.

    No TTL. A position's lifetime is managed explicitly - it is deleted when
    the position closes - because a mirror that expired on its own would make
    a live position vanish from the fast path while it was still held, and
    every reader of that path treats absent as "no position".
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def write(self, symbol: str, snapshot: PositionSnapshot) -> None:
        from algotrader.common.redis import keys, state

        await state.set_state(self._client, keys.position_state(symbol), snapshot)

    async def clear(self, symbol: str) -> None:
        from algotrader.common.redis import keys, state

        await state.delete_state(self._client, keys.position_state(symbol))


class PositionManager:
    """Opens positions from fills, prices them, and closes them when exited."""

    def __init__(
        self,
        *,
        protector: ProtectiveStop,
        store: PositionStore,
        mirror: Mirror,
        calendar: Any,
        exit_buffer_minutes: int = 5,
        max_quote_age_seconds: float = 60.0,
        metrics: object | None = None,
    ) -> None:
        self._protector = protector
        #: REQUIRED, for the reason the gateway's store is: a manager that
        #: cannot record a position would open real ones untracked, and the
        #: absent capability would read as a satisfied one.
        self._store = store
        self._mirror = mirror
        self._calendar = calendar
        self._exit_buffer_minutes = exit_buffer_minutes
        #: A second staleness guard, on top of ``QuotePublisher.read_fresh``.
        #: Deliberate duplication: marking is what the daily-loss halt reads,
        #: and a component whose wrong answer stops the day from stopping does
        #: not delegate its own safety check to its caller.
        self._max_quote_age_seconds = max_quote_age_seconds
        self._metrics = metrics
        self._book: dict[str, TrackedPosition] = {}

    # -- reading ------------------------------------------------------------

    def tracked(self, symbol: str) -> TrackedPosition | None:
        return self._book.get(symbol)

    def open_positions(self) -> list[TrackedPosition]:
        return list(self._book.values())

    @property
    def unrealised_pnl(self) -> Decimal | None:
        """The book's live P&L, or ``None`` if anything in it is unpriced.

        Refusing to sum a partially-priced book is the fail-closed choice: the
        daily-loss halt reads this, and a total that silently omits the one
        position nobody could price is a total that is wrong in the direction
        of continuing to trade.
        """
        if not self._book:
            return Decimal("0")
        total = Decimal("0")
        for tracked in self._book.values():
            if tracked.marks.unrealised_pnl is None:
                return None
            total += tracked.marks.unrealised_pnl
        return total

    # -- opening ------------------------------------------------------------

    async def open_from_fill(
        self,
        order: Order,
        *,
        sizing: SizingResult,
        slot_index: int,
        trade_date: dt.date,
        now: dt.datetime,
        is_cas_stock: bool,
        is_fno: bool = False,
        strategy_id: str | None = None,
    ) -> ProtectedPosition:
        """Turn a filled entry into a protected, tracked position.

        **This is the fill-confirmation trigger** E15-S04 recorded as belonging
        here. Until it existed, nothing called ``ProtectiveStop.attach`` and an
        entry could fill unprotected.

        The order of operations is the gateway's TX1/TX2 reasoning applied one
        component along. The row is written **before** the stop is attached,
        because the two crash windows are not equally bad: die after the insert
        and reconciliation finds a position row whose stop is missing, and
        exits one position; die after attaching but before the insert and the
        broker holds a position we have no record of, which §8.3 says trips the
        kill switch. Losing a trade beats losing the day.
        """
        if now.tzinfo is None:
            raise ValueError(
                f"fill timestamp {now!r} is naive. Which session a position "
                f"belongs to must be unambiguous."
            )

        entry = confirm_fill(order)
        if not entry.complete:
            self._count("partial_fills_total")
            log.warning(
                "entry %s for %s filled %s of %s - opening the position that "
                "EXISTS, not the one that was ordered",
                order.client_order_id,
                order.symbol,
                entry.quantity,
                order.quantity,
            )

        direction = Direction.LONG if order.side.value == "BUY" else Direction.SHORT
        deadline = self._calendar.squareoff_deadline(
            trade_date,
            is_cas_stock=is_cas_stock,
            is_fno=is_fno,
            buffer_minutes=self._exit_buffer_minutes,
        )

        try:
            position = Position(
                correlation_id=order.correlation_id,
                symbol=order.symbol,
                slot_index=slot_index,
                direction=direction,
                quantity=entry.quantity,
                entry_price=entry.price,
                stop_price=sizing.stop_price,
                target_price=sizing.target_price,
                opened_at=now,
                squareoff_deadline=deadline,
            )
        except ValidationError as exc:
            # The fill landed at or through the approved stop. No stop can be
            # placed here that would not trigger instantly, so this holding
            # never becomes a position. Nothing is persisted on purpose: a row
            # claiming a stop that is not on the protective side of entry would
            # be a false record, and the exit order the gateway writes carries
            # the same correlation_id for anyone tracing it.
            await self._protector.exit_now(
                UnprotectableFill(
                    correlation_id=order.correlation_id,
                    symbol=order.symbol,
                    direction=direction,
                    quantity=entry.quantity,
                ),
                trade_date=trade_date,
                now=now,
                because=(
                    f"filled at {entry.price} against an approved stop of "
                    f"{sizing.stop_price}; the stop is already breached"
                ),
            )
            self._count("unprotectable_fills_total")
            raise UnprotectableFillError(
                f"{order.symbol} x{entry.quantity} filled at {entry.price}, which "
                f"puts the approved stop {sizing.stop_price} on the wrong side of "
                f"entry. The holding was exited at market and is not a position. "
                f"({one_safe_line(str(exc))})"
            ) from exc

        position_id = await self._store.open_position(
            {
                "correlation_id": position.correlation_id,
                "symbol": position.symbol,
                "strategy_id": strategy_id,
                "slot_index": position.slot_index,
                "direction": position.direction.value,
                "quantity": position.quantity,
                "entry_price": position.entry_price,
                "stop_price": position.stop_price,
                "target_price": position.target_price,
                "opened_at": position.opened_at,
                "squareoff_deadline": position.squareoff_deadline,
            }
        )
        position = position.model_copy(update={"position_id": position_id})

        try:
            established = await self._protector.attach(position, trade_date=trade_date, now=now)
        except StopNotEstablishedError as exc:
            # Contained: a live market exit exists for the whole position. It
            # stays in the book until its fill is confirmed, which is the
            # difference between "an exit was placed" and "we are flat".
            exiting = ExitingPosition(position=position, because=one_safe_line(str(exc)))
            self._book[position.symbol] = exiting
            await self._mirror.write(position.symbol, PositionSnapshot.of(exiting))
            raise

        tracked = ProtectedPosition(established=established)
        self._book[position.symbol] = tracked
        await self._mirror.write(position.symbol, PositionSnapshot.of(tracked))
        self._count("positions_opened_total")
        log.info(
            "position %s x%s opened at %s and protected by %s",
            position.symbol,
            position.quantity,
            position.entry_price,
            established.stop_broker_order_id,
        )
        return tracked

    # -- pricing ------------------------------------------------------------

    async def mark(self, quote: Quoted, *, now: dt.datetime) -> TrackedPosition | None:
        """Price one position against a quote. Stale quotes do not count."""
        tracked = self._book.get(quote.symbol)
        if tracked is None:
            return None
        age = (now - quote.as_of).total_seconds()
        if age > self._max_quote_age_seconds:
            log.warning(
                "quote for %s is %.1fs old (limit %.1fs) - not marking",
                quote.symbol,
                age,
                self._max_quote_age_seconds,
            )
            return tracked
        updated = replace(tracked, marks=tracked.marks.marked(tracked.position, quote.ltp, now=now))
        self._book[quote.symbol] = updated
        await self._mirror.write(quote.symbol, PositionSnapshot.of(updated))
        return updated

    # -- closing ------------------------------------------------------------

    async def confirm_exit(
        self, exit_order: Order, *, now: dt.datetime, reason: ExitReason = ExitReason.UNPROTECTED
    ) -> TrackedPosition | None:
        """Confirm an exit FILLED, and only then call the position closed.

        Returns ``None`` when the position is gone from the book. A partially
        filled exit leaves the position open for the remainder, because a
        half-exited position is still a position and calling it closed is the
        same class of mistake as opening one for the ordered quantity.
        """
        tracked = self._book.get(exit_order.symbol)
        if tracked is None:
            log.warning(
                "exit %s is for %s, which this book does not hold",
                exit_order.client_order_id,
                exit_order.symbol,
            )
            return None

        fill = confirm_fill(exit_order)
        position = tracked.position
        if fill.quantity < position.quantity:
            self._count("partial_fills_total")
            log.warning(
                "exit for %s filled %s of %s - the remainder is still held",
                position.symbol,
                fill.quantity,
                position.quantity,
            )
            return tracked

        realised = position.unrealized_pnl(fill.price)
        marks = tracked.marks.marked(position, fill.price, now=now)
        if position.position_id is not None:
            await self._store.close_position(
                position.position_id,
                exit_price=fill.price,
                exit_reason=reason.value,
                realized_pnl=realised,
                closed_at=now,
                max_favourable_excursion=marks.max_favourable_excursion,
                max_adverse_excursion=marks.max_adverse_excursion,
            )
        await self._mirror.clear(position.symbol)
        del self._book[position.symbol]
        log.info(
            "position %s closed at %s for %s (%s)",
            position.symbol,
            fill.price,
            realised,
            reason.value,
        )
        return None

    # -- restarting ---------------------------------------------------------

    async def restore(self) -> list[UnverifiedPosition]:
        """Rebuild the book from the database, protection UNKNOWN.

        Every restored position is :class:`UnverifiedPosition`, and there is no
        path from here to :class:`ProtectedPosition` — obtaining one requires
        an ``EstablishedPosition``, which requires the broker to have confirmed
        a live stop. E15-S09's loop is what asks.
        """
        restored: list[UnverifiedPosition] = []
        for row in await self._store.open_positions():
            marks = Marks(
                max_favourable_excursion=row.get("max_favourable_excursion") or Decimal("0"),
                max_adverse_excursion=row.get("max_adverse_excursion") or Decimal("0"),
            )
            position = Position.model_validate(
                {k: v for k, v in row.items() if k not in _NOT_MODEL_FIELDS}
            )
            unverified = UnverifiedPosition(position=position, marks=marks)
            self._book[position.symbol] = unverified
            restored.append(unverified)
        log.info("restored %s open position(s); none is presumed protected", len(restored))
        return restored

    # -- metrics ------------------------------------------------------------

    def _count(self, name: str) -> None:
        counter = getattr(self._metrics, name, None)
        if counter is None:
            return
        try:
            counter.inc()
        except Exception:  # pragma: no cover - defensive
            log.warning("could not increment %s", name)


#: Repository rows carry a couple of keys the Pydantic model does not model.
_NOT_MODEL_FIELDS = frozenset({"strategy_id"})
