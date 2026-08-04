"""Configuration loading and validation.

Three gates stand between a YAML file and a running system
(LOW_LEVEL_ARCHITECTURE.md §10.11, MVP_UI_AND_LEGAL.md §9.4):

1. **Type / range validation** — Pydantic field constraints.
2. **Cross-field validation** — weights sum to 1.0, slots × slot-capital ≤
   capital, interval floor ≤ ceiling.
3. **HARD BOUNDS** — absolute safety limits defined *in code*, which reject
   dangerous values regardless of what the file says.

Gate 3 is the important one and it is the reason these constants live here
rather than in YAML:

    Configuration can tune the system.  It can never disable safety.

A compromised or mistaken config file cannot raise the order rate above the
SEBI-safe cap, set a 50% per-trade risk, or turn off the human approval gate
on strategy promotion.
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from algotrader.common.enums import AutonomyLevel, SystemMode, Timeframe

# ---------------------------------------------------------------------------
# HARD BOUNDS — config may not exceed these.  Changing them is a code change
# that goes through review, which is exactly the point.
# ---------------------------------------------------------------------------

MAX_ORDERS_PER_SECOND = 5          # SEBI threshold is 10; we cap at half
MAX_RISK_PCT_PER_TRADE = Decimal("10.0")
MAX_POSITION_SLOTS = 20
MAX_DAILY_LOSS_PCT = Decimal("25.0")
MAX_POSITION_PCT = Decimal("100.0")
MIN_STRATEGY_TRIALS = 50           # below this, statistics are meaningless
MAX_PBO_ALLOWED = Decimal("0.6")   # Probability of Backtest Overfitting
MAX_ACTIVE_STRATEGIES = 12
MIN_EXIT_BUFFER_MINUTES = 1

Pct = Annotated[Decimal, Field(ge=0, le=100)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------


class SystemConfig(_Model):
    mode: SystemMode = SystemMode.PAPER
    timezone: str = "Asia/Kolkata"
    deployment_region: str = "ap-south-1"
    static_ip: str = ""
    log_level: str = "INFO"

    @field_validator("deployment_region")
    @classmethod
    def _must_be_india(cls, v: str) -> str:
        """SEBI requires algos to be hosted on Indian servers."""
        india_regions = {"ap-south-1", "ap-south-2", "centralindia", "southindia",
                         "asia-south1", "asia-south2", "in-mumbai", "in-delhi"}
        if v.lower() not in india_regions:
            raise ValueError(
                f"deployment_region {v!r} is not an India region. SEBI requires "
                f"algos to run on Indian servers. Allowed: {sorted(india_regions)}"
            )
        return v


class BrokerAuthConfig(_Model):
    method: str = "oauth_2fa"
    daily_reauth_time: time = time(7, 0)
    credentials_source: str = "env"


class BrokerConfig(_Model):
    primary: str = "angelone"
    fallback: str | None = "fyers"
    algo_id: str = ""
    auth: BrokerAuthConfig = Field(default_factory=BrokerAuthConfig)


class HardFilters(_Model):
    """India-specific exclusions.  See INDIA_FEATURES_AND_CONFIG.md §5.2."""

    exclude_t2t: bool = True
    exclude_asm_gsm: bool = True
    exclude_fno_ban: bool = True
    exclude_earnings_today: bool = True
    min_circuit_band_pct: Decimal = Decimal("10")
    min_price: Decimal = Decimal("100")
    min_avg_volume_20d: int = 500_000
    min_market_cap_cr: Decimal = Decimal("5000")
    min_avg_daily_range_pct: Decimal = Decimal("1.5")
    max_spread_pct: Decimal = Decimal("0.05")

    @model_validator(mode="after")
    def _t2t_cannot_be_disabled(self) -> Self:
        if not self.exclude_t2t:
            raise ValueError(
                "exclude_t2t cannot be disabled: intraday trading is structurally "
                "impossible in the Trade-to-Trade segment (compulsory delivery)"
            )
        return self


class ScoringWeights(_Model):
    trend_alignment: Decimal = Decimal("0.25")
    relative_strength: Decimal = Decimal("0.20")
    volatility_fitness: Decimal = Decimal("0.15")
    volume_expansion: Decimal = Decimal("0.15")
    level_proximity: Decimal = Decimal("0.15")
    catalyst_news: Decimal = Decimal("0.10")

    @model_validator(mode="after")
    def _sums_to_one(self) -> Self:
        total = sum(
            (self.trend_alignment, self.relative_strength, self.volatility_fitness,
             self.volume_expansion, self.level_proximity, self.catalyst_news),
            Decimal(0),
        )
        if abs(total - Decimal(1)) > Decimal("0.001"):
            raise ValueError(f"scoring weights must sum to 1.0, got {total}")
        return self


class UniverseConfig(_Model):
    base: str = "nifty200"
    custom_symbols: list[str] = Field(default_factory=list)
    hard_filters: HardFilters = Field(default_factory=HardFilters)
    scoring_weights: ScoringWeights = Field(default_factory=ScoringWeights)
    shortlist_size: int = Field(default=15, ge=1, le=100)
    final_watchlist_size: int = Field(default=8, ge=1, le=50)

    @model_validator(mode="after")
    def _watchlist_fits_shortlist(self) -> Self:
        if self.final_watchlist_size > self.shortlist_size:
            raise ValueError("final_watchlist_size cannot exceed shortlist_size")
        return self


class PerTradeRisk(_Model):
    risk_pct: Decimal = Field(default=Decimal("1.0"), gt=0)
    sizing_method: str = "atr_based"
    atr_multiplier_stop: Decimal = Field(default=Decimal("1.5"), gt=0)
    max_position_pct: Pct = Decimal("20")
    target_r_multiple: Decimal = Field(default=Decimal("2.0"), gt=0)
    trailing_stop_after_r: Decimal | None = Decimal("1.0")

    @field_validator("risk_pct")
    @classmethod
    def _within_hard_bound(cls, v: Decimal) -> Decimal:
        if v > MAX_RISK_PCT_PER_TRADE:
            raise ValueError(
                f"risk_pct {v} exceeds the hard safety bound of "
                f"{MAX_RISK_PCT_PER_TRADE}%. This limit is defined in code and "
                f"cannot be raised by configuration."
            )
        return v


class PortfolioRisk(_Model):
    max_daily_loss_pct: Decimal = Field(default=Decimal("3.0"), gt=0)
    max_sector_exposure_pct: Pct = Decimal("40")
    max_correlated_positions: int = Field(default=2, ge=1)
    max_net_directional_exposure_pct: Pct = Decimal("60")
    consecutive_loss_halt: int = Field(default=3, ge=1)

    @field_validator("max_daily_loss_pct")
    @classmethod
    def _within_hard_bound(cls, v: Decimal) -> Decimal:
        if v > MAX_DAILY_LOSS_PCT:
            raise ValueError(
                f"max_daily_loss_pct {v} exceeds the hard bound of {MAX_DAILY_LOSS_PCT}%"
            )
        return v


class SquareOffTimes(_Model):
    cas_stocks: time = time(15, 10)
    non_cas_stocks: time = time(15, 20)
    fno: time = time(15, 25)


class RiskConfig(_Model):
    capital: Decimal = Field(default=Decimal("500000"), gt=0)
    position_slots: int = Field(default=5, ge=1)
    capital_per_slot_pct: Pct = Decimal("20")
    per_trade: PerTradeRisk = Field(default_factory=PerTradeRisk)
    portfolio: PortfolioRisk = Field(default_factory=PortfolioRisk)
    exit_buffer_minutes: int = Field(default=5, ge=MIN_EXIT_BUFFER_MINUTES)
    square_off_times: SquareOffTimes = Field(default_factory=SquareOffTimes)

    @field_validator("position_slots")
    @classmethod
    def _slots_within_bound(cls, v: int) -> int:
        if v > MAX_POSITION_SLOTS:
            raise ValueError(f"position_slots {v} exceeds the hard bound of {MAX_POSITION_SLOTS}")
        return v

    @model_validator(mode="after")
    def _slot_allocation_coherent(self) -> Self:
        allocated = self.capital_per_slot_pct * self.position_slots
        if allocated > Decimal(100):
            raise ValueError(
                f"{self.position_slots} slots x {self.capital_per_slot_pct}% = "
                f"{allocated}% of capital, which exceeds 100%"
            )
        return self

    @property
    def capital_per_slot(self) -> Decimal:
        return self.capital * self.capital_per_slot_pct / Decimal(100)

    @property
    def risk_per_trade_rupees(self) -> Decimal:
        return self.capital * self.per_trade.risk_pct / Decimal(100)

    @property
    def daily_loss_limit_rupees(self) -> Decimal:
        return self.capital * self.portfolio.max_daily_loss_pct / Decimal(100)


class ExecutionConfig(_Model):
    interval_mode: str = "adaptive"
    interval_floor: Timeframe = Timeframe.M5
    interval_ceiling: Timeframe = Timeframe.M15
    latency_headroom_multiplier: Decimal = Field(default=Decimal("2.0"), ge=1)
    recalibrate_interval_daily: bool = True
    max_orders_per_second: int = Field(default=5, ge=1)
    order_type: str = "limit"
    limit_offset_pct: Decimal = Decimal("0.05")
    order_timeout_sec: int = Field(default=30, ge=1)
    no_trade_windows: list[tuple[time, time]] = Field(default_factory=list)

    @field_validator("max_orders_per_second")
    @classmethod
    def _sebi_safe(cls, v: int) -> int:
        if v > MAX_ORDERS_PER_SECOND:
            raise ValueError(
                f"max_orders_per_second {v} exceeds the hard cap of "
                f"{MAX_ORDERS_PER_SECOND}. SEBI's algo-registration threshold is "
                f"10 orders/sec per segment; we stay at half that deliberately."
            )
        return v

    @model_validator(mode="after")
    def _floor_below_ceiling(self) -> Self:
        if self.interval_floor.seconds > self.interval_ceiling.seconds:
            raise ValueError("interval_floor must not exceed interval_ceiling")
        return self


class AIModels(_Model):
    deep_synthesis: str = "claude-opus-5"
    session_reasoning: str = "claude-sonnet-5"
    news_triage: str = "claude-haiku-4-5"


class AIConfidence(_Model):
    min_to_act: Decimal = Field(default=Decimal("0.65"), ge=0, le=1)
    min_for_full_size: Decimal = Field(default=Decimal("0.80"), ge=0, le=1)
    treat_conflict_as_no_trade: bool = True

    @model_validator(mode="after")
    def _thresholds_ordered(self) -> Self:
        if self.min_for_full_size < self.min_to_act:
            raise ValueError("min_for_full_size must be >= min_to_act")
        return self


class AICostControls(_Model):
    daily_token_budget: int = Field(default=2_000_000, gt=0)
    alert_at_pct: Pct = Decimal("80")
    hard_stop_at_pct: Pct = Decimal("100")


class AIConfig(_Model):
    provider: str = "anthropic"
    models: AIModels = Field(default_factory=AIModels)
    confidence: AIConfidence = Field(default_factory=AIConfidence)
    cost_controls: AICostControls = Field(default_factory=AICostControls)
    session_timeout_sec: int = Field(default=15, ge=1)
    fallback_on_timeout: str = "skip_trade"

    @field_validator("fallback_on_timeout")
    @classmethod
    def _must_fail_closed(cls, v: str) -> str:
        if v != "skip_trade":
            raise ValueError(
                "fallback_on_timeout must be 'skip_trade'. The system fails "
                "closed: an AI timeout never results in an unreviewed order."
            )
        return v


class StrategyValidationConfig(_Model):
    """The overfitting gauntlet.  See STRATEGY_ENGINE.md §5."""

    min_trades: int = Field(default=100, ge=MIN_STRATEGY_TRIALS)
    min_regimes: int = Field(default=2, ge=1)
    max_pbo: Decimal = Field(default=Decimal("0.5"), gt=0, le=1)
    min_deflated_sharpe_confidence: Decimal = Field(default=Decimal("0.95"), gt=0, lt=1)
    max_correlation_to_active: Decimal = Field(default=Decimal("0.8"), gt=0, le=1)
    parameter_sensitivity_pct: Decimal = Field(default=Decimal("20"), gt=0)
    holdout_months: int = Field(default=6, ge=1)

    @field_validator("max_pbo")
    @classmethod
    def _pbo_within_bound(cls, v: Decimal) -> Decimal:
        if v > MAX_PBO_ALLOWED:
            raise ValueError(
                f"max_pbo {v} exceeds the hard bound of {MAX_PBO_ALLOWED}. A PBO "
                f"above 0.5 means the in-sample ranking is more likely than not "
                f"to fail out-of-sample."
            )
        return v


class StrategyPromotionConfig(_Model):
    shadow_min_sessions: int = Field(default=20, ge=1)
    paper_min_trades: int = Field(default=30, ge=1)
    paper_min_expectancy_r: Decimal = Decimal("0.15")
    require_human_approval: bool = True

    @field_validator("require_human_approval")
    @classmethod
    def _cannot_be_disabled(cls, v: bool) -> bool:
        if not v:
            raise ValueError(
                "require_human_approval cannot be disabled. Promotion of a "
                "strategy to live capital is always a human decision, at every "
                "autonomy level. See STRATEGY_ENGINE.md §4.1."
            )
        return v


class AIGenerationConfig(_Model):
    enabled: bool = False              # Phase 2 — off until the gauntlet exists
    cadence: str = "weekly"
    max_proposals_per_cycle: int = Field(default=5, ge=1, le=20)
    max_active_ai_strategies: int = Field(default=3, ge=0)
    model: str = "claude-opus-5"
    modes: list[str] = Field(default_factory=lambda: ["observation", "journal"])
    require_hypothesis: bool = True
    min_journal_trades: int = Field(default=50, ge=1)

    @field_validator("cadence")
    @classmethod
    def _not_continuous(cls, v: str) -> str:
        if v == "continuous":
            raise ValueError(
                "Continuous strategy generation is not permitted. Each cycle "
                "adds to the global trial count and raises the Deflated Sharpe "
                "bar; generating continuously is the recursive-overfitting "
                "failure mode. Use 'weekly' or 'monthly'."
            )
        return v

    @field_validator("require_hypothesis")
    @classmethod
    def _hypothesis_mandatory(cls, v: bool) -> bool:
        if not v:
            raise ValueError(
                "require_hypothesis cannot be disabled. A strategy whose author "
                "cannot state an economic mechanism is data mining."
            )
        return v


class StrategyEngineConfig(_Model):
    enabled: bool = True
    max_active: int = Field(default=6, ge=1)
    max_active_per_regime: int = Field(default=3, ge=1)
    max_shadow: int = Field(default=10, ge=0)
    validation: StrategyValidationConfig = Field(default_factory=StrategyValidationConfig)
    promotion: StrategyPromotionConfig = Field(default_factory=StrategyPromotionConfig)
    ai_generation: AIGenerationConfig = Field(default_factory=AIGenerationConfig)

    @field_validator("max_active")
    @classmethod
    def _within_bound(cls, v: int) -> int:
        if v > MAX_ACTIVE_STRATEGIES:
            raise ValueError(f"max_active {v} exceeds the hard bound of {MAX_ACTIVE_STRATEGIES}")
        return v


class AutonomyConfig(_Model):
    level: AutonomyLevel = AutonomyLevel.L1_ALERT
    max_position_value_pct: Pct = Decimal("20")
    min_ai_confidence: Decimal = Field(default=Decimal("0.70"), ge=0, le=1)
    min_timeframe_agreement: int = Field(default=2, ge=0, le=3)
    max_india_vix: Decimal = Decimal("22")
    require_symbol_traded_before: bool = True
    escalation_timeout_sec: int = Field(default=60, ge=10)


class NotificationConfig(_Model):
    channels: list[str] = Field(default_factory=lambda: ["telegram"])
    premarket_briefing: bool = True
    trade_alerts: bool = True
    risk_breach_alerts: bool = True
    eod_report: bool = True
    require_approval_before_entry: bool = True
    recipients: list[str] = Field(default_factory=list, max_length=1)

    @model_validator(mode="after")
    def _single_recipient(self) -> Self:
        """Regulatory control, not a preference.

        Broadcasting trade signals to more than one person can trigger SEBI
        Research Analyst obligations.  See MVP_UI_AND_LEGAL.md §2.3.
        """
        if len(self.recipients) > 1:
            raise ValueError(
                "More than one notification recipient configured. Sharing trade "
                "signals with others can trigger SEBI Research Analyst "
                "obligations. Personal use means a single recipient."
            )
        return self


class AppConfig(_Model):
    """Root configuration object."""

    system: SystemConfig = Field(default_factory=SystemConfig)
    broker: BrokerConfig = Field(default_factory=BrokerConfig)
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    strategy_engine: StrategyEngineConfig = Field(default_factory=StrategyEngineConfig)
    autonomy: AutonomyConfig = Field(default_factory=AutonomyConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)

    @model_validator(mode="after")
    def _live_mode_requires_compliance(self) -> Self:
        if self.system.mode is SystemMode.LIVE:
            if not self.system.static_ip:
                raise ValueError(
                    "live mode requires system.static_ip to be set and whitelisted "
                    "with the broker (SEBI requirement)"
                )
            if not self.broker.algo_id:
                raise ValueError(
                    "live mode requires broker.algo_id — every algorithmic order "
                    "must carry an exchange-assigned Algo-ID (SEBI, since 1 Apr 2026)"
                )
        return self

    def config_hash(self) -> str:
        """Stable hash, recorded in each day's plan.

        Lets every trade be traced back to the exact configuration that
        produced it.
        """
        import hashlib
        import json

        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load and validate configuration.

    Raises with a full list of problems rather than the first one — a
    misconfiguration should be fixable in one pass, not one restart at a time.
    """
    import os

    resolved = Path(path or os.environ.get("ALGOTRADER_CONFIG", "config/system.yaml"))
    if not resolved.exists():
        raise FileNotFoundError(f"config file not found: {resolved}")

    with resolved.open(encoding="utf-8") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh) or {}

    return AppConfig.model_validate(raw)
