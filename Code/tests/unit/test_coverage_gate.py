"""The coverage gate refuses what it says it refuses, and CI runs it (E22-S07)."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

CODE = Path(__file__).resolve().parents[2]


def _gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "script_coverage_gate", CODE / "scripts" / "coverage_gate.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _gate()

RISK = "src/algotrader/execution/risk/checks/loss.py"
SIZER = "src/algotrader/execution/sizer.py"
OTHER = "src/algotrader/ingest/bars.py"


def report(
    *,
    missing: dict[str, list[int]] | None = None,
    total: float = 94.1,
    files: tuple[str, ...] = (RISK, SIZER, OTHER),
) -> dict[str, Any]:
    missing = missing or {}
    return {
        "files": {name: {"missing_lines": missing.get(name, [])} for name in files},
        "totals": {"percent_covered": total},
    }


def test_a_fully_covered_risk_engine_and_sizer_pass() -> None:
    """Control: everything below must be refusing for its own reason."""
    assert gate.check(report()) == []


def test_a_missing_line_in_a_risk_module_fails_naming_it() -> None:
    problems = gate.check(report(missing={RISK: [372]}))
    assert len(problems) == 1
    assert RISK in problems[0] and "[372]" in problems[0]


def test_a_missing_line_in_the_sizer_fails() -> None:
    assert SIZER in gate.check(report(missing={SIZER: [10, 11]}))[0]


def test_a_missing_line_elsewhere_is_only_the_totals_business() -> None:
    """The 100% rule is for the risk engine and the sizer, not the package."""
    assert gate.check(report(missing={OTHER: [1, 2, 3]})) == []


def test_a_total_below_the_floor_fails() -> None:
    problems = gate.check(report(total=gate.TOTAL_FLOOR - 0.01))
    assert len(problems) == 1 and "below the floor" in problems[0]
    assert gate.check(report(total=gate.TOTAL_FLOOR)) == []


def test_a_prefix_that_matches_nothing_fails() -> None:
    """The defect this story found: a coverage entry naming execution/risk_engine.py,
    a file that does not exist, sat under 'must be fully covered' and held
    nothing. A gate whose path matches no file must refuse, not pass."""
    problems = gate.check(report(files=(SIZER, OTHER)))
    assert any("execution/risk/" in p and "matches no measured file" in p for p in problems)


def test_windows_paths_are_judged_the_same() -> None:
    """coverage.py writes backslashes on Windows; the gate must not go vacuous there."""
    windows = {
        "files": {
            RISK.replace("/", "\\"): {"missing_lines": [5]},
            SIZER.replace("/", "\\"): {"missing_lines": []},
        },
        "totals": {"percent_covered": 95.0},
    }
    problems = gate.check(windows)
    assert len(problems) == 1 and "[5]" in problems[0]


def test_main_exits_non_zero_on_a_failing_report_and_on_no_report(tmp_path: Path) -> None:
    good, bad = tmp_path / "good.json", tmp_path / "bad.json"
    good.write_text(json.dumps(report()), encoding="utf-8")
    bad.write_text(json.dumps(report(missing={RISK: [1]})), encoding="utf-8")
    assert gate.main(["coverage_gate.py", str(good)]) == 0
    assert gate.main(["coverage_gate.py", str(bad)]) == 1
    assert gate.main(["coverage_gate.py", str(tmp_path / "absent.json")]) == 1


def test_ci_measures_coverage_and_runs_the_gate_on_it() -> None:
    """The gate is only a control if CI runs it, after the suite, on the report the
    suite wrote - CI ran the suite with no coverage at all until 21 Nov 2026."""
    workflow = yaml.safe_load(
        (CODE.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    steps = [s.get("run", "") for s in workflow["jobs"]["test"]["steps"]]
    tests = next(i for i, run in enumerate(steps) if run.startswith("pytest"))
    assert "--cov=src/algotrader" in steps[tests]
    report_path = re.search(r"--cov-report=json:(\S+)", steps[tests])
    assert report_path, "the suite writes no JSON report for the gate"
    assert f"python scripts/coverage_gate.py {report_path.group(1)}" in steps[tests + 1 :]


def test_make_cov_runs_the_same_gate() -> None:
    makefile = (CODE / "Makefile").read_text(encoding="utf-8")
    assert "--cov-report=json:coverage.json" in makefile
    assert "python scripts/coverage_gate.py coverage.json" in makefile


def test_the_gate_names_files_that_exist() -> None:
    """Control on the configuration itself: each FULLY_COVERED prefix names real code."""
    for prefix in gate.FULLY_COVERED:
        target = CODE / prefix
        assert target.is_dir() or target.is_file(), prefix
