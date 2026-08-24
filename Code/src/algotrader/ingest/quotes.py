"""Quote state and tick fan-out (E05-S08).

Two consumers with genuinely different needs, so two mechanisms:

- **The current quote** goes to ``state:quote:{symbol}`` as a plain key. The
  risk engine asks "what is INFY worth right now" and wants one read, not a
  scan back through a stream. Last-write-wins is exactly right here.
- **The tick history** goes to ``stream:ticks`` for archival and replay, where
  order and completeness matter and overwriting would destroy the point.

Every quote carries a TTL. A session-scoped key with no expiry survives a crash
and is then read the next morning as though it were live — a stale price is
worse than a missing one, because a missing one is obviously missing.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

import redis.asyncio as aioredis
from pydantic import BaseModel, ConfigDict

from algotrader.common.events import Envelope, stream
from algotrader.common.models.market import Tick
from algotrader.common.redis import keys, state

log = logging.getLogger(__name__)

#: A quote must not outlive the session that produced it. Slightly longer than a
#: trading day so an end-of-session read still works, short enough that
#: yesterday's price can never be mistaken for today's.
QUOTE_TTL_SECONDS = 8 * 60 * 60

#: Spread beyond this is a liquidity warning rather than a tradable market.
WIDE_SPREAD_PCT = Decimal("1.0")


class QuoteState(BaseModel):
    """What the risk engine reads before sizing or exiting.

    Carries ``as_of`` rather than relying on the key's TTL: a consumer needs to
    know HOW stale, not merely that the key still exists. Fail-closed decisions
    depend on the difference.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    ltp: Decimal
    bid: Decimal | None = None
    ask: Decimal | None = None
    bid_qty: int | None = None
    ask_qty: int | None = None
    volume: int = 0
    as_of: dt.datetime

    @property
    def spread_pct(self) -> Decimal | None:
        if self.bid is None or self.ask is None or self.bid <= 0:
            return None
        return (self.ask - self.bid) / self.bid * Decimal(100)

    @property
    def is_wide(self) -> bool:
        """True when the spread makes the quote expensive to cross.

        Not the same as illiquid, and not a hard exclusion — it is an input to
        the entry decision, because a wide spread turns a good signal into a
        losing trade through cost alone.
        """
        spread = self.spread_pct
        return spread is not None and spread > WIDE_SPREAD_PCT

    def age_seconds(self, *, now: dt.datetime | None = None) -> float:
        return ((now or dt.datetime.now(dt.UTC)) - self.as_of).total_seconds()

    @classmethod
    def from_tick(cls, tick: Tick) -> QuoteState:
        return cls(
            symbol=tick.symbol,
            ltp=tick.ltp,
            bid=tick.bid,
            ask=tick.ask,
            bid_qty=tick.bid_qty,
            ask_qty=tick.ask_qty,
            volume=tick.volume,
            as_of=tick.exchange_ts,
        )


@dataclass
class QuotePublisher:
    """Writes quote state and fans ticks out to the archival stream."""

    client: aioredis.Redis
    ttl_seconds: int = QUOTE_TTL_SECONDS
    #: Publishing every tick to the stream is a lot of writes for little value —
    #: the archive is for replay, and replay does not need microstructure. Off
    #: by default; E03-S06 turns it on when tick archival is wanted.
    archive_ticks: bool = False
    published: int = 0
    archived: int = 0
    wide_spreads: dict[str, int] = field(default_factory=dict)

    async def publish(self, tick: Tick) -> QuoteState:
        """Write the current quote, and optionally archive the tick."""
        quote = QuoteState.from_tick(tick)
        await state.set_state(
            self.client, keys.quote(tick.symbol), quote, ttl_seconds=self.ttl_seconds
        )
        self.published += 1

        if quote.is_wide:
            self.wide_spreads[tick.symbol] = self.wide_spreads.get(tick.symbol, 0) + 1

        if self.archive_ticks:
            await stream.publish(
                self.client,
                keys.stream_ticks(),
                Envelope(
                    correlation_id=uuid.uuid4(),
                    emitted_at=dt.datetime.now(dt.UTC),
                    emitted_by="market-ingest",
                    payload=json.loads(tick.model_dump_json()),
                ),
                maxlen=stream.default_maxlen(keys.stream_ticks()),
            )
            self.archived += 1
        return quote

    async def read(self, symbol: str) -> QuoteState | None:
        """The current quote, or ``None`` when there is none.

        ``None`` is a real answer — it means no tick has arrived for this symbol
        this session, which is a reason to stand down rather than to guess.
        """
        return await state.get_state(self.client, keys.quote(symbol), QuoteState)

    async def read_fresh(
        self, symbol: str, *, max_age_seconds: float, now: dt.datetime | None = None
    ) -> QuoteState | None:
        """The quote, but only if recent enough to act on.

        Separate from :meth:`read` so staleness is a decision the caller makes
        explicitly. A single ``read`` that silently returned stale data is how a
        position gets sized against a price from twenty minutes ago.
        """
        quote = await self.read(symbol)
        if quote is None:
            return None
        age = quote.age_seconds(now=now)
        if age > max_age_seconds:
            log.warning(
                "quote for %s is %.1fs old (limit %.1fs) — treating as absent",
                symbol,
                age,
                max_age_seconds,
            )
            return None
        return quote
