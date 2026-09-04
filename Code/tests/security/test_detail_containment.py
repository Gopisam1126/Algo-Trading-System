"""A rejection detail is one bounded line, whatever a check puts in it.

**This is the third time log forgery has been found in this system**, and the
third occurrence is what moved the fix:

* QA-SEC-16 — a newline in ``OrderRequest.symbol`` forged a log line. Fixed on
  that model.
* QA-SEC-28 — the same untrusted symbol reaches ``Trigger`` and
  ``Recommendation`` *first*, and the risk engine logs it on every rejection.
  Fixed by defining the symbol validator once and attaching it to all three.
* QA-SEC-30 — found here, in E14-S03's surveillance-restriction labels, which
  arrive from NSE data by way of E04. A newline in a label produced a log line
  reading *"CRITICAL kill switch disarmed by operator"*.

The pattern is not "symbols are dangerous". It is **any untrusted string
interpolated into a rejection detail forges a log line**, and there are
fourteen checks each free to interpolate whatever they read. Validating each
new source in turn is whack-a-mole on a board that keeps growing.

So the rule now lives where details are CONSTRUCTED — ``CheckOutcome`` — and
these tests assert the property of the type rather than of any one check. A
fifteenth check written next year gets it without its author knowing this file
exists.

The same construction point also bounds the length. QA-SEC-29 capped the
health gate's service *count*, which is not the same as bounding its output:
E14-S03's restriction list was capped at 8 labels and still produced an 80,054
character detail from eight long ones. Bound the output, not the input count.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import decimal
import io
import logging
from collections.abc import Mapping, Sequence
from decimal import Decimal
from types import MappingProxyType
from uuid import UUID

import pytest

from algotrader.common.calendar import IST
from algotrader.common.enums import AIVerdict, Direction, RejectReason
from algotrader.common.models.trading import Recommendation
from algotrader.execution.risk.checks import (
    EXPOSURE_ORDER,
    build_eligibility_checks,
    build_exposure_checks,
    build_margin_timing_checks,
)
from algotrader.execution.risk.context import OpenPosition, RiskContext, RiskContextError
from algotrader.execution.risk.framework import (
    MAX_DETAIL,
    CheckOutcome,
    RiskCheck,
    RiskEngine,
)

NOW = dt.datetime(2026, 8, 25, 4, 30, tzinfo=dt.UTC)


def _rec(symbol: str = "INFY") -> Recommendation:
    return Recommendation(
        correlation_id=UUID(int=1),
        symbol=symbol,
        strategy_id="s",
        direction=Direction.LONG,
        trigger_price=Decimal("100"),
        suggested_stop=Decimal("95"),
        timeframe_agreement=3,
        ai_confidence=Decimal("0.5"),
        ai_verdict=AIVerdict.CONFIRM,
        ai_rationale="r",
        emitted_at=NOW,
    )


def _pos(symbol: str, sector: str | None = "PSU_BANK") -> OpenPosition:
    return OpenPosition(
        symbol=symbol,
        direction=Direction.LONG,
        quantity=500,
        entry_price=Decimal("100"),
        stop_price=Decimal("95"),
        sector=sector,
    )


def _ctx(**overrides) -> RiskContext:
    base: dict = {
        "now": NOW,
        "squareoff_deadline": dt.datetime(2026, 8, 25, 9, 35, tzinfo=dt.UTC),
        "capital": Decimal("500000"),
        "slots_total": 5,
        "slots_used": 0,
        "symbol_restrictions": (),
        "symbol_sector": "PSU_BANK",
    }
    base.update(overrides)
    return RiskContext(**base)


class TestTheTypeItselfRefusesToCarryAForgedLine:
    """Asserted on ``CheckOutcome``, not on any check. That is the point of
    having moved the fix here."""

    @pytest.mark.parametrize(
        "hostile",
        [
            "blocked\nCRITICAL kill switch disarmed by operator",
            "blocked\r\nWARN feed healthy",
            "blocked\rINFO all clear",
            "blocked\x00nul",
            "blocked\x1b[31mansi",
            "blocked\x7fdel",
            "blocked\tTAB",
            "blocked\x0bvertical\x0ctab",
        ],
    )
    def test_no_control_character_survives_construction(self, hostile: str) -> None:
        outcome = CheckOutcome.fail(RejectReason.SYMBOL_NOT_TRADABLE, hostile)
        assert "\n" not in outcome.detail
        assert "\r" not in outcome.detail
        assert not any(ord(c) < 0x20 or ord(c) == 0x7F for c in outcome.detail)

    def test_the_direct_constructor_is_covered_too(self) -> None:
        """Not only ``fail``. A check constructing ``CheckOutcome(...)`` by
        hand must get the same treatment, or the rule has a second door."""
        outcome = CheckOutcome(
            passed=False,
            reason=RejectReason.SYMBOL_NOT_TRADABLE,
            detail="blocked\nCRITICAL forged",
        )
        assert "\n" not in outcome.detail

    def test_the_information_is_escaped_not_discarded(self) -> None:
        """Escaping rather than stripping: an operator investigating an attack
        should still be able to see that a newline was there."""
        outcome = CheckOutcome.fail(RejectReason.SYMBOL_NOT_TRADABLE, "a\nb")
        assert outcome.detail == "a\\nb"

    def test_an_ordinary_detail_is_untouched(self) -> None:
        """The control. A sanitiser that mangled normal text would pass every
        test above and make every rejection harder to read."""
        plain = "no free capital slot (5 of 5 in use). Sizing divides capital per slot."
        assert CheckOutcome.fail(RejectReason.NO_SLOT_AVAILABLE, plain).detail == plain

    @pytest.mark.parametrize("size", [MAX_DETAIL + 1, 10_000, 200_000])
    def test_the_detail_is_bounded(self, size: int) -> None:
        outcome = CheckOutcome.fail(RejectReason.SYMBOL_NOT_TRADABLE, "X" * size)
        assert len(outcome.detail) <= MAX_DETAIL

    def test_truncation_says_how_much_was_dropped(self) -> None:
        """A silently truncated detail reads as a complete one, and an operator
        would act on a partial sentence without knowing it."""
        outcome = CheckOutcome.fail(RejectReason.SYMBOL_NOT_TRADABLE, "X" * 10_000)
        assert "more chars]" in outcome.detail

    def test_a_detail_at_the_boundary_is_not_truncated(self) -> None:
        exact = "Y" * MAX_DETAIL
        assert CheckOutcome.fail(RejectReason.SYMBOL_NOT_TRADABLE, exact).detail == exact


class TestNoCheckCanForgeALogLine:
    """The end-to-end property, through the engine that does the logging."""

    @staticmethod
    def _lines(check: RiskCheck, ctx: RiskContext) -> list[str]:
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger = logging.getLogger("algotrader.execution.risk.framework")
        logger.addHandler(handler)
        previous = logger.level
        logger.setLevel(logging.INFO)
        try:
            RiskEngine(checks=[check]).evaluate(_rec(), ctx)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)
        return [line for line in buf.getvalue().splitlines() if line.strip()]

    def test_a_hostile_restriction_label_produces_one_line(self) -> None:
        """QA-SEC-30 exactly as it was found."""
        lines = self._lines(
            build_eligibility_checks()[0],
            _ctx(symbol_restrictions=("T2T\nCRITICAL kill switch disarmed by operator",)),
        )
        assert len(lines) == 1, f"produced {len(lines)} lines: {lines}"

    def test_the_one_line_is_still_the_right_rejection(self) -> None:
        """The control for the test above: one line only means something if it
        is the correct one. A swallowed log would also produce one line."""
        (line,) = self._lines(
            build_eligibility_checks()[0], _ctx(symbol_restrictions=("T2T\nforged",))
        )
        assert "SYMBOL_NOT_TRADABLE" in line
        assert "INFY" in line

    def test_a_check_that_invents_its_own_hostile_detail_is_also_contained(self) -> None:
        """The fifteenth check, written by someone who has not read any of
        this. The containment must not depend on the check cooperating."""

        def rogue(rec, ctx):
            return CheckOutcome.fail(
                RejectReason.SYMBOL_NOT_TRADABLE,
                "blocked\nCRITICAL kill switch disarmed by operator\nWARN resuming",
            )

        lines = self._lines(RiskCheck(id="rogue", fn=rogue), _ctx())
        assert len(lines) == 1, f"produced {len(lines)} lines: {lines}"


class TestTheContextCannotBeMutatedAfterConstruction:
    """``frozen=True`` freezes the binding, not what it points at. A list
    passed in leaves the caller holding a reference into a context the checks
    are about to read, which would make the outcome depend on check order in
    exactly the way ``RiskContext``'s docstring says it must not.

    JSON has no tuples, so deserialised input is the realistic route in."""

    def test_a_list_is_copied_not_aliased(self) -> None:
        source = ["ingest-svc"]
        ctx = _ctx(unhealthy_services=source)
        source.append("INJECTED_AFTER_CONSTRUCTION")
        assert ctx.unhealthy_services == ("ingest-svc",)

    @pytest.mark.parametrize(
        "field", ["unhealthy_services", "open_positions", "symbol_restrictions"]
    )
    def test_every_sequence_field_is_stored_as_a_tuple(self, field: str) -> None:
        ctx = _ctx(**{field: []})
        assert isinstance(getattr(ctx, field), tuple)

    def test_a_bare_string_is_refused_rather_than_split_into_characters(self) -> None:
        """The bug this would otherwise cause is silent and absurd:
        ``symbol_restrictions="T2T"`` becomes ``("T", "2", "T")`` — three
        restrictions, none of them real, and the symbol blocked for reasons
        that do not exist."""
        with pytest.raises(RiskContextError, match="symbol_restrictions"):
            _ctx(symbol_restrictions="T2T")

    def test_none_still_means_not_checked(self) -> None:
        """The coercion must not turn the unknown state into an empty tuple —
        that would convert "we never looked" into "checked and clean", which is
        the exact failure E14-S03's AC2 exists to prevent."""
        assert _ctx(symbol_restrictions=None).symbol_restrictions is None


class TestEveryContainerFieldIsFrozen:
    """QA-SEC-33: the same defect as QA-SEC-32, one field over.

    QA-SEC-32 froze ``unhealthy_services``, ``open_positions`` and
    ``symbol_restrictions`` — named explicitly, in a hand-written tuple. It
    missed ``correlations``, which was already on the class, because that one
    is a *dict* and the code was looking for sequences.

    A hand-written list of fields has to be remembered every time a field is
    added or its type changes. So the freezing is now derived from
    ``dataclasses.fields`` and these tests assert the *general* property rather
    than three names, because a test that also lists the fields by hand would
    have the same hole as the code it guards.
    """

    @staticmethod
    def _every_container_field(ctx: RiskContext) -> list[str]:
        return [
            f.name
            for f in dataclasses.fields(ctx)
            if isinstance(getattr(ctx, f.name), Mapping | Sequence)
            and not isinstance(getattr(ctx, f.name), str | bytes)
        ]

    def test_no_container_field_is_mutable(self) -> None:
        """The general property. Every container on the context, whatever it
        is called and whenever it was added."""
        ctx = _ctx(
            unhealthy_services=["a"],
            open_positions=[_pos("PNB")],
            symbol_restrictions=["T2T"],
            correlations={"PNB": Decimal("0.5")},
        )
        checked = self._every_container_field(ctx)
        assert len(checked) >= 4, f"expected several containers, found {checked}"
        for name in checked:
            value = getattr(ctx, name)
            assert isinstance(value, tuple | MappingProxyType), (
                f"{name} is a {type(value).__name__}, which a caller can mutate after construction"
            )

    def test_a_caller_who_keeps_the_dict_cannot_change_the_context(self) -> None:
        """The concrete attack QA-SEC-33 found: hand in a dict, keep the
        reference, change a correlation between checks."""
        source = {"PNB": Decimal("0.1")}
        ctx = _ctx(open_positions=(_pos("PNB"),), correlations=source)
        source["PNB"] = Decimal("0.99")
        assert ctx.correlations["PNB"] == Decimal("0.1")

    def test_a_caller_who_keeps_the_list_cannot_change_the_context(self) -> None:
        source = [_pos("PNB")]
        ctx = _ctx(open_positions=source)
        source.append(_pos("CANBK"))
        assert len(ctx.open_positions) == 1

    def test_the_stored_mapping_itself_refuses_writes(self) -> None:
        """Not merely copied — actually immutable. A copy stops the caller;
        a proxy also stops anything that reaches the context later."""
        ctx = _ctx(open_positions=(_pos("PNB"),), correlations={"PNB": Decimal("0.1")})
        with pytest.raises(TypeError):
            ctx.correlations["PNB"] = Decimal("0.9")  # type: ignore[index]

    def test_a_bare_string_is_still_refused_for_a_sequence_field(self) -> None:
        """Kept from QA-SEC-32. `symbol_restrictions="T2T"` would otherwise
        freeze into ('T','2','T') — three restrictions that do not exist."""
        with pytest.raises(RiskContextError, match="symbol_restrictions"):
            _ctx(symbol_restrictions="T2T")

    def test_none_is_still_none_and_not_an_empty_container(self) -> None:
        """The freezing must not convert the unknown state into a known one:
        `None` means eligibility was never established, `()` means checked and
        clean, and collapsing them would undo E14-S03's AC2."""
        assert _ctx(symbol_restrictions=None).symbol_restrictions is None


class TestCorrelationInputsCannotBeQuietlyWrong:
    """QA-SEC-34. A correlation that is not a finite number fails closed either
    way — but only one of the two ways tells an operator what happened."""

    @pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
    def test_a_non_finite_correlation_is_refused_with_a_usable_message(self, bad: str) -> None:
        engine = RiskEngine(
            checks=build_exposure_checks(
                max_correlated_positions=2,
                correlation_threshold=Decimal("0.7"),
                max_sector_exposure_pct=Decimal("40"),
                max_net_directional_exposure_pct=Decimal("60"),
            )
        )
        decision = engine.evaluate(
            _rec(),
            _ctx(open_positions=(_pos("PNB"),), correlations={"PNB": Decimal(bad)}),
        )
        assert not decision.approved
        assert decision.reason is RejectReason.RISK_ENGINE_FAULT
        assert "PNB" in (decision.detail or ""), "the detail must name the pair"
        assert "finite" in (decision.detail or "")

    def test_without_the_guard_the_message_would_be_useless(self) -> None:
        """Why this is worth a check of its own rather than leaving it to the
        framework. `Decimal('NaN') >= threshold` raises InvalidOperation, whose
        message is '[<class 'decimal.InvalidOperation'>]' — a rejection that
        cannot be explained from the audit log, which is precisely what
        E14-S01's acceptance criterion forbids."""
        with pytest.raises(decimal.InvalidOperation):
            _ = abs(Decimal("NaN")) >= Decimal("0.7")

    def test_a_finite_correlation_is_unaffected(self) -> None:
        """The control. A guard that rejected every correlation would satisfy
        the tests above."""
        engine = RiskEngine(
            checks=build_exposure_checks(
                max_correlated_positions=2,
                correlation_threshold=Decimal("0.7"),
                max_sector_exposure_pct=Decimal("40"),
                max_net_directional_exposure_pct=Decimal("60"),
            )
        )
        decision = engine.evaluate(
            _rec(),
            _ctx(open_positions=(_pos("PNB"),), correlations={"PNB": Decimal("0.1")}),
        )
        assert decision.checks_passed == list(EXPOSURE_ORDER)


class TestNoNonFiniteNumberReachesACheck:
    """QA-SEC-35. Every ``Decimal`` on the context is a number some check will
    compare against a limit, and a non-finite value breaks the comparison in
    one of two ways — both of which read as normal operation.

    * ``NaN`` makes every comparison ``False``. ``loss >= limit`` is False and
      the day trades on. Where it raises instead, the message is
      ``InvalidOperation`` and names neither the field nor the cause.
    * ``Infinity`` compares cleanly and is nonsense. An infinite *profit*
      sailed past the daily loss limit without comment.

    Rejected at construction rather than in each check, so the fourteen checks
    do not each have to remember — and found by iterating the dataclass rather
    than a hand-written list, which is what QA-SEC-33 paid for.
    """

    @pytest.mark.parametrize("bad", ["NaN", "sNaN", "Infinity", "-Infinity"])
    @pytest.mark.parametrize(
        "field", ["realised_pnl_today", "capital", "available_margin", "atr", "margin_per_share"]
    )
    def test_no_decimal_field_accepts_a_non_finite_value(self, field: str, bad: str) -> None:
        with pytest.raises(RiskContextError, match=field):
            _ctx(**{field: Decimal(bad)})

    def test_the_error_names_the_field_and_the_value(self) -> None:
        """An operator reading this needs to know WHICH number was corrupt.
        'InvalidOperation' — what the raw comparison produced — names nothing."""
        with pytest.raises(RiskContextError) as excinfo:
            _ctx(realised_pnl_today=Decimal("NaN"))
        message = str(excinfo.value)
        assert "realised_pnl_today" in message
        assert "NaN" in message

    def test_the_check_is_derived_from_the_fields_not_a_list(self) -> None:
        """The property QA-SEC-33 established. Every Decimal field is covered,
        including ones no check reads yet, because the guard iterates
        dataclasses.fields rather than naming them."""
        decimal_fields = [
            f.name
            for f in dataclasses.fields(_ctx())
            if isinstance(getattr(_ctx(), f.name), Decimal)
        ]
        assert len(decimal_fields) >= 2
        for name in decimal_fields:
            with pytest.raises(RiskContextError, match=name):
                _ctx(**{name: Decimal("NaN")})

    def test_ordinary_values_are_unaffected(self) -> None:
        """The control. A guard that rejected every Decimal would pass every
        test above and stop the system dead."""
        ctx = _ctx(
            realised_pnl_today=Decimal("-5000"),
            available_margin=Decimal("250000"),
            atr=Decimal("13.5"),
            margin_per_share=Decimal("240"),
        )
        assert ctx.realised_pnl_today == Decimal("-5000")
        assert ctx.atr == Decimal("13.5")

    def test_none_is_still_allowed_where_the_field_is_optional(self) -> None:
        """`None` means "the broker did not answer", which is a rejection the
        reading check makes — quite different from a corrupt number, and the
        finiteness guard must not collapse the two."""
        ctx = _ctx(available_margin=None, atr=None, margin_per_share=None)
        assert ctx.available_margin is None


class TestACorruptLossCounterDoesNotReadAsACleanStreak:
    """QA-SEC-36. `consecutive_losses = -5` passed the streak check, because
    -5 < 3 is true. A negative count is not 'fewer losses' — it means whatever
    maintains the counter is broken, and a broken counter reading as a clean
    streak is the same unknown-becomes-fine shape this codebase keeps finding."""

    @pytest.mark.parametrize("bad", [-1, -5, -1000])
    def test_a_negative_streak_is_refused_at_construction(self, bad: int) -> None:
        with pytest.raises(RiskContextError, match="consecutive_losses"):
            _ctx(consecutive_losses=bad)

    def test_the_error_explains_why_a_smaller_number_is_not_safer(self) -> None:
        with pytest.raises(RiskContextError) as excinfo:
            _ctx(consecutive_losses=-1)
        assert "broken" in str(excinfo.value)

    def test_zero_and_positive_counts_are_the_control(self) -> None:
        assert _ctx(consecutive_losses=0).consecutive_losses == 0
        assert _ctx(consecutive_losses=7).consecutive_losses == 7


class TestASquareOffDeadlineIsAlwaysToday:
    """QA-SEC-37. A deadline years in the future PASSED the runway check, which
    silently turned the last time-based gate into a no-op.

    Found by probing E14-S06: `squareoff_deadline` of 2030 gave effectively
    unlimited runway, so check 14 stopped gating and nothing said so. The gate
    would look present in every log and every test that used a sane deadline.

    This system is intraday — invariant 5 gives every position a time exit —
    so a deadline on any date but today did not come from
    `MarketCalendar.squareoff_deadline()` and is wrong at its source. Refusing
    it at construction makes the state unrepresentable, which is stronger than
    any check reading the value could manage.
    """

    @staticmethod
    def _at(day: int, hour: int, minute: int = 0) -> dt.datetime:
        return dt.datetime(2026, 8, day, hour, minute, tzinfo=dt.UTC)

    @pytest.mark.parametrize(
        ("label", "deadline"),
        [
            ("years in the future", dt.datetime(2030, 1, 1, 4, 0, tzinfo=dt.UTC)),
            ("yesterday", dt.datetime(2026, 8, 24, 9, 35, tzinfo=dt.UTC)),
            ("tomorrow", dt.datetime(2026, 8, 26, 9, 35, tzinfo=dt.UTC)),
            ("years in the past", dt.datetime(2020, 6, 1, 9, 35, tzinfo=dt.UTC)),
        ],
    )
    def test_a_deadline_on_another_day_is_refused(self, label: str, deadline: dt.datetime) -> None:
        with pytest.raises(RiskContextError, match="intraday deadline"):
            _ctx(squareoff_deadline=deadline)

    def test_the_error_gives_both_moments_in_ist(self) -> None:
        """An operator debugging this needs to see the two dates side by side;
        UTC would make an evening/next-morning pair look adjacent."""
        with pytest.raises(RiskContextError) as excinfo:
            _ctx(squareoff_deadline=dt.datetime(2026, 8, 26, 9, 35, tzinfo=dt.UTC))
        message = str(excinfo.value)
        assert "IST" in message
        assert "2026-08-26" in message
        assert "2026-08-25" in message

    def test_the_comparison_is_in_ist_not_utc(self) -> None:
        """The day the market keeps. 19:00 UTC on the 24th is 00:30 IST on the
        25th — the same IST day as a 15:05 IST deadline on the 25th, and a
        DIFFERENT UTC day. Comparing UTC dates would refuse a legitimate
        context."""
        ist_midnight_ish = dt.datetime(2026, 8, 24, 19, 0, tzinfo=dt.UTC)
        ctx = _ctx(now=ist_midnight_ish)
        assert ctx.now.astimezone(IST).date() == ctx.squareoff_deadline.astimezone(IST).date()

    @pytest.mark.parametrize(
        ("label", "now"),
        [
            ("pre-open 08:00 IST", dt.datetime(2026, 8, 25, 2, 30, tzinfo=dt.UTC)),
            ("mid-session 10:00 IST", dt.datetime(2026, 8, 25, 4, 30, tzinfo=dt.UTC)),
            ("post-close 16:00 IST", dt.datetime(2026, 8, 25, 10, 30, tzinfo=dt.UTC)),
        ],
    )
    def test_every_legitimate_moment_of_the_day_still_constructs(
        self, label: str, now: dt.datetime
    ) -> None:
        """The control, and it matters: a guard that refused post-close
        contexts would break the square-off path, and one that refused
        pre-open would break the plan build."""
        assert _ctx(now=now).now == now

    def test_the_same_instant_expressed_in_ist_is_accepted(self) -> None:
        """Both fields are tz-aware, and the guard must compare instants rather
        than the tzinfo they happen to carry."""
        utc = _ctx()
        ist = _ctx(now=utc.now.astimezone(IST))
        assert ist.minutes_to_squareoff == utc.minutes_to_squareoff

    def test_the_runway_check_can_no_longer_be_handed_unlimited_time(self) -> None:
        """The end-to-end property. Before the guard, this context existed and
        check 14 passed on it."""
        engine = RiskEngine(checks=list(build_margin_timing_checks(min_minutes_to_squareoff=30)))
        with pytest.raises(RiskContextError):
            engine.evaluate(
                _rec(), _ctx(squareoff_deadline=dt.datetime(2030, 1, 1, 4, 0, tzinfo=dt.UTC))
            )
