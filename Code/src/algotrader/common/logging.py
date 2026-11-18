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
import traceback
from collections.abc import Callable
from typing import Any

import structlog

from algotrader.common.secrets import REDACTED, SecretString, known_secret_values

#: A key NAME that marks its value as a credential - matched as a SUFFIX, so
#: ``request_token``, ``api_secret``, ``KITE_API_SECRET`` and ``enctoken`` are
#: all caught. The first version matched whole words, and ``_`` is a word
#: character, so the keyword inside ``request_token`` had no boundary and every
#: prefixed key passed through in clear (audit SEC-011, 17 Nov 2026).
_CREDENTIAL_NAME = (
    r"[A-Za-z0-9_\-]*(?:api[_-]?key|apikey|api[_-]?secret|secret|password|passwd|totp"
    r"|checksum|signature|private[_-]?key|token)"
)

#: Key names that END like a credential and are not one. Kite's instrument and
#: exchange tokens are public numeric ids; redacting them would blind every
#: feed log line.
_NOT_SECRETS = frozenset({"instrument_token", "exchange_token"})

#: A credential VALUE after ``key=`` or ``key:``. A quoted value is one token
#: - ``password="my pass phrase"`` used to keep everything after the first
#: space (SEC-011) - and an unquoted one runs to the next whitespace.
_ASSIGNED_VALUE = r"""(?:"[^"]*"|'[^']*'|\S+)"""


def _unless_public(replacement: str) -> Callable[[re.Match[str]], str]:
    """A replacement that leaves a match alone when its key is a public id."""

    def replace(match: re.Match[str]) -> str:
        if match.group("key").lower() in _NOT_SECRETS:
            return match.group(0)
        return match.expand(replacement)

    return replace


#: Patterns that look like credentials regardless of where they came from,
#: each with what replaces a match. Most replace the whole match; some keep
#: the key or the scheme so the line still says WHAT was hidden.
#:
#: These are a BACKSTOP, not the primary defence.  Exact-match scrubbing of
#: values loaded from the secrets provider (``known_values``) plus the
#: sensitive-key-name rule below do the real work; regexes only catch
#: credentials that arrive from somewhere we did not load them, such as a
#: third-party SDK echoing a response body.
#:
#: **Order matters.** A header value (``Bearer x``, ``Basic x``, ``token k:s``)
#: is matched before the generic assignment, which would otherwise take the
#: scheme word as the value and leave the credential after it.
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]]] = [
    # Anthropic API keys
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}", re.I), REDACTED),
    # JWTs — match the three-part header.payload.signature structure rather
    # than assuming a minimum header length. A short-header token was
    # previously slipping through.
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]*"), REDACTED),
    # Bearer tokens in headers
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-.=]{8,}"), REDACTED),
    # HTTP Basic credentials: base64 of user:password (SEC-011).
    (re.compile(r"(?i)(?P<pre>\bBasic\s+)[A-Za-z0-9+/]{8,}={0,2}"), r"\g<pre>" + REDACTED),
    # Kite's own Authorization header value: "token <api_key>:<access_token>".
    # No key name precedes the secret, so nothing else could match it.
    (re.compile(r"(?i)\b(?:token|enctoken)\s+[A-Za-z0-9]{4,}:[A-Za-z0-9]{6,}"), REDACTED),
    # A Telegram bot token, which lives in the URL PATH
    # (/bot<id>:<secret>/sendMessage) - and httpx puts the URL into every
    # HTTPStatusError, so a failed notification would log it (SEC-011).
    (re.compile(r"(?P<pre>\bbot)\d{6,}:[A-Za-z0-9_\-]{20,}"), r"\g<pre>" + REDACTED),
    # Base32 TOTP seeds (typically 16-32 chars, A-Z and 2-7)
    (re.compile(r"\b[A-Z2-7]{16,64}={0,6}\b(?=\s*(?:totp|seed|secret))", re.I), REDACTED),
    # Six-digit OTP adjacent to a totp/otp label
    (re.compile(r"\b[0-9]{6}\b(?=\s*(?:totp|otp))", re.I), REDACTED),
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
    #
    # The match starts at ``://``, not at the scheme: a scheme tried from every
    # word boundary re-scanned a long dashed or dotted run once per boundary -
    # quadratic, and in the logging path, where a stall stops the event loop
    # (QA, 18 Nov 2026). The scheme was never needed to find the password, and
    # it is left in the line untouched either way. The password also stops at
    # the next ``://``, so each candidate scans only to the next one: without
    # that, ``x://u:`` repeated with no ``@`` was quadratic still.
    (
        re.compile(r"(?P<pre>://[^\s:/@]*:)(?:(?!://)[^\s@])+(?P<post>@)"),
        r"\g<pre>" + REDACTED + r"\g<post>",
    ),
    # A credential under a QUOTED key - JSON ("api_key": "...") and Python's
    # dict repr ({'access_token': '...'}). The assignment pattern below needs
    # `key=` or `key:` directly after the name, and the closing quote broke it:
    # probed 27 Oct 2026, kiteconnect's own debug format and a logged session
    # payload both passed the api_key, request_token, checksum and access_token
    # through in clear. The key survives so the line still says WHAT was
    # redacted; only the value goes.
    (
        re.compile(
            r"""(?P<pre>(?P<q>["'])(?P<key>"""
            + _CREDENTIAL_NAME
            + r"|[A-Za-z0-9_\-]*authorization)"
            r"""(?P=q)\s*:\s*)(?:"[^"]*"|'[^']*'|[^\s,}\]]+)""",
            re.I,
        ),
        _unless_public(r'\g<pre>"' + REDACTED + '"'),
    ),
    # Any credential-shaped assignment: key=value, key: value. The key is kept.
    #
    # The key starts only where a run of key characters STARTS (the
    # lookbehind). With ``\b`` it could start at every boundary inside a run -
    # after each ``-`` - and each start re-scanned the rest of the run: 6 KB
    # of base64url took 0.9 s and 100 KB minutes, per log line (QA, 18 Nov
    # 2026). It matches no less: a key found from inside a run is found from
    # the run's start too, because the prefix class takes the rest of the run.
    (
        re.compile(
            r"(?i)(?P<pre>(?<![A-Za-z0-9_\-])(?P<key>"
            + _CREDENTIAL_NAME
            + r")\b\s*[:=]\s*)"
            + _ASSIGNED_VALUE
        ),
        _unless_public(r"\g<pre>" + REDACTED),
    ),
]

#: Field names whose values are always redacted, whatever they contain - on
#: top of every name :data:`_CREDENTIAL_NAME` matches.
_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "api_key",
        "apikey",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "totp",
        "totp_secret",
        "authorization",
        "auth",
        "client_secret",
        "private_key",
        "session_token",
        "feed_token",
    }
)

_CREDENTIAL_NAME_NAME = re.compile(_CREDENTIAL_NAME, re.I)


def is_sensitive_key(key: str) -> bool:
    """Whether a field NAMED ``key`` holds a credential, whatever its value.

    The named set, plus any name :data:`_CREDENTIAL_NAME` matches as a suffix -
    ``{'request_token': ...}`` used to keep its value (SEC-011) - less the
    public ids in :data:`_NOT_SECRETS`.
    """
    lowered = key.lower()
    if lowered in _NOT_SECRETS:
        return False
    return lowered in _SENSITIVE_KEYS or _CREDENTIAL_NAME_NAME.fullmatch(lowered) is not None


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
        # Patterns FIRST, on the text as it arrived. Value-scrubbing first
        # changed the shape the patterns look for: with the access token
        # already replaced, "token <key>:<secret>" became "token <key>:***",
        # the header pattern no longer matched, and the api key beside it was
        # left in the line. Found as a test that passed alone and failed in
        # the suite - an earlier test had registered the token's value.
        for pattern, replacement in _SECRET_PATTERNS:
            text = pattern.sub(replacement, text)
        # Then every known value, longest first: if one secret contains
        # another, scrubbing the short one first would leave the rest of the
        # long one in the line.
        for value in sorted(self._known | known_secret_values(), key=len, reverse=True):
            if value in text:
                text = text.replace(value, REDACTED)
        return text

    def _scrub_value(self, key: str, value: Any) -> Any:
        if isinstance(value, SecretString):
            return REDACTED
        if is_sensitive_key(key):
            return REDACTED
        if isinstance(value, str):
            return self._scrub_text(value)
        if isinstance(value, dict):
            return {k: self._scrub_value(str(k), v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            items = [self._scrub_value(key, v) for v in value]
            if isinstance(value, list):
                return items
            if isinstance(value, (set, frozenset)):
                # A set of strings is a set of strings again; two that scrub to
                # the same text merge, which is fine for a log line.
                return type(value)(items) if type(value) in (set, frozenset) else frozenset(items)
            if hasattr(value, "_fields"):
                # A namedtuple takes its fields as ARGUMENTS: rebuilding one
                # from a generator raised TypeError inside the logging
                # pipeline (SEC-011). Its field names are keys too.
                fields = [
                    self._scrub_value(f, v) for f, v in zip(value._fields, value, strict=True)
                ]
                return type(value)(*fields)
            return tuple(items)
        if isinstance(value, (bytes, bytearray)):
            # Rendered by repr() much later, past any scrub - so render it now.
            return self._scrub_text(repr(value))
        if isinstance(value, BaseException):
            # An exception object is not a str, so it used to fall through here
            # untouched — and the formatter calls str() on it much later, well
            # past any redaction. `log.warning("failed: %s", exc)` therefore
            # printed the DSN password in full. Rendering it here is what makes
            # the scrub actually apply.
            return self._scrub_text(f"{type(value).__name__}: {value}")
        return value

    def __call__(self, _logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        return {k: self._scrub_value(k, v) for k, v in event_dict.items()}


class RedactingFilter(logging.Filter):
    """stdlib logging filter — catches third-party library output.

    **Tracebacks are the leak path that matters most**, and they do not travel
    through ``msg`` or ``args``. ``logging.Formatter.format`` renders
    ``record.exc_info`` itself, long after every filter has run, so an earlier
    version of this class scrubbed the message perfectly and still wrote

        ConnectionError: could not connect to
        postgresql+psycopg://algotrader:<real password>@db.internal:5432/...

    to disk on the first failed healthcheck. SQLAlchemy, psycopg and Alembic all
    put the connection URL into their exceptions, and ``engine.healthcheck``
    logs with ``exc_info=True`` by design.

    The fix relies on how the formatter is written: it renders from
    ``exc_info`` **only when ``exc_text`` is empty**, and uses ``exc_text``
    verbatim when it is set. Pre-rendering it here and scrubbing that string
    means the formatter never sees the raw traceback.
    """

    def __init__(self, processor: RedactingProcessor) -> None:
        super().__init__()
        self._processor = processor

    def filter(self, record: logging.LogRecord) -> bool:
        # The message is RENDERED first and the finished line scrubbed. Scrubbing
        # the template and the arguments apart missed a credential split across
        # them - ``"...://app:%s@db"`` with the password as the argument - and
        # worse: the URI pattern ate the ``%s``, the format then failed, and
        # logging's error handler printed the raw arguments to stderr (found
        # writing the SEC-012 test, 17 Nov 2026).
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = None
        if rendered is not None:
            # Empty args: getMessage() formats only when args are truthy, so a
            # '%' left in the scrubbed text is never treated as a placeholder.
            record.msg, record.args = self._processor._scrub_text(rendered), ()
        else:
            if isinstance(record.msg, str):
                record.msg = self._processor._scrub_text(record.msg)
            if isinstance(record.args, dict):
                record.args = {
                    k: self._processor._scrub_value(str(k), v) for k, v in record.args.items()
                }
            elif record.args:
                record.args = tuple(self._processor._scrub_value("", a) for a in record.args)

        if record.exc_info:
            if not record.exc_text:
                record.exc_text = "".join(traceback.format_exception(*record.exc_info))
        if record.exc_text:
            record.exc_text = self._processor._scrub_text(record.exc_text)

        # `stack_info` is already a rendered string and is appended verbatim.
        if record.stack_info:
            record.stack_info = self._processor._scrub_text(record.stack_info)

        return True


_redactor = RedactingProcessor()


def register_secret_values(values: set[str]) -> None:
    """Tell the logging layer about loaded secrets so it can scrub them.

    Call this immediately after loading secrets, before any other work.
    """
    _redactor.add_known_values(values)


def redacting_filter() -> RedactingFilter:
    """A stdlib filter over the process's one redactor, for a handler this
    module did not install (Alembic's command-line console, SEC-012)."""
    return RedactingFilter(_redactor)


def redact(text: str) -> str:
    """Scrub ``text`` the way a log line is scrubbed, for sinks that are not logs.

    ``decision_log`` details, heartbeat details and anything else written
    outside the logging pipeline never pass the redacting filter (REC-003).
    Text that may carry a credential - a connection string in a service's
    own status, an exception message - goes through this before it is stored.
    """
    return _redactor._scrub_text(text)


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
        _redactor,  # <- redaction, always on
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

    quiet_noisy_libraries()


#: Libraries that log request lines or bodies at DEBUG. `websockets` logs the
#: request line, which for the market feed carries the access token as a query
#: parameter; `kiteconnect` logs every request with its Authorization header
#: when its debug flag is on; `requests` sits under it. Pinned at WARNING so
#: turning the ROOT logger to DEBUG to chase a problem cannot turn them on too.
#: The redactor still scrubs whatever does get through - this is the layer in
#: front of it.
NOISY_LIBRARIES: tuple[str, ...] = (
    "httpx",
    "httpcore",
    "urllib3",
    "asyncio",
    "anthropic",
    "websockets",
    "kiteconnect",
    "requests",
)


def quiet_noisy_libraries() -> None:
    """Pin every library in :data:`NOISY_LIBRARIES` at WARNING."""
    for noisy in NOISY_LIBRARIES:
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]


def bind_correlation(correlation_id: str) -> None:
    """Bind a correlation id for the current async task.

    Everything logged downstream carries it, which is what makes end-to-end
    trade tracing a single query.
    """
    structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
