"""Every service command names a process that can start, or is listed as one that cannot yet.

Audit ARC-001 / ENG-015 (10 Nov 2026): docker-compose and the image ran
``python -m algotrader.<pkg>`` against packages with no ``__main__`` - and one
module that does not exist - while DEPLOYMENT.md presented ``make up`` as a
working stack, and the image HEALTHCHECK only imported the package, so nothing
could tell. The entrypoints are E23-S03's to build.

Until they exist this file is the CHECKED form of "no service starts":
``PENDING`` is exactly the commands that cannot start. A command that becomes
runnable fails here, so the documents that say otherwise (DEPLOYMENT.md §6,
Code/README.md, MASTER_REFERENCE.md §2) change in the same commit, and a new
command that cannot start is refused unless it is listed with its story.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

CODE = Path(__file__).resolve().parents[2]
COMPOSE = CODE / "ops" / "docker-compose.yml"
DOCKERFILE = CODE / "ops" / "Dockerfile"

#: ``python -m`` targets that cannot start yet, and the story that makes each start.
PENDING: dict[str, str] = {
    "algotrader.orchestrator": "E23-S03",  # also the image CMD
    "algotrader.api": "E23-S03",
    "algotrader.notifier": "E23-S03",
    "algotrader.ingest": "E23-S03",
    "algotrader.signals": "E23-S03",
    "algotrader.macro": "E23-S03",
    "algotrader.execution": "E23-S03",
    "algotrader.premarket": "E23-S03",
    "algotrader.strategy.service": "E23-S03 (the module does not exist)",
}


def service_commands() -> dict[str, str]:
    """``python -m`` target -> where it is named (compose service or the image CMD)."""
    found: dict[str, str] = {}
    service = ""
    for line in COMPOSE.read_text(encoding="utf-8").splitlines():
        if m := re.match(r"^  ([a-z][a-z0-9-]+):\s*$", line):
            service = m.group(1)
        if m := re.match(r"^\s+command:\s*python -m (\S+)\s*$", line):
            found[m.group(1)] = f"compose {service}"
    cmd = re.search(
        r'^CMD \["python", "-m", "([^"]+)"\]', DOCKERFILE.read_text(encoding="utf-8"), re.M
    )
    assert cmd, "the image CMD is not `python -m <module>` - update this parser"
    found.setdefault(cmd.group(1), "image CMD")
    return found


def can_start(module: str) -> bool:
    """Would ``python -m module`` run something, rather than fail or import and exit?"""
    try:
        spec = importlib.util.find_spec(module)
    except ModuleNotFoundError:  # a parent package is missing
        return False
    if spec is None:
        return False
    if spec.submodule_search_locations is not None:  # a package: needs __main__
        return importlib.util.find_spec(f"{module}.__main__") is not None
    origin = Path(str(spec.origin))
    return bool(
        re.search(r"""__name__\s*==\s*["']__main__["']""", origin.read_text(encoding="utf-8"))
    )


def test_the_parser_finds_every_service_command() -> None:
    """Control: an empty parse would make every other test here vacuous."""
    commands = service_commands()
    services = re.findall(r"^\s+command:\s*python -m ", COMPOSE.read_text(encoding="utf-8"), re.M)
    assert len(services) == 9
    assert "algotrader.orchestrator" in commands


@pytest.mark.parametrize("module", sorted(service_commands()))
def test_every_command_starts_or_is_listed_pending(module: str) -> None:
    assert can_start(module) or module in PENDING, (
        f"{service_commands()[module]} runs `python -m {module}`, which cannot start "
        "and is not in PENDING with the story that builds it"
    )


@pytest.mark.parametrize("module", sorted(PENDING))
def test_a_pending_command_really_cannot_start(module: str) -> None:
    """When E23-S03 gives a service its entrypoint, this fails: remove it from
    PENDING and correct DEPLOYMENT.md §6, Code/README.md and MASTER_REFERENCE §2."""
    assert not can_start(module), f"{module} can start now - update PENDING and the docs"


def test_pending_names_only_commands_something_runs() -> None:
    assert set(PENDING) <= set(service_commands())


def test_the_probe_tells_an_entrypoint_from_a_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control for ``can_start``: a probe that always said False would pass every
    pending test above."""
    for name, files in {
        "runs_pkg": {"__init__.py": "", "__main__.py": "print('up')\n"},
        "bare_pkg": {"__init__.py": '"""placeholder."""\n'},
    }.items():
        (tmp_path / name).mkdir()
        for file, text in files.items():
            (tmp_path / name / file).write_text(text, encoding="utf-8")
    (tmp_path / "guarded.py").write_text('if __name__ == "__main__":\n    pass\n', encoding="utf-8")
    (tmp_path / "unguarded.py").write_text("X = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert can_start("runs_pkg")
    assert can_start("guarded")
    assert not can_start("bare_pkg")
    assert not can_start("unguarded")
    assert not can_start("no_such_module_here")
    assert not can_start("bare_pkg.no_such_child")
