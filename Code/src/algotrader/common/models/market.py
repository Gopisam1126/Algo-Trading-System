"""Market data domain models.

Two rules hold throughout this package:

1.  **All prices and money are ``Decimal``, never ``float``.**  Floating-point
    representation error in a system that compares prices to tick-size
    boundaries and accumulates P&L is a real correctness bug, not a
    theoretical one.  Conversion to float happens only at the boundary of
    numerical libraries, never in stored state.

2.  **All timestamps are timezone-aware UTC.**  IST appears only at the
    display boundary and in market-hours logic.  Mixing naive and aware
    datetimes is a subtle, recurring source of bugs; ruff's DTZ rules are
    enabled to catch it.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from algotrader.common.enums import Exchange, Timeframe

Price = Annotated[Decimal, Field(gt=0, decimal_places=4)]
NonNegPrice = Annotated[Decimal, Field(ge=0, decimal_places=4)]


class _Frozen(BaseModel):
    """Immutable base.  Market data is a record of what happened; nothing
    downstream should be able to mutate it in place."""

    model_config = ConfigDict(frozen=True, extra="forbid")


def _require_utc(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware (UTC)")
    return v


class Instrument(_Frozen):
    symbol: str
    exchange: Exchange
    broker_token: str
    isin: str | None = None
    lot_size: int = Field(default=1, ge=1)
    tick_size: Decimal = Field(default=Decimal("0.05"), gt=0)
    sector: str | None = None

    def round_to_tick(self, price: Decimal) -> Decimal:
        """Snap a price to the instrument's tick grid.

        An order at a non-tick price is rejected by the exchange, so this is
        applied to every limit and trigger price before submission.
        """
        return (price / self.tick_size).quantize(Decimal("1")) * self.tick_size


class Tick(_Frozen):
    """A single normalized market update, post-cleaning."""

    symbol: str
    exchange_ts: datetime
    received_ts: datetime
    ltp: Price
    volume: int = Field(ge=0)
    bid: NonNegPrice | None = None
    ask: NonNegPrice | None = None
    bid_qty: int | None = Field(default=None, ge=0)
    ask_qty: int | None = Field(default=None, ge=0)

    _utc = field_validator("exchange_ts", "received_ts")(_require_utc)

    @property
    def spread_pct(self) -> Decimal | None:
        if self.bid is None or self.ask is None or self.bid <= 0:
            return None
        return (self.ask - self.bid) / self.bid * Decimal(100)


class Bar(_Frozen):
    """An OHLCV bar.

    ``open_ts`` is the bar's OPEN time, and bars are aligned to the exchange
    session start (09:15 IST), not to wall-clock hours — so a 15-minute bar
    runs 09:15–09:30, never 09:00–09:15.
    """

    symbol: str
    timeframe: Timeframe
    open_ts: datetime
    open: Price
    high: Price
    low: Price
    close: Price
    volume: int = Field(ge=0)
    trade_count: int | None = Field(default=None, ge=0)
    vwap: Price | None = None

    #: True when no trades occurred in the interval and the bar was carried
    #: forward.  Indicators must not treat this as a real price move.
    synthetic: bool = False

    #: False while the bar is still forming.  Strategies evaluate on final
    #: bars only; acting on a partial bar is look-ahead bias in live trading.
    is_final: bool = True

    _utc = field_validator("open_ts")(_require_utc)

    @field_validator("high")
    @classmethod
    def _high_is_highest(cls, v: Decimal, info: object) -> Decimal:
        data = getattr(info, "data", {})
        for field in ("open", "low", "close"):
            other = data.get(field)
            if other is not None and v < other:
                raise ValueError(f"high {v} is below {field} {other}")
        return v

    @field_validator("low")
    @classmethod
    def _low_is_lowest(cls, v: Decimal, info: object) -> Decimal:
        data = getattr(info, "data", {})
        for field in ("open", "close"):
            other = data.get(field)
            if other is not None and v > other:
                raise ValueError(f"low {v} is above {field} {other}")
        return v

    @property
    def range(self) -> Decimal:
        return self.high - self.low

    @property
    def range_pct(self) -> Decimal:
        return self.range / self.low * Decimal(100) if self.low > 0 else Decimal(0)

    @property
    def close_ts(self) -> datetime:
        from datetime import timedelta

        return self.open_ts + timedelta(seconds=self.timeframe.seconds)


class InstrumentDailyStatus(_Frozen):
    """Per-symbol, per-day tradability flags.

    These are the India-specific hazards that will silently break an algo
    that ignores them — T2T makes intraday structurally impossible, ASM/GSM
    carries punitive margins, and ``is_cas_stock`` determines the square-off
    deadline.  Refreshed every morning by the pre-market job.

    See INDIA_FEATURES_AND_CONFIG.md §2.2.
    """

    symbol: str
    trade_date: datetime
    is_t2t: bool = False
    is_asm: bool = False
    is_gsm: bool = False
    is_fno_ban: bool = False
    is_cas_stock: bool = False           # drives the square-off deadline
    circuit_band_pct: Decimal | None = None
    upper_circuit: Price | None = None
    lower_circuit: Price | None = None
    has_earnings_today: bool = False

    @property
    def intraday_permitted(self) -> bool:
        """Hard gate.  A False here removes the symbol from the universe
        entirely, before any scoring happens."""
        return not (self.is_t2t or self.is_asm or self.is_gsm)


class IndicatorSnapshot(_Frozen):
    """Indicator state for one symbol on one timeframe at a point in time.

    ``ready`` is False until enough bars have been seen for the longest-period
    indicator.  Trading off a 20-period EMA computed from 3 bars is a classic
    and expensive bug; the flag makes it impossible to do accidentally.
    """

    symbol: str
    timeframe: Timeframe
    as_of: datetime
    ready: bool

    close: Price
    ema_20: Decimal | None = None
    ema_50: Decimal | None = None
    ema_200: Decimal | None = None
    rsi_14: Decimal | None = Field(default=None, ge=0, le=100)
    atr_14: Decimal | None = Field(default=None, ge=0)
    macd: Decimal | None = None
    macd_signal: Decimal | None = None
    bb_upper: Decimal | None = None
    bb_lower: Decimal | None = None
    vwap: Decimal | None = None
    volume_ratio_20: Decimal | None = Field(default=None, ge=0)

    _utc = field_validator("as_of")(_require_utc)

    @property
    def atr_pct(self) -> Decimal | None:
        if self.atr_14 is None or self.close <= 0:
            return None
        return self.atr_14 / self.close * Decimal(100)


class MultiTimeframeSnapshot(_Frozen):
    """All timeframes for one symbol, side by side.

    Deliberately NOT flattened into a single feature vector: the AI layer and
    the strategies must be able to reason about agreement and conflict
    *across* timeframes explicitly.  Flattening hides exactly the structure
    that matters.  See ARCHITECTURE_RESEARCH.md §9.3.
    """

    symbol: str
    as_of: datetime
    frames: dict[Timeframe, IndicatorSnapshot]

    _utc = field_validator("as_of")(_require_utc)

    @property
    def all_ready(self) -> bool:
        return bool(self.frames) and all(f.ready for f in self.frames.values())

    def get(self, tf: Timeframe) -> IndicatorSnapshot | None:
        return self.frames.get(tf)

    def trend_agreement(self, timeframes: list[Timeframe]) -> int:
        """Count how many of the given timeframes agree on direction.

        Returns the size of the largest agreeing group (so 3 means unanimous
        across three frames).  This is the "confluence" measure — the highest
        probability setups are where multiple timeframes tell the same story.
        """
        directions: list[int] = []
        for tf in timeframes:
            snap = self.frames.get(tf)
            if snap is None or not snap.ready or snap.ema_20 is None or snap.ema_50 is None:
                continue
            directions.append(1 if snap.ema_20 > snap.ema_50 else -1)
        if not directions:
            return 0
        return max(directions.count(1), directions.count(-1))
