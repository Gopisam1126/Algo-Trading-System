"""SIT: the pre-condition gates across a whole simulated trading session.

The unit tests for E14-S02 assert each check at chosen moments. This asks the
question those cannot: **walk the clock from before dawn to after the close,
one minute at a time, and is the set of minutes on which this system will
consider taking risk exactly the set it should be?**

That phrasing matters. A piecewise test says "09:17 is blocked" and "10:00
passes". It does not say the gates *compose* into one contiguous tradable
window with no hole in the middle and no leak at either end. Two individually
correct checks can still leave a gap between them, and the only way to see it
is to enumerate the day.

Everything here is pure computation over a frozen clock, so it needs no
container, no credentials and no network. The engine is deliberately built
with **no sizer**, which is the truth about the system today: ten of the
fourteen checks are unwritten, so nothing can be approved, and SIT should
assert that rather than paper over it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import logging
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from algotrader.broker.adapter import AmbiguousOrderError, BrokerPosition, OrderRejectedError
from algotrader.broker.kite.mapping import broker_tag
from algotrader.common.calendar import IST, MarketCalendar, load_holidays_with_status
from algotrader.common.enums import (
    AIVerdict,
    Direction,
    ExitReason,
    OrderIntent,
    OrderStatus,
    OrderType,
    Product,
    RejectReason,
    Side,
)
from algotrader.common.metrics import get_metrics, reset_metrics_for_testing
from algotrader.common.models.trading import Order, OrderRequest, Position, Recommendation
from algotrader.execution.gateway import (
    GatewayError,
    GatewayPolicy,
    OrderGateway,
    client_order_id,
)
from algotrader.execution.halt import (
    HaltController,
    HaltReason,
    OperatorAction,
)
from algotrader.execution.order_state import IllegalTransitionError
from algotrader.execution.positions import (
    ExitingPosition,
    PositionAlreadyHeldError,
    PositionManager,
    ProtectedPosition,
    UnverifiedPosition,
)
from algotrader.execution.protective_stop import (
    NakedPositionError,
    ProtectiveStop,
    StopNotEstablishedError,
    stop_client_order_id,
)
from algotrader.execution.reconciliation import Drift, Reconciler
from algotrader.execution.risk.checks import (
    ELIGIBILITY_ORDER,
    EXPOSURE_ORDER,
    LOSS_ORDER,
    PRECONDITION_ORDER,
    all_check_ids,
    build_eligibility_checks,
    build_exposure_checks,
    build_loss_checks,
    build_margin_timing_checks,
    build_precondition_checks,
)
from algotrader.execution.risk.context import OpenPosition, RiskContext
from algotrader.execution.risk.framework import RiskCheck, RiskEngine
from algotrader.execution.sizer import (
    NET_EXPOSURE_CAP,
    SECTOR_CAP,
    SizingPolicy,
    build_sizer,
)

# --------------------------------------------------------------------------
# The session under test
# --------------------------------------------------------------------------

#: Tuesday 25 Aug 2026 — an ordinary session, on no holiday list.
TRADING_DAY = dt.date(2026, 8, 25)
#: Sunday.
WEEKEND_DAY = dt.date(2026, 8, 23)
#: Two real 2026 NSE closures, from different causes: one from the annual
#: circular, one a separate special closure for the Maharashtra municipal
#: elections. B3's research turned on exactly this distinction.
HOLIDAY_CIRCULAR = dt.date(2026, 12, 25)
HOLIDAY_SPECIAL = dt.date(2026, 1, 15)

#: What system.yaml configures.
NO_TRADE_WINDOWS = (
    (dt.time(9, 15), dt.time(9, 20)),
    (dt.time(15, 0), dt.time(15, 30)),
)

#: The window a correct system should trade in: the continuous session
#: (09:15–15:30) minus the opening noise and the closing blackout. Stated here
#: as the EXPECTED ANSWER, independently of any check, so the test compares the
#: system against the intent rather than against itself.
EXPECTED_FIRST_TRADABLE = dt.time(9, 20)
EXPECTED_LAST_TRADABLE = dt.time(14, 59)


@pytest.fixture(autouse=True)
def _fresh_metrics() -> None:
    reset_metrics_for_testing()


#: ONE event loop for the whole module, not one per call.
#:
#: Every ``asyncio.run`` builds a new event loop, and on Windows every new loop
#: builds its self-pipe with ``socket.socketpair()`` - which Windows lacks, so
#: CPython emulates it with a real loopback TCP connection and then waits in
#: ``accept()`` with NO timeout. This module walks sessions minute by minute and
#: decision by decision, so it created thousands of loops per test; eventually
#: one loopback connect never arrived and the run hung forever at zero CPU. The
#: faulthandler dump showed exactly that: a listener on 127.0.0.1 in LISTEN,
#: no peer connected, the test blocked in ``socketpair -> accept``.
#:
#: One loop removes the cause rather than the symptom, and is also closer to
#: production, which runs one long-lived loop. Linux CI never saw this because
#: Linux has a real ``socketpair`` syscall.
_RUNNER: asyncio.Runner | None = None


def _run_async(coro):
    """``asyncio.run`` on the module's single loop."""
    global _RUNNER
    if _RUNNER is None:
        _RUNNER = asyncio.Runner()
    return _RUNNER.run(coro)


@pytest.fixture(scope="module", autouse=True)
def _one_loop_per_module():
    yield
    global _RUNNER
    if _RUNNER is not None:
        _RUNNER.close()
        _RUNNER = None


@pytest.fixture(scope="module")
def calendar() -> MarketCalendar:
    """The real shipped holiday list. A stub here would make the holiday
    scenarios test the stub."""
    path = Path(__file__).resolve().parents[2] / "config" / "nse_holidays.yaml"
    status = load_holidays_with_status(str(path))
    return MarketCalendar(status.dates, covers_years=status.covers_years)


@pytest.fixture(scope="module")
def session(calendar) -> Session:
    """One full ordinary session, walked once and shared.

    Module-scoped rather than class-scoped: a class-scoped fixture written as
    an instance method is deprecated in pytest and removed in 10, and the walk
    is read-only once it has run.
    """
    return Session(calendar, TRADING_DAY).run()


def _at(day: dt.date, hour: int, minute: int) -> dt.datetime:
    """An IST wall-clock moment, carried as UTC — how the system holds it."""
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST).astimezone(dt.UTC)


def _recommendation(now: dt.datetime, symbol: str = "INFY") -> Recommendation:
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
        ai_rationale="session replay",
        emitted_at=now,
    )


class Session:
    """One simulated trading day, evaluated minute by minute.

    Holds the audit sink and the captured log so the cross-cutting assertions
    can be made over the WHOLE run rather than over one decision.
    """

    def __init__(
        self,
        calendar: MarketCalendar,
        day: dt.date,
        *,
        checks: Sequence[RiskCheck] | None = None,
        **ctx_overrides: object,
    ):
        self.day = day
        self.audit: list[dict[str, object]] = []
        built = (
            tuple(checks)
            if checks is not None
            else build_precondition_checks(calendar, NO_TRADE_WINDOWS)
        )
        #: What "cleared everything" means for THIS engine. Derived from the
        #: checks actually registered rather than hardcoded, so a walk with the
        #: seven-check pipeline and a walk with four are both expressible.
        self.expected_order = tuple(c.id for c in built)
        self.engine = RiskEngine(checks=built, audit=self.audit.append)
        #: 15:05 IST — the CAS deadline (15:10) minus the 5-minute exit
        #: buffer. Overridable, so a non-CAS name's later deadline can be
        #: walked as its own session.
        self.deadline = ctx_overrides.pop("squareoff_deadline", None) or _at(day, 15, 5)
        self.ctx_overrides = ctx_overrides
        self.decisions: dict[dt.time, object] = {}
        self.log_text = ""

    def run(self, start=(8, 0), end=(16, 0), *, mutate=None):
        """Walk the clock. ``mutate(ist_time, ctx_kwargs)`` may alter the
        context at a given minute — that is how a mid-session outage is
        expressed."""
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        root = logging.getLogger("algotrader")
        root.addHandler(handler)
        previous = root.level
        root.setLevel(logging.INFO)
        try:
            moment = _at(self.day, *start)
            stop = _at(self.day, *end)
            while moment < stop:
                ist_time = moment.astimezone(IST).time()
                kwargs: dict[str, object] = {
                    # The session risk state, stated rather than defaulted (AUDIT-005).
                    # "Nothing is halted" is a claim these tests rely on, so they say it.
                    "kill_switch_active": False,
                    "unhealthy_services": (),
                    "realised_pnl_today": Decimal(0),
                    "consecutive_losses": 0,
                    "daily_loss_halted": False,
                    "consecutive_loss_halted": False,
                    "now": moment,
                    "squareoff_deadline": self.deadline,
                    "capital": Decimal("500000"),
                    "slots_total": 5,
                    "slots_used": 0,
                    # `()` is "checked, and clean" -- NOT None, which means
                    # eligibility was never established and must reject.
                    "symbol_restrictions": (),
                }
                kwargs.update(self.ctx_overrides)
                if mutate is not None:
                    mutate(ist_time, kwargs)
                ctx = RiskContext(**kwargs)  # type: ignore[arg-type]
                self.decisions[ist_time] = self.engine.evaluate(_recommendation(moment), ctx)
                moment += dt.timedelta(minutes=1)
        finally:
            root.removeHandler(handler)
            root.setLevel(previous)
            self.log_text = buf.getvalue()
        return self

    # -- views over the run -------------------------------------------------

    def fully_cleared(self) -> list[dt.time]:
        """Minutes where every registered gate passed. With no sizer configured
        the engine then refuses, so 'cleared' is read from checks_passed rather
        than from approval — the honest reading of a half-built engine."""
        return sorted(
            t
            for t, d in self.decisions.items()
            if list(d.checks_passed) == list(self.expected_order)  # type: ignore[attr-defined]
        )

    def stopped_by(self) -> dict[dt.time, str]:
        """Which check actually refused each minute, from the audit `stage`.

        The reason code alone is not enough to answer this. With no sizer
        configured a fully-cleared candidate is refused by the sizer, which
        carries RISK_ENGINE_FAULT -- the same code a raising check produces.
        Filtering a session walk on the reason therefore sweeps up every clean
        minute as well, which is what this view exists to avoid.

        Audit entries are appended in evaluation order, one per decision, so
        they zip with the decisions in insertion order.
        """
        return {
            moment: str(entry["stage"])
            for moment, entry in zip(self.decisions, self.audit, strict=True)
        }

    def reasons(self) -> dict[dt.time, RejectReason | None]:
        return {t: d.reason for t, d in self.decisions.items()}  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# SIT-1 — realistic session replay
# --------------------------------------------------------------------------


class TestSit1TheShapeOfTheTradingDay:
    def test_the_tradable_window_is_contiguous_with_no_hole(self, session: Session) -> None:
        """The claim no piecewise test makes. If two gates left a gap anywhere
        in the middle of the day, this is what would see it."""
        cleared = session.fully_cleared()
        assert cleared, "no minute of an ordinary session was tradable"
        first, last = cleared[0], cleared[-1]
        expected_run = []
        m = dt.datetime.combine(TRADING_DAY, first)
        while m.time() <= last:
            expected_run.append(m.time())
            m += dt.timedelta(minutes=1)
        assert cleared == expected_run, (
            f"the tradable window has a hole in it: {sorted(set(expected_run) - set(cleared))}"
        )

    def test_the_window_starts_and_ends_where_it_should(self, session: Session) -> None:
        cleared = session.fully_cleared()
        assert cleared[0] == EXPECTED_FIRST_TRADABLE
        assert cleared[-1] == EXPECTED_LAST_TRADABLE

    def test_it_is_339_minutes_and_that_number_is_derived_not_guessed(
        self, session: Session
    ) -> None:
        """09:20 to 15:00 exclusive. Stated as a count because an off-by-one at
        either boundary changes it and nothing else would."""
        assert len(session.fully_cleared()) == 340

    @pytest.mark.parametrize(
        ("hour", "minute", "expected"),
        [
            (8, 0, RejectReason.OUTSIDE_TRADING_WINDOW),  # long before open
            (9, 14, RejectReason.OUTSIDE_TRADING_WINDOW),  # one minute early
            (9, 15, RejectReason.NO_TRADE_WINDOW),  # open, but opening noise
            (9, 19, RejectReason.NO_TRADE_WINDOW),  # last blackout minute
            (9, 20, None),  # first tradable minute
            (14, 59, None),  # last tradable minute
            (15, 0, RejectReason.NO_TRADE_WINDOW),  # closing blackout begins
            (15, 29, RejectReason.NO_TRADE_WINDOW),  # last blackout minute
            (15, 30, RejectReason.OUTSIDE_TRADING_WINDOW),  # closed
            (15, 45, RejectReason.OUTSIDE_TRADING_WINDOW),
        ],
    )
    def test_each_boundary_gives_the_reason_an_operator_needs(
        self, session: Session, hour: int, minute: int, expected: RejectReason | None
    ) -> None:
        """Not just blocked/allowed — *which* reason. "outside trading hours"
        and "we are choosing to sit this out" mean different things at 09:17,
        and an operator acts on them differently."""
        decision = session.decisions[dt.time(hour, minute)]
        if expected is None:
            assert list(decision.checks_passed) == list(PRECONDITION_ORDER)  # type: ignore[attr-defined]
        else:
            assert decision.reason is expected  # type: ignore[attr-defined]

    def test_nothing_was_ever_approved(self, session: Session) -> None:
        """The honest state of the system. Ten checks are missing and there is
        no sizer, so an approval anywhere in a 480-minute walk would mean the
        pipeline can be cleared by four gates out of fourteen."""
        approved = [t for t, d in session.decisions.items() if d.approved]  # type: ignore[attr-defined]
        assert approved == [], f"the engine approved something at {approved}"


# --------------------------------------------------------------------------
# SIT-2 — degraded dependencies
# --------------------------------------------------------------------------


class TestSit2WhenSomethingBreaksMidSession:
    def test_an_outage_blocks_exactly_its_own_duration_and_then_releases(self, calendar) -> None:
        """Both halves matter. A gate that never released after a transient
        outage would silently end the trading day, and nothing in the logs
        would say the outage was over."""

        def outage(ist_time: dt.time, kwargs: dict) -> None:
            if dt.time(11, 0) <= ist_time < dt.time(11, 30):
                kwargs["unhealthy_services"] = ("ingest-svc",)

        session = Session(calendar, TRADING_DAY).run(mutate=outage)
        blocked = {
            t
            for t, d in session.decisions.items()
            if d.reason is RejectReason.HEALTH_GATE_FAILED  # type: ignore[attr-defined]
        }
        expected = {dt.time(11, m) for m in range(0, 30)}
        assert blocked == expected, f"unexpected: {blocked ^ expected}"
        assert dt.time(11, 30) in session.fully_cleared(), (
            "the health gate did not release after the service recovered"
        )

    def test_the_kill_switch_overrides_a_moment_that_would_otherwise_trade(self, calendar) -> None:
        session = Session(calendar, TRADING_DAY, kill_switch_active=True).run(
            start=(9, 20), end=(9, 30)
        )
        assert session.fully_cleared() == []
        assert all(
            d.reason is RejectReason.KILL_SWITCH_ACTIVE  # type: ignore[attr-defined]
            for d in session.decisions.values()
        )

    def test_a_check_that_raises_becomes_a_rejection_not_a_gap(self, calendar) -> None:
        """The fail-closed property, exercised through the assembled engine
        rather than the framework in isolation. A broken gate must not silently
        stop being a gate."""
        from algotrader.execution.risk.framework import RiskCheck

        def explodes(rec, ctx):
            raise RuntimeError("the health probe socket is gone")

        checks = list(build_precondition_checks(calendar, NO_TRADE_WINDOWS))
        checks.insert(0, RiskCheck(id="exploding_probe", fn=explodes))
        engine = RiskEngine(checks=checks)
        now = _at(TRADING_DAY, 10, 0)
        decision = engine.evaluate(
            _recommendation(now),
            RiskContext(
                # Session risk state, stated rather than defaulted (AUDIT-005).
                kill_switch_active=False,
                unhealthy_services=(),
                realised_pnl_today=Decimal(0),
                consecutive_losses=0,
                daily_loss_halted=False,
                consecutive_loss_halted=False,
                now=now,
                squareoff_deadline=_at(TRADING_DAY, 15, 10),
                capital=Decimal("500000"),
                slots_total=5,
                slots_used=0,
            ),
        )
        assert not decision.approved
        assert decision.reason is RejectReason.RISK_ENGINE_FAULT
        assert "exploding_probe" in decision.detail

    def test_an_engine_fault_is_not_reported_as_a_downed_service(self, calendar) -> None:
        """SIT-001, stated as the property rather than the fix. A fault in the
        engine and a component being down are different situations with
        different responses, and the reason code is what an operator sees
        first."""
        from algotrader.execution.risk.framework import RiskCheck

        def explodes(rec, ctx):
            raise RuntimeError("the health probe socket is gone")

        now = _at(TRADING_DAY, 10, 0)
        ctx = RiskContext(
            # Session risk state, stated rather than defaulted (AUDIT-005).
            kill_switch_active=False,
            unhealthy_services=(),
            realised_pnl_today=Decimal(0),
            consecutive_losses=0,
            daily_loss_halted=False,
            consecutive_loss_halted=False,
            now=now,
            squareoff_deadline=_at(TRADING_DAY, 15, 10),
            capital=Decimal("500000"),
            slots_total=5,
            slots_used=0,
        )
        faulty = RiskEngine(checks=[RiskCheck(id="exploding_probe", fn=explodes)]).evaluate(
            _recommendation(now), ctx
        )
        genuinely_unhealthy = RiskEngine(
            checks=build_precondition_checks(calendar, NO_TRADE_WINDOWS)
        ).evaluate(
            _recommendation(now),
            RiskContext(
                # Session risk state, stated rather than defaulted (AUDIT-005).
                kill_switch_active=False,
                realised_pnl_today=Decimal(0),
                consecutive_losses=0,
                daily_loss_halted=False,
                consecutive_loss_halted=False,
                now=now,
                squareoff_deadline=_at(TRADING_DAY, 15, 10),
                capital=Decimal("500000"),
                slots_total=5,
                slots_used=0,
                unhealthy_services=("ingest-svc",),
            ),
        )
        assert genuinely_unhealthy.reason is RejectReason.HEALTH_GATE_FAILED
        assert faulty.reason is not genuinely_unhealthy.reason

    def test_a_calendar_that_cannot_answer_refuses_rather_than_guesses(self) -> None:
        """An uncovered year is the realistic version of this: the holiday list
        is a file someone must renew each December. If it lapses, the system
        must stop, not assume every weekday is a trading day."""
        blind = MarketCalendar(frozenset(), covers_years=frozenset({2026}))
        engine = RiskEngine(checks=build_precondition_checks(blind, NO_TRADE_WINDOWS))
        now = _at(dt.date(2027, 3, 10), 10, 0)  # a Wednesday in an uncovered year
        decision = engine.evaluate(
            _recommendation(now),
            RiskContext(
                # Session risk state, stated rather than defaulted (AUDIT-005).
                kill_switch_active=False,
                unhealthy_services=(),
                realised_pnl_today=Decimal(0),
                consecutive_losses=0,
                daily_loss_halted=False,
                consecutive_loss_halted=False,
                now=now,
                squareoff_deadline=_at(dt.date(2027, 3, 10), 15, 10),
                capital=Decimal("500000"),
                slots_total=5,
                slots_used=0,
            ),
        )
        assert not decision.approved, "an unanswerable calendar let a trade through"


# --------------------------------------------------------------------------
# SIT-3 — boundary conditions of the trading day
# --------------------------------------------------------------------------


class TestSit3DaysThatAreNotOrdinary:
    @pytest.mark.parametrize(
        ("day", "label"),
        [
            (WEEKEND_DAY, "Sunday"),
            (HOLIDAY_CIRCULAR, "Christmas — annual circular"),
            (HOLIDAY_SPECIAL, "15 Jan — Maharashtra election special closure"),
        ],
    )
    def test_not_one_minute_of_a_closed_day_is_tradable(
        self, calendar, day: dt.date, label: str
    ) -> None:
        session = Session(calendar, day).run()
        assert session.fully_cleared() == [], f"{label} was tradable"
        assert all(
            d.reason is RejectReason.OUTSIDE_TRADING_WINDOW  # type: ignore[attr-defined]
            for d in session.decisions.values()
        ), label

    def test_the_ordinary_day_is_the_control(self, calendar) -> None:
        """Without this, a calendar that called every day closed would pass
        every test above."""
        assert Session(calendar, TRADING_DAY).run().fully_cleared()


# --------------------------------------------------------------------------
# SIT-4 — cross-cutting invariants, asserted over the whole run
# --------------------------------------------------------------------------


class TestSit4WhatMustHoldAcrossTheEntireSession:
    def test_every_decision_was_audited(self, session: Session) -> None:
        """ "Was this even considered?" is the question asked after a day with
        no trades, and only a complete log answers it."""
        assert len(session.audit) == len(session.decisions)

    def test_no_audit_entry_has_a_null_timestamp_or_correlation_id(self, session: Session) -> None:
        """Repudiation. A gap in the chain is worth finding here rather than
        during an incident."""
        for entry in session.audit:
            assert isinstance(entry["ts"], dt.datetime)
            assert entry["ts"].tzinfo is not None  # type: ignore[union-attr]
            assert isinstance(entry["correlation_id"], UUID)

    def test_every_audit_stage_fits_the_column_it_is_written_to(self, session: Session) -> None:
        """``decision_log.stage`` is String(28). A stage longer than that fails
        the insert at the moment a rejection happens — precisely when the
        record is wanted."""
        for entry in session.audit:
            assert len(str(entry["stage"])) <= 28, entry["stage"]

    def test_no_credential_shaped_string_reached_the_log(self, session: Session) -> None:
        """A whole simulated session's worth of log output, checked once."""
        import re

        assert not re.search(
            r"(api[_-]?key|secret|password|access[_-]?token)\s*[:=]\s*\S{8,}",
            session.log_text,
            re.IGNORECASE,
        )

    def test_no_log_line_was_forged_by_a_symbol(self, calendar) -> None:
        """QA-SEC-28 at session scale: a full day of rejections must produce
        exactly one line each, so the line count is the decision count."""
        session = Session(calendar, TRADING_DAY).run(start=(8, 0), end=(9, 0))
        lines = [ln for ln in session.log_text.splitlines() if ln.strip()]
        assert len(lines) == 60, f"60 rejections produced {len(lines)} log lines"

    def test_the_recommendation_never_gained_a_sizing_field(self) -> None:
        """Constraint C4, checked in the assembled path rather than only in the
        model's own test file."""
        fields = set(Recommendation.model_fields)
        assert not fields & {
            "quantity",
            "capital_at_risk",
            "stop_price",
            "position_size",
            "notional",
        }


# --------------------------------------------------------------------------
# SIT-5 — idempotency and restart
# --------------------------------------------------------------------------


class TestSit5ReplayAndRestart:
    def test_the_same_session_replayed_twice_decides_identically(self, calendar) -> None:
        """The property that makes an incident reconstructable: given the same
        inputs the engine must reach the same answers, with no dependence on
        wall-clock time or ordering."""
        first = Session(calendar, TRADING_DAY).run()
        second = Session(calendar, TRADING_DAY).run()
        assert first.reasons() == second.reasons()
        assert first.fully_cleared() == second.fully_cleared()

    def test_a_restarted_engine_agrees_with_one_that_has_run_all_day(self, calendar) -> None:
        """The restart case. A gate that accumulated state — a cached calendar
        answer, a counter, a memo of the last verdict — would drift from a
        freshly started one, and the drift would only appear after a crash.
        """
        all_day = Session(calendar, TRADING_DAY).run()
        fresh = Session(calendar, TRADING_DAY).run(start=(14, 55), end=(15, 5))
        for moment, decision in fresh.decisions.items():
            assert decision.reason is all_day.decisions[moment].reason, moment  # type: ignore[attr-defined]
            assert (
                list(decision.checks_passed)  # type: ignore[attr-defined]
                == list(all_day.decisions[moment].checks_passed)  # type: ignore[attr-defined]
            ), moment


# --------------------------------------------------------------------------
# SIT-6 — eligibility across a session (E14-S03)
# --------------------------------------------------------------------------


def _seven(calendar) -> tuple[RiskCheck, ...]:
    """The full built pipeline: four pre-conditions then three eligibility."""
    return (
        *build_precondition_checks(calendar, NO_TRADE_WINDOWS),
        *build_eligibility_checks(),
    )


class TestSit6EligibilityAcrossASession:
    """Checks 5-7 walked through a whole day, alongside the four that precede
    them. The question these ask that the unit tests cannot: does adding three
    gates change the SHAPE of the trading day, and do they release when the
    condition that tripped them clears?"""

    def test_adding_three_gates_did_not_narrow_the_trading_day(self, calendar) -> None:
        """The composition property. A clean candidate must clear all seven on
        exactly the minutes it cleared four — if eligibility silently shaved a
        minute off either end, that is a gate interacting with the clock, which
        none of these three has any business doing."""
        four = Session(calendar, TRADING_DAY).run()
        seven = Session(calendar, TRADING_DAY, checks=_seven(calendar)).run()
        assert seven.fully_cleared() == four.fully_cleared()
        assert len(seven.fully_cleared()) == 340

    def test_a_symbol_banned_mid_session_blocks_from_that_minute_on(self, calendar) -> None:
        """The case the design exists for: eligibility is re-read at order time
        precisely because a symbol can enter a ban list intraday. The plan
        built at 09:00 was right when it was built."""

        def bans_at_noon(ist_time: dt.time, kwargs: dict) -> None:
            if ist_time >= dt.time(12, 0):
                kwargs["symbol_restrictions"] = ("ASM_ST_1",)

        session = Session(calendar, TRADING_DAY, checks=_seven(calendar)).run(mutate=bans_at_noon)
        cleared = session.fully_cleared()
        assert cleared[-1] == dt.time(11, 59), "trading continued past the ban"
        assert cleared[0] == dt.time(9, 20), "the morning was affected too"
        assert session.decisions[dt.time(12, 0)].reason is RejectReason.SYMBOL_NOT_TRADABLE

    def test_eligibility_going_unavailable_blocks_and_then_releases(self, calendar) -> None:
        """Both halves. A fetcher that dropped out for half an hour must stop
        trading for exactly that half hour — and must not leave the gate stuck
        closed for the rest of the day once it returns."""

        def outage(ist_time: dt.time, kwargs: dict) -> None:
            if dt.time(11, 0) <= ist_time < dt.time(11, 30):
                kwargs["symbol_restrictions"] = None

        session = Session(calendar, TRADING_DAY, checks=_seven(calendar)).run(mutate=outage)
        blocked = {
            moment for moment, stage in session.stopped_by().items() if stage == "symbol_tradable"
        }
        assert blocked == {dt.time(11, m) for m in range(30)}
        assert dt.time(11, 30) in session.fully_cleared()
        # And it is a FAULT, not a business rejection -- the fetcher is down,
        # the symbol is not banned.
        assert session.decisions[dt.time(11, 0)].reason is RejectReason.RISK_ENGINE_FAULT

    def test_unavailable_eligibility_is_not_reported_as_an_untradable_symbol(
        self, calendar
    ) -> None:
        """SIT-001's distinction holding at session scale. "This symbol is
        banned" is normal operation; "we could not find out" means a fetcher is
        down, and an operator seeing the first would never go looking for the
        second."""
        engine = RiskEngine(checks=_seven(calendar))
        now = _at(TRADING_DAY, 10, 0)
        base = {
            # The session risk state, stated rather than defaulted (AUDIT-005).
            # "Nothing is halted" is a claim these tests rely on, so they say it.
            "kill_switch_active": False,
            "unhealthy_services": (),
            "realised_pnl_today": Decimal(0),
            "consecutive_losses": 0,
            "daily_loss_halted": False,
            "consecutive_loss_halted": False,
            "now": now,
            "squareoff_deadline": _at(TRADING_DAY, 15, 10),
            "capital": Decimal("500000"),
            "slots_total": 5,
            "slots_used": 0,
        }
        unknown = engine.evaluate(
            _recommendation(now), RiskContext(**base, symbol_restrictions=None)
        )
        banned = engine.evaluate(
            _recommendation(now), RiskContext(**base, symbol_restrictions=("T2T",))
        )
        assert banned.reason is RejectReason.SYMBOL_NOT_TRADABLE
        assert unknown.reason is RejectReason.RISK_ENGINE_FAULT

    def test_the_book_filling_up_stops_trading_and_frees_again(self, calendar) -> None:
        def fills_then_frees(ist_time: dt.time, kwargs: dict) -> None:
            if dt.time(10, 0) <= ist_time < dt.time(14, 0):
                kwargs["slots_used"] = 5

        session = Session(calendar, TRADING_DAY, checks=_seven(calendar)).run(
            mutate=fills_then_frees
        )
        blocked = {
            t for t, d in session.decisions.items() if d.reason is RejectReason.NO_SLOT_AVAILABLE
        }
        assert min(blocked) == dt.time(10, 0)
        assert max(blocked) == dt.time(13, 59)
        assert dt.time(14, 0) in session.fully_cleared()

    def test_a_position_opened_mid_session_blocks_re_entry_for_the_rest_of_it(
        self, calendar
    ) -> None:
        """The 2x-risk case, walked. Every per-trade limit still reads as
        satisfied while the name would carry twice its intended risk, which is
        what makes this worth a gate of its own."""
        held = OpenPosition(
            symbol="INFY",
            direction=Direction.LONG,
            quantity=40,
            entry_price=Decimal("1200"),
            stop_price=Decimal("1186"),
        )

        def opens_at_eleven(ist_time: dt.time, kwargs: dict) -> None:
            if ist_time >= dt.time(11, 0):
                kwargs["open_positions"] = (held,)
                kwargs["slots_used"] = 1

        session = Session(calendar, TRADING_DAY, checks=_seven(calendar)).run(
            mutate=opens_at_eleven
        )
        cleared = session.fully_cleared()
        assert cleared[-1] == dt.time(10, 59)
        assert session.decisions[dt.time(11, 0)].reason is RejectReason.ALREADY_HOLDING
        # And it never releases, because the position is still open at 15:00.
        assert session.decisions[dt.time(14, 59)].reason is RejectReason.ALREADY_HOLDING

    def test_holding_a_different_name_leaves_the_day_untouched(self, calendar) -> None:
        """The control for the test above. A gate matching on the wrong thing
        would block the whole afternoon here too."""
        other = OpenPosition(
            symbol="SOMETHINGELSE",
            direction=Direction.LONG,
            quantity=10,
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
        )
        session = Session(
            calendar,
            TRADING_DAY,
            checks=_seven(calendar),
            open_positions=(other,),
            slots_used=1,
        ).run()
        assert len(session.fully_cleared()) == 340

    def test_a_precondition_still_wins_across_the_whole_day(self, calendar) -> None:
        """Order across the two groups, at every minute rather than one. With a
        banned symbol all day, every rejection outside the session must still
        name the session — the more fundamental reason."""
        session = Session(
            calendar,
            TRADING_DAY,
            checks=_seven(calendar),
            symbol_restrictions=("T2T",),
        ).run()
        assert session.decisions[dt.time(8, 0)].reason is RejectReason.OUTSIDE_TRADING_WINDOW
        assert session.decisions[dt.time(9, 17)].reason is RejectReason.NO_TRADE_WINDOW
        assert session.decisions[dt.time(10, 0)].reason is RejectReason.SYMBOL_NOT_TRADABLE
        assert session.fully_cleared() == []

    def test_every_decision_is_still_audited_with_all_seven_registered(self, calendar) -> None:
        session = Session(calendar, TRADING_DAY, checks=_seven(calendar)).run()
        assert len(session.audit) == len(session.decisions)
        for entry in session.audit:
            assert len(str(entry["stage"])) <= 28, entry["stage"]

    def test_a_hostile_restriction_label_cannot_forge_a_line_across_a_session(
        self, calendar
    ) -> None:
        """QA-SEC-30 at session scale. An hour of rejections carrying a
        newline-bearing label must still produce exactly one line each."""
        session = Session(
            calendar,
            TRADING_DAY,
            checks=_seven(calendar),
            symbol_restrictions=("T2T\nCRITICAL kill switch disarmed by operator",),
        ).run(start=(10, 0), end=(11, 0))
        lines = [line for line in session.log_text.splitlines() if line.strip()]
        assert len(lines) == 60, f"60 rejections produced {len(lines)} log lines"

    def test_the_seven_check_day_replays_identically(self, calendar) -> None:
        first = Session(calendar, TRADING_DAY, checks=_seven(calendar)).run()
        second = Session(calendar, TRADING_DAY, checks=_seven(calendar)).run()
        assert first.reasons() == second.reasons()
        assert first.fully_cleared() == second.fully_cleared()

    def test_the_full_order_is_what_a_cleared_minute_records(self, calendar) -> None:
        session = Session(calendar, TRADING_DAY, checks=_seven(calendar)).run(
            start=(10, 0), end=(10, 5)
        )
        decision = session.decisions[dt.time(10, 0)]
        assert list(decision.checks_passed) == [*PRECONDITION_ORDER, *ELIGIBILITY_ORDER]


# --------------------------------------------------------------------------
# SIT-7 — portfolio exposure across a session (E14-S04)
# --------------------------------------------------------------------------


def _ten(calendar) -> tuple[RiskCheck, ...]:
    """The full built pipeline: four pre-conditions, three eligibility, three
    exposure."""
    return (
        *build_precondition_checks(calendar, NO_TRADE_WINDOWS),
        *build_eligibility_checks(),
        *build_exposure_checks(
            max_correlated_positions=2,
            correlation_threshold=Decimal("0.7"),
            max_sector_exposure_pct=Decimal("40"),
            max_net_directional_exposure_pct=Decimal("60"),
        ),
    )


def _position(symbol: str, notional: str, sector: str | None, direction=Direction.LONG):
    price = Decimal("100")
    return OpenPosition(
        symbol=symbol,
        direction=direction,
        quantity=int(Decimal(notional) / price),
        entry_price=price,
        stop_price=Decimal("95") if direction is Direction.LONG else Decimal("105"),
        sector=sector,
    )


class TestSit7ExposureAcrossASession:
    """Checks 8-10 walked across a whole day behind the seven that precede
    them. The question the unit tests cannot ask: does the book EVOLVING
    through a session drive these gates correctly, and do they release?"""

    @staticmethod
    def _clean_ctx() -> dict:
        return {"symbol_sector": "IT", "correlations": {}}

    def test_adding_three_more_gates_did_not_narrow_the_trading_day(self, calendar) -> None:
        """The composition property again, now at ten checks. A clean
        candidate against an empty book must clear all ten on exactly the
        minutes it cleared four."""
        four = Session(calendar, TRADING_DAY).run()
        ten = Session(calendar, TRADING_DAY, checks=_ten(calendar), **self._clean_ctx()).run()
        assert ten.fully_cleared() == four.fully_cleared()
        assert len(ten.fully_cleared()) == 340

    def test_a_sector_filling_up_during_the_session_closes_the_gate(self, calendar) -> None:
        """The realistic shape: positions accumulate through the morning and
        the sector cap binds partway through."""

        def fills(ist_time: dt.time, kwargs: dict) -> None:
            if ist_time >= dt.time(12, 0):
                kwargs["open_positions"] = (_position("TCS", "200000", "IT"),)
                kwargs["slots_used"] = 1
                kwargs["correlations"] = {"TCS": Decimal("0.2")}

        session = Session(calendar, TRADING_DAY, checks=_ten(calendar), **self._clean_ctx()).run(
            mutate=fills
        )
        cleared = session.fully_cleared()
        assert cleared[-1] == dt.time(11, 59)
        assert session.decisions[dt.time(12, 0)].reason is RejectReason.SECTOR_EXPOSURE_LIMIT

    def test_a_position_closing_reopens_the_gate(self, calendar) -> None:
        """The other half, and the one a latching gate would fail. When the
        sector position is closed the system must trade again."""

        def open_then_close(ist_time: dt.time, kwargs: dict) -> None:
            if dt.time(10, 0) <= ist_time < dt.time(13, 0):
                kwargs["open_positions"] = (_position("TCS", "200000", "IT"),)
                kwargs["slots_used"] = 1
                kwargs["correlations"] = {"TCS": Decimal("0.2")}

        session = Session(calendar, TRADING_DAY, checks=_ten(calendar), **self._clean_ctx()).run(
            mutate=open_then_close
        )
        blocked = {
            moment for moment, stage in session.stopped_by().items() if stage == "sector_exposure"
        }
        assert min(blocked) == dt.time(10, 0)
        assert max(blocked) == dt.time(12, 59)
        assert dt.time(13, 0) in session.fully_cleared()

    def test_the_correlation_matrix_going_missing_mid_session_refuses(self, calendar) -> None:
        """What a failed pre-market matrix job looks like from inside a
        session. It must refuse, as a FAULT rather than a business rejection,
        and it must recover when the data returns."""

        def matrix_outage(ist_time: dt.time, kwargs: dict) -> None:
            kwargs["open_positions"] = (_position("TCS", "50000", "IT"),)
            kwargs["slots_used"] = 1
            if dt.time(11, 0) <= ist_time < dt.time(11, 30):
                kwargs["correlations"] = {}
            else:
                kwargs["correlations"] = {"TCS": Decimal("0.2")}

        session = Session(calendar, TRADING_DAY, checks=_ten(calendar), **self._clean_ctx()).run(
            mutate=matrix_outage
        )
        blocked = {
            moment for moment, stage in session.stopped_by().items() if stage == "correlation"
        }
        assert blocked == {dt.time(11, m) for m in range(30)}
        assert session.decisions[dt.time(11, 0)].reason is RejectReason.RISK_ENGINE_FAULT
        assert dt.time(11, 30) in session.fully_cleared()

    def test_an_unclassified_position_appearing_mid_session_refuses(self, calendar) -> None:
        """A sector that goes missing is not a smaller sector. Its notional
        would escape every total and the primary cap would stop binding."""

        def unclassified(ist_time: dt.time, kwargs: dict) -> None:
            kwargs["slots_used"] = 1
            kwargs["correlations"] = {"MYSTERY": Decimal("0.1")}
            kwargs["open_positions"] = (
                _position("MYSTERY", "50000", None if ist_time >= dt.time(12, 0) else "IT"),
            )

        session = Session(calendar, TRADING_DAY, checks=_ten(calendar), **self._clean_ctx()).run(
            mutate=unclassified
        )
        assert session.fully_cleared()[-1] == dt.time(11, 59)
        assert session.decisions[dt.time(12, 0)].reason is RejectReason.RISK_ENGINE_FAULT
        assert "sector" in (session.decisions[dt.time(12, 0)].detail or "")

    def test_four_psu_banks_cannot_accumulate_over_a_whole_session(self, calendar) -> None:
        """AC10 as a session rather than a single evaluation. The names arrive
        one at a time across the morning, which is how it would actually
        happen — and how a per-evaluation guard could still let the book drift
        if it only ever looked at one candidate."""
        banks = [
            _position("PNB", "60000", "PSU_BANK"),
            _position("BANKBARODA", "60000", "PSU_BANK"),
            _position("CANBK", "60000", "PSU_BANK"),
        ]

        def accumulate(ist_time: dt.time, kwargs: dict) -> None:
            held = banks[: min(3, max(0, (ist_time.hour - 9)))]
            kwargs["open_positions"] = tuple(held)
            kwargs["slots_used"] = len(held)
            kwargs["symbol_sector"] = "PSU_BANK"
            kwargs["correlations"] = {p.symbol: Decimal("0.85") for p in held}

        session = Session(calendar, TRADING_DAY, checks=_ten(calendar)).run(mutate=accumulate)
        # From 11:00 two PSU banks are held and correlated -> the guard binds
        # and never releases, so a fourth can never be added.
        for hour in (11, 12, 13, 14):
            decision = session.decisions[dt.time(hour, 0)]
            assert not decision.approved
            assert decision.reason in {
                RejectReason.CORRELATION_LIMIT,
                RejectReason.SECTOR_EXPOSURE_LIMIT,
            }, f"at {hour}:00 the reason was {decision.reason}"

    def test_a_hedged_book_trades_all_day(self, calendar) -> None:
        """The control for the whole group. A long and a short of equal size
        is not a directional bet, and a session-long refusal here would mean
        the net check was reading gross."""
        session = Session(
            calendar,
            TRADING_DAY,
            checks=_ten(calendar),
            symbol_sector="PHARMA",
            slots_used=2,
            open_positions=(
                _position("TCS", "160000", "IT"),
                _position("RELIANCE", "160000", "ENERGY", Direction.SHORT),
            ),
            correlations={"TCS": Decimal("0.1"), "RELIANCE": Decimal("0.2")},
        ).run()
        assert len(session.fully_cleared()) == 340

    def test_every_decision_is_still_audited_with_all_ten_registered(self, calendar) -> None:
        session = Session(calendar, TRADING_DAY, checks=_ten(calendar), **self._clean_ctx()).run()
        assert len(session.audit) == len(session.decisions)
        for entry in session.audit:
            assert len(str(entry["stage"])) <= 28, entry["stage"]

    def test_a_hostile_sector_name_cannot_forge_a_line_across_a_session(self, calendar) -> None:
        """QA-SEC-30's containment at a FOURTH source. Sector names reach the
        detail too, and the fix lives in CheckOutcome so this one never had to
        be found the hard way."""
        evil = "IT\nCRITICAL kill switch disarmed by operator"
        session = Session(
            calendar,
            TRADING_DAY,
            checks=_ten(calendar),
            symbol_sector=evil,
            slots_used=1,
            open_positions=(_position("TCS", "250000", evil),),
            correlations={"TCS": Decimal("0.1")},
        ).run(start=(10, 0), end=(11, 0))
        lines = [line for line in session.log_text.splitlines() if line.strip()]
        assert len(lines) == 60, f"60 rejections produced {len(lines)} log lines"

    def test_the_ten_check_day_replays_identically(self, calendar) -> None:
        first = Session(calendar, TRADING_DAY, checks=_ten(calendar), **self._clean_ctx()).run()
        second = Session(calendar, TRADING_DAY, checks=_ten(calendar), **self._clean_ctx()).run()
        assert first.reasons() == second.reasons()
        assert first.fully_cleared() == second.fully_cleared()

    def test_the_full_order_is_what_a_cleared_minute_records(self, calendar) -> None:
        session = Session(calendar, TRADING_DAY, checks=_ten(calendar), **self._clean_ctx()).run(
            start=(10, 0), end=(10, 5)
        )
        decision = session.decisions[dt.time(10, 0)]
        assert list(decision.checks_passed) == [
            *PRECONDITION_ORDER,
            *ELIGIBILITY_ORDER,
            *EXPOSURE_ORDER,
        ]

    def test_nothing_was_approved_across_the_whole_day(self, calendar) -> None:
        """Still true at ten checks, and worth re-asserting rather than
        assuming: four checks are unwritten and there is no sizer."""
        session = Session(calendar, TRADING_DAY, checks=_ten(calendar), **self._clean_ctx()).run()
        assert [t for t, d in session.decisions.items() if d.approved] == []


# --------------------------------------------------------------------------
# SIT-8 — loss limits across a session (E14-S05)
# --------------------------------------------------------------------------


def _twelve(calendar) -> tuple[RiskCheck, ...]:
    """Every built check: four pre-conditions, three eligibility, three
    exposure, two loss limits."""
    return (
        *build_precondition_checks(calendar, NO_TRADE_WINDOWS),
        *build_eligibility_checks(),
        *build_exposure_checks(
            max_correlated_positions=2,
            correlation_threshold=Decimal("0.7"),
            max_sector_exposure_pct=Decimal("40"),
            max_net_directional_exposure_pct=Decimal("60"),
        ),
        *build_loss_checks(max_daily_loss_pct=Decimal("3.0"), consecutive_loss_halt=3),
    )


class TestSit8LossLimitsAcrossASession:
    """The latch is the thing a session walk can test and a single evaluation
    cannot. Every other check in this pipeline answers from the state in front
    of it; these two have to remember something."""

    @staticmethod
    def _clean() -> dict:
        return {"symbol_sector": "IT", "correlations": {}}

    def test_adding_the_loss_checks_did_not_narrow_a_healthy_day(self, calendar) -> None:
        four = Session(calendar, TRADING_DAY).run()
        twelve = Session(calendar, TRADING_DAY, checks=_twelve(calendar), **self._clean()).run()
        assert twelve.fully_cleared() == four.fully_cleared()
        assert len(twelve.fully_cleared()) == 340

    def test_the_day_stops_when_the_loss_limit_is_reached(self, calendar) -> None:
        def loses(ist_time: dt.time, kwargs: dict) -> None:
            if ist_time >= dt.time(12, 0):
                kwargs["realised_pnl_today"] = Decimal("-16000")

        session = Session(calendar, TRADING_DAY, checks=_twelve(calendar), **self._clean()).run(
            mutate=loses
        )
        assert session.fully_cleared()[-1] == dt.time(11, 59)
        assert session.decisions[dt.time(12, 0)].reason is RejectReason.DAILY_LOSS_LIMIT

    def test_a_recovery_does_not_restart_the_day_when_the_latch_is_set(self, calendar) -> None:
        """The scenario the latch exists for, walked minute by minute.

        The limit trips at noon. Losing positions then close -- one at a
        profit -- and realised P&L climbs back well inside the limit. Without
        the latch the afternoon trades again, on a day a risk limit already
        stopped.
        """

        def breach_then_recover(ist_time: dt.time, kwargs: dict) -> None:
            if ist_time >= dt.time(12, 0):
                kwargs["daily_loss_halted"] = True
            if ist_time >= dt.time(13, 0):
                kwargs["realised_pnl_today"] = Decimal("-1000")  # recovered
            elif ist_time >= dt.time(12, 0):
                kwargs["realised_pnl_today"] = Decimal("-16000")

        session = Session(calendar, TRADING_DAY, checks=_twelve(calendar), **self._clean()).run(
            mutate=breach_then_recover
        )

        assert session.fully_cleared()[-1] == dt.time(11, 59), (
            "trading resumed after the daily loss limit halted the day"
        )
        for hour in (12, 13, 14):
            decision = session.decisions[dt.time(hour, 0)]
            assert decision.reason is RejectReason.DAILY_LOSS_LIMIT, (
                f"at {hour}:00 the day was trading again after a halt"
            )

    def test_without_the_latch_the_recovery_would_have_resumed_trading(self, calendar) -> None:
        """The counterfactual, stated as a test so the latch's value is
        demonstrated rather than asserted. Same session, latch never set: the
        afternoon trades again."""

        def breach_then_recover_unlatched(ist_time: dt.time, kwargs: dict) -> None:
            if dt.time(12, 0) <= ist_time < dt.time(13, 0):
                kwargs["realised_pnl_today"] = Decimal("-16000")
            elif ist_time >= dt.time(13, 0):
                kwargs["realised_pnl_today"] = Decimal("-1000")

        session = Session(calendar, TRADING_DAY, checks=_twelve(calendar), **self._clean()).run(
            mutate=breach_then_recover_unlatched
        )
        assert dt.time(13, 0) in session.fully_cleared(), (
            "this is what the latch prevents -- if this ever fails, the live "
            "figure alone has started behaving as a latch and the test above "
            "has stopped proving anything"
        )

    def test_a_streak_halt_survives_a_winning_exit(self, calendar) -> None:
        """The consecutive counter resets to zero on any win, so the latch is
        doing all the work here."""

        def streak_then_win(ist_time: dt.time, kwargs: dict) -> None:
            if ist_time >= dt.time(11, 0):
                kwargs["consecutive_loss_halted"] = True
                kwargs["consecutive_losses"] = 0 if ist_time >= dt.time(12, 0) else 3

        session = Session(calendar, TRADING_DAY, checks=_twelve(calendar), **self._clean()).run(
            mutate=streak_then_win
        )
        assert session.fully_cleared()[-1] == dt.time(10, 59)
        assert session.decisions[dt.time(13, 0)].reason is RejectReason.CONSECUTIVE_LOSS_LIMIT

    def test_a_profitable_session_trades_all_day(self, calendar) -> None:
        """The control for the whole group. A sign inversion would turn this
        into a session that halts at noon on its best day."""
        session = Session(
            calendar,
            TRADING_DAY,
            checks=_twelve(calendar),
            realised_pnl_today=Decimal("25000"),
            **self._clean(),
        ).run()
        assert len(session.fully_cleared()) == 340

    def test_a_losing_but_within_limit_session_trades_all_day(self, calendar) -> None:
        """Down 2.8% on a 3% limit. The system must keep working on a bad day
        that has not reached its limit -- otherwise the limit is effectively
        tighter than configured."""
        session = Session(
            calendar,
            TRADING_DAY,
            checks=_twelve(calendar),
            realised_pnl_today=Decimal("-14000"),
            **self._clean(),
        ).run()
        assert len(session.fully_cleared()) == 340

    def test_the_loss_limits_come_last_across_a_whole_day(self, calendar) -> None:
        """With the day halted AND a banned symbol, every minute must report
        the symbol -- the more specific reason."""
        session = Session(
            calendar,
            TRADING_DAY,
            checks=_twelve(calendar),
            symbol_restrictions=("T2T",),
            realised_pnl_today=Decimal("-20000"),
            **{"symbol_sector": "IT", "correlations": {}},
        ).run(start=(10, 0), end=(11, 0))
        for stage in session.stopped_by().values():
            assert stage == "symbol_tradable"

    def test_every_decision_is_audited_with_all_twelve_registered(self, calendar) -> None:
        session = Session(calendar, TRADING_DAY, checks=_twelve(calendar), **self._clean()).run()
        assert len(session.audit) == len(session.decisions)
        for entry in session.audit:
            assert len(str(entry["stage"])) <= 28, entry["stage"]

    def test_the_full_order_is_what_a_cleared_minute_records(self, calendar) -> None:
        session = Session(calendar, TRADING_DAY, checks=_twelve(calendar), **self._clean()).run(
            start=(10, 0), end=(10, 5)
        )
        assert list(session.decisions[dt.time(10, 0)].checks_passed) == [
            *PRECONDITION_ORDER,
            *ELIGIBILITY_ORDER,
            *EXPOSURE_ORDER,
            *LOSS_ORDER,
        ]

    def test_the_twelve_check_day_replays_identically(self, calendar) -> None:
        first = Session(calendar, TRADING_DAY, checks=_twelve(calendar), **self._clean()).run()
        second = Session(calendar, TRADING_DAY, checks=_twelve(calendar), **self._clean()).run()
        assert first.reasons() == second.reasons()
        assert first.fully_cleared() == second.fully_cleared()

    def test_nothing_was_approved_across_the_whole_day(self, calendar) -> None:
        session = Session(calendar, TRADING_DAY, checks=_twelve(calendar), **self._clean()).run()
        assert [t for t, d in session.decisions.items() if d.approved] == []


# --------------------------------------------------------------------------
# SIT-9 — the complete fourteen-check pipeline across a session (E14-S06)
# --------------------------------------------------------------------------


def _fourteen(calendar) -> tuple[RiskCheck, ...]:
    """Every check the system has, in LOW_LEVEL_ARCHITECTURE 5.7's order."""
    return (
        *build_precondition_checks(calendar, NO_TRADE_WINDOWS),
        *build_eligibility_checks(),
        *build_exposure_checks(
            max_correlated_positions=2,
            correlation_threshold=Decimal("0.7"),
            max_sector_exposure_pct=Decimal("40"),
            max_net_directional_exposure_pct=Decimal("60"),
        ),
        *build_loss_checks(max_daily_loss_pct=Decimal("3.0"), consecutive_loss_halt=3),
        *build_margin_timing_checks(min_minutes_to_squareoff=30),
    )


class TestSit9TheCompletePipelineAcrossASession:
    """The whole risk engine, walked minute by minute.

    Every earlier SIT group ran a partial pipeline because the rest was
    unwritten. This one asks the question that only became askable now: with
    all fourteen gates in place, what does a trading day actually look like?
    """

    @staticmethod
    def _healthy() -> dict:
        return {
            "symbol_sector": "IT",
            "correlations": {},
            "available_margin": Decimal("250000"),
            "margin_per_share": Decimal("240"),
        }

    def test_the_fourteen_check_day_is_shorter_than_the_four_check_day(self, calendar) -> None:
        """The first time adding checks legitimately NARROWS the session, and
        the reason is check 14 rather than a bug.

        Every previous group asserted the tradable window was unchanged,
        because none of those gates had an opinion about the clock. The runway
        check does: a CAS deadline of 15:05 with a 30-minute minimum ends
        entries at 14:35, not 15:00.
        """
        four = Session(calendar, TRADING_DAY).run()
        fourteen = Session(
            calendar, TRADING_DAY, checks=_fourteen(calendar), **self._healthy()
        ).run()
        assert len(four.fully_cleared()) == 340
        assert fourteen.fully_cleared()[0] == dt.time(9, 20)
        assert fourteen.fully_cleared()[-1] == dt.time(14, 35), (
            "entries should stop 30 minutes before the 15:05 deadline"
        )
        assert len(fourteen.fully_cleared()) == 316

    def test_the_narrowing_is_the_runway_check_and_nothing_else(self, calendar) -> None:
        """Names the cause rather than inferring it. If a different gate ever
        starts closing the afternoon, this fails instead of quietly agreeing."""
        session = Session(
            calendar, TRADING_DAY, checks=_fourteen(calendar), **self._healthy()
        ).run()
        stages = session.stopped_by()
        for minute in (dt.time(14, 36), dt.time(14, 45), dt.time(14, 59)):
            assert stages[minute] == "time_to_squareoff", (
                f"at {minute} the day was closed by {stages[minute]}, not the runway"
            )

    def test_the_window_is_contiguous_with_no_hole(self, calendar) -> None:
        session = Session(
            calendar, TRADING_DAY, checks=_fourteen(calendar), **self._healthy()
        ).run()
        cleared = session.fully_cleared()
        expected, moment = [], dt.datetime.combine(TRADING_DAY, cleared[0])
        while moment.time() <= cleared[-1]:
            expected.append(moment.time())
            moment += dt.timedelta(minutes=1)
        assert cleared == expected

    def test_margin_running_out_mid_session_stops_the_day(self, calendar) -> None:
        """The realistic shape: earlier fills consume margin until the next
        candidate is unaffordable."""

        def spends(ist_time: dt.time, kwargs: dict) -> None:
            if ist_time >= dt.time(12, 0):
                kwargs["available_margin"] = Decimal("10")

        session = Session(calendar, TRADING_DAY, checks=_fourteen(calendar), **self._healthy()).run(
            mutate=spends
        )
        assert session.fully_cleared()[-1] == dt.time(11, 59)
        assert session.decisions[dt.time(12, 0)].reason is RejectReason.INSUFFICIENT_MARGIN

    def test_the_broker_going_dark_mid_session_is_a_fault_and_recovers(self, calendar) -> None:
        """Unknown margin is not zero margin. It must read as a FAULT — the
        broker is unreachable, which is an outage — and it must release when
        the broker answers again."""

        def broker_outage(ist_time: dt.time, kwargs: dict) -> None:
            if dt.time(11, 0) <= ist_time < dt.time(11, 30):
                kwargs["available_margin"] = None

        session = Session(calendar, TRADING_DAY, checks=_fourteen(calendar), **self._healthy()).run(
            mutate=broker_outage
        )
        blocked = {
            moment for moment, stage in session.stopped_by().items() if stage == "margin_sufficient"
        }
        assert blocked == {dt.time(11, m) for m in range(30)}
        assert session.decisions[dt.time(11, 0)].reason is RejectReason.RISK_ENGINE_FAULT
        assert dt.time(11, 30) in session.fully_cleared()

    def test_an_empty_account_is_a_business_rejection_not_a_fault(self, calendar) -> None:
        """The control for the test above, and the distinction SIT-001 exists
        to protect. Zero margin is an ANSWER; absent margin is the lack of one,
        and Decimal(0) is falsy so the two are easy to conflate."""
        session = Session(
            calendar,
            TRADING_DAY,
            checks=_fourteen(calendar),
            **{**self._healthy(), "available_margin": Decimal(0)},
        ).run(start=(10, 0), end=(10, 5))
        decision = session.decisions[dt.time(10, 0)]
        assert decision.reason is RejectReason.INSUFFICIENT_MARGIN
        assert decision.reason is not RejectReason.RISK_ENGINE_FAULT

    def test_a_non_cas_stock_trades_later_than_a_cas_one(self, calendar) -> None:
        """The per-stock deadline, walked. A non-CAS name squares off at 15:20
        rather than 15:10, so with the same buffer and minimum it keeps
        trading ten minutes longer. A global deadline would make these two
        sessions identical."""
        cas = Session(calendar, TRADING_DAY, checks=_fourteen(calendar), **self._healthy()).run()
        non_cas = Session(
            calendar,
            TRADING_DAY,
            checks=_fourteen(calendar),
            squareoff_deadline=_at(TRADING_DAY, 15, 15),
            **self._healthy(),
        ).run()
        assert cas.fully_cleared()[-1] == dt.time(14, 35)
        assert non_cas.fully_cleared()[-1] == dt.time(14, 45)

    def test_nothing_is_approved_on_any_minute_of_the_day(self, calendar) -> None:
        """Now the strongest form of this assertion: all fourteen gates exist,
        a clean candidate clears every one of them on 316 minutes, and the
        engine still approves nothing because there is no sizer."""
        session = Session(
            calendar, TRADING_DAY, checks=_fourteen(calendar), **self._healthy()
        ).run()
        assert session.fully_cleared(), "the day must have tradable minutes to be a test"
        assert [t for t, d in session.decisions.items() if d.approved] == []
        cleared_minute = session.decisions[session.fully_cleared()[0]]
        assert cleared_minute.reason is RejectReason.RISK_ENGINE_FAULT
        assert "no sizer" in (cleared_minute.detail or "")

    def test_a_cleared_minute_records_all_fourteen_in_spec_order(self, calendar) -> None:
        session = Session(calendar, TRADING_DAY, checks=_fourteen(calendar), **self._healthy()).run(
            start=(10, 0), end=(10, 5)
        )
        assert list(session.decisions[dt.time(10, 0)].checks_passed) == list(all_check_ids())

    def test_every_decision_is_audited_with_all_fourteen_registered(self, calendar) -> None:
        session = Session(
            calendar, TRADING_DAY, checks=_fourteen(calendar), **self._healthy()
        ).run()
        assert len(session.audit) == len(session.decisions)
        for entry in session.audit:
            assert len(str(entry["stage"])) <= 28, entry["stage"]
            assert isinstance(entry["ts"], dt.datetime)

    def test_the_fourteen_check_day_replays_identically(self, calendar) -> None:
        first = Session(calendar, TRADING_DAY, checks=_fourteen(calendar), **self._healthy()).run()
        second = Session(calendar, TRADING_DAY, checks=_fourteen(calendar), **self._healthy()).run()
        assert first.reasons() == second.reasons()
        assert first.fully_cleared() == second.fully_cleared()

    def test_no_credential_reached_the_log_across_a_full_pipeline_day(self, calendar) -> None:
        """Re-asserted now that every check exists — the margin checks are the
        first to carry broker-derived numbers into a rejection detail."""
        import re

        session = Session(
            calendar, TRADING_DAY, checks=_fourteen(calendar), **self._healthy()
        ).run()
        assert not re.search(
            r"(api[_-]?key|secret|password|access[_-]?token)\s*[:=]\s*\S{8,}",
            session.log_text,
            re.IGNORECASE,
        )


# --------------------------------------------------------------------------
# SIT-10 — a session that can APPROVE (E14-S07)
# --------------------------------------------------------------------------


def _sizing_policy() -> SizingPolicy:
    return SizingPolicy(
        # E14-S10: the portfolio caps, now binding rather than decorative.
        max_sector_exposure_pct=Decimal("40"),
        max_net_directional_exposure_pct=Decimal("60"),
        risk_pct=Decimal("1.0"),
        atr_multiplier_stop=Decimal("1.5"),
        max_position_pct=Decimal("20"),
        capital_per_slot_pct=Decimal("20"),
        target_r_multiple=Decimal("2.0"),
    )


class TestSit10ASessionThatCanApprove:
    """Every previous SIT group asserted that NOTHING was ever approved,
    because there was no sizer. That assertion has been true for six stories
    and is now false by design — so it is replaced rather than reused, and
    what replaces it is the harder question: on a day the system CAN trade,
    does it stay inside its risk budget on every single minute?
    """

    @staticmethod
    def _healthy() -> dict:
        return {
            "symbol_sector": "IT",
            "correlations": {},
            "available_margin": Decimal("400000"),
            "margin_per_share": Decimal("240"),
            "atr": Decimal("13.5000"),
            "lot_size": 1,
        }

    def _session(self, calendar, **overrides):
        kwargs = {**self._healthy(), **overrides}
        session = Session(
            calendar,
            TRADING_DAY,
            checks=[
                *build_precondition_checks(calendar, NO_TRADE_WINDOWS),
                *build_eligibility_checks(),
                *build_exposure_checks(
                    max_correlated_positions=2,
                    correlation_threshold=Decimal("0.7"),
                    max_sector_exposure_pct=Decimal("40"),
                    max_net_directional_exposure_pct=Decimal("60"),
                ),
                *build_loss_checks(max_daily_loss_pct=Decimal("3.0"), consecutive_loss_halt=3),
                *build_margin_timing_checks(min_minutes_to_squareoff=30),
            ],
            **kwargs,
        )
        session.engine = RiskEngine(
            checks=list(session.engine.checks),
            sizer=build_sizer(_sizing_policy()),
            audit=session.audit.append,
        )
        return session

    def test_the_system_now_approves_on_a_normal_day(self, calendar) -> None:
        """The assertion six SIT groups made in the negative, finally
        positive."""
        session = self._session(calendar).run()
        approved = [t for t, d in session.decisions.items() if d.approved]
        assert approved, "the system approved nothing on an ordinary session"
        assert len(approved) == 316, "approvals should span exactly the tradable window"

    def test_it_approves_on_exactly_the_minutes_it_used_to_merely_clear(self, calendar) -> None:
        """The sizer must not narrow the day further. Before this story those
        316 minutes cleared fourteen checks and were refused for want of a
        sizer; now they are approvals, and the set must be identical."""
        session = self._session(calendar).run()
        approved = sorted(t for t, d in session.decisions.items() if d.approved)
        assert approved == session.fully_cleared()

    def test_every_approval_across_the_whole_day_is_inside_the_risk_budget(self, calendar) -> None:
        """AC1 as a SESSION property rather than a per-call one. 316 sized
        positions, every one of them within 1% of capital -- which is what
        'risk 1% per trade' has to mean to be worth configuring."""
        session = self._session(calendar).run()
        budget = Decimal("500000") * Decimal("1.0") / 100
        sized = [d.sizing for d in session.decisions.values() if d.approved]
        assert sized
        for sizing in sized:
            assert sizing is not None
            assert sizing.capital_at_risk <= budget

    def test_every_approval_carries_a_quantity_and_a_stop(self, calendar) -> None:
        """An approval with no quantity, or a stop at the entry price, would
        reach the order gateway and do something indefensible."""
        session = self._session(calendar).run()
        for decision in session.decisions.values():
            if not decision.approved:
                continue
            assert decision.sizing is not None
            assert decision.sizing.quantity > 0
            assert decision.sizing.stop_price < decision.sizing.entry_price
            assert decision.sizing.binding_constraint

    def test_a_halted_day_approves_nothing(self, calendar) -> None:
        """The control the previous groups gave for free and this one must
        state: the sizer runs only after every gate passes, so a kill switch
        still produces no position at any minute."""
        session = self._session(calendar, kill_switch_active=True).run()
        assert [t for t, d in session.decisions.items() if d.approved] == []

    def test_the_loss_limit_stops_approvals_mid_session(self, calendar) -> None:
        """A halt has to stop the thing that actually costs money, not merely
        change a reason code."""

        def breach(ist_time: dt.time, kwargs: dict) -> None:
            if ist_time >= dt.time(12, 0):
                kwargs["daily_loss_halted"] = True

        session = self._session(calendar).run(mutate=breach)
        approved = sorted(t for t, d in session.decisions.items() if d.approved)
        assert approved[-1] == dt.time(11, 59)

    def test_a_volatile_session_sizes_smaller_all_day(self, calendar) -> None:
        """The point of ATR sizing, walked. Four times the volatility, smaller
        positions on every minute, and both inside the same budget."""
        quiet = self._session(calendar, atr=Decimal("5.0000")).run()
        volatile = self._session(calendar, atr=Decimal("50.0000")).run()
        budget = Decimal("500000") * Decimal("1.0") / 100
        q = [d.sizing.quantity for d in quiet.decisions.values() if d.approved]
        v = [d.sizing.quantity for d in volatile.decisions.values() if d.approved]
        assert q and v
        assert min(q) > max(v), "a quieter instrument must size larger throughout"
        for d in list(quiet.decisions.values()) + list(volatile.decisions.values()):
            if d.approved:
                assert d.sizing.capital_at_risk <= budget

    def test_the_audit_carries_a_quantity_on_every_approval(self, calendar) -> None:
        """The number that becomes an order has to be reconstructable."""
        session = self._session(calendar).run()
        approvals = [e for e in session.audit if e["outcome"] == "approved"]
        assert len(approvals) == 316
        for entry in approvals:
            assert entry["stage"] == "risk_approved"
            assert entry["payload"]["quantity"] > 0
            assert entry["payload"]["binding_constraint"]

    def test_the_sized_day_replays_identically(self, calendar) -> None:
        """Sizing is arithmetic over Decimals; if it ever drifted between runs,
        an incident could not be reconstructed."""
        first = self._session(calendar).run()
        second = self._session(calendar).run()
        assert [
            (t, d.approved, d.sizing.quantity if d.sizing else None)
            for t, d in first.decisions.items()
        ] == [
            (t, d.approved, d.sizing.quantity if d.sizing else None)
            for t, d in second.decisions.items()
        ]

    def test_the_quantity_is_the_same_on_every_approved_minute(self, calendar) -> None:
        """Nothing in the context changes across the day except the clock, and
        sizing must not read the clock. A drifting quantity would mean it does.
        """
        session = self._session(calendar).run()
        quantities = {d.sizing.quantity for d in session.decisions.values() if d.approved}
        assert len(quantities) == 1, f"quantity varied across the day: {sorted(quantities)}"

    def test_no_approval_survives_a_missing_atr(self, calendar) -> None:
        """The sizer's own fail-closed path at session scale."""
        session = self._session(calendar, atr=None).run()
        assert [t for t, d in session.decisions.items() if d.approved] == []
        assert session.decisions[dt.time(10, 0)].reason is RejectReason.RISK_ENGINE_FAULT

    def test_no_credential_reached_the_log_on_a_day_that_placed_orders(self, calendar) -> None:
        """Re-asserted now that the log carries approvals and quantities."""
        import re

        session = self._session(calendar).run()
        assert not re.search(
            r"(api[_-]?key|secret|password|access[_-]?token)\s*[:=]\s*\S{8,}",
            session.log_text,
            re.IGNORECASE,
        )


# --------------------------------------------------------------------------
# SIT-11 — a session whose exposure caps actually bind (E14-S10)
# --------------------------------------------------------------------------


class TestSit11ExposureCapsHoldAcrossAWholeSession:
    """SIT-10 asked whether every approved minute stayed inside the *per-trade*
    risk budget. This asks the portfolio question E14-S10 exists for: on a day
    where the book is already concentrated, does every minute stay inside the
    *sector* and *net-directional* caps as well?

    The distinction is the whole story. Checks 9 and 10 run before sizing, so
    across 316 tradable minutes they were asserting only that the book was
    under its caps when the day began. Nothing asserted where the book ended
    up — and a component test on one candidate cannot, because the failure
    mode is a cap that holds on the first minute and is breached by the
    position sizing would have taken on the second.

    The signed-direction behaviour is deliberately NOT re-tested here. It is a
    per-decision property, covered exhaustively in ``tests/unit/test_sizer.py``
    and killed by two mutations; asserting it at session scale would mean
    reshaping the shared ``Session`` recommendation factory, which is only ever
    LONG, for no evidence a component test has not already produced.
    """

    #: 36% of 5,00,000 in the candidate's own sector, against a 40% cap. Chosen
    #: so the sector clamp — not the ₹1200 position cap — is the smallest
    #: constraint, and is therefore doing the work on every minute.
    CONCENTRATED_SECTOR = "180000"
    #: 2,90,000 net long against a 60% (3,00,000) cap, held OUTSIDE the
    #: candidate's sector so the two clamps can be observed separately.
    ONE_SIDED_BOOK = "290000"

    @staticmethod
    def _healthy() -> dict:
        return {
            "symbol_sector": "IT",
            "correlations": {},
            "available_margin": Decimal("400000"),
            "margin_per_share": Decimal("240"),
            "atr": Decimal("13.5000"),
            "lot_size": 1,
        }

    def _session(self, calendar, **overrides):
        kwargs = {**self._healthy(), **overrides}
        # Check 8 reads an ABSENT correlation as unknown and refuses, which is
        # the designed behaviour and would otherwise silence this whole group:
        # every held name needs a stated, uncorrelated figure so the day
        # reaches sizing and the CLAMPS are what is under test.
        held = kwargs.get("open_positions") or ()
        if held:
            kwargs["correlations"] = {
                **{p.symbol: Decimal("0.1") for p in held},
                **(overrides.get("correlations") or {}),
            }
        session = Session(
            calendar,
            TRADING_DAY,
            checks=[
                *build_precondition_checks(calendar, NO_TRADE_WINDOWS),
                *build_eligibility_checks(),
                *build_exposure_checks(
                    max_correlated_positions=2,
                    correlation_threshold=Decimal("0.7"),
                    max_sector_exposure_pct=Decimal("40"),
                    max_net_directional_exposure_pct=Decimal("60"),
                ),
                *build_loss_checks(max_daily_loss_pct=Decimal("3.0"), consecutive_loss_halt=3),
                *build_margin_timing_checks(min_minutes_to_squareoff=30),
            ],
            **kwargs,
        )
        session.engine = RiskEngine(
            checks=list(session.engine.checks),
            sizer=build_sizer(_sizing_policy()),
            audit=session.audit.append,
        )
        return session

    def test_a_concentrated_sector_still_trades_but_smaller_all_day(self, calendar) -> None:
        """The behaviour the story is for. A 36%-concentrated sector does not
        stop the day — it makes every position smaller, on every minute. A
        clamp that turned into a refusal would be the over-tight design the
        story explicitly rejected."""
        held = (_position("TCS", self.CONCENTRATED_SECTOR, "IT"),)
        clear_q = {
            d.sizing.quantity
            for d in self._session(calendar).run().decisions.values()
            if d.approved
        }
        crowded_q = {
            d.sizing.quantity
            for d in self._session(calendar, open_positions=held).run().decisions.values()
            if d.approved
        }
        assert clear_q == {83}, "the clear-book size changed; SIT-10's baseline moved"
        assert crowded_q == {16}, "the sector clamp did not size the day down"

    def test_every_approved_minute_leaves_the_sector_at_or_under_its_cap(self, calendar) -> None:
        """The session-scale property. 316 sized positions, and the book AFTER
        any one of them is still inside the 40% cap — not merely inside it
        before."""
        held = _position("TCS", self.CONCENTRATED_SECTOR, "IT")
        session = self._session(calendar, open_positions=(held,)).run()
        cap_value = Decimal("500000") * Decimal("40") / 100
        sized = [d.sizing for d in session.decisions.values() if d.approved]
        assert sized, "nothing was approved, so the property is untested"
        for sizing in sized:
            after = held.notional + sizing.quantity * sizing.entry_price
            assert after <= cap_value, f"sector reached {after} against {cap_value}"

    def test_every_approved_minute_leaves_the_book_at_or_under_the_net_cap(self, calendar) -> None:
        held = _position("HDFCBANK", self.ONE_SIDED_BOOK, "BANKING")
        session = self._session(calendar, open_positions=(held,)).run()
        cap_value = Decimal("500000") * Decimal("60") / 100
        sized = [d.sizing for d in session.decisions.values() if d.approved]
        assert sized
        for sizing in sized:
            after = held.notional + sizing.quantity * sizing.entry_price
            assert after <= cap_value, f"net reached {after} against {cap_value}"

    def test_the_two_caps_bind_independently(self, calendar) -> None:
        """A book concentrated in the candidate's sector and a book merely
        one-sided are different failures, and must produce different clamps.
        One clamp shadowing the other would still cap exposure and would make
        every rejection reason wrong."""
        crowded = self._session(
            calendar, open_positions=(_position("TCS", self.CONCENTRATED_SECTOR, "IT"),)
        ).run()
        one_sided = self._session(
            calendar,
            open_positions=(_position("HDFCBANK", self.ONE_SIDED_BOOK, "BANKING"),),
        ).run()
        assert {d.sizing.binding_constraint for d in crowded.decisions.values() if d.approved} == {
            SECTOR_CAP
        }
        assert {
            d.sizing.binding_constraint for d in one_sided.decisions.values() if d.approved
        } == {NET_EXPOSURE_CAP}

    def test_a_sector_at_its_cap_refuses_the_whole_day(self, calendar) -> None:
        """At the cap exactly, check 9 refuses before sizing is reached. That
        is correct and is what an operator should see — asserted so the check
        and the clamp are known to agree rather than to disagree quietly."""
        held = (_position("TCS", "200000", "IT"),)
        session = self._session(calendar, open_positions=held).run()
        assert [t for t, d in session.decisions.items() if d.approved] == []
        reasons = {d.reason for d in session.decisions.values() if d.reason}
        assert RejectReason.SECTOR_EXPOSURE_LIMIT in reasons

    def test_an_unclassified_position_stops_the_day_as_a_fault(self, calendar) -> None:
        """Degraded input, which is what SIT is for. One position with no
        sector makes every sector total untrustworthy, so the honest answer is
        to refuse the session — and to report it as a FAULT rather than as a
        business limit, because those send an operator to different places."""
        held = (_position("TCS", "100000", None),)
        session = self._session(calendar, open_positions=held).run()
        assert [t for t, d in session.decisions.items() if d.approved] == []
        # On a TRADABLE minute. The run spans 08:00-16:00, so minutes outside
        # the session are correctly refused by check 3 long before the book is
        # ever read — asserting over the whole day would be asserting that the
        # trading window has stopped working.
        assert session.decisions[dt.time(10, 0)].reason is RejectReason.RISK_ENGINE_FAULT
        assert session.decisions[dt.time(14, 0)].reason is RejectReason.RISK_ENGINE_FAULT

    def test_the_audit_records_which_cap_bound_on_every_approval(self, calendar) -> None:
        """A surprisingly small position has to be explainable from the log
        alone, without reconstructing the book."""
        held = (_position("TCS", self.CONCENTRATED_SECTOR, "IT"),)
        session = self._session(calendar, open_positions=held).run()
        approvals = [e for e in session.audit if e["outcome"] == "approved"]
        assert approvals
        for entry in approvals:
            assert entry["payload"]["binding_constraint"] == SECTOR_CAP
            assert entry["payload"]["quantity"] == 16

    def test_the_concentrated_day_replays_identically(self, calendar) -> None:
        """Two clamps more arithmetic than yesterday, and still no drift — an
        incident on a concentrated day has to be reconstructable."""
        first = self._session(
            calendar, open_positions=(_position("TCS", self.CONCENTRATED_SECTOR, "IT"),)
        ).run()
        second = self._session(
            calendar, open_positions=(_position("TCS", self.CONCENTRATED_SECTOR, "IT"),)
        ).run()
        assert [
            (t, d.approved, d.sizing.quantity if d.sizing else None)
            for t, d in first.decisions.items()
        ] == [
            (t, d.approved, d.sizing.quantity if d.sizing else None)
            for t, d in second.decisions.items()
        ]

    def test_no_credential_reached_the_log_on_a_concentrated_day(self, calendar) -> None:
        """Re-asserted for this session shape, which logs two new clamp names
        and two new reason codes."""
        import re

        held = (_position("TCS", self.CONCENTRATED_SECTOR, "IT"),)
        session = self._session(calendar, open_positions=held).run()
        assert not re.search(
            r"(api[_-]?key|secret|password|access[_-]?token)\s*[:=]\s*\S{8,}",
            session.log_text,
            re.IGNORECASE,
        )


# --------------------------------------------------------------------------
# SIT-12 — a session whose book fills and recycles (E14-S08)
# --------------------------------------------------------------------------


class TestSit12TheBookFillsAndRecyclesAcrossASession:
    """E14-S08 built the allocator; check 6 is what a full book *does* to a
    trading day. The unit and integration tiers prove that two signals cannot
    take one slot. This asks the session question neither can: when the book
    fills at 11:00 and a position closes at 13:00, does the day stop and
    restart at exactly the right minutes, and does it say why?

    Slot state is driven here rather than allocated, because ``SlotManager``
    needs a real Redis and a real Postgres and this tier runs without either.
    That split is deliberate: the allocator's correctness is proved where the
    stores are real, and its *effect on a session* is proved where a whole day
    can be walked in milliseconds.
    """

    @staticmethod
    def _healthy() -> dict:
        return {
            "symbol_sector": "IT",
            "correlations": {},
            "available_margin": Decimal("400000"),
            "margin_per_share": Decimal("240"),
            "atr": Decimal("13.5000"),
            "lot_size": 1,
        }

    def _session(self, calendar, **overrides):
        kwargs = {**self._healthy(), **overrides}
        session = Session(
            calendar,
            TRADING_DAY,
            checks=[
                *build_precondition_checks(calendar, NO_TRADE_WINDOWS),
                *build_eligibility_checks(),
                *build_exposure_checks(
                    max_correlated_positions=2,
                    correlation_threshold=Decimal("0.7"),
                    max_sector_exposure_pct=Decimal("40"),
                    max_net_directional_exposure_pct=Decimal("60"),
                ),
                *build_loss_checks(max_daily_loss_pct=Decimal("3.0"), consecutive_loss_halt=3),
                *build_margin_timing_checks(min_minutes_to_squareoff=30),
            ],
            **kwargs,
        )
        session.engine = RiskEngine(
            checks=list(session.engine.checks),
            sizer=build_sizer(_sizing_policy()),
            audit=session.audit.append,
        )
        return session

    @staticmethod
    def _fills_at_eleven(ist_time: dt.time, kwargs: dict) -> None:
        """Five of five in use from 11:00. The shape of a morning that found
        five setups before lunch."""
        if ist_time >= dt.time(11, 0):
            kwargs["slots_used"] = 5

    @staticmethod
    def _fills_then_recycles(ist_time: dt.time, kwargs: dict) -> None:
        """Full at 11:00, one position closes at 13:00."""
        if dt.time(11, 0) <= ist_time < dt.time(13, 0):
            kwargs["slots_used"] = 5
        elif ist_time >= dt.time(13, 0):
            kwargs["slots_used"] = 4

    def test_a_full_book_stops_approving_from_that_minute(self, calendar) -> None:
        session = self._session(calendar).run(mutate=self._fills_at_eleven)
        approved = sorted(t for t, d in session.decisions.items() if d.approved)
        assert approved, "the morning approved nothing, so the property is untested"
        assert approved[-1] == dt.time(10, 59)

    def test_a_full_book_says_why(self, calendar) -> None:
        """NO_SLOT_AVAILABLE, not a generic refusal.
        ``signals_rejected_total{reason}`` is what answers "why isn't it
        trading?" at a glance, and "the book is full" and "the market is shut"
        need different answers."""
        session = self._session(calendar).run(mutate=self._fills_at_eleven)
        assert session.decisions[dt.time(12, 0)].reason is RejectReason.NO_SLOT_AVAILABLE

    def test_the_detail_distinguishes_contention_from_misconfiguration(self, calendar) -> None:
        """Five of five clears on its own; zero of zero never can. The check
        reports used/total precisely so an operator can tell them apart."""
        session = self._session(calendar).run(mutate=self._fills_at_eleven)
        detail = session.decisions[dt.time(12, 0)].detail or ""
        assert "5 of 5" in detail

    def test_a_recycled_slot_restarts_the_day(self, calendar) -> None:
        """Task 3 at session scale. A slot freed at 13:00 must make the very
        next minute tradable again — a book that filled once and never
        recovered would look identical for the rest of the day."""
        session = self._session(calendar).run(mutate=self._fills_then_recycles)
        approved = sorted(t for t, d in session.decisions.items() if d.approved)
        assert dt.time(10, 59) in approved
        assert dt.time(12, 0) not in approved
        assert dt.time(13, 0) in approved

    def test_the_gap_is_exactly_the_full_window(self, calendar) -> None:
        """Not merely "it stopped and started" — the boundaries must be the
        two minutes the book actually changed on."""
        session = self._session(calendar).run(mutate=self._fills_then_recycles)
        refused = sorted(
            t for t, d in session.decisions.items() if d.reason is RejectReason.NO_SLOT_AVAILABLE
        )
        assert refused[0] == dt.time(11, 0)
        assert refused[-1] == dt.time(12, 59)

    def test_a_book_with_no_slots_configured_never_trades(self, calendar) -> None:
        """Zero of zero. Distinct from contention, and a configuration that can
        never trade should be obvious from the first minute rather than look
        like a quiet day."""
        session = self._session(calendar, slots_total=0, slots_used=0).run()
        assert [t for t, d in session.decisions.items() if d.approved] == []
        assert session.decisions[dt.time(10, 0)].reason is RejectReason.NO_SLOT_AVAILABLE
        assert "0 of 0" in (session.decisions[dt.time(10, 0)].detail or "")

    def test_slot_pressure_does_not_change_the_size_of_what_does_trade(self, calendar) -> None:
        """A subtle one worth pinning. Sizing divides capital per SLOT, so a
        book under pressure must still size each position the same — a sizer
        that reacted to occupancy would quietly shrink positions as the day
        got busier, and nothing else would report it."""
        empty = self._session(calendar).run()
        pressured = self._session(calendar, slots_used=4).run()
        empty_q = {d.sizing.quantity for d in empty.decisions.values() if d.approved}
        pressured_q = {d.sizing.quantity for d in pressured.decisions.values() if d.approved}
        assert pressured_q, "a book with one free slot approved nothing"
        assert empty_q == pressured_q

    def test_the_audit_records_the_refusal_on_every_blocked_minute(self, calendar) -> None:
        """A day that stopped trading has to be explainable afterwards without
        re-deriving the book."""
        session = self._session(calendar).run(mutate=self._fills_at_eleven)
        blocked = [
            e
            for e in session.audit
            if e["outcome"] == "rejected"
            and e["reason_code"] == RejectReason.NO_SLOT_AVAILABLE.value
        ]
        assert blocked, "the full book produced no audit trail"
        # `stage` is the CHECK that stopped it, not a generic "rejected" — which
        # is what makes the log answer "which gate?" rather than only "no".
        assert {e["stage"] for e in blocked} == {"slot_available"}
        assert all("5 of 5" in (e["payload"]["detail"] or "") for e in blocked)


# --------------------------------------------------------------------------
# SIT-13 — a halt walked across a whole session (E14-S09)
# --------------------------------------------------------------------------


class _SessionRedis:
    """A store that lives for one simulated session.

    Deliberately not a mock of the controller: the REAL ``HaltController`` runs
    here, on every minute of the day, and only the transport underneath it is
    substituted. A test that stubbed the controller would assert that a fake
    said "halted" — which is not the claim.
    """

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str, **_kwargs) -> bool:
        self.data[key] = value
        return True

    async def delete(self, key: str) -> int:
        return 1 if self.data.pop(key, None) is not None else 0


class TestSit13AHaltIsTerminalForTheDay:
    """``LOW_LEVEL_ARCHITECTURE.md §8.1`` says HALTED is terminal for the day.
    "Terminal" is a claim about a *session*, not about a function call, and the
    unit suite cannot make it: it asserts that one read returns True. This
    walks 480 minutes with the real controller in the loop and asks whether the
    day ever comes back on its own.

    The distinction matters because the failure it guards against is
    specifically a *recovery*: a daily-loss limit trips precisely when losing
    positions are open, and one of them closing at a profit lifts
    ``realised_pnl_today`` back over the threshold. A predicate would resume
    trading at that moment, silently, and only a session-length test can see
    the difference between a predicate and a latch.
    """

    @staticmethod
    def _healthy() -> dict:
        return {
            "symbol_sector": "IT",
            "correlations": {},
            "available_margin": Decimal("400000"),
            "margin_per_share": Decimal("240"),
            "atr": Decimal("13.5000"),
            "lot_size": 1,
        }

    def _session(self, calendar, **overrides):
        kwargs = {**self._healthy(), **overrides}
        session = Session(
            calendar,
            TRADING_DAY,
            checks=[
                *build_precondition_checks(calendar, NO_TRADE_WINDOWS),
                *build_eligibility_checks(),
                *build_exposure_checks(
                    max_correlated_positions=2,
                    correlation_threshold=Decimal("0.7"),
                    max_sector_exposure_pct=Decimal("40"),
                    max_net_directional_exposure_pct=Decimal("60"),
                ),
                *build_loss_checks(max_daily_loss_pct=Decimal("3.0"), consecutive_loss_halt=3),
                *build_margin_timing_checks(min_minutes_to_squareoff=30),
            ],
            **kwargs,
        )
        session.engine = RiskEngine(
            checks=list(session.engine.checks),
            sizer=build_sizer(_sizing_policy()),
            audit=session.audit.append,
        )
        return session

    @staticmethod
    def _driven_by(controller: HaltController, *, arm_at: dt.time, reason: HaltReason):
        """Read the REAL controller on every minute, arming it once at ``arm_at``.

        ``asyncio.run`` per minute is affordable against an in-memory store and
        is what keeps the controller genuinely in the loop rather than
        simulated by a boolean the test flips.
        """

        def mutate(ist_time: dt.time, kwargs: dict) -> None:
            if ist_time == arm_at:
                _run_async(
                    controller.arm(
                        reason,
                        detail="realised -3.2% against a 3.0% limit",
                        armed_by="risk-engine",
                        now=_at(TRADING_DAY, ist_time.hour, ist_time.minute),
                    )
                )
            state = _run_async(controller.state())
            kwargs["kill_switch_active"] = state.kill_switch
            kwargs["daily_loss_halted"] = state.daily_loss
            kwargs["consecutive_loss_halted"] = state.consecutive_loss

        return mutate

    def test_the_day_stops_at_the_minute_the_halt_is_armed(self, calendar) -> None:
        controller = HaltController(_SessionRedis())  # type: ignore[arg-type]
        session = self._session(calendar).run(
            mutate=self._driven_by(
                controller, arm_at=dt.time(12, 0), reason=HaltReason.DAILY_LOSS_LIMIT
            )
        )
        approved = sorted(t for t, d in session.decisions.items() if d.approved)
        assert approved, "the morning approved nothing, so the property is untested"
        assert approved[-1] == dt.time(11, 59)

    def test_the_day_never_comes_back(self, calendar) -> None:
        """Terminal. Not "stopped for a while" — every remaining minute of the
        session, to the close."""
        controller = HaltController(_SessionRedis())  # type: ignore[arg-type]
        session = self._session(calendar).run(
            mutate=self._driven_by(
                controller, arm_at=dt.time(12, 0), reason=HaltReason.DAILY_LOSS_LIMIT
            )
        )
        after = [t for t, d in session.decisions.items() if t >= dt.time(12, 0) and d.approved]
        assert after == [], f"trading resumed after the halt at {after[:5]}"

    def test_a_recovering_loss_figure_does_not_resume_trading(self, calendar) -> None:
        """The failure the latch exists for, at session scale.

        The halt is armed at 12:00 while the book is down. From 12:30 the
        realised P&L RECOVERS to healthy — a losing position closed at a
        profit. A predicate over ``realised_pnl_today`` would clear itself and
        trade again; the latch must not.
        """
        controller = HaltController(_SessionRedis())  # type: ignore[arg-type]
        driver = self._driven_by(
            controller, arm_at=dt.time(12, 0), reason=HaltReason.DAILY_LOSS_LIMIT
        )

        def mutate(ist_time: dt.time, kwargs: dict) -> None:
            driver(ist_time, kwargs)
            # The book heals; the halt must not.
            kwargs["realised_pnl_today"] = (
                Decimal("-16000") if ist_time < dt.time(12, 30) else Decimal("5000")
            )

        session = self._session(calendar).run(mutate=mutate)
        recovered = [t for t, d in session.decisions.items() if t >= dt.time(12, 30) and d.approved]
        assert recovered == [], (
            f"trading resumed once the P&L recovered — that is a predicate, not a "
            f"latch: {recovered[:5]}"
        )

    def test_the_halt_reason_reaches_the_rejection(self, calendar) -> None:
        """A halted day must say WHICH limit stopped it. Three latches exist so
        that this stays possible; one flag would have made every halt look the
        same."""
        controller = HaltController(_SessionRedis())  # type: ignore[arg-type]
        session = self._session(calendar).run(
            mutate=self._driven_by(
                controller, arm_at=dt.time(12, 0), reason=HaltReason.DAILY_LOSS_LIMIT
            )
        )
        assert session.decisions[dt.time(13, 0)].reason is RejectReason.DAILY_LOSS_LIMIT

    def test_a_manual_halt_reports_the_kill_switch_not_a_loss_limit(self, calendar) -> None:
        """The same walk with a different reason, to prove the reason is
        carried rather than assumed. An operator stopping the day manually must
        not appear in the metrics as a loss limit."""
        controller = HaltController(_SessionRedis())  # type: ignore[arg-type]
        session = self._session(calendar).run(
            mutate=self._driven_by(controller, arm_at=dt.time(12, 0), reason=HaltReason.MANUAL)
        )
        assert session.decisions[dt.time(13, 0)].reason is RejectReason.KILL_SWITCH_ACTIVE

    def test_only_an_operator_can_bring_the_day_back(self, calendar) -> None:
        """The other half of "terminal": it is not permanent, it is
        human-gated. A halt nothing could clear would be a different defect —
        the operator could never resume."""
        redis = _SessionRedis()
        controller = HaltController(redis)  # type: ignore[arg-type]
        driver = self._driven_by(
            controller, arm_at=dt.time(12, 0), reason=HaltReason.DAILY_LOSS_LIMIT
        )

        def mutate(ist_time: dt.time, kwargs: dict) -> None:
            if ist_time == dt.time(14, 0):
                _run_async(
                    controller.clear_all(
                        OperatorAction(
                            operator="gopikrishnan",
                            acknowledged="reviewed the drawdown and chose to resume",
                        )
                    )
                )
            driver(ist_time, kwargs)

        session = self._session(calendar).run(mutate=mutate)
        approved = sorted(t for t, d in session.decisions.items() if d.approved)
        assert dt.time(13, 0) not in approved, "the halt did not hold"
        assert dt.time(14, 0) in approved, "the operator could not resume the day"

    def test_the_halt_survives_a_restart_mid_session(self, calendar) -> None:
        """A crash at 12:30. The store outlives the process, so a fresh
        controller must find the day still halted — which is the whole reason
        the latch is persisted rather than held in memory."""
        redis = _SessionRedis()
        first = HaltController(redis)  # type: ignore[arg-type]

        def mutate(ist_time: dt.time, kwargs: dict) -> None:
            if ist_time == dt.time(12, 0):
                _run_async(
                    first.arm(
                        HaltReason.MARGIN_SHORTFALL,
                        detail="short by 12,000",
                        armed_by="risk",
                        now=_at(TRADING_DAY, 12, 0),
                    )
                )
            # From 12:30 a NEW controller instance serves every read.
            live = first if ist_time < dt.time(12, 30) else HaltController(redis)  # type: ignore[arg-type]
            state = _run_async(live.state())
            kwargs["kill_switch_active"] = state.kill_switch
            kwargs["daily_loss_halted"] = state.daily_loss
            kwargs["consecutive_loss_halted"] = state.consecutive_loss

        session = self._session(calendar).run(mutate=mutate)
        after = [t for t, d in session.decisions.items() if t >= dt.time(12, 30) and d.approved]
        assert after == [], f"the restarted process forgot the halt: {after[:5]}"

    def test_an_unreachable_store_halts_the_day_rather_than_opening_it(self, calendar) -> None:
        """Fail closed, at session scale. If the coordination layer breaks at
        12:00, the day must stop — that is precisely the circumstance in which
        it should."""

        class _DeadRedis(_SessionRedis):
            async def get(self, key: str) -> str | None:
                raise ConnectionError("redis is gone")

        redis = _SessionRedis()
        dead = _DeadRedis()

        def mutate(ist_time: dt.time, kwargs: dict) -> None:
            store = redis if ist_time < dt.time(12, 0) else dead
            state = _run_async(HaltController(store).state())  # type: ignore[arg-type]
            kwargs["kill_switch_active"] = state.kill_switch
            kwargs["daily_loss_halted"] = state.daily_loss
            kwargs["consecutive_loss_halted"] = state.consecutive_loss

        session = self._session(calendar).run(mutate=mutate)
        approved = sorted(t for t, d in session.decisions.items() if d.approved)
        assert approved, "nothing was approved before the outage"
        assert approved[-1] == dt.time(11, 59)

    def test_a_clean_store_trades_the_whole_day(self, calendar) -> None:
        """The control. Every assertion above is satisfied by a controller that
        always reports halted, which would stop the system trading forever."""
        controller = HaltController(_SessionRedis())  # type: ignore[arg-type]

        def mutate(_ist_time: dt.time, kwargs: dict) -> None:
            state = _run_async(controller.state())
            kwargs["kill_switch_active"] = state.kill_switch
            kwargs["daily_loss_halted"] = state.daily_loss
            kwargs["consecutive_loss_halted"] = state.consecutive_loss

        session = self._session(calendar).run(mutate=mutate)
        approved = [t for t, d in session.decisions.items() if d.approved]
        assert len(approved) == 316, f"a clean store did not trade the day: {len(approved)}"

    def test_no_credential_reached_the_log_on_a_halted_day(self, calendar) -> None:
        """The halt path logs CRITICAL with an operator name and a detail on
        every arm and clear, so this re-asserts for that shape."""
        import re

        controller = HaltController(_SessionRedis())  # type: ignore[arg-type]
        session = self._session(calendar).run(
            mutate=self._driven_by(controller, arm_at=dt.time(12, 0), reason=HaltReason.MANUAL)
        )
        assert not re.search(
            r"(api[_-]?key|secret|password|access[_-]?token)\s*[:=]\s*\S{8,}",
            session.log_text,
            re.IGNORECASE,
        )


# --------------------------------------------------------------------------
# SIT-14 — a session that reaches the broker (E15-S01)
# --------------------------------------------------------------------------


class _RecordingBroker:
    """Stands in for the broker, and records exactly what it was asked to do.

    The gateway is real, the risk engine is real, the sizer is real. Only the
    socket at the far end is substituted — which is the most that can be
    substituted while the claim is still "the system reaches a broker".

    ``fail_next_with`` and ``orderbook`` were added for E15-S02: a session
    that never fails cannot show that a failing one produces no duplicates.
    """

    def __init__(self) -> None:
        self.orders: list[object] = []
        self.fail_next_with: Exception | None = None
        self.orderbook: dict[str, object] = {}

    async def place_order(self, request) -> str:
        self.orders.append(request)
        if self.fail_next_with is not None:
            failure, self.fail_next_with = self.fail_next_with, None
            # The order LANDED; only the response was lost. This is the case
            # blind retry gets wrong, so it is the one the session replays.
            self.orderbook[request.client_order_id] = _landed_order(
                request, f"BROKER-{len(self.orders):06d}"
            )
            raise failure
        return f"BROKER-{len(self.orders):06d}"

    async def find_by_client_order_id(self, client_order_id: str):
        return self.orderbook.get(client_order_id)


def _landed_order(request, broker_order_id: str, status: OrderStatus = OrderStatus.OPEN):
    from algotrader.common.models.trading import Order

    return Order(
        client_order_id=request.client_order_id,
        broker_order_id=broker_order_id,
        correlation_id=request.correlation_id,
        symbol=request.symbol,
        side=request.side,
        order_type=request.order_type,
        product=request.product,
        quantity=request.quantity,
        status=status,
        intent=request.intent,
        placed_at=_at(TRADING_DAY, 10, 0),
        last_update_at=_at(TRADING_DAY, 10, 0),
    )


class _SessionOrderStore:
    """The order book as this system records it, for one session.

    Enforces ``uq_client_order`` the way the real table does. A session that
    tried to insert one idempotency key twice fails here rather than in
    production, which is the only place the constraint would otherwise speak.
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.insert_count = 0

    async def insert_submitting(self, order: dict) -> int:
        cid = order["client_order_id"]
        assert cid not in self.rows, f"uq_client_order: {cid} inserted twice"
        self.rows[cid] = {**order, "status": "SUBMITTING", "broker_order_id": None}
        self.insert_count += 1
        return self.insert_count

    async def attach_broker_id(self, client_order_id: str, broker_order_id: str) -> None:
        self.rows[client_order_id]["broker_order_id"] = broker_order_id
        self.rows[client_order_id]["status"] = "SUBMITTED"

    async def mark_rejected(self, client_order_id: str, *, reason: str | None) -> None:
        self.rows[client_order_id]["status"] = "REJECTED"
        self.rows[client_order_id]["rejection_reason"] = reason

    async def find_by_client_order_id(self, client_order_id: str):
        row = self.rows.get(client_order_id)
        return None if row is None else dict(row)


class TestSit14ASessionThatReachesABroker:
    """Every SIT group before this one ended at a `RiskDecision`. Ten stories
    have been walked across a session and none of them could produce an order,
    because there was nothing to produce one.

    This asks the question that only becomes askable now: across a whole
    trading day, does the number of orders that reach the broker equal the
    number of decisions the risk engine approved — and never exceed it?

    That is a *session*-scale claim. A component test shows one approval
    becoming one order. It cannot show that 316 approvals become 316 orders and
    not 317, nor that a halted afternoon sends nothing, nor that every order
    carries an id the recovery path could find it by.
    """

    @staticmethod
    def _healthy() -> dict:
        return {
            "symbol_sector": "IT",
            "correlations": {},
            "available_margin": Decimal("400000"),
            "margin_per_share": Decimal("240"),
            "atr": Decimal("13.5000"),
            "lot_size": 1,
        }

    def _session(self, calendar, **overrides):
        kwargs = {**self._healthy(), **overrides}
        session = Session(
            calendar,
            TRADING_DAY,
            checks=[
                *build_precondition_checks(calendar, NO_TRADE_WINDOWS),
                *build_eligibility_checks(),
                *build_exposure_checks(
                    max_correlated_positions=2,
                    correlation_threshold=Decimal("0.7"),
                    max_sector_exposure_pct=Decimal("40"),
                    max_net_directional_exposure_pct=Decimal("60"),
                ),
                *build_loss_checks(max_daily_loss_pct=Decimal("3.0"), consecutive_loss_halt=3),
                *build_margin_timing_checks(min_minutes_to_squareoff=30),
            ],
            **kwargs,
        )
        session.engine = RiskEngine(
            checks=list(session.engine.checks),
            sizer=build_sizer(_sizing_policy()),
            audit=session.audit.append,
        )
        return session

    @staticmethod
    def _submit_every_approval(session, broker):
        """Walk the day's decisions through the REAL gateway, in order."""
        gateway = OrderGateway(
            broker,
            policy=GatewayPolicy(algo_id="ALGO12345", market_protection=Decimal("-1")),
            store=_SessionOrderStore(),
        )
        submitted = []
        for ist_time in sorted(session.decisions):
            decision = session.decisions[ist_time]
            if not decision.approved:
                continue
            submitted.append(
                _run_async(
                    gateway.submit_entry(
                        decision,
                        _recommendation(_at(TRADING_DAY, ist_time.hour, ist_time.minute)),
                        trade_date=TRADING_DAY,
                    )
                )
            )
        return submitted

    def test_every_approval_becomes_exactly_one_order(self, calendar) -> None:
        """The count that could not be checked before this story existed."""
        session = self._session(calendar).run()
        broker = _RecordingBroker()
        submitted = self._submit_every_approval(session, broker)
        approved = [d for d in session.decisions.values() if d.approved]
        assert len(approved) == 316, "the tradable window moved; the rest is untrustworthy"
        assert len(broker.orders) == len(approved)
        assert len(submitted) == len(approved)

    def test_a_halted_day_sends_nothing(self, calendar) -> None:
        """The property that matters most, stated as a session: a kill switch
        does not merely change a reason code, it stops orders reaching a
        broker. Asserted at the SOCKET, which is the only place it counts."""
        session = self._session(calendar, kill_switch_active=True).run()
        broker = _RecordingBroker()
        self._submit_every_approval(session, broker)
        assert broker.orders == []

    def test_a_loss_limit_stops_the_orders_mid_session(self, calendar) -> None:
        def breach(ist_time: dt.time, kwargs: dict) -> None:
            if ist_time >= dt.time(12, 0):
                kwargs["daily_loss_halted"] = True

        session = self._session(calendar).run(mutate=breach)
        broker = _RecordingBroker()
        self._submit_every_approval(session, broker)
        assert broker.orders, "the morning sent nothing, so the property is untested"
        assert len(broker.orders) < 316

    def test_every_order_carries_what_the_sizer_decided(self, calendar) -> None:
        """The seam. A quantity that changed between the risk engine and the
        broker would be a position nobody sized."""
        session = self._session(calendar).run()
        broker = _RecordingBroker()
        self._submit_every_approval(session, broker)
        sized = {d.sizing.quantity for d in session.decisions.values() if d.approved}
        assert {o.quantity for o in broker.orders} == sized

    def test_a_configured_algo_id_reaches_every_one_of_them(self, calendar) -> None:
        """316 chances to drop it on the floor.

        RENAMED AND RE-ARGUED 9 Sep 2026. This used to claim an order without
        an Algo-ID was a compliance breach. It is not, below the registration
        threshold — see the test below. What this asserts is the mechanical
        property that survives either way: whatever the policy carries, every
        order carries, across a whole session and not just the first one.
        """
        session = self._session(calendar).run()
        broker = _RecordingBroker()
        self._submit_every_approval(session, broker)
        assert broker.orders
        assert all(o.algo_id == "ALGO12345" for o in broker.orders)

    def test_the_real_configuration_sends_no_algo_id_and_that_is_correct(self, calendar) -> None:
        """The session as it will actually be configured on day one.

        Below SEBI's 10 orders/sec threshold this algo is unregistered, so no
        Algo-ID is issued and none is sent; the broker tags the order with a
        generic identifier. Run across the full day because the failure mode
        worth excluding is an empty string leaking into the payload on some
        orders and not others — Zerodha would reject those and accept the
        rest, which is the hardest kind of defect to see.
        """
        session = self._session(calendar).run()
        broker = _RecordingBroker()
        gateway = OrderGateway(
            broker,
            policy=GatewayPolicy(algo_id=None, market_protection=Decimal("-1")),
            store=_SessionOrderStore(),
        )
        for ist_time in sorted(session.decisions):
            decision = session.decisions[ist_time]
            if not decision.approved:
                continue
            _run_async(
                gateway.submit_entry(
                    decision,
                    _recommendation(_at(TRADING_DAY, ist_time.hour, ist_time.minute)),
                    trade_date=TRADING_DAY,
                )
            )
        assert broker.orders
        assert all(o.algo_id is None for o in broker.orders)
        assert all(o.market_protection == Decimal("-1") for o in broker.orders)

    def test_every_order_carries_market_protection(self, calendar) -> None:
        session = self._session(calendar).run()
        broker = _RecordingBroker()
        self._submit_every_approval(session, broker)
        assert all(o.market_protection == Decimal("-1") for o in broker.orders)

    def test_every_order_is_findable_by_its_idempotency_key(self, calendar) -> None:
        """The recovery path searches the orderbook by tag. An order whose id
        could not round-trip through Kite's 20-character alphanumeric field
        would be invisible to it — and invisible means 'never landed', which
        means resubmit."""
        session = self._session(calendar).run()
        broker = _RecordingBroker()
        self._submit_every_approval(session, broker)
        assert broker.orders
        for order in broker.orders:
            assert order.client_order_id.isalnum()
            assert broker_tag(order.client_order_id) == order.client_order_id[:20]

    def test_resubmitting_the_same_signals_reuses_the_same_ids(self, calendar) -> None:
        """The idempotency claim, at session scale: re-submitting the SAME
        signals must produce the SAME ids, so a retry after a timeout finds the
        first order rather than opening a second position.

        The recommendations are minted ONCE and submitted twice. The first
        version of this test built them twice and failed — correctly, because
        ``_recommendation`` mints a fresh ``correlation_id`` per call, so the
        two runs were two different sets of signals rather than one set
        replayed. That is realistic (two signals really are two ids) and it is
        also the residual risk the gateway's docstring records from the other
        side: **the key is only stable because the correlation id is.**
        """
        session = self._session(calendar).run()
        approved = [
            (ist_time, session.decisions[ist_time])
            for ist_time in sorted(session.decisions)
            if session.decisions[ist_time].approved
        ]
        assert approved, "nothing was approved, so the property is untested"

        # One set of signals, minted once — this is what makes it a replay.
        signals = [
            (decision, _recommendation(_at(TRADING_DAY, t.hour, t.minute)))
            for t, decision in approved
        ]

        def submit_all() -> list[str]:
            broker = _RecordingBroker()
            gateway = OrderGateway(
                broker,
                policy=GatewayPolicy(algo_id="ALGO12345", market_protection=Decimal("-1")),
                store=_SessionOrderStore(),
            )
            for decision, rec in signals:
                _run_async(gateway.submit_entry(decision, rec, trade_date=TRADING_DAY))
            return [o.client_order_id for o in broker.orders]

        assert submit_all() == submit_all()

    def test_two_different_signals_never_share_an_id(self, calendar) -> None:
        """The other half. Every minute of the day is a distinct signal, so the
        316 ids must be 316 distinct values — a collision would suppress a real
        order as a duplicate of an unrelated one."""
        session = self._session(calendar).run()
        broker = _RecordingBroker()
        self._submit_every_approval(session, broker)
        ids = [o.client_order_id for o in broker.orders]
        assert len(ids) == len(set(ids)), "two different signals produced one id"

    def test_no_credential_reached_the_log_on_a_day_that_placed_orders(self, calendar) -> None:
        """Re-asserted now that the log carries broker order ids and tags."""
        import re

        session = self._session(calendar).run()
        self._submit_every_approval(session, _RecordingBroker())
        assert not re.search(
            r"(api[_-]?key|secret|password|access[_-]?token)\s*[:=]\s*\S{8,}",
            session.log_text,
            re.IGNORECASE,
        )


# --------------------------------------------------------------------------
# SIT-15 — a session that survives a broker timeout (E15-S02)
# --------------------------------------------------------------------------


class TestSit15ASessionThatSurvivesATimeout:
    """SIT-14 asked whether every approval becomes exactly one order on a
    HEALTHY day. This asks the question that only matters on a bad one.

    A component test can show that one timeout produces one order. It cannot
    show that a day containing a timeout still ends with as many orders as
    approvals — that the recovery path did not quietly drop the order it was
    recovering, and did not add one. That is a session-scale claim, and it is
    the claim under which real money is lost.
    """

    @staticmethod
    def _healthy() -> dict:
        return {
            "symbol_sector": "IT",
            "correlations": {},
            "available_margin": Decimal("400000"),
            "margin_per_share": Decimal("240"),
            "atr": Decimal("13.5000"),
            "lot_size": 1,
        }

    def _session(self, calendar, **overrides):
        kwargs = {**self._healthy(), **overrides}
        session = Session(
            calendar,
            TRADING_DAY,
            checks=[
                *build_precondition_checks(calendar, NO_TRADE_WINDOWS),
                *build_eligibility_checks(),
                *build_exposure_checks(
                    max_correlated_positions=2,
                    correlation_threshold=Decimal("0.7"),
                    max_sector_exposure_pct=Decimal("40"),
                    max_net_directional_exposure_pct=Decimal("60"),
                ),
                *build_loss_checks(max_daily_loss_pct=Decimal("3.0"), consecutive_loss_halt=3),
                *build_margin_timing_checks(min_minutes_to_squareoff=30),
            ],
            **kwargs,
        )
        session.engine = RiskEngine(
            checks=list(session.engine.checks),
            sizer=build_sizer(_sizing_policy()),
            audit=session.audit.append,
        )
        return session

    @staticmethod
    def _signals(session) -> list:
        """The day's approved signals, minted ONCE.

        `_recommendation` generates a fresh correlation_id on every call, and
        the correlation_id is the first component of the idempotency key. A
        helper that re-minted per run would make a "replay" a different day
        with different keys — and the replay test would pass while proving
        nothing. SIT-14 was corrected for exactly this; the trap is in the
        helper, so it catches every new caller too.
        """
        return [
            (session.decisions[t], _recommendation(_at(TRADING_DAY, t.hour, t.minute)))
            for t in sorted(session.decisions)
            if session.decisions[t].approved
        ]

    @staticmethod
    def _run_day(signals, broker, store, fail_at: int | None = None) -> list[str]:
        gateway = OrderGateway(
            broker,
            policy=GatewayPolicy(algo_id="ALGO12345", market_protection=Decimal("-1")),
            store=store,
        )
        ids: list[str] = []
        for decision, recommendation in signals:
            if fail_at is not None and len(ids) == fail_at:
                broker.fail_next_with = AmbiguousOrderError("read timed out")
            ids.append(
                _run_async(gateway.submit_entry(decision, recommendation, trade_date=TRADING_DAY))
            )
        return ids

    def test_a_timeout_mid_session_still_yields_one_order_per_approval(self, calendar) -> None:
        """The count SIT-14 established, re-asserted across a failure."""
        session = self._session(calendar).run()
        signals = self._signals(session)
        broker, store = _RecordingBroker(), _SessionOrderStore()
        ids = self._run_day(signals, broker, store, fail_at=100)
        approved = [d for d in session.decisions.values() if d.approved]
        assert len(ids) == len(approved) == 316
        assert len(set(ids)) == len(ids), "two approvals adopted one broker order"
        assert store.insert_count == len(approved)

    def test_the_timed_out_order_is_adopted_not_resent(self, calendar) -> None:
        """One `place_order` per approval even though one of them raised. The
        broker records the attempt that timed out, so the count proves the
        recovery path queried rather than resubmitting."""
        session = self._session(calendar).run()
        broker, store = _RecordingBroker(), _SessionOrderStore()
        self._run_day(self._signals(session), broker, store, fail_at=100)
        approved = [d for d in session.decisions.values() if d.approved]
        assert len(broker.orders) == len(approved)

    def test_replaying_the_whole_day_sends_nothing_the_second_time(self, calendar) -> None:
        """Restart, same store. Every order is already recorded, so a second
        pass must reach the broker zero times and return the same ids.

        This is the property that makes a crash-and-restart safe, and it is
        not observable in any single-order test.
        """
        session = self._session(calendar).run()
        signals = self._signals(session)
        broker, store = _RecordingBroker(), _SessionOrderStore()
        first = self._run_day(signals, broker, store)

        replay = _RecordingBroker()
        replay.orderbook = broker.orderbook
        second = self._run_day(signals, replay, store)

        assert first == second
        assert replay.orders == [], "the restart resubmitted the whole day"

    def test_no_credential_reached_the_log_on_a_day_that_timed_out(self, calendar) -> None:
        """Re-asserted on the failure path: recovery logs carry ids, statuses
        and broker exception text, none of which may carry a secret."""
        import re

        session = self._session(calendar).run()
        self._run_day(self._signals(session), _RecordingBroker(), _SessionOrderStore(), fail_at=10)
        assert not re.search(
            r"(api[_-]?key|secret|password|access[_-]?token)\s*[:=]\s*\S{8,}",
            session.log_text,
            re.IGNORECASE,
        )


# --------------------------------------------------------------------------
# SIT-16 — a session that restarts onto a store it did not leave (E15-S03)
# --------------------------------------------------------------------------


class TestSit16ASessionThatRefusesToResumeWhatItCannotResume:
    """SIT-15 asked what a session does when the BROKER misbehaves. This asks
    what it does when its OWN RECORDS are not what it expects.

    That is the realistic restart: the process died yesterday, or a reconciler
    that does not exist yet (E15-S09) wrote a status, or a row was edited by
    hand during an incident. A component test shows one row being refused. It
    cannot show that a whole day's worth of decisions meeting a poisoned store
    produces zero orders rather than a few — and "a few" is the outcome that
    opens positions nobody chose.
    """

    @staticmethod
    def _healthy() -> dict:
        return {
            "symbol_sector": "IT",
            "correlations": {},
            "available_margin": Decimal("400000"),
            "margin_per_share": Decimal("240"),
            "atr": Decimal("13.5000"),
            "lot_size": 1,
        }

    def _session(self, calendar, **overrides):
        kwargs = {**self._healthy(), **overrides}
        session = Session(
            calendar,
            TRADING_DAY,
            checks=[
                *build_precondition_checks(calendar, NO_TRADE_WINDOWS),
                *build_eligibility_checks(),
                *build_exposure_checks(
                    max_correlated_positions=2,
                    correlation_threshold=Decimal("0.7"),
                    max_sector_exposure_pct=Decimal("40"),
                    max_net_directional_exposure_pct=Decimal("60"),
                ),
                *build_loss_checks(max_daily_loss_pct=Decimal("3.0"), consecutive_loss_halt=3),
                *build_margin_timing_checks(min_minutes_to_squareoff=30),
            ],
            **kwargs,
        )
        session.engine = RiskEngine(
            checks=list(session.engine.checks),
            sizer=build_sizer(_sizing_policy()),
            audit=session.audit.append,
        )
        return session

    @staticmethod
    def _signals(session) -> list:
        return [
            (session.decisions[t], _recommendation(_at(TRADING_DAY, t.hour, t.minute)))
            for t in sorted(session.decisions)
            if session.decisions[t].approved
        ]

    @staticmethod
    def _gateway(broker, store):
        return OrderGateway(
            broker,
            policy=GatewayPolicy(algo_id="ALGO12345", market_protection=Decimal("-1")),
            store=store,
        )

    def _poison(self, signals, store, status: str) -> None:
        """Record every one of the day's orders in `status`, as a previous run
        would have left them. No broker id: these are rows for orders that
        either finished or were never completed, not orders in flight."""
        gateway = self._gateway(_RecordingBroker(), store)
        for decision, rec in signals:
            request = gateway.build_entry(decision, rec, trade_date=TRADING_DAY)
            store.rows[request.client_order_id] = {
                "client_order_id": request.client_order_id,
                "broker_order_id": None,
                "status": status,
            }

    @pytest.mark.parametrize("status", ["FILLED", "CANCELLED", "REJECTED"])
    def test_a_day_of_finished_rows_produces_no_orders_at_all(self, calendar, status: str) -> None:
        """Terminal is terminal at session scale. Every one of 316 decisions
        meets a row the state machine will not move, and the count that matters
        is zero — not "most"."""
        session = self._session(calendar).run()
        signals = self._signals(session)
        broker, store = _RecordingBroker(), _SessionOrderStore()
        self._poison(signals, store, status)

        gateway = self._gateway(broker, store)
        refused = 0
        for decision, rec in signals:
            try:
                _run_async(gateway.submit_entry(decision, rec, trade_date=TRADING_DAY))
            except IllegalTransitionError:
                refused += 1
        assert refused == len(signals) == 316
        assert broker.orders == [], "a finished order was resubmitted"

    def test_a_reconcile_owned_day_is_left_to_reconciliation(self, calendar) -> None:
        """RECONCILE_REQUIRED cannot return to SUBMITTED: §8.3 gives those
        orders to the reconciliation loop, and a second owner is how one gets
        submitted twice."""
        session = self._session(calendar).run()
        signals = self._signals(session)
        broker, store = _RecordingBroker(), _SessionOrderStore()
        self._poison(signals, store, "RECONCILE_REQUIRED")

        gateway = self._gateway(broker, store)
        for decision, rec in signals:
            with pytest.raises(IllegalTransitionError):
                _run_async(gateway.submit_entry(decision, rec, trade_date=TRADING_DAY))
        assert broker.orders == []

    def test_an_unreadable_status_stops_the_day_rather_than_guessing(self, calendar) -> None:
        """Corrupt a cached value; the system must refuse, not guess. There is
        no safe default for a status we cannot parse — treating it as
        SUBMITTING would adopt a corrupted row as though it were in flight."""
        session = self._session(calendar).run()
        signals = self._signals(session)
        broker, store = _RecordingBroker(), _SessionOrderStore()
        self._poison(signals, store, "NOT_A_STATUS")

        gateway = self._gateway(broker, store)
        for decision, rec in signals:
            with pytest.raises(GatewayError, match="not a modelled OrderStatus"):
                _run_async(gateway.submit_entry(decision, rec, trade_date=TRADING_DAY))
        assert broker.orders == []

    def test_a_clean_store_still_trades_the_whole_day(self, calendar) -> None:
        """The control, and it is not optional. Every test above asserts that
        the session refuses; without this one, a gateway that refused
        everything would pass all of them and the day would simply never
        trade."""
        session = self._session(calendar).run()
        signals = self._signals(session)
        broker, store = _RecordingBroker(), _SessionOrderStore()

        gateway = self._gateway(broker, store)
        ids = [
            _run_async(gateway.submit_entry(decision, rec, trade_date=TRADING_DAY))
            for decision, rec in signals
        ]
        assert len(ids) == len(broker.orders) == 316
        assert all(row["status"] == "SUBMITTED" for row in store.rows.values())

    def test_a_half_poisoned_day_trades_exactly_the_clean_half(self, calendar) -> None:
        """The one that would catch an off-by-one in the refusal. A store where
        alternate orders are finished must produce exactly the other half — not
        all, not none."""
        session = self._session(calendar).run()
        signals = self._signals(session)
        broker, store = _RecordingBroker(), _SessionOrderStore()
        gateway = self._gateway(broker, store)

        poisoned = set()
        for index, (decision, rec) in enumerate(signals):
            if index % 2:
                continue
            request = gateway.build_entry(decision, rec, trade_date=TRADING_DAY)
            store.rows[request.client_order_id] = {
                "client_order_id": request.client_order_id,
                "broker_order_id": None,
                "status": "FILLED",
            }
            poisoned.add(request.client_order_id)

        placed = 0
        for decision, rec in signals:
            try:
                _run_async(gateway.submit_entry(decision, rec, trade_date=TRADING_DAY))
                placed += 1
            except IllegalTransitionError:
                pass
        assert placed == len(signals) - len(poisoned)
        assert len(broker.orders) == placed
        assert not ({o.client_order_id for o in broker.orders} & poisoned)


# --------------------------------------------------------------------------
# SIT-17 — a session whose positions are protected, or are not positions
#          (E15-S04)
# --------------------------------------------------------------------------


class _OrderbookBroker:
    """A broker that LISTS what it accepted — which SIT-15's did not need to.

    ``_RecordingBroker`` fills its orderbook only when a response was lost,
    because a timeout was the only reason SIT-15 ever queried. The protective
    stop queries on the HAPPY path too: submitted is not protecting, so every
    attach asks the orderbook. A broker that recorded nothing would make every
    stop look dead, and every test here would pass for the wrong reason.

    The knobs are kept separate because the module treats them separately:
    ``refuse`` is a synchronous rejection, ``hide`` is an order accepted and
    then not listed, and ``status_for`` is an order listed in a state that is
    not protection. Collapsing them would hide the distinction this story is
    about.
    """

    def __init__(self) -> None:
        self.orders: list[object] = []
        self.orderbook: dict[str, object] = {}
        self.refuse: frozenset[OrderIntent] = frozenset()
        self.hide: frozenset[OrderIntent] = frozenset()
        self.status_for: dict[OrderIntent, OrderStatus] = {}

    async def place_order(self, request) -> str:
        self.orders.append(request)
        if request.intent in self.refuse:
            raise OrderRejectedError(
                f"{request.intent.value} refused by RMS", reason_code="RMS_REJECT"
            )
        broker_order_id = f"BROKER-{len(self.orders):06d}"
        if request.intent not in self.hide:
            self.orderbook[request.client_order_id] = _landed_order(
                request,
                broker_order_id,
                status=self.status_for.get(request.intent, OrderStatus.OPEN),
            )
        return broker_order_id

    async def find_by_client_order_id(self, client_order_id: str):
        return self.orderbook.get(client_order_id)

    def intents(self, intent: OrderIntent) -> list:
        return [o for o in self.orders if o.intent is intent]


class TestSit17ASessionWhosePositionsAreProtectedOrClosed:
    """SIT-14 through SIT-16 end at an order reaching the broker. This asks
    what happens *after* one fills, which is where the fifth invariant lives:
    every position has a stop.

    The component suite shows one position being protected, and one failure
    being contained. It cannot show the three things that only exist at
    session scale, and all three are the ones that would cost money:

    * that a day of 316 fills yields 316 stops and not 315 — the missing one
      being a real position with no exit but the square-off deadline;
    * that a day on which the broker refuses EVERY stop still ends with
      nothing open and the session still trading, because a contained failure
      must not stop the day — "does not halt" is a claim about 316 failures,
      not about one;
    * that when the exits fail too, the escalation actually stops the day
      rather than merely raising — which needs the halt latch and the risk
      engine in the same test.

    **The trigger is the test.** E15-S05 owns the fill-confirmation hook that
    will call ``attach`` in production; until it exists, this file plays that
    part deliberately, and says so rather than letting a reader infer wiring
    that is not there.
    """

    @staticmethod
    def _healthy() -> dict:
        return {
            "symbol_sector": "IT",
            "correlations": {},
            "available_margin": Decimal("400000"),
            "margin_per_share": Decimal("240"),
            "atr": Decimal("13.5000"),
            "lot_size": 1,
        }

    def _session(self, calendar, **overrides):
        kwargs = {**self._healthy(), **overrides}
        session = Session(
            calendar,
            TRADING_DAY,
            checks=[
                *build_precondition_checks(calendar, NO_TRADE_WINDOWS),
                *build_eligibility_checks(),
                *build_exposure_checks(
                    max_correlated_positions=2,
                    correlation_threshold=Decimal("0.7"),
                    max_sector_exposure_pct=Decimal("40"),
                    max_net_directional_exposure_pct=Decimal("60"),
                ),
                *build_loss_checks(max_daily_loss_pct=Decimal("3.0"), consecutive_loss_halt=3),
                *build_margin_timing_checks(min_minutes_to_squareoff=30),
            ],
            **kwargs,
        )
        session.engine = RiskEngine(
            checks=list(session.engine.checks),
            sizer=build_sizer(_sizing_policy()),
            audit=session.audit.append,
        )
        return session

    @staticmethod
    def _signals(session) -> list:
        """The day's approved signals, minted ONCE — see SIT-15's note.

        It matters more here than there. The stop's idempotency key is derived
        from the POSITION's correlation_id, so a helper that re-minted would
        give the same position a different stop on every call, and the replay
        test would prove nothing while passing.
        """
        return [
            (session.decisions[t], _recommendation(_at(TRADING_DAY, t.hour, t.minute)), t)
            for t in sorted(session.decisions)
            if session.decisions[t].approved
        ]

    @staticmethod
    def _positions(signals) -> list[Position]:
        """The day's fills, as the position manager will one day record them."""
        positions = []
        for index, (decision, recommendation, ist_time) in enumerate(signals):
            sizing = decision.sizing
            assert sizing is not None and sizing.quantity > 0, "an approval sized to nothing"
            positions.append(
                Position(
                    correlation_id=recommendation.correlation_id,
                    symbol=recommendation.symbol,
                    slot_index=index % 5,
                    direction=recommendation.direction,
                    quantity=sizing.quantity,
                    entry_price=sizing.entry_price,
                    stop_price=sizing.stop_price,
                    opened_at=_at(TRADING_DAY, ist_time.hour, ist_time.minute),
                    squareoff_deadline=_at(TRADING_DAY, 15, 15),
                )
            )
        return positions

    @staticmethod
    def _attacher(broker, store, controller):
        """The REAL gateway and the REAL halt controller. Only the socket and
        the row store are substituted, which is the most that can be while the
        claim is still "the assembled system protects its positions"."""
        gateway = OrderGateway(
            broker,
            policy=GatewayPolicy(algo_id="ALGO12345", market_protection=Decimal("-1")),
            store=store,
        )
        return gateway, ProtectiveStop(gateway, halter=controller, metrics=get_metrics())

    @staticmethod
    def _counter(name: str) -> float:
        for metric in get_metrics().registry.collect():
            for sample in metric.samples:
                if sample.name == name:
                    return sample.value
        raise AssertionError(f"{name} is registered nowhere")

    def _walk(self, positions, attacher, *, stop_on_first_raise=False):
        """Attach a stop to every position in the day's order.

        Returns the established ones and the failures, because both counts are
        assertions somewhere below and "most of them worked" is never the
        claim.
        """
        established, failures = [], []
        for position in positions:
            try:
                established.append(
                    _run_async(
                        attacher.attach(
                            position,
                            trade_date=TRADING_DAY,
                            now=position.opened_at,
                        )
                    )
                )
            except Exception as exc:
                failures.append((position, exc))
                if stop_on_first_raise:
                    break
        return established, failures

    # -- the healthy day, which is also the control --------------------------

    def test_every_filled_position_gets_exactly_one_live_stop(self, calendar) -> None:
        """The count SIT-14 established for entries, one component along.

        This is also the control for every test below it: without it, an
        attacher that closed every position would satisfy all the failure
        assertions and the book would simply never hold anything.
        """
        session = self._session(calendar).run()
        positions = self._positions(self._signals(session))
        broker, store = _OrderbookBroker(), _SessionOrderStore()
        _gateway, attacher = self._attacher(broker, store, HaltController(_SessionRedis()))

        established, failures = self._walk(positions, attacher)

        assert len(positions) == 316, "the tradable window moved; the rest is untrustworthy"
        assert failures == []
        assert len(established) == len(positions)
        assert len(broker.intents(OrderIntent.STOP)) == len(positions)
        assert broker.intents(OrderIntent.SQUAREOFF) == [], "a healthy day exited a position"
        assert len({e.stop_client_order_id for e in established}) == len(positions), (
            "two positions were protected by one stop order"
        )

    def test_no_position_is_established_without_its_stop_reaching_the_broker(
        self, calendar
    ) -> None:
        """Invariant 5, asserted at the SOCKET rather than in the database.

        The entry goes through the real gateway too, so the pairing checked is
        the one that exists in production: for every ENTRY that reached the
        broker there is a STOP that reached it, carrying the key derived from
        the position — not merely a row saying ``stop_price``.
        """
        session = self._session(calendar).run()
        signals = self._signals(session)
        positions = self._positions(signals)
        broker, store = _OrderbookBroker(), _SessionOrderStore()
        gateway, attacher = self._attacher(broker, store, HaltController(_SessionRedis()))

        for decision, recommendation, _t in signals:
            _run_async(gateway.submit_entry(decision, recommendation, trade_date=TRADING_DAY))
        established, _failures = self._walk(positions, attacher)

        entries = broker.intents(OrderIntent.ENTRY)
        stops = {o.client_order_id for o in broker.intents(OrderIntent.STOP)}
        assert len(entries) == len(positions) == len(established)
        assert stops == {stop_client_order_id(p, trade_date=TRADING_DAY) for p in positions}, (
            "an entry reached the broker whose stop did not"
        )

    # -- the day the broker refuses every stop -------------------------------

    def test_a_day_of_refused_stops_leaves_nothing_open_and_keeps_trading(self, calendar) -> None:
        """The claim that is only a claim at session scale.

        One contained failure is a unit test. Three hundred and sixteen of them
        not halting the day is the actual policy — one bad symbol must not stop
        trading — and an implementation that escalated on the tenth would pass
        every component test written for this story.
        """
        session = self._session(calendar).run()
        positions = self._positions(self._signals(session))
        broker, store = _OrderbookBroker(), _SessionOrderStore()
        broker.refuse = frozenset({OrderIntent.STOP})
        controller = HaltController(_SessionRedis())
        _gateway, attacher = self._attacher(broker, store, controller)

        established, failures = self._walk(positions, attacher)

        assert established == [], "a position was called protected with no stop"
        assert len(failures) == len(positions)
        assert all(isinstance(exc, StopNotEstablishedError) for _p, exc in failures)
        assert len(broker.intents(OrderIntent.SQUAREOFF)) == len(positions)
        assert not _run_async(controller.is_halted()), (
            "a contained failure stopped the day; one bad symbol must not"
        )
        assert self._counter("stop_attach_failures_total") == len(positions)
        assert self._counter("naked_positions_total") == 0

    def test_a_day_of_refused_stops_leaves_no_row_claiming_to_be_in_flight(self, calendar) -> None:
        """SIT-003, and the reason it was found here rather than in the unit
        suite: the defect is in what the day LEAVES BEHIND, not in what it
        does. Every position was exited correctly and every exception was the
        right one — and the store finished holding 316 rows that said an order
        was in flight at a broker that had refused it.

        Stated as a count over the whole store, because "the reconciliation
        working set is clean at the end of the day" is the actual claim and
        one row is not a working set.
        """
        session = self._session(calendar).run()
        positions = self._positions(self._signals(session))
        broker, store = _OrderbookBroker(), _SessionOrderStore()
        broker.refuse = frozenset({OrderIntent.STOP})
        _gateway, attacher = self._attacher(broker, store, HaltController(_SessionRedis()))

        self._walk(positions, attacher)

        stops = [r for r in store.rows.values() if r["intent"] == OrderIntent.STOP.value]
        exits = [r for r in store.rows.values() if r["intent"] == OrderIntent.SQUAREOFF.value]
        assert len(stops) == len(exits) == len(positions)
        assert {r["status"] for r in stops} == {"REJECTED"}
        assert {r["status"] for r in exits} == {"SUBMITTED"}
        assert all(r["rejection_reason"] for r in stops), (
            "the broker's reason for refusing 316 stops was recorded nowhere"
        )

    def test_a_stop_the_broker_accepts_and_never_lists_is_treated_as_absent(self, calendar) -> None:
        """Acceptance is not liveness, asserted across a whole day.

        Distinct from the test above in the only way that matters: here every
        submission SUCCEEDS. A system that trusted its own submission would
        report a fully protected book and be wrong 316 times, with no exception
        anywhere to notice.
        """
        session = self._session(calendar).run()
        positions = self._positions(self._signals(session))
        broker, store = _OrderbookBroker(), _SessionOrderStore()
        broker.hide = frozenset({OrderIntent.STOP})
        _gateway, attacher = self._attacher(broker, store, HaltController(_SessionRedis()))

        established, failures = self._walk(positions, attacher)

        assert established == []
        assert len(broker.intents(OrderIntent.STOP)) == len(positions), (
            "the stops were never submitted, so liveness is not what was tested"
        )
        assert len(broker.intents(OrderIntent.SQUAREOFF)) == len(positions)
        assert len(failures) == len(positions)

    def test_a_stop_listed_as_rejected_is_not_protection(self, calendar) -> None:
        """The third shape: submitted, listed, and dead. Same outcome."""
        session = self._session(calendar).run()
        positions = self._positions(self._signals(session))
        broker, store = _OrderbookBroker(), _SessionOrderStore()
        broker.status_for = {OrderIntent.STOP: OrderStatus.REJECTED}
        _gateway, attacher = self._attacher(broker, store, HaltController(_SessionRedis()))

        established, failures = self._walk(positions, attacher)

        assert established == []
        assert len(failures) == len(positions)
        assert len(broker.intents(OrderIntent.SQUAREOFF)) == len(positions)

    # -- the day the exits fail too ------------------------------------------

    def test_a_naked_position_halts_the_session_and_the_day_stops(self, calendar) -> None:
        """The escalation, composed with the thing it escalates to.

        The unit suite asserts that ``arm`` was called. That is a claim about a
        method. The claim that matters is that the DAY stops, and it needs the
        real latch and the real risk engine in one test: a halt that armed a
        latch nothing reads would satisfy every component assertion while the
        session carried on opening positions.
        """
        session = self._session(calendar).run()
        positions = self._positions(self._signals(session))
        broker, store = _OrderbookBroker(), _SessionOrderStore()
        broker.refuse = frozenset({OrderIntent.STOP, OrderIntent.SQUAREOFF})
        controller = HaltController(_SessionRedis())
        _gateway, attacher = self._attacher(broker, store, controller)

        _established, failures = self._walk(positions, attacher, stop_on_first_raise=True)

        assert len(failures) == 1
        assert isinstance(failures[0][1], NakedPositionError)
        state = _run_async(controller.state())
        assert state.kill_switch, "a naked position did not arm the kill switch"

        after = self._session(calendar, kill_switch_active=state.kill_switch).run()
        assert [d for d in after.decisions.values() if d.approved] == [], (
            "the session kept approving after a position was left naked"
        )
        assert self._counter("naked_positions_total") == 1

    def test_the_halt_names_the_naked_position_rather_than_a_neighbour(self, calendar) -> None:
        """``UNKNOWN_POSITION`` means the broker reports a position we have no
        record of — a different fact, and the one an operator would chase down
        the wrong path at the worst possible moment."""
        session = self._session(calendar).run()
        positions = self._positions(self._signals(session))
        broker, store = _OrderbookBroker(), _SessionOrderStore()
        broker.refuse = frozenset({OrderIntent.STOP, OrderIntent.SQUAREOFF})
        controller = HaltController(_SessionRedis())
        _gateway, attacher = self._attacher(broker, store, controller)

        self._walk(positions, attacher, stop_on_first_raise=True)

        record = next(iter(_run_async(controller.state()).records.values()))
        assert record.reason is HaltReason.NAKED_POSITION
        assert positions[0].symbol in record.detail
        assert record.armed_by == "protective-stop"

    # -- restart, and the predicate the reconciler will call ------------------

    def test_replaying_the_days_attachments_sends_nothing_the_second_time(self, calendar) -> None:
        """The property that makes the derived key safe across a restart.

        The stop carries no column in ``positions``; its key is recomputed from
        the position. So a process that dies after protecting the book and
        comes back must recompute the SAME keys and place nothing — and that is
        a claim about 316 derivations agreeing, not one.
        """
        session = self._session(calendar).run()
        positions = self._positions(self._signals(session))
        broker, store = _OrderbookBroker(), _SessionOrderStore()
        _gateway, attacher = self._attacher(broker, store, HaltController(_SessionRedis()))
        first, _ = self._walk(positions, attacher)

        replay = _OrderbookBroker()
        replay.orderbook = broker.orderbook
        _g2, attacher2 = self._attacher(replay, store, HaltController(_SessionRedis()))
        second, failures = self._walk(positions, attacher2)

        assert failures == []
        assert [e.stop_broker_order_id for e in first] == [e.stop_broker_order_id for e in second]
        assert replay.orders == [], "the restart placed a second stop on every position"

    def test_is_protected_finds_the_one_stop_that_died_after_it_was_verified(
        self, calendar
    ) -> None:
        """The predicate E15-S09 must call, walked across the whole book.

        Every stop here was verified live at attach time, so no synchronous
        check can see the failure — it happens afterwards, which is exactly the
        case this story's build concern names. The count is the assertion: 315
        still protected and EXACTLY one not, because a predicate that answered
        False for the whole book would also "find" it.
        """
        session = self._session(calendar).run()
        positions = self._positions(self._signals(session))
        broker, store = _OrderbookBroker(), _SessionOrderStore()
        _gateway, attacher = self._attacher(broker, store, HaltController(_SessionRedis()))
        established, _ = self._walk(positions, attacher)
        assert len(established) == len(positions)

        casualty = positions[100]
        cid = stop_client_order_id(casualty, trade_date=TRADING_DAY)
        broker.orderbook[cid] = broker.orderbook[cid].model_copy(
            update={"status": OrderStatus.REJECTED}
        )

        unprotected = [
            p for p in positions if not _run_async(attacher.is_protected(p, trade_date=TRADING_DAY))
        ]
        assert unprotected == [casualty]

    # -- cross-cutting -------------------------------------------------------

    def test_no_credential_reached_the_log_on_a_day_of_naked_positions(self, calendar) -> None:
        """The CRITICAL path logs broker exception text, which is the one place
        a rejection reason from an untrusted source enters a log line."""
        import re

        session = self._session(calendar).run()
        positions = self._positions(self._signals(session))
        broker, store = _OrderbookBroker(), _SessionOrderStore()
        broker.refuse = frozenset({OrderIntent.STOP})
        _gateway, attacher = self._attacher(broker, store, HaltController(_SessionRedis()))
        self._walk(positions[:20], attacher)

        assert not re.search(
            r"(api[_-]?key|secret|password|access[_-]?token)\s*[:=]\s*\S{8,}",
            session.log_text,
            re.IGNORECASE,
        )


# --------------------------------------------------------------------------
# SIT-18 — a session whose book is the book it actually holds (E15-S05)
# --------------------------------------------------------------------------


class _UniqueViolationError(Exception):
    """What Postgres raises when a partial unique index refuses an insert.

    ``PositionRepository.open_position`` documents this as "a normal, expected
    outcome under concurrency, not an error to surface - callers must catch it
    and treat it as 'slot taken'".
    """


class _SessionPositionStore:
    """The positions table for one session, with the constraints that bite.

    Enforces ``uq_open_slot`` the way the real partial index does: two OPEN
    positions cannot share a slot. A session that leaked slots fails here
    rather than in production, where the index is the only thing standing
    between a recycled slot and two positions on one line of capital.
    """

    def __init__(self) -> None:
        self.rows: dict[int, dict] = {}
        self.next_id = 0

    async def open_position(self, position: dict) -> int:
        slot, symbol = position["slot_index"], position["symbol"]
        open_rows = [r for r in self.rows.values() if r["status"] == "OPEN"]
        if slot in {r["slot_index"] for r in open_rows}:
            raise _UniqueViolationError(f"uq_open_slot: slot {slot} already holds a position")
        if symbol in {r["symbol"] for r in open_rows}:
            raise _UniqueViolationError(f"uq_open_symbol: {symbol} is already held")
        self.next_id += 1
        self.rows[self.next_id] = {
            **position,
            "position_id": self.next_id,
            "status": "OPEN",
            "closed_at": None,
            "exit_price": None,
            "exit_reason": None,
            "realized_pnl": None,
            "max_favourable_excursion": None,
            "max_adverse_excursion": None,
        }
        return self.next_id

    async def open_positions(self) -> list[dict]:
        return [
            {k: v for k, v in r.items() if k != "strategy_id"}
            for r in self.rows.values()
            if r["status"] == "OPEN"
        ]

    async def close_position(self, position_id: int, **kwargs) -> None:
        self.rows[position_id].update(status="CLOSED", **kwargs)

    @property
    def open_count(self) -> int:
        return sum(1 for r in self.rows.values() if r["status"] == "OPEN")


class _DictMirror:
    def __init__(self) -> None:
        self.data: dict[str, object] = {}

    async def write(self, symbol: str, snapshot) -> None:
        self.data[symbol] = snapshot

    async def clear(self, symbol: str) -> None:
        self.data.pop(symbol, None)


class _SessionQuote:
    def __init__(self, symbol: str, ltp: Decimal, as_of: dt.datetime) -> None:
        self.symbol = symbol
        self.ltp = ltp
        self.as_of = as_of


class TestSit18ASessionWhoseBookIsWhatItHolds:
    """SIT-17 ends when a stop is verified live. This asks the question that
    starts there: across a whole day, is the book the system reasons about the
    book it is actually holding?

    Three claims are session-scale and only session-scale:

    * **A day of partial fills sums to what filled, not what was ordered.** One
      partial fill is a unit test. Three hundred and sixteen of them is where a
      systematic off-by-one becomes a book that is wrong by thousands of
      shares — and every stop, every exposure check and every exit is sized
      from that number.
    * **A day of failed stops ends with the book EMPTY**, not merely with 316
      exceptions raised. The positions have to be exited *and their exits
      confirmed*, and "confirmed" is the half E15-S04 could not deliver.
    * **A restart mid-session does not silently re-protect the book.** Every
      restored position must come back unverified, all 316 of them, because
      one presumed-protected position is one naked position.
    """

    @staticmethod
    def _healthy() -> dict:
        return {
            "symbol_sector": "IT",
            "correlations": {},
            "available_margin": Decimal("400000"),
            "margin_per_share": Decimal("240"),
            "atr": Decimal("13.5000"),
            "lot_size": 1,
        }

    def _session(self, calendar, **overrides):
        kwargs = {**self._healthy(), **overrides}
        session = Session(
            calendar,
            TRADING_DAY,
            checks=[
                *build_precondition_checks(calendar, NO_TRADE_WINDOWS),
                *build_eligibility_checks(),
                *build_exposure_checks(
                    max_correlated_positions=2,
                    correlation_threshold=Decimal("0.7"),
                    max_sector_exposure_pct=Decimal("40"),
                    max_net_directional_exposure_pct=Decimal("60"),
                ),
                *build_loss_checks(max_daily_loss_pct=Decimal("3.0"), consecutive_loss_halt=3),
                *build_margin_timing_checks(min_minutes_to_squareoff=30),
            ],
            **kwargs,
        )
        session.engine = RiskEngine(
            checks=list(session.engine.checks),
            sizer=build_sizer(_sizing_policy()),
            audit=session.audit.append,
        )
        return session

    @staticmethod
    def _signals(session) -> list:
        """The day's approved signals, minted ONCE — SIT-15's note applies."""
        return [
            (session.decisions[t], _recommendation(_at(TRADING_DAY, t.hour, t.minute)), t)
            for t in sorted(session.decisions)
            if session.decisions[t].approved
        ]

    @staticmethod
    def _manager(broker, store, mirror, calendar, halter=None):
        gateway = OrderGateway(
            broker,
            policy=GatewayPolicy(algo_id="ALGO12345", market_protection=Decimal("-1")),
            store=_SessionOrderStore(),
        )
        protector = ProtectiveStop(
            gateway, halter=halter or HaltController(_SessionRedis()), metrics=get_metrics()
        )
        return PositionManager(
            protector=protector,
            store=store,
            mirror=mirror,
            calendar=calendar,
            metrics=get_metrics(),
        )

    @staticmethod
    def _fill(recommendation, decision, *, filled_fraction: Decimal, symbol: str) -> Order:
        """What the broker reports about an entry that filled.

        ``filled_quantity`` is what the book must believe, and it is
        deliberately not ``quantity`` — this is the number the whole story
        turns on.
        """
        ordered = decision.sizing.quantity
        filled = max(1, int(ordered * filled_fraction))
        return Order(
            client_order_id=f"E{recommendation.correlation_id.hex[:24]}",
            broker_order_id="BROKER-ENTRY",
            correlation_id=recommendation.correlation_id,
            symbol=symbol,
            side=Side.BUY,
            order_type=OrderType.MARKET,
            product=Product.MIS,
            quantity=ordered,
            status=OrderStatus.FILLED if filled == ordered else OrderStatus.OPEN,
            filled_quantity=filled,
            average_price=Decimal("1200.0000"),
            intent=OrderIntent.ENTRY,
            placed_at=_at(TRADING_DAY, 10, 0),
            last_update_at=_at(TRADING_DAY, 10, 0),
        )

    @staticmethod
    def _exit_fill(position, *, price: str = "1195.0000") -> Order:
        return Order(
            client_order_id=f"X{position.correlation_id.hex[:24]}",
            broker_order_id="BROKER-EXIT",
            correlation_id=position.correlation_id,
            symbol=position.symbol,
            side=Side.SELL,
            order_type=OrderType.MARKET,
            product=Product.MIS,
            quantity=position.quantity,
            status=OrderStatus.FILLED,
            filled_quantity=position.quantity,
            average_price=Decimal(price),
            intent=OrderIntent.SQUAREOFF,
            placed_at=_at(TRADING_DAY, 15, 0),
            last_update_at=_at(TRADING_DAY, 15, 0),
        )

    def _walk_the_day(self, session, manager, *, filled_fraction=Decimal("1"), limit=None):
        """Open a position for every approval, as the day produces them.

        One symbol per signal so the slot discipline is exercised rather than
        sidestepped; the store asserts ``uq_open_slot`` on every insert.
        """
        opened, failures = [], []
        for index, (decision, recommendation, ist_time) in enumerate(self._signals(session)):
            if limit is not None and index >= limit:
                break
            symbol = f"SYM{index:04d}"
            order = self._fill(
                recommendation, decision, filled_fraction=filled_fraction, symbol=symbol
            )
            try:
                opened.append(
                    _run_async(
                        manager.open_from_fill(
                            order,
                            sizing=decision.sizing,
                            slot_index=index,
                            trade_date=TRADING_DAY,
                            now=_at(TRADING_DAY, ist_time.hour, ist_time.minute),
                            is_cas_stock=False,
                        )
                    )
                )
            except Exception as exc:
                failures.append((symbol, exc))
        return opened, failures

    # -- the healthy day, and the control ----------------------------------

    def test_every_fill_becomes_one_protected_position(self, calendar) -> None:
        session = self._session(calendar).run()
        broker, store, mirror = _OrderbookBroker(), _SessionPositionStore(), _DictMirror()
        manager = self._manager(broker, store, mirror, calendar)

        opened, failures = self._walk_the_day(session, manager, limit=120)

        assert failures == []
        assert len(opened) == 120
        assert all(isinstance(p, ProtectedPosition) for p in opened)
        assert len(broker.intents(OrderIntent.STOP)) == 120
        assert broker.intents(OrderIntent.SQUAREOFF) == []
        assert store.open_count == 120
        assert len(mirror.data) == 120

    # -- the day everything fills short ------------------------------------

    def test_a_day_of_partial_fills_books_what_filled_not_what_was_ordered(self, calendar) -> None:
        """The claim that only exists at scale.

        One partial fill is a unit test. A whole day of them is where the
        difference between ordered and filled becomes thousands of shares, and
        every stop the system places is sized from the number the book holds.
        """
        session = self._session(calendar).run()
        broker, store, mirror = _OrderbookBroker(), _SessionPositionStore(), _DictMirror()
        manager = self._manager(broker, store, mirror, calendar)

        opened, failures = self._walk_the_day(
            session, manager, filled_fraction=Decimal("0.4"), limit=120
        )
        assert failures == []

        ordered_total = sum(d.sizing.quantity for d, _r, _t in self._signals(session)[:120])
        booked_total = sum(p.position.quantity for p in opened)
        stopped_total = sum(o.quantity for o in broker.intents(OrderIntent.STOP))

        assert booked_total < ordered_total, "nothing filled short; the test proves nothing"
        assert stopped_total == booked_total, (
            "the stops protect a different number of shares than the book holds"
        )
        assert all(
            r["quantity"] == p.position.quantity
            for r, p in zip(store.rows.values(), opened, strict=True)
        )

    def test_the_gap_between_ordered_and_filled_is_never_protected(self, calendar) -> None:
        """Stated as the failure rather than the total: no stop anywhere in the
        day is for more shares than its position holds. One such stop sells
        what does not exist."""
        session = self._session(calendar).run()
        broker, store, mirror = _OrderbookBroker(), _SessionPositionStore(), _DictMirror()
        manager = self._manager(broker, store, mirror, calendar)
        opened, _f = self._walk_the_day(session, manager, filled_fraction=Decimal("0.4"), limit=80)

        held = {p.position.symbol: p.position.quantity for p in opened}
        for stop in broker.intents(OrderIntent.STOP):
            assert stop.quantity == held[stop.symbol]

    # -- the day the stops all fail ----------------------------------------

    def test_a_day_of_failed_stops_ends_with_an_empty_book(self, calendar) -> None:
        """E15-S04 could place the exits; it could not confirm they filled.

        This is that half, at session scale: every position exited, every exit
        confirmed, and the book EMPTY at the close — not 316 exceptions and a
        book still holding everything.
        """
        session = self._session(calendar).run()
        broker, store, mirror = _OrderbookBroker(), _SessionPositionStore(), _DictMirror()
        broker.refuse = frozenset({OrderIntent.STOP})
        manager = self._manager(broker, store, mirror, calendar)

        opened, failures = self._walk_the_day(session, manager, limit=60)
        assert opened == []
        assert len(failures) == 60
        assert all(isinstance(p, ExitingPosition) for p in manager.open_positions())
        assert len(broker.intents(OrderIntent.SQUAREOFF)) == 60

        for tracked in list(manager.open_positions()):
            _run_async(
                manager.confirm_exit(
                    self._exit_fill(tracked.position),
                    now=_at(TRADING_DAY, 15, 5),
                    reason=ExitReason.UNPROTECTED,
                )
            )

        assert manager.open_positions() == []
        assert store.open_count == 0
        assert mirror.data == {}
        assert all(r["exit_reason"] == "UNPROTECTED" for r in store.rows.values())

    def test_a_placed_exit_is_not_a_closed_position(self, calendar) -> None:
        """The distinction the whole story rests on, asserted before the
        confirmations run: the exits exist and the positions are still held."""
        session = self._session(calendar).run()
        broker, store, mirror = _OrderbookBroker(), _SessionPositionStore(), _DictMirror()
        broker.refuse = frozenset({OrderIntent.STOP})
        manager = self._manager(broker, store, mirror, calendar)
        self._walk_the_day(session, manager, limit=30)

        assert len(broker.intents(OrderIntent.SQUAREOFF)) == 30
        assert store.open_count == 30, "closed on the strength of an exit being placed"

    # -- the restart -------------------------------------------------------

    def test_a_restart_mid_session_presumes_nothing(self, calendar) -> None:
        """One presumed-protected position is one naked position. The assertion
        is the count: all of them unverified, not most."""
        session = self._session(calendar).run()
        broker, store, mirror = _OrderbookBroker(), _SessionPositionStore(), _DictMirror()
        manager = self._manager(broker, store, mirror, calendar)
        opened, _f = self._walk_the_day(session, manager, limit=100)
        assert all(isinstance(p, ProtectedPosition) for p in opened)

        restarted = self._manager(_OrderbookBroker(), store, _DictMirror(), calendar)
        restored = _run_async(restarted.restore())

        assert len(restored) == 100
        assert all(isinstance(r, UnverifiedPosition) for r in restored)
        assert not any(isinstance(r, ProtectedPosition) for r in restarted.open_positions())
        assert [r.position.quantity for r in restored] == [p.position.quantity for p in opened]

    def test_the_restarted_book_can_still_be_priced(self, calendar) -> None:
        """The control. A restore that produced unusable positions would
        satisfy the test above and leave the day unable to compute its P&L."""
        session = self._session(calendar).run()
        broker, store, mirror = _OrderbookBroker(), _SessionPositionStore(), _DictMirror()
        manager = self._manager(broker, store, mirror, calendar)
        self._walk_the_day(session, manager, limit=40)

        restarted = self._manager(_OrderbookBroker(), store, _DictMirror(), calendar)
        restored = _run_async(restarted.restore())
        now = _at(TRADING_DAY, 12, 0)
        for r in restored:
            _run_async(
                restarted.mark(_SessionQuote(r.position.symbol, Decimal("1210.0000"), now), now=now)
            )
        assert restarted.unrealised_pnl is not None
        assert restarted.unrealised_pnl > 0

    def test_replaying_the_days_fills_after_a_restart(self, calendar) -> None:
        """Idempotency and restart, the scenario SIT is specifically for.

        The broker still reports yesterday's entries as FILLED after a crash,
        so whatever asks "have we opened a position for this fill?" will ask
        again. Replaying must not produce a second position, a second stop, or
        a second row - and must not look like a failure to open something that
        is in fact open and protected.
        """
        session = self._session(calendar).run()
        store = _SessionPositionStore()
        broker = _OrderbookBroker()
        manager = self._manager(broker, store, _DictMirror(), calendar)
        opened, _f = self._walk_the_day(session, manager, limit=40)
        assert len(opened) == 40

        replay_broker = _OrderbookBroker()
        replay_broker.orderbook = broker.orderbook
        restarted = self._manager(replay_broker, store, _DictMirror(), calendar)
        _run_async(restarted.restore())
        again, failures = self._walk_the_day(session, restarted, limit=40)

        assert store.open_count == 40, "the replay opened a second book"
        assert replay_broker.intents(OrderIntent.STOP) == [], (
            "the replay placed a second stop on every position"
        )
        assert again == []
        assert len(failures) == 40
        assert all(isinstance(exc, PositionAlreadyHeldError) for _sym, exc in failures), (
            "a replay is a normal restart outcome and must say so - SIT-004 found "
            "it surfacing as a raw database integrity error, which a caller "
            "cannot tell apart from a genuine failure to open"
        )

    # -- the book's P&L across the session ---------------------------------

    def test_the_book_refuses_a_total_until_every_position_is_priced(self, calendar) -> None:
        """Fail closed, at book scale. The daily-loss halt reads this number,
        and a total that quietly omitted the one position nobody could price
        would be wrong in the direction of continuing to trade."""
        session = self._session(calendar).run()
        broker, store, mirror = _OrderbookBroker(), _SessionPositionStore(), _DictMirror()
        manager = self._manager(broker, store, mirror, calendar)
        opened, _f = self._walk_the_day(session, manager, limit=50)

        now = _at(TRADING_DAY, 12, 0)
        for tracked in opened[:-1]:
            _run_async(
                manager.mark(
                    _SessionQuote(tracked.position.symbol, Decimal("1210.0000"), now), now=now
                )
            )
        assert manager.unrealised_pnl is None, "49 of 50 priced read as a complete total"

        last = opened[-1].position
        _run_async(manager.mark(_SessionQuote(last.symbol, Decimal("1210.0000"), now), now=now))
        assert manager.unrealised_pnl == sum(
            p.position.unrealized_pnl(Decimal("1210.0000")) for p in opened
        )

    def test_the_excursions_follow_the_days_path(self, calendar) -> None:
        """Marked minute by minute across a real session shape rather than a
        generated list: MFE and MAE must be the extremes of the path walked."""
        session = self._session(calendar).run()
        broker, store, mirror = _OrderbookBroker(), _SessionPositionStore(), _DictMirror()
        manager = self._manager(broker, store, mirror, calendar)
        self._walk_the_day(session, manager, limit=5)

        path = [Decimal("1205"), Decimal("1240"), Decimal("1180"), Decimal("1150"), Decimal("1220")]
        for minute, price in enumerate(path):
            now = _at(TRADING_DAY, 11, minute)
            for tracked in list(manager.open_positions()):
                _run_async(
                    manager.mark(_SessionQuote(tracked.position.symbol, price, now), now=now)
                )

        for tracked in manager.open_positions():
            pnls = [tracked.position.unrealized_pnl(p) for p in path]
            assert tracked.marks.max_favourable_excursion == max(max(pnls), Decimal("0"))
            assert tracked.marks.max_adverse_excursion == min(min(pnls), Decimal("0"))

    # -- cross-cutting -----------------------------------------------------

    def test_nothing_in_the_book_is_ever_both_unprotected_and_unexiting(self, calendar) -> None:
        """Invariant 5 stated over the whole book: every position the system
        holds is either protected by a verified stop or on its way out."""
        session = self._session(calendar).run()
        broker, store, mirror = _OrderbookBroker(), _SessionPositionStore(), _DictMirror()
        broker.status_for = {OrderIntent.STOP: OrderStatus.REJECTED}
        manager = self._manager(broker, store, mirror, calendar)
        self._walk_the_day(session, manager, limit=40)

        for tracked in manager.open_positions():
            assert isinstance(tracked, ProtectedPosition | ExitingPosition)

    def test_no_credential_reached_the_log_on_a_day_of_failed_stops(self, calendar) -> None:
        import re

        session = self._session(calendar).run()
        broker, store, mirror = _OrderbookBroker(), _SessionPositionStore(), _DictMirror()
        broker.refuse = frozenset({OrderIntent.STOP})
        manager = self._manager(broker, store, mirror, calendar)
        self._walk_the_day(session, manager, limit=20)

        assert not re.search(
            r"(api[_-]?key|secret|password|access[_-]?token)\s*[:=]\s*\S{8,}",
            session.log_text,
            re.IGNORECASE,
        )


# --------------------------------------------------------------------------
# SIT-19 — a session reconciled against its broker all day (E15-S09)
# --------------------------------------------------------------------------


def _counter_value(name: str) -> float:
    for metric in get_metrics().registry.collect():
        for sample in metric.samples:
            if sample.name == name:
                return sample.value
    raise AssertionError(f"{name} is registered nowhere")


class _ReconciledBroker:
    """One broker account for a whole session, kept the way Kite keeps it.

    MARKET orders fill on arrival; a stop stays working. The day's positions
    are cumulative bought and sold per symbol, and a symbol traded flat stays
    in the list at zero - Kite's own behaviour, and the reason the reconciler
    judges activity rather than row presence. Keys are stored whole here; the
    20-character truncation is the Kite adapter's and is proved in integration.
    """

    def __init__(self) -> None:
        self.orders: dict[str, Order] = {}
        self.placed: list[OrderRequest] = []
        self.day: dict[str, list[int]] = {}

    async def place_order(self, request) -> str:
        self.placed.append(request)
        broker_order_id = f"B{len(self.placed):06d}"
        order = _landed_order(request, broker_order_id)
        if request.order_type is OrderType.MARKET:
            order = order.model_copy(
                update={
                    "status": OrderStatus.FILLED,
                    "filled_quantity": request.quantity,
                    "average_price": Decimal("1200.0000"),
                }
            )
            self._fill(request.symbol, request.side, request.quantity)
        self.orders[request.client_order_id] = order
        return broker_order_id

    def _fill(self, symbol: str, side: Side, quantity: int) -> None:
        bought_sold = self.day.setdefault(symbol, [0, 0])
        bought_sold[0 if side is Side.BUY else 1] += quantity

    def foreign_fill(self, symbol: str, quantity: int) -> None:
        """A fill no order of ours placed - someone else on the account."""
        self._fill(symbol, Side.BUY, quantity)

    def reject(self, client_order_id: str) -> None:
        self.orders[client_order_id] = self.orders[client_order_id].model_copy(
            update={"status": OrderStatus.REJECTED}
        )

    async def find_by_client_order_id(self, client_order_id: str):
        return self.orders.get(client_order_id)

    async def fetch_orderbook(self) -> list[Order]:
        return list(self.orders.values())

    async def fetch_positions(self) -> list[BrokerPosition]:
        return [
            BrokerPosition(
                symbol=symbol,
                exchange="NSE",
                product="MIS",
                quantity=bought - sold,
                day_buy_quantity=bought,
                day_sell_quantity=sold,
            )
            for symbol, (bought, sold) in self.day.items()
        ]

    def order_key(self, client_order_id: str) -> str:
        return client_order_id

    def net(self) -> dict[str, int]:
        return {symbol: bought - sold for symbol, (bought, sold) in self.day.items()}


class _SessionLedger:
    """Our orders table for one session: the gateway writes it, the reconciler
    reads and corrects it. ``now`` is the session clock, so ``placed_at`` is the
    minute the order was sent rather than the minute the test ran."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.now = _at(TRADING_DAY, 9, 15)

    async def insert_submitting(self, order: dict) -> int:
        cid = order["client_order_id"]
        assert cid not in self.rows, f"uq_client_order: {cid} inserted twice"
        self.rows[cid] = {
            **order,
            "status": "SUBMITTING",
            "broker_order_id": None,
            "filled_quantity": 0,
            "average_price": None,
            "placed_at": self.now,
            "last_update_at": self.now,
        }
        return len(self.rows)

    async def attach_broker_id(self, client_order_id: str, broker_order_id: str) -> None:
        self.rows[client_order_id].update(broker_order_id=broker_order_id, status="SUBMITTED")

    async def mark_rejected(self, client_order_id: str, *, reason) -> None:
        self.rows[client_order_id].update(status="REJECTED", rejection_reason=reason)

    async def find_by_client_order_id(self, client_order_id: str):
        row = self.rows.get(client_order_id)
        return None if row is None else dict(row)

    async def orders_placed_since(self, since) -> list[dict]:
        return [dict(r) for r in self.rows.values() if r["placed_at"] >= since]

    async def apply_broker_state(self, client_order_id: str, **kwargs) -> None:
        row = self.rows[client_order_id]
        row.update(
            status=kwargs["status"],
            filled_quantity=kwargs["filled_quantity"],
            average_price=kwargs["average_price"],
        )
        if kwargs["broker_order_id"] is not None:
            row["broker_order_id"] = kwargs["broker_order_id"]


class TestSit19ASessionReconciledAllDay:
    """SIT-17 and SIT-18 ended at a position being protected or exited. This
    runs the loop that watches them - after every fill of the day - and asks
    what only a whole day can answer:

    * **Does a day of our OWN fills and exits ever halt?** One race is a unit
      test. Sixty fills, each followed by its exit and by a reconciliation
      cycle, is where a check that confused our activity for someone else's
      would stop the day.
    * **Does a stranger's position halt within one cycle, and does the day then
      stop?** That needs the latch and the risk engine in the same test.
    * **Until E15-S12 books fills, is every fill exited exactly once, and does
      the day end flat?** That is the fail-closed claim, and it is only a claim
      about a whole day.

    **The trigger is the test**, again: nothing yet runs the loop on a timer in
    production, so this file calls it after each fill, as the scheduler will.
    """

    LIMIT = 60

    @staticmethod
    def _healthy() -> dict:
        return {
            "symbol_sector": "IT",
            "correlations": {},
            "available_margin": Decimal("400000"),
            "margin_per_share": Decimal("240"),
            "atr": Decimal("13.5000"),
            "lot_size": 1,
        }

    def _session(self, calendar, **overrides):
        kwargs = {**self._healthy(), **overrides}
        session = Session(
            calendar,
            TRADING_DAY,
            checks=[
                *build_precondition_checks(calendar, NO_TRADE_WINDOWS),
                *build_eligibility_checks(),
                *build_exposure_checks(
                    max_correlated_positions=2,
                    correlation_threshold=Decimal("0.7"),
                    max_sector_exposure_pct=Decimal("40"),
                    max_net_directional_exposure_pct=Decimal("60"),
                ),
                *build_loss_checks(max_daily_loss_pct=Decimal("3.0"), consecutive_loss_halt=3),
                *build_margin_timing_checks(min_minutes_to_squareoff=30),
            ],
            **kwargs,
        )
        session.engine = RiskEngine(
            checks=list(session.engine.checks),
            sizer=build_sizer(_sizing_policy()),
            audit=session.audit.append,
        )
        return session

    @staticmethod
    def _signals(session) -> list:
        return [
            (session.decisions[t], _recommendation(_at(TRADING_DAY, t.hour, t.minute)), t)
            for t in sorted(session.decisions)
            if session.decisions[t].approved
        ]

    @staticmethod
    def _stack(calendar):
        broker, ledger = _ReconciledBroker(), _SessionLedger()
        gateway = OrderGateway(
            broker,
            policy=GatewayPolicy(algo_id="ALGO12345", market_protection=Decimal("-1")),
            store=ledger,
        )
        halter = HaltController(_SessionRedis())
        protector = ProtectiveStop(gateway, halter=halter, metrics=get_metrics())
        store = _SessionPositionStore()
        manager = PositionManager(
            protector=protector,
            store=store,
            mirror=_DictMirror(),
            calendar=calendar,
            metrics=get_metrics(),
        )
        audit: list = []

        async def sink(entry) -> None:
            audit.append(entry)

        reconciler = Reconciler(
            broker=broker,
            ledger=ledger,
            manager=manager,
            protector=protector,
            halter=halter,
            audit=sink,
            metrics=get_metrics(),
        )
        return {
            "broker": broker,
            "ledger": ledger,
            "gateway": gateway,
            "halter": halter,
            "protector": protector,
            "store": store,
            "manager": manager,
            "reconciler": reconciler,
            "audit": audit,
        }

    @staticmethod
    def _enter(stack, index, decision, recommendation, ist_time) -> str:
        """Send one entry, as the pipeline will. Returns its idempotency key."""
        symbol = f"SYM{index:04d}"
        stack["ledger"].now = _at(TRADING_DAY, ist_time.hour, ist_time.minute)
        cid = client_order_id(
            correlation_id=recommendation.correlation_id,
            symbol=symbol,
            side=Side.BUY,
            intent=OrderIntent.ENTRY,
            trade_date=TRADING_DAY,
        )
        _run_async(
            stack["gateway"].submit(
                OrderRequest(
                    client_order_id=cid,
                    correlation_id=recommendation.correlation_id,
                    symbol=symbol,
                    side=Side.BUY,
                    order_type=OrderType.MARKET,
                    product=Product.MIS,
                    quantity=decision.sizing.quantity,
                    intent=OrderIntent.ENTRY,
                    algo_id="ALGO12345",
                    market_protection=Decimal("-1"),
                )
            )
        )
        return cid

    @staticmethod
    def _cycle(stack, ist_time, *, seconds: int = 30):
        moment = _at(TRADING_DAY, ist_time.hour, ist_time.minute) + dt.timedelta(seconds=seconds)
        return _run_async(stack["reconciler"].run_cycle(now=moment))

    def _run_day(self, calendar, *, limit=LIMIT, before_cycle=None):
        session = self._session(calendar).run()
        stack = self._stack(calendar)
        reports = []
        for index, (decision, rec, ist_time) in enumerate(self._signals(session)[:limit]):
            self._enter(stack, index, decision, rec, ist_time)
            if before_cycle is not None:
                before_cycle(index, stack)
            reports.append(self._cycle(stack, ist_time))
        return session, stack, reports

    # -- the ordinary day ------------------------------------------------------

    def test_a_day_of_our_own_fills_and_exits_never_halts(self, calendar) -> None:
        _session, stack, reports = self._run_day(calendar)
        assert len(reports) == self.LIMIT
        assert all(r.unknown == [] for r in reports), "our own activity read as a stranger's"
        assert not any(r.halted for r in reports)
        assert not _run_async(stack["halter"].state()).any_halted

    def test_until_fills_are_booked_every_fill_is_exited_exactly_once(self, calendar) -> None:
        """E15-S12 books fills. Until it lands, AC7 means every fill leaves - and
        leaves once: a second exit per fill would sell shares already sold."""
        _session, stack, _reports = self._run_day(calendar)
        exits = [r for r in stack["broker"].placed if r.side is Side.SELL]
        assert len(exits) == self.LIMIT
        assert len({r.symbol for r in exits}) == self.LIMIT, "a symbol was exited twice"
        assert all(r.intent is OrderIntent.SQUAREOFF for r in exits)

    def test_the_day_ends_flat(self, calendar) -> None:
        _session, stack, _reports = self._run_day(calendar)
        assert set(stack["broker"].net().values()) == {0}

    def test_every_difference_is_one_audit_row_and_clean_cycles_write_none(self, calendar) -> None:
        _session, stack, reports = self._run_day(calendar)
        assert len(stack["audit"]) == sum(len(r.drifts) for r in reports)
        assert {e.stage for e in stack["audit"]} == {"RECONCILIATION_DRIFT"}
        kinds = {e.outcome for e in stack["audit"]}
        assert kinds <= {d.value for d in Drift}
        assert "UNKNOWN_POS" not in kinds

    # -- a stranger on the account ---------------------------------------------

    def test_a_stranger_mid_session_halts_within_one_cycle_and_the_day_stops(
        self, calendar
    ) -> None:
        """The acceptance criterion at session scale, composed with the latch
        and the risk engine: a halt nothing reads would pass a unit test."""

        def stranger(index, stack) -> None:
            if index == 30:
                stack["broker"].foreign_fill("WIPRO", 25)

        _session, stack, reports = self._run_day(calendar, before_cycle=stranger)
        first = next(i for i, r in enumerate(reports) if r.unknown)
        assert first == 30, "the stranger was seen late, or early"
        state = _run_async(stack["halter"].state())
        assert state.kill_switch
        assert state.records["kill_switch"].reason is HaltReason.UNKNOWN_POSITION

        after = self._session(calendar, kill_switch_active=state.kill_switch).run()
        assert [d for d in after.decisions.values() if d.approved] == []

    def test_a_stranger_is_alerted_once_not_every_cycle(self, calendar) -> None:
        """The stranger's position stays at the broker, so every cycle sees it.
        Seeing it is right; ALERTING on it every 30 seconds for the rest of the
        day is an alarm that stops being read - and a counter documented as
        'positions' that would really be counting cycles."""

        def stranger(index, stack) -> None:
            if index == 30:
                stack["broker"].foreign_fill("WIPRO", 25)

        _session, stack, reports = self._run_day(calendar, before_cycle=stranger)
        assert sum(1 for r in reports if r.unknown) == self.LIMIT - 30, "detection stopped"
        unknown_rows = [e for e in stack["audit"] if e.outcome == "UNKNOWN_POS"]
        assert len(unknown_rows) == 1, f"alerted {len(unknown_rows)} times for one stranger"
        assert _counter_value("unknown_positions_total") == 1

    def test_the_halt_does_not_stop_our_own_exits(self, calendar) -> None:
        """A halt must never disable the exit path. The fill in the halting
        cycle and every one after it still leaves."""

        def stranger(index, stack) -> None:
            if index == 30:
                stack["broker"].foreign_fill("WIPRO", 25)

        _session, stack, _reports = self._run_day(calendar, before_cycle=stranger)
        ours = {s: n for s, n in stack["broker"].net().items() if s != "WIPRO"}
        assert set(ours.values()) == {0}
        assert stack["broker"].net()["WIPRO"] == 25, "the loop traded a position it did not open"

    # -- protected positions ---------------------------------------------------

    def _book(self, stack, calendar, count: int):
        """Book positions by hand - E15-S12's job, played by the test."""
        session = self._session(calendar).run()
        booked = []
        for index, (decision, rec, ist_time) in enumerate(self._signals(session)[:count]):
            cid = self._enter(stack, index, decision, rec, ist_time)
            order = stack["broker"].orders[cid]
            booked.append(
                _run_async(
                    stack["manager"].open_from_fill(
                        order,
                        sizing=decision.sizing,
                        slot_index=index,
                        trade_date=TRADING_DAY,
                        now=_at(TRADING_DAY, ist_time.hour, ist_time.minute),
                        is_cas_stock=False,
                    )
                )
            )
        return booked

    def test_booked_positions_with_live_stops_are_left_alone_all_day(self, calendar) -> None:
        """The control for everything above: a loop that exited everything would
        pass the unbooked tests and this is where it would fail."""
        stack = self._stack(calendar)
        booked = self._book(stack, calendar, 5)
        for minute in range(0, 120, 5):
            report = self._cycle(stack, dt.time(12, 0), seconds=minute * 60)
            assert report.exited == []
        assert [type(t) for t in stack["manager"].open_positions()] == [ProtectedPosition] * 5
        assert [p.position.symbol for p in booked] == [f"SYM{i:04d}" for i in range(5)]

    def test_a_stop_rejected_mid_afternoon_is_exited_in_the_next_cycle(self, calendar) -> None:
        """Verified live at attach, rejected afterwards: E15-S04's asynchronous
        case, which no synchronous check reaches."""
        stack = self._stack(calendar)
        booked = self._book(stack, calendar, 3)
        assert self._cycle(stack, dt.time(13, 0)).exited == []

        stack["broker"].reject(booked[1].established.stop_client_order_id)
        report = self._cycle(stack, dt.time(13, 1))
        assert report.exited == ["SYM0001"]
        assert stack["broker"].net()["SYM0001"] == 0

    def test_a_restart_resolves_every_restored_position_in_one_cycle(self, calendar) -> None:
        """E15-S05 ends a restart with protection unknown. One cycle later
        nothing may still be unknown: each is proved protected or has left."""
        stack = self._stack(calendar)
        booked = self._book(stack, calendar, 4)
        stack["broker"].reject(booked[2].established.stop_client_order_id)

        restarted = PositionManager(
            protector=stack["protector"],
            store=stack["store"],
            mirror=_DictMirror(),
            calendar=calendar,
            metrics=get_metrics(),
        )
        restored = _run_async(restarted.restore())
        assert [type(r) for r in restored] == [UnverifiedPosition] * 4
        stack["reconciler"]._manager = restarted

        report = self._cycle(stack, dt.time(13, 30))
        assert sorted(report.promoted) == ["SYM0000", "SYM0001", "SYM0003"]
        assert report.exited == ["SYM0002"]
        assert not any(isinstance(t, UnverifiedPosition) for t in restarted.open_positions())

    # -- cross-cutting -------------------------------------------------------

    def test_no_credential_reached_the_log_on_a_reconciled_day(self, calendar) -> None:
        import re

        def stranger(index, stack) -> None:
            if index == 10:
                stack["broker"].foreign_fill("WIPRO", 25)

        session, _stack, _reports = self._run_day(calendar, limit=20, before_cycle=stranger)
        assert not re.search(
            r"(api[_-]?key|secret|password|access[_-]?token)\s*[:=]\s*\S{8,}",
            session.log_text,
            re.IGNORECASE,
        )
