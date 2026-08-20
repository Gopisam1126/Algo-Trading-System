"""Kite exception -> domain taxonomy.

**The default disposition for an unrecognised failure is the dangerous one.**

Kite raises seven exception types and a long list of message strings, and the
list changes without notice. The question that matters is not "which error is
this" but "may the order have reached the exchange". For anything unrecognised
during a *mutating* call the honest answer is "possibly", and the only safe
mapping is :class:`AmbiguousOrderError` — which routes to query-by-tag rather
than to a retry.

Mapping an unknown error to something retryable is how a timeout becomes two
positions. Mapping it to a plain failure is how a live order gets forgotten.
So unknown + mutating always means ambiguous, and the caller reconciles.

Reads are different: a failed read has no side effect, so an unknown error
there is just a failure.
"""

from __future__ import annotations

import logging

from kiteconnect import exceptions as kx

from algotrader.broker.adapter import (
    AmbiguousOrderError,
    AuthenticationError,
    BrokerError,
    OrderRejectedError,
    RateLimitError,
)

log = logging.getLogger(__name__)

#: HTTP status codes that mean "the broker never processed this".
#: A 429 is emphatically NOT ambiguous — the request was refused before
#: reaching the exchange, so backing off and retrying later is safe.
_RATE_LIMITED = 429

#: Statuses where the request demonstrably did not take effect. Anything else
#: in the 5xx range is ambiguous: the exchange may well have seen it.
_DEFINITELY_NOT_PROCESSED = frozenset({400, 401, 403, 404})


def classify(exc: Exception, *, mutating: bool) -> BrokerError:
    """Translate a broker exception into the domain taxonomy.

    ``mutating`` is not a hint, it is the safety switch. Set it True for any
    call that could have changed state at the exchange — place, modify,
    cancel. It makes the unknown case fail closed.
    """
    code = getattr(exc, "code", None)

    if isinstance(exc, kx.TokenException):
        # Session died. Every downstream caller must stop rather than retry;
        # a fresh login is a human action (redirect flow), not a retry.
        return AuthenticationError(f"broker session is no longer valid: {exc}")

    if isinstance(exc, kx.PermissionException):
        return AuthenticationError(f"broker refused this operation: {exc}")

    if code == _RATE_LIMITED:
        return RateLimitError(f"broker rate limit hit: {exc}")

    if isinstance(exc, kx.InputException):
        # Malformed request. The exchange rejected it outright, so there is
        # nothing to reconcile — but it is OUR bug, not a market condition.
        return OrderRejectedError(
            f"broker rejected the request as invalid: {exc}", reason_code="INPUT"
        )

    if isinstance(exc, kx.OrderException):
        # OrderException covers both "rejected, definitively" and "something
        # went wrong while placing". Only the former is safe to treat as final.
        if code in _DEFINITELY_NOT_PROCESSED:
            return OrderRejectedError(f"broker rejected the order: {exc}", reason_code=str(code))
        if mutating:
            return AmbiguousOrderError(
                f"order may or may not have been placed (OrderException, code={code}): {exc}"
            )
        return BrokerError(f"order operation failed: {exc}")

    if isinstance(exc, kx.NetworkException | TimeoutError | ConnectionError):
        if mutating:
            return AmbiguousOrderError(
                f"network failure during a mutating call — outcome unknown: {exc}"
            )
        return BrokerError(f"network failure during a read: {exc}")

    if isinstance(exc, kx.DataException):
        return BrokerError(f"broker returned unusable data: {exc}")

    # Unmapped. Loud, because a new code appearing mid-session is something to
    # triage the same day — and fail-closed, because we cannot know.
    log.error(
        "UNMAPPED broker error type=%s code=%s mutating=%s — treating as %s",
        type(exc).__name__,
        code,
        mutating,
        "ambiguous" if mutating else "failure",
    )
    if mutating:
        return AmbiguousOrderError(
            f"unrecognised broker error during a mutating call, outcome unknown "
            f"({type(exc).__name__}, code={code}): {exc}"
        )
    return BrokerError(f"unrecognised broker error ({type(exc).__name__}, code={code}): {exc}")


def is_retryable(err: BrokerError) -> bool:
    """Only a rate limit is safe to retry unchanged.

    Deliberately narrow. ``AmbiguousOrderError`` is never retryable — it is
    *reconcilable*, which is a different operation with a different entry
    point, and conflating the two is the duplicate-position bug.
    """
    return isinstance(err, RateLimitError)
