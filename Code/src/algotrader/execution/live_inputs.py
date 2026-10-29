"""The live risk inputs: ATR, per-share margin and correlations (E15-S14).

:class:`~algotrader.execution.session_context.NoProducerYet` answered all three
as unknown, so the risk engine rejected every signal. This module produces
them. Each producer answers ``None`` (or, for correlations, leaves a symbol
out) whenever it cannot vouch for its answer, and the engine still refuses on
that - this story makes the inputs KNOWABLE, never assumed.

## The design record

**The decision.** Three producers, one per input, each reading the source that
is authoritative for that number:

* **ATR** from the indicator state the strategy side publishes to
  ``state:indicator:{symbol}:{timeframe}``, on the timeframe the signal's own
  strategy declares (:class:`PublishedATR`).
* **Per-share margin** from the broker - Kite's ``/margins/orders`` for one
  share, shaped as the entry will be (:class:`BrokerMarginPerShare`).
* **Correlations** from daily closes in Postgres, aligned on common dates,
  through :func:`~algotrader.execution.risk.correlation.correlations_against`
  (:class:`DailyCloseCorrelations`).

**The alternative rejected, for ATR:** computing it here from bars in Postgres.
It is self-contained and testable today, and it is wrong in production: live
bars are not written to Postgres during the session (``LOW_LEVEL_ARCHITECTURE.md
§4.1`` - L1 streams, flushed to L2 afterwards), so that ATR would be a day
stale all session and every signal would be refused. The hot indicator state
is where the design puts a live ATR, and it is the ATR the strategy evaluated
on - one number, not two that can disagree. *Carrying the ATR on the
Recommendation* was rejected too: it is the AI/deterministic boundary, and a
number the sizer divides by must not arrive from the side that is not trusted
to size (invariant 1's reasoning).

**The alternative rejected, for margin:** a fixed leverage (price / 5). It is
right until the broker changes a stock's margin - a surveillance stage, a
volatility rule, an exchange circular - and then sizes against a number the
broker no longer uses. The broker's own answer for the order about to be sent
is the only one that cannot drift. It costs one read per signal, on the DATA
bucket (see :meth:`KiteTradingAdapter.margin_required`).

**The failure each prevents.** An ATR from a stale or unready state sizes from
a range that no longer holds - every position wrong by the same factor, all
day. A margin guessed low lets the margin clamp pass a quantity the account
cannot carry (the broker then rejects it; guessed high, the system under-
trades for a reason nobody can see). A correlation across misaligned dates
reads two names as unrelated when they are one bet.

**What would make this wrong.** A strategy whose sizing ATR should not be its
own timeframe's ``atr_14`` (a 1h-ATR stop on a 5m entry): the mapping would
then need to come from the strategy's stop primitive, not its timeframe. And a
broker whose margin answer depends on the account's open positions in a way
that a one-share lookup misreads (Kite's does net against an OPPOSITE position
in the same symbol; the engine refuses a second position in a held symbol
before sizing, so that answer is never used).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Protocol

from algotrader.common.calendar import IST
from algotrader.common.enums import Direction, Side, Timeframe
from algotrader.common.models.market import IndicatorSnapshot
from algotrader.common.redis import keys, state
from algotrader.common.text import one_safe_line
from algotrader.execution.risk.correlation import (
    CORRELATION_WINDOW_SESSIONS,
    CorrelationError,
    correlations_against,
)

log = logging.getLogger(__name__)

__all__ = [
    "ATR_FRESHNESS_GRACE",
    "AtrReading",
    "BrokerMarginPerShare",
    "DailyCloseCorrelations",
    "LiveSessionFacts",
    "PublishedATR",
    "daily_correlations",
    "read_atr",
    "strategy_timeframes",
]

#: How late a published state may be past the bar AFTER the one it describes.
#: The newest closed bar is legitimately up to one interval old; past that, a
#: bar has closed and its state has not arrived. The grace covers sealing (the
#: first tick after a boundary, or the carry-forward timer) and the publish
#: itself. A judgement: long enough not to refuse a healthy pipeline at the
#: boundary, far shorter than the interval it guards.
ATR_FRESHNESS_GRACE: Final = dt.timedelta(seconds=30)
#: A state dated this far in the future is not skew - it describes a bar that
#: has not closed, which means a forming bar was published.
MAX_STATE_SKEW: Final = dt.timedelta(seconds=5)
#: The margin read happens under the service's lock, which the square-off
#: timer also takes. Bounded, so a hung call delays a time exit by seconds,
#: not by the SDK's own timeout.
DEFAULT_MARGIN_TIMEOUT_SECONDS: Final = 5.0
#: Daily bars fetched beyond the window, so that a few sessions one symbol
#: did not trade (a suspension) do not leave the pair short of the window.
DAILY_SLACK_SESSIONS: Final = 10
#: The timeframes a live state exists for. Daily and weekly are not built from
#: ticks during the session, so a strategy declaring one has no live ATR.
_INTRADAY: Final = frozenset({Timeframe.M1, Timeframe.M5, Timeframe.M15, Timeframe.H1})


# ---------------------------------------------------------------------------
# Strategy -> timeframe
# ---------------------------------------------------------------------------


class _Applicability(Protocol):
    @property
    def timeframe(self) -> Timeframe: ...


class _Strategy(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def applicability(self) -> _Applicability: ...


def strategy_timeframes(documents: Iterable[_Strategy]) -> dict[str, Timeframe]:
    """Each strategy's declared timeframe, by id: whose ATR sizes its signals.

    Built at the composition root from the same strategy documents the
    strategy side loads. A signal whose strategy is not here has no ATR this
    service can find, and is refused rather than sized off another strategy's
    timeframe.
    """
    out: dict[str, Timeframe] = {}
    for doc in documents:
        timeframe = doc.applicability.timeframe
        if out.get(doc.id, timeframe) != timeframe:
            raise ValueError(
                f"strategy {doc.id} is declared twice with different timeframes; "
                f"which ATR sizes its signals would depend on load order"
            )
        out[doc.id] = timeframe
    return out


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AtrReading:
    """An ATR this service can size from, or why there is none."""

    atr: Decimal | None
    refused: str | None = None


def read_atr(
    snapshot: IndicatorSnapshot | None,
    *,
    symbol: str,
    timeframe: Timeframe,
    now: dt.datetime,
    grace: dt.timedelta = ATR_FRESHNESS_GRACE,
) -> AtrReading:
    """Judge a published state. Pure: every refusal is a decision worth a test.

    In order, because each later question assumes the earlier answer: there is
    a state; it describes this symbol on this timeframe; it is ready; it is not
    from the future; it is at most one bar (plus grace) behind; it has an ATR
    the sizer can divide by.
    """
    if snapshot is None:
        return AtrReading(None, "no published indicator state (absent or unparseable)")
    if snapshot.symbol != symbol or snapshot.timeframe != timeframe:
        # The key names both; a state that disagrees with its own key was
        # written by something other than publish_indicator_state.
        return AtrReading(
            None,
            f"the state at this key describes {one_safe_line(snapshot.symbol)} "
            f"{snapshot.timeframe.value}",
        )
    if not snapshot.ready:
        return AtrReading(None, "the indicators are not ready (warming up, or a feed gap)")
    if snapshot.as_of - now > MAX_STATE_SKEW:
        return AtrReading(
            None, f"the state is dated {snapshot.as_of.isoformat()}, after now: a forming bar"
        )
    age = now - snapshot.as_of
    limit = dt.timedelta(seconds=timeframe.seconds) + grace
    if age > limit:
        return AtrReading(
            None,
            f"the state is {int(age.total_seconds())}s old; a {timeframe.value} state "
            f"older than {int(limit.total_seconds())}s has missed a bar",
        )
    if snapshot.atr_14 is None or snapshot.atr_14 <= 0:
        return AtrReading(None, f"the state carries no usable ATR ({snapshot.atr_14})")
    return AtrReading(snapshot.atr_14)


class PublishedATR:
    """``SessionFacts.atr``: the published ``atr_14`` on the strategy's timeframe."""

    def __init__(
        self,
        redis: Any,
        *,
        timeframes: Mapping[str, Timeframe],
        grace: dt.timedelta = ATR_FRESHNESS_GRACE,
    ) -> None:
        if not timeframes:
            raise ValueError(
                "PublishedATR needs each strategy's timeframe; with none, every "
                "signal's ATR is unknown and every signal is refused"
            )
        live = {sid: tf for sid, tf in timeframes.items() if tf in _INTRADAY}
        for sid in sorted(set(timeframes) - set(live)):
            log.error(
                "strategy %s declares %s, which has no live indicator state; "
                "its signals will be refused for want of an ATR",
                sid,
                timeframes[sid].value,
            )
        self._redis = redis
        self._timeframes = dict(live)
        self._grace = grace

    async def atr(self, symbol: str, *, strategy_id: str, now: dt.datetime) -> Decimal | None:
        timeframe = self._timeframes.get(strategy_id)
        if timeframe is None:
            log.warning(
                "no live timeframe is known for strategy %s; the ATR for %s is unknown",
                one_safe_line(strategy_id),
                one_safe_line(symbol),
            )
            return None
        snapshot = await state.get_state(
            self._redis, keys.indicator_state(symbol, timeframe), IndicatorSnapshot
        )
        reading = read_atr(snapshot, symbol=symbol, timeframe=timeframe, now=now, grace=self._grace)
        if reading.refused is not None:
            log.warning(
                "ATR for %s on %s refused: %s",
                one_safe_line(symbol),
                timeframe.value,
                reading.refused,
            )
        return reading.atr


# ---------------------------------------------------------------------------
# Per-share margin
# ---------------------------------------------------------------------------


class OrderMarginSource(Protocol):
    async def margin_required(self, symbol: str, *, side: Side, quantity: int = 1) -> Decimal: ...


class BrokerMarginPerShare:
    """``SessionFacts.margin_per_share``: the broker's margin for one share.

    ``price`` is part of the contract and unused here: the broker prices a
    MARKET order's margin at its own last traded price, which is what the
    entry will be charged. A failure - an error, the rate limiter, the
    timeout - propagates; the context builder turns it into ``None`` and logs
    it, as it does for every live input.
    """

    def __init__(
        self,
        broker: OrderMarginSource,
        *,
        timeout_seconds: float = DEFAULT_MARGIN_TIMEOUT_SECONDS,
    ) -> None:
        if not timeout_seconds > 0:
            raise ValueError(f"timeout_seconds must be positive, not {timeout_seconds}")
        self._broker = broker
        self._timeout = timeout_seconds

    async def margin_per_share(
        self, symbol: str, price: Decimal, *, direction: Direction, now: dt.datetime
    ) -> Decimal | None:
        side = Side.BUY if direction is Direction.LONG else Side.SELL
        total = await asyncio.wait_for(
            self._broker.margin_required(symbol, side=side, quantity=1), self._timeout
        )
        if total <= 0:
            # Not "free": a one-share entry blocks margin. Zero is the broker
            # netting against something, or an answer misread - and the sizer
            # divides by it.
            log.warning(
                "the broker's margin for one %s share is %s; treated as unknown",
                one_safe_line(symbol),
                total,
            )
            return None
        return total


# ---------------------------------------------------------------------------
# Correlations
# ---------------------------------------------------------------------------


def _closes_by_date(
    bars: Sequence[Mapping[str, Any]], *, before: dt.date
) -> dict[dt.date, Decimal]:
    """Daily closes keyed by IST trade date, excluding ``before`` and later.

    Today's bar is excluded even if one exists: the correlation is a
    pre-market quantity (``correlation.py``'s design record), and a partial
    daily bar would make it an intraday one.
    """
    out: dict[dt.date, Decimal] = {}
    for bar in bars:
        day = bar["open_ts"].astimezone(IST).date()
        if day < before:
            out[day] = Decimal(bar["close"])
    return out


def daily_correlations(
    candidate: str,
    history: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    against: Sequence[str],
    today: dt.date,
    last_session: dt.date,
    window: int = CORRELATION_WINDOW_SESSIONS,
) -> dict[str, Decimal]:
    """The candidate's correlation with each of ``against``, where computable.

    **Aligned on common dates.** ``correlations_against`` pairs returns by
    position, which is right only when both series cover the same sessions. A
    symbol suspended for three days would otherwise pair Monday's return with
    Thursday's for the rest of the window. Each pair is restricted to the
    sessions both traded.

    **Current or absent.** A series whose last close is not ``last_session``
    is missing at least yesterday, so the history behind it has stopped being
    maintained; it is left out - unknown - rather than measured over a window
    that ended somewhere in the past.
    """
    candidate_closes = _closes_by_date(history.get(candidate, ()), before=today)
    if not candidate_closes or max(candidate_closes) != last_session:
        last = max(candidate_closes) if candidate_closes else None
        log.warning(
            "daily history for %s ends %s, not %s; every correlation against it is unknown",
            one_safe_line(candidate),
            last,
            last_session,
        )
        return {}
    out: dict[str, Decimal] = {}
    for symbol in against:
        other = _closes_by_date(history.get(symbol, ()), before=today)
        if not other or max(other) != last_session:
            log.warning(
                "daily history for %s ends %s, not %s; its correlation is unknown",
                one_safe_line(symbol),
                max(other) if other else None,
                last_session,
            )
            continue
        common = sorted(set(candidate_closes) & set(other))[-(window + 1) :]
        try:
            pair = correlations_against(
                candidate,
                {
                    candidate: [candidate_closes[d] for d in common],
                    symbol: [other[d] for d in common],
                },
                against=[symbol],
                window=window,
            )
        except CorrelationError as exc:
            log.warning(
                "no correlation for %s against %s: %s",
                one_safe_line(candidate),
                one_safe_line(symbol),
                one_safe_line(str(exc)),
            )
            continue
        out.update(pair)
    return out


class DailyBars(Protocol):
    async def warm_up_batch(
        self, symbols: Sequence[str], timeframe: Timeframe, bars_each: int
    ) -> dict[str, list[dict[str, Any]]]: ...


class TradingDays(Protocol):
    def previous_trading_day(self, d: dt.date) -> dt.date: ...


class DailyCloseCorrelations:
    """``SessionFacts.correlations``: from daily closes, in one query."""

    def __init__(
        self,
        bars: DailyBars,
        *,
        calendar: TradingDays,
        window: int = CORRELATION_WINDOW_SESSIONS,
    ) -> None:
        if not callable(getattr(calendar, "previous_trading_day", None)):
            raise TypeError("DailyCloseCorrelations needs a calendar with previous_trading_day()")
        self._bars = bars
        self._calendar = calendar
        self._window = window

    async def correlations(
        self, symbol: str, against: Sequence[str], *, now: dt.datetime
    ) -> Mapping[str, Decimal]:
        if not against:
            return {}
        today = now.astimezone(IST).date()
        history = await self._bars.warm_up_batch(
            [symbol, *against], Timeframe.D1, self._window + 1 + DAILY_SLACK_SESSIONS
        )
        return daily_correlations(
            symbol,
            history,
            against=against,
            today=today,
            last_session=self._calendar.previous_trading_day(today),
            window=self._window,
        )


# ---------------------------------------------------------------------------
# The composite the service is handed
# ---------------------------------------------------------------------------


class _Health(Protocol):
    async def unhealthy_services(self, *, now: dt.datetime) -> tuple[str, ...] | None: ...


class _Atr(Protocol):
    async def atr(self, symbol: str, *, strategy_id: str, now: dt.datetime) -> Decimal | None: ...


class _Margin(Protocol):
    async def margin_per_share(
        self, symbol: str, price: Decimal, *, direction: Direction, now: dt.datetime
    ) -> Decimal | None: ...


class _Correlations(Protocol):
    async def correlations(
        self, symbol: str, against: Sequence[str], *, now: dt.datetime
    ) -> Mapping[str, Decimal]: ...


class LiveSessionFacts:
    """``SessionFacts`` from the live producers.

    ``health`` is a separate argument because its producer is E20-S05: until
    it exists, pass :class:`~algotrader.execution.session_context.NoProducerYet`
    and the engine keeps refusing every signal on health alone - correctly.
    """

    def __init__(
        self, *, health: _Health, atr: _Atr, margin: _Margin, correlations: _Correlations
    ) -> None:
        self._health = health
        self._atr = atr
        self._margin = margin
        self._correlations = correlations

    async def unhealthy_services(self, *, now: dt.datetime) -> tuple[str, ...] | None:
        return await self._health.unhealthy_services(now=now)

    async def atr(self, symbol: str, *, strategy_id: str, now: dt.datetime) -> Decimal | None:
        return await self._atr.atr(symbol, strategy_id=strategy_id, now=now)

    async def margin_per_share(
        self, symbol: str, price: Decimal, *, direction: Direction, now: dt.datetime
    ) -> Decimal | None:
        return await self._margin.margin_per_share(symbol, price, direction=direction, now=now)

    async def correlations(
        self, symbol: str, against: Sequence[str], *, now: dt.datetime
    ) -> Mapping[str, Decimal]:
        return await self._correlations.correlations(symbol, against, now=now)
