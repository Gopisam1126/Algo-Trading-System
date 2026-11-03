"""E12-S01 - the strategy registry, attacked.

Three questions, each with its control:

1. **Can registering a strategy arm it?** No write path in the registry sets
   a state, and every row it writes is DRAFT with no approval - for any
   document, from any origin.
2. **Can a stored row become something other than what was registered, and
   still run?** The load re-derives the row and refuses a mismatch; hostile
   stored values are escaped and bounded before they reach a log.
3. **Can the registry be made to run forever or touch the ORM?** Lineage is
   bounded, and the strategy package imports no SQLAlchemy.

The database half - the grants that make a stored document immutable - is
``tests/integration/test_strategy_registry_db.py``.
"""

from __future__ import annotations

import ast
import copy
import datetime as dt
import inspect
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from algotrader.common.db.repositories import StrategyRepository
from algotrader.common.enums import StrategyOrigin, StrategyState
from algotrader.common.text import MAX_DETAIL
from algotrader.strategy.dsl import StrategyDocument, load_strategy_yaml
from algotrader.strategy.registry import (
    MAX_LINEAGE_DEPTH,
    CorruptStrategyRowError,
    StrategyRegistry,
    StrategyStore,
    row_for,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "algotrader"
SEED = Path(__file__).resolve().parents[2] / "config" / "strategies" / "orb_classic.yaml"
T0 = dt.datetime(2026, 11, 3, 3, 0, tzinfo=dt.UTC)


def _seed() -> StrategyDocument:
    return load_strategy_yaml(SEED.read_text(encoding="utf-8"))


class _Rows:
    """A store with no guarantees at all: what a hand-written row can be."""

    def __init__(self, *rows: dict[str, Any]) -> None:
        self.rows = {r["id"]: copy.deepcopy(r) for r in rows}
        self.inserted: list[dict[str, Any]] = []

    async def insert_strategy(self, row: dict[str, Any]) -> bool:
        self.inserted.append(copy.deepcopy(row))
        self.rows[row["id"]] = copy.deepcopy(row)
        return True

    async def strategy_by_id(self, strategy_id: str) -> dict[str, Any] | None:
        return copy.deepcopy(self.rows.get(strategy_id))

    async def strategy_by_hash(self, dsl_hash: str) -> dict[str, Any] | None:
        return next(
            (copy.deepcopy(r) for r in self.rows.values() if r["dsl_hash"] == dsl_hash), None
        )

    async def all_strategies(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(r) for r in self.rows.values()]


class TestRegistrationNeverArms:
    @settings(max_examples=40, deadline=None)
    @given(
        origin=st.sampled_from(list(StrategyOrigin)), created_by=st.text(min_size=1, max_size=64)
    )
    def test_every_row_the_registry_writes_is_an_unapproved_draft(
        self, origin: StrategyOrigin, created_by: str
    ) -> None:
        doc = _seed().model_copy(update={"origin": origin, "created_by": created_by})
        row = row_for(doc, now=T0)
        assert row["state"] == StrategyState.DRAFT.value
        assert row["approved_by"] is None and row["approved_at"] is None
        assert row["retirement_reason"] is None

    async def test_a_document_claiming_approval_is_still_stored_as_a_draft(self) -> None:
        """There is no field to claim it with - the document is forbidden
        extras - and the row is built from the registry's own constants."""
        data = _seed().model_dump(mode="json")
        with pytest.raises(ValueError, match="Extra inputs"):
            StrategyDocument.model_validate({**data, "state": "ACTIVE", "approved_by": "me"})

        store = _Rows()
        await StrategyRegistry(store).register(_seed(), now=T0)
        assert [r["state"] for r in store.inserted] == ["DRAFT"]

    def test_the_store_offers_no_way_to_change_a_row(self) -> None:
        """State changes are E12-S02's and go through the lifecycle columns.
        The registry's store contract is insert and read; the repository
        implementing it has nothing else public."""

        def public(cls: type) -> set[str]:
            return {
                n for n, _ in inspect.getmembers(cls, inspect.isfunction) if not n.startswith("_")
            }

        contract = {"insert_strategy", "strategy_by_id", "strategy_by_hash", "all_strategies"}
        assert public(StrategyStore) == contract
        assert public(StrategyRepository) == contract


class TestAStoredRowCannotRunAsSomethingElse:
    async def test_an_unknown_primitive_written_into_a_row_never_runs(self) -> None:
        """Elevation: the stored document is data; a name outside the vetted
        vocabulary is refused, not looked up."""
        row = row_for(_seed(), now=T0)
        row["state"] = "SHADOW"
        row["dsl"]["entry"]["all_of"][0]["primitive"] = "__import__"
        loaded = await StrategyRegistry(_Rows(row)).load()
        assert loaded.runnable == ()
        assert "unknown primitive" in loaded.rejected["orb_classic"]

    @pytest.mark.parametrize(
        ("column", "hostile", "path"),
        [
            pytest.param(
                "state",
                "SHADOW\n2026-11-03 INFO forged log line " + "x" * 100_000,
                "not a strategy state",
                id="state",
            ),
            pytest.param("dsl_hash", "\x1b[2J\n" + "y" * 100_000, "hashes to", id="hash"),
            pytest.param(
                "level", "\x1b[2J\nforged " + "z" * 100_000, "does not compile", id="document"
            ),
        ],
    )
    async def test_hostile_stored_text_is_escaped_and_bounded_before_it_is_reported(
        self, caplog: pytest.LogCaptureFixture, column: str, hostile: str, path: str
    ) -> None:
        """Each case reaches a different refusal, asserted, so the bound is
        checked on the path that echoes that column."""
        row = row_for(_seed(), now=T0)
        if column == "level":
            row["dsl"]["entry"]["all_of"][0]["params"]["level"] = hostile
        else:
            row[column] = hostile
        loaded = await StrategyRegistry(_Rows(row)).load()

        reason = loaded.rejected["orb_classic"]
        assert path in reason
        assert len(reason) <= MAX_DETAIL
        assert "\n" not in reason and "\x1b" not in reason
        assert caplog.records, "the refusal was not logged"
        for record in caplog.records:
            message = record.getMessage()
            assert "\n" not in message and "\x1b" not in message
            assert len(message) <= 2 * MAX_DETAIL

    async def test_the_control_a_clean_row_runs(self) -> None:
        """Without this, the refusals above could be a registry that refuses
        everything. (orb_classic cannot run in this deployment - it asks for
        1d/1w agreement - so the control uses a document that can.)"""
        doc = _seed()
        data = doc.model_dump(mode="json")
        data["entry"]["all_of"][2]["params"]["of"] = "5m,15m,1h"
        runnable_doc = StrategyDocument.model_validate(data)
        row = row_for(runnable_doc, now=T0)
        row["state"] = "SHADOW"
        loaded = await StrategyRegistry(_Rows(row)).load()
        assert [r.strategy_id for r in loaded.runnable] == ["orb_classic"]


class TestNothingUnboundedNothingOrm:
    async def test_a_lineage_too_long_to_be_real_is_refused_not_walked_forever(self) -> None:
        rows = []
        for i in range(MAX_LINEAGE_DEPTH + 5):
            rows.append(
                {"id": f"s{i}", "parent_id": f"s{i + 1}", "dsl_hash": f"{i:064x}", "version": 1}
            )
        rows.append({"id": f"s{MAX_LINEAGE_DEPTH + 5}", "parent_id": None, "dsl_hash": "f" * 64})
        with pytest.raises(CorruptStrategyRowError, match="loops"):
            await StrategyRegistry(_Rows(*rows)).lineage("s0")

    def test_the_strategy_package_imports_no_sqlalchemy(self) -> None:
        """The repository boundary (E01-S02), for the package this story adds
        a persistence consumer to. Asserted on the AST, so an import inside a
        function or behind TYPE_CHECKING is caught too."""
        offenders = []
        for path in sorted((SRC / "strategy").rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                offenders += [
                    f"{path.name}: {n}"
                    for n in names
                    if n.split(".")[0] == "sqlalchemy" or n.startswith("algotrader.common.db")
                ]
        assert offenders == []

    def test_the_boundary_check_would_see_an_orm_import(self, tmp_path: Path) -> None:
        """The control for the walk above."""
        probe = tmp_path / "probe.py"
        probe.write_text("def f():\n    from sqlalchemy import select\n", encoding="utf-8")
        found = [
            n.module
            for n in ast.walk(ast.parse(probe.read_text(encoding="utf-8")))
            if isinstance(n, ast.ImportFrom)
        ]
        assert found == ["sqlalchemy"]
