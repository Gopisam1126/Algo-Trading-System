"""Pre-condition checks 1–4 (E14-S02) — one class per acceptance criterion.

AC1 kill switch rejects, and first
AC2 unhealthy service rejects, detail names it
AC3 outside continuous trading rejects with OUTSIDE_TRADING_WINDOW
AC4 inside a configured blackout rejects with NO_TRADE_WINDOW, detail names it
AC5 the CONTROL — a normal mid-session moment passes all four
AC6 registration order is kill_switch, health_gate, trading_window, no_trade_window
AC7 an uncovered holiday year fails CLOSED rather than passing

AC5 is the one that makes the rest mean anything. Four checks that reject
everything would satisfy AC1–AC4 perfectly.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from uuid import UUID

import pytest

from algotrader.common.calendar import HolidayDataError, MarketCalendar
from algotrader.common.enums import AIVerdict, Direction, RejectReason
from algotrader.common.metrics import reset_metrics_for_testing
from algotrader.common.models.trading import Recommendation, SizingResult
from algotrader.execution.risk.checks import (
    PRECONDITION_ORDER,
    build_no_trade_window_check,
    build_precondition_checks,
    build_trading_window_check,
    check_health_gate,
    check_kill_switch,
    validate_no_trade_windows,
)
from algotrader.execution.risk.context import RiskContext
from algotrader.execution.risk.framework import RiskEngine

#: 2026-08-25 is a Tuesday and not on the holiday list — an ordinary session.
#: 04:30 UTC is 10:00 IST: mid-session, outside both configured blackouts.
MIDSESSION = dt.datetime(2026, 8, 25, 4, 30, tzinfo=dt.UTC)
DEADLINE = dt.datetime(2026, 8, 25, 9, 40, tzinfo=dt.UTC)  # 15:10 IST
CID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

#: What system.yaml actually configures.
WINDOWS = ((dt.time(9, 15), dt.time(9, 20)), (dt.time(15, 0), dt.time(15, 30)))


@pytest.fixture(autouse=True)
def _fresh_metrics() -> None:
    reset_metrics_for_testing()


@pytest.fixture(scope="module")
def calendar() -> MarketCalendar:
    """The real shipped calendar, not a stub — these checks are only as correct
    as the holiday list behind them."""
    from pathlib import Path

    from algotrader.common.calendar import load_holidays_with_status

    path = Path(__file__).resolve().parents[2] / "config" / "nse_holidays.yaml"
    status = load_holidays_with_status(str(path))
    return MarketCalendar(status.dates, covers_years=status.covers_years)


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
    base: dict = {
        "now": MIDSESSION,
        "squareoff_deadline": DEADLINE,
        "capital": Decimal("500000"),
        "slots_total": 5,
        "slots_used": 0,
    }
    base.update(overrides)
    return RiskContext(**base)


def _ist(hour: int, minute: int, day: int = 25, month: int = 8) -> dt.datetime:
    """An IST wall-clock moment, returned in UTC — how the system carries it."""
    from algotrader.common.calendar import IST

    naive = dt.datetime(2026, month, day, hour, minute)
    return naive.replace(tzinfo=IST).astimezone(dt.UTC)


class TestAC1KillSwitch:
    def test_an_engaged_switch_rejects(self) -> None:
        outcome = check_kill_switch(_rec(), _ctx(kill_switch_active=True))
        assert not outcome.passed
        assert outcome.reason is RejectReason.KILL_SWITCH_ACTIVE

    def test_a_disengaged_switch_passes(self) -> None:
        assert check_kill_switch(_rec(), _ctx(kill_switch_active=False)).passed

    def test_it_runs_first_so_nothing_else_is_consulted(self, calendar) -> None:
        """AC1's real content. With the switch on, an operator must see
        KILL_SWITCH_ACTIVE — not whichever later gate also happened to fail."""
        engine = RiskEngine(checks=build_precondition_checks(calendar, WINDOWS))
        # Deliberately ALSO outside the trading window and inside a blackout.
        decision = engine.evaluate(_rec(), _ctx(now=_ist(15, 10), kill_switch_active=True))
        assert decision.reason is RejectReason.KILL_SWITCH_ACTIVE
        assert decision.checks_passed == [], "a later check ran before the switch"


class TestAC2HealthGate:
    def test_an_unhealthy_service_rejects(self) -> None:
        outcome = check_health_gate(_rec(), _ctx(unhealthy_services=("ingest-svc",)))
        assert not outcome.passed
        assert outcome.reason is RejectReason.HEALTH_GATE_FAILED

    def test_the_detail_names_every_unhealthy_service(self) -> None:
        """An operator should not have to go looking for which one."""
        outcome = check_health_gate(_rec(), _ctx(unhealthy_services=("signals-svc", "ingest-svc")))
        assert "ingest-svc" in outcome.detail
        assert "signals-svc" in outcome.detail

    def test_all_healthy_passes(self) -> None:
        assert check_health_gate(_rec(), _ctx(unhealthy_services=())).passed

    def test_one_unhealthy_service_is_enough_to_block(self) -> None:
        """Not a quorum, and not "only the critical ones" — naming which
        services may be down while trading continues is a judgement that would
        have to be right for every combination."""
        assert not check_health_gate(_rec(), _ctx(unhealthy_services=("notifier",))).passed


class TestAC3TradingWindow:
    def _check(self, calendar):
        return build_trading_window_check(calendar).fn

    @pytest.mark.parametrize(
        ("moment", "label"),
        [
            (_ist(9, 0), "pre-open"),
            (_ist(15, 45), "post-close"),
            (_ist(10, 0, day=23), "Sunday"),
            (_ist(10, 0, day=15, month=1), "holiday — Maharashtra elections"),
            (_ist(10, 0, day=25, month=12), "holiday — Christmas"),
        ],
    )
    def test_outside_continuous_trading_is_refused(
        self, calendar, moment: dt.datetime, label: str
    ) -> None:
        outcome = self._check(calendar)(_rec(), _ctx(now=moment))
        assert not outcome.passed, f"{label} was treated as tradable"
        assert outcome.reason is RejectReason.OUTSIDE_TRADING_WINDOW

    def test_a_normal_session_moment_passes(self, calendar) -> None:
        assert self._check(calendar)(_rec(), _ctx(now=MIDSESSION)).passed

    def test_the_boundaries_are_the_session_boundaries(self, calendar) -> None:
        """09:15:00 is open; 15:30:00 is not. Half-open, matching the calendar."""
        check = self._check(calendar)
        assert check(_rec(), _ctx(now=_ist(9, 15))).passed
        assert not check(_rec(), _ctx(now=_ist(15, 30))).passed

    def test_the_detail_gives_ist_not_utc(self, calendar) -> None:
        """An operator reads IST. A UTC timestamp in the rejection would make
        every out-of-hours message look five and a half hours wrong."""
        outcome = self._check(calendar)(_rec(), _ctx(now=_ist(8, 0)))
        assert "08:00 IST" in outcome.detail


class TestAC4NoTradeWindow:
    def _check(self, windows=WINDOWS):
        return build_no_trade_window_check(windows).fn

    @pytest.mark.parametrize(
        ("moment", "label"),
        [(_ist(9, 17), "opening noise"), (_ist(15, 10), "near close")],
    )
    def test_inside_a_configured_blackout_is_refused(self, moment: dt.datetime, label: str) -> None:
        outcome = self._check()(_rec(), _ctx(now=moment))
        assert not outcome.passed, f"{label} window did not fire"
        assert outcome.reason is RejectReason.NO_TRADE_WINDOW

    def test_the_detail_names_the_window_that_matched(self) -> None:
        """With two windows configured, "in a no-trade window" does not say
        which — and the two mean different things."""
        outcome = self._check()(_rec(), _ctx(now=_ist(15, 10)))
        assert "15:00-15:30" in outcome.detail

    def test_outside_every_window_passes(self) -> None:
        assert self._check()(_rec(), _ctx(now=MIDSESSION)).passed

    def test_windows_are_half_open(self) -> None:
        """09:15 is inside 09:15-09:20; 09:20 is not. Closed-at-both-ends would
        make two adjacent windows overlap on their shared boundary."""
        check = self._check()
        assert not check(_rec(), _ctx(now=_ist(9, 15))).passed
        assert check(_rec(), _ctx(now=_ist(9, 20))).passed

    def test_an_empty_window_list_blocks_nothing(self) -> None:
        """The control for this check: with no blackouts configured it must be
        transparent, not a gate that rejects by default."""
        assert self._check(windows=())(_rec(), _ctx(now=_ist(9, 17))).passed

    def test_an_inverted_window_is_refused_at_construction(self) -> None:
        """A window with start >= end matches nothing, so a typo silently
        disables a blackout someone deliberately configured."""
        with pytest.raises(ValueError, match="can never"):
            build_no_trade_window_check([(dt.time(15, 0), dt.time(9, 20))])

    def test_the_validator_accepts_the_real_configuration(self) -> None:
        validate_no_trade_windows(WINDOWS)

    def test_the_containment_helper_refuses_a_wrap_around_on_its_own(self) -> None:
        """The second layer, tested directly because the first layer makes it
        unreachable from the public path — which is the point.

        ``validate_no_trade_windows`` refuses an inverted window at wiring
        time, so ``_within_window`` should never see one. It still refuses to
        guess, because "unreachable" is a property of today's callers. Testing
        only the public entry would leave this branch uncovered and its
        behaviour unpinned, and the day someone adds a caller that skips
        validation, a wrap-around window would start matching the whole day.
        """
        from algotrader.execution.risk.checks.preconditions import _in_window

        assert _in_window(dt.time(23, 0), dt.time(22, 0), dt.time(2, 0)) is False
        assert _in_window(dt.time(1, 0), dt.time(22, 0), dt.time(2, 0)) is False
        assert _in_window(dt.time(9, 17), dt.time(9, 15), dt.time(9, 20)) is True


class TestAC5TheControl:
    """Four checks that rejected everything would satisfy AC1–AC4 perfectly."""

    def test_a_normal_midsession_moment_passes_all_four(self, calendar) -> None:
        checks = build_precondition_checks(calendar, WINDOWS)
        ctx = _ctx()
        for check in checks:
            outcome = check.fn(_rec(), ctx)
            assert outcome.passed, f"{check.id} rejected a clean mid-session moment"

    def test_the_engine_clears_all_four_and_reaches_sizing(self, calendar) -> None:
        """End of the pre-condition block: with these four passing, the
        pipeline must get as far as the sizer rather than stopping silently."""
        engine = RiskEngine(
            checks=build_precondition_checks(calendar, WINDOWS),
            sizer=lambda rec, ctx: SizingResult(
                quantity=36,
                entry_price=Decimal("1200.00"),
                stop_price=Decimal("1186.45"),
                capital_at_risk=Decimal("487.80"),
                binding_constraint="risk_per_trade",
            ),
        )
        decision = engine.evaluate(_rec(), _ctx())
        assert decision.approved
        assert decision.checks_passed == list(PRECONDITION_ORDER)


class TestAC6Order:
    def test_the_registration_order_is_the_declared_one(self, calendar) -> None:
        checks = build_precondition_checks(calendar, WINDOWS)
        assert tuple(c.id for c in checks) == PRECONDITION_ORDER

    def test_every_id_fits_the_audit_column(self, calendar) -> None:
        """decision_log.stage is String(28). RiskCheck enforces it, so this is
        the probe that the four real ids actually satisfy it."""
        from algotrader.execution.risk.framework import MAX_CHECK_ID

        for check in build_precondition_checks(calendar, WINDOWS):
            assert len(check.id) <= MAX_CHECK_ID

    def test_the_dependency_free_checks_come_first(self, calendar) -> None:
        """kill_switch and health_gate read only the context, so a
        partially-wired engine still refuses correctly."""
        assert PRECONDITION_ORDER[:2] == ("kill_switch", "health_gate")

    def test_every_check_carries_a_description(self, calendar) -> None:
        """`RiskEngine.describe()` is what a reviewer and the health panel
        read; a blank line there is useless to both."""
        for check in build_precondition_checks(calendar, WINDOWS):
            assert check.description


class TestAC7AnUncoveredYearFailsClosed:
    """The holiday list covers 2026 only, and the calendar RAISES rather than
    guessing for a year it does not describe. That must become a rejection."""

    def test_the_calendar_raises_for_an_uncovered_year(self, calendar) -> None:
        with pytest.raises(HolidayDataError):
            calendar.is_market_open(_ist(10, 0, day=5, month=1).replace(year=2027))

    def test_the_engine_turns_that_into_a_rejection_not_a_crash(self, calendar) -> None:
        """The framework catches a raising check. Verified here rather than
        assumed, because 'the framework handles it' is exactly the sort of
        claim that stops being true."""
        engine = RiskEngine(checks=build_precondition_checks(calendar, WINDOWS))
        future = _ctx(
            now=_ist(10, 0).replace(year=2027),
            squareoff_deadline=DEADLINE.replace(year=2027),
        )
        decision = engine.evaluate(_rec(), future)
        assert not decision.approved
        assert decision.reason is RejectReason.HEALTH_GATE_FAILED
        assert "HolidayDataError" in (decision.detail or "")

    def test_it_does_not_silently_pass(self, calendar) -> None:
        """The failure that would matter: an uncovered year treated as 'no
        holidays', so the system trades on Republic Day."""
        engine = RiskEngine(checks=build_precondition_checks(calendar, WINDOWS))
        future = _ctx(
            now=_ist(10, 0).replace(year=2027),
            squareoff_deadline=DEADLINE.replace(year=2027),
        )
        assert engine.evaluate(_rec(), future).approved is False


class TestTheChecksAreSymbolBlind:
    """The property that justifies these four running first: none looks at the
    symbol. If one ever needs to, it belongs in S03 and the ordering rationale
    no longer holds."""

    def test_the_verdict_does_not_depend_on_the_symbol(self, calendar) -> None:
        checks = build_precondition_checks(calendar, WINDOWS)
        ctx = _ctx(now=_ist(9, 17))  # inside the opening blackout
        verdicts = {
            tuple(c.fn(_rec(sym), ctx).passed for c in checks)
            for sym in ("INFY", "TCS", "RELIANCE", "SBIN")
        }
        assert len(verdicts) == 1, "a pre-condition check varied by symbol"
