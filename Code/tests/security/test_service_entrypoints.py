"""A service builds its engine with the constructor that checks its login (audit SEC-002).

``open_service_engine`` refuses a login that can rewrite the audit trail;
``create_engine`` does not ask. No process entrypoint exists yet (E23-S03), so
this binds the ones that will: every ``__main__.py`` under ``src`` that builds
an engine builds it through ``open_service_engine``. Vacuous today, by design -
the rule is in force before the first entrypoint is written, not after.

And every process that reaches PostgreSQL - a ``__main__`` or a script under
``scripts/`` - sets the Windows event-loop policy BEFORE its loop exists
(``common/db/eventloop.py``). Not vacuous: the bhavcopy command is one, and it
shipped without the call because its test ran in a process that had already
made it (SIT-011).
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from sqlalchemy.exc import DBAPIError

from algotrader.common.db.privilege import LeastPrivilegeError, require_least_privilege

SRC = Path(__file__).resolve().parents[2] / "src" / "algotrader"
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

#: What marks a process as one that reaches PostgreSQL through async psycopg.
_TOUCHES_DATABASE = re.compile(r"\b(create_engine(_from_url)?|open_service_engine)\s*\(")


def test_no_entrypoint_builds_an_unchecked_engine() -> None:
    offenders = [
        str(p.relative_to(SRC))
        for p in SRC.rglob("__main__.py")
        if re.search(r"\bcreate_engine(_from_url)?\s*\(", p.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"build the engine with open_service_engine: {offenders}"


def _policy_missing_or_late(text: str) -> bool:
    """True when ``text`` starts an event loop that reaches the database without
    setting the loop policy first. A process that never reaches PostgreSQL
    (``scripts/halt.py`` talks to Redis, which runs on any loop) is not judged."""
    run = text.find("asyncio.run(")
    if run < 0 or not _TOUCHES_DATABASE.search(text):
        return False
    policy = text.find("configure_event_loop_policy()")
    return policy < 0 or policy > run


def test_every_database_entrypoint_sets_the_loop_policy_before_its_loop() -> None:
    """On Windows ``asyncio.run`` builds a ProactorEventLoop, and async psycopg
    refuses it at the first query - a crash far from its cause, and invisible on
    Linux CI. The test process sets the policy for itself (tests/conftest.py),
    so a command's in-process test cannot see the omission (SIT-011)."""
    entrypoints = [*SRC.rglob("__main__.py"), *SCRIPTS.glob("*.py")]
    judged = [p for p in entrypoints if "asyncio.run(" in p.read_text(encoding="utf-8")]
    assert any(p.name == "backfill_bhavcopy.py" for p in judged), "the scan found no command"
    offenders = [p.name for p in judged if _policy_missing_or_late(p.read_text(encoding="utf-8"))]
    assert not offenders, f"call configure_event_loop_policy() before asyncio.run: {offenders}"


def test_the_policy_check_sees_a_missing_or_late_call() -> None:
    """The control: a check that passes everything would pass the test above."""
    engine, run, policy = (
        "engine = create_engine_from_url(url)\n",
        "asyncio.run(go())\n",
        "configure_event_loop_policy()\n",
    )
    assert _policy_missing_or_late(engine + run)
    assert _policy_missing_or_late(engine + run + policy)
    assert not _policy_missing_or_late(engine + policy + run)
    assert not _policy_missing_or_late("client = build_client(cfg)\n" + run)


def test_a_database_that_cannot_answer_is_a_refusal() -> None:
    """An unmigrated schema or an unreadable catalogue is "could not check",
    and that is not "checked"."""

    class Unanswering:
        async def execute(self, *_args: object) -> object:
            raise DBAPIError("SELECT ...", {}, OSError("relation does not exist"))

    with pytest.raises(LeastPrivilegeError, match="could not verify"):
        asyncio.run(require_least_privilege(Unanswering()))  # type: ignore[arg-type]
