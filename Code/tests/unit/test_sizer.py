"""ATR-based position sizing (E14-S07) — one class per acceptance criterion.

AC1  PROPERTY: realised risk never exceeds risk_pct, for any generated input
AC2  a small or zero position is explainable from binding_constraint alone
AC3  PROPERTY: quantity always floors to a whole lot, never rounds to nearest
AC4  a zero quantity REJECTS
AC5  the stop comes from ATR × multiplier, not from suggested_stop
AC6  unknown ATR rejects; non-positive ATR is unrepresentable
AC7  each clamp binds when it should and names itself
AC8  the stop is on the correct side of entry; the target is R away
AC9  the CONTROL — an ordinary candidate produces a sensible quantity
AC10 PROPERTY: sizing is deterministic and pure

AC1 and AC3 are the same fact seen twice: flooring is *why* the risk bound
holds. Round-to-nearest would break AC1 on every round-up, by up to one lot's
worth of stop distance, and the breach would be invisible.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from algotrader.common.enums import AIVerdict, Direction, RejectReason
from algotrader.common.metrics import reset_metrics_for_testing
from algotrader.common.models.trading import Recommendation
from algotrader.execution.risk.context import RiskContext, RiskContextError
from algotrader.execution.risk.framework import RiskCheck, RiskEngine
from algotrader.execution.sizer import (
    LOT_ROUNDING,
    MARGIN_CAP,
    POSITION_CAP,
    RISK_BUDGET,
    SLOT_CAP,
    SizingPolicy,
    build_sizer,
    size_position,
)

MIDSESSION = dt.datetime(2026, 8, 25, 4, 30, tzinfo=dt.UTC)
CAS_DEADLINE = dt.datetime(2026, 8, 25, 9, 35, tzinfo=dt.UTC)
CID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

#: What system.yaml configures.
POLICY = SizingPolicy(
    risk_pct=Decimal("1.0"),
    atr_multiplier_stop=Decimal("1.5"),
    max_position_pct=Decimal("20"),
    capital_per_slot_pct=Decimal("20"),
    target_r_multiple=Decimal("2.0"),
)
CAPITAL = Decimal("500000")


@pytest.fixture(autouse=True)
def _fresh_metrics() -> None:
    reset_metrics_for_testing()


def _rec(
    symbol: str = "INFY",
    direction: Direction = Direction.LONG,
    entry: str = "100.00",
) -> Recommendation:
    price = Decimal(entry)
    long = direction is Direction.LONG
    return Recommendation(
        correlation_id=CID,
        symbol=symbol,
        strategy_id="orb_long_v1",
        direction=direction,
        trigger_price=price,
        # Deliberately NOT the stop the sizer computes — AC5 asserts it is
        # ignored, so it must differ from ATR × multiplier. A fraction rather
        # than a fixed rupee, because `entry - 1` is not a valid Price at an
        # entry of 1.00 and the property tests generate exactly that.
        suggested_stop=price * (Decimal("0.99") if long else Decimal("1.01")),
        timeframe_agreement=3,
        ai_confidence=Decimal("0.82"),
        ai_verdict=AIVerdict.CONFIRM,
        ai_rationale="probe",
        emitted_at=MIDSESSION,
    )


def _ctx(**overrides) -> RiskContext:
    """Ample everything, so a test that wants one clamp to bind says so."""
    base: dict = {
        "now": MIDSESSION,
        "squareoff_deadline": CAS_DEADLINE,
        "capital": CAPITAL,
        "slots_total": 5,
        "slots_used": 0,
        "atr": Decimal("13.5000"),
        "available_margin": Decimal("10000000"),
        "margin_per_share": Decimal("1"),
        "lot_size": 1,
    }
    base.update(overrides)
    return RiskContext(**base)


def _size(rec=None, **ctx_kwargs):
    return size_position(rec or _rec(), _ctx(**ctx_kwargs), POLICY)


class TestAC1RiskNeverExceedsTheBudget:
    """The property the whole story exists for."""

    def test_a_worked_example(self) -> None:
        """Numbers a reader can check by hand: 1% of 500,000 is 5,000; ATR 13.5
        × 1.5 is a 20.25 stop distance; 5,000 / 20.25 is 246.9, floored to 246.
        At a 100-rupee entry the caps allow 1,000 shares, so the risk budget is
        what binds."""
        result = _size()
        assert result.quantity == 246
        assert result.binding_constraint == RISK_BUDGET
        assert result.capital_at_risk == Decimal("246") * Decimal("20.2500")
        assert result.capital_at_risk <= CAPITAL * Decimal("1.0") / 100

    def test_an_expensive_stock_is_bound_by_the_position_cap_instead(self) -> None:
        """Worth its own test because it is the common case and it surprised me
        while writing the worked example: at a 1,200-rupee entry the 20%
        position cap allows 83 shares while the risk budget would allow 246. A
        high-priced name therefore risks LESS than the configured 1% — safe,
        and explainable only because binding_constraint says which limit it
        was."""
        result = _size(rec=_rec(entry="1200.00"))
        assert result.quantity == 83
        assert result.binding_constraint == POSITION_CAP
        assert result.capital_at_risk < CAPITAL * Decimal("1.0") / 100

    @settings(max_examples=400, deadline=None)
    @given(
        capital=st.decimals(min_value=10_000, max_value=100_000_000, places=2),
        risk_pct=st.decimals(min_value="0.1", max_value="5.0", places=2),
        atr=st.decimals(min_value="0.05", max_value="500", places=4),
        entry=st.decimals(min_value="1", max_value="100000", places=2),
        lot_size=st.sampled_from([1, 5, 25, 50, 100, 1200]),
    )
    def test_realised_risk_never_exceeds_the_budget(
        self,
        capital: Decimal,
        risk_pct: Decimal,
        atr: Decimal,
        entry: Decimal,
        lot_size: int,
    ) -> None:
        """AC1, over the input space rather than over my imagination.

        `capital_at_risk = quantity × stop_distance` must never exceed
        `capital × risk_pct / 100` — for any capital, any risk percentage, any
        volatility, any price and any lot size.
        """
        policy = SizingPolicy(
            risk_pct=risk_pct,
            atr_multiplier_stop=Decimal("1.5"),
            max_position_pct=Decimal("20"),
            capital_per_slot_pct=Decimal("20"),
            target_r_multiple=Decimal("2.0"),
        )
        ctx = _ctx(capital=capital, atr=atr, lot_size=lot_size)
        result = size_position(_rec(entry=str(entry)), ctx, policy)
        budget = capital * risk_pct / 100
        assert result.capital_at_risk <= budget, (
            f"risked {result.capital_at_risk} against a budget of {budget}"
        )

    @settings(max_examples=200, deadline=None)
    @given(
        atr=st.decimals(min_value="0.05", max_value="500", places=4),
        entry=st.decimals(min_value="1", max_value="100000", places=2),
    )
    def test_the_bound_holds_for_shorts_too(self, atr: Decimal, entry: Decimal) -> None:
        """Direction changes which side the stop sits on, and must not change
        the risk arithmetic."""
        result = size_position(
            _rec(direction=Direction.SHORT, entry=str(entry)), _ctx(atr=atr), POLICY
        )
        assert result.capital_at_risk <= CAPITAL * Decimal("1.0") / 100

    def test_a_tighter_stop_buys_more_shares_at_the_same_risk(self) -> None:
        """The point of ATR sizing, as a comparison rather than a formula. A
        quieter name gets a bigger position for the SAME rupees at risk."""
        quiet = _size(atr=Decimal("5.0000"))
        volatile = _size(atr=Decimal("50.0000"))
        assert quiet.quantity > volatile.quantity
        budget = CAPITAL * Decimal("1.0") / 100
        assert quiet.capital_at_risk <= budget
        assert volatile.capital_at_risk <= budget


class TestAC2TheBindingConstraintExplainsIt:
    def test_an_ordinary_position_is_bound_by_the_risk_budget(self) -> None:
        assert _size().binding_constraint == RISK_BUDGET

    def test_a_zero_from_lot_rounding_says_so(self) -> None:
        """A 1200-lot F&O contract against a budget that affords 246 shares.
        Naming a clamp here would point at the wrong thing — every clamp was
        satisfied; the lot simply does not divide."""
        result = _size(lot_size=1200)
        assert result.quantity == 0
        assert result.binding_constraint == LOT_ROUNDING

    def test_a_zero_from_margin_says_margin(self) -> None:
        result = _size(available_margin=Decimal("0.5"), margin_per_share=Decimal("1"))
        assert result.quantity == 0
        assert result.binding_constraint == MARGIN_CAP

    def test_a_zero_that_is_not_a_margin_problem_is_not_reported_as_one(self) -> None:
        """AC2's real content, and the reason RejectReason gained
        POSITION_TOO_SMALL. A well-funded account whose lot does not divide
        must not send an operator to look at funds that are fine."""
        engine = RiskEngine(
            checks=[
                RiskCheck(
                    "ok",
                    lambda rec, ctx: __import__(
                        "algotrader.execution.risk.framework", fromlist=["CheckOutcome"]
                    ).CheckOutcome.ok(),
                )
            ],
            sizer=build_sizer(POLICY),
        )
        decision = engine.evaluate(
            _rec(), _ctx(lot_size=1200, available_margin=Decimal("10000000"))
        )
        assert not decision.approved
        assert decision.reason is RejectReason.POSITION_TOO_SMALL
        assert LOT_ROUNDING in (decision.detail or "")

    def test_a_genuine_margin_shortfall_still_says_insufficient_margin(self) -> None:
        """The control for the test above. Splitting one reason into two is
        only useful if each still fires for its own case."""
        engine = RiskEngine(
            checks=[],
            sizer=build_sizer(POLICY),
        )
        decision = engine.evaluate(
            _rec(), _ctx(available_margin=Decimal("0.5"), margin_per_share=Decimal("1"))
        )
        assert decision.reason is RejectReason.INSUFFICIENT_MARGIN

    def test_the_framework_and_the_sizer_agree_on_the_margin_name(self) -> None:
        """The frame duplicates this one string rather than importing the
        sizer, because the sizer is injected. The duplication is only safe if
        something asserts they match."""
        from algotrader.execution.risk import framework

        assert framework.MARGIN_CAP == MARGIN_CAP


class TestAC3QuantityAlwaysFloorsToAWholeLot:
    @settings(max_examples=300, deadline=None)
    @given(
        atr=st.decimals(min_value="0.05", max_value="200", places=4),
        lot_size=st.sampled_from([1, 5, 25, 50, 100, 1200]),
    )
    def test_the_quantity_is_always_a_whole_number_of_lots(
        self, atr: Decimal, lot_size: int
    ) -> None:
        result = _size(atr=atr, lot_size=lot_size)
        assert result.quantity % lot_size == 0

    @settings(max_examples=300, deadline=None)
    @given(atr=st.decimals(min_value="0.05", max_value="200", places=4))
    def test_it_never_rounds_up(self, atr: Decimal) -> None:
        """The Build Concerns note as a property: the quantity must never
        exceed what the unrounded budget allowed."""
        result = _size(atr=atr)
        stop_distance = atr * POLICY.atr_multiplier_stop
        raw = (CAPITAL * POLICY.risk_pct / 100) / stop_distance
        assert Decimal(result.quantity) <= raw

    def test_a_fractional_result_loses_the_fraction(self) -> None:
        """246.9 shares becomes 246, not 247. One share of a 20.25 stop
        distance is 20 rupees of unbudgeted risk — small, and it compounds
        across every trade the system ever makes."""
        assert _size().quantity == 246

    def test_lot_rounding_applies_to_the_clamped_value_not_the_raw_one(self) -> None:
        """Order matters: floor(min(...)) and min(floor(...)) differ when a cap
        binds, and only the first keeps the quantity inside every clamp."""
        result = _size(lot_size=100, available_margin=Decimal("150"), margin_per_share=Decimal("1"))
        # The margin cap allows 150 shares and the risk budget 246; the
        # smaller is what gets floored to a 100-lot, not the raw 246.
        assert result.quantity == 100


class TestAC4AZeroQuantityRejects:
    def test_the_engine_rejects_rather_than_approving_nothing(self) -> None:
        engine = RiskEngine(checks=[], sizer=build_sizer(POLICY))
        decision = engine.evaluate(_rec(), _ctx(lot_size=1200))
        assert not decision.approved
        assert decision.sizing is None or decision.sizing.quantity == 0

    def test_it_never_returns_an_approval_carrying_zero(self) -> None:
        """The dangerous shape: an APPROVED decision with quantity 0 would
        reach the order gateway and place nothing, silently."""
        engine = RiskEngine(checks=[], sizer=build_sizer(POLICY))
        for lot in (100, 500, 1200, 5000):
            decision = engine.evaluate(_rec(), _ctx(lot_size=lot))
            if decision.approved:
                assert decision.sizing is not None
                assert decision.sizing.quantity > 0, f"approved 0 shares at lot {lot}"

    def test_an_atr_that_vanishes_at_four_decimal_places_rejects(self) -> None:
        """`Price` carries 4 decimals, so an ATR of 0.00005 x 1.5 quantises to
        a zero-width stop. Dividing the budget by it would be an infinite
        quantity, and a stop at the entry price is not a stop."""
        result = _size(atr=Decimal("0.00005"))
        assert result.quantity == 0
        assert "zero-width stop" in result.binding_constraint

    def test_a_clamp_of_exactly_zero_produces_no_position(self) -> None:
        """An empty account reaching the sizer directly — check 13 normally
        stops it first, but the sizer must not divide its way to a negative or
        infinite quantity if it ever does."""
        result = _size(available_margin=Decimal("0"), margin_per_share=Decimal("1"))
        assert result.quantity == 0
        assert result.binding_constraint == MARGIN_CAP

    def test_a_stop_wider_than_the_price_rejects(self) -> None:
        """A penny stock with an enormous ATR. `Price` requires gt=0, so a
        negative stop would raise somewhere far less informative."""
        result = _size(rec=_rec(entry="5.00"), atr=Decimal("100.0000"))
        assert result.quantity == 0
        assert "not less than the entry price" in result.binding_constraint


class TestAC5TheStopComesFromATRNotTheRecommendation:
    def test_the_stop_is_atr_times_the_multiplier_from_entry(self) -> None:
        result = _size()
        assert result.stop_price == Decimal("100.00") - Decimal("20.2500")

    def test_the_suggested_stop_is_ignored(self) -> None:
        """Invariant 1: a Recommendation whose suggested_stop drove the
        quantity would be carrying a sizing field under another name."""
        rec = _rec()
        assert rec.suggested_stop == Decimal("99.0000")  # deliberately different
        result = size_position(rec, _ctx(), POLICY)
        assert result.stop_price != rec.suggested_stop

    def test_changing_the_suggested_stop_changes_nothing(self) -> None:
        """The probe. If suggested_stop were an input, this would differ."""
        near = _rec()
        far = Recommendation(**{**near.model_dump(), "suggested_stop": Decimal("50.00")})
        assert size_position(near, _ctx(), POLICY).quantity == (
            size_position(far, _ctx(), POLICY).quantity
        )


class TestAC6ATRMustBeKnownAndPositive:
    def test_unknown_atr_raises(self) -> None:
        with pytest.raises(RiskContextError, match="ATR"):
            _size(atr=None)

    def test_the_engine_turns_that_into_a_fault(self) -> None:
        engine = RiskEngine(checks=[], sizer=build_sizer(POLICY))
        decision = engine.evaluate(_rec(), _ctx(atr=None))
        assert decision.reason is RejectReason.RISK_ENGINE_FAULT

    @pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-1"), Decimal("-13.5")])
    def test_a_non_positive_atr_is_refused_at_construction(self, bad: Decimal) -> None:
        with pytest.raises(RiskContextError, match="atr"):
            _ctx(atr=bad)

    def test_the_error_explains_the_division(self) -> None:
        with pytest.raises(RiskContextError) as excinfo:
            _ctx(atr=Decimal("0"))
        assert "infinite quantity" in str(excinfo.value)

    @pytest.mark.parametrize("bad", [0, -1])
    def test_a_nonsensical_lot_size_is_refused_at_construction(self, bad: int) -> None:
        with pytest.raises(RiskContextError, match="lot_size"):
            _ctx(lot_size=bad)


class TestAC7EachClampBindsAndNamesItself:
    def test_the_risk_budget_binds_by_default(self) -> None:
        assert _size().binding_constraint == RISK_BUDGET

    def test_the_position_cap_binds_when_it_is_the_smallest(self) -> None:
        """A very tight stop makes the risk budget enormous in share terms, so
        the 20%-of-capital position cap is what actually limits it."""
        result = _size(atr=Decimal("0.0500"))
        assert result.binding_constraint in (POSITION_CAP, SLOT_CAP)

    def test_the_margin_cap_binds_when_margin_is_the_constraint(self) -> None:
        result = _size(available_margin=Decimal("1000"), margin_per_share=Decimal("100"))
        assert result.binding_constraint == MARGIN_CAP
        assert result.quantity == 10

    def test_a_tie_reports_the_earlier_clamp(self) -> None:
        """When the risk budget and a cap agree on the number, risk is what
        bound it and the cap merely concurred. Reporting the cap would send an
        operator to widen a limit that was not the reason."""
        # position_cap and slot_cap are both 20% here, so they tie exactly.
        result = _size(atr=Decimal("0.0500"))
        assert result.binding_constraint == POSITION_CAP

    def test_the_binding_constraint_is_never_empty(self) -> None:
        for kwargs in (
            {},
            {"lot_size": 1200},
            {"available_margin": Decimal("1000"), "margin_per_share": Decimal("100")},
            {"atr": Decimal("0.0500")},
        ):
            assert _size(**kwargs).binding_constraint


class TestAC8TheStopAndTargetSitOnTheRightSides:
    def test_a_long_stops_below_and_targets_above(self) -> None:
        result = _size(rec=_rec(direction=Direction.LONG))
        assert result.stop_price < result.entry_price
        assert result.target_price is not None
        assert result.target_price > result.entry_price

    def test_a_short_stops_above_and_targets_below(self) -> None:
        result = _size(rec=_rec(direction=Direction.SHORT))
        assert result.stop_price > result.entry_price
        assert result.target_price is not None
        assert result.target_price < result.entry_price

    def test_the_target_is_r_multiple_times_the_stop_distance(self) -> None:
        result = _size()
        stop_distance = result.entry_price - result.stop_price
        assert result.target_price is not None
        assert result.target_price - result.entry_price == stop_distance * Decimal("2.0")

    @settings(max_examples=200, deadline=None)
    @given(
        atr=st.decimals(min_value="0.05", max_value="100", places=4),
        long=st.booleans(),
    )
    def test_the_stop_is_always_on_the_losing_side(self, atr: Decimal, long: bool) -> None:
        """A stop on the wrong side is not a stop — it is a target that closes
        the position at a profit and lets the loss run."""
        direction = Direction.LONG if long else Direction.SHORT
        result = _size(rec=_rec(direction=direction), atr=atr)
        if result.quantity == 0:
            return
        if long:
            assert result.stop_price < result.entry_price
        else:
            assert result.stop_price > result.entry_price


class TestAC9TheControl:
    """A sizer that always returned 0 would satisfy AC1 and AC3 perfectly."""

    def test_an_ordinary_candidate_gets_a_real_position(self) -> None:
        result = _size()
        assert result.quantity > 0
        assert result.notional > 0
        assert result.capital_at_risk > 0

    def test_the_engine_approves_a_clean_candidate(self) -> None:
        """The first approval this system has ever produced."""
        engine = RiskEngine(checks=[], sizer=build_sizer(POLICY))
        decision = engine.evaluate(_rec(), _ctx())
        assert decision.approved
        assert decision.sizing is not None
        assert decision.sizing.quantity == 246
        assert decision.sizing.binding_constraint == RISK_BUDGET

    @settings(max_examples=200, deadline=None)
    @given(atr=st.decimals(min_value="0.05", max_value="60", places=4))
    def test_a_realistic_range_of_volatility_produces_a_position(self, atr: Decimal) -> None:
        """Across ordinary NSE volatility the sizer must actually size. A bound
        that is always satisfied by returning nothing is not a risk control,
        it is an off switch."""
        assert _size(atr=atr).quantity > 0


class TestAC10SizingIsPureAndDeterministic:
    def test_the_same_inputs_give_the_same_result(self) -> None:
        first = _size()
        second = _size()
        assert first == second

    def test_it_reads_no_clock(self) -> None:
        """Two contexts differing only in `now` must size identically. If the
        sizer consulted the clock, replay would be impossible."""
        early = _size(now=dt.datetime(2026, 8, 25, 4, 0, tzinfo=dt.UTC))
        late = _size(now=dt.datetime(2026, 8, 25, 8, 0, tzinfo=dt.UTC))
        assert early == late

    def test_the_module_imports_nothing_that_does_io(self) -> None:
        import ast
        import pathlib

        import algotrader.execution.sizer as sizer_module

        tree = ast.parse(pathlib.Path(sizer_module.__file__).read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module.split(".")[0])
        banned = {"random", "time", "datetime", "socket", "requests", "httpx", "os", "redis"}
        assert not (modules & banned), f"sizing can reach {modules & banned}"

    def test_a_nonsensical_policy_is_refused_at_construction(self) -> None:
        for field in (
            "risk_pct",
            "atr_multiplier_stop",
            "max_position_pct",
            "capital_per_slot_pct",
            "target_r_multiple",
        ):
            kwargs = {
                "risk_pct": Decimal("1.0"),
                "atr_multiplier_stop": Decimal("1.5"),
                "max_position_pct": Decimal("20"),
                "capital_per_slot_pct": Decimal("20"),
                "target_r_multiple": Decimal("2.0"),
                field: Decimal("0"),
            }
            with pytest.raises(ValueError, match=field):
                SizingPolicy(**kwargs)  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad", ["NaN", "Infinity"])
    def test_a_non_finite_policy_value_is_refused(self, bad: str) -> None:
        with pytest.raises(ValueError, match="risk_pct"):
            SizingPolicy(
                risk_pct=Decimal(bad),
                atr_multiplier_stop=Decimal("1.5"),
                max_position_pct=Decimal("20"),
                capital_per_slot_pct=Decimal("20"),
                target_r_multiple=Decimal("2.0"),
            )
