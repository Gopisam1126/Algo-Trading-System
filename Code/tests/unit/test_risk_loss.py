"""Loss limit checks 11–12 (E14-S05) — one class per acceptance criterion.

AC1 realised loss at the daily limit rejects
AC2 a PROFIT of the same magnitude does not — the sign is not inverted
AC3 a breached day does not un-halt when losses partly recover
AC4 the consecutive streak at its limit rejects
AC5 the consecutive halt does not clear when a win resets the counter
AC6 the CONTROL — a flat or profitable day passes both
AC7 the threshold comes from CONFIGURED capital, so the limit does not move
AC8 registration order, running after exposure
AC9 each detail carries the actual figures

AC3 and AC5 are the story. A pure predicate over the two live fields
un-halts itself, and `LOW_LEVEL_ARCHITECTURE.md §8.1` forbids that outright.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from uuid import UUID

import pytest

from algotrader.common.enums import AIVerdict, Direction, RejectReason
from algotrader.common.metrics import reset_metrics_for_testing
from algotrader.common.models.trading import Recommendation
from algotrader.execution.risk.checks import (
    LOSS_ORDER,
    build_consecutive_loss_check,
    build_daily_loss_check,
    build_loss_checks,
)
from algotrader.execution.risk.context import RiskContext
from algotrader.execution.risk.framework import MAX_CHECK_ID, RiskEngine

MIDSESSION = dt.datetime(2026, 8, 25, 4, 30, tzinfo=dt.UTC)
DEADLINE = dt.datetime(2026, 8, 25, 9, 40, tzinfo=dt.UTC)
CID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

#: What system.yaml configures. 3% of 500,000 is 15,000.
CAPITAL = Decimal("500000")
MAX_DAILY_LOSS_PCT = Decimal("3.0")
LIMIT_RUPEES = Decimal("15000")
CONSECUTIVE_HALT = 3


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
    base: dict = {
        "now": MIDSESSION,
        "squareoff_deadline": DEADLINE,
        "capital": CAPITAL,
        "slots_total": 5,
        "slots_used": 0,
    }
    base.update(overrides)
    return RiskContext(**base)


def _daily():
    return build_daily_loss_check(MAX_DAILY_LOSS_PCT).fn


def _streak():
    return build_consecutive_loss_check(CONSECUTIVE_HALT).fn


def _both():
    return build_loss_checks(
        max_daily_loss_pct=MAX_DAILY_LOSS_PCT,
        consecutive_loss_halt=CONSECUTIVE_HALT,
    )


class TestAC1DailyLossLimit:
    def test_a_loss_at_the_limit_rejects(self) -> None:
        outcome = _daily()(_rec(), _ctx(realised_pnl_today=-LIMIT_RUPEES))
        assert not outcome.passed
        assert outcome.reason is RejectReason.DAILY_LOSS_LIMIT

    def test_a_loss_beyond_the_limit_rejects(self) -> None:
        outcome = _daily()(_rec(), _ctx(realised_pnl_today=Decimal("-20000")))
        assert not outcome.passed

    def test_a_loss_just_short_of_the_limit_passes(self) -> None:
        """The boundary. The limit is inclusive — reaching it halts — so one
        rupee short must still trade, or the configured limit is not the
        limit."""
        assert _daily()(_rec(), _ctx(realised_pnl_today=Decimal("-14999.99"))).passed

    @pytest.mark.parametrize("bad", [Decimal("0"), Decimal("101"), Decimal("-3")])
    def test_a_nonsensical_limit_is_refused_at_construction(self, bad: Decimal) -> None:
        with pytest.raises(ValueError, match="outside"):
            build_daily_loss_check(bad)


class TestAC2TheSignIsNotInverted:
    """The failure mode that would be worst here: halting on good days and
    trading freely on bad ones. A sign error reads perfectly well and is
    invisible to any test that only ever feeds it losses."""

    def test_a_profit_of_the_same_magnitude_passes(self) -> None:
        assert _daily()(_rec(), _ctx(realised_pnl_today=LIMIT_RUPEES)).passed

    def test_a_very_large_profit_passes(self) -> None:
        assert _daily()(_rec(), _ctx(realised_pnl_today=Decimal("1000000"))).passed

    def test_the_two_directions_disagree(self) -> None:
        """Stated as one assertion so the asymmetry itself is the claim, rather
        than two tests that could both be satisfied by a check that ignores
        the field entirely."""
        down = _daily()(_rec(), _ctx(realised_pnl_today=-LIMIT_RUPEES))
        up = _daily()(_rec(), _ctx(realised_pnl_today=LIMIT_RUPEES))
        assert down.passed is False
        assert up.passed is True

    def test_a_flat_day_passes(self) -> None:
        assert _daily()(_rec(), _ctx(realised_pnl_today=Decimal(0))).passed


class TestAC3ABreachedDayDoesNotUnHalt:
    """§8.1: "HALTED is terminal for the day and is only exited by explicit
    operator action. There is no automatic un-halt."

    The scenario is not exotic. A daily loss limit trips precisely when losing
    positions are open — that is what put the day underwater — and those
    positions then close. One closing at a profit lifts realised P&L back over
    the threshold.
    """

    def test_the_latch_rejects_even_when_the_live_figure_has_recovered(self) -> None:
        recovered = _ctx(
            realised_pnl_today=Decimal("-2000"),  # well inside the limit again
            daily_loss_halted=True,
        )
        outcome = _daily()(_rec(), recovered)
        assert not outcome.passed, (
            "a partial recovery resumed trading on a day the limit had already "
            "halted — §8.1 forbids an automatic un-halt"
        )
        assert outcome.reason is RejectReason.DAILY_LOSS_LIMIT

    def test_the_latch_rejects_even_on_a_profitable_recovery(self) -> None:
        outcome = _daily()(
            _rec(), _ctx(realised_pnl_today=Decimal("50000"), daily_loss_halted=True)
        )
        assert not outcome.passed

    def test_the_detail_says_the_halt_is_already_in_force(self) -> None:
        """An operator seeing this needs to know it is a standing halt rather
        than a fresh breach, because only one of those needs a decision now."""
        detail = _daily()(_rec(), _ctx(daily_loss_halted=True)).detail
        assert "already" in detail
        assert "operator" in detail

    def test_the_live_figure_still_fires_before_the_latch_is_written(self) -> None:
        """Why both halves are read. The latch alone leaves a window between
        the breach and whatever writes the flag."""
        outcome = _daily()(_rec(), _ctx(realised_pnl_today=-LIMIT_RUPEES, daily_loss_halted=False))
        assert not outcome.passed

    def test_an_unlatched_healthy_day_is_the_control(self) -> None:
        assert _daily()(
            _rec(), _ctx(realised_pnl_today=Decimal("-100"), daily_loss_halted=False)
        ).passed


class TestAC4ConsecutiveLossLimit:
    def test_the_streak_at_the_limit_rejects(self) -> None:
        outcome = _streak()(_rec(), _ctx(consecutive_losses=CONSECUTIVE_HALT))
        assert not outcome.passed
        assert outcome.reason is RejectReason.CONSECUTIVE_LOSS_LIMIT

    def test_a_streak_beyond_the_limit_rejects(self) -> None:
        assert not _streak()(_rec(), _ctx(consecutive_losses=10)).passed

    def test_one_short_of_the_limit_passes(self) -> None:
        assert _streak()(_rec(), _ctx(consecutive_losses=CONSECUTIVE_HALT - 1)).passed

    def test_no_losses_passes(self) -> None:
        assert _streak()(_rec(), _ctx(consecutive_losses=0)).passed

    def test_a_nonsensical_limit_is_refused_at_construction(self) -> None:
        """0 would halt before a single trade was placed."""
        with pytest.raises(ValueError, match="below 1"):
            build_consecutive_loss_check(0)


class TestAC5TheConsecutiveHaltDoesNotClearItself:
    """Worse than the daily case: `consecutive_losses` resets to zero on ANY
    winning close, so one good exit would clear a halt that three losses
    caused."""

    def test_the_latch_rejects_after_the_counter_resets(self) -> None:
        outcome = _streak()(_rec(), _ctx(consecutive_losses=0, consecutive_loss_halted=True))
        assert not outcome.passed, (
            "a winning exit reset the counter and resumed trading on a day the "
            "streak limit had already halted"
        )
        assert outcome.reason is RejectReason.CONSECUTIVE_LOSS_LIMIT

    def test_the_detail_explains_why_a_zero_counter_still_rejects(self) -> None:
        """Otherwise the rejection reads as a contradiction — zero consecutive
        losses, rejected for consecutive losses — and an operator would
        reasonably think it a bug."""
        detail = _streak()(_rec(), _ctx(consecutive_loss_halted=True)).detail
        assert "resets the counter" in detail
        assert "operator" in detail

    def test_the_live_counter_still_fires_before_the_latch(self) -> None:
        assert not _streak()(
            _rec(), _ctx(consecutive_losses=CONSECUTIVE_HALT, consecutive_loss_halted=False)
        ).passed

    def test_the_two_latches_are_independent(self) -> None:
        """One shared 'halted' flag would make both checks fire together and an
        operator could not tell which limit tripped — SIT-001's lesson."""
        daily_only = _ctx(daily_loss_halted=True)
        assert not _daily()(_rec(), daily_only).passed
        assert _streak()(_rec(), daily_only).passed

        streak_only = _ctx(consecutive_loss_halted=True)
        assert _daily()(_rec(), streak_only).passed
        assert not _streak()(_rec(), streak_only).passed


class TestAC6TheControl:
    """Two checks that rejected everything would satisfy AC1–AC5 perfectly."""

    def test_a_flat_day_passes_both(self) -> None:
        engine = RiskEngine(checks=_both())
        assert engine.evaluate(_rec(), _ctx()).checks_passed == list(LOSS_ORDER)

    def test_a_normal_losing_day_still_trades(self) -> None:
        """The realistic case, and the one that matters most: a day down 1%
        with one loss behind it must keep trading. A limit that stopped here
        would make the system useless without anything looking broken."""
        engine = RiskEngine(checks=_both())
        ctx = _ctx(realised_pnl_today=Decimal("-5000"), consecutive_losses=1)
        assert engine.evaluate(_rec(), ctx).checks_passed == list(LOSS_ORDER)

    def test_a_profitable_day_with_a_short_streak_passes(self) -> None:
        engine = RiskEngine(checks=_both())
        ctx = _ctx(realised_pnl_today=Decimal("12000"), consecutive_losses=2)
        assert engine.evaluate(_rec(), ctx).checks_passed == list(LOSS_ORDER)


class TestAC7TheThresholdComesFromConfiguredCapital:
    def test_the_limit_is_a_percentage_of_capital(self) -> None:
        """Doubling capital doubles the rupee limit, so a loss that halted the
        smaller account does not halt the larger one."""
        loss = _ctx(capital=Decimal("500000"), realised_pnl_today=Decimal("-15000"))
        assert not _daily()(_rec(), loss).passed
        bigger = _ctx(capital=Decimal("1000000"), realised_pnl_today=Decimal("-15000"))
        assert _daily()(_rec(), bigger).passed

    def test_the_limit_does_not_shrink_as_the_day_loses(self) -> None:
        """`ctx.capital` is the configured base, not a running balance. If the
        threshold were computed from what is left, 3% would mean fewer rupees
        with every loss and the same configuration would mean something
        different at 15:00 than at 09:20."""
        early = _daily()(_rec(), _ctx(realised_pnl_today=Decimal("-14000")))
        assert early.passed
        # Same capital, deeper loss -> now it fires. The threshold itself never
        # moved: 14,000 passed and 15,000 does not, on the same base.
        assert not _daily()(_rec(), _ctx(realised_pnl_today=Decimal("-15000"))).passed


class TestAC8OrderAndRegistration:
    def test_the_declared_order(self) -> None:
        assert LOSS_ORDER == ("daily_loss", "consecutive_loss")
        assert tuple(c.id for c in _both()) == LOSS_ORDER

    def test_every_check_id_fits_the_audit_column(self) -> None:
        for check in _both():
            assert len(check.id) <= MAX_CHECK_ID, check.id

    def test_every_check_carries_a_description(self) -> None:
        for check in _both():
            assert check.description

    def test_the_factory_is_keyword_only(self) -> None:
        """A percentage and a count side by side is exactly the signature where
        a positional call swaps them and a 3-trade streak gets compared against
        3% of capital."""
        with pytest.raises(TypeError):
            build_loss_checks(Decimal("3.0"), 3)  # type: ignore[misc]

    def test_the_daily_limit_is_reported_before_the_streak(self) -> None:
        """With both tripped, an operator should see the one denominated in
        money."""
        engine = RiskEngine(checks=_both())
        decision = engine.evaluate(
            _rec(),
            _ctx(realised_pnl_today=Decimal("-20000"), consecutive_losses=5),
        )
        assert decision.reason is RejectReason.DAILY_LOSS_LIMIT


class TestAC9DetailsCarryTheFigures:
    def test_the_daily_detail_gives_loss_limit_and_capital(self) -> None:
        detail = _daily()(_rec(), _ctx(realised_pnl_today=Decimal("-16000"))).detail
        assert "16000" in detail
        assert "15000" in detail
        assert "500000" in detail

    def test_the_streak_detail_gives_the_count_and_the_limit(self) -> None:
        detail = _streak()(_rec(), _ctx(consecutive_losses=4)).detail
        assert "4 consecutive" in detail
        assert str(CONSECUTIVE_HALT) in detail


class TestTheChecksStayPure:
    def test_they_are_deterministic(self) -> None:
        ctx = _ctx(realised_pnl_today=Decimal("-16000"), consecutive_losses=4)
        first = [c.fn(_rec(), ctx) for c in _both()]
        second = [c.fn(_rec(), ctx) for c in _both()]
        assert [(o.passed, o.reason, o.detail) for o in first] == [
            (o.passed, o.reason, o.detail) for o in second
        ]

    def test_they_do_not_read_the_symbol(self) -> None:
        """These are session-wide conditions. A loss limit that behaved
        differently per symbol would be a different feature, and a surprising
        one."""
        ctx = _ctx(realised_pnl_today=Decimal("-16000"))
        for symbol in ("INFY", "TCS", "SBIN"):
            outcome = _daily()(_rec(symbol), ctx)
            assert not outcome.passed
            assert symbol not in outcome.detail
