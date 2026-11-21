"""Coverage gate (E22-S07): the risk engine and the sizer stay fully covered.

    pytest --cov=src/algotrader --cov-report=json:coverage.json
    python scripts/coverage_gate.py coverage.json

Two rules over coverage.py's JSON report:

- every module under ``FULLY_COVERED`` has **no missing line** - the risk engine
  (``execution/risk/``) and the sizer, the code that decides whether and how much
  to trade (LOW_LEVEL_ARCHITECTURE.md §12.1);
- the package's total stays at or above ``TOTAL_FLOOR``.

**Decision:** a script over the JSON report, run by CI after the suite. **Rejected:**
coverage.py's own ``fail_under`` - it judges only the total, and a total of 94
hides a risk check whose refusal branch never ran. **The failure it prevents:**
nothing held coverage anywhere until 21 Nov 2026 (no ``fail_under``, CI did not
even measure), and ``[tool.coverage.paths]`` named ``execution/risk_engine.py``
- a file that does not exist - under a comment saying the risk modules "must be
fully covered". So a prefix that matches no file is itself a failure here: a
gate over nothing passes forever. **What would make it wrong:** line coverage
says a line RAN, not that a test would notice it wrong - mutation testing
answers that (E22-S00); this only stops the floor moving down.

A line that cannot run is excluded with ``# pragma: no cover`` beside a test that
proves it cannot - never to make this pass.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

#: Path prefixes (relative to Code/, forward slashes) that must have no missing line.
FULLY_COVERED = ("src/algotrader/execution/risk/", "src/algotrader/execution/sizer.py")

#: The package total, statements and branches (coverage's percent_covered):
#: 93.96 on the full gate of 21 Nov 2026 (which the report rounds to 94). The
#: floor sits just under it so platform differences between Windows and CI's
#: Linux cannot fail a run - it is a floor, not a target; raise it as the total
#: rises, never lower it to make a run pass.
TOTAL_FLOOR = 93.0


def check(report: dict[str, Any]) -> list[str]:
    """Every rule the report breaks, as one line each. Empty means the gate passes."""
    files = {name.replace("\\", "/"): data for name, data in report["files"].items()}
    problems: list[str] = []
    for prefix in FULLY_COVERED:
        matched = {name: data for name, data in files.items() if name.startswith(prefix)}
        if not matched:
            problems.append(
                f"{prefix} matches no measured file - a gate over nothing passes forever "
                f"(renamed, moved, or not measured?)"
            )
        for name, data in sorted(matched.items()):
            missing = data.get("missing_lines") or []
            if missing:
                problems.append(f"{name} must be fully covered; lines not run: {missing}")
    total = float(report["totals"]["percent_covered"])
    if total < TOTAL_FLOOR:
        problems.append(f"total coverage {total:.2f} is below the floor of {TOTAL_FLOOR:.1f}")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: coverage_gate.py <coverage.json>")
        return 2
    path = Path(argv[1])
    if not path.is_file():
        print(f"coverage gate: no report at {path} - run pytest with --cov-report=json")
        return 1
    report = json.loads(path.read_text(encoding="utf-8"))
    problems = check(report)
    for problem in problems:
        print(f"coverage gate: FAIL {problem}")
    if not problems:
        print(
            f"coverage gate: OK - {', '.join(FULLY_COVERED)} fully covered; total "
            f"{float(report['totals']['percent_covered']):.2f} >= {TOTAL_FLOOR:.1f}"
        )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
