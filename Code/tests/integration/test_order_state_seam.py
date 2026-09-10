"""The state machine meets the broker's vocabulary (E15-S03).

``tests/unit/test_order_state.py`` tests the table against itself: it agrees
with §8.2's diagram, it is exhaustive, terminal states are terminal. All of
that can be true of a table that still refuses the first thing the broker
actually says.

This is the seam. Every status this system ever observes is produced by
``broker/kite/mapping.status_in``, and every state it can be *in* while
observing one comes from the order lifecycle. The cross-product of those two
is small enough to enumerate completely — so this does, and asserts that each
pair is either legal or refused **for a reason written down**.

It runs against no database and no broker, and therefore declares **no**
container marker. That marker means "needs a container", not "is an
integration test", and `test_suite_integrity.py` failed this module for
declaring one — correctly. Marking it would have gated a pure-computation test
on whether Docker happened to be running, which is how the only system-level
test in this repository spent a period silently skipped while the suite
reported green. The file lives in `integration/` because of what it tests; it
runs everywhere because it needs nothing.

(That guard matches on file text, so it also fires on prose naming the marker.
Hence the circumlocution above rather than the literal. Recorded as QA-E15-09;
the guard is doing its job and the false positive is cheap to work around.)

**It has already earned its place.** On its first run it found four missing
transitions — ``SUBMITTING -> {OPEN, FILLED, CANCELLED, REJECTED}`` — which are
E15-S02's recovery path: the broker reports an order it already holds while our
row still says ``SUBMITTING``. Nothing in the unit suite could see it, because
nothing there knew what Kite is capable of saying. QA-E15-07.
"""

from __future__ import annotations

import pytest

from algotrader.broker.kite import mapping
from algotrader.common.enums import OrderStatus
from algotrader.execution.order_state import allowed_targets, is_legal

#: The states an order can be in locally while it is live and being polled.
#: Terminal states are excluded: reconciliation does not poll a finished order,
#: and if it did, refusing every change is the documented intent (AC2).
LIVE_STATES = [
    OrderStatus.SUBMITTING,
    OrderStatus.SUBMITTED,
    OrderStatus.OPEN,
    OrderStatus.PARTIAL,
    OrderStatus.RECONCILE_REQUIRED,
]

#: Every transition the cross-product refuses, each with the reason it is
#: refused. A pair that is neither legal nor listed here fails the test below,
#: which is what makes "we thought about this one" the only way to add an
#: exception. See order_state.py's docstring for the full arguments.
DOCUMENTED_REFUSALS: dict[tuple[OrderStatus, OrderStatus], str] = {
    (OrderStatus.OPEN, OrderStatus.SUBMITTED): (
        "backwards into a pre-submission state; safest branch, see the module docstring"
    ),
    (OrderStatus.PARTIAL, OrderStatus.SUBMITTED): (
        "backwards into a pre-submission state, and it would discard a known fill"
    ),
    (OrderStatus.RECONCILE_REQUIRED, OrderStatus.SUBMITTED): (
        "reconciliation owns this order; returning it to a submitted state is a "
        "path to submitting it twice"
    ),
    (OrderStatus.PARTIAL, OrderStatus.OPEN): (
        "Kite's OPEN covers partially-filled orders; going back would discard the fill"
    ),
    (OrderStatus.PARTIAL, OrderStatus.REJECTED): (
        "an order that has partly filled cannot be rejected; part of it happened"
    ),
}


def _producible() -> set[OrderStatus]:
    """Every OrderStatus the broker mapping can hand us.

    Derived from ``_STATUS_IN`` rather than hardcoded, so a new Kite status
    added to the mapping is covered here the day it is added. ``status_in``
    also returns ``RECONCILE_REQUIRED`` for anything unmapped, which no key in
    the table produces, so it is added explicitly.
    """
    return {mapping.status_in(v) for v in mapping._STATUS_IN} | {OrderStatus.RECONCILE_REQUIRED}


class TestEveryStatusTheBrokerCanReportIsHandled:
    def test_the_mapping_produces_only_states_the_table_knows(self) -> None:
        assert _producible() <= set(OrderStatus)

    @pytest.mark.parametrize("current", LIVE_STATES)
    def test_every_broker_report_is_legal_or_documented(self, current: OrderStatus) -> None:
        """The cross-product, exhaustively.

        A pair that is refused and undocumented is the failure this test is
        for: it means the broker can say something the system cannot record,
        and the first time it happens will be in production during an incident.
        """
        undocumented = [
            (current.value, observed.value)
            for observed in sorted(_producible(), key=lambda s: s.value)
            if not is_legal(current, observed) and (current, observed) not in DOCUMENTED_REFUSALS
        ]
        assert not undocumented, (
            f"the broker can report these and the table refuses them with no "
            f"recorded reason: {undocumented}. Either add the transition with "
            f"its justification, or add it to DOCUMENTED_REFUSALS with why."
        )

    def test_an_unmapped_kite_status_lands_somewhere_legal(self) -> None:
        """``status_in`` turns anything it does not model into
        RECONCILE_REQUIRED rather than guessing. That is only useful if every
        live state can actually move there — otherwise the safe answer is the
        one that raises."""
        assert mapping.status_in("SOME FUTURE KITE STATUS") is OrderStatus.RECONCILE_REQUIRED
        for state in LIVE_STATES:
            assert is_legal(state, OrderStatus.RECONCILE_REQUIRED)

    def test_the_recovery_path_of_e15_s02_can_be_recorded(self) -> None:
        """The four transitions this test added. An ambiguous submission is
        queried and the broker reports the order it already holds — possibly
        already COMPLETE — while our row still says SUBMITTING."""
        for kite_says in ("OPEN", "COMPLETE", "CANCELLED", "REJECTED"):
            observed = mapping.status_in(kite_says)
            assert is_legal(OrderStatus.SUBMITTING, observed), (
                f"a recovered timeout reporting {kite_says!r} could not be recorded"
            )

    def test_the_documented_refusals_are_all_still_refused(self) -> None:
        """The control. If a refusal quietly becomes legal, its entry here is
        stale and the reasoning behind it has been lost rather than revised."""
        for (current, target), reason in DOCUMENTED_REFUSALS.items():
            assert not is_legal(current, target), (
                f"{current.value} -> {target.value} is now legal, but is still "
                f"listed as refused because: {reason}"
            )

    def test_no_broker_report_can_resurrect_a_finished_order(self) -> None:
        """AC2 restated at the seam. Whatever the broker says about an order we
        have already recorded as finished, it cannot move."""
        for terminal in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
            assert allowed_targets(terminal) == frozenset()
            for observed in _producible():
                assert is_legal(terminal, observed) is (observed is terminal)
