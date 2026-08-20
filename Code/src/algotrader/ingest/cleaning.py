"""Tick cleaning: validate, deduplicate, reject outliers, normalise (E05-S02..S05).

One bad print corrupts an EMA permanently. There is no later stage that notices
and no way to undo it — the indicator carries the poisoned value forward for as
many periods as its window, and every signal derived from it is quietly wrong.
So this pipeline runs before anything else sees a tick, and every rejection is
counted rather than silently dropped.

**The cold-start rule is the part most likely to be got wrong.** The outlier
filter is specified as ``max(5 x ATR%, 2%)`` — but ATR comes from the indicator
engine, which is not ready at session start. At 09:15, exactly when prints are
most erratic, the ATR term is unavailable. If that resolved to "no limit" the
filter would be inert precisely when it is needed. It resolves to the 2% floor
instead, and :meth:`OutlierFilter.cold_start` says so explicitly.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from algotrader.common.models.market import Tick
from algotrader.ingest.kite_protocol import RawTick

log = logging.getLogger(__name__)

#: How far a broker timestamp may sit from our clock before the tick is refused.
#: Generous enough for ordinary NTP drift, tight enough that a stuck or replayed
#: feed is caught rather than accepted as live.
MAX_CLOCK_SKEW_SECONDS = 5.0

#: The floor the outlier filter falls back to before ATR exists.
COLD_START_MOVE_PCT = Decimal("2.0")

#: Multiplier applied to ATR% once it is available.
ATR_MULTIPLE = Decimal(5)


class RejectReason(str):
    """Why a tick was dropped. A plain str so it reads well in a metric label."""


NULL_PRICE = RejectReason("null_or_zero_price")
NEGATIVE_PRICE = RejectReason("negative_price")
NEGATIVE_VOLUME = RejectReason("negative_volume")
VOLUME_WENT_BACKWARDS = RejectReason("volume_went_backwards")
CLOCK_SKEW = RejectReason("timestamp_outside_skew_window")
DUPLICATE = RejectReason("duplicate")
OUTLIER = RejectReason("outlier_move")
CROSSED_CIRCUIT = RejectReason("outside_circuit_band")
NO_TIMESTAMP = RejectReason("no_exchange_timestamp")


@dataclass
class RejectionLog:
    """Counts per reason, and a bounded sample of what was rejected.

    A count alone tells you something is wrong; a sample tells you what. The
    sample is bounded because a feed gone haywire would otherwise turn this into
    an unbounded memory leak at the worst possible moment.
    """

    counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    samples: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    sample_limit: int = 5

    def record(self, reason: str, detail: str) -> None:
        self.counts[reason] += 1
        bucket = self.samples[reason]
        if len(bucket) < self.sample_limit:
            bucket.append(detail)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def rate_for(self, reason: str, accepted: int) -> float:
        seen = self.counts.get(reason, 0) + accepted
        return 0.0 if seen == 0 else self.counts.get(reason, 0) / seen


# ---------------------------------------------------------------------------
# E05-S02 validation
# ---------------------------------------------------------------------------


@dataclass
class TickValidator:
    """Structural checks. Nothing here needs history or context."""

    max_skew_seconds: float = MAX_CLOCK_SKEW_SECONDS
    rejections: RejectionLog = field(default_factory=RejectionLog)
    #: Last accepted cumulative volume per instrument. Volume is monotonic
    #: within a session; a decrease means a stale or replayed packet.
    _last_volume: dict[int, int] = field(default_factory=dict)

    def check(self, tick: RawTick, *, now: dt.datetime | None = None) -> str | None:
        """``None`` when the tick is acceptable, otherwise the reason."""
        if tick.last_price is None or tick.last_price == 0:
            self.rejections.record(NULL_PRICE, f"token={tick.instrument_token}")
            return NULL_PRICE
        if tick.last_price < 0:
            self.rejections.record(
                NEGATIVE_PRICE, f"token={tick.instrument_token} ltp={tick.last_price}"
            )
            return NEGATIVE_PRICE

        if tick.volume is not None:
            if tick.volume < 0:
                self.rejections.record(
                    NEGATIVE_VOLUME, f"token={tick.instrument_token} vol={tick.volume}"
                )
                return NEGATIVE_VOLUME
            previous = self._last_volume.get(tick.instrument_token)
            if previous is not None and tick.volume < previous:
                # Cumulative session volume cannot fall. A decrease is a replayed
                # or out-of-order packet, and accepting it would make any
                # volume-delta calculation negative.
                self.rejections.record(
                    VOLUME_WENT_BACKWARDS,
                    f"token={tick.instrument_token} {previous} -> {tick.volume}",
                )
                return VOLUME_WENT_BACKWARDS

        if tick.exchange_timestamp is not None:
            now = now or dt.datetime.now(dt.UTC)
            skew = abs((now - tick.exchange_timestamp).total_seconds())
            if skew > self.max_skew_seconds:
                self.rejections.record(
                    CLOCK_SKEW,
                    f"token={tick.instrument_token} skew={skew:.1f}s",
                )
                return CLOCK_SKEW

        if tick.volume is not None:
            self._last_volume[tick.instrument_token] = tick.volume
        return None


# ---------------------------------------------------------------------------
# E05-S03 deduplication
# ---------------------------------------------------------------------------


@dataclass
class Deduplicator:
    """Bounded LRU on (token, exchange_ts, ltp, volume).

    A reconnect replays recent ticks; without this they are counted twice and
    every volume-derived indicator is inflated. Bounded because an unbounded set
    grows for the whole session — 200 symbols ticking through six hours is a lot
    of tuples, and the memory is needed elsewhere.

    Per-instrument rather than global so one busy symbol cannot evict the
    history of every other one.
    """

    capacity_per_instrument: int = 512
    rejections: RejectionLog = field(default_factory=RejectionLog)
    _seen: dict[int, OrderedDict[tuple[object, ...], None]] = field(default_factory=dict)

    def is_duplicate(self, tick: RawTick) -> bool:
        key = (
            tick.exchange_timestamp.timestamp() if tick.exchange_timestamp else None,
            str(tick.last_price),
            tick.volume,
            tick.last_quantity,
        )
        window = self._seen.setdefault(tick.instrument_token, OrderedDict())
        if key in window:
            window.move_to_end(key)
            self.rejections.record(DUPLICATE, f"token={tick.instrument_token}")
            return True
        window[key] = None
        if len(window) > self.capacity_per_instrument:
            window.popitem(last=False)
        return False


# ---------------------------------------------------------------------------
# E05-S04 outlier filtering
# ---------------------------------------------------------------------------


@dataclass
class OutlierFilter:
    """Reject a move too large to be real.

    Two independent bounds, and the tick must satisfy both:

    - **A relative move bound**, ``max(5 x ATR%, 2%)``. ATR adapts the threshold
      to the instrument: 2% is a normal morning for one stock and impossible for
      another.
    - **The circuit band**, when known. A price outside it is not merely
      unlikely, it is one the exchange would not have printed — so it is a data
      error by definition rather than a judgement call.

    Rejections are logged, never silently dropped, and a cluster on one symbol
    is worth an alert: it usually means the feed is wrong about that instrument
    rather than that the instrument is moving.
    """

    rejections: RejectionLog = field(default_factory=RejectionLog)
    #: token -> last accepted price
    _last_price: dict[int, Decimal] = field(default_factory=dict)
    #: token -> ATR as a percentage of price, supplied by the indicator engine
    #: once it is warm. Absent means cold start.
    _atr_pct: dict[int, Decimal] = field(default_factory=dict)
    #: token -> (lower, upper) circuit prices for the session
    _circuit: dict[int, tuple[Decimal, Decimal]] = field(default_factory=dict)

    def set_atr_pct(self, token: int, atr_pct: Decimal) -> None:
        """Called by the indicator engine once ATR is ready for this symbol."""
        if atr_pct <= 0:
            raise ValueError(f"ATR% must be positive, got {atr_pct}")
        self._atr_pct[token] = atr_pct

    def set_circuit(self, token: int, lower: Decimal, upper: Decimal) -> None:
        if lower >= upper:
            raise ValueError(f"circuit band is inverted: lower={lower} upper={upper}")
        self._circuit[token] = (lower, upper)

    def cold_start(self, token: int) -> bool:
        """True when no ATR is known yet, so the 2% floor is doing the work.

        Exposed rather than implied: the filter being weaker at 09:15 is a real
        operational fact, and something that can be asserted in a test and shown
        on a health panel is better than a comment claiming it is handled.
        """
        return token not in self._atr_pct

    def threshold_pct(self, token: int) -> Decimal:
        """The move bound in force for this instrument, right now."""
        atr_pct = self._atr_pct.get(token)
        if atr_pct is None:
            return COLD_START_MOVE_PCT
        return max(ATR_MULTIPLE * atr_pct, COLD_START_MOVE_PCT)

    def check(self, tick: RawTick) -> str | None:
        """``None`` when the tick is plausible, otherwise the reason."""
        band = self._circuit.get(tick.instrument_token)
        if band is not None and not (band[0] <= tick.last_price <= band[1]):
            self.rejections.record(
                CROSSED_CIRCUIT,
                f"token={tick.instrument_token} ltp={tick.last_price} band={band}",
            )
            return CROSSED_CIRCUIT

        previous = self._last_price.get(tick.instrument_token)
        if previous is None or previous <= 0:
            # First price for this instrument: nothing to compare against. The
            # circuit band above is the only check that can apply.
            self._last_price[tick.instrument_token] = tick.last_price
            return None

        move_pct = abs(tick.last_price - previous) / previous * Decimal(100)
        limit = self.threshold_pct(tick.instrument_token)
        if move_pct > limit:
            self.rejections.record(
                OUTLIER,
                f"token={tick.instrument_token} {previous} -> {tick.last_price} "
                f"({move_pct:.2f}% > {limit:.2f}%"
                f"{', cold start' if self.cold_start(tick.instrument_token) else ''})",
            )
            return OUTLIER

        self._last_price[tick.instrument_token] = tick.last_price
        return None


# ---------------------------------------------------------------------------
# E05-S05 normalisation
# ---------------------------------------------------------------------------


def normalise(
    tick: RawTick,
    symbol: str,
    *,
    received_ts: dt.datetime | None = None,
) -> Tick:
    """``RawTick`` -> the domain :class:`Tick`.

    Runs LAST, so a tick that failed any check never becomes a domain object —
    there is no half-validated ``Tick`` anywhere in the system.

    A missing exchange timestamp falls back to arrival time and is flagged by
    the caller rather than silently substituted: for an index packet in quote
    mode the exchange simply does not send one, which is normal, but for a
    tradable instrument it is not.
    """
    received = received_ts or dt.datetime.now(dt.UTC)
    return Tick(
        symbol=symbol,
        exchange_ts=tick.exchange_timestamp or received,
        received_ts=received,
        ltp=tick.last_price,
        volume=tick.volume or 0,
        bid=tick.best_bid,
        ask=tick.best_ask,
        bid_qty=tick.bids[0].quantity if tick.bids else None,
        ask_qty=tick.asks[0].quantity if tick.asks else None,
    )


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


@dataclass
class CleaningPipeline:
    """Validate -> deduplicate -> outlier-filter -> normalise, in that order.

    The order is not arbitrary. Validation is cheapest and catches the most
    obviously broken input. Deduplication runs before the outlier filter so a
    replayed tick cannot update the last-price baseline the filter compares
    against — otherwise a duplicate would reset the reference and let a genuine
    outlier through immediately afterwards.
    """

    validator: TickValidator = field(default_factory=TickValidator)
    deduplicator: Deduplicator = field(default_factory=Deduplicator)
    outliers: OutlierFilter = field(default_factory=OutlierFilter)
    accepted: int = 0

    def process(self, tick: RawTick, symbol: str, *, now: dt.datetime | None = None) -> Tick | None:
        """A clean domain tick, or ``None`` with the reason already recorded."""
        reason = self.validator.check(tick, now=now)
        if reason is not None:
            return None
        if self.deduplicator.is_duplicate(tick):
            return None
        reason = self.outliers.check(tick)
        if reason is not None:
            return None
        self.accepted += 1
        return normalise(tick, symbol, received_ts=now)

    @property
    def rejection_summary(self) -> dict[str, int]:
        merged: dict[str, int] = {}
        sources = (
            self.validator.rejections,
            self.deduplicator.rejections,
            self.outliers.rejections,
        )
        for source in sources:
            for reason, count in source.counts.items():
                merged[reason] = merged.get(reason, 0) + count
        return merged

    def looks_unhealthy(self, *, threshold: float = 0.25) -> bool:
        """True when rejections dominate.

        A feed rejecting a quarter of what it sends is not a feed with some bad
        prints — it is a feed this system has misunderstood, and continuing to
        trade on the remainder is a worse choice than standing down.
        """
        rejected = sum(self.rejection_summary.values())
        seen = rejected + self.accepted
        return seen > 100 and rejected / seen > threshold
