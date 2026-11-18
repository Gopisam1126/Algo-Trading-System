"""The services' login cannot rewrite the record - checked AS that login (audit SEC-002).

Every other privilege test in this suite runs ``SET ROLE algotrader_app`` on
an owner's connection. That proves the GRANTS. It does not prove what a
service gets, because the services did not connect as algotrader_app at all:
they connected as the bootstrap user that owns the tables, for whom every one
of those grants was irrelevant.

So this module starts its own TimescaleDB with the REAL first-init script
mounted (``ops/postgres/initdb``), migrates it as the owner exactly as
``make migrate`` does, and then connects with the DSN the services use -
a fresh login, no SET ROLE - and asks it to rewrite history.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql

from algotrader.common.config import DatabaseConfig
from algotrader.common.db import privilege
from algotrader.common.db.engine import create_engine_from_url, open_service_engine
from algotrader.common.db.models import APPEND_ONLY_TABLES
from algotrader.common.db.privilege import LeastPrivilegeError, require_least_privilege

pytestmark = [pytest.mark.integration]

CODE = Path(__file__).resolve().parents[2]
INITDB = CODE / "ops" / "postgres" / "initdb"
SVC = "algotrader_svc"
#: Test-only values for a throwaway container, assembled so the secret scanner
#: does not read them as credentials.
SVC_PASSWORD = "svc_" + "test_only_not_a_real_secret"
OWNER_PASSWORD = "owner_" + "test_only_not_a_real_secret"


@pytest.fixture(scope="module")
def deployed() -> Iterator[dict[str, str]]:
    """A database initialised the way compose initialises one, then migrated."""
    from alembic import command
    from alembic.config import Config
    from testcontainers.community.postgres import PostgresContainer

    from tests.conftest import TIMESCALE_IMAGE

    container = (
        PostgresContainer(
            image=TIMESCALE_IMAGE,
            username="algotrader",
            password=OWNER_PASSWORD,
            dbname="algotrader",
            driver="psycopg",
        )
        .with_env("ALGOTRADER_APP_DB_USER", SVC)
        .with_env("ALGOTRADER_APP_DB_PASSWORD", SVC_PASSWORD)
        .with_volume_mapping(str(INITDB), "/docker-entrypoint-initdb.d", "ro")
    )
    with container:
        owner_url: str = container.get_connection_url()
        host = container.get_container_host_ip()
        port = container.get_exposed_port(5432)
        svc_url = f"postgresql+psycopg://{SVC}:{SVC_PASSWORD}@{host}:{port}/algotrader"
        with psycopg.connect(_plain(owner_url)) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;")
            conn.commit()
        previous = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = owner_url
        try:
            cfg = Config(str(CODE / "alembic.ini"))
            cfg.set_main_option("script_location", str(CODE / "migrations"))
            command.upgrade(cfg, "head")
        finally:
            if previous is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = previous
        yield {"owner": owner_url, "svc": svc_url}


def _plain(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://")


def _check(url: str) -> None:
    async def run() -> None:
        engine = create_engine_from_url(url)
        try:
            async with engine.connect() as conn:
                await require_least_privilege(conn)
        finally:
            await engine.dispose()

    asyncio.run(run())


class TestTheFirstInitCreatesTheServicesLogin:
    def test_it_is_a_login_member_of_the_app_role_that_owns_nothing(
        self, deployed: dict[str, str]
    ) -> None:
        with psycopg.connect(_plain(deployed["owner"])) as conn:
            row = conn.execute(
                "SELECT r.rolcanlogin, r.rolsuper, r.rolinherit, "
                "pg_has_role(r.rolname, 'algotrader_app', 'MEMBER'), "
                "(SELECT count(*) FROM pg_tables t WHERE t.tableowner = r.rolname) "
                "FROM pg_roles r WHERE r.rolname = %s",
                (SVC,),
            ).fetchone()
        assert row == (True, False, True, True, 0)


class TestConnectedAsTheServicesNothingRewritesTheRecord:
    """Each statement below is what a bug or a compromised service would run.
    Not under SET ROLE: on the login the services' DSN names."""

    @pytest.mark.parametrize(
        "statement",
        [
            "UPDATE decision_log SET outcome = 'ALLOW'",
            "DELETE FROM decision_log",
            "TRUNCATE decision_log",
            "UPDATE strategy_trial SET observed_sharpe = 9",
            "DELETE FROM strategy_trial",
            "UPDATE strategy SET dsl = '{}'::jsonb",
            "UPDATE strategy SET dsl_hash = repeat('0', 64)",
            "DELETE FROM strategy",
            "INSERT INTO strategy_approval (strategy_id, dsl_hash, approved_by) "
            "VALUES ('x', repeat('0', 64), 'mallory')",
        ],
    )
    def test_it_is_refused(self, deployed: dict[str, str], statement: str) -> None:
        with psycopg.connect(_plain(deployed["svc"]), autocommit=True) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)

    def test_the_control_it_can_do_its_job(self, deployed: dict[str, str]) -> None:
        """A login that could do nothing would pass every refusal above."""
        with psycopg.connect(_plain(deployed["svc"]), autocommit=True) as conn:
            conn.execute("SELECT count(*) FROM decision_log").fetchone()
            row = conn.execute(
                "SELECT has_table_privilege('decision_log', 'INSERT'), "
                "has_table_privilege('orders', 'UPDATE'), "
                "has_column_privilege('strategy', 'state', 'UPDATE')"
            ).fetchone()
        assert row == (True, True, True)


class TestTheServicesRefuseALoginThatCould:
    def test_the_services_login_passes(self, deployed: dict[str, str]) -> None:
        _check(deployed["svc"])

    def test_the_owner_is_refused_and_told_why(self, deployed: dict[str, str]) -> None:
        with pytest.raises(LeastPrivilegeError) as refused:
            _check(deployed["owner"])
        message = str(refused.value)
        assert "owns tables in the schema" in message
        assert "can DELETE from decision_log" in message

    def test_a_member_of_the_owner_is_refused_too(self, deployed: dict[str, str]) -> None:
        """Owning nothing is not enough: membership in the owner inherits its rights."""
        with psycopg.connect(_plain(deployed["owner"]), autocommit=True) as conn:
            conn.execute("DROP ROLE IF EXISTS qa_owner_member")
            # A utility statement takes no bind parameters; psycopg quotes the literal.
            conn.execute(
                sql.SQL("CREATE ROLE qa_owner_member LOGIN PASSWORD {} IN ROLE algotrader").format(
                    sql.Literal(SVC_PASSWORD)
                )
            )
        try:
            member = deployed["svc"].replace(f"{SVC}:", "qa_owner_member:")
            with pytest.raises(LeastPrivilegeError, match="can UPDATE decision_log"):
                _check(member)
        finally:
            with psycopg.connect(_plain(deployed["owner"]), autocommit=True) as conn:
                conn.execute("DROP ROLE qa_owner_member")

    def test_open_service_engine_refuses_the_owners_dsn(
        self, deployed: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", deployed["owner"])
        with pytest.raises(LeastPrivilegeError):
            asyncio.run(open_service_engine(DatabaseConfig()))

    def test_open_service_engine_returns_an_engine_for_the_services_login(
        self, deployed: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", deployed["svc"])

        async def run() -> Any:
            engine = await open_service_engine(DatabaseConfig())
            try:
                async with engine.connect() as conn:
                    return (await conn.exec_driver_sql("SELECT current_user")).scalar_one()
            finally:
                await engine.dispose()

        assert asyncio.run(run()) == SVC


class TestTheCheckKnowsEveryAppendOnlyTable:
    def test_every_append_only_table_is_checked_for_update_and_delete(self) -> None:
        """A table added to APPEND_ONLY_TABLES without a line in the check
        would be append-only for the grants and not for the check."""
        sql = str(privilege._CHECK)
        for table in APPEND_ONLY_TABLES:
            for right in ("UPDATE", "DELETE", "TRUNCATE"):
                assert f"has_table_privilege('{table}', '{right}')" in sql, (table, right)
