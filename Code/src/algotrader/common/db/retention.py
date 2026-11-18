"""Data retention: archive to Parquet, verify compression, restore.

Three jobs, all nightly and none of them on the trading hot path:

1. **Archive** old bars to Parquet so the durable copy outlives whatever the
   database is doing.
2. **Verify** the columnstore/compression policy is actually converting chunks
   — a policy that silently stops working is invisible until the disk fills.
3. **Restore** from archive, because an archive nobody has ever restored from
   is a hypothesis, not a backup.

**Archive before delete, verified, always.** :func:`archive_bars` writes and
re-reads the Parquet file, comparing its content, *before* it returns, and
never replaces an archive with one that holds less. Nothing here deletes
anything: :func:`purge_archived_bars` is a separate call that deletes only bars
it can show are in the archive of exactly that range, by value. Losing market
data is unrecoverable — it cannot be re-derived, and for a strategy backtest
the gap is permanent. (Audit ENG-006, 17 Nov 2026: the first version named an
archive by date, overwrote it in place, and authorised a purge by row count.)

Parquet rather than CSV: columnar, compressed roughly 10x on this shape of
data, preserves types (a CSV would turn every ``Decimal`` back into a string
and every timestamp into an ambiguous one), and is readable by pandas, DuckDB
and Polars without a loader.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

from algotrader.common.db.corporate_actions import CorporateActionRepository

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
    "price_adj_factor",
    "volume_adj_factor",
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


#: Parquet schema-metadata keys an archive carries about itself, so a purge
#: can check the file is the archive OF this range - not one with a colliding
#: name - and that its content is what was written.
_META_START: Final = b"algotrader.range_start"
_META_END: Final = b"algotrader.range_end"
_META_DIGEST: Final = b"algotrader.sha256"

_SELECT_RANGE: Final = (
    "SELECT symbol_id, timeframe, ts, open, high, low, close, volume, "
    "       trade_count, vwap, price_adj_factor, volume_adj_factor, synthetic "
    "FROM ohlcv WHERE ts >= :start AND ts < :end"
)

#: One archived bar, compared by VALUE: every column as text, the time in UTC.
RowKey = tuple[str | None, ...]


def _instant(moment: dt.datetime, name: str) -> dt.datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise RetentionError(f"{name} must be timezone-aware, got {moment!r}")
    return moment.astimezone(dt.UTC)


def _range(start: dt.datetime, end: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    start, end = _instant(start, "start"), _instant(end, "end")
    if not start < end:
        raise RetentionError(f"an archive range must have start < end; got [{start}, {end})")
    return start, end


def archive_path(root: Path | str, start: dt.datetime, end: dt.datetime) -> Path:
    """Deterministic file name for a range - its EXACT instants, in UTC.

    Deterministic so a re-run of the same range finds the same file. Exact,
    because the first version named only the DATES: [1 Jan 00:00, 8 Jan 00:00)
    and [1 Jan 00:00, 8 Jan 12:00) were the same file, so archiving the second
    after purging the first overwrote the only copy of the purged bars (audit
    ENG-006, 17 Nov 2026).
    """
    start, end = _range(start, end)
    stamp = "%Y%m%dT%H%M%S%fZ"
    return Path(root) / f"ohlcv_{start.strftime(stamp)}_{end.strftime(stamp)}.parquet"


def _row_key(values: Sequence[Any]) -> RowKey:
    out: list[str | None] = []
    for value in values:
        if value is None:
            out.append(None)
        elif isinstance(value, dt.datetime):
            aware = value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)
            out.append(aware.astimezone(dt.UTC).isoformat())
        elif isinstance(value, bool):
            out.append("true" if value else "false")
        else:
            out.append(str(getattr(value, "value", value)))  # an enum is its value
    return tuple(out)


def _digest(rows: Iterable[RowKey]) -> str:
    h = hashlib.sha256()
    for row in sorted(rows, key=lambda r: tuple("" if v is None else v for v in r)):
        h.update(json.dumps(row).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def _archived_rows(path: Path) -> list[RowKey]:
    table = pq.read_table(path)
    return [_row_key([row[name] for name in _BAR_COLUMNS]) for row in table.to_pylist()]


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

    **An existing archive is never made smaller** (ENG-006). A re-run that
    finds the same bars changes nothing; one that finds MORE replaces the file;
    one that lacks any bar the existing archive holds - the range was purged,
    or a bar revised - is refused, because that archive may be the only copy.
    The new file is written beside the old one and renamed over it only once
    it has been read back, so a crash mid-write leaves the previous archive
    whole.
    """
    start, end = _range(start, end)
    rows = (
        await session.execute(
            text(_SELECT_RANGE + " ORDER BY symbol_id, timeframe, ts"),
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

    keys = [_row_key(row) for row in rows]
    digest = _digest(keys)
    if path.exists():
        existing = set(_archived_rows(path))
        missing = existing - set(keys)
        if missing:
            raise RetentionError(
                f"refusing to overwrite {path}: it holds {len(missing)} bar(s) the database "
                f"no longer has in this range - it may be their only copy"
            )
        if len(existing) == len(keys):
            log.info("archive %s already holds exactly these %d bars", path, len(keys))
            return ArchiveResult(path, len(keys), len(keys), start, end)

    columns: dict[str, list[Any]] = {name: [] for name in _BAR_COLUMNS}
    for row in rows:
        for name, value in zip(_BAR_COLUMNS, row, strict=True):
            # Decimal is preserved as string, not float. A float round-trip
            # loses exactness and would silently corrupt every archived price —
            # the one thing this file exists to preserve faithfully.
            columns[name].append(str(value) if _is_decimal(value) else value)

    table = pa.table(columns).replace_schema_metadata(
        {
            _META_START: start.isoformat().encode(),
            _META_END: end.isoformat().encode(),
            _META_DIGEST: digest.encode(),
        }
    )
    partial = path.with_name(path.name + ".partial")
    pq.write_table(table, partial, compression="zstd")

    back = _archived_rows(partial)
    result = ArchiveResult(
        path=path,
        rows_written=len(rows),
        rows_verified=len(back),
        start=start,
        end=end,
    )
    if not result.verified or _digest(back) != digest:
        partial.unlink(missing_ok=True)
        raise RetentionError(
            f"archive verification FAILED for {path}: wrote {len(rows)} rows but read "
            f"back {len(back)}, or different content. The file is not trustworthy and "
            f"nothing has been purged."
        )
    os.replace(partial, path)
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

    Adjustment factors are recomputed after the insert, and that is not
    housekeeping. Archived bars are not in ``ohlcv``, so every corporate action
    recorded while they were away updated the live bars and missed them. Their
    stored factors are frozen at archive time. Restoring them as-is splices an
    unadjusted segment onto an adjusted series — a discontinuity that raises
    nothing, looks like a real price move, and corrupts every backtest crossing
    it. ``recompute_factors`` rebuilds from the full action history, so a single
    call heals the restored range no matter how many actions were missed.

    Returns the number of bars actually INSERTED - not the number in the file.
    A restore that found every bar already live inserted nothing and returns 0
    (audit ENG-030: it used to report the file's row count, so a drill that
    restored nothing read as full success). The skipped count is logged.
    """
    records = read_archive(path)
    if not records:
        return 0

    restored = 0
    for start in range(0, len(records), 5_000):
        chunk = [{k: _jsonable(v) for k, v in r.items()} for r in records[start : start + 5_000]]
        # One statement per chunk, so rowcount is exactly the rows inserted;
        # an executemany's rowcount is driver-defined.
        result = await session.execute(
            text(
                "INSERT INTO ohlcv (symbol_id, timeframe, ts, open, high, low, close, "
                "                   volume, trade_count, vwap, price_adj_factor, "
                "                   volume_adj_factor, synthetic) "
                "SELECT r.symbol_id, r.timeframe, r.ts, r.open, "
                "       r.high, r.low, r.close, r.volume, r.trade_count, r.vwap, "
                "       r.price_adj_factor, r.volume_adj_factor, r.synthetic "
                "FROM jsonb_to_recordset(CAST(:rows AS JSONB)) AS r("
                "    symbol_id integer, timeframe text, ts timestamptz, open numeric, "
                "    high numeric, low numeric, close numeric, volume bigint, "
                "    trade_count integer, vwap numeric, price_adj_factor numeric, "
                "    volume_adj_factor numeric, synthetic boolean) "
                "ON CONFLICT (symbol_id, timeframe, ts) DO NOTHING"
            ),
            {"rows": json.dumps(chunk)},
        )
        restored += int(cast("CursorResult[Any]", result).rowcount or 0)

    symbol_ids = sorted({int(r["symbol_id"]) for r in records})
    await CorporateActionRepository(session).recompute_all(symbol_ids)
    log.info(
        "restored %d of %d bars from %s (%d already live, kept) and recomputed factors "
        "for %d symbols",
        restored,
        len(records),
        path,
        len(records) - restored,
        len(symbol_ids),
    )
    return restored


def _jsonable(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)
        return aware.isoformat()
    return value


async def purge_archived_bars(
    session: AsyncSession,
    root: Path | str,
    *,
    start: dt.datetime,
    end: dt.datetime,
) -> int:
    """Delete bars in ``[start, end)`` — **only** if a verified archive exists.

    This is the only function here that destroys anything, and it refuses to
    act on trust. Market data cannot be re-derived; a gap is permanent and
    silently corrupts every future backtest over that window.

    **Every bar deleted is proven to be in the archive, by value** (ENG-006).
    The first version compared row COUNTS: an archive holding as many rows as
    the range - any rows - authorised the purge. Now:

    - the archive must record this exact range and still hash to what was
      written (a file with a colliding name, or a damaged one, is refused);
    - the DELETE returns what it deleted, inside a savepoint, and unless every
      one of those bars is in the archive - same key, same values - the
      savepoint is rolled back and nothing is deleted. Checking first and
      deleting after would delete a bar that arrived in between, unarchived.
    """
    start, end = _range(start, end)
    path = archive_path(root, start, end)
    if not path.exists():
        raise RetentionError(
            f"refusing to purge [{start.isoformat()}, {end.isoformat()}): no archive at "
            f"{path}. Archive first."
        )

    meta = pq.read_schema(path).metadata or {}
    recorded = (meta.get(_META_START), meta.get(_META_END))
    if recorded != (start.isoformat().encode(), end.isoformat().encode()):
        raise RetentionError(
            f"refusing to purge: {path} does not record the range "
            f"[{start.isoformat()}, {end.isoformat()}). Re-archive before purging."
        )
    archived = _archived_rows(path)
    if _digest(archived).encode() != meta.get(_META_DIGEST):
        raise RetentionError(
            f"refusing to purge: {path} no longer hashes to what was written. "
            f"Re-archive before purging."
        )
    held = set(archived)

    async with session.begin_nested():
        returned = (
            await session.execute(
                text(
                    "DELETE FROM ohlcv WHERE ts >= :start AND ts < :end RETURNING "
                    "symbol_id, timeframe, ts, open, high, low, close, volume, "
                    "trade_count, vwap, price_adj_factor, volume_adj_factor, synthetic"
                ),
                {"start": start, "end": end},
            )
        ).all()
        unarchived = [row for row in returned if _row_key(row) not in held]
        if unarchived:
            raise RetentionError(
                f"refusing to purge: {len(unarchived)} of the {len(returned)} bars in the "
                f"range are not in the archive at {path} as they are now. Re-archive "
                f"before purging. Nothing was deleted."
            )
    deleted = len(returned)
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
