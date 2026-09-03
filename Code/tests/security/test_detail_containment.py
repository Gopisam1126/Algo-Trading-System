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

from algotrader.common.enums import AIVerdict, Direction, RejectReason
from algotrader.common.models.trading import Recommendation
from algotrader.execution.risk.checks import (
    EXPOSURE_ORDER,
    build_eligibility_checks,
    build_exposure_checks,
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
        "squareoff_deadline": NOW + dt.timedelta(hours=4),
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
