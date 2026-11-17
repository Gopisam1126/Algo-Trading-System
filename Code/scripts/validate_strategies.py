#!/usr/bin/env python
"""Compile every strategy file in config/strategies/, and check it can RUN here.

Run before committing a strategy change. Compilation is only the first gate
(G1–G2 of the validation gauntlet) — passing here means the strategy is
well-formed and evaluable by this deployment, NOT that it may trade. See
STRATEGY_ENGINE.md §5.

CHANGED 16 Nov 2026 (audit TRD-014): this compiled only, and printed OK for an
orb_classic that asked for daily and weekly agreement the engine does not
carry - every load refused it. It now builds the runtime evaluator too, exactly
as the registry does, and fails on what the registry would refuse.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from algotrader.strategy import primitives  # noqa: F401 — populates REGISTRY
from algotrader.strategy.dsl import (
    REGISTRY,
    CompilationError,
    compile_strategy,
    load_strategy_yaml,
)
from algotrader.strategy.runtime import StrategyEvaluator


def main() -> int:
    strategy_dir = Path(__file__).parents[1] / "config" / "strategies"
    if not strategy_dir.exists():
        print(f"No strategy directory at {strategy_dir}")
        return 1

    files = sorted(strategy_dir.glob("*.yaml")) + sorted(strategy_dir.glob("*.yml"))
    if not files:
        print(f"No strategy files in {strategy_dir}")
        return 1

    print(f"Registry: {len(REGISTRY.names())} primitives available\n")

    failures = 0
    for path in files:
        try:
            doc = load_strategy_yaml(path.read_text(encoding="utf-8"))
            compile_strategy(doc)
            StrategyEvaluator(doc)  # what the registry builds; refuses the unrunnable
        except (CompilationError, ValueError) as exc:
            print(f"  FAIL  {path.name}")
            print(f"        {exc}")
            failures += 1
            continue

        conditions = len(doc.entry.all_conditions())
        print(f"  OK    {path.name}")
        print(f"        id={doc.id} v{doc.version}  origin={doc.origin.value}")
        print(
            f"        direction={doc.direction.value}  "
            f"timeframe={doc.applicability.timeframe.value}"
        )
        print(f"        regimes={[r.value for r in doc.applicability.regimes]}")
        print(f"        entry conditions={conditions}  hash={doc.content_hash()[:12]}")

    print()
    if failures:
        print(f"{failures} of {len(files)} strategies failed to compile or cannot run here")
        return 1

    print(f"All {len(files)} strategies compile and can be evaluated here.")
    print("\nNote: compilation is gate G1-G2 only. A strategy must still pass the")
    print("full validation gauntlet (walk-forward, Deflated Sharpe, PBO) and a")
    print("human approval gate before it can trade live capital.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
