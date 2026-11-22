"""E15-S15 task 2: the day's statistics, and that they ARE the loss limit's.

Every expected figure is derived by hand from the published rates in
``execution/charges.py`` (as ``test_day_ledger.py`` does) - never copied from
the implementation:

* BUY 100 @ 1000 = 30.35 (that file's first test).
* SELL 100 @ 1010 = 52.63 (its second).
* SELL 100 @ 990: turnover 99,000; brokerage 20; transaction 3.0393;
  SEBI 0.099; STT 24.75; GST 18% x 23.1383 = 4.164894; total 52.053194
  -> 52.06.
* SELL 100 @ 1000: turnover 1,00,000; 20 + 3.07 + 0.10 + 25 +
  18% x 23.17 = 4.1706; total 52.3406 -> 52.35.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from algotrader.execution.day_ledger import day_ledger, round_trips
from algotrader.execution.day_stats import (
    DayStatistics,
    TradeOutcome,
    TradeResult,
    day_statistics,
)

DAY = dt.date(2026, 8, 25)
T0 = dt.datetime(2026, 8, 25, 4, 0, tzinfo=dt.UTC)


def _order(
    cid: UUID,
    side: str,
    price: str | None,
    quantity: int = 100,
    *,
    symbol: str = "INFY",
    at: dt.datetime = T0,
    filled: int | None = None,
) -> dict[str, Any]:
    return {
        "correlation_id": cid,
        "symbol": symbol,
        "side": side,
        "filled_quantity": quantity if filled is None else filled,
        "average_price": None if price is None else Decimal(price),
        "last_update_at": at,
    }


def _trip(
    entry: str, exit_: str, *, symbol: str = "INFY", at: dt.datetime = T0
) -> tuple[UUID, list[dict[str, Any]]]:
    cid = uuid4()
    return cid, [
        _order(cid, "BUY", entry, symbol=symbol, at=at),
        _order(cid, "SELL", exit_, symbol=symbol, at=at + dt.timedelta(minutes=30)),
    ]


def _row(
    cid: UUID, position_id: int, *, entry: str = "1000", stop: str = "990", quantity: int = 100
) -> dict[str, Any]:
    return {
        "position_id": position_id,
        "correlation_id": cid,
        "symbol": "INFY",
        "quantity": quantity,
        "entry_price": Decimal(entry),
        "stop_price": Decimal(stop),
        "exit_reason": "TIME",
    }


def _no_price(_symbol: str) -> Decimal | None:
    return None


def _stats(orders: list[dict[str, Any]], rows: list[dict[str, Any]], price=_no_price):
    stats = day_statistics(trade_date=DAY, orders=orders, closed_positions=rows, last_price=price)
    assert stats is not None
    return stats


class TestEachTrade:
    def test_a_winner_is_its_gross_less_both_orders_charges(self) -> None:
        cid, orders = _trip("1000", "1010")
        (trade,) = _stats(orders, [_row(cid, 1)]).trades
        assert trade.gross == Decimal("1000")
        assert trade.charges == Decimal("30.35") + Decimal("52.63")
        assert trade.net == Decimal("917.02")
        assert trade.outcome is TradeOutcome.WIN

    def test_a_loser(self) -> None:
        cid, orders = _trip("1000", "990")
        (trade,) = _stats(orders, [_row(cid, 1)]).trades
        assert trade.net == Decimal("-1000") - Decimal("30.35") - Decimal("52.06")
        assert trade.outcome is TradeOutcome.LOSS

    def test_out_at_the_entry_price_is_a_loss_not_a_scratch(self) -> None:
        """The decision in the design record: the losing streak counts this as
        a loss (net -82.70), so the report does too; its gross shows why."""
        cid, orders = _trip("1000", "1000")
        (trade,) = _stats(orders, [_row(cid, 1)]).trades
        assert trade.gross == 0
        assert trade.net == Decimal("-82.70")
        assert trade.outcome is TradeOutcome.LOSS

    def test_a_small_gross_profit_eaten_by_charges_is_a_loss(self) -> None:
        """BUY 100 @ 1000 (30.35), SELL 100 @ 1000.50: turnover 1,00,050;
        brokerage 20; transaction 3.0715...; SEBI 0.10005; STT 25.0125;
        GST 4.17...; about 52.36. Gross +50, net about -32.7: the account is
        poorer, so it is a LOSS - as the losing streak counts it."""
        cid, orders = _trip("1000", "1000.50")
        (trade,) = _stats(orders, [_row(cid, 1)]).trades
        assert trade.gross == Decimal("50") and trade.net < 0
        assert trade.outcome is TradeOutcome.LOSS

    def test_a_scratch_is_a_net_of_exactly_zero(self) -> None:
        trade = TradeResult(
            correlation_id="c",
            symbol="INFY",
            quantity=1,
            gross=Decimal("82.70"),
            charges=Decimal("82.70"),
            position_id=None,
            exit_reason=None,
            r_multiple=None,
        )
        assert trade.outcome is TradeOutcome.SCRATCH

    def test_r_is_net_over_the_risk_the_position_opened_with(self) -> None:
        """Risk = |1000 - 990| x 100 = 1000. 917.02 / 1000 = 0.917 -> 0.92."""
        cid, orders = _trip("1000", "1010")
        (trade,) = _stats(orders, [_row(cid, 1)]).trades
        assert trade.r_multiple == Decimal("0.92")

    def test_a_losers_r_is_negative(self) -> None:
        """-1082.41 / 1000 -> -1.08: a full stop-out plus its costs."""
        cid, orders = _trip("1000", "990")
        (trade,) = _stats(orders, [_row(cid, 1)]).trades
        assert trade.r_multiple == Decimal("-1.08")

    def test_a_short_has_the_same_risk_arithmetic(self) -> None:
        """Short at 1010, stop 1020, covered at 1000: risk 10 x 100 = 1000;
        gross +1000; charges SELL 100 @ 1010 (52.63) + BUY 100 @ 1000 (30.35)."""
        cid = uuid4()
        orders = [
            _order(cid, "SELL", "1010"),
            _order(cid, "BUY", "1000", at=T0 + dt.timedelta(minutes=5)),
        ]
        (trade,) = _stats(orders, [_row(cid, 1, entry="1010", stop="1020")]).trades
        assert trade.net == Decimal("917.02")
        assert trade.r_multiple == Decimal("0.92")

    def test_no_row_no_r(self) -> None:
        _cid, orders = _trip("1000", "1010")
        (trade,) = _stats(orders, []).trades
        assert trade.position_id is None and trade.r_multiple is None

    def test_a_row_with_no_risk_has_no_r_rather_than_a_division(self) -> None:
        cid, orders = _trip("1000", "1010")
        (trade,) = _stats(orders, [_row(cid, 1, stop="1000")]).trades
        assert trade.r_multiple is None


class TestTheDay:
    def test_counts(self) -> None:
        win, w = _trip("1000", "1010", at=T0)
        loss, lo = _trip("1000", "990", at=T0 + dt.timedelta(hours=1))
        even, e = _trip("1000", "1000", at=T0 + dt.timedelta(hours=2))
        stats = _stats(w + lo + e, [_row(win, 1), _row(loss, 2), _row(even, 3)])
        assert (len(stats.trades), stats.wins, stats.losses, stats.scratches) == (3, 1, 2, 0)
        assert stats.booked == 3 and stats.reconciles

    def test_trades_are_in_the_order_they_finished(self) -> None:
        _late, a = _trip("1000", "1010", symbol="TCS", at=T0 + dt.timedelta(hours=2))
        _early, b = _trip("1000", "990", symbol="SBIN", at=T0)
        stats = _stats(a + b, [])
        assert [t.symbol for t in stats.trades] == ["SBIN", "TCS"]

    def test_a_fill_never_booked_is_still_a_trade_and_is_named(self) -> None:
        """RSK-004's hole, in the report: closed positions alone would miss it."""
        booked, a = _trip("1000", "1010")
        _unbooked, b = _trip("1000", "990", symbol="TCS")
        stats = _stats(a + b, [_row(booked, 1)])
        assert len(stats.trades) == 2 and stats.booked == 1
        assert [t.symbol for t in stats.unbooked] == ["TCS"]
        assert stats.reconciles, "an unbooked trade is counted, not a disagreement"

    def test_a_close_no_order_explains_is_a_discrepancy(self) -> None:
        cid, orders = _trip("1000", "1010")
        stats = _stats(orders, [_row(cid, 1), _row(uuid4(), 9)])
        assert stats.unexplained_closes == (9,)
        assert not stats.reconciles

    def test_a_closed_row_whose_orders_are_still_open_is_a_discrepancy(self) -> None:
        cid = uuid4()
        stats = _stats([_order(cid, "BUY", "1000")], [_row(cid, 4)])
        assert stats.trades == () and stats.still_open == ("INFY",)
        assert stats.unexplained_closes == (4,)

    def test_an_open_trip_is_not_a_trade_and_leaves_the_net_unknown_without_a_price(
        self,
    ) -> None:
        _cid, closed = _trip("1000", "1010")
        stats = _stats([*closed, _order(uuid4(), "BUY", "500", symbol="TCS")], [])
        assert stats.still_open == ("TCS",) and len(stats.trades) == 1
        assert stats.net is None and stats.gross is None
        assert not stats.reconciles

    def test_a_fill_with_no_price_means_no_statistics(self) -> None:
        cid = uuid4()
        assert (
            day_statistics(
                trade_date=DAY,
                orders=[_order(cid, "BUY", None)],
                closed_positions=[],
                last_price=_no_price,
            )
            is None
        )

    def test_an_order_with_nothing_filled_is_not_a_trade(self) -> None:
        cid = uuid4()
        stats = _stats([_order(cid, "BUY", None, filled=0)], [])
        assert stats.trades == () and stats.still_open == ()


class TestAc2TheNetIsTheLossLimitsNet:
    def test_equal_to_day_ledger_for_the_same_orders(self) -> None:
        _a, w = _trip("1000", "1010")
        _b, lo = _trip("1000", "990", symbol="TCS")
        orders = w + lo
        stats = _stats(orders, [])
        ledger = day_ledger(orders, _no_price)
        assert stats.net == ledger.net == Decimal("917.02") + Decimal("-1082.41")
        assert stats.ledger == ledger

    def test_with_something_open_and_priced_still_equal(self) -> None:
        _a, w = _trip("1000", "1010")
        orders = [*w, _order(uuid4(), "BUY", "500", symbol="TCS")]

        def price(symbol: str) -> Decimal | None:
            return Decimal("510") if symbol == "TCS" else None

        stats = _stats(orders, [], price)
        assert stats.net is not None and stats.net == day_ledger(orders, price).net

    def test_the_trades_sum_to_the_day_when_flat(self) -> None:
        _a, w = _trip("1000", "1010")
        _b, lo = _trip("1000", "990")
        stats = _stats(w + lo, [])
        assert sum(t.net for t in stats.trades) == stats.net

    @settings(max_examples=200, deadline=None)
    @given(
        st.lists(
            st.tuples(
                st.integers(1, 500),
                st.decimals("10", "5000", places=2),
                st.decimals("10", "5000", places=2),
                st.booleans(),
            ),
            max_size=12,
        )
    )
    def test_for_any_flat_day_the_trades_sum_to_the_loss_limits_net(
        self, trips: list[tuple[int, Decimal, Decimal, bool]]
    ) -> None:
        orders: list[dict[str, Any]] = []
        for i, (qty, entry, exit_, short) in enumerate(trips):
            cid = uuid4()
            first, second = ("SELL", "BUY") if short else ("BUY", "SELL")
            at = T0 + dt.timedelta(minutes=i)
            orders.append(_order(cid, first, str(entry), qty, at=at))
            orders.append(_order(cid, second, str(exit_), qty, at=at + dt.timedelta(seconds=30)))
        stats = _stats(orders, [])
        ledger = day_ledger(orders, _no_price)
        assert stats.net == ledger.net
        assert sum((t.net for t in stats.trades), Decimal(0)) == ledger.net
        assert len(stats.trades) == len(trips)
        assert stats.wins + stats.losses + stats.scratches == len(trips)


class TestRoundTripsIsTheOneDefinition:
    def test_day_ledger_reads_round_trips(self) -> None:
        """The refactor's own claim: the ledger's realised figure is the sum of
        the finished trips' cash, from the same function the report uses."""
        _a, w = _trip("1000", "1010")
        _b, lo = _trip("1000", "990")
        trips = round_trips(w + lo)
        assert trips is not None
        assert day_ledger(w + lo, _no_price).realised == sum(t.cash for t in trips)

    def test_unknown_is_none_not_empty(self) -> None:
        assert round_trips([_order(uuid4(), "BUY", None)]) is None
        assert round_trips([]) == ()


def test_statistics_are_a_frozen_record() -> None:
    _cid, orders = _trip("1000", "1010")
    stats = _stats(orders, [])
    assert isinstance(stats, DayStatistics)
    with pytest.raises(AttributeError):
        stats.trades = ()  # type: ignore[misc]
