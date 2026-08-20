"""Behaviour of the Kite adapters, against a fake client (E02-S03/S04/S06/S07).

No network and no credentials. What is under test is this codebase's handling
of the broker's shapes — the SDK is not the thing being verified.

The fake deliberately returns the payload shapes Kite documents, including the
awkward ones: naive IST timestamps, prices as strings, a status vocabulary
wider than ours.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any, ClassVar

import pytest

from algotrader.broker.adapter import AmbiguousOrderError
from algotrader.broker.kite import mapping
from algotrader.broker.kite.auth import KiteAuthManager, next_expiry
from algotrader.broker.kite.instruments import InstrumentSync, tick_grid_is_respected
from algotrader.broker.kite.market_data import KiteMarketDataAdapter
from algotrader.broker.kite.trading import KiteTradingAdapter, StaleMarginError
from algotrader.common.enums import Exchange, OrderIntent, OrderStatus, OrderType, Product, Side
from algotrader.common.models.market import Instrument
from algotrader.common.models.trading import OrderRequest
from algotrader.common.secrets import SecretString


def _auth() -> KiteAuthManager:
    auth = KiteAuthManager(
        api_key="pubkey", api_secret=SecretString("secret_value_x"), client_id="AB1234"
    )
    auth.adopt_token("access_token_value")
    return auth


class FakeKite:
    """Stands in for ``KiteConnect``, returning documented payload shapes."""

    def __init__(self, **canned: Any) -> None:
        self.canned = canned
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def place_order(self, **kwargs: Any) -> str:
        self.calls.append(("place_order", kwargs))
        if isinstance(self.canned.get("place_order"), Exception):
            raise self.canned["place_order"]
        return self.canned.get("place_order", "240819000000001")

    def modify_order(self, **kwargs: Any) -> str:
        self.calls.append(("modify_order", kwargs))
        return "240819000000001"

    def cancel_order(self, **kwargs: Any) -> str:
        self.calls.append(("cancel_order", kwargs))
        return "240819000000001"

    def orders(self) -> list[dict[str, Any]]:
        self.calls.append(("orders", {}))
        return self.canned.get("orders", [])

    def positions(self) -> dict[str, Any]:
        self.calls.append(("positions", {}))
        return self.canned.get("positions", {"net": []})

    def margins(self, segment: str) -> dict[str, Any]:
        self.calls.append(("margins", {"segment": segment}))
        return self.canned.get("margins", {})

    def instruments(self, exchange: str) -> list[dict[str, Any]]:
        self.calls.append(("instruments", {"exchange": exchange}))
        return self.canned.get("instruments", [])

    def historical_data(self, *args: Any) -> list[dict[str, Any]]:
        self.calls.append(("historical_data", {"args": args}))
        return self.canned.get("historical_data", [])

    def ltp(self, symbols: list[str]) -> dict[str, Any]:
        self.calls.append(("ltp", {"symbols": symbols}))
        return self.canned.get("ltp", {})


def _order_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "order_id": "240819000000001",
        "tag": "a" * 20,
        "tradingsymbol": "INFY",
        "transaction_type": "BUY",
        "order_type": "MARKET",
        "product": "MIS",
        "quantity": 10,
        "price": 0,
        "trigger_price": 0,
        "status": "COMPLETE",
        "filled_quantity": 10,
        "average_price": "1502.35",
        # Kite returns NAIVE IST datetimes.
        "order_timestamp": dt.datetime(2026, 8, 19, 9, 20, 15),
        "status_message": None,
    }
    row.update(overrides)
    return row


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


class TestPriceAndTimeConversion:
    def test_paise_convert_exactly(self) -> None:
        """Float division here would introduce error into every price."""
        assert mapping.paise_to_rupees(250075) == Decimal("2500.75")
        assert isinstance(mapping.paise_to_rupees(1), Decimal)

    def test_a_naive_broker_timestamp_is_treated_as_ist(self) -> None:
        """Reading it as UTC would shift every order 5h30m into the past and
        silently corrupt any time-ordered reconstruction of a trade."""
        parsed = mapping.parse_broker_timestamp(dt.datetime(2026, 8, 19, 9, 20, 0))
        assert parsed.tzinfo is dt.UTC
        assert parsed == dt.datetime(2026, 8, 19, 3, 50, 0, tzinfo=dt.UTC)

    def test_an_unparseable_timestamp_raises(self) -> None:
        with pytest.raises(mapping.MappingError):
            mapping.parse_broker_timestamp("not a time")

    def test_an_unknown_status_becomes_reconcile_required(self) -> None:
        """Guessing OPEN would strand a position; guessing REJECTED would
        fabricate one. Escalating is the only safe answer."""
        assert mapping.status_in("SOME NEW STATUS") is OrderStatus.RECONCILE_REQUIRED

    def test_known_statuses_map(self) -> None:
        assert mapping.status_in("COMPLETE") is OrderStatus.FILLED
        assert mapping.status_in("REJECTED") is OrderStatus.REJECTED
        assert mapping.status_in("TRIGGER PENDING") is OrderStatus.OPEN


class TestTickRoundingHasADirection:
    """Rounding direction is not cosmetic — it decides who pays the spread."""

    def test_a_buy_rounds_down(self) -> None:
        assert mapping.round_to_tick(Decimal("100.037"), Decimal("0.05"), side=Side.BUY) == Decimal(
            "100.00"
        )

    def test_a_sell_rounds_up(self) -> None:
        assert mapping.round_to_tick(
            Decimal("100.037"), Decimal("0.05"), side=Side.SELL
        ) == Decimal("100.05")

    def test_neither_side_ever_pays_more_than_intended(self) -> None:
        """The invariant: a buy limit never rises, a sell limit never falls."""
        for raw in ("100.01", "100.024", "100.049", "99.999"):
            price = Decimal(raw)
            assert mapping.round_to_tick(price, Decimal("0.05"), side=Side.BUY) <= price
            assert mapping.round_to_tick(price, Decimal("0.05"), side=Side.SELL) >= price

    def test_a_price_already_on_the_grid_does_not_move(self) -> None:
        for side in (Side.BUY, Side.SELL):
            assert mapping.round_to_tick(Decimal("100.05"), Decimal("0.05"), side=side) == Decimal(
                "100.05"
            )

    def test_a_non_standard_tick_is_honoured(self) -> None:
        """A hardcoded 0.05 is wrong for a large slice of the market."""
        assert mapping.round_to_tick(Decimal("10.037"), Decimal("0.01"), side=Side.BUY) == Decimal(
            "10.03"
        )

    def test_a_zero_tick_raises_rather_than_dividing(self) -> None:
        with pytest.raises(mapping.MappingError):
            mapping.round_to_tick(Decimal("100"), Decimal(0), side=Side.BUY)

    def test_the_grid_assertion_agrees_with_the_rounding(self) -> None:
        rounded = mapping.round_to_tick(Decimal("100.037"), Decimal("0.05"), side=Side.BUY)
        assert tick_grid_is_respected(rounded, Decimal("0.05"))
        assert not tick_grid_is_respected(Decimal("100.037"), Decimal("0.05"))


class TestOrderSubmission:
    async def test_a_placed_order_returns_the_broker_id(self) -> None:
        client = FakeKite()
        adapter = KiteTradingAdapter(auth=_auth(), client=client)
        assert await adapter.place_order(_request()) == "240819000000001"

    async def test_the_tag_reaches_the_broker(self) -> None:
        client = FakeKite()
        adapter = KiteTradingAdapter(auth=_auth(), client=client)
        await adapter.place_order(_request())
        _, kwargs = client.calls[0]
        assert kwargs["tag"] == mapping.broker_tag("a" * 32)

    async def test_an_empty_order_id_is_ambiguous_not_success(self) -> None:
        """A blank id means we cannot identify what may have been created."""
        adapter = KiteTradingAdapter(auth=_auth(), client=FakeKite(place_order=""))
        with pytest.raises(AmbiguousOrderError, match="Reconcile by tag"):
            await adapter.place_order(_request())

    async def test_a_network_failure_while_placing_is_ambiguous(self) -> None:
        import kiteconnect.exceptions as kx

        adapter = KiteTradingAdapter(
            auth=_auth(), client=FakeKite(place_order=kx.NetworkException("timeout"))
        )
        with pytest.raises(AmbiguousOrderError):
            await adapter.place_order(_request())

    async def test_modify_with_nothing_to_change_is_refused(self) -> None:
        adapter = KiteTradingAdapter(auth=_auth(), client=FakeKite())
        with pytest.raises(ValueError, match="nothing to modify"):
            await adapter.modify_order("240819000000001")


class TestRecoveryByTag:
    """The path taken after an ambiguous failure. It has to actually find it."""

    async def test_an_order_is_found_by_its_client_order_id(self) -> None:
        client_order_id = "a" * 32
        rows = [_order_row(tag=mapping.broker_tag(client_order_id))]
        adapter = KiteTradingAdapter(auth=_auth(), client=FakeKite(orders=rows))
        found = await adapter.find_by_client_order_id(client_order_id)
        assert found is not None
        assert found.broker_order_id == "240819000000001"
        assert found.status is OrderStatus.FILLED

    async def test_an_absent_order_returns_none_not_an_error(self) -> None:
        """Absent is a real answer — it is what authorises a resubmission."""
        adapter = KiteTradingAdapter(auth=_auth(), client=FakeKite(orders=[]))
        assert await adapter.find_by_client_order_id("b" * 32) is None

    async def test_another_orders_tag_does_not_match(self) -> None:
        rows = [_order_row(tag=mapping.broker_tag("z" * 32))]
        adapter = KiteTradingAdapter(auth=_auth(), client=FakeKite(orders=rows))
        assert await adapter.find_by_client_order_id("a" * 32) is None

    async def test_the_orderbook_maps_into_our_order_model(self) -> None:
        adapter = KiteTradingAdapter(auth=_auth(), client=FakeKite(orders=[_order_row()]))
        orders = await adapter.fetch_orderbook()
        assert len(orders) == 1
        order = orders[0]
        assert order.symbol == "INFY"
        assert order.side is Side.BUY
        assert order.product is Product.MIS
        assert order.average_price == Decimal("1502.35")
        assert order.placed_at.tzinfo is dt.UTC


class TestMarginStaleness:
    """Sizing against an aged margin is a time-of-check/time-of-use gap."""

    _MARGINS: ClassVar[dict[str, Any]] = {
        "available": {"cash": "500000", "live_balance": "487500"},
        "utilised": {"debits": "12500"},
    }

    async def test_margins_map_to_decimals(self) -> None:
        adapter = KiteTradingAdapter(auth=_auth(), client=FakeKite(margins=self._MARGINS))
        snapshot = await adapter.fetch_margins()
        assert snapshot.available_margin == Decimal("487500")
        assert snapshot.used_margin == Decimal("12500")

    async def test_a_fresh_snapshot_is_usable(self) -> None:
        adapter = KiteTradingAdapter(auth=_auth(), client=FakeKite(margins=self._MARGINS))
        await adapter.fetch_margins()
        assert adapter.margin_for_sizing().available_margin == Decimal("487500")

    async def test_an_aged_snapshot_is_refused(self) -> None:
        """Margin falls after every fill; a stale reading authorises a position
        the account cannot carry."""
        adapter = KiteTradingAdapter(
            auth=_auth(), client=FakeKite(margins=self._MARGINS), margin_ttl_seconds=5.0
        )
        await adapter.fetch_margins()
        later = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=30)
        with pytest.raises(StaleMarginError, match="re-fetch before sizing"):
            adapter.margin_for_sizing(now=later)

    def test_sizing_before_any_fetch_is_refused(self) -> None:
        adapter = KiteTradingAdapter(auth=_auth(), client=FakeKite())
        with pytest.raises(StaleMarginError, match="no margin fetched"):
            adapter.margin_for_sizing()


class TestInstrumentSync:
    class FakeRepo:
        def __init__(self, existing: set[str]) -> None:
            self._by_symbol = dict.fromkeys(existing, 1)
            self.rows: list[dict[str, Any]] = []

        async def refresh_cache(self) -> int:
            return len(self._by_symbol)

        async def upsert(self, rows: list[dict[str, Any]]) -> int:
            self.rows = rows
            return len(rows)

    class FakeAdapter:
        def __init__(self, instruments: list[Instrument]) -> None:
            self._instruments = instruments

        async def fetch_instruments(self, exchange: str = "NSE") -> list[Instrument]:
            return self._instruments

    @staticmethod
    def _instrument(symbol: str, tick: str = "0.05") -> Instrument:
        return Instrument(
            symbol=symbol,
            exchange=Exchange.NSE,
            broker_token=f"tok{symbol}",
            tick_size=Decimal(tick),
        )

    async def test_new_and_missing_symbols_are_reported(self) -> None:
        adapter = self.FakeAdapter([self._instrument("INFY"), self._instrument("TCS")])
        repo = self.FakeRepo({"INFY", "DELISTED"})
        result = await InstrumentSync(adapter, repo).run()
        assert result.new_symbols == ("TCS",)
        assert result.missing_symbols == ("DELISTED",)

    async def test_tick_size_is_carried_through(self) -> None:
        """A hardcoded 0.05 would put orders off-grid for a slice of the market."""
        adapter = self.FakeAdapter([self._instrument("SOMESTOCK", tick="0.01")])
        repo = self.FakeRepo(set())
        await InstrumentSync(adapter, repo).run()
        assert repo.rows[0]["tick_size"] == Decimal("0.01")

    async def test_a_truncated_dump_is_flagged_as_implausible(self) -> None:
        """A short download parses fine and would silently shrink the universe."""
        adapter = self.FakeAdapter([self._instrument("INFY")])
        repo = self.FakeRepo(set())
        result = await InstrumentSync(adapter, repo).run()
        assert result.looks_implausible

    async def test_a_full_dump_is_not_flagged(self) -> None:
        instruments = [self._instrument(f"SYM{i}") for i in range(1200)]
        adapter = self.FakeAdapter(instruments)
        repo = self.FakeRepo({f"SYM{i}" for i in range(1200)})
        result = await InstrumentSync(adapter, repo).run()
        assert not result.looks_implausible


class TestSessionExpiry:
    def test_expiry_lands_on_the_next_reauth_time(self) -> None:
        from algotrader.common.calendar import IST

        # 10:00 IST Wednesday -> next 07:00 IST is Thursday.
        now = dt.datetime(2026, 8, 19, 10, 0, tzinfo=IST).astimezone(dt.UTC)
        expiry = next_expiry(now, reauth_hour=7)
        assert expiry.astimezone(IST).date() == dt.date(2026, 8, 20)
        assert expiry.astimezone(IST).hour == 7

    def test_before_the_reauth_time_expiry_is_the_same_day(self) -> None:
        from algotrader.common.calendar import IST

        now = dt.datetime(2026, 8, 19, 5, 0, tzinfo=IST).astimezone(dt.UTC)
        assert next_expiry(now, reauth_hour=7).astimezone(IST).date() == dt.date(2026, 8, 19)

    def test_a_fresh_session_needs_no_reauth_notice(self) -> None:
        assert _auth().notice_if_reauth_needed() is None

    def test_an_unauthenticated_manager_asks_for_reauth(self) -> None:
        auth = KiteAuthManager(
            api_key="k", api_secret=SecretString("s_value_12"), client_id="AB1234"
        )
        notice = auth.notice_if_reauth_needed()
        assert notice is not None
        assert "no session yet today" in notice.reason


class TestReadPath:
    async def test_instruments_map_and_skip_bad_rows(self) -> None:
        """One malformed row must not lose the other few thousand."""
        rows = [
            {"tradingsymbol": "INFY", "instrument_token": 408065, "tick_size": 0.05, "lot_size": 1},
            {"tradingsymbol": "BROKEN"},  # no token
            {"tradingsymbol": "TCS", "instrument_token": 2953217, "tick_size": 0.05, "lot_size": 1},
        ]
        adapter = KiteMarketDataAdapter(auth=_auth(), client=FakeKite(instruments=rows))
        got = await adapter.fetch_instruments()
        assert [i.symbol for i in got] == ["INFY", "TCS"]

    async def test_an_unsupported_timeframe_is_refused(self) -> None:
        from algotrader.common.enums import Timeframe

        adapter = KiteMarketDataAdapter(auth=_auth(), client=FakeKite())
        with pytest.raises(mapping.MappingError, match=r"no historical interval"):
            await adapter.fetch_historical(
                "408065",
                Timeframe.W1,
                dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                dt.datetime(2026, 2, 1, tzinfo=dt.UTC),
            )

    async def test_an_inverted_range_is_refused(self) -> None:
        from algotrader.common.enums import Timeframe

        adapter = KiteMarketDataAdapter(auth=_auth(), client=FakeKite())
        with pytest.raises(ValueError, match=r"not before"):
            await adapter.fetch_historical(
                "408065",
                Timeframe.D1,
                dt.datetime(2026, 2, 1, tzinfo=dt.UTC),
                dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            )
