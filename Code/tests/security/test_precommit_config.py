"""The pre-commit barrier is one pre-commit can find, and its rules match this tree (audit SEC-010).

It lived in Code/, below the git root, so ``pre-commit install`` never found
it; its .env rule was anchored at the root and could not match Code/.env; its
safety hook ran a tests/ directory that does not exist from the root; and its
ruff was not CI's. Held here statically, so the config cannot drift back.

The owner's first install (18 Nov 2026) then found two more, both in the
config that fixed the first four:

- the local hooks ran ``bash -c``, and on Windows ``bash`` on the PATH was WSL's
  launcher, which failed both with ``execvpe(/bin/bash)``;
- the ruff hook passed ``--config Code/pyproject.toml`` and ``--fix``. Passed
  that way, the config's paths resolve against the repository root, so the
  project's own package and per-file rules were lost, and ``--fix`` rewrote
  92 files into an import order CI rejects.

Each is held below. The .env rule was probed end to end in a scratch
repository: a forced ``git add Code/.env`` was refused at commit.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
CONFIG = REPO / ".pre-commit-config.yaml"


def _hooks() -> dict[str, dict[str, Any]]:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    return {
        hook["id"]: {**hook, "rev": repo.get("rev"), "repo": repo["repo"]}
        for repo in config["repos"]
        for hook in repo["hooks"]
    }


def test_the_config_is_at_the_git_root_and_only_there() -> None:
    assert CONFIG.exists()
    assert not (REPO / "Code" / ".pre-commit-config.yaml").exists()


@pytest.mark.parametrize(
    ("path", "refused"),
    [
        ("Code/.env", True),
        (".env", True),
        ("Code/.env.local", True),
        ("deep/x/.env.prod", True),
        ("Code/.env.example", False),  # the template is meant to be committed
        (".env.example", False),
        ("Code/src/algotrader/common/envelope.py", False),
        ("Code/.envrc.md", False),
    ],
)
def test_the_env_rule_matches_at_any_depth(path: str, refused: bool) -> None:
    """pre-commit selects a hook's files with re.search on `files`, minus
    `exclude`; a `fail` hook refuses every file it selects."""
    hook = _hooks()["no-env-files"]
    assert hook["language"] == "fail"
    selected = bool(re.search(hook["files"], path)) and not re.search(hook["exclude"], path)
    assert selected is refused


def test_no_local_hook_needs_a_shell() -> None:
    for hook_id, hook in _hooks().items():
        if hook["repo"] == "local":
            assert not re.search(r"\b(bash|sh|cmd|powershell)\b", str(hook.get("entry", ""))), (
                hook_id
            )


def test_ruff_is_the_version_ci_runs() -> None:
    constraints = (REPO / "Code" / "constraints.txt").read_text(encoding="utf-8")
    pinned = re.search(r"^ruff==(\S+)$", constraints, re.M)
    assert pinned is not None
    assert _hooks()["ruff"]["rev"] == f"v{pinned.group(1)}"


def test_ruff_only_checks_and_finds_the_projects_config_itself() -> None:
    hooks = _hooks()
    for hook_id in ("ruff", "ruff-format"):
        args = hooks[hook_id].get("args", [])
        assert "--fix" not in args, f"{hook_id} must not rewrite the owner's files"
        assert "--config" not in args, f"{hook_id}: --config resolves its paths from the root"
    assert "--check" in hooks["ruff-format"].get("args", [])


def test_the_safety_hook_runs_the_projects_tests_in_code() -> None:
    hook = _hooks()["safety-invariants"]
    script = REPO / "scripts" / "hooks" / "safety_invariants.py"
    assert hook["entry"] == "python scripts/hooks/safety_invariants.py"
    text = script.read_text(encoding="utf-8")
    assert '"tests/security"' in text and "cwd=CODE" in text
    assert (REPO / "Code" / "tests" / "security").is_dir()


def test_the_secret_scan_hook_is_present() -> None:
    assert "gitleaks" in _hooks()
