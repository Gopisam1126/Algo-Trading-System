"""Free text from the AI, and a strategy's id, can never end a log line.

Audit SEC-013 (17 Nov 2026). ``one_safe_line`` - the one defence against log
forging - escaped only C0 controls and DEL, while ``str.splitlines`` and any
Unicode-aware reader also break at U+0085, U+2028 and U+2029; and
``Recommendation.ai_rationale``, ``AIReview``'s free-text fields and
``strategy_id`` were not escaped or restricted at all. The rationale will be
an LLM's output, which is untrusted by construction.
"""

from __future__ import annotations

import datetime as dt
import unicodedata
import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from algotrader.common.enums import AIVerdict, Direction
from algotrader.common.models.trading import AIReview, Recommendation, Trigger
from algotrader.common.text import escape_controls, one_safe_line

NOW = dt.datetime(2026, 11, 17, 5, 30, tzinfo=dt.UTC)

#: Each one a line break to some reader. Built from code points so no editor
#: or tool can silently turn them into the characters they name.
BREAKS = [chr(c) for c in (0x0A, 0x0D, 0x0B, 0x0C, 0x1C, 0x1D, 0x1E, 0x85, 0x2028, 0x2029)]
FORGED = "CRITICAL kill switch disarmed by operator"


def one_line(text: str) -> bool:
    return len(text.splitlines()) <= 1 and not any(
        unicodedata.category(c) in {"Cc", "Zl", "Zp"} for c in text
    )


def review(**overrides: object) -> AIReview:
    base: dict = {
        "verdict": AIVerdict.CONFIRM,
        "confidence": Decimal("0.8"),
        "timeframe_agreement": 2,
        "thesis_alignment": "aligned",
        "supporting_factors": ["volume"],
        "risk_factors": ["news"],
        "rationale": "fine",
        "model_used": "claude-sonnet-5",
        "latency_ms": 10,
    }
    base.update(overrides)
    return AIReview(**base)


def recommendation(**overrides: object) -> Recommendation:
    base: dict = {
        "correlation_id": uuid.uuid4(),
        "symbol": "INFY",
        "strategy_id": "orb_classic",
        "direction": Direction.LONG,
        "trigger_price": Decimal("1500"),
        "suggested_stop": Decimal("1490"),
        "timeframe_agreement": 2,
        "ai_confidence": Decimal("0.8"),
        "ai_verdict": AIVerdict.CONFIRM,
        "ai_rationale": "fine",
        "emitted_at": NOW,
    }
    base.update(overrides)
    return Recommendation(**base)


class TestTheEscapeCoversEveryLineBreak:
    @pytest.mark.parametrize("brk", BREAKS, ids=lambda c: f"U+{ord(c):04X}")
    def test_one_safe_line_leaves_one_line(self, brk: str) -> None:
        out = one_safe_line(f"blocked{brk}{FORGED}")
        assert one_line(out), repr(out)
        assert FORGED in out, "the text is escaped, not discarded"

    def test_ordinary_unicode_is_untouched(self) -> None:
        """The control: a rupee sign and Devanagari are text, not breaks."""
        text = "₹ रिलायंस crossed 1500"
        assert escape_controls(text) == text


class TestTheAiFieldsAreOneLine:
    @pytest.mark.parametrize("brk", BREAKS, ids=lambda c: f"U+{ord(c):04X}")
    def test_the_rationale(self, brk: str) -> None:
        rec = recommendation(ai_rationale=f"looks good{brk}{FORGED}")
        assert one_line(rec.ai_rationale)

    @pytest.mark.parametrize("field", ["rationale", "thesis_alignment", "model_used"], ids=str)
    def test_every_review_text_field(self, field: str) -> None:
        value = "x" + chr(0x2028) + "y"
        assert one_line(getattr(review(**{field: value}), field))

    def test_the_factor_lists(self) -> None:
        r = review(supporting_factors=["a" + chr(0x85) + "b"], risk_factors=["c\nd"])
        assert all(one_line(f) for f in [*r.supporting_factors, *r.risk_factors])

    def test_built_from_a_review_the_rationale_is_still_one_line(self) -> None:
        trigger = Trigger(
            symbol="INFY",
            strategy_id="orb_classic",
            direction=Direction.LONG,
            trigger_price=Decimal("1500"),
            suggested_stop=Decimal("1490"),
            timeframe_agreement=2,
            fired_at=NOW,
        )
        rec = Recommendation.build(trigger, review(rationale="ok\n" + FORGED), now=NOW)
        assert one_line(rec.ai_rationale)


class TestAStrategyIdIsAnId:
    @pytest.mark.parametrize(
        "bad",
        ["orb\nCRITICAL", "orb" + chr(0x2028) + "x", "a:b", "", "x" * 65, "orb classic"],
        ids=["newline", "u2028", "colon", "empty", "too-long", "space"],
    )
    def test_on_a_trigger_and_a_recommendation(self, bad: str) -> None:
        with pytest.raises(ValidationError, match="strategy_id"):
            recommendation(strategy_id=bad)
        with pytest.raises(ValidationError, match="strategy_id"):
            Trigger(
                symbol="INFY",
                strategy_id=bad,
                direction=Direction.LONG,
                trigger_price=Decimal("1500"),
                suggested_stop=Decimal("1490"),
                timeframe_agreement=2,
                fired_at=NOW,
            )

    @pytest.mark.parametrize("good", ["orb_classic", "orb_long_v1", "s", "ORB-2.v1"])
    def test_the_control_real_ids_pass(self, good: str) -> None:
        assert recommendation(strategy_id=good).strategy_id == good
