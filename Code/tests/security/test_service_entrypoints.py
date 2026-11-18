"""A service builds its engine with the constructor that checks its login (audit SEC-002).

``open_service_engine`` refuses a login that can rewrite the audit trail;
``create_engine`` does not ask. No process entrypoint exists yet (E23-S03), so
this binds the ones that will: every ``__main__.py`` under ``src`` that builds
an engine builds it through ``open_service_engine``. Vacuous today, by design -
the rule is in force before the first entrypoint is written, not after.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from sqlalchemy.exc import DBAPIError

from algotrader.common.db.privilege import LeastPrivilegeError, require_least_privilege

SRC = Path(__file__).resolve().parents[2] / "src" / "algotrader"


def test_no_entrypoint_builds_an_unchecked_engine() -> None:
    offenders = [
        str(p.relative_to(SRC))
        for p in SRC.rglob("__main__.py")
        if re.search(r"\bcreate_engine(_from_url)?\s*\(", p.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"build the engine with open_service_engine: {offenders}"


def test_a_database_that_cannot_answer_is_a_refusal() -> None:
    """An unmigrated schema or an unreadable catalogue is "could not check",
    and that is not "checked"."""

    class Unanswering:
        async def execute(self, *_args: object) -> object:
            raise DBAPIError("SELECT ...", {}, OSError("relation does not exist"))

    with pytest.raises(LeastPrivilegeError, match="could not verify"):
        asyncio.run(require_least_privilege(Unanswering()))  # type: ignore[arg-type]
