"""Security properties of the broker layer (E02).

This is the layer that holds a bearer credential and can move money, so the
properties below are the ones worth asserting structurally rather than trusting
to review:

1. **The re-auth notice cannot carry a login link.** Enforced by the shape of
   the type, because a reviewer noticing is not a control.
2. **The access token never renders**, through any path a log line might take.
3. **A read-only adapter cannot trade**, and a trading adapter is not merely a
   read-only one with the guard overridden.
4. **An unrecognised broker failure during a write fails closed** — ambiguous,
   never retryable.
5. **Market protection cannot be omitted or zeroed** on the order types Zerodha
   rejects without it.
"""

from __future__ import annotations

import datetime as dt
import pickle
import uuid
from decimal import Decimal
from typing import Any

import pytest

from algotrader.broker.adapter import (
    AmbiguousOrderError,
    AuthenticationError,
    OrderRejectedError,
    RateLimitError,
)
from algotrader.broker.kite import mapping
from algotrader.broker.kite.auth import KiteAuthManager, ReauthNotice, checksum
from algotrader.broker.kite.errors import classify, is_retryable
from algotrader.broker.kite.market_data import KiteMarketDataAdapter, KiteReads
from algotrader.broker.kite.trading import KiteTradingAdapter
from algotrader.common.enums import OrderIntent, OrderType, Product, Side
from algotrader.common.models.trading import OrderRequest
from algotrader.common.secrets import SecretString

TOKEN = "kite_access_token_SUPERSECRET_9f2b"
API_SECRET = "api_secret_DO_NOT_LEAK_7c1d"


def _percent_format(value: object) -> str:
    """Percent formatting, kept as its own leak path.

    Not an f-string: `%s` reaches `__str__` by a different route, and this
    module exists to check every route a credential could take into a log line.
    """
    return "%s" % (value,)  # noqa: UP031 - the operator IS the thing under test


def _auth() -> KiteAuthManager:
    return KiteAuthManager(
        api_key="pubkey123",
        api_secret=SecretString(API_SECRET),
        client_id="AB1234",
    )


class TestTheReauthNoticeCannotCarryALink:
    """The anti-phishing control, enforced by the type rather than by review.

    Sending the login link at a predictable time trains the operator to
    authenticate from an inbound message. Anyone who can post one message into
    that chat can then send a link to THEIR Kite app; the operator authenticates
    on a genuine Zerodha page and hands over a request_token the attacker
    exchanges with their own api_secret. 2FA does not help — the operator is
    authorising the wrong application. No server-side check helps either,
    because the attack completes entirely outside this system.
    """

    def test_the_notice_has_no_url_shaped_field(self) -> None:
        fields = set(ReauthNotice.__slots__)
        forbidden = {"url", "link", "login_url", "login_link", "href", "uri"}
        assert not (fields & forbidden), (
            f"ReauthNotice grew {sorted(fields & forbidden)}. That field is how the "
            f"phishing reflex gets re-introduced — the notice must name a destination, "
            f"never address one."
        )

    def test_the_rendered_message_contains_no_url(self) -> None:
        text = ReauthNotice(trade_date=dt.date(2026, 8, 19), reason="expired").message()
        assert "http://" not in text and "https://" not in text
        assert "kite.zerodha.com" not in text

    def test_the_message_warns_that_a_link_is_never_legitimate(self) -> None:
        """The operator needs to know an inbound link is always an attack."""
        text = ReauthNotice(trade_date=dt.date(2026, 8, 19), reason="expired").message()
        assert "never contain a login link" in text

    def test_the_login_url_still_exists_for_the_dashboard(self) -> None:
        """It is not that the URL is secret — it is that it is not SENT."""
        url = _auth().login_url()
        assert url.startswith("https://kite.zerodha.com/connect/login")
        assert "pubkey123" in url


class TestTheAccessTokenNeverRenders:
    def test_token_is_returned_as_a_secret_not_a_string(self) -> None:
        auth = _auth()
        auth.adopt_token(TOKEN)
        assert not isinstance(auth.token(), str)

    @pytest.mark.parametrize(
        "render",
        [str, repr, lambda s: f"{s}", _percent_format, lambda s: f"{s!r}"],
        ids=["str", "repr", "fstring", "percent", "fstring-repr"],
    )
    def test_no_render_path_exposes_it(self, render: Any) -> None:
        auth = _auth()
        auth.adopt_token(TOKEN)
        assert TOKEN not in render(auth.token())

    def test_the_session_envelope_carries_no_token(self) -> None:
        """The envelope is what gets persisted; a token in Redis is a bearer
        credential sitting in a keyspace that gets dumped and replicated."""
        auth = _auth()
        auth.adopt_token(TOKEN)
        envelope = auth.envelope()
        assert envelope is not None
        serialised = envelope.model_dump_json()
        assert TOKEN not in serialised
        assert not (set(type(envelope).model_fields) & {"token", "access_token"})

    def test_the_token_does_not_pickle(self) -> None:
        auth = _auth()
        auth.adopt_token(TOKEN)
        with pytest.raises(Exception, match=r"pickle|Secret"):
            pickle.dumps(auth.token())

    def test_the_api_secret_does_not_leak_through_the_checksum(self) -> None:
        digest = checksum("pubkey123", "req_tok", SecretString(API_SECRET))
        assert API_SECRET not in digest
        assert len(digest) == 64

    def test_asking_for_a_token_without_a_session_raises(self) -> None:
        """Returning None would let a caller send an unauthenticated request and
        read the resulting 403 as a market condition."""
        with pytest.raises(AuthenticationError):
            _auth().token()

    def test_an_expired_session_refuses_the_token(self) -> None:
        auth = _auth()
        auth.adopt_token(TOKEN, now=dt.datetime(2026, 8, 18, 1, 0, tzinfo=dt.UTC))
        assert not auth.is_valid(now=dt.datetime(2026, 8, 20, 1, 0, tzinfo=dt.UTC))


class TestReadOnlyMeansReadOnly:
    @pytest.fixture
    def read_only(self) -> KiteMarketDataAdapter:
        return KiteMarketDataAdapter(auth=_auth(), client=object())

    async def test_place_order_is_refused(self, read_only: KiteMarketDataAdapter) -> None:
        with pytest.raises(PermissionError):
            await read_only.place_order(_market_request())

    async def test_modify_order_is_refused(self, read_only: KiteMarketDataAdapter) -> None:
        with pytest.raises(PermissionError):
            await read_only.modify_order("240101000000001")

    async def test_cancel_order_is_refused(self, read_only: KiteMarketDataAdapter) -> None:
        with pytest.raises(PermissionError):
            await read_only.cancel_order("240101000000001")

    def test_the_trading_adapter_does_not_inherit_the_guard(self) -> None:
        """'May trade' must be stated positively, not achieved by overriding a
        refusal.

        If the trading adapter inherited ``ReadOnlyGuard``, then forgetting one
        override would produce an execution adapter that silently cannot place
        orders — a failure that only shows up with real money on the line.
        """
        from algotrader.broker.adapter import ReadOnlyGuard

        assert ReadOnlyGuard not in KiteTradingAdapter.__mro__
        assert KiteReads in KiteTradingAdapter.__mro__

    def test_the_read_only_adapter_does_inherit_it(self) -> None:
        from algotrader.broker.adapter import ReadOnlyGuard

        assert ReadOnlyGuard in KiteMarketDataAdapter.__mro__

    def test_live_streaming_refuses_rather_than_using_the_vulnerable_client(self) -> None:
        """KiteTicker pulls in autobahn 19.11.2 (CVE-2020-35678) plus a Twisted
        reactor. The wire protocol is fully documented, so E05 implements it
        directly. Refusing here keeps that decision from being undone by reflex.
        """
        adapter = KiteMarketDataAdapter(auth=_auth(), client=object())
        with pytest.raises(NotImplementedError, match=r"autobahn|WebSocket protocol"):
            adapter.subscribe(["408065"])


class TestUnknownFailuresFailClosed:
    """The disposition of an unrecognised error is the whole safety question."""

    class _NovelError(Exception):
        """Stands in for a broker error code that does not exist yet."""

        code = 500

    def test_an_unknown_error_during_a_write_is_ambiguous(self) -> None:
        err = classify(self._NovelError("something new"), mutating=True)
        assert isinstance(err, AmbiguousOrderError), (
            "an unrecognised failure on a write must route to reconcile-by-tag, "
            "never to a retry — that is how a timeout becomes two positions"
        )

    def test_an_unknown_error_during_a_read_is_just_a_failure(self) -> None:
        err = classify(self._NovelError("something new"), mutating=False)
        assert not isinstance(err, AmbiguousOrderError)

    def test_a_timeout_on_a_write_is_ambiguous_not_retryable(self) -> None:
        err = classify(TimeoutError("read timed out"), mutating=True)
        assert isinstance(err, AmbiguousOrderError)
        assert not is_retryable(err)

    def test_only_a_rate_limit_is_retryable(self) -> None:
        """A 429 was refused BEFORE reaching the exchange, so retrying is safe.
        Nothing else is."""
        import kiteconnect.exceptions as kx

        limited = classify(kx.NetworkException("too many", code=429), mutating=True)
        assert isinstance(limited, RateLimitError)
        assert is_retryable(limited)

    def test_a_dead_session_is_an_auth_failure_not_a_retry(self) -> None:
        import kiteconnect.exceptions as kx

        err = classify(kx.TokenException("token expired"), mutating=True)
        assert isinstance(err, AuthenticationError)
        assert not is_retryable(err)

    def test_a_definitive_rejection_is_never_ambiguous(self) -> None:
        """A 400 means the exchange saw it and said no. Reconciling would find
        nothing and could prompt a resubmission of an order that was correctly
        refused."""
        import kiteconnect.exceptions as kx

        err = classify(kx.OrderException("bad price", code=400), mutating=True)
        assert isinstance(err, OrderRejectedError)
        assert not isinstance(err, AmbiguousOrderError)


def _market_request(**overrides: Any) -> OrderRequest:
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


class TestMarketProtectionCannotBeSkipped:
    """Zerodha rejects MARKET/SL-M without it, and rejects 0 outright.

    The order that matters most is the forced square-off at the deadline. A
    rejection there is the one you cannot afford, so this is gated twice: once
    in ``OrderRequest`` and again here, at the boundary.
    """

    @pytest.fixture
    def adapter(self) -> KiteTradingAdapter:
        return KiteTradingAdapter(auth=_auth(), client=object())

    def test_a_protected_market_order_builds(self, adapter: KiteTradingAdapter) -> None:
        params = adapter._build_params(_market_request())
        assert params["market_protection"] == -1.0
        assert params["order_type"] == "MARKET"

    def test_market_without_protection_is_refused_at_the_boundary(
        self, adapter: KiteTradingAdapter
    ) -> None:
        request = _market_request()
        stripped = request.model_copy(update={"market_protection": None})
        with pytest.raises(OrderRejectedError, match="market_protection"):
            adapter._build_params(stripped)

    def test_zero_protection_is_refused(self, adapter: KiteTradingAdapter) -> None:
        request = _market_request()
        zeroed = request.model_copy(update={"market_protection": Decimal(0)})
        with pytest.raises(OrderRejectedError, match="rejected by the broker"):
            adapter._build_params(zeroed)

    def test_slm_also_requires_protection(self, adapter: KiteTradingAdapter) -> None:
        request = _market_request(
            order_type=OrderType.SLM, trigger_price=Decimal("990"), market_protection=Decimal(-1)
        )
        stripped = request.model_copy(update={"market_protection": None})
        with pytest.raises(OrderRejectedError):
            adapter._build_params(stripped)

    def test_a_limit_order_needs_no_protection(self) -> None:
        """The requirement is specific to MARKET and SL-M; applying it to LIMIT
        would reject perfectly valid orders.

        Built with a tick resolver because a priced order now requires one —
        see TestAPricedOrderIsAlwaysOnTheTickGrid. That rule is separate from
        market protection and must not be confused with it here.
        """
        priced = KiteTradingAdapter(
            auth=_auth(), client=object(), tick_size_for=lambda _s: Decimal("0.05")
        )
        request = _market_request(
            order_type=OrderType.LIMIT,
            limit_price=Decimal("1000"),
            market_protection=None,
        )
        params = priced._build_params(request)
        assert "market_protection" not in params


class TestTheIdempotencyKeySurvivesTheBrokerField:
    """Kite's tag is alphanumeric, max 20 — shorter than our id.

    If the tag did not round-trip, ``find_by_client_order_id`` would never match
    and every ambiguous order would look absent. That is exactly the state in
    which a caller wrongly resubmits.
    """

    def test_the_tag_fits_the_broker_field(self) -> None:
        tag = mapping.broker_tag("a" * 32)
        assert len(tag) <= mapping.BROKER_TAG_MAX
        assert tag.isalnum()

    def test_the_tag_is_deterministic(self) -> None:
        assert mapping.broker_tag("b" * 40) == mapping.broker_tag("b" * 40)

    def test_a_non_alphanumeric_id_is_refused_rather_than_mangled(self) -> None:
        """The broker would strip the offending characters and the stored tag
        would no longer match what the recovery path searches for."""
        with pytest.raises(mapping.MappingError, match="alphanumeric"):
            mapping.broker_tag("order-with-dashes-1234")

    def test_the_submitted_tag_matches_what_recovery_searches_for(self) -> None:
        """The two paths must agree, or idempotency is decorative."""
        adapter = KiteTradingAdapter(auth=_auth(), client=object())
        client_order_id = "c" * 32
        submitted = adapter._build_params(_market_request(client_order_id=client_order_id))["tag"]
        assert submitted == mapping.broker_tag(client_order_id)


class TestAlgoIdIsOptionalByDesign:
    def test_no_algo_id_is_sent_when_unset(self) -> None:
        """SEBI's framework has the BROKER tag sub-10-OPS strategies, and
        Zerodha's compliance guidance never asks the developer to supply one.
        Sending an empty string would be worse than sending nothing."""
        adapter = KiteTradingAdapter(auth=_auth(), client=object(), algo_id="")
        assert "algo_id" not in adapter._build_params(_market_request())

    def test_a_configured_algo_id_is_attached(self) -> None:
        adapter = KiteTradingAdapter(auth=_auth(), client=object(), algo_id="GENERIC123")
        assert adapter._build_params(_market_request())["algo_id"] == "GENERIC123"
