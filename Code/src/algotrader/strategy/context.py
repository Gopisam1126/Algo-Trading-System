"""The read-only view a primitive is allowed to see (E13-S01).

``compile_strategy`` has always ended with "the runtime evaluator walks the
validated tree". This module and :mod:`algotrader.strategy.runtime` are that
evaluator; until they existed the 27 primitives were specifications with no
implementation, and the whole strategy layer could be validated but not run.

**Why a context object rather than passing the engine around.** A primitive
that could reach the indicator engine could also reach the broker, the
database, or the clock. Strategies are required to be pure functions over a
snapshot — that is what makes a backtest a faithful replay rather than an
approximation. A frozen context with only value fields makes purity structural:
there is nothing here to call, so a primitive cannot do I/O even by accident.

**Every optional field is genuinely optional.** ``india_vix`` is None before the
macro feed lands; ``levels`` is None for a symbol with no prior session. The
rule throughout is that a missing input yields UNKNOWN, never a default. A
default would let "we don't know the VIX" silently become "the VIX is fine".
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from algotrader.common.enums import Direction, Regime, Timeframe
from algotrader.common.models.market import Bar
from algotrader.indicators.engine import MultiTimeframeSnapshot
from algotrader.indicators.levels import LevelSet, OpeningRange


class ContextError(ValueError):
    """The context is internally inconsistent and cannot be trusted."""


@dataclass(frozen=True)
class EvalContext:
    """Everything a strategy primitive may read, and nothing else.

    Frozen on purpose: a primitive that could mutate the context could make the
    evaluation of one condition depend on the order the others ran in, which
    would make a strategy's behaviour depend on dict ordering.
    """

    # -- identity and clock -------------------------------------------------
    symbol: str
    now: dt.datetime
    #: The strategy's own timeframe, from ``Applicability.timeframe``. Primitives
    #: that do not name a timeframe read this one.
    timeframe: Timeframe
    #: The direction the strategy trades. ``structure_stop`` needs it to know
    #: which side of a level the stop belongs on.
    direction: Direction

    # -- price --------------------------------------------------------------
    last_price: Decimal
    snapshot: MultiTimeframeSnapshot
    #: The instrument's tick size. A stop is a price that must be PLACEABLE:
    #: NSE rejects anything off the tick grid, so an unsnapped stop is not a
    #: slightly-imprecise stop, it is a rejected order and therefore a position
    #: with no protection at all. Defaults to the same 0.05 as
    #: ``Instrument.tick_size``; callers holding a real instrument must pass
    #: its own value, because the default is right for most NSE equities and
    #: wrong for the ones that matter.
    tick_size: Decimal = Decimal("0.05")

    bar: Bar | None = None
    #: Today's opening print. Needed by ``gap_from_prev_close``, which is a
    #: statement about the OPEN against yesterday's close — not about the
    #: current price, which drifts away from the gap all morning.
    day_open: Decimal | None = None
    day_high: Decimal | None = None
    day_low: Decimal | None = None
    prev_close: Decimal | None = None

    # -- structure ----------------------------------------------------------
    levels: LevelSet | None = None
    opening_range: OpeningRange | None = None

    # -- session position ---------------------------------------------------
    bars_since_open: int | None = None
    bars_until_squareoff: int | None = None

    # -- market context (E09) ----------------------------------------------
    regime: Regime | None = None
    india_vix: Decimal | None = None
    #: Percentage change on the day, per index. Keys are index symbols
    #: ("NIFTY", "BANKNIFTY") to match the ``index_not_opposing`` enum.
    index_change_pct: dict[str, Decimal] = field(default_factory=dict)
    sector_rank: int | None = None

    # -- news (E08) ---------------------------------------------------------
    news_score: float | None = None
    hours_since_material_news: float | None = None

    def __post_init__(self) -> None:
        if self.now.tzinfo is None:
            raise ContextError("EvalContext.now must be timezone-aware")
        if self.last_price <= 0:
            raise ContextError(f"last_price must be positive, got {self.last_price}")
        if self.tick_size <= 0:
            raise ContextError(f"tick_size must be positive, got {self.tick_size}")
        if self.snapshot.symbol != self.symbol:
            raise ContextError(
                f"snapshot is for {self.snapshot.symbol!r} but the context is for "
                f"{self.symbol!r} — evaluating one symbol against another's "
                f"indicators would produce a plausible, entirely wrong signal"
            )

    # -- helpers primitives share ------------------------------------------

    def indicator(self, name: str, timeframe: Timeframe | None = None) -> float | None:
        """Current value, or ``None`` when the indicator is not carried."""
        return self.snapshot.value(timeframe or self.timeframe, name)

    def indicator_ago(
        self, name: str, bars: int, timeframe: Timeframe | None = None
    ) -> float | None:
        return self.snapshot.value_ago(timeframe or self.timeframe, name, bars)

    def named_level(self, name: str) -> Decimal | None:
        """Resolve a DSL level name to a price.

        Returns ``None`` when the level does not exist yet — an opening range
        before 09:30, pivots for a symbol with no prior session. The caller
        turns that into UNKNOWN; it must never become zero, because a stop at
        zero is a stop that never triggers.
        """
        opening = self.opening_range
        if name == "opening_range_high":
            return opening.high if opening is not None and opening.is_usable else None
        if name == "opening_range_low":
            return opening.low if opening is not None and opening.is_usable else None

        levels = self.levels
        if name == "prev_day_high":
            return levels.prior_high if levels else None
        if name == "prev_day_low":
            return levels.prior_low if levels else None
        if name in ("pivot", "r1", "s1"):
            if levels is None or levels.pivots is None:
                return None
            return levels.pivots.all_levels().get(name)
        if name == "vwap":
            value = self.indicator("vwap")
            return None if value is None else Decimal(repr(value))
        if name == "day_high":
            return self.day_high
        if name == "day_low":
            return self.day_low
        if name.startswith("ema_"):
            value = self.indicator(name)
            return None if value is None else Decimal(repr(value))
        if name in ("swing_high", "swing_low"):
            return self._nearest_swing(name)
        return None

    def _nearest_swing(self, name: str) -> Decimal | None:
        """The closest swing level on the relevant side of price.

        A stop goes at the nearest structure price has respected, not the most
        extreme one — an unqualified "swing low" would put the stop at the
        session's lowest swing and size the position to nothing.
        """
        if self.levels is None:
            return None
        above = name == "swing_high"
        want = "resistance" if above else "support"
        candidates = [
            level.price
            for level in self.levels.swing_levels
            if level.kind == want
            and (level.price > self.last_price if above else level.price < self.last_price)
        ]
        if not candidates:
            return None
        return min(candidates) if above else max(candidates)
