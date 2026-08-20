"""Read-only Kite adapter (E02-S03).

Handed to every service that needs prices and to none that may trade. The
:class:`ReadOnlyGuard` mixin makes the trading methods raise, so a wiring
mistake fails loudly at the first attempt instead of quietly succeeding.

Two things worth knowing about the read path:

**It does not need the static IP.** Zerodha validates the whitelist on order
endpoints only, so quotes, instruments and historical candles work from
anywhere. That is why all of E03, E04, E05 and E06 can be built and tested
before the VPS exists.

**Live ticks are not here.** ``subscribe`` deliberately raises. The Kite
WebSocket is E05's story, and it will be built directly on the documented wire
protocol rather than on ``KiteTicker`` — the SDK's ticker drags in
``autobahn==19.11.2`` (CVE-2020-35678) and a Twisted reactor that has to be
bridged into asyncio. Implementing the protocol avoids both. Raising here keeps
that decision visible instead of letting someone wire up the vulnerable path by
reflex.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

from algotrader.broker.adapter import AuthenticationError, BrokerSession, ReadOnlyGuard
from algotrader.broker.kite import mapping
from algotrader.broker.kite.auth import KiteAuthManager
from algotrader.broker.kite.errors import classify
from algotrader.broker.ratelimit import BrokerRateLimiter
from algotrader.common.enums import Exchange, Timeframe
from algotrader.common.models.market import Bar, Instrument, Tick

log = logging.getLogger(__name__)

#: Kite's interval vocabulary, keyed by ours.
_INTERVAL: dict[Timeframe, str] = {
    Timeframe.M1: "minute",
    Timeframe.M5: "5minute",
    Timeframe.M15: "15minute",
    Timeframe.H1: "60minute",
    Timeframe.D1: "day",
}


class KiteReads:
    """The read half of the Kite REST API, with no opinion about trading.

    Split out from :class:`KiteMarketDataAdapter` so the trading adapter can
    reuse the reads WITHOUT inheriting ``ReadOnlyGuard``. Having the trading
    adapter inherit a guard and then override it method-by-method inverts the
    relationship: it makes 'can trade' the exception to a read-only rule,
    which is one forgotten override away from a trading adapter that cannot
    place an order.
    Every SDK call is pushed to a worker thread: the client is synchronous, and
    a blocking call inline would stall the event loop for a full network round
    trip — which during the pre-market warm-up stalls every other symbol behind
    it.
    """

    def __init__(
        self,
        *,
        auth: KiteAuthManager,
        client: Any,
        limiter: BrokerRateLimiter | None = None,
    ) -> None:
        self._auth = auth
        self._client = client
        self._limiter = limiter

    # -- session -------------------------------------------------------------

    async def authenticate(self) -> BrokerSession:
        """Return the live session, or refuse.

        This adapter never performs the login itself — the redirect flow needs a
        human. It only reports whether a usable session already exists.
        """
        session = self._auth.session
        if session is None or not self._auth.is_valid():
            raise AuthenticationError(
                "no valid broker session; complete the redirect login from the "
                "dashboard before using this adapter"
            )
        return session

    async def is_session_valid(self) -> bool:
        return self._auth.is_valid()

    # -- reads ---------------------------------------------------------------

    async def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a blocking SDK call off the loop, mapping failures to the taxonomy.

        ``mutating=False`` throughout: nothing in this class changes state at
        the broker, so an unknown failure here is a plain failure rather than
        an ambiguous one.
        """
        if self._limiter is not None:
            await self._limiter.acquire_data()
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except Exception as exc:
            raise classify(exc, mutating=False) from None

    async def fetch_instruments(self, exchange: str = "NSE") -> list[Instrument]:
        """The daily instrument dump.

        Note what this does NOT contain: any surveillance flag. Kite's dump
        carries token, symbol, tick size, lot size and segment — there is no
        series, ASM, GSM, T2T or ban field. Those come from E04's NSE fetchers,
        which is why E04 cannot be satisfied from the broker.
        """
        raw = await self._call(self._client.instruments, exchange)
        out: list[Instrument] = []
        for row in raw:
            try:
                out.append(
                    Instrument(
                        symbol=str(row["tradingsymbol"]),
                        exchange=mapping.exchange_in(row.get("exchange", exchange)),
                        broker_token=str(row["instrument_token"]),
                        lot_size=int(row.get("lot_size") or 1),
                        tick_size=Decimal(str(row.get("tick_size") or "0.05")),
                    )
                )
            except (KeyError, ValueError, mapping.MappingError) as exc:
                # One malformed row must not lose the other few thousand, but
                # it must not vanish either — a rising count here means the
                # dump's shape has moved.
                log.warning(
                    "skipping unmappable instrument row %s: %s",
                    row.get("tradingsymbol"),
                    exc,
                )
        log.info("fetched %d instruments from %s", len(out), exchange)
        return out

    async def fetch_historical(
        self,
        token: str,
        timeframe: Timeframe,
        start: dt.datetime,
        end: dt.datetime,
    ) -> list[Bar]:
        """Historical candles.

        Included in the ₹500/month Connect plan — there is no longer a separate
        historical add-on, so E03-S03's cost blocker (B4) is resolved.
        """
        interval = _INTERVAL.get(timeframe)
        if interval is None:
            raise mapping.MappingError(
                f"Kite has no historical interval for {timeframe!r}; "
                f"aggregate it from a finer one instead"
            )
        if start >= end:
            raise ValueError(f"start {start.isoformat()} is not before end {end.isoformat()}")

        raw = await self._call(self._client.historical_data, int(token), start, end, interval)
        bars: list[Bar] = []
        for row in raw:
            bars.append(
                Bar(
                    symbol=str(token),
                    timeframe=timeframe,
                    open_ts=mapping.parse_broker_timestamp(row["date"]),
                    open=Decimal(str(row["open"])),
                    high=Decimal(str(row["high"])),
                    low=Decimal(str(row["low"])),
                    close=Decimal(str(row["close"])),
                    volume=int(row.get("volume") or 0),
                )
            )
        return bars

    async def fetch_quote(self, symbols: list[str]) -> dict[str, Decimal]:
        """Last traded price per ``EXCHANGE:SYMBOL``. Used for staleness checks."""
        if not symbols:
            return {}
        raw = await self._call(self._client.ltp, symbols)
        return {k: Decimal(str(v["last_price"])) for k, v in raw.items()}

    def subscribe(self, tokens: list[str]) -> AsyncIterator[Tick]:
        """Not implemented here — deliberately. See the module docstring.

        A plain method rather than an async generator: a generator body would
        not run until first iteration, so the refusal would surface somewhere
        far from the call that caused it.
        """
        raise NotImplementedError(
            "live tick streaming is E05-S01 and will be built on the documented "
            "Kite WebSocket protocol, not on KiteTicker. KiteTicker pulls in "
            "autobahn 19.11.2 (CVE-2020-35678) and a Twisted reactor; the wire "
            "protocol is fully specified, so neither is necessary."
        )


def instrument_exchange(symbol: str, exchange: Exchange = Exchange.NSE) -> str:
    """Kite addresses instruments as ``NSE:INFY`` in quote calls."""
    return f"{exchange.value}:{symbol}"


class KiteMarketDataAdapter(KiteReads, ReadOnlyGuard):
    """Reads plus a hard refusal on every write.

    This is what every service except ``execution-svc`` receives. The guard is
    last in the MRO on purpose — its ``place_order`` / ``modify_order`` /
    ``cancel_order`` are the only implementations here, so there is nothing to
    accidentally shadow them.
    """
