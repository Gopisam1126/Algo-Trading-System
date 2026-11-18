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
import math
import uuid
from decimal import Decimal
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Bumped only when a change is NOT backward compatible. Consumers must refuse
#: a version they do not understand rather than guess at the payload shape.
CURRENT_SCHEMA_VERSION = 1

#: The versions this code can read - exactly these, not "at least 1". The
#: validator was named for refusing unknown versions and refused only those
#: below 1, so a v99 message was accepted by a v1 consumer (audit ENG-012).
SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({CURRENT_SCHEMA_VERSION})


def _refuse_non_finite(value: Any, path: str) -> None:
    """NaN and infinity are not JSON. Pydantic writes them as ``null``, so a NaN
    price published on a stream arrived as None - read downstream as "missing",
    not "invalid" (ENG-012)."""
    if isinstance(value, dict):
        for key, item in value.items():
            _refuse_non_finite(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _refuse_non_finite(item, f"{path}[{index}]")
    elif (isinstance(value, float) and not math.isfinite(value)) or (
        isinstance(value, Decimal) and not value.is_finite()
    ):
        raise ValueError(f"{path} is {value!r}: a stream payload carries finite numbers only")


class Envelope(BaseModel):
    """A single stream message.

    Frozen because a message is a record of something that happened. Mutating
    one after publication would make the audit trail a work of fiction.

    **The payload is JSON, and comes back as JSON.** A ``Decimal`` or a
    ``datetime`` put in is written as a string and READ BACK AS A STRING; it
    does not round-trip as its type, whatever this docstring once said
    (audit ENG-012). A consumer re-validates the payload with the typed model
    it expects (``Bar.model_validate``, ``Recommendation.model_validate``),
    which is where a string becomes a ``Decimal`` again - exactly, because it
    was never a float. Non-finite numbers are refused at construction.
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

    @field_validator("payload")
    @classmethod
    def _payload_is_finite(cls, v: dict[str, Any]) -> dict[str, Any]:
        _refuse_non_finite(v, "payload")
        return v

    @model_validator(mode="after")
    def _version_must_be_known(self) -> Self:
        if self.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(
                f"schema_version {self.schema_version} is not one this code reads "
                f"({sorted(SUPPORTED_SCHEMA_VERSIONS)}); refused rather than guessed at"
            )
        return self

    def to_fields(self) -> dict[str, str]:
        """Flatten to the Redis stream field map.

        The payload is nested JSON in a single field rather than spread across
        stream fields. Redis stream fields are a flat string map, so a nested
        payload would need flattening and re-inflating — and every number would
        come back a string, not only the ``Decimal`` ones.
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
            # {"payload": {...}} — so unwrap one level here. What comes back is
            # JSON-native: a Decimal written as "1234.50" is the STRING
            # "1234.50" here, never a float, and the consumer's typed model
            # turns it back into a Decimal (see the class docstring).
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
