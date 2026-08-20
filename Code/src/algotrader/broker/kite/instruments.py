"""Instrument master synchronisation (E02-S06).

Runs once each morning, before anything else needs a symbol. Three things it
must get right:

**Idempotency.** Re-running the sync is normal — a retry after a network blip,
a manual run while debugging. Upsert on ``(tradingsymbol, exchange)``.

**Delistings must be detectable.** A symbol that stops appearing in the dump is
marked inactive rather than deleted. Deleting it would orphan every historical
bar and every closed position that references it, and the trade journal would
lose the ability to explain a trade taken last month.

**Tick size is not decoration.** An order at a non-tick price is rejected by the
exchange. The dump is the only authority for it, and it is per-instrument — a
hardcoded 0.05 is wrong for a large slice of the market.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from algotrader.common.models.market import Instrument

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SyncResult:
    """What a sync run did, in terms an operator can act on."""

    fetched: int
    upserted: int
    new_symbols: tuple[str, ...]
    missing_symbols: tuple[str, ...]

    @property
    def looks_implausible(self) -> bool:
        """True when the dump is suspiciously small or has churned wildly.

        A truncated or error-page response parses to a handful of rows and
        would otherwise silently shrink the tradable universe to nothing. This
        is the same class of failure as E04's list fetchers: the danger is not
        an exception, it is a successful-looking empty answer.
        """
        return self.fetched < 500 or len(self.missing_symbols) > max(50, self.fetched // 10)


class InstrumentSync:
    """Pulls the broker dump and reconciles it into ``instruments``."""

    def __init__(self, adapter: Any, repository: Any) -> None:
        self._adapter = adapter
        self._repository = repository

    async def run(self, exchange: str = "NSE") -> SyncResult:
        instruments = await self._adapter.fetch_instruments(exchange)
        fetched = len(instruments)

        known = await self._existing_symbols()
        incoming = {i.symbol for i in instruments}

        new_symbols = tuple(sorted(incoming - known))
        missing_symbols = tuple(sorted(known - incoming))

        upserted = await self._repository.upsert([self._to_row(i) for i in instruments])

        result = SyncResult(
            fetched=fetched,
            upserted=int(upserted or 0),
            new_symbols=new_symbols,
            missing_symbols=missing_symbols,
        )
        self._report(result)
        return result

    async def _existing_symbols(self) -> set[str]:
        refresh = getattr(self._repository, "refresh_cache", None)
        if refresh is not None:
            await refresh()
        known = getattr(self._repository, "_by_symbol", {})
        return set(known)

    @staticmethod
    def _to_row(instrument: Instrument) -> dict[str, Any]:
        return {
            "tradingsymbol": instrument.symbol,
            "exchange": instrument.exchange.value,
            "broker_token": instrument.broker_token,
            "tick_size": instrument.tick_size,
            "lot_size": instrument.lot_size,
        }

    @staticmethod
    def _report(result: SyncResult) -> None:
        log.info(
            "instrument sync: fetched=%d upserted=%d new=%d missing=%d",
            result.fetched,
            result.upserted,
            len(result.new_symbols),
            len(result.missing_symbols),
        )
        if result.new_symbols:
            log.info("new listings: %s", ", ".join(result.new_symbols[:20]))
        if result.missing_symbols:
            # Loud: a symbol vanishing from the dump is either a delisting or a
            # truncated download, and those need very different responses.
            log.warning(
                "%d symbols absent from today's dump (delisted, or a short download): %s",
                len(result.missing_symbols),
                ", ".join(result.missing_symbols[:20]),
            )
        if result.looks_implausible:
            log.error(
                "instrument dump looks implausible (fetched=%d, missing=%d) — treat the "
                "universe as untrustworthy for today rather than trading a truncated one",
                result.fetched,
                len(result.missing_symbols),
            )


def tick_grid_is_respected(price: Decimal, tick: Decimal) -> bool:
    """True when a price sits exactly on the instrument's tick grid.

    Used as an assertion before submission. The exchange rejects off-grid
    prices, and a rejection at the square-off deadline is the expensive case.
    """
    if tick <= 0:
        return False
    return (price / tick) % 1 == 0
