"""Quote state and publishing (E05-S08).

This module was at 0% coverage — 68 statements, never executed by any test.

Most of it is arithmetic on a frozen model, which is exactly the kind of code
that reads correctly and is wrong: ``spread_pct`` divides by the bid, and a
zero or missing bid is normal on an illiquid open. The interesting behaviour is
:meth:`QuotePublisher.read_fresh`, which exists so that "the quote is too old
to act on" is a decision a caller makes deliberately rather than something a
plain read hides.

Redis is faked with a dictionary rather than mocked call-by-call, so the tests
assert on what was STORED and read back, not on which client methods happened
to be invoked.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from pydantic import ValidationError

from algotrader.common.models.market import Tick
from algotrader.common.redis import keys
from algotrader.ingest.quotes import (
    QUOTE_TTL_SECONDS,
    WIDE_SPREAD_PCT,
    QuotePublisher,
    QuoteState,
)

NOW = dt.datetime(2026, 8, 20, 5, 0, tzinfo=dt.UTC)


def _tick(
    *,
    ltp: str = "2500.00",
    bid: str | None = "2499.50",
    ask: str | None = "2500.50",
    symbol: str = "INFY",
    ts: dt.datetime = NOW,
    volume: int = 12_000,
) -> Tick:
    return Tick(
        symbol=symbol,
        exchange_ts=ts,
        received_ts=ts,
        ltp=Decimal(ltp),
        volume=volume,
        bid=None if bid is None else Decimal(bid),
        ask=None if ask is None else Decimal(ask),
        bid_qty=100,
        ask_qty=150,
    )


class FakeRedis:
    """A dict with the handful of methods the quote path actually uses."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.streams: dict[str, list[dict]] = {}

    async def set(self, key: str, value: str, ex: int | None = None, **kw: object) -> bool:
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def xadd(self, key: str, fields: dict, maxlen: int | None = None, **kw: object) -> bytes:
        self.streams.setdefault(key, []).append(fields)
        return b"1-1"


class TestSpreadArithmetic:
    def test_a_normal_spread_is_computed(self) -> None:
        quote = QuoteState.from_tick(_tick(bid="100.00", ask="100.50"))
        assert quote.spread_pct == Decimal("0.5")

    def test_a_missing_side_has_no_spread(self) -> None:
        """LTP-mode ticks carry no depth. A missing spread must be None, not
        zero — zero would read as a perfectly tight market."""
        assert QuoteState.from_tick(_tick(bid=None)).spread_pct is None
        assert QuoteState.from_tick(_tick(ask=None)).spread_pct is None

    def test_a_zero_bid_does_not_divide_by_zero(self) -> None:
        """Normal on an illiquid open, and a crash here would take down the
        whole publish path for every symbol."""
        assert QuoteState.from_tick(_tick(bid="0")).spread_pct is None

    def test_a_wide_spread_is_flagged(self) -> None:
        wide = QuoteState.from_tick(_tick(bid="100.00", ask="102.00"))
        assert wide.is_wide

    def test_a_tight_spread_is_not(self) -> None:
        tight = QuoteState.from_tick(_tick(bid="100.00", ask="100.10"))
        assert not tight.is_wide

    def test_the_boundary_is_exclusive(self) -> None:
        """Exactly at the threshold is not 'wide' — otherwise the constant
        reads as a limit and behaves as one-below-the-limit."""
        at = QuoteState.from_tick(_tick(bid="100.00", ask="101.00"))
        assert at.spread_pct == WIDE_SPREAD_PCT
        assert not at.is_wide

    def test_an_unknown_spread_is_not_wide(self) -> None:
        """Absence must not be treated as a liquidity warning; it is a reason
        to look elsewhere, and is_wide is consumed as a soft signal."""
        assert not QuoteState.from_tick(_tick(bid=None)).is_wide


class TestAgeIsCarriedNotInferred:
    def test_age_is_measured_from_the_exchange_timestamp(self) -> None:
        """From the EXCHANGE clock, not ours. Measuring from receipt would hide
        a feed that is running minutes behind."""
        quote = QuoteState.from_tick(_tick(ts=NOW - dt.timedelta(seconds=45)))
        assert quote.age_seconds(now=NOW) == pytest.approx(45.0)

    def test_a_fresh_quote_has_near_zero_age(self) -> None:
        assert QuoteState.from_tick(_tick()).age_seconds(now=NOW) == pytest.approx(0.0)

    def test_the_model_is_frozen(self) -> None:
        """A quote that could be edited after publication would let one
        consumer change what another reads."""
        quote = QuoteState.from_tick(_tick())
        with pytest.raises(ValidationError):
            quote.ltp = Decimal("1")  # type: ignore[misc]


class TestPublishing:
    @pytest.mark.asyncio
    async def test_a_quote_is_stored_under_its_symbol_key(self) -> None:
        client = FakeRedis()
        publisher = QuotePublisher(client=client)  # type: ignore[arg-type]
        await publisher.publish(_tick())
        assert keys.quote("INFY") in client.store

    @pytest.mark.asyncio
    async def test_the_key_carries_a_ttl(self) -> None:
        """Without one, a symbol that stops ticking keeps serving its last
        quote forever."""
        client = FakeRedis()
        publisher = QuotePublisher(client=client)  # type: ignore[arg-type]
        await publisher.publish(_tick())
        assert client.ttls[keys.quote("INFY")] == QUOTE_TTL_SECONDS

    @pytest.mark.asyncio
    async def test_publishing_round_trips(self) -> None:
        client = FakeRedis()
        publisher = QuotePublisher(client=client)  # type: ignore[arg-type]
        await publisher.publish(_tick(ltp="2500.00"))
        back = await publisher.read("INFY")
        assert back is not None and back.ltp == Decimal("2500.00")

    @pytest.mark.asyncio
    async def test_an_absent_quote_reads_as_none(self) -> None:
        """None is a real answer: no tick this session, which is a reason to
        stand down rather than to guess."""
        publisher = QuotePublisher(client=FakeRedis())  # type: ignore[arg-type]
        assert await publisher.read("NOSUCH") is None

    @pytest.mark.asyncio
    async def test_wide_spreads_are_counted_per_symbol(self) -> None:
        client = FakeRedis()
        publisher = QuotePublisher(client=client)  # type: ignore[arg-type]
        for _ in range(3):
            await publisher.publish(_tick(bid="100.00", ask="105.00"))
        await publisher.publish(_tick(bid="100.00", ask="100.05"))
        assert publisher.wide_spreads["INFY"] == 3

    @pytest.mark.asyncio
    async def test_ticks_are_not_archived_by_default(self) -> None:
        """Every tick to the stream is a lot of writes for little value; replay
        does not need microstructure."""
        client = FakeRedis()
        publisher = QuotePublisher(client=client)  # type: ignore[arg-type]
        await publisher.publish(_tick())
        assert publisher.archived == 0
        assert not client.streams

    @pytest.mark.asyncio
    async def test_archiving_can_be_switched_on(self) -> None:
        client = FakeRedis()
        publisher = QuotePublisher(client=client, archive_ticks=True)  # type: ignore[arg-type]
        await publisher.publish(_tick())
        assert publisher.archived == 1
        assert client.streams

    @pytest.mark.asyncio
    async def test_the_published_count_tracks_every_write(self) -> None:
        client = FakeRedis()
        publisher = QuotePublisher(client=client)  # type: ignore[arg-type]
        for _ in range(5):
            await publisher.publish(_tick())
        assert publisher.published == 5


class TestReadFreshIsSeparateOnPurpose:
    """A single ``read`` that silently returned stale data is how a position
    gets sized against a price from twenty minutes ago."""

    async def _seeded(self, age_seconds: float) -> QuotePublisher:
        client = FakeRedis()
        publisher = QuotePublisher(client=client)  # type: ignore[arg-type]
        await publisher.publish(_tick(ts=NOW - dt.timedelta(seconds=age_seconds)))
        return publisher

    @pytest.mark.asyncio
    async def test_a_recent_quote_is_returned(self) -> None:
        publisher = await self._seeded(2)
        assert await publisher.read_fresh("INFY", max_age_seconds=10, now=NOW) is not None

    @pytest.mark.asyncio
    async def test_a_stale_quote_is_treated_as_absent(self) -> None:
        publisher = await self._seeded(600)
        assert await publisher.read_fresh("INFY", max_age_seconds=10, now=NOW) is None

    @pytest.mark.asyncio
    async def test_plain_read_still_returns_the_stale_one(self) -> None:
        """The two methods must genuinely differ, or the separation is
        decorative and callers will use whichever they reach for."""
        publisher = await self._seeded(600)
        assert await publisher.read("INFY") is not None

    @pytest.mark.asyncio
    async def test_staleness_is_logged_so_it_is_diagnosable(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        publisher = await self._seeded(600)
        with caplog.at_level("WARNING"):
            await publisher.read_fresh("INFY", max_age_seconds=10, now=NOW)
        assert "INFY" in caplog.text

    @pytest.mark.asyncio
    async def test_an_absent_quote_is_none_rather_than_an_error(self) -> None:
        publisher = QuotePublisher(client=FakeRedis())  # type: ignore[arg-type]
        assert await publisher.read_fresh("NOSUCH", max_age_seconds=10, now=NOW) is None

    @pytest.mark.asyncio
    async def test_the_boundary_age_is_accepted(self) -> None:
        """Exactly at the limit is fresh; the check is 'older than', not
        'as old as'."""
        publisher = await self._seeded(10)
        assert await publisher.read_fresh("INFY", max_age_seconds=10, now=NOW) is not None
