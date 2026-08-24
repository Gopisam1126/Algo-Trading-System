"""Alembic environment — wired to the application's own configuration.

The connection URL is **not** read from ``alembic.ini``.  It is built from
``config/system.yaml`` plus the ``POSTGRES_PASSWORD`` secret, exactly the way
the running application builds it, so migrations cannot drift onto a different
database than the one the services use.  ``DATABASE_URL`` overrides both, which
is how CI and the integration tests point Alembic at an ephemeral container.

Two TimescaleDB-specific behaviours are configured here and both matter:

1. **Chunks are excluded from autogenerate.**  A hypertable's physical chunks
   live in ``_timescaledb_internal`` and are not in ``Base.metadata``.  Without
   the filter below, ``--autogenerate`` sees hundreds of unknown tables and
   emits ``DROP TABLE`` for every one of them.  Running that migration destroys
   the market data while reporting success.

2. **Migrations run in a transaction.**  So a failed migration leaves no
   half-applied schema.  Note that some TimescaleDB policy functions cannot run
   inside a transaction block; where that bites, the migration itself must use
   an autocommit block rather than this setting being relaxed globally.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# The application's declarative metadata.  `prepend_sys_path = src` in
# alembic.ini makes this importable when alembic is run from Code/.
from algotrader.common.db import models  # noqa: F401  — registers every table
from algotrader.common.db.base import Base
from algotrader.common.db.eventloop import configure_event_loop_policy

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False is NOT cosmetic. fileConfig defaults to
    # True, which reaches into every logger that already exists and switches it
    # off. Alembic runs in-process here — the scaffold helper and the test
    # suite both call it — so the default would let a migration silently kill
    # application logging for the rest of the process. A trading system that
    # stops logging without failing is the worst possible outcome: it keeps
    # trading and there is no record of what it did.
    #
    # Found because it broke pytest's caplog for every test that ran after a
    # migration, which is the same defect wearing a smaller hat.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# Connection URL — from the application config, not from alembic.ini
# ---------------------------------------------------------------------------


def _database_url() -> str:
    """Resolve the URL the same way the application does.

    Precedence: ``DATABASE_URL`` env override, else ``config/system.yaml``
    structure + ``POSTGRES_PASSWORD``.  Failure here is loud on purpose —
    a migration run against the wrong database is worse than one that refuses
    to start.
    """
    if override := os.environ.get("DATABASE_URL"):
        return override

    from algotrader.common.config import load_config

    app_config = load_config()

    password = os.environ.get("POSTGRES_PASSWORD")
    if not password:
        raise RuntimeError(
            "POSTGRES_PASSWORD is not set and DATABASE_URL is not set, so the "
            "migration has no database to connect to.\n"
            "  Local:      copy .env.example to .env and fill in POSTGRES_PASSWORD\n"
            "  Containers: DATABASE_URL is injected by ops/docker-compose.yml\n"
            "  Tests:      the postgres fixture sets DATABASE_URL for you"
        )

    return app_config.database.dsn(password)


def _include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    """Keep objects autogenerate does not own out of its comparison.

    Three classes are excluded, and each one exists because autogenerate
    otherwise proposes a destructive change:

    1. **TimescaleDB internals.** Chunks live in ``_timescaledb_internal`` and
       are not in ``Base.metadata``, so autogenerate sees hundreds of unknown
       tables and emits ``DROP TABLE`` for each. Running that destroys the
       market data while reporting success.

    2. **TimescaleDB's own indexes.** ``create_hypertable`` builds
       ``<table>_ts_idx`` itself. It is not in the model, so autogenerate
       proposes dropping it — removing the time index the hypertable is built
       around.

    3. **Enum CHECK constraints.** ``Enum(create_constraint=True)`` creates a
       CHECK at table-creation time, but alembic does not associate the
       reflected constraint with the type, so every autogenerate run proposes
       dropping all of them. Accepting that would silently remove every enum
       validation in the schema — the precise defect QA-SEC found when
       ``create_constraint`` defaulted to False, reintroduced by a migration
       nobody read closely.
    """
    internal_schemas = {
        "_timescaledb_internal",
        "_timescaledb_catalog",
        "_timescaledb_config",
        "_timescaledb_cache",
        "_timescaledb_functions",
        "timescaledb_information",
        "timescaledb_experimental",
    }
    if getattr(obj, "schema", None) in internal_schemas:
        return False

    if type_ == "table" and name and name.startswith("_hyper_"):
        return False

    if type_ == "index" and name and name.endswith("_ts_idx"):
        return False

    if type_ == "check_constraint" and name and name.endswith("_enum"):
        return False

    return True


def _configure(connection: Connection | None = None, url: str | None = None) -> None:
    """Shared context configuration for both offline and online runs."""
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        include_object=_include_object,
        # Detect column type changes (e.g. NUMERIC(12,2) -> NUMERIC(14,4)),
        # which autogenerate ignores by default.  In a system where money
        # precision is a correctness property, that default is wrong.
        compare_type=True,
        # Detect server-default changes too.
        compare_server_default=True,
        transaction_per_migration=True,
        literal_binds=url is not None,
        dialect_opts={"paramstyle": "named"} if url is not None else {},
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting — ``alembic upgrade head --sql``.

    Useful for reviewing exactly what a migration will do to the production
    database before it touches it.
    """
    _configure(url=_database_url())
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    # MUST happen before the loop is created. On Windows the default
    # ProactorEventLoop makes async psycopg raise InterfaceError at the first
    # query — see common/db/eventloop.py. No-op elsewhere.
    configure_event_loop_policy()
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
