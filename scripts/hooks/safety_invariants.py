"""pre-commit hook: the safety-invariant tests must pass before a commit exists.

Runs ``Code/tests/security`` - the tests that need no container - with the
PROJECT's interpreter (``Code/.venv``), from ``Code/``, exactly as CI's
Security job does minus the Docker-backed tests (a hook that needs Docker is a
hook people bypass; CI runs those).

Python rather than ``bash -c``: on Windows ``bash`` on the PATH is often WSL's
launcher, which fails with ``execvpe(/bin/bash)`` when no Linux distribution
is installed - the first version of this hook did exactly that (18 Nov 2026).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

CODE = Path(__file__).resolve().parents[2] / "Code"


def project_python() -> Path | None:
    for candidate in (CODE / ".venv" / "Scripts" / "python.exe", CODE / ".venv" / "bin" / "python"):
        if candidate.exists():
            return candidate
    return None


def main() -> int:
    python = project_python()
    if python is None:
        print(
            "safety-invariants: no project venv at Code/.venv - create it (see Code/README.md) "
            "so the hook runs the interpreter CI runs, not whichever is on the PATH."
        )
        return 1
    command = [str(python), "-m", "pytest", "tests/security", "-q", "-m", "not integration",
               "-p", "no:cacheprovider"]  # fmt: skip
    return subprocess.run(command, cwd=CODE, check=False).returncode  # noqa: S603 - fixed


if __name__ == "__main__":
    sys.exit(main())
