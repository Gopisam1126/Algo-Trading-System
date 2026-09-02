"""The read-only state a risk check is allowed to see (E14-S01).

Mirrors :class:`~algotrader.strategy.context.EvalContext`, for the same reason:
a check that could reach the broker, the database or the clock could also do
I/O in the middle of an order decision, and the pipeline would stop being
replayable. Everything a check needs is a value on this object, so purity is
structural rather than a rule someone has to remember.

**Nothing here is optional-with-a-default.** ``LOW_LEVEL_ARCHITECTURE.md §5.7``
lists ``check_margin_sufficient`` as reading *live broker margin, not assumed
leverage*, and a default would turn "we could not reach the broker" into a
number. Where a value may genuinely be unavailable the field is ``| None``, and
the check that reads it is required to reject rather than assume — see
:meth:`require`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TypeVar

from algotrader.common.enums import Direction

#: `require` is generic so a narrowed type comes back, rather than `object`
#: plus an `assert isinstance(...)` at every call site. Those asserts vanish
#: under `python -O`, which would leave the narrowing unchecked in exactly the
#: build most likely to run unattended.
T = TypeVar("T")


class RiskContextError(ValueError):
    """The context is internally inconsistent and must not be evaluated."""


@dataclass(frozen=True)
class OpenPosition:
    """What the portfolio checks need about a position already on."""

    symbol: str
    direction: Direction
    quantity: int
    entry_price: Decimal
    stop_price: Decimal
    sector: str | None = None

    @property
    def notional(self) -> Decimal:
        return self.entry_price * self.quantity


@dataclass(frozen=True)
class RiskContext:
    """Everything the fourteen checks read, and nothing else.

    Frozen: a check that mutated the context would make the outcome depend on
    check ORDER in ways the ordering was not designed for. The order is meant
    to control which rejection you see first, not what the later checks are
    looking at.
    """

    # -- identity and clock -------------------------------------------------
    now: dt.datetime
    #: Deadline for THIS symbol — 15:10 CAS / 15:20 non-CAS / 15:25 F&O, minus
    #: the configured buffer. Per-stock, never global; a single deadline would
    #: be wrong for two thirds of the universe.
    squareoff_deadline: dt.datetime

    # -- capital ------------------------------------------------------------
    capital: Decimal
    #: Live broker margin. ``None`` means the broker did not answer, which is a
    #: rejection and never an assumption.
    available_margin: Decimal | None = None

    # -- session state ------------------------------------------------------
    kill_switch_active: bool = False
    #: Services that have missed their heartbeat. Non-empty means degraded.
    unhealthy_services: tuple[str, ...] = ()
    realised_pnl_today: Decimal = Decimal(0)
    consecutive_losses: int = 0

    # -- portfolio ----------------------------------------------------------
    open_positions: tuple[OpenPosition, ...] = ()
    slots_total: int = 0
    slots_used: int = 0

    # -- the symbol under consideration -------------------------------------
    #: Restrictions that BLOCK trading this symbol — T2T, ASM, GSM, F&O ban —
    #: re-verified at ORDER time rather than trusted from the pre-market
    #: snapshot, because a symbol can enter a ban list intraday.
    #:
    #: Three states, and the difference between two of them is the whole point:
    #:
    #: * ``None``  — eligibility was never established. A **rejection**, never
    #:   read as "no restrictions found". This is the state an unwired E04
    #:   leaves, so the safe reading is the one that refuses.
    #: * ``()``    — checked, and clean.
    #: * non-empty — checked, and blocked. The labels reach the rejection
    #:   detail, because "not tradable" without the reason tells an operator
    #:   nothing they can act on.
    #:
    #: **The contract E04 must meet:** put here only what BLOCKS. Deciding
    #: which surveillance flags qualify belongs where the data is, not in a
    #: risk check that would have to encode NSE's surveillance rules to read a
    #: flag. A single ``bool`` plus a parallel reasons tuple was rejected for
    #: making ``tradable=True`` alongside ``("BAN",)`` representable.
    symbol_restrictions: tuple[str, ...] | None = None
    symbol_sector: str | None = None
    atr: Decimal | None = None
    #: Per-share margin the broker will demand. Separate from `available_margin`
    #: because the sizing cap needs both.
    margin_per_share: Decimal | None = None
    #: Correlation of the candidate against each open position, by symbol.
    correlations: dict[str, Decimal] = field(default_factory=dict)

    #: Fields typed as tuples that a caller can still hand a list. `frozen=True`
    #: freezes the *binding*, not what it points at, so a list here would let a
    #: caller keep a reference and mutate the context between checks — making
    #: the outcome depend on check order in exactly the way the docstring above
    #: says it must not. Deserialised input (JSON has no tuples) is the
    #: realistic route in.
    _SEQUENCE_FIELDS = ("unhealthy_services", "open_positions", "symbol_restrictions")

    def __post_init__(self) -> None:
        for name in self._SEQUENCE_FIELDS:
            value = getattr(self, name)
            if value is not None and not isinstance(value, tuple):
                if isinstance(value, str) or not isinstance(value, Sequence):
                    raise RiskContextError(
                        f"{name} must be a sequence of values, got {type(value).__name__}"
                    )
                object.__setattr__(self, name, tuple(value))
        if self.now.tzinfo is None:
            raise RiskContextError("RiskContext.now must be timezone-aware")
        if self.squareoff_deadline.tzinfo is None:
            raise RiskContextError("squareoff_deadline must be timezone-aware")
        if self.capital <= 0:
            raise RiskContextError(f"capital must be positive, got {self.capital}")
        if self.slots_used > self.slots_total:
            raise RiskContextError(
                f"slots_used {self.slots_used} exceeds slots_total "
                f"{self.slots_total} — the slot accounting is wrong, and sizing "
                f"would divide capital that does not exist"
            )

    # -- helpers the checks share ------------------------------------------

    @property
    def slots_available(self) -> int:
        return max(0, self.slots_total - self.slots_used)

    @property
    def minutes_to_squareoff(self) -> float:
        return (self.squareoff_deadline - self.now).total_seconds() / 60.0

    def holds(self, symbol: str) -> bool:
        return any(p.symbol == symbol for p in self.open_positions)

    def net_exposure(self) -> Decimal:
        """Signed notional. Longs positive, shorts negative."""
        total = Decimal(0)
        for position in self.open_positions:
            sign = 1 if position.direction is Direction.LONG else -1
            total += position.notional * sign
        return total

    def gross_exposure(self) -> Decimal:
        return sum((p.notional for p in self.open_positions), Decimal(0))

    def sector_exposure(self, sector: str | None) -> Decimal:
        if sector is None:
            return Decimal(0)
        return sum((p.notional for p in self.open_positions if p.sector == sector), Decimal(0))

    def require(self, value: T | None, what: str) -> T:
        """Read a value that must be present, or say precisely what was missing.

        The alternative — a check quietly treating ``None`` as zero — is the
        failure this whole module is shaped to avoid. An unreachable broker
        becoming "margin: 0" would reject everything, which looks like caution;
        becoming "margin: unlimited" would approve everything, which looks like
        nothing at all until the statement arrives.
        """
        if value is None:
            raise RiskContextError(
                f"{what} is unavailable, so the decision cannot be made. "
                f"A risk check must reject on missing input, never assume one."
            )
        return value
