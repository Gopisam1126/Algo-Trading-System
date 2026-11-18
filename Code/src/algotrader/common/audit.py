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
failures happen. Only an OUTAGE is buffered: an entry that is wrong is
refused before the database is asked (it raises ``AuditError`` to the
caller - a programming error), and one the database refuses on its merits is
quarantined, never left to wedge the replay of everything behind it. A
buffer directory belongs to ONE writer: replay and buffering are serialised
by that writer's lock, which another process does not hold.

**What the chain does NOT prove on its own.** The hash is unkeyed, so an
actor who can rewrite rows can recompute every later hash, and a chain cut
short at either end is still a chain. :func:`verify_chain` anchors the first
row to :data:`GENESIS_HASH`, and detects a truncated or rewritten tail only
against a head (:func:`chain_head`) recorded somewhere the database's writers
cannot reach (audit SEC-005).

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
import math
import os
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from sqlalchemy import exc as sa_exc
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


#: At and above this magnitude Python spells a float with an exponent
#: (``1e+16``) while PostgreSQL's ``numeric`` - which is what JSONB stores a
#: number as - spells it as an integer (``10000000000000000``), which
#: ``json.loads`` then reads back as an ``int``. Every float this large is
#: integral, so it is written as the int it equals and both sides agree.
_FLOAT_SPELLED_WITH_EXPONENT: Final = 1e16


def _canonical_value(value: Any, path: str) -> Any:
    """The payload as JSON can hold it AND as JSONB will hand it back.

    The chain is verified by re-hashing what the database returns, so any
    value whose spelling JSONB changes would fail verification of a chain
    nobody touched (audit NR-008). Three cases, each refused or normalised
    here rather than discovered by a false tamper alarm:

    - **Non-string keys are refused.** ``json.dumps`` coerces them, so
      ``{1: "a"}`` and ``{"1": "a"}`` serialise identically - two payloads,
      one hash - and ``{1: "a", "1": "b"}`` emits a duplicate key.
    - **Non-finite floats are refused.** ``json.dumps`` writes ``NaN`` and
      ``Infinity``, which are not JSON; JSONB rejects them, so an entry
      carrying one could never be inserted (audit ENG-011).
    - **Two floats are respelled**: ``-0.0`` (``numeric`` has no negative
      zero, so it comes back ``0.0``) and anything at or above 1e16 (see
      :data:`_FLOAT_SPELLED_WITH_EXPONENT`). Both are respelled as the value
      they equal, so nothing is lost.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise AuditError(
                    f"{path} contains a non-string key {key!r} ({type(key).__name__}). "
                    f"JSON coerces it to a string, so {{1: 'a'}} and {{'1': 'a'}} would "
                    f"hash identically — an ambiguity the audit chain must not have."
                )
            out[key] = _canonical_value(item, f"{path}.{key}")
        return out
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item, f"{path}[{i}]") for i, item in enumerate(value)]
    if isinstance(value, float) and not isinstance(value, bool):
        if not math.isfinite(value):
            raise AuditError(
                f"{path} is {value!r}: JSON has no non-finite numbers and JSONB refuses "
                f"them, so this entry could never be written"
            )
        if value == 0.0:
            return 0.0  # -0.0 included: numeric has no negative zero
        if abs(value) >= _FLOAT_SPELLED_WITH_EXPONENT:
            return int(value)
    return value


def canonical_json(payload: dict[str, Any]) -> str:
    """Deterministic JSON — the same input always produces the same bytes.

    ``sort_keys`` and the compact separators are not style choices. Python's
    dict ordering is insertion-ordered, so the same logical payload built by two
    code paths would serialise differently and hash differently, and the chain
    would fail verification for no reason at all. ``default=str`` keeps
    ``Decimal``, ``datetime`` and ``UUID`` serialisable without silently
    converting money to float.

    Refuses what the chain could not hold — see :func:`_canonical_value`.
    It is idempotent over its own output: ``canonical_json(json.loads(s)) ==
    s``, which is the property verification depends on.
    """
    return json.dumps(
        _canonical_value(payload, "payload"),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    )


def _is_outage(exc: BaseException) -> bool:
    """True when the database could not be REACHED, not when it refused the entry.

    Only an outage is buffered (audit ENG-011). An entry the database refuses
    on its merits - a constraint, a bad value - would be refused again on
    every replay, so buffering it would wedge the replay of everything behind
    it; it is quarantined instead, and the writer is not "degraded", because
    the database is up.
    """
    if isinstance(exc, (sa_exc.OperationalError, sa_exc.InterfaceError, sa_exc.TimeoutError)):
        return True
    if isinstance(exc, sa_exc.DisconnectionError):
        return True
    if isinstance(exc, sa_exc.DBAPIError) and exc.connection_invalidated:
        return True
    # OSError covers ConnectionError; TimeoutError is asyncio's too on 3.11.
    return isinstance(exc, (OSError, TimeoutError))


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
        # True until a replay has completed in THIS process, so entries a
        # crashed predecessor left on disk drain on the first good write
        # rather than waiting for a later outage (audit ENG-010).
        self._replay_due = True
        # Serialises this process's writes, buffering AND replay. The advisory
        # lock is what makes the chain safe across processes; this lock is what
        # makes the buffer safe: a replay that read a file and awaited the
        # database while another write appended to it then deleted the file,
        # and the appended entry with it (audit ENG-010).
        self._local_lock = asyncio.Lock()

    @property
    def degraded(self) -> bool:
        """True when entries are going to disk instead of the database."""
        return self._degraded

    async def write(self, entry: AuditEntry) -> str | None:
        """Append one entry. Returns its ``row_hash``, or ``None`` if it did not land.

        **Never raises on a database failure.** An audit write that brought down
        the caller would mean a database blip stops trading *and* loses the
        record of why. An outage buffers the entry to disk and sets
        :attr:`degraded`; an entry the database refuses on its merits is
        quarantined (:meth:`quarantined_count`). Both are logged loudly, and
        differently, because they mean different things.

        **Does raise ``AuditError`` for an entry that is wrong** - a non-string
        key, a non-finite float, a naive timestamp. That is a caller's bug, it
        is refused before the database is asked, and it is never buffered: a
        buffered entry that can never be inserted only postpones the error to a
        replay, where it used to block every entry behind it (audit ENG-011).
        """
        stamped = _prepared(entry if entry.ts is not None else _with_now(entry))
        async with self._local_lock:
            if self._replay_due:
                _, complete = await self._replay_locked()
                if not complete:
                    # The database is still unreachable (the replay logged
                    # why). Go straight to disk - behind the entries already
                    # there, so order is kept - without a second attempt.
                    self._buffer_outage(stamped, cause=None)
                    return None
            try:
                row_hash = await self._insert(stamped)
            except Exception as exc:
                if not _is_outage(exc):
                    self._quarantine(stamped.as_buffer_line(), exc)
                    return None
                self._buffer_outage(stamped, cause=exc)
                return None
            if self._degraded:
                log.warning("audit database recovered; buffered entries replayed")
                self._degraded = False
            return row_hash

    def _buffer_outage(self, entry: AuditEntry, *, cause: BaseException | None) -> None:
        log.error(
            "AUDIT WRITE FAILED — the database is unreachable; buffering to disk. "
            "The system is running without a live audit trail.",
            exc_info=cause,
        )
        self._degraded = True
        self._replay_due = True
        self._buffer(entry)

    async def _insert(self, entry: AuditEntry) -> str:
        """One entry, one transaction, holding the chain lock.

        The lock is taken **before** reading the head. Read-then-lock would let
        two writers observe the same ``prev_hash`` and fork the chain — and a
        forked chain fails verification without any tampering having occurred.
        """
        if entry.ts is None:
            # NOT an assert. `AuditEntry.ts` defaults to None, so this check is
            # load-bearing rather than type narrowing — and `python -O` strips
            # asserts. A stripped check here would write a NULL timestamp into
            # a hash-chained audit log, which is the one table whose whole
            # purpose is being trustworthy after the fact.
            raise AuditError(
                "audit entry reached _insert without a timestamp; the caller "
                "must stamp it before the chain lock is taken"
            )
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

    @staticmethod
    def _progress_path(path: Path) -> Path:
        return path.with_name(path.name + ".progress")

    @property
    def _quarantine_path(self) -> Path:
        # A subdirectory, so the replay's non-recursive glob never reads it.
        return self._buffer_dir / "quarantine" / "refused.jsonl"

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
        progress = self._progress_path(path)
        if not path.exists() and progress.exists():
            # A replay finished this day's file and stopped before removing
            # its marker. Left in place it would skip this new file's lines.
            progress.unlink()
        _append_durably(path, entry.as_buffer_line())

    def _quarantine(self, line: str, exc: BaseException) -> None:
        """Keep an entry the database refused, verbatim, for an operator.

        Only the exception's TYPE is recorded: a driver's message can carry a
        DSN or the offending value, and this file is read by people.
        """
        self._quarantine_path.parent.mkdir(parents=True, exist_ok=True)
        record = json.dumps(
            {
                "refused_at": dt.datetime.now(dt.UTC).isoformat(),
                "error": type(exc).__name__,
                "line": line,
            }
        )
        _append_durably(self._quarantine_path, record)
        log.error(
            "AUDIT ENTRY REFUSED — the database is up and refused this entry (%s); "
            "it is quarantined in %s and will not be retried",
            type(exc).__name__,
            self._quarantine_path,
        )

    async def replay_buffer(self) -> int:
        """Replay buffered entries into the chain, in order. Returns how many.

        Never raises for an outage: a replay the database interrupts stops,
        keeps what is left, and reports what it did manage.
        """
        async with self._local_lock:
            replayed, _ = await self._replay_locked()
            return replayed

    async def _replay_locked(self) -> tuple[int, bool]:
        """Replay every buffer file in order. Returns (replayed, complete).

        Caller holds :attr:`_local_lock`, so no write appends to a file while
        it is read.

        **Progress is durable.** After each line is committed (or
        quarantined) the count of lines done is written beside the file, so a
        replay the database interrupts - or a crash - resumes at the next
        line instead of inserting the earlier ones twice (audit ENG-010). The
        one window left is a commit whose acknowledgement was lost, which no
        client can tell from a failure.

        **A file is deleted only once every line in it is handled.** Deleting
        first and failing after would lose them; a line the database refuses
        on its merits is quarantined and the replay goes on (audit ENG-011).
        """
        if not self._buffer_dir.exists():
            self._replay_due = False
            return 0, True
        for orphan in self._buffer_dir.glob("*.jsonl.progress"):
            if not orphan.with_name(orphan.name.removesuffix(".progress")).exists():
                orphan.unlink()

        replayed = 0
        for path in sorted(self._buffer_dir.glob("*.jsonl")):
            lines = _buffered_lines(path)
            progress = self._progress_path(path)
            done = _read_progress(progress)
            for index in range(done, len(lines)):
                line = lines[index]
                try:
                    entry = _prepared(AuditEntry.from_buffer_line(line))
                except (ValueError, KeyError, TypeError, AuditError) as exc:
                    self._quarantine(line, exc)
                else:
                    try:
                        await self._insert(entry)
                    except Exception as exc:
                        if _is_outage(exc):
                            log.error(
                                "replay of %s stopped at line %d — the database is "
                                "unreachable; the rest stays buffered",
                                path,
                                index + 1,
                                exc_info=True,
                            )
                            return replayed, False
                        self._quarantine(line, exc)
                    else:
                        replayed += 1
                _write_progress(progress, index + 1)
            path.unlink()
            progress.unlink(missing_ok=True)
        self._replay_due = False
        return replayed, True

    def buffered_count(self) -> int:
        """How many entries are sitting on disk unreplayed."""
        if not self._buffer_dir.exists():
            return 0
        return sum(
            max(0, len(_buffered_lines(p)) - _read_progress(self._progress_path(p)))
            for p in self._buffer_dir.glob("*.jsonl")
        )

    def quarantined_count(self) -> int:
        """How many entries the database refused. Non-zero needs an operator."""
        if not self._quarantine_path.exists():
            return 0
        return len(_buffered_lines(self._quarantine_path))


def _with_now(entry: AuditEntry) -> AuditEntry:
    return replace(entry, ts=dt.datetime.now(dt.UTC))


def _prepared(entry: AuditEntry) -> AuditEntry:
    """The entry as it will be hashed, inserted AND buffered - or ``AuditError``.

    The payload becomes the JSON-native value :func:`canonical_json` writes,
    so the bytes hashed now, the line buffered, and the JSONB the database
    hands back to the verifier are one value (audit ENG-011, NR-008).
    """
    if entry.ts is None or entry.ts.tzinfo is None:
        raise AuditError("an audit entry needs a timezone-aware timestamp")
    return replace(entry, payload=json.loads(canonical_json(entry.payload)))


def _append_durably(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _buffered_lines(path: Path) -> list[str]:
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _read_progress(path: Path) -> int:
    """Lines of the buffer file already handled. A marker that will not read is 0.

    Reading 0 re-replays the file: duplicates, which are visible and
    recoverable. Guessing high would skip entries, which is neither.
    """
    try:
        return max(0, int(path.read_text(encoding="utf-8").strip()))
    except (FileNotFoundError, ValueError):
        return 0


def _write_progress(path: Path, done: int) -> None:
    """Atomically: a torn marker must never read as a larger number."""
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(str(done))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


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


@dataclass(frozen=True, slots=True)
class ChainPoint:
    """One row's place in the chain: its ``seq`` and ``row_hash``.

    Recorded somewhere the database's writers cannot reach, it is what turns
    "the chain is internally consistent" into "the chain is the one we wrote"
    (audit SEC-005).
    """

    seq: int
    row_hash: str


#: The point before the first row: seq 0, the genesis hash.
GENESIS: Final = ChainPoint(seq=0, row_hash=GENESIS_HASH)


async def chain_head(session: AsyncSession) -> ChainPoint | None:
    """The newest row's place in the chain, or ``None`` for an empty chain.

    Record it OFF this host, periodically. A head kept in the same database
    proves nothing: whoever can truncate the chain can rewrite the record.
    """
    row = (
        await session.execute(
            text("SELECT seq, row_hash FROM decision_log ORDER BY seq DESC LIMIT 1")
        )
    ).first()
    return None if row is None else ChainPoint(seq=int(row[0]), row_hash=str(row[1]))


def _failed(rows: int, seq: int | None, ts: dt.datetime | None, detail: str) -> VerificationResult:
    return VerificationResult(
        verified=False, rows_checked=rows, first_bad_seq=seq, first_bad_ts=ts, detail=detail
    )


async def verify_chain(
    session: AsyncSession,
    *,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
    after: ChainPoint | None = None,
    head: ChainPoint | None = None,
) -> VerificationResult:
    """Recompute every hash and report the first divergence.

    Two distinct failures are detected, and the distinction matters:

    - **A modified row** — the recomputed hash differs from the stored one.
    - **A deleted row** — the chain is intact either side but ``prev_hash`` does
      not match its predecessor's ``row_hash``, or ``seq`` skips.

    **Both ends are anchored** (audit SEC-005; before it, deleting the first
    or last rows verified):

    - **The first row** must follow ``after``. With no window that is
      :data:`GENESIS` - seq 1, ``prev_hash`` all zeroes - so a deleted
      beginning is a failure. A window that begins mid-chain cannot vouch for
      its own first link, so it needs ``after`` from a trusted record, and
      FAILS without one rather than verifying half a chain.
    - **The last row** is checked against ``head``, a :func:`chain_head`
      recorded earlier and kept off this host: the chain must still contain
      that row, unchanged. Without one, a truncated tail is undetectable by
      construction - nothing in the remaining rows refers to the missing
      ones - and the result says so. ``head`` is refused with ``end``: a
      window that ends early has no way to tell truncation from its own end.

    A per-``correlation_id`` chain would be simpler and lock-free but could not
    detect deletions, and deleting an entire trade's worth of entries is
    exactly the tampering worth defending against.
    """
    if head is not None and end is not None:
        raise AuditError("verify_chain: `head` checks the tail, which a window with `end` excludes")
    if head is not None and after is not None and head.seq <= after.seq:
        # The head would lie before the window: never compared, while the
        # result said "tail checked against the recorded head" - or, with no
        # rows since, a truncation that did not happen (QA, 18 Nov 2026).
        raise AuditError(
            f"verify_chain: the head (seq {head.seq}) is not after the anchor (seq "
            f"{after.seq}), so the window cannot contain it"
        )
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
        if head is not None:
            return _failed(
                0, head.seq, None, f"chain is empty but seq {head.seq} was recorded — truncated"
            )
        return VerificationResult(verified=True, rows_checked=0, detail="chain is empty")

    n = len(rows)
    first_seq = int(rows[0][0])
    anchor = after if after is not None else (GENESIS if first_seq == 1 or start is None else None)
    if anchor is None:
        return _failed(
            n,
            first_seq,
            rows[0][1],
            f"the window begins at seq {first_seq} with no anchor: pass after= the trusted "
            f"ChainPoint of seq {first_seq - 1}",
        )

    expected_prev = anchor.row_hash
    expected_seq = anchor.seq + 1
    for row in rows:
        seq, ts, correlation_id, stage, outcome, payload, prev_hash, row_hash = row

        if seq != expected_seq:
            return _failed(
                n,
                int(seq),
                ts,
                f"sequence gap: expected seq {expected_seq}, found {seq} — rows deleted",
            )

        if prev_hash != expected_prev:
            return _failed(
                n,
                int(seq),
                ts,
                f"broken link at seq {seq}: prev_hash does not match the previous row",
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
            return _failed(
                n,
                int(seq),
                ts,
                f"row {seq} has been modified: stored hash does not match its contents",
            )
        if head is not None and int(seq) == head.seq and row_hash != head.row_hash:
            return _failed(
                n, int(seq), ts, f"row {seq} is not the row recorded as the head — rewritten"
            )

        expected_prev = row_hash
        expected_seq = int(seq) + 1

    last_seq = expected_seq - 1
    if head is not None and last_seq < head.seq:
        return _failed(
            n,
            last_seq + 1,
            None,
            f"the chain ends at seq {last_seq} but seq {head.seq} was recorded — truncated",
        )
    tail = (
        f"; tail checked against the recorded head (seq {head.seq})"
        if head is not None
        else "; tail NOT checked: no recorded head was given"
    )
    return VerificationResult(verified=True, rows_checked=n, detail=f"{n} rows verified{tail}")
