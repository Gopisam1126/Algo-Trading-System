"""drop decision_log symbol_id FK

Revision ID: a357ef3e5fdb
Revises: 4e29d1c6d892
Created: 2026-08-07 13:10:44.346412+00:00

Checklist before merging this migration (EPIC01_TECHNICAL_SPEC.md §12):

- [ ] ``downgrade()`` is implemented and actually tested, not left as ``pass``.
      ``alembic upgrade head`` -> ``downgrade base`` -> ``upgrade head`` must
      run clean on an empty database.  This is an E01-S01 acceptance criterion
      and it is easy to leave broken.
- [ ] If this adds a TimescaleDB policy, ``downgrade()`` drops the policy
      BEFORE dropping the hypertable, or it fails.
- [ ] Any unique index on a hypertable includes the partitioning column
      (``ts``), which TimescaleDB requires.
- [ ] Money columns are ``NUMERIC(14,4)``, never float.
- [ ] Timestamps are ``TIMESTAMP(timezone=True)``, never naive.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "a357ef3e5fdb"
down_revision: str | None = "4e29d1c6d892"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the FK. The column and its usefulness stay.

    QA-SEC-05. An audit write referencing an instrument whose row is uncommitted
    blocks on PostgreSQL's shared FK lock until that transaction commits — which
    defeats the entire reason AuditWriter uses an independent session. During the
    daily instrument sync (one transaction, thousands of rows) every concurrent
    audit write for those symbols would stall, and at statement_timeout the entry
    would fall through to the disk buffer.

    Confirmed by probe before changing anything: two sessions, one holding an
    uncommitted instrument insert, the other timing out on the audit insert.
    """
    op.drop_constraint("fk_decision_log_symbol_id_instruments", "decision_log", type_="foreignkey")


def downgrade() -> None:
    """Deliberately a no-op, with a reason.

    Two independent grounds:

    1. **TimescaleDB will not allow it.** decision_log is a hypertable with
       compression enabled, and adding a foreign key to one fails with
       "operation not supported on hypertables that have compression enabled".
       A downgrade that cannot run is worse than one that does nothing, because
       it strands the whole migration chain.
    2. **The constraint was a defect.** Recreating it would restore the
       blocking behaviour this migration exists to remove.

    The column itself is untouched by this migration in either direction, so
    nothing is lost by not restoring the constraint.
    """
