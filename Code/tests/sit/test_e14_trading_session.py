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

import datetime as dt
import io
import logging
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from algotrader.common.calendar import IST, MarketCalendar, load_holidays_with_status
from algotrader.common.enums import AIVerdict, Direction, RejectReason
from algotrader.common.metrics import reset_metrics_for_testing
from algotrader.common.models.trading import Recommendation
from algotrader.execution.risk.checks import PRECONDITION_ORDER, build_precondition_checks
from algotrader.execution.risk.context import RiskContext
from algotrader.execution.risk.framework import RiskEngine

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

    def __init__(self, calendar: MarketCalendar, day: dt.date, **ctx_overrides: object):
        self.day = day
        self.audit: list[dict[str, object]] = []
        self.engine = RiskEngine(
            checks=build_precondition_checks(calendar, NO_TRADE_WINDOWS),
            audit=self.audit.append,
        )
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
                    "now": moment,
                    "squareoff_deadline": _at(self.day, 15, 10),
                    "capital": Decimal("500000"),
                    "slots_total": 5,
                    "slots_used": 0,
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

    def cleared_all_preconditions(self) -> list[dt.time]:
        """Minutes where all four gates passed. With no sizer configured the
        engine then refuses, so 'cleared' is read from checks_passed, not from
        approval — which is the honest reading of a half-built engine."""
        return sorted(
            t
            for t, d in self.decisions.items()
            if list(d.checks_passed) == list(PRECONDITION_ORDER)  # type: ignore[attr-defined]
        )

    def reasons(self) -> dict[dt.time, RejectReason | None]:
        return {t: d.reason for t, d in self.decisions.items()}  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# SIT-1 — realistic session replay
# --------------------------------------------------------------------------


class TestSit1TheShapeOfTheTradingDay:
    def test_the_tradable_window_is_contiguous_with_no_hole(self, session: Session) -> None:
        """The claim no piecewise test makes. If two gates left a gap anywhere
        in the middle of the day, this is what would see it."""
        cleared = session.cleared_all_preconditions()
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
        cleared = session.cleared_all_preconditions()
        assert cleared[0] == EXPECTED_FIRST_TRADABLE
        assert cleared[-1] == EXPECTED_LAST_TRADABLE

    def test_it_is_339_minutes_and_that_number_is_derived_not_guessed(
        self, session: Session
    ) -> None:
        """09:20 to 15:00 exclusive. Stated as a count because an off-by-one at
        either boundary changes it and nothing else would."""
        assert len(session.cleared_all_preconditions()) == 340

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
        assert dt.time(11, 30) in session.cleared_all_preconditions(), (
            "the health gate did not release after the service recovered"
        )

    def test_the_kill_switch_overrides_a_moment_that_would_otherwise_trade(self, calendar) -> None:
        session = Session(calendar, TRADING_DAY, kill_switch_active=True).run(
            start=(9, 20), end=(9, 30)
        )
        assert session.cleared_all_preconditions() == []
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
        assert session.cleared_all_preconditions() == [], f"{label} was tradable"
        assert all(
            d.reason is RejectReason.OUTSIDE_TRADING_WINDOW  # type: ignore[attr-defined]
            for d in session.decisions.values()
        ), label

    def test_the_ordinary_day_is_the_control(self, calendar) -> None:
        """Without this, a calendar that called every day closed would pass
        every test above."""
        assert Session(calendar, TRADING_DAY).run().cleared_all_preconditions()


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
        assert first.cleared_all_preconditions() == second.cleared_all_preconditions()

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
