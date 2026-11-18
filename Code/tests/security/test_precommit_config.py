"""The pre-commit barrier is one pre-commit can find, and its rules match this tree (audit SEC-010).

It lived in Code/, below the git root, so ``pre-commit install`` never found
it; its .env rule was anchored at the root and could not match Code/.env; its
safety hook ran a tests/ directory that does not exist from the root; and its
ruff was not CI's. Probed end to end on 18 Nov 2026 in a scratch repository
(a forced ``git add Code/.env`` was refused at commit, ``.env.example`` was
not); held here statically, so the config cannot drift back.
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
        hook["id"]: {**hook, "rev": repo.get("rev")}
        for repo in config["repos"]
        for hook in repo["hooks"]
    }


def _env_patterns() -> tuple[re.Pattern[str], re.Pattern[str]]:
    """The two grep -E patterns the no-env-files hook applies: block, then spare."""
    entry = _hooks()["no-env-files"]["entry"]
    block, spare = re.findall(r'grep -v?E "([^"]+)"', entry)
    return re.compile(block), re.compile(spare)


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
    block, spare = _env_patterns()
    assert (bool(block.search(path)) and not spare.search(path)) is refused


def test_ruff_is_the_version_ci_runs() -> None:
    constraints = (REPO / "Code" / "constraints.txt").read_text(encoding="utf-8")
    pinned = re.search(r"^ruff==(\S+)$", constraints, re.M)
    assert pinned is not None
    assert _hooks()["ruff"]["rev"] == f"v{pinned.group(1)}"


def test_the_safety_hook_runs_in_code() -> None:
    entry = _hooks()["safety-invariants"]["entry"]
    assert "cd Code &&" in entry
    assert (REPO / "Code" / "tests" / "security").is_dir()


def test_the_secret_scan_hook_is_present() -> None:
    assert "gitleaks" in _hooks()
