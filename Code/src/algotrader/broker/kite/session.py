"""Building an authenticated Kite client — the one place the token is revealed.

This module exists because the adapters take a ``client`` and nothing was
constructing one. Every piece of E02 was present and the flow could not actually
be run, which a passing unit test will never tell you.

**The token is revealed exactly here and nowhere else.** ``SecretString`` makes
that a deliberate, greppable act rather than something that happens wherever a
caller finds it convenient. Two lines in this file are the entire surface on
which a raw credential exists in memory as a plain ``str``, and both hand it
straight to the SDK.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from algotrader.broker.adapter import AuthenticationError
from algotrader.broker.kite.auth import KiteAuthManager
from algotrader.broker.kite.errors import classify
from algotrader.common.secrets import SecretString

log = logging.getLogger(__name__)


def build_client(auth: KiteAuthManager) -> Any:
    """A ``KiteConnect`` carrying the current access token.

    Raises if there is no valid session rather than returning an unauthenticated
    client — an unauthenticated client fails at the first call with a 403, which
    a caller can easily misread as a broker problem rather than as its own
    missing login.
    """
    from kiteconnect import KiteConnect

    token = auth.token()  # raises AuthenticationError when there is no session
    client = KiteConnect(api_key=auth.api_key)
    # Reveal point 1 of 2. Handed directly to the SDK; never stored, logged or
    # returned by anything in this package.
    client.set_access_token(token.reveal())
    return client


async def exchange_request_token(
    auth: KiteAuthManager,
    request_token: str,
    *,
    api_secret: SecretString,
) -> str:
    """Complete the redirect login: request_token -> access_token.

    The final step of E02-S01. The ``request_token`` arrives on the callback
    from Zerodha, is single-use, and is worthless without the api_secret — which
    is why the exchange happens server-side here rather than anywhere the token
    has already travelled.

    Returns the access token, having already adopted it into ``auth``. The raw
    value is returned only so a caller can hand it to a client factory; it is
    not persisted by this function and must not be persisted by the caller.
    """
    if not request_token:
        raise AuthenticationError("no request_token supplied; the callback carried nothing")

    from kiteconnect import KiteConnect

    client = KiteConnect(api_key=auth.api_key)
    try:
        # Reveal point 2 of 2. The SDK computes the checksum itself from these.
        data = await asyncio.to_thread(
            client.generate_session, request_token, api_secret=api_secret.reveal()
        )
    except Exception as exc:
        # mutating=False: this creates a session, not an order. Nothing at the
        # exchange changed, so there is nothing to reconcile.
        raise classify(exc, mutating=False) from None

    access_token = str(data.get("access_token") or "")
    if not access_token:
        raise AuthenticationError(
            "broker completed the session exchange without returning an access token"
        )
    auth.adopt_token(access_token)
    log.info("redirect login completed for %s", data.get("user_id", "unknown"))
    return access_token
