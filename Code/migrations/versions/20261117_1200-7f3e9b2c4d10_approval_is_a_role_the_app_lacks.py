"""approval is a role the application lacks; the rejection reader matches its index

Revision ID: 7f3e9b2c4d10
Revises: 3d12560eeba9
Created: 2026-11-17 12:00:00+00:00

Audit remediation, 17 Nov 2026.

1. **RSK-007 - a strategy's approval is not the application's to write.** BR-5
   was two columns on ``strategy`` the application role could UPDATE, so
   anything holding the app's credentials could run ``UPDATE strategy SET
   state = 'ACTIVE', approved_by = 'anyone', approved_at = now()`` on a DRAFT
   that never passed validation, shadow or paper, and the CHECK accepted it.

   - A new role, ``algotrader_approver`` (NOLOGIN; the operator grants a login
     or membership out of band), is the only one that can INSERT into the new
     ``strategy_approval``. The application has SELECT on it.
   - The application loses UPDATE on ``approved_by`` and ``approved_at``.
   - A trigger on ``strategy_approval`` refuses an approval unless it is
     written AS ``algotrader_approver`` (``SET ROLE``), the strategy is
     ``AWAITING_APPROVAL`` and the approval names its current ``dsl_hash``;
     it records ``approved_as`` (that role), ``approved_login`` (the login
     that set it) and ``approved_at`` itself - the writer chooses none of
     them. Membership alone is not enough: a member writing under its own
     name would be accepted here and refused by the registry's load, so the
     database and the code would disagree about what an approval is (QA,
     18 Nov 2026).
   - A trigger on ``strategy`` refuses any INSERT or UPDATE into ACTIVE or
     DEGRADED without an approval of that exact document, and copies the
     approver onto the row - for every writer, the owner included.

2. **ENG-026 - the rejection index and its reader agree.** ``ix_audit_reason``
   covered ``outcome = 'rejected'`` only, the risk engine's spelling; the
   execution service writes ``REJECTED`` and ``REFUSED``. It now covers all
   three (``models.REJECTION_OUTCOMES``), led by ``ts`` so the reader's
   ``ORDER BY ts DESC LIMIT n`` can use it.

Checklist before merging this migration (EPIC01_TECHNICAL_SPEC.md §12):

- [x] ``downgrade()`` is implemented: it drops what ``upgrade()`` created and
      restores the grants and the index; the role, cluster-level and possibly
      shared, is not dropped (as with ``algotrader_app``).
- [x] No TimescaleDB policy is added; ``strategy_approval`` is not a hypertable.
- [x] No money columns; one timestamp column, timezone-aware.
- [x] Existing rows: no strategy has ever been ACTIVE or DEGRADED (E12-S02,
      which moves states, is unbuilt), so the new trigger refuses no row a
      deployment already holds.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7f3e9b2c4d10"
down_revision: str | None = "3d12560eeba9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Frozen here: a migration is a snapshot. ``models.APPROVER_ROLE`` and
#: ``models.REJECTION_OUTCOMES`` are held to these by the integration tests.
_APPROVER = "algotrader_approver"
_REJECTIONS = ("rejected", "REJECTED", "REFUSED")


def _in(values: Sequence[str]) -> str:
    return "outcome IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'algotrader_approver') THEN
                -- NOLOGIN and no password: a credential never lives in a
                -- migration. The operator grants it to a human's login.
                CREATE ROLE algotrader_approver NOLOGIN;
            END IF;
        END
        $$;
        """
    )

    op.create_table(
        "strategy_approval",
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column("dsl_hash", sa.String(length=64), nullable=False),
        sa.Column("approved_by", sa.String(length=64), nullable=False),
        sa.Column(
            "approved_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "approved_as",
            sa.String(length=64),
            server_default=sa.text("CURRENT_USER"),
            nullable=False,
        ),
        sa.Column(
            "approved_login",
            sa.String(length=64),
            server_default=sa.text("SESSION_USER"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "dsl_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_strategy_approval_approved_hash_is_sha256"),
        ),
        sa.CheckConstraint(
            "length(btrim(approved_by)) >= 2",
            name=op.f("ck_strategy_approval_approver_is_named"),
        ),
        sa.ForeignKeyConstraint(
            ["strategy_id"],
            ["strategy.id"],
            name=op.f("fk_strategy_approval_strategy_id_strategy"),
        ),
        sa.PrimaryKeyConstraint("strategy_id", name=op.f("pk_strategy_approval")),
    )

    op.execute(
        """
        CREATE FUNCTION strategy_approval_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            target RECORD;
        BEGIN
            -- AS the role, not as a member of it: the registry accepts an
            -- approval only with approved_as = algotrader_approver, so a
            -- member writing under its own name must be refused HERE, not
            -- accepted here and refused at load.
            IF current_user <> 'algotrader_approver' THEN
                RAISE EXCEPTION USING ERRCODE = 'insufficient_privilege', MESSAGE = format(
                    'an approval is written as algotrader_approver (SET ROLE '
                    || 'algotrader_approver first); %s is not it', current_user);
            END IF;
            SELECT state, dsl_hash INTO target FROM strategy WHERE id = NEW.strategy_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'no strategy % to approve', NEW.strategy_id
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
            IF target.state <> 'AWAITING_APPROVAL' THEN
                RAISE EXCEPTION USING ERRCODE = 'check_violation', MESSAGE = format(
                    'strategy %s is %s, not AWAITING_APPROVAL: nothing is approved '
                    || 'before validation, shadow and paper', NEW.strategy_id, target.state);
            END IF;
            IF target.dsl_hash <> NEW.dsl_hash THEN
                RAISE EXCEPTION 'the approval names a document strategy % does not hold',
                    NEW.strategy_id USING ERRCODE = 'check_violation';
            END IF;
            NEW.approved_as := current_user;
            -- Who actually logged in: approved_as is always the role, and
            -- approved_by is a name the writer types.
            NEW.approved_login := session_user;
            NEW.approved_at := now();
            RETURN NEW;
        END
        $$;
        """
    )
    op.execute(
        "CREATE TRIGGER strategy_approval_guard BEFORE INSERT ON strategy_approval "
        "FOR EACH ROW EXECUTE FUNCTION strategy_approval_guard()"
    )
    op.execute(
        """
        CREATE FUNCTION strategy_live_needs_approval() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            granted RECORD;
        BEGIN
            IF NEW.state IN ('ACTIVE', 'DEGRADED') THEN
                SELECT approved_by, approved_at INTO granted FROM strategy_approval
                WHERE strategy_id = NEW.id AND dsl_hash = NEW.dsl_hash;
                IF NOT FOUND THEN
                    RAISE EXCEPTION USING ERRCODE = 'check_violation', MESSAGE = format(
                        'strategy %s may not be %s: no approval of this document by '
                        || 'algotrader_approver (BR-5, live_states_need_approval)', NEW.id, NEW.state);
                END IF;
                NEW.approved_by := granted.approved_by;
                NEW.approved_at := granted.approved_at;
            END IF;
            RETURN NEW;
        END
        $$;
        """
    )
    op.execute(
        "CREATE TRIGGER strategy_live_needs_approval BEFORE INSERT OR UPDATE ON strategy "
        "FOR EACH ROW EXECUTE FUNCTION strategy_live_needs_approval()"
    )

    op.execute("REVOKE UPDATE (approved_by, approved_at) ON strategy FROM algotrader_app")
    op.execute("GRANT SELECT ON strategy_approval TO algotrader_app")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {_APPROVER}")
    op.execute(f"GRANT SELECT ON strategy TO {_APPROVER}")
    op.execute(f"GRANT SELECT, INSERT ON strategy_approval TO {_APPROVER}")

    op.drop_index(
        "ix_audit_reason",
        table_name="decision_log",
        postgresql_where=sa.text("outcome = 'rejected'"),
    )
    op.create_index(
        "ix_audit_reason",
        "decision_log",
        [sa.text("ts DESC"), "reason_code"],
        unique=False,
        postgresql_where=sa.text(_in(_REJECTIONS)),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_audit_reason",
        table_name="decision_log",
        postgresql_where=sa.text(_in(_REJECTIONS)),
    )
    op.create_index(
        "ix_audit_reason",
        "decision_log",
        ["reason_code", sa.text("ts DESC")],
        unique=False,
        postgresql_where=sa.text("outcome = 'rejected'"),
    )

    op.execute(f"REVOKE ALL ON strategy_approval FROM {_APPROVER}")
    op.execute(f"REVOKE ALL ON strategy FROM {_APPROVER}")
    op.execute("REVOKE ALL ON strategy_approval FROM algotrader_app")
    op.execute("GRANT UPDATE (approved_by, approved_at) ON strategy TO algotrader_app")

    op.execute("DROP TRIGGER strategy_live_needs_approval ON strategy")
    op.execute("DROP FUNCTION strategy_live_needs_approval()")
    op.execute("DROP TRIGGER strategy_approval_guard ON strategy_approval")
    op.execute("DROP FUNCTION strategy_approval_guard()")
    op.drop_table("strategy_approval")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {_APPROVER}")
