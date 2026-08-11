"""Every business rule in EPIC01_TECHNICAL_SPEC.md §3, asserted against a real database.

These are the tests that matter most in E01. Each one constructs the violation
the rule forbids and asserts the database refuses it — because a constraint
nobody has ever seen reject anything is indistinguishable from a constraint
that was never applied.

Two of these have already earned their keep during development:

- BR-3/BR-4 are enforced by the *absence* of an UPDATE/DELETE grant. Asserting
  "the grant is missing" would pass trivially if the whole role were missing.
  These tests do the DELETE and require the error.
- The first version of ``decision_log`` had a NOT NULL ``id`` with no sequence,
  because SQLAlchemy only auto-attaches one to a *primary key*. Every insert
  failed. Nothing in a static read caught it; the first INSERT did.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import psycopg
import pytest

pytestmark = [pytest.mark.integration]

_HASH_A = "0" * 64
_HASH_B = "1" * 64


@pytest.fixture
def conn(migrated_database: str) -> Iterator[psycopg.Connection]:
    """A psycopg connection to the migrated database, rolled back after each test."""
    dsn = migrated_database.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn) as connection:
        yield connection
        connection.rollback()


@pytest.fixture
def symbol_id(conn: psycopg.Connection) -> int:
    """A throwaway instrument to hang foreign keys off."""
    row = conn.execute(
        """
        INSERT INTO instruments (tradingsymbol, exchange, broker_token, tick_size)
        VALUES (%s, 'NSE', %s, 0.05) RETURNING id
        """,
        (f"TEST{uuid.uuid4().hex[:8].upper()}", uuid.uuid4().hex[:12]),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _insert_position(conn: psycopg.Connection, symbol_id: int, **overrides: object) -> None:
    values: dict[str, object] = {
        "correlation_id": uuid.uuid4(),
        "symbol_id": symbol_id,
        "slot_index": 0,
        "direction": "LONG",
        "quantity": 10,
        "entry_price": 100,
        "stop_price": 95,
        "opened_at": datetime.now(UTC),
        "squareoff_deadline": datetime.now(UTC) + timedelta(hours=5),
        "status": "OPEN",
    }
    values.update(overrides)
    cols = ", ".join(values)
    placeholders = ", ".join(["%s"] * len(values))
    conn.execute(f"INSERT INTO positions ({cols}) VALUES ({placeholders})", tuple(values.values()))


class TestPositionSafety:
    """BR-1 and BR-7 — the two rules whose violation costs real money."""

    def test_br1_position_without_stop_is_rejected(
        self, conn: psycopg.Connection, symbol_id: int
    ) -> None:
        """A naked position is the single most expensive failure. Nothing may create one."""
        with pytest.raises(psycopg.errors.NotNullViolation):
            _insert_position(conn, symbol_id, stop_price=None)

    def test_br7_position_without_squareoff_deadline_is_rejected(
        self, conn: psycopg.Connection, symbol_id: int
    ) -> None:
        """Without a deadline the broker force-closes at an arbitrary auction price."""
        with pytest.raises(psycopg.errors.NotNullViolation):
            _insert_position(conn, symbol_id, squareoff_deadline=None)

    def test_a_valid_position_is_accepted(self, conn: psycopg.Connection, symbol_id: int) -> None:
        """The control. Without this, the two tests above could pass for the wrong reason."""
        _insert_position(conn, symbol_id)
        count = conn.execute(
            "SELECT count(*) FROM positions WHERE symbol_id = %s", (symbol_id,)
        ).fetchone()
        assert count is not None and count[0] == 1

    def test_closed_position_must_record_how_it_closed(
        self, conn: psycopg.Connection, symbol_id: int
    ) -> None:
        """A CLOSED row with no exit price or reason is an unauditable hole."""
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_position(conn, symbol_id, status="CLOSED")


class TestSlotDiscipline:
    """The partial unique indexes — over-allocation is a real-money failure."""

    def test_two_open_positions_cannot_share_a_slot(
        self, conn: psycopg.Connection, symbol_id: int
    ) -> None:
        second = conn.execute(
            """
            INSERT INTO instruments (tradingsymbol, exchange, broker_token, tick_size)
            VALUES (%s, 'NSE', %s, 0.05) RETURNING id
            """,
            (f"TEST{uuid.uuid4().hex[:8].upper()}", uuid.uuid4().hex[:12]),
        ).fetchone()
        assert second is not None

        _insert_position(conn, symbol_id, slot_index=3)
        with pytest.raises(psycopg.errors.UniqueViolation):
            _insert_position(conn, int(second[0]), slot_index=3)

    def test_the_same_symbol_cannot_be_held_twice(
        self, conn: psycopg.Connection, symbol_id: int
    ) -> None:
        _insert_position(conn, symbol_id, slot_index=0)
        with pytest.raises(psycopg.errors.UniqueViolation):
            _insert_position(conn, symbol_id, slot_index=1)

    def test_a_closed_position_frees_its_slot(
        self, conn: psycopg.Connection, symbol_id: int
    ) -> None:
        """The index is partial — CLOSED rows must not keep occupying a slot."""
        _insert_position(
            conn,
            symbol_id,
            slot_index=5,
            status="CLOSED",
            closed_at=datetime.now(UTC),
            exit_price=101,
            exit_reason="TARGET",
        )
        _insert_position(conn, symbol_id, slot_index=5, status="OPEN")


class TestOrderIntegrity:
    """BR-2 and BR-12."""

    def _insert_order(self, conn: psycopg.Connection, symbol_id: int, **kw: object) -> None:
        values: dict[str, object] = {
            "client_order_id": f"cid-{uuid.uuid4().hex[:12]}",
            "correlation_id": uuid.uuid4(),
            "symbol_id": symbol_id,
            "side": "BUY",
            "order_type": "LIMIT",
            "product": "MIS",
            "quantity": 10,
            "status": "OPEN",
            "intent": "ENTRY",
            "placed_at": datetime.now(UTC),
            "last_update_at": datetime.now(UTC),
        }
        values.update(kw)
        cols = ", ".join(values)
        ph = ", ".join(["%s"] * len(values))
        conn.execute(f"INSERT INTO orders ({cols}) VALUES ({ph})", tuple(values.values()))

    def test_br2_client_order_id_is_unique(self, conn: psycopg.Connection, symbol_id: int) -> None:
        """This constraint is what makes query-don't-retry safe after a timeout."""
        cid = f"cid-{uuid.uuid4().hex[:12]}"
        self._insert_order(conn, symbol_id, client_order_id=cid)
        with pytest.raises(psycopg.errors.UniqueViolation):
            self._insert_order(conn, symbol_id, client_order_id=cid)

    def test_br12_filled_cannot_exceed_ordered(
        self, conn: psycopg.Connection, symbol_id: int
    ) -> None:
        """Catches a broker-response parsing bug before it corrupts sizing."""
        with pytest.raises(psycopg.errors.CheckViolation):
            self._insert_order(conn, symbol_id, quantity=10, filled_quantity=11)

    def test_invalid_side_is_rejected(self, conn: psycopg.Connection, symbol_id: int) -> None:
        """`side` is a CHECK-constrained enum, not a free string."""
        with pytest.raises(psycopg.errors.CheckViolation):
            self._insert_order(conn, symbol_id, side="SIDEWAYS")


class TestBarIntegrity:
    """BR-6 and the OHLC coherence rule."""

    def _insert_bar(self, conn: psycopg.Connection, symbol_id: int, **kw: object) -> None:
        values: dict[str, object] = {
            "symbol_id": symbol_id,
            "timeframe": "5m",
            "ts": datetime(2026, 8, 6, 3, 45, tzinfo=UTC),
            "open": 100,
            "high": 105,
            "low": 99,
            "close": 103,
            "volume": 1000,
        }
        values.update(kw)
        cols = ", ".join(f'"{c}"' for c in values)
        ph = ", ".join(["%s"] * len(values))
        conn.execute(f"INSERT INTO ohlcv ({cols}) VALUES ({ph})", tuple(values.values()))

    def test_br6_duplicate_bars_are_rejected(
        self, conn: psycopg.Connection, symbol_id: int
    ) -> None:
        """A duplicate bar double-counts volume and corrupts every indicator."""
        self._insert_bar(conn, symbol_id)
        with pytest.raises(psycopg.errors.UniqueViolation):
            self._insert_bar(conn, symbol_id)

    @pytest.mark.parametrize(
        ("label", "kw"),
        [
            ("close above high", {"close": 999}),
            ("open above high", {"open": 999}),
            ("low above high", {"low": 999}),
            ("close below low", {"close": 1}),
        ],
    )
    def test_incoherent_ohlc_is_rejected(
        self, conn: psycopg.Connection, symbol_id: int, label: str, kw: dict
    ) -> None:
        """The invariant that was silently inert in the Pydantic model.

        Enforced in both places deliberately: a bad backfill script bypasses the
        model entirely, and this is the one data-quality rule whose violation
        corrupts everything downstream without raising anything.
        """
        with pytest.raises(psycopg.errors.CheckViolation):
            self._insert_bar(conn, symbol_id, **kw)

    def test_negative_volume_is_rejected(self, conn: psycopg.Connection, symbol_id: int) -> None:
        with pytest.raises(psycopg.errors.CheckViolation):
            self._insert_bar(conn, symbol_id, volume=-1)


class TestAppendOnlyEnforcement:
    """BR-3 and BR-4 — enforced by a missing grant, not by convention.

    ``SET ROLE`` drops superuser privileges, so these run with exactly the
    permissions the application will have in production.
    """

    def test_br3_decision_log_cannot_be_deleted(self, conn: psycopg.Connection) -> None:
        conn.execute("SET ROLE algotrader_app")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM decision_log")

    def test_br3_decision_log_cannot_be_updated(self, conn: psycopg.Connection) -> None:
        conn.execute("SET ROLE algotrader_app")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE decision_log SET outcome = 'TAMPERED'")

    def test_br4_strategy_trials_cannot_be_deleted(self, conn: psycopg.Connection) -> None:
        """Deleting failed trials corrupts the Deflated Sharpe denominator."""
        conn.execute("SET ROLE algotrader_app")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM strategy_trial")

    def test_decision_log_can_still_be_inserted(self, conn: psycopg.Connection) -> None:
        """Append-only, not read-only.

        This is also the regression test for the missing identity sequence: the
        column is NOT NULL and nothing supplies ``id`` here, so a table without
        a working default fails this outright.
        """
        conn.execute("SET ROLE algotrader_app")
        row = conn.execute(
            """
            INSERT INTO decision_log
                (ts, seq, correlation_id, stage, outcome, payload, service, prev_hash, row_hash)
            VALUES (now(), %s, %s, 'SIGNAL', 'ALLOW', '{}', 'test', %s, %s)
            RETURNING id
            """,
            (1, uuid.uuid4(), _HASH_A, _HASH_B),
        ).fetchone()
        assert row is not None and row[0] is not None

    def test_mutable_tables_are_still_mutable(self, conn: psycopg.Connection) -> None:
        """The control: the restriction must be targeted, not blanket."""
        conn.execute("SET ROLE algotrader_app")
        conn.execute("DELETE FROM orders")


class TestStrategyApprovalGate:
    """BR-5 — the human approval gate, enforced by the database."""

    def _insert_strategy(self, conn: psycopg.Connection, **kw: object) -> None:
        values: dict[str, object] = {
            "id": f"strat-{uuid.uuid4().hex[:8]}",
            "name": "test",
            "origin": "USER_AUTHORED",
            "state": "DRAFT",
            "dsl": "{}",
            "dsl_hash": _HASH_A,
            "hypothesis": "{}",
            "hypothesis_frozen_at": datetime.now(UTC),
            "applicable_regimes": ["TRENDING"],
            "created_at": datetime.now(UTC),
            "created_by": "test",
            "state_changed_at": datetime.now(UTC),
        }
        values.update(kw)
        cols = ", ".join(values)
        ph = ", ".join(["%s"] * len(values))
        conn.execute(f"INSERT INTO strategy ({cols}) VALUES ({ph})", tuple(values.values()))

    def test_br5_active_without_approval_is_rejected(self, conn: psycopg.Connection) -> None:
        with pytest.raises(psycopg.errors.CheckViolation):
            self._insert_strategy(conn, state="ACTIVE")

    def test_br5_active_with_approval_is_accepted(self, conn: psycopg.Connection) -> None:
        self._insert_strategy(
            conn, state="ACTIVE", approved_by="human", approved_at=datetime.now(UTC)
        )

    def test_br5_cannot_be_bypassed_by_update(self, conn: psycopg.Connection) -> None:
        """A CHECK, not an application rule — so a bad UPDATE cannot promote one either."""
        sid = f"strat-{uuid.uuid4().hex[:8]}"
        self._insert_strategy(conn, id=sid, state="DRAFT")
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE strategy SET state = 'ACTIVE' WHERE id = %s", (sid,))


class TestHazardFlagsAndView:
    """BR-8, and the timezone correction from §2.3 C."""

    def test_br8_hazard_flags_are_per_symbol_per_day(
        self, conn: psycopg.Connection, symbol_id: int
    ) -> None:
        """One row per symbol per day — a current-state row would lose history."""
        conn.execute(
            "INSERT INTO instrument_daily_status (symbol_id, trade_date) VALUES (%s, %s)",
            (symbol_id, "2026-08-06"),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO instrument_daily_status (symbol_id, trade_date) VALUES (%s, %s)",
                (symbol_id, "2026-08-06"),
            )

    def test_eligible_view_is_anchored_to_ist_not_server_time(
        self, conn: psycopg.Connection
    ) -> None:
        """Bare CURRENT_DATE returns the previous day between 00:00 and 05:30 IST.

        A job running in that window would read the wrong day's hazard flags and
        could admit a T2T or ASM symbol.
        """
        row = conn.execute("SELECT pg_get_viewdef('v_eligible_today')").fetchone()
        assert row is not None
        assert "Asia/Kolkata" in row[0]

    def test_hazardous_symbols_are_excluded_today(
        self, conn: psycopg.Connection, symbol_id: int
    ) -> None:
        ist_today = conn.execute("SELECT (now() AT TIME ZONE 'Asia/Kolkata')::date").fetchone()
        assert ist_today is not None
        conn.execute(
            "INSERT INTO instrument_daily_status (symbol_id, trade_date, is_t2t) "
            "VALUES (%s, %s, TRUE)",
            (symbol_id, ist_today[0]),
        )
        found = conn.execute(
            "SELECT count(*) FROM v_eligible_today WHERE id = %s", (symbol_id,)
        ).fetchone()
        assert found is not None and found[0] == 0


class TestTimescaleLayout:
    """The physical layout the capacity analysis assumes."""

    def test_both_hypertables_exist(self, conn: psycopg.Connection) -> None:
        rows = conn.execute(
            "SELECT hypertable_name FROM timescaledb_information.hypertables"
        ).fetchall()
        names = {r[0] for r in rows}
        assert {"ohlcv", "decision_log"} <= names

    def test_compression_segments_match_the_query_predicate(self, conn: psycopg.Connection) -> None:
        """Compressed scans are fast only when segmentby matches the filter columns."""
        rows = conn.execute(
            """
            SELECT attname FROM timescaledb_information.compression_settings
            WHERE hypertable_name = 'ohlcv' AND segmentby_column_index IS NOT NULL
            """
        ).fetchall()
        assert {r[0] for r in rows} == {"symbol_id", "timeframe"}

    def test_a_compression_policy_is_scheduled(self, conn: psycopg.Connection) -> None:
        rows = conn.execute(
            "SELECT hypertable_name FROM timescaledb_information.jobs "
            "WHERE proc_name LIKE '%%compress%%'"
        ).fetchall()
        assert {r[0] for r in rows} >= {"ohlcv", "decision_log"}


class TestMoneyPrecision:
    """BR-10 — exact arithmetic, never float."""

    def test_price_columns_are_numeric_not_float(self, conn: psycopg.Connection) -> None:
        """Float error accumulates in P&L and breaks tick-size equality."""
        rows = conn.execute(
            """
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND (column_name LIKE '%%price%%' OR column_name IN
                   ('open', 'high', 'low', 'close', 'vwap', 'realized_pnl'))
            """
        ).fetchall()
        assert rows, "no price columns found — the query is wrong, not the schema"
        offenders = [(t, c, d) for t, c, d in rows if d not in ("numeric",)]
        assert not offenders, f"non-exact numeric types in money columns: {offenders}"

    def test_all_timestamps_are_timezone_aware(self, conn: psycopg.Connection) -> None:
        """BR-9 — naive timestamps in a fixed-session market are a silent bug class."""
        rows = conn.execute(
            """
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public' AND data_type LIKE 'timestamp%%'
            """
        ).fetchall()
        naive = [(t, c) for t, c, d in rows if d != "timestamp with time zone"]
        assert not naive, f"naive timestamp columns: {naive}"


class TestEveryTableHasDeliberateGrants:
    """No table may ship without a decision about who can touch it.

    The grant migration enumerates tables **by name**, so any table added later
    gets nothing. `corporate_action` shipped exactly that way: zero privileges
    for `algotrader_app`, and its sequence missing USAGE too. Every test passed,
    because the harness connects as the table owner. The first failure would
    have been in production, on the corporate action feed, as
    InsufficientPrivilege.

    These tests compare the live catalogue against the model metadata rather
    than against a hardcoded list, so a new table fails here the moment it is
    created rather than the moment it is used.
    """

    #: Append-only by design (BR-3, BR-4). UPDATE/DELETE must stay absent.
    APPEND_ONLY: ClassVar[set[str]] = {"decision_log", "strategy_trial"}

    #: Owned by tooling, not the application. Migrations run as the owner.
    NOT_APP_TABLES: ClassVar[set[str]] = {"alembic_version"}

    @staticmethod
    def _granted(conn: psycopg.Connection, table: str) -> set[str]:
        rows = conn.execute(
            """
            SELECT privilege_type FROM information_schema.role_table_grants
            WHERE table_name = %s AND grantee = 'algotrader_app'
            """,
            (table,),
        ).fetchall()
        return {r[0] for r in rows}

    def _model_tables(self) -> set[str]:
        from algotrader.common.db.base import Base

        return set(Base.metadata.tables)

    def test_no_model_table_is_completely_ungranted(self, conn: psycopg.Connection) -> None:
        """The failure that shipped: a table the app role cannot see at all."""
        ungranted = sorted(
            t for t in self._model_tables() - self.NOT_APP_TABLES if not self._granted(conn, t)
        )
        assert not ungranted, (
            f"{ungranted} have NO privileges for algotrader_app. The grant "
            f"migration lists tables by name, so a new table gets nothing and "
            f"fails only in production. Add a grant migration for it."
        )

    def test_mutable_tables_can_be_written_and_corrected(self, conn: psycopg.Connection) -> None:
        from algotrader.common.db.models import MUTABLE_TABLES

        required = {"SELECT", "INSERT", "UPDATE", "DELETE"}
        for table in MUTABLE_TABLES:
            missing = required - self._granted(conn, table)
            assert not missing, f"{table} is declared MUTABLE but is missing {sorted(missing)}"

    def test_append_only_tables_still_refuse_update_and_delete(
        self, conn: psycopg.Connection
    ) -> None:
        """The control. Fixing one grant gap must not blanket-grant everything."""
        for table in self.APPEND_ONLY:
            granted = self._granted(conn, table)
            assert "INSERT" in granted and "SELECT" in granted
            assert not ({"UPDATE", "DELETE"} & granted), (
                f"{table} is append-only (BR-3/BR-4) but has {sorted(granted)}"
            )

    def test_every_sequence_backing_a_writable_table_is_usable(
        self, conn: psycopg.Connection
    ) -> None:
        """A table grant without a sequence grant still fails on INSERT.

        `GRANT ... ON ALL SEQUENCES` only covers sequences existing at that
        moment, which is why corporate_action_id_seq was missed alongside its
        table. nextval() fails before any row is written.
        """
        rows = conn.execute(
            """
            SELECT c.relname, has_sequence_privilege('algotrader_app', c.oid, 'USAGE')
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'S' AND n.nspname = 'public'
            """
        ).fetchall()
        unusable = sorted(name for name, usable in rows if not usable)
        assert not unusable, f"algotrader_app cannot use sequences {unusable}; INSERT will fail"

    def test_the_app_role_can_actually_write_a_corporate_action(
        self, conn: psycopg.Connection, symbol_id: int
    ) -> None:
        """End to end under the production role, not the owner.

        This is the probe that would have caught the original defect: every
        other corporate action test runs as the owner and cannot see the
        permission at all.
        """
        conn.execute("SET ROLE algotrader_app")
        conn.execute(
            """
            INSERT INTO corporate_action
                (symbol_id, action_type, ex_date, ratio_from, ratio_to, source)
            VALUES (%s, 'SPLIT', DATE '2026-06-15', 1, 5, 'test')
            """,
            (symbol_id,),
        )
        row = conn.execute(
            "SELECT count(*) FROM corporate_action WHERE symbol_id = %s", (symbol_id,)
        ).fetchone()
        assert row is not None and row[0] == 1

        # A mis-entered action must be correctable — this table is NOT an
        # audit record, and recompute rebuilds from whatever it holds.
        conn.execute("UPDATE corporate_action SET ratio_to = 10 WHERE symbol_id = %s", (symbol_id,))
        conn.execute("DELETE FROM corporate_action WHERE symbol_id = %s", (symbol_id,))
