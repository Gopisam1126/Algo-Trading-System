"""Incremental indicator framework (E06-S01).

Indicators update in O(1) per bar rather than recomputing over a window. At 200
symbols x 6 timeframes that is the difference between a warm-up that finishes
and one that does not.

**The seeding convention is the whole correctness question, and it is fixed
here.** E06-S01's acceptance criterion is that incremental values match batch
computation to four decimal places — but "match" is undefined until you say
which batch. TA-Lib's conventions were established by experiment, not assumed:

- ``EMA(N)`` emits its first value at index ``N-1``, seeded with the **simple
  mean of the first N** values. A naive streaming EMA seeded from the first
  value never converges to this exactly; it stays close and disagrees in the
  digits the criterion is written in.
- ``RSI(N)`` and ``ATR(N)`` use **Wilder smoothing**, seeded with the simple
  mean of the first N changes / true ranges, emitting first at index ``N``.

Every indicator here matches those, and the tests assert it against TA-Lib
directly rather than against a remembered formula.

**State serialisation must round-trip exactly.** A restart mid-session that
produced even slightly different values would change signals with nothing
visible to show for it, so :meth:`Indicator.restore` is required to reproduce
what :meth:`Indicator.snapshot` captured — asserted in the tests, not assumed.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from algotrader.common.models.market import Bar


class IndicatorError(RuntimeError):
    """An indicator was misconfigured or asked for a value it does not have."""


def _checked_period(value: object, *, minimum: int, what: str) -> int:
    """Validate a period arriving from a SNAPSHOT, not just from a constructor.

    ``restore`` reads state that has round-tripped through Redis, and it used to
    trust it. A snapshot carrying ``period: -5`` produced an EMA with
    ``alpha = -0.5`` — an indicator that diverges instead of converging, emits
    numbers the whole time, and is wrong in a direction nothing downstream can
    detect. Version skew or a partially-written key is enough to cause it; an
    attacker is not required.
    """
    try:
        period: int = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        raise IndicatorError(f"{what} period is not an integer: {value!r}") from None
    if period < minimum:
        raise IndicatorError(
            f"{what} period must be >= {minimum}, got {period}. A snapshot carrying "
            f"this would restore an indicator that emits values and diverges."
        )
    return period


def to_decimal(value: float | None, places: int = 4) -> Decimal | None:
    """The one sanctioned crossing from indicator float back into money.

    Indicators compute in ``float`` deliberately — they are the numerical-library
    boundary CLAUDE.md carves out, and TA-Lib parity is defined in float. But an
    ATR of ``1.4000000000000057`` becoming a stop distance, and then a position
    size, drags that representation error into the money path where the rest of
    the system is ``Decimal``.

    Quantising here makes the crossing explicit and lossy-on-purpose rather than
    implicit and lossy-by-accident. Anything feeding sizing or a stop price goes
    through this.
    """
    if value is None:
        return None
    return Decimal(repr(value)).quantize(Decimal(10) ** -places, rounding=ROUND_HALF_UP)


class Indicator(ABC):
    """One value, updated one bar at a time.

    ``value`` is ``None`` until ``is_ready``. Returning a provisional number
    instead would be worse than returning nothing: a 20-EMA built from three
    bars looks like a working indicator and is not, and the caller has no way to
    tell the difference.
    """

    name: str

    @property
    @abstractmethod
    def is_ready(self) -> bool:
        """True once enough bars have been seen for the value to mean anything."""

    @property
    @abstractmethod
    def value(self) -> float | None:
        """The current value, or ``None`` while warming up."""

    @abstractmethod
    def update(self, bar: Bar) -> float | None:
        """Apply one bar. Returns the new value, or ``None`` if still warming."""

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Serialisable state. Must be sufficient to reproduce this instance."""

    @abstractmethod
    def restore(self, state: dict[str, Any]) -> None:
        """Reload state captured by :meth:`snapshot`."""

    def warm_up(self, bars: list[Bar]) -> float | None:
        """Feed historical bars in order.

        Deliberately the same code path as live updates. A separate batch
        warm-up would be faster and would let the two disagree — and the
        disagreement would only appear as a discontinuity at the moment the
        system switched from one to the other, mid-session.
        """
        result = None
        for bar in bars:
            result = self.update(bar)
        return result


@dataclass
class _Wilder:
    """Wilder's smoothing, the convention RSI and ATR use.

    Seeded with the simple mean of the first ``period`` samples, then
    ``prev + (sample - prev) / period``. Verified against TA-Lib rather than
    taken from a formula.
    """

    period: int
    _seed: list[float] = field(default_factory=list)
    _value: float | None = None

    def update(self, sample: float) -> float | None:
        if self._value is None:
            self._seed.append(sample)
            if len(self._seed) < self.period:
                return None
            self._value = sum(self._seed) / self.period
            self._seed = []
            return self._value
        self._value += (sample - self._value) / self.period
        return self._value

    @property
    def value(self) -> float | None:
        return self._value

    def snapshot(self) -> dict[str, Any]:
        return {"period": self.period, "seed": list(self._seed), "value": self._value}

    def restore(self, state: dict[str, Any]) -> None:
        self.period = _checked_period(state["period"], minimum=1, what="Wilder")
        self._seed = [float(x) for x in state.get("seed", [])]
        self._value = None if state.get("value") is None else float(state["value"])


class SMA(Indicator):
    """Simple moving average over a fixed window."""

    def __init__(self, period: int, name: str | None = None) -> None:
        if period < 1:
            raise IndicatorError(f"SMA period must be >= 1, got {period}")
        self.period = period
        self.name = name or f"sma_{period}"
        self._window: deque[float] = deque(maxlen=period)
        self._sum = 0.0

    @property
    def is_ready(self) -> bool:
        return len(self._window) == self.period

    @property
    def value(self) -> float | None:
        return self._sum / self.period if self.is_ready else None

    def update(self, bar: Bar) -> float | None:
        price = float(bar.close)
        if len(self._window) == self.period:
            self._sum -= self._window[0]
        self._window.append(price)
        self._sum += price
        return self.value

    def snapshot(self) -> dict[str, Any]:
        return {"period": self.period, "window": list(self._window)}

    def restore(self, state: dict[str, Any]) -> None:
        self.period = _checked_period(state["period"], minimum=1, what="SMA")
        self._window = deque((float(x) for x in state["window"]), maxlen=self.period)
        self._sum = sum(self._window)


class EMA(Indicator):
    """Exponential moving average, seeded from the SMA of the first N.

    That seed is not a detail. TA-Lib emits its first EMA at index ``N-1`` equal
    to the simple mean of the first N values; a streaming EMA seeded from the
    first value alone produces a series that is close but never equal, and the
    acceptance criterion is written in four decimal places.
    """

    def __init__(self, period: int, name: str | None = None) -> None:
        if period < 1:
            raise IndicatorError(f"EMA period must be >= 1, got {period}")
        self.period = period
        self.name = name or f"ema_{period}"
        self.alpha = 2.0 / (period + 1.0)
        self._seed: list[float] = []
        self._value: float | None = None

    @property
    def is_ready(self) -> bool:
        return self._value is not None

    @property
    def value(self) -> float | None:
        return self._value

    def update(self, bar: Bar) -> float | None:
        return self.update_raw(float(bar.close))

    def update_raw(self, price: float) -> float | None:
        """Update from a bare number — used when chaining EMAs (MACD)."""
        if self._value is None:
            self._seed.append(price)
            if len(self._seed) < self.period:
                return None
            self._value = sum(self._seed) / self.period
            self._seed = []
            return self._value
        self._value += self.alpha * (price - self._value)
        return self._value

    def snapshot(self) -> dict[str, Any]:
        return {"period": self.period, "seed": list(self._seed), "value": self._value}

    def restore(self, state: dict[str, Any]) -> None:
        self.period = _checked_period(state["period"], minimum=1, what="EMA")
        self.alpha = 2.0 / (self.period + 1.0)
        self._seed = [float(x) for x in state.get("seed", [])]
        self._value = None if state.get("value") is None else float(state["value"])


class RSI(Indicator):
    """Relative strength index, Wilder-smoothed."""

    def __init__(self, period: int = 14, name: str | None = None) -> None:
        if period < 2:
            raise IndicatorError(f"RSI period must be >= 2, got {period}")
        self.period = period
        self.name = name or f"rsi_{period}"
        self._gains = _Wilder(period)
        self._losses = _Wilder(period)
        self._previous_close: float | None = None
        self._value: float | None = None

    @property
    def is_ready(self) -> bool:
        return self._value is not None

    @property
    def value(self) -> float | None:
        return self._value

    def update(self, bar: Bar) -> float | None:
        close = float(bar.close)
        if self._previous_close is None:
            self._previous_close = close
            return None
        change = close - self._previous_close
        self._previous_close = close

        avg_gain = self._gains.update(max(change, 0.0))
        avg_loss = self._losses.update(max(-change, 0.0))
        if avg_gain is None or avg_loss is None:
            return None

        if avg_loss == 0:
            # No down moves in the window. RSI is 100 by definition; computing
            # it would divide by zero.
            self._value = 100.0
        else:
            rs = avg_gain / avg_loss
            self._value = 100.0 - (100.0 / (1.0 + rs))
        return self._value

    def snapshot(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "gains": self._gains.snapshot(),
            "losses": self._losses.snapshot(),
            "previous_close": self._previous_close,
            "value": self._value,
        }

    def restore(self, state: dict[str, Any]) -> None:
        self.period = _checked_period(state["period"], minimum=2, what="RSI")
        self._gains.restore(state["gains"])
        self._losses.restore(state["losses"])
        self._previous_close = (
            None if state.get("previous_close") is None else float(state["previous_close"])
        )
        self._value = None if state.get("value") is None else float(state["value"])


class ATR(Indicator):
    """Average true range, Wilder-smoothed.

    **This one drives position sizing.** Stop distance is ``ATR x multiplier``,
    so an ATR that is wrong by a few percent sizes every position wrong by the
    same amount, in the same direction, all day. It is worth the extra care of
    matching TA-Lib exactly rather than approximately.
    """

    def __init__(self, period: int = 14, name: str | None = None) -> None:
        if period < 1:
            raise IndicatorError(f"ATR period must be >= 1, got {period}")
        self.period = period
        self.name = name or f"atr_{period}"
        self._wilder = _Wilder(period)
        self._previous_close: float | None = None

    @property
    def is_ready(self) -> bool:
        return self._wilder.value is not None

    @property
    def value(self) -> float | None:
        return self._wilder.value

    def update(self, bar: Bar) -> float | None:
        high, low, close = float(bar.high), float(bar.low), float(bar.close)
        if self._previous_close is None:
            # The first bar has no previous close, so true range is simply the
            # bar's own range. TA-Lib skips it entirely; skipping matches.
            self._previous_close = close
            return None
        true_range = max(
            high - low,
            abs(high - self._previous_close),
            abs(low - self._previous_close),
        )
        self._previous_close = close
        return self._wilder.update(true_range)

    def percent_of(self, price: float) -> float | None:
        """ATR as a percentage of price — what the outlier filter wants.

        Stays ``float``: the outlier filter compares it against a threshold and
        nothing downstream of that comparison is money.
        """
        if self.value is None or price <= 0:
            return None
        return self.value / price * 100.0

    def as_decimal(self, places: int = 4) -> Decimal | None:
        """ATR for the MONEY path — stop distance, and therefore position size.

        Use this, not ``.value``, anywhere the number becomes rupees. ``.value``
        is ``1.4000000000000057``; multiplied by a stop multiplier and divided
        into a risk budget, that representation error reaches the quantity. The
        quantisation is deliberate and one-directional, which is the point of a
        named boundary rather than an implicit ``Decimal()`` at whichever call
        site happens to need one.
        """
        return to_decimal(self.value, places)

    def snapshot(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "wilder": self._wilder.snapshot(),
            "previous_close": self._previous_close,
        }

    def restore(self, state: dict[str, Any]) -> None:
        self.period = _checked_period(state["period"], minimum=1, what="ATR")
        self._wilder.restore(state["wilder"])
        self._previous_close = (
            None if state.get("previous_close") is None else float(state["previous_close"])
        )


class MACD(Indicator):
    """MACD line, signal, and histogram.

    **Both EMAs are seeded at the same bar**, and that is not the obvious
    implementation. Composing an independent ``EMA(12)`` and ``EMA(26)`` starts
    the fast one fourteen bars earlier, so by the time the slow one exists the
    fast has already been decaying and the two are measured from different
    origins.

    TA-Lib aligns them: both seed at index ``slow - 1``, the fast from the mean
    of the last ``fast`` closes and the slow from the mean of all ``slow``.
    Established by experiment, and the difference is not cosmetic — on a
    200-bar series the naive version read -18.75 where TA-Lib read -20.11, a
    7% error on a line whose crossings are the signal.
    """

    def __init__(
        self, fast: int = 12, slow: int = 26, signal: int = 9, name: str | None = None
    ) -> None:
        if fast >= slow:
            raise IndicatorError(f"MACD fast ({fast}) must be shorter than slow ({slow})")
        self.name = name or f"macd_{fast}_{slow}_{signal}"
        self.fast_period = fast
        self.slow_period = slow
        self._fast_alpha = 2.0 / (fast + 1.0)
        self._slow_alpha = 2.0 / (slow + 1.0)
        self._buffer: deque[float] = deque(maxlen=slow)
        self._fast_value: float | None = None
        self._slow_value: float | None = None
        self._signal = EMA(signal)
        self._macd: float | None = None

    @property
    def is_ready(self) -> bool:
        return self._signal.value is not None

    @property
    def value(self) -> float | None:
        return self._macd

    @property
    def signal(self) -> float | None:
        return self._signal.value

    @property
    def histogram(self) -> float | None:
        if self._macd is None or self._signal.value is None:
            return None
        return self._macd - self._signal.value

    def update(self, bar: Bar) -> float | None:
        price = float(bar.close)
        self._buffer.append(price)

        if self._slow_value is None:
            if len(self._buffer) < self.slow_period:
                return None
            # Both seeds are taken at THIS bar, from windows ending here.
            window = list(self._buffer)
            self._slow_value = sum(window) / self.slow_period
            self._fast_value = sum(window[-self.fast_period :]) / self.fast_period
        else:
            assert self._fast_value is not None
            self._fast_value += self._fast_alpha * (price - self._fast_value)
            self._slow_value += self._slow_alpha * (price - self._slow_value)

        self._macd = self._fast_value - self._slow_value
        self._signal.update_raw(self._macd)
        return self._macd

    def snapshot(self) -> dict[str, Any]:
        return {
            "fast_period": self.fast_period,
            "slow_period": self.slow_period,
            "buffer": list(self._buffer),
            "fast_value": self._fast_value,
            "slow_value": self._slow_value,
            "signal": self._signal.snapshot(),
            "macd": self._macd,
        }

    def restore(self, state: dict[str, Any]) -> None:
        self.fast_period = _checked_period(state["fast_period"], minimum=1, what="MACD fast")
        self.slow_period = _checked_period(state["slow_period"], minimum=2, what="MACD slow")
        self._fast_alpha = 2.0 / (self.fast_period + 1.0)
        self._slow_alpha = 2.0 / (self.slow_period + 1.0)
        self._buffer = deque((float(x) for x in state["buffer"]), maxlen=self.slow_period)
        self._fast_value = None if state.get("fast_value") is None else float(state["fast_value"])
        self._slow_value = None if state.get("slow_value") is None else float(state["slow_value"])
        self._signal.restore(state["signal"])
        self._macd = None if state.get("macd") is None else float(state["macd"])


class BollingerBands(Indicator):
    """Middle SMA with bands at k population standard deviations.

    Population rather than sample standard deviation, matching TA-Lib. The two
    differ by ``sqrt(n/(n-1))``, which at n=20 is about 2.6% — small enough to
    look like rounding and large enough to move a band through a price.
    """

    def __init__(self, period: int = 20, deviations: float = 2.0, name: str | None = None) -> None:
        if period < 2:
            raise IndicatorError(f"Bollinger period must be >= 2, got {period}")
        self.period = period
        self.deviations = deviations
        self.name = name or f"bb_{period}_{deviations:g}"
        self._window: deque[float] = deque(maxlen=period)

    @property
    def is_ready(self) -> bool:
        return len(self._window) == self.period

    @property
    def value(self) -> float | None:
        """The middle band."""
        return sum(self._window) / self.period if self.is_ready else None

    @property
    def upper(self) -> float | None:
        middle, sigma = self.value, self._stddev()
        return None if middle is None or sigma is None else middle + self.deviations * sigma

    @property
    def lower(self) -> float | None:
        middle, sigma = self.value, self._stddev()
        return None if middle is None or sigma is None else middle - self.deviations * sigma

    @property
    def width_pct(self) -> float | None:
        """Band width as a percentage of the middle — a volatility read."""
        middle, upper, lower = self.value, self.upper, self.lower
        if middle is None or upper is None or lower is None or middle == 0:
            return None
        return (upper - lower) / middle * 100.0

    def _stddev(self) -> float | None:
        if not self.is_ready:
            return None
        mean = sum(self._window) / self.period
        variance = sum((x - mean) ** 2 for x in self._window) / self.period
        return math.sqrt(variance)

    def update(self, bar: Bar) -> float | None:
        self._window.append(float(bar.close))
        return self.value

    def snapshot(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "deviations": self.deviations,
            "window": list(self._window),
        }

    def restore(self, state: dict[str, Any]) -> None:
        self.period = _checked_period(state["period"], minimum=2, what="Bollinger")
        self.deviations = float(state["deviations"])
        self._window = deque((float(x) for x in state["window"]), maxlen=self.period)


class VWAP(Indicator):
    """Session-anchored volume-weighted average price.

    Anchored to the session, not a rolling window: VWAP's meaning comes from
    being the day's average cost basis, and a rolling version is a different
    statistic wearing the same name. :meth:`reset` is called at the session
    boundary, and forgetting to call it is the way this silently becomes a
    multi-day average.
    """

    def __init__(self, name: str = "vwap") -> None:
        self.name = name
        self._pv = 0.0
        self._volume = 0.0

    @property
    def is_ready(self) -> bool:
        return self._volume > 0

    @property
    def value(self) -> float | None:
        return self._pv / self._volume if self._volume > 0 else None

    def update(self, bar: Bar) -> float | None:
        # Typical price, matching the standard definition. Using close alone
        # makes VWAP drift toward a plain moving average on wide bars.
        typical = (float(bar.high) + float(bar.low) + float(bar.close)) / 3.0
        volume = float(bar.volume)
        if volume <= 0:
            # A synthetic bar carries no volume and must not move VWAP — it
            # represents an interval in which nothing traded.
            return self.value
        self._pv += typical * volume
        self._volume += volume
        return self.value

    def reset(self) -> None:
        """Call at the session boundary."""
        self._pv = 0.0
        self._volume = 0.0

    def snapshot(self) -> dict[str, Any]:
        return {"pv": self._pv, "volume": self._volume}

    def restore(self, state: dict[str, Any]) -> None:
        self._pv = float(state["pv"])
        self._volume = float(state["volume"])


class VolumeRatio(Indicator):
    """Current volume against the average of the previous N bars.

    The average deliberately EXCLUDES the current bar. Including it damps the
    very spike the indicator exists to detect — a bar with ten times normal
    volume would raise its own baseline and report far less than 10.
    """

    def __init__(self, period: int = 20, name: str | None = None) -> None:
        if period < 1:
            raise IndicatorError(f"VolumeRatio period must be >= 1, got {period}")
        self.period = period
        self.name = name or f"volume_ratio_{period}"
        self._window: deque[float] = deque(maxlen=period)
        self._value: float | None = None

    @property
    def is_ready(self) -> bool:
        return self._value is not None

    @property
    def value(self) -> float | None:
        return self._value

    def update(self, bar: Bar) -> float | None:
        volume = float(bar.volume)
        if len(self._window) == self.period:
            average = sum(self._window) / self.period
            self._value = volume / average if average > 0 else None
        self._window.append(volume)
        return self._value

    def snapshot(self) -> dict[str, Any]:
        return {"period": self.period, "window": list(self._window), "value": self._value}

    def restore(self, state: dict[str, Any]) -> None:
        self.period = _checked_period(state["period"], minimum=1, what="VolumeRatio")
        self._window = deque((float(x) for x in state["window"]), maxlen=self.period)
        self._value = None if state.get("value") is None else float(state["value"])
