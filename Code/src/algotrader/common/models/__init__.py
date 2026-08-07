"""Domain models.

Every inter-service message, database row, and AI structured output is a
Pydantic model defined here.  Single source of truth for validation, JSON
serialization, and LLM output schemas.
"""

from algotrader.common.models.market import (
    Bar,
    IndicatorSnapshot,
    Instrument,
    InstrumentDailyStatus,
    MultiTimeframeSnapshot,
    NonNegPrice,
    Price,
    Tick,
)
from algotrader.common.models.trading import (
    AIReview,
    Confidence,
    Order,
    OrderRequest,
    Position,
    Recommendation,
    RiskDecision,
    SizingResult,
    Trigger,
)

__all__ = [
    # trading
    "AIReview",
    # market
    "Bar",
    "Confidence",
    "IndicatorSnapshot",
    "Instrument",
    "InstrumentDailyStatus",
    "MultiTimeframeSnapshot",
    "NonNegPrice",
    "Order",
    "OrderRequest",
    "Position",
    "Price",
    "Recommendation",
    "RiskDecision",
    "SizingResult",
    "Tick",
    "Trigger",
]
