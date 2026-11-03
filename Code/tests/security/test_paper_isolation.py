"""Paper and live differ in exactly one place (E15-S10, AC1) - asserted, not promised.

The build concern on this story: "one stray ``if mode == PAPER`` in the risk
path makes every paper result unrepresentative of live". So every reference to
a ``SystemMode`` member, and every comparison against a mode's string value,
is found in the source by AST and held to an allowlist:

* ``common/config.py`` - the field's default, and LIVE's static-IP validation;
* ``execution/service.py`` - TRADING_MODES, the refusal of modes that place
  no orders;
* ``broker/paper.py`` - :func:`trading_client`, the one injection.

A new branch anywhere else - the risk engine sizing differently on paper, the
gateway skipping a check - fails here, naming the file. And the paper client
itself is held to importing nothing that can open a connection.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "algotrader"
MODE_VALUES = {"paper", "live", "alert_only", "approval"}

#: file -> the SystemMode members it may name. Anything else is a stray branch.
ALLOWED: dict[str, set[str]] = {
    "common/config.py": {"PAPER", "LIVE"},
    "execution/service.py": {"PAPER", "LIVE"},
    "broker/paper.py": {"LIVE"},
}


def _mode_references(source: str) -> tuple[set[str], list[str]]:
    """The SystemMode members a source names, and its comparisons to mode strings."""
    members: set[str] = set()
    compares: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "SystemMode"
        ):
            members.add(node.attr)
        if isinstance(node, ast.Compare):
            for operand in [node.left, *node.comparators]:
                if isinstance(operand, ast.Constant) and str(operand.value).lower() in MODE_VALUES:
                    compares.append(f"line {node.lineno}: {operand.value!r}")
    return members, compares


def _sources() -> dict[str, str]:
    return {
        path.relative_to(SRC).as_posix(): path.read_text(encoding="utf-8")
        for path in SRC.rglob("*.py")
        if path.relative_to(SRC).as_posix() != "common/enums.py"  # the definition
    }


class TestModeBranchesAreConfined:
    def test_only_the_allowlisted_files_name_a_mode(self) -> None:
        offenders = []
        for rel, source in _sources().items():
            members, _ = _mode_references(source)
            stray = members - ALLOWED.get(rel, set())
            if stray:
                offenders.append(f"{rel}: SystemMode.{sorted(stray)}")
        assert not offenders, (
            f"mode branches outside the one injection: {offenders}. Paper must run "
            f"the live code; make the difference a client handed to the adapter."
        )

    def test_no_code_compares_against_a_modes_string_value(self) -> None:
        """`config.system.mode.value == "paper"` is the same branch, spelled to
        slip past the member scan."""
        offenders = {
            rel: compares
            for rel, source in _sources().items()
            if (compares := _mode_references(source)[1])
        }
        assert not offenders, offenders

    def test_the_injection_branches_on_live_and_nothing_else(self) -> None:
        source = (SRC / "broker" / "paper.py").read_text(encoding="utf-8")
        assert _mode_references(source)[0] == {"LIVE"}


class TestTheScanSeesABranch:
    """The control: a scanner that finds nothing would pass the tests above forever."""

    def test_a_member_branch_is_found(self) -> None:
        members, _ = _mode_references("if mode is SystemMode.PAPER:\n    size = 0\n")
        assert members == {"PAPER"}

    def test_a_string_branch_is_found(self) -> None:
        _, compares = _mode_references('if config.system.mode.value == "paper":\n    pass\n')
        assert compares == ["line 1: 'paper'"]

    def test_an_unrelated_string_is_not(self) -> None:
        _, compares = _mode_references('if name == "papers":\n    pass\n')
        assert compares == []


class TestThePaperClientCannotReachTheBroker:
    def test_it_imports_only_kites_exception_types(self) -> None:
        tree = ast.parse((SRC / "broker" / "paper.py").read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.update(f"{node.module}.{alias.name}" for alias in node.names)
        kite = {name for name in imported if name.startswith("kiteconnect")}
        assert kite == {"kiteconnect.exceptions"}, kite
        network = {
            n
            for n in imported
            if n.split(".")[0]
            in {
                "requests",
                "httpx",
                "urllib",
                "urllib3",
                "socket",
                "aiohttp",
                "redis",
                "websockets",
            }
        }
        assert not network, f"the paper client imports a network library: {network}"
