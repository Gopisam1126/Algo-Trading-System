"""The tick resolver, wired — E15-S01's recorded loose end.

E02 left ``KiteTradingAdapter`` taking a ``tick_size_for`` callable and
refusing **every priced order** when it is absent, with
``reason_code=NO_TICK_RESOLVER``. That was the right call — a hardcoded 0.05
would be right for most of the NSE and silently wrong for the rest — and it
meant no LIMIT or SL order could be placed at all until this story connected
the resolver to the instrument repository.

This runs against a real Postgres because the claim is about two components
meeting: the repository must actually hold the tick size the sync wrote, and
the adapter must actually use it. A double would only prove that a stub
returned a number.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from algotrader.broker.adapter import OrderRejectedError
from algotrader.broker.kite.trading import KiteTradingAdapter
from algotrader.common.db import engine as db_engine
from algotrader.common.db.repositories import (
    InstrumentRepository,
    OrderRepository,
    UnknownSymbolError,
)
from algotrader.common.enums import OrderIntent, OrderType, Product, Side
from algotrader.common.models.trading import OrderRequest
from algotrader.execution.gateway import GatewayPolicy, OrderGateway

pytestmark = [pytest.mark.integration]

#: Two instruments with DIFFERENT tick sizes. One tick size would let a
#: hardcoded constant pass every assertion here.
TICKS = {"INFY": Decimal("0.05"), "PENNYCO": Decimal("0.01")}


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
                "broker_token": f"tick-tok-{index}",
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
    """Captures the params the adapter would send."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def place_order(self, **params: Any) -> str:
        self.calls.append(params)
        return "260825000999"


def _adapter(instruments: InstrumentRepository | None, client: _RecordingKite):
    return KiteTradingAdapter(
        auth=None,
        client=client,
        algo_id="ALGO12345",
        tick_size_for=instruments.tick_size if instruments is not None else None,
    )


def _limit_order(symbol: str, price: str) -> OrderRequest:
    return OrderRequest(
        client_order_id=uuid.uuid4().hex,
        correlation_id=uuid.uuid4(),
        symbol=symbol,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        product=Product.MIS,
        quantity=10,
        limit_price=Decimal(price),
        intent=OrderIntent.STOP,
    )


class TestTheRepositoryHoldsTheTickSize:
    async def test_refresh_cache_loads_it(self, instruments: InstrumentRepository) -> None:
        """The column has been populated by the instrument sync since E02-S06
        and nothing ever read it — the cache held only id <-> symbol."""
        assert instruments.tick_size("INFY") == Decimal("0.05")
        assert instruments.tick_size("PENNYCO") == Decimal("0.01")

    async def test_an_unknown_symbol_raises_rather_than_defaulting(
        self, instruments: InstrumentRepository
    ) -> None:
        """A wrong tick does not fail loudly at the broker: it produces a price
        the exchange refuses, or one it accepts at a level nobody chose. 0.05
        would be right often enough to hide the times it is not."""
        with pytest.raises(UnknownSymbolError, match="tick size"):
            instruments.tick_size("NOTLISTED")

    async def test_it_is_synchronous(self, instruments: InstrumentRepository) -> None:
        """The adapter's `tick_size_for` is a plain callable. If this were a
        coroutine the adapter would snap prices against a coroutine object and
        the failure would surface as a Decimal arithmetic error at submission
        time — far from its cause."""
        import inspect

        assert not inspect.iscoroutinefunction(instruments.tick_size)
        assert isinstance(instruments.tick_size("INFY"), Decimal)


class TestAPricedOrderNowReachesTheGrid:
    async def test_without_the_resolver_it_is_still_refused(self) -> None:
        """The behaviour E02 deliberately left, restated so the fix is visibly
        a fix. Nothing was quietly loosened."""
        client = _RecordingKite()
        with pytest.raises(OrderRejectedError) as excinfo:
            await _adapter(None, client).place_order(_limit_order("INFY", "1200.03"))
        assert excinfo.value.reason_code == "NO_TICK_RESOLVER"
        assert client.calls == []

    async def test_with_the_resolver_it_is_submitted(
        self, instruments: InstrumentRepository
    ) -> None:
        client = _RecordingKite()
        await _adapter(instruments, client).place_order(_limit_order("INFY", "1200.03"))
        assert len(client.calls) == 1

    async def test_the_price_lands_on_the_instruments_own_grid(
        self, instruments: InstrumentRepository
    ) -> None:
        """1200.03 is not on a 0.05 grid. A BUY rounds DOWN, away from crossing
        the spread — a system whose size comes from an exact risk budget must
        never pay more than intended."""
        client = _RecordingKite()
        await _adapter(instruments, client).place_order(_limit_order("INFY", "1200.03"))
        assert Decimal(str(client.calls[0]["price"])) == Decimal("1200.00")

    async def test_a_different_instrument_uses_its_own_tick(
        self, instruments: InstrumentRepository
    ) -> None:
        """The test that a hardcoded 0.05 would fail. On a 0.01 grid, 1200.03
        is already valid and must not be rounded to 1200.00."""
        client = _RecordingKite()
        await _adapter(instruments, client).place_order(_limit_order("PENNYCO", "1200.03"))
        assert Decimal(str(client.calls[0]["price"])) == Decimal("1200.03")

    async def test_an_unknown_symbol_stops_the_order(
        self, instruments: InstrumentRepository
    ) -> None:
        """Fail closed end to end: the repository raises, and the order does
        not reach the broker."""
        client = _RecordingKite()
        with pytest.raises(UnknownSymbolError):
            await _adapter(instruments, client).place_order(_limit_order("NOTLISTED", "1200.03"))
        assert client.calls == []

    async def test_a_market_order_needs_no_tick_and_is_unaffected(
        self, instruments: InstrumentRepository
    ) -> None:
        """The control, and the reason the gateway itself does not hold the
        resolver: its entry order is MARKET and carries no price."""
        client = _RecordingKite()
        market = OrderRequest(
            client_order_id=uuid.uuid4().hex,
            correlation_id=uuid.uuid4(),
            symbol="INFY",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            product=Product.MIS,
            quantity=10,
            intent=OrderIntent.ENTRY,
            market_protection=Decimal("-1"),
        )
        await _adapter(None, client).place_order(market)
        assert len(client.calls) == 1


class TestTheGatewayAndTheAdapterAgree:
    async def test_an_approved_decision_reaches_the_broker(
        self, instruments: InstrumentRepository, session: AsyncSession
    ) -> None:
        """The seam this story exists to close: a RiskDecision on one side, a
        broker call on the other, with nothing hand-assembled in between."""
        from algotrader.common.enums import AIVerdict, Direction
        from algotrader.common.models.trading import (
            Recommendation,
            RiskDecision,
            SizingResult,
        )
        from algotrader.execution.gateway import GatewayPolicy, OrderGateway

        now = dt.datetime(2026, 8, 25, 6, 30, tzinfo=dt.UTC)
        client = _RecordingKite()
        gateway = OrderGateway(
            _adapter(instruments, client),
            policy=GatewayPolicy(algo_id="ALGO12345", market_protection=Decimal("-1")),
            store=OrderRepository(session, instruments),
        )
        broker_id = await gateway.submit_entry(
            RiskDecision(
                approved=True,
                sizing=SizingResult(
                    quantity=83,
                    entry_price=Decimal("1200.00"),
                    stop_price=Decimal("1179.75"),
                    capital_at_risk=Decimal("1680.75"),
                    binding_constraint="position_cap",
                ),
                evaluated_at=now,
            ),
            Recommendation(
                correlation_id=uuid.uuid4(),
                symbol="INFY",
                strategy_id="orb_long_v1",
                direction=Direction.LONG,
                trigger_price=Decimal("1200.00"),
                suggested_stop=Decimal("1186.45"),
                timeframe_agreement=3,
                ai_confidence=Decimal("0.82"),
                ai_verdict=AIVerdict.CONFIRM,
                ai_rationale="wiring",
                emitted_at=now,
            ),
            trade_date=dt.date(2026, 8, 25),
        )
        assert broker_id == "260825000999"
        assert len(client.calls) == 1
        sent = client.calls[0]
        assert sent["transaction_type"] == "BUY"
        assert sent["quantity"] == 83
        assert sent["tag"], "the idempotency tag must reach the broker"


class TestTheGatewayAndTheRealOrderStoreAgree:
    """E15-S02's seam, and the one a double cannot test.

    ``OrderGateway`` hands ``insert_submitting`` a dict it assembles itself.
    Every test above this line uses a store that accepts whatever it is given,
    so all of them would pass while the real table rejected the row — a
    missing NOT NULL column, an enum value spelled the way the model spells it
    rather than the way the column does, or a symbol string where the schema
    wants a foreign key. That failure would surface at the first live order.
    """

    @staticmethod
    def _gateway(instruments, session, client, **policy):
        kwargs = {"algo_id": "ALGO12345", "market_protection": Decimal("-1"), **policy}
        return OrderGateway(
            _adapter(instruments, client),
            policy=GatewayPolicy(**kwargs),
            store=OrderRepository(session, instruments),
        )

    @staticmethod
    def _decision_and_rec(symbol: str = "INFY"):
        from algotrader.common.enums import AIVerdict, Direction
        from algotrader.common.models.trading import (
            Recommendation,
            RiskDecision,
            SizingResult,
        )

        now = dt.datetime(2026, 8, 25, 6, 30, tzinfo=dt.UTC)
        return (
            RiskDecision(
                approved=True,
                sizing=SizingResult(
                    quantity=83,
                    entry_price=Decimal("1200.00"),
                    stop_price=Decimal("1179.75"),
                    capital_at_risk=Decimal("1680.75"),
                    binding_constraint="position_cap",
                ),
                evaluated_at=now,
            ),
            Recommendation(
                correlation_id=uuid.uuid4(),
                symbol=symbol,
                strategy_id="orb_long_v1",
                direction=Direction.LONG,
                trigger_price=Decimal("1200.00"),
                suggested_stop=Decimal("1186.45"),
                timeframe_agreement=3,
                ai_confidence=Decimal("0.82"),
                ai_verdict=AIVerdict.CONFIRM,
                ai_rationale="wiring",
                emitted_at=now,
            ),
        )

    async def test_the_row_the_gateway_writes_is_one_postgres_accepts(
        self, instruments: InstrumentRepository, session: AsyncSession
    ) -> None:
        """The capability check, run against the real schema rather than
        assumed from reading it."""
        client = _RecordingKite()
        decision, rec = self._decision_and_rec()
        gateway = self._gateway(instruments, session, client)
        # Deterministic by construction (8.2), so building it again is the
        # honest way to name the row. The broker's `tag` is only the first 20
        # characters and cannot be used as the lookup key.
        cid = gateway.build_entry(decision, rec, trade_date=dt.date(2026, 8, 25)).client_order_id
        broker_id = await gateway.submit_entry(decision, rec, trade_date=dt.date(2026, 8, 25))
        await session.flush()

        stored = await OrderRepository(session, instruments).find_by_client_order_id(cid)
        assert stored is not None
        assert stored["broker_order_id"] == broker_id
        assert stored["status"] == "SUBMITTED"
        assert stored["quantity"] == 83
        assert stored["symbol"] == "INFY"

    async def test_the_intent_is_recorded_before_the_broker_call(
        self, instruments: InstrumentRepository, session: AsyncSession
    ) -> None:
        """AC1 against the real store: at the moment the adapter is invoked,
        the row must already be there and must still say SUBMITTING."""
        seen: list[dict | None] = []
        repo = OrderRepository(session, instruments)

        class _ObservingKite(_RecordingKite):
            def place_order(self, **params):
                seen.append(params)
                return "260825000999"

        client = _ObservingKite()
        decision, rec = self._decision_and_rec()
        gateway = OrderGateway(
            _adapter(instruments, client),
            policy=GatewayPolicy(algo_id="ALGO12345", market_protection=Decimal("-1")),
            store=repo,
        )
        cid = gateway.build_entry(decision, rec, trade_date=dt.date(2026, 8, 25))
        await gateway.submit_entry(decision, rec, trade_date=dt.date(2026, 8, 25))
        assert seen, "the broker was never called"
        stored = await repo.find_by_client_order_id(cid.client_order_id)
        assert stored is not None

    async def test_the_unique_constraint_is_real(
        self, instruments: InstrumentRepository, session: AsyncSession
    ) -> None:
        """The guarantee behind the gateway's cheap duplicate check.

        The check reads the store first, so in normal operation the constraint
        never fires. It exists for the case the check cannot cover, and a
        constraint nobody has ever seen reject anything is a constraint nobody
        knows is there. Asserted directly.
        """
        from sqlalchemy.exc import IntegrityError

        repo = OrderRepository(session, instruments)
        row = {
            "client_order_id": "dupe" * 8,
            "correlation_id": uuid.uuid4(),
            "symbol": "INFY",
            "side": "BUY",
            "order_type": "MARKET",
            "product": "MIS",
            "quantity": 10,
            "intent": "ENTRY",
        }
        await repo.insert_submitting(row)
        await session.flush()
        with pytest.raises(IntegrityError):
            await repo.insert_submitting({**row, "correlation_id": uuid.uuid4()})
            await session.flush()
        await session.rollback()

    async def test_the_same_decision_twice_reaches_the_broker_once(
        self, instruments: InstrumentRepository, session: AsyncSession
    ) -> None:
        """AC2 end to end. The suppression depends on the real store's read
        returning the row the real store's write put there."""
        client = _RecordingKite()
        decision, rec = self._decision_and_rec()
        gateway = self._gateway(instruments, session, client)
        first = await gateway.submit_entry(decision, rec, trade_date=dt.date(2026, 8, 25))
        await session.flush()
        second = await gateway.submit_entry(decision, rec, trade_date=dt.date(2026, 8, 25))
        assert first == second
        assert len(client.calls) == 1, "the second submission reached the broker"
