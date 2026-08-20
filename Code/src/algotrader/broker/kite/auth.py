"""Kite session lifecycle — the redirect login and the daily re-auth (E02-S01/S02).

SEBI requires the session to be re-established before each pre-open, and
Zerodha's flow is a browser redirect, so **one human touchpoint every trading
morning is a permanent floor**, not a gap to engineer away.

The security decision that shapes this module: **the re-auth notification
carries no link.**

The obvious design sends the Kite login URL to Telegram at 07:00. It also
trains the operator to authenticate from an inbound message — which is the
consent-phishing pattern. Anyone able to post one message into that chat (a
leaked bot token, a SIM-swapped Telegram account, a lookalike bot) can send a
link to *their* Kite app at the moment one is expected. The operator would
authenticate on a genuine Zerodha page and hand them a request_token they
exchange with their own api_secret. Two-factor authentication does not help:
the operator is authorising the wrong application, not leaking a password.
Nothing this system can validate server-side helps either, because that attack
completes entirely outside it.

So :class:`ReauthNotice` has **no URL field at all**. The link cannot be sent
because there is nowhere to put it. The operator opens their own dashboard and
starts the flow there — the same structural trick as ``Recommendation`` having
no quantity field.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from algotrader.broker.adapter import AuthenticationError, BrokerSession
from algotrader.common.secrets import SecretString

log = logging.getLogger(__name__)

KITE_LOGIN_BASE = "https://kite.zerodha.com/connect/login"


@dataclass(frozen=True, slots=True)
class ReauthNotice:
    """What the operator is told when a session needs re-establishing.

    Deliberately has no ``url`` / ``link`` / ``login_url`` field. See the module
    docstring — this is the anti-phishing control, and it is enforced by the
    shape of the type rather than by a reviewer noticing.
    """

    trade_date: dt.date
    reason: str
    #: Where to go — a NAME, not an address. The operator navigates from their
    #: own bookmark, so a spoofed message cannot redirect them anywhere.
    open_instead: str = "the algotrader dashboard"

    def message(self) -> str:
        return (
            f"Broker re-authentication required for {self.trade_date.isoformat()}.\n"
            f"Reason: {self.reason}\n"
            f"Open {self.open_instead} from your own bookmark and start the login there.\n"
            f"This message will never contain a login link - if one arrives claiming to, "
            f"it did not come from your system."
        )


class SessionEnvelope(BaseModel):
    """What is persisted about a session. **Never the token.**

    The access token lives only in the adapter's memory as a
    :class:`SecretString`. Persisting it would put a bearer credential into
    Redis, where a keyspace dump, a replication link or an errant ``KEYS *``
    would expose it — and the token is all an attacker needs to trade the
    account for the rest of the day.
    """

    model_config = ConfigDict(frozen=True)

    broker: str
    client_id: str
    authenticated_at: dt.datetime
    expires_at: dt.datetime


def login_url(api_key: str) -> str:
    """The Kite login URL, for the DASHBOARD to render — never for a message.

    Contains only ``api_key``, which is an app identifier rather than a
    credential. It is still not sent anywhere outbound: see the module
    docstring.
    """
    if not api_key:
        raise AuthenticationError("no api_key configured; cannot build a login URL")
    return f"{KITE_LOGIN_BASE}?api_key={api_key}&v=3"


def checksum(api_key: str, request_token: str, api_secret: SecretString) -> str:
    """SHA-256 of api_key + request_token + api_secret, per Kite's session flow.

    ``api_secret`` is taken as a :class:`SecretString` and revealed only inside
    this function, so no caller has to hold the raw value to compute it.
    """
    material = f"{api_key}{request_token}{api_secret.reveal()}"
    return hashlib.sha256(material.encode()).hexdigest()


def next_expiry(now: dt.datetime, *, reauth_hour: int = 7, reauth_minute: int = 0) -> dt.datetime:
    """When the session should stop being trusted.

    Kite's own token expiry is early morning, but the value that matters
    operationally is *our* re-auth time: a session must not be used past the
    next scheduled login, because SEBI requires re-establishing it and the
    broker will drop it regardless.
    """
    from algotrader.common.calendar import IST

    ist = now.astimezone(IST)
    target = ist.replace(hour=reauth_hour, minute=reauth_minute, second=0, microsecond=0)
    if target <= ist:
        target = target + dt.timedelta(days=1)
    return target.astimezone(dt.UTC)


class KiteAuthManager:
    """Holds the access token and answers "is the session usable right now".

    The token never leaves this object except through :meth:`token`, which
    returns a :class:`SecretString`. There is deliberately no property that
    returns the raw string.
    """

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: SecretString,
        client_id: str,
        reauth_hour: int = 7,
        reauth_minute: int = 0,
    ) -> None:
        if not api_key:
            raise AuthenticationError("api_key is required")
        self._api_key = api_key
        self._api_secret = api_secret
        self._client_id = client_id
        self._reauth_hour = reauth_hour
        self._reauth_minute = reauth_minute
        self._token: SecretString | None = None
        self._session: BrokerSession | None = None

    # -- state ---------------------------------------------------------------

    @property
    def session(self) -> BrokerSession | None:
        return self._session

    def token(self) -> SecretString:
        """The live access token. Raises rather than returning ``None``.

        Returning ``None`` would let a caller send an unauthenticated request
        and read the broker's 403 as a market condition rather than as its own
        misconfiguration.
        """
        if self._token is None or self._session is None or self._session.is_expired:
            raise AuthenticationError(
                "no valid broker session; re-authentication is required before any "
                "broker call. This is expected once each trading morning."
            )
        return self._token

    def is_valid(self, *, now: dt.datetime | None = None) -> bool:
        now = now or dt.datetime.now(dt.UTC)
        return (
            self._token is not None and self._session is not None and now < self._session.expires_at
        )

    def envelope(self) -> SessionEnvelope | None:
        if self._session is None:
            return None
        return SessionEnvelope(
            broker="zerodha",
            client_id=self._session.client_id,
            authenticated_at=self._session.authenticated_at,
            expires_at=self._session.expires_at,
        )

    # -- login ---------------------------------------------------------------

    def login_url(self) -> str:
        return login_url(self._api_key)

    def adopt_token(self, access_token: str, *, now: dt.datetime | None = None) -> BrokerSession:
        """Record a freshly-exchanged access token.

        Kept separate from the HTTP exchange so the session logic is testable
        without a broker, and so the raw token has exactly one entry point into
        the process.
        """
        if not access_token:
            raise AuthenticationError("broker returned an empty access token")
        now = now or dt.datetime.now(dt.UTC)
        expires = next_expiry(now, reauth_hour=self._reauth_hour, reauth_minute=self._reauth_minute)
        self._token = SecretString(access_token, name="KITE_ACCESS_TOKEN")
        self._session = BrokerSession(
            broker="zerodha",
            client_id=self._client_id,
            authenticated_at=now,
            expires_at=expires,
        )
        # Note what is NOT logged: the token, or any prefix of it. A "first six
        # characters" debug line is still a credential leak once the log ships
        # somewhere.
        log.info(
            "broker session established for %s, valid until %s",
            self._client_id,
            expires.isoformat(),
        )
        return self._session

    def invalidate(self, reason: str) -> ReauthNotice:
        """Drop the session and produce the (link-free) operator notice."""
        self._token = None
        self._session = None
        log.warning("broker session invalidated: %s", reason)
        return ReauthNotice(trade_date=dt.datetime.now(dt.UTC).date(), reason=reason)

    def notice_if_reauth_needed(self, *, now: dt.datetime | None = None) -> ReauthNotice | None:
        """The scheduler's hook. ``None`` means the session is good."""
        now = now or dt.datetime.now(dt.UTC)
        if self.is_valid(now=now):
            return None
        reason = "no session yet today" if self._session is None else "session expired"
        return ReauthNotice(trade_date=now.date(), reason=reason)
