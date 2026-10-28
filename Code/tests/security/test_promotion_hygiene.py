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


def _promote_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("promote_tool_b", REPO / "scripts" / "promote.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTheScriptCannotPromoteToProd:
    """The /sdlc skill's first rule is "never push to PROD", and it cited
    promote.yml's exit 1 as the gate - a workflow this script never touches.
    `--from DEV --to PROD` would have pushed DEV's whole tree to PROD,
    skipping QA. Now refused, before any git command runs."""

    @pytest.mark.parametrize(
        ("source", "target"),
        [("DEV", "PROD"), ("QA", "PROD"), ("DEV", "prod"), ("DEV", "main"), ("QA", "QA")],
    )
    def test_every_promotion_but_dev_to_qa_is_refused(self, source, target) -> None:
        assert _promote_module().refusal(source, target) is not None

    def test_dev_to_qa_is_allowed(self) -> None:
        """The control: a refusal that refused everything passes the test above."""
        assert _promote_module().refusal("DEV", "QA") is None

    def test_each_layer_holds_on_its_own(self) -> None:
        """Two independent layers refuse PROD - an explicit PROD refusal, and
        an allowlist that never names PROD. Either alone suffices, so a test
        of the outcome cannot see one of them break; each is pinned here."""
        module = _promote_module()
        assert module.ALLOWED == frozenset({("DEV", "QA")})
        for source in ("DEV", "QA"):
            assert "never promotes to PROD" in module.refusal(source, "PROD")

    def test_prod_is_refused_before_git_is_touched(self, monkeypatch) -> None:
        module = _promote_module()

        def tripwire(*_args, **_kwargs):
            raise AssertionError("git was called for a PROD promotion")

        monkeypatch.setattr(module, "git", tripwire)
        assert module.main(["--from", "DEV", "--to", "PROD"]) == 1


class TestOnlyACommitCiPassedIsPromoted:
    """The script's docstring named "promoting a commit CI never saw" as a
    failure it prevents, and nothing in it asked CI. ``ci_verdict`` is the
    decision; ``main`` refuses on any verdict, and on any failure to ask."""

    SHA = "a" * 40

    def _run(
        self, conclusion, *, status="completed", name="CI", sha=None, at="2026-10-28T05:00:00Z"
    ):
        return {
            "head_sha": sha or self.SHA,
            "name": name,
            "status": status,
            "conclusion": conclusion,
            "created_at": at,
        }

    def test_a_green_run_is_a_pass(self) -> None:
        assert _promote_module().ci_verdict([self._run("success")], self.SHA) is None

    @pytest.mark.parametrize(
        "runs",
        [
            [],
            [{"head_sha": "b" * 40, "name": "CI", "status": "completed", "conclusion": "success"}],
            [
                {
                    "head_sha": "a" * 40,
                    "name": "Promote",
                    "status": "completed",
                    "conclusion": "success",
                }
            ],
        ],
        ids=["no-run", "another-commit", "another-workflow"],
    )
    def test_no_ci_run_for_this_commit_is_a_refusal(self, runs) -> None:
        assert "has not seen" in _promote_module().ci_verdict(runs, self.SHA)

    def test_a_red_or_unfinished_run_is_a_refusal(self) -> None:
        module = _promote_module()
        assert "failure" in module.ci_verdict([self._run("failure")], self.SHA)
        assert "in_progress" in module.ci_verdict([self._run(None, status="in_progress")], self.SHA)

    def test_the_latest_run_decides(self) -> None:
        """A re-run that passed after a failure is a pass; a failure after a
        pass is not."""
        module = _promote_module()
        fail_then_pass = [
            self._run("failure", at="2026-10-28T05:00:00Z"),
            self._run("success", at="2026-10-28T06:00:00Z"),
        ]
        pass_then_fail = [
            self._run("success", at="2026-10-28T05:00:00Z"),
            self._run("failure", at="2026-10-28T06:00:00Z"),
        ]
        assert module.ci_verdict(fail_then_pass, self.SHA) is None
        assert module.ci_verdict(pass_then_fail, self.SHA) is not None

    def _level_repo(self, module, monkeypatch) -> None:
        """A healthy, undiverged DEV/QA pair whose QA tree is already right.

        Everything except the CI check would let ``main`` finish with 0, so a
        refusal can ONLY come from the check under test. The first version of
        these tests answered every git call with a hash - which the divergence
        check reads as "QA has commits DEV lacks" - so they passed with the CI
        check deleted outright (mutations P4, P5). Passing for the wrong
        reason is a test defect; this fixture removes the wrong reason.
        """

        def fake_git(*args, **_kwargs):
            if args[0] == "log":
                return ""  # no divergence
            if args[0] == "rev-list":
                return "0"
            return self.SHA

        monkeypatch.setattr(module, "git", fake_git)
        monkeypatch.setattr(module, "filtered_tree", lambda *a, **k: self.SHA)

    def test_an_unanswerable_question_refuses(self, monkeypatch, capsys) -> None:
        """GitHub down or rate-limited is "unknown", and unknown is not green."""
        module = _promote_module()
        self._level_repo(module, monkeypatch)

        def unreachable(*_args, **_kwargs):
            raise OSError("network unreachable")

        monkeypatch.setattr(module, "fetch_runs", unreachable)
        assert module.main(["--from", "DEV", "--to", "QA"]) == 1
        assert "could not ask GitHub whether CI passed" in capsys.readouterr().out

    def test_a_red_commit_is_refused_by_main(self, monkeypatch, capsys) -> None:
        module = _promote_module()
        self._level_repo(module, monkeypatch)
        monkeypatch.setattr(module, "fetch_runs", lambda *a, **k: [self._run("failure")])
        assert module.main(["--from", "DEV", "--to", "QA"]) == 1
        assert "concluded 'failure'" in capsys.readouterr().out

    def test_a_green_commit_proceeds_past_the_check(self, monkeypatch) -> None:
        """The control on the two above: with CI green and the target already
        carrying the right tree, main reaches its "nothing to do" exit."""
        module = _promote_module()
        self._level_repo(module, monkeypatch)
        monkeypatch.setattr(module, "fetch_runs", lambda *a, **k: [self._run("success")])
        assert module.main(["--from", "DEV", "--to", "QA"]) == 0
