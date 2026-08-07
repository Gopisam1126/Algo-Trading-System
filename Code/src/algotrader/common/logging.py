"""Structured logging with mandatory secret redaction.

Every log line is JSON and carries ``correlation_id`` where available, so
tracing one trade from pre-market candidacy through signal, AI review, risk
decision, order, fill and exit is a single query.

The :class:`RedactingProcessor` is the important part.  It is installed as a
structlog processor *and* as a stdlib logging filter, so it applies to
third-party library output too — a broker SDK that helpfully logs the request
body must not be able to leak a session token.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Callable
from typing import Any

import structlog

from algotrader.common.secrets import REDACTED, SecretString

#: Patterns that look like credentials regardless of where they came from.
#:
#: These are a BACKSTOP, not the primary defence.  Exact-match scrubbing of
#: values loaded from the secrets provider (``known_values``) plus the
#: sensitive-key-name list below do the real work; regexes only catch
#: credentials that arrive from somewhere we did not load them, such as a
#: third-party SDK echoing a response body.
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    # Anthropic API keys
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}", re.I),
    # JWTs — match the three-part header.payload.signature structure rather
    # than assuming a minimum header length. A short-header token was
    # previously slipping through.
    re.compile(r"\beyJ[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]*"),
    # Bearer tokens in headers
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-.=]{8,}"),
    # Base32 TOTP seeds (typically 16-32 chars, A-Z and 2-7)
    re.compile(r"\b[A-Z2-7]{16,64}={0,6}\b(?=\s*(?:totp|seed|secret))", re.I),
    # Six-digit OTP adjacent to a totp/otp label
    re.compile(r"\b[0-9]{6}\b(?=\s*(?:totp|otp))", re.I),
    # Connection URIs with embedded credentials — postgresql://, redis://,
    # amqp://, https:// and friends. Only the password is replaced; the scheme,
    # user and host survive, because a redacted DSN that still says *which*
    # database failed is far more useful when debugging than a wall of asterisks.
    #
    # This is not hypothetical. SQLAlchemy, Alembic and psycopg all put the URL
    # into connection errors, and `DatabaseConfig.dsn()` returns a plain string
    # by necessity (SQLAlchemy requires one), so any `log.error("connecting to
    # %s", dsn)` would otherwise write the database password to disk in clear
    # text. Found by probing the redactor with a real DSN during the Sprint 1
    # security pass — every other pattern here passed and this one did not
    # exist.
    #
    # The username part is ``*`` not ``+`` on purpose: Redis URIs conventionally
    # omit the user entirely (``redis://:password@host``), which is precisely the
    # shape ``RedisConfig.dsn()`` produces. An earlier version of this pattern
    # required a username and let every Redis password through.
    re.compile(r"(?P<pre>\b[a-z][a-z0-9+.\-]*://[^\s:/@]*:)[^\s@]+(?P<post>@)", re.I),
    # Any credential-shaped assignment
    re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|token|totp|access_token)\b\s*[:=]\s*\S+"),
]

#: Patterns that keep surrounding context instead of replacing the whole match.
#: Keyed by the pattern above, value is the replacement template.
_PARTIAL_REDACTIONS: dict[int, str] = {
    # index of the connection-URI pattern -> keep scheme/user/host, mask password
    5: r"\g<pre>" + REDACTED + r"\g<post>",
}

#: Field names whose values are always redacted, whatever they contain.
_SENSITIVE_KEYS = frozenset({
    "password", "api_key", "apikey", "secret", "token", "access_token",
    "refresh_token", "totp", "totp_secret", "authorization", "auth",
    "client_secret", "private_key", "session_token", "feed_token",
})


class RedactingProcessor:
    """Scrubs secrets from log events.

    Three layers:
      1. Known secret VALUES loaded from the provider (exact match).
      2. Sensitive field NAMES (redact whatever the value is).
      3. Regex patterns for credential-shaped strings.
    """

    def __init__(self, known_values: set[str] | None = None) -> None:
        self._known = {v for v in (known_values or set()) if len(v) >= 8}

    def add_known_values(self, values: set[str]) -> None:
        self._known.update(v for v in values if len(v) >= 8)

    def _scrub_text(self, text: str) -> str:
        for value in self._known:
            if value in text:
                text = text.replace(value, REDACTED)
        for index, pattern in enumerate(_SECRET_PATTERNS):
            # Most patterns replace the whole match; a few keep context around
            # the secret (see _PARTIAL_REDACTIONS) so the message stays useful.
            replacement = _PARTIAL_REDACTIONS.get(index, REDACTED)
            text = pattern.sub(replacement, text)
        return text

    def _scrub_value(self, key: str, value: Any) -> Any:
        if isinstance(value, SecretString):
            return REDACTED
        if key.lower() in _SENSITIVE_KEYS:
            return REDACTED
        if isinstance(value, str):
            return self._scrub_text(value)
        if isinstance(value, dict):
            return {k: self._scrub_value(k, v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(self._scrub_value(key, v) for v in value)
        return value

    def __call__(
        self, _logger: Any, _name: str, event_dict: dict[str, Any]
    ) -> dict[str, Any]:
        return {k: self._scrub_value(k, v) for k, v in event_dict.items()}


class RedactingFilter(logging.Filter):
    """stdlib logging filter — catches third-party library output."""

    def __init__(self, processor: RedactingProcessor) -> None:
        super().__init__()
        self._processor = processor

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._processor._scrub_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: self._processor._scrub_value(str(k), v) for k, v in record.args.items()
                }
            else:
                record.args = tuple(
                    self._processor._scrub_value("", a) for a in record.args
                )
        return True


_redactor = RedactingProcessor()


def register_secret_values(values: set[str]) -> None:
    """Tell the logging layer about loaded secrets so it can scrub them.

    Call this immediately after loading secrets, before any other work.
    """
    _redactor.add_known_values(values)


def configure_logging(
    *,
    level: str = "INFO",
    service: str = "algotrader",
    json_output: bool = True,
) -> None:
    """Install structlog + stdlib logging with redaction on both paths."""

    processors: list[Callable[..., Any]] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _redactor,                                   # <- redaction, always on
        structlog.processors.EventRenamer("message"),
    ]
    processors.append(
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RedactingFilter(_redactor))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    structlog.contextvars.bind_contextvars(service=service)

    # These libraries log request bodies at DEBUG — never enable that here.
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]


def bind_correlation(correlation_id: str) -> None:
    """Bind a correlation id for the current async task.

    Everything logged downstream carries it, which is what makes end-to-end
    trade tracing a single query.
    """
    structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
