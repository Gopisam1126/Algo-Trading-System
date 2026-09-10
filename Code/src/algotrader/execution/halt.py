"""The halt controller — arming, persisting and clearing a trading halt (E14-S09).

``LOW_LEVEL_ARCHITECTURE.md §8.1``: *"HALTED is terminal for the day and is only
exited by explicit operator action. There is no automatic un-halt — if a risk
limit tripped, a human decides whether resuming is appropriate."*

Everything downstream of that sentence already existed and none of it was
connected. ``control:killswitch`` had a reader that fails closed and no writer.
``RiskContext.kill_switch_active``, ``daily_loss_halted`` and
``consecutive_loss_halted`` were made mandatory by AUDIT-005 with a comment
naming *this story* as their owner. This module is that owner.

## The design record

**The decision.** Three separately persisted latches — the manual kill switch
and the two loss limits — each carrying a record of why, when and by whom it
was armed. Arming is idempotent and never overwrites an existing reason.
Clearing takes an :class:`OperatorAction`, which cannot be constructed without
a human identity.

**The alternative rejected.** One shared ``halted`` flag with a reason field.
Less code, and it would collapse three rejections into one: an operator seeing
HALTED would have to read prose to learn whether a loss limit or a manual stop
did it, and ``signals_rejected_total{reason}`` would stop telling them apart.
That is the SIT-001 mistake — a wrong label on the metric that exists to answer
"why isn't it trading?" — and E14-S05 already recorded the same reasoning when
it chose two latch fields over one.

**The failure it prevents.** A halt that clears itself. A daily-loss limit
trips precisely when losing positions are open, and one of them closing at a
profit lifts ``realised_pnl_today`` back over the threshold; a predicate would
silently resume trading. A latch cannot, and nothing in the arming path here
can remove one.

**What would make this decision wrong.** If halts ever needed to be scoped per
symbol rather than per session, three global latches would be the wrong shape.

## Halting must never disable the exit path

``E14-S09``'s recorded build concern, and the reason this class holds **no
reference to positions, orders or the square-off timer**. A kill switch tripped
at 14:00 that also stopped the exit path would leave positions for the broker
to close at 15:20 at whatever price exists — turning a risk control into the
thing that costs the money. The safety here is structural rather than
remembered: there is no method on this object that could close anything, and a
test asserts the class exposes no such capability.

The corollary is that arming a halt is **not** an instruction to liquidate.
``ExitReason.KILLSWITCH`` exists for a deliberate, separate operator command to
close positions, which is E14-S13 and not this.

## What is deliberately not here

* **Telegram and dashboard triggers** (task 1, and AC1's "from a phone in under
  ten seconds"). Both packages are empty and a bot credential is blocker B5.
  Raised as **E14-S12**.
* **Cancelling pending orders and the explicit close command** (tasks 3b, 4).
  There is no order path to cancel through. Raised as **E14-S13**.
* **The "AI unavailable" auto-trigger.** The ``ai`` package is empty, so there
  is no availability to observe. Folded into E14-S12's story rather than
  stubbed, because a trigger that watches nothing reads as a live control.

Every other auto-trigger in task 2 has a real input today and is expressible as
a :class:`HaltReason`, which is what makes this half of the story buildable.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Final

import redis.asyncio as aioredis

from algotrader.common.redis import keys
from algotrader.common.text import one_safe_line

log = logging.getLogger(__name__)


class HaltReason(StrEnum):
    """Why the session stopped.

    Separate from ``RejectReason``, which says why one *trade* was refused.
    These are session-scoped and terminal for the day; conflating them would
    let a per-trade rejection look like a halt in the metrics.
    """

    #: A human pressed it. Task 1's transports are deferred, but the reason is
    #: not: the controller must already accept a manual arm, or the deferred
    #: transport would have nothing to call.
    MANUAL = "MANUAL"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    CONSECUTIVE_LOSS_LIMIT = "CONSECUTIVE_LOSS_LIMIT"
    BROKER_DISCONNECTED = "BROKER_DISCONNECTED"
    STALE_FEED = "STALE_FEED"
    MARGIN_SHORTFALL = "MARGIN_SHORTFALL"
    UNKNOWN_POSITION = "UNKNOWN_POSITION"
    #: A position exists that we could neither protect nor close (E15-S04).
    #: Distinct from UNKNOWN_POSITION, which means the BROKER reports a
    #: position we have no record of. Here the position is ours, we know
    #: about it, and both the stop and the emergency exit failed. Borrowing
    #: the nearest plausible neighbour is how HEALTH_GATE_FAILED came to mean
    #: four different things in this codebase; this gets its own member.
    NAKED_POSITION = "NAKED_POSITION"


#: Which latch each reason writes to. Three latches rather than one flag,
#: because ``RiskContext`` carries three booleans and each keeps its own
#: rejection reason code downstream.
KILL_SWITCH: Final = "kill_switch"
DAILY_LOSS: Final = "daily_loss"
CONSECUTIVE_LOSS: Final = "consecutive_loss"

_LATCH_FOR: Final[dict[HaltReason, str]] = {
    HaltReason.DAILY_LOSS_LIMIT: DAILY_LOSS,
    HaltReason.CONSECUTIVE_LOSS_LIMIT: CONSECUTIVE_LOSS,
}

#: Everything else is a session-wide stop and writes the kill switch. Stated as
#: a fallback rather than enumerated so that a HaltReason added later halts by
#: default instead of silently arming nothing — the fail-closed choice.
_DEFAULT_LATCH: Final = KILL_SWITCH

#: A halt record is small. This bounds what a corrupted or hostile value can
#: cost when it is read back and logged.
MAX_RECORD_BYTES: Final = 2_048


@dataclass(frozen=True)
class OperatorAction:
    """Proof that a human asked for something.

    Construction validates, which is one rung above a runtime check: holding
    one of these *is* the evidence that a named person acted. ``clear`` takes
    this type and nothing else, so an automatic trigger cannot un-halt the
    session without fabricating an operator — which is a deliberate act
    visible in review, not an omission.
    """

    #: Who. Free text, but it must be present and must not be a component name.
    operator: str
    #: What they are acknowledging. Recorded so the audit says what was known
    #: at the time, not merely that a button was pressed.
    acknowledged: str

    #: Names that look like a person but are not one. A trigger clearing a halt
    #: "as the system" is the exact bypass this type exists to prevent.
    #:
    #: ``ClassVar``, not ``Final``: inside a dataclass, an annotated assignment
    #: without ClassVar declares a FIELD with a default. This would have become
    #: a third constructor parameter — so a caller could have passed their own
    #: allow-list and cleared a halt as "system", which is precisely what the
    #: type exists to forbid. mypy caught it; the fix is the annotation.
    FORBIDDEN: ClassVar[frozenset[str]] = frozenset(
        {"system", "auto", "automatic", "scheduler", "execution-svc", "none", "unknown"}
    )

    def __post_init__(self) -> None:
        operator = self.operator.strip()
        if not operator:
            raise ValueError(
                "clearing a halt requires a named operator. §8.1 says HALTED is "
                "exited only by explicit operator action, and an unnamed actor "
                "makes that unauditable."
            )
        if operator.lower() in self.FORBIDDEN:
            raise ValueError(
                f"{operator!r} is not a person. A halt cleared 'by the system' is "
                f"an automatic un-halt wearing a name, which §8.1 forbids: if a "
                f"risk limit tripped, a human decides whether resuming is "
                f"appropriate."
            )
        if not self.acknowledged.strip():
            raise ValueError(
                "clearing a halt requires acknowledging what is being cleared, so "
                "the audit records a decision rather than a keystroke."
            )


@dataclass(frozen=True)
class HaltRecord:
    """What one latch holds."""

    reason: HaltReason
    detail: str
    since: dt.datetime
    armed_by: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "reason": self.reason.value,
                "detail": self.detail,
                "since": self.since.isoformat(),
                "armed_by": self.armed_by,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> HaltRecord | None:
        """Parse a stored record, or ``None`` if it is not one.

        Returning ``None`` rather than raising, and the caller treats that as
        **still halted** — see :meth:`HaltController.state`. A latch whose
        payload cannot be read is not evidence that trading may resume; it is
        evidence that something is wrong with the store that holds the halt.
        """
        if len(raw.encode("utf-8", "replace")) > MAX_RECORD_BYTES:
            log.error("halt record exceeds %d bytes; treating as unreadable", MAX_RECORD_BYTES)
            return None
        try:
            data: Any = json.loads(raw)
            return cls(
                reason=HaltReason(data["reason"]),
                detail=one_safe_line(str(data["detail"])),
                since=dt.datetime.fromisoformat(data["since"]),
                armed_by=one_safe_line(str(data["armed_by"])),
            )
        except Exception:
            log.error("halt record is unreadable; the halt STANDS", exc_info=True)
            return None


@dataclass(frozen=True)
class HaltState:
    """The three latches, as the risk engine needs them."""

    kill_switch: bool
    daily_loss: bool
    consecutive_loss: bool
    #: The record behind each armed latch, where it could be read.
    records: dict[str, HaltRecord]

    @property
    def any_halted(self) -> bool:
        return self.kill_switch or self.daily_loss or self.consecutive_loss

    def describe(self) -> str:
        if not self.any_halted:
            return "no halt is in force"
        parts = []
        for latch in (KILL_SWITCH, DAILY_LOSS, CONSECUTIVE_LOSS):
            if not getattr(self, latch):
                continue
            record = self.records.get(latch)
            if record is None:
                parts.append(f"{latch} (armed; its record could not be read)")
            else:
                parts.append(f"{latch} since {record.since.isoformat()} — {record.reason.value}")
        return "; ".join(parts)


class HaltController:
    """Arms, reads and clears the session's halt latches.

    Holds a Redis client and nothing else. That is the structural half of "a
    halt must never disable the exit path": this object has no way to reach a
    position, an order or the square-off timer, so no future edit here can stop
    the system exiting.
    """

    def __init__(self, client: aioredis.Redis) -> None:
        self._client = client

    # -- reading ------------------------------------------------------------

    async def state(self) -> HaltState:
        """Read all three latches. **Fails closed on every error.**

        An unreachable Redis returns all three ARMED, matching
        ``state.is_kill_switch_active``'s documented asymmetry: a false halt
        costs a missed trade, a false all-clear costs an uncontrolled position.
        """
        try:
            raw = {latch: await self._client.get(self._key(latch)) for latch in self._latches()}
        except Exception:
            log.error(
                "halt lookup failed — treating ALL latches as ENGAGED (fail closed)",
                exc_info=True,
            )
            return HaltState(True, True, True, {})

        records: dict[str, HaltRecord] = {}
        for latch, value in raw.items():
            if value is None:
                continue
            text = value.decode() if isinstance(value, bytes) else str(value)
            record = HaltRecord.from_json(text)
            if record is not None:
                records[latch] = record

        return HaltState(
            kill_switch=raw[KILL_SWITCH] is not None,
            daily_loss=raw[DAILY_LOSS] is not None,
            consecutive_loss=raw[CONSECUTIVE_LOSS] is not None,
            records=records,
        )

    async def is_halted(self) -> bool:
        return (await self.state()).any_halted

    # -- arming -------------------------------------------------------------

    async def arm(
        self,
        reason: HaltReason,
        *,
        detail: str,
        armed_by: str,
        now: dt.datetime,
    ) -> HaltRecord:
        """Engage the latch this reason belongs to.

        **Idempotent, and the FIRST reason wins.** If the daily loss limit
        stops the day and the broker then disconnects, the operator needs to
        know a loss limit stopped it — a later trigger relabelling the halt
        would rewrite the history of the decision. The existing record is
        returned unchanged in that case.
        """
        if now.tzinfo is None:
            raise ValueError(
                f"halt timestamp {now!r} is naive. A halt is terminal for the DAY, "
                f"so which day it belongs to has to be unambiguous."
            )
        latch = _LATCH_FOR.get(reason, _DEFAULT_LATCH)
        key = self._key(latch)

        existing = await self._client.get(key)
        if existing is not None:
            text = existing.decode() if isinstance(existing, bytes) else str(existing)
            prior = HaltRecord.from_json(text)
            if prior is not None:
                log.info("%s is already armed (%s); leaving the first reason", latch, prior.reason)
                return prior
            log.warning("%s is armed but unreadable; overwriting with the current reason", latch)

        record = HaltRecord(
            reason=reason,
            detail=one_safe_line(detail),
            since=now.astimezone(dt.UTC),
            armed_by=one_safe_line(armed_by),
        )
        # No TTL, ever. A halt that expires is an automatic un-halt on a timer,
        # which is exactly what §8.1 forbids.
        await self._client.set(key, record.to_json())
        log.critical(
            "TRADING HALTED — %s (%s) armed by %s: %s",
            latch,
            record.reason.value,
            record.armed_by,
            record.detail,
        )
        return record

    # -- clearing -----------------------------------------------------------

    async def clear(self, latch: str, action: OperatorAction) -> bool:
        """Release one latch. Only a human gets to call this.

        The signature is the control: ``action`` cannot be constructed without
        a named operator, so there is no way to clear a halt automatically
        without writing code that invents a person — a deliberate act, visible
        in review, rather than something that happens by omission.

        Returns whether a latch was actually released.
        """
        key = self._key(latch)
        released = bool(await self._client.delete(key))
        log.critical(
            "halt %s CLEARED by %s (acknowledged: %s); released=%s",
            latch,
            one_safe_line(action.operator),
            one_safe_line(action.acknowledged),
            released,
        )
        return released

    async def clear_all(self, action: OperatorAction) -> tuple[str, ...]:
        """Release every latch, for the ordinary "I have looked, resume" case."""
        released: list[str] = []
        for latch in self._latches():
            if await self.clear(latch, action):
                released.append(latch)
        return tuple(released)

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _latches() -> tuple[str, ...]:
        return (KILL_SWITCH, DAILY_LOSS, CONSECUTIVE_LOSS)

    @staticmethod
    def _key(latch: str) -> str:
        if latch == KILL_SWITCH:
            return keys.kill_switch()
        if latch in (DAILY_LOSS, CONSECUTIVE_LOSS):
            return keys.halt_latch(latch)
        raise ValueError(
            f"{latch!r} is not a halt latch. Valid latches are "
            f"{HaltController._latches()}; a typo here would read as 'not halted'."
        )
