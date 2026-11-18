"""SQL echo prints bound parameters past the log redactor: development only, and said.

Audit NR-005 / NR-010 (17 Nov 2026): the guard read an unset ``ALGOTRADER_ENV``
as ``development``, so a deployment that forgot the variable could turn echo on.
Nothing tested the guard at all.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from algotrader.common.config import DatabaseConfig


class TestEchoNeedsAnExplicitDevelopment:
    @pytest.mark.parametrize("environment", [None, "", "production", "qa", "Development"])
    def test_echo_is_refused_unless_development_is_set(
        self, monkeypatch: pytest.MonkeyPatch, environment: str | None
    ) -> None:
        if environment is None:
            monkeypatch.delenv("ALGOTRADER_ENV", raising=False)
        else:
            monkeypatch.setenv("ALGOTRADER_ENV", environment)
        with pytest.raises(ValidationError, match="ALGOTRADER_ENV=development"):
            DatabaseConfig(echo_sql=True)

    def test_the_control_explicit_development_may_echo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ALGOTRADER_ENV", "development")
        assert DatabaseConfig(echo_sql=True).echo_sql is True

    def test_no_echo_is_fine_anywhere(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ALGOTRADER_ENV", raising=False)
        assert DatabaseConfig(echo_sql=False).echo_sql is False
