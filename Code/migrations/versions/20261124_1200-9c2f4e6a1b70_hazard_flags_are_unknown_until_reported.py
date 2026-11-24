"""hazard flags are unknown until their source reports

Revision ID: 9c2f4e6a1b70
Revises: 7f3e9b2c4d10
Created: 2026-11-24 12:00:00+00:00

E04-S01, 24 Nov 2026.

``instrument_daily_status`` carried its six flags as ``NOT NULL DEFAULT false``,
and ``DailyStatusRepository.upsert`` rewrote every column on conflict. The
hazard lists arrive from separate sources (E04-S01 surveillance, S02 T2T, S03
the F&O ban, S05 CAS scope, S06 earnings), so the first source to write a
day's row - the surveillance roster, say - stored ``is_t2t = false``,
``is_fno_ban = false`` and ``is_cas_stock = false`` for flags nobody had
checked: a T2T stock read as tradable, a CAS stock got the later deadline.
And each later source, writing its own flag, rewrote the others back to the
default, so the T2T write would have cleared the surveillance flags it
followed. Latent until today: no producer existed.

After this migration a flag is NULL until its source has reported for that
day, and every reader already refuses NULL: ``restrictions_from`` blocks on
anything but ``False``, the CAS deadline takes the earlier time for ``None``,
the execution service refuses an unknown CAS flag, and ``v_eligible_today``'s
``NOT s.is_t2t`` is NULL - not true - for an unknown flag, so the row drops
out. The repository now writes only the columns a source supplies.

Checklist before merging this migration (EPIC01_TECHNICAL_SPEC.md §12):

- [x] ``downgrade()`` is implemented: NOT NULL and the old default come back,
      and a NULL left by this schema becomes ``true`` first - the restricted
      reading, never the clean one (for ``is_cas_stock``, the earlier
      deadline).
- [x] No TimescaleDB policy is added; the table is not a hypertable.
- [x] No money or timestamp columns change.
- [x] Existing rows keep their values: only the default and the NOT NULL go.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9c2f4e6a1b70"
down_revision: str | None = "7f3e9b2c4d10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Frozen here: a migration is a snapshot (``models.InstrumentDailyStatus``
#: is held to it by tests/integration/test_schema_matches_models.py).
_FLAGS = ("is_t2t", "is_asm", "is_gsm", "is_fno_ban", "is_cas_stock", "has_earnings_today")


def upgrade() -> None:
    for column in _FLAGS:
        op.alter_column(
            "instrument_daily_status",
            column,
            existing_type=sa.Boolean(),
            existing_server_default=sa.text("false"),
            nullable=True,
            server_default=None,
        )


def downgrade() -> None:
    for column in _FLAGS:
        # Unknown becomes restricted: the old schema cannot say "unknown", and
        # "false" there means "checked and clean".
        table = sa.table("instrument_daily_status", sa.column(column, sa.Boolean()))
        op.execute(table.update().where(table.c[column].is_(None)).values({column: sa.true()}))
        op.alter_column(
            "instrument_daily_status",
            column,
            existing_type=sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        )
