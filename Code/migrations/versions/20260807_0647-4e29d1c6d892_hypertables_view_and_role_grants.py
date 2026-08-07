"""hypertables, eligible-today view, and role grants

Revision ID: 4e29d1c6d892
Revises: 15bc5004e435
Created: 2026-08-07

Everything in this migration is TimescaleDB- or PostgreSQL-specific and
therefore hand-written; ``--autogenerate`` cannot see any of it.

Three things happen here, and each has a trap worth knowing about:

1. **Hypertable conversion.** ``ohlcv`` and ``decision_log`` become
   hypertables. Both already have primary keys that include the partitioning
   column, which TimescaleDB requires — ``(symbol_id, timeframe, ts)`` and
   ``(ts, seq)``. Adding a unique index later that omits ``ts`` will be
   rejected, so check any new index against that rule.

2. **Compression, chosen at runtime by version.** ``add_columnstore_policy``
   and ``timescaledb.enable_columnstore`` arrived in TimescaleDB 2.18
   (hypercore). Below that only the legacy ``add_compression_policy`` and
   ``timescaledb.compress`` exist. The pin is currently 2.17.2 and is under
   review, so this migration **detects the installed version and emits the
   matching DDL** rather than committing to either. Verified against 2.17.2
   and 2.29.1. That means the pin can move in either direction without this
   migration needing a rewrite.

3. **Role grants — where BR-3 and BR-4 actually live.** ``decision_log`` and
   ``strategy_trial`` get SELECT and INSERT only. No UPDATE. No DELETE. Ever.
   Append-only enforced by application convention is a promise; enforced by a
   missing grant it is a guarantee.

   The role is created **NOLOGIN and without a password**, deliberately. The
   spec's original ``CREATE ROLE ... LOGIN PASSWORD :'app_password'`` would put
   a credential in a version-controlled file. Granting LOGIN and setting the
   password is an operator step, done out of band. BR-3/BR-4 stay fully
   testable via ``SET ROLE`` regardless.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4e29d1c6d892"
down_revision: str | None = "15bc5004e435"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# A note on the f-strings below, so nobody has to re-derive this during a
# security review: PostgreSQL cannot parameterise DDL *identifiers* — table
# names, column lists and storage options must be literal SQL text. Every value
# interpolated here comes from the module-level constants in this file and none
# of it is reachable from user input, request data, or the database. Where a
# value CAN be bound (intervals, table names passed as function arguments) it
# is bound, via `.bindparams`.
#
#: table -> (chunk interval, segmentby columns, orderby clause)
#:
#: `segmentby` is chosen to match the query predicate: compressed scans are
#: fast exactly when the segment columns are the ones being filtered on.
#: `ohlcv` is always queried by symbol and timeframe; `decision_log` by stage.
_HYPERTABLES: dict[str, tuple[str, str, str]] = {
    "ohlcv": ("7 days", "symbol_id, timeframe", "ts DESC"),
    "decision_log": ("30 days", "stage", "ts DESC"),
}

#: Chunks older than this are compressed. Long enough that recent data — the
#: only data the live system reads — is never in columnstore form.
_COMPRESS_AFTER = "90 days"

#: The release that introduced hypercore / the columnstore API.
_COLUMNSTORE_MIN = (2, 18)


def _timescale_version(conn: sa.Connection) -> tuple[int, int]:
    raw = conn.execute(
        sa.text("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
    ).scalar_one_or_none()
    if raw is None:  # pragma: no cover - guarded by upgrade()
        raise RuntimeError("the timescaledb extension is not installed")
    major, minor, *_ = (int(part) for part in str(raw).split(".")[:2])
    return major, minor


def upgrade() -> None:
    conn = op.get_bind()

    # The extension is normally created at provisioning time by a superuser.
    # Creating it here as well keeps a fresh dev or CI database self-sufficient.
    conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE"))

    version = _timescale_version(conn)
    use_columnstore = version >= _COLUMNSTORE_MIN

    for table, (interval, segment_by, order_by) in _HYPERTABLES.items():
        conn.execute(
            sa.text(
                "SELECT create_hypertable("
                "  :table, by_range('ts', CAST(:interval AS INTERVAL)),"
                "  if_not_exists => TRUE, migrate_data => TRUE)"
            ).bindparams(table=table, interval=interval)
        )

        if use_columnstore:
            conn.execute(
                sa.text(
                    f"ALTER TABLE {table} SET ("
                    f"  timescaledb.enable_columnstore = true,"
                    f"  timescaledb.segmentby = '{segment_by}',"
                    f"  timescaledb.orderby = '{order_by}')"
                )
            )
            conn.execute(
                sa.text(
                    "SELECT add_columnstore_policy(:table, after => CAST(:after AS INTERVAL),"
                    " if_not_exists => TRUE)"
                ).bindparams(table=table, after=_COMPRESS_AFTER)
            )
        else:
            conn.execute(
                sa.text(
                    f"ALTER TABLE {table} SET ("
                    f"  timescaledb.compress,"
                    f"  timescaledb.compress_segmentby = '{segment_by}',"
                    f"  timescaledb.compress_orderby = '{order_by}')"
                )
            )
            conn.execute(
                sa.text(
                    "SELECT add_compression_policy(:table, CAST(:after AS INTERVAL),"
                    " if_not_exists => TRUE)"
                ).bindparams(table=table, after=_COMPRESS_AFTER)
            )

    # -----------------------------------------------------------------------
    # v_eligible_today
    #
    # NOTE the timezone expression. The spec used bare CURRENT_DATE, which is
    # server-timezone dependent: IST is UTC+5:30, so a UTC server returns the
    # PREVIOUS day between 00:00 and 05:30 IST. Any job running in that window
    # would read the wrong day's hazard flags and could admit a T2T or ASM
    # symbol. Anchoring explicitly to Asia/Kolkata removes the whole class.
    # -----------------------------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE VIEW v_eligible_today AS
        SELECT i.*
        FROM instruments i
        JOIN instrument_daily_status s
          ON s.symbol_id = i.id
         AND s.trade_date = (now() AT TIME ZONE 'Asia/Kolkata')::date
        WHERE i.is_active
          AND NOT s.is_t2t
          AND NOT s.is_asm
          AND NOT s.is_gsm
        """
    )

    # -----------------------------------------------------------------------
    # Application role and grants
    # -----------------------------------------------------------------------
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'algotrader_app') THEN
                -- NOLOGIN and no password on purpose: a credential must never
                -- live in a version-controlled migration. The operator grants
                -- LOGIN and sets the password out of band.
                CREATE ROLE algotrader_app NOLOGIN;
            END IF;
        END
        $$;
        """
    )

    mutable = (
        "instruments, instrument_daily_status, ohlcv, daily_plan, plan_candidate, "
        "orders, order_fills, positions, trade_journal, strategy, "
        "strategy_validation, strategy_performance, shadow_signal"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {mutable} TO algotrader_app")

    # BR-3 and BR-4. The absence of UPDATE and DELETE here IS the enforcement.
    op.execute("GRANT SELECT, INSERT ON decision_log   TO algotrader_app")
    op.execute("GRANT SELECT, INSERT ON strategy_trial TO algotrader_app")

    op.execute("GRANT SELECT ON v_eligible_today TO algotrader_app")
    op.execute("GRANT USAGE ON SCHEMA public TO algotrader_app")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO algotrader_app")


def downgrade() -> None:
    conn = op.get_bind()

    op.execute("DROP VIEW IF EXISTS v_eligible_today")

    # Policies must be removed BEFORE the hypertables are dropped by the
    # preceding migration's downgrade, or that drop fails.
    version = _timescale_version(conn)
    remover = (
        "remove_columnstore_policy" if version >= _COLUMNSTORE_MIN else "remove_compression_policy"
    )
    for table in _HYPERTABLES:
        conn.execute(
            sa.text(f"SELECT {remover}(:table, if_exists => TRUE)").bindparams(table=table)
        )

    # Grants disappear with the tables; the role is cluster-level and may be
    # shared with other databases, so it is deliberately NOT dropped here.
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM algotrader_app")
    op.execute("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM algotrader_app")
