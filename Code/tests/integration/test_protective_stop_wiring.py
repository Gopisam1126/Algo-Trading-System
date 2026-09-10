"""The protective stop meets the real gateway, adapter and database (E15-S04).

Every test in `tests/unit/test_protective_stop.py` uses a gateway double that
accepts whatever it is handed. That proves the *policy* — place, verify, close,
halt — and proves nothing about whether the order it builds can actually be
sent.

It matters more here than usual, because **the protective stop is the first
order this system sends that carries a price.** The entry is MARKET and needs
no tick grid; a stop carries a trigger price, and `KiteTradingAdapter` refuses
every priced order when no tick resolver is wired. E15-S01 built that resolver
and tested it in isolation; until this story nothing in the trading path used
it. So the wiring E15-S01 called a "recorded loose end" becomes load-bearing
exactly here, and a regression in it would surface as *every stop rejected* —
which, without the emergency close, is every position naked.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from algotrader.broker.kite.trading import KiteTradingAdapter
from algotrader.common.db import engine as db_engine
from algotrader.common.db.repositories import InstrumentRepository, OrderRepository
from algotrader.common.enums import Direction, OrderIntent
from algotrader.common.models.trading import Position
from algotrader.execution.gateway import GatewayPolicy, OrderGateway
from algotrader.execution.protective_stop import (
    ProtectiveStop,
    StopNotEstablishedError,
    stop_client_order_id,
)

pytestmark = [pytest.mark.integration]

#: Two instruments with DIFFERENT tick sizes, so a hardcoded 0.05 fails.
TICKS = {"INFY": Decimal("0.05"), "PENNYCO": Decimal("0.01")}
TRADE_DATE = dt.date(2026, 8, 25)
NOW = dt.datetime(2026, 8, 25, 6, 30, tzinfo=dt.UTC)


@pytest.fixture
async def engine(migrated_database: str) -> AsyncIterator[object]:
    eng = db_engine.create_engine_from_url(migrated_database)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: object) -> AsyncIterator[AsyncSession]:
    factory = db_engine.create_session_factory(engine)  # type: ignore[arg-type]
    async with factory() as s:
        yield s
        await s.rollback()


@pytest.fixture
async def instruments(session: AsyncSession) -> InstrumentRepository:
    repo = InstrumentRepository(session)
    await repo.upsert(
        [
            {
                "tradingsymbol": symbol,
                "exchange": "NSE",
                "broker_token": f"stop-tok-{index}",
                "tick_size": tick,
                "lot_size": 1,
            }
            for index, (symbol, tick) in enumerate(TICKS.items())
        ]
    )
    await session.flush()
    await repo.refresh_cache()
    return repo


class _RecordingKite:
    """Captures the params the adapter would send, and can list orders back."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.book: list[dict[str, Any]] = []

    def place_order(self, **params: Any) -> str:
        self.calls.append(params)
        self.book.append(
            {
                "order_id": f"2608250009{len(self.calls):02d}",
                "tag": params.get("tag"),
                "status": "TRIGGER PENDING",
                "tradingsymbol": params["tradingsymbol"],
                "transaction_type": params["transaction_type"],
                "order_type": params["order_type"],
                "product": params["product"],
                "quantity": params["quantity"],
                "filled_quantity": 0,
                "order_timestamp": "2026-08-25 12:00:00",
                "exchange": "NSE",
            }
        )
        return self.book[-1]["order_id"]

    def orders(self) -> list[dict[str, Any]]:
        return list(self.book)


class _Halter:
    def __init__(self) -> None:
        self.armed: list[tuple] = []

    async def arm(self, reason, *, detail, armed_by, now):
        self.armed.append((reason, detail, armed_by))
        return object()


def _position(symbol: str = "INFY", stop: str = "1179.75") -> Position:
    return Position(
        correlation_id=uuid.uuid4(),
        symbol=symbol,
        slot_index=0,
        direction=Direction.LONG,
        quantity=83,
        entry_price=Decimal("1200.00"),
        stop_price=Decimal(stop),
        opened_at=NOW,
        squareoff_deadline=dt.datetime(2026, 8, 25, 9, 40, tzinfo=dt.UTC),
    )


def _gateway(instruments, session, client, *, with_resolver: bool = True) -> OrderGateway:
    return OrderGateway(
        KiteTradingAdapter(
            auth=None,
            client=client,
            algo_id="",
            tick_size_for=instruments.tick_size if with_resolver else None,
        ),
        policy=GatewayPolicy(algo_id=None, market_protection=Decimal("-1")),
        store=OrderRepository(session, instruments),
    )


class TestTheStopReachesTheBrokerCorrectly:
    async def test_the_trigger_price_lands_on_the_instruments_grid(
        self, instruments: InstrumentRepository, session: AsyncSession
    ) -> None:
        """A stop derived from ATR is essentially never on a 0.05 grid by
        accident, and the exchange rejects off-grid prices — so without the
        resolver every computed stop would be refused."""
        client = _RecordingKite()
        await _gateway(instruments, session, client).submit_stop(
            _position(stop="1179.73"), trade_date=TRADE_DATE
        )
        assert Decimal(str(client.calls[0]["trigger_price"])) == Decimal("1179.75")

    async def test_a_different_instrument_uses_its_own_tick(
        self, instruments: InstrumentRepository, session: AsyncSession
    ) -> None:
        """The test a hardcoded 0.05 fails. On a 0.01 grid 1179.73 is already
        valid and must not be moved."""
        client = _RecordingKite()
        await _gateway(instruments, session, client).submit_stop(
            _position(symbol="PENNYCO", stop="1179.73"), trade_date=TRADE_DATE
        )
        assert Decimal(str(client.calls[0]["trigger_price"])) == Decimal("1179.73")

    async def test_snapping_never_widens_the_stop(
        self, instruments: InstrumentRepository, session: AsyncSession
    ) -> None:
        """End to end, on the real grid: the price the broker receives is never
        further from entry than the price the sizer chose. If it were, the
        position would risk more than the risk budget that sized it."""
        client = _RecordingKite()
        position = _position(stop="1179.71")
        await _gateway(instruments, session, client).submit_stop(position, trade_date=TRADE_DATE)
        sent = Decimal(str(client.calls[0]["trigger_price"]))
        assert position.entry_price - sent <= position.entry_price - position.stop_price

    async def test_it_is_sent_as_sl_m(
        self, instruments: InstrumentRepository, session: AsyncSession
    ) -> None:
        """SL-M, in Kite's spelling. An SL (limit) stop can fail to fill in
        exactly the move it exists to protect against."""
        client = _RecordingKite()
        await _gateway(instruments, session, client).submit_stop(_position(), trade_date=TRADE_DATE)
        assert client.calls[0]["order_type"] == "SL-M"
        assert client.calls[0]["transaction_type"] == "SELL"
        assert client.calls[0]["market_protection"] == -1.0


class TestTheStopIsRecordedLikeAnyOtherOrder:
    async def test_it_persists_with_intent_stop(
        self, instruments: InstrumentRepository, session: AsyncSession
    ) -> None:
        """The shared submission path means the stop inherits TX1/TX2 — which
        is what lets reconciliation find it after a restart."""
        client = _RecordingKite()
        position = _position()
        broker_id = await _gateway(instruments, session, client).submit_stop(
            position, trade_date=TRADE_DATE
        )
        await session.flush()

        cid = stop_client_order_id(position, trade_date=TRADE_DATE)
        stored = await OrderRepository(session, instruments).find_by_client_order_id(cid)
        assert stored is not None
        assert stored["intent"] == OrderIntent.STOP.value
        assert stored["broker_order_id"] == broker_id
        assert stored["status"] == "SUBMITTED"

    async def test_submitting_the_same_stop_twice_reaches_the_broker_once(
        self, instruments: InstrumentRepository, session: AsyncSession
    ) -> None:
        """Idempotency inherited from the shared path, and it matters more here
        than for an entry: two stops on one position is two exits, and the
        second sells a position that no longer exists."""
        client = _RecordingKite()
        gateway = _gateway(instruments, session, client)
        position = _position()
        first = await gateway.submit_stop(position, trade_date=TRADE_DATE)
        await session.flush()
        second = await gateway.submit_stop(position, trade_date=TRADE_DATE)
        assert first == second
        assert len(client.calls) == 1


class TestAMisconfiguredResolverDoesNotLeaveAPositionNaked:
    """The integration that would not occur to anyone as a scenario.

    E02 made the adapter refuse every priced order when no tick resolver is
    wired — the right call, and it means a deployment that forgets the wiring
    rejects every stop. Before E15-S04 that produced a naked position per
    entry, silently. Now it produces a closed position and a loud CRITICAL.
    """

    async def test_the_position_is_closed_at_market_instead(
        self, instruments: InstrumentRepository, session: AsyncSession
    ) -> None:
        client = _RecordingKite()
        gateway = _gateway(instruments, session, client, with_resolver=False)
        halter = _Halter()

        with pytest.raises(StopNotEstablishedError):
            await ProtectiveStop(gateway, halter=halter).attach(
                _position(), trade_date=TRADE_DATE, now=NOW
            )

        assert len(client.calls) == 1, "exactly one order should have reached the broker"
        assert client.calls[0]["order_type"] == "MARKET", "the emergency exit"
        assert halter.armed == [], "a contained failure must not stop the day"

    async def test_the_exit_needs_no_tick_resolver(
        self, instruments: InstrumentRepository, session: AsyncSession
    ) -> None:
        """The reason the emergency close can succeed where the stop failed:
        it is a MARKET order and carries no price to snap. If the close had
        needed the resolver too, the same misconfiguration would have taken
        out both halves and every position would have gone naked."""
        client = _RecordingKite()
        await _gateway(instruments, session, client, with_resolver=False).submit_exit(
            _position(), trade_date=TRADE_DATE
        )
        assert client.calls[0]["order_type"] == "MARKET"
        assert "trigger_price" not in client.calls[0]


class TestTheWholeSequenceAgainstRealComponents:
    async def test_a_healthy_attach_verifies_through_the_real_orderbook(
        self, instruments: InstrumentRepository, session: AsyncSession
    ) -> None:
        """The verification queries the broker by tag, through the real
        adapter's `find_by_client_order_id` — which matches on the TRUNCATED
        20-character tag, not the full id. If that truncation ever disagreed
        with what was sent, every stop would verify as absent and every
        position would be closed immediately after being opened."""
        client = _RecordingKite()
        gateway = _gateway(instruments, session, client)
        position = _position()

        established = await ProtectiveStop(gateway, halter=_Halter()).attach(
            position, trade_date=TRADE_DATE, now=NOW
        )
        assert established.stop_broker_order_id == client.book[0]["order_id"]
        assert len(client.calls) == 1, "no emergency exit should have been sent"

    async def test_the_predicate_answers_from_the_real_orderbook(
        self, instruments: InstrumentRepository, session: AsyncSession
    ) -> None:
        """What E15-S09 will call every 30 seconds."""
        client = _RecordingKite()
        gateway = _gateway(instruments, session, client)
        attacher = ProtectiveStop(gateway, halter=_Halter())
        position = _position()
        await attacher.attach(position, trade_date=TRADE_DATE, now=NOW)

        assert await attacher.is_protected(position, trade_date=TRADE_DATE)
        client.book[0]["status"] = "REJECTED"
        assert not await attacher.is_protected(position, trade_date=TRADE_DATE)
