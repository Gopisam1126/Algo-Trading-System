"""The paper broker: a simulated Kite client behind the REAL adapter (E15-S10).

Almost every test here goes through ``KiteTradingAdapter`` over ``PaperKite``,
because that seam is the story: paper must run the adapter's mapping, snapping,
market-protection gate, tag truncation and error classification exactly as
live does, with only the client different.
"""

from __future__ import annotations

import datetime as dt
import threading
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from kiteconnect import exceptions as kx

from algotrader.broker.adapter import BrokerError, OrderRejectedError
from algotrader.broker.kite import mapping
from algotrader.broker.kite.trading import KiteTradingAdapter
from algotrader.broker.paper import (
    ZERODHA_NSE_INTRADAY,
    ChargeSchedule,
    PaperConfig,
    PaperKite,
    trading_client,
)
from algotrader.common.enums import OrderIntent, OrderStatus, OrderType, Product, Side, SystemMode
from algotrader.common.models.trading import OrderRequest
from algotrader.ingest.quotes import QuoteState, quote_reader

NOW = dt.datetime(2026, 8, 25, 5, 0, tzinfo=dt.UTC)  # 10:30 IST


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> dt.datetime:
        return self.now


@dataclass
class Tape:
    """The quotes the paper broker reads."""

    quotes: dict[str, QuoteState]

    def __call__(self, symbol: str) -> QuoteState | None:
        return self.quotes.get(symbol)

    def set(self, symbol: str, ltp: str, *, at: dt.datetime = NOW, **kw: Any) -> None:
        self.quotes[symbol] = QuoteState(symbol=symbol, ltp=Decimal(ltp), as_of=at, **kw)


def _paper(**config: Any) -> tuple[PaperKite, Tape, Clock, KiteTradingAdapter]:
    tape, clock = Tape({}), Clock()
    client = PaperKite(quotes=tape, clock=clock, config=PaperConfig(**config))
    adapter = KiteTradingAdapter(
        auth=None,  # type: ignore[arg-type]
        client=client,
        tick_size_for=lambda _s: Decimal("0.05"),
    )
    return client, tape, clock, adapter


def _request(
    side: Side = Side.BUY,
    quantity: int = 50,
    *,
    symbol: str = "INFY",
    order_type: OrderType = OrderType.MARKET,
    trigger: str | None = None,
    key: str = "a" * 64,
) -> OrderRequest:
    return OrderRequest(
        client_order_id=key,
        correlation_id="00000000-0000-0000-0000-000000000001",  # type: ignore[arg-type]
        symbol=symbol,
        side=side,
        order_type=order_type,
        product=Product.MIS,
        quantity=quantity,
        trigger_price=Decimal(trigger) if trigger else None,
        intent=OrderIntent.STOP if order_type is OrderType.SLM else OrderIntent.ENTRY,
        algo_id=None,
        market_protection=Decimal("-1"),
    )


async def _only(adapter: KiteTradingAdapter):
    (order,) = await adapter.fetch_orderbook()
    return order


# ---------------------------------------------------------------------------
# The one injection
# ---------------------------------------------------------------------------


class TestTheOneInjection:
    def test_live_gets_the_live_client(self) -> None:
        made: list[str] = []
        client = trading_client(
            SystemMode.LIVE,
            live=lambda: made.append("live") or "kite",
            paper=lambda: made.append("paper") or "paper",
        )
        assert (client, made) == ("kite", ["live"])

    @pytest.mark.parametrize("mode", [SystemMode.PAPER, SystemMode.ALERT_ONLY, SystemMode.APPROVAL])
    def test_every_other_mode_gets_paper_and_never_builds_a_kite_client(
        self, mode: SystemMode
    ) -> None:
        def live() -> Any:
            raise AssertionError(f"{mode} constructed the live client")

        assert trading_client(mode, live=live, paper=lambda: "paper") == "paper"

    def test_the_paper_client_holds_nothing_that_reaches_the_broker(self) -> None:
        client, *_ = _paper()
        names = {n.lower() for n in vars(client)}
        for forbidden in ("api_key", "access_token", "_session", "root", "session"):
            assert not any(forbidden in n for n in names), names


# ---------------------------------------------------------------------------
# Fills, through the real adapter
# ---------------------------------------------------------------------------


class TestMarketOrders:
    async def test_a_buy_fills_at_the_ask_plus_slippage_on_the_next_read(self) -> None:
        _client, tape, _, adapter = _paper(slippage_bps=Decimal("10"))
        tape.set("INFY", "1200.00", bid=Decimal("1199.90"), ask=Decimal("1200.10"))
        order_id = await adapter.place_order(_request())
        assert order_id.startswith("PAPER-")
        order = await _only(adapter)
        assert order.status is OrderStatus.FILLED and order.filled_quantity == 50
        # 1200.10 x 1.001 = 1201.3001 -> rounded UP to the tick: 1201.35
        assert order.average_price == Decimal("1201.35")

    async def test_a_sell_fills_at_the_bid_minus_slippage(self) -> None:
        _client, tape, _, adapter = _paper(slippage_bps=Decimal("10"))
        tape.set("INFY", "1200.00", bid=Decimal("1199.90"), ask=Decimal("1200.10"))
        await adapter.place_order(_request(Side.SELL))
        # 1199.90 x 0.999 = 1198.7001 -> rounded DOWN to the tick: 1198.70
        assert (await _only(adapter)).average_price == Decimal("1198.70")

    async def test_no_bid_or_ask_fills_at_the_ltp(self) -> None:
        _client, tape, _, adapter = _paper(slippage_bps=Decimal("0"))
        tape.set("INFY", "1200.00")
        await adapter.place_order(_request())
        assert (await _only(adapter)).average_price == Decimal("1200.00")

    async def test_the_adapters_tag_is_what_the_broker_stores(self) -> None:
        """The truncation the recovery path depends on runs in paper too."""
        _client, tape, _, adapter = _paper()
        tape.set("INFY", "1200.00")
        key = "b" * 64
        await adapter.place_order(_request(key=key))
        found = await adapter.find_by_client_order_id(key)
        assert found is not None and found.client_order_id == mapping.broker_tag(key)

    async def test_no_quote_is_rejected_never_filled_at_a_guess(self) -> None:
        _client, _tape, _, adapter = _paper()
        await adapter.place_order(_request())
        order = await _only(adapter)
        assert order.status is OrderStatus.REJECTED and order.filled_quantity == 0
        assert "no fresh quote" in str(order.rejection_reason)

    async def test_a_stale_quote_is_no_quote(self) -> None:
        _client, tape, _clock, adapter = _paper(max_quote_age_seconds=10.0)
        tape.set("INFY", "1200.00", at=NOW - dt.timedelta(seconds=11))
        await adapter.place_order(_request())
        assert (await _only(adapter)).status is OrderStatus.REJECTED

    async def test_a_fresh_quote_at_the_limit_fills(self) -> None:
        _client, tape, _clock, adapter = _paper(max_quote_age_seconds=10.0)
        tape.set("INFY", "1200.00", at=NOW - dt.timedelta(seconds=10))
        await adapter.place_order(_request())
        assert (await _only(adapter)).status is OrderStatus.FILLED

    async def test_an_order_larger_than_the_visible_depth_fills_in_parts(self) -> None:
        _client, tape, clock, adapter = _paper(slippage_bps=Decimal("0"))
        tape.set("INFY", "1200.00", ask=Decimal("1200.00"), ask_qty=20)
        await adapter.place_order(_request(quantity=55))
        first = await _only(adapter)
        assert (first.status, first.filled_quantity) == (OrderStatus.OPEN, 20)
        for second in (1, 2):
            clock.now = NOW + dt.timedelta(seconds=second)
            tape.set("INFY", "1201.00", at=clock.now, ask=Decimal("1201.00"), ask_qty=20)
            await _only(adapter)
        third = await _only(adapter)
        assert (third.status, third.filled_quantity) == (OrderStatus.FILLED, 55)
        # VWAP: 20 @ 1200 + 35 @ 1201
        assert third.average_price == Decimal("1200.64")

    async def test_one_quotes_depth_is_taken_once_however_often_it_is_read(self) -> None:
        """QA-E15-42: a reconciliation cycle reads the broker several times,
        and matching on every read filled the same 20 shares of depth three
        times at one instant. New liquidity arrives with a new quote."""
        _client, tape, clock, adapter = _paper(slippage_bps=Decimal("0"))
        tape.set("INFY", "1200.00", ask=Decimal("1200.00"), ask_qty=20)
        await adapter.place_order(_request(quantity=55))
        for _ in range(3):
            await adapter.fetch_orderbook()
            await adapter.fetch_positions()
        assert (await _only(adapter)).filled_quantity == 20
        clock.now = NOW + dt.timedelta(seconds=1)
        tape.set("INFY", "1200.00", at=clock.now, ask=Decimal("1200.00"), ask_qty=20)
        assert (await _only(adapter)).filled_quantity == 40


class TestStops:
    async def test_a_sell_stop_waits_until_the_ltp_crosses(self) -> None:
        _client, tape, _, adapter = _paper(slippage_bps=Decimal("0"))
        tape.set("INFY", "1200.00")
        await adapter.place_order(_request(Side.SELL, order_type=OrderType.SLM, trigger="1186.45"))
        waiting = await _only(adapter)
        assert waiting.status is OrderStatus.OPEN and waiting.filled_quantity == 0
        assert waiting.trigger_price == Decimal("1186.45")  # snapped by the adapter: SELL rounds up
        tape.set("INFY", "1186.50")
        assert (await _only(adapter)).filled_quantity == 0, "not yet crossed"
        tape.set("INFY", "1186.45")
        filled = await _only(adapter)
        assert filled.status is OrderStatus.FILLED and filled.average_price == Decimal("1186.45")

    async def test_a_buy_stop_triggers_at_or_above(self) -> None:
        _client, tape, _, adapter = _paper(slippage_bps=Decimal("0"))
        tape.set("INFY", "1200.00")
        await adapter.place_order(_request(Side.BUY, order_type=OrderType.SLM, trigger="1213.55"))
        tape.set("INFY", "1213.50")
        assert (await _only(adapter)).filled_quantity == 0
        tape.set("INFY", "1214.00")
        assert (await _only(adapter)).status is OrderStatus.FILLED

    async def test_a_stop_with_no_fresh_quote_stays_pending(self) -> None:
        """Nobody can know whether it triggered - it is neither filled nor rejected."""
        _client, tape, _clock, adapter = _paper()
        tape.set("INFY", "1100.00", at=NOW - dt.timedelta(minutes=5))
        await adapter.place_order(_request(Side.SELL, order_type=OrderType.SLM, trigger="1186.45"))
        order = await _only(adapter)
        assert order.status is OrderStatus.OPEN and order.filled_quantity == 0

    async def test_a_gap_through_the_stop_fills_at_the_market_not_the_trigger(self) -> None:
        _client, tape, _, adapter = _paper(slippage_bps=Decimal("0"))
        tape.set("INFY", "1200.00")
        await adapter.place_order(_request(Side.SELL, order_type=OrderType.SLM, trigger="1186.45"))
        await _only(adapter)
        tape.set("INFY", "1150.00")
        assert (await _only(adapter)).average_price == Decimal("1150.00")


class TestMargin:
    async def test_an_entry_the_paper_margin_cannot_carry_is_rejected_by_rms(self) -> None:
        _client, tape, _, adapter = _paper(opening_cash=Decimal("10000"))
        tape.set("INFY", "1200.00")  # 50 x 1200 x 20% = 12,000 > 10,000
        await adapter.place_order(_request(quantity=50))
        order = await _only(adapter)
        assert order.status is OrderStatus.REJECTED and order.filled_quantity == 0
        assert str(order.rejection_reason).startswith("RMS:Margin Exceeds")

    async def test_margin_is_checked_for_the_whole_order_not_the_first_slice(self) -> None:
        """Only 10 shares are on offer, and 10 x 1200 x 20% = 2,400 fits; the
        order is for 50 (12,000), which does not. Checking the slice would let
        a size the account cannot carry fill ten shares at a time."""
        _client, tape, _, adapter = _paper(opening_cash=Decimal("10000"))
        tape.set("INFY", "1200.00", ask=Decimal("1200.00"), ask_qty=10)
        await adapter.place_order(_request(quantity=50))
        order = await _only(adapter)
        assert order.status is OrderStatus.REJECTED and order.filled_quantity == 0

    async def test_the_control_one_that_fits_fills(self) -> None:
        _client, tape, _, adapter = _paper(opening_cash=Decimal("10000"))
        tape.set("INFY", "1200.00")  # 40 x 1200 x 20% = 9,600
        await adapter.place_order(_request(quantity=40))
        assert (await _only(adapter)).status is OrderStatus.FILLED

    async def test_an_exit_is_never_margin_checked(self) -> None:
        _client, tape, _, adapter = _paper(opening_cash=Decimal("10000"))
        tape.set("INFY", "1200.00")
        await adapter.place_order(_request(quantity=40))
        await adapter.fetch_orderbook()
        tape.set("INFY", "1000.00")  # the account is now below water on margin
        await adapter.place_order(_request(Side.SELL, quantity=40, key="c" * 64))
        orders = await adapter.fetch_orderbook()
        assert [o.status for o in orders] == [OrderStatus.FILLED, OrderStatus.FILLED]

    async def test_fetch_margins_reflects_the_open_position_and_the_charges(self) -> None:
        _client, tape, _, adapter = _paper(slippage_bps=Decimal("0"))
        tape.set("INFY", "1200.00")
        before = await adapter.fetch_margins()
        await adapter.place_order(_request(quantity=50))
        await adapter.fetch_orderbook()
        after = await adapter.fetch_margins()
        charges = ZERODHA_NSE_INTRADAY.for_order("BUY", Decimal("60000"))
        assert after.available_cash == before.available_cash - charges
        assert after.used_margin == Decimal("12000.0")  # 60,000 x 20%
        assert after.available_margin == after.available_cash - Decimal("12000")

    async def test_margin_required_is_the_order_value_at_the_margin_rate(self) -> None:
        _client, tape, _, adapter = _paper()
        tape.set("INFY", "1200.00")
        assert await adapter.margin_required("INFY", side=Side.BUY) == Decimal("240.0")

    async def test_margin_required_with_no_quote_is_a_failed_read(self) -> None:
        _client, _tape, _, adapter = _paper()
        with pytest.raises(BrokerError):
            await adapter.margin_required("INFY", side=Side.BUY)


class TestPositions:
    async def test_the_net_row_is_what_reconciliation_reads(self) -> None:
        _client, tape, _, adapter = _paper(slippage_bps=Decimal("0"))
        tape.set("INFY", "1200.00")
        await adapter.place_order(_request(quantity=50))
        await adapter.place_order(_request(Side.SELL, quantity=20, key="d" * 64))
        (position,) = await adapter.fetch_positions()
        assert (position.symbol, position.product, position.exchange) == ("INFY", "MIS", "NSE")
        assert (position.quantity, position.day_buy_quantity, position.day_sell_quantity) == (
            30,
            50,
            20,
        )
        assert position.average_price == Decimal("1200.0")

    async def test_a_flat_position_stays_in_the_list_at_zero(self) -> None:
        """As Kite's does - the reconciler must not read it as held."""
        _client, tape, _, adapter = _paper()
        tape.set("INFY", "1200.00")
        await adapter.place_order(_request(quantity=10))
        await adapter.place_order(_request(Side.SELL, quantity=10, key="e" * 64))
        (position,) = await adapter.fetch_positions()
        assert position.quantity == 0 and position.average_price is None


class TestCharges:
    def test_a_buy_is_charged_as_zerodha_publishes(self) -> None:
        # 66,000 bought: brokerage 19.80, txn 2.0262, SEBI 0.066, stamp 1.98,
        # GST 18% of (19.80 + 2.0262 + 0.066) = 3.94056 -> 27.81276
        assert ZERODHA_NSE_INTRADAY.for_order("BUY", Decimal("66000")) == Decimal("27.81")

    def test_a_sell_carries_stt_and_no_stamp(self) -> None:
        # as above with STT 16.50 for stamp 1.98 -> 42.33276
        assert ZERODHA_NSE_INTRADAY.for_order("SELL", Decimal("66000")) == Decimal("42.33")

    def test_brokerage_is_capped_at_twenty_rupees_an_order(self) -> None:
        schedule = ChargeSchedule(transaction=Decimal(0), sebi=Decimal(0), stamp_buy=Decimal(0))
        assert schedule.for_order("BUY", Decimal("1000000")) == Decimal("23.60")  # 20 + 18% GST

    async def test_a_partly_filled_order_is_charged_once_on_its_total(self) -> None:
        client, tape, clock, adapter = _paper(slippage_bps=Decimal("0"))
        await adapter.place_order(_request(quantity=90))
        for second in range(3):
            clock.now = NOW + dt.timedelta(seconds=second)
            tape.set("INFY", "1000.00", at=clock.now, ask=Decimal("1000.00"), ask_qty=30)
            await adapter.fetch_orderbook()
        assert (await _only(adapter)).status is OrderStatus.FILLED
        assert client.charges_total == ZERODHA_NSE_INTRADAY.for_order("BUY", Decimal("90000"))

    async def test_realised_pnl_and_charges_reach_cash(self) -> None:
        client, tape, _, adapter = _paper(slippage_bps=Decimal("0"))
        tape.set("INFY", "100.00")
        await adapter.place_order(_request(quantity=10))
        await adapter.fetch_orderbook()
        tape.set("INFY", "110.00")
        await adapter.place_order(_request(Side.SELL, quantity=10, key="f" * 64))
        await adapter.fetch_orderbook()
        margins = await adapter.fetch_margins()
        assert margins.available_cash == Decimal("500000") + Decimal("100") - client.charges_total


class TestRefusals:
    async def test_a_cancelled_order_cannot_fill(self) -> None:
        _client, tape, _, adapter = _paper()
        tape.set("INFY", "1200.00")
        order_id = await adapter.place_order(
            _request(Side.SELL, order_type=OrderType.SLM, trigger="1186.45")
        )
        await adapter.cancel_order(order_id)
        tape.set("INFY", "1100.00")
        assert (await _only(adapter)).status is OrderStatus.CANCELLED

    async def test_cancelling_a_completed_order_is_rejected_as_kite_rejects_it(self) -> None:
        _client, tape, _, adapter = _paper()
        tape.set("INFY", "1200.00")
        order_id = await adapter.place_order(_request())
        await adapter.fetch_orderbook()
        with pytest.raises(OrderRejectedError):
            await adapter.cancel_order(order_id)

    def test_an_unprotected_market_order_is_refused_by_the_client_too(self) -> None:
        """The adapter refuses first; the client is the second gate, as Kite is."""
        client, *_ = _paper()
        with pytest.raises(kx.InputException, match="market protection"):
            client.place_order("regular", "NSE", "INFY", "BUY", 1, "MIS", "MARKET", tag="abc")

    @pytest.mark.parametrize("order_type", ["LIMIT", "SL"])
    def test_an_order_type_it_does_not_model_is_refused_not_mis_filled(
        self, order_type: str
    ) -> None:
        client, *_ = _paper()
        with pytest.raises(kx.InputException, match="does not model"):
            client.place_order(
                "regular",
                "NSE",
                "INFY",
                "BUY",
                1,
                "MIS",
                order_type,
                price=1200.0,
                tag="abc",
                market_protection=-1,
            )

    @pytest.mark.parametrize(
        ("field", "value"),
        [("product", "CNC"), ("exchange", "BSE"), ("tag", "x" * 21), ("tag", "a b")],
    )
    def test_what_the_simulation_does_not_cover_is_refused(self, field: str, value: str) -> None:
        client, *_ = _paper()
        args: dict[str, Any] = {
            "variety": "regular",
            "exchange": "NSE",
            "tradingsymbol": "INFY",
            "transaction_type": "BUY",
            "quantity": 1,
            "product": "MIS",
            "order_type": "MARKET",
            "tag": "abc",
            "market_protection": -1,
        }
        args[field] = value
        with pytest.raises(kx.InputException):
            client.place_order(**args)


class TestPersistence:
    async def test_a_snapshot_restores_the_same_account(self) -> None:
        client, tape, clock, adapter = _paper()
        tape.set("INFY", "1200.00", ask_qty=20)
        await adapter.place_order(_request(quantity=50))
        await adapter.place_order(
            _request(Side.SELL, order_type=OrderType.SLM, trigger="1186.45", key="g" * 64)
        )
        await adapter.fetch_orderbook()
        restored = PaperKite(quotes=tape, clock=clock)
        restored.restore(client.snapshot())
        assert restored.orders() == client.orders()
        assert restored.positions() == client.positions()
        assert restored.charges_total == client.charges_total

    def test_every_change_is_offered_for_persistence(self) -> None:
        seen: list[dict[str, Any]] = []
        tape, clock = Tape({}), Clock()
        client = PaperKite(quotes=tape, clock=clock, on_change=seen.append)
        client.place_order(
            "regular", "NSE", "INFY", "BUY", 1, "MIS", "MARKET", tag="abc", market_protection=-1
        )
        assert len(seen) == 1 and seen[0]["orders"][0]["status"] == "OPEN"


class TestConcurrency:
    def test_orders_from_many_threads_get_distinct_ids(self) -> None:
        client, *_ = _paper()
        ids: list[str] = []

        def place(n: int) -> None:
            ids.append(
                client.place_order(
                    "regular",
                    "NSE",
                    "INFY",
                    "BUY",
                    1,
                    "MIS",
                    "MARKET",
                    tag=f"t{n}",
                    market_protection=-1,
                )
            )

        threads = [threading.Thread(target=place, args=(n,)) for n in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(set(ids)) == 40


class TestConfig:
    @pytest.mark.parametrize(
        "kw",
        [
            {"opening_cash": Decimal("0")},
            {"margin_rate": Decimal("0")},
            {"margin_rate": Decimal("1.5")},
            {"slippage_bps": Decimal("-1")},
            {"max_quote_age_seconds": 0},
        ],
    )
    def test_a_config_that_would_flatter_or_break_the_simulation_is_refused(self, kw) -> None:
        with pytest.raises(ValueError):
            PaperConfig(**kw)


class TestTheSyncQuoteReader:
    class Sync:
        def __init__(self, values: dict[str, str]) -> None:
            self.values = values

        def get(self, key: str) -> str | None:
            return self.values.get(key)

    def test_it_reads_the_published_quote(self) -> None:
        quote = QuoteState(symbol="INFY", ltp=Decimal("1200.5"), as_of=NOW)
        read = quote_reader(self.Sync({"state:quote:INFY": quote.model_dump_json()}))
        assert read("INFY") == quote

    def test_absent_and_unparseable_are_no_quote(self) -> None:
        read = quote_reader(self.Sync({"state:quote:TCS": '{"ltp": "x"}'}))
        assert read("INFY") is None and read("TCS") is None
