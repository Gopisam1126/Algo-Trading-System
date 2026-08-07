"""Tamper-evident audit log. **Safety-critical.**

This is the record of every decision the system made and why. It is what a
regulator, a tax assessment, or a post-mortem reads. Three properties make it
worth trusting, and each is enforced structurally rather than by convention:

**1. It cannot be modified or deleted.** The application role has ``INSERT``
but no ``UPDATE`` and no ``DELETE`` on ``decision_log`` (see the role-grants
migration). Append-only by convention is a promise; append-only by a missing
grant is a guarantee.

**2. Tampering is detectable even by someone who can bypass the grants.** Each
row hashes the previous row's hash, so altering any historical row invalidates
every hash after it. A DBA with superuser rights can still change a row — but
they cannot do so *undetectably* without recomputing the entire remaining
chain, and the verifier names the first divergent row.

**3. It survives the database being down.** Entries are buffered to disk and
replayed on recovery. A failed attempt is often exactly what you need to
investigate later, and "the database was down" is precisely when interesting
failures happen.

**The writer owns its own session and refuses a caller's.** That is deliberate
and is the reason :class:`AuditWriter` takes a session *factory*, never a
session. An audit entry written inside a business transaction disappears when
that transaction rolls back::

    # WRONG — the audit vanishes with the rollback
    async with session.begin():
        await audit.write(stage=RISK_CHECK, outcome=REJECT, ...)
        await orders.insert(...)      # raises -> the record of the rejection is gone

Making the dependency a factory means sharing a transaction is impossible, not
merely discouraged.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = logging.getLogger(__name__)

#: The genesis row's predecessor. 64 hex zeroes — the same width as a SHA-256
#: digest, so the chain is uniform from the first row.
GENESIS_HASH: Final = "0" * 64

#: Advisory lock key. Transaction-scoped (``_xact_``) so a crashed writer
#: cannot leak the lock — it releases on commit OR rollback, with no timeout to
#: tune and no cleanup path to forget.
_CHAIN_LOCK_SQL: Final = "SELECT pg_advisory_xact_lock(hashtext('audit_chain'))"


#: Every SELECT the verifier can issue, written out in full. Keyed by
#: (has_start, has_end). No interpolation means no injection surface to reason
#: about — see :func:`verify_chain`.
_SELECT_CHAIN: Final = (
    "SELECT seq, ts, correlation_id, stage, outcome, payload, prev_hash, row_hash FROM decision_log"
)
_WHERE_CLAUSES: Final[dict[tuple[bool, bool], str]] = {
    (False, False): "",
    (True, False): " WHERE ts >= :start",
    (False, True): " WHERE ts < :end",
    (True, True): " WHERE ts >= :start AND ts < :end",
}


class AuditError(RuntimeError):
    """Raised when the audit layer is used incorrectly."""


def _reject_non_string_keys(value: Any, path: str = "payload") -> None:
    """Refuse payloads whose keys are not strings, at any depth.

    ``json.dumps`` coerces non-string keys, so ``{1: "a"}`` and ``{"1": "a"}``
    serialise identically — two different payloads with one hash. Worse,
    ``{1: "a", "1": "b"}`` emits a duplicate key. Both are canonicalisation
    defects in a structure whose entire purpose is to be canonical, so they are
    rejected at the boundary rather than papered over.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise AuditError(
                    f"{path} contains a non-string key {key!r} ({type(key).__name__}). "
                    f"JSON coerces it to a string, so {{1: 'a'}} and {{'1': 'a'}} would "
                    f"hash identically — an ambiguity the audit chain must not have."
                )
            _reject_non_string_keys(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_non_string_keys(item, f"{path}[{index}]")


def canonical_json(payload: dict[str, Any]) -> str:
    """Deterministic JSON — the same input always produces the same bytes.

    ``sort_keys`` and the compact separators are not style choices. Python's
    dict ordering is insertion-ordered, so the same logical payload built by two
    code paths would serialise differently and hash differently, and the chain
    would fail verification for no reason at all. ``default=str`` keeps
    ``Decimal``, ``datetime`` and ``UUID`` serialisable without silently
    converting money to float.

    Non-string keys are refused — see :func:`_reject_non_string_keys`.
    """
    _reject_non_string_keys(payload)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _length_prefixed(*fields: str) -> str:
    """Join fields so their boundaries are unambiguous.

    **This is a security control, not formatting.**

    The original construction was ``"|".join(fields)``. Because ``|`` was not
    escaped and the fields are variable length, a field containing ``|`` could
    shift a boundary and two *different* rows would hash identically::

        stage="SIGNAL"        outcome="ALLOW|X"   ->  "SIGNAL|ALLOW|X"
        stage="SIGNAL|ALLOW"  outcome="X"         ->  "SIGNAL|ALLOW|X"

    Both produced the same digest, which defeats the whole point of the chain:
    an attacker able to influence any field could substitute one record for
    another without breaking it. This is the classic concatenation-ambiguity
    defect — the same class as CVE-2012-2459's Merkle-tree ambiguity — and the
    standard remedy is length-prefixing, which makes the encoding injective:
    ``len(field)`` cannot be confused with the field's own content because it is
    read first and fixes exactly how many characters follow.

    ``AUDITv2`` is a domain-separation tag. It also makes the format change
    explicit: digests produced by the old construction will not verify under
    this one, which is correct — they were computed with a broken encoding.
    """
    parts = [f"{len(field)}:{field}" for field in fields]
    return "AUDITv2|" + "".join(parts)


def compute_row_hash(
    *,
    prev_hash: str,
    seq: int,
    ts: dt.datetime,
    correlation_id: uuid.UUID | str,
    stage: str,
    outcome: str,
    payload: dict[str, Any],
) -> str:
    """Hash one row, including its predecessor. This is the chain link.

    Field order is fixed and must never change: it is part of the on-disk
    format, and reordering would invalidate every historical row.

    The timestamp is normalised to UTC ISO-8601 so a row hashed on a machine in
    one timezone verifies on a machine in another.
    """
    if ts.tzinfo is None:
        raise AuditError("ts must be timezone-aware — a naive timestamp hashes ambiguously")

    material = _length_prefixed(
        prev_hash,
        str(seq),
        ts.astimezone(dt.UTC).isoformat(),
        str(correlation_id),
        stage,
        outcome,
        canonical_json(payload),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One decision, before it has a place in the chain."""

    correlation_id: uuid.UUID
    stage: str
    outcome: str
    service: str
    payload: dict[str, Any]
    symbol_id: int | None = None
    reason_code: str | None = None
    latency_ms: int | None = None
    ts: dt.datetime | None = None

    def as_buffer_line(self) -> str:
        return json.dumps(
            {
                "correlation_id": str(self.correlation_id),
                "stage": self.stage,
                "outcome": self.outcome,
                "service": self.service,
                "payload": self.payload,
                "symbol_id": self.symbol_id,
                "reason_code": self.reason_code,
                "latency_ms": self.latency_ms,
                "ts": (self.ts or dt.datetime.now(dt.UTC)).astimezone(dt.UTC).isoformat(),
            },
            default=str,
        )

    @classmethod
    def from_buffer_line(cls, line: str) -> AuditEntry:
        raw = json.loads(line)
        return cls(
            correlation_id=uuid.UUID(raw["correlation_id"]),
            stage=raw["stage"],
            outcome=raw["outcome"],
            service=raw["service"],
            payload=raw["payload"],
            symbol_id=raw.get("symbol_id"),
            reason_code=raw.get("reason_code"),
            latency_ms=raw.get("latency_ms"),
            ts=dt.datetime.fromisoformat(raw["ts"]),
        )


class AuditWriter:
    """Appends to the chain, buffering to disk when the database is unavailable.

    Takes a session **factory**, never a session — see the module docstring.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        buffer_dir: Path | str = "data/audit_buffer",
    ) -> None:
        self._factory = session_factory
        self._buffer_dir = Path(buffer_dir)
        self._degraded = False
        # Serialises this process's writers before they reach the database.
        # The advisory lock is what makes the chain safe ACROSS processes; this
        # avoids pointless contention on it within one.
        self._local_lock = asyncio.Lock()

    @property
    def degraded(self) -> bool:
        """True when entries are going to disk instead of the database."""
        return self._degraded

    async def write(self, entry: AuditEntry) -> str | None:
        """Append one entry. Returns its ``row_hash``, or ``None`` if buffered.

        **Never raises on a database failure.** An audit write that brought down
        the caller would mean a database blip stops trading *and* loses the
        record of why. Instead the entry goes to disk, a degraded flag is set,
        and the failure is logged loudly.
        """
        stamped = entry if entry.ts is not None else _with_now(entry)
        try:
            async with self._local_lock:
                row_hash = await self._insert(stamped)
            if self._degraded:
                log.warning("audit database recovered; replaying buffered entries")
                self._degraded = False
                await self.replay_buffer()
            return row_hash
        except Exception:
            log.error(
                "AUDIT WRITE FAILED — buffering to disk. The system is running "
                "without a live audit trail.",
                exc_info=True,
            )
            self._degraded = True
            self._buffer(stamped)
            return None

    async def _insert(self, entry: AuditEntry) -> str:
        """One entry, one transaction, holding the chain lock.

        The lock is taken **before** reading the head. Read-then-lock would let
        two writers observe the same ``prev_hash`` and fork the chain — and a
        forked chain fails verification without any tampering having occurred.
        """
        assert entry.ts is not None
        session = self._factory()
        try:
            async with session.begin():
                await session.execute(text(_CHAIN_LOCK_SQL))

                head = (
                    await session.execute(
                        text("SELECT seq, row_hash FROM decision_log ORDER BY seq DESC LIMIT 1")
                    )
                ).first()
                prev_seq, prev_hash = (head[0], head[1]) if head else (0, GENESIS_HASH)
                seq = int(prev_seq) + 1

                row_hash = compute_row_hash(
                    prev_hash=prev_hash,
                    seq=seq,
                    ts=entry.ts,
                    correlation_id=entry.correlation_id,
                    stage=entry.stage,
                    outcome=entry.outcome,
                    payload=entry.payload,
                )

                await session.execute(
                    text(
                        "INSERT INTO decision_log "
                        "(ts, seq, correlation_id, stage, symbol_id, outcome, "
                        " reason_code, payload, latency_ms, service, prev_hash, row_hash) "
                        "VALUES (:ts, :seq, :correlation_id, :stage, :symbol_id, :outcome, "
                        " :reason_code, CAST(:payload AS JSONB), :latency_ms, :service, "
                        " :prev_hash, :row_hash)"
                    ),
                    {
                        "ts": entry.ts,
                        "seq": seq,
                        "correlation_id": str(entry.correlation_id),
                        "stage": entry.stage,
                        "symbol_id": entry.symbol_id,
                        "outcome": entry.outcome,
                        "reason_code": entry.reason_code,
                        "payload": canonical_json(entry.payload),
                        "latency_ms": entry.latency_ms,
                        "service": entry.service,
                        "prev_hash": prev_hash,
                        "row_hash": row_hash,
                    },
                )
                return row_hash
        finally:
            await session.close()

    # -- disk buffer --------------------------------------------------------

    def _buffer_path(self, when: dt.datetime | None = None) -> Path:
        day = (when or dt.datetime.now(dt.UTC)).astimezone(dt.UTC).date()
        return self._buffer_dir / f"{day.isoformat()}.jsonl"

    def _buffer(self, entry: AuditEntry) -> None:
        """Append to the disk buffer, fsynced per line.

        fsync per line costs throughput and is worth it: the buffer exists
        precisely for the case where things are failing, and an entry sitting in
        the OS page cache when the machine loses power is an entry that was
        never written. Buffered entries carry **no hash** — the chain is
        computed at replay time, in buffer order, because their position in the
        chain is not knowable until the database is back.
        """
        self._buffer_dir.mkdir(parents=True, exist_ok=True)
        path = self._buffer_path(entry.ts)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(entry.as_buffer_line() + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    async def replay_buffer(self) -> int:
        """Replay buffered entries into the chain, in order. Returns how many.

        A buffer file is deleted only after **every** line in it has been
        committed. If replay fails partway, the file stays and replay is
        retried — at worst producing duplicate entries, which is recoverable.
        Deleting first and failing after would lose them permanently, which is
        not.
        """
        if not self._buffer_dir.exists():
            return 0

        replayed = 0
        for path in sorted(self._buffer_dir.glob("*.jsonl")):
            lines = path.read_text(encoding="utf-8").splitlines()
            try:
                for line in lines:
                    if line.strip():
                        await self._insert(AuditEntry.from_buffer_line(line))
                        replayed += 1
            except Exception:
                log.error("replay of %s failed — keeping the file for retry", path, exc_info=True)
                return replayed
            path.unlink()
        return replayed

    def buffered_count(self) -> int:
        """How many entries are sitting on disk unreplayed."""
        if not self._buffer_dir.exists():
            return 0
        return sum(
            len([ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()])
            for p in self._buffer_dir.glob("*.jsonl")
        )


def _with_now(entry: AuditEntry) -> AuditEntry:
    from dataclasses import replace

    return replace(entry, ts=dt.datetime.now(dt.UTC))


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Outcome of walking the chain."""

    verified: bool
    rows_checked: int
    first_bad_seq: int | None = None
    first_bad_ts: dt.datetime | None = None
    detail: str = ""

    def __bool__(self) -> bool:
        return self.verified


async def verify_chain(
    session: AsyncSession,
    *,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
) -> VerificationResult:
    """Recompute every hash and report the first divergence.

    Two distinct failures are detected, and the distinction matters:

    - **A modified row** — the recomputed hash differs from the stored one.
    - **A deleted row** — the chain is intact either side but ``prev_hash`` does
      not match its predecessor's ``row_hash``, or ``seq`` skips.

    A per-``correlation_id`` chain would be simpler and lock-free but could not
    detect the second case, and deleting an entire trade's worth of entries is
    exactly the tampering worth defending against.
    """
    # The four possible queries are written out in full rather than assembled by
    # string interpolation. There are only four, and a lookup table has no SQL
    # injection surface at all — which is a better answer than interpolating and
    # then justifying why it happens to be safe. The date values are still bound
    # parameters.
    params: dict[str, dt.datetime] = {}
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    where = _WHERE_CLAUSES[(start is not None, end is not None)]
    sql = _SELECT_CHAIN + where + " ORDER BY seq"
    rows = (await session.execute(text(sql), params)).all()

    if not rows:
        return VerificationResult(verified=True, rows_checked=0, detail="chain is empty")

    expected_prev = None
    expected_seq = None
    for row in rows:
        seq, ts, correlation_id, stage, outcome, payload, prev_hash, row_hash = row

        if expected_seq is not None and seq != expected_seq:
            return VerificationResult(
                verified=False,
                rows_checked=len(rows),
                first_bad_seq=int(seq),
                first_bad_ts=ts,
                detail=f"sequence gap: expected seq {expected_seq}, found {seq} — rows deleted",
            )

        if expected_prev is not None and prev_hash != expected_prev:
            return VerificationResult(
                verified=False,
                rows_checked=len(rows),
                first_bad_seq=int(seq),
                first_bad_ts=ts,
                detail=f"broken link at seq {seq}: prev_hash does not match the previous row",
            )

        recomputed = compute_row_hash(
            prev_hash=prev_hash,
            seq=int(seq),
            ts=ts,
            correlation_id=correlation_id,
            stage=stage,
            outcome=outcome,
            payload=payload,
        )
        if recomputed != row_hash:
            return VerificationResult(
                verified=False,
                rows_checked=len(rows),
                first_bad_seq=int(seq),
                first_bad_ts=ts,
                detail=f"row {seq} has been modified: stored hash does not match its contents",
            )

        expected_prev = row_hash
        expected_seq = int(seq) + 1

    return VerificationResult(
        verified=True, rows_checked=len(rows), detail=f"{len(rows)} rows verified"
    )
