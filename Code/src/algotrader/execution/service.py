"""The execution service: from a signal to a protected position (E15-S13).

E15's stories each delivered one mechanism and left its caller for later: the
gateway sends an entry (S01-S03), the protective stop attaches (S04), the book
opens a position from a fill (S05), reconciliation watches everything (S09) and
books fills against their plans (S12). This module is the caller. Until it
existed nothing registered an entry's plan as the entry was sent, so every fill
would have been exited as unprotected.

## The design record

**The decision.** One object consumes Recommendations, evaluates each with the
production risk engine, and - for an approval - claims a slot, registers the
entry's plan and its pending slot, and only then sends the entry. The same
object runs the reconciliation loop, and both hold **one asyncio lock**, so a
reconciliation cycle never runs in the middle of a signal and a signal never
runs in the middle of a cycle.

**The alternative rejected.** Two independent loops sharing the book, the
plans and the slot allocator, coordinated by care. ``LOW_LEVEL_ARCHITECTURE.md
§9.1``: *"execution-svc is single-threaded, strictly serialized ... concurrency
here buys nothing and risks double-entry races"*. On asyncio, "single-threaded"
is not "serialized": every ``await`` is a point where the other loop runs. A
cycle that ran between the broker accepting a MARKET entry and the plan being
registered would see a fill with no plan and exit it - E15-S12's constraint 1,
which a lock makes structural rather than a matter of statement order.

**The failure it prevents.** A filled entry exited as unprotected (plan
registered too late); two entries sharing one slot (the slot was free in the
database while the first was in flight); a restarted service exiting
everything it held (a cycle run on an empty book).

**What would make this wrong.** Signal latency. Holding the lock across a
broker call means a cycle waits for a submission and a signal waits for a
cycle, both bounded by a broker round trip. If entries ever needed to be faster
than a reconciliation cycle, the lock would have to shrink to the plan
registration and the slot bookkeeping, and the cycle would have to tolerate
the rest.

## What the service refuses before the risk engine is asked

Each of these is audited under stage ``EXECUTION`` with its reason:

* **Not reconciled since start** - the book has not been checked against the
  broker once since this process started (AC3's companion: restored positions
  are unverified, and trading on top of them is trading blind).
* **A mode that places no orders** - ``alert_only`` never trades and
  ``approval`` needs a human confirmation flow that does not exist yet, so
  both refuse rather than send.
* **AI veto** - none of the fourteen checks reads the verdict; a VETO that
  reached this stream must still not trade.
* **Stale or future-dated** - the entry is MARKET and sized from the trigger
  price, so a signal acted on late is sized for a price that has moved.
* **A duplicate** - redelivery is normal on an at-least-once stream. If this
  signal's entry already exists, re-evaluating it could re-size an order
  already sent; it is refused, and if that entry fills without a plan in this
  process, reconciliation exits it rather than booking it on a guess.
* **A context that cannot be built** - see ``session_context``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Protocol

from pydantic import ValidationError

from algotrader.broker.adapter import OrderRejectedError
from algotrader.common.audit import AuditEntry
from algotrader.common.calendar import IST
from algotrader.common.db.models import DecisionLog
from algotrader.common.enums import (
    AIVerdict,
    Direction,
    OrderIntent,
    RejectReason,
    Side,
    SystemMode,
)
from algotrader.common.events import stream as streams
from algotrader.common.metrics import get_metrics
from algotrader.common.models.trading import OrderRequest, Recommendation, RiskDecision
from algotrader.common.redis import keys
from algotrader.common.text import one_safe_line
from algotrader.execution.gateway import RateLimitedError, client_order_id
from algotrader.execution.positions import EntryPlan
from algotrader.execution.reconciliation import CycleReport, Reconciler, ReconciliationLoop
from algotrader.execution.risk.factory import build_risk_engine
from algotrader.execution.session_context import (
    ContextStore,
    ContextUnavailableError,
    MarginSource,
    PendingAwareSlots,
    PendingEntries,
    PendingEntry,
    RiskContextBuilder,
    SessionFacts,
)
from algotrader.execution.slots import SlotManager

if TYPE_CHECKING:
    import redis.asyncio as aioredis

    from algotrader.common.calendar import MarketCalendar
    from algotrader.common.config import AppConfig
    from algotrader.common.events.stream import Delivery
    from algotrader.execution.positions import PositionManager

log = logging.getLogger(__name__)

__all__ = [
    "ExecutionService",
    "Outcome",
    "RedisSignalSource",
    "SignalResult",
]

#: Every row this service writes to ``decision_log`` carries this stage. The
#: risk engine writes its own rows under the stopping check's id.
STAGE: Final = "EXECUTION"
SERVICE: Final = "execution-svc"
#: The consumer group on ``stream:signals``.
GROUP: Final = "execution-svc"
#: How far in the FUTURE a signal's timestamp may be. The producer and this
#: service share a host clock, so anything beyond this is not skew.
MAX_CLOCK_SKEW: Final = dt.timedelta(seconds=5)
#: The modes in which this service sends orders. PAPER relies on the process
#: entrypoint handing the gateway a paper placer - see the story's notes.
TRADING_MODES: Final = frozenset({SystemMode.PAPER, SystemMode.LIVE})


class Outcome(StrEnum):
    """What happened to one signal. Each value is an audit ``outcome``."""

    #: Sent; its plan is registered and reconciliation will book the fill.
    SUBMITTED = "SUBMITTED"
    #: Sent, and the outcome is unknown. The plan is KEPT, so a fill that
    #: does land is booked; reconciliation resolves the order either way.
    UNKNOWN = "UNKNOWN"
    #: The risk engine said no (it audits that itself).
    REJECTED = "REJECTED"
    #: Refused before the engine was asked.
    REFUSED = "REFUSED"
    #: This signal's entry already exists.
    DUPLICATE = "DUPLICATE"
    #: Approved, but no slot could be claimed.
    NO_SLOT = "NO_SLOT"
    #: Definitively not in flight: refused by a limiter or the broker, or never
    #: recorded. The plan and the pending slot are released.
    SEND_FAILED = "SEND_FAILED"


def _assert_fits_audit_columns() -> None:
    """Refuse to load if a label this module writes would not fit the table.

    The reconciler's lesson, applied here: ``decision_log.outcome`` is
    String(12), and an over-long label fails the insert at the moment the
    event happens.
    """
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


@dataclass(frozen=True)
class SignalResult:
    """The outcome for one signal, and enough to explain it."""

    outcome: Outcome
    detail: str
    client_order_id: str | None = None
    reason: RejectReason | None = None
    slot_index: int | None = None
    broker_order_id: str | None = None


# ---------------------------------------------------------------------------
# The seams
# ---------------------------------------------------------------------------


class EntryGateway(Protocol):
    def build_entry(
        self, decision: RiskDecision, recommendation: Recommendation, *, trade_date: dt.date
    ) -> OrderRequest: ...

    async def submit(self, request: OrderRequest) -> str: ...


class OrderLookup(Protocol):
    async def find_by_client_order_id(self, client_order_id: str) -> dict[str, Any] | None: ...


class HaltSource(Protocol):
    async def state(self) -> Any: ...

    async def arm(self, reason: Any, *, detail: str, armed_by: str, now: dt.datetime) -> object: ...


class SlotClaimer(Protocol):
    def claiming(self, symbol: str) -> AbstractAsyncContextManager[int | None]: ...


AuditSink = Callable[[AuditEntry], Awaitable[object]]


class _EngineAudit:
    """Collects the risk engine's audit rows so they can be written async.

    ``RiskEngine.audit`` is synchronous - the engine is pure and does no I/O -
    and ``AuditWriter.write`` is a coroutine. The engine appends here; the
    service writes what was appended as soon as ``evaluate`` returns.
    """

    def __init__(self) -> None:
        self._rows: list[dict[str, object]] = []

    def __call__(self, row: dict[str, object]) -> None:
        self._rows.append(row)

    def drain(self) -> list[dict[str, object]]:
        rows, self._rows = self._rows, []
        return rows


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class ExecutionService:
    """Signals in, protected positions out, the book watched throughout."""

    def __init__(
        self,
        *,
        config: AppConfig,
        calendar: MarketCalendar,
        gateway: EntryGateway,
        manager: PositionManager,
        reconciler: Reconciler,
        halts: HaltSource,
        margins: MarginSource,
        store: ContextStore,
        orders: OrderLookup,
        redis: aioredis.Redis,
        facts: SessionFacts,
        clock: Callable[[], dt.datetime],
        audit: AuditSink | None = None,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
        reconcile_interval_seconds: float = 30.0,
    ) -> None:
        self._mode = config.system.mode
        self._max_age = dt.timedelta(seconds=config.execution.max_signal_age_seconds)
        self._calendar = calendar
        self._gateway = gateway
        self._manager = manager
        self._orders = orders
        self._clock = clock
        self._audit = audit
        self._sleep = sleep
        self._interval = reconcile_interval_seconds

        #: The lock §9.1 asks for. Held by every signal and every cycle.
        self._guard = asyncio.Lock()
        #: Built HERE, through the one factory, and never accepted from a
        #: caller: a service handed ``RiskEngine(checks=[])`` would approve
        #: everything (QA-VER-01).
        self._engine_audit = _EngineAudit()
        self._engine = build_risk_engine(config, calendar=calendar, audit=self._engine_audit)
        self._pending = PendingEntries(manager)
        self._slots: SlotClaimer = SlotManager(
            redis,
            PendingAwareSlots(store, self._pending),
            total_slots=config.risk.position_slots,
        )
        self._contexts = RiskContextBuilder(
            capital=config.risk.capital,
            slots_total=config.risk.position_slots,
            exit_buffer_minutes=config.risk.exit_buffer_minutes,
            calendar=calendar,
            halts=halts,
            margins=margins,
            store=store,
            book=manager,
            pending=self._pending,
            facts=facts,
        )
        #: Built here so that it can only ever hold THIS service's lock.
        self._loop = ReconciliationLoop(
            reconciler,
            calendar=calendar,
            halter=halts,
            clock=clock,
            interval_seconds=reconcile_interval_seconds,
            sleep=sleep,
            guard=self._guard,
        )
        self._restored = False
        self._reconciled = False

    # -- reading ------------------------------------------------------------

    @property
    def check_ids(self) -> tuple[str, ...]:
        return self._engine.check_ids

    @property
    def pending(self) -> PendingEntries:
        return self._pending

    @property
    def ready(self) -> bool:
        """Restored, and reconciled against the broker at least once."""
        return self._restored and self._reconciled

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> CycleReport | None:
        """Restore the book, THEN reconcile it. AC3, and S12's constraint 2.

        A cycle run on an empty book finds every held position unbooked and
        exits it. Restoring first means every row the database holds is in
        the book - as UNVERIFIED, which is what it is - before the broker is
        asked about any of it.

        A restore that fails raises: a service that cannot read its own book
        does not start. A first cycle that fails does not: the loop counts it,
        three in a row arm BROKER_DISCONNECTED, and signals are refused until
        a cycle succeeds (``ready``).
        """
        async with self._guard:
            restored = await self._manager.restore()
            self._restored = True
        log.info("execution service restored %d position(s); reconciling", len(restored))
        return await self.on_reconnect()

    async def on_reconnect(self) -> CycleReport | None:
        """One cycle now, whatever the clock says (the loop's own rule)."""
        report = await self._loop.on_reconnect()
        if report is not None:
            self._reconciled = True
        return report

    async def tick(self) -> CycleReport | None:
        """One scheduled reconciliation iteration (nothing outside hours)."""
        report = await self._loop.tick()
        if report is not None:
            self._reconciled = True
        return report

    async def run(self, source: SignalSource, stop: asyncio.Event) -> None:
        """Start, then consume signals and reconcile until ``stop`` is set.

        A TaskGroup, so that either half failing stops the other and the
        process exits: a service consuming signals while its reconciliation
        has died would trade on a book nothing is watching.
        """
        await self.start()
        async with asyncio.TaskGroup() as group:
            group.create_task(self._reconcile_until(stop))
            group.create_task(self._consume_until(source, stop))

    async def _reconcile_until(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.tick()
            await self._sleep(self._interval)

    async def _consume_until(self, source: SignalSource, stop: asyncio.Event) -> None:
        await source.start()
        while not stop.is_set():
            for delivery in await source.next_batch():
                await self.deliver(source, delivery)

    async def deliver(self, source: SignalSource, delivery: Delivery) -> SignalResult | None:
        """Handle one stream entry, then acknowledge it - or dead-letter it.

        A payload that is not a Recommendation is dead-lettered at once:
        retrying a malformed message cannot fix it. So is one whose envelope
        and payload name different correlation ids - a message that disagrees
        with itself about which trade it is.
        """
        try:
            rec = Recommendation.model_validate(delivery.envelope.payload)
        except ValidationError as exc:
            await source.poison(
                delivery, f"not a Recommendation ({exc.error_count()} validation error(s))"
            )
            return None
        if rec.correlation_id != delivery.envelope.correlation_id:
            await source.poison(
                delivery, "the envelope and the payload carry different correlation ids"
            )
            return None
        try:
            result = await self.handle(rec)
        except Exception as exc:
            # handle() returns an outcome for everything it anticipates. An
            # exception here is a defect; retrying it would repeat it, and
            # dead-lettering it alerts. Its plan, if one was registered, stays
            # - a fill is then booked rather than exited.
            log.exception("handling %s raised; dead-lettering it", rec.symbol)
            await source.poison(delivery, f"handling raised {type(exc).__name__}")
            return None
        await source.done(delivery)
        return result

    # -- one signal ---------------------------------------------------------

    async def handle(self, rec: Recommendation) -> SignalResult:
        """Take one Recommendation as far as it should go. Serialized."""
        async with self._guard:
            now = self._clock()
            result = await self._handle(rec, now)
        if result.outcome is not Outcome.REJECTED:
            # The engine audits and counts its own rejections; these are the rest.
            await self._record(rec, result, now)
            if result.reason is not None:
                get_metrics().rejected(check=STAGE.lower(), reason=result.reason.value)
        log.info(
            "signal %s %s -> %s: %s",
            rec.symbol,
            rec.direction.value,
            result.outcome.value,
            result.detail,
        )
        return result

    async def _handle(self, rec: Recommendation, now: dt.datetime) -> SignalResult:
        trade_date = now.astimezone(IST).date()
        side = Side.BUY if rec.direction is Direction.LONG else Side.SELL
        cid = client_order_id(
            correlation_id=rec.correlation_id,
            symbol=rec.symbol,
            side=side,
            intent=OrderIntent.ENTRY,
            trade_date=trade_date,
        )

        refusal = self._refusal(rec, now)
        if refusal is not None:
            return refusal

        if cid in self._pending or await self._already_recorded(cid):
            return SignalResult(
                Outcome.DUPLICATE,
                f"the entry for correlation {rec.correlation_id} already exists ({cid}); "
                f"it is not evaluated or sent again",
                client_order_id=cid,
            )

        try:
            built = await self._contexts.build(rec, now=now, trade_date=trade_date)
        except ContextUnavailableError as exc:
            return SignalResult(Outcome.REFUSED, str(exc), reason=RejectReason.RISK_ENGINE_FAULT)

        decision = self._engine.evaluate(rec, built.context)
        await self._write_engine_audit()
        if not decision.approved:
            return SignalResult(
                Outcome.REJECTED,
                decision.detail or "rejected",
                reason=decision.reason,
                client_order_id=cid,
            )
        if built.is_cas_stock is None:
            # BuiltContext explains why this is not expected. Refused rather
            # than guessed: the CAS flag sets this position's time exit.
            return SignalResult(
                Outcome.REFUSED,
                f"approved, but the CAS flag for {rec.symbol} on {trade_date} is unknown, "
                f"so the position's square-off deadline cannot be set",
                reason=RejectReason.SYMBOL_NOT_TRADABLE,
                client_order_id=cid,
            )
        return await self._send(rec, decision, cid, trade_date, is_cas=built.is_cas_stock)

    def _refusal(self, rec: Recommendation, now: dt.datetime) -> SignalResult | None:
        """The refusals that need nothing but the signal and the clock."""
        if not self.ready:
            return SignalResult(
                Outcome.REFUSED,
                "the book has not been restored and reconciled against the broker since "
                "this service started; no entry is sent on an unverified book",
                reason=RejectReason.HEALTH_GATE_FAILED,
            )
        if self._mode not in TRADING_MODES:
            # No reason code: nothing is wrong and no limit was hit. Borrowing
            # one would put a working mode on the "why isn't it trading?"
            # dashboard as a fault (the SIT-001 conflation).
            return SignalResult(
                Outcome.REFUSED, f"system mode is {self._mode.value}, which places no orders"
            )
        if rec.ai_verdict is AIVerdict.VETO:
            return SignalResult(
                Outcome.REFUSED, "the AI review vetoed this signal", reason=RejectReason.AI_VETO
            )
        age = now - rec.emitted_at
        if age > self._max_age:
            return SignalResult(
                Outcome.REFUSED,
                f"the signal is {age.total_seconds():.0f}s old; the limit is "
                f"{self._max_age.total_seconds():.0f}s, and a MARKET entry sized from a "
                f"stale trigger price carries risk nobody approved",
                reason=RejectReason.STALE_DATA,
            )
        if -age > MAX_CLOCK_SKEW:
            return SignalResult(
                Outcome.REFUSED,
                f"the signal is dated {(-age).total_seconds():.0f}s in the future",
                reason=RejectReason.STALE_DATA,
            )
        return None

    async def _send(
        self,
        rec: Recommendation,
        decision: RiskDecision,
        cid: str,
        trade_date: dt.date,
        *,
        is_cas: bool,
    ) -> SignalResult:
        """Claim the slot, register the plan, THEN send. In that order."""
        sizing = decision.sizing
        assert sizing is not None  # approved decisions carry sizing - RiskDecision enforces it
        request = self._gateway.build_entry(decision, rec, trade_date=trade_date)
        if request.client_order_id != cid:
            # The duplicate check above used `cid`. If the gateway derived a
            # different key, that check guarded nothing.
            return SignalResult(
                Outcome.REFUSED,
                f"the gateway's key {request.client_order_id} differs from {cid}",
                reason=RejectReason.RISK_ENGINE_FAULT,
                client_order_id=cid,
            )

        async with self._slots.claiming(rec.symbol) as index:
            if index is None:
                return SignalResult(
                    Outcome.NO_SLOT,
                    "approved, but no slot could be claimed",
                    reason=RejectReason.NO_SLOT_AVAILABLE,
                    client_order_id=cid,
                )
            # BEFORE the broker call (S12 constraint 1). A MARKET entry can fill
            # before submit() returns; the plan must already be waiting.
            self._manager.expect_fill(
                cid,
                EntryPlan(
                    sizing=sizing,
                    slot_index=index,
                    is_cas_stock=is_cas,
                    strategy_id=rec.strategy_id,
                ),
            )
            self._pending.add(
                PendingEntry(
                    client_order_id=cid,
                    symbol=rec.symbol,
                    direction=rec.direction,
                    sizing=sizing,
                    slot_index=index,
                )
            )
            try:
                broker_order_id = await self._gateway.submit(request)
            except (RateLimitedError, OrderRejectedError) as exc:
                self._release(cid)
                log.warning("entry %s was not sent: %s", cid, one_safe_line(str(exc)))
                return SignalResult(
                    Outcome.SEND_FAILED,
                    f"not sent: refused ({type(exc).__name__})",
                    client_order_id=cid,
                    slot_index=index,
                )
            except Exception as exc:
                return await self._unresolved(cid, index, exc)
        return SignalResult(
            Outcome.SUBMITTED,
            f"{sizing.quantity} x {rec.symbol} sent as {broker_order_id}; slot {index}",
            client_order_id=cid,
            slot_index=index,
            broker_order_id=broker_order_id,
        )

    async def _unresolved(self, cid: str, index: int, exc: Exception) -> SignalResult:
        """A send that ended in something other than a known refusal.

        The gateway records an order BEFORE calling the broker and never calls
        it if that record fails. So no row means nothing was sent, and the
        plan and slot are released. A row means it may be in flight: the plan
        is kept, so a fill is booked, and reconciliation resolves the row -
        writing it SUBMIT_FAILED, and releasing the plan, if it never lands.
        A lookup that itself fails keeps the plan: a held slot costs capacity,
        a released one could cost a second position in it.

        The detail names the exception's TYPE, never its text (REC-002/003):
        it reaches decision_log, a sink the logging layer's redaction never
        sees, and a broker's error text can echo what was sent. The text goes
        to the log line below, which is redacted.
        """
        log.error("sending entry %s ended unresolved: %s", cid, one_safe_line(str(exc)))
        try:
            recorded = await self._orders.find_by_client_order_id(cid)
        except Exception:
            recorded = {"status": "unknown"}
        detail = type(exc).__name__
        if recorded is None:
            self._release(cid)
            return SignalResult(
                Outcome.SEND_FAILED,
                f"never recorded, so never sent ({detail})",
                client_order_id=cid,
                slot_index=index,
            )
        return SignalResult(
            Outcome.UNKNOWN,
            f"outcome unknown; reconciliation owns it ({detail})",
            client_order_id=cid,
            slot_index=index,
        )

    def _release(self, cid: str) -> None:
        # The pending entry follows: PendingEntries prunes from the plans on
        # every read, so the plan is the one record of "still in flight".
        self._manager.forget(cid)

    async def _already_recorded(self, cid: str) -> bool:
        """Is there already an order row for this entry? Unknown counts as yes.

        Refusing a signal because the ledger could not be read costs one
        trade; re-evaluating one whose entry may already be at the broker
        could re-size an order that was already sent.
        """
        try:
            return await self._orders.find_by_client_order_id(cid) is not None
        except Exception:
            log.warning("could not check for an existing entry %s; refusing", cid, exc_info=True)
            return True

    # -- audit --------------------------------------------------------------

    async def _write_engine_audit(self) -> None:
        for row in self._engine_audit.drain():
            await self._write(AuditEntry(**row))  # type: ignore[arg-type]

    async def _record(self, rec: Recommendation, result: SignalResult, now: dt.datetime) -> None:
        await self._write(
            AuditEntry(
                correlation_id=rec.correlation_id,
                stage=STAGE,
                outcome=result.outcome.value,
                service=SERVICE,
                reason_code=result.reason.value if result.reason else None,
                payload={
                    "symbol": rec.symbol,
                    "strategy_id": rec.strategy_id,
                    "direction": rec.direction.value,
                    "detail": one_safe_line(result.detail),
                    "client_order_id": result.client_order_id,
                    "slot_index": result.slot_index,
                    "broker_order_id": result.broker_order_id,
                },
                ts=now,
            )
        )

    async def _write(self, entry: AuditEntry) -> None:
        """A failed audit write never undoes a decision; it is logged loudly."""
        if self._audit is None:
            return
        try:
            await self._audit(entry)
        except Exception:
            log.exception("could not write the %s audit row for %s", entry.stage, entry.outcome)


# ---------------------------------------------------------------------------
# The signal stream
# ---------------------------------------------------------------------------


class SignalSource(Protocol):
    async def start(self) -> None: ...

    async def next_batch(self) -> list[Delivery]: ...

    async def done(self, delivery: Delivery) -> None: ...

    async def poison(self, delivery: Delivery, reason: str) -> None: ...


class RedisSignalSource:
    """``stream:signals`` through the ``execution-svc`` consumer group.

    At-least-once: an entry is acknowledged only after its outcome is
    recorded, so a crash mid-signal redelivers it - and the duplicate check
    is what makes the redelivery harmless.
    """

    def __init__(
        self,
        client: aioredis.Redis,
        *,
        consumer: str | None = None,
        block_ms: int = 1_000,
        count: int = 10,
    ) -> None:
        self._client = client
        self._stream = keys.stream_signals()
        self._consumer = consumer or streams.new_consumer_name(GROUP)
        self._block_ms = block_ms
        self._count = count

    async def start(self) -> None:
        await streams.ensure_group(self._client, self._stream, GROUP)

    async def next_batch(self) -> list[Delivery]:
        return await streams.consume(
            self._client,
            self._stream,
            GROUP,
            self._consumer,
            count=self._count,
            block_ms=self._block_ms,
        )

    async def done(self, delivery: Delivery) -> None:
        await streams.acknowledge(self._client, self._stream, GROUP, delivery.entry_id)

    async def poison(self, delivery: Delivery, reason: str) -> None:
        await streams.dead_letter(self._client, self._stream, GROUP, delivery, reason)
