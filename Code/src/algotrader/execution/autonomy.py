"""The autonomy ladder, enforced on every entry (audit RSK-001).

``MVP_UI_AND_LEGAL.md §3.1`` defines five levels. ``system.yaml`` shipped
``autonomy.level: L1`` ("alert - you trade manually") and
``notifications.require_approval_before_entry: true``, both validated, and the
execution service read neither: it traded on ``system.mode`` alone. An owner
who set ``mode: live`` believing L1 kept a human in the loop would have had
none. This module is the reader.

## The design record

**The decision.** One pure function answers, before the risk engine is asked,
whether this configuration may send an entry at all, and if not, why:

=========  ===================================================================
Level      Entries
=========  ===================================================================
L0, L1     Never. The system observes or alerts; a human places the order.
L2         Only with a per-trade human approval. No approval channel exists
           (E16-S03), so none can be given - refused, as an approval that
           times out is ("on timeout, skip the trade", §3.2).
L3         Only inside the envelope, escalating outside it. The envelope
           evaluation does not exist (E16-S02), so nothing can be shown to be
           inside it - refused, as an escalation that times out is.
L4         Yes. ``§3.1`` permits L4 on live capital "only after months of L3
           evidence", and L3 cannot run, so a LIVE configuration at L4 is
           refused when it LOADS (``common/config.py``,
           ``_live_mode_requires_compliance``). L4 therefore trades paper only.
=========  ===================================================================

``require_approval_before_entry`` adds the L2 rule at every level.

The function takes no mode. Paper and live differ in exactly one place - the
client handed to the adapter (``tests/security/test_paper_isolation.py``) - so
the one mode-dependent rule here, "not L4 on live capital", is a property of a
configuration and is refused where configurations are validated.

**The alternative rejected.** Refusing to START in live mode. Simpler, and it
would also stop the exit path: a live process holding a position (restored at
start, or opened before a config change) must keep reconciling it and time
exiting it. Halting *entries* while every exit path keeps running is the
system's general rule (``halt.py``), and this is the same rule.

**The failure it prevents.** An unsupervised live order under a configuration
that says a human approves every one.

**What would make this wrong.** E16-S02 and E16-S03 landing. Each replaces a
refusal here with the real control; the refusal is what a level whose control
is missing must do in the meantime.
"""

from __future__ import annotations

from typing import Final

from algotrader.common.enums import AutonomyLevel

__all__ = ["NO_ORDERS", "entry_refusal"]

#: The levels that never place an order, in any mode.
NO_ORDERS: Final = frozenset({AutonomyLevel.L0_OBSERVE, AutonomyLevel.L1_ALERT})


def entry_refusal(*, level: AutonomyLevel, approval_required: bool) -> str | None:
    """Why no entry may be sent under this configuration, or ``None``.

    Pure, and a function of configuration only, so the answer is the same for
    every signal of a session - the service asks it per signal anyway, so the
    refusal is audited against each one rather than logged once at start.
    """
    if level in NO_ORDERS:
        name = "observe" if level is AutonomyLevel.L0_OBSERVE else "alert"
        return (
            f"autonomy {level.value} ({name}) places no orders: the system reports, a human trades"
        )
    if level is AutonomyLevel.L2_APPROVE or approval_required:
        return (
            "every entry needs a per-trade human approval and no approval channel "
            "exists yet (E16-S03); skipped, as an approval that times out is"
        )
    if level is AutonomyLevel.L3_SUPERVISED:
        return (
            "autonomy L3 trades only inside the envelope, and the envelope evaluation "
            "does not exist yet (E16-S02); skipped, as an escalation that times out is"
        )
    return None
