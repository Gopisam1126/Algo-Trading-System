"""E07-S01 against the real schema: a published index file resolved against the
instrument master as Postgres holds it.

The unit tests resolve against Kite's symbol list as a file. This is the seam
the pre-market stage will use: ``InstrumentRepository.active_symbols`` over the
``instruments`` table the 05:30 sync writes - including a delisted symbol,
which the sync keeps as an inactive row and which must read as unresolved.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from algotrader.common.config import UniverseConfig
from algotrader.common.db import engine as db_engine
from algotrader.common.db.repositories import InstrumentRepository
from algotrader.premarket.universe import load_base_universe, parse_constituents

pytestmark = [pytest.mark.integration]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "universe"
AS_OF = dt.date(2026, 11, 22)


def _nifty50() -> bytes:
    return (FIXTURES / "ind_nifty50list.csv").read_bytes()


@pytest.fixture
async def session(migrated_database: str) -> AsyncIterator[AsyncSession]:
    engine = db_engine.create_engine_from_url(migrated_database)
    factory = db_engine.create_session_factory(engine)
    async with factory() as s:
        yield s
        await s.rollback()
    await engine.dispose()


async def _master_with(session: AsyncSession, *, inactive: set[str]) -> InstrumentRepository:
    repo = InstrumentRepository(session)
    members = parse_constituents(_nifty50(), base="nifty50")
    await repo.upsert(
        [
            {
                "tradingsymbol": c.tradingsymbol,
                "exchange": "NSE",
                "broker_token": f"u50-{i}",
                "tick_size": Decimal("0.10"),
                "lot_size": 1,
                "is_active": c.tradingsymbol not in inactive,
            }
            for i, c in enumerate(members)
        ]
    )
    await session.flush()
    return repo


async def test_the_nifty50_resolves_against_the_table(session: AsyncSession) -> None:
    repo = await _master_with(session, inactive=set())
    got = load_base_universe(
        UniverseConfig(base="nifty50"),
        as_of=AS_OF,
        master=await repo.active_symbols(),
        fetch=lambda _base, _source: _nifty50(),
    )
    assert len(got) == 50 and got.unresolved == ()


async def test_a_delisted_row_is_kept_but_does_not_resolve(session: AsyncSession) -> None:
    repo = await _master_with(session, inactive={"TRENT"})
    active = await repo.active_symbols()
    assert "TRENT" not in active and "ULTRACEMCO" in active
    got = load_base_universe(
        UniverseConfig(base="nifty50"),
        as_of=AS_OF,
        master=active,
        fetch=lambda _base, _source: _nifty50(),
    )
    assert got.unresolved == ("TRENT",) and len(got) == 49
