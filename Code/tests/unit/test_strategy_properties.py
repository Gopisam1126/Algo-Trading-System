"""Property-based tests for the strategy runtime.

Example-based tests assert what I thought to check. These assert what must hold
for *every* input, which is the right tool for two things here:

**Tick snapping**, because it is arithmetic that feeds an order. The properties
are absolute — a snapped stop is always on the grid, always on the protective
side of the raw price, and never more than one tick away from it — and any
input that breaks one is a stop that either gets rejected by the exchange or
silently changes the risk the strategy declared.

**Three-valued composition**, because the truth tables have 3^n rows and I would
otherwise test the handful I imagined. The property that matters is the
fail-closed one: an entry fires only when every condition is definitively true,
and no combination containing an UNKNOWN may ever fire.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from algotrader.common.enums import Direction
from algotrader.strategy.primitives.evaluators import snap_stop, snap_target
from algotrader.strategy.runtime import _all_of, _any_of, _none_of

#: Real NSE tick sizes. 0.01 for sub-250 scrips, 0.05 for most equities.
TICKS = st.sampled_from([Decimal("0.01"), Decimal("0.05"), Decimal("0.10"), Decimal("0.25")])

PRICES = st.decimals(
    min_value=Decimal("0.01"),
    max_value=Decimal("500000"),
    allow_nan=False,
    allow_infinity=False,
    places=4,
)

DIRECTIONS = st.sampled_from([Direction.LONG, Direction.SHORT])

#: True / False / UNKNOWN.
TRISTATE = st.sampled_from([True, False, None])


class TestSnappingIsAlwaysPlaceable:
    @given(price=PRICES, tick=TICKS, direction=DIRECTIONS)
    @settings(max_examples=400)
    def test_a_snapped_stop_lands_on_the_tick_grid(
        self, price: Decimal, tick: Decimal, direction: Direction
    ) -> None:
        """NSE rejects an off-grid price outright, so this is the difference
        between a protective stop and no protection at all."""
        assert snap_stop(price, tick, direction) % tick == 0

    @given(price=PRICES, tick=TICKS, direction=DIRECTIONS)
    @settings(max_examples=400)
    def test_a_snapped_target_lands_on_the_tick_grid(
        self, price: Decimal, tick: Decimal, direction: Direction
    ) -> None:
        assert snap_target(price, tick, direction) % tick == 0

    @given(price=PRICES, tick=TICKS, direction=DIRECTIONS)
    @settings(max_examples=400)
    def test_snapping_never_moves_more_than_one_tick(
        self, price: Decimal, tick: Decimal, direction: Direction
    ) -> None:
        """A stop that jumped several ticks would change the declared risk by
        an amount nobody chose."""
        assert abs(snap_stop(price, tick, direction) - price) < tick

    @given(price=PRICES, tick=TICKS)
    @settings(max_examples=400)
    def test_a_long_stop_never_moves_toward_the_entry(self, price: Decimal, tick: Decimal) -> None:
        """Rounding inward would silently turn a declared 1.5x ATR stop into
        1.49x — tightening every stop in the system and inventing whipsaws."""
        assert snap_stop(price, tick, Direction.LONG) <= price

    @given(price=PRICES, tick=TICKS)
    @settings(max_examples=400)
    def test_a_short_stop_never_moves_toward_the_entry(self, price: Decimal, tick: Decimal) -> None:
        assert snap_stop(price, tick, Direction.SHORT) >= price

    @given(price=PRICES, tick=TICKS)
    @settings(max_examples=300)
    def test_a_target_never_moves_away_from_the_entry(self, price: Decimal, tick: Decimal) -> None:
        """The mirror of a stop: a target only pays if it fills."""
        assert snap_target(price, tick, Direction.LONG) <= price
        assert snap_target(price, tick, Direction.SHORT) >= price

    @given(price=PRICES, tick=TICKS, direction=DIRECTIONS)
    @settings(max_examples=400)
    def test_the_result_always_fits_the_price_type(
        self, price: Decimal, tick: Decimal, direction: Direction
    ) -> None:
        """``Price`` accepts at most four decimal places; anything more is a
        ValidationError at whichever downstream site builds the model first."""
        snapped = snap_stop(price, tick, direction)
        assert -snapped.as_tuple().exponent <= 4

    @given(price=PRICES, tick=TICKS, direction=DIRECTIONS)
    @settings(max_examples=300)
    def test_snapping_is_idempotent(
        self, price: Decimal, tick: Decimal, direction: Direction
    ) -> None:
        """An already-snapped price must survive a second pass unchanged, or
        any retry path would walk the stop away from where it was placed."""
        once = snap_stop(price, tick, direction)
        assert snap_stop(once, tick, direction) == once

    @given(price=PRICES, tick=TICKS, direction=DIRECTIONS)
    @settings(max_examples=200)
    def test_a_positive_price_never_snaps_negative(
        self, price: Decimal, tick: Decimal, direction: Direction
    ) -> None:
        """A negative stop is a stop that can never trigger."""
        assert snap_stop(price, tick, direction) >= 0


class TestThreeValuedCompositionFailsClosed:
    """The truth tables have 3^n rows. These assert the whole space."""

    @given(outcomes=st.lists(TRISTATE, min_size=1, max_size=6))
    @settings(max_examples=500)
    def test_all_of_is_true_only_when_everything_is_true(self, outcomes: list[bool | None]) -> None:
        assert (_all_of(outcomes) is True) == all(o is True for o in outcomes)

    @given(outcomes=st.lists(TRISTATE, min_size=1, max_size=6))
    @settings(max_examples=500)
    def test_all_of_never_returns_true_with_an_unknown_present(
        self, outcomes: list[bool | None]
    ) -> None:
        assume(any(o is None for o in outcomes))
        assert _all_of(outcomes) is not True

    @given(outcomes=st.lists(TRISTATE, min_size=1, max_size=6))
    @settings(max_examples=500)
    def test_any_of_is_true_exactly_when_some_alternative_holds(
        self, outcomes: list[bool | None]
    ) -> None:
        """A definite True settles a disjunction whatever the rest say — an
        unreadable alternative must not block a decision already made."""
        assert (_any_of(outcomes) is True) == any(o is True for o in outcomes)

    @given(outcomes=st.lists(TRISTATE, min_size=1, max_size=6))
    @settings(max_examples=500)
    def test_none_of_never_passes_when_anything_is_unknown(
        self, outcomes: list[bool | None]
    ) -> None:
        """THE fail-open this design exists to prevent. ``none_of`` asserts an
        absence; a condition that could not be evaluated has not been shown to
        be absent, so it must never satisfy the guard. Collapsing it to True
        would mean a dead news feed granted permission to trade."""
        assume(any(o is None for o in outcomes))
        assert _none_of(outcomes) is not True

    @given(outcomes=st.lists(st.sampled_from([True, False]), min_size=1, max_size=6))
    @settings(max_examples=300)
    def test_none_of_is_ordinary_negation_when_everything_is_known(
        self, outcomes: list[bool]
    ) -> None:
        """The control: with no UNKNOWN present it must behave exactly as a
        two-valued NOT-ANY, or the tri-state handling has changed the meaning
        of the operator itself."""
        assert _none_of(list(outcomes)) is (not any(outcomes))

    @given(outcomes=st.lists(TRISTATE, min_size=1, max_size=6))
    @settings(max_examples=500)
    def test_every_operator_returns_a_tristate(self, outcomes: list[bool | None]) -> None:
        for operator in (_all_of, _any_of, _none_of):
            assert operator(list(outcomes)) in (True, False, None)

    @given(outcomes=st.lists(TRISTATE, min_size=1, max_size=6))
    @settings(max_examples=400)
    def test_a_definite_false_beats_unknown_in_all_of(self, outcomes: list[bool | None]) -> None:
        """Both block entry, but False is actionable and UNKNOWN is a health
        signal. Conflating them hides a broken feed behind a normal no-trade."""
        assume(any(o is False for o in outcomes))
        assert _all_of(outcomes) is False


class TestSnappingRejectsNonsenseTicks:
    @given(price=PRICES, direction=DIRECTIONS)
    @settings(max_examples=50)
    def test_a_zero_or_negative_tick_is_refused(self, price: Decimal, direction: Direction) -> None:
        from algotrader.strategy.primitives.evaluators import PrimitiveError

        for bad in (Decimal("0"), Decimal("-0.05")):
            with pytest.raises(PrimitiveError):
                snap_stop(price, bad, direction)
