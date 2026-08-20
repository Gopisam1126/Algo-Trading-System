"""Findings from the BA / QA / pentest pass over E02, each pinned by a test.

Five defects, none of which any existing test caught:

- an unvalidated ``api_key`` steers the login URL to an attacker's redirect;
- a newline in a symbol forges a whole log line;
- limit and trigger prices were never snapped to the tick grid, so every
  ATR-derived stop would have been rejected by the exchange;
- nothing constructed an authenticated client, so the flow could not be run;
- the raw credential must stay unwrapped in as few places as possible.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from algotrader.broker.adapter import AuthenticationError, OrderRejectedError
from algotrader.broker.kite.auth import KiteAuthManager
from algotrader.broker.kite.trading import KiteTradingAdapter
from algotrader.common.enums import OrderIntent, OrderType, Product, Side
from algotrader.common.models.trading import OrderRequest
from algotrader.common.secrets import SecretString

API_SECRET = "api_secret_DO_NOT_LEAK_7c1d"


def _auth() -> KiteAuthManager:
    return KiteAuthManager(
        api_key="pubkey123", api_secret=SecretString(API_SECRET), client_id="AB1234"
    )


def _request(**overrides: Any) -> OrderRequest:
    base: dict[str, Any] = {
        "client_order_id": "a" * 32,
        "correlation_id": uuid.uuid4(),
        "symbol": "INFY",
        "side": Side.BUY,
        "order_type": OrderType.MARKET,
        "product": Product.MIS,
        "quantity": 10,
        "intent": OrderIntent.ENTRY,
        "market_protection": Decimal(-1),
    }
    base.update(overrides)
    return OrderRequest(**base)


class TestTheLoginUrlCannotBeSteered:
    """An unvalidated api_key steers the OAuth callback.

    Interpolated unchecked, ``realkey&redirect_uri=https://evil.example.com``
    produces a perfectly valid Kite login URL that returns the request_token to
    somebody else — and the operator, having navigated from their own bookmark
    exactly as the anti-phishing design intends, would see nothing wrong. The
    link-free notice defends the inbound direction; this defends the outbound.
    """

    @pytest.mark.parametrize(
        "hostile",
        [
            "realkey&redirect_uri=https://evil.example.com/steal",
            "key\r\nSet-Cookie: session=x",
            "key with space",
            "key#fragment",
            "key?extra=1",
        ],
    )
    def test_a_steerable_api_key_is_refused(self, hostile: str) -> None:
        with pytest.raises(AuthenticationError, match="not alphanumeric"):
            KiteAuthManager(
                api_key=hostile, api_secret=SecretString(API_SECRET), client_id="AB1234"
            ).login_url()

    def test_a_real_api_key_still_builds(self) -> None:
        url = _auth().login_url()
        assert url == "https://kite.zerodha.com/connect/login?api_key=pubkey123&v=3"

    def test_the_error_does_not_echo_the_whole_value(self) -> None:
        """A long hostile value must not become a log-flooding vector."""
        with pytest.raises(AuthenticationError) as exc:
            KiteAuthManager(
                api_key="&" * 5000, api_secret=SecretString(API_SECRET), client_id="AB"
            ).login_url()
        assert len(str(exc.value)) < 500


class TestASymbolCannotForgeALogLine:
    """A newline in a symbol appends a fabricated log entry.

    Symbols come from the broker's daily dump — external data this system does
    not author. QA-SEC-03 covered the Redis half; the same value reaches the
    order log line, where a line break writes a second, entirely fictional
    entry. Application logs are what an incident is reconstructed from.
    """

    @pytest.mark.parametrize(
        "hostile",
        [
            "INFY\n2026-01-01 CRITICAL kill switch disarmed",
            "INFY\r\nFAKE",
            "INFY:5m",
            "IN*FY",
            "A" * 70,
            "",
        ],
        ids=["newline", "crlf", "colon", "glob", "too-long", "empty"],
    )
    def test_a_hostile_symbol_does_not_construct(self, hostile: str) -> None:
        with pytest.raises(ValidationError):
            _request(symbol=hostile)

    @pytest.mark.parametrize("real", ["INFY", "M&M", "BAJAJ-AUTO", "L&TFH", "NIFTY.NS", "IDEA_EQ"])
    def test_real_nse_symbols_still_work(self, real: str) -> None:
        """The control. Indian symbols legitimately carry & - . and _, so a
        validator that only allowed A-Z would break the actual universe."""
        assert _request(symbol=real).symbol == real


class TestAPricedOrderIsAlwaysOnTheTickGrid:
    """E02-S06's acceptance criterion, which the code did not meet.

    ``round_to_tick`` existed, was correct, and was thoroughly unit-tested — and
    was never called in the order path. A stop derived from ATR is essentially
    never on a 0.05 grid by accident, so every computed stop would have been
    rejected by the exchange. The helper passing its own tests is exactly why
    this went unnoticed.
    """

    @staticmethod
    def _limit(side: Side, price: str) -> OrderRequest:
        return _request(
            side=side,
            order_type=OrderType.LIMIT,
            limit_price=Decimal(price),
            market_protection=None,
        )

    def test_without_a_resolver_a_priced_order_is_refused(self) -> None:
        """Fail closed. A hardcoded 0.05 fallback would be right for most of the
        market and silently wrong for the rest."""
        adapter = KiteTradingAdapter(auth=_auth(), client=object())
        with pytest.raises(OrderRejectedError, match="tick-size resolver"):
            adapter._build_params(self._limit(Side.BUY, "1234.5678"))

    def test_a_buy_is_snapped_down(self) -> None:
        adapter = KiteTradingAdapter(
            auth=_auth(), client=object(), tick_size_for=lambda _s: Decimal("0.05")
        )
        assert adapter._build_params(self._limit(Side.BUY, "1234.5678"))["price"] == 1234.55

    def test_a_sell_is_snapped_up(self) -> None:
        adapter = KiteTradingAdapter(
            auth=_auth(), client=object(), tick_size_for=lambda _s: Decimal("0.05")
        )
        assert adapter._build_params(self._limit(Side.SELL, "1234.5678"))["price"] == 1234.60

    def test_a_trigger_price_is_snapped_too(self) -> None:
        """An SL order's trigger is off-grid just as often as its limit."""
        adapter = KiteTradingAdapter(
            auth=_auth(), client=object(), tick_size_for=lambda _s: Decimal("0.05")
        )
        request = _request(
            order_type=OrderType.SL,
            limit_price=Decimal("990.0"),
            trigger_price=Decimal("991.2345"),
            market_protection=None,
        )
        params = adapter._build_params(request)
        assert (Decimal(str(params["trigger_price"])) / Decimal("0.05")) % 1 == 0

    def test_a_non_standard_tick_is_used(self) -> None:
        adapter = KiteTradingAdapter(
            auth=_auth(), client=object(), tick_size_for=lambda _s: Decimal("0.01")
        )
        assert adapter._build_params(self._limit(Side.BUY, "13.4567"))["price"] == 13.45

    def test_a_market_order_needs_no_resolver(self) -> None:
        """A MARKET order carries no price, so nothing needs snapping — the
        fail-closed rule must not block the most common order type."""
        adapter = KiteTradingAdapter(auth=_auth(), client=object())
        assert "price" not in adapter._build_params(_request())


class TestTheClientFactoryGuardsTheCredential:
    def test_an_unauthenticated_manager_yields_no_client(self) -> None:
        """Returning an unauthenticated client would fail later as a 403, which
        reads as a broker problem rather than a missing login."""
        from algotrader.broker.kite import session as ksession

        with pytest.raises(AuthenticationError):
            ksession.build_client(_auth())

    def test_reveal_sites_stay_confined_to_the_boundary(self) -> None:
        """``.reveal()`` is the one act that turns a guarded secret into a plain
        string. Keeping the sites few and greppable IS the control — each one is
        somewhere the credential can be logged, stored or passed on by mistake.
        """
        import inspect
        import pkgutil

        import algotrader.broker.kite as pkg

        sites: list[str] = []
        for module in pkgutil.iter_modules(pkg.__path__):
            mod = __import__(f"algotrader.broker.kite.{module.name}", fromlist=["_"])
            for number, line in enumerate(inspect.getsource(mod).splitlines(), 1):
                if ".reveal()" in line and not line.strip().startswith("#"):
                    sites.append(f"{module.name}:{number}")
        assert len(sites) <= 3, (
            f"the raw credential is now unwrapped in {len(sites)} places ({sites}); "
            f"each is a place it can leak"
        )

    def test_an_empty_request_token_is_refused(self) -> None:
        """An empty callback parameter must not reach the broker as a session
        exchange attempt."""
        import asyncio

        from algotrader.broker.kite import session as ksession

        with pytest.raises(AuthenticationError, match="carried nothing"):
            asyncio.run(
                ksession.exchange_request_token(_auth(), "", api_secret=SecretString(API_SECRET))
            )
