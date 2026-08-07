"""The message envelope every stream entry carries.

``schema_version`` exists from message one. Adding it later, once streams
already contain unversioned entries, means writing a compatibility shim that
guesses — for the cost of one integer field now.

``correlation_id`` is what makes a trade traceable end to end: pre-market
candidacy, signal, AI review, risk decision, order, fill, exit. Every envelope
carries it, and it is the same id that appears on the order row and in
``decision_log``. A message without one cannot be tied to the decision that
produced it, so the field is required.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Bumped only when a change is NOT backward compatible. Consumers must refuse
#: a version they do not understand rather than guess at the payload shape.
CURRENT_SCHEMA_VERSION = 1


class Envelope(BaseModel):
    """A single stream message.

    Frozen because a message is a record of something that happened. Mutating
    one after publication would make the audit trail a work of fiction.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    message_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    correlation_id: uuid.UUID
    schema_version: int = CURRENT_SCHEMA_VERSION
    emitted_at: dt.datetime
    emitted_by: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any]

    @field_validator("emitted_at")
    @classmethod
    def _must_be_tz_aware(cls, v: dt.datetime) -> dt.datetime:
        """A naive timestamp here would be silently interpreted as UTC by one
        consumer and as local time by another. In a market with a fixed session
        that is a five-and-a-half-hour error, not a rounding one."""
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("emitted_at must be timezone-aware")
        return v

    @model_validator(mode="after")
    def _version_must_be_known(self) -> Self:
        if self.schema_version < 1:
            raise ValueError(f"schema_version must be >= 1, got {self.schema_version}")
        return self

    def to_fields(self) -> dict[str, str]:
        """Flatten to the Redis stream field map.

        The payload is nested JSON in a single field rather than spread across
        stream fields. Redis stream fields are a flat string map, so a nested
        payload would need flattening and re-inflating — losing type information
        (every value becomes a string) exactly where ``Decimal`` matters most.
        """
        return {
            "message_id": str(self.message_id),
            "correlation_id": str(self.correlation_id),
            "schema_version": str(self.schema_version),
            "emitted_at": self.emitted_at.isoformat(),
            "emitted_by": self.emitted_by,
            "payload": self.model_dump_json(include={"payload"}),
        }

    @classmethod
    def from_fields(cls, fields: dict[str, str]) -> Envelope:
        """Rebuild from a Redis stream field map.

        Raises ``ValueError`` on anything malformed — a missing field, a bad
        UUID, an unparseable timestamp. The caller decides what to do with that;
        for a consumer it means dead-lettering the entry rather than letting the
        exception kill the loop, because **one poisoned message must not stop
        the stream.**
        """
        try:
            # to_fields() writes model_dump_json(include={"payload"}), which is
            # {"payload": {...}} — so unwrap one level here. Going through
            # Pydantic's encoder both ways is what keeps Decimal and datetime
            # round-tripping exactly rather than degrading to float/str.
            wrapper = json.loads(fields["payload"])
            payload = (
                wrapper["payload"]
                if isinstance(wrapper, dict) and "payload" in wrapper
                else wrapper
            )

            return cls(
                message_id=uuid.UUID(fields["message_id"]),
                correlation_id=uuid.UUID(fields["correlation_id"]),
                schema_version=int(fields["schema_version"]),
                emitted_at=dt.datetime.fromisoformat(fields["emitted_at"]),
                emitted_by=fields["emitted_by"],
                payload=payload,
            )
        except ValueError:
            raise
        except Exception as exc:  # KeyError, JSONDecodeError, ValidationError
            raise ValueError(f"malformed stream entry: {exc}") from exc
