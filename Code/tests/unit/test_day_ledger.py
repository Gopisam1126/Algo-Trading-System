"""The day's money: charges, the ledger, and the latch that acts on it.

Audit 10 Nov 2026: RSK-003 (nothing armed the loss latches), RSK-004 (unbooked
round trips were outside the limit), RSK-005 (open losses and charges were
outside the limit). Every expected figure below is derived by hand from the
published rates in ``execution/charges.py`` (Zerodha, read 16 Nov 2026), and
the arithmetic is in each docstring - never copied from the implementation.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from algotrader.common.enums import Side
from algotrader.execution.charges import SCHEDULE, order_charges
from algotrader.execution.day_ledger import UNKNOWN, day_ledger
from algotrader.execution.halt import HaltReason
from algotrader.execution.loss_latch import ARMED_BY, LossLatch

NOW = dt.datetime(2026, 8, 25, 6, 30, tzinfo=dt.UTC)


class TestOrderCharges:
    def test_a_buy_above_the_brokerage_cap(self) -> None:
        """BUY 100 @ 1000 = turnover 1,00,000.
        brokerage min(0.03% = 30, 20) = 20; transaction 0.00307% = 3.07;
        SEBI 10/crore = 0.10; stamp 0.003% = 3.00; STT 0 (buy);
        GST 18% x (20 + 0.10 + 3.07) = 4.1706.
        Total 30.3406 -> 30.35 (rounded UP to the paisa)."""
        assert order_charges(Side.BUY, Decimal("1000"), 100) == Decimal("30.35")

    def test_a_sell_carries_stt_and_no_stamp(self) -> None:
        """SELL 100 @ 1010 = 1,01,000.
        brokerage 20; transaction 3.1007; SEBI 0.101; STT 0.025% = 25.25;
        GST 18% x (20 + 0.101 + 3.1007) = 4.176306.
        Total 52.628006 -> 52.63."""
        assert order_charges(Side.SELL, Decimal("1010"), 100) == Decimal("52.63")

    def test_a_small_order_pays_percentage_brokerage(self) -> None:
        """BUY 10 @ 100 = 1,000. brokerage 0.03% = 0.30 (under the cap);
        transaction 0.0307; SEBI 0.001; stamp 0.03;
        GST 18% x (0.30 + 0.001 + 0.0307) = 0.059706. Total 0.421406 -> 0.43."""
        assert order_charges(Side.BUY, Decimal("100"), 10) == Decimal("0.43")

    def test_bse_has_its_own_transaction_rate(self) -> None:
        assert order_charges(Side.BUY, Decimal("1000"), 100, exchange="BSE") > order_charges(
            Side.BUY, Decimal("1000"), 100, exchange="NSE"
        )

    def test_an_unknown_exchange_is_refused_not_priced_at_zero(self) -> None:
        with pytest.raises(ValueError, match="exchange"):
            order_charges(Side.BUY, Decimal("1000"), 100, exchange="MCX")

    def test_no_quantity_costs_nothing(self) -> None:
        assert order_charges(Side.SELL, Decimal("1000"), 0) == 0

    def test_the_schedule_matches_the_published_page(self) -> None:
        """The six numbers as the page states them, so an edit to one is a
        deliberate act that fails here first."""
        assert SCHEDULE.brokerage_rate == Decimal("0.0003")
        assert SCHEDULE.brokerage_cap == Decimal("20")
        assert SCHEDULE.stt_sell_rate == Decimal("0.00025")
        assert SCHEDULE.transaction_rate["NSE"] == Decimal("0.0000307")
        assert SCHEDULE.sebi_rate == Decimal("0.000001")
        assert SCHEDULE.stamp_buy_rate == Decimal("0.00003")
        assert SCHEDULE.gst_rate == Decimal("0.18")


def _order(
    corr: Any, side: str, qty: int, price: str | None, *, minute: int = 0, symbol: str = "INFY"
) -> dict[str, Any]:
    return {
        "correlation_id": corr,
        "symbol": symbol,
        "side": side,
        "filled_quantity": qty,
        "average_price": None if price is None else Decimal(price),
        "last_update_at": NOW + dt.timedelta(minutes=minute),
    }


class TestTheLedger:
    def test_a_closed_and_an_open_trip(self) -> None:
        """A: BUY 100 @1000 then SELL 100 @1010 -> gross +1,000, charges
        30.35 + 52.63 = 82.98. B: BUY 50 @2000 still open, live 1,980 ->
        -1,00,000 + 50 x 1,980 = -1,000; charges on the buy (turnover
        1,00,000, as above) 30.35 and on the closing sell 50 @1980 (99,000:
        20 + 3.0393 + 0.099 + 24.75 + GST 4.164894 = 52.053194 -> 52.06).
        Charges 82.98 + 30.35 + 52.06 = 165.39; net 1,000 - 1,000 - 165.39."""
        a, b = uuid4(), uuid4()
        ledger = day_ledger(
            [
                _order(a, "BUY", 100, "1000"),
                _order(a, "SELL", 100, "1010", minute=5),
                _order(b, "BUY", 50, "2000", symbol="TCS"),
            ],
            {"TCS": Decimal("1980")}.get,
        )
        assert ledger.realised == Decimal("1000")
        assert ledger.unrealised == Decimal("-1000")
        assert ledger.charges == Decimal("165.39")
        assert ledger.net == Decimal("-165.39")
        assert ledger.streak == 0

    def test_an_unbooked_round_trip_counts(self) -> None:
        """RSK-004: an entry that filled through its stop, and the emergency
        exit that closed it, have no positions row. They are two orders, so
        the ledger counts them: 100 @1000 then 100 @998 -> -200 gross."""
        corr = uuid4()
        ledger = day_ledger(
            [_order(corr, "BUY", 100, "1000"), _order(corr, "SELL", 100, "998", minute=1)],
            lambda _: None,
        )
        assert ledger.realised == Decimal("-200")
        assert ledger.net is not None and ledger.net < Decimal("-200")
        assert ledger.streak == 1

    def test_the_streak_is_net_of_charges_and_ends_on_a_winner(self) -> None:
        trips = []
        for minute, (buy, sell) in enumerate([("1000", "1005"), ("1000", "990"), ("1000", "995")]):
            corr = uuid4()
            trips += [
                _order(corr, "BUY", 100, buy, minute=minute * 10),
                _order(corr, "SELL", 100, sell, minute=minute * 10 + 1),
            ]
        # +500 gross, then two losers: the winner is oldest, so the streak is 2.
        assert day_ledger(trips, lambda _: None).streak == 2

    def test_a_gross_winner_smaller_than_its_charges_is_a_loss(self) -> None:
        """+10 gross on 1,00,000 each way costs ~83 in charges: a net loser."""
        corr = uuid4()
        ledger = day_ledger(
            [_order(corr, "BUY", 100, "1000"), _order(corr, "SELL", 100, "1000.10", minute=1)],
            lambda _: None,
        )
        assert ledger.realised == Decimal("10.00")
        assert ledger.streak == 1

    def test_a_short_round_trip(self) -> None:
        corr = uuid4()
        ledger = day_ledger(
            [_order(corr, "SELL", 10, "500"), _order(corr, "BUY", 10, "490", minute=1)],
            lambda _: None,
        )
        assert ledger.realised == Decimal("100")

    def test_a_fill_with_no_price_makes_the_day_unknown(self) -> None:
        corr = uuid4()
        assert day_ledger([_order(corr, "BUY", 10, None)], lambda _: Decimal("1")) == UNKNOWN

    def test_an_open_trip_with_no_live_price_makes_the_day_unknown(self) -> None:
        ledger = day_ledger([_order(uuid4(), "BUY", 10, "100")], lambda _: None)
        assert ledger.net is None

    def test_unfilled_orders_are_ignored(self) -> None:
        corr = uuid4()
        ledger = day_ledger([_order(corr, "BUY", 0, None)], lambda _: None)
        assert ledger.net == 0 and ledger.streak == 0

    def test_a_partly_exited_trip_is_marked_whole(self) -> None:
        """BUY 100 @10, SELL 40 @11, open 60 at live 12: true P&L is
        40 x 1 + 60 x 2 = 160, all of it in `unrealised` because the trip is
        not flat."""
        corr = uuid4()
        ledger = day_ledger(
            [_order(corr, "BUY", 100, "10"), _order(corr, "SELL", 40, "11", minute=1)],
            {"INFY": Decimal("12")}.get,
        )
        assert ledger.realised == 0
        assert ledger.unrealised == Decimal("160")


class Halts:
    def __init__(self) -> None:
        self.armed: list[tuple[HaltReason, str]] = []

    async def arm(self, reason: HaltReason, *, detail: str, armed_by: str, now: dt.datetime):
        assert armed_by == ARMED_BY
        self.armed.append((reason, detail))


def _latch(halts: Halts) -> LossLatch:
    return LossLatch(
        capital=Decimal("500000"),
        max_daily_loss_pct=Decimal("3"),
        consecutive_loss_halt=3,
        halts=halts,
    )


def _ledger(net: str, *, streak: int = 0):
    from algotrader.execution.day_ledger import DayLedger

    return DayLedger(
        realised=Decimal(net), unrealised=Decimal(0), charges=Decimal(0), streak=streak
    )


class TestTheLossLatch:
    """RSK-003: until 16 Nov 2026 nothing armed either latch."""

    async def test_a_breach_arms_the_daily_latch(self) -> None:
        halts = Halts()
        armed = await _latch(halts).observe(_ledger("-15000"), now=NOW)
        assert armed == (HaltReason.DAILY_LOSS_LIMIT,)
        assert "15000" in halts.armed[0][1].replace(",", "")

    async def test_one_paisa_short_of_the_limit_arms_nothing(self) -> None:
        halts = Halts()
        assert await _latch(halts).observe(_ledger("-14999.99"), now=NOW) == ()

    async def test_the_streak_arms_its_own_latch(self) -> None:
        halts = Halts()
        armed = await _latch(halts).observe(_ledger("-10", streak=3), now=NOW)
        assert armed == (HaltReason.CONSECUTIVE_LOSS_LIMIT,)

    async def test_both_can_breach_together(self) -> None:
        halts = Halts()
        armed = await _latch(halts).observe(_ledger("-20000", streak=4), now=NOW)
        assert set(armed) == {HaltReason.DAILY_LOSS_LIMIT, HaltReason.CONSECUTIVE_LOSS_LIMIT}

    async def test_an_unknown_day_arms_nothing(self) -> None:
        """The per-signal check refuses on unknown; the latch records only a
        breach it can SEE, so its reason is never a guess."""
        halts = Halts()
        assert await _latch(halts).observe(UNKNOWN, now=NOW) == ()

    async def test_a_profitable_day_arms_nothing(self) -> None:
        halts = Halts()
        assert await _latch(halts).observe(_ledger("15000"), now=NOW) == ()
