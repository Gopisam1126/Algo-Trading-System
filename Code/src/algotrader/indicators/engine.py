"""Indicator sets, warm-up, and the multi-timeframe snapshot (E06-S03, E06-S04).

An ``IndicatorSet`` is every indicator for one (symbol, timeframe). The engine
holds one per pair and answers the question the signal engine actually asks:
*is this symbol ready, and what do the timeframes say together?*

**``all_ready`` is a gate, not a status field.** A symbol whose 200-EMA has
seen forty bars is not partially ready; it is not ready. Trading off an
indicator that has not warmed up is a real bug that looks like a working
system — the number is present, plausible, and wrong — so the snapshot refuses
to describe a symbol whose indicators are still filling.

**Restoring state must equal re-warming from history.** A mid-session restart
that produced even slightly different values would change signals with nothing
visible to show for it. That equivalence is asserted in the tests rather than
assumed, because it is exactly the kind of property that quietly stops holding
when a new indicator is added and its ``snapshot`` forgets a field.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from algotrader.common.enums import Timeframe
from algotrader.common.models.market import Bar
from algotrader.indicators.framework import (
    ATR,
    EMA,
    MACD,
    RSI,
    VWAP,
    BollingerBands,
    Indicator,
    VolumeRatio,
)

log = logging.getLogger(__name__)


def default_indicators() -> dict[str, Indicator]:
    """The standard set, per (symbol, timeframe).

    Periods are the conventional ones. They are not configurable here on
    purpose: a strategy that wants a different period declares it in the DSL,
    and letting config quietly change what "the 20-EMA" means would make two
    backtests incomparable without either of them being wrong.
    """
    return {
        "ema_20": EMA(20),
        "ema_50": EMA(50),
        "ema_200": EMA(200),
        "rsi_14": RSI(14),
        "macd": MACD(12, 26, 9),
        "atr_14": ATR(14),
        "bb_20": BollingerBands(20, 2.0),
        "vwap": VWAP(),
        "volume_ratio_20": VolumeRatio(20),
    }


@dataclass
class IndicatorSet:
    """Every indicator for one symbol on one timeframe."""

    symbol: str
    timeframe: Timeframe
    indicators: dict[str, Indicator] = field(default_factory=default_indicators)
    bars_seen: int = 0
    #: Set when a feed gap is detected. A stale set must not be traded on, and
    #: this is deliberately NOT cleared by the next bar — one bar after a
    #: fifteen-minute hole does not make a 200-EMA correct again.
    stale: bool = False

    @property
    def is_ready(self) -> bool:
        """Every indicator warm, and no unresolved gap."""
        return not self.stale and all(i.is_ready for i in self.indicators.values())

    def not_ready(self) -> list[str]:
        """Which indicators are still warming. For the health panel."""
        return sorted(name for name, i in self.indicators.items() if not i.is_ready)

    def update(self, bar: Bar) -> None:
        for indicator in self.indicators.values():
            indicator.update(bar)
        self.bars_seen += 1

    def warm_up(self, bars: list[Bar]) -> None:
        """Feed history through the same path live updates take."""
        for bar in bars:
            self.update(bar)

    def mark_stale(self, reason: str) -> None:
        """Called on a feed gap. Recovery is a re-warm, not the next tick."""
        if not self.stale:
            log.warning(
                "indicators for %s %s marked STALE: %s",
                self.symbol,
                self.timeframe.value,
                reason,
            )
        self.stale = True

    def clear_stale(self) -> None:
        """Only after a genuine re-warm from history."""
        self.stale = False

    def values(self) -> dict[str, float | None]:
        out: dict[str, float | None] = {n: i.value for n, i in self.indicators.items()}
        macd = self.indicators.get("macd")
        if isinstance(macd, MACD):
            out["macd_signal"] = macd.signal
            out["macd_histogram"] = macd.histogram
        bb = self.indicators.get("bb_20")
        if isinstance(bb, BollingerBands):
            out["bb_upper"] = bb.upper
            out["bb_lower"] = bb.lower
            out["bb_width_pct"] = bb.width_pct
        return out

    def atr_percent(self, price: float) -> float | None:
        """ATR as a percentage of price — what the tick outlier filter wants."""
        atr = self.indicators.get("atr_14")
        return atr.percent_of(price) if isinstance(atr, ATR) else None

    def snapshot(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe.value,
            "bars_seen": self.bars_seen,
            "stale": self.stale,
            "indicators": {n: i.snapshot() for n, i in self.indicators.items()},
        }

    def restore(self, state: dict[str, Any]) -> None:
        """Reload from a snapshot.

        An indicator present in the snapshot but absent here (or vice versa) is
        a version mismatch, and silently ignoring it would leave a set that
        looks restored and is partly empty.
        """
        saved = state.get("indicators", {})
        missing = set(self.indicators) - set(saved)
        extra = set(saved) - set(self.indicators)
        if missing or extra:
            raise ValueError(
                f"indicator snapshot for {self.symbol} {self.timeframe.value} does not "
                f"match this build: missing {sorted(missing)}, unexpected {sorted(extra)}. "
                f"Re-warm from history rather than restoring a partial set."
            )
        for name, indicator in self.indicators.items():
            indicator.restore(saved[name])
        self.bars_seen = int(state.get("bars_seen", 0))
        self.stale = bool(state.get("stale", False))


@dataclass
class MultiTimeframeSnapshot:
    """What the signal engine reads. Never assembled unless everything is warm."""

    symbol: str
    per_timeframe: dict[Timeframe, dict[str, float | None]]
    all_ready: bool
    not_ready: dict[Timeframe, list[str]] = field(default_factory=dict)

    def value(self, timeframe: Timeframe, indicator: str) -> float | None:
        return self.per_timeframe.get(timeframe, {}).get(indicator)

    def trend_agreement(self, fast: str = "ema_20", slow: str = "ema_50") -> int:
        """How many timeframes agree on direction.

        Counts +1 for each timeframe where fast is above slow and -1 where it is
        below, so the sign carries the direction and the magnitude carries the
        confluence. A timeframe missing either value contributes nothing rather
        than a zero, because "no opinion" and "flat" are different.
        """
        score = 0
        for values in self.per_timeframe.values():
            f, s = values.get(fast), values.get(slow)
            if f is None or s is None:
                continue
            score += 1 if f > s else -1 if f < s else 0
        return score


@dataclass
class IndicatorEngine:
    """One ``IndicatorSet`` per (symbol, timeframe), and the snapshot over them."""

    timeframes: tuple[Timeframe, ...] = (Timeframe.M5, Timeframe.M15, Timeframe.H1)
    _sets: dict[tuple[str, Timeframe], IndicatorSet] = field(default_factory=dict)

    def set_for(self, symbol: str, timeframe: Timeframe) -> IndicatorSet:
        key = (symbol, timeframe)
        if key not in self._sets:
            self._sets[key] = IndicatorSet(symbol=symbol, timeframe=timeframe)
        return self._sets[key]

    def update(self, bar: Bar) -> None:
        self.set_for(bar.symbol, bar.timeframe).update(bar)

    def mark_stale(self, symbol: str, reason: str) -> None:
        """A feed gap invalidates every timeframe for that symbol.

        Every one, not just the fastest: a fifteen-minute hole is a missing 5m
        bar and a corrupted hourly bar alike, and the hourly one is the harder
        of the two to notice.
        """
        for (sym, _tf), indicator_set in self._sets.items():
            if sym == symbol:
                indicator_set.mark_stale(reason)

    def mark_all_stale(self, reason: str) -> None:
        for indicator_set in self._sets.values():
            indicator_set.mark_stale(reason)

    def snapshot(self, symbol: str) -> MultiTimeframeSnapshot:
        """Assemble the cross-timeframe view.

        ``all_ready`` false means the signal engine must not evaluate this
        symbol. The values are still returned so a health panel can show what
        is missing rather than an unexplained refusal.
        """
        per_timeframe: dict[Timeframe, dict[str, float | None]] = {}
        not_ready: dict[Timeframe, list[str]] = {}
        ready = True
        for timeframe in self.timeframes:
            indicator_set = self._sets.get((symbol, timeframe))
            if indicator_set is None:
                ready = False
                not_ready[timeframe] = ["<no bars yet>"]
                continue
            per_timeframe[timeframe] = indicator_set.values()
            if not indicator_set.is_ready:
                ready = False
                pending = indicator_set.not_ready()
                not_ready[timeframe] = pending or (["<stale>"] if indicator_set.stale else [])
        return MultiTimeframeSnapshot(
            symbol=symbol,
            per_timeframe=per_timeframe,
            all_ready=ready,
            not_ready=not_ready,
        )

    def ready_symbols(self) -> list[str]:
        symbols = {sym for sym, _ in self._sets}
        return sorted(s for s in symbols if self.snapshot(s).all_ready)


@dataclass
class WarmUpReport:
    """What warm-up achieved, per symbol. Read before the session opens."""

    warmed: list[str] = field(default_factory=list)
    insufficient: dict[str, list[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.insufficient

    def summary(self) -> str:
        return (
            f"{len(self.warmed)} symbols warm, {len(self.insufficient)} excluded for "
            f"insufficient history"
        )


def warm_up_symbols(
    engine: IndicatorEngine,
    history: dict[tuple[str, Timeframe], list[Bar]],
) -> WarmUpReport:
    """Warm every set from stored history, and report what could not warm.

    E06-S03's acceptance criterion: a symbol with insufficient history is
    EXCLUDED, not traded on bad values. So this returns which symbols failed
    rather than logging and continuing — the caller is expected to drop them
    from the watchlist, and a return value is harder to ignore than a log line.
    """
    report = WarmUpReport()
    symbols = {symbol for symbol, _ in history}
    for symbol in sorted(symbols):
        for timeframe in engine.timeframes:
            bars = history.get((symbol, timeframe), [])
            engine.set_for(symbol, timeframe).warm_up(bars)
        snapshot = engine.snapshot(symbol)
        if snapshot.all_ready:
            report.warmed.append(symbol)
        else:
            missing = sorted({name for names in snapshot.not_ready.values() for name in names})
            report.insufficient[symbol] = missing
            log.warning(
                "%s excluded: insufficient history for %s",
                symbol,
                ", ".join(missing[:6]),
            )
    log.info("warm-up complete: %s", report.summary())
    return report
