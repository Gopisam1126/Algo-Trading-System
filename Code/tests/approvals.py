"""The one path to a live strategy in the database, for tests that need one.

Since audit RSK-007 (17 Nov 2026) a strategy reaches ACTIVE or DEGRADED only
with a ``strategy_approval`` row written by ``algotrader_approver`` for its
exact document while it was AWAITING_APPROVAL. Setting ``approved_by`` by hand
no longer counts - the trigger refuses it - so a test that wants a live
strategy walks the real path, here, rather than each one inventing its own.
"""

from __future__ import annotations

import psycopg

from algotrader.common.roles import APPROVER_ROLE

#: Run before DELETE FROM strategy: an approval references its strategy.
WIPE_APPROVALS = "DELETE FROM strategy_approval"


def approve(conn: psycopg.Connection, strategy_id: str, *, approved_by: str = "operator") -> None:
    """Approve ``strategy_id``'s current document as the approver role.

    Moves it to AWAITING_APPROVAL first (approval is refused in any other
    state). ``conn`` is an OWNER connection; the role is set and reset here.
    The caller then moves the strategy to ACTIVE or DEGRADED.
    """
    conn.execute("UPDATE strategy SET state = 'AWAITING_APPROVAL' WHERE id = %s", (strategy_id,))
    conn.execute(f"SET ROLE {APPROVER_ROLE}")
    try:
        conn.execute(
            "INSERT INTO strategy_approval (strategy_id, dsl_hash, approved_by) "
            "SELECT id, dsl_hash, %s FROM strategy WHERE id = %s",
            (approved_by, strategy_id),
        )
    finally:
        conn.execute("RESET ROLE")


def make_live(conn: psycopg.Connection, strategy_id: str, state: str = "ACTIVE") -> None:
    """Approve, then move to ``state`` (ACTIVE or DEGRADED)."""
    approve(conn, strategy_id)
    conn.execute("UPDATE strategy SET state = %s WHERE id = %s", (state, strategy_id))
