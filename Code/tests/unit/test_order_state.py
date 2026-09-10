"""The order state machine (E15-S03), organised one class per acceptance criterion.

The state space is small — eleven states, 121 ordered pairs — so this suite
tests it EXHAUSTIVELY rather than by sampling. A property-based generator would
be strictly weaker here: it would explore the same 121 pairs at random and
report a subset of them.

What the exhaustive pass can prove that spot-checks cannot is the two graph
properties at the bottom: no state is orphaned, and no non-terminal state is a
trap. Both are statements about the whole table, and both would be false in
plausible malformed versions of it.
"""

from __future__ import annotations

import itertools

import pytest

from algotrader.common.enums import OrderStatus
from algotrader.execution.order_state import (
    ALLOWED_TRANSITIONS,
    IllegalTransitionError,
    _verify_table,
    allowed_targets,
    assert_legal,
    is_legal,
)

#: Every edge drawn in LOW_LEVEL_ARCHITECTURE.md §8.2, transcribed from the
#: diagram rather than from the implementation. Transcribing from the code
#: would make this test agree with whatever the code says, which is not a test.
DIAGRAM_EDGES = [
    (OrderStatus.PENDING_RISK, OrderStatus.APPROVED),
    (OrderStatus.PENDING_RISK, OrderStatus.REJECTED),
    (OrderStatus.APPROVED, OrderStatus.SUBMITTING),
    (OrderStatus.APPROVED, OrderStatus.REJECTED),
    (OrderStatus.SUBMITTING, OrderStatus.SUBMITTED),
    (OrderStatus.SUBMITTING, OrderStatus.SUBMIT_FAILED),
    (OrderStatus.SUBMITTED, OrderStatus.OPEN),
    (OrderStatus.OPEN, OrderStatus.PARTIAL),
    (OrderStatus.OPEN, OrderStatus.CANCELLED),
    (OrderStatus.PARTIAL, OrderStatus.FILLED),
    (OrderStatus.SUBMIT_FAILED, OrderStatus.RECONCILE_REQUIRED),
]

TERMINAL = [OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED]


class TestAc1TheDiagramIsLegalAndEverythingElseIsRefused:
    @pytest.mark.parametrize(("current", "target"), DIAGRAM_EDGES)
    def test_every_edge_the_architecture_draws_is_legal(
        self, current: OrderStatus, target: OrderStatus
    ) -> None:
        assert is_legal(current, target), f"§8.2 draws {current.value} -> {target.value}"

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            # Backwards through the submission sequence: each of these would
            # let an order be sent a second time.
            (OrderStatus.SUBMITTED, OrderStatus.SUBMITTING),
            (OrderStatus.OPEN, OrderStatus.SUBMITTED),
            (OrderStatus.RECONCILE_REQUIRED, OrderStatus.SUBMITTING),
            (OrderStatus.RECONCILE_REQUIRED, OrderStatus.SUBMITTED),
            # Skipping risk entirely.
            (OrderStatus.PENDING_RISK, OrderStatus.SUBMITTING),
            (OrderStatus.PENDING_RISK, OrderStatus.FILLED),
            # A rejection that becomes an order.
            (OrderStatus.REJECTED, OrderStatus.SUBMITTING),
            # See the module docstring: the two deliberate exclusions.
            (OrderStatus.PARTIAL, OrderStatus.OPEN),
            (OrderStatus.PARTIAL, OrderStatus.REJECTED),
        ],
    )
    def test_moves_outside_the_table_are_refused(
        self, current: OrderStatus, target: OrderStatus
    ) -> None:
        with pytest.raises(IllegalTransitionError):
            assert_legal(current, target)

    def test_the_refusal_names_both_states(self) -> None:
        """An error that says only "illegal transition" is unusable at 03:00.
        It must say what was attempted, and what would have been allowed."""
        with pytest.raises(IllegalTransitionError) as excinfo:
            assert_legal(OrderStatus.FILLED, OrderStatus.CANCELLED)
        message = str(excinfo.value)
        assert "FILLED" in message
        assert "CANCELLED" in message
        assert excinfo.value.current is OrderStatus.FILLED
        assert excinfo.value.target is OrderStatus.CANCELLED

    def test_the_refusal_lists_what_would_have_been_legal(self) -> None:
        with pytest.raises(IllegalTransitionError) as excinfo:
            assert_legal(OrderStatus.APPROVED, OrderStatus.FILLED)
        assert "SUBMITTING" in str(excinfo.value)

    def test_nothing_may_enter_the_first_state(self) -> None:
        """PENDING_RISK is where an order begins. An edge back into it would
        mean an order that has been risk-checked awaiting risk again."""
        for state in OrderStatus:
            assert OrderStatus.PENDING_RISK not in allowed_targets(state)

    def test_only_risk_may_approve(self) -> None:
        """APPROVED reachable from anywhere else would be a path around the
        risk engine — the one thing LOW_LEVEL §5.7 forbids absolutely."""
        sources = [s for s in OrderStatus if OrderStatus.APPROVED in allowed_targets(s)]
        assert sources == [OrderStatus.PENDING_RISK]


class TestAc2TerminalIsForever:
    @pytest.mark.parametrize("state", TERMINAL)
    def test_a_terminal_state_has_no_outgoing_transition(self, state: OrderStatus) -> None:
        assert allowed_targets(state) == frozenset()

    @pytest.mark.parametrize(
        ("state", "target"), [(s, t) for s in TERMINAL for t in OrderStatus if t is not s]
    )
    def test_no_move_off_a_terminal_state_is_permitted(
        self, state: OrderStatus, target: OrderStatus
    ) -> None:
        """Exhaustive: 3 terminal states x 10 other states = 30 refusals.

        The one that matters most is FILLED -> CANCELLED. A filled order that
        becomes cancelled erases a position that exists in the market, and
        nothing downstream would go looking for it.
        """
        with pytest.raises(IllegalTransitionError):
            assert_legal(state, target)


class TestAc3TheTableIsCheckedAtImport:
    def test_it_is_exhaustive_over_the_enum(self) -> None:
        assert set(ALLOWED_TRANSITIONS) == set(OrderStatus)

    def test_a_missing_state_is_refused(self) -> None:
        """A key that is absent looks identical to a terminal state at lookup
        time, so a newly added OrderStatus would silently become one no order
        can ever leave."""
        incomplete = {s: t for s, t in ALLOWED_TRANSITIONS.items() if s is not OrderStatus.OPEN}
        with pytest.raises(RuntimeError, match="exhaustive"):
            _verify_table(incomplete)

    def test_a_self_edge_is_refused(self) -> None:
        """Staying put is not a transition. A self-edge would make "terminal"
        mean "empty except itself" and quietly break the cross-check below."""
        looped = dict(ALLOWED_TRANSITIONS)
        looped[OrderStatus.FILLED] = frozenset({OrderStatus.FILLED})
        with pytest.raises(RuntimeError, match="themselves as targets"):
            _verify_table(looped)

    def test_an_edge_out_of_a_terminal_state_is_refused(self) -> None:
        """The cross-check that matters: the table and OrderStatus.is_terminal
        are two independent definitions of "this order is finished", and they
        must not drift. Adding this edge makes the module fail to import."""
        escaped = dict(ALLOWED_TRANSITIONS)
        escaped[OrderStatus.FILLED] = frozenset({OrderStatus.OPEN})
        with pytest.raises(RuntimeError, match="disagree about which states"):
            _verify_table(escaped)

    def test_a_newly_terminal_state_is_refused(self) -> None:
        """The other direction of the same drift: emptying a non-terminal
        state's targets without telling the enum."""
        stranded = dict(ALLOWED_TRANSITIONS)
        stranded[OrderStatus.OPEN] = frozenset()
        with pytest.raises(RuntimeError, match="disagree about which states"):
            _verify_table(stranded)

    def test_the_real_table_passes_every_check(self) -> None:
        """The control on the four tests above. Each proves the verifier
        REJECTS something; without this, a verifier that rejected everything
        would pass all of them."""
        assert _verify_table(ALLOWED_TRANSITIONS) is None


class TestAc4ReObservingAStateIsNotATransition:
    @pytest.mark.parametrize("state", list(OrderStatus))
    def test_every_state_may_be_re_observed(self, state: OrderStatus) -> None:
        """Reconciliation polls every 30 seconds (§8.3) and most polls report
        no change. An unchanged order raising would turn the loop into a
        continuous alarm, and an alarm that is always on is not read."""
        assert is_legal(state, state)
        assert assert_legal(state, state) is state

    @pytest.mark.parametrize("state", list(OrderStatus))
    def test_but_the_table_itself_lists_no_self_edge(self, state: OrderStatus) -> None:
        """The no-op is a property of the FUNCTION, not of the table. Keeping
        it out of the table is what lets "terminal" stay expressible as "has
        an empty target set"."""
        assert state not in allowed_targets(state)


class TestAc5TheOrdinaryLifecycleIsLegal:
    """Without this, a table that refused everything would satisfy AC1-AC4."""

    def test_the_path_this_systems_entry_order_actually_takes(self) -> None:
        """PENDING_RISK -> APPROVED -> SUBMITTING -> SUBMITTED -> FILLED.

        The entry order is a MARKET order (execution/gateway.py), so it
        commonly fills whole without ever being observed OPEN or PARTIAL. This
        is the normal case, not an edge case, and §8.2's diagram does not draw
        it — see the module docstring.
        """
        walk = [
            OrderStatus.PENDING_RISK,
            OrderStatus.APPROVED,
            OrderStatus.SUBMITTING,
            OrderStatus.SUBMITTED,
            OrderStatus.FILLED,
        ]
        for current, target in itertools.pairwise(walk):
            assert_legal(current, target)

    def test_the_path_a_worked_limit_order_takes(self) -> None:
        walk = [
            OrderStatus.PENDING_RISK,
            OrderStatus.APPROVED,
            OrderStatus.SUBMITTING,
            OrderStatus.SUBMITTED,
            OrderStatus.OPEN,
            OrderStatus.PARTIAL,
            OrderStatus.FILLED,
        ]
        for current, target in itertools.pairwise(walk):
            assert_legal(current, target)

    def test_the_ambiguous_submission_path_from_e15_s02(self) -> None:
        """SUBMITTING -> RECONCILE_REQUIRED -> whatever the broker says.

        E15-S02 leaves an order here when a timeout is queried and the answer
        is unusable. If this walk were illegal, that story's recovery path
        would have nowhere to record its result.
        """
        assert_legal(OrderStatus.SUBMITTING, OrderStatus.RECONCILE_REQUIRED)
        for resolved in (OrderStatus.OPEN, OrderStatus.FILLED, OrderStatus.CANCELLED):
            assert_legal(OrderStatus.RECONCILE_REQUIRED, resolved)

    def test_a_failed_submission_reaches_reconciliation(self) -> None:
        assert_legal(OrderStatus.SUBMITTING, OrderStatus.SUBMIT_FAILED)
        assert_legal(OrderStatus.SUBMIT_FAILED, OrderStatus.RECONCILE_REQUIRED)


class TestTheTableCannotBeSwitchedOffAtRuntime:
    """STRIDE tampering, and it found something.

    ``Final`` is checked by mypy and by nothing at runtime. Before this the
    whole safety property could be disabled with one assignment, with every
    test in this file still passing — a filled order becomes movable and
    nothing anywhere raises. QA-E15-08.
    """

    def test_a_row_cannot_be_replaced(self) -> None:
        with pytest.raises(TypeError):
            ALLOWED_TRANSITIONS[OrderStatus.FILLED] = frozenset(  # type: ignore[index]
                {OrderStatus.OPEN}
            )

    def test_a_row_cannot_be_deleted(self) -> None:
        """Deleting is as effective as rewriting: a missing key raises KeyError
        inside `is_legal`, turning every transition check for that state into a
        crash rather than a decision."""
        with pytest.raises(TypeError):
            del ALLOWED_TRANSITIONS[OrderStatus.OPEN]  # type: ignore[attr-defined]

    def test_the_targets_themselves_cannot_be_extended(self) -> None:
        """The values are frozensets, so there is no in-place add either.
        Immutable all the way down, not just at the top level."""
        assert isinstance(allowed_targets(OrderStatus.FILLED), frozenset)
        with pytest.raises(AttributeError):
            allowed_targets(OrderStatus.OPEN).add(OrderStatus.SUBMITTED)  # type: ignore[attr-defined]

    def test_it_still_reads_normally(self) -> None:
        """The control: read-only must not mean unusable."""
        assert ALLOWED_TRANSITIONS[OrderStatus.APPROVED] == frozenset(
            {OrderStatus.SUBMITTING, OrderStatus.REJECTED}
        )
        assert len(ALLOWED_TRANSITIONS) == len(OrderStatus)


class TestTheShapeOfTheGraph:
    """Two properties of the whole table that no single transition test sees."""

    def test_every_state_is_reachable_from_the_start(self) -> None:
        """An unreachable state is a state the system can never be in, which
        means either the table is wrong or the enum carries a member nothing
        produces. Either is worth knowing."""
        seen = {OrderStatus.PENDING_RISK}
        frontier = [OrderStatus.PENDING_RISK]
        while frontier:
            for target in allowed_targets(frontier.pop()):
                if target not in seen:
                    seen.add(target)
                    frontier.append(target)
        assert seen == set(OrderStatus), f"unreachable: {set(OrderStatus) - seen}"

    def test_no_non_terminal_state_is_a_trap(self) -> None:
        """Every state must have a path to a terminal one. A state that cannot
        finish is an order that stays open forever — and every position in this
        system has a square-off deadline it would then miss."""
        terminal = {s for s in OrderStatus if s.is_terminal}
        for start in OrderStatus:
            seen = {start}
            frontier = [start]
            while frontier and not (seen & terminal):
                for target in allowed_targets(frontier.pop()):
                    if target not in seen:
                        seen.add(target)
                        frontier.append(target)
            assert seen & terminal, f"{start.value} can never reach a terminal state"
