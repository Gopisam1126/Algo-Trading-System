"""The read-only view a primitive is allowed to see (completes E00-S08).

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
import logging
import math
from dataclasses import dataclass, field
from decimal import Decimal

from algotrader.common.enums import Direction, Regime, Timeframe
from algotrader.common.models.market import Bar
from algotrader.indicators.engine import MultiTimeframeSnapshot
from algotrader.indicators.levels import LevelSet, OpeningRange

log = logging.getLogger(__name__)


def _finite(value: float | None) -> float | None:
    """Non-finite indicator values are absent, not extreme.

    A NaN compares False against everything, so ``price_above_ma`` would answer
    a confident "no" rather than declining; an infinity compares True against
    everything, so a band check would pass unconditionally. Both are silent.
    They reach here from a corrupted snapshot — Python's ``json`` emits and
    accepts bare ``NaN`` — or from a degenerate computation, so filtering at
    the one place every primitive reads through covers all 27 at once.
    """
    if value is None:
        return None
    if not math.isfinite(value):
        log.warning("indicator value %r is not finite; treating it as absent", value)
        return None
    return value


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
    #: with no protection at all. REQUIRED (audit NR-006): it defaulted to
    #: 0.05, described as "right for most NSE equities" - and since 15 Apr 2025
    #: NSE sets the tick by price band (NSE/CMTR/67133: 0.10 from Rs 1,000 to
    #: 5,000, 0.50 to 10,000, ...), so 0.05 is off the grid for every stock
    #: above Rs 1,000. The instrument master's value is the only right one.
    tick_size: Decimal

    bar: Bar | None = None
    #: Today's opening print. Needed by ``gap_from_prev_close``, which is a
    #: statement about the OPEN against yesterday's close — not about the
    #: current price, which drifts away from the gap all morning.
    day_open: Decimal | None = None
    day_high: Decimal | None = None
    day_low: Decimal | None = None
    prev_close: Decimal | None = None
    #: The session's high and low from the bars that OPENED BEFORE ``bar`` -
    #: what "breaks the day's high" is measured against (audit TRD-025). The
    #: day's high including the current bar cannot be exceeded by that bar's
    #: own close, so ``price_breaks_level(day_high)`` could never be True.
    day_high_before_bar: Decimal | None = None
    day_low_before_bar: Decimal | None = None
    #: The close of the previous bar on the strategy's timeframe. A BREAK is a
    #: crossing - this bar's close beyond the level and the previous one not
    #: (audit TRD-017) - and a crossing cannot be told from one price.
    previous_bar_close: Decimal | None = None

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
    #: The candidate's sector, for ``Applicability.sectors``. ``None`` means
    #: unclassified, and a strategy restricted to named sectors does not apply
    #: to an instrument whose sector nobody knows.
    sector: str | None = None

    # -- the strategy's own history today -----------------------------------
    #: Entries THIS strategy has already made in THIS symbol today, counted by
    #: the caller from the order book (E15-S13 wiring). ``None`` means nobody
    #: counted, and the strategy then does not apply: ``max_entries_per_day``
    #: defaults to 1 on every strategy and was enforced by nothing until 27 Oct
    #: 2026, so a strategy stopped out could re-enter the same name again and
    #: again. A count is a claim that someone looked; "unknown" must not read as
    #: "none yet" - the AUDIT-005 rule, applied here.
    entries_today: int | None = None

    # -- news (E08) ---------------------------------------------------------
    news_score: float | None = None
    hours_since_material_news: float | None = None

    def __post_init__(self) -> None:
        if self.now.tzinfo is None:
            raise ContextError("EvalContext.now must be timezone-aware")
        self._every_number_is_finite()
        if self.entries_today is not None and (
            not isinstance(self.entries_today, int)
            or isinstance(self.entries_today, bool)
            or self.entries_today < 0
        ):
            raise ContextError(
                f"entries_today must be a non-negative int, got {self.entries_today!r}"
            )
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

    def _every_number_is_finite(self) -> None:
        """No field may hold NaN or an infinity (audit ENG-017, 16 Nov 2026).

        Only indicator values were filtered (``_finite``). A ``news_score`` of
        +inf passed every "above" gate; a NaN reached ``Decimal(repr(...))``
        and raised ``InvalidOperation``, which the signal loop did not catch,
        so the bar was dead-lettered. Refused here, at construction, the one
        place every primitive reads through; the loop catches ContextError and
        reports the strategy unevaluable for that bar.
        """
        floats = {
            "news_score": self.news_score,
            "hours_since_material_news": self.hours_since_material_news,
        }
        for name, number in floats.items():
            if number is not None and not math.isfinite(number):
                raise ContextError(f"{name} is not finite: {number!r}")
        if self.hours_since_material_news is not None and self.hours_since_material_news < 0:
            raise ContextError(
                f"hours_since_material_news is negative: {self.hours_since_material_news!r}"
            )
        decimals: dict[str, Decimal | None] = {
            "last_price": self.last_price,
            "tick_size": self.tick_size,
            "day_open": self.day_open,
            "day_high": self.day_high,
            "day_low": self.day_low,
            "prev_close": self.prev_close,
            "day_high_before_bar": self.day_high_before_bar,
            "day_low_before_bar": self.day_low_before_bar,
            "previous_bar_close": self.previous_bar_close,
            "india_vix": self.india_vix,
            **{f"index_change_pct[{k}]": v for k, v in self.index_change_pct.items()},
        }
        for name, value in decimals.items():
            if value is not None and not Decimal(value).is_finite():
                raise ContextError(f"{name} is not finite: {value!r}")

    # -- helpers primitives share ------------------------------------------

    def indicator(self, name: str, timeframe: Timeframe | None = None) -> float | None:
        """Current value, or ``None`` when the indicator is not carried."""
        return _finite(self.snapshot.value(timeframe or self.timeframe, name))

    def indicator_ago(
        self, name: str, bars: int, timeframe: Timeframe | None = None
    ) -> float | None:
        return _finite(self.snapshot.value_ago(timeframe or self.timeframe, name, bars))

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
