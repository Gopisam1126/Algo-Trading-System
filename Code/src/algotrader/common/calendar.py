"""NSE session calendar, market-hours logic, and square-off deadlines.

This module encodes constraint C5 from LOW_LEVEL_ARCHITECTURE.md §1.1:

    Every intraday position must be closed on our own schedule, before the
    broker's per-stock auto square-off.

The subtlety that makes this non-trivial: since NSE's Closing Auction Session
went live on 2026-08-03, **the square-off deadline is per-stock, not global**.
F&O stocks in CAS scope square off at 15:10; everything else at 15:20; F&O
positions at 15:25.  A system with one global exit time will have positions
force-closed by the broker at whatever price happens to be there — a silent,
recurring source of slippage.

⚠️  The holiday list below MUST be verified against NSE's official circular
    before live trading.  It is a placeholder, not an authority.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")

# ---------------------------------------------------------------------------
# Session timings (IST) — see INDIA_FEATURES_AND_CONFIG.md §2.1
# ---------------------------------------------------------------------------

BLOCK_DEAL_MORNING_START = time(8, 45)
BLOCK_DEAL_MORNING_END = time(9, 0)

PRE_OPEN_START = time(9, 0)
PRE_OPEN_ORDER_END = time(9, 8)      # order collection closes
PRE_OPEN_MATCH_END = time(9, 12)     # matching completes
PRE_OPEN_END = time(9, 15)

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)

#: Continuous trading ends earlier for CAS-scope stocks (live 2026-08-03).
CAS_CONTINUOUS_END = time(15, 15)
CAS_END = time(15, 35)

#: Broker auto square-off times — per-stock.  We always exit BEFORE these.
SQUAREOFF_CAS_STOCKS = time(15, 10)
SQUAREOFF_NON_CAS = time(15, 20)
SQUAREOFF_FNO = time(15, 25)

#: Weekly market holidays (Sat/Sun).  0 = Monday.
WEEKEND = {5, 6}

# ---------------------------------------------------------------------------
# Holidays
# ---------------------------------------------------------------------------

#: ⚠️  PLACEHOLDER — verify against the official NSE holiday circular before
#: live trading.  Getting this wrong means the system tries to trade on a
#: closed exchange (harmless but noisy) or stands down on an open one
#: (costly).  Load the real list via ``load_holidays()``.
_FALLBACK_HOLIDAYS_2026: frozenset[date] = frozenset()


class MarketCalendar:
    """Session-aware calendar for NSE/BSE.

    All public methods take and return timezone-aware datetimes.  Internally
    everything converts to IST for market-hours logic, because exchange
    sessions are defined in local time, then back to UTC for storage.
    """

    def __init__(self, holidays: frozenset[date] | None = None) -> None:
        self._holidays = holidays if holidays is not None else _FALLBACK_HOLIDAYS_2026

    # -- Trading days -------------------------------------------------------

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() not in WEEKEND and d not in self._holidays

    def next_trading_day(self, d: date) -> date:
        nxt = d + timedelta(days=1)
        while not self.is_trading_day(nxt):
            nxt += timedelta(days=1)
        return nxt

    def previous_trading_day(self, d: date) -> date:
        prev = d - timedelta(days=1)
        while not self.is_trading_day(prev):
            prev -= timedelta(days=1)
        return prev

    def trading_days_between(self, start: date, end: date) -> list[date]:
        out: list[date] = []
        cur = start
        while cur <= end:
            if self.is_trading_day(cur):
                out.append(cur)
            cur += timedelta(days=1)
        return out

    # -- Session state ------------------------------------------------------

    def _ist(self, moment: datetime) -> datetime:
        if moment.tzinfo is None:
            raise ValueError("moment must be timezone-aware")
        return moment.astimezone(IST)

    def is_pre_open(self, moment: datetime) -> bool:
        ist = self._ist(moment)
        return self.is_trading_day(ist.date()) and PRE_OPEN_START <= ist.time() < PRE_OPEN_END

    def is_market_open(self, moment: datetime) -> bool:
        """True during continuous trading (09:15–15:30).

        Note this is the general session.  CAS-scope stocks stop continuous
        trading at 15:15 — use :meth:`is_continuous_for` for a per-stock answer.
        """
        ist = self._ist(moment)
        return self.is_trading_day(ist.date()) and MARKET_OPEN <= ist.time() < MARKET_CLOSE

    def is_continuous_for(self, moment: datetime, *, is_cas_stock: bool) -> bool:
        """Per-stock continuous-trading check."""
        ist = self._ist(moment)
        if not self.is_trading_day(ist.date()):
            return False
        end = CAS_CONTINUOUS_END if is_cas_stock else MARKET_CLOSE
        return MARKET_OPEN <= ist.time() < end

    # -- Square-off deadlines (constraint C5) -------------------------------

    def broker_squareoff_time(self, *, is_cas_stock: bool, is_fno: bool = False) -> time:
        """The time the BROKER will force-close an intraday position."""
        if is_fno:
            return SQUAREOFF_FNO
        return SQUAREOFF_CAS_STOCKS if is_cas_stock else SQUAREOFF_NON_CAS

    def squareoff_deadline(
        self,
        trade_date: date,
        *,
        is_cas_stock: bool,
        is_fno: bool = False,
        buffer_minutes: int = 5,
    ) -> datetime:
        """OUR deadline — the broker's, minus a safety buffer.

        Exiting on our own terms a few minutes early costs a little edge;
        being force-closed at market by the broker costs slippage on every
        position, every day, silently.  The buffer is cheap insurance.

        Returns a timezone-aware UTC datetime.
        """
        broker_time = self.broker_squareoff_time(is_cas_stock=is_cas_stock, is_fno=is_fno)
        deadline_ist = datetime.combine(trade_date, broker_time, tzinfo=IST)
        return (deadline_ist - timedelta(minutes=buffer_minutes)).astimezone(UTC)

    def minutes_to_squareoff(
        self,
        moment: datetime,
        *,
        is_cas_stock: bool,
        is_fno: bool = False,
        buffer_minutes: int = 5,
    ) -> float:
        ist = self._ist(moment)
        deadline = self.squareoff_deadline(
            ist.date(),
            is_cas_stock=is_cas_stock,
            is_fno=is_fno,
            buffer_minutes=buffer_minutes,
        )
        return (deadline - moment).total_seconds() / 60.0

    # -- Bar alignment ------------------------------------------------------

    def bar_open_time(self, moment: datetime, interval_seconds: int) -> datetime:
        """Align a moment to its bar's open time.

        Bars align to the SESSION START (09:15 IST), not to wall-clock hours.
        So a 15-minute bar runs 09:15–09:30, never 09:00–09:15.  Getting this
        wrong shifts every indicator by an offset that is hard to spot and
        makes backtests disagree with live.
        """
        ist = self._ist(moment)
        session_start = datetime.combine(ist.date(), MARKET_OPEN, tzinfo=IST)
        if ist < session_start:
            return session_start.astimezone(UTC)
        elapsed = int((ist - session_start).total_seconds())
        aligned = session_start + timedelta(seconds=(elapsed // interval_seconds) * interval_seconds)
        return aligned.astimezone(UTC)

    def session_bounds(self, d: date) -> tuple[datetime, datetime]:
        """(open, close) for a trading day, in UTC."""
        return (
            datetime.combine(d, MARKET_OPEN, tzinfo=IST).astimezone(UTC),
            datetime.combine(d, MARKET_CLOSE, tzinfo=IST).astimezone(UTC),
        )


def load_holidays(path: str | None = None) -> frozenset[date]:
    """Load the NSE holiday list from a YAML file.

    Expected shape::

        holidays:
          - 2026-01-26   # Republic Day
          - 2026-03-06   # Holi

    Falls back to the (empty) built-in list if no path is given — which means
    the system will treat every weekday as a trading day.  Supply the real
    list before live trading.
    """
    if path is None:
        return _FALLBACK_HOLIDAYS_2026

    import yaml

    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    entries = raw.get("holidays", [])
    parsed: set[date] = set()
    for entry in entries:
        parsed.add(entry if isinstance(entry, date) else date.fromisoformat(str(entry)))
    return frozenset(parsed)
