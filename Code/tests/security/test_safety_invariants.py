"""Tests that the design's safety invariants hold structurally.

These are not ordinary unit tests.  Each one asserts a property the whole
architecture depends on — if one of these fails, a documented safety
guarantee has been silently removed and the corresponding design decision
needs revisiting before the code ships.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from algotrader.common.config import (
    MAX_ORDERS_PER_SECOND,
    AIConfig,
    AppConfig,
    ExecutionConfig,
    HardFilters,
    NotificationConfig,
    PerTradeRisk,
    RiskConfig,
    ScoringWeights,
    StrategyPromotionConfig,
)
from algotrader.common.models.trading import Recommendation
from algotrader.common.secrets import REDACTED, SecretString


class TestAICannotSizePositions:
    """Constraint C4 — the LLM must never compute position size or place orders.

    Enforced structurally: ``Recommendation`` is the only type that crosses
    from the AI layer to the risk engine, and it has no field through which
    size could be expressed.
    """

    FORBIDDEN = {
        "quantity", "qty", "size", "position_size", "capital_at_risk",
        "stop_price", "notional", "amount", "rupees", "lots", "value",
    }

    def test_recommendation_has_no_sizing_fields(self) -> None:
        fields = set(Recommendation.model_fields)
        leaked = fields & self.FORBIDDEN
        assert not leaked, (
            f"Recommendation exposes sizing field(s) {leaked}. This breaks "
            f"constraint C4: the AI layer must not be able to influence "
            f"position size. See LOW_LEVEL_ARCHITECTURE.md §1.1."
        )

    def test_recommendation_rejects_extra_fields(self) -> None:
        """extra='forbid' means sizing cannot be smuggled in dynamically."""
        with pytest.raises(ValidationError):
            Recommendation(  # type: ignore[call-arg]
                correlation_id="00000000-0000-0000-0000-000000000000",
                symbol="RELIANCE",
                strategy_id="orb",
                direction="LONG",
                trigger_price=Decimal("100"),
                suggested_stop=Decimal("98"),
                timeframe_agreement=3,
                ai_confidence=Decimal("0.8"),
                ai_verdict="CONFIRM",
                ai_rationale="test",
                emitted_at=datetime.now(UTC),
                quantity=500,          # <- must be rejected
            )


class TestSecretsCannotLeak:
    """Rules 3–5 of §10.4 — no secret in logs, prompts, or error messages."""

    def test_str_is_redacted(self) -> None:
        assert str(SecretString("hunter2", name="pw")) == REDACTED

    def test_repr_is_redacted(self) -> None:
        assert "hunter2" not in repr(SecretString("hunter2", name="pw"))

    def test_fstring_is_redacted(self) -> None:
        secret = SecretString("hunter2", name="pw")
        assert "hunter2" not in f"password={secret}"

    def test_format_is_redacted(self) -> None:
        secret = SecretString("hunter2", name="pw")
        assert "hunter2" not in "{}".format(secret)  # noqa: UP032

    def test_cannot_be_pickled(self) -> None:
        import pickle

        with pytest.raises(TypeError):
            pickle.dumps(SecretString("hunter2", name="pw"))

    def test_reveal_is_explicit(self) -> None:
        assert SecretString("hunter2", name="pw").reveal() == "hunter2"

    def test_exception_message_does_not_leak(self) -> None:
        secret = SecretString("hunter2", name="pw")
        try:
            raise RuntimeError(f"auth failed for {secret}")
        except RuntimeError as exc:
            assert "hunter2" not in str(exc)


class TestConfigCannotDisableSafety:
    """§10.11 — configuration tunes the system; it can never disable safety."""

    def test_order_rate_cannot_exceed_sebi_safe_cap(self) -> None:
        with pytest.raises(ValidationError, match="exceeds the hard cap"):
            ExecutionConfig(max_orders_per_second=MAX_ORDERS_PER_SECOND + 1)

    def test_per_trade_risk_cannot_be_absurd(self) -> None:
        with pytest.raises(ValidationError, match="hard safety bound"):
            PerTradeRisk(risk_pct=Decimal("50"))

    def test_slots_cannot_over_allocate_capital(self) -> None:
        with pytest.raises(ValidationError, match="exceeds 100%"):
            RiskConfig(position_slots=10, capital_per_slot_pct=Decimal("20"))

    def test_t2t_filter_cannot_be_disabled(self) -> None:
        """Intraday trading is structurally impossible in the T2T segment."""
        with pytest.raises(ValidationError, match="cannot be disabled"):
            HardFilters(exclude_t2t=False)

    def test_ai_must_fail_closed(self) -> None:
        with pytest.raises(ValidationError, match="fails closed"):
            AIConfig(fallback_on_timeout="proceed_anyway")

    def test_human_approval_cannot_be_disabled(self) -> None:
        """Promotion of a strategy to live capital is always a human call."""
        with pytest.raises(ValidationError, match="cannot be disabled"):
            StrategyPromotionConfig(require_human_approval=False)

    def test_scoring_weights_must_sum_to_one(self) -> None:
        with pytest.raises(ValidationError, match="must sum to 1.0"):
            ScoringWeights(trend_alignment=Decimal("0.9"))

    def test_deployment_must_be_india_region(self) -> None:
        """SEBI requires algos to be hosted on Indian servers."""
        from algotrader.common.config import SystemConfig

        with pytest.raises(ValidationError, match="not an India region"):
            SystemConfig(deployment_region="us-east-1")


class TestNotificationSingleRecipient:
    """§2.3 — broadcasting signals can trigger SEBI Research Analyst duties."""

    def test_single_recipient_is_allowed(self) -> None:
        assert len(NotificationConfig(recipients=["12345"]).recipients) == 1

    def test_multiple_recipients_rejected(self) -> None:
        with pytest.raises(ValidationError):
            NotificationConfig(recipients=["12345", "67890"])


class TestLiveModeRequiresCompliance:
    def test_live_mode_requires_static_ip(self) -> None:
        with pytest.raises(ValidationError, match="static_ip"):
            AppConfig.model_validate(
                {"system": {"mode": "live", "static_ip": ""},
                 "broker": {"algo_id": "ALGO123"}}
            )

    def test_live_mode_requires_algo_id(self) -> None:
        with pytest.raises(ValidationError, match="algo_id"):
            AppConfig.model_validate(
                {"system": {"mode": "live", "static_ip": "1.2.3.4"},
                 "broker": {"algo_id": ""}}
            )

    def test_paper_mode_does_not_require_them(self) -> None:
        cfg = AppConfig.model_validate({"system": {"mode": "paper"}})
        assert cfg.system.mode.value == "paper"
