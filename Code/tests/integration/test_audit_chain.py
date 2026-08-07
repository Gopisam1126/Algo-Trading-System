"""E01-S05 — audit hash chain. **SAFETY-CRITICAL (🔴).**

Per DEVELOPMENT_PROCEDURE.md §4.3, a red story requires more than the usual
tests, and all four are here:

- **Property-based** over *generated sequences* — chains, orderings and
  interleavings are where these defects live, not single values.
- **Explicit tamper tests** — modification, deletion, and re-hashing.
- **Explicit outage test** — the database goes away mid-run and nothing is lost.
- **Concurrency test** — two writers must produce one valid chain, not a fork.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import itertools
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from algotrader.common.audit import (
    GENESIS_HASH,
    AuditEntry,
    AuditError,
    AuditWriter,
    canonical_json,
    compute_row_hash,
    verify_chain,
)
from algotrader.common.db import engine as db_engine

pytestmark = [pytest.mark.integration]


@pytest.fixture
async def factory(migrated_database: str) -> AsyncIterator[object]:
    eng = db_engine.create_engine_from_url(migrated_database)
    yield db_engine.create_session_factory(eng)
    await eng.dispose()


@pytest.fixture
async def session(factory: object) -> AsyncIterator[AsyncSession]:
    async with factory() as s:  # type: ignore[operator]
        await s.execute(text("DELETE FROM decision_log"))
        await s.commit()
        yield s


@pytest.fixture
def writer(factory: object, tmp_path: Path) -> AuditWriter:
    return AuditWriter(factory, buffer_dir=tmp_path / "audit_buffer")  # type: ignore[arg-type]


def entry(n: int = 0, **kw: object) -> AuditEntry:
    defaults: dict = {
        "correlation_id": uuid.uuid4(),
        "stage": "RISK_CHECK",
        "outcome": "ALLOW",
        "service": "test",
        "payload": {"n": n},
    }
    defaults.update(kw)
    return AuditEntry(**defaults)


# ---------------------------------------------------------------------------
# Hash function — pure, so testable without a database
# ---------------------------------------------------------------------------


class TestCanonicalJson:
    def test_key_order_does_not_change_the_output(self) -> None:
        """Python dicts are insertion-ordered.

        Without sort_keys, the same logical payload built by two code paths
        serialises differently, hashes differently, and the chain fails
        verification with no tampering having occurred.
        """
        assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})

    def test_output_is_compact_and_stable(self) -> None:
        assert canonical_json({"a": 1, "b": 2}) == '{"a":1,"b":2}'

    def test_decimal_does_not_become_float(self) -> None:
        """Money through the audit path must not lose precision."""
        out = canonical_json({"price": Decimal("1234.5678")})
        assert "1234.5678" in out


class TestHashFunction:
    def test_hash_is_deterministic(self) -> None:
        kw = {
            "prev_hash": GENESIS_HASH,
            "seq": 1,
            "ts": dt.datetime(2026, 8, 6, 9, 15, tzinfo=dt.UTC),
            "correlation_id": uuid.UUID(int=1),
            "stage": "SIGNAL",
            "outcome": "ALLOW",
            "payload": {"x": 1},
        }
        assert compute_row_hash(**kw) == compute_row_hash(**kw)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "field",
        ["prev_hash", "seq", "correlation_id", "stage", "outcome", "payload"],
    )
    def test_every_field_affects_the_hash(self, field: str) -> None:
        """If a field did not affect the hash, it could be altered undetectably."""
        base: dict = {
            "prev_hash": GENESIS_HASH,
            "seq": 1,
            "ts": dt.datetime(2026, 8, 6, 9, 15, tzinfo=dt.UTC),
            "correlation_id": uuid.UUID(int=1),
            "stage": "SIGNAL",
            "outcome": "ALLOW",
            "payload": {"x": 1},
        }
        altered = dict(base)
        altered[field] = {
            "prev_hash": "f" * 64,
            "seq": 2,
            "correlation_id": uuid.UUID(int=2),
            "stage": "ORDER",
            "outcome": "REJECT",
            "payload": {"x": 2},
        }[field]
        assert compute_row_hash(**base) != compute_row_hash(**altered)

    def test_timezone_is_normalised_not_ignored(self) -> None:
        """The same instant in two timezones must hash identically."""
        utc = dt.datetime(2026, 8, 6, 9, 15, tzinfo=dt.UTC)
        ist = utc.astimezone(dt.timezone(dt.timedelta(hours=5, minutes=30)))
        common: dict = {
            "prev_hash": GENESIS_HASH,
            "seq": 1,
            "correlation_id": uuid.UUID(int=1),
            "stage": "SIGNAL",
            "outcome": "ALLOW",
            "payload": {},
        }
        assert compute_row_hash(ts=utc, **common) == compute_row_hash(ts=ist, **common)

    def test_naive_timestamp_is_refused(self) -> None:
        with pytest.raises(AuditError):
            compute_row_hash(
                prev_hash=GENESIS_HASH,
                seq=1,
                ts=dt.datetime(2026, 8, 6, 9, 15),
                correlation_id=uuid.UUID(int=1),
                stage="SIGNAL",
                outcome="ALLOW",
                payload={},
            )


class TestHashChainProperties:
    """Property-based, over generated SEQUENCES — the 🔴 DoD requirement."""

    @settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        st.lists(
            st.tuples(
                st.sampled_from(["SIGNAL", "RISK_CHECK", "ORDER", "FILL", "EXIT"]),
                st.sampled_from(["ALLOW", "REJECT", "ERROR"]),
                st.dictionaries(
                    st.text(min_size=1, max_size=8),
                    st.integers() | st.text(max_size=12) | st.booleans(),
                    max_size=5,
                ),
            ),
            min_size=1,
            max_size=25,
        )
    )
    def test_a_chain_built_from_any_sequence_always_verifies(
        self, events: list[tuple[str, str, dict]]
    ) -> None:
        """Build a chain in memory from arbitrary events; it must always link up.

        This is the invariant the whole design rests on, checked against inputs
        nobody thought to write down.
        """
        prev = GENESIS_HASH
        chain: list[tuple[int, str]] = []
        base = dt.datetime(2026, 8, 6, 9, 15, tzinfo=dt.UTC)

        for i, (stage, outcome, payload) in enumerate(events, start=1):
            row_hash = compute_row_hash(
                prev_hash=prev,
                seq=i,
                ts=base + dt.timedelta(seconds=i),
                correlation_id=uuid.UUID(int=i),
                stage=stage,
                outcome=outcome,
                payload=payload,
            )
            chain.append((i, row_hash))
            prev = row_hash

        # Every link must be distinct and re-derivable in order.
        assert len({h for _, h in chain}) == len(chain), "hash collision within one chain"

        replay = GENESIS_HASH
        for i, (stage, outcome, payload) in enumerate(events, start=1):
            replay = compute_row_hash(
                prev_hash=replay,
                seq=i,
                ts=base + dt.timedelta(seconds=i),
                correlation_id=uuid.UUID(int=i),
                stage=stage,
                outcome=outcome,
                payload=payload,
            )
        assert replay == chain[-1][1]

    @settings(max_examples=40, deadline=None)
    @given(st.integers(min_value=0, max_value=19), st.integers(min_value=1, max_value=999))
    def test_altering_any_row_breaks_every_hash_after_it(self, victim: int, new_value: int) -> None:
        """Tamper-evidence, as a property rather than a single example."""
        base = dt.datetime(2026, 8, 6, 9, 15, tzinfo=dt.UTC)
        payloads = [{"n": i} for i in range(20)]

        def build(rows: list[dict]) -> list[str]:
            prev, out = GENESIS_HASH, []
            for i, payload in enumerate(rows, start=1):
                prev = compute_row_hash(
                    prev_hash=prev,
                    seq=i,
                    ts=base + dt.timedelta(seconds=i),
                    correlation_id=uuid.UUID(int=i),
                    stage="SIGNAL",
                    outcome="ALLOW",
                    payload=payload,
                )
                out.append(prev)
            return out

        original = build(payloads)
        tampered_rows = list(payloads)
        tampered_rows[victim] = {"n": new_value + 10_000}
        tampered = build(tampered_rows)

        assert tampered[victim] != original[victim]
        for i in range(victim, len(original)):
            assert tampered[i] != original[i], f"tamper at {victim} did not propagate to {i}"


# ---------------------------------------------------------------------------
# Against a real database
# ---------------------------------------------------------------------------


class TestWriterBuildsAValidChain:
    async def test_first_entry_starts_from_genesis(
        self, writer: AuditWriter, session: AsyncSession
    ) -> None:
        await writer.write(entry(1))
        row = (await session.execute(text("SELECT prev_hash, seq FROM decision_log"))).first()
        assert row is not None
        assert row[0] == GENESIS_HASH
        assert row[1] == 1

    async def test_a_written_chain_verifies(
        self, writer: AuditWriter, session: AsyncSession
    ) -> None:
        for i in range(20):
            await writer.write(entry(i))
        result = await verify_chain(session)
        assert result, result.detail
        assert result.rows_checked == 20

    async def test_each_row_links_to_its_predecessor(
        self, writer: AuditWriter, session: AsyncSession
    ) -> None:
        for i in range(5):
            await writer.write(entry(i))
        rows = (
            await session.execute(
                text("SELECT seq, prev_hash, row_hash FROM decision_log ORDER BY seq")
            )
        ).all()
        for earlier, later in itertools.pairwise(rows):
            assert later[1] == earlier[2], f"seq {later[0]} does not link to {earlier[0]}"


class TestTamperDetection:
    """The point of the whole component."""

    async def test_a_modified_payload_is_detected_and_named(
        self, writer: AuditWriter, session: AsyncSession
    ) -> None:
        for i in range(10):
            await writer.write(entry(i))

        await session.execute(
            text("UPDATE decision_log SET payload = '{\"n\": 999}'::jsonb WHERE seq = 5")
        )
        await session.commit()

        result = await verify_chain(session)
        assert not result
        assert result.first_bad_seq == 5, "the verifier must name the row"
        assert "modified" in result.detail

    async def test_a_changed_outcome_is_detected(
        self, writer: AuditWriter, session: AsyncSession
    ) -> None:
        """The realistic attack: flip a REJECT to an ALLOW after the fact."""
        for i in range(6):
            await writer.write(entry(i, outcome="REJECT"))

        await session.execute(text("UPDATE decision_log SET outcome = 'ALLOW' WHERE seq = 3"))
        await session.commit()

        result = await verify_chain(session)
        assert not result
        assert result.first_bad_seq == 3

    async def test_a_deleted_row_is_detected(
        self, writer: AuditWriter, session: AsyncSession
    ) -> None:
        """The case a per-correlation_id chain could NOT catch.

        Deleting an entire trade's worth of entries is exactly the tampering
        worth defending against, which is why the chain is global.
        """
        for i in range(10):
            await writer.write(entry(i))

        await session.execute(text("DELETE FROM decision_log WHERE seq = 4"))
        await session.commit()

        result = await verify_chain(session)
        assert not result
        assert "gap" in result.detail or "link" in result.detail

    async def test_rehashing_a_row_alone_still_fails(
        self, writer: AuditWriter, session: AsyncSession
    ) -> None:
        """A tamperer who recomputes ONE row's hash still breaks the next link.

        To hide a change they must recompute the entire remaining chain — which
        is the property that makes this worth having.
        """
        for i in range(8):
            await writer.write(entry(i))

        row = (
            await session.execute(
                text("SELECT prev_hash, ts, correlation_id, stage FROM decision_log WHERE seq = 4")
            )
        ).first()
        assert row is not None
        forged = compute_row_hash(
            prev_hash=row[0],
            seq=4,
            ts=row[1],
            correlation_id=row[2],
            stage=row[3],
            outcome="ALLOW",
            payload={"n": 999},
        )
        await session.execute(
            text(
                "UPDATE decision_log SET payload = '{\"n\": 999}'::jsonb, row_hash = :h "
                "WHERE seq = 4"
            ),
            {"h": forged},
        )
        await session.commit()

        result = await verify_chain(session)
        assert not result, "a locally-consistent forgery must still break the chain"
        assert result.first_bad_seq == 5, "the break should surface at the NEXT row"

    async def test_an_untampered_chain_verifies_clean(
        self, writer: AuditWriter, session: AsyncSession
    ) -> None:
        """The control — without it every test above could pass for the wrong reason."""
        for i in range(10):
            await writer.write(entry(i))
        assert await verify_chain(session)


class TestConcurrentWriters:
    async def test_concurrent_writers_produce_one_valid_chain(
        self, factory: object, session: AsyncSession, tmp_path: Path
    ) -> None:
        """Two writers must not fork the chain.

        Without the advisory lock, both read the same head and produce two rows
        claiming the same predecessor — which fails verification even though
        nobody tampered with anything.
        """
        writers = [
            AuditWriter(factory, buffer_dir=tmp_path / f"buf{i}")  # type: ignore[arg-type]
            for i in range(4)
        ]
        await asyncio.gather(*[w.write(entry(i)) for i, w in enumerate(writers) for _ in range(5)])

        result = await verify_chain(session)
        assert result, f"concurrent writes forked the chain: {result.detail}"

        seqs = [
            r[0]
            for r in (
                await session.execute(text("SELECT seq FROM decision_log ORDER BY seq"))
            ).all()
        ]
        assert seqs == list(range(1, len(seqs) + 1)), "sequence numbers are not contiguous"


class TestDatabaseOutage:
    """Acceptance: a DB outage must not lose audit entries."""

    async def test_entries_are_buffered_when_the_database_is_unreachable(
        self, tmp_path: Path
    ) -> None:
        dead = db_engine.create_engine_from_url(
            "postgresql+psycopg://nobody:nothing@127.0.0.1:1/nothing",
            connect_timeout_seconds=1,
        )
        writer = AuditWriter(db_engine.create_session_factory(dead), buffer_dir=tmp_path / "buf")
        try:
            result = await writer.write(entry(1))
            assert result is None, "a failed write must report that it did not reach the DB"
            assert writer.degraded is True
            assert writer.buffered_count() == 1
        finally:
            await dead.dispose()

    async def test_a_write_failure_never_raises_into_the_caller(self, tmp_path: Path) -> None:
        """An audit failure that killed the caller would mean a database blip
        stops trading AND loses the record of why."""
        dead = db_engine.create_engine_from_url(
            "postgresql+psycopg://nobody:nothing@127.0.0.1:1/nothing",
            connect_timeout_seconds=1,
        )
        writer = AuditWriter(db_engine.create_session_factory(dead), buffer_dir=tmp_path / "buf")
        try:
            for i in range(5):
                await writer.write(entry(i))  # must not raise
            assert writer.buffered_count() == 5
        finally:
            await dead.dispose()

    async def test_buffered_entries_replay_in_order_and_verify(
        self, factory: object, session: AsyncSession, tmp_path: Path
    ) -> None:
        """The whole point: nothing is lost, and the chain is valid afterwards."""
        buf = tmp_path / "buf"

        dead = db_engine.create_engine_from_url(
            "postgresql+psycopg://nobody:nothing@127.0.0.1:1/nothing",
            connect_timeout_seconds=1,
        )
        offline = AuditWriter(db_engine.create_session_factory(dead), buffer_dir=buf)
        for i in range(10):
            await offline.write(entry(i))
        await dead.dispose()
        assert offline.buffered_count() == 10

        online = AuditWriter(factory, buffer_dir=buf)  # type: ignore[arg-type]
        assert await online.replay_buffer() == 10
        assert online.buffered_count() == 0

        result = await verify_chain(session)
        assert result, result.detail
        assert result.rows_checked == 10

        payloads = [
            r[0]["n"]
            for r in (
                await session.execute(text("SELECT payload FROM decision_log ORDER BY seq"))
            ).all()
        ]
        assert payloads == list(range(10)), "replay did not preserve order"

    async def test_a_failed_replay_keeps_the_buffer_for_retry(self, tmp_path: Path) -> None:
        """Deleting the file first and failing after would lose entries permanently."""
        buf = tmp_path / "buf"
        dead = db_engine.create_engine_from_url(
            "postgresql+psycopg://nobody:nothing@127.0.0.1:1/nothing",
            connect_timeout_seconds=1,
        )
        writer = AuditWriter(db_engine.create_session_factory(dead), buffer_dir=buf)
        try:
            await writer.write(entry(1))
            assert await writer.replay_buffer() == 0
            assert writer.buffered_count() == 1, "the buffer was discarded on a failed replay"
        finally:
            await dead.dispose()

    async def test_recovery_clears_the_degraded_flag(
        self, factory: object, session: AsyncSession, tmp_path: Path
    ) -> None:
        writer = AuditWriter(factory, buffer_dir=tmp_path / "buf")  # type: ignore[arg-type]
        writer._degraded = True  # simulate having been offline
        await writer.write(entry(1))
        assert writer.degraded is False


class TestVerificationRange:
    async def test_empty_chain_verifies(self, session: AsyncSession) -> None:
        assert await verify_chain(session)

    async def test_verification_can_be_scoped_to_a_window(
        self, writer: AuditWriter, session: AsyncSession
    ) -> None:
        for i in range(5):
            await writer.write(entry(i))
        result = await verify_chain(session, start=dt.datetime(2020, 1, 1, tzinfo=dt.UTC))
        assert result.rows_checked == 5
