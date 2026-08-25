"""The risk engine (E14).

``framework`` is the ordered fail-fast pipeline and the only path to an
approved decision; ``context`` is the read-only state a check may read. The
fourteen checks themselves are E14-S02..S06 and the sizer is E14-S07.
"""

from algotrader.execution.risk.context import OpenPosition, RiskContext, RiskContextError
from algotrader.execution.risk.framework import (
    CheckOutcome,
    RiskCheck,
    RiskCheckError,
    RiskEngine,
)

__all__ = [
    "CheckOutcome",
    "OpenPosition",
    "RiskCheck",
    "RiskCheckError",
    "RiskContext",
    "RiskContextError",
    "RiskEngine",
]
