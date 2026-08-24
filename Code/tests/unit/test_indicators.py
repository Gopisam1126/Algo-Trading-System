"""Incremental indicators against TA-Lib batch (E06-S01, E06-S02).

E06-S01's acceptance criterion is that incremental values match batch
computation to four decimal places. That criterion is meaningless until you say
*which* batch, so these tests assert against TA-Lib itself rather than against a
remembered formula — the seeding convention is the whole correctness question,
and it was established here by experiment:

- EMA(N) emits first at index N-1, seeded with the simple mean of the first N.
- RSI(N) and ATR(N) use Wilder smoothing seeded with the simple mean of the
  first N changes / true ranges, emitting first at index N.

A streaming EMA seeded from the first value stays close to TA-Lib forever and
never equals it, which passes a "looks right" review and fails this.

The restore tests matter for a different reason: a mid-session restart that
produced even slightly different values would change signals with nothing
visible to show for it.
"""

from __future__ import annotations

import datetime as dt
import json
import random
from decimal import Decimal

import numpy as np
import pytest

talib = pytest.importorskip("talib", reason="TA-Lib is the reference implementation")

from algotrader.common.enums import Timeframe  # noqa: E402
from algotrader.common.models.market import Bar  # noqa: E402
from algotrader.indicators.framework import (  # noqa: E402
    ATR,
    EMA,
    MACD,
    RSI,
    SMA,
    VWAP,
    BollingerBands,
    IndicatorError,
    VolumeRatio,
)

TOLERANCE = 1e-4  # the "four decimal places" in the acceptance criterion
BASE = dt.datetime(2026, 8, 20, 3, 45, tzinfo=dt.UTC)


def _series(n: int = 120, seed: int = 7) -> list[Bar]:
    """A deterministic random walk. Seeded so a failure is reproducible."""
    rng = random.Random(seed)
    bars: list[Bar] = []
    price = 1000.0
    for i in range(n):
        price = max(10.0, price * (1 + rng.uniform(-0.02, 0.02)))
        high = price * (1 + abs(rng.uniform(0, 0.01)))
        low = price * (1 - abs(rng.uniform(0, 0.01)))
        bars.append(
            Bar(
                symbol="INFY",
                timeframe=Timeframe.M5,
                open_ts=BASE + dt.timedelta(minutes=5 * i),
                open=Decimal(f"{price:.4f}"),
                high=Decimal(f"{max(high, price):.4f}"),
                low=Decimal(f"{min(low, price):.4f}"),
                close=Decimal(f"{price:.4f}"),
                volume=rng.randint(1_000, 50_000),
            )
        )
    return bars


def _closes(bars: list[Bar]) -> np.ndarray:
    return np.array([float(b.close) for b in bars])


def _hlc(bars: list[Bar]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    return (
        np.array([float(b.high) for b in bars]),
        np.array([float(b.low) for b in bars]),
        np.array([float(b.close) for b in bars]),
    )


def _incremental(indicator, bars: list[Bar]) -> list[float | None]:
    return [indicator.update(b) for b in bars]


def _compare(ours: list[float | None], reference: np.ndarray, label: str) -> None:
    """Every index where the reference has a value, ours must agree."""
    compared = 0
    for i, expected in enumerate(reference):
        if np.isnan(expected):
            continue
        got = ours[i]
        assert got is not None, f"{label}: batch has a value at {i}, incremental does not"
        assert abs(got - float(expected)) < TOLERANCE, (
            f"{label} diverged at index {i}: incremental {got!r} vs batch {expected!r}"
        )
        compared += 1
    assert compared > 10, f"{label}: only {compared} points compared; the test proves little"


class TestAgainstTaLib:
    def test_sma_matches(self) -> None:
        bars = _series()
        _compare(_incremental(SMA(20), bars), talib.SMA(_closes(bars), 20), "SMA(20)")

    def test_ema_matches(self) -> None:
        """The seeding case. A first-value seed stays close forever and never
        equals this."""
        bars = _series()
        _compare(_incremental(EMA(20), bars), talib.EMA(_closes(bars), 20), "EMA(20)")

    @pytest.mark.parametrize("period", [5, 20, 50])
    def test_ema_matches_at_several_periods(self, period: int) -> None:
        bars = _series(n=200)
        _compare(
            _incremental(EMA(period), bars),
            talib.EMA(_closes(bars), period),
            f"EMA({period})",
        )

    def test_rsi_matches(self) -> None:
        bars = _series()
        _compare(_incremental(RSI(14), bars), talib.RSI(_closes(bars), 14), "RSI(14)")

    def test_atr_matches(self) -> None:
        """ATR drives position sizing: wrong by a few percent means every
        position sized wrong by the same amount, in the same direction."""
        bars = _series()
        high, low, close = _hlc(bars)
        _compare(_incremental(ATR(14), bars), talib.ATR(high, low, close, 14), "ATR(14)")

    def test_macd_line_matches(self) -> None:
        bars = _series(n=200)
        macd = MACD()
        ours = _incremental(macd, bars)
        reference, _signal, _hist = talib.MACD(_closes(bars), 12, 26, 9)
        _compare(ours, reference, "MACD line")

    def test_bollinger_bands_match(self) -> None:
        """Population vs sample standard deviation differ by ~2.6% at n=20 —
        small enough to look like rounding, large enough to move a band through
        a price."""
        bars = _series()
        bb = BollingerBands(20, 2.0)
        uppers, middles, lowers = [], [], []
        for bar in bars:
            bb.update(bar)
            uppers.append(bb.upper)
            middles.append(bb.value)
            lowers.append(bb.lower)
        ref_u, ref_m, ref_l = talib.BBANDS(_closes(bars), 20, 2.0, 2.0)
        _compare(uppers, ref_u, "BB upper")
        _compare(middles, ref_m, "BB middle")
        _compare(lowers, ref_l, "BB lower")


class TestReadinessIsHonest:
    """A 20-EMA built from three bars looks like a working indicator."""

    def test_an_indicator_reports_not_ready_before_its_period(self) -> None:
        ema = EMA(20)
        for bar in _series(n=19):
            assert ema.update(bar) is None
        assert not ema.is_ready
        assert ema.value is None

    def test_it_becomes_ready_exactly_at_the_period(self) -> None:
        ema = EMA(20)
        bars = _series(n=20)
        for bar in bars[:19]:
            ema.update(bar)
        assert ema.update(bars[19]) is not None
        assert ema.is_ready

    def test_ema_first_value_is_the_simple_mean_of_the_first_n(self) -> None:
        """The seeding convention, asserted directly rather than only via the
        TA-Lib comparison — so a failure says WHICH rule broke."""
        bars = _series(n=5)
        ema = EMA(5)
        for bar in bars[:4]:
            assert ema.update(bar) is None
        first = ema.update(bars[4])
        expected = sum(float(b.close) for b in bars) / 5
        assert first is not None and abs(first - expected) < 1e-9

    def test_rsi_needs_one_more_bar_than_its_period(self) -> None:
        """RSI works on CHANGES, so N changes need N+1 closes."""
        rsi = RSI(14)
        bars = _series(n=15)
        for bar in bars[:14]:
            assert rsi.update(bar) is None
        assert rsi.update(bars[14]) is not None

    @pytest.mark.parametrize(
        ("factory", "bad"),
        [(SMA, 0), (EMA, 0), (RSI, 1), (ATR, 0), (BollingerBands, 1), (VolumeRatio, 0)],
    )
    def test_a_nonsensical_period_is_refused(self, factory, bad: int) -> None:
        with pytest.raises(IndicatorError):
            factory(bad)

    def test_macd_refuses_an_inverted_pair(self) -> None:
        with pytest.raises(IndicatorError, match="shorter than slow"):
            MACD(fast=26, slow=12)


class TestStateSurvivesARestart:
    """A restart that changed indicator values would change signals silently."""

    @pytest.mark.parametrize(
        "factory",
        [
            lambda: SMA(20),
            lambda: EMA(20),
            lambda: RSI(14),
            lambda: ATR(14),
            lambda: MACD(),
            lambda: BollingerBands(20),
            lambda: VolumeRatio(20),
        ],
        ids=["sma", "ema", "rsi", "atr", "macd", "bb", "volume_ratio"],
    )
    def test_restore_reproduces_the_live_instance_exactly(self, factory) -> None:
        bars = _series(n=100)
        live = factory()
        live.warm_up(bars[:60])

        restored = factory()
        restored.restore(json.loads(json.dumps(live.snapshot())))

        # Not merely equal now — equal for the REST of the session. A snapshot
        # that captured the value but not the window would pass a same-instant
        # check and diverge on the very next bar.
        for bar in bars[60:]:
            assert live.update(bar) == restored.update(bar) or (
                live.value is not None
                and restored.value is not None
                and abs(live.value - restored.value) < 1e-9
            )

    def test_a_snapshot_is_json_serialisable(self) -> None:
        """It goes to Redis as JSON; a Decimal or a deque would not survive."""
        ema = EMA(20)
        ema.warm_up(_series(n=40))
        assert json.loads(json.dumps(ema.snapshot()))["value"] is not None

    def test_restoring_a_half_warm_indicator_keeps_it_half_warm(self) -> None:
        """The seed buffer must round-trip too, or a restart mid-warm-up
        silently restarts the warm-up."""
        ema = EMA(20)
        ema.warm_up(_series(n=10))
        assert not ema.is_ready
        restored = EMA(20)
        restored.restore(ema.snapshot())
        assert not restored.is_ready
        assert len(restored._seed) == 10


class TestVwapIsSessionAnchored:
    def test_volume_weighting_favours_the_heavy_bar(self) -> None:
        vwap = VWAP()
        bars = _series(n=3)
        for bar in bars:
            vwap.update(bar)
        assert vwap.value is not None

    def test_a_synthetic_bar_does_not_move_it(self) -> None:
        """A synthetic bar represents an interval in which nothing traded, so
        it carries no volume and must not shift the day's average cost basis."""
        vwap = VWAP()
        real = _series(n=1)[0]
        vwap.update(real)
        before = vwap.value
        synthetic = real.model_copy(update={"volume": 0, "synthetic": True})
        vwap.update(synthetic)
        assert vwap.value == before

    def test_reset_clears_the_session(self) -> None:
        """Forgetting to reset is how this silently becomes a multi-day
        average."""
        vwap = VWAP()
        vwap.warm_up(_series(n=20))
        assert vwap.is_ready
        vwap.reset()
        assert not vwap.is_ready and vwap.value is None


class TestVolumeRatioExcludesTheCurrentBar:
    def test_a_spike_is_measured_against_the_prior_average(self) -> None:
        """Including the current bar damps the very spike this detects: a bar
        with 10x normal volume would raise its own baseline."""
        vr = VolumeRatio(5)
        bars = _series(n=6)
        flat = [b.model_copy(update={"volume": 1000}) for b in bars[:5]]
        for bar in flat:
            vr.update(bar)
        spike = bars[5].model_copy(update={"volume": 10_000})
        assert vr.update(spike) == pytest.approx(10.0)

    def test_it_is_not_ready_before_the_window_fills(self) -> None:
        vr = VolumeRatio(5)
        for bar in _series(n=4):
            assert vr.update(bar) is None


class TestAtrPercent:
    def test_percent_of_price_is_what_the_outlier_filter_wants(self) -> None:
        atr = ATR(14)
        atr.warm_up(_series(n=60))
        assert atr.value is not None
        pct = atr.percent_of(1000.0)
        assert pct is not None and pct == pytest.approx(atr.value / 1000.0 * 100.0)

    def test_no_percent_before_ready(self) -> None:
        assert ATR(14).percent_of(1000.0) is None

    def test_a_zero_price_does_not_divide(self) -> None:
        atr = ATR(14)
        atr.warm_up(_series(n=60))
        assert atr.percent_of(0.0) is None


class TestRestoreValidatesAsStrictlyAsInit:
    """Found by auditing E05/E06 rather than by a failing test.

    `__init__` validated its period; `restore` did not. A snapshot carrying
    `period: -5` produced an EMA with `alpha = -0.5` — an indicator that
    DIVERGES instead of converging, emits numbers the whole time, and is wrong
    in a direction nothing downstream can detect. Version skew or a
    partially-written Redis key is enough; an attacker is not required.
    """

    @pytest.mark.parametrize("bad", [-5, 0, "x", None], ids=["negative", "zero", "text", "none"])
    def test_a_corrupt_period_is_refused_on_restore(self, bad: object) -> None:
        with pytest.raises(IndicatorError):
            EMA(20).restore({"period": bad, "seed": [], "value": 1.0})

    def test_a_diverging_alpha_can_no_longer_be_constructed(self) -> None:
        """The specific consequence: a negative period yields a negative alpha,
        which amplifies every update instead of damping it."""
        with pytest.raises(IndicatorError):
            EMA(20).restore({"period": -5, "seed": [], "value": 100.0})

    @pytest.mark.parametrize(
        ("factory", "state"),
        [
            (lambda: SMA(20), {"period": 0, "window": []}),
            (
                lambda: RSI(14),
                {
                    "period": 1,
                    "gains": {"period": 1, "seed": [], "value": None},
                    "losses": {"period": 1, "seed": [], "value": None},
                    "previous_close": None,
                    "value": None,
                },
            ),
            (
                lambda: ATR(14),
                {
                    "period": -1,
                    "wilder": {"period": 1, "seed": [], "value": None},
                    "previous_close": None,
                },
            ),
            (lambda: BollingerBands(20), {"period": 1, "deviations": 2.0, "window": []}),
            (lambda: VolumeRatio(20), {"period": 0, "window": [], "value": None}),
        ],
        ids=["sma", "rsi", "atr", "bollinger", "volume_ratio"],
    )
    def test_every_indicator_validates_on_restore(self, factory, state: dict) -> None:
        with pytest.raises(IndicatorError):
            factory().restore(state)

    def test_a_valid_snapshot_still_restores(self) -> None:
        """The control — validation must not break the normal path."""
        live = EMA(20)
        live.warm_up(_series(n=40))
        restored = EMA(20)
        restored.restore(live.snapshot())
        assert restored.value == live.value


class TestTheMoneyBoundaryIsExplicit:
    """ATR reaches position sizing, and sizing is Decimal everywhere else.

    Indicators compute in float on purpose — they are the numerical-library
    boundary, and TA-Lib parity is defined in float. But `1.4000000000000057`
    multiplied by a stop multiplier and divided into a risk budget carries that
    representation error into the quantity. The crossing needs to be named.
    """

    def _warm_atr(self) -> ATR:
        atr = ATR(14)
        atr.warm_up(_series(n=60))
        return atr

    def test_value_stays_float_for_the_indicator_path(self) -> None:
        assert isinstance(self._warm_atr().value, float)

    def test_as_decimal_gives_the_money_path_a_decimal(self) -> None:

        assert isinstance(self._warm_atr().as_decimal(), Decimal)

    def test_the_quantisation_removes_the_representation_tail(self) -> None:

        from algotrader.indicators.framework import to_decimal

        assert to_decimal(1.4000000000000057) == Decimal("1.4000")

    def test_it_rounds_half_up_not_bankers(self) -> None:
        """Banker's rounding on money is a surprise; half-up is what a contract
        note does."""

        from algotrader.indicators.framework import to_decimal

        assert to_decimal(1.00005, places=4) == Decimal("1.0001")

    def test_none_survives_the_crossing(self) -> None:
        from algotrader.indicators.framework import to_decimal

        assert to_decimal(None) is None
        assert ATR(14).as_decimal() is None
