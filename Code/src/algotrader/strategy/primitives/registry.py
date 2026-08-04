"""The vetted primitive library.

This is the **entire vocabulary** available to any strategy, from any source.
A strategy — whether hand-written by you or proposed by the AI — can only
reference names declared here.

Adding a primitive is a deliberate, reviewed code change: the spec below
declares the parameter bounds, and a matching evaluator function must be
written and unit-tested.  The AI can compose these in novel ways; it cannot
invent new ones.

Each primitive's bounds are part of the safety story.  ``atr_stop`` capping
``multiplier`` at 5.0, for instance, means no strategy can express a stop so
wide it is effectively absent.
"""

from __future__ import annotations

from decimal import Decimal

from algotrader.strategy.dsl import REGISTRY, ParamSpec, PrimitiveSpec


def _p(
    name: str,
    type_: str,
    *,
    required: bool = True,
    lo: str | None = None,
    hi: str | None = None,
    choices: list[str] | None = None,
    default: object = None,
) -> ParamSpec:
    return ParamSpec(
        name=name,
        type=type_,  # type: ignore[arg-type]
        required=required,
        minimum=Decimal(lo) if lo is not None else None,
        maximum=Decimal(hi) if hi is not None else None,
        choices=choices,
        default=default,
    )


PRIMITIVES: list[PrimitiveSpec] = [
    # -- Price / level ------------------------------------------------------
    PrimitiveSpec(
        name="price_breaks_level",
        category="price",
        description="Price crosses a named structural level with a buffer.",
        params=[
            _p("level", "enum", choices=[
                "opening_range_high", "opening_range_low", "prev_day_high",
                "prev_day_low", "pivot", "r1", "s1", "vwap", "day_high", "day_low",
            ]),
            _p("buffer_pct", "float", lo="0", hi="2", default=0.05, required=False),
            _p("direction", "enum", choices=["above", "below"]),
        ],
    ),
    PrimitiveSpec(
        name="price_within_pct_of_level",
        category="price",
        description="Price is near a level — used for bounce/rejection setups.",
        params=[
            _p("level", "enum", choices=[
                "prev_day_high", "prev_day_low", "pivot", "r1", "s1", "vwap",
                "ema_20", "ema_50", "ema_200",
            ]),
            _p("max_distance_pct", "float", lo="0.01", hi="5"),
        ],
    ),
    PrimitiveSpec(
        name="gap_from_prev_close",
        category="price",
        description="Opening gap size is within a band.",
        params=[_p("min_pct", "float", lo="-20", hi="20"),
                _p("max_pct", "float", lo="-20", hi="20")],
    ),

    # -- Trend --------------------------------------------------------------
    PrimitiveSpec(
        name="price_above_ma",
        category="trend",
        description="Close is above (or below) a moving average.",
        params=[_p("period", "int", lo="5", hi="200"),
                _p("above", "bool", default=True, required=False)],
    ),
    PrimitiveSpec(
        name="ma_crossover",
        category="trend",
        description="Fast MA has crossed slow MA in the given direction.",
        params=[_p("fast", "int", lo="3", hi="100"),
                _p("slow", "int", lo="5", hi="200"),
                _p("direction", "enum", choices=["bullish", "bearish"])],
    ),
    PrimitiveSpec(
        name="ma_slope_positive",
        category="trend",
        description="Moving average slope over N bars has the given sign.",
        params=[_p("period", "int", lo="5", hi="200"),
                _p("lookback", "int", lo="2", hi="50"),
                _p("positive", "bool", default=True, required=False)],
    ),

    # -- Momentum -----------------------------------------------------------
    PrimitiveSpec(
        name="rsi_between",
        category="momentum",
        description="RSI is inside a band.",
        params=[_p("period", "int", lo="2", hi="50", default=14, required=False),
                _p("min", "float", lo="0", hi="100"),
                _p("max", "float", lo="0", hi="100")],
    ),
    PrimitiveSpec(
        name="macd_histogram_sign",
        category="momentum",
        description="MACD histogram is positive or negative.",
        params=[_p("positive", "bool")],
    ),

    # -- Volatility ---------------------------------------------------------
    PrimitiveSpec(
        name="atr_pct_between",
        category="volatility",
        description="ATR as a percentage of price is inside a band.",
        params=[_p("period", "int", lo="2", hi="50", default=14, required=False),
                _p("min_pct", "float", lo="0", hi="20"),
                _p("max_pct", "float", lo="0", hi="20")],
    ),
    PrimitiveSpec(
        name="range_pct_between",
        category="volatility",
        description="Bar or opening-range width as a percentage of price.",
        params=[_p("source", "enum", choices=["bar", "opening_range"]),
                _p("min_pct", "float", lo="0", hi="20"),
                _p("max_pct", "float", lo="0", hi="20")],
    ),

    # -- Volume -------------------------------------------------------------
    PrimitiveSpec(
        name="volume_ratio_above",
        category="volume",
        description="Volume relative to its own N-period average.",
        params=[_p("window", "int", lo="5", hi="100", default=20, required=False),
                _p("threshold", "float", lo="0.1", hi="20")],
    ),

    # -- Multi-timeframe ----------------------------------------------------
    PrimitiveSpec(
        name="timeframe_agreement_at_least",
        category="multiframe",
        description="At least N of the given timeframes agree on direction — "
                    "the confluence measure.",
        params=[_p("count", "int", lo="1", hi="3"),
                _p("of", "str", required=False, default="1h,1d,1w")],
    ),
    PrimitiveSpec(
        name="higher_tf_trend_is",
        category="multiframe",
        description="A specific higher timeframe is trending in the given direction.",
        params=[_p("timeframe", "enum", choices=["1h", "1d", "1w"]),
                _p("direction", "enum", choices=["up", "down"])],
    ),

    # -- Market context -----------------------------------------------------
    PrimitiveSpec(
        name="india_vix_between",
        category="context",
        description="India VIX is inside a band — the volatility regime gate.",
        params=[_p("min", "float", lo="0", hi="100"),
                _p("max", "float", lo="0", hi="100")],
    ),
    PrimitiveSpec(
        name="regime_is",
        category="context",
        description="Current market regime is one of the listed values.",
        params=[_p("regimes", "str")],
    ),
    PrimitiveSpec(
        name="index_not_opposing",
        category="context",
        description="The index is not moving against the trade direction — "
                    "don't fight the Nifty.",
        params=[_p("index", "enum", choices=["NIFTY", "BANKNIFTY"], default="NIFTY",
                   required=False),
                _p("tolerance_pct", "float", lo="0", hi="5", default=0.3, required=False)],
    ),
    PrimitiveSpec(
        name="sector_rank_top_n",
        category="context",
        description="The symbol's sector is among the day's top N by strength.",
        params=[_p("n", "int", lo="1", hi="24")],
    ),

    # -- News ---------------------------------------------------------------
    PrimitiveSpec(
        name="news_score_above",
        category="news",
        description="Composite firm-level news score exceeds a threshold.",
        params=[_p("threshold", "float", lo="-1", hi="1")],
    ),
    PrimitiveSpec(
        name="no_material_news",
        category="news",
        description="No high-magnitude news in the lookback window.",
        params=[_p("lookback_hours", "int", lo="1", hi="168", default=24, required=False)],
    ),

    # -- Time ---------------------------------------------------------------
    PrimitiveSpec(
        name="within_window",
        category="time",
        description="Current time is inside an intraday window (IST).",
        params=[_p("start", "str"), _p("end", "str")],
    ),
    PrimitiveSpec(
        name="min_bars_since_open",
        category="time",
        description="At least N bars have completed since the session open.",
        params=[_p("bars", "int", lo="0", hi="100")],
    ),
    PrimitiveSpec(
        name="bars_until_squareoff_above",
        category="time",
        description="Enough runway remains before the square-off deadline for "
                    "the trade to work.",
        params=[_p("bars", "int", lo="1", hi="100")],
    ),

    # -- Exits (stop and time exits are MANDATORY) --------------------------
    PrimitiveSpec(
        name="atr_stop",
        category="exit",
        description="Stop placed at an ATR multiple from entry.",
        params=[_p("multiplier", "float", lo="0.5", hi="5"),
                _p("period", "int", lo="2", hi="50", default=14, required=False)],
        is_mandatory_exit=True,
    ),
    PrimitiveSpec(
        name="structure_stop",
        category="exit",
        description="Stop placed beyond a structural level.",
        params=[_p("level", "enum", choices=[
                    "opening_range_low", "opening_range_high", "prev_day_low",
                    "prev_day_high", "swing_low", "swing_high"]),
                _p("buffer_pct", "float", lo="0", hi="2", default=0.1, required=False)],
        is_mandatory_exit=True,
    ),
    PrimitiveSpec(
        name="r_multiple_target",
        category="exit",
        description="Profit target at an R multiple of initial risk.",
        params=[_p("r", "float", lo="0.5", hi="10")],
    ),
    PrimitiveSpec(
        name="trail_after_r",
        category="exit",
        description="Begin trailing the stop once N R of profit is reached.",
        params=[_p("activate_at_r", "float", lo="0.1", hi="10"),
                _p("atr_mult", "float", lo="0.5", hi="5")],
    ),
    PrimitiveSpec(
        name="squareoff_deadline",
        category="exit",
        description="Exit before the broker's per-stock auto square-off. "
                    "Required on every strategy — non-removable.",
        params=[],
        is_mandatory_exit=True,
    ),
]


def install() -> None:
    """Populate the process-wide registry.  Idempotent."""
    for spec in PRIMITIVES:
        if spec.name not in REGISTRY.names():
            REGISTRY.register(spec)


install()
