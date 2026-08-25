"""The risk pipeline frame (E14-S01).

Two acceptance criteria, both properties of the FRAME rather than of any
individual check:

1. 🔴 No order path bypasses the pipeline.
2. Any rejection is explainable from the audit log.

The tests that matter most here are the ones about a check that *raises*. The
obvious implementations are to let it propagate, or to log and continue, and
both are wrong in ways that look reasonable in review: propagating kills the
evaluation of every other candidate in the batch, and continuing means a broken
check silently stops being a check — the pipeline keeps approving with one
fewer gate and nothing in the output says so.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from uuid import UUID

import pytest

from algotrader.common.enums import AIVerdict, Direction, RejectReason
from algotrader.common.metrics import reset_metrics_for_testing
from algotrader.common.models.trading import Recommendation, RiskDecision, SizingResult
from algotrader.execution.risk.context import OpenPosition, RiskContext, RiskContextError
from algotrader.execution.risk.framework import (
    MAX_CHECK_ID,
    CheckOutcome,
    RiskCheck,
    RiskCheckError,
    RiskEngine,
)

NOW = dt.datetime(2026, 8, 25, 5, 30, tzinfo=dt.UTC)  # 11:00 IST
DEADLINE = dt.datetime(2026, 8, 25, 9, 40, tzinfo=dt.UTC)  # 15:10 IST
CID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


@pytest.fixture(autouse=True)
def _fresh_metrics() -> None:
    """Counters start at zero, so a test asserting 0 -> 1 is order-independent."""
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
        emitted_at=NOW,
    )


def _ctx(**overrides) -> RiskContext:
    base: dict = {
        "now": NOW,
        "squareoff_deadline": DEADLINE,
        "capital": Decimal("500000"),
        "slots_total": 5,
        "slots_used": 0,
    }
    base.update(overrides)
    return RiskContext(**base)


def _sizing(quantity: int = 36, binding: str = "risk_per_trade") -> SizingResult:
    return SizingResult(
        quantity=quantity,
        entry_price=Decimal("1200.00"),
        stop_price=Decimal("1186.45"),
        capital_at_risk=Decimal("487.80"),
        binding_constraint=binding,
    )


def _always(passed: bool, reason: RejectReason = RejectReason.KILL_SWITCH_ACTIVE):
    def fn(rec: Recommendation, ctx: RiskContext) -> CheckOutcome:
        return CheckOutcome.ok() if passed else CheckOutcome.fail(reason, "probe refusal")

    return fn


def _sizer(quantity: int = 36, binding: str = "risk_per_trade"):
    return lambda rec, ctx: _sizing(quantity, binding)


class TestTheOrderIsTheDesign:
    def test_the_first_refusal_stops_the_pipeline(self) -> None:
        ran: list[str] = []

        def record(name: str, passed: bool):
            def fn(rec, ctx):
                ran.append(name)
                return (
                    CheckOutcome.ok()
                    if passed
                    else CheckOutcome.fail(RejectReason.KILL_SWITCH_ACTIVE, "no")
                )

            return fn

        engine = RiskEngine(
            checks=[
                RiskCheck("first", record("first", True)),
                RiskCheck("second", record("second", False)),
                RiskCheck("third", record("third", True)),
            ],
            sizer=_sizer(),
        )
        decision = engine.evaluate(_rec(), _ctx())
        assert not decision.approved
        assert ran == ["first", "second"], "a check after the refusal still ran"

    def test_the_rejection_names_the_checks_it_cleared_first(self) -> None:
        """'Rejected by daily_loss' is half an explanation. The other half is
        what it got through, which distinguishes 'blocked immediately' from
        'blocked at the last gate'."""
        engine = RiskEngine(
            checks=[
                RiskCheck("kill_switch", _always(True)),
                RiskCheck("health_gate", _always(True)),
                RiskCheck("daily_loss", _always(False, RejectReason.DAILY_LOSS_LIMIT)),
            ],
            sizer=_sizer(),
        )
        decision = engine.evaluate(_rec(), _ctx())
        assert decision.checks_passed == ["kill_switch", "health_gate"]
        assert decision.reason is RejectReason.DAILY_LOSS_LIMIT

    def test_an_empty_pipeline_still_requires_sizing(self) -> None:
        """No checks is not the same as approved. Sizing is the last gate."""
        engine = RiskEngine(checks=[], sizer=_sizer())
        assert engine.evaluate(_rec(), _ctx()).approved

    def test_the_pipeline_is_introspectable(self) -> None:
        """A reviewer and a health panel both need to see the order."""
        engine = RiskEngine(checks=[RiskCheck("kill_switch", _always(True), "C8")])
        assert engine.check_ids == ("kill_switch",)
        assert "kill_switch" in engine.describe()


class TestABrokenCheckIsARefusalNotASkip:
    """The most important behaviour in this file."""

    @staticmethod
    def _exploding(rec, ctx) -> CheckOutcome:
        raise RuntimeError("upstream data source went away")

    def test_a_raising_check_rejects_rather_than_propagating(self) -> None:
        """Propagating would kill the evaluation of every other candidate in
        the batch, turning one broken check into a total outage."""
        engine = RiskEngine(checks=[RiskCheck("explodes", self._exploding)], sizer=_sizer())
        decision = engine.evaluate(_rec(), _ctx())
        assert not decision.approved
        assert decision.reason is RejectReason.HEALTH_GATE_FAILED

    def test_a_raising_check_does_not_let_the_pipeline_continue(self) -> None:
        """The dangerous alternative: log-and-continue means the pipeline keeps
        approving with one fewer gate and nothing says so."""
        engine = RiskEngine(
            checks=[
                RiskCheck("explodes", self._exploding),
                RiskCheck("would_pass", _always(True)),
            ],
            sizer=_sizer(),
        )
        assert not engine.evaluate(_rec(), _ctx()).approved

    def test_the_detail_names_the_check_and_the_exception(self) -> None:
        engine = RiskEngine(checks=[RiskCheck("explodes", self._exploding)])
        detail = engine.evaluate(_rec(), _ctx()).detail or ""
        assert "explodes" in detail
        assert "RuntimeError" in detail

    def test_an_error_increments_both_counters(self) -> None:
        """An errored check is genuinely two facts at once, and the metrics say
        both: the order WAS rejected (so it belongs in the rejection total), and
        a gate is broken (so it also needs a signal of its own). Recording only
        the rejection would hide a broken check inside a plausible-looking
        rejection rate; recording only the error would understate how many
        candidates were turned away."""
        from algotrader.common.metrics import get_metrics

        engine = RiskEngine(checks=[RiskCheck("explodes", self._exploding)])
        engine.evaluate(_rec(), _ctx())
        samples = {
            (s.labels.get("check"), s.name): s.value
            for m in get_metrics().registry.collect()
            for s in m.samples
        }
        assert samples.get(("explodes", "risk_check_errors_total")) == 1.0
        assert samples.get(("explodes", "signals_rejected_total")) == 1.0

    def test_a_plain_rejection_does_not_touch_the_error_counter(self) -> None:
        """The control. Without it the error counter could increment on every
        rejection and the test above would still pass."""
        from algotrader.common.metrics import get_metrics

        engine = RiskEngine(checks=[RiskCheck("says_no", _always(False))])
        engine.evaluate(_rec(), _ctx())
        samples = {
            (s.labels.get("check"), s.name): s.value
            for m in get_metrics().registry.collect()
            for s in m.samples
        }
        assert samples.get(("says_no", "signals_rejected_total")) == 1.0
        assert samples.get(("says_no", "risk_check_errors_total")) is None


class TestSizingIsTheLastGate:
    def test_no_sizer_means_refusal_not_approval(self) -> None:
        """The correct behaviour for a half-assembled system. A placeholder
        that approved would be the worst possible default."""
        engine = RiskEngine(checks=[RiskCheck("ok", _always(True))], sizer=None)
        decision = engine.evaluate(_rec(), _ctx())
        assert not decision.approved
        assert "no sizer" in (decision.detail or "")

    def test_a_raising_sizer_refuses(self) -> None:
        def boom(rec, ctx):
            raise ZeroDivisionError("atr was zero")

        engine = RiskEngine(checks=[], sizer=boom)
        decision = engine.evaluate(_rec(), _ctx())
        assert not decision.approved
        assert "ZeroDivisionError" in (decision.detail or "")

    def test_a_zero_quantity_is_a_refusal_carrying_the_binding_constraint(self) -> None:
        """Not an error — every clamp applied and nothing fitted. The binding
        constraint is the explanation, so a surprisingly small position can be
        read rather than investigated."""
        engine = RiskEngine(checks=[], sizer=_sizer(quantity=0, binding="broker_margin"))
        decision = engine.evaluate(_rec(), _ctx())
        assert not decision.approved
        assert decision.reason is RejectReason.INSUFFICIENT_MARGIN
        assert "broker_margin" in (decision.detail or "")

    def test_the_control_a_clean_pipeline_approves(self) -> None:
        """Every restrictive test needs its opposite, or a frame that rejected
        everything would pass all of them."""
        engine = RiskEngine(
            checks=[RiskCheck("a", _always(True)), RiskCheck("b", _always(True))],
            sizer=_sizer(quantity=36),
        )
        decision = engine.evaluate(_rec(), _ctx())
        assert decision.approved
        assert decision.sizing is not None and decision.sizing.quantity == 36
        assert decision.checks_passed == ["a", "b"]


class TestEveryEvaluationIsExplainable:
    """Acceptance criterion 2."""

    def _captured(self, engine_kwargs: dict) -> list[dict]:
        written: list[dict] = []
        engine = RiskEngine(audit=written.append, **engine_kwargs)
        engine.evaluate(_rec(), _ctx())
        return written

    def test_a_rejection_is_written(self) -> None:
        written = self._captured(
            {"checks": [RiskCheck("kill_switch", _always(False))], "sizer": _sizer()}
        )
        assert len(written) == 1
        assert written[0]["outcome"] == "rejected"
        assert written[0]["stage"] == "kill_switch"
        assert written[0]["reason_code"] == RejectReason.KILL_SWITCH_ACTIVE.value

    def test_an_approval_is_written_too(self) -> None:
        """Recording only rejections answers 'why was this blocked?' but not
        'was this even considered?' — and the second is the question asked
        after a day with no trades."""
        written = self._captured({"checks": [], "sizer": _sizer()})
        assert len(written) == 1
        assert written[0]["outcome"] == "approved"
        assert written[0]["payload"]["quantity"] == 36

    def test_the_entry_carries_the_correlation_id(self) -> None:
        """Without it the decision cannot be joined to the trigger that caused
        it, and the audit chain stops being a chain."""
        written = self._captured({"checks": [], "sizer": _sizer()})
        assert written[0]["correlation_id"] == CID

    def test_an_unwritable_audit_log_does_not_swallow_the_decision(self) -> None:
        """The caller still has to act on the answer. AuditWriter already
        buffers to disk when the database is down."""

        def explode(entry):
            raise OSError("disk full")

        engine = RiskEngine(checks=[], sizer=_sizer(), audit=explode)
        assert engine.evaluate(_rec(), _ctx()).approved

    def test_the_stage_fits_the_audit_column(self) -> None:
        """decision_log.stage is String(28). A longer value fails the insert at
        the exact moment a rejection needs recording."""
        written = self._captured(
            {"checks": [RiskCheck("squareoff_runway", _always(False))], "sizer": _sizer()}
        )
        assert len(str(written[0]["stage"])) <= MAX_CHECK_ID


class TestCheckRegistrationIsValidated:
    def test_an_id_longer_than_the_audit_column_is_refused_at_declaration(self) -> None:
        """Three of the spec's fourteen names are longer than String(28) —
        check_time_to_squareoff_deadline is 32. Catching it here means it fails
        when the check is written, not at 09:20 on the first rejection."""
        with pytest.raises(RiskCheckError, match="String\\(28\\)"):
            RiskCheck("check_time_to_squareoff_deadline", _always(True))

    def test_an_empty_id_is_refused(self) -> None:
        with pytest.raises(RiskCheckError, match="must have an id"):
            RiskCheck("", _always(True))

    @pytest.mark.parametrize("bad", ["Kill_Switch", "kill switch"])
    def test_ids_are_lowercase_without_spaces(self, bad: str) -> None:
        with pytest.raises(RiskCheckError, match="lowercase"):
            RiskCheck(bad, _always(True))

    def test_duplicate_ids_are_refused(self) -> None:
        """The audit log could not say which of the two rejected an order."""
        with pytest.raises(RiskCheckError, match="duplicate"):
            RiskEngine(checks=[RiskCheck("dup", _always(True)), RiskCheck("dup", _always(True))])

    def test_a_failing_outcome_must_carry_a_reason(self) -> None:
        with pytest.raises(RiskCheckError, match="RejectReason"):
            CheckOutcome(passed=False)

    def test_a_failing_outcome_must_carry_a_detail(self) -> None:
        """The reason code is the category; the detail is what an operator
        acts on."""
        with pytest.raises(RiskCheckError, match="detail"):
            CheckOutcome(passed=False, reason=RejectReason.DAILY_LOSS_LIMIT)


class TestTheDecisionConstructors:
    def test_approve_requires_sizing_structurally(self) -> None:
        with pytest.raises(ValueError, match="must carry sizing"):
            RiskDecision(approved=True, evaluated_at=NOW)

    def test_reject_requires_a_reason_structurally(self) -> None:
        with pytest.raises(ValueError, match="must carry a reject reason"):
            RiskDecision(approved=False, evaluated_at=NOW)

    def test_approve_produces_a_coherent_decision(self) -> None:
        decision = RiskDecision.approve(_sizing(), checks_passed=["a"], now=NOW)
        assert decision.approved and decision.reason is None

    def test_reject_produces_a_coherent_decision(self) -> None:
        decision = RiskDecision.reject(
            RejectReason.NO_SLOT_AVAILABLE, detail="5 of 5 used", checks_passed=[], now=NOW
        )
        assert not decision.approved and decision.sizing is None

    def test_checks_passed_is_copied_not_aliased(self) -> None:
        """A caller mutating its list afterwards must not rewrite history in a
        decision already recorded."""
        passed = ["a"]
        decision = RiskDecision.approve(_sizing(), checks_passed=passed, now=NOW)
        passed.append("b")
        assert decision.checks_passed == ["a"]


class TestTheContextRefusesIncoherentState:
    def test_a_naive_now_is_refused(self) -> None:
        with pytest.raises(RiskContextError, match="timezone-aware"):
            _ctx(now=dt.datetime(2026, 8, 25, 5, 30))

    def test_a_naive_deadline_is_refused(self) -> None:
        with pytest.raises(RiskContextError, match="timezone-aware"):
            _ctx(squareoff_deadline=dt.datetime(2026, 8, 25, 9, 40))

    def test_nonpositive_capital_is_refused(self) -> None:
        with pytest.raises(RiskContextError, match="capital"):
            _ctx(capital=Decimal(0))

    def test_more_slots_used_than_exist_is_refused(self) -> None:
        """Sizing would divide capital that does not exist."""
        with pytest.raises(RiskContextError, match="slots_used"):
            _ctx(slots_total=3, slots_used=4)

    def test_require_refuses_a_missing_value_rather_than_defaulting(self) -> None:
        """An unreachable broker becoming 'margin: 0' looks like caution;
        becoming 'margin: unlimited' looks like nothing at all until the
        statement arrives."""
        ctx = _ctx(available_margin=None)
        with pytest.raises(RiskContextError, match="unavailable"):
            ctx.require(ctx.available_margin, "live broker margin")

    def test_require_passes_a_present_value_through(self) -> None:
        ctx = _ctx(available_margin=Decimal("100000"))
        assert ctx.require(ctx.available_margin, "margin") == Decimal("100000")


class TestPortfolioArithmetic:
    def _two_sided(self) -> RiskContext:
        return _ctx(
            open_positions=(
                OpenPosition("TCS", Direction.LONG, 10, Decimal("100"), Decimal("95"), "IT"),
                OpenPosition("SBIN", Direction.SHORT, 20, Decimal("50"), Decimal("55"), "BANK"),
            ),
            slots_total=5,
            slots_used=2,
        )

    def test_net_exposure_carries_direction(self) -> None:
        """A long and a short of equal size are close to flat, not double
        exposed. Summing absolute notionals would say the opposite."""
        assert self._two_sided().net_exposure() == Decimal(0)

    def test_gross_exposure_does_not(self) -> None:
        assert self._two_sided().gross_exposure() == Decimal(2000)

    def test_sector_exposure_is_per_sector(self) -> None:
        ctx = self._two_sided()
        assert ctx.sector_exposure("IT") == Decimal(1000)
        assert ctx.sector_exposure("PHARMA") == Decimal(0)

    def test_an_unknown_sector_contributes_nothing(self) -> None:
        assert self._two_sided().sector_exposure(None) == Decimal(0)

    def test_holds_finds_an_existing_position(self) -> None:
        ctx = self._two_sided()
        assert ctx.holds("TCS") and not ctx.holds("INFY")

    def test_slots_available_never_goes_negative(self) -> None:
        assert self._two_sided().slots_available == 3

    def test_minutes_to_squareoff(self) -> None:
        assert _ctx().minutes_to_squareoff == pytest.approx(250.0)
