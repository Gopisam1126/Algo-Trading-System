"""The risk context for one signal, assembled from the live session (E15-S13).

:class:`~algotrader.execution.risk.context.RiskContext` is what the fourteen
checks are allowed to see. Until this story nothing in production built one:
every test supplied it by hand. This module is the one place it is built from
the real session - the halt latches, the book, the entries already sent, the
day's closed trades, the instrument master and the day's surveillance flags.

## The design record

**The decision.** An entry that has been SENT but not yet BOOKED counts as a
held position, an occupied slot and exposure, exactly as if it had filled.
:class:`PendingEntries` holds them, and :class:`PendingAwareSlots` makes the
slot allocator see them.

**The alternative rejected.** Counting only the book and the ``positions``
table. Both learn of an entry when reconciliation books its fill - up to one
cycle, thirty seconds, after it was sent. A second signal in that window would
see a free slot, an unheld symbol and an under-used sector, and every one of
those readings would be wrong. Two entries into one symbol is the cheapest
version of that; five slots holding six positions is the expensive one.

**The failure it prevents.** Double allocation inside the booking window. The
slot index itself is also protected: the allocator's Redis lock is held only
across the claim, and nothing inserts the slot's ``positions`` row until the
fill is booked, so without the pending set a second claim would be handed the
same index.

**What would make this wrong.** A second execution process. The pending set
lives in memory, which is sound only because execution is single-process and
strictly serialised (``LOW_LEVEL_ARCHITECTURE.md §9.1``). A second process
would need the set in Redis - and the slot claim held until booking.

## What a missing input becomes

Each ``RiskContext`` field that may be unknown is ``| None``, and the check that
reads it refuses on ``None`` (AUDIT-005). This module keeps that contract: a
read that fails, or an input nothing produces yet, becomes ``None`` - never a
zero, never "nothing armed". The engine then rejects, audits the rejection and
names what was missing.

Three inputs have no producer in this codebase yet, and :class:`NoProducerYet`
supplies each as unknown: the service-health roll-up (E20-S05), a live ATR, and
the broker's per-share margin. **Until they exist the engine rejects every
signal** - which is the correct behaviour for a system missing a safety input,
and the reason ``SessionFacts`` is a seam rather than a class.

What cannot be ``None`` - the slot count, the deadline, the instrument itself -
raises :class:`ContextUnavailableError`, and the service refuses the signal with
an audited reason before the engine is asked.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Awaitable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol, TypeVar

from algotrader.common.calendar import IST
from algotrader.common.enums import Direction
from algotrader.common.models.trading import Recommendation, SizingResult
from algotrader.common.text import one_safe_line
from algotrader.execution.risk.context import OpenPosition, RiskContext, RiskContextError

log = logging.getLogger(__name__)

T = TypeVar("T")

__all__ = [
    "BuiltContext",
    "ContextStore",
    "ContextUnavailableError",
    "NoProducerYet",
    "PendingAwareSlots",
    "PendingEntries",
    "PendingEntry",
    "RepositoryContextStore",
    "RiskContextBuilder",
    "SessionFacts",
    "restrictions_from",
    "session_losses",
]


class ContextUnavailableError(RuntimeError):
    """A value the risk context cannot be built without could not be read."""


# ---------------------------------------------------------------------------
# Entries sent and not yet booked
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingEntry:
    """An entry the broker has been sent, sized and slotted, not yet booked."""

    client_order_id: str
    symbol: str
    direction: Direction
    sizing: SizingResult
    slot_index: int


class PlanHolder(Protocol):
    """The one thing pruning needs from the position manager."""

    def plan_for(self, client_order_id: str) -> object | None: ...


class PendingEntries:
    """Entries that hold a slot, a symbol and exposure before they are booked.

    **Pruned from the position manager's plans, not maintained alongside
    them.** Reconciliation forgets an entry's plan when it books the fill, when
    the entry ends unfilled, and when it is written SUBMIT_FAILED; each of
    those is the moment the entry stops being pending. Mirroring each event
    here would be a second copy of that lifecycle to keep in step - and the
    copy that drifts is the one that leaks a slot for the rest of the day.
    """

    def __init__(self, plans: PlanHolder) -> None:
        self._plans = plans
        self._entries: dict[str, PendingEntry] = {}

    def add(self, entry: PendingEntry) -> None:
        if entry.client_order_id in self._entries:
            raise ValueError(f"entry {entry.client_order_id} is already pending")
        self._entries[entry.client_order_id] = entry

    def prune(self) -> None:
        """Drop every entry whose plan the position manager no longer holds.

        Every read below prunes first, so the set cannot be OBSERVED stale: a
        slot whose entry was booked or failed a cycle ago is never reported
        as still in flight, whoever asks and whenever.
        """
        for cid in [c for c in self._entries if self._plans.plan_for(c) is None]:
            del self._entries[cid]

    def __contains__(self, client_order_id: object) -> bool:
        self.prune()
        return client_order_id in self._entries

    def __len__(self) -> int:
        self.prune()
        return len(self._entries)

    def entries(self) -> tuple[PendingEntry, ...]:
        self.prune()
        return tuple(self._entries.values())

    def slots(self) -> set[int]:
        self.prune()
        return {e.slot_index for e in self._entries.values()}


class SlotsSource(Protocol):
    async def occupied_slots(self) -> set[int]: ...


class PendingAwareSlots:
    """The slot allocator's view: open positions AND entries still in flight.

    Passed to :class:`~algotrader.execution.slots.SlotManager` as its
    ``SlotsProvider``. Without it, the index an in-flight entry was given reads
    as free until its fill is booked.
    """

    def __init__(self, positions: SlotsSource, pending: PendingEntries) -> None:
        self._positions = positions
        self._pending = pending

    async def occupied_slots(self) -> set[int]:
        return set(await self._positions.occupied_slots()) | self._pending.slots()


# ---------------------------------------------------------------------------
# Pure derivations - each one a decision worth testing on its own
# ---------------------------------------------------------------------------


def restrictions_from(status: Mapping[str, Any] | None) -> tuple[str, ...] | None:
    """The day's surveillance flags as the restrictions that BLOCK an entry.

    ``None`` when there is no row for the day: eligibility was never
    established, which the eligibility check refuses (``RiskContext`` documents
    the three states).

    **All four flags block - a recorded assumption, and the conservative one.**
    T2T is delivery-only (an intraday MIS order is refused or force-delivered);
    the F&O ban stops new positions in the derivative and is a strong signal on
    the cash leg; ASM and GSM bring 100% margin and narrowed bands at minimum,
    and GSM's higher stages permit no upward move at all. Deciding that some
    stage or category is tradable intraday would be a business call, made with
    the data; until someone makes it, a flagged symbol is not traded.
    """
    if status is None:
        return None
    blocked: list[str] = []
    if status.get("is_t2t") is not False:
        blocked.append("T2T")
    if status.get("is_asm") is not False:
        category = status.get("asm_category")
        blocked.append(f"ASM:{category}" if category else "ASM")
    if status.get("is_gsm") is not False:
        stage = status.get("gsm_stage")
        blocked.append(f"GSM:{stage}" if stage is not None else "GSM")
    if status.get("is_fno_ban") is not False:
        blocked.append("FNO_BAN")
    # `is not False` rather than truthiness: a flag that is missing or NULL is
    # not a clean flag. The columns are NOT NULL today; this keeps a future
    # nullable column, or a row built by hand, from reading as "not restricted".
    return tuple(blocked)


def session_losses(closed: Sequence[Mapping[str, Any]]) -> tuple[Decimal | None, int | None]:
    """Today's realised P&L and the current losing streak, from closed trades.

    ``closed`` is oldest first. Returns ``(None, None)`` when any closed trade
    has no realised P&L: a total that silently skips one trade is a total that
    is wrong in the direction of trading on, and a streak counted past a gap
    cannot be trusted either.

    **A break-even trade neither extends nor ends a streak.** Ending it would
    let one scratch trade clear two real losses; extending it would halt on
    trades that lost nothing. Only a strictly positive result ends a streak.
    """
    pnls: list[Decimal] = []
    for row in closed:
        pnl = row.get("realized_pnl")
        if pnl is None:
            return None, None
        pnls.append(Decimal(pnl))
    streak = 0
    for pnl in reversed(pnls):
        if pnl > 0:
            break
        if pnl < 0:
            streak += 1
    return sum(pnls, Decimal(0)), streak


def session_start(trade_date: dt.date) -> dt.datetime:
    """Midnight IST on the trade date, as UTC: where "today" begins."""
    return dt.datetime.combine(trade_date, dt.time(0, 0), tzinfo=IST).astimezone(dt.UTC)


# ---------------------------------------------------------------------------
# The seams
# ---------------------------------------------------------------------------


class ContextStore(Protocol):
    """The persisted facts the context reads. One method per question."""

    async def closed_since(self, since: dt.datetime) -> list[dict[str, Any]]: ...

    async def occupied_slots(self) -> set[int]: ...

    async def profile(self, symbol: str) -> dict[str, Any] | None: ...

    async def daily_status(self, symbol: str, trade_date: dt.date) -> dict[str, Any] | None: ...


class RepositoryContextStore:
    """:class:`ContextStore` over the three repositories that hold the answers."""

    def __init__(self, *, positions: Any, instruments: Any, daily_status: Any) -> None:
        self._positions = positions
        self._instruments = instruments
        self._daily_status = daily_status

    async def closed_since(self, since: dt.datetime) -> list[dict[str, Any]]:
        return list(await self._positions.closed_since(since))

    async def occupied_slots(self) -> set[int]:
        return set(await self._positions.occupied_slots())

    async def profile(self, symbol: str) -> dict[str, Any] | None:
        result: dict[str, Any] | None = await self._instruments.profile(symbol)
        return result

    async def daily_status(self, symbol: str, trade_date: dt.date) -> dict[str, Any] | None:
        result: dict[str, Any] | None = await self._daily_status.for_symbol(symbol, trade_date)
        return result


class SessionFacts(Protocol):
    """The live inputs whose producers are other stories.

    Each answer is ``None`` (or, for correlations, an absent symbol) when it is
    not known. The risk engine refuses on every one of those.
    """

    async def unhealthy_services(self, *, now: dt.datetime) -> tuple[str, ...] | None: ...

    async def atr(self, symbol: str, *, now: dt.datetime) -> Decimal | None: ...

    async def margin_per_share(
        self, symbol: str, price: Decimal, *, now: dt.datetime
    ) -> Decimal | None: ...

    async def correlations(
        self, symbol: str, against: Sequence[str], *, now: dt.datetime
    ) -> Mapping[str, Decimal]: ...


class NoProducerYet:
    """Every live input that nothing in this codebase produces yet: unknown.

    Not a placeholder that approves. Each answer is the one the risk engine
    refuses on, so wiring this means every signal is rejected - with the
    missing input named in the audit row - until the producer exists:

    * ``unhealthy_services`` - the heartbeat roll-up is E20-S05.
    * ``atr`` - nothing publishes live indicator state; the strategy runtime
      computes ATR in-process and does not export it.
    * ``margin_per_share`` - the adapter has no ``order_margins`` call.
    * ``correlations`` - empty, so every held symbol is "unknown" and the
      correlation guard refuses a second position.
    """

    async def unhealthy_services(self, *, now: dt.datetime) -> tuple[str, ...] | None:
        return None

    async def atr(self, symbol: str, *, now: dt.datetime) -> Decimal | None:
        return None

    async def margin_per_share(
        self, symbol: str, price: Decimal, *, now: dt.datetime
    ) -> Decimal | None:
        return None

    async def correlations(
        self, symbol: str, against: Sequence[str], *, now: dt.datetime
    ) -> Mapping[str, Decimal]:
        return {}


class HaltReader(Protocol):
    async def state(self) -> Any: ...


class MarginSource(Protocol):
    async def fetch_margins(self) -> Any: ...


class Book(Protocol):
    def open_positions(self) -> Iterable[Any]: ...


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BuiltContext:
    """The context, and the facts about the symbol the entry plan needs.

    ``is_cas_stock`` is ``None`` when the day's row is missing. The context's
    deadline then assumes CAS - the EARLIER square-off, so the guess can only
    refuse more - and the eligibility check refuses anyway, because the same
    missing row leaves ``symbol_restrictions`` unknown. An approved entry with
    an unknown CAS flag is therefore not expected; the service refuses one
    rather than guess the flag a position's time exit is computed from.
    """

    context: RiskContext
    is_cas_stock: bool | None


class RiskContextBuilder:
    """Reads the session and builds the :class:`RiskContext` for one signal."""

    def __init__(
        self,
        *,
        capital: Decimal,
        slots_total: int,
        exit_buffer_minutes: int,
        calendar: Any,
        halts: HaltReader,
        margins: MarginSource,
        store: ContextStore,
        book: Book,
        pending: PendingEntries,
        facts: SessionFacts,
    ) -> None:
        if not callable(getattr(calendar, "squareoff_deadline", None)):
            raise TypeError("RiskContextBuilder needs a calendar with squareoff_deadline()")
        self._capital = capital
        self._slots_total = slots_total
        self._buffer = exit_buffer_minutes
        self._calendar = calendar
        self._halts = halts
        self._margins = margins
        self._store = store
        self._book = book
        self._pending = pending
        self._facts = facts

    async def build(
        self, rec: Recommendation, *, now: dt.datetime, trade_date: dt.date
    ) -> BuiltContext:
        """The context for ``rec`` at ``now``. Raises ContextUnavailableError."""
        self._pending.prune()
        symbol = rec.symbol

        profile = await self._required(
            f"the instrument record for {symbol}", self._store.profile(symbol)
        )
        if profile is None:
            raise ContextUnavailableError(
                f"{one_safe_line(symbol)} is not in the instrument master; an instrument "
                f"this system has no record of is not one it trades"
            )
        status = await self._optional(
            f"the {trade_date} surveillance flags for {symbol}",
            self._store.daily_status(symbol, trade_date),
        )
        is_cas = status.get("is_cas_stock") if status is not None else None
        if is_cas is not None and not isinstance(is_cas, bool):
            is_cas = None
        deadline = self._calendar.squareoff_deadline(
            trade_date,
            is_cas_stock=True if is_cas is None else is_cas,
            buffer_minutes=self._buffer,
        )

        occupied = await self._required("the occupied slots", self._store.occupied_slots())
        occupied = set(occupied) | self._pending.slots()

        halts = await self._halts.state()  # HaltController fails closed on its own
        open_positions = await self._open_positions()
        realised, streak = (None, None)
        closed = await self._optional(
            "today's closed trades", self._store.closed_since(session_start(trade_date))
        )
        if closed is not None:
            realised, streak = session_losses(closed)

        margin = await self._optional("live broker margin", self._margins.fetch_margins())
        held = sorted({p.symbol for p in open_positions} - {symbol})
        correlations = await self._optional(
            f"correlations for {symbol}", self._facts.correlations(symbol, held, now=now)
        )

        try:
            context = RiskContext(
                now=now,
                squareoff_deadline=deadline,
                capital=self._capital,
                available_margin=(margin.available_margin if margin is not None else None),
                kill_switch_active=halts.kill_switch,
                unhealthy_services=await self._optional(
                    "service health", self._facts.unhealthy_services(now=now)
                ),
                realised_pnl_today=realised,
                consecutive_losses=streak,
                daily_loss_halted=halts.daily_loss,
                consecutive_loss_halted=halts.consecutive_loss,
                open_positions=tuple(open_positions),
                slots_total=self._slots_total,
                slots_used=len(occupied),
                symbol_restrictions=restrictions_from(status),
                symbol_sector=profile.get("sector"),
                atr=await self._optional(f"ATR for {symbol}", self._facts.atr(symbol, now=now)),
                lot_size=int(profile.get("lot_size", 0)),
                margin_per_share=await self._optional(
                    f"per-share margin for {symbol}",
                    self._facts.margin_per_share(symbol, rec.trigger_price, now=now),
                ),
                correlations=correlations or {},
            )
        except RiskContextError as exc:
            raise ContextUnavailableError(f"the risk context is inconsistent: {exc}") from exc
        return BuiltContext(context=context, is_cas_stock=is_cas)

    async def _open_positions(self) -> list[OpenPosition]:
        """The book and the entries in flight, each with its sector.

        The sector is looked up per symbol; one that cannot be read is
        ``None``, which the sector check and the sizer both refuse on.
        """
        out: list[OpenPosition] = []
        sectors: dict[str, str | None] = {}

        async def sector_of(symbol: str) -> str | None:
            if symbol not in sectors:
                profile = await self._optional(
                    f"the sector of {symbol}", self._store.profile(symbol)
                )
                sectors[symbol] = profile.get("sector") if profile else None
            return sectors[symbol]

        for tracked in self._book.open_positions():
            position = tracked.position
            out.append(
                OpenPosition(
                    symbol=position.symbol,
                    direction=position.direction,
                    quantity=position.quantity,
                    entry_price=position.entry_price,
                    stop_price=position.stop_price,
                    sector=await sector_of(position.symbol),
                )
            )
        for entry in self._pending.entries():
            out.append(
                OpenPosition(
                    symbol=entry.symbol,
                    direction=entry.direction,
                    quantity=entry.sizing.quantity,
                    entry_price=entry.sizing.entry_price,
                    stop_price=entry.sizing.stop_price,
                    sector=await sector_of(entry.symbol),
                )
            )
        return out

    @staticmethod
    async def _required(what: str, pending: Awaitable[T]) -> T:
        try:
            return await pending
        except Exception as exc:
            # The TYPE in the message, the text in the (redacted) log: the
            # message becomes an audit detail, and decision_log is a sink the
            # redaction never sees - a driver error can name a DSN (REC-003).
            log.warning("%s could not be read: %s", what, one_safe_line(str(exc)))
            raise ContextUnavailableError(
                f"{what} could not be read ({type(exc).__name__})"
            ) from exc

    @staticmethod
    async def _optional(what: str, pending: Awaitable[T]) -> T | None:
        """A read whose failure the risk engine refuses on, as ``None``."""
        try:
            return await pending
        except Exception as exc:
            log.warning(
                "%s could not be read (%s: %s); the risk engine will refuse on it",
                what,
                type(exc).__name__,
                one_safe_line(str(exc)),
            )
            return None
