"""Where a run must be complete, no Docker is a failure, not a skip (audit ENG-036).

pytest exits 0 when tests are skipped, and every container-backed test skips
when the Docker daemon cannot be reached - so a run on a machine with Docker
Desktop closed read as green. With ``REQUIRE_DOCKER=1`` (set in CI and in the
QA gate) the same run now fails at collection.

Each case runs pytest in a CHILD process with ``DOCKER_HOST`` pointed at a
closed port - an unreachable daemon - over one container-backed class.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

CODE = Path(__file__).resolve().parents[2]
TARGET = "tests/security/test_compose_secrets.py::TestWhatComposeActuallyRenders"


def _run(require: str | None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, DOCKER_HOST="tcp://127.0.0.1:9")
    env.pop("REQUIRE_DOCKER", None)
    if require is not None:
        env["REQUIRE_DOCKER"] = require
    return subprocess.run(  # this interpreter, a fixed test id
        [sys.executable, "-m", "pytest", TARGET, "-q", "-p", "no:cacheprovider"],
        cwd=CODE,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def test_an_unreachable_docker_fails_the_run_when_required() -> None:
    done = _run("1")
    assert done.returncode != 0
    assert "REQUIRE_DOCKER=1 and Docker is not reachable" in done.stdout + done.stderr


def test_the_control_without_the_switch_it_is_a_skip() -> None:
    """A developer's partial run still works: the switch, not the default, fails."""
    done = _run(None)
    assert done.returncode == 0, done.stdout[-2000:]
    assert "skipped" in done.stdout
