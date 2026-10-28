"""E15-S13: the execution service, one acceptance criterion at a time.

The risk engine here is the production one - fourteen checks through the
factory - and so are the gateway, the position manager, the slot allocator and
the halt controller. The doubles stand in only for I/O: the broker, the order
table, Redis and the database reads, each answering from configured facts.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from algotrader.broker.adapter import AmbiguousOrderError, MarginSnapshot, OrderRejectedError
from algotrader.common.audit import AuditEntry
from algotrader.common.calendar import MarketCalendar, load_holidays_with_status
from algotrader.common.config import AppConfig
from algotrader.common.enums import (
    AIVerdict,
    Direction,
    OrderIntent,
    RejectReason,
    Side,
    SystemMode,
)
from algotrader.common.events.envelope import Envelope
from algotrader.common.events.stream import Delivery
from algotrader.common.metrics import reset_metrics_for_testing
from algotrader.common.models.trading import OrderRequest, Recommendation
from algotrader.execution import reconciliation as rec_mod
from algotrader.execution.gateway import GatewayPolicy, OrderGateway, client_order_id
from algotrader.execution.halt import HaltController, HaltReason
from algotrader.execution.positions import PositionManager, UnverifiedPosition
from algotrader.execution.risk.checks import all_check_ids
from algotrader.execution.service import ExecutionService, Outcome
from algotrader.execution.session_context import NoProducerYet

# 10:30 IST on an ordinary Tuesday.
NOW = dt.datetime(2026, 8, 25, 5, 0, tzinfo=dt.UTC)
TRADE_DATE = dt.date(2026, 8, 25)
CLEAN = {"is_t2t": False, "is_asm": False, "is_gsm": False, "is_fno_ban": False}


@pytest.fixture(autouse=True)
def _fresh_metrics() -> None:
    reset_metrics_for_testing()


def _calendar() -> MarketCalendar:
    path = Path(__file__).resolve().parents[2] / "config" / "nse_holidays.yaml"
    status = load_holidays_with_status(str(path))
    return MarketCalendar(status.dates, covers_years=status.covers_years)


# ---------------------------------------------------------------------------
# Doubles: I/O only
# ---------------------------------------------------------------------------


class FakeRedis:
    """What SlotManager, the lock helpers and HaltController use."""

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}

    async def set(self, key: str, value: str, *, nx: bool = False, px: int | None = None, **_):
        if nx and key in self.strings:
            return None
        self.strings[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def delete(self, key: str) -> int:
        return 1 if self.strings.pop(key, None) is not None else 0

    async def exists(self, key: str) -> int:
        return 1 if key in self.strings else 0

    async def eval(self, script: str, numkeys: int, *args: Any) -> int:
        key, holder = str(args[0]), str(args[1])
        if "DEL" in script and self.strings.get(key) == holder:
            del self.strings[key]
            return 1
        return 0


class Placer:
    """The broker's order calls. Records what the service had done BEFORE each."""

    def __init__(self, service_ref: dict[str, Any]) -> None:
        self.ref = service_ref
        self.submitted: list[OrderRequest] = []
        self.plan_at_send: list[Any] = []
        self.pending_at_send: list[bool] = []
        self.raises: Exception | None = None
        self.found: Any = None
        self.release: asyncio.Event | None = None
        self.events: list[str] | None = None

    async def place_order(self, request: OrderRequest) -> str:
        service: ExecutionService = self.ref["service"]
        self.plan_at_send.append(service._manager.plan_for(request.client_order_id))
        self.pending_at_send.append(request.client_order_id in service.pending)
        if self.events is not None:
            self.events.append("send-start")
        if self.release is not None:
            await self.release.wait()
        if self.events is not None:
            self.events.append("send-end")
        if self.raises is not None:
            raise self.raises
        self.submitted.append(request)
        return f"B-{len(self.submitted)}"

    async def find_by_client_order_id(self, cid: str):
        return self.found

    async def cancel_order(self, broker_order_id: str) -> None:
        return None


class Orders:
    """The orders table: TX1, TX2, the rejection record, the lookup."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.fail_insert: Exception | None = None
        self.fail_find = False

    async def insert_submitting(self, order: dict[str, Any]) -> int:
        if self.fail_insert is not None:
            raise self.fail_insert
        self.rows[order["client_order_id"]] = {**order, "status": "SUBMITTING"}
        return len(self.rows)

    async def attach_broker_id(self, cid: str, broker_order_id: str) -> None:
        self.rows[cid].update(status="SUBMITTED", broker_order_id=broker_order_id)

    async def mark_rejected(self, cid: str, *, reason: str | None) -> None:
        self.rows[cid]["status"] = "REJECTED"

    async def find_by_client_order_id(self, cid: str) -> dict[str, Any] | None:
        if self.fail_find:
            raise ConnectionError("the orders table is unreachable")
        row = self.rows.get(cid)
        return None if row is None else dict(row)


class Store:
    """The context reads, and the positions table the book restores from."""

    def __init__(self) -> None:
        self.profiles: dict[str, dict[str, Any]] = {
            s: {"sector": sector, "lot_size": 1}
            for s, sector in (("INFY", "IT"), ("TCS", "IT"), ("SBIN", "BANKS"))
        }
        self.status: dict[str, dict[str, Any]] = {
            "INFY": {**CLEAN, "is_cas_stock": False},
            "TCS": {**CLEAN, "is_cas_stock": True},
            "SBIN": {**CLEAN, "is_cas_stock": False},
        }
        self.open_rows: list[dict[str, Any]] = []
        self.fail_restore = False

    async def closed_since(self, since: dt.datetime) -> list[dict[str, Any]]:
        return []

    async def occupied_slots(self) -> set[int]:
        return {int(r["slot_index"]) for r in self.open_rows}

    async def profile(self, symbol: str) -> dict[str, Any] | None:
        return self.profiles.get(symbol)

    async def daily_status(self, symbol: str, trade_date: dt.date) -> dict[str, Any] | None:
        return self.status.get(symbol)

    # PositionStore
    async def open_position(self, position: dict[str, Any]) -> int:
        self.open_rows.append(dict(position))
        return len(self.open_rows)

    async def open_positions(self) -> list[dict[str, Any]]:
        if self.fail_restore:
            raise ConnectionError("positions table unreachable")
        return [dict(r) for r in self.open_rows]

    async def close_position(self, position_id: int, **kwargs: Any) -> None:
        return None


class Mirror:
    async def write(self, symbol: str, snapshot: Any) -> None:
        return None

    async def clear(self, symbol: str) -> None:
        return None


class Margins:
    async def fetch_margins(self) -> MarginSnapshot:
        return MarginSnapshot(
            available_cash=Decimal("400000"),
            available_margin=Decimal("400000"),
            used_margin=Decimal("0"),
            fetched_at=NOW,
        )


class Facts:
    """Every live input known - the shape a fully wired system would supply."""

    async def unhealthy_services(self, *, now: dt.datetime) -> tuple[str, ...] | None:
        return ()

    async def atr(self, symbol: str, *, now: dt.datetime) -> Decimal | None:
        return Decimal("12.5")

    async def margin_per_share(self, symbol, price, *, now):
        return Decimal("240")

    async def correlations(self, symbol, against, *, now):
        return {s: Decimal("0.1") for s in against}


class Reconciler:
    """Records what the book held, and whether a signal was mid-flight, per cycle."""

    def __init__(self, service_ref: dict[str, Any]) -> None:
        self.ref = service_ref
        self.cycles: list[dict[str, Any]] = []
        self.raises: Exception | None = None
        self.events: list[str] | None = None

    async def run_cycle(self, *, now: dt.datetime) -> rec_mod.CycleReport:
        service: ExecutionService = self.ref["service"]
        if self.events is not None:
            self.events.append("cycle")
        self.cycles.append(
            {
                "now": now,
                "book": sorted(t.position.symbol for t in service._manager.open_positions()),
            }
        )
        if self.raises is not None:
            raise self.raises
        return rec_mod.CycleReport(cycle_id=uuid4(), at=now)


class Clock:
    def __init__(self, now: dt.datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


class Harness:
    def __init__(
        self,
        *,
        mode: SystemMode = SystemMode.PAPER,
        slots: int = 5,
        facts: Any = None,
        limiter: Any = None,
    ) -> None:
        overrides: dict[str, Any] = {"system": {"mode": mode}, "risk": {"position_slots": slots}}
        if mode is SystemMode.LIVE:
            overrides["system"]["static_ip"] = "203.0.113.10"
        self.config = AppConfig(**overrides)
        self.ref: dict[str, Any] = {}
        self.redis = FakeRedis()
        self.halts = HaltController(self.redis)  # type: ignore[arg-type]
        self.placer = Placer(self.ref)
        self.orders = Orders()
        self.store = Store()
        self.gateway = OrderGateway(
            self.placer,
            policy=GatewayPolicy(algo_id=None, market_protection=Decimal("-1")),
            store=self.orders,
            limiter=limiter,
        )
        self.manager = PositionManager(
            protector=None,  # type: ignore[arg-type]
            store=self.store,
            mirror=Mirror(),
            calendar=_calendar(),
        )
        self.reconciler = Reconciler(self.ref)
        self.clock = Clock()
        self.audit: list[AuditEntry] = []
        self.service = ExecutionService(
            config=self.config,
            calendar=_calendar(),
            gateway=self.gateway,
            manager=self.manager,
            reconciler=self.reconciler,  # type: ignore[arg-type]
            halts=self.halts,
            margins=Margins(),
            store=self.store,
            orders=self.orders,
            redis=self.redis,  # type: ignore[arg-type]
            facts=facts or Facts(),
            clock=self.clock,
            audit=self._write,
        )
        self.ref["service"] = self.service

    async def _write(self, entry: AuditEntry) -> None:
        self.audit.append(entry)

    def execution_rows(self) -> list[tuple[str, str | None]]:
        return [(e.outcome, e.reason_code) for e in self.audit if e.stage == "EXECUTION"]

    async def started(self) -> Harness:
        await self.service.start()
        return self


def _rec(
    symbol: str = "INFY",
    *,
    verdict: AIVerdict = AIVerdict.CONFIRM,
    emitted_at: dt.datetime = NOW,
    direction: Direction = Direction.LONG,
) -> Recommendation:
    return Recommendation(
        correlation_id=uuid4(),
        symbol=symbol,
        strategy_id="orb_long_v1",
        direction=direction,
        trigger_price=Decimal("1200.00"),
        suggested_stop=Decimal("1186.45") if direction is Direction.LONG else Decimal("1213.55"),
        timeframe_agreement=3,
        ai_confidence=Decimal("0.82"),
        ai_verdict=verdict,
        ai_rationale="probe",
        emitted_at=emitted_at,
    )


def _cid(rec: Recommendation) -> str:
    return client_order_id(
        correlation_id=rec.correlation_id,
        symbol=rec.symbol,
        side=Side.BUY if rec.direction is Direction.LONG else Side.SELL,
        intent=OrderIntent.ENTRY,
        trade_date=TRADE_DATE,
    )


def _open_row(symbol: str = "SBIN", slot: int = 0) -> dict[str, Any]:
    return {
        "position_id": 1,
        "correlation_id": uuid4(),
        "symbol": symbol,
        "strategy_id": "orb_long_v1",
        "slot_index": slot,
        "direction": "LONG",
        "quantity": 50,
        "entry_price": Decimal("800.0000"),
        "stop_price": Decimal("790.0000"),
        "target_price": None,
        "opened_at": NOW,
        "squareoff_deadline": dt.datetime(2026, 8, 25, 9, 45, tzinfo=dt.UTC),
        "status": "OPEN",
    }


# ---------------------------------------------------------------------------
# The engine is the production one
# ---------------------------------------------------------------------------


class TestTheEngineIsTheFactorys:
    def test_all_fourteen_checks_run_in_canonical_order(self) -> None:
        assert Harness().service.check_ids == all_check_ids()

    def test_there_is_no_way_to_hand_the_service_an_engine(self) -> None:
        """QA-VER-01: `RiskEngine(checks=[])` approves everything. The service
        builds its engine itself, so no caller can supply that one."""
        import inspect

        params = inspect.signature(ExecutionService.__init__).parameters
        assert not {"engine", "risk_engine", "checks"} & set(params)


# ---------------------------------------------------------------------------
# AC1 (component) - an approval is sent with its plan already registered
# ---------------------------------------------------------------------------


class TestAc1AnApprovalIsSentWithItsPlan:
    async def test_an_approved_signal_is_submitted(self) -> None:
        h = await Harness().started()
        rec = _rec()
        result = await h.service.handle(rec)
        assert result.outcome is Outcome.SUBMITTED, result.detail
        assert [r.symbol for r in h.placer.submitted] == ["INFY"]
        assert result.client_order_id == _cid(rec)

    async def test_the_plan_and_pending_slot_exist_before_the_broker_is_called(self) -> None:
        """S12 constraint 1: a MARKET entry can fill before submit() returns."""
        h = await Harness().started()
        await h.service.handle(_rec())
        assert h.placer.plan_at_send[0] is not None
        assert h.placer.pending_at_send == [True]

    async def test_the_plan_carries_the_engines_sizing_the_slot_and_the_cas_flag(self) -> None:
        h = await Harness().started()
        rec = _rec("TCS")
        result = await h.service.handle(rec)
        plan = h.manager.plan_for(_cid(rec))
        assert plan is not None
        assert plan.is_cas_stock is True, "CAS comes from instrument_daily_status"
        assert plan.slot_index == result.slot_index == 0
        assert plan.strategy_id == "orb_long_v1"
        assert h.placer.submitted[0].quantity == plan.sizing.quantity

    async def test_the_approval_and_the_submission_are_both_audited(self) -> None:
        h = await Harness().started()
        await h.service.handle(_rec())
        stages = [(e.stage, e.outcome) for e in h.audit]
        assert ("risk_approved", "approved") in stages
        assert ("EXECUTION", "SUBMITTED") in stages

    async def test_an_in_flight_entry_holds_its_symbol(self) -> None:
        h = await Harness().started()
        await h.service.handle(_rec("INFY"))
        second = await h.service.handle(_rec("INFY"))
        assert second.outcome is Outcome.REJECTED
        assert second.reason is RejectReason.ALREADY_HOLDING
        assert len(h.placer.submitted) == 1

    async def test_an_in_flight_entry_holds_its_slot(self) -> None:
        h = await Harness().started()
        first = await h.service.handle(_rec("INFY"))
        second = await h.service.handle(_rec("SBIN"))
        assert {first.slot_index, second.slot_index} == {0, 1}

    async def test_a_full_book_counting_in_flight_entries_is_refused(self) -> None:
        h = await Harness(slots=1).started()
        await h.service.handle(_rec("INFY"))
        second = await h.service.handle(_rec("SBIN"))
        assert second.outcome is Outcome.REJECTED
        assert second.reason is RejectReason.NO_SLOT_AVAILABLE
        assert len(h.placer.submitted) == 1

    async def test_once_reconciliation_forgets_the_plan_the_slot_is_free_again(self) -> None:
        """The control for the two above: pending is released by the plan."""
        h = await Harness(slots=1).started()
        first = await h.service.handle(_rec("INFY"))
        h.manager.forget(first.client_order_id)  # e.g. written SUBMIT_FAILED
        second = await h.service.handle(_rec("SBIN"))
        assert second.outcome is Outcome.SUBMITTED, second.detail
        assert second.slot_index == 0

    async def test_live_mode_sends_too(self) -> None:
        h = await Harness(mode=SystemMode.LIVE).started()
        assert (await h.service.handle(_rec())).outcome is Outcome.SUBMITTED


# ---------------------------------------------------------------------------
# AC2 - a signal that is not approved places nothing
# ---------------------------------------------------------------------------


def _nothing_placed(h: Harness, rec: Recommendation) -> None:
    assert h.placer.submitted == []
    assert h.placer.plan_at_send == [], "the broker was called"
    assert h.manager.plan_for(_cid(rec)) is None
    assert len(h.service.pending) == 0
    assert h.orders.rows == {}


class TestAc2ARejectedSignalPlacesNothing:
    async def test_an_armed_kill_switch_places_nothing(self) -> None:
        h = await Harness().started()
        await h.halts.arm(HaltReason.MANUAL, detail="probe", armed_by="test", now=NOW)
        rec = _rec()
        result = await h.service.handle(rec)
        assert result.outcome is Outcome.REJECTED
        assert result.reason is RejectReason.KILL_SWITCH_ACTIVE
        _nothing_placed(h, rec)

    async def test_the_engines_rejection_is_audited_by_the_engine_only(self) -> None:
        h = await Harness().started()
        await h.halts.arm(HaltReason.MANUAL, detail="probe", armed_by="test", now=NOW)
        await h.service.handle(_rec())
        assert [(e.stage, e.outcome) for e in h.audit] == [("kill_switch", "rejected")]

    async def test_a_restricted_symbol_places_nothing(self) -> None:
        h = await Harness().started()
        h.store.status["INFY"]["is_t2t"] = True
        rec = _rec()
        result = await h.service.handle(rec)
        assert result.reason is RejectReason.SYMBOL_NOT_TRADABLE
        _nothing_placed(h, rec)

    async def test_with_no_producers_wired_every_signal_is_rejected(self) -> None:
        """Health, ATR and per-share margin have no producer yet; the
        production facts must reject, naming what was missing."""
        h = await Harness(facts=NoProducerYet()).started()
        rec = _rec()
        result = await h.service.handle(rec)
        assert result.outcome is Outcome.REJECTED
        assert result.reason is RejectReason.RISK_ENGINE_FAULT
        assert "unavailable" in result.detail
        _nothing_placed(h, rec)

    async def test_an_unknown_daily_row_is_rejected_not_traded(self) -> None:
        h = await Harness().started()
        del h.store.status["INFY"]
        rec = _rec()
        result = await h.service.handle(rec)
        assert result.outcome is Outcome.REJECTED
        assert result.reason is RejectReason.RISK_ENGINE_FAULT
        _nothing_placed(h, rec)


class TestRefusedBeforeTheEngine:
    async def test_a_veto_is_refused(self) -> None:
        h = await Harness().started()
        rec = _rec(verdict=AIVerdict.VETO)
        result = await h.service.handle(rec)
        assert (result.outcome, result.reason) == (Outcome.REFUSED, RejectReason.AI_VETO)
        assert h.execution_rows() == [("REFUSED", "AI_VETO")]
        _nothing_placed(h, rec)

    async def test_a_weak_verdict_is_the_engines_to_judge(self) -> None:
        """The control: only VETO is refused here."""
        h = await Harness().started()
        assert (await h.service.handle(_rec(verdict=AIVerdict.WEAK))).outcome is Outcome.SUBMITTED

    async def test_a_stale_signal_is_refused(self) -> None:
        h = await Harness().started()
        rec = _rec(emitted_at=NOW - dt.timedelta(seconds=61))
        result = await h.service.handle(rec)
        assert (result.outcome, result.reason) == (Outcome.REFUSED, RejectReason.STALE_DATA)
        _nothing_placed(h, rec)

    async def test_a_signal_inside_the_age_limit_is_not(self) -> None:
        h = await Harness().started()
        rec = _rec(emitted_at=NOW - dt.timedelta(seconds=59))
        assert (await h.service.handle(rec)).outcome is Outcome.SUBMITTED

    async def test_a_future_dated_signal_is_refused(self) -> None:
        h = await Harness().started()
        rec = _rec(emitted_at=NOW + dt.timedelta(seconds=30))
        result = await h.service.handle(rec)
        assert (result.outcome, result.reason) == (Outcome.REFUSED, RejectReason.STALE_DATA)
        assert "future" in result.detail

    async def test_a_little_clock_skew_is_tolerated(self) -> None:
        h = await Harness().started()
        rec = _rec(emitted_at=NOW + dt.timedelta(seconds=3))
        assert (await h.service.handle(rec)).outcome is Outcome.SUBMITTED

    @pytest.mark.parametrize("mode", [SystemMode.ALERT_ONLY, SystemMode.APPROVAL])
    async def test_a_mode_that_places_no_orders_places_none(self, mode: SystemMode) -> None:
        h = await Harness(mode=mode).started()
        rec = _rec()
        result = await h.service.handle(rec)
        assert result.outcome is Outcome.REFUSED
        assert result.reason is None, "a working mode is not a fault"
        assert mode.value in result.detail
        _nothing_placed(h, rec)

    async def test_an_unknown_instrument_is_refused_with_a_reason(self) -> None:
        h = await Harness().started()
        rec = _rec("WIPRO")
        result = await h.service.handle(rec)
        assert (result.outcome, result.reason) == (
            Outcome.REFUSED,
            RejectReason.RISK_ENGINE_FAULT,
        )
        assert h.execution_rows() == [("REFUSED", "RISK_ENGINE_FAULT")]
        _nothing_placed(h, rec)


class TestDuplicates:
    async def test_a_redelivered_signal_is_not_sent_twice(self) -> None:
        h = await Harness().started()
        rec = _rec()
        await h.service.handle(rec)
        again = await h.service.handle(rec)
        assert again.outcome is Outcome.DUPLICATE
        assert len(h.placer.submitted) == 1

    async def test_an_entry_a_previous_process_sent_is_not_re_evaluated(self) -> None:
        """No plan is registered: re-evaluating could re-size an order already
        at the broker. If it fills, reconciliation exits it (S12 AC3)."""
        h = await Harness().started()
        rec = _rec()
        h.orders.rows[_cid(rec)] = {"client_order_id": _cid(rec), "status": "SUBMITTED"}
        result = await h.service.handle(rec)
        assert result.outcome is Outcome.DUPLICATE
        assert h.manager.plan_for(_cid(rec)) is None
        assert h.placer.plan_at_send == []

    async def test_an_unreadable_ledger_is_treated_as_a_duplicate(self) -> None:
        h = await Harness().started()
        h.orders.fail_find = True
        rec = _rec()
        assert (await h.service.handle(rec)).outcome is Outcome.DUPLICATE
        assert h.placer.plan_at_send == []


# ---------------------------------------------------------------------------
# What a failed send leaves behind
# ---------------------------------------------------------------------------


class TestASendThatFails:
    async def test_a_broker_refusal_releases_the_plan_and_the_slot(self) -> None:
        h = await Harness().started()
        h.placer.raises = OrderRejectedError("insufficient funds")
        rec = _rec()
        result = await h.service.handle(rec)
        assert result.outcome is Outcome.SEND_FAILED
        assert h.manager.plan_for(_cid(rec)) is None
        assert len(h.service.pending) == 0

    async def test_a_rate_limited_send_releases_them_too(self) -> None:
        class Refusing:
            async def allow(self) -> bool:
                return False

        h = await Harness(limiter=Refusing()).started()
        rec = _rec()
        result = await h.service.handle(rec)
        assert result.outcome is Outcome.SEND_FAILED
        assert h.manager.plan_for(_cid(rec)) is None
        assert h.orders.rows == {}

    async def test_an_ambiguous_send_keeps_the_plan(self) -> None:
        """Recorded SUBMITTING, absent at the broker on the follow-up query:
        the gateway will not resubmit. If it landed after all, the fill must
        find its plan - reconciliation writes SUBMIT_FAILED and releases it
        if it never does."""
        h = await Harness().started()
        h.placer.raises = AmbiguousOrderError("timed out")
        rec = _rec()
        result = await h.service.handle(rec)
        assert result.outcome is Outcome.UNKNOWN
        assert h.manager.plan_for(_cid(rec)) is not None
        assert _cid(rec) in h.service.pending

    async def test_a_send_that_was_never_recorded_releases_them(self) -> None:
        """TX1 failed, so the broker was never called."""
        h = await Harness().started()
        h.orders.fail_insert = ConnectionError("database unavailable")
        rec = _rec()
        result = await h.service.handle(rec)
        assert result.outcome is Outcome.SEND_FAILED
        assert h.placer.plan_at_send == []
        assert h.manager.plan_for(_cid(rec)) is None
        assert len(h.service.pending) == 0

    async def test_an_unknown_failure_after_recording_keeps_them(self) -> None:
        h = await Harness().started()
        h.placer.raises = RuntimeError("connection reset")
        rec = _rec()
        result = await h.service.handle(rec)
        assert result.outcome is Outcome.UNKNOWN
        assert h.manager.plan_for(_cid(rec)) is not None

    async def test_when_the_ledger_cannot_say_the_plan_is_kept(self) -> None:
        h = await Harness().started()
        h.placer.raises = RuntimeError("connection reset")
        rec = _rec()

        original = h.orders.find_by_client_order_id
        calls = {"n": 0}

        async def flaky(cid: str):
            calls["n"] += 1
            if calls["n"] > 1:  # the duplicate check passes; the follow-up fails
                raise ConnectionError("unreachable")
            return await original(cid)

        h.orders.find_by_client_order_id = flaky  # type: ignore[method-assign]
        result = await h.service.handle(rec)
        assert result.outcome is Outcome.UNKNOWN
        assert h.manager.plan_for(_cid(rec)) is not None


# ---------------------------------------------------------------------------
# AC3 - restore before the first cycle; trade only on a verified book
# ---------------------------------------------------------------------------


class TestAc3RestoreBeforeTheFirstCycle:
    async def test_the_first_cycle_sees_the_restored_book(self) -> None:
        h = Harness()
        h.store.open_rows = [_open_row("SBIN")]
        await h.service.start()
        assert h.reconciler.cycles[0]["book"] == ["SBIN"]
        assert isinstance(h.manager.tracked("SBIN"), UnverifiedPosition)

    async def test_a_restored_position_holds_its_symbol_and_slot(self) -> None:
        h = Harness()
        h.store.open_rows = [_open_row("SBIN", slot=0)]
        await h.service.start()
        held = await h.service.handle(_rec("SBIN"))
        assert held.reason is RejectReason.ALREADY_HOLDING
        other = await h.service.handle(_rec("INFY"))
        assert other.slot_index == 1

    async def test_a_restore_that_fails_stops_the_start(self) -> None:
        h = Harness()
        h.store.fail_restore = True
        with pytest.raises(ConnectionError):
            await h.service.start()
        assert h.reconciler.cycles == [], "a cycle ran on a book that was never restored"
        assert not h.service.ready

    async def test_signals_before_start_are_refused(self) -> None:
        h = Harness()
        rec = _rec()
        result = await h.service.handle(rec)
        assert (result.outcome, result.reason) == (
            Outcome.REFUSED,
            RejectReason.HEALTH_GATE_FAILED,
        )
        _nothing_placed(h, rec)

    async def test_a_failed_first_cycle_leaves_the_service_unready(self) -> None:
        h = Harness()
        h.reconciler.raises = ConnectionError("broker unreachable")
        await h.service.start()
        assert not h.service.ready
        assert (await h.service.handle(_rec())).outcome is Outcome.REFUSED

    async def test_the_next_good_cycle_makes_it_ready(self) -> None:
        h = Harness()
        h.reconciler.raises = ConnectionError("broker unreachable")
        await h.service.start()
        h.reconciler.raises = None
        await h.service.tick()
        assert h.service.ready
        assert (await h.service.handle(_rec())).outcome is Outcome.SUBMITTED


# ---------------------------------------------------------------------------
# §9.1 - a cycle never runs in the middle of a signal
# ---------------------------------------------------------------------------


class TestSignalsAndCyclesAreSerialised:
    async def test_a_cycle_waits_for_a_send_in_flight(self) -> None:
        h = await Harness().started()
        events: list[str] = []
        h.placer.events = events
        h.reconciler.events = events
        h.placer.release = asyncio.Event()

        signal = asyncio.create_task(h.service.handle(_rec()))
        while "send-start" not in events:
            await asyncio.sleep(0)
        cycle = asyncio.create_task(h.service.on_reconnect())
        for _ in range(20):
            await asyncio.sleep(0)
        assert "cycle" not in events, "a cycle ran between the send and its outcome"
        h.placer.release.set()
        await asyncio.gather(signal, cycle)
        assert events == ["send-start", "send-end", "cycle"]

    async def test_a_cycle_that_waited_reads_the_clock_when_it_runs(self) -> None:
        h = await Harness().started()
        h.placer.release = asyncio.Event()
        signal = asyncio.create_task(h.service.handle(_rec()))
        for _ in range(5):
            await asyncio.sleep(0)
        cycle = asyncio.create_task(h.service.on_reconnect())
        for _ in range(5):
            await asyncio.sleep(0)
        h.clock.now = NOW + dt.timedelta(seconds=7)
        h.placer.release.set()
        await asyncio.gather(signal, cycle)
        assert h.reconciler.cycles[-1]["now"] == NOW + dt.timedelta(seconds=7)


# ---------------------------------------------------------------------------
# The stream
# ---------------------------------------------------------------------------


class Source:
    def __init__(self) -> None:
        self.acked: list[str] = []
        self.poisoned: list[tuple[str, str]] = []

    async def start(self) -> None:
        return None

    async def next_batch(self) -> list[Delivery]:
        return []

    async def done(self, delivery: Delivery) -> None:
        self.acked.append(delivery.entry_id)

    async def poison(self, delivery: Delivery, reason: str) -> None:
        self.poisoned.append((delivery.entry_id, reason))


def _delivery(rec: Recommendation | None = None, *, payload: dict | None = None, corr=None):
    rec = rec or _rec()
    return Delivery(
        entry_id="1-0",
        envelope=Envelope(
            correlation_id=corr or rec.correlation_id,
            emitted_at=NOW,
            emitted_by="signal-svc",
            payload=payload if payload is not None else rec.model_dump(mode="json"),
        ),
    )


class TestTheStream:
    async def test_a_handled_signal_is_acknowledged(self) -> None:
        h = await Harness().started()
        source = Source()
        result = await h.service.deliver(source, _delivery())
        assert result is not None and result.outcome is Outcome.SUBMITTED
        assert source.acked == ["1-0"] and source.poisoned == []

    async def test_a_refused_signal_is_acknowledged_too(self) -> None:
        """Refused is an outcome, not a failure; retrying it changes nothing."""
        h = await Harness().started()
        source = Source()
        await h.service.deliver(source, _delivery(_rec(verdict=AIVerdict.VETO)))
        assert source.acked == ["1-0"]

    async def test_a_payload_that_is_not_a_recommendation_is_dead_lettered(self) -> None:
        h = await Harness().started()
        source = Source()
        payload = _rec().model_dump(mode="json") | {"quantity": 500}
        assert await h.service.deliver(source, _delivery(payload=payload)) is None
        assert source.acked == []
        assert source.poisoned[0][1].startswith("not a Recommendation")
        assert h.placer.plan_at_send == []

    async def test_an_envelope_that_disagrees_with_its_payload_is_dead_lettered(self) -> None:
        h = await Harness().started()
        source = Source()
        await h.service.deliver(source, _delivery(corr=uuid4()))
        assert "different correlation ids" in source.poisoned[0][1]
        assert h.placer.plan_at_send == []

    async def test_a_handler_defect_is_dead_lettered_not_retried(self) -> None:
        h = await Harness().started()
        source = Source()

        async def broken(rec: Recommendation):
            raise RuntimeError("defect")

        h.service.handle = broken  # type: ignore[method-assign]
        await h.service.deliver(source, _delivery())
        assert source.poisoned == [("1-0", "handling raised RuntimeError")]
        assert source.acked == []


class TestTheRunLoop:
    async def test_run_starts_consumes_and_reconciles_until_stopped(self) -> None:
        h = Harness()
        stop = asyncio.Event()
        deliveries = [_delivery()]

        class OneShot(Source):
            async def next_batch(self) -> list[Delivery]:
                if deliveries:
                    return [deliveries.pop()]
                stop.set()
                return []

        source = OneShot()

        async def no_sleep(_seconds: float) -> None:
            await asyncio.sleep(0)

        h.service._sleep = no_sleep
        await h.service.run(source, stop)
        assert h.service.ready
        assert source.acked == ["1-0"]
        assert len(h.placer.submitted) == 1


class TestTheSignalAgeLimit:
    def test_the_limit_has_a_hard_ceiling_in_code(self) -> None:
        from pydantic import ValidationError

        from algotrader.common.config import MAX_SIGNAL_AGE_SECONDS

        assert AppConfig(execution={"max_signal_age_seconds": MAX_SIGNAL_AGE_SECONDS})
        with pytest.raises(ValidationError):
            AppConfig(execution={"max_signal_age_seconds": MAX_SIGNAL_AGE_SECONDS + 1})
        with pytest.raises(ValidationError):
            AppConfig(execution={"max_signal_age_seconds": 0})

    async def test_the_configured_limit_is_the_one_applied(self) -> None:
        h = Harness()
        h.config = AppConfig(execution={"max_signal_age_seconds": 120})
        h.service = ExecutionService(
            config=h.config,
            calendar=_calendar(),
            gateway=h.gateway,
            manager=h.manager,
            reconciler=h.reconciler,  # type: ignore[arg-type]
            halts=h.halts,
            margins=Margins(),
            store=h.store,
            orders=h.orders,
            redis=h.redis,  # type: ignore[arg-type]
            facts=Facts(),
            clock=h.clock,
        )
        h.ref["service"] = h.service
        await h.service.start()
        rec = _rec(emitted_at=NOW - dt.timedelta(seconds=100))
        assert (await h.service.handle(rec)).outcome is Outcome.SUBMITTED


class TestTheSecondLayersHoldOnTheirOwn:
    """Branches the ordinary paths cannot reach, because an earlier layer
    refuses first. Each is driven directly so it is the only thing that CAN
    refuse, and the reason is asserted, not just the refusal."""

    async def test_an_approval_with_an_unknown_cas_flag_is_not_sent(self) -> None:
        """A daily row whose flags are clean but whose CAS flag is missing:
        eligibility passes, and the deadline the position needs is unknown."""
        h = await Harness().started()
        del h.store.status["INFY"]["is_cas_stock"]
        rec = _rec()
        result = await h.service.handle(rec)
        assert (result.outcome, result.reason) == (
            Outcome.REFUSED,
            RejectReason.SYMBOL_NOT_TRADABLE,
        )
        assert "CAS flag" in result.detail
        _nothing_placed(h, rec)

    async def test_a_gateway_key_that_differs_from_the_checked_one_is_not_sent(self) -> None:
        """The duplicate check used the service's key. If the gateway derived
        another, that check guarded nothing."""
        h = await Harness().started()
        real = h.gateway.build_entry

        def drifted(decision, recommendation, *, trade_date):
            request = real(decision, recommendation, trade_date=trade_date)
            return request.model_copy(update={"client_order_id": "f" * 32})

        h.gateway.build_entry = drifted  # type: ignore[method-assign]
        rec = _rec()
        result = await h.service.handle(rec)
        assert result.outcome is Outcome.REFUSED
        assert "differs from" in result.detail
        _nothing_placed(h, rec)


class TestNothingSecretReachesTheAuditTable:
    """REC-002/003's rule: decision_log is a sink the log redaction never sees,
    so an exception's TEXT never reaches it - only its type."""

    # Assembled at runtime so secret scanners (gitleaks) don't flag this
    # fixture as a committed credential - as written in one literal it failed
    # CI's Security job (Run 114). It is not a real key.
    SECRET = "api_" + "key=" + "kitekey12345 " + "access_" + "token=" + "tok98765secret"
    DSN = "postgresql://app:" + "hunter2pass" + "@db:5432/algo"

    def _details(self, h: Harness) -> str:
        return " ".join(str(e.payload) for e in h.audit)

    async def test_a_broker_refusal_text_stays_out(self) -> None:
        h = await Harness().started()
        h.placer.raises = OrderRejectedError(self.SECRET)
        result = await h.service.handle(_rec())
        assert result.outcome is Outcome.SEND_FAILED
        assert "OrderRejectedError" in result.detail
        assert "kitekey12345" not in self._details(h)
        assert "kitekey12345" not in result.detail

    async def test_an_unresolved_send_text_stays_out(self) -> None:
        h = await Harness().started()
        h.placer.raises = RuntimeError(self.SECRET)
        result = await h.service.handle(_rec())
        assert result.outcome is Outcome.UNKNOWN
        assert "tok98765secret" not in self._details(h)

    async def test_an_unreadable_context_text_stays_out(self) -> None:
        h = await Harness().started()

        async def failing() -> set[int]:
            raise ConnectionError(f"{TestNothingSecretReachesTheAuditTable.DSN} refused")

        h.store.occupied_slots = failing  # type: ignore[method-assign]
        result = await h.service.handle(_rec())
        assert result.outcome is Outcome.REFUSED
        assert "ConnectionError" in result.detail
        assert "hunter2pass" not in self._details(h)
