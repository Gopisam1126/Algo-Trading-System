"""What each of the 27 primitives actually computes (E13-S01).

``registry.py`` declares the vocabulary — names, parameters, bounds. This is
the other half: the function behind each name. Until it existed a strategy
could be written, validated, hashed and stored, and then not run.

**Every evaluator is tri-state.** ``True`` and ``False`` mean the condition was
evaluated and holds or does not. ``None`` means UNKNOWN — the inputs were not
available, so the primitive is declining to answer.

That third state is not fastidiousness. ``ConditionGroup`` has a ``none_of``,
and a two-valued evaluator has to return ``False`` when it cannot compute — at
which point ``none_of`` reads "the forbidden condition is absent" and the
strategy enters. Missing data would become *permission*. Tri-state makes the
distinction between "the thing is not there" and "I could not look" survive all
the way to the composition, which is where it decides the outcome.

**Nothing here does I/O, and nothing mutates the context.** These are pure
functions over :class:`~algotrader.strategy.context.EvalContext`, which is what
makes replaying a backtest identical to running live.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

from algotrader.common.calendar import IST
from algotrader.common.enums import Direction, Regime, Timeframe
from algotrader.indicators.framework import to_decimal
from algotrader.strategy.context import EvalContext

#: An evaluator answers True, False, or None for UNKNOWN.
ConditionFn = Callable[[EvalContext, Mapping[str, Any]], "bool | None"]
StopFn = Callable[[EvalContext, Mapping[str, Any]], "Decimal | None"]


#: Longest fragment of strategy-supplied text an error message may echo.
#: Exception strings reach the application log, so echoing an unbounded
#: parameter turns a malformed strategy into a log-flooding primitive — and a
#: 10 KB value repeated once per symbol per bar fills a disk during a session.
_ECHO_LIMIT = 60


def _echo(value: object) -> str:
    """Quote a strategy-supplied value for an error message, bounded."""
    text = repr(value)
    return text if len(text) <= _ECHO_LIMIT else f"{text[:_ECHO_LIMIT]}...(truncated)"


class PrimitiveError(ValueError):
    """A primitive was invoked with parameters it cannot act on.

    Distinct from returning UNKNOWN. UNKNOWN means the market data was not
    available; this means the STRATEGY is malformed in a way the registry's
    type checks did not catch — a regime name that is not a regime, a time
    window that is not a time. That is a bug in the strategy, not a quiet
    session, so it is raised rather than absorbed.
    """


# ---------------------------------------------------------------------------
# Parameter coercion
# ---------------------------------------------------------------------------


def _num(params: Mapping[str, Any], name: str, default: Any = None) -> Decimal:
    value = params.get(name, default)
    if value is None:
        raise PrimitiveError(f"missing required numeric parameter {name!r}")
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError) as exc:
        raise PrimitiveError(f"parameter {name!r} is not numeric: {_echo(value)}") from exc


def _int(params: Mapping[str, Any], name: str, default: Any = None) -> int:
    value = params.get(name, default)
    if value is None:
        raise PrimitiveError(f"missing required integer parameter {name!r}")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise PrimitiveError(f"parameter {name!r} is not an integer: {_echo(value)}") from exc


def _bool(params: Mapping[str, Any], name: str, default: bool) -> bool:
    value = params.get(name, default)
    if not isinstance(value, bool):
        raise PrimitiveError(f"parameter {name!r} must be a bool, got {_echo(value)}")
    return value


def _str(params: Mapping[str, Any], name: str, default: str | None = None) -> str:
    value = params.get(name, default)
    if not isinstance(value, str):
        raise PrimitiveError(f"parameter {name!r} must be a string, got {_echo(value)}")
    return value


def _between(value: Decimal, low: Decimal, high: Decimal) -> bool:
    """Inclusive on both ends.

    Inclusive because the DSL's bounds read as ranges a human wrote — an RSI
    band of 30..70 is meant to include 30. Exclusive ends would make
    ``rsi_between(min=30, max=30)`` unsatisfiable rather than exact.
    """
    return low <= value <= high


def parse_timeframe(text: str) -> Timeframe:
    try:
        return Timeframe(text.strip())
    except ValueError as exc:
        raise PrimitiveError(
            f"{_echo(text)} is not a timeframe; expected one of {[t.value for t in Timeframe]}"
        ) from exc


def parse_regimes(text: str) -> frozenset[Regime]:
    names = [part.strip() for part in text.split(",") if part.strip()]
    if not names:
        raise PrimitiveError("regime_is was given no regimes")
    out = set()
    for name in names:
        try:
            out.add(Regime(name.upper()))
        except ValueError as exc:
            raise PrimitiveError(
                f"{_echo(name)} is not a regime; expected one of {[r.value for r in Regime]}"
            ) from exc
    return frozenset(out)


def parse_clock(text: str) -> dt.time:
    try:
        hour, minute = text.strip().split(":")
        return dt.time(int(hour), int(minute))
    except (ValueError, TypeError) as exc:
        raise PrimitiveError(f"{_echo(text)} is not a HH:MM time") from exc


# ---------------------------------------------------------------------------
# price
# ---------------------------------------------------------------------------


def price_breaks_level(ctx: EvalContext, p: Mapping[str, Any]) -> bool | None:
    # Parameters are checked BEFORE market data is consulted. Reversed, a
    # strategy with a misspelled direction would return UNKNOWN whenever the
    # level happened to be unavailable — which reads as "conditions not met"
    # and hides a malformed strategy behind a quiet market.
    direction = _str(p, "direction")
    if direction not in ("above", "below"):
        raise PrimitiveError(f"direction must be 'above' or 'below', got {_echo(direction)}")
    buffer_pct = _num(p, "buffer_pct", "0.05")

    level = ctx.named_level(_str(p, "level"))
    if level is None or level <= 0:
        return None
    buffer = level * buffer_pct / Decimal(100)
    if direction == "above":
        return ctx.last_price > level + buffer
    if direction == "below":
        return ctx.last_price < level - buffer
    raise PrimitiveError(f"direction must be 'above' or 'below', got {_echo(direction)}")


def price_within_pct_of_level(ctx: EvalContext, p: Mapping[str, Any]) -> bool | None:
    level = ctx.named_level(_str(p, "level"))
    if level is None or level <= 0:
        return None
    distance = abs(ctx.last_price - level) / level * Decimal(100)
    return distance <= _num(p, "max_distance_pct")


def gap_from_prev_close(ctx: EvalContext, p: Mapping[str, Any]) -> bool | None:
    if ctx.prev_close is None or ctx.prev_close <= 0 or ctx.day_open is None:
        return None
    gap = (ctx.day_open - ctx.prev_close) / ctx.prev_close * Decimal(100)
    return _between(gap, _num(p, "min_pct"), _num(p, "max_pct"))


# ---------------------------------------------------------------------------
# trend
# ---------------------------------------------------------------------------


def price_above_ma(ctx: EvalContext, p: Mapping[str, Any]) -> bool | None:
    ma = ctx.indicator(f"ema_{_int(p, 'period')}")
    if ma is None:
        return None
    above = _bool(p, "above", True)
    price = float(ctx.last_price)
    return price > ma if above else price < ma


def ma_crossover(ctx: EvalContext, p: Mapping[str, Any]) -> bool | None:
    """A CROSSING, not a comparison.

    The distinction is the whole value of the primitive: "fast is above slow"
    is true on every bar of a trend, while "fast crossed above slow" is true on
    one. A strategy written for the second and evaluated as the first would
    re-enter every bar until some other condition happened to stop it.
    """
    direction = _str(p, "direction")
    if direction not in ("bullish", "bearish"):
        raise PrimitiveError(f"direction must be 'bullish' or 'bearish', got {_echo(direction)}")
    fast, slow = f"ema_{_int(p, 'fast')}", f"ema_{_int(p, 'slow')}"
    now_fast, now_slow = ctx.indicator(fast), ctx.indicator(slow)
    was_fast, was_slow = ctx.indicator_ago(fast, 1), ctx.indicator_ago(slow, 1)
    if None in (now_fast, now_slow, was_fast, was_slow):
        return None
    assert now_fast is not None and now_slow is not None
    assert was_fast is not None and was_slow is not None

    if direction == "bullish":
        return was_fast <= was_slow and now_fast > now_slow
    if direction == "bearish":
        return was_fast >= was_slow and now_fast < now_slow
    raise PrimitiveError(f"direction must be 'bullish' or 'bearish', got {_echo(direction)}")


def ma_slope_positive(ctx: EvalContext, p: Mapping[str, Any]) -> bool | None:
    name = f"ema_{_int(p, 'period')}"
    lookback = _int(p, "lookback")
    now, then = ctx.indicator(name), ctx.indicator_ago(name, lookback)
    if now is None or then is None:
        return None
    return now > then if _bool(p, "positive", True) else now < then


# ---------------------------------------------------------------------------
# momentum
# ---------------------------------------------------------------------------


def rsi_between(ctx: EvalContext, p: Mapping[str, Any]) -> bool | None:
    rsi = ctx.indicator(f"rsi_{_int(p, 'period', 14)}")
    if rsi is None:
        return None
    return _between(Decimal(repr(rsi)), _num(p, "min"), _num(p, "max"))


def macd_histogram_sign(ctx: EvalContext, p: Mapping[str, Any]) -> bool | None:
    histogram = ctx.indicator("macd_histogram")
    if histogram is None:
        return None
    return histogram > 0 if _bool(p, "positive", True) else histogram < 0


# ---------------------------------------------------------------------------
# volatility
# ---------------------------------------------------------------------------


def atr_pct_between(ctx: EvalContext, p: Mapping[str, Any]) -> bool | None:
    atr = ctx.indicator(f"atr_{_int(p, 'period', 14)}")
    if atr is None or ctx.last_price <= 0:
        return None
    atr_pct = Decimal(repr(atr)) / ctx.last_price * Decimal(100)
    return _between(atr_pct, _num(p, "min_pct"), _num(p, "max_pct"))


def range_pct_between(ctx: EvalContext, p: Mapping[str, Any]) -> bool | None:
    source = _str(p, "source")
    if source == "bar":
        bar = ctx.bar
        if bar is None:
            return None
        mid = (bar.high + bar.low) / 2
        if mid <= 0:
            return None
        range_pct = (bar.high - bar.low) / mid * Decimal(100)
    elif source == "opening_range":
        opening = ctx.opening_range
        if opening is None or not opening.is_usable:
            return None
        value = opening.range_pct
        if value is None:
            return None
        range_pct = value
    else:
        raise PrimitiveError(f"source must be 'bar' or 'opening_range', got {_echo(source)}")
    return _between(range_pct, _num(p, "min_pct"), _num(p, "max_pct"))


# ---------------------------------------------------------------------------
# volume
# ---------------------------------------------------------------------------


def volume_ratio_above(ctx: EvalContext, p: Mapping[str, Any]) -> bool | None:
    ratio = ctx.indicator(f"volume_ratio_{_int(p, 'window', 20)}")
    if ratio is None:
        return None
    return Decimal(repr(ratio)) > _num(p, "threshold")


# ---------------------------------------------------------------------------
# multiframe
# ---------------------------------------------------------------------------


def _trend_on(ctx: EvalContext, timeframe: Timeframe) -> bool | None:
    """Does this timeframe agree with the strategy's direction?"""
    fast = ctx.snapshot.value(timeframe, "ema_20")
    slow = ctx.snapshot.value(timeframe, "ema_50")
    if fast is None or slow is None:
        return None
    if fast == slow:
        return False
    rising = fast > slow
    return rising if ctx.direction is Direction.LONG else not rising


def timeframe_agreement_at_least(ctx: EvalContext, p: Mapping[str, Any]) -> bool | None:
    """Rigorous about partial knowledge.

    With some timeframes unreadable the true count is bounded, not unknown. If
    the confirmed agreements already meet the threshold the answer is True
    whatever the rest say; if even counting every unreadable timeframe as
    agreeing falls short, it is False. Only the genuinely undecided middle
    returns UNKNOWN — which keeps a single cold timeframe from blocking a
    decision that the other two have already settled.
    """
    required = _int(p, "count")
    timeframes = [parse_timeframe(t) for t in _str(p, "of").split(",") if t.strip()]
    if not timeframes:
        raise PrimitiveError("timeframe_agreement_at_least was given no timeframes")

    confirmed = sum(1 for tf in timeframes if _trend_on(ctx, tf) is True)
    unknown = sum(1 for tf in timeframes if _trend_on(ctx, tf) is None)
    if confirmed >= required:
        return True
    if confirmed + unknown < required:
        return False
    return None


def higher_tf_trend_is(ctx: EvalContext, p: Mapping[str, Any]) -> bool | None:
    timeframe = parse_timeframe(_str(p, "timeframe"))
    direction = _str(p, "direction")
    if direction not in ("up", "down"):
        raise PrimitiveError(f"direction must be 'up' or 'down', got {_echo(direction)}")
    fast = ctx.snapshot.value(timeframe, "ema_20")
    slow = ctx.snapshot.value(timeframe, "ema_50")
    if fast is None or slow is None:
        return None
    if direction == "up":
        return fast > slow
    if direction == "down":
        return fast < slow
    raise PrimitiveError(f"direction must be 'up' or 'down', got {_echo(direction)}")


# ---------------------------------------------------------------------------
# context
# ---------------------------------------------------------------------------


def india_vix_between(ctx: EvalContext, p: Mapping[str, Any]) -> bool | None:
    if ctx.india_vix is None:
        return None
    return _between(ctx.india_vix, _num(p, "min"), _num(p, "max"))


def regime_is(ctx: EvalContext, p: Mapping[str, Any]) -> bool | None:
    wanted = parse_regimes(_str(p, "regimes"))
    if ctx.regime is None:
        return None
    return ctx.regime in wanted


def index_not_opposing(ctx: EvalContext, p: Mapping[str, Any]) -> bool | None:
    """The index is not moving hard against the trade.

    Deliberately permissive: it asks that the index is not falling more than
    ``tolerance_pct`` against a long, NOT that it is rising. A stock can lead
    its index, and requiring confirmation would filter out exactly that case.
    """
    index = _str(p, "index", "NIFTY")
    change = ctx.index_change_pct.get(index)
    if change is None:
        return None
    tolerance = _num(p, "tolerance_pct", "0.3")
    if ctx.direction is Direction.LONG:
        return change >= -tolerance
    return change <= tolerance


def sector_rank_top_n(ctx: EvalContext, p: Mapping[str, Any]) -> bool | None:
    if ctx.sector_rank is None:
        return None
    return ctx.sector_rank <= _int(p, "n")


# ---------------------------------------------------------------------------
# news
# ---------------------------------------------------------------------------


def news_score_above(ctx: EvalContext, p: Mapping[str, Any]) -> bool | None:
    if ctx.news_score is None:
        return None
    return Decimal(repr(ctx.news_score)) > _num(p, "threshold")


def no_material_news(ctx: EvalContext, p: Mapping[str, Any]) -> bool | None:
    """Asserts an ABSENCE, which is why the missing case must stay UNKNOWN.

    ``hours_since_material_news is None`` means the news subsystem had nothing
    to say — which is not the same as "there was no news". Returning True there
    would turn a dead news feed into a green light on every symbol, on exactly
    the days when a feed is most likely to be swamped.

    The news subsystem reports a large number, not ``None``, when it has looked
    and found nothing.
    """
    hours = ctx.hours_since_material_news
    if hours is None:
        return None
    return Decimal(repr(hours)) >= _num(p, "lookback_hours", "24")


# ---------------------------------------------------------------------------
# time
# ---------------------------------------------------------------------------


def within_window(ctx: EvalContext, p: Mapping[str, Any]) -> bool | None:
    """Window bounds are IST wall-clock, because that is how a trader states them."""
    start, end = parse_clock(_str(p, "start")), parse_clock(_str(p, "end"))
    if start >= end:
        raise PrimitiveError(f"window start {start} is not before end {end}")
    now = ctx.now.astimezone(IST).time()
    return start <= now <= end


def min_bars_since_open(ctx: EvalContext, p: Mapping[str, Any]) -> bool | None:
    if ctx.bars_since_open is None:
        return None
    return ctx.bars_since_open >= _int(p, "bars")


def bars_until_squareoff_above(ctx: EvalContext, p: Mapping[str, Any]) -> bool | None:
    if ctx.bars_until_squareoff is None:
        return None
    return ctx.bars_until_squareoff > _int(p, "bars")


# ---------------------------------------------------------------------------
# exit — these produce PRICES, not booleans
# ---------------------------------------------------------------------------


#: ``Price`` accepts at most four decimal places. Every price this module hands
#: back crosses into a Pydantic model eventually, so quantising is not
#: cosmetic — an unquantised value is a ValidationError somewhere downstream,
#: raised at whichever call site happens to build the model first.
_PRICE_PLACES = Decimal("0.0001")


def _to_price(value: Decimal) -> Decimal:
    return value.quantize(_PRICE_PLACES)


def snap_stop(price: Decimal, tick: Decimal, direction: Direction) -> Decimal:
    """Move a stop onto the tick grid, AWAY from the entry.

    Two reasons the direction is not arbitrary. NSE rejects a price off the
    tick grid outright, so an unsnapped stop is not an imprecise stop — it is a
    rejected order and a position left with no protection.

    And snapping away rather than to the nearest tick keeps the strategy's
    declared risk honest: rounding a 1.5x ATR stop inward would silently make
    it 1.49x, tightening every stop in the system by up to one tick and
    inventing whipsaws nobody asked for. Wider costs a fraction of a tick of
    risk per share, which the sizer then accounts for exactly.
    """
    if tick <= 0:
        raise PrimitiveError(f"tick size must be positive, got {tick}")
    steps = price / tick
    floor = int(steps)
    # LONG stops sit below entry, so away means down; SHORT stops sit above.
    step = floor if steps == floor else (floor if direction is Direction.LONG else floor + 1)
    return _to_price(Decimal(step) * tick)


def snap_target(price: Decimal, tick: Decimal, direction: Direction) -> Decimal:
    """Onto the tick grid, TOWARD the entry.

    The opposite of a stop, and for the mirrored reason: a target is a limit
    order that only pays if it fills. Rounding it further away would leave a
    fraction of a tick permanently unfilled at the exact price the strategy
    meant to exit.
    """
    if tick <= 0:
        raise PrimitiveError(f"tick size must be positive, got {tick}")
    steps = price / tick
    floor = int(steps)
    step = floor if steps == floor else (floor if direction is Direction.LONG else floor + 1)
    return _to_price(Decimal(step) * tick)


def atr_stop(ctx: EvalContext, p: Mapping[str, Any]) -> Decimal | None:
    """A volatility stop, in ``Decimal`` from the first operation.

    ATR is computed in float — indicators are the numerical-library boundary —
    and this is the crossing back. ``to_decimal`` is used rather than an
    implicit ``Decimal(atr)`` so the quantisation happens once, here, instead
    of at whichever downstream site first needs money.
    """
    atr = to_decimal(ctx.indicator(f"atr_{_int(p, 'period', 14)}"))
    if atr is None or atr <= 0:
        return None
    distance = atr * _num(p, "multiplier")
    raw = (
        ctx.last_price - distance if ctx.direction is Direction.LONG else ctx.last_price + distance
    )
    return snap_stop(raw, ctx.tick_size, ctx.direction)


def structure_stop(ctx: EvalContext, p: Mapping[str, Any]) -> Decimal | None:
    """A stop beyond a structural level, with the buffer on the correct side.

    The buffer widens the stop — below a support for a long, above a resistance
    for a short. Applying it in the other direction would place the stop inside
    the level it is meant to protect, where ordinary noise takes it out.
    """
    level = ctx.named_level(_str(p, "level"))
    if level is None or level <= 0:
        return None
    buffer = level * _num(p, "buffer_pct", "0.1") / Decimal(100)
    raw = level - buffer if ctx.direction is Direction.LONG else level + buffer
    return snap_stop(raw, ctx.tick_size, ctx.direction)


def r_multiple_target(
    entry: Decimal,
    stop: Decimal,
    direction: Direction,
    p: Mapping[str, Any],
    tick: Decimal = Decimal("0.05"),
) -> Decimal | None:
    """Target at R multiples of the risk ACTUALLY taken.

    Measured against the snapped stop, not the theoretical one, so the reward
    ratio a journal reports is the ratio the position really had.
    """
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    reward = risk * _num(p, "r")
    raw = entry + reward if direction is Direction.LONG else entry - reward
    return snap_target(raw, tick, direction)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

CONDITION_EVALUATORS: dict[str, ConditionFn] = {
    "price_breaks_level": price_breaks_level,
    "price_within_pct_of_level": price_within_pct_of_level,
    "gap_from_prev_close": gap_from_prev_close,
    "price_above_ma": price_above_ma,
    "ma_crossover": ma_crossover,
    "ma_slope_positive": ma_slope_positive,
    "rsi_between": rsi_between,
    "macd_histogram_sign": macd_histogram_sign,
    "atr_pct_between": atr_pct_between,
    "range_pct_between": range_pct_between,
    "volume_ratio_above": volume_ratio_above,
    "timeframe_agreement_at_least": timeframe_agreement_at_least,
    "higher_tf_trend_is": higher_tf_trend_is,
    "india_vix_between": india_vix_between,
    "regime_is": regime_is,
    "index_not_opposing": index_not_opposing,
    "sector_rank_top_n": sector_rank_top_n,
    "news_score_above": news_score_above,
    "no_material_news": no_material_news,
    "within_window": within_window,
    "min_bars_since_open": min_bars_since_open,
    "bars_until_squareoff_above": bars_until_squareoff_above,
}

#: Exit primitives that produce an entry-time stop price.
STOP_EVALUATORS: dict[str, StopFn] = {
    "atr_stop": atr_stop,
    "structure_stop": structure_stop,
}

#: Primitives with no entry-time computation, handled by the position manager.
#: Listed explicitly so the coverage test can tell "deliberately downstream"
#: from "somebody forgot to implement it".
DEFERRED_TO_POSITION_MANAGER: frozenset[str] = frozenset(
    {"r_multiple_target", "trail_after_r", "squareoff_deadline"}
)
