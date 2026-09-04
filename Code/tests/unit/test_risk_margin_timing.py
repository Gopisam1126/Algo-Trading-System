"""Margin and timing checks 13–14 (E14-S06) — one class per acceptance criterion.

AC1  unknown margin rejects
AC2  margin below one share rejects
AC3  ample margin passes
AC4  an unknown margin_per_share rejects
AC5  a non-positive margin_per_share is refused at construction
AC6  too little runway rejects
AC7  a deadline already PASSED rejects
AC8  ample runway passes
AC9  the CONTROL — known margin, an affordable share and ample runway pass
AC10 order, and these run last

These are the last two of the fourteen, so this file also pins the shape of the
completed pipeline.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from uuid import UUID

import pytest

from algotrader.common.calendar import IST
from algotrader.common.enums import AIVerdict, Direction, RejectReason
from algotrader.common.metrics import reset_metrics_for_testing
from algotrader.common.models.trading import Recommendation
from algotrader.execution.risk.checks import (
    MARGIN_TIMING_ORDER,
    all_check_ids,
    build_margin_check,
    build_margin_timing_checks,
    build_squareoff_runway_check,
)
from algotrader.execution.risk.context import RiskContext, RiskContextError
from algotrader.execution.risk.framework import MAX_CHECK_ID, RiskEngine

#: 2026-08-25 is a Tuesday. 04:30 UTC is 10:00 IST; the CAS deadline that day
#: is 15:10 IST minus the 5-minute exit buffer = 15:05 IST = 09:35 UTC.
MIDSESSION = dt.datetime(2026, 8, 25, 4, 30, tzinfo=dt.UTC)
CAS_DEADLINE = dt.datetime(2026, 8, 25, 9, 35, tzinfo=dt.UTC)
CID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

MIN_RUNWAY = 30


@pytest.fixture(autouse=True)
def _fresh_metrics() -> None:
    reset_metrics_for_testing()


def _rec(symbol: str = "INFY") -> Recommendation:
    return Recommendation(
        correlation_id=CID,
        symbol=symbol,
        strategy_id="orb_long_v1",
        direction=Direction.LONG,
        trigger_price=Decimal("1200.00"),
        suggested_stop=Decimal("1186.45"),
        timeframe_agreement=3,
        ai_confidence=Decimal("0.82"),
        ai_verdict=AIVerdict.CONFIRM,
        ai_rationale="probe",
        emitted_at=MIDSESSION,
    )


def _ctx(**overrides) -> RiskContext:
    """Healthy by default: margin known and ample, deadline hours away."""
    base: dict = {
        "now": MIDSESSION,
        "squareoff_deadline": CAS_DEADLINE,
        "capital": Decimal("500000"),
        "slots_total": 5,
        "slots_used": 0,
        "available_margin": Decimal("250000"),
        "margin_per_share": Decimal("240"),
    }
    base.update(overrides)
    return RiskContext(**base)


def _at_ist(hour: int, minute: int) -> dt.datetime:
    return dt.datetime(2026, 8, 25, hour, minute, tzinfo=IST).astimezone(dt.UTC)


def _margin():
    return build_margin_check().fn


def _runway():
    return build_squareoff_runway_check(MIN_RUNWAY).fn


def _both():
    return build_margin_timing_checks(min_minutes_to_squareoff=MIN_RUNWAY)


class TestAC1UnknownMarginRejects:
    """`RiskContext`'s own docstring names this check as the reason
    `available_margin` is `| None`: a default would turn "we could not reach
    the broker" into a number."""

    def test_absent_margin_raises(self) -> None:
        with pytest.raises(RiskContextError, match="margin"):
            _margin()(_rec(), _ctx(available_margin=None))

    def test_the_error_names_the_symbol(self) -> None:
        with pytest.raises(RiskContextError, match="INFY"):
            _margin()(_rec(), _ctx(available_margin=None))

    def test_the_engine_turns_it_into_a_fault_not_an_approval(self) -> None:
        engine = RiskEngine(checks=[build_margin_check()])
        decision = engine.evaluate(_rec(), _ctx(available_margin=None))
        assert not decision.approved
        assert decision.reason is RejectReason.RISK_ENGINE_FAULT

    def test_unknown_is_distinguishable_from_insufficient(self) -> None:
        """SIT-001's distinction at the last site. "The broker is unreachable"
        and "the account is too small" need opposite responses — one is an
        outage, the other is normal operation."""
        engine = RiskEngine(checks=[build_margin_check()])
        unknown = engine.evaluate(_rec(), _ctx(available_margin=None))
        broke = engine.evaluate(_rec(), _ctx(available_margin=Decimal("10")))
        assert broke.reason is RejectReason.INSUFFICIENT_MARGIN
        assert unknown.reason is RejectReason.RISK_ENGINE_FAULT

    def test_zero_margin_is_a_real_answer_not_an_absent_one(self) -> None:
        """`Decimal(0)` is falsy. A truthiness test on the field would treat a
        genuinely empty account as "unknown" and report a fault instead of the
        correct INSUFFICIENT_MARGIN."""
        outcome = _margin()(_rec(), _ctx(available_margin=Decimal(0)))
        assert not outcome.passed
        assert outcome.reason is RejectReason.INSUFFICIENT_MARGIN


class TestAC2MarginBelowOneShareRejects:
    def test_margin_short_of_one_share_rejects(self) -> None:
        outcome = _margin()(
            _rec(), _ctx(available_margin=Decimal("239"), margin_per_share=Decimal("240"))
        )
        assert not outcome.passed
        assert outcome.reason is RejectReason.INSUFFICIENT_MARGIN

    def test_exactly_one_share_passes(self) -> None:
        """The boundary. Affording exactly one share is affording a position,
        and sizing is what decides whether one share is worth it."""
        assert _margin()(
            _rec(), _ctx(available_margin=Decimal("240"), margin_per_share=Decimal("240"))
        ).passed

    def test_a_negative_margin_rejects(self) -> None:
        """An account in debit. A real state, and one where no new position is
        acceptable."""
        assert not _margin()(_rec(), _ctx(available_margin=Decimal("-5000"))).passed

    def test_the_detail_gives_both_figures(self) -> None:
        detail = _margin()(
            _rec(), _ctx(available_margin=Decimal("100"), margin_per_share=Decimal("240"))
        ).detail
        assert "100.00" in detail
        assert "240.00" in detail
        assert "INFY" in detail


class TestAC3AmpleMarginPasses:
    def test_a_well_funded_account_passes(self) -> None:
        assert _margin()(_rec(), _ctx()).passed


class TestAC4UnknownPerShareMarginRejects:
    """Affordability is a ratio. Without the denominator there is no answer,
    and "no answer" is not "yes"."""

    def test_absent_per_share_margin_raises(self) -> None:
        with pytest.raises(RiskContextError, match="per-share"):
            _margin()(_rec(), _ctx(margin_per_share=None))

    def test_the_engine_turns_it_into_a_fault(self) -> None:
        engine = RiskEngine(checks=[build_margin_check()])
        decision = engine.evaluate(_rec(), _ctx(margin_per_share=None))
        assert decision.reason is RejectReason.RISK_ENGINE_FAULT


class TestAC5ANonPositivePerShareMarginIsUnrepresentable:
    """The sizer divides available margin by this. Zero is a crash; negative is
    a negative quantity, which is an order to sell what we were trying to buy.
    Refused at CONSTRUCTION rather than in the check, so no future reader of
    the field has to remember."""

    @pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-1"), Decimal("-240")])
    def test_it_is_refused_at_construction(self, bad: Decimal) -> None:
        with pytest.raises(RiskContextError, match="margin_per_share"):
            _ctx(margin_per_share=bad)

    def test_the_error_explains_the_consequence(self) -> None:
        with pytest.raises(RiskContextError) as excinfo:
            _ctx(margin_per_share=Decimal("-1"))
        message = str(excinfo.value)
        assert "divides" in message
        assert "negative quantity" in message

    def test_none_is_still_allowed(self) -> None:
        """`None` means "not known", which AC4 rejects at check time. The
        construction guard must not collapse "absent" into "invalid"."""
        assert _ctx(margin_per_share=None).margin_per_share is None

    def test_a_positive_value_is_the_control(self) -> None:
        assert _ctx(margin_per_share=Decimal("0.05")).margin_per_share == Decimal("0.05")


class TestAC6TooLittleRunwayRejects:
    def test_inside_the_minimum_rejects(self) -> None:
        outcome = _runway()(_rec(), _ctx(now=_at_ist(14, 50)))  # 15 min to 15:05
        assert not outcome.passed
        assert outcome.reason is RejectReason.TOO_CLOSE_TO_SQUAREOFF

    def test_exactly_the_minimum_passes(self) -> None:
        """The boundary is inclusive: 30 minutes of runway is the configured
        minimum, so it must be allowed or the setting means 31."""
        assert _runway()(_rec(), _ctx(now=_at_ist(14, 35))).passed  # exactly 30

    def test_one_minute_inside_the_minimum_rejects(self) -> None:
        assert not _runway()(_rec(), _ctx(now=_at_ist(14, 36))).passed

    def test_the_detail_gives_the_remaining_time_and_the_deadline(self) -> None:
        detail = _runway()(_rec(), _ctx(now=_at_ist(14, 50))).detail
        assert "15 minute" in detail
        assert "15:05 IST" in detail
        assert "30-minute" in detail

    def test_it_catches_what_the_no_trade_window_does_not(self) -> None:
        """14:59 is inside the tradable session — the 15:00 blackout has not
        started — and a CAS name has six minutes of runway. This is the case
        the check exists for."""
        assert not _runway()(_rec(), _ctx(now=_at_ist(14, 59))).passed

    def test_a_nonsensical_minimum_is_refused_at_construction(self) -> None:
        with pytest.raises(ValueError, match="below 1"):
            build_squareoff_runway_check(0)


class TestAC7APassedDeadlineRejects:
    """A naive `remaining < minimum` happens to handle a negative, which is
    exactly why it needs its own test: the correct behaviour here is an
    accident of the comparison, not something anyone chose."""

    def test_a_deadline_already_passed_rejects(self) -> None:
        outcome = _runway()(_rec(), _ctx(now=_at_ist(15, 30)))
        assert not outcome.passed
        assert outcome.reason is RejectReason.TOO_CLOSE_TO_SQUAREOFF

    def test_the_detail_says_it_has_passed_rather_than_reporting_negative_time(
        self,
    ) -> None:
        """ "-25 minutes remain" is a sentence an operator has to decode."""
        import re

        detail = _runway()(_rec(), _ctx(now=_at_ist(15, 30))).detail
        assert "passed" in detail
        assert "25 minute" in detail
        # A negative NUMBER, not merely a hyphen -- "square-off" has one of
        # those, which is what the first version of this assertion tripped on.
        assert not re.search(r"-\d", detail), f"negative time leaked: {detail}"

    def test_the_deadline_exactly_now_rejects(self) -> None:
        assert not _runway()(_rec(), _ctx(now=CAS_DEADLINE)).passed


class TestAC8AmpleRunwayPasses:
    def test_mid_morning_passes(self) -> None:
        assert _runway()(_rec(), _ctx(now=_at_ist(10, 0))).passed

    def test_the_open_passes(self) -> None:
        assert _runway()(_rec(), _ctx(now=_at_ist(9, 20))).passed


class TestAC9TheControl:
    """Two checks that rejected everything would satisfy AC1–AC8 perfectly."""

    def test_a_healthy_candidate_passes_both(self) -> None:
        engine = RiskEngine(checks=_both())
        assert engine.evaluate(_rec(), _ctx()).checks_passed == list(MARGIN_TIMING_ORDER)

    def test_a_modest_but_sufficient_account_late_in_the_morning_passes(self) -> None:
        """The realistic case. If this failed the system would trade only in a
        narrow band and nothing would report why."""
        engine = RiskEngine(checks=_both())
        ctx = _ctx(
            now=_at_ist(11, 30),
            available_margin=Decimal("5000"),
            margin_per_share=Decimal("240"),
        )
        assert engine.evaluate(_rec(), ctx).checks_passed == list(MARGIN_TIMING_ORDER)


class TestAC10OrderAndTheCompletedPipeline:
    def test_the_declared_order(self) -> None:
        assert MARGIN_TIMING_ORDER == ("margin_sufficient", "time_to_squareoff")
        assert tuple(c.id for c in _both()) == MARGIN_TIMING_ORDER

    def test_margin_is_reported_before_timing(self) -> None:
        """§5.7's order. With both failing, an operator sees the margin."""
        engine = RiskEngine(checks=_both())
        decision = engine.evaluate(_rec(), _ctx(now=_at_ist(15, 0), available_margin=Decimal("1")))
        assert decision.reason is RejectReason.INSUFFICIENT_MARGIN

    def test_every_check_id_fits_the_audit_column(self) -> None:
        for check in _both():
            assert len(check.id) <= MAX_CHECK_ID, check.id

    def test_every_check_carries_a_description(self) -> None:
        for check in _both():
            assert check.description

    def test_all_fourteen_checks_now_exist_in_the_spec_order(self) -> None:
        """The pipeline is complete. This is the list `LOW_LEVEL_ARCHITECTURE
        §5.7` declares, and it is derived from the group constants rather than
        retyped, so it cannot drift from what the factories build."""
        assert all_check_ids() == (
            "kill_switch",
            "health_gate",
            "trading_window",
            "no_trade_window",
            "symbol_tradable",
            "slot_available",
            "symbol_not_already_held",
            "correlation",
            "sector_exposure",
            "net_exposure",
            "daily_loss",
            "consecutive_loss",
            "margin_sufficient",
            "time_to_squareoff",
        )

    def test_there_are_exactly_fourteen_and_they_are_unique(self) -> None:
        ids = all_check_ids()
        assert len(ids) == 14
        assert len(set(ids)) == 14, "a duplicate id would make the audit ambiguous"

    def test_every_id_fits_the_audit_column(self) -> None:
        """All fourteen, not just this story's two. `decision_log.stage` is
        String(28) and three of the spec's names were longer — this is the
        assertion that would have caught it."""
        for cid in all_check_ids():
            assert len(cid) <= MAX_CHECK_ID, cid


class TestTheChecksStayPure:
    def test_they_are_deterministic(self) -> None:
        ctx = _ctx(now=_at_ist(14, 50), available_margin=Decimal("100"))
        first = [c.fn(_rec(), ctx) for c in _both()]
        second = [c.fn(_rec(), ctx) for c in _both()]
        assert [(o.passed, o.reason, o.detail) for o in first] == [
            (o.passed, o.reason, o.detail) for o in second
        ]

    def test_the_runway_check_reads_the_context_clock_not_the_wall_clock(self) -> None:
        """If it called datetime.now() the decision would depend on when the
        test ran, and replay would be impossible."""
        past = _runway()(_rec(), _ctx(now=_at_ist(10, 0)))
        near = _runway()(_rec(), _ctx(now=_at_ist(14, 55)))
        assert past.passed
        assert not near.passed
