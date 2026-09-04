"""The ordered, fail-fast risk pipeline (E14-S01).

``LOW_LEVEL_ARCHITECTURE.md §5.7``: *"This is the most safety-critical
component. It is entirely deterministic — no AI, no randomness, no network
calls except to the broker."* The checks themselves are E14-S02..S06; this is
the frame they run in, and the two acceptance criteria are both properties of
the frame rather than of any check:

* **No order path bypasses the pipeline.** Enforced structurally — an approved
  :class:`~algotrader.common.models.trading.RiskDecision` requires a
  ``SizingResult``, only :meth:`RiskEngine.evaluate` produces one, and
  ``tests/security/test_risk_pipeline_integrity.py`` asserts that nothing else
  in ``src/`` constructs an approval.
* **Any rejection is explainable from the audit log.** Every evaluation is
  written — passes included — carrying the check that stopped it, the reason
  code, the human detail, and the checks it cleared first.

**Three decisions worth knowing about.**

*Fail-fast, and the order is the design.* The first check to say no ends the
evaluation, so the order determines which rejection an operator sees. Cheap and
absolute conditions come first: there is no point computing sector exposure for
a symbol the kill switch has already ruled out.

*A check that RAISES is a rejection, not a skip.* The obvious implementation
lets the exception propagate and the caller decides; the tempting one logs and
continues. Both are wrong. Propagating kills the evaluation of every other
candidate in the batch, and continuing means a broken check silently stops
being a check — the pipeline keeps approving, with one fewer gate, and nothing
in the output says so. Errors are counted on their own metric so a fault never
hides inside the rejection rate.

*An engine fault is not a business rejection.* Every ``RejectReason`` except
one means "the system worked and the answer is no". ``RISK_ENGINE_FAULT``
means "the system is broken, and refusing is the safe reading of that" — a
check raised, or sizing was unavailable or raised. The distinction is not
cosmetic: ``signals_rejected_total{reason}`` is the dashboard an operator
glances at to answer "why isn't it trading?", and SIT-001 found this frame
reporting those three faults as ``HEALTH_GATE_FAILED``, which sends someone
looking for a downed service that is perfectly healthy.

*Check IDs are short and stable, not function names.* ``decision_log.stage`` is
``String(28)`` and three of the spec's fourteen names are longer than that —
``check_time_to_squareoff_deadline`` is 32 characters. Using the function name
would make the audit insert fail at rejection time, which is precisely when the
record is wanted.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from algotrader.common.enums import RejectReason
from algotrader.common.metrics import get_metrics
from algotrader.common.models.trading import Recommendation, RiskDecision, SizingResult
from algotrader.common.text import one_safe_line
from algotrader.execution.risk.context import RiskContext

log = logging.getLogger(__name__)

#: ``decision_log.stage`` is ``String(28)``. Enforced at registration so a
#: too-long id fails when the check is DECLARED, not when it first rejects
#: something at 09:20.
MAX_CHECK_ID = 28

#: ``decision_log.reason_code`` is ``String(48)``.
MAX_REASON_CODE = 48


class RiskCheckError(RuntimeError):
    """A check could not reach a verdict. Always becomes a rejection."""


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """One check's verdict.

    ``passed`` is the whole answer; ``reason`` and ``detail`` exist only to
    explain a refusal. There is deliberately no third state — unlike the
    strategy evaluator, a risk check that cannot decide must REJECT rather than
    return UNKNOWN. The strategy layer declines to trade on missing data; the
    risk layer must actively refuse it, and collapsing those two into one
    vocabulary would make "I don't know" look like "no objection".

    The detail is normalised at construction — one line, bounded length — so
    that no check can emit text that forges a log line or floods the audit
    payload. See :func:`~algotrader.common.text.one_safe_line`, which is
    shared with :class:`RiskDecision` — QA-SEC-38 found that the sizer
    writes a detail through THAT constructor, never through this one.
    """

    passed: bool
    reason: RejectReason | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.passed and self.reason is None:
            raise RiskCheckError("a failing check must carry a RejectReason")
        if not self.passed and not self.detail:
            raise RiskCheckError(
                "a failing check must carry a detail — the reason code is the "
                "category, the detail is what an operator acts on"
            )
        if self.detail:
            # frozen dataclass: normalise through object.__setattr__, so every
            # construction path gets it rather than only the `fail` classmethod.
            object.__setattr__(self, "detail", one_safe_line(self.detail))

    @classmethod
    def ok(cls) -> CheckOutcome:
        return cls(passed=True)

    @classmethod
    def fail(cls, reason: RejectReason, detail: str) -> CheckOutcome:
        return cls(passed=False, reason=reason, detail=detail)


class CheckFn(Protocol):
    def __call__(self, rec: Recommendation, ctx: RiskContext) -> CheckOutcome: ...


@dataclass(frozen=True, slots=True)
class RiskCheck:
    """A named check, with the id that reaches the audit log."""

    id: str
    fn: CheckFn
    description: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            raise RiskCheckError("a risk check must have an id")
        if len(self.id) > MAX_CHECK_ID:
            raise RiskCheckError(
                f"check id {self.id!r} is {len(self.id)} characters; "
                f"decision_log.stage is String({MAX_CHECK_ID}). A longer id "
                f"fails the audit insert at the moment a rejection happens."
            )
        if self.id != self.id.lower() or " " in self.id:
            raise RiskCheckError(f"check id {self.id!r} must be lowercase, no spaces")


#: What the engine reports as the stopping check when a check itself raised.
ERRORED = "check_errored"

#: The one binding-constraint name this frame has to recognise, to tell a
#: margin-bound zero from any other zero.
#:
#: Duplicated as a literal rather than imported from
#: :mod:`algotrader.execution.sizer`, because the sizer is injected — the
#: frame must not depend on any particular one. ``test_risk_framework.py``
#: asserts the two agree, so the duplication cannot drift silently.
MARGIN_CAP = "margin_cap"


@dataclass
class RiskEngine:
    """Converts a ``Recommendation`` into a ``RiskDecision``. The only path.

    The sizer is injected rather than constructed here: E14-S07 owns it, and
    the framework has to be testable before it exists. ``None`` means the
    engine can only reject — which is the correct behaviour for a
    half-assembled system, and better than a placeholder that approves.
    """

    checks: Sequence[RiskCheck] = ()
    #: ``(rec, ctx) -> SizingResult``. E14-S07.
    sizer: Callable[[Recommendation, RiskContext], SizingResult] | None = None
    #: ``(AuditEntry) -> None``. Injected so the framework does not depend on a
    #: database being reachable; E14 wires the real ``AuditWriter``.
    audit: Callable[[dict[str, object]], None] | None = None
    _seen_ids: set[str] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        for check in self.checks:
            if check.id in self._seen_ids:
                raise RiskCheckError(
                    f"duplicate check id {check.id!r} — the audit log could not "
                    f"distinguish which of them rejected an order"
                )
            self._seen_ids.add(check.id)

    # -- the pipeline -------------------------------------------------------

    def evaluate(self, rec: Recommendation, ctx: RiskContext) -> RiskDecision:
        """Run every check in order, stopping at the first refusal."""
        metrics = get_metrics()
        started = time.perf_counter()
        passed: list[str] = []

        for check in self.checks:
            outcome = self._run(check, rec, ctx)
            if outcome.passed:
                passed.append(check.id)
                continue

            assert outcome.reason is not None  # CheckOutcome guarantees it
            decision = RiskDecision.reject(
                outcome.reason,
                detail=outcome.detail,
                checks_passed=passed,
                now=ctx.now,
            )
            metrics.rejected(check=check.id, reason=outcome.reason.value)
            self._audit(rec, decision, stopped_by=check.id, ctx=ctx)
            log.info(
                "risk REJECTED %s (%s): %s [cleared %d check(s) first]",
                rec.symbol,
                outcome.reason.value,
                outcome.detail,
                len(passed),
            )
            metrics.risk_evaluation_seconds.observe(time.perf_counter() - started)
            return decision

        decision = self._size(rec, ctx, passed)
        metrics.risk_evaluation_seconds.observe(time.perf_counter() - started)
        return decision

    def _run(self, check: RiskCheck, rec: Recommendation, ctx: RiskContext) -> CheckOutcome:
        """Execute one check. An exception becomes a refusal, never a skip."""
        try:
            return check.fn(rec, ctx)
        except Exception as exc:
            # Deliberately broad. A check that raises for ANY reason has failed
            # to be a gate, and the safe reading of "this gate is broken" is
            # "do not pass", not "pass without it".
            get_metrics().check_errored(check.id)
            log.exception(
                "risk check %r raised for %s; treating as a REJECTION",
                check.id,
                rec.symbol,
            )
            return CheckOutcome.fail(
                RejectReason.RISK_ENGINE_FAULT,
                f"check {check.id!r} raised {type(exc).__name__}: {exc}"[:400],
            )

    def _size(self, rec: Recommendation, ctx: RiskContext, passed: list[str]) -> RiskDecision:
        """Every check cleared. Size it, or refuse if sizing cannot run."""
        metrics = get_metrics()
        if self.sizer is None:
            decision = RiskDecision.reject(
                RejectReason.RISK_ENGINE_FAULT,
                detail=(
                    "every risk check passed but no sizer is configured, so no "
                    "quantity can be computed. Refusing rather than defaulting."
                ),
                checks_passed=passed,
                now=ctx.now,
            )
            metrics.rejected(check="sizer_missing", reason=decision.reason.value)  # type: ignore[union-attr]
            self._audit(rec, decision, stopped_by="sizer_missing", ctx=ctx)
            return decision

        try:
            sizing = self.sizer(rec, ctx)
        except Exception as exc:
            metrics.check_errored("sizer")
            log.exception("position sizing raised for %s; refusing the trade", rec.symbol)
            decision = RiskDecision.reject(
                RejectReason.RISK_ENGINE_FAULT,
                detail=f"sizing raised {type(exc).__name__}: {exc}"[:400],
                checks_passed=passed,
                now=ctx.now,
            )
            metrics.rejected(check="sizer", reason=decision.reason.value)  # type: ignore[union-attr]
            self._audit(rec, decision, stopped_by="sizer", ctx=ctx)
            return decision

        if sizing.quantity <= 0:
            # Not an error: every clamp applied and the answer was "nothing
            # fits". It is still a refusal, and the binding constraint is the
            # explanation an operator needs.
            #
            # The reason code follows WHICH clamp bound. Reporting a zero from
            # the position cap or from lot rounding as INSUFFICIENT_MARGIN
            # would send someone to look at funds that are perfectly healthy —
            # the SIT-001 conflation, and the reason E14-S07's AC2 ("a
            # surprisingly small position is explainable") needs two codes
            # rather than one.
            margin_bound = sizing.binding_constraint == MARGIN_CAP
            reason = (
                RejectReason.INSUFFICIENT_MARGIN
                if margin_bound
                else RejectReason.POSITION_TOO_SMALL
            )
            decision = RiskDecision.reject(
                reason,
                detail=(
                    f"sizing produced quantity 0; binding constraint was "
                    f"{sizing.binding_constraint}"
                ),
                checks_passed=passed,
                now=ctx.now,
            )
            metrics.rejected(check="sizer", reason=decision.reason.value)  # type: ignore[union-attr]
            self._audit(rec, decision, stopped_by="sizer", ctx=ctx)
            # Logged like every other rejection. Without this, the one path
            # that can refuse a candidate which cleared all fourteen checks is
            # also the only one that says nothing, and an operator watching the
            # log sees the system go quiet with no reason given.
            log.info(
                "risk REJECTED %s (%s): %s [cleared %d check(s) first]",
                rec.symbol,
                decision.reason.value,  # type: ignore[union-attr]
                decision.detail,
                len(passed),
            )
            return decision

        decision = RiskDecision.approve(sizing, checks_passed=passed, now=ctx.now)
        metrics.approved()
        self._audit(rec, decision, stopped_by=None, ctx=ctx)
        log.info(
            "risk APPROVED %s x%d (binding: %s)",
            rec.symbol,
            sizing.quantity,
            sizing.binding_constraint,
        )
        return decision

    # -- audit --------------------------------------------------------------

    def _audit(
        self,
        rec: Recommendation,
        decision: RiskDecision,
        *,
        stopped_by: str | None,
        ctx: RiskContext,
    ) -> None:
        """Write the evaluation. Approvals too, not only refusals.

        Recording only rejections would make the log answer "why was this
        blocked?" and not "was this even considered?" — and the second question
        is the one asked after a day with no trades.
        """
        if self.audit is None:
            return
        reason_code = decision.reason.value if decision.reason else None
        if reason_code and len(reason_code) > MAX_REASON_CODE:  # pragma: no cover
            reason_code = reason_code[:MAX_REASON_CODE]
        entry = {
            "correlation_id": rec.correlation_id,
            "stage": stopped_by or "risk_approved",
            "outcome": "approved" if decision.approved else "rejected",
            "service": "execution-svc",
            "reason_code": reason_code,
            "payload": {
                "symbol": rec.symbol,
                "strategy_id": rec.strategy_id,
                "direction": rec.direction.value,
                "trigger_price": str(rec.trigger_price),
                "suggested_stop": str(rec.suggested_stop),
                "checks_passed": decision.checks_passed,
                "detail": decision.detail,
                "quantity": decision.sizing.quantity if decision.sizing else None,
                "binding_constraint": (
                    decision.sizing.binding_constraint if decision.sizing else None
                ),
            },
            "ts": ctx.now,
        }
        try:
            self.audit(entry)
        except Exception:
            # An unwritable audit log must not stop the engine returning a
            # decision — the caller still has to act on it. It is loud, and
            # AuditWriter already buffers to disk when the database is down.
            log.exception("failed to write the risk decision for %s to the audit log", rec.symbol)

    # -- introspection ------------------------------------------------------

    @property
    def check_ids(self) -> tuple[str, ...]:
        return tuple(c.id for c in self.checks)

    def describe(self) -> str:
        """The pipeline, in order. For the health panel and for a reviewer."""
        lines = [f"{i + 1:2}. {c.id:<28} {c.description}" for i, c in enumerate(self.checks)]
        return "\n".join(lines) or "(no checks registered)"


# NOTE: there is deliberately no `utc_now()` helper here. It existed briefly and
# tests/security/test_risk_pipeline_integrity.py rejected it: a clock read
# anywhere in this module weakens the property the module is supposed to have.
# The caller building a RiskContext supplies `now` explicitly, which is what
# makes an evaluation reproducible from its recorded inputs.
