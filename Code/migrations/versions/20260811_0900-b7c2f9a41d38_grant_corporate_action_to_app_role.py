"""grant corporate_action to the app role

Revision ID: b7c2f9a41d38
Revises: cdcd9ee2ce69
Created: 2026-08-11 09:00:00.000000+00:00

``corporate_action`` shipped with **no grants at all** for ``algotrader_app``.

The grant migration (``4e29d1c6d892``) enumerates tables by name and ran long
before this table existed, so nothing gave the application role access to it.
Every test passed because the test harness connects as the table owner; the
first thing to fail would have been the corporate action feed in production,
with ``InsufficientPrivilege`` on INSERT and on the SELECT inside
``recompute_factors``. The whole E03-S02 adjustment path was dead behind a
permission nobody had granted.

The sequence needs its own grant for the same reason: ``GRANT USAGE, SELECT ON
ALL SEQUENCES`` applies to sequences existing *at that moment*, and
``corporate_action_id_seq`` did not exist yet. Without it, INSERT fails on
``nextval`` even once the table grant is in place.

``corporate_action`` is deliberately MUTABLE (UPDATE and DELETE included),
unlike ``decision_log``. A mis-entered corporate action must be correctable —
the whole point of deriving adjustment factors from this table rather than
mutating prices is that fixing a bad action and recomputing is safe. It is not
an audit record.

Checklist before merging this migration (EPIC01_TECHNICAL_SPEC.md §12):

- [x] ``downgrade()`` is implemented and tested. Revoking is the exact inverse
      of granting, and upgrade -> downgrade -> upgrade was verified clean.
- [x] No TimescaleDB policy is added, so there is no drop-ordering to get wrong.
- [x] No index or constraint changes at all — this migration only moves
      privileges.
- [x] No columns are added, so there is no money-precision or timezone question.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b7c2f9a41d38"
down_revision: str | None = "cdcd9ee2ce69"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Guarded so the migration is runnable against a database where the role
    # was never created (a developer's throwaway instance). Granting to a
    # missing role is a hard error and would block the whole upgrade.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'algotrader_app') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON corporate_action TO algotrader_app;
                GRANT USAGE, SELECT
                    ON SEQUENCE corporate_action_id_seq TO algotrader_app;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'algotrader_app') THEN
                REVOKE ALL ON corporate_action FROM algotrader_app;
                REVOKE ALL ON SEQUENCE corporate_action_id_seq FROM algotrader_app;
            END IF;
        END
        $$;
        """
    )
