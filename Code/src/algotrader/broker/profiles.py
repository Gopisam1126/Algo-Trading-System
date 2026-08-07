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
    # Suppression justified: this is the NAME of an auth flow, not a
    # credential. bandit matches the "token" substring in the identifier.
    REDIRECT_REQUEST_TOKEN = "redirect_request_token"  # noqa: S105


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

    #: True when the static-IP whitelist applies ONLY to order endpoints,
    #: leaving quotes/WebSocket/orderbook reachable from any IP.  This is
    #: architecturally useful: it means only the execution service needs to
    #: originate from the whitelisted address.
    static_ip_order_endpoints_only: bool = False

    #: True when MARKET and SL-M orders require an explicit market-protection
    #: parameter, without which the broker rejects them.
    requires_market_protection: bool = False

    #: Broker-imposed daily order cap, if any (0 = none published).
    max_orders_per_day: int = 0

    notes: str = ""
    verified_on: str = "2026-08"
    caveats: list[str] = field(default_factory=list)

    def effective_order_rate(self, sebi_safe_cap: int) -> int:
        """The rate the system may actually use."""
        return min(self.max_orders_per_second, sebi_safe_cap)


ZERODHA = BrokerProfile(
    key="zerodha",
    display_name="Zerodha Kite Connect",
    # Zerodha staff on the Kite Connect developer forum state 10 OPS enforced
    # ACCOUNT-WIDE (not per app) with HTTP 429 on excess: of 15 attempted, 10
    # place and 5 are blocked.  An older documented Kite limit of ~3/sec
    # circulates in secondary sources; the forum figure is more recent and
    # comes from Zerodha directly.  We still run well under it by choice.
    max_orders_per_second=10,
    max_data_requests_per_minute=200,
    auth_flow=AuthFlow.REDIRECT_REQUEST_TOKEN,
    daily_token_expiry=True,
    requires_manual_daily_login=True,
    monthly_cost_inr=500,  # data APIs; order placement reported free
    historical_data_extra_cost=True,
    static_ip_order_endpoints_only=True,
    requires_market_protection=True,
    max_orders_per_day=3000,
    notes=(
        "Most mature Indian broker API - best documentation and ecosystem. "
        "Static IP applies to ORDER endpoints only; quotes, WebSocket, "
        "orderbook and positions remain reachable from any IP, which maps "
        "cleanly onto this system's read-only vs trading service split."
    ),
    caveats=[
        "MARKET and SL-M orders REQUIRE a market_protection parameter from "
        "1 Apr 2026; without it the broker rejects them. Accepts -1 for "
        "auto-protection or a numeric percentage. Market protection converts "
        "a market order to a limit order and remains subject to exchange LPP "
        "ranges.",
        "pykiteconnect 5.1.0 on PyPI does NOT expose market_protection in "
        "place_order() (it is on the main branch only - see zerodha/"
        "pykiteconnect issue #225). Verify the installed version supports it "
        "before relying on market or SL-M orders.",
        "Access token expires daily; a fresh login is required every trading "
        "morning before pre-open. Auth is a browser redirect flow, so this "
        "step is manual by design in this deployment.",
        "Each static IP can be linked to only one account (community-reported "
        "error). Family sharing is permitted; multiple Zerodha accounts can "
        "sit under one developer profile.",
        "Zerodha applies a ~3,000 orders/day account cap for most users, "
        "extendable on request - unlikely to bind for this system.",
        "Historical data is a separate paid add-on beyond the base API "
        "subscription - CONFIRM current pricing with Zerodha.",
        "Self-developed algos under 10 OPS receive a GENERIC exchange algo ID "
        "rather than a unique one. Sources disagree on whether the developer "
        "attaches it via the order `tag` field or the broker injects it - "
        "CONFIRM with Zerodha before going live.",
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
    caveats=[
        "WebSocket stability at the open and on expiry days is a "
        "commonly reported weak point — plan for reconnection."
    ],
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


PROFILES: dict[str, BrokerProfile] = {p.key: p for p in (ZERODHA, ANGELONE, FYERS, UPSTOX, DHAN)}


def get_profile(key: str) -> BrokerProfile:
    if key not in PROFILES:
        raise ValueError(
            f"unknown broker {key!r}. Known: {sorted(PROFILES)}. "
            f"Adding one requires a reviewed change to broker/profiles.py."
        )
    return PROFILES[key]
