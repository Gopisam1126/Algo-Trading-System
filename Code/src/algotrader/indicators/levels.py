"""Price levels: pivots, support/resistance, and the opening range (E06-S05, E06-S06).

Levels are where stops go, so an error here is not a worse signal — it is a stop
in the wrong place on a real position.

**The opening range seals at 09:30 and never moves.** That is E06-S06's
acceptance criterion and it has teeth: by 09:31 the range has already been used
to size a position and place its stop. A late tick reopening it would shift
those levels underneath a trade that is already on. So :class:`OpeningRange`
refuses updates after the seal and counts the refusals, rather than quietly
taking the last one.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from decimal import Decimal

from algotrader.common.calendar import IST, MARKET_OPEN
from algotrader.common.models.market import Bar, Tick

log = logging.getLogger(__name__)

#: The opening range window. 09:15-09:30 IST — the first fifteen minutes.
OPENING_RANGE_END = dt.time(9, 30)

#: Two levels closer than this (as a percentage of price) are the same level
#: wearing two names. Merging them stops a cluster of near-identical prices from
#: looking like strong confluence when it is one line counted five times.
LEVEL_MERGE_PCT = Decimal("0.15")


class LevelError(RuntimeError):
    """A level could not be computed, or was asked to change after sealing."""


# ---------------------------------------------------------------------------
# E06-S06 opening range
# ---------------------------------------------------------------------------


@dataclass
class OpeningRange:
    """High and low of the first fifteen minutes, then frozen.

    Not merely "stops updating" — it actively refuses, and counts. A silent
    no-op would hide a feed delivering out of order, which is a condition worth
    knowing about because it means every bar boundary that morning is
    approximate.
    """

    symbol: str
    trade_date: dt.date
    high: Decimal | None = None
    low: Decimal | None = None
    sealed: bool = False
    ticks_after_seal: int = 0
    _first_price: Decimal | None = None

    def _in_window(self, moment: dt.datetime) -> bool:
        ist = moment.astimezone(IST)
        return ist.date() == self.trade_date and MARKET_OPEN <= ist.time() < OPENING_RANGE_END

    def update(self, tick: Tick) -> bool:
        """Apply a tick. Returns True if it counted toward the range.

        A tick outside the window is not an error — most of the day is outside
        it. A tick inside the window arriving after the seal IS notable, and is
        counted separately.
        """
        inside = self._in_window(tick.exchange_ts)
        if self.sealed:
            if inside:
                self.ticks_after_seal += 1
                log.warning(
                    "%s: tick inside the opening-range window arrived after the seal "
                    "(%d so far). The range is NOT being changed — levels derived from "
                    "it are already sizing positions.",
                    self.symbol,
                    self.ticks_after_seal,
                )
            return False
        if not inside:
            return False

        if self._first_price is None:
            self._first_price = tick.ltp
        self.high = tick.ltp if self.high is None else max(self.high, tick.ltp)
        self.low = tick.ltp if self.low is None else min(self.low, tick.ltp)
        return True

    def seal(self) -> None:
        """Freeze at 09:30. Idempotent."""
        if self.sealed:
            return
        self.sealed = True
        if self.high is None or self.low is None:
            log.warning(
                "%s: opening range sealed with no ticks in the window — the symbol "
                "did not trade in the first fifteen minutes",
                self.symbol,
            )
        else:
            log.info(
                "%s opening range sealed: %s - %s (%.2f%%)",
                self.symbol,
                self.low,
                self.high,
                float(self.range_pct or 0),
            )

    @property
    def is_usable(self) -> bool:
        """Sealed AND populated. An empty range is not a narrow one."""
        return self.sealed and self.high is not None and self.low is not None

    @property
    def width(self) -> Decimal | None:
        if self.high is None or self.low is None:
            return None
        return self.high - self.low

    @property
    def range_pct(self) -> Decimal | None:
        """Range as a percentage of its midpoint.

        The midpoint rather than the open: a gap-and-reverse morning has an open
        sitting at one edge of the range, which would make the same range look
        wider or narrower depending on which way it went.
        """
        if self.high is None or self.low is None:
            return None
        mid = (self.high + self.low) / 2
        if mid <= 0:
            return None
        return (self.high - self.low) / mid * Decimal(100)

    def breakout_direction(self, price: Decimal) -> str | None:
        """``"up"``, ``"down"`` or ``None``. Refuses to answer before sealing.

        Answering early would let a strategy trade a breakout of a range that is
        still forming — which is not a breakout, it is just the current high.
        """
        if not self.is_usable:
            return None
        assert self.high is not None and self.low is not None
        if price > self.high:
            return "up"
        if price < self.low:
            return "down"
        return None


# ---------------------------------------------------------------------------
# E06-S05 pivots and levels
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PivotLevels:
    """Classic floor-trader pivots from the previous session's HLC."""

    pivot: Decimal
    r1: Decimal
    r2: Decimal
    r3: Decimal
    s1: Decimal
    s2: Decimal
    s3: Decimal

    def all_levels(self) -> dict[str, Decimal]:
        return {
            "pivot": self.pivot,
            "r1": self.r1,
            "r2": self.r2,
            "r3": self.r3,
            "s1": self.s1,
            "s2": self.s2,
            "s3": self.s3,
        }

    def nearest(self, price: Decimal) -> tuple[str, Decimal]:
        name, level = min(self.all_levels().items(), key=lambda kv: abs(kv[1] - price))
        return name, level


def classic_pivots(high: Decimal, low: Decimal, close: Decimal) -> PivotLevels:
    """Floor-trader pivots. Requires the PREVIOUS session's high, low and close.

    Passing today's values produces levels that describe where price has already
    been, which is worse than useless — they look like forward-looking levels
    and are not.
    """
    if high < low:
        raise LevelError(f"high {high} is below low {low}")
    pivot = (high + low + close) / 3
    span = high - low
    return PivotLevels(
        pivot=pivot,
        r1=2 * pivot - low,
        s1=2 * pivot - high,
        r2=pivot + span,
        s2=pivot - span,
        r3=high + 2 * (pivot - low),
        s3=low - 2 * (high - pivot),
    )


@dataclass(frozen=True, slots=True)
class Level:
    """One price level, with how much evidence stands behind it."""

    price: Decimal
    kind: str
    touches: int = 1

    @property
    def strength(self) -> int:
        """More touches means more traders remember it. Capped: a level touched
        twenty times is not twice as strong as one touched ten."""
        return min(self.touches, 5)


def find_swings(bars: list[Bar], lookback: int = 2) -> tuple[list[Decimal], list[Decimal]]:
    """Swing highs and lows — a bar higher (lower) than ``lookback`` either side.

    A larger lookback finds fewer, more significant swings. Two is the usual
    intraday choice: it survives a single noisy bar without smoothing away the
    structure that matters on a 5-minute chart.
    """
    if lookback < 1:
        raise LevelError(f"swing lookback must be >= 1, got {lookback}")
    highs: list[Decimal] = []
    lows: list[Decimal] = []
    for i in range(lookback, len(bars) - lookback):
        window = bars[i - lookback : i + lookback + 1]
        centre = bars[i]
        if all(centre.high >= b.high for b in window) and any(centre.high > b.high for b in window):
            highs.append(centre.high)
        if all(centre.low <= b.low for b in window) and any(centre.low < b.low for b in window):
            lows.append(centre.low)
    return highs, lows


def cluster_levels(
    prices: list[Decimal], kind: str, *, merge_pct: Decimal = LEVEL_MERGE_PCT
) -> list[Level]:
    """Merge levels that are within ``merge_pct`` of each other.

    Without this, five swing highs within a rupee of each other read as five
    independent levels and any confluence score counts them five times. They are
    one level that price visited five times — which IS meaningful, but as
    strength, not as count.
    """
    if not prices:
        return []
    ordered = sorted(prices)
    clusters: list[list[Decimal]] = [[ordered[0]]]
    for price in ordered[1:]:
        anchor = clusters[-1][0]
        if anchor > 0 and abs(price - anchor) / anchor * Decimal(100) <= merge_pct:
            clusters[-1].append(price)
        else:
            clusters.append([price])
    levels: list[Level] = []
    for cluster in clusters:
        total = Decimal(0)
        for price in cluster:
            total += price
        levels.append(Level(price=total / len(cluster), kind=kind, touches=len(cluster)))
    return levels


@dataclass
class LevelSet:
    """Every level for one symbol, from every source."""

    symbol: str
    pivots: PivotLevels | None = None
    prior_high: Decimal | None = None
    prior_low: Decimal | None = None
    prior_close: Decimal | None = None
    swing_levels: list[Level] = field(default_factory=list)

    def from_prior_session(self, high: Decimal, low: Decimal, close: Decimal) -> None:
        self.prior_high, self.prior_low, self.prior_close = high, low, close
        self.pivots = classic_pivots(high, low, close)

    def from_bars(self, bars: list[Bar], *, lookback: int = 2) -> None:
        highs, lows = find_swings(bars, lookback=lookback)
        self.swing_levels = cluster_levels(highs, "resistance") + cluster_levels(lows, "support")

    def all_levels(self) -> list[Level]:
        out = list(self.swing_levels)
        if self.pivots is not None:
            out += [Level(price=p, kind=name) for name, p in self.pivots.all_levels().items()]
        for price, name in (
            (self.prior_high, "prior_high"),
            (self.prior_low, "prior_low"),
            (self.prior_close, "prior_close"),
        ):
            if price is not None:
                out.append(Level(price=price, kind=name))
        return out

    def nearest_above(self, price: Decimal) -> Level | None:
        candidates = [level for level in self.all_levels() if level.price > price]
        return min(candidates, key=lambda lv: lv.price - price) if candidates else None

    def nearest_below(self, price: Decimal) -> Level | None:
        candidates = [level for level in self.all_levels() if level.price < price]
        return min(candidates, key=lambda lv: price - lv.price) if candidates else None

    def proximity_pct(self, price: Decimal) -> Decimal | None:
        """Distance to the nearest level either way, as a percentage.

        Feeds the tradeability score: a setup right on top of resistance has a
        worse expectancy than the same setup with room to run, and this is how
        that becomes a number.
        """
        levels = self.all_levels()
        if not levels or price <= 0:
            return None
        closest = min(levels, key=lambda lv: abs(lv.price - price))
        return abs(closest.price - price) / price * Decimal(100)
