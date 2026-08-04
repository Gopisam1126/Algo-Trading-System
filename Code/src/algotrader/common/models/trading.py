"""Trading domain models — signals, recommendations, orders, positions.

The most important type in this module is :class:`Recommendation`.  It is the
boundary between the probabilistic layer (strategies + AI) and the
deterministic layer (risk engine + execution), and it enforces constraint C4
from LOW_LEVEL_ARCHITECTURE.md §1.1:

    The LLM must never compute position size, stop price, or place an order.

That is enforced structurally rather than by convention: ``Recommendation``
has **no quantity field, no rupee amounts, and no final stop price**.  The AI
layer cannot influence them because there is no field through which to do so.
``quantity`` and the executable ``stop_price`` are computed downstream by the
risk engine from config and live broker margin.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from algotrader.common.enums import (
    AIVerdict,
    Direction,
    ExitReason,
    OrderIntent,
    OrderStatus,
    OrderType,
    PositionStatus,
    Product,
    RejectReason,
    Side,
)
from algotrader.common.models.market import Price

Confidence = Annotated[Decimal, Field(ge=0, le=1, decimal_places=3)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _require_utc(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware (UTC)")
    return v


# ---------------------------------------------------------------------------
# Strategy output
# ---------------------------------------------------------------------------


class Trigger(_Frozen):
    """A deterministic strategy firing.  No AI involvement at this point."""

    correlation_id: UUID = Field(default_factory=uuid4)
    symbol: str
    strategy_id: str
    direction: Direction
    trigger_price: Price
    suggested_stop: Price
    timeframe_agreement: int = Field(ge=0, le=3)
    fired_at: datetime

    _utc = field_validator("fired_at")(_require_utc)

    @model_validator(mode="after")
    def _stop_on_correct_side(self) -> Trigger:
        if self.direction is Direction.LONG and self.suggested_stop >= self.trigger_price:
            raise ValueError("long stop must be below the trigger price")
        if self.direction is Direction.SHORT and self.suggested_stop <= self.trigger_price:
            raise ValueError("short stop must be above the trigger price")
        return self

    @property
    def stop_distance(self) -> Decimal:
        return abs(self.trigger_price - self.suggested_stop)


class AIReview(_Frozen):
    """Structured output from the AI confirmation call.

    Every field is an enum, a bounded number, or a length-capped string.
    Nothing free-form escapes into a downstream prompt or into sizing.
    """

    verdict: AIVerdict
    confidence: Confidence
    timeframe_agreement: int = Field(ge=0, le=3)
    thesis_alignment: str = Field(max_length=32)
    supporting_factors: list[str] = Field(default_factory=list, max_length=8)
    risk_factors: list[str] = Field(default_factory=list, max_length=8)
    rationale: str = Field(max_length=2000)

    model_used: str
    latency_ms: int = Field(ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)


class Recommendation(_Frozen):
    """The AI / deterministic boundary.

    NOTE what this type does NOT contain:
      * no ``quantity``
      * no ``capital_at_risk``
      * no executable ``stop_price``
      * no rupee amounts of any kind

    Those are computed by :mod:`algotrader.execution.sizer` from config and
    live broker margin.  Adding any of them to this model would break
    constraint C4 — do not.
    """

    correlation_id: UUID
    symbol: str
    strategy_id: str
    direction: Direction

    trigger_price: Price       # from the deterministic strategy, not the AI
    suggested_stop: Price      # from ATR/structure, not the AI

    timeframe_agreement: int = Field(ge=0, le=3)
    ai_confidence: Confidence
    ai_verdict: AIVerdict
    ai_rationale: str = Field(max_length=2000)

    score_snapshot: dict[str, float] = Field(default_factory=dict)
    emitted_at: datetime

    _utc = field_validator("emitted_at")(_require_utc)

    @classmethod
    def build(cls, trigger: Trigger, review: AIReview, score: dict[str, float] | None = None,
              *, now: datetime) -> Recommendation:
        return cls(
            correlation_id=trigger.correlation_id,
            symbol=trigger.symbol,
            strategy_id=trigger.strategy_id,
            direction=trigger.direction,
            trigger_price=trigger.trigger_price,
            suggested_stop=trigger.suggested_stop,
            timeframe_agreement=review.timeframe_agreement,
            ai_confidence=review.confidence,
            ai_verdict=review.verdict,
            ai_rationale=review.rationale,
            score_snapshot=score or {},
            emitted_at=now,
        )


# ---------------------------------------------------------------------------
# Risk engine output
# ---------------------------------------------------------------------------


class SizingResult(_Frozen):
    """The outcome of position sizing, including WHICH constraint bound.

    Recording the binding clamp means a surprisingly small position can be
    explained from the audit log rather than investigated.
    """

    quantity: int = Field(ge=0)
    entry_price: Price
    stop_price: Price
    target_price: Price | None = None
    capital_at_risk: Decimal = Field(ge=0)
    binding_constraint: str

    @property
    def notional(self) -> Decimal:
        return self.entry_price * self.quantity


class RiskDecision(_Frozen):
    approved: bool
    reason: RejectReason | None = None
    detail: str | None = None
    sizing: SizingResult | None = None
    checks_passed: list[str] = Field(default_factory=list)
    evaluated_at: datetime

    _utc = field_validator("evaluated_at")(_require_utc)

    @model_validator(mode="after")
    def _coherent(self) -> RiskDecision:
        if self.approved and self.sizing is None:
            raise ValueError("an approved decision must carry sizing")
        if self.approved and self.reason is not None:
            raise ValueError("an approved decision must not carry a reject reason")
        if not self.approved and self.reason is None:
            raise ValueError("a rejected decision must carry a reject reason")
        return self


# ---------------------------------------------------------------------------
# Orders & positions
# ---------------------------------------------------------------------------


class OrderRequest(_Frozen):
    """An order about to be sent to the broker.

    ``client_order_id`` is a deterministic hash of the decision, so the same
    logical decision always produces the same id.  After an ambiguous failure
    (timeout, 5xx) the recovery path is therefore to **query the broker, not
    retry** — blind retry after a timeout is the most expensive bug possible
    in a trading system, and this makes it structurally unnecessary.
    See LOW_LEVEL_ARCHITECTURE.md §8.2.
    """

    client_order_id: str = Field(min_length=8, max_length=64)
    correlation_id: UUID
    symbol: str
    side: Side
    order_type: OrderType
    product: Product
    quantity: int = Field(gt=0)
    limit_price: Price | None = None
    trigger_price: Price | None = None
    intent: OrderIntent
    algo_id: str | None = None      # SEBI-mandated, attached by the gateway

    #: Market protection for MARKET and SL-M orders.
    #:
    #: Zerodha rejects unprotected market orders from 1 Apr 2026. ``-1``
    #: requests broker-default auto-protection; a positive value is a
    #: percentage band. Market protection converts the market order into a
    #: limit order and is still subject to exchange LPP ranges.
    #:
    #: Note this is NOT the same as our own stop-loss: it bounds slippage on
    #: the fill, it does not bound the position's risk.
    market_protection: Decimal | None = None

    @model_validator(mode="after")
    def _price_required_for_type(self) -> OrderRequest:
        if self.order_type in (OrderType.LIMIT, OrderType.SL) and self.limit_price is None:
            raise ValueError(f"{self.order_type} requires a limit price")
        if self.order_type in (OrderType.SL, OrderType.SLM) and self.trigger_price is None:
            raise ValueError(f"{self.order_type} requires a trigger price")
        return self

    @model_validator(mode="after")
    def _market_orders_need_protection(self) -> OrderRequest:
        """MARKET and SL-M orders must carry market protection.

        Without it the broker rejects the order. Catching it here means the
        failure surfaces in a unit test rather than as a rejected exit at
        15:09 with a position still open.
        """
        if self.order_type in (OrderType.MARKET, OrderType.SLM):
            if self.market_protection is None:
                raise ValueError(
                    f"{self.order_type.value} orders require market_protection "
                    f"(-1 for broker auto-protection, or a percentage). "
                    f"Unprotected market orders are rejected by the broker."
                )
            if self.market_protection != Decimal("-1") and self.market_protection <= 0:
                raise ValueError(
                    "market_protection must be -1 (auto) or a positive percentage; "
                    "0 is explicitly rejected by the broker"
                )
        return self


class Order(_Frozen):
    client_order_id: str
    broker_order_id: str | None = None
    correlation_id: UUID
    symbol: str
    side: Side
    order_type: OrderType
    product: Product
    quantity: int = Field(gt=0)
    limit_price: Price | None = None
    trigger_price: Price | None = None
    status: OrderStatus
    filled_quantity: int = Field(default=0, ge=0)
    average_price: Price | None = None
    intent: OrderIntent
    placed_at: datetime
    last_update_at: datetime
    rejection_reason: str | None = None

    _utc = field_validator("placed_at", "last_update_at")(_require_utc)

    @property
    def is_complete(self) -> bool:
        return self.status.is_terminal

    @property
    def remaining(self) -> int:
        return max(0, self.quantity - self.filled_quantity)


class Position(_Frozen):
    """An open or closed position.

    ``stop_price`` is non-optional by design.  A position without a
    protective stop must not be representable — if the stop order fails to
    place after entry, the position is closed at market immediately.
    """

    position_id: int | None = None
    correlation_id: UUID
    symbol: str
    slot_index: int = Field(ge=0)
    direction: Direction
    quantity: int = Field(gt=0)
    entry_price: Price
    stop_price: Price                       # never None — invariant
    target_price: Price | None = None
    opened_at: datetime
    squareoff_deadline: datetime            # per-stock; see calendar.py
    status: PositionStatus = PositionStatus.OPEN

    closed_at: datetime | None = None
    exit_price: Price | None = None
    exit_reason: ExitReason | None = None
    realized_pnl: Decimal | None = None
    max_favourable_excursion: Decimal | None = None
    max_adverse_excursion: Decimal | None = None

    _utc = field_validator("opened_at", "squareoff_deadline")(_require_utc)

    @model_validator(mode="after")
    def _stop_on_correct_side(self) -> Position:
        if self.direction is Direction.LONG and self.stop_price >= self.entry_price:
            raise ValueError("long stop must be below entry")
        if self.direction is Direction.SHORT and self.stop_price <= self.entry_price:
            raise ValueError("short stop must be above entry")
        return self

    @property
    def risk_per_share(self) -> Decimal:
        return abs(self.entry_price - self.stop_price)

    @property
    def initial_risk(self) -> Decimal:
        return self.risk_per_share * self.quantity

    def unrealized_pnl(self, ltp: Decimal) -> Decimal:
        delta = ltp - self.entry_price
        if self.direction is Direction.SHORT:
            delta = -delta
        return delta * self.quantity

    def r_multiple(self, ltp: Decimal) -> Decimal:
        risk = self.initial_risk
        if risk == 0:
            return Decimal(0)
        return self.unrealized_pnl(ltp) / risk
