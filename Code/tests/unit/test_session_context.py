"""E15-S13: the risk context, built from the session rather than by hand.

Every risk test before this story supplied a ``RiskContext`` directly. These
probe the one place production builds it, and above all what each missing
input becomes: ``None`` that the engine refuses, never a zero or "nothing
armed".
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from algotrader.broker.adapter import MarginSnapshot
from algotrader.common.calendar import MarketCalendar, load_holidays_with_status
from algotrader.common.enums import AIVerdict, Direction
from algotrader.common.models.trading import Position, Recommendation, SizingResult
from algotrader.execution.halt import HaltState
from algotrader.execution.positions import UnverifiedPosition
from algotrader.execution.session_context import (
    ContextUnavailableError,
    NoProducerYet,
    PendingAwareSlots,
    PendingEntries,
    PendingEntry,
    RepositoryContextStore,
    RiskContextBuilder,
    restrictions_from,
    session_losses,
    session_start,
)

# 10:30 IST on an ordinary Tuesday.
NOW = dt.datetime(2026, 8, 25, 5, 0, tzinfo=dt.UTC)
TRADE_DATE = dt.date(2026, 8, 25)


def _calendar() -> MarketCalendar:
    path = Path(__file__).resolve().parents[2] / "config" / "nse_holidays.yaml"
    status = load_holidays_with_status(str(path))
    return MarketCalendar(status.dates, covers_years=status.covers_years)


CLEAN = {"is_t2t": False, "is_asm": False, "is_gsm": False, "is_fno_ban": False}


def _sizing(quantity: int = 40, entry: str = "1200.0000", stop: str = "1181.2500") -> SizingResult:
    return SizingResult(
        quantity=quantity,
        entry_price=Decimal(entry),
        stop_price=Decimal(stop),
        capital_at_risk=Decimal("750.00"),
        binding_constraint="risk_budget",
    )


def _rec(symbol: str = "INFY") -> Recommendation:
    return Recommendation(
        correlation_id=uuid4(),
        symbol=symbol,
        strategy_id="orb_long_v1",
        direction=Direction.LONG,
        trigger_price=Decimal("1200.00"),
        suggested_stop=Decimal("1186.45"),
        timeframe_agreement=3,
        ai_confidence=Decimal("0.82"),
        ai_verdict=AIVerdict.CONFIRM,
        ai_rationale="probe",
        emitted_at=NOW,
    )


class Plans:
    def __init__(self) -> None:
        self.held: set[str] = set()

    def plan_for(self, cid: str) -> object | None:
        return object() if cid in self.held else None


class Store:
    """Answers from configured facts. ``fail`` names the reads that raise."""

    def __init__(self) -> None:
        self.profiles: dict[str, dict[str, Any]] = {
            "INFY": {"sector": "IT", "lot_size": 1},
            "TCS": {"sector": "IT", "lot_size": 1},
            "SBIN": {"sector": "BANKS", "lot_size": 1},
        }
        self.status: dict[str, dict[str, Any]] = {
            "INFY": {**CLEAN, "is_cas_stock": False},
            "TCS": {**CLEAN, "is_cas_stock": True},
        }
        self.closed: list[dict[str, Any]] = []
        self.occupied: set[int] = set()
        self.fail: set[str] = set()

    def _maybe(self, op: str) -> None:
        if op in self.fail:
            raise ConnectionError(f"{op} is unreachable")

    async def closed_since(self, since: dt.datetime) -> list[dict[str, Any]]:
        self._maybe("closed_since")
        return list(self.closed)

    async def occupied_slots(self) -> set[int]:
        self._maybe("occupied_slots")
        return set(self.occupied)

    async def profile(self, symbol: str) -> dict[str, Any] | None:
        self._maybe("profile")
        self._maybe(f"profile:{symbol}")
        return self.profiles.get(symbol)

    async def daily_status(self, symbol: str, trade_date: dt.date) -> dict[str, Any] | None:
        self._maybe("daily_status")
        return self.status.get(symbol)


class Halts:
    def __init__(self, *, kill: bool = False) -> None:
        self.kill = kill

    async def state(self) -> HaltState:
        return HaltState(self.kill, False, False, {})


class Margins:
    def __init__(self) -> None:
        self.fail = False

    async def fetch_margins(self) -> MarginSnapshot:
        if self.fail:
            raise ConnectionError("margin read timed out")
        return MarginSnapshot(
            available_cash=Decimal("400000"),
            available_margin=Decimal("400000"),
            used_margin=Decimal("0"),
            fetched_at=NOW,
        )


class Facts:
    async def unhealthy_services(self, *, now: dt.datetime) -> tuple[str, ...] | None:
        return ()

    async def atr(self, symbol: str, *, strategy_id: str, now: dt.datetime) -> Decimal | None:
        return Decimal("12.5")

    async def margin_per_share(
        self, symbol: str, price: Decimal, *, direction: Direction, now: dt.datetime
    ) -> Decimal | None:
        return Decimal("240")

    async def correlations(self, symbol, against, *, now):
        return {s: Decimal("0.1") for s in against}


class Book:
    def __init__(self, positions: list[Any] | None = None) -> None:
        self.positions = positions or []

    def open_positions(self) -> list[Any]:
        return list(self.positions)


def _builder(
    *,
    store: Store | None = None,
    halts: Halts | None = None,
    margins: Margins | None = None,
    book: Book | None = None,
    pending: PendingEntries | None = None,
    facts: Any = None,
    slots_total: int = 5,
) -> RiskContextBuilder:
    return RiskContextBuilder(
        capital=Decimal("500000"),
        slots_total=slots_total,
        exit_buffer_minutes=5,
        calendar=_calendar(),
        halts=halts or Halts(),
        margins=margins or Margins(),
        store=store or Store(),
        book=book or Book(),
        pending=pending or PendingEntries(Plans()),
        facts=facts or Facts(),
    )


async def _build(builder: RiskContextBuilder, symbol: str = "INFY"):
    return await builder.build(_rec(symbol), now=NOW, trade_date=TRADE_DATE)


def _held(symbol: str = "SBIN") -> UnverifiedPosition:
    return UnverifiedPosition(
        position=Position(
            correlation_id=uuid4(),
            symbol=symbol,
            slot_index=0,
            direction=Direction.LONG,
            quantity=50,
            entry_price=Decimal("800.0000"),
            stop_price=Decimal("790.0000"),
            opened_at=NOW,
            squareoff_deadline=dt.datetime(2026, 8, 25, 9, 45, tzinfo=dt.UTC),
        )
    )


# ---------------------------------------------------------------------------
# Pure derivations
# ---------------------------------------------------------------------------


class TestRestrictions:
    def test_no_row_is_unknown_not_clean(self) -> None:
        assert restrictions_from(None) is None

    def test_a_clean_row_is_an_empty_tuple(self) -> None:
        """The control: checked and clean must not read as unknown."""
        assert restrictions_from(CLEAN) == ()

    @pytest.mark.parametrize(
        ("flag", "label"),
        [("is_t2t", "T2T"), ("is_asm", "ASM"), ("is_gsm", "GSM"), ("is_fno_ban", "FNO_BAN")],
    )
    def test_each_flag_blocks_on_its_own(self, flag: str, label: str) -> None:
        assert restrictions_from({**CLEAN, flag: True}) == (label,)

    def test_the_detail_behind_a_flag_reaches_the_label(self) -> None:
        row = {**CLEAN, "is_asm": True, "asm_category": "LONG_TERM", "is_gsm": True, "gsm_stage": 2}
        assert restrictions_from(row) == ("ASM:LONG_TERM", "GSM:2")

    @pytest.mark.parametrize("flag", ["is_t2t", "is_asm", "is_gsm", "is_fno_ban"])
    def test_a_missing_or_null_flag_is_not_a_clean_flag(self, flag: str) -> None:
        missing = {k: v for k, v in CLEAN.items() if k != flag}
        assert restrictions_from(missing) != ()
        assert restrictions_from({**CLEAN, flag: None}) != ()


class TestSessionLosses:
    def test_no_closed_trades_is_zero_and_no_streak(self) -> None:
        assert session_losses([]) == (Decimal(0), 0)

    def test_the_total_is_the_sum_and_the_streak_counts_back_from_the_end(self) -> None:
        rows = [{"realized_pnl": Decimal(p)} for p in ("500", "-200", "-300")]
        assert session_losses(rows) == (Decimal("0"), 2)

    def test_a_win_ends_a_streak(self) -> None:
        rows = [{"realized_pnl": Decimal(p)} for p in ("-200", "-300", "100")]
        assert session_losses(rows) == (Decimal("-400"), 0)

    def test_break_even_neither_extends_nor_ends_a_streak(self) -> None:
        rows = [{"realized_pnl": Decimal(p)} for p in ("-200", "0", "-300", "0")]
        assert session_losses(rows) == (Decimal("-500"), 2)

    def test_one_trade_without_a_pnl_makes_both_unknown(self) -> None:
        rows = [{"realized_pnl": Decimal("-200")}, {"realized_pnl": None}]
        assert session_losses(rows) == (None, None)

    def test_the_day_starts_at_midnight_ist(self) -> None:
        assert session_start(TRADE_DATE) == dt.datetime(2026, 8, 24, 18, 30, tzinfo=dt.UTC)


# ---------------------------------------------------------------------------
# Pending entries
# ---------------------------------------------------------------------------


def _pending(cid: str = "cid-1", symbol: str = "TCS", slot: int = 1) -> PendingEntry:
    return PendingEntry(
        client_order_id=cid,
        symbol=symbol,
        direction=Direction.LONG,
        sizing=_sizing(),
        slot_index=slot,
    )


class TestPendingEntries:
    def test_an_entry_is_dropped_once_its_plan_is_gone(self) -> None:
        plans = Plans()
        plans.held = {"cid-1", "cid-2"}
        pending = PendingEntries(plans)
        pending.add(_pending("cid-1", slot=1))
        pending.add(_pending("cid-2", slot=2))
        plans.held.discard("cid-1")
        pending.prune()
        assert "cid-1" not in pending
        assert pending.slots() == {2}

    def test_one_key_is_pending_once(self) -> None:
        pending = PendingEntries(Plans())
        pending.add(_pending())
        with pytest.raises(ValueError, match="already pending"):
            pending.add(_pending())

    async def test_the_allocator_sees_pending_slots_as_occupied(self) -> None:
        store = Store()
        store.occupied = {0}
        plans = Plans()
        plans.held = {"cid-1"}
        pending = PendingEntries(plans)
        pending.add(_pending(slot=3))
        assert await PendingAwareSlots(store, pending).occupied_slots() == {0, 3}

    @pytest.mark.parametrize(
        ("read", "empty"),
        [
            (len, 0),
            (lambda p: "cid-1" in p, False),
            (lambda p: p.entries(), ()),
            (lambda p: p.slots(), set()),
        ],
        ids=["len", "contains", "entries", "slots"],
    )
    def test_every_read_prunes_first(self, read, empty) -> None:
        """A forgotten plan is never reported as in flight, by any read.

        One read per case, on a fresh set: asserted in sequence, the first
        read pruned for all of them and a read that never pruned passed
        (mutation 'ctx: reads do not prune' survived exactly that way)."""
        plans = Plans()
        plans.held = {"cid-1"}
        pending = PendingEntries(plans)
        pending.add(_pending("cid-1", slot=3))
        plans.held.clear()
        assert read(pending) == empty


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------


class TestTheBuilder:
    async def test_a_full_session_builds_a_complete_context(self) -> None:
        built = await _build(_builder())
        ctx = built.context
        assert built.is_cas_stock is False
        assert ctx.kill_switch_active is False
        assert ctx.unhealthy_services == ()
        assert ctx.symbol_restrictions == ()
        assert ctx.symbol_sector == "IT"
        assert ctx.available_margin == Decimal("400000")
        assert ctx.realised_pnl_today == Decimal(0)
        assert ctx.consecutive_losses == 0
        assert ctx.lot_size == 1
        assert ctx.slots_total == 5 and ctx.slots_used == 0

    async def test_the_cas_flag_sets_the_deadline(self) -> None:
        """15:10 CAS / 15:20 non-CAS, minus the 5-minute buffer, in IST."""
        cas = await _build(_builder(), "TCS")
        non_cas = await _build(_builder(), "INFY")
        assert cas.is_cas_stock is True
        assert cas.context.squareoff_deadline == dt.datetime(2026, 8, 25, 9, 35, tzinfo=dt.UTC)
        assert non_cas.context.squareoff_deadline == dt.datetime(2026, 8, 25, 9, 45, tzinfo=dt.UTC)

    async def test_no_daily_row_means_unknown_eligibility_and_the_earlier_deadline(self) -> None:
        store = Store()
        del store.status["INFY"]
        built = await _build(_builder(store=store))
        assert built.is_cas_stock is None
        assert built.context.symbol_restrictions is None
        assert built.context.squareoff_deadline == dt.datetime(2026, 8, 25, 9, 35, tzinfo=dt.UTC)

    async def test_a_failed_daily_read_is_the_same_unknown(self) -> None:
        store = Store()
        store.fail = {"daily_status"}
        built = await _build(_builder(store=store))
        assert built.is_cas_stock is None
        assert built.context.symbol_restrictions is None

    async def test_an_armed_latch_reaches_the_context(self) -> None:
        built = await _build(_builder(halts=Halts(kill=True)))
        assert built.context.kill_switch_active is True

    async def test_an_unreadable_margin_is_none_not_zero(self) -> None:
        margins = Margins()
        margins.fail = True
        built = await _build(_builder(margins=margins))
        assert built.context.available_margin is None

    async def test_unreadable_closed_trades_leave_both_loss_figures_unknown(self) -> None:
        store = Store()
        store.fail = {"closed_since"}
        built = await _build(_builder(store=store))
        assert built.context.realised_pnl_today is None
        assert built.context.consecutive_losses is None

    async def test_the_days_losses_reach_the_context(self) -> None:
        store = Store()
        store.closed = [{"realized_pnl": Decimal("-1200")}, {"realized_pnl": Decimal("-800")}]
        built = await _build(_builder(store=store))
        assert built.context.realised_pnl_today == Decimal("-2000")
        assert built.context.consecutive_losses == 2

    async def test_with_no_producers_every_live_input_is_unknown(self) -> None:
        """NoProducerYet wired in: health, ATR and per-share margin are None,
        and the correlations of a held symbol are absent - each a refusal."""
        built = await _build(_builder(facts=NoProducerYet(), book=Book([_held("SBIN")])))
        ctx = built.context
        assert ctx.unhealthy_services is None
        assert ctx.atr is None
        assert ctx.margin_per_share is None
        assert "SBIN" not in ctx.correlations

    async def test_the_producers_are_asked_about_this_signal(self) -> None:
        """E15-S14: the ATR is the signal's own strategy's, the margin is for the
        signal's own side. A builder that dropped either would size a short from
        a long's margin, or a 15m strategy from whichever timeframe came first."""
        asked: dict[str, dict[str, Any]] = {}

        class Asked(Facts):
            async def atr(self, symbol, *, strategy_id, now):
                asked["atr"] = {"symbol": symbol, "strategy_id": strategy_id, "now": now}
                return await super().atr(symbol, strategy_id=strategy_id, now=now)

            async def margin_per_share(self, symbol, price, *, direction, now):
                asked["margin"] = {"symbol": symbol, "price": price, "direction": direction}
                return await super().margin_per_share(symbol, price, direction=direction, now=now)

        rec = _rec("INFY").model_copy(
            update={"strategy_id": "orb_short_v2", "direction": Direction.SHORT}
        )
        await _builder(facts=Asked()).build(rec, now=NOW, trade_date=TRADE_DATE)
        assert asked["atr"] == {"symbol": "INFY", "strategy_id": "orb_short_v2", "now": NOW}
        assert asked["margin"] == {
            "symbol": "INFY",
            "price": rec.trigger_price,
            "direction": Direction.SHORT,
        }

    async def test_an_unknown_instrument_is_refused_before_the_engine(self) -> None:
        with pytest.raises(ContextUnavailableError, match="not in the instrument master"):
            await _build(_builder(), "WIPRO")

    async def test_an_unreadable_instrument_is_refused(self) -> None:
        store = Store()
        store.fail = {"profile"}
        with pytest.raises(ContextUnavailableError, match="instrument record"):
            await _build(_builder(store=store))

    async def test_unreadable_slots_are_refused_rather_than_counted_as_free(self) -> None:
        store = Store()
        store.fail = {"occupied_slots"}
        with pytest.raises(ContextUnavailableError, match="occupied slots"):
            await _build(_builder(store=store))

    async def test_a_missing_lot_size_is_refused_not_defaulted_to_one(self) -> None:
        store = Store()
        store.profiles["INFY"] = {"sector": "IT"}
        with pytest.raises(ContextUnavailableError, match="lot_size"):
            await _build(_builder(store=store))

    async def test_more_slots_used_than_exist_is_refused(self) -> None:
        store = Store()
        store.occupied = {0, 1}
        with pytest.raises(ContextUnavailableError, match="slots_used"):
            await _build(_builder(store=store, slots_total=1))


class TestPendingEntriesCountAsHeld:
    def _with_pending(self) -> tuple[RiskContextBuilder, Plans]:
        plans = Plans()
        plans.held = {"cid-1"}
        pending = PendingEntries(plans)
        pending.add(_pending("cid-1", symbol="TCS", slot=2))
        return _builder(pending=pending), plans

    async def test_a_pending_entry_is_held_slotted_and_exposed(self) -> None:
        builder, _plans = self._with_pending()
        ctx = (await _build(builder)).context
        assert ctx.holds("TCS")
        assert ctx.slots_used == 1
        assert ctx.sector_exposure("IT") == Decimal("48000.0000")

    async def test_its_slot_is_counted_once_with_the_databases(self) -> None:
        store = Store()
        store.occupied = {2}
        plans = Plans()
        plans.held = {"cid-1"}
        pending = PendingEntries(plans)
        pending.add(_pending("cid-1", slot=2))
        ctx = (await _build(_builder(store=store, pending=pending))).context
        assert ctx.slots_used == 1

    async def test_once_its_plan_is_forgotten_it_holds_nothing(self) -> None:
        """The control: pruning is what releases it."""
        builder, plans = self._with_pending()
        plans.held.clear()
        ctx = (await _build(builder)).context
        assert not ctx.holds("TCS")
        assert ctx.slots_used == 0

    async def test_the_book_is_held_too_with_its_sector(self) -> None:
        ctx = (await _build(_builder(book=Book([_held("SBIN")])))).context
        assert ctx.holds("SBIN")
        assert ctx.sector_exposure("BANKS") == Decimal("40000.0000")

    async def test_a_held_symbol_whose_sector_cannot_be_read_is_unclassified(self) -> None:
        store = Store()
        store.fail = {"profile:SBIN"}
        ctx = (await _build(_builder(store=store, book=Book([_held("SBIN")])))).context
        assert ctx.positions_missing_a_sector() == ("SBIN",)


class TestTheRepositoryStore:
    async def test_it_answers_from_the_three_repositories(self) -> None:
        class Positions:
            async def closed_since(self, since):
                return [{"realized_pnl": Decimal("1")}]

            async def occupied_slots(self):
                return {1}

        class Instruments:
            async def profile(self, symbol):
                return {"sector": "IT", "lot_size": 1}

        class Daily:
            async def for_symbol(self, symbol, trade_date):
                return {"symbol": symbol, "trade_date": trade_date}

        store = RepositoryContextStore(
            positions=Positions(), instruments=Instruments(), daily_status=Daily()
        )
        assert await store.occupied_slots() == {1}
        assert await store.closed_since(NOW) == [{"realized_pnl": Decimal("1")}]
        assert (await store.profile("INFY"))["sector"] == "IT"
        assert (await store.daily_status("INFY", TRADE_DATE))["trade_date"] == TRADE_DATE
