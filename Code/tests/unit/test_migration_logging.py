"""Alembic must not switch off application logging (found by test isolation).

``logging.config.fileConfig`` defaults to ``disable_existing_loggers=True``,
which reaches into every logger that already exists and sets ``disabled``.
Alembic runs IN-PROCESS here — the scaffold helper and the test suite both
invoke it — so the default let a migration silently kill logging for the rest
of the process.

For a system that trades real money that is close to the worst available
failure: it does not crash, it keeps working, and it stops producing the record
of what it did. It surfaced as pytest's ``caplog`` going empty for every test
that ran after a migration, which is the same defect wearing a smaller hat.
"""

from __future__ import annotations

import configparser
import logging
import logging.config
from pathlib import Path

import pytest

ENV_PY = Path(__file__).resolve().parents[2] / "migrations" / "env.py"
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


class TestFileConfigCannotSilenceTheApplication:
    def test_env_py_passes_disable_existing_loggers_false(self) -> None:
        """Structural, because the behavioural test below can only demonstrate
        the mechanism — it cannot prove env.py calls it correctly."""
        source = ENV_PY.read_text(encoding="utf-8")
        assert "fileConfig(" in source
        assert "disable_existing_loggers=False" in source, (
            "migrations/env.py calls fileConfig with the default "
            "disable_existing_loggers=True, which switches off every logger "
            "that already exists when a migration runs in-process."
        )

    @pytest.mark.skipif(not ALEMBIC_INI.exists(), reason="alembic.ini not present")
    def test_the_default_really_would_disable_an_existing_logger(self) -> None:
        """The mechanism, demonstrated rather than asserted from the docs."""
        victim = logging.getLogger("algotrader.probe.disabled_by_fileconfig")
        assert not victim.disabled

        original = logging.getLogger().handlers[:]
        try:
            logging.config.fileConfig(str(ALEMBIC_INI), disable_existing_loggers=True)
            assert victim.disabled, "fileConfig's default no longer disables loggers"

            victim.disabled = False
            logging.config.fileConfig(str(ALEMBIC_INI), disable_existing_loggers=False)
            assert not victim.disabled, "the False flag failed to protect the logger"
        finally:
            victim.disabled = False
            logging.getLogger().handlers[:] = original

    def test_alembic_ini_does_not_claim_the_algotrader_logger(self) -> None:
        """A second way to lose application logs: naming `algotrader` in
        alembic's [loggers] would let alembic own its level and handlers."""
        parser = configparser.ConfigParser()
        parser.read(ALEMBIC_INI, encoding="utf-8")
        if parser.has_option("loggers", "keys"):
            claimed = {k.strip() for k in parser.get("loggers", "keys").split(",")}
            assert "algotrader" not in claimed
