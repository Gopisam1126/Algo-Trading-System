"""Symbol eligibility checks 5–7 (E14-S03) — one class per acceptance criterion.

AC1 a blocking restriction rejects with SYMBOL_NOT_TRADABLE, detail names them
AC2 eligibility never established rejects — "not checked" is not "clean"
AC3 checked and clean passes
AC4 no free slot rejects with NO_SLOT_AVAILABLE, detail gives used/total
AC5 already held rejects with ALREADY_HOLDING, in either direction
AC6 the CONTROL — a clean symbol, a free slot and a flat book pass all three
AC7 registration order, and it runs after the four pre-conditions

AC6 is the one that makes the rest mean anything. Three checks that rejected
everything would satisfy AC1–AC5 perfectly.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from uuid import UUID

import pytest

from algotrader.common.calendar import MarketCalendar
from algotrader.common.enums import AIVerdict, Direction, RejectReason
from algotrader.common.metrics import reset_metrics_for_testing
from algotrader.common.models.trading import Recommendation
from algotrader.execution.risk.checks import (
    ELIGIBILITY_ORDER,
    MAX_RESTRICTIONS_NAMED,
    PRECONDITION_ORDER,
    build_eligibility_checks,
    build_precondition_checks,
    check_slot_available,
    check_symbol_not_already_held,
    check_symbol_tradable,
)
from algotrader.execution.risk.context import OpenPosition, RiskContext, RiskContextError
from algotrader.execution.risk.framework import MAX_CHECK_ID, RiskEngine

#: 2026-08-25 is a Tuesday and not a holiday. 04:30 UTC is 10:00 IST.
MIDSESSION = dt.datetime(2026, 8, 25, 4, 30, tzinfo=dt.UTC)
DEADLINE = dt.datetime(2026, 8, 25, 9, 40, tzinfo=dt.UTC)
CID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
WINDOWS = ((dt.time(9, 15), dt.time(9, 20)), (dt.time(15, 0), dt.time(15, 30)))


@pytest.fixture(autouse=True)
def _fresh_metrics() -> None:
    reset_metrics_for_testing()


@pytest.fixture(scope="module")
def calendar() -> MarketCalendar:
    from pathlib import Path

    from algotrader.common.calendar import load_holidays_with_status

    path = Path(__file__).resolve().parents[2] / "config" / "nse_holidays.yaml"
    status = load_holidays_with_status(str(path))
    return MarketCalendar(status.dates, covers_years=status.covers_years)


def _rec(symbol: str = "INFY", direction: Direction = Direction.LONG) -> Recommendation:
    long = direction is Direction.LONG
    return Recommendation(
        correlation_id=CID,
        symbol=symbol,
        strategy_id="orb_long_v1",
        direction=direction,
        trigger_price=Decimal("1200.00"),
        suggested_stop=Decimal("1186.45") if long else Decimal("1213.55"),
        timeframe_agreement=3,
        ai_confidence=Decimal("0.82"),
        ai_verdict=AIVerdict.CONFIRM,
        ai_rationale="probe",
        emitted_at=MIDSESSION,
    )


def _ctx(**overrides) -> RiskContext:
    """A context that is CLEAN by default, so each test states only the one
    thing it is testing. `symbol_restrictions=()` means checked-and-clean."""
    base: dict = {
        "now": MIDSESSION,
        "squareoff_deadline": DEADLINE,
        "capital": Decimal("500000"),
        "slots_total": 5,
        "slots_used": 0,
        "symbol_restrictions": (),
    }
    base.update(overrides)
    return RiskContext(**base)


def _position(symbol: str = "INFY", direction: Direction = Direction.LONG) -> OpenPosition:
    return OpenPosition(
        symbol=symbol,
        direction=direction,
        quantity=40,
        entry_price=Decimal("1200.00"),
        stop_price=Decimal("1186.45"),
    )


class TestAC1ABlockingRestrictionRejects:
    @pytest.mark.parametrize(
        "restrictions",
        [("T2T",), ("ASM_ST_1",), ("GSM_2",), ("FNO_BAN",), ("T2T", "ASM_LT_3")],
    )
    def test_any_restriction_rejects(self, restrictions: tuple[str, ...]) -> None:
        outcome = check_symbol_tradable(_rec(), _ctx(symbol_restrictions=restrictions))
        assert not outcome.passed
        assert outcome.reason is RejectReason.SYMBOL_NOT_TRADABLE

    def test_the_detail_names_the_restrictions(self) -> None:
        """ "not tradable" without the reason tells an operator nothing they can
        act on — T2T clears tomorrow, an F&O ban may not."""
        outcome = check_symbol_tradable(_rec(), _ctx(symbol_restrictions=("T2T", "FNO_BAN")))
        assert "T2T" in outcome.detail
        assert "FNO_BAN" in outcome.detail

    def test_the_detail_names_the_symbol(self) -> None:
        outcome = check_symbol_tradable(_rec("BAJAJ-AUTO"), _ctx(symbol_restrictions=("T2T",)))
        assert "BAJAJ-AUTO" in outcome.detail

    def test_a_flood_of_restrictions_does_not_flood_the_detail(self) -> None:
        """QA-SEC-29's lesson applied before it becomes a finding again: the
        detail reaches the audit payload and a log line once per rejected
        candidate per bar, and nothing upstream promises this list is short."""
        many = tuple(f"FLAG_{i}" for i in range(500))
        outcome = check_symbol_tradable(_rec(), _ctx(symbol_restrictions=many))
        assert len(outcome.detail) < 400, f"detail is {len(outcome.detail)} chars"
        assert "500 blocking restriction(s)" in outcome.detail
        assert f"and {500 - MAX_RESTRICTIONS_NAMED} more" in outcome.detail

    def test_a_realistic_count_still_names_them_all(self) -> None:
        """The control for the cap. A bound that truncated the normal case
        would have made the detail useless for the only case that happens."""
        outcome = check_symbol_tradable(_rec(), _ctx(symbol_restrictions=("T2T", "ASM_ST_1")))
        assert "more" not in outcome.detail


class TestAC2NotCheckedIsARejection:
    """The failure this whole story is shaped around. "Nothing was found" and
    "we never looked" are different, and reading the second as the first is
    wrong in the direction that costs money."""

    def test_unknown_eligibility_raises_rather_than_passing(self) -> None:
        with pytest.raises(RiskContextError):
            check_symbol_tradable(_rec(), _ctx(symbol_restrictions=None))

    def test_the_error_names_the_symbol_whose_eligibility_was_missing(self) -> None:
        with pytest.raises(RiskContextError, match="INFY"):
            check_symbol_tradable(_rec(), _ctx(symbol_restrictions=None))

    def test_the_engine_turns_that_into_a_rejection_not_a_crash(self, calendar) -> None:
        """Verified through the engine rather than assumed. "The framework
        handles it" is exactly the sort of claim that stops being true."""
        engine = RiskEngine(checks=build_eligibility_checks())
        decision = engine.evaluate(_rec(), _ctx(symbol_restrictions=None))
        assert not decision.approved
        assert decision.checks_passed == []

    def test_unknown_is_reported_as_a_fault_not_as_an_untradable_symbol(self) -> None:
        """SIT-001's distinction, applied at a new site. "We checked and this
        symbol is banned" and "we could not check" need different responses:
        the first is normal operation, the second means a fetcher is down."""
        engine = RiskEngine(checks=build_eligibility_checks())
        unknown = engine.evaluate(_rec(), _ctx(symbol_restrictions=None))
        banned = engine.evaluate(_rec(), _ctx(symbol_restrictions=("T2T",)))
        assert banned.reason is RejectReason.SYMBOL_NOT_TRADABLE
        assert unknown.reason is RejectReason.RISK_ENGINE_FAULT
        assert unknown.reason is not banned.reason

    def test_an_empty_tuple_is_not_the_same_as_none(self) -> None:
        """The pair that must not collapse. `()` is a real answer meaning
        clean; `None` is the absence of one. A truthiness test on the field
        would treat them identically and pass this story's every other test."""
        assert check_symbol_tradable(_rec(), _ctx(symbol_restrictions=())).passed
        with pytest.raises(RiskContextError):
            check_symbol_tradable(_rec(), _ctx(symbol_restrictions=None))


class TestAC3CleanPasses:
    def test_a_checked_and_clean_symbol_passes(self) -> None:
        assert check_symbol_tradable(_rec(), _ctx(symbol_restrictions=())).passed


class TestAC4SlotAvailable:
    def test_no_free_slot_rejects(self) -> None:
        outcome = check_slot_available(_rec(), _ctx(slots_total=5, slots_used=5))
        assert not outcome.passed
        assert outcome.reason is RejectReason.NO_SLOT_AVAILABLE

    def test_a_free_slot_passes(self) -> None:
        assert check_slot_available(_rec(), _ctx(slots_total=5, slots_used=4)).passed

    def test_the_detail_distinguishes_contention_from_misconfiguration(self) -> None:
        """Five of five clears on its own; zero of zero never will. An operator
        needs to know which one they are looking at."""
        contended = check_slot_available(_rec(), _ctx(slots_total=5, slots_used=5))
        assert "5 of 5" in contended.detail
        misconfigured = check_slot_available(_rec(), _ctx(slots_total=0, slots_used=0))
        assert "0 of 0" in misconfigured.detail

    def test_an_unconfigured_slot_count_rejects(self) -> None:
        """`slots_total` defaults to 0, so a context nobody configured refuses
        rather than trading unbounded. Fail closed on the default."""
        assert not check_slot_available(_rec(), _ctx(slots_total=0, slots_used=0)).passed

    def test_the_last_slot_is_usable(self) -> None:
        """The boundary. An off-by-one here would waste a whole slot's capital
        permanently, and nothing would report it as an error."""
        assert check_slot_available(_rec(), _ctx(slots_total=1, slots_used=0)).passed
        assert not check_slot_available(_rec(), _ctx(slots_total=1, slots_used=1)).passed


class TestAC5AlreadyHeld:
    def test_a_held_symbol_rejects(self) -> None:
        outcome = check_symbol_not_already_held(
            _rec("INFY"), _ctx(open_positions=(_position("INFY"),))
        )
        assert not outcome.passed
        assert outcome.reason is RejectReason.ALREADY_HOLDING

    @pytest.mark.parametrize("held", [Direction.LONG, Direction.SHORT])
    @pytest.mark.parametrize("incoming", [Direction.LONG, Direction.SHORT])
    def test_it_is_direction_blind(self, held: Direction, incoming: Direction) -> None:
        """All four combinations reject. A SHORT on a held LONG is a reversal
        and a LONG on a held LONG is pyramiding; neither is an entry, and this
        pipeline only makes entries."""
        outcome = check_symbol_not_already_held(
            _rec("INFY", incoming), _ctx(open_positions=(_position("INFY", held),))
        )
        assert not outcome.passed, f"{incoming.value} on a held {held.value} passed"

    def test_a_different_symbol_passes(self) -> None:
        """The control. A check that rejected whenever ANY position was open
        would pass every test above and stop the system at one position."""
        assert check_symbol_not_already_held(
            _rec("TCS"), _ctx(open_positions=(_position("INFY"),))
        ).passed

    def test_a_flat_book_passes(self) -> None:
        assert check_symbol_not_already_held(_rec(), _ctx(open_positions=())).passed

    def test_the_detail_says_what_is_held(self) -> None:
        """An operator seeing "already holding" wants to know which side and
        how much, because that decides whether it is worth intervening."""
        outcome = check_symbol_not_already_held(
            _rec("INFY"), _ctx(open_positions=(_position("INFY", Direction.SHORT),))
        )
        assert "SHORT" in outcome.detail
        assert "40" in outcome.detail

    def test_it_finds_the_symbol_among_several_positions(self) -> None:
        positions = (_position("TCS"), _position("INFY"), _position("WIPRO"))
        assert not check_symbol_not_already_held(
            _rec("INFY"), _ctx(open_positions=positions)
        ).passed
        assert check_symbol_not_already_held(
            _rec("HDFCBANK"), _ctx(open_positions=positions)
        ).passed


class TestAC6TheControl:
    """Three checks that rejected everything would satisfy AC1–AC5 perfectly."""

    def test_a_clean_candidate_passes_all_three(self) -> None:
        engine = RiskEngine(checks=build_eligibility_checks())
        decision = engine.evaluate(_rec(), _ctx())
        assert decision.checks_passed == list(ELIGIBILITY_ORDER)

    def test_it_passes_with_a_partially_used_book(self) -> None:
        """Not just the empty case — a realistic mid-session state with other
        positions open and slots in use must still let a new name through."""
        engine = RiskEngine(checks=build_eligibility_checks())
        decision = engine.evaluate(
            _rec("HDFCBANK"),
            _ctx(
                slots_total=5,
                slots_used=2,
                open_positions=(_position("INFY"), _position("TCS")),
            ),
        )
        assert decision.checks_passed == list(ELIGIBILITY_ORDER)


class TestAC7OrderAndRegistration:
    def test_the_declared_order(self) -> None:
        assert ELIGIBILITY_ORDER == (
            "symbol_tradable",
            "slot_available",
            "symbol_not_already_held",
        )

    def test_eligibility_runs_after_the_preconditions(self, calendar) -> None:
        """Position in the pipeline, not just internal order. A blocked symbol
        during a blackout must report the blackout — the more fundamental
        reason — not the symbol."""
        engine = RiskEngine(
            checks=[
                *build_precondition_checks(calendar, WINDOWS),
                *build_eligibility_checks(),
            ]
        )
        decision = engine.evaluate(
            _rec(),
            _ctx(
                now=dt.datetime(2026, 8, 25, 3, 47, tzinfo=dt.UTC),  # 09:17 IST
                symbol_restrictions=("T2T",),
            ),
        )
        assert decision.reason is RejectReason.NO_TRADE_WINDOW

    def test_the_full_seven_clear_in_order_on_a_clean_candidate(self, calendar) -> None:
        engine = RiskEngine(
            checks=[
                *build_precondition_checks(calendar, WINDOWS),
                *build_eligibility_checks(),
            ]
        )
        decision = engine.evaluate(_rec(), _ctx())
        assert decision.checks_passed == [*PRECONDITION_ORDER, *ELIGIBILITY_ORDER]

    def test_every_check_id_fits_the_audit_column(self) -> None:
        """`decision_log.stage` is String(28). A longer id fails the insert at
        the moment a rejection happens."""
        for check in build_eligibility_checks():
            assert len(check.id) <= MAX_CHECK_ID, check.id

    def test_every_check_carries_a_description(self) -> None:
        for check in build_eligibility_checks():
            assert check.description


class TestTheChecksStayPure:
    def test_none_of_them_mutate_the_context(self) -> None:
        """The context is frozen, but a check could still mutate the mutable
        `correlations` dict or rebind a list. Asserting the whole object is
        unchanged catches what `frozen=True` alone does not."""
        ctx = _ctx(
            open_positions=(_position("TCS"),),
            symbol_restrictions=("T2T",),
            slots_total=5,
            slots_used=5,
        )
        before = (
            ctx.symbol_restrictions,
            ctx.slots_used,
            ctx.slots_total,
            ctx.open_positions,
            dict(ctx.correlations),
        )
        for check in build_eligibility_checks():
            try:
                check.fn(_rec(), ctx)
            except RiskContextError:
                pass
        assert (
            ctx.symbol_restrictions,
            ctx.slots_used,
            ctx.slots_total,
            ctx.open_positions,
            dict(ctx.correlations),
        ) == before

    def test_they_are_deterministic(self) -> None:
        """Same inputs, same answers. The replay property the whole risk layer
        rests on."""
        ctx = _ctx(symbol_restrictions=("T2T", "ASM_ST_1"))
        first = [check.fn(_rec(), ctx) for check in build_eligibility_checks()]
        second = [check.fn(_rec(), ctx) for check in build_eligibility_checks()]
        assert [(o.passed, o.reason, o.detail) for o in first] == [
            (o.passed, o.reason, o.detail) for o in second
        ]
