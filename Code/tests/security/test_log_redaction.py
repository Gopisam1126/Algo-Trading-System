"""Log redaction must hold on every path a secret could take.

Regression note: the JWT pattern originally required a 20+ character header
after ``eyJ``, which real short-header tokens do not have — so a JWT could
appear in logs unredacted. Now matched on the three-part
header.payload.signature structure instead.
"""

from __future__ import annotations

import pytest

structlog = pytest.importorskip("structlog", reason="structlog is a runtime dependency")

from algotrader.common.logging import RedactingProcessor  # noqa: E402
from algotrader.common.secrets import SecretString  # noqa: E402

KNOWN = "MYBROKERSECRET99"


@pytest.fixture
def redactor() -> RedactingProcessor:
    return RedactingProcessor({KNOWN})


def render(redactor: RedactingProcessor, event: dict) -> str:
    return str(redactor(None, "", dict(event)))


class TestKnownValueScrubbing:
    def test_value_in_message(self, redactor: RedactingProcessor) -> None:
        assert KNOWN not in render(redactor, {"message": f"auth with {KNOWN} ok"})

    def test_value_in_list(self, redactor: RedactingProcessor) -> None:
        assert KNOWN not in render(redactor, {"items": [KNOWN, "safe"]})

    def test_value_in_nested_dict(self, redactor: RedactingProcessor) -> None:
        assert KNOWN not in render(redactor, {"outer": {"inner": KNOWN}})


class TestSensitiveKeyNames:
    @pytest.mark.parametrize(
        "key",
        ["password", "api_key", "secret", "access_token", "totp_secret", "authorization"],
    )
    def test_value_redacted_by_key_name(self, redactor: RedactingProcessor, key: str) -> None:
        assert "sensitive-payload" not in render(redactor, {key: "sensitive-payload"})

    def test_nested_sensitive_key(self, redactor: RedactingProcessor) -> None:
        assert "nested-leak" not in render(redactor, {"cfg": {"api_key": "nested-leak"}})


class TestPatternMatching:
    def test_anthropic_key(self, redactor: RedactingProcessor) -> None:
        # Assembled at runtime so secret scanners (gitleaks) don't flag this
        # test fixture as a committed credential. It is not a real key.
        fake = "sk-" + "ant-" + "api03-" + "AbCdEf1234567890XyZ"
        out = render(redactor, {"message": f"key {fake} end"})
        assert "AbCdEf1234567890XyZ" not in out

    def test_short_header_jwt(self, redactor: RedactingProcessor) -> None:
        """Regression: short-header JWTs previously slipped through."""
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc"
        assert "eyJzdWIiOiIxIn0" not in render(redactor, {"message": f"token {jwt}"})

    def test_long_jwt(self, redactor: RedactingProcessor) -> None:
        jwt = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
               ".eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4")
        assert "SflKxwRJSM" not in render(redactor, {"message": jwt})

    def test_bearer_header(self, redactor: RedactingProcessor) -> None:
        out = render(redactor, {"message": "Authorization: Bearer abc123def456ghi789"})
        assert "abc123def456ghi789" not in out

    def test_credential_assignment(self, redactor: RedactingProcessor) -> None:
        out = render(redactor, {"message": "access_token=aBcD1234EfGh5678IjKl"})
        assert "aBcD1234EfGh5678IjKl" not in out


class TestSecretStringHandling:
    def test_secret_string_never_renders(self, redactor: RedactingProcessor) -> None:
        assert "TOPSECRETVAL" not in render(
            redactor, {"cred": SecretString("TOPSECRETVAL", name="c")}
        )


class TestNoFalsePositives:
    """Redaction that eats ordinary trading data is its own failure mode."""

    def test_trading_text_survives(self, redactor: RedactingProcessor) -> None:
        out = render(redactor, {"message": "RELIANCE LONG 120 @ 712.40 stop 705.20"})
        for token in ("RELIANCE", "LONG", "712.40", "705.20"):
            assert token in out

    def test_numeric_fields_survive(self, redactor: RedactingProcessor) -> None:
        out = render(redactor, {"quantity": 120, "confidence": 0.82, "symbol": "INFY"})
        assert "120" in out and "0.82" in out and "INFY" in out
