"""Configuration says what the code does (audit 10 Nov 2026).

ENG-003: ``system.yaml`` advertised LIMIT entries with a 0.05% offset while the
gateway sent MARKET; RSK-001: the autonomy ladder was validated and read by
nothing; RSK-010: the order-rate cap had no reader. Each was a number in a file
that read as a control. This module makes that class of defect a failing test:
every leaf of ``AppConfig`` is either READ (by a named file, checked here) or
RESERVED for a named story that will read it - and the reserved ones are
safety-neutral, or refused at load, until then.

Also here: the hard bounds RSK-009 found missing, RiskContext's required
fields (RSK-014), and the rate limiter's cap (RSK-010).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import BaseModel, ValidationError

from algotrader.broker.ratelimit import RateLimitConfig
from algotrader.common.config import (
    MAX_CONSECUTIVE_LOSS_HALT,
    MAX_ORDERS_PER_SECOND,
    AppConfig,
    ExecutionConfig,
)
from algotrader.execution.gateway import GatewayPolicy
from algotrader.execution.risk.context import RiskContext

CODE = Path(__file__).resolve().parents[2]

#: leaf -> the file that reads it. The test checks the file names the leaf.
WIRED: dict[str, str] = {
    "system.mode": "src/algotrader/execution/service.py",
    "universe.base": "src/algotrader/premarket/universe.py",
    "universe.custom_symbols": "src/algotrader/premarket/universe.py",
    "system.timezone": "src/algotrader/common/config.py",  # fixed: Literal
    "system.deployment_region": "scripts/doctor.py",
    "system.static_ip": "scripts/doctor.py",
    "database.host": "src/algotrader/common/config.py",
    "database.port": "src/algotrader/common/config.py",
    "database.name": "src/algotrader/common/config.py",
    "database.user": "src/algotrader/common/config.py",
    "database.pool_size": "src/algotrader/common/db/engine.py",
    "database.max_overflow": "src/algotrader/common/db/engine.py",
    "database.pool_pre_ping": "src/algotrader/common/db/engine.py",
    "database.pool_recycle_seconds": "src/algotrader/common/db/engine.py",
    "database.statement_timeout_ms": "src/algotrader/common/db/engine.py",
    "database.echo_sql": "src/algotrader/common/db/engine.py",
    "redis.host": "src/algotrader/common/config.py",
    "redis.port": "src/algotrader/common/config.py",
    "redis.db": "src/algotrader/common/config.py",
    "redis.max_connections": "src/algotrader/common/redis/client.py",
    "redis.socket_timeout_seconds": "src/algotrader/common/redis/client.py",
    "redis.socket_connect_timeout_seconds": "src/algotrader/common/redis/client.py",
    "broker.primary": "src/algotrader/common/config.py",
    "broker.fallback": "scripts/doctor.py",
    "broker.algo_id": "src/algotrader/execution/gateway.py",
    "broker.algo_tagging_confirmation": "src/algotrader/common/config.py",  # live gate
    "risk.capital": "src/algotrader/execution/service.py",
    "risk.position_slots": "src/algotrader/execution/service.py",
    "risk.capital_per_slot_pct": "src/algotrader/execution/risk/factory.py",
    "risk.per_trade.risk_pct": "src/algotrader/execution/risk/factory.py",
    "risk.per_trade.sizing_method": "src/algotrader/common/config.py",  # fixed: Literal
    "risk.per_trade.atr_multiplier_stop": "src/algotrader/execution/risk/factory.py",
    "risk.per_trade.max_position_pct": "src/algotrader/execution/risk/factory.py",
    "risk.per_trade.min_position_pct": "src/algotrader/execution/risk/factory.py",
    "risk.per_trade.max_price_divergence_pct": "src/algotrader/execution/risk/factory.py",
    "risk.per_trade.target_r_multiple": "src/algotrader/execution/risk/factory.py",
    "risk.per_trade.trailing_stop_after_r": "src/algotrader/common/config.py",  # must be unset
    "risk.portfolio.max_daily_loss_pct": "src/algotrader/execution/risk/factory.py",
    "risk.portfolio.max_sector_exposure_pct": "src/algotrader/execution/risk/factory.py",
    "risk.portfolio.max_correlated_positions": "src/algotrader/execution/risk/factory.py",
    "risk.portfolio.correlation_threshold": "src/algotrader/execution/risk/factory.py",
    "risk.portfolio.max_net_directional_exposure_pct": "src/algotrader/execution/risk/factory.py",
    "risk.portfolio.consecutive_loss_halt": "src/algotrader/execution/risk/factory.py",
    "risk.exit_buffer_minutes": "src/algotrader/execution/service.py",
    "risk.min_minutes_to_squareoff": "src/algotrader/execution/risk/factory.py",
    "risk.square_off_times.cas_stocks": "src/algotrader/common/config.py",  # = calendar
    "risk.square_off_times.non_cas_stocks": "src/algotrader/common/config.py",
    "risk.square_off_times.fno": "src/algotrader/common/config.py",
    "execution.max_orders_per_second": "src/algotrader/broker/ratelimit.py",
    "execution.no_trade_windows": "src/algotrader/execution/risk/factory.py",
    "execution.market_protection": "src/algotrader/execution/gateway.py",
    "execution.max_signal_age_seconds": "src/algotrader/execution/service.py",
    "ai.confidence.min_to_act": "src/algotrader/execution/service.py",
    "ai.confidence.min_for_full_size": "src/algotrader/execution/service.py",
    "ai.confidence.treat_conflict_as_no_trade": "src/algotrader/execution/service.py",
    "ai.fallback_on_timeout": "src/algotrader/common/config.py",  # fixed: skip_trade
    "strategy_engine.promotion.require_human_approval": "src/algotrader/common/config.py",
    "strategy_engine.ai_generation.require_hypothesis": "src/algotrader/common/config.py",
    "strategy_engine.ai_generation.cadence": "src/algotrader/common/config.py",
    "autonomy.level": "src/algotrader/execution/service.py",
    "notifications.require_approval_before_entry": "src/algotrader/execution/service.py",
    "notifications.recipients": "src/algotrader/common/config.py",
}

#: leaf prefix -> the story that will read it. Nothing here gates money
#: today: either the feature is unbuilt (and the level or path that would use
#: it refuses), or the value is informational.
RESERVED: dict[str, str] = {
    "system.log_level": "E23-S03 - the entrypoints configure logging",
    "redis.default_stream_maxlen": "E23-S03 - the composition root passes it to the publishers",
    "broker.auth.": "E02-S02 / E23-S03 - the composition root passes the re-auth time to KiteAuth",
    "universe.": "E04 surveillance + E11 pre-market selection",
    "execution.interval_": "E20-S02 - latency profiler and adaptive interval",
    "execution.latency_headroom_multiplier": "E20-S02",
    "execution.recalibrate_interval_daily": "E20-S02",
    "ai.provider": "E10-S01 - Anthropic client wrapper",
    "ai.models.": "E10-S01",
    "ai.cost_controls.": "E10-S04 - token budget and cost control",
    "ai.session_timeout_sec": "E13-S03 - AI review",
    "strategy_engine.enabled": "E12-S09 / E18-S04 - strategy management",
    "strategy_engine.max_": "E12-S09 / E18-S04",
    "strategy_engine.validation.": "E12-S05..S08 - the gauntlet",
    "strategy_engine.promotion.": "E12-S09 / E12-S10 - shadow, paper, promotion",
    "strategy_engine.ai_generation.": "E12-S11 - AI strategy generation (enabled: false)",
    "autonomy.max_position_value_pct": "E16-S02 - envelope evaluation (L3 refuses until then)",
    "autonomy.min_ai_confidence": "E16-S02",
    "autonomy.min_timeframe_agreement": "E16-S02",
    "autonomy.max_india_vix": "E16-S02",
    "autonomy.require_symbol_traded_before": "E16-S02",
    "autonomy.escalation_timeout_sec": "E16-S03 - escalation and approval flow",
    "notifications.channels": "E19-S01 - Telegram bot foundation",
    "notifications.premarket_briefing": "E11-S08 - plan lock and briefing delivery",
    "notifications.trade_alerts": "E19-S01",
    "notifications.risk_breach_alerts": "E19-S01",
    "notifications.eod_report": "E19-S01",
}


def _leaves(model: type[BaseModel], prefix: str = "") -> list[str]:
    out: list[str] = []
    for name, field in model.model_fields.items():
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            out += _leaves(annotation, f"{prefix}{name}.")
        else:
            out.append(prefix + name)
    return out


def _reserved(leaf: str) -> bool:
    return any(
        leaf == key or (key.endswith((".", "_")) and leaf.startswith(key)) for key in RESERVED
    )


class TestEveryKeyIsReadOrReserved:
    def test_every_leaf_is_classified_exactly_once(self) -> None:
        leaves = _leaves(AppConfig)
        unclassified = [leaf for leaf in leaves if leaf not in WIRED and not _reserved(leaf)]
        # A wired key inside a reserved prefix is wired; an EXACT reservation of
        # a wired key is a contradiction.
        both = [leaf for leaf in leaves if leaf in WIRED and leaf in RESERVED]
        assert not unclassified, (
            f"config keys with no reader and no owning story: {unclassified}. Wire them, "
            f"reserve them for a story, or delete them - a number nobody reads is not a "
            f"control"
        )
        assert not both, both
        assert set(WIRED) <= set(leaves), sorted(set(WIRED) - set(leaves))

    @pytest.mark.parametrize("leaf", sorted(WIRED))
    def test_the_named_reader_names_the_key(self, leaf: str) -> None:
        text = (CODE / WIRED[leaf]).read_text(encoding="utf-8")
        assert leaf.rsplit(".", 1)[-1] in text, f"{WIRED[leaf]} does not read {leaf}"

    @pytest.mark.parametrize("key", ["order_type", "limit_offset_pct", "order_timeout_sec"])
    def test_the_dead_order_keys_are_gone(self, key: str) -> None:
        """ENG-003. Refused now, by extra='forbid', rather than ignored."""
        with pytest.raises(ValidationError):
            ExecutionConfig(**{key: "limit"})


class TestFixedFactsAreNotTunables:
    def test_a_different_square_off_time_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="calendar"):
            AppConfig.model_validate({"risk": {"square_off_times": {"non_cas_stocks": "15:25"}}})

    def test_another_timezone_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            AppConfig.model_validate({"system": {"timezone": "UTC"}})

    def test_a_sizing_method_that_does_not_exist_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            AppConfig.model_validate({"risk": {"per_trade": {"sizing_method": "kelly"}}})

    def test_a_trailing_stop_is_refused_until_one_exists(self) -> None:
        """NR-009: no trailing stop is implemented, and Position cannot hold one."""
        with pytest.raises(ValidationError, match="trailing"):
            AppConfig.model_validate({"risk": {"per_trade": {"trailing_stop_after_r": 1.0}}})

    def test_the_gateway_policy_comes_from_config(self) -> None:
        policy = GatewayPolicy.from_config(AppConfig())
        assert policy.market_protection == Decimal("-1")
        assert policy.algo_id is None


class TestTheHardBoundsHold:
    """RSK-009: each of these could switch its control off from the YAML."""

    @pytest.mark.parametrize(
        ("raw", "says"),
        [
            (
                {"risk": {"portfolio": {"consecutive_loss_halt": MAX_CONSECUTIVE_LOSS_HALT + 1}}},
                "consecutive_loss_halt",
            ),
            (
                {"risk": {"position_slots": 5, "portfolio": {"max_correlated_positions": 6}}},
                "max_correlated_positions",
            ),
            ({"risk": {"exit_buffer_minutes": 31}}, "exit_buffer_minutes"),
            ({"risk": {"min_minutes_to_squareoff": 14}}, "min_minutes_to_squareoff"),
            ({"risk": {"min_minutes_to_squareoff": 181}}, "min_minutes_to_squareoff"),
            ({"risk": {"per_trade": {"max_price_divergence_pct": 6}}}, "max_price_divergence_pct"),
            ({"risk": {"per_trade": {"min_position_pct": 25}}}, "min_position_pct"),
        ],
    )
    def test_the_first_value_past_each_bound_is_refused(self, raw: dict, says: str) -> None:
        with pytest.raises(ValidationError, match=says):
            AppConfig.model_validate(raw)

    @pytest.mark.parametrize(
        "raw",
        [
            {"risk": {"portfolio": {"consecutive_loss_halt": MAX_CONSECUTIVE_LOSS_HALT}}},
            {"risk": {"exit_buffer_minutes": 30}},
            {"risk": {"min_minutes_to_squareoff": 15}},
            {"risk": {"per_trade": {"max_price_divergence_pct": 5}}},
        ],
    )
    def test_the_bounds_themselves_are_allowed(self, raw: dict) -> None:
        """The control: a bound that refused its own edge would be a tighter bound."""
        AppConfig.model_validate(raw)

    def test_an_empty_window_list_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="must cover"):
            AppConfig.model_validate({"execution": {"no_trade_windows": []}})

    def test_an_inverted_window_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="does not start before"):
            AppConfig.model_validate(
                {
                    "execution": {
                        "no_trade_windows": [["09:15", "09:20"], ["15:30", "15:00"]],
                    }
                }
            )

    def test_one_mandatory_window_missing_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="15:00-15:30"):
            AppConfig.model_validate({"execution": {"no_trade_windows": [["09:15", "09:20"]]}})

    def test_a_wider_window_covers_the_mandatory_one(self) -> None:
        cfg = AppConfig.model_validate(
            {"execution": {"no_trade_windows": [["09:15", "09:30"], ["14:45", "15:30"]]}}
        )
        assert len(cfg.execution.no_trade_windows) == 2

    def test_the_default_is_the_mandatory_windows(self) -> None:
        assert [tuple(w) for w in AppConfig().execution.no_trade_windows] == [
            (dt.time(9, 15), dt.time(9, 20)),
            (dt.time(15, 0), dt.time(15, 30)),
        ]


class TestTheBookFieldsAreRequired:
    """RSK-014: RiskContext's own docstring said nothing was optional-with-a-
    default, while open_positions, slots, correlations and lot_size were."""

    BASE: ClassVar[dict[str, object]] = {
        "now": dt.datetime(2026, 8, 25, 5, 0, tzinfo=dt.UTC),
        "squareoff_deadline": dt.datetime(2026, 8, 25, 9, 35, tzinfo=dt.UTC),
        "capital": Decimal("500000"),
        "open_positions": (),
        "slots_total": 5,
        "slots_used": 0,
        "correlations": {},
        "lot_size": 1,
    }

    @pytest.mark.parametrize(
        "missing", ["open_positions", "slots_total", "slots_used", "correlations", "lot_size"]
    )
    def test_each_one_is_required(self, missing: str) -> None:
        with pytest.raises(TypeError, match=missing):
            RiskContext(**{k: v for k, v in self.BASE.items() if k != missing})  # type: ignore[arg-type]

    def test_positional_construction_is_impossible(self) -> None:
        assert all(f.kw_only for f in dataclasses.fields(RiskContext))


class TestTheOrderRateIsCappedInCode:
    """RSK-010: MAX_ORDERS_PER_SECOND and execution.max_orders_per_second were
    read by nothing; the limiter's own guard was SEBI's 10 and its burst was
    unbounded."""

    @pytest.mark.parametrize("field", ["orders_per_second", "order_burst"])
    def test_one_above_the_hard_cap_is_refused(self, field: str) -> None:
        with pytest.raises(ValueError, match="MAX_ORDERS_PER_SECOND"):
            RateLimitConfig(**{field: MAX_ORDERS_PER_SECOND + 1})

    def test_the_config_value_is_the_limiter(self) -> None:
        cfg = AppConfig.model_validate({"execution": {"max_orders_per_second": 4}})
        limits = RateLimitConfig.from_execution(cfg.execution)
        assert (limits.orders_per_second, limits.order_burst) == (4, 4)

    def test_the_trading_adapter_cannot_be_built_without_a_limiter(self) -> None:
        from algotrader.broker.kite.trading import KiteTradingAdapter

        with pytest.raises(TypeError, match="BrokerRateLimiter"):
            KiteTradingAdapter(auth=None, client=object(), limiter=None)  # type: ignore[arg-type]
