"""QA-SEC-16, found again one layer up — and the detail it flooded the log with.

Two findings from E14-S02's adversarial round, both verified against running
code before being fixed.

**Log forgery.** QA-SEC-16 closed a hole where a newline in an instrument
symbol forged a whole line in the risk log. The fix was applied to
``OrderRequest`` and stopped there. But symbols come from the broker's daily
instrument dump — external data this system does not author — and the same
value reaches ``Trigger`` and ``Recommendation`` *first*, both of which the
risk engine logs on every rejection. Probing it produced this, from a real
captured handler::

    INFO risk REJECTED INFY
    CRITICAL kill switch disarmed by operator (KILL_SWITCH_ACTIVE): ...
    CRITICAL kill switch disarmed by operator LONG not evaluated further)

Two fabricated CRITICAL lines in the log an incident is reconstructed from.
The lesson is the one the fix encodes: a validator belongs at *every* boundary
the untrusted value crosses, defined once so it cannot be applied to only some
of them.

**Unbounded detail.** A rejection detail reaches both the audit payload and a
log line, once per rejected candidate per bar. The health gate named every
unhealthy service, so 5000 of them produced a 48,957-character detail — at
precisely the moment the system is already unwell.
"""

from __future__ import annotations

import datetime as dt
import io
import logging
from decimal import Decimal
from uuid import UUID

import pytest

from algotrader.common.enums import AIVerdict, Direction
from algotrader.common.models.trading import Recommendation, Trigger
from algotrader.execution.risk.checks import KILL_SWITCH_CHECK, check_health_gate
from algotrader.execution.risk.context import RiskContext
from algotrader.execution.risk.framework import RiskEngine

NOW = dt.datetime(2026, 8, 25, 4, 30, tzinfo=dt.UTC)


def _trigger(symbol: str) -> Trigger:
    return Trigger(
        correlation_id=UUID(int=1),
        symbol=symbol,
        strategy_id="s",
        direction=Direction.LONG,
        trigger_price=Decimal("100"),
        suggested_stop=Decimal("95"),
        timeframe_agreement=3,
        fired_at=NOW,
    )


def _recommendation(symbol: str) -> Recommendation:
    return Recommendation(
        correlation_id=UUID(int=1),
        symbol=symbol,
        strategy_id="s",
        direction=Direction.LONG,
        trigger_price=Decimal("100"),
        suggested_stop=Decimal("95"),
        timeframe_agreement=3,
        ai_confidence=Decimal("0.5"),
        ai_verdict=AIVerdict.CONFIRM,
        ai_rationale="r",
        emitted_at=NOW,
    )


def _context(**overrides: object) -> RiskContext:
    kwargs: dict[str, object] = {
        "now": NOW,
        "squareoff_deadline": NOW + dt.timedelta(hours=4),
        "capital": Decimal("500000"),
        "slots_total": 5,
        "slots_used": 0,
    }
    kwargs.update(overrides)
    return RiskContext(**kwargs)  # type: ignore[arg-type]


#: Each is a real attack shape, not a fuzz artefact. The first two forge log
#: lines; the third collides with a Redis key namespace; the fourth is a glob
#: that could widen a lookup; the last two are the length boundaries.
HOSTILE = (
    pytest.param("INFY\nCRITICAL kill switch disarmed", id="newline-forges-a-log-line"),
    pytest.param("INFY\r\nWARN feed healthy", id="crlf-forges-a-log-line"),
    pytest.param("INFY:TCS", id="colon-collides-with-a-redis-key"),
    pytest.param("INFY*", id="glob-metacharacter"),
    pytest.param("", id="empty"),
    pytest.param("A" * 65, id="over-the-length-cap"),
)

#: The control. A validator that rejected everything would pass every test
#: above and break the system. These are shapes NSE actually issues.
REAL = ("INFY", "M&M", "BAJAJ-AUTO", "L&TFH", "IDEA_EQ", "NIFTY.NS", "20MICRONS")


class TestSymbolValidationReachesEveryBoundary:
    @pytest.mark.parametrize("symbol", HOSTILE)
    def test_trigger_refuses_a_hostile_symbol(self, symbol: str) -> None:
        with pytest.raises(ValueError):
            _trigger(symbol)

    @pytest.mark.parametrize("symbol", HOSTILE)
    def test_recommendation_refuses_a_hostile_symbol(self, symbol: str) -> None:
        with pytest.raises(ValueError):
            _recommendation(symbol)

    @pytest.mark.parametrize("symbol", REAL)
    def test_the_control_real_nse_symbols_still_construct(self, symbol: str) -> None:
        assert _trigger(symbol).symbol == symbol
        assert _recommendation(symbol).symbol == symbol

    def test_build_cannot_launder_a_symbol_past_the_validator(self) -> None:
        """``Recommendation.build`` copies ``trigger.symbol`` across. It is only
        safe because the Trigger was validated — assert the chain, since a
        future constructor that skipped Trigger would reopen the hole."""
        with pytest.raises(ValueError):
            _trigger("INFY\nCRITICAL")

    def test_one_definition_serves_every_boundary(self) -> None:
        """The finding was not a missing check but a check applied to one of
        three boundaries. Assert they share a definition rather than each
        having their own, which is how they drift apart again."""
        from algotrader.common.models import trading

        assert trading._SAFE_SYMBOL.fullmatch("INFY") is not None
        assert trading._SAFE_SYMBOL.fullmatch("INFY\nX") is None
        for model in (Trigger, Recommendation):
            validators = model.__pydantic_decorators__.field_validators
            symbol_validators = [v for v in validators.values() if "symbol" in v.info.fields]
            assert symbol_validators, f"{model.__name__} has no validator on symbol"


class TestNoForgedLineReachesTheRiskLog:
    """The end-to-end probe. The unit tests above assert the validator; this
    asserts the property the validator exists to protect."""

    @staticmethod
    def _capture_rejection(symbol: str) -> list[str]:
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger = logging.getLogger("algotrader.execution.risk.framework")
        logger.addHandler(handler)
        previous = logger.level
        logger.setLevel(logging.INFO)
        try:
            RiskEngine(checks=[KILL_SWITCH_CHECK]).evaluate(
                _recommendation(symbol), _context(kill_switch_active=True)
            )
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)
        return [line for line in buf.getvalue().splitlines() if line.strip()]

    def test_a_rejection_occupies_exactly_one_line(self) -> None:
        lines = self._capture_rejection("INFY")
        assert len(lines) == 1, f"a rejection produced {len(lines)} log lines: {lines}"

    def test_the_line_is_the_rejection_it_claims_to_be(self) -> None:
        """The control for the test above: one line is only meaningful if it is
        the right line. A silently-dropped log would also pass with one line."""
        (line,) = self._capture_rejection("INFY")
        assert "REJECTED INFY" in line
        assert "KILL_SWITCH_ACTIVE" in line


class TestRejectionDetailsAreBounded:
    @staticmethod
    def _detail(count: int) -> str:
        ctx = _context(unhealthy_services=tuple(f"svc-{i}" for i in range(count)))
        return check_health_gate(_recommendation("INFY"), ctx).detail

    def test_a_flood_of_unhealthy_services_does_not_flood_the_detail(self) -> None:
        detail = self._detail(5000)
        assert len(detail) < 500, f"detail is {len(detail)} characters"

    def test_the_count_stays_exact_when_the_list_is_truncated(self) -> None:
        """Truncating the names must not truncate the number. *How many* is the
        part an operator acts on; *which* is a convenience."""
        detail = self._detail(5000)
        assert "5000 service(s)" in detail
        assert "and 4988 more" in detail

    def test_a_realistic_outage_still_names_every_service(self) -> None:
        """The control. A cap that truncated the normal case would have made
        the detail useless to fix a real problem — the system has around a
        dozen services, and three being down is the case that matters."""
        detail = self._detail(3)
        assert "svc-0" in detail
        assert "svc-2" in detail
        assert "more" not in detail
