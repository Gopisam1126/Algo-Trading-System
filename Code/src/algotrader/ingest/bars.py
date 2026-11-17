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

**Only continuous trading makes bars.** A tick stamped outside the stock's
continuous session - pre-open (before 09:15), the closing auction of a CAS-scope
stock (15:15 onwards), or after 15:30 - is DROPPED and counted
(``out_of_session_ticks``), never folded in. Pre-open and auction prints are
call-auction prices, not comparable to continuous prints, and the first 5m bar
is the one the opening range is built from.

CHANGED 16 Nov 2026 (audit TRD-013, TRD-020). This said bars covering the
auction "are marked": the flag was computed while a bar formed and discarded
when it sealed, so no consumer ever saw it, and pre-open ticks were clamped into
the 09:15 bar. Dropping is what the paragraph above always asked for.

**Bar volume is a difference of running totals** (audit TRD-011). The feed
reports the session's cumulative volume, so a bar's volume is the last total
inside it minus the last total BEFORE it - not minus the total at its own first
tick, which lost every share that first tick carried (a one-tick bar read 0).
The session's first bar starts from 0, so it holds everything the exchange
reports traded before it closed, the pre-open auction included: splitting that
out needs a pre-open figure the feed is not known to send. A builder that starts
mid-session has no earlier total, and starts from its first tick's - under-
counting one tick, rather than calling the whole morning one bar's volume.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from algotrader.common.calendar import IST, MarketCalendar
from algotrader.common.enums import Timeframe
from algotrader.common.events import stream
from algotrader.common.events.envelope import Envelope
from algotrader.common.models.market import Bar, Tick
from algotrader.common.redis import keys

if TYPE_CHECKING:
    import redis.asyncio as aioredis

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
    #: Cumulative session volume just BEFORE this bar (see the module docstring).
    volume_base: int
    #: The highest cumulative session volume seen inside this bar.
    last_cumulative: int
    trade_count: int = 0

    @property
    def volume(self) -> int:
        return max(0, self.last_cumulative - self.volume_base)

    def apply(self, tick: Tick) -> None:
        self.high = max(self.high, tick.ltp)
        self.low = min(self.low, tick.ltp)
        self.close = tick.ltp
        self.trade_count += 1
        # A running total never falls; a lower one is an out-of-order packet.
        self.last_cumulative = max(self.last_cumulative, tick.volume)

    def seal(self, *, synthetic: bool = False) -> Bar:
        return Bar(
            symbol=self.symbol,
            timeframe=self.timeframe,
            open_ts=self.open_ts,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
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
    #: Ticks outside the stock's continuous session, dropped (audit TRD-013).
    out_of_session_ticks: int = 0
    #: The latest cumulative session volume seen, and the IST date it is for.
    _cumulative: int | None = None
    _cumulative_date: dt.date | None = None
    _last_close: Decimal | None = field(default=None, repr=False)

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

    # -- the tick path -------------------------------------------------------

    def add(self, tick: Tick, *, is_cas_stock: bool = False) -> Bar | None:
        """Apply a tick. Returns a sealed bar when this tick closed one.

        A tick outside the stock's continuous session is DROPPED and counted
        (see the module docstring). A tick belonging to an already-sealed bar is
        DROPPED and counted, never applied. Reopening a sealed bar would mutate
        a value downstream has already acted on — the opening range in
        particular is sealed at 09:30 and every level derived from it would
        shift underneath positions already sized against it.
        """
        moment = tick.exchange_ts
        if not self.calendar.is_continuous_for(moment, is_cas_stock=is_cas_stock):
            self.out_of_session_ticks += 1
            log.debug(
                "out-of-session tick for %s %s at %s dropped",
                self.symbol,
                self.timeframe.value,
                moment.isoformat(),
            )
            return None
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

        sealed: Bar | None = None
        if self._forming is not None and open_ts > self._forming.open_ts:
            sealed = self._seal()
        if self._forming is None:
            self._forming = self._start(tick, open_ts)
        else:
            self._forming.apply(tick)
        self._note_cumulative(tick)
        return sealed

    def _note_cumulative(self, tick: Tick) -> None:
        day = tick.exchange_ts.astimezone(IST).date()
        if self._cumulative is None or self._cumulative_date != day:
            self._cumulative, self._cumulative_date = tick.volume, day
        else:
            self._cumulative = max(self._cumulative, tick.volume)

    def _base_for(self, tick: Tick, open_ts: dt.datetime) -> int:
        """The cumulative volume just before the bar this tick opens."""
        day = tick.exchange_ts.astimezone(IST).date()
        if self._cumulative is not None and self._cumulative_date == day:
            return self._cumulative
        if open_ts == self.calendar.session_bounds(day)[0]:
            return 0  # the session's first bar: everything traded so far
        return tick.volume  # started mid-session: nothing earlier is known

    def _start(self, tick: Tick, open_ts: dt.datetime) -> _Forming:
        return _Forming(
            symbol=self.symbol,
            timeframe=self.timeframe,
            open_ts=open_ts,
            open=tick.ltp,
            high=tick.ltp,
            low=tick.ltp,
            close=tick.ltp,
            volume_base=self._base_for(tick, open_ts),
            last_cumulative=tick.volume,
            trade_count=1,
        )

    def _seal(self) -> Bar:
        """Seal the forming bar - the ONE seal path, so every one records its
        close (audit TRD-024: ``force_seal`` did not, and a carry-forward after
        it repeated a stale close)."""
        if self._forming is None:
            raise BarError("nothing is forming")
        sealed = self._forming.seal()
        self._sealed_through = self._forming.open_ts
        self._forming = None
        self._last_close = sealed.close
        return sealed

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
        return self._seal()

    def carry_forward(self, open_ts: dt.datetime) -> Bar | None:
        """A synthetic bar for an interval in which nothing traded (E05-S07).

        Flat OHLC at the previous close and zero volume, flagged ``synthetic``.
        The flag is the whole point: an illiquid symbol printing a run of
        identical bars looks like a volatility collapse to any indicator that
        cannot tell the difference between "did not move" and "did not trade".

        NOT called by the live tick path, by decision (audit TRD-024, 16 Nov
        2026). An interval with no tick cannot be told apart, from here, from a
        feed that stopped delivering this symbol; a synthetic bar would dress
        the second up as the first. The live path leaves the interval empty,
        and the signal loop marks the symbol stale on the gap - fail closed,
        recovered by a re-warm. This serves a caller that KNOWS nothing traded
        (a backfill from the exchange's own record).
        """
        if self._sealed_through is None or self._forming is not None:
            return None
        if open_ts <= self._sealed_through:
            raise BarError(
                f"carry_forward({open_ts.isoformat()}) would reopen an interval already "
                f"sealed through {self._sealed_through.isoformat()}"
            )
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

    def remember_close(self, close: Decimal) -> None:
        """Record a close so :meth:`carry_forward` has something to repeat.

        Every seal path records its own close now; this is for a builder that
        resumes after a restart with the previous close known."""
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

    @property
    def out_of_session_tick_count(self) -> int:
        """Ticks dropped for falling outside continuous trading.

        Every builder sees every tick and applies the same session rule, so the
        count is one builder's, not the sum.
        """
        return max((b.out_of_session_ticks for b in self._builders.values()), default=0)


# ---------------------------------------------------------------------------
# Publishing sealed bars (E13-S01)
# ---------------------------------------------------------------------------

#: Bars are addressed by what they are, not by when they were sent: the same
#: bar republished (a producer retry, a replay) carries the same id, which is
#: how a reader recognises it.
BAR_NAMESPACE = uuid.UUID("5b1e0c0e-13a1-4c6e-9b0b-0e13a1b4a201")


def bar_id(bar: Bar) -> uuid.UUID:
    """The identity of one sealed bar: symbol, timeframe, open time."""
    return uuid.uuid5(
        BAR_NAMESPACE,
        f"{bar.symbol}|{bar.timeframe.value}|{bar.open_ts.astimezone(dt.UTC).isoformat()}",
    )


def bar_close(bar: Bar) -> dt.datetime:
    """When the bar's interval ends: open + timeframe, or the session close.

    The day's last hourly bar opens at 15:15 and closes at 15:30, with the
    market - not at 16:15. ``Bar.close_ts``, by the one definition
    (``calendar.bar_close_time``, audit ENG-022), in UTC.
    """
    return bar.close_ts.astimezone(dt.UTC)


def publication_order(bars: Sequence[Bar]) -> list[Bar]:
    """By close time, and at one close time the LONGEST timeframe first.

    One tick at 10:15 seals the 5m, 15m and 1h bars together. The signal loop
    reads every bar stream in a single XREADGROUP, so writing the longer bars
    first means any read that returns the 5m bar also returns the 15m and 1h
    bars that close with it - and a 5m strategy is never evaluated against an
    hourly trend one bar behind the one that just closed. Written the other way
    round, which bar a decision saw would depend on read timing, and no
    backtest could reproduce it.
    """
    return sorted(bars, key=lambda b: (bar_close(b), -b.timeframe.seconds))


async def publish_sealed_bars(
    client: aioredis.Redis,
    bars: Sequence[Bar],
    *,
    now: dt.datetime,
    emitted_by: str = "market-ingest",
) -> list[str]:
    """XADD sealed bars to ``stream:bars:{tf}``, in :func:`publication_order`.

    Refuses a bar that is not final: a forming bar is look-ahead, and the
    stream's readers fold everything they are given into indicators.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    ordered = publication_order(bars)
    for bar in ordered:
        if not bar.is_final:
            raise ValueError(
                f"refusing to publish a forming bar for {bar.symbol} {bar.timeframe.value}"
            )
    entry_ids = []
    for bar in ordered:
        name = keys.stream_bars(bar.timeframe)
        envelope = Envelope(
            correlation_id=bar_id(bar),
            emitted_at=now,
            emitted_by=emitted_by,
            payload=json.loads(bar.model_dump_json()),
        )
        entry_ids.append(
            await stream.publish(client, name, envelope, maxlen=stream.default_maxlen(name))
        )
    return entry_ids
