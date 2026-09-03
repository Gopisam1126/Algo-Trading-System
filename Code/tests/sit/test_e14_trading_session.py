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
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from algotrader.common.calendar import IST, MarketCalendar, load_holidays_with_status
from algotrader.common.enums import AIVerdict, Direction, RejectReason
from algotrader.common.metrics import reset_metrics_for_testing
from algotrader.common.models.trading import Recommendation
from algotrader.execution.risk.checks import (
    ELIGIBILITY_ORDER,
    EXPOSURE_ORDER,
    PRECONDITION_ORDER,
    build_eligibility_checks,
    build_exposure_checks,
    build_precondition_checks,
)
from algotrader.execution.risk.context import OpenPosition, RiskContext
from algotrader.execution.risk.framework import RiskCheck, RiskEngine

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
