"""Reconciliation against the real adapter, database, audit chain and halt latch.

`tests/unit/test_reconciliation.py` proves the decisions. It proves none of the
places where the decisions meet something real, and each of those has a way of
failing that no double can show:

* **The adapter's mapping of Kite's own payload.** Kite keeps positions closed
  during the day in the list at zero, reports products this system never
  trades, and signs quantity for shorts. A double hands the reconciler whatever
  shape the test author imagined.
* **Tag matching.** Our key is 32 characters; Kite stores 20. A reconciler that
  compared full keys would find none of our orders - and every one of our fills
  would then look foreign, and halt the day.
* **The audit columns.** ``decision_log.outcome`` is String(12). Only a real
  insert proves the labels fit.
* **The halt latch.** A halt that arms a double proves a method was called; this
  proves the kill switch is set in Redis where the risk engine reads it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from algotrader.broker.kite.trading import KiteTradingAdapter
from algotrader.common.audit import AuditWriter
from algotrader.common.db import engine as db_engine
from algotrader.common.db.repositories import (
    InstrumentRepository,
    OrderRepository,
    PositionRepository,
)
from algotrader.common.enums import Direction, OrderIntent, OrderType, Product, Side
from algotrader.common.models.trading import OrderRequest, Position
from algotrader.execution.gateway import GatewayPolicy, OrderGateway, client_order_id
from algotrader.execution.halt import HaltController, HaltReason
from algotrader.execution.positions import (
    PositionManager,
    ProtectedPosition,
    RedisMirror,
    UnverifiedPosition,
)
from algotrader.execution.protective_stop import ProtectiveStop
from algotrader.execution.reconciliation import Drift, Reconciler, ReconciliationReadError

pytestmark = [pytest.mark.integration]

TRADE_DATE = dt.date(2026, 8, 25)
NOW = dt.datetime(2026, 8, 25, 5, 0, tzinfo=dt.UTC)  # 10:30 IST
TICKS = {"INFY": Decimal("0.05"), "TCS": Decimal("0.05")}


@pytest.fixture
async def engine(migrated_database: str) -> AsyncIterator[object]:
    eng = db_engine.create_engine_from_url(migrated_database)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: object) -> AsyncIterator[AsyncSession]:
    factory = db_engine.create_session_factory(engine)  # type: ignore[arg-type]
    async with factory() as s:
        await s.execute(text("DELETE FROM decision_log"))
        await s.commit()
        yield s
        await s.rollback()


@pytest.fixture
def audit(engine: object, tmp_path: Path) -> AuditWriter:
    return AuditWriter(
        db_engine.create_session_factory(engine),  # type: ignore[arg-type]
        buffer_dir=tmp_path / "audit_buffer",
    )


@pytest.fixture
async def instruments(session: AsyncSession) -> InstrumentRepository:
    repo = InstrumentRepository(session)
    await repo.upsert(
        [
            {
                "tradingsymbol": symbol,
                "exchange": "NSE",
                "broker_token": f"rec-tok-{i}",
                "tick_size": tick,
                "lot_size": 1,
            }
            for i, (symbol, tick) in enumerate(TICKS.items())
        ]
    )
    await session.flush()
    await repo.refresh_cache()
    return repo


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[aioredis.Redis]:
    client = aioredis.from_url(redis_url, decode_responses=True)
    await client.flushall()
    yield client
    await client.aclose()


class KiteAccount:
    """Kite's own payload shapes: an orderbook and a positions response.

    ``net`` rows carry the fields Kite documents - including rows for positions
    closed during the day at quantity zero, and products this system never
    trades - because the adapter's mapping is part of what is under test.
    """

    def __init__(self) -> None:
        self.book: list[dict[str, Any]] = []
        self.net: list[dict[str, Any]] = []
        self.placed: list[dict[str, Any]] = []

    def place_order(self, **params: Any) -> str:
        self.placed.append(params)
        order_id = f"26082500{len(self.placed):04d}"
        self.book.append(
            {
                "order_id": order_id,
                "tag": params.get("tag"),
                "status": "OPEN" if params["order_type"] == "MARKET" else "TRIGGER PENDING",
                "tradingsymbol": params["tradingsymbol"],
                "exchange": params.get("exchange", "NSE"),
                "transaction_type": params["transaction_type"],
                "order_type": params["order_type"],
                "product": params["product"],
                "quantity": params["quantity"],
                "filled_quantity": 0,
                "order_timestamp": "2026-08-25 10:00:00",
            }
        )
        return order_id

    def orders(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.book]

    def positions(self) -> dict[str, list[dict[str, Any]]]:
        return {"net": [dict(r) for r in self.net], "day": []}

    def fill(self, tag: str, quantity: int, price: str) -> None:
        for row in self.book:
            if row["tag"] == tag:
                row.update(status="COMPLETE", filled_quantity=quantity, average_price=price)
                return
        raise AssertionError(f"no order tagged {tag}")

    def hold(
        self,
        symbol: str,
        *,
        quantity: int,
        bought: int = 0,
        sold: int = 0,
        product: str = "MIS",
        exchange: str = "NSE",
    ) -> None:
        self.net.append(
            {
                "tradingsymbol": symbol,
                "exchange": exchange,
                "product": product,
                "quantity": quantity,
                "day_buy_quantity": bought,
                "day_sell_quantity": sold,
                "average_price": 1200.0,
                "pnl": 0,
            }
        )


class Wiring:
    """The real stack, over one Kite account."""

    def __init__(self, session, instruments, redis, audit, account: KiteAccount) -> None:
        self.account = account
        self.adapter = KiteTradingAdapter(
            auth=None, client=account, algo_id="", tick_size_for=instruments.tick_size
        )
        self.orders = OrderRepository(session, instruments)
        self.gateway = OrderGateway(
            self.adapter,
            policy=GatewayPolicy(algo_id=None, market_protection=Decimal("-1")),
            store=self.orders,
        )
        self.halter = HaltController(redis)
        self.protector = ProtectiveStop(self.gateway, halter=self.halter)
        self.positions = PositionRepository(session, instruments)
        self.manager = PositionManager(
            protector=self.protector,
            store=self.positions,
            mirror=RedisMirror(redis),
            calendar=None,
        )
        self.reconciler = Reconciler(
            broker=self.adapter,
            ledger=self.orders,
            manager=self.manager,
            protector=self.protector,
            halter=self.halter,
            audit=audit.write,
        )

    async def enter(self, correlation: uuid.UUID, symbol: str = "INFY", quantity: int = 40) -> str:
        """Place an entry through the real gateway. Returns its tag."""
        cid = client_order_id(
            correlation_id=correlation,
            symbol=symbol,
            side=Side.BUY,
            intent=OrderIntent.ENTRY,
            trade_date=TRADE_DATE,
        )
        await self.gateway.submit(
            OrderRequest(
                client_order_id=cid,
                correlation_id=correlation,
                symbol=symbol,
                side=Side.BUY,
                order_type=OrderType.MARKET,
                product=Product.MIS,
                quantity=quantity,
                intent=OrderIntent.ENTRY,
                algo_id=None,
                market_protection=Decimal("-1"),
            )
        )
        return self.adapter.order_key(cid)

    async def run(self):
        return await self.reconciler.run_cycle(now=NOW)


@pytest.fixture
def wiring(session, instruments, redis, audit) -> Wiring:
    return Wiring(session, instruments, redis, audit, KiteAccount())


async def _drift_rows(session: AsyncSession) -> list[tuple[str, str]]:
    await session.commit()  # the audit writer commits in its own session
    rows = (
        await session.execute(text("SELECT stage, outcome FROM decision_log ORDER BY seq"))
    ).all()
    return [(r.stage, r.outcome) for r in rows]


def _position(correlation: uuid.UUID, symbol: str = "INFY") -> Position:
    return Position(
        correlation_id=correlation,
        symbol=symbol,
        slot_index=0,
        direction=Direction.LONG,
        quantity=40,
        entry_price=Decimal("1200.0000"),
        stop_price=Decimal("1186.4500"),
        opened_at=NOW,
        squareoff_deadline=dt.datetime(2026, 8, 25, 9, 45, tzinfo=dt.UTC),
    )


class TestAnUnknownPositionHaltsForReal:
    async def test_the_kill_switch_is_set_in_redis_and_the_drift_is_in_the_table(
        self, wiring: Wiring, session: AsyncSession
    ) -> None:
        """The acceptance criterion end to end: Kite's payload in, a latch the
        risk engine reads set, and an audit row that fits its columns."""
        wiring.account.hold("WIPRO", quantity=25, bought=25)
        report = await wiring.run()

        state = await wiring.halter.state()
        assert state.kill_switch
        assert state.records["kill_switch"].reason is HaltReason.UNKNOWN_POSITION
        assert report.unknown[0].symbol == "WIPRO"
        assert ("RECONCILIATION_DRIFT", "UNKNOWN_POS") in await _drift_rows(session)

    @pytest.mark.parametrize("product", ["CNC", "NRML", "MTF"])
    async def test_products_this_system_never_trades_pass_through_the_adapter(
        self, wiring: Wiring, product: str
    ) -> None:
        """MTF is a real Zerodha product. Mapping ``product`` through a closed
        enum would make one margin-funded holding fail every read - which is,
        after three cycles, a halted day."""
        wiring.account.hold("HDFCBANK", quantity=10, bought=10, product=product)
        report = await wiring.run()
        assert not report.halted
        assert not (await wiring.halter.state()).kill_switch

    async def test_our_own_fill_is_found_through_the_truncated_tag(self, wiring: Wiring) -> None:
        """Our key is 32 characters; Kite stores 20. Compare the full key and none
        of our orders are found, every fill looks foreign, and the day halts."""
        corr = uuid.uuid4()
        tag = await wiring.enter(corr)
        assert len(tag) == 20
        wiring.account.fill(tag, 40, "1201.50")
        wiring.account.hold("INFY", quantity=40, bought=40)
        report = await wiring.run()
        assert not report.halted
        assert report.unknown == []

    async def test_a_position_closed_during_the_day_is_not_a_position(self, wiring: Wiring) -> None:
        """Kite lists it at quantity zero, with the day's buys and sells."""
        wiring.account.hold("TCS", quantity=0, bought=0, sold=0)
        report = await wiring.run()
        assert not report.halted


class TestOurOrdersFollowTheBroker:
    async def test_a_fill_reaches_the_orders_table(
        self, wiring: Wiring, session: AsyncSession
    ) -> None:
        corr = uuid.uuid4()
        tag = await wiring.enter(corr)
        wiring.account.fill(tag, 40, "1201.50")
        wiring.account.hold("INFY", quantity=40, bought=40)
        await wiring.run()

        row = (
            await session.execute(
                text(
                    "SELECT status, filled_quantity, average_price FROM orders "
                    "WHERE correlation_id = :c AND intent = 'ENTRY'"
                ),
                {"c": corr},
            )
        ).one()
        assert row.status == "FILLED"
        assert row.filled_quantity == 40
        assert Decimal(str(row.average_price)) == Decimal("1201.50")

    async def test_a_submission_that_never_landed_becomes_submit_failed(
        self, wiring: Wiring, session: AsyncSession
    ) -> None:
        """SUBMIT_FAILED was modelled in E15-S03 and written by nothing until now."""
        corr = uuid.uuid4()
        await wiring.orders.insert_submitting(
            {
                "client_order_id": uuid.uuid4().hex,
                "correlation_id": corr,
                "symbol": "INFY",
                "side": "BUY",
                "order_type": "MARKET",
                "product": "MIS",
                "quantity": 40,
                "intent": "ENTRY",
                "placed_at": NOW - dt.timedelta(minutes=5),
                "last_update_at": NOW - dt.timedelta(minutes=5),
            }
        )
        report = await wiring.run()
        status = (
            await session.execute(
                text("SELECT status FROM orders WHERE correlation_id = :c"), {"c": corr}
            )
        ).scalar_one()
        assert status == "SUBMIT_FAILED"
        assert Drift.SUBMIT_FAIL in [d.kind for d in report.drifts]
        still_open = {r["correlation_id"] for r in await wiring.orders.open_orders()}
        assert corr in still_open, "SUBMIT_FAILED is not terminal; it must stay reconcilable"

    async def test_open_orders_now_asks_the_state_machine(self, wiring: Wiring) -> None:
        """The terminal set was three string literals; it is now derived."""
        corr = uuid.uuid4()
        tag = await wiring.enter(corr)
        wiring.account.fill(tag, 40, "1201.50")
        wiring.account.hold("INFY", quantity=40, bought=40)
        await wiring.run()
        # By intent, not correlation: the reconciler's own exit for this fill
        # shares the correlation_id and is still working, so it belongs in the
        # set. Only the FILLED entry must be gone from it.
        open_entries = [
            r
            for r in await wiring.orders.open_orders()
            if r["correlation_id"] == corr and r["intent"] == "ENTRY"
        ]
        assert open_entries == []
        assert any(
            r["correlation_id"] == corr and r["intent"] == "SQUAREOFF"
            for r in await wiring.orders.open_orders()
        ), "the control: the working exit is non-terminal and must still be reconciled"


class TestTheLedgerSurvivesAdoption:
    async def test_an_adopted_order_keeps_the_broker_id_it_was_found_under(
        self, wiring: Wiring, session: AsyncSession
    ) -> None:
        """Mutation M35 survived the first run: nothing adopted a SUBMITTING row
        against the real table, which is the only moment the broker id is
        learned. Lose it and the order can never be cancelled or found by id."""
        corr = uuid.uuid4()
        cid = uuid.uuid4().hex
        await wiring.orders.insert_submitting(
            {
                "client_order_id": cid,
                "correlation_id": corr,
                "symbol": "TCS",
                "side": "BUY",
                "order_type": "MARKET",
                "product": "MIS",
                "quantity": 10,
                "intent": "ENTRY",
                "placed_at": NOW - dt.timedelta(seconds=10),
                "last_update_at": NOW - dt.timedelta(seconds=10),
            }
        )
        wiring.account.book.append(
            {
                "order_id": "260825009999",
                "tag": wiring.adapter.order_key(cid),
                "status": "OPEN",
                "tradingsymbol": "TCS",
                "exchange": "NSE",
                "transaction_type": "BUY",
                "order_type": "MARKET",
                "product": "MIS",
                "quantity": 10,
                "filled_quantity": 0,
                "order_timestamp": "2026-08-25 10:29:50",
            }
        )
        report = await wiring.run()
        row = (
            await session.execute(
                text("SELECT status, broker_order_id FROM orders WHERE client_order_id = :c"),
                {"c": cid},
            )
        ).one()
        assert (row.status, row.broker_order_id) == ("OPEN", "260825009999")
        assert Drift.ADOPTED in [d.kind for d in report.drifts]

    async def test_yesterdays_orders_are_not_todays_working_set(
        self, wiring: Wiring, session: AsyncSession
    ) -> None:
        """Mutation M34 survived and was verified benign for CORRECTNESS: a past
        day's orders are not in today's Kite orderbook, so they explain nothing.
        What ``since`` protects is the size of the work - without it every cycle
        loads the table's entire history, every 30 seconds, forever - and it
        keeps a past day's leftovers with end-of-day reconciliation (E15-S11)."""
        await wiring.orders.insert_submitting(
            {
                "client_order_id": uuid.uuid4().hex,
                "correlation_id": uuid.uuid4(),
                "symbol": "INFY",
                "side": "BUY",
                "order_type": "MARKET",
                "product": "MIS",
                "quantity": 10,
                "intent": "ENTRY",
                "placed_at": NOW - dt.timedelta(days=1),
                "last_update_at": NOW - dt.timedelta(days=1),
            }
        )
        today = dt.datetime(2026, 8, 24, 18, 30, tzinfo=dt.UTC)  # 00:00 IST, 25 Aug
        assert await wiring.orders.orders_placed_since(today) == []
        report = await wiring.run()
        assert report.drifts == [], "a past day's order was reconciled as today's"


class TestEveryDriftFitsTheAuditTable:
    async def test_every_kind_written_in_one_cycle_is_readable(
        self, wiring: Wiring, session: AsyncSession
    ) -> None:
        """Several kinds at once: an unknown position, a submit failure and an
        unbooked fill. All must reach decision_log - a String(12) overflow would
        raise inside the writer and the row would silently go to the buffer."""
        corr = uuid.uuid4()
        tag = await wiring.enter(corr)
        wiring.account.fill(tag, 40, "1201.50")
        wiring.account.hold("INFY", quantity=40, bought=40)
        wiring.account.hold("WIPRO", quantity=25, bought=25)
        report = await wiring.run()

        written = await _drift_rows(session)
        assert len(written) == len(report.drifts) >= 2
        assert {stage for stage, _ in written} == {"RECONCILIATION_DRIFT"}
        assert {outcome for _, outcome in written} == {d.kind.value for d in report.drifts}


class TestProtectionIsAskedOfTheBroker:
    async def test_a_restored_position_with_its_stop_live_is_promoted(
        self, wiring: Wiring, session: AsyncSession
    ) -> None:
        """The restart E15-S05 left 'honest and not safe': the row comes back
        unverified, the real adapter finds the stop by its truncated tag, and the
        position becomes protected without a second stop being placed."""
        corr = uuid.uuid4()
        tag = await wiring.enter(corr)
        wiring.account.fill(tag, 40, "1200.00")
        wiring.account.hold("INFY", quantity=40, bought=40)
        position = _position(corr)
        await wiring.positions.open_position(
            {
                "correlation_id": corr,
                "symbol": "INFY",
                "slot_index": 0,
                "direction": "LONG",
                "quantity": 40,
                "entry_price": position.entry_price,
                "stop_price": position.stop_price,
                "opened_at": position.opened_at,
                "squareoff_deadline": position.squareoff_deadline,
            }
        )
        await wiring.gateway.submit_stop(position, trade_date=TRADE_DATE)
        stops_before = len([p for p in wiring.account.placed if p["order_type"] == "SL-M"])

        (restored,) = await wiring.manager.restore()
        assert isinstance(restored, UnverifiedPosition)
        report = await wiring.run()

        assert isinstance(wiring.manager.tracked("INFY"), ProtectedPosition)
        assert report.promoted == ["INFY"]
        stops_after = len([p for p in wiring.account.placed if p["order_type"] == "SL-M"])
        assert stops_after == stops_before, "adopting placed a second stop"
        assert not report.halted

    async def test_our_fill_with_no_book_entry_is_exited_at_the_broker(
        self, wiring: Wiring, session: AsyncSession
    ) -> None:
        """Until E15-S12 books fills, every fill is unprotected and leaves. The
        exit is a real MARKET order through the gateway, for the open quantity."""
        corr = uuid.uuid4()
        tag = await wiring.enter(corr)
        wiring.account.fill(tag, 40, "1201.50")
        wiring.account.hold("INFY", quantity=40, bought=40)
        report = await wiring.run()

        exits = [p for p in wiring.account.placed if p["transaction_type"] == "SELL"]
        assert [(p["order_type"], p["quantity"]) for p in exits] == [("MARKET", 40)]
        assert report.exited == ["INFY"]
        intent = (
            await session.execute(
                text("SELECT intent FROM orders WHERE correlation_id = :c AND intent <> 'ENTRY'"),
                {"c": corr},
            )
        ).scalar_one()
        assert intent == "SQUAREOFF"

    async def test_a_second_cycle_does_not_send_a_second_exit(self, wiring: Wiring) -> None:
        """The exit is live and unfilled; asking again must not place another."""
        corr = uuid.uuid4()
        tag = await wiring.enter(corr)
        wiring.account.fill(tag, 40, "1201.50")
        wiring.account.hold("INFY", quantity=40, bought=40)
        await wiring.run()
        await wiring.run()
        exits = [p for p in wiring.account.placed if p["transaction_type"] == "SELL"]
        assert len(exits) == 1


class TestAnUnreadablePayloadIsNotActedOn:
    async def test_a_positions_row_missing_its_quantity_stops_the_cycle(
        self, wiring: Wiring
    ) -> None:
        """AUDIT-002 in the place it would cost most: a missing quantity read as
        zero makes an open position invisible to the loop whose job is to see it."""
        wiring.account.net.append(
            {
                "tradingsymbol": "WIPRO",
                "exchange": "NSE",
                "product": "MIS",
                "day_buy_quantity": 25,
                "day_sell_quantity": 0,
            }
        )
        with pytest.raises(ReconciliationReadError, match="no action taken"):
            await wiring.run()
        assert not (await wiring.halter.state()).kill_switch
