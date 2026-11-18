"""An in-process migration must leave the application's redacting log handler alone.

Audit SEC-012 (17 Nov 2026). ``migrations/env.py`` called ``fileConfig``, which
REPLACES the root logger's handler: after any in-process migration the
RedactingFilter was gone and the root level had dropped from INFO to WARNING,
so a DSN in a driver's traceback went out in clear. The unit test beside it
(``tests/unit/test_migration_logging.py``) checked one flag and restored the
handlers around its own probe, so the real failure - the replacement - was
invisible to it.
"""

from __future__ import annotations

import io
import logging
import os
from collections.abc import Iterator

import pytest
import structlog

from algotrader.common.logging import NOISY_LIBRARIES, RedactingFilter, configure_logging
from tests.conftest import CODE_ROOT

pytestmark = [pytest.mark.integration]


@pytest.fixture
def application_logging() -> Iterator[None]:
    """configure_logging() as a service would call it, every global restored."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    saved_noisy = {name: logging.getLogger(name).level for name in NOISY_LIBRARIES}
    saved_structlog = structlog.get_config()
    configure_logging(level="INFO", service="migration-probe", json_output=True)
    try:
        yield
    finally:
        structlog.contextvars.clear_contextvars()
        structlog.configure(**saved_structlog)
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        for name, level in saved_noisy.items():
            logging.getLogger(name).setLevel(level)


def upgrade_in_process(database_url: str) -> None:
    from alembic import command
    from alembic.config import Config

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        cfg = Config(str(CODE_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(CODE_ROOT / "migrations"))
        command.upgrade(cfg, "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def redacting_handlers() -> list[logging.Handler]:
    return [
        h
        for h in logging.getLogger().handlers
        if any(isinstance(f, RedactingFilter) for f in h.filters)
    ]


class TestAnInProcessMigrationKeepsTheRedactor:
    @pytest.mark.usefixtures("application_logging")
    def test_the_redacting_handler_and_level_survive(self, migrated_database: str) -> None:
        before = redacting_handlers()
        assert before, "control: configure_logging installed no redacting handler"

        upgrade_in_process(migrated_database)

        root = logging.getLogger()
        assert redacting_handlers() == before, "the migration replaced the redacting handler"
        assert root.level == logging.INFO, "the migration lowered the root level"

    @pytest.mark.usefixtures("application_logging")
    def test_a_dsn_logged_after_the_migration_is_still_redacted(
        self, migrated_database: str
    ) -> None:
        upgrade_in_process(migrated_database)

        (handler,) = redacting_handlers()
        assert isinstance(handler, logging.StreamHandler)
        captured = io.StringIO()
        original = handler.setStream(captured)
        password = "hunter2" + "MigrationProbe"
        try:
            logging.getLogger("sqlalchemy.pool").error(
                "could not connect to postgresql+psycopg://app:%s@db.internal:5432/x", password
            )
        finally:
            handler.setStream(original)  # type: ignore[arg-type]
        out = captured.getvalue()
        assert "db.internal" in out, "control: the line was not written at all"
        assert password not in out


class TestTheCommandLineStillGetsAlembicsConsole:
    def test_with_nothing_configured_alembic_ini_applies_with_a_redactor(
        self, migrated_database: str
    ) -> None:
        """The command line: nothing configured logging, so alembic.ini's
        console handler is installed - and it is given the redactor too."""
        root = logging.getLogger()
        saved_handlers, saved_level = root.handlers[:], root.level
        root.handlers.clear()
        try:
            upgrade_in_process(migrated_database)
            assert root.handlers, "alembic.ini's console handler was not installed"
            assert redacting_handlers() == root.handlers
        finally:
            root.handlers[:] = saved_handlers
            root.setLevel(saved_level)
