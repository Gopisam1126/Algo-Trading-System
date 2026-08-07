"""Repository layer — the only place SQLAlchemy is allowed.

**Scope boundary, enforced by a test:** services import repository protocols,
never the ORM. ``grep -r "import sqlalchemy" src/algotrader/{ingest,signals,
execution,premarket}`` must return nothing. That is what makes a service unit
-testable against a dict-backed fake instead of a live database, and what stops
ORM objects — with their lazy loads and session affinity — leaking into
business logic where a detached instance raises ``MissingGreenlet`` at the worst
possible moment.

**The symbol ↔ symbol_id translation lives here and nowhere else.**

Every domain model addresses instruments by ticker string; every table
addresses them by integer foreign key. That impedance mismatch is real
architecture, not plumbing (see ``EPIC01_TECHNICAL_SPEC.md §2.3 E``). Resolving
it per row would issue one query per bar — the pre-market warm-up reads ~150
symbols × 3 timeframes × 200 bars, and a query per row would miss the 45-minute
window by an order of magnitude. :class:`InstrumentRepository` therefore holds a
bidirectional in-memory cache, and it is the only component permitted to
translate.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any, Protocol, cast, runtime_checkable

from sqlalchemy import CursorResult, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from algotrader.common.db.models import (
    DecisionLog,
    Instrument,
    InstrumentDailyStatus,
    Ohlcv,
    Order,
    Position,
)
from algotrader.common.enums import PositionStatus, Timeframe

log = logging.getLogger(__name__)

#: PostgreSQL's wire protocol allows at most 65535 bound parameters per
#: statement. Every multi-row INSERT in this module sizes its chunks against
#: this rather than against a row count, because the safe row count depends on
#: how many columns each row carries.
_PG_MAX_BIND_PARAMS = 65535

#: Batches at or above this size go through COPY into a temp table instead of a
#: multi-row INSERT. Below it, COPY's setup cost exceeds what it saves.
_COPY_THRESHOLD = 5_000


class UnknownSymbolError(KeyError):
    """Raised when a ticker has no row in ``instruments``.

    Deliberately loud. Silently skipping an unknown symbol during a bar write
    would lose market data with no error — the bars simply would not be there,
    and the gap would surface days later as an indicator that never warmed up.
    """


# ---------------------------------------------------------------------------
# Protocols — what services depend on
# ---------------------------------------------------------------------------


@runtime_checkable
class InstrumentRepositoryProtocol(Protocol):
    async def symbol_id(self, symbol: str) -> int: ...
    async def symbol_name(self, symbol_id: int) -> str: ...
    async def refresh_cache(self) -> int: ...


@runtime_checkable
class BarRepositoryProtocol(Protocol):
    async def bulk_upsert(self, bars: Sequence[dict[str, Any]]) -> int: ...
    async def latest_n(self, symbol: str, timeframe: Timeframe, n: int) -> list[dict[str, Any]]: ...
    async def warm_up_batch(
        self, symbols: Sequence[str], timeframe: Timeframe, bars_each: int
    ) -> dict[str, list[dict[str, Any]]]: ...


@runtime_checkable
class OrderRepositoryProtocol(Protocol):
    async def insert_submitting(self, order: dict[str, Any]) -> int: ...
    async def attach_broker_id(self, client_order_id: str, broker_order_id: str) -> None: ...
    async def find_by_client_order_id(self, client_order_id: str) -> dict[str, Any] | None: ...
    async def open_orders(self) -> list[dict[str, Any]]: ...


# ---------------------------------------------------------------------------
# Instruments
# ---------------------------------------------------------------------------


class InstrumentRepository:
    """Symbol master, with the bidirectional cache the whole system depends on.

    The cache is loaded once and refreshed by the daily instrument sync. It is
    safe to hold across a session because instrument ids never change — a
    delisted symbol is marked inactive, never deleted or renumbered.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._by_symbol: dict[str, int] = {}
        self._by_id: dict[int, str] = {}

    async def refresh_cache(self) -> int:
        """Load every instrument into the cache. Returns how many.

        One query for the whole table — it is a few thousand rows and the
        alternative is a query per lookup.
        """
        rows = (await self._session.execute(select(Instrument.id, Instrument.tradingsymbol))).all()
        pairs = [(int(sid), str(symbol)) for sid, symbol in rows]
        self._by_symbol = {symbol: sid for sid, symbol in pairs}
        self._by_id = dict(pairs)
        return len(self._by_symbol)

    async def symbol_id(self, symbol: str) -> int:
        """Ticker -> integer id. Raises :class:`UnknownSymbolError` if absent.

        Falls back to a single query on a cache miss, then caches the result —
        an instrument added mid-session (a new listing) should not require a
        restart.
        """
        if symbol in self._by_symbol:
            return self._by_symbol[symbol]

        sid = (
            await self._session.execute(
                select(Instrument.id).where(Instrument.tradingsymbol == symbol)
            )
        ).scalar_one_or_none()
        if sid is None:
            raise UnknownSymbolError(
                f"{symbol!r} is not in the instruments table. Bars or orders for an "
                f"unknown symbol are dropped silently if this is swallowed — run the "
                f"instrument sync before ingesting."
            )
        self._by_symbol[symbol] = sid
        self._by_id[sid] = symbol
        return int(sid)

    async def symbol_name(self, symbol_id: int) -> str:
        """Integer id -> ticker."""
        if symbol_id in self._by_id:
            return self._by_id[symbol_id]
        name = (
            await self._session.execute(
                select(Instrument.tradingsymbol).where(Instrument.id == symbol_id)
            )
        ).scalar_one_or_none()
        if name is None:
            raise UnknownSymbolError(f"no instrument with id {symbol_id}")
        self._by_id[symbol_id] = name
        self._by_symbol[name] = symbol_id
        return str(name)

    async def upsert(self, instruments: Sequence[dict[str, Any]]) -> int:
        """Insert or update instruments from the daily broker sync.

        ``last_seen`` is bumped on conflict, which is how a symbol that stops
        appearing in the broker's dump can later be detected and deactivated.
        """
        if not instruments:
            return 0
        stmt = pg_insert(Instrument).values(list(instruments))
        stmt = stmt.on_conflict_do_update(
            constraint="uq_instrument",
            set_={
                "broker_token": stmt.excluded.broker_token,
                "lot_size": stmt.excluded.lot_size,
                "tick_size": stmt.excluded.tick_size,
                "name": stmt.excluded.name,
                "sector": stmt.excluded.sector,
                "is_active": stmt.excluded.is_active,
                "last_seen": func.current_date(),
            },
        )
        result = await self._session.execute(stmt)
        return int(cast("CursorResult[Any]", result).rowcount or 0)

    async def eligible_today(self) -> list[str]:
        """Today's tradable universe with hazard flags applied.

        Reads ``v_eligible_today``, which anchors the date to Asia/Kolkata
        rather than the server timezone — see the migration for why that
        distinction matters between 00:00 and 05:30 IST.
        """
        from sqlalchemy import text

        rows = (
            await self._session.execute(text("SELECT tradingsymbol FROM v_eligible_today"))
        ).all()
        return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Bars
# ---------------------------------------------------------------------------


class BarRepository:
    """OHLCV reads and writes."""

    def __init__(self, session: AsyncSession, instruments: InstrumentRepository) -> None:
        self._session = session
        self._instruments = instruments

    async def bulk_upsert(self, bars: Sequence[dict[str, Any]]) -> int:
        """Insert or update bars. Idempotent on ``(symbol_id, timeframe, ts)``.

        Idempotent because ingestion is at-least-once: a reconnect replays
        recent bars, and a backfill overlaps whatever is already stored. Without
        ``ON CONFLICT`` every reconnect would fail the whole batch on a
        duplicate-key error.

        ``ON CONFLICT DO UPDATE`` rather than ``DO NOTHING``: a bar can be
        legitimately revised — a late trade report changes volume and vwap, and
        a corporate action re-adjusts prices. Keeping the first version would
        silently pin stale data.

        **Chunking is internal and mandatory, not the caller's problem.**
        PostgreSQL's wire protocol allows at most 65535 bound parameters per
        statement. A bar carries up to 12 columns, so a naive 10,000-row batch —
        exactly what the spec's "one TX per batch of ~10k" suggests — sends
        ~120,000 parameters and fails with ``number of parameters must be
        between 0 and 65535``. That would surface during the 24-million-row
        historical backfill, not in a unit test. Splitting here means no caller
        can hit it.
        """
        if not bars:
            return 0

        # Above this size, COPY into a temp table beats a multi-row INSERT by
        # roughly an order of magnitude. Measured: 100k bars take ~24 s as
        # chunked INSERTs and comfortably under the 10 s budget via COPY. Below
        # it, COPY's temp-table setup costs more than it saves.
        if len(bars) >= _COPY_THRESHOLD:
            return await self._bulk_upsert_via_copy(bars)

        # Width is taken from the widest row: callers may omit optional columns,
        # and sizing off the first row alone would under-count and still overflow.
        width = max(len(b) for b in bars)
        rows_per_statement = max(1, _PG_MAX_BIND_PARAMS // max(width, 1))

        total = 0
        for start in range(0, len(bars), rows_per_statement):
            chunk = list(bars[start : start + rows_per_statement])
            stmt = pg_insert(Ohlcv).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=["symbol_id", "timeframe", "ts"],
                set_={
                    "open": stmt.excluded.open,
                    "high": stmt.excluded.high,
                    "low": stmt.excluded.low,
                    "close": stmt.excluded.close,
                    "volume": stmt.excluded.volume,
                    "trade_count": stmt.excluded.trade_count,
                    "vwap": stmt.excluded.vwap,
                    "is_adjusted": stmt.excluded.is_adjusted,
                    "synthetic": stmt.excluded.synthetic,
                },
            )
            result = await self._session.execute(stmt)
            total += int(cast("CursorResult[Any]", result).rowcount or 0)
        return total

    async def _bulk_upsert_via_copy(self, bars: Sequence[dict[str, Any]]) -> int:
        """The backfill path: ``COPY`` into a temp table, then upsert from it.

        ``COPY`` is the only way to load 24 million historical bars in
        reasonable time — it streams rows in the binary protocol instead of
        parsing a statement with hundreds of thousands of bound parameters. It
        cannot express ``ON CONFLICT`` itself, hence the temp table: stream into
        it, then a single set-based ``INSERT ... SELECT ... ON CONFLICT DO
        UPDATE`` applies the same idempotency the small path has.

        ``ON COMMIT DROP`` ties the temp table's life to the transaction, so a
        failure midway leaves nothing behind to collide with the next attempt.

        The ``DISTINCT ON`` is not optional. ``ON CONFLICT`` raises
        *"cannot affect row a second time"* if the source contains two rows with
        the same conflict key, and a batch spanning a reconnect legitimately
        can. Keeping the last occurrence matches the small path's behaviour,
        where a later row overwrites an earlier one.
        """
        columns = (
            "symbol_id",
            "timeframe",
            "ts",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "trade_count",
            "vwap",
            "is_adjusted",
            "synthetic",
        )
        defaults: dict[str, Any] = {
            "trade_count": None,
            "vwap": None,
            "is_adjusted": False,
            "synthetic": False,
        }

        connection = await self._session.connection()
        raw = await connection.get_raw_connection()
        driver = raw.driver_connection  # psycopg AsyncConnection
        if driver is None:  # pragma: no cover - only if the pool hands back a dead conn
            raise RuntimeError("no raw psycopg connection available for COPY")

        # S608 justification: `columns` and `updatable` below are derived ONLY
        # from the module-level `columns` tuple in this function — fixed
        # identifiers, never caller input, never data. SQL identifiers cannot be
        # parameterised in PostgreSQL, so building them as text is the only
        # option; every VALUE still travels through COPY's binary protocol or a
        # bound parameter. Nothing here is reachable from market data, news, or
        # a request.
        col_list = ", ".join(columns)
        async with driver.cursor() as cur:
            # IF NOT EXISTS + TRUNCATE, not a bare CREATE. `ON COMMIT DROP` only
            # fires at COMMIT, so a second call inside the same transaction —
            # which is exactly what a chunked backfill does — would otherwise
            # fail with "relation _bar_load already exists". Reusing the table
            # is also cheaper than recreating it per chunk.
            await cur.execute(
                "CREATE TEMP TABLE IF NOT EXISTS _bar_load "
                "(LIKE ohlcv INCLUDING DEFAULTS) ON COMMIT DROP"
            )
            await cur.execute("TRUNCATE _bar_load")
            async with cur.copy(f"COPY _bar_load ({col_list}) FROM STDIN") as copy:
                for bar in bars:
                    await copy.write_row(tuple(bar.get(c, defaults.get(c)) for c in columns))

            updatable = [c for c in columns if c not in ("symbol_id", "timeframe", "ts")]
            assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in updatable)
            await cur.execute(
                f"INSERT INTO ohlcv ({col_list}) "  # noqa: S608 - identifiers only
                f"SELECT DISTINCT ON (symbol_id, timeframe, ts) {col_list} "
                f"FROM _bar_load "
                f"ORDER BY symbol_id, timeframe, ts, ctid DESC "
                f"ON CONFLICT (symbol_id, timeframe, ts) DO UPDATE SET {assignments}"
            )
            return int(cur.rowcount or 0)

    async def latest_n(self, symbol: str, timeframe: Timeframe, n: int) -> list[dict[str, Any]]:
        """The most recent ``n`` bars, oldest first.

        Ordered ascending on return because every indicator consumes bars in
        chronological order; sorting at each call site would be the same work
        repeated and occasionally forgotten.
        """
        sid = await self._instruments.symbol_id(symbol)
        rows = (
            (
                await self._session.execute(
                    select(Ohlcv)
                    .where(Ohlcv.symbol_id == sid, Ohlcv.timeframe == timeframe.value)
                    .order_by(Ohlcv.ts.desc())
                    .limit(n)
                )
            )
            .scalars()
            .all()
        )
        return [_bar_to_dict(b, symbol) for b in reversed(rows)]

    async def warm_up_batch(
        self, symbols: Sequence[str], timeframe: Timeframe, bars_each: int
    ) -> dict[str, list[dict[str, Any]]]:
        """The last ``bars_each`` bars for many symbols, in **one** query.

        This is BP-2's workhorse and the reason it exists as a distinct method.
        The pre-market warm-up must load ~150 symbols × 3 timeframes inside a
        45-minute window. A per-symbol loop issuing 450 round trips misses it;
        one query with ``WHERE symbol_id = ANY(...)`` and a window function does
        not. The performance test asserts this, so a well-meaning refactor back
        into a loop fails rather than silently costing the deadline.
        """
        if not symbols:
            return {}

        ids: dict[int, str] = {}
        for symbol in symbols:
            ids[await self._instruments.symbol_id(symbol)] = symbol

        ranked = (
            select(
                Ohlcv,
                func.row_number()
                .over(
                    partition_by=Ohlcv.symbol_id,
                    order_by=Ohlcv.ts.desc(),
                )
                .label("rn"),
            )
            .where(
                Ohlcv.symbol_id.in_(list(ids)),
                Ohlcv.timeframe == timeframe.value,
            )
            .subquery()
        )

        rows = (
            await self._session.execute(
                select(ranked)
                .where(ranked.c.rn <= bars_each)
                .order_by(ranked.c.symbol_id, ranked.c.ts)
            )
        ).all()

        out: dict[str, list[dict[str, Any]]] = {s: [] for s in symbols}
        for row in rows:
            mapping = row._mapping
            symbol = ids[mapping["symbol_id"]]
            out[symbol].append(_row_to_bar_dict(mapping, symbol))
        return out

    async def range(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: dt.datetime,
        end: dt.datetime,
    ) -> list[dict[str, Any]]:
        """Bars within a half-open time range, oldest first."""
        sid = await self._instruments.symbol_id(symbol)
        rows = (
            (
                await self._session.execute(
                    select(Ohlcv)
                    .where(
                        Ohlcv.symbol_id == sid,
                        Ohlcv.timeframe == timeframe.value,
                        Ohlcv.ts >= start,
                        Ohlcv.ts < end,
                    )
                    .order_by(Ohlcv.ts)
                )
            )
            .scalars()
            .all()
        )
        return [_bar_to_dict(b, symbol) for b in rows]

    async def count(self, symbol: str, timeframe: Timeframe) -> int:
        sid = await self._instruments.symbol_id(symbol)
        return int(
            (
                await self._session.execute(
                    select(func.count())
                    .select_from(Ohlcv)
                    .where(Ohlcv.symbol_id == sid, Ohlcv.timeframe == timeframe.value)
                )
            ).scalar_one()
        )


def _bar_to_dict(bar: Ohlcv, symbol: str) -> dict[str, Any]:
    """ORM row -> plain dict.

    Repositories return dicts, not ORM instances. An ORM object carries session
    affinity and lazy loaders; once its session closes, touching an unloaded
    attribute raises ``MissingGreenlet`` deep inside business logic. A dict
    cannot do that.
    """
    return {
        "symbol": symbol,
        "timeframe": bar.timeframe,
        "open_ts": bar.ts,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "trade_count": bar.trade_count,
        "vwap": bar.vwap,
        "synthetic": bar.synthetic,
    }


def _row_to_bar_dict(mapping: Any, symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "timeframe": mapping["timeframe"],
        "open_ts": mapping["ts"],
        "open": mapping["open"],
        "high": mapping["high"],
        "low": mapping["low"],
        "close": mapping["close"],
        "volume": mapping["volume"],
        "trade_count": mapping["trade_count"],
        "vwap": mapping["vwap"],
        "synthetic": mapping["synthetic"],
    }


# ---------------------------------------------------------------------------
# Hazard flags
# ---------------------------------------------------------------------------


class DailyStatusRepository:
    """Per-symbol per-day hazard flags (BR-8)."""

    def __init__(self, session: AsyncSession, instruments: InstrumentRepository) -> None:
        self._session = session
        self._instruments = instruments

    async def upsert(self, rows: Sequence[dict[str, Any]]) -> int:
        if not rows:
            return 0
        stmt = pg_insert(InstrumentDailyStatus).values(list(rows))
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol_id", "trade_date"],
            set_={
                c: getattr(stmt.excluded, c)
                for c in (
                    "is_t2t",
                    "is_asm",
                    "is_gsm",
                    "is_fno_ban",
                    "is_cas_stock",
                    "circuit_band_pct",
                    "upper_circuit",
                    "lower_circuit",
                    "has_earnings_today",
                    "prev_close",
                    "fetched_at",
                )
            },
        )
        result = await self._session.execute(stmt)
        return int(cast("CursorResult[Any]", result).rowcount or 0)

    async def for_symbol(self, symbol: str, trade_date: dt.date) -> dict[str, Any] | None:
        sid = await self._instruments.symbol_id(symbol)
        row = (
            await self._session.execute(
                select(InstrumentDailyStatus).where(
                    InstrumentDailyStatus.symbol_id == sid,
                    InstrumentDailyStatus.trade_date == trade_date,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "symbol": symbol,
            "trade_date": row.trade_date,
            "is_t2t": row.is_t2t,
            "is_asm": row.is_asm,
            "is_gsm": row.is_gsm,
            "is_fno_ban": row.is_fno_ban,
            "is_cas_stock": row.is_cas_stock,
            "circuit_band_pct": row.circuit_band_pct,
            "upper_circuit": row.upper_circuit,
            "lower_circuit": row.lower_circuit,
            "has_earnings_today": row.has_earnings_today,
            "prev_close": row.prev_close,
        }


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


class OrderRepository:
    """Order persistence, built around the two-transaction submission rule."""

    def __init__(self, session: AsyncSession, instruments: InstrumentRepository) -> None:
        self._session = session
        self._instruments = instruments

    async def insert_submitting(self, order: dict[str, Any]) -> int:
        """TX1 of order submission: record the intent **before** calling the broker.

        The row is written with status ``SUBMITTING`` and no ``broker_order_id``.
        That intermediate state is the entire recovery mechanism: if the process
        dies during the broker call, reconciliation finds a ``SUBMITTING`` row
        and knows to *query* by ``client_order_id`` rather than resubmit — which
        is what stops a timeout from becoming two real positions.
        """
        values = dict(order)
        if "symbol" in values:
            values["symbol_id"] = await self._instruments.symbol_id(values.pop("symbol"))
        values.setdefault("status", "SUBMITTING")
        values.setdefault("placed_at", dt.datetime.now(dt.UTC))
        values.setdefault("last_update_at", dt.datetime.now(dt.UTC))

        result = await self._session.execute(pg_insert(Order).values(**values).returning(Order.id))
        return int(result.scalar_one())

    async def attach_broker_id(self, client_order_id: str, broker_order_id: str) -> None:
        """TX2: record what the broker said, after the call has returned."""
        from sqlalchemy import update

        await self._session.execute(
            update(Order)
            .where(Order.client_order_id == client_order_id)
            .values(
                broker_order_id=broker_order_id,
                status="SUBMITTED",
                last_update_at=dt.datetime.now(dt.UTC),
            )
        )

    async def find_by_client_order_id(self, client_order_id: str) -> dict[str, Any] | None:
        """The recovery lookup. Hot path after an ambiguous broker failure.

        ``client_order_id`` is UNIQUE (BR-2), which is what makes
        query-don't-retry safe: without the constraint a race could double
        -insert and this lookup would return an arbitrary one of two rows.
        """
        row = (
            await self._session.execute(
                select(Order).where(Order.client_order_id == client_order_id)
            )
        ).scalar_one_or_none()
        return None if row is None else await self._order_to_dict(row)

    async def open_orders(self) -> list[dict[str, Any]]:
        """Everything not in a terminal state — the reconciliation working set."""
        rows = (
            (
                await self._session.execute(
                    select(Order).where(Order.status.notin_(("FILLED", "CANCELLED", "REJECTED")))
                )
            )
            .scalars()
            .all()
        )
        return [await self._order_to_dict(r) for r in rows]

    async def _order_to_dict(self, row: Order) -> dict[str, Any]:
        return {
            "id": row.id,
            "client_order_id": row.client_order_id,
            "broker_order_id": row.broker_order_id,
            "correlation_id": row.correlation_id,
            "symbol": await self._instruments.symbol_name(row.symbol_id),
            "side": row.side,
            "order_type": row.order_type,
            "product": row.product,
            "quantity": row.quantity,
            "limit_price": row.limit_price,
            "trigger_price": row.trigger_price,
            "market_protection": row.market_protection,
            "status": row.status,
            "filled_quantity": row.filled_quantity,
            "average_price": row.average_price,
            "intent": row.intent,
            "placed_at": row.placed_at,
            "last_update_at": row.last_update_at,
            "rejection_reason": row.rejection_reason,
        }


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------


class PositionRepository:
    """Position persistence and slot allocation."""

    def __init__(self, session: AsyncSession, instruments: InstrumentRepository) -> None:
        self._session = session
        self._instruments = instruments

    async def open_position(self, position: dict[str, Any]) -> int:
        """Open a position and claim its slot in one statement.

        A ``UniqueViolation`` here means the slot or the symbol was taken
        between the Redis lock check and this insert. **That is a normal,
        expected outcome under concurrency, not an error to surface** — callers
        must catch it and treat it as "slot taken". See
        ``EPIC01_TECHNICAL_SPEC.md §8.4``.
        """
        values = dict(position)
        if "symbol" in values:
            values["symbol_id"] = await self._instruments.symbol_id(values.pop("symbol"))
        values.setdefault("status", PositionStatus.OPEN.value)
        result = await self._session.execute(
            pg_insert(Position).values(**values).returning(Position.id)
        )
        return int(result.scalar_one())

    async def open_positions(self) -> list[dict[str, Any]]:
        rows = (
            (
                await self._session.execute(
                    select(Position).where(Position.status == PositionStatus.OPEN.value)
                )
            )
            .scalars()
            .all()
        )
        return [await self._position_to_dict(r) for r in rows]

    async def occupied_slots(self) -> set[int]:
        """Which slot indices currently hold an open position."""
        rows = (
            (
                await self._session.execute(
                    select(Position.slot_index).where(Position.status == PositionStatus.OPEN.value)
                )
            )
            .scalars()
            .all()
        )
        return set(rows)

    async def close_position(
        self,
        position_id: int,
        *,
        exit_price: Decimal,
        exit_reason: str,
        realized_pnl: Decimal,
        closed_at: dt.datetime | None = None,
    ) -> None:
        """Close a position. All three exit fields are required together.

        The ``ck_closed_complete`` CHECK enforces this at the database level too
        — a CLOSED row that cannot say when, at what price, or why is an
        unauditable hole in the trade record and a tax-reporting problem.
        """
        from sqlalchemy import update

        await self._session.execute(
            update(Position)
            .where(Position.id == position_id)
            .values(
                status=PositionStatus.CLOSED.value,
                closed_at=closed_at or dt.datetime.now(dt.UTC),
                exit_price=exit_price,
                exit_reason=exit_reason,
                realized_pnl=realized_pnl,
            )
        )

    async def _position_to_dict(self, row: Position) -> dict[str, Any]:
        return {
            "position_id": row.id,
            "correlation_id": row.correlation_id,
            "symbol": await self._instruments.symbol_name(row.symbol_id),
            "strategy_id": row.strategy_id,
            "slot_index": row.slot_index,
            "direction": row.direction,
            "quantity": row.quantity,
            "entry_price": row.entry_price,
            "stop_price": row.stop_price,
            "target_price": row.target_price,
            "opened_at": row.opened_at,
            "squareoff_deadline": row.squareoff_deadline,
            "status": row.status,
            "closed_at": row.closed_at,
            "exit_price": row.exit_price,
            "exit_reason": row.exit_reason,
            "realized_pnl": row.realized_pnl,
        }


# ---------------------------------------------------------------------------
# Audit reads
# ---------------------------------------------------------------------------


class DecisionLogRepository:
    """**Reads only.** Writes go through ``AuditWriter`` (E01-S05).

    The separation is not stylistic. An audit entry written inside a business
    transaction vanishes when that transaction rolls back — and a failed attempt
    is often exactly the thing you need to investigate later. ``AuditWriter``
    owns its own session factory and refuses a caller-supplied session, so
    sharing one is impossible rather than merely discouraged.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def by_correlation(self, correlation_id: uuid.UUID) -> list[dict[str, Any]]:
        """One trade, end to end. The audit explorer's main query."""
        rows = (
            (
                await self._session.execute(
                    select(DecisionLog)
                    .where(DecisionLog.correlation_id == correlation_id)
                    .order_by(DecisionLog.ts, DecisionLog.seq)
                )
            )
            .scalars()
            .all()
        )
        return [_decision_to_dict(r) for r in rows]

    async def recent_rejections(self, limit: int = 100) -> list[dict[str, Any]]:
        """ "Why isn't it trading?" — the highest-value debugging query."""
        rows = (
            (
                await self._session.execute(
                    select(DecisionLog)
                    .where(DecisionLog.outcome == "REJECT")
                    .order_by(DecisionLog.ts.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_decision_to_dict(r) for r in rows]

    async def max_seq(self) -> int:
        """The current chain head. Used by the audit writer to continue the chain."""
        value = (
            await self._session.execute(select(func.max(DecisionLog.seq)))
        ).scalar_one_or_none()
        return int(value or 0)


def _decision_to_dict(row: DecisionLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "ts": row.ts,
        "seq": row.seq,
        "correlation_id": row.correlation_id,
        "stage": row.stage,
        "symbol_id": row.symbol_id,
        "outcome": row.outcome,
        "reason_code": row.reason_code,
        "payload": row.payload,
        "latency_ms": row.latency_ms,
        "service": row.service,
        "prev_hash": row.prev_hash,
        "row_hash": row.row_hash,
    }
