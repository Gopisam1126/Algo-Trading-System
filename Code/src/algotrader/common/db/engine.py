"""Async engine, session factory, and the unit-of-work boundary.

**The rule this module exists to enforce: never hold a database transaction
open across a broker API call.**

A broker call takes 300 ms on a good day and can hang for 30 s. Holding a
transaction for that long blocks vacuum, holds row locks, and exhausts the
connection pool at exactly the moment the system is under stress — which is to
say, at exactly the moment it matters. Order submission is therefore two
transactions with the broker call *between* them, never one wrapped around it.

The intermediate ``SUBMITTING`` state is what makes recovery possible: if the
process dies between the two, reconciliation finds an order in ``SUBMITTING``
with no ``broker_order_id`` and knows to query by ``client_order_id`` rather
than blindly resubmit.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from algotrader.common.db.eventloop import configure_event_loop_policy

if TYPE_CHECKING:
    from algotrader.common.config import DatabaseConfig

log = logging.getLogger(__name__)


def create_engine(config: DatabaseConfig, password: str | None = None) -> AsyncEngine:
    """Build the async engine from configuration.

    ``configure_event_loop_policy()`` is called first and is not optional on
    Windows: async psycopg refuses the default ProactorEventLoop and raises
    ``InterfaceError`` at the first query, a long way from the cause. No-op
    elsewhere.

    ``statement_timeout`` is applied server-side rather than as a client-side
    cancel. A client-side timeout abandons the query while the server keeps
    executing it, so the load stays and the connection is unusable until it
    finishes. The server-side setting actually stops the work.
    """
    configure_event_loop_policy()

    return create_async_engine(
        config.dsn(password),
        pool_size=config.pool_size,
        max_overflow=config.max_overflow,
        pool_pre_ping=config.pool_pre_ping,
        pool_recycle=config.pool_recycle_seconds,
        echo=config.echo_sql,
        connect_args={
            "options": f"-c statement_timeout={config.statement_timeout_ms}",
            "application_name": "algotrader",
        },
    )


def create_engine_from_url(
    url: str, *, echo: bool = False, connect_timeout_seconds: int = 10
) -> AsyncEngine:
    """Build an engine from a URL that is already resolved.

    For tests and tools that hold a DSN directly (an ephemeral testcontainer,
    a one-off migration check) rather than a :class:`DatabaseConfig`. It still
    goes through :func:`configure_event_loop_policy`, which is the part that is
    easy to forget and fails only on Windows.

    ``connect_timeout_seconds`` is not optional in practice. Without it, psycopg
    waits on the OS TCP timeout — which on an unreachable host is minutes, not
    seconds. That turns "the database is down" into "the process appears to
    hang", which is a far worse failure to diagnose at 09:15. It also makes the
    audit outage tests possible: they point at a dead endpoint and need it to
    fail promptly rather than stall the suite.
    """
    configure_event_loop_policy()
    return create_async_engine(
        url,
        echo=echo,
        connect_args={
            "application_name": "algotrader",
            "connect_timeout": connect_timeout_seconds,
        },
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build the session factory.

    ``expire_on_commit=False`` because the default makes every attribute of
    every object a fresh SELECT after commit — and in async code that lazy load
    raises ``MissingGreenlet`` rather than quietly being slow. Objects returned
    from a repository must stay usable after their session closes.
    """
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


@asynccontextmanager
async def unit_of_work(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One transaction, committed on success and rolled back on any exception.

    The explicit boundary is the point. Without it, "when does this commit?"
    becomes a question you answer by reading the whole call stack, and the
    answer changes when someone refactors a caller.

    Use one of these per logical operation, and **close it before any broker
    call.** See the module docstring.
    """
    session = factory()
    try:
        async with session.begin():
            yield session
    finally:
        await session.close()


@asynccontextmanager
async def read_only(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """A session for reads. Rolled back rather than committed, always.

    Rolling back a read-only transaction is not pedantry — it releases the
    snapshot immediately. A long-lived idle-in-transaction session holds back
    vacuum across the whole database, and the pre-market warm-up reads a lot.
    """
    session = factory()
    try:
        yield session
    finally:
        await session.rollback()
        await session.close()


async def dispose(engine: AsyncEngine) -> None:
    """Close all pooled connections."""
    await engine.dispose()


async def healthcheck(factory: async_sessionmaker[AsyncSession]) -> bool:
    """True if the database answers. Never raises."""
    from sqlalchemy import text

    try:
        async with read_only(factory) as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        log.warning("database healthcheck failed", exc_info=True)
        return False
