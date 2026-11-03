"""strategy registry integrity: an immutable document, one row per behaviour

Revision ID: a986f25d3838
Revises: c4e8a2d7f913
Created: 2026-11-03 12:00:00+00:00

E12-S01 gives the ``strategy`` table its first writer. What a row may become
is decided here rather than in the writer, because the writer is not the only
thing with the application's credentials.

1. **The document cannot change.** The application role loses table-wide
   UPDATE and keeps UPDATE on the five lifecycle columns only (``state``,
   ``state_changed_at``, ``approved_by``, ``approved_at``,
   ``retirement_reason`` - ``models.LIFECYCLE_COLUMNS``). A strategy's
   validation, shadow sessions and human approval are evidence about ONE
   document; an UPDATE to ``dsl`` would keep all three for a strategy nobody
   validated. A new version is a new row with ``parent_id``.
2. **No DELETE.** A rejected strategy is archived and counted, never deleted
   (STRATEGY_ENGINE.md §5); deleting a retired one erases the provenance P8
   says is permanent.
3. **One row per behaviour** - ``uq_strategy_dsl_hash``. The registry checks
   first and says which strategy is the twin; this is the guarantee under a
   race, and against a writer that skips the check.
4. **BR-5 covers every state that places live orders.** The CHECK said
   ``state <> 'ACTIVE'``, and ``StrategyState.places_live_orders`` is ACTIVE
   *and* DEGRADED: ``UPDATE strategy SET state = 'DEGRADED'`` on a DRAFT put
   it on live capital with no approval and passed. Renamed for what it now
   covers.
5. **The state is one of the ten** - a misspelt state was a row no code path
   recognises, which every reader would have had to remember to refuse.
6. **Lineage is a chain from a v1**: version >= 1, a root is version 1, and a
   strategy is not its own parent (the foreign key accepts ``parent_id = id``).
   ``version = parent.version + 1`` needs the parent row and lives in the
   registry.

Checklist before merging this migration (EPIC01_TECHNICAL_SPEC.md §12):

- [x] ``downgrade()`` is implemented: it restores the grants and the original
      CHECK, and drops exactly what ``upgrade()`` added.
- [x] No TimescaleDB policy is added; ``strategy`` is not a hypertable.
- [x] No money or timestamp columns are added.
- [x] Existing rows: nothing has written ``strategy`` before this migration
      (E12-S01 adds the first repository), so no row can violate the new
      constraints on a deployment that only ran the application.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "a986f25d3838"
down_revision: str | None = "c4e8a2d7f913"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Frozen here rather than imported: a migration is a snapshot, and one that
#: imported StrategyState would silently change meaning when the enum did.
#: ``tests/integration/test_strategy_registry_db.py`` holds the two together.
_STATES = (
    "DRAFT",
    "VALIDATING",
    "REJECTED",
    "SHADOW",
    "PAPER",
    "AWAITING_APPROVAL",
    "ACTIVE",
    "DEGRADED",
    "RETIRED",
    "QUARANTINED",
)
_LIFECYCLE = "state, state_changed_at, approved_by, approved_at, retirement_reason"


def upgrade() -> None:
    op.execute("REVOKE UPDATE, DELETE ON strategy FROM algotrader_app")
    op.execute(f"GRANT UPDATE ({_LIFECYCLE}) ON strategy TO algotrader_app")

    op.drop_constraint(op.f("ck_strategy_active_needs_approval"), "strategy", type_="check")
    op.create_check_constraint(
        op.f("ck_strategy_live_states_need_approval"),
        "strategy",
        "state NOT IN ('ACTIVE', 'DEGRADED') "
        "OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_strategy_known_state"),
        "strategy",
        "state IN (" + ", ".join(f"'{s}'" for s in _STATES) + ")",
    )
    op.create_unique_constraint("uq_strategy_dsl_hash", "strategy", ["dsl_hash"])
    op.create_check_constraint(
        op.f("ck_strategy_dsl_hash_is_sha256"), "strategy", "dsl_hash ~ '^[0-9a-f]{64}$'"
    )
    op.create_check_constraint(op.f("ck_strategy_version_positive"), "strategy", "version >= 1")
    op.create_check_constraint(
        op.f("ck_strategy_root_is_version_one"), "strategy", "parent_id IS NOT NULL OR version = 1"
    )
    op.create_check_constraint(
        op.f("ck_strategy_not_its_own_parent"), "strategy", "parent_id IS NULL OR parent_id <> id"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_strategy_not_its_own_parent"), "strategy", type_="check")
    op.drop_constraint(op.f("ck_strategy_root_is_version_one"), "strategy", type_="check")
    op.drop_constraint(op.f("ck_strategy_version_positive"), "strategy", type_="check")
    op.drop_constraint(op.f("ck_strategy_dsl_hash_is_sha256"), "strategy", type_="check")
    op.drop_constraint("uq_strategy_dsl_hash", "strategy", type_="unique")
    op.drop_constraint(op.f("ck_strategy_known_state"), "strategy", type_="check")
    op.drop_constraint(op.f("ck_strategy_live_states_need_approval"), "strategy", type_="check")
    op.create_check_constraint(
        op.f("ck_strategy_active_needs_approval"),
        "strategy",
        "state <> 'ACTIVE' OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)",
    )

    op.execute(f"REVOKE UPDATE ({_LIFECYCLE}) ON strategy FROM algotrader_app")
    op.execute("GRANT UPDATE, DELETE ON strategy TO algotrader_app")
