"""The runtime dependency list is what the deployed code runs on (audit ENG-039).

Thirteen declared runtime dependencies - an AI SDK, a web framework, a
scheduler, an HTTP client and more - were imported by nothing in src, so every
image shipped them and every dependency audit had to triage CVEs in code that
never ran. They are extras now. Held both ways:

- every runtime dependency is imported by src, or is one of the two it needs
  WITHOUT importing (psycopg, loaded by SQLAlchemy from the URL scheme; tzdata,
  zoneinfo's data where the OS has none);
- every third-party module src imports is a declared runtime dependency, so
  nothing reaches production as someone else's transitive dependency.
"""

from __future__ import annotations

import ast
import importlib.metadata as md
import re
import sys
import tomllib
from pathlib import Path

CODE = Path(__file__).resolve().parents[2]
SRC = CODE / "src" / "algotrader"

#: Needed at runtime and never imported by name, each for a stated reason.
NEEDED_WITHOUT_IMPORT = {
    "psycopg": "SQLAlchemy's driver, loaded from postgresql+psycopg:// URLs",
    "tzdata": "zoneinfo's database on a slim image and on Windows",
}


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _runtime() -> set[str]:
    project = tomllib.loads((CODE / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return {_norm(re.split(r"[\s\[<>=;!~]", d, maxsplit=1)[0]) for d in project["dependencies"]}


def _imported_top_levels() -> set[str]:
    found: set[str] = set()
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                found.add(node.module.split(".")[0])
    return found - set(sys.stdlib_module_names) - {"algotrader", "__future__"}


def _distributions_of(modules: set[str]) -> dict[str, set[str]]:
    owners = md.packages_distributions()
    return {m: {_norm(d) for d in owners.get(m, [])} for m in modules}


def test_every_runtime_dependency_is_used_by_src() -> None:
    imported = set().union(*_distributions_of(_imported_top_levels()).values())
    unused = _runtime() - imported - set(NEEDED_WITHOUT_IMPORT)
    assert not unused, f"declared at runtime, imported by no module in src: {sorted(unused)}"


def test_every_third_party_import_in_src_is_declared() -> None:
    runtime = _runtime()
    undeclared = {
        module: sorted(dists)
        for module, dists in _distributions_of(_imported_top_levels()).items()
        if not dists & runtime
    }
    assert not undeclared, f"src imports packages it does not declare: {undeclared}"


def test_the_exceptions_are_still_declared() -> None:
    """The control: an allowlist entry for a package no longer declared would
    excuse nothing and hide nothing - it must name a real runtime entry."""
    assert set(NEEDED_WITHOUT_IMPORT) <= _runtime()


def test_the_planned_service_packages_are_extras_not_runtime() -> None:
    project = tomllib.loads((CODE / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extras = project["optional-dependencies"]
    for extra, package in (("ai", "anthropic"), ("api", "fastapi"), ("scheduler", "apscheduler")):
        assert any(_norm(d).startswith(package) for d in extras[extra]), (extra, package)
        assert package not in _runtime(), package
