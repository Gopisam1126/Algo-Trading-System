"""Bar construction from ticks (E05-S06, E05-S07).

**Bars align to the session, not the clock.** A 15-minute bar runs 09:15–09:30,
never 09:00–09:15, because the session starts at 09:15 and a bar straddling the
open would mix two regimes into one candle. ``MarketCalendar.bar_open_time`` owns
that arithmetic; this module asks it rather than recomputing it, so there is one
definition of where a boundary falls.

Two hazards shape the design.

**Cascading is not free.** Building 5m from 1m, 15m from 5m and so on is
efficient and means one bad 1m bar propagates silently into every higher
timeframe with nothing to catch it. So each timeframe is built independently
from ticks. It costs a little more arithmetic and removes a whole class of
correlated corruption.

**The closing auction is not continuous trading.** For CAS-scope stocks the
15:15–15:30 window is a call auction: prints there are not comparable to
intraday prints and must not shape a bar an indicator will read. Bars covering
it are marked, so a consumer can decide rather than being silently fed them.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from decimal import Decimal

from algotrader.common.calendar import CAS_CONTINUOUS_END, IST, MarketCalendar
from algotrader.common.enums import Timeframe
from algotrader.common.models.market import Bar, Tick

log = logging.getLogger(__name__)

#: Seconds in each timeframe. Weekly is absent on purpose: a week is not a fixed
#: number of seconds once holidays exist, so it is aggregated from daily bars in
#: E03-S05 rather than built from ticks.
_INTERVAL_SECONDS: dict[Timeframe, int] = {
    Timeframe.M1: 60,
    Timeframe.M5: 300,
    Timeframe.M15: 900,
    Timeframe.H1: 3600,
}


class BarError(RuntimeError):
    """A bar could not be built or would be incoherent."""


@dataclass
class _Forming:
    """Mutable accumulator. Becomes an immutable ``Bar`` when sealed."""

    symbol: str
    timeframe: Timeframe
    open_ts: dt.datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int = 0
    trade_count: int = 0
    #: Cumulative session volume at the first tick of this bar. Bar volume is
    #: the DIFFERENCE, because the feed reports a running session total — using
    #: it directly would make every bar's volume the whole day's.
    volume_at_open: int = 0
    covers_call_auction: bool = False

    def apply(self, tick: Tick, *, in_call_auction: bool) -> None:
        self.high = max(self.high, tick.ltp)
        self.low = min(self.low, tick.ltp)
        self.close = tick.ltp
        self.trade_count += 1
        if tick.volume > self.volume_at_open:
            self.volume = tick.volume - self.volume_at_open
        if in_call_auction:
            self.covers_call_auction = True

    def seal(self, *, synthetic: bool = False) -> Bar:
        return Bar(
            symbol=self.symbol,
            timeframe=self.timeframe,
            open_ts=self.open_ts,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=max(0, self.volume),
            trade_count=self.trade_count or None,
            synthetic=synthetic,
            is_final=True,
        )


@dataclass
class BarBuilder:
    """Builds one timeframe for one symbol.

    Holds the forming bar and seals it when a tick crosses the boundary. The
    seal is driven by an incoming tick rather than by a timer: a timer would
    seal a bar at wall-clock 09:30:00 even if the last trade was at 09:29:58,
    and the two disagree about which bar that trade belongs to.
    """

    symbol: str
    timeframe: Timeframe
    calendar: MarketCalendar
    _forming: _Forming | None = None
    #: Latest sealed boundary. Anything at or before it is late.
    _sealed_through: dt.datetime | None = None
    late_ticks: int = 0

    def __post_init__(self) -> None:
        if self.timeframe not in _INTERVAL_SECONDS and self.timeframe is not Timeframe.D1:
            raise BarError(
                f"{self.timeframe} cannot be built from ticks. Weekly bars are "
                f"aggregated from daily (E03-S05) because a week is not a fixed span."
            )

    # -- boundaries ----------------------------------------------------------

    def bar_open_for(self, moment: dt.datetime) -> dt.datetime:
        """Which bar this instant belongs to, aligned to the session start."""
        if self.timeframe is Timeframe.D1:
            ist = moment.astimezone(IST)
            return self.calendar.session_bounds(ist.date())[0]
        return self.calendar.bar_open_time(moment, _INTERVAL_SECONDS[self.timeframe])

    def _in_call_auction(self, moment: dt.datetime, *, is_cas_stock: bool) -> bool:
        if not is_cas_stock:
            return False
        return moment.astimezone(IST).time() >= CAS_CONTINUOUS_END

    # -- the tick path -------------------------------------------------------

    def add(self, tick: Tick, *, is_cas_stock: bool = False) -> Bar | None:
        """Apply a tick. Returns a sealed bar when this tick closed one.

        A tick belonging to an already-sealed bar is DROPPED and counted, never
        applied. Reopening a sealed bar would mutate a value downstream has
        already acted on — the opening range in particular is sealed at 09:30
        and every level derived from it would shift underneath positions already
        sized against it.
        """
        moment = tick.exchange_ts
        open_ts = self.bar_open_for(moment)

        if self._sealed_through is not None and open_ts <= self._sealed_through:
            self.late_ticks += 1
            log.debug(
                "late tick for %s %s: belongs to the bar opening %s, already sealed",
                self.symbol,
                self.timeframe.value,
                open_ts.isoformat(),
            )
            return None

        in_auction = self._in_call_auction(moment, is_cas_stock=is_cas_stock)

        if self._forming is None:
            self._forming = self._start(tick, open_ts, in_auction)
            return None

        if open_ts > self._forming.open_ts:
            sealed = self._forming.seal()
            self._sealed_through = self._forming.open_ts
            self._forming = self._start(tick, open_ts, in_auction)
            return sealed

        self._forming.apply(tick, in_call_auction=in_auction)
        return None

    def _start(self, tick: Tick, open_ts: dt.datetime, in_auction: bool) -> _Forming:
        return _Forming(
            symbol=self.symbol,
            timeframe=self.timeframe,
            open_ts=open_ts,
            open=tick.ltp,
            high=tick.ltp,
            low=tick.ltp,
            close=tick.ltp,
            volume_at_open=tick.volume,
            trade_count=1,
            covers_call_auction=in_auction,
        )

    # -- explicit control ----------------------------------------------------

    def snapshot(self) -> Bar | None:
        """The bar in progress, marked ``is_final=False``.

        Strategies evaluate on final bars only — acting on a forming bar is
        look-ahead bias in live trading, because the bar can still move against
        you before it closes. This exists for display and for the health panel.
        """
        if self._forming is None:
            return None
        forming = self._forming.seal()
        return forming.model_copy(update={"is_final": False})

    def force_seal(self) -> Bar | None:
        """Seal at the end of a session, when no further tick will arrive."""
        if self._forming is None:
            return None
        sealed = self._forming.seal()
        self._sealed_through = self._forming.open_ts
        self._forming = None
        return sealed

    def carry_forward(self, open_ts: dt.datetime) -> Bar | None:
        """A synthetic bar for an interval in which nothing traded (E05-S07).

        Flat OHLC at the previous close and zero volume, flagged ``synthetic``.
        The flag is the whole point: an illiquid symbol printing a run of
        identical bars looks like a volatility collapse to any indicator that
        cannot tell the difference between "did not move" and "did not trade".
        """
        if self._sealed_through is None or self._forming is not None:
            return None
        last_close = self._last_close
        if last_close is None:
            return None
        bar = Bar(
            symbol=self.symbol,
            timeframe=self.timeframe,
            open_ts=open_ts,
            open=last_close,
            high=last_close,
            low=last_close,
            close=last_close,
            volume=0,
            trade_count=None,
            synthetic=True,
            is_final=True,
        )
        self._sealed_through = open_ts
        return bar

    _last_close: Decimal | None = field(default=None, repr=False)

    def remember_close(self, close: Decimal) -> None:
        """Record a close so :meth:`carry_forward` has something to repeat."""
        self._last_close = close


@dataclass
class MultiTimeframeBuilder:
    """One symbol, every timeframe, each built independently from ticks.

    Independent rather than cascaded: see the module docstring. The cost is
    re-doing some arithmetic; the benefit is that a defect in the 1m path cannot
    silently reappear in the hourly series that a higher-timeframe filter reads.
    """

    symbol: str
    calendar: MarketCalendar
    timeframes: tuple[Timeframe, ...] = (
        Timeframe.M1,
        Timeframe.M5,
        Timeframe.M15,
        Timeframe.H1,
    )
    is_cas_stock: bool = False
    _builders: dict[Timeframe, BarBuilder] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for timeframe in self.timeframes:
            self._builders[timeframe] = BarBuilder(
                symbol=self.symbol, timeframe=timeframe, calendar=self.calendar
            )

    def add(self, tick: Tick) -> list[Bar]:
        """Apply one tick to every timeframe. Returns whatever sealed."""
        sealed: list[Bar] = []
        for builder in self._builders.values():
            bar = builder.add(tick, is_cas_stock=self.is_cas_stock)
            if bar is not None:
                builder.remember_close(bar.close)
                sealed.append(bar)
        return sealed

    def snapshots(self) -> dict[Timeframe, Bar]:
        out: dict[Timeframe, Bar] = {}
        for timeframe, builder in self._builders.items():
            snap = builder.snapshot()
            if snap is not None:
                out[timeframe] = snap
        return out

    def force_seal_all(self) -> list[Bar]:
        sealed = []
        for builder in self._builders.values():
            bar = builder.force_seal()
            if bar is not None:
                sealed.append(bar)
        return sealed

    @property
    def late_tick_count(self) -> int:
        """Ticks that arrived after their bar had sealed.

        A rising count is a real signal: it means the feed is delivering
        out of order, and every bar boundary becomes approximate.
        """
        return sum(b.late_ticks for b in self._builders.values())
