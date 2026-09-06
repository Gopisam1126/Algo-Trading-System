"""Tests that the design's safety invariants hold structurally.

These are not ordinary unit tests.  Each one asserts a property the whole
architecture depends on — if one of these fails, a documented safety
guarantee has been silently removed and the corresponding design decision
needs revisiting before the code ships.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import ClassVar
from uuid import UUID

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

#: Sentinel for the AUDIT-005 probes: 'do not pass this field at all',
#: which is a different claim from passing None.
_OMIT = object()


class TestAICannotSizePositions:
    """Constraint C4 — the LLM must never compute position size or place orders.

    Enforced structurally: ``Recommendation`` is the only type that crosses
    from the AI layer to the risk engine, and it has no field through which
    size could be expressed.
    """

    FORBIDDEN: ClassVar[set[str]] = {
        "quantity",
        "qty",
        "size",
        "position_size",
        "capital_at_risk",
        "stop_price",
        "notional",
        "amount",
        "rupees",
        "lots",
        "value",
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
                quantity=500,  # <- must be rejected
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
        with pytest.raises(ValidationError, match=r"must sum to 1\.0"):
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
                {"system": {"mode": "live", "static_ip": ""}, "broker": {"algo_id": "ALGO123"}}
            )

    def test_live_mode_requires_algo_id(self) -> None:
        with pytest.raises(ValidationError, match="algo_id"):
            AppConfig.model_validate(
                {"system": {"mode": "live", "static_ip": "1.2.3.4"}, "broker": {"algo_id": ""}}
            )

    def test_paper_mode_does_not_require_them(self) -> None:
        cfg = AppConfig.model_validate({"system": {"mode": "paper"}})
        assert cfg.system.mode.value == "paper"


class TestReadOnlyAdapterCannotTrade:
    """The read-only / trading adapter split is a security boundary.

    ``LOW_LEVEL_ARCHITECTURE.md §10.3`` and `MASTER_REFERENCE §1.1` both state
    that compromising any service other than execution-svc cannot place an
    order. That guarantee rests entirely on three ``raise PermissionError``
    lines in ``ReadOnlyGuard`` — which had no test at all until QA measured
    coverage and found ``broker/adapter.py`` at 0%.

    A claim this load-bearing needs a probe, not a code read: delete the raises
    and every other test in this repository still passes.
    """

    @staticmethod
    def _guard() -> object:
        from algotrader.broker.adapter import ReadOnlyGuard

        return ReadOnlyGuard()

    async def test_place_order_is_refused(self) -> None:
        import pytest as _pytest

        with _pytest.raises(PermissionError, match="read-only"):
            await self._guard().place_order(object())  # type: ignore[attr-defined]

    async def test_modify_order_is_refused(self) -> None:
        import pytest as _pytest

        with _pytest.raises(PermissionError, match="read-only"):
            await self._guard().modify_order()  # type: ignore[attr-defined]

    async def test_cancel_order_is_refused(self) -> None:
        import pytest as _pytest

        with _pytest.raises(PermissionError, match="read-only"):
            await self._guard().cancel_order("X123")  # type: ignore[attr-defined]

    def test_the_guard_covers_every_state_changing_method(self) -> None:
        """The real risk is a NEW mutating method the guard does not override.

        ``TradingAdapter`` gaining ``place_gtt`` or ``exit_position`` without a
        matching override would leave a read-only adapter able to move real
        money. This fails the moment that happens.

        Scoped to *mutating* verbs deliberately. ``TradingAdapter`` also adds
        account reads — ``fetch_margins``, ``fetch_positions``,
        ``fetch_orderbook``, ``find_by_client_order_id`` — which are correctly
        absent from ``MarketDataAdapter`` but change nothing at the broker and
        so need no guard.
        """
        from algotrader.broker.adapter import ReadOnlyGuard, TradingAdapter

        mutating_verbs = (
            "place",
            "modify",
            "cancel",
            "exit",
            "square_off",
            "squareoff",
            "submit",
            "close",
            "amend",
            "convert",
        )
        trading = {n for n in dir(TradingAdapter) if not n.startswith("_")}
        mutating = {n for n in trading if any(n.startswith(v) for v in mutating_verbs)}
        guarded = {n for n in vars(ReadOnlyGuard) if not n.startswith("_")}

        assert mutating, "no mutating methods found — the verb list has gone stale"
        unguarded = mutating - guarded
        assert not unguarded, (
            f"TradingAdapter exposes {sorted(unguarded)} which ReadOnlyGuard does "
            f"not override, so a read-only adapter could change broker state"
        )


class TestBrokerSessionExpiry:
    """C3 — daily re-auth. An expiry predicate that is wrong fails open."""

    def test_a_past_expiry_reads_as_expired(self) -> None:
        import datetime as _dt

        from algotrader.broker.adapter import BrokerSession

        now = _dt.datetime.now(_dt.UTC)
        s = BrokerSession(
            broker="zerodha",
            client_id="AB1234",
            authenticated_at=now - _dt.timedelta(hours=9),
            expires_at=now - _dt.timedelta(seconds=1),
        )
        assert s.is_expired

    def test_a_future_expiry_reads_as_live(self) -> None:
        import datetime as _dt

        from algotrader.broker.adapter import BrokerSession

        now = _dt.datetime.now(_dt.UTC)
        s = BrokerSession(
            broker="zerodha",
            client_id="AB1234",
            authenticated_at=now,
            expires_at=now + _dt.timedelta(hours=8),
        )
        assert not s.is_expired

    def test_the_session_carries_no_token_field(self) -> None:
        """Invariant 3 at the session boundary — a token here would be logged."""
        from algotrader.broker.adapter import BrokerSession

        fields = set(BrokerSession.model_fields)
        leaky = {f for f in fields if any(k in f.lower() for k in ("token", "secret", "password"))}
        assert not leaky, f"BrokerSession exposes {leaky}; tokens belong in SecretString"


class TestSessionRiskStateIsNeverAssumed:
    """AUDIT-005 — invariant 6 (**fail closed**) at the one place it was not.

    ``RiskContext`` carried six fields describing the session's risk state,
    and every one of them had a permissive default::

        kill_switch_active: bool = False
        unhealthy_services: tuple[str, ...] = ()
        realised_pnl_today: Decimal = Decimal(0)
        consecutive_losses: int = 0
        daily_loss_halted: bool = False
        consecutive_loss_halted: bool = False

    So the answer to *"is the kill switch on?"* was **no** whenever nobody
    asked. A Redis timeout, a half-built context, or a field dropped in a
    future refactor each read as "nothing is halted, trade" — a fail-open on
    the most absolute control in the system, and one that no test could catch,
    because a test that omits the field is indistinguishable from one that
    means "off".

    The context's own docstring already stated the rule these six broke, on
    the field right above them: *"``None`` means the broker did not answer,
    which is a rejection and never an assumption."* The fix was to make these
    six follow it — the same ``| None`` + :meth:`RiskContext.require` shape the
    module already used for margin, restrictions, ATR and margin-per-share.

    Asserted through the **engine**, not the check functions, because the
    engine is what turns a raised ``RiskContextError`` into a refusal. A check
    tested directly would only show that it raises, which is not the same
    claim as "the system does not trade".
    """

    #: The six fields, with a value that is a genuine, checked "all clear".
    HEALTHY: ClassVar[dict[str, object]] = {
        "kill_switch_active": False,
        "unhealthy_services": (),
        "realised_pnl_today": Decimal(0),
        "consecutive_losses": 0,
        "daily_loss_halted": False,
        "consecutive_loss_halted": False,
    }

    @staticmethod
    def _at(hour: int, minute: int) -> datetime:
        from algotrader.common.calendar import IST

        naive = datetime(2026, 8, 25, hour, minute)
        return naive.replace(tzinfo=IST).astimezone(UTC)

    def _decide(self, **omit_or_override: object):
        """Run the real fourteen-check engine over one mid-session moment."""
        from algotrader.common.calendar import IST, MarketCalendar
        from algotrader.common.enums import AIVerdict, Direction
        from algotrader.execution.risk.checks.loss import build_loss_checks
        from algotrader.execution.risk.checks.preconditions import (
            build_precondition_checks,
        )
        from algotrader.execution.risk.context import RiskContext
        from algotrader.execution.risk.framework import RiskEngine
        from algotrader.execution.sizer import SizingPolicy, build_sizer

        state = dict(self.HEALTHY)
        for key, value in omit_or_override.items():
            if value is _OMIT:
                del state[key]
            else:
                state[key] = value

        now = self._at(11, 0)
        deadline = datetime(2026, 8, 25, 15, 10, tzinfo=IST).astimezone(UTC)
        rec = Recommendation(
            symbol="INFY",
            direction=Direction.LONG,
            correlation_id=UUID("11111111-2222-3333-4444-555555555555"),
            strategy_id="probe",
            trigger_price=Decimal("1200.00"),
            suggested_stop=Decimal("1186.45"),
            timeframe_agreement=3,
            ai_confidence=Decimal("0.82"),
            ai_verdict=AIVerdict.CONFIRM,
            ai_rationale="probe",
            emitted_at=now,
        )
        calendar = MarketCalendar(holidays=frozenset(), covers_years=(2026,))
        engine = RiskEngine(
            checks=[
                *build_precondition_checks(calendar, ()),
                *build_loss_checks(max_daily_loss_pct=Decimal("3.0"), consecutive_loss_halt=3),
            ],
            sizer=build_sizer(
                SizingPolicy(
                    risk_pct=Decimal("1.0"),
                    atr_multiplier_stop=Decimal("1.5"),
                    max_position_pct=Decimal("20"),
                    capital_per_slot_pct=Decimal("20"),
                    target_r_multiple=Decimal("2.0"),
                )
            ),
        )
        ctx = RiskContext(
            now=now,
            squareoff_deadline=deadline,
            capital=Decimal("500000"),
            slots_total=5,
            slots_used=0,
            # What the sizer needs, so the control is a real APPROVAL rather
            # than "cleared every check and then had nowhere to go".
            atr=Decimal("13.5000"),
            lot_size=1,
            available_margin=Decimal("400000"),
            margin_per_share=Decimal("240"),
            **state,  # type: ignore[arg-type]
        )
        return engine.evaluate(rec, ctx)

    def test_the_control_a_fully_stated_healthy_session_clears(self) -> None:
        """Without this, every assertion below is satisfied by a system that
        refuses everything — which is not fail-closed, it is broken."""

        decision = self._decide()
        assert decision.approved, f"a fully stated healthy session was refused: {decision.detail}"
        assert decision.sizing is not None
        assert decision.sizing.quantity > 0

    @pytest.mark.parametrize("field", list(HEALTHY))
    def test_an_unstated_field_refuses_rather_than_assumes(self, field: str) -> None:
        """Each of the six, one at a time. Omitting it must not read as
        'clear' — it must stop the decision."""
        from algotrader.execution.risk.framework import RejectReason

        decision = self._decide(**{field: _OMIT})
        assert not decision.approved
        assert decision.reason is RejectReason.RISK_ENGINE_FAULT, (
            f"omitting {field} produced {decision.reason}, not an engine fault. "
            f"An unstated session risk state must never read as 'nothing is halted'."
        )

    @pytest.mark.parametrize("field", list(HEALTHY))
    def test_the_refusal_names_what_was_missing(self, field: str) -> None:
        """An operator seeing this at 09:20 needs to know which input was
        absent, or the fail-closed behaviour is just an outage."""
        decision = self._decide(**{field: _OMIT})
        assert decision.detail
        assert len(decision.detail) < 512

    def test_an_armed_kill_switch_still_stops_the_session(self) -> None:
        """The control on the control: making the field nullable must not have
        cost the check its actual job."""
        from algotrader.execution.risk.framework import RejectReason

        decision = self._decide(kill_switch_active=True)
        assert not decision.approved
        assert decision.reason is RejectReason.KILL_SWITCH_ACTIVE

    def test_a_degraded_service_still_stops_the_session(self) -> None:
        from algotrader.execution.risk.framework import RejectReason

        decision = self._decide(unhealthy_services=("ingest-svc",))
        assert not decision.approved
        assert decision.reason is RejectReason.HEALTH_GATE_FAILED

    def test_a_latched_halt_still_stops_the_session(self) -> None:
        from algotrader.execution.risk.framework import RejectReason

        decision = self._decide(daily_loss_halted=True)
        assert not decision.approved
        assert decision.reason is RejectReason.DAILY_LOSS_LIMIT

    def test_no_session_risk_field_still_carries_a_permissive_default(self) -> None:
        """The structural claim, asserted on the type rather than on behaviour.

        The six tests above would all still pass if someone re-added a default
        of ``False`` *and* every caller kept passing the value explicitly —
        the hole would be back and invisible. This reads the dataclass.
        """
        import dataclasses

        from algotrader.execution.risk.context import RiskContext

        offenders = [
            f.name
            for f in dataclasses.fields(RiskContext)
            if f.name in self.HEALTHY and f.default is not None
        ]
        assert not offenders, (
            f"{offenders} carry a non-None default again. A permissive default "
            f"means 'nobody asked' is indistinguishable from 'nothing is halted', "
            f"which is the fail-open AUDIT-005 closed."
        )
