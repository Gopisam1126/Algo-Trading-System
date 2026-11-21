"""pre-commit hook: the design documents make no claim the tree has falsified.

Runs ``Code/tests/unit/test_doc_claims.py`` - the audit W8 checks (no
hand-written test counts, no denial of built code, B1 not called closed, India
hosting not cited as a regulation, the empty packages named correctly) - over
the design documents as well as the files every branch carries.

Why a hook and not only the test: QA promotion strips ``Documents/``, and
nothing that runs on QA may read a stripped path
(``Code/tests/security/test_promotion_hygiene.py``). So the suite under
``Code/`` checks the READMEs, ``Code/CLAUDE.md`` and ``Code/SETUP.md`` on every
branch, and this hook - DEV-side, outside ``Code/`` - hands it the design
documents through ``DOC_CLAIMS_EXTRA``. PRE_LIVE_CHECKLIST.md, the gate before
real capital, is among them: it still called B1 closed five days after the
audit reopened it (E24-S09).

Python rather than ``bash -c``, for the reason ``safety_invariants.py`` gives.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CODE = ROOT / "Code"

#: The design documents the checks cover, relative to the repository root.
DOCUMENTS = [
    f"Documents/{name}.md"
    for name in (
        "MASTER_REFERENCE",
        "ARCHITECTURE_RESEARCH",
        "INDIA_FEATURES_AND_CONFIG",
        "LOW_LEVEL_ARCHITECTURE",
        "MVP_UI_AND_LEGAL",
        "STRATEGY_ENGINE",
        "VERIFICATION_REPORT",
        "PRE_LIVE_CHECKLIST",
        "DEPLOYMENT",
    )
]


def environment() -> dict[str, str]:
    return {**os.environ, "DOC_CLAIMS_EXTRA": os.pathsep.join(DOCUMENTS)}


def main() -> int:
    python = next(
        (p for p in (CODE / ".venv" / "Scripts" / "python.exe", CODE / ".venv" / "bin" / "python") if p.exists()),
        None,
    )
    if python is None:
        print("doc-claims: no project venv at Code/.venv - create it (see Code/README.md).")
        return 1
    command = [str(python), "-m", "pytest", "tests/unit/test_doc_claims.py", "-q", "-p", "no:cacheprovider"]
    return subprocess.run(command, cwd=CODE, env=environment(), check=False).returncode  # noqa: S603 - fixed


if __name__ == "__main__":
    sys.exit(main())
