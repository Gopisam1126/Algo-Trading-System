"""Data retention: archive to Parquet, verify compression, restore.

Three jobs, all nightly and none of them on the trading hot path:

1. **Archive** old bars to Parquet so the durable copy outlives whatever the
   database is doing.
2. **Verify** the columnstore/compression policy is actually converting chunks
   — a policy that silently stops working is invisible until the disk fills.
3. **Restore** from archive, because an archive nobody has ever restored from
   is a hypothesis, not a backup.

**Archive before delete, verified, always.** :func:`archive_bars` writes and
re-reads the Parquet file, comparing row counts, *before* it returns. Nothing
here deletes anything: :func:`purge_archived_bars` is a separate call that
refuses to run unless a verified archive for that range already exists. Losing
market data is unrecoverable — it cannot be re-derived, and for a strategy
backtest the gap is permanent.

Parquet rather than CSV: columnar, compressed roughly 10x on this shape of
data, preserves types (a CSV would turn every ``Decimal`` back into a string
and every timestamp into an ambiguous one), and is readable by pandas, DuckDB
and Polars without a loader.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

#: Parquet columns for `ohlcv`, in a fixed order. Fixed because the restore
#: path reads by name but a stable order keeps files diffable and makes a
#: schema change obvious in review.
_BAR_COLUMNS = (
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


class RetentionError(RuntimeError):
    """Raised when an archive or purge operation cannot proceed safely."""


@dataclass(frozen=True, slots=True)
class ArchiveResult:
    """What an archive run did, and whether it was verified."""

    path: Path
    rows_written: int
    rows_verified: int
    start: dt.datetime
    end: dt.datetime

    @property
    def verified(self) -> bool:
        """True only if the file was read back and matched what was written."""
        return self.rows_written == self.rows_verified and self.rows_written > 0


def archive_path(root: Path | str, start: dt.datetime, end: dt.datetime) -> Path:
    """Deterministic file name for a range.

    Deterministic so a re-run overwrites rather than accumulating near-duplicate
    files that nobody can later tell apart.
    """
    return Path(root) / f"ohlcv_{start.date().isoformat()}_{end.date().isoformat()}.parquet"


async def archive_bars(
    session: AsyncSession,
    root: Path | str,
    *,
    start: dt.datetime,
    end: dt.datetime,
) -> ArchiveResult:
    """Write bars in ``[start, end)`` to Parquet and verify by reading back.

    The read-back is not ceremony. A write that reports success having produced
    a truncated or unreadable file is exactly the failure an archive must not
    have, and it is only detectable by reading. The verification happens here so
    that :func:`purge_archived_bars` has something trustworthy to check.
    """
    rows = (
        await session.execute(
            text(
                "SELECT symbol_id, timeframe, ts, open, high, low, close, volume, "
                "       trade_count, vwap, is_adjusted, synthetic "
                "FROM ohlcv WHERE ts >= :start AND ts < :end ORDER BY symbol_id, timeframe, ts"
            ),
            {"start": start, "end": end},
        )
    ).all()

    path = archive_path(root, start, end)
    path.parent.mkdir(parents=True, exist_ok=True)

    if not rows:
        raise RetentionError(
            f"no bars in [{start.isoformat()}, {end.isoformat()}) — refusing to write an "
            f"empty archive, which would later look like a verified archive of nothing "
            f"and could authorise a purge"
        )

    columns: dict[str, list[Any]] = {name: [] for name in _BAR_COLUMNS}
    for row in rows:
        for name, value in zip(_BAR_COLUMNS, row, strict=True):
            # Decimal is preserved as string, not float. A float round-trip
            # loses exactness and would silently corrupt every archived price —
            # the one thing this file exists to preserve faithfully.
            columns[name].append(str(value) if _is_decimal(value) else value)

    table = pa.table(columns)
    pq.write_table(table, path, compression="zstd")

    verified = pq.read_table(path).num_rows
    result = ArchiveResult(
        path=path,
        rows_written=len(rows),
        rows_verified=verified,
        start=start,
        end=end,
    )
    if not result.verified:
        raise RetentionError(
            f"archive verification FAILED for {path}: wrote {len(rows)} rows but read "
            f"back {verified}. The file is not trustworthy and nothing has been purged."
        )
    log.info("archived %d bars to %s", result.rows_written, path)
    return result


def _is_decimal(value: object) -> bool:
    from decimal import Decimal

    return isinstance(value, Decimal)


def read_archive(path: Path | str) -> list[dict[str, Any]]:
    """Read an archive back into plain dicts."""
    table = pq.read_table(path)
    rows: list[dict[str, Any]] = table.to_pylist()
    return rows


async def restore_bars(session: AsyncSession, path: Path | str) -> int:
    """Restore an archive into ``ohlcv``. Idempotent.

    ``ON CONFLICT DO NOTHING``, not ``DO UPDATE``: a restore must never
    overwrite data that is currently live. If a row already exists, the live
    copy is authoritative — the archive is older by definition.
    """
    records = read_archive(path)
    if not records:
        return 0

    restored = 0
    for start in range(0, len(records), 5_000):
        chunk = records[start : start + 5_000]
        await session.execute(
            text(
                "INSERT INTO ohlcv (symbol_id, timeframe, ts, open, high, low, close, "
                "                   volume, trade_count, vwap, is_adjusted, synthetic) "
                "VALUES (:symbol_id, :timeframe, :ts, CAST(:open AS NUMERIC), "
                "        CAST(:high AS NUMERIC), CAST(:low AS NUMERIC), "
                "        CAST(:close AS NUMERIC), :volume, :trade_count, "
                "        CAST(:vwap AS NUMERIC), :is_adjusted, :synthetic) "
                "ON CONFLICT (symbol_id, timeframe, ts) DO NOTHING"
            ),
            chunk,
        )
        restored += len(chunk)
    return restored


async def purge_archived_bars(
    session: AsyncSession,
    root: Path | str,
    *,
    start: dt.datetime,
    end: dt.datetime,
) -> int:
    """Delete bars in ``[start, end)`` — **only** if a verified archive exists.

    This is the only function here that destroys anything, and it refuses to
    act on trust. It re-reads the archive file and compares its row count
    against what is in the database before deleting a single row. Market data
    cannot be re-derived; a gap is permanent and silently corrupts every future
    backtest over that window.
    """
    path = archive_path(root, start, end)
    if not path.exists():
        raise RetentionError(
            f"refusing to purge [{start.isoformat()}, {end.isoformat()}): no archive at "
            f"{path}. Archive first."
        )

    archived_rows = pq.read_table(path).num_rows
    live_rows = (
        await session.execute(
            text("SELECT count(*) FROM ohlcv WHERE ts >= :start AND ts < :end"),
            {"start": start, "end": end},
        )
    ).scalar_one()

    if archived_rows < live_rows:
        raise RetentionError(
            f"refusing to purge: the archive at {path} holds {archived_rows} rows but "
            f"the database holds {live_rows} in the same range. Re-archive before purging."
        )

    result = await session.execute(
        text("DELETE FROM ohlcv WHERE ts >= :start AND ts < :end"),
        {"start": start, "end": end},
    )
    deleted = int(cast("CursorResult[Any]", result).rowcount or 0)
    log.warning("purged %d bars from [%s, %s) after verifying %s", deleted, start, end, path)
    return deleted


# ---------------------------------------------------------------------------
# Compression policy health
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompressionStatus:
    """Whether the columnstore policy is doing anything."""

    hypertable: str
    total_chunks: int
    compressed_chunks: int
    policy_scheduled: bool
    last_run_success: bool | None

    @property
    def healthy(self) -> bool:
        """A scheduled policy that has never failed.

        Zero compressed chunks is NOT unhealthy on a young database — nothing
        is old enough to qualify yet. What matters is that the policy exists
        and its last run did not fail.
        """
        return self.policy_scheduled and self.last_run_success is not False


async def compression_status(session: AsyncSession, hypertable: str) -> CompressionStatus:
    """Report whether the compression policy is scheduled and succeeding.

    Worth checking nightly. A policy that silently stops converting chunks
    produces no error at all — the first symptom is the disk filling up weeks
    later, by which time the cause is far away.
    """
    chunks = (
        await session.execute(
            text(
                "SELECT count(*) FILTER (WHERE TRUE), "
                "       count(*) FILTER (WHERE is_compressed) "
                "FROM timescaledb_information.chunks WHERE hypertable_name = :name"
            ),
            {"name": hypertable},
        )
    ).first()
    total, compressed = (int(chunks[0]), int(chunks[1])) if chunks else (0, 0)

    job = (
        await session.execute(
            text(
                "SELECT j.job_id, s.last_run_status "
                "FROM timescaledb_information.jobs j "
                "LEFT JOIN timescaledb_information.job_stats s ON s.job_id = j.job_id "
                "WHERE j.hypertable_name = :name AND j.proc_name LIKE '%compress%'"
            ),
            {"name": hypertable},
        )
    ).first()

    last_success: bool | None = None
    if job is not None and job[1] is not None:
        last_success = str(job[1]).lower() == "success"

    return CompressionStatus(
        hypertable=hypertable,
        total_chunks=total,
        compressed_chunks=compressed,
        policy_scheduled=job is not None,
        last_run_success=last_success,
    )
