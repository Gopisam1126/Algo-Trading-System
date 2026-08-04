"""Per-broker capability and limit profiles.

The system already caps order rate below SEBI's 10/sec registration threshold
(``common/config.py``).  But that is not the only ceiling: **each broker
imposes its own, often lower, API rate limit**, and exceeding it gets you
throttled or blocked by the broker regardless of what SEBI permits.

So the real constraint is::

    effective_limit = min(SEBI_safe_cap, broker_api_limit)

Encoding the broker limits here lets config validation catch a mismatch at
startup rather than at 09:20 on a busy morning.  Zerodha in particular allows
roughly a third of what Angel One does, so a config written for one broker is
actively wrong for the other.

⚠️  These figures were researched in August 2026 and brokers revise them.
    Confirm against your broker's current API documentation, and treat their
    written answer as authoritative over this table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class AuthFlow(StrEnum):
    """How a broker's daily authentication works.

    This matters more than it looks.  SEBI requires sessions to be
    re-established before each pre-open, so whatever this flow is, it happens
    every trading morning — and a flow that needs a human in front of a
    browser sets a floor on how unattended the system can actually be.
    """

    #: Programmatic: credentials + TOTP exchanged directly for a token.
    DIRECT_TOTP = "direct_totp"

    #: Browser redirect: login URL -> user authenticates -> request_token
    #: returned to a redirect URI -> exchanged with the API secret for an
    #: access token.  Cannot be fully automated without either driving a
    #: browser or hitting undocumented endpoints.
    REDIRECT_REQUEST_TOKEN = "redirect_request_token"


@dataclass(frozen=True)
class BrokerProfile:
    """What a given broker actually permits."""

    key: str
    display_name: str

    #: Hard API ceiling on order placement, orders/second.
    max_orders_per_second: int

    #: Quote/historical request ceiling, requests/minute.
    max_data_requests_per_minute: int

    auth_flow: AuthFlow

    #: True when the access token expires daily and must be re-established.
    daily_token_expiry: bool

    #: True when the daily flow cannot complete without human interaction.
    #: Drives the operational warning in ``doctor.py``.
    requires_manual_daily_login: bool

    #: Monthly cost in INR for API access (0 = free tier available).
    monthly_cost_inr: int

    #: Whether historical data is a separate paid add-on.
    historical_data_extra_cost: bool

    notes: str = ""
    verified_on: str = "2026-08"
    caveats: list[str] = field(default_factory=list)

    def effective_order_rate(self, sebi_safe_cap: int) -> int:
        """The rate the system may actually use."""
        return min(self.max_orders_per_second, sebi_safe_cap)


ZERODHA = BrokerProfile(
    key="zerodha",
    display_name="Zerodha Kite Connect",
    max_orders_per_second=3,
    max_data_requests_per_minute=200,
    auth_flow=AuthFlow.REDIRECT_REQUEST_TOKEN,
    daily_token_expiry=True,
    requires_manual_daily_login=True,
    monthly_cost_inr=500,
    historical_data_extra_cost=True,
    notes=(
        "Most mature Indian broker API — best documentation and ecosystem. "
        "Two operational constraints matter: the order rate is roughly a third "
        "of Angel One's, and the daily auth is a browser redirect flow rather "
        "than a programmatic login."
    ),
    caveats=[
        "Access token expires daily; a fresh login is required every trading "
        "morning before pre-open.",
        "Auth is a redirect flow (login URL -> request_token -> exchange with "
        "api_secret). It cannot be fully automated without driving a browser "
        "or using undocumented endpoints — confirm what Zerodha permits before "
        "relying on either.",
        "Historical data appears to be a separate paid add-on beyond the base "
        "API subscription — CONFIRM current pricing with Zerodha.",
        "3 orders/sec is the binding constraint, well below SEBI's 10/sec "
        "threshold. Config must not exceed it.",
    ],
)

ANGELONE = BrokerProfile(
    key="angelone",
    display_name="Angel One SmartAPI",
    max_orders_per_second=10,
    max_data_requests_per_minute=180,
    auth_flow=AuthFlow.DIRECT_TOTP,
    daily_token_expiry=True,
    requires_manual_daily_login=False,
    monthly_cost_inr=0,
    historical_data_extra_cost=False,
    notes="Free, programmatic TOTP login, highest order-rate headroom.",
    caveats=["WebSocket stability at the open and on expiry days is a "
             "commonly reported weak point — plan for reconnection."],
)

FYERS = BrokerProfile(
    key="fyers",
    display_name="Fyers API",
    max_orders_per_second=10,
    max_data_requests_per_minute=200,
    auth_flow=AuthFlow.REDIRECT_REQUEST_TOKEN,
    daily_token_expiry=True,
    requires_manual_daily_login=True,
    monthly_cost_inr=0,
    historical_data_extra_cost=False,
    notes="Free API with free minute-level history (~1-2 years) — useful as a "
          "data fallback even when trading elsewhere.",
)

UPSTOX = BrokerProfile(
    key="upstox",
    display_name="Upstox API v2",
    max_orders_per_second=10,
    max_data_requests_per_minute=200,
    auth_flow=AuthFlow.REDIRECT_REQUEST_TOKEN,
    daily_token_expiry=True,
    requires_manual_daily_login=True,
    monthly_cost_inr=0,
    historical_data_extra_cost=False,
)

DHAN = BrokerProfile(
    key="dhan",
    display_name="DhanHQ",
    max_orders_per_second=10,
    max_data_requests_per_minute=200,
    auth_flow=AuthFlow.DIRECT_TOTP,
    daily_token_expiry=True,
    requires_manual_daily_login=False,
    monthly_cost_inr=499,
    historical_data_extra_cost=False,
)


PROFILES: dict[str, BrokerProfile] = {
    p.key: p for p in (ZERODHA, ANGELONE, FYERS, UPSTOX, DHAN)
}


def get_profile(key: str) -> BrokerProfile:
    if key not in PROFILES:
        raise ValueError(
            f"unknown broker {key!r}. Known: {sorted(PROFILES)}. "
            f"Adding one requires a reviewed change to broker/profiles.py."
        )
    return PROFILES[key]
