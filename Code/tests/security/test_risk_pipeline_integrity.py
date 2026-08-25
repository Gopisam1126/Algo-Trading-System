"""🔴 No order path bypasses the risk pipeline (E14-S01, criterion 1).

This is the criterion the whole epic exists to satisfy, and it is not the kind
of thing a behavioural test can establish. A test can show that *this* call went
through the pipeline; it cannot show that no future call skips it. So the
checks here are structural — they read the source and assert that the shape of
the code makes bypassing hard.

Three layers, weakest to strongest:

1. **Type-level.** An approved ``RiskDecision`` cannot exist without a
   ``SizingResult`` — the model validator refuses. So "approve without sizing"
   is unrepresentable rather than discouraged.
2. **Choke point.** Only ``RiskEngine`` constructs an approval, so a reviewer
   has exactly one function to read.
3. **Grep-level.** Nothing else in ``src/`` builds one, asserted below.

Layer 3 is a text scan and will produce a false positive one day when some
unrelated file legitimately mentions the phrase. That is the intended
direction: it fails loudly and a human looks, rather than staying quiet while
an order path grows around the side.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import ClassVar

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "algotrader"
ENGINE = SRC / "execution" / "risk" / "framework.py"


def _python_files() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts]


class TestApprovalIsUnrepresentableWithoutSizing:
    """Layer 1 — the type refuses, so nothing downstream has to remember."""

    def test_an_approval_without_sizing_does_not_construct(self) -> None:
        import datetime as dt

        from algotrader.common.models.trading import RiskDecision

        with pytest.raises(ValueError, match="must carry sizing"):
            RiskDecision(approved=True, evaluated_at=dt.datetime.now(dt.UTC))

    def test_an_approval_carrying_a_reject_reason_does_not_construct(self) -> None:
        """A decision that is both approved and rejected is incoherent, and
        without this it would be constructible — the sort of state that reads
        fine in a log line and means nothing."""
        import datetime as dt
        from decimal import Decimal

        from algotrader.common.enums import RejectReason
        from algotrader.common.models.trading import RiskDecision, SizingResult

        sizing = SizingResult(
            quantity=1,
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
            capital_at_risk=Decimal("5"),
            binding_constraint="probe",
        )
        with pytest.raises(ValueError, match="must not carry a reject reason"):
            RiskDecision(
                approved=True,
                sizing=sizing,
                reason=RejectReason.KILL_SWITCH_ACTIVE,
                evaluated_at=dt.datetime.now(dt.UTC),
            )


class TestOnlyTheEngineApproves:
    """Layers 2 and 3 — one choke point, asserted by reading the source."""

    #: Every way an approval can be spelled.
    APPROVAL_PATTERNS = (
        re.compile(r"RiskDecision\.approve\s*\("),
        re.compile(r"RiskDecision\s*\(\s*\n?\s*approved\s*=\s*True"),
        re.compile(r"approved\s*=\s*True"),
    )

    def test_no_module_outside_the_engine_constructs_an_approval(self) -> None:
        offenders: list[str] = []
        for path in _python_files():
            if path == ENGINE:
                continue
            # The model module DEFINES approve(); that is the constructor, not
            # a caller of it.
            if path.name == "trading.py" and path.parent.name == "models":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for pattern in self.APPROVAL_PATTERNS:
                if pattern.search(text):
                    offenders.append(f"{path.relative_to(SRC)} matched {pattern.pattern!r}")
        assert not offenders, (
            "an approved RiskDecision is constructed outside RiskEngine, so an "
            "order path exists that never ran the checks:\n  " + "\n  ".join(offenders)
        )

    def test_the_engine_does_construct_one(self) -> None:
        """The control. Without it the test above passes trivially if approval
        is renamed or removed, and the criterion would be 'satisfied' by a
        system that can never trade."""
        assert re.search(r"RiskDecision\.approve\s*\(", ENGINE.read_text(encoding="utf-8"))

    def test_the_engine_approves_in_exactly_one_place(self) -> None:
        """More than one approval site inside the engine means more than one
        path to an order, which is the same problem one directory in."""
        text = ENGINE.read_text(encoding="utf-8")
        assert len(re.findall(r"RiskDecision\.approve\s*\(", text)) == 1


class TestTheEngineIsDeterministic:
    """LOW_LEVEL_ARCHITECTURE.md §5.7: 'entirely deterministic — no AI, no
    randomness, no network calls except to the broker.'

    Asserted rather than trusted, because the same claim was made about the
    strategy evaluator and was false — ``fire()`` minted a UUID with
    ``uuid4()`` until a review caught it.
    """

    FORBIDDEN_IMPORTS: ClassVar[set[str]] = {
        "random",
        "secrets",
        "requests",
        "httpx",
        "urllib",
        "socket",
        "anthropic",
        "kiteconnect",
        "sqlalchemy",
        "psycopg",
        "redis",
    }

    @pytest.mark.parametrize("module", ["framework.py", "context.py"])
    def test_the_risk_modules_import_nothing_that_could_do_io(self, module: str) -> None:
        path = SRC / "execution" / "risk" / module
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        forbidden = imported & self.FORBIDDEN_IMPORTS
        assert not forbidden, f"{module} imports {sorted(forbidden)}"

    def test_the_engine_reads_no_clock(self) -> None:
        """Every timestamp comes from the context, so an evaluation replayed
        with the same inputs produces the same decision. A clock read here
        would make the audit record unreproducible."""
        text = ENGINE.read_text(encoding="utf-8")
        body = text.split("class RiskEngine")[1]
        for forbidden in ("datetime.now(", "time.time(", "utcnow("):
            assert forbidden not in body, f"RiskEngine reads the clock: {forbidden}"

    def test_evaluate_takes_its_time_from_the_context(self) -> None:
        import inspect

        from algotrader.execution.risk.framework import RiskEngine

        source = inspect.getsource(RiskEngine.evaluate)
        assert "ctx.now" in source


class TestFailClosedIsStructural:
    """Invariant 6. A component that cannot decide must refuse, and the ways it
    could accidentally do otherwise are enumerated here."""

    def test_a_missing_sizer_cannot_produce_an_approval(self) -> None:
        text = ENGINE.read_text(encoding="utf-8")
        assert "if self.sizer is None" in text
        # The branch must reject. If someone ever makes it fall through to
        # approval, this is where it shows.
        block = text.split("if self.sizer is None")[1][:600]
        assert "RiskDecision.reject" in block

    def test_a_raising_check_is_caught_and_turned_into_a_refusal(self) -> None:
        text = ENGINE.read_text(encoding="utf-8")
        assert "except Exception" in text
        block = text.split("except Exception")[1][:900]
        assert "CheckOutcome.fail" in block, (
            "a check that raises must become a refusal; anything else means a "
            "broken gate stops being a gate"
        )

    def test_the_audit_failure_path_does_not_change_the_decision(self) -> None:
        """The caller still has to act on the answer, and AuditWriter already
        buffers to disk when the database is down."""
        text = ENGINE.read_text(encoding="utf-8")
        audit_block = text.split("def _audit")[1]
        assert "log.exception" in audit_block
        assert "return decision" not in audit_block
