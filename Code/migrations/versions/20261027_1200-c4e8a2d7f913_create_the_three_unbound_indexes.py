"""create the three indexes the models declared and nothing built

Revision ID: c4e8a2d7f913
Revises: b7c2f9a41d38
Created: 2026-10-27 12:00:00+00:00

``ix_audit_reason``, ``ix_trial_ts`` and ``ix_journal_date`` were declared in
``common/db/models.py`` from ``text()`` expressions alone. An Index built that
way attaches to no table, so it was never in the metadata: ``create_all`` did
not build it, Alembic's comparator could not see it, and no migration created
it. Found on 27 Oct 2026 by the schema-drift check, which compares the migrated
database with the metadata and so was blind to exactly these three.

``ix_audit_reason`` also filtered on ``outcome = 'REJECT'``, while the risk
engine writes ``'rejected'`` - it would have indexed nothing. It is created
here with the value actually written.

Checklist before merging this migration (EPIC01_TECHNICAL_SPEC.md §12):

- [x] ``downgrade()`` is implemented: it drops exactly what ``upgrade()`` made.
- [x] No TimescaleDB policy is added.
- [x] No unique index on a hypertable (all three are non-unique).
- [x] No money or timestamp columns are added.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4e8a2d7f913"
down_revision: str | None = "b7c2f9a41d38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_audit_reason",
        "decision_log",
        ["reason_code", sa.text("ts DESC")],
        unique=False,
        postgresql_where=sa.text("outcome = 'rejected'"),
    )
    op.create_index("ix_trial_ts", "strategy_trial", [sa.text("trial_ts DESC")], unique=False)
    op.create_index("ix_journal_date", "trade_journal", [sa.text("trade_date DESC")], unique=False)


def downgrade() -> None:
    op.drop_index("ix_journal_date", table_name="trade_journal")
    op.drop_index("ix_trial_ts", table_name="strategy_trial")
    op.drop_index(
        "ix_audit_reason",
        table_name="decision_log",
        postgresql_where=sa.text("outcome = 'rejected'"),
    )
