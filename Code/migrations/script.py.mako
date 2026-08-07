"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Created: ${create_date}

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

import sqlalchemy as sa

from alembic import op
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
