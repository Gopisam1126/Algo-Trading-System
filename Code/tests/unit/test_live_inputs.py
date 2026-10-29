"""The live risk inputs: ATR, per-share margin and correlations (E15-S14).

Each producer's job is to answer only when it can vouch for the answer, so
nearly every test here is a refusal paired with the control that shows the
refusal is not universal. The service-level acceptance criteria - an approval
sized from these inputs, a rejection naming each missing one - are in
``tests/integration/test_execution_service_wiring.py::TestS14*``, against the
real Redis, Postgres and Kite adapter.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
import random
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from algotrader.broker.adapter import RateLimitError
from algotrader.broker.kite.mapping import MappingError
from algotrader.broker.kite.trading import KiteTradingAdapter
from algotrader.broker.ratelimit import RateLimitExceededError
from algotrader.common.calendar import IST, MarketCalendar, load_holidays_with_status
from algotrader.common.enums import Direction, Side, Timeframe
from algotrader.common.models.market import Bar, IndicatorSnapshot
from algotrader.common.redis import keys
from algotrader.execution.live_inputs import (
    ATR_FRESHNESS_GRACE,
    DAILY_SLACK_SESSIONS,
    MAX_STATE_SKEW,
    BrokerMarginPerShare,
    DailyCloseCorrelations,
    LiveSessionFacts,
    PublishedATR,
    daily_correlations,
    read_atr,
    strategy_timeframes,
)
from algotrader.execution.risk.correlation import CORRELATION_WINDOW_SESSIONS
from algotrader.execution.session_context import NoProducerYet
from algotrader.indicators.engine import IndicatorSet
from algotrader.indicators.publish import INDICATOR_STATE_TTL_SECONDS, publish_indicator_state
from algotrader.strategy.dsl import load_strategy_yaml

NOW = dt.datetime(2026, 8, 25, 5, 0, tzinfo=dt.UTC)  # 10:30 IST
CONFIG = Path(__file__).resolve().parents[2] / "config"


class FakeRedis:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}

    async def set(self, key: str, value: str, ex: int | None = None, **_: Any) -> bool:
        self.strings[key] = value
        self.ttls[key] = ex
        return True

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)


def _bars(
    n: int,
    *,
    symbol: str = "INFY",
    timeframe: Timeframe = Timeframe.M5,
    last_open: dt.datetime = NOW - dt.timedelta(minutes=5),
    spread: float = 0.004,
) -> list[Bar]:
    """``n`` bars ending with the one that opens at ``last_open``."""
    rng = random.Random(7)
    price = 1200.0
    step = dt.timedelta(seconds=timeframe.seconds)
    out: list[Bar] = []
    for i in range(n):
        price = max(10.0, price * (1 + rng.uniform(-0.005, 0.005)))
        out.append(
            Bar(
                symbol=symbol,
                timeframe=timeframe,
                open_ts=last_open - step * (n - 1 - i),
                open=Decimal(f"{price:.4f}"),
                high=Decimal(f"{price * (1 + spread):.4f}"),
                low=Decimal(f"{price * (1 - spread):.4f}"),
                close=Decimal(f"{price:.4f}"),
                volume=rng.randint(1_000, 20_000),
            )
        )
    return out


def _warm(symbol: str = "INFY", timeframe: Timeframe = Timeframe.M5, **kw: Any) -> IndicatorSet:
    s = IndicatorSet(symbol=symbol, timeframe=timeframe)
    s.warm_up(_bars(260, symbol=symbol, timeframe=timeframe, **kw))
    assert s.is_ready
    return s


def _snapshot(**overrides: Any) -> IndicatorSnapshot:
    fields: dict[str, Any] = {
        "symbol": "INFY",
        "timeframe": Timeframe.M5,
        "as_of": NOW,
        "ready": True,
        "close": Decimal("1200"),
        "atr_14": Decimal("12.5"),
    }
    fields.update(overrides)
    return IndicatorSnapshot(**fields)


# ---------------------------------------------------------------------------
# The writer: IndicatorSet.published and publish_indicator_state
# ---------------------------------------------------------------------------


class TestTheSetPublishesWhatTheReaderNeeds:
    def test_nothing_is_published_before_a_bar(self) -> None:
        assert IndicatorSet(symbol="INFY", timeframe=Timeframe.M5).published() is None

    def test_as_of_is_the_close_of_the_newest_bar(self) -> None:
        s = _warm(last_open=NOW - dt.timedelta(minutes=5))
        snap = s.published()
        assert snap is not None
        assert snap.as_of == NOW, "a 10:25 bar closes at 10:30"
        assert (snap.symbol, snap.timeframe, snap.ready) == ("INFY", Timeframe.M5, True)
        assert snap.close == s.last_close

    def test_the_atr_is_the_money_path_value(self) -> None:
        s = _warm()
        snap = s.published()
        assert snap is not None and snap.atr_14 is not None
        assert snap.atr_14 == s.indicators["atr_14"].as_decimal()  # type: ignore[attr-defined]
        assert snap.atr_14.as_tuple().exponent == -4

    def test_a_warming_set_publishes_not_ready(self) -> None:
        s = IndicatorSet(symbol="INFY", timeframe=Timeframe.M5)
        s.warm_up(_bars(30))
        snap = s.published()
        assert snap is not None and snap.ready is False

    def test_a_feed_gap_publishes_not_ready(self) -> None:
        s = _warm()
        s.mark_stale("feed gap")
        snap = s.published()
        assert snap is not None and snap.ready is False

    def test_a_restored_set_publishes_the_same_state(self) -> None:
        live = _warm()
        restored = IndicatorSet(symbol="INFY", timeframe=Timeframe.M5)
        restored.restore(json.loads(json.dumps(live.snapshot())))
        assert restored.published() == live.published()

    def test_a_snapshot_from_before_this_story_restores_unpublishable(self) -> None:
        """Old snapshots carry no last bar: the set restores, and publishes
        nothing until its next bar - refused for one interval, never wrong."""
        state = _warm().snapshot()
        del state["last_bar_open"], state["last_close"]
        restored = IndicatorSet(symbol="INFY", timeframe=Timeframe.M5)
        restored.restore(state)
        assert restored.published() is None

    def test_a_naive_last_bar_time_is_refused_on_restore(self) -> None:
        state = _warm().snapshot()
        state["last_bar_open"] = "2026-08-25T04:55:00"
        with pytest.raises(ValueError, match="naive"):
            IndicatorSet(symbol="INFY", timeframe=Timeframe.M5).restore(state)


class TestPublishIndicatorState:
    async def test_it_writes_the_key_the_reader_reads_with_a_ttl(self) -> None:
        redis = FakeRedis()
        written = await publish_indicator_state(redis, _warm())  # type: ignore[arg-type]
        key = keys.indicator_state("INFY", Timeframe.M5)
        assert written is not None
        assert IndicatorSnapshot.model_validate_json(redis.strings[key]) == written
        assert redis.ttls[key] == INDICATOR_STATE_TTL_SECONDS

    async def test_it_writes_nothing_before_a_bar(self) -> None:
        redis = FakeRedis()
        s = IndicatorSet(symbol="INFY", timeframe=Timeframe.M5)
        assert await publish_indicator_state(redis, s) is None  # type: ignore[arg-type]
        assert redis.strings == {}


# ---------------------------------------------------------------------------
# The reader: read_atr (pure) and PublishedATR
# ---------------------------------------------------------------------------


def _read(snapshot: IndicatorSnapshot | None, **kw: Any):
    return read_atr(
        snapshot,
        symbol=kw.pop("symbol", "INFY"),
        timeframe=kw.pop("timeframe", Timeframe.M5),
        now=kw.pop("now", NOW),
        **kw,
    )


class TestReadAtr:
    def test_a_current_ready_state_is_read(self) -> None:
        reading = _read(_snapshot())
        assert (reading.atr, reading.refused) == (Decimal("12.5"), None)

    def test_no_state(self) -> None:
        reading = _read(None)
        assert reading.atr is None and "no published indicator state" in str(reading.refused)

    @pytest.mark.parametrize(("field", "value"), [("symbol", "TCS"), ("timeframe", Timeframe.M15)])
    def test_a_state_that_disagrees_with_its_key(self, field: str, value: object) -> None:
        reading = _read(_snapshot(**{field: value}))
        assert reading.atr is None and "describes" in str(reading.refused)

    def test_not_ready(self) -> None:
        reading = _read(_snapshot(ready=False))
        assert reading.atr is None and "not ready" in str(reading.refused)

    def test_a_state_from_the_future_is_a_forming_bar(self) -> None:
        reading = _read(_snapshot(as_of=NOW + MAX_STATE_SKEW + dt.timedelta(seconds=1)))
        assert reading.atr is None and "forming bar" in str(reading.refused)

    def test_skew_within_the_bound_is_read(self) -> None:
        assert _read(_snapshot(as_of=NOW + MAX_STATE_SKEW)).atr == Decimal("12.5")

    def test_one_bar_plus_grace_is_the_limit(self) -> None:
        limit = dt.timedelta(minutes=5) + ATR_FRESHNESS_GRACE
        assert _read(_snapshot(as_of=NOW - limit)).atr == Decimal("12.5")
        late = _read(_snapshot(as_of=NOW - limit - dt.timedelta(seconds=1)))
        assert late.atr is None and "missed a bar" in str(late.refused)

    def test_yesterdays_state_is_stale(self) -> None:
        reading = _read(_snapshot(as_of=NOW - dt.timedelta(hours=19)))
        assert reading.atr is None

    @pytest.mark.parametrize("atr", [None, Decimal("0"), Decimal("-0")])
    def test_no_usable_atr(self, atr: Decimal | None) -> None:
        reading = _read(_snapshot(atr_14=atr))
        assert reading.atr is None and "no usable ATR" in str(reading.refused)

    @given(
        timeframe=st.sampled_from([Timeframe.M1, Timeframe.M5, Timeframe.M15, Timeframe.H1]),
        age=st.integers(min_value=-5, max_value=2 * 3600 + 60),
    )
    def test_fresh_exactly_when_at_most_one_bar_plus_grace_old(
        self, timeframe: Timeframe, age: int
    ) -> None:
        snapshot = _snapshot(timeframe=timeframe, as_of=NOW - dt.timedelta(seconds=age))
        reading = _read(snapshot, timeframe=timeframe)
        fresh = age <= timeframe.seconds + ATR_FRESHNESS_GRACE.total_seconds()
        assert (reading.atr is not None) is fresh


class TestPublishedAtr:
    def _reader(self, redis: FakeRedis, **timeframes: Timeframe) -> PublishedATR:
        return PublishedATR(redis, timeframes=timeframes or {"orb_classic": Timeframe.M5})

    async def test_what_the_writer_publishes_the_reader_reads(self) -> None:
        """The seam itself: the real writer, the real key, the real model."""
        redis = FakeRedis()
        s = _warm()
        await publish_indicator_state(redis, s)  # type: ignore[arg-type]
        atr = await self._reader(redis).atr("INFY", strategy_id="orb_classic", now=NOW)
        assert atr == s.indicators["atr_14"].as_decimal()  # type: ignore[attr-defined]

    async def test_the_strategys_own_timeframe_is_read(self) -> None:
        """A 5m state exists; a 15m strategy's ATR is not the 5m one."""
        redis = FakeRedis()
        await publish_indicator_state(redis, _warm())  # type: ignore[arg-type]
        reader = self._reader(redis, orb_classic=Timeframe.M5, orb_slow=Timeframe.M15)
        assert await reader.atr("INFY", strategy_id="orb_slow", now=NOW) is None
        assert await reader.atr("INFY", strategy_id="orb_classic", now=NOW) is not None

    async def test_an_unknown_strategy_has_no_atr(self) -> None:
        redis = FakeRedis()
        await publish_indicator_state(redis, _warm())  # type: ignore[arg-type]
        assert await self._reader(redis).atr("INFY", strategy_id="mystery", now=NOW) is None

    async def test_an_unparseable_state_is_absent(self) -> None:
        redis = FakeRedis()
        redis.strings[keys.indicator_state("INFY", Timeframe.M5)] = '{"atr_14": "12.5"}'
        assert await self._reader(redis).atr("INFY", strategy_id="orb_classic", now=NOW) is None

    async def test_a_stale_state_is_not_read(self) -> None:
        redis = FakeRedis()
        await publish_indicator_state(  # type: ignore[arg-type]
            redis, _warm(last_open=NOW - dt.timedelta(minutes=30))
        )
        assert await self._reader(redis).atr("INFY", strategy_id="orb_classic", now=NOW) is None

    def test_no_timeframes_is_refused_at_construction(self) -> None:
        with pytest.raises(ValueError, match="every signal is refused"):
            PublishedATR(FakeRedis(), timeframes={})

    async def test_a_daily_strategy_has_no_live_atr(self, caplog: pytest.LogCaptureFixture) -> None:
        redis = FakeRedis()
        reader = PublishedATR(
            redis, timeframes={"orb_classic": Timeframe.M5, "swing": Timeframe.D1}
        )
        assert "swing" in caplog.text and "no live indicator state" in caplog.text
        assert await reader.atr("INFY", strategy_id="swing", now=NOW) is None


class TestStrategyTimeframes:
    def test_from_the_shipped_strategy(self) -> None:
        doc = load_strategy_yaml((CONFIG / "strategies" / "orb_classic.yaml").read_text())
        assert strategy_timeframes([doc]) == {"orb_classic": Timeframe.M5}

    def test_one_id_two_timeframes_is_refused(self) -> None:
        doc = load_strategy_yaml((CONFIG / "strategies" / "orb_classic.yaml").read_text())
        other = doc.model_copy(
            update={"applicability": doc.applicability.model_copy(update={"timeframe": "15m"})}
        )
        with pytest.raises(ValueError, match="different timeframes"):
            strategy_timeframes([doc, other])

    def test_the_same_declaration_twice_is_one(self) -> None:
        doc = load_strategy_yaml((CONFIG / "strategies" / "orb_classic.yaml").read_text())
        assert strategy_timeframes([doc, doc]) == {"orb_classic": Timeframe.M5}


# ---------------------------------------------------------------------------
# Margin: the adapter read, and the producer over it
# ---------------------------------------------------------------------------


class KiteMargins:
    """Kite's ``order_margins`` (the fields we read), recording each call."""

    def __init__(self, answer: Any = None) -> None:
        self.calls: list[list[dict[str, Any]]] = []
        self.answer = answer

    def order_margins(self, params: list[dict[str, Any]]) -> Any:
        self.calls.append(params)
        if self.answer is not None:
            return self.answer
        return [{"type": "equity", "tradingsymbol": params[0]["tradingsymbol"], "total": 240.35}]


class Limiter:
    def __init__(self, *, refuse_data: bool = False) -> None:
        self.data = 0
        self.orders = 0
        self.refuse_data = refuse_data

    async def acquire_data(self) -> float:
        if self.refuse_data:
            raise RateLimitExceededError("data", tokens_left=0.0)
        self.data += 1
        return 1.0

    async def acquire_order(self) -> float:
        self.orders += 1
        return 1.0


def _adapter(client: Any, limiter: Limiter | None = None) -> KiteTradingAdapter:
    return KiteTradingAdapter(auth=None, client=client, limiter=limiter)  # type: ignore[arg-type]


class TestMarginRequired:
    async def test_it_asks_for_the_entrys_own_order(self) -> None:
        client = KiteMargins()
        total = await _adapter(client).margin_required("INFY", side=Side.SELL)
        assert total == Decimal("240.35")
        (params,) = client.calls
        assert params == [
            {
                "exchange": "NSE",
                "tradingsymbol": "INFY",
                "transaction_type": "SELL",
                "variety": "regular",
                "product": "MIS",
                "order_type": "MARKET",
                "quantity": 1,
                "price": 0,
                "trigger_price": 0,
            }
        ]

    async def test_it_takes_a_data_token_never_an_order_token(self) -> None:
        """A margin lookup on the order bucket could refuse an exit order."""
        limiter = Limiter()
        await _adapter(KiteMargins(), limiter).margin_required("INFY", side=Side.BUY)
        assert (limiter.data, limiter.orders) == (1, 0)

    async def test_a_refused_data_token_is_a_rate_limit_error(self) -> None:
        client = KiteMargins()
        with pytest.raises(RateLimitError):
            await _adapter(client, Limiter(refuse_data=True)).margin_required("INFY", side=Side.BUY)
        assert client.calls == [], "refused before the broker was asked"

    @pytest.mark.parametrize(
        "answer",
        [
            [],
            [{"tradingsymbol": "INFY", "total": 1}, {"tradingsymbol": "INFY", "total": 2}],
            ["INFY"],
            {"total": 240},
            [{"tradingsymbol": "TCS", "total": 240}],
            [{"tradingsymbol": "INFY"}],
            [{"tradingsymbol": "INFY", "total": None}],
            [{"tradingsymbol": "INFY", "total": "lots"}],
            [{"tradingsymbol": "INFY", "total": float("nan")}],
            [{"tradingsymbol": "INFY", "total": float("inf")}],
        ],
        ids=[
            "no-rows",
            "two-rows",
            "not-a-row",
            "not-a-list",
            "another-symbol",
            "no-total",
            "null-total",
            "text-total",
            "nan-total",
            "inf-total",
        ],
    )
    async def test_an_answer_it_cannot_read_raises(self, answer: Any) -> None:
        with pytest.raises(MappingError):
            await _adapter(KiteMargins(answer)).margin_required("INFY", side=Side.BUY)

    async def test_a_zero_total_is_returned_as_the_broker_said_it(self) -> None:
        """The adapter reports; the producer decides zero is unusable."""
        answer = [{"tradingsymbol": "INFY", "total": 0}]
        assert await _adapter(KiteMargins(answer)).margin_required(
            "INFY", side=Side.BUY
        ) == Decimal("0")

    async def test_zero_shares_is_not_a_question(self) -> None:
        with pytest.raises(ValueError, match="at least one share"):
            await _adapter(KiteMargins()).margin_required("INFY", side=Side.BUY, quantity=0)


class Broker:
    def __init__(self, total: Decimal = Decimal("240"), delay: float = 0.0) -> None:
        self.total = total
        self.delay = delay
        self.sides: list[Side] = []

    async def margin_required(self, symbol: str, *, side: Side, quantity: int = 1) -> Decimal:
        assert quantity == 1
        self.sides.append(side)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.total


class TestBrokerMarginPerShare:
    @pytest.mark.parametrize(
        ("direction", "side"), [(Direction.LONG, Side.BUY), (Direction.SHORT, Side.SELL)]
    )
    async def test_the_entrys_side_is_asked(self, direction: Direction, side: Side) -> None:
        broker = Broker()
        margin = await BrokerMarginPerShare(broker).margin_per_share(
            "INFY", Decimal("1200"), direction=direction, now=NOW
        )
        assert margin == Decimal("240") and broker.sides == [side]

    @pytest.mark.parametrize("total", [Decimal("0"), Decimal("-1")])
    async def test_a_non_positive_margin_is_unknown(self, total: Decimal) -> None:
        margin = await BrokerMarginPerShare(Broker(total)).margin_per_share(
            "INFY", Decimal("1200"), direction=Direction.LONG, now=NOW
        )
        assert margin is None

    async def test_a_slow_broker_times_out(self) -> None:
        producer = BrokerMarginPerShare(Broker(delay=1.0), timeout_seconds=0.01)
        with pytest.raises(TimeoutError):
            await producer.margin_per_share(
                "INFY", Decimal("1200"), direction=Direction.LONG, now=NOW
            )

    @pytest.mark.parametrize("timeout", [0, -1.0])
    def test_the_timeout_must_be_positive(self, timeout: float) -> None:
        with pytest.raises(ValueError, match="positive"):
            BrokerMarginPerShare(Broker(), timeout_seconds=timeout)


# ---------------------------------------------------------------------------
# Correlations: daily_correlations (pure) and DailyCloseCorrelations
# ---------------------------------------------------------------------------

TODAY = dt.date(2026, 8, 25)


@pytest.fixture(scope="module")
def calendar() -> MarketCalendar:
    status = load_holidays_with_status(str(CONFIG / "nse_holidays.yaml"))
    return MarketCalendar(status.dates, covers_years=status.covers_years)


def _sessions(calendar: MarketCalendar, n: int, *, last: dt.date) -> list[dt.date]:
    days = [last]
    while len(days) < n:
        days.append(calendar.previous_trading_day(days[-1]))
    return list(reversed(days))


def _daily(symbol: str, closes: dict[dt.date, float]) -> list[dict[str, Any]]:
    return [
        {
            "symbol": symbol,
            "open_ts": dt.datetime.combine(day, dt.time(9, 15), tzinfo=IST).astimezone(dt.UTC),
            "close": Decimal(f"{price:.4f}"),
        }
        for day, price in sorted(closes.items())
    ]


def _walk(days: list[dt.date], seed: int, *, follow: list[float] | None = None) -> dict:
    """A random walk; with ``follow``, the same returns plus a little noise."""
    rng = random.Random(seed)
    price, out = 1000.0, {}
    for i, day in enumerate(days):
        step = follow[i] if follow is not None else rng.gauss(0, 0.01)
        if follow is not None:
            step += rng.gauss(0, 0.001)
        price *= math.exp(step)
        out[day] = price
    return out


def _returns(days: list[dt.date], seed: int) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(0, 0.01) for _ in days]


class TestDailyCorrelations:
    def _history(self, calendar: MarketCalendar, n: int = 75) -> tuple[list, dict]:
        days = _sessions(calendar, n, last=calendar.previous_trading_day(TODAY))
        base = _returns(days, 1)
        return days, {
            "INFY": _daily("INFY", _walk(days, 1, follow=base)),
            "TCS": _daily("TCS", _walk(days, 2, follow=base)),  # one bet with INFY
            "SBIN": _daily("SBIN", _walk(days, 3)),  # independent
        }

    def _corr(self, calendar: MarketCalendar, history: dict, against=("TCS", "SBIN")) -> dict:
        return daily_correlations(
            "INFY",
            history,
            against=list(against),
            today=TODAY,
            last_session=calendar.previous_trading_day(TODAY),
        )

    def test_one_bet_reads_high_and_an_independent_name_low(self, calendar) -> None:
        _, history = self._history(calendar)
        out = self._corr(calendar, history)
        assert out["TCS"] > Decimal("0.9")
        assert abs(out["SBIN"]) < Decimal("0.5")

    def test_todays_bar_is_not_part_of_a_pre_market_quantity(self, calendar) -> None:
        _, history = self._history(calendar)
        before = self._corr(calendar, history)
        wild = dict(history)
        wild["TCS"] = history["TCS"] + _daily("TCS", {TODAY: 5.0})
        assert self._corr(calendar, wild) == before

    def test_a_candidate_whose_history_stopped_has_no_correlations(self, calendar) -> None:
        _, history = self._history(calendar)
        history["INFY"] = history["INFY"][:-1]  # yesterday's close missing
        assert self._corr(calendar, history) == {}

    def test_a_held_name_whose_history_stopped_is_unknown_alone(self, calendar) -> None:
        _, history = self._history(calendar)
        history["SBIN"] = history["SBIN"][:-1]
        out = self._corr(calendar, history)
        assert "SBIN" not in out and "TCS" in out

    def test_too_few_common_sessions_is_unknown(self, calendar) -> None:
        _, history = self._history(calendar, n=31)  # 30 returns; the floor is 30
        assert "TCS" in self._corr(calendar, history)
        short = {k: v[-30:] for k, v in history.items()}  # 29 returns
        assert self._corr(calendar, short) == {}

    def test_a_symbol_with_no_history_is_unknown(self, calendar) -> None:
        _, history = self._history(calendar)
        del history["SBIN"]
        assert "SBIN" not in self._corr(calendar, history)

    def test_pairs_are_aligned_on_common_dates(self, calendar) -> None:
        """TCS skipped three sessions mid-window. Paired by position, every
        return before the gap meets the wrong day's return and the reading
        collapses; aligned on dates it is the one bet it is."""
        days, history = self._history(calendar)
        gap = {days[-30], days[-31], days[-32]}
        history["TCS"] = [
            b for b in history["TCS"] if b["open_ts"].astimezone(IST).date() not in gap
        ]
        aligned = self._corr(calendar, history, against=["TCS"])["TCS"]
        assert aligned > Decimal("0.9")

        from algotrader.execution.risk.correlation import correlations_against

        by_position = correlations_against(
            "INFY",
            {s: [Decimal(b["close"]) for b in bars] for s, bars in history.items()},
            against=["TCS"],
        )["TCS"]
        assert by_position < Decimal("0.7"), "the misalignment this guards against is real"


class Bars:
    def __init__(self, history: dict[str, list[dict[str, Any]]]) -> None:
        self.history = history
        self.asked: list[tuple[list[str], Timeframe, int]] = []

    async def warm_up_batch(self, symbols, timeframe, bars_each):
        self.asked.append((list(symbols), timeframe, bars_each))
        return {s: self.history.get(s, [])[-bars_each:] for s in symbols}


class TestDailyCloseCorrelations:
    async def test_one_query_for_the_candidate_and_every_held_name(self, calendar) -> None:
        _, history = TestDailyCorrelations()._history(calendar)
        bars = Bars(history)
        out = await DailyCloseCorrelations(bars, calendar=calendar).correlations(
            "INFY", ["TCS", "SBIN"], now=NOW
        )
        assert set(out) == {"TCS", "SBIN"}
        assert bars.asked == [
            (
                ["INFY", "TCS", "SBIN"],
                Timeframe.D1,
                CORRELATION_WINDOW_SESSIONS + 1 + DAILY_SLACK_SESSIONS,
            )
        ]

    async def test_nothing_held_asks_nothing(self, calendar) -> None:
        bars = Bars({})
        assert (
            await DailyCloseCorrelations(bars, calendar=calendar).correlations("INFY", [], now=NOW)
            == {}
        )
        assert bars.asked == []

    async def test_today_is_the_ist_date(self, calendar) -> None:
        """19:00 UTC on the 24th is 00:30 IST on the 25th: yesterday's session
        is the 24th's, and the 24th's bar must count."""
        _, history = TestDailyCorrelations()._history(calendar)
        late = dt.datetime(2026, 8, 24, 19, 0, tzinfo=dt.UTC)
        out = await DailyCloseCorrelations(Bars(history), calendar=calendar).correlations(
            "INFY", ["TCS"], now=late
        )
        assert "TCS" in out

    def test_a_calendar_is_required(self) -> None:
        with pytest.raises(TypeError, match="previous_trading_day"):
            DailyCloseCorrelations(Bars({}), calendar=object())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The composite
# ---------------------------------------------------------------------------


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    async def unhealthy_services(self, **kw):
        self.calls.append(("health", (), kw))
        return ()

    async def atr(self, symbol, **kw):
        self.calls.append(("atr", (symbol,), kw))
        return Decimal("1")

    async def margin_per_share(self, symbol, price, **kw):
        self.calls.append(("margin", (symbol, price), kw))
        return Decimal("2")

    async def correlations(self, symbol, against, **kw):
        self.calls.append(("corr", (symbol, tuple(against)), kw))
        return {"TCS": Decimal("0.1")}


class TestLiveSessionFacts:
    async def test_each_question_goes_to_its_producer_unchanged(self) -> None:
        r = Recorder()
        facts = LiveSessionFacts(health=r, atr=r, margin=r, correlations=r)
        assert await facts.unhealthy_services(now=NOW) == ()
        assert await facts.atr("INFY", strategy_id="orb_classic", now=NOW) == Decimal("1")
        assert await facts.margin_per_share(
            "INFY", Decimal("1200"), direction=Direction.SHORT, now=NOW
        ) == Decimal("2")
        assert await facts.correlations("INFY", ["TCS"], now=NOW) == {"TCS": Decimal("0.1")}
        assert r.calls == [
            ("health", (), {"now": NOW}),
            ("atr", ("INFY",), {"strategy_id": "orb_classic", "now": NOW}),
            ("margin", ("INFY", Decimal("1200")), {"direction": Direction.SHORT, "now": NOW}),
            ("corr", ("INFY", ("TCS",)), {"now": NOW}),
        ]

    async def test_health_without_a_producer_is_still_unknown(self) -> None:
        r = Recorder()
        facts = LiveSessionFacts(health=NoProducerYet(), atr=r, margin=r, correlations=r)
        assert await facts.unhealthy_services(now=NOW) is None
