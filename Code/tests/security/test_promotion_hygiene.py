"""What each branch is allowed to carry, asserted rather than remembered.

`scripts/promote.py` strips `Documents/` from the tree it pushes to QA: design
specs, the backlog, the tracker workbook and the architecture diagrams are
written and read on DEV, and QA exists to verify the deployable system.

That creates a failure mode which is easy to introduce and unpleasant to
diagnose. A test or workflow step that reads a stripped path passes on DEV, and
then fails on QA only — after promotion, on a branch nobody is actively editing,
with an error that points at a missing file rather than at the promotion rule
that removed it.

So the rule is checked here, on DEV, where it is cheap: **nothing that runs on
QA may read a path the QA promotion strips.**

These are deliberately crude text scans. A more precise check would need to
resolve every path expression in the repository, and the crude version fails in
the safe direction — it flags a mention that turns out to be harmless, rather
than missing one that is not.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
CODE = REPO / "Code"


def _excluded_for(branch: str) -> tuple[str, ...]:
    """Read the exclusion list from the promotion script itself.

    Imported rather than restated, so the test cannot drift away from the rule
    it is checking — a copy would eventually disagree with the original and
    assert nothing useful.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("promote_tool", REPO / "scripts" / "promote.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return tuple(module.EXCLUDED.get(branch, ()))


class TestNothingRunningOnQaReadsAStrippedPath:
    def test_the_exclusion_list_is_what_we_expect(self) -> None:
        """Pins the rule so widening it is a deliberate edit that shows up in a
        diff, not a quiet change of behaviour."""
        assert _excluded_for("QA") == ("Documents", "scripts/tracker", ".claude")

    @pytest.mark.parametrize("subdir", ["src", "tests", "scripts", "migrations"])
    def test_no_python_under_code_reads_an_excluded_path(self, subdir: str) -> None:
        excluded = _excluded_for("QA")
        offenders: list[str] = []
        root = CODE / subdir
        if not root.exists():
            pytest.skip(f"{subdir} not present")

        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts or path.name == Path(__file__).name:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for name in excluded:
                # A path expression, not prose: quoted, or joined, or a literal
                # segment with a separator. Prose mentions in a docstring are
                # fine and must not fail this.
                pattern = rf'["\'/\\]{re.escape(name)}[/"\'\\]'
                if re.search(pattern, text):
                    offenders.append(f"{path.relative_to(REPO)} -> {name}")
        assert not offenders, (
            "these read a path that QA promotion strips, so they pass on DEV "
            "and fail on QA:\n  " + "\n  ".join(offenders)
        )

    def test_no_ci_step_reads_an_excluded_path(self) -> None:
        """The CI workflow runs on QA, so a step reading a stripped path breaks
        the branch it was promoted to."""
        excluded = _excluded_for("QA")
        workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        offenders = []
        for line in workflow.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # a comment cannot read a file
            for name in excluded:
                if f"{name}/" in stripped:
                    offenders.append(stripped[:100])
        assert not offenders, "ci.yml runs on QA and references a stripped path:\n  " + "\n  ".join(
            offenders
        )

    def test_the_makefile_targets_ci_runs_do_not_need_it(self) -> None:
        makefile = (CODE / "Makefile").read_text(encoding="utf-8")
        excluded = _excluded_for("QA")
        offenders = [
            line.strip()
            for line in makefile.splitlines()
            if not line.strip().startswith("#") and any(f"{name}/" in line for name in excluded)
        ]
        assert not offenders, f"Makefile references a stripped path: {offenders}"


class TestThePromotionRuleIsSafe:
    def test_promote_never_force_pushes(self) -> None:
        """A force push on a promotion branch discards whatever was there. The
        script's whole safety argument rests on this."""
        source = (REPO / "scripts" / "promote.py").read_text(encoding="utf-8")
        for forbidden in ("--force", "-f ", "+refs/"):
            assert forbidden not in source, f"promote.py contains {forbidden!r}"

    def test_promote_verifies_the_result(self) -> None:
        """Pushing and assuming it landed is how a promotion silently does
        nothing."""
        source = (REPO / "scripts" / "promote.py").read_text(encoding="utf-8")
        assert "TREE MISMATCH" in source
        assert "PUSH DID NOT LAND" in source

    def test_promote_refuses_on_divergence(self) -> None:
        source = (REPO / "scripts" / "promote.py").read_text(encoding="utf-8")
        assert "REFUSING" in source
        assert "--no-merges" in source, (
            "the divergence check must exclude merge commits, or every "
            "promotion looks like divergence"
        )
