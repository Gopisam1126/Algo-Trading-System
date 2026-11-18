""" "Why isn't it trading?" finds every rejection, through the index built for it.

Audit ENG-026 (17 Nov 2026). ``DecisionLogRepository.recent_rejections``
filtered on ``outcome = 'REJECT'``, a value no producer writes - the risk
engine writes ``rejected``, the execution service ``REJECTED`` and
``REFUSED`` - so the highest-value debugging query always came back empty,
and the partial index meant for it covered only one of the three spellings.
Both now read ``models.REJECTION_OUTCOMES``.
"""

from __future__ import annotations

import ast
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from algotrader.common.audit import AuditEntry, AuditWriter
from algotrader.common.db import engine as db_engine
from algotrader.common.db.models import REJECTION_OUTCOMES
from algotrader.common.db.repositories import DecisionLogRepository
from algotrader.execution.risk.framework import RiskCheck, RiskEngine
from algotrader.execution.service import Outcome
from tests.conftest import CODE_ROOT
from tests.unit.test_risk_framework import NOW, _always, _ctx, _rec, _sizer

pytestmark = [pytest.mark.integration]


@pytest.fixture
async def factory(migrated_database: str) -> AsyncIterator[object]:
    eng = db_engine.create_engine_from_url(migrated_database)
    yield db_engine.create_session_factory(eng)
    await eng.dispose()


@pytest.fixture
async def session(factory: object) -> AsyncIterator[AsyncSession]:
    """decision_log is emptied at BOTH ends: the writer commits (QA-E15-28)."""
    async with factory() as s:  # type: ignore[operator]
        await s.execute(text("DELETE FROM decision_log"))
        await s.commit()
        yield s
        await s.rollback()
        await s.execute(text("DELETE FROM decision_log"))
        await s.commit()


def service_entry(outcome: str) -> AuditEntry:
    return AuditEntry(
        correlation_id=uuid.uuid4(),
        stage="SIGNAL",
        outcome=outcome,
        service="execution-svc",
        payload={},
        ts=NOW,
    )


class TestEveryRejectionIsFound:
    async def test_a_real_risk_engine_rejection_and_the_services_refusals(
        self, factory: object, session: AsyncSession, tmp_path: Path
    ) -> None:
        engine_rows: list[dict] = []
        engine = RiskEngine(
            checks=[RiskCheck("kill_switch", _always(False))],
            sizer=_sizer(),
            audit=engine_rows.append,
        )
        engine.evaluate(_rec(), _ctx())
        writer = AuditWriter(factory, buffer_dir=tmp_path / "audit")  # type: ignore[arg-type]
        for row in engine_rows:  # exactly what ExecutionService writes for the engine
            assert await writer.write(AuditEntry(**row)) is not None
        for outcome in (Outcome.REJECTED, Outcome.REFUSED, Outcome.SUBMITTED, Outcome.DUPLICATE):
            assert await writer.write(service_entry(outcome.value)) is not None

        found = await DecisionLogRepository(session).recent_rejections()
        assert sorted(r["outcome"] for r in found) == ["REFUSED", "REJECTED", "rejected"]

    async def test_the_index_serves_the_query(self, session: AsyncSession) -> None:
        """The partial index's predicate is the reader's, so the planner can use it."""
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        listed = ", ".join(f"'{o}'" for o in REJECTION_OUTCOMES)
        plan = "\n".join(
            r[0]
            for r in await session.execute(
                text(
                    "EXPLAIN SELECT * FROM decision_log "
                    f"WHERE outcome IN ({listed}) ORDER BY ts DESC LIMIT 100"
                )
            )
        )
        assert "ix_audit_reason" in plan, plan


class TestTheVocabularyIsOneList:
    def test_the_service_outcomes_that_mean_no_trade_are_in_it(self) -> None:
        assert {Outcome.REJECTED.value, Outcome.REFUSED.value} <= set(REJECTION_OUTCOMES)

    def test_the_migration_froze_the_same_list(self) -> None:
        source = next((CODE_ROOT / "migrations" / "versions").glob("*7f3e9b2c4d10*.py")).read_text(
            encoding="utf-8"
        )
        frozen = next(
            ast.literal_eval(node.value)
            for node in ast.parse(source).body
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "_REJECTIONS" for t in node.targets)
        )
        assert tuple(frozen) == REJECTION_OUTCOMES
