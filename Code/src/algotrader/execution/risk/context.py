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

import dataclasses
import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
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
    #: Closed-trade P&L for the session. **Negative is a loss.** Open positions
    #: are not in it — the name is the contract.
    realised_pnl_today: Decimal = Decimal(0)
    consecutive_losses: int = 0

    #: Latches for the two loss limits (E14-S05).
    #:
    #: These exist because a check that is a pure predicate over the two fields
    #: above **un-halts itself**. A daily loss limit trips precisely when
    #: losing positions are open; one of them closing at a profit lifts
    #: ``realised_pnl_today`` back over the threshold and trading silently
    #: resumes. ``consecutive_losses`` is worse — a single winning close resets
    #: it to zero.
    #:
    #: ``LOW_LEVEL_ARCHITECTURE.md §8.1``: *"HALTED is terminal for the day and
    #: is only exited by explicit operator action. There is no automatic
    #: un-halt — if a risk limit tripped, a human decides whether resuming is
    #: appropriate."* A predicate cannot express "terminal"; a latch can.
    #:
    #: **Set by E14-S09**, which owns arming and persisting halts, exactly as
    #: it owns ``kill_switch_active`` while check 1 only reads it. Two fields
    #: rather than one shared flag so each rejection keeps its own reason code.
    daily_loss_halted: bool = False
    consecutive_loss_halted: bool = False

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
    #:
    #: A symbol that is ABSENT means "unknown", and the correlation guard
    #: refuses on it. It must never be present as 0, which would read as
    #: "uncorrelated" and admit the fourth PSU bank. See
    #: :func:`~algotrader.execution.risk.correlation.correlations_against`,
    #: which omits what it cannot compute rather than fabricating a zero.
    #:
    #: Typed ``Mapping`` because it is frozen at construction — see
    #: :meth:`_freeze_mutable_fields`.
    correlations: Mapping[str, Decimal] = field(default_factory=dict)

    def _freeze_mutable_fields(self) -> None:
        """Make every container field immutable, whatever the caller passed.

        ``frozen=True`` freezes the *binding*, not what it points at, so a
        caller who passes a list or dict keeps a live reference into state the
        checks are about to read — and can change it between checks, which is
        precisely what the class docstring says must not happen. JSON has
        neither tuples nor frozen mappings, so any deserialised context is the
        realistic route in.

        **Derived from the dataclass fields rather than a hand-written list.**
        The first version of this (QA-SEC-32) named three fields explicitly and
        missed ``correlations``, because that one is a dict rather than a
        sequence and did not match the shape being looked for. A list of names
        has to be remembered every time a field is added; iterating the fields
        cannot be forgotten.
        """
        for f in dataclasses.fields(self):
            value = getattr(self, f.name)
            if value is None or isinstance(value, tuple | MappingProxyType):
                continue
            if isinstance(value, Mapping):
                object.__setattr__(self, f.name, MappingProxyType(dict(value)))
            elif isinstance(value, str | bytes):
                continue  # a string is a Sequence, and is already immutable
            elif isinstance(value, Sequence | set | frozenset):
                object.__setattr__(self, f.name, tuple(value))

    def _reject_non_finite_numbers(self) -> None:
        """No ``Decimal`` on this context may be NaN or an infinity.

        Every one of these is a number a check will compare against a limit,
        and a non-finite value breaks the comparison in one of two ways, both
        bad:

        * ``NaN`` makes every comparison ``False``, so ``loss >= limit`` is
          False and the day trades on — or raises ``InvalidOperation``, whose
          message names neither the field nor the cause (QA-SEC-34).
        * ``Infinity`` compares cleanly and is nonsense: an infinite *profit*
          passes the daily loss limit without comment.

        Rejecting here rather than in each check makes the state unrepresentable
        instead of guarded — the fourteen checks do not each have to remember —
        and the fields are found by iterating the dataclass rather than by a
        hand-written list, which is the lesson QA-SEC-33 paid for.
        """
        for f in dataclasses.fields(self):
            value = getattr(self, f.name)
            if isinstance(value, Decimal) and not value.is_finite():
                raise RiskContextError(
                    f"{f.name} is {value}, which is not a finite number. A risk "
                    f"check would compare it against a limit, and NaN makes "
                    f"every comparison False while an infinity makes the limit "
                    f"meaningless."
                )

    def __post_init__(self) -> None:
        for name in ("unhealthy_services", "open_positions", "symbol_restrictions"):
            value = getattr(self, name)
            if value is not None and isinstance(value, str | bytes):
                raise RiskContextError(
                    f"{name} must be a sequence of values, got "
                    f"{type(value).__name__}. A bare string would be split into "
                    f"characters — 'T2T' becoming three restrictions that do "
                    f"not exist."
                )
        self._freeze_mutable_fields()
        if self.now.tzinfo is None:
            raise RiskContextError("RiskContext.now must be timezone-aware")
        if self.squareoff_deadline.tzinfo is None:
            raise RiskContextError("squareoff_deadline must be timezone-aware")
        self._reject_non_finite_numbers()
        if self.capital <= 0:
            raise RiskContextError(f"capital must be positive, got {self.capital}")
        if self.consecutive_losses < 0:
            raise RiskContextError(
                f"consecutive_losses is {self.consecutive_losses}. A negative "
                f"count is not 'fewer losses' — it means whatever maintains "
                f"the counter is broken, and a broken counter must not read as "
                f"a clean streak."
            )
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

    def sector_exposure(self, sector: str) -> Decimal:
        """Notional held in one sector.

        Takes ``str``, not ``str | None``. The earlier signature accepted None
        and returned ``Decimal(0)``, which reads as "no exposure in that
        sector" and is the ``or 0`` failure the module docstring warns about:
        an instrument with no sector classification would have sailed past the
        sector cap, and the sector cap is the PRIMARY control against four PSU
        banks taking four slots. Callers must resolve the unknown case — see
        :func:`~algotrader.execution.risk.checks.exposure.check_sector_exposure`,
        which rejects.
        """
        return sum((p.notional for p in self.open_positions if p.sector == sector), Decimal(0))

    def positions_missing_a_sector(self) -> tuple[str, ...]:
        """Held symbols whose sector is unknown.

        The other half of the same hole. A position with ``sector=None``
        matches no sector, so its notional silently escapes every sector
        total — four PSU banks with unclassified sectors would each contribute
        nothing and the cap would never bind. A sector total computed while any
        position is unclassified is not trustworthy, so the check refuses
        rather than reporting a number it cannot stand behind.
        """
        return tuple(p.symbol for p in self.open_positions if p.sector is None)

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
