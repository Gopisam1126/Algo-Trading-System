"""held positions hold their slot: the slot indexes cover every non-CLOSED row

Revision ID: 3d12560eeba9
Revises: a986f25d3838
Created: 2026-11-16 12:00:00+00:00

Audit RSK-019 (10 Nov 2026). ``uq_open_slot`` and ``uq_open_symbol`` - the
slot-discipline guarantee under the Redis lock - were partial indexes on
``status = 'OPEN'``, while the schema allows ``CLOSING``: a position whose exit
is in flight, which still carries live exposure until that exit fills. A row
written CLOSING would have released its slot and its symbol to a second entry
while the first was still on.

No code writes CLOSING today (a closing position is ``ExitingPosition`` in
memory and stays OPEN in the row), so nothing is wrong YET. The guarantee is
moved to what it means - a position that is not closed holds its slot - so a
future writer of CLOSING cannot reintroduce the gap. ``occupied_slots`` and
``open_positions`` read the same predicate.

Checklist before merging this migration (EPIC01_TECHNICAL_SPEC.md §12):

- [x] ``downgrade()`` restores the two original indexes exactly.
- [x] No TimescaleDB policy is added; ``positions`` is not a hypertable.
- [x] No money or timestamp columns are added.
- [x] Existing rows: the new predicate admits a superset of the old one's rows.
      With no CLOSING rows ever written, the sets are equal and the rebuild
      cannot fail on a deployment that only ran the application.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3d12560eeba9"
down_revision: str | None = "a986f25d3838"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HELD = "status <> 'CLOSED'"
_OPEN = "status = 'OPEN'"


def _rebuild(predicate: str) -> None:
    op.drop_index("uq_open_slot", table_name="positions")
    op.drop_index("uq_open_symbol", table_name="positions")
    op.create_index(
        "uq_open_slot",
        "positions",
        ["slot_index"],
        unique=True,
        postgresql_where=sa.text(predicate),
    )
    op.create_index(
        "uq_open_symbol",
        "positions",
        ["symbol_id"],
        unique=True,
        postgresql_where=sa.text(predicate),
    )


def upgrade() -> None:
    _rebuild(_HELD)


def downgrade() -> None:
    _rebuild(_OPEN)
