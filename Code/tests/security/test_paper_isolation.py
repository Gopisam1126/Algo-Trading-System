"""Paper and live differ in exactly one place (E15-S10, AC1) - asserted, not promised.

The build concern on this story: "one stray ``if mode == PAPER`` in the risk
path makes every paper result unrepresentative of live". So every reference to
a ``SystemMode`` member, and every comparison against a mode's string value,
is found in the source by AST and held to an allowlist:

* ``common/config.py`` - the field's default, and LIVE's static-IP validation;
* ``execution/service.py`` - TRADING_MODES, the refusal of modes that place
  no orders;
* ``broker/paper.py`` - :func:`venue_for`, the one decision, which
  :func:`trading_client` injects by and the execution service checks
  its collaborators against (audit NR-002).

A new branch anywhere else - the risk engine sizing differently on paper, the
gateway skipping a check - fails here, naming the file. And the paper client
itself is held to importing nothing that can open a connection.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "algotrader"
MODE_VALUES = {"paper", "live", "alert_only", "approval"}

#: file -> the SystemMode members it may name. Anything else is a stray branch.
ALLOWED: dict[str, set[str]] = {
    "common/config.py": {"PAPER", "LIVE"},
    "execution/service.py": {"PAPER", "LIVE"},
    "broker/paper.py": {"LIVE"},
}


def _mode_aliases(tree: ast.AST) -> set[str]:
    """Every local name bound to SystemMode: the class itself, an import
    alias (``SystemMode as SM``) and a plain assignment (``M = SystemMode``)."""
    names = {"SystemMode"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(a.asname for a in node.names if a.name == "SystemMode" and a.asname)
    changed = True
    while changed:  # an alias of an alias is an alias
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and _is_mode_class(node.value, names):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id not in names:
                        names.add(target.id)
                        changed = True
    return names


def _is_mode_class(node: ast.AST, aliases: set[str]) -> bool:
    """``SystemMode``, an alias of it, or any attribute chain ending in it
    (``enums.SystemMode``, ``algotrader.common.enums.SystemMode``)."""
    if isinstance(node, ast.Name):
        return node.id in aliases
    return isinstance(node, ast.Attribute) and node.attr == "SystemMode"


def _mode_references(source: str) -> tuple[set[str], list[str]]:
    """The SystemMode members a source names, and its comparisons to mode strings.

    Every spelling counts (audit ENG-045): the first version matched only the
    literal ``SystemMode.X``, so ``SM.LIVE`` after an import alias,
    ``enums.SystemMode.LIVE``, ``getattr(SystemMode, "LIVE")`` and a ``case
    "live":`` arm each named a mode the scan could not see.
    """
    tree = ast.parse(source)
    aliases = _mode_aliases(tree)
    members: set[str] = set()
    compares: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and _is_mode_class(node.value, aliases):
            members.add(node.attr)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and _is_mode_class(node.args[0], aliases)
            and isinstance(node.args[1], ast.Constant)
        ):
            members.add(str(node.args[1].value))
        operands: list[ast.AST] = []
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
        elif isinstance(node, ast.MatchValue):
            operands = [node.value]
        for operand in operands:
            if isinstance(operand, ast.Constant) and str(operand.value).lower() in MODE_VALUES:
                line = getattr(node, "lineno", None) or getattr(operand, "lineno", 0)
                compares.append(f"line {line}: {operand.value!r}")
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

    @pytest.mark.parametrize(
        "source",
        [
            "from algotrader.common.enums import SystemMode as SM\nif m is SM.LIVE:\n    pass\n",
            "from algotrader.common.enums import SystemMode\nM = SystemMode\nN = M\nx = N.LIVE\n",
            "from algotrader.common import enums\nif m is enums.SystemMode.LIVE:\n    pass\n",
            "import algotrader.common.enums\nx = algotrader.common.enums.SystemMode.LIVE\n",
            'from algotrader.common.enums import SystemMode\nx = getattr(SystemMode, "LIVE")\n',
            "match m:\n    case SystemMode.LIVE:\n        pass\n",
        ],
        ids=[
            "import-alias",
            "assigned-alias",
            "module-qualified",
            "fully-qualified",
            "getattr",
            "match",
        ],
    )
    def test_every_spelling_of_a_member_is_found(self, source: str) -> None:
        """The control for ENG-045: each of these named LIVE unseen."""
        assert "LIVE" in _mode_references(source)[0]

    def test_a_match_on_a_modes_string_is_found(self) -> None:
        _, compares = _mode_references('match mode:\n    case "live":\n        pass\n')
        assert compares

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
