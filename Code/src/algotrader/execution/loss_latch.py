"""Arms the two loss-limit halts when the day breaches them (audit RSK-003).

``execution/risk/checks/loss.py`` reads two latches so that a loss limit, once
breached, stays breached: one later winning close must not resume trading on a
day a limit already stopped. Until 16 Nov 2026 **nothing armed them** - every
SIT test proving "a recovered P&L does not resume trading" armed the latch by
hand - so in the assembled service one winning close after a breach resumed
entries, and the consecutive-loss limit cleared on the first winner.

## The design record

**The decision.** One small object, given the day's ledger, arms
``DAILY_LOSS_LIMIT`` when the day's loss reaches the limit and
``CONSECUTIVE_LOSS_LIMIT`` when the losing streak does. The execution service
calls it after every reconciliation cycle (which is when closes are booked and
fills written back) and with every signal's context, so the latch is set no
later than the next decision after a breach becomes visible.

**The alternative rejected.** Arming inside the risk check. The checks are
pure - no I/O, replayable - and a check that wrote Redis would no longer be.

**The failure it prevents.** A 3% day becoming a 9% day: the halt that
"cannot clear itself" clearing itself.

**What would make this wrong.** A loss limit that must be re-evaluated on a
FALLING figure (to un-halt). That is forbidden by ``LOW_LEVEL_ARCHITECTURE.md
§8.1`` - only an operator clears a halt - so this object can only ever arm.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from typing import Any, Final, Protocol

from algotrader.execution.day_ledger import DayLedger
from algotrader.execution.halt import HaltReason

log = logging.getLogger(__name__)

__all__ = ["ARMED_BY", "LossLatch"]

ARMED_BY: Final = "loss-latch"


class Arming(Protocol):
    async def arm(
        self, reason: HaltReason, *, detail: str, armed_by: str, now: dt.datetime
    ) -> Any: ...


class LossLatch:
    """Arms, never clears. Unknown figures arm nothing - the checks refuse on them."""

    def __init__(
        self,
        *,
        capital: Decimal,
        max_daily_loss_pct: Decimal,
        consecutive_loss_halt: int,
        halts: Arming,
    ) -> None:
        if capital <= 0 or not 0 < max_daily_loss_pct <= 100 or consecutive_loss_halt < 1:
            raise ValueError(
                "LossLatch needs positive capital, a limit in (0, 100] and a streak >= 1"
            )
        self._limit = capital * max_daily_loss_pct / 100
        self._pct = max_daily_loss_pct
        self._streak = consecutive_loss_halt
        self._halts = halts

    async def observe(self, ledger: DayLedger, *, now: dt.datetime) -> tuple[HaltReason, ...]:
        """Arm whichever limit the ledger breaches. Returns what was armed."""
        armed: list[HaltReason] = []
        net = ledger.net
        if net is not None and -net >= self._limit:
            await self._halts.arm(
                HaltReason.DAILY_LOSS_LIMIT,
                detail=(
                    f"the day stands at {net:.2f} (realised {ledger.realised:.2f}, open "
                    f"{ledger.unrealised:.2f}, charges {ledger.charges:.2f}); the "
                    f"{self._pct}% limit is {self._limit:.2f}"
                ),
                armed_by=ARMED_BY,
                now=now,
            )
            armed.append(HaltReason.DAILY_LOSS_LIMIT)
        if ledger.streak is not None and ledger.streak >= self._streak:
            await self._halts.arm(
                HaltReason.CONSECUTIVE_LOSS_LIMIT,
                detail=f"{ledger.streak} losing round trips in a row; the limit is {self._streak}",
                armed_by=ARMED_BY,
                now=now,
            )
            armed.append(HaltReason.CONSECUTIVE_LOSS_LIMIT)
        return tuple(armed)
