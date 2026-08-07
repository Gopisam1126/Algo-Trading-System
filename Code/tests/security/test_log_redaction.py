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
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4"
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


class TestConnectionUriRedaction:
    """A DSN in a log line is a database password on disk.

    Regression: the redactor caught JWTs, bearer tokens and TOTP seeds, but
    nothing matched ``scheme://user:password@host``. SQLAlchemy, Alembic and
    psycopg all put the URL into connection errors, and ``DatabaseConfig.dsn()``
    must return a plain string because SQLAlchemy requires one — so a single
    ``log.error("connecting to %s", dsn)`` wrote the password out in clear text.

    Found by probing the redactor with a real DSN, not by reading it.
    """

    @pytest.mark.parametrize(
        ("uri", "secret"),
        [
            ("postgresql+psycopg://algotrader:SuperSecret123@localhost:5432/db", "SuperSecret123"),
            # percent-encoded passwords must not slip through in escaped form
            ("postgresql+psycopg://u:p%40ss%2Fw0rd@db:5432/x", "p%40ss%2Fw0rd"),
            # Redis conventionally omits the username entirely — this is the
            # exact shape RedisConfig.dsn() emits, and the first version of the
            # pattern required a username and let it through.
            ("redis://:MyRedisPass@redis:6379/0", "MyRedisPass"),
            ("redis://default:MyRedisPass@redis:6379/0", "MyRedisPass"),
            ("amqp://user:brokerpw@rabbit:5672/", "brokerpw"),
            ("https://apiuser:hunter2@api.example.com/v1", "hunter2"),
            ("(OperationalError) failed for postgresql://u:LeakMe99@h:5432/d", "LeakMe99"),
        ],
    )
    def test_password_is_stripped_from_uri(
        self, redactor: RedactingProcessor, uri: str, secret: str
    ) -> None:
        out = render(redactor, {"message": uri})
        assert secret not in out

    def test_host_and_scheme_survive(self, redactor: RedactingProcessor) -> None:
        """Only the password goes.

        A redacted DSN that still names the database is far more useful when
        debugging a connection failure than a wall of asterisks.
        """
        out = render(
            redactor,
            {"message": "postgresql+psycopg://algotrader:pw123456@tsdb:5432/algotrader"},
        )
        for kept in ("postgresql", "algotrader", "tsdb", "5432"):
            assert kept in out
        assert "pw123456" not in out

    def test_the_dsns_this_codebase_actually_generates(self, redactor: RedactingProcessor) -> None:
        """Close the loop against the real producers, not just handwritten strings."""
        from algotrader.common.config import DatabaseConfig, RedisConfig

        db_secret, redis_secret = "RealDbPassword99", "RealRedisPass99"
        for dsn, secret in (
            (DatabaseConfig().dsn(db_secret), db_secret),
            (RedisConfig().dsn(redis_secret), redis_secret),
        ):
            assert secret not in render(redactor, {"message": dsn})

    def test_credentialless_uri_is_untouched(self, redactor: RedactingProcessor) -> None:
        out = render(redactor, {"message": "postgresql+psycopg://algotrader@localhost:5432/db"})
        assert "REDACTED" not in out


class TestNoFalsePositives:
    """Redaction that eats ordinary trading data is its own failure mode."""

    def test_trading_text_survives(self, redactor: RedactingProcessor) -> None:
        out = render(redactor, {"message": "RELIANCE LONG 120 @ 712.40 stop 705.20"})
        for token in ("RELIANCE", "LONG", "712.40", "705.20"):
            assert token in out

    def test_numeric_fields_survive(self, redactor: RedactingProcessor) -> None:
        out = render(redactor, {"quantity": 120, "confidence": 0.82, "symbol": "INFY"})
        assert "120" in out and "0.82" in out and "INFY" in out
