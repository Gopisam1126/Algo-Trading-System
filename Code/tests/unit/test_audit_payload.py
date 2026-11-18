"""What the audit chain will and will not hold, before any database is asked.

Audit ENG-011 and NR-008 (17 Nov 2026). ``AuditWriter.write`` treated every
exception as an outage, so a payload that could never be written - a NaN, a
non-string key - was buffered instead of refused, and then blocked the replay
of every entry behind it. And JSONB hands some floats back in a different
spelling, which failed verification of an untouched chain. The database half
of both is in ``tests/integration/test_audit_chain.py``.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import uuid
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from algotrader.common.audit import AuditEntry, AuditError, AuditWriter, canonical_json


def entry(payload: dict, *, ts: dt.datetime | None = None) -> AuditEntry:
    return AuditEntry(
        correlation_id=uuid.uuid4(),
        stage="RISK_CHECK",
        outcome="ALLOW",
        service="test",
        payload=payload,
        ts=ts,
    )


def never_asked() -> object:
    raise AssertionError("the database must not be asked about an entry that is wrong")


class TestCanonicalJsonRefusesWhatTheChainCannotHold:
    @pytest.mark.parametrize(
        "payload",
        [
            {"x": float("nan")},
            {"x": float("inf")},
            {"x": float("-inf")},
            {"deep": [1, {"y": float("nan")}]},
        ],
        ids=["nan", "inf", "-inf", "nested"],
    )
    def test_a_non_finite_float_is_refused(self, payload: dict) -> None:
        with pytest.raises(AuditError, match="non-finite"):
            canonical_json(payload)

    def test_a_non_string_key_is_still_refused(self) -> None:
        with pytest.raises(AuditError, match="non-string key"):
            canonical_json({"outer": [{1: "a"}]})

    def test_the_control_finite_floats_pass(self) -> None:
        assert canonical_json({"x": 1.5, "y": [0.25]}) == '{"x":1.5,"y":[0.25]}'

    @pytest.mark.parametrize(
        ("value", "spelled"),
        [(1e16, "10000000000000000"), (-2.5e17, "-250000000000000000"), (-0.0, "0.0")],
    )
    def test_floats_jsonb_respells_are_written_as_jsonb_returns_them(
        self, value: float, spelled: str
    ) -> None:
        assert canonical_json({"v": value}) == '{"v":' + spelled + "}"

    def test_a_float_below_the_threshold_keeps_its_spelling(self) -> None:
        assert canonical_json({"v": 9999999999999998.0}) == '{"v":9999999999999998.0}'


_json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(),
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(), inner, max_size=4),
    max_leaves=12,
)


class TestCanonicalJsonIsIdempotent:
    @given(st.dictionaries(st.text(), _json_values, max_size=6))
    def test_re_canonicalising_its_own_output_changes_nothing(self, payload: dict) -> None:
        """Verification re-hashes what the database hands back, which is a
        parse of what was written. Any drift here is a false tamper alarm."""
        once = canonical_json(payload)
        assert canonical_json(json.loads(once)) == once


class TestTheWriterRefusesAWrongEntryAndBuffersNothing:
    @pytest.mark.parametrize(
        "payload",
        [{"x": float("nan")}, {"score": {"rsi": math.inf}}, {"k": {2: "b"}}],
        ids=["nan", "inf", "int-key"],
    )
    async def test_it_raises_to_the_caller(self, payload: dict, tmp_path: Path) -> None:
        writer = AuditWriter(never_asked, buffer_dir=tmp_path / "buf")  # type: ignore[arg-type]
        with pytest.raises(AuditError):
            await writer.write(entry(payload))
        assert writer.buffered_count() == 0, "a wrong entry was buffered as an outage"
        assert writer.quarantined_count() == 0
        assert writer.degraded is False

    async def test_a_naive_timestamp_is_refused(self, tmp_path: Path) -> None:
        writer = AuditWriter(never_asked, buffer_dir=tmp_path / "buf")  # type: ignore[arg-type]
        with pytest.raises(AuditError, match="timezone-aware"):
            await writer.write(entry({}, ts=dt.datetime(2026, 11, 17, 9, 30)))
        assert writer.buffered_count() == 0

    async def test_an_outage_still_buffers_and_never_raises(self, tmp_path: Path) -> None:
        """The control: the refusal above is for wrong entries, not for outages."""

        def down() -> object:
            raise ConnectionRefusedError("database unreachable")

        writer = AuditWriter(down, buffer_dir=tmp_path / "buf")  # type: ignore[arg-type]
        assert await writer.write(entry({"x": 1.0})) is None
        assert writer.buffered_count() == 1
        assert writer.degraded is True

    async def test_the_buffered_line_is_the_canonical_payload(self, tmp_path: Path) -> None:
        """What is buffered is what will be hashed: a Decimal becomes its string
        once, here, not differently at replay."""
        from decimal import Decimal

        def down() -> object:
            raise ConnectionRefusedError("database unreachable")

        writer = AuditWriter(down, buffer_dir=tmp_path / "buf")  # type: ignore[arg-type]
        await writer.write(entry({"price": Decimal("1234.50"), "big": 1e16}))
        (line,) = (tmp_path / "buf").glob("*.jsonl")
        buffered = json.loads(line.read_text(encoding="utf-8"))["payload"]
        assert buffered == {"price": "1234.50", "big": 10000000000000000}
