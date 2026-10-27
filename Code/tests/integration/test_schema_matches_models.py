"""The migrations build the schema the models describe — checked, not assumed.

**Why this file exists.** Every integration test runs against a database built
by ``alembic upgrade head``, and every repository is written against
``common/db/models.py``. Nothing compared the two. A constraint declared on a
model and never migrated is the worst kind of drift in this codebase, because
the code is written as if it holds: ``uq_open_slot`` is the guarantee behind
slot allocation (E14-S08), ``uq_client_order`` is the guarantee behind order
idempotency (BR-2), and the CHECKs on ``orders`` and ``positions`` are the
database's last word on a value the application got wrong. If any of them
existed only in Python, a unit test would never notice and production would
simply not have it.

So this asks Alembic's own comparator what differs between a freshly migrated
database and ``Base.metadata``, and requires the answer to be "nothing" apart
from the differences listed below — each with its reason, so the exclusion list
is an assessed set rather than a place to hide drift.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from algotrader.common.db.base import Base

pytestmark = pytest.mark.integration

#: Objects present in the database that the models deliberately do not
#: describe. Each is created by a migration on purpose.
_DATABASE_ONLY_TABLES = frozenset(
    {
        # Alembic's own bookkeeping.
        "alembic_version",
    }
)


def _enum_check_names() -> dict[str, str]:
    """``{constraint name: table}`` for every CHECK a model's Enum column implies.

    A non-native ``sa.Enum`` with ``create_constraint=True`` gets its CHECK at
    DDL time, named by the ``ck`` convention in ``base.py``. Alembic's
    comparator cannot see those on the metadata side, so it reports each one
    in the database as "removed". They are excluded from the diff below by
    exact name — computed from the models, never typed in — and their presence
    in the database is then asserted separately, so excluding them hides
    nothing.
    """
    import algotrader.common.db.models  # noqa: F401 - registers every table on Base

    names: dict[str, str] = {}
    for table in Base.metadata.tables.values():
        for column in table.columns:
            kind = column.type
            if isinstance(kind, sa.Enum) and not kind.native_enum and kind.create_constraint:
                names[f"ck_{table.name}_{kind.name}"] = table.name
    return names


def _include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    """Leave out what the migrations create that no model can express.

    TimescaleDB creates an index on the time column of every hypertable
    (``<table>_ts_idx``) when ``create_hypertable`` runs; it is the extension's
    object, not ours, and a model declaring it would make ``create_all`` and
    the migration disagree in the other direction.
    """
    if type_ == "table" and name in _DATABASE_ONLY_TABLES:
        return False
    if type_ == "index" and reflected and compare_to is None and name and name.endswith("_ts_idx"):
        return False
    return not (
        type_ == "check_constraint"
        and reflected
        and compare_to is None
        and name in _enum_check_names()
    )


def _diff_on(conn: sa.Connection) -> list[Any]:
    context = MigrationContext.configure(
        conn,
        opts={
            "compare_type": True,
            "compare_server_default": False,
            "include_object": _include_object,
        },
    )
    return list(compare_metadata(context, Base.metadata))


def test_migrations_produce_exactly_the_modelled_schema(migrated_database: str) -> None:
    engine = sa.create_engine(migrated_database)
    try:
        with engine.connect() as conn:
            diff = _diff_on(conn)
    finally:
        engine.dispose()
    assert not diff, "the migrated schema differs from the models:\n" + "\n".join(
        f"  {d!r}" for d in diff
    )


def test_every_enum_check_the_models_imply_exists_in_the_database(migrated_database: str) -> None:
    """The other half of the exclusion above: excluded from the diff, then
    required here. A model Enum whose CHECK was never migrated would let any
    string into ``orders.side`` or ``positions.status``."""
    expected = _enum_check_names()
    assert expected, "no Enum-derived CHECKs found; the probe is not measuring anything"
    engine = sa.create_engine(migrated_database)
    try:
        inspector = sa.inspect(engine)
        present = {
            str(check["name"])
            for table in set(expected.values())
            for check in inspector.get_check_constraints(table)
        }
    finally:
        engine.dispose()
    missing = sorted(set(expected) - present)
    assert not missing, (
        f"Enum CHECKs declared by the models but absent from the database: {missing}"
    )


def test_the_comparator_reports_a_constraint_the_database_lacks(migrated_database: str) -> None:
    """The control. A comparator that returned nothing for any input would
    pass the first test forever; this drops a real safety constraint and
    requires the diff to name it.

    Inside a transaction that is rolled back: PostgreSQL DDL is transactional,
    so the shared session database never loses the index other tests rely on.
    """
    engine = sa.create_engine(migrated_database)
    try:
        with engine.connect() as conn:
            transaction = conn.begin()
            try:
                conn.execute(sa.text("DROP INDEX uq_open_slot"))
                diff = _diff_on(conn)
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
    assert any("uq_open_slot" in repr(d) for d in diff), diff


def test_every_index_declared_in_the_models_is_in_the_metadata() -> None:
    """The blind spot the comparison above cannot cover.

    An ``Index`` built from ``text()`` expressions alone attaches to no table,
    so it never reaches the metadata - and a check that compares the database
    WITH the metadata cannot see it is missing from both. Three were found
    that way on 27 Oct 2026 (``ix_audit_reason``, ``ix_trial_ts``,
    ``ix_journal_date``): declared, documented, and built by nothing. This
    reads ``models.py`` itself and requires every named Index to be carried.
    """
    import ast
    from pathlib import Path

    import algotrader.common.db.models as models

    source = Path(models.__file__).read_text(encoding="utf-8")
    declared = {
        node.args[0].value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "Index"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    carried = {index.name for table in Base.metadata.tables.values() for index in table.indexes}
    assert declared, "no Index declarations found; the scan is not measuring anything"
    missing = sorted(declared - carried)
    assert not missing, (
        f"Index declared in models.py but attached to no table: {missing}. Build it "
        f"from the mapped columns (Model.column, Model.column.desc()), not text()."
    )
