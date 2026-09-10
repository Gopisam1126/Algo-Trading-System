"""The order state machine — which moves are legal, and which are not (E15-S03).

``LOW_LEVEL_ARCHITECTURE.md §8.2`` draws the order lifecycle. Until this module
existed the drawing was the only thing enforcing it: :class:`OrderStatus` listed
eleven states and nothing anywhere said which moves between them were allowed.
The gateway wrote ``"SUBMITTING"`` and then ``"SUBMITTED"`` as raw strings, and
a bug that wrote ``"FILLED"`` over a working order — or moved a filled order
back to ``OPEN`` — would have been recorded without complaint.

## The design record

**The decision.** One frozen table of legal transitions, checked at import for
exhaustiveness and consistency, with a single function that refuses anything
not in it. The table is the specification; §8.2's diagram is its picture.

**The alternative rejected.** Letting each caller check its own transitions —
the gateway validating its two, reconciliation validating its several. That is
how the table drifts: two callers disagree about whether ``SUBMITTED -> FILLED``
is allowed and nothing notices, because neither is wrong in its own file. It
also leaves the property untestable as a property: "no order leaves a terminal
state" is a statement about the whole table, not about any one call site.

**The failure it prevents.** A filled order moving to ``CANCELLED`` erases a
position that exists in the market. A cancelled order moving to ``OPEN``
invents one that does not. Both are silently plausible in a database column
typed ``String(24)``, and both would be discovered by a reconciliation loop
concluding that the broker and this system disagree — long after the order
that caused it.

**What would make this decision wrong.** If order status ever needs to carry
per-broker variation, a single global table stops fitting and this becomes a
per-adapter policy. Nothing suggests that today: Kite's vocabulary is smaller
than ours and maps *into* these states (``broker/kite/mapping.py``).

## Where this table differs from the drawing, and why

§8.2's diagram shows the happy path and the three ways out of it. Reality has
more edges, and each addition below is a transition the broker can actually
produce. They are listed here rather than only in the table because an
undocumented edge in a safety table is indistinguishable from a mistake.

* ``SUBMITTED -> FILLED`` and ``OPEN -> FILLED``. The diagram routes every fill
  through ``PARTIAL``. A MARKET order — which is precisely what this system
  sends for an entry (``execution/gateway.py``) — commonly fills whole, and
  reconciliation polls every 30 seconds (§8.3), so most fills are *observed*
  as complete without a partial ever being seen. Requiring ``PARTIAL`` would
  make the normal case illegal.
* ``SUBMITTED -> REJECTED``. Kite accepts an order into
  ``PUT ORDER REQ RECEIVED`` and the exchange can reject it afterwards;
  ``_STATUS_IN`` maps ``"REJECTED"`` unconditionally.
* ``SUBMITTED -> CANCELLED``. Cancellable before it is ever observed working.
* ``PARTIAL -> CANCELLED``. A partly filled order cancelled at square-off. The
  filled quantity is a real position and must remain representable.
* ``SUBMITTING -> {OPEN, FILLED, CANCELLED, REJECTED, RECONCILE_REQUIRED}``.
  E15-S02's recovery path, and the one the integration seam test found missing
  (QA-E15-07). After an ambiguous submission the broker is queried and reports
  the order it already holds — possibly already filled — while our row still
  says ``SUBMITTING`` because the outcome was never recorded. Without these
  edges the result of a recovered timeout could not be written down, which is
  precisely the case that story exists to handle. ``RECONCILE_REQUIRED``
  covers the sub-case where the answer is unusable.
* ``* -> RECONCILE_REQUIRED`` from every non-terminal state. ``status_in``
  returns it for any Kite status we do not model, which can arrive on any poll.

**And one exclusion that matters more than the additions.** ``PARTIAL -> OPEN``
is **illegal**, and this is the edge most likely to be reported as a bug.
Kite's ``OPEN`` covers working *and* partially-filled orders — its own mapping
says so — so a reconciler that maps broker status naively **will** try to move
a ``PARTIAL`` order back to ``OPEN`` on the next poll. Refusing is deliberate:
going backwards discards the knowledge that a fill occurred, and the refusal is
what surfaces that the caller must consult ``filled_quantity`` rather than
status alone. E15-S09 inherits that constraint; it is recorded on that story.

``PARTIAL -> REJECTED`` is likewise illegal: an order that has partly filled
cannot be rejected, because part of it already happened.

**Backwards moves into ``SUBMITTED`` are refused from every later state.**
Kite's ``OPEN PENDING``, ``VALIDATION PENDING`` and ``PUT ORDER REQ RECEIVED``
all map to ``SUBMITTED``, so a cross-product of "our state" against "what the
broker can say" contains ``OPEN -> SUBMITTED``. Whether Kite ever really
reports a pre-open status *after* an order has worked could not be established
without a live account, so the safest branch was taken: refuse. Backwards
toward a pre-submission state is the direction that could re-enable
submission. A reconciler meeting this must record ``RECONCILE_REQUIRED``,
which is legal from every non-terminal state, rather than retrying the
transition — recorded on E15-S09.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from algotrader.common.enums import OrderStatus

__all__ = [
    "ALLOWED_TRANSITIONS",
    "IllegalTransitionError",
    "allowed_targets",
    "assert_legal",
    "is_legal",
]


class IllegalTransitionError(ValueError):
    """An order was asked to move somewhere §8.2 does not allow.

    Carries both states, because the first question in an incident is always
    "from what, to what" and an error that omits either forces a reader to
    reconstruct it from surrounding logs.
    """

    def __init__(self, current: OrderStatus, target: OrderStatus) -> None:
        self.current = current
        self.target = target
        legal = sorted(s.value for s in ALLOWED_TRANSITIONS[current])
        super().__init__(
            f"illegal order transition {current.value} -> {target.value}. "
            f"Legal from {current.value}: {legal or ['(terminal - none)']}"
        )


#: The state machine of §8.2. A state maps to every OTHER state it may move to;
#: staying put is not a transition and is handled by :func:`assert_legal`, which
#: keeps "terminal" expressible as "has an empty target set".
_TABLE: Final[dict[OrderStatus, frozenset[OrderStatus]]] = {
    OrderStatus.PENDING_RISK: frozenset({OrderStatus.APPROVED, OrderStatus.REJECTED}),
    OrderStatus.APPROVED: frozenset({OrderStatus.SUBMITTING, OrderStatus.REJECTED}),
    # The four broker-reported outcomes below were ADDED after the integration
    # seam test (QA-E15-07). They are E15-S02's recovery path: an ambiguous
    # submission is queried, and the broker reports the order it already holds
    # while our row still says SUBMITTING. Refusing them would make the outcome
    # of a recovered timeout unrecordable — the one case that story exists for.
    OrderStatus.SUBMITTING: frozenset(
        {
            OrderStatus.SUBMITTED,
            OrderStatus.SUBMIT_FAILED,
            OrderStatus.OPEN,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.RECONCILE_REQUIRED,
        }
    ),
    OrderStatus.SUBMITTED: frozenset(
        {
            OrderStatus.OPEN,
            OrderStatus.PARTIAL,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.RECONCILE_REQUIRED,
        }
    ),
    OrderStatus.OPEN: frozenset(
        {
            OrderStatus.PARTIAL,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.RECONCILE_REQUIRED,
        }
    ),
    # No OPEN and no REJECTED — see the module docstring. Both are refusals
    # that carry information rather than omissions.
    OrderStatus.PARTIAL: frozenset(
        {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.RECONCILE_REQUIRED,
        }
    ),
    OrderStatus.SUBMIT_FAILED: frozenset({OrderStatus.RECONCILE_REQUIRED}),
    # §8.3: "broker state wins — it is the legal record." Reconciliation
    # resolves this to whatever the broker reports. Deliberately NOT back to
    # SUBMITTING or SUBMITTED: those are our-side states, and a path back to
    # them is a path to submitting the order a second time.
    OrderStatus.RECONCILE_REQUIRED: frozenset(
        {
            OrderStatus.OPEN,
            OrderStatus.PARTIAL,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
        }
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
}

#: A read-only VIEW, not the dict. ``Final`` is checked by mypy and by nothing
#: at runtime, so before this the entire safety property could be switched off
#: by one assignment — ``ALLOWED_TRANSITIONS[FILLED] = frozenset({OPEN})`` made
#: a filled order movable again, silently, with every test still green.
#: A ``MappingProxyType`` raises instead. The values are already ``frozenset``,
#: so the whole structure is now immutable rather than merely annotated as
#: such. Found by the STRIDE tampering question, not by reading. QA-E15-08.
ALLOWED_TRANSITIONS: Final[Mapping[OrderStatus, frozenset[OrderStatus]]] = MappingProxyType(_TABLE)


def _verify_table(table: Mapping[OrderStatus, frozenset[OrderStatus]]) -> None:
    """Import-time guards. A table that is wrong must not load.

    Takes the table as an ARGUMENT rather than reading the module global, so a
    test can feed it a deliberately broken one. Left reading the global, these
    checks would only ever run against a correct table, and a mutation that
    deleted any of them would survive the whole suite while looking covered.
    That is not hypothetical: exactly that mutation survived on ``config``'s
    order-rate guard and was only killed by this same refactor.
    """
    missing = sorted(s.value for s in OrderStatus if s not in table)
    if missing:
        raise RuntimeError(
            f"ALLOWED_TRANSITIONS is not exhaustive over OrderStatus: {missing} "
            f"have no entry. A missing key is indistinguishable from a terminal "
            f"state at lookup time, so a new status would silently become one "
            f"that no order can ever leave."
        )

    unknown = sorted(t.value for targets in table.values() for t in targets if t not in OrderStatus)
    if unknown:  # pragma: no cover - unreachable while the table is typed
        raise RuntimeError(f"ALLOWED_TRANSITIONS names non-members: {unknown}")

    self_edges = sorted(s.value for s, targets in table.items() if s in targets)
    if self_edges:
        raise RuntimeError(
            f"{self_edges} list themselves as targets. Staying put is not a "
            f"transition; a self-edge would make 'terminal' mean 'empty except "
            f"itself' and break the cross-check below."
        )

    dead_ends = {s for s, targets in table.items() if not targets}
    terminal = {s for s in OrderStatus if s.is_terminal}
    if dead_ends != terminal:
        raise RuntimeError(
            f"the table and OrderStatus.is_terminal disagree about which states "
            f"are terminal: table says {sorted(s.value for s in dead_ends)}, the "
            f"enum says {sorted(s.value for s in terminal)}. Two definitions of "
            f"'this order is finished' that drift apart is how a filled order "
            f"acquires a new state."
        )


_verify_table(ALLOWED_TRANSITIONS)


def allowed_targets(current: OrderStatus) -> frozenset[OrderStatus]:
    """Every state ``current`` may move to. Empty means terminal."""
    return ALLOWED_TRANSITIONS[current]


def is_legal(current: OrderStatus, target: OrderStatus) -> bool:
    """Whether this move is allowed. Re-observing the same state counts.

    Reconciliation polls every 30 seconds and most polls report no change; an
    unchanged order is not a transition and must not read as an illegal one.
    """
    return current is target or target in ALLOWED_TRANSITIONS[current]


def assert_legal(current: OrderStatus, target: OrderStatus) -> OrderStatus:
    """Return ``target`` if the move is legal, else raise.

    Returns rather than yielding ``None`` so it can wrap an assignment at the
    point of use — ``order.status = assert_legal(order.status, new)`` — which
    makes the validated form the shorter one to write.
    """
    if not is_legal(current, target):
        raise IllegalTransitionError(current, target)
    return target
