"""What a package can reach by importing - statically, transitively, without running it.

Isolation tests used to read only the imports written in the package's own
files, so ``signals -> strategy -> execution`` passed while promising that the
signals package "cannot reach an order" (audit ENG-046). This follows every
``algotrader.*`` import to its source file and on, and reports everything
reached - internal modules and third-party top-levels alike.

Imports under ``if TYPE_CHECKING:`` are skipped: they never execute, so they
reach nothing at runtime.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def imported_names(source: str, package: str) -> list[str]:
    """Every module name ``source`` imports at runtime; relative names resolved."""
    names: list[str] = []

    def visit(nodes: Iterable[ast.AST]) -> None:
        for node in nodes:
            if isinstance(node, ast.If) and _is_type_checking(node.test):
                visit(node.orelse)
                continue
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = package.split(".")[: len(package.split(".")) - node.level + 1]
                    base = ".".join([*parts, base]) if base else ".".join(parts)
                names.append(base)
                names.extend(f"{base}.{alias.name}" for alias in node.names)
            visit(ast.iter_child_nodes(node))

    visit(ast.parse(source).body)
    return names


def module_file(name: str, src: Path = SRC) -> Path | None:
    base = src.joinpath(*name.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.exists():
            return candidate
    return None


def closure(
    start: Iterable[str],
    *,
    read: Callable[[str], str | None] | None = None,
) -> set[str]:
    """Every module name reachable from ``start``. ``read`` maps an internal
    module name to its source (``None`` when it is not a module); the default
    reads ``src``. A name that is not a module - an attribute in ``from x
    import y`` - is still reported, harmlessly."""

    def default_read(name: str) -> str | None:
        path = module_file(name)
        return None if path is None else path.read_text(encoding="utf-8")

    reader = read or default_read
    seen: set[str] = set()
    todo = list(start)
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        if not name.startswith("algotrader"):
            continue
        source = reader(name)
        if source is None:
            continue
        is_package = module_file(name) is not None and module_file(name).name == "__init__.py"  # type: ignore[union-attr]
        package = name if is_package else name.rsplit(".", 1)[0]
        todo.extend(imported_names(source, package))
    return seen
