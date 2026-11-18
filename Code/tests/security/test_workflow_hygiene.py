"""The workflows run only pinned code, with the least token, on data they never execute.

Three findings of the 10 Nov 2026 audit, each held here so it cannot return:

- **SEC-003** - every action was referenced by a mutable tag (``@v4``). A tag
  is moved by whoever controls the action's repository; the steps run with
  this repository's token. Every ``uses:`` is now a 40-hex commit SHA.
- **SEC-008** - ``promote.yml`` expanded the free-text ``reason`` input
  straight into a bash script. A backtick ran as shell in a job holding a
  contents:write token. No ``${{ }}`` expression is expanded inside a
  ``run:`` or ``script:`` now; values arrive through ``env:``.
- **SEC-009** - ``ci.yml`` declared no ``permissions:``, so its token had the
  repository default. It reads the repository and nothing else now.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = sorted((REPO / ".github" / "workflows").glob("*.yml"))
PINNED = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


def _load(path: Path) -> dict[Any, Any]:
    return dict(yaml.safe_load(path.read_text(encoding="utf-8")))


def _steps(path: Path) -> list[dict[str, Any]]:
    return [s for job in _load(path)["jobs"].values() for s in job.get("steps", [])]


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit(workflow: Path) -> None:
    unpinned = [s["uses"] for s in _steps(workflow) if "uses" in s and not PINNED.match(s["uses"])]
    assert not unpinned, f"{workflow.name}: pin to a full commit SHA: {unpinned}"


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_each_pin_says_which_release_it_is(workflow: Path) -> None:
    """A bare SHA is unreviewable; the comment is what a reader checks."""
    text = workflow.read_text(encoding="utf-8")
    for line in text.splitlines():
        if re.search(r"\buses:\s", line):
            assert re.search(r"@[0-9a-f]{40} # v\d+\.\d+\.\d+$", line), line.strip()


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_no_expression_is_expanded_into_a_script(workflow: Path) -> None:
    offenders = []
    for step in _steps(workflow):
        for body in (step.get("run"), (step.get("with") or {}).get("script")):
            if isinstance(body, str) and "${{" in body:
                offenders.append(step.get("name") or step.get("uses"))
    assert not offenders, f"{workflow.name}: pass values through env:, not into: {offenders}"


def test_ci_reads_the_repository_and_nothing_more() -> None:
    ci = _load(REPO / ".github" / "workflows" / "ci.yml")
    assert ci["permissions"] == {"contents": "read"}
    for name, job in ci["jobs"].items():
        for scope, level in (job.get("permissions") or {}).items():
            assert level == "read", f"job {name} asks for {scope}: {level}"


def test_ci_fails_rather_than_skips_without_docker() -> None:
    """ENG-036: a skipped container suite exits 0."""
    assert _load(REPO / ".github" / "workflows" / "ci.yml")["env"]["REQUIRE_DOCKER"] == "1"


def test_dependabot_keeps_the_pins_current() -> None:
    config = yaml.safe_load((REPO / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    assert any(u["package-ecosystem"] == "github-actions" for u in config["updates"])


def test_the_control_a_tag_and_an_expansion_are_caught() -> None:
    """The rules above, on lines that break them - so a regex that matches
    nothing cannot pass them."""
    assert not PINNED.match("actions/checkout@v4")
    assert PINNED.match("actions/checkout@" + "a" * 40)
    step = {"run": 'git merge -m "${{ inputs.reason }}"'}
    assert "${{" in step["run"]
