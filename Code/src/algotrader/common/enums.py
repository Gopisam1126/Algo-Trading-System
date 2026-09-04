"""Enumerations shared across every service.

Kept in one module so that a value's meaning cannot drift between services.
"""

from __future__ import annotations

from enum import StrEnum


class Timeframe(StrEnum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    D1 = "1d"
    W1 = "1w"

    @property
    def seconds(self) -> int:
        return {
            Timeframe.M1: 60,
            Timeframe.M5: 300,
            Timeframe.M15: 900,
            Timeframe.H1: 3600,
            Timeframe.D1: 86_400,
            Timeframe.W1: 604_800,
        }[self]


class Exchange(StrEnum):
    NSE = "NSE"
    BSE = "BSE"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    SL = "SL"  # stop-loss limit
    SLM = "SLM"  # stop-loss market


class Product(StrEnum):
    MIS = "MIS"  # intraday, auto square-off applies
    CNC = "CNC"  # delivery
    NRML = "NRML"  # F&O carry-forward


class OrderIntent(StrEnum):
    ENTRY = "ENTRY"
    STOP = "STOP"
    TARGET = "TARGET"
    SQUAREOFF = "SQUAREOFF"


class OrderStatus(StrEnum):
    """See LOW_LEVEL_ARCHITECTURE.md §8.2 for the state machine."""

    PENDING_RISK = "PENDING_RISK"
    APPROVED = "APPROVED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    SUBMIT_FAILED = "SUBMIT_FAILED"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"

    @property
    def is_terminal(self) -> bool:
        return self in {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
        }


class PositionStatus(StrEnum):
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"


class ExitReason(StrEnum):
    STOP = "STOP"
    TARGET = "TARGET"
    TRAILING_STOP = "TRAILING_STOP"
    TIME = "TIME"  # square-off deadline
    THESIS_INVALIDATED = "THESIS_INVALIDATED"
    KILLSWITCH = "KILLSWITCH"
    MANUAL = "MANUAL"


class SessionState(StrEnum):
    """See LOW_LEVEL_ARCHITECTURE.md §8.1."""

    STOPPED = "STOPPED"
    PREPARING = "PREPARING"
    PLAN_LOCKED = "PLAN_LOCKED"
    WATCHING = "WATCHING"
    TRADING = "TRADING"
    CLOSING_ONLY = "CLOSING_ONLY"
    HALTED = "HALTED"  # terminal for the day; human-only exit


class SystemMode(StrEnum):
    PAPER = "paper"
    ALERT_ONLY = "alert_only"
    APPROVAL = "approval"
    LIVE = "live"


class AutonomyLevel(StrEnum):
    """See MVP_UI_AND_LEGAL.md §3.1."""

    L0_OBSERVE = "L0"
    L1_ALERT = "L1"
    L2_APPROVE = "L2"
    L3_SUPERVISED = "L3"  # the design target
    L4_FULL_AUTO = "L4"


class Regime(StrEnum):
    RISK_ON = "RISK_ON"
    RISK_OFF = "RISK_OFF"
    NEUTRAL = "NEUTRAL"
    HIGH_VOL = "HIGH_VOL"
    LOW_VOL = "LOW_VOL"
    TRENDING = "TRENDING"
    RANGEBOUND = "RANGEBOUND"


class AIVerdict(StrEnum):
    CONFIRM = "CONFIRM"
    WEAK = "WEAK"
    VETO = "VETO"


class NewsScope(StrEnum):
    """Firm / sector / macro are kept as SEPARATE streams, never blended.

    Research finding: macro and firm-level news interact rather than
    substitute.  See MVP_UI_AND_LEGAL.md §5.1.
    """

    FIRM = "FIRM"
    SECTOR = "SECTOR"
    MACRO = "MACRO"


class NewsEventType(StrEnum):
    EARNINGS = "EARNINGS"
    GUIDANCE = "GUIDANCE"
    MERGER_ACQUISITION = "MERGER_ACQUISITION"
    REGULATORY = "REGULATORY"
    LEGAL = "LEGAL"
    MANAGEMENT_CHANGE = "MANAGEMENT_CHANGE"
    PRODUCT = "PRODUCT"
    CONTRACT_WIN = "CONTRACT_WIN"
    DOWNGRADE_UPGRADE = "DOWNGRADE_UPGRADE"
    MACRO_POLICY = "MACRO_POLICY"
    COMMODITY = "COMMODITY"
    OTHER = "OTHER"

    @property
    def decay_half_life_days(self) -> float:
        """Time decay differs by event class — one global half-life is wrong.

        See MVP_UI_AND_LEGAL.md §5.4.
        """
        return {
            NewsEventType.EARNINGS: 2.0,
            NewsEventType.GUIDANCE: 2.0,
            NewsEventType.MERGER_ACQUISITION: 15.0,
            NewsEventType.REGULATORY: 10.0,
            NewsEventType.LEGAL: 10.0,
            NewsEventType.MANAGEMENT_CHANGE: 5.0,
            NewsEventType.PRODUCT: 3.0,
            NewsEventType.CONTRACT_WIN: 3.0,
            NewsEventType.DOWNGRADE_UPGRADE: 2.0,
            NewsEventType.MACRO_POLICY: 20.0,
            NewsEventType.COMMODITY: 5.0,
            NewsEventType.OTHER: 1.0,
        }[self]


class NewsHorizon(StrEnum):
    INTRADAY = "INTRADAY"
    DAYS = "DAYS"
    WEEKS = "WEEKS"
    STRUCTURAL = "STRUCTURAL"


class StrategyOrigin(StrEnum):
    USER_AUTHORED = "USER_AUTHORED"
    AI_PROPOSED_OBSERVATION = "AI_PROPOSED_OBSERVATION"
    AI_PROPOSED_JOURNAL = "AI_PROPOSED_JOURNAL"
    BUILTIN_SEED = "BUILTIN_SEED"


class StrategyState(StrEnum):
    """See STRATEGY_ENGINE.md §4."""

    DRAFT = "DRAFT"
    VALIDATING = "VALIDATING"
    REJECTED = "REJECTED"
    SHADOW = "SHADOW"
    PAPER = "PAPER"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    RETIRED = "RETIRED"
    QUARANTINED = "QUARANTINED"

    @property
    def places_live_orders(self) -> bool:
        return self in {StrategyState.ACTIVE, StrategyState.DEGRADED}

    @property
    def is_runnable(self) -> bool:
        """States whose signals are evaluated (even if not executed live)."""
        return self in {
            StrategyState.SHADOW,
            StrategyState.PAPER,
            StrategyState.ACTIVE,
            StrategyState.DEGRADED,
        }


class DecisionStage(StrEnum):
    """Audit-log stages — threads one trade end to end via correlation_id."""

    PREMARKET_CANDIDATE = "PREMARKET_CANDIDATE"
    SIGNAL = "SIGNAL"
    AI_REVIEW = "AI_REVIEW"
    RISK_CHECK = "RISK_CHECK"
    ORDER = "ORDER"
    FILL = "FILL"
    EXIT = "EXIT"
    RECONCILIATION = "RECONCILIATION"


class RejectReason(StrEnum):
    """Machine-readable rejection codes.

    Surfaced on the dashboard as `signals_rejected_total{check}` — the
    highest-value debugging metric in the system, because it turns
    "why isn't it trading?" into a glance instead of an investigation.
    """

    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    HEALTH_GATE_FAILED = "HEALTH_GATE_FAILED"
    #: The risk engine itself could not do its job — a check raised, or
    #: sizing was unavailable or raised. Distinct from every other member
    #: here, which say "the system worked and the answer is no". This one
    #: says "the system is broken, and refusing is the safe reading of
    #: that". SIT-001 found the framework borrowing HEALTH_GATE_FAILED for
    #: these, which sent an operator looking for a downed service that was
    #: perfectly healthy.
    RISK_ENGINE_FAULT = "RISK_ENGINE_FAULT"
    OUTSIDE_TRADING_WINDOW = "OUTSIDE_TRADING_WINDOW"
    NO_TRADE_WINDOW = "NO_TRADE_WINDOW"
    SYMBOL_NOT_TRADABLE = "SYMBOL_NOT_TRADABLE"
    NO_SLOT_AVAILABLE = "NO_SLOT_AVAILABLE"
    ALREADY_HOLDING = "ALREADY_HOLDING"
    CORRELATION_LIMIT = "CORRELATION_LIMIT"
    SECTOR_EXPOSURE_LIMIT = "SECTOR_EXPOSURE_LIMIT"
    NET_EXPOSURE_LIMIT = "NET_EXPOSURE_LIMIT"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    CONSECUTIVE_LOSS_LIMIT = "CONSECUTIVE_LOSS_LIMIT"
    INSUFFICIENT_MARGIN = "INSUFFICIENT_MARGIN"
    #: Sizing produced nothing, and margin was not why (E14-S07).
    #:
    #: The position cap, the slot cap or lot rounding can each take a
    #: quantity to zero on an account with plenty of margin. Reporting
    #: that as INSUFFICIENT_MARGIN sends an operator to look at funds
    #: that are fine — the SIT-001 conflation, and the reason E14-S07's
    #: AC2 ("a surprisingly small position is explainable") could not
    #: otherwise pass. The binding constraint says which clamp it was.
    POSITION_TOO_SMALL = "POSITION_TOO_SMALL"
    TOO_CLOSE_TO_SQUAREOFF = "TOO_CLOSE_TO_SQUAREOFF"
    AI_LOW_CONFIDENCE = "AI_LOW_CONFIDENCE"
    AI_VETO = "AI_VETO"
    AI_UNAVAILABLE = "AI_UNAVAILABLE"
    AI_REFUSAL = "AI_REFUSAL"
    TIMEFRAME_CONFLICT = "TIMEFRAME_CONFLICT"
    NOT_IN_PLAN = "NOT_IN_PLAN"
    INDICATORS_NOT_READY = "INDICATORS_NOT_READY"
    STALE_DATA = "STALE_DATA"
    EVENT_BLACKOUT = "EVENT_BLACKOUT"


class CorporateActionType(StrEnum):
    """Corporate actions that change the meaning of a historical price.

    SPLIT and BONUS both restate the share count, so both scale price and
    volume. DIVIDEND reduces price only — volume is untouched, which is why
    price and volume carry separate adjustment factors.
    """

    SPLIT = "SPLIT"
    BONUS = "BONUS"
    DIVIDEND = "DIVIDEND"
    RIGHTS = "RIGHTS"
    CONSOLIDATION = "CONSOLIDATION"


class SurveillanceCategory(StrEnum):
    """ASM list membership. Both are hard exclusions; the distinction is for audit."""

    SHORT_TERM = "SHORT_TERM"
    LONG_TERM = "LONG_TERM"
