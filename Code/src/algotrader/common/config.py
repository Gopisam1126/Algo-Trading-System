"""Configuration loading and validation.

Three gates stand between a YAML file and a running system
(LOW_LEVEL_ARCHITECTURE.md §10.11, MVP_UI_AND_LEGAL.md §9.4):

1. **Type / range validation** — Pydantic field constraints.
2. **Cross-field validation** — weights sum to 1.0, slots × slot-capital ≤
   capital, interval floor ≤ ceiling.
3. **HARD BOUNDS** — absolute safety limits defined *in code*, which reject
   dangerous values regardless of what the file says.

Gate 3 is the important one and it is the reason these constants live here
rather than in YAML:

    Configuration can tune the system.  It can never disable safety.

A compromised or mistaken config file cannot raise the order rate above the
SEBI-safe cap, set a 50% per-trade risk, or turn off the human approval gate
on strategy promotion.
"""

from __future__ import annotations

import ipaddress
import os
from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from algotrader.common.enums import AutonomyLevel, SystemMode, Timeframe

# ---------------------------------------------------------------------------
# HARD BOUNDS — config may not exceed these.  Changing them is a code change
# that goes through review, which is exactly the point.
# ---------------------------------------------------------------------------

#: The algo-registration threshold, in orders per second (per exchange and
#: segment). At or above it a self-developed algo must be registered with the
#: exchange through the broker and carries a registered Algo-ID; below it the
#: order is tagged with a GENERIC algo identifier.
#:
#: **Where the number comes from (corrected 16 Nov 2026, audit HAL-002).** The
#: SEBI circular (SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/0000013, 4 Feb 2025) sets
#: no number: its clause I(c) speaks of "the specified order per second
#: threshold" and a footnote leaves the figure to the Brokers' Industry
#: Standards Forum. The 10 is the exchanges' implementation standard - NSE
#: circular NSE/INVG/67858 (5 May 2025) and its follow-ons (NSE/INVG/69255, 22
#: Jul 2025) - read from broker and secondary summaries; the NSE text itself was
#: not reachable from this host (blocker B8). The exchanges may revise it after
#: notice, and a broker may set a lower client limit, so it is re-checked once
#: a year with the holiday file (PRE_LIVE_CHECKLIST.md).
SEBI_ALGO_REGISTRATION_OPS = 10

MAX_ORDERS_PER_SECOND = 5  # SEBI threshold is 10; we cap at half
MAX_RISK_PCT_PER_TRADE = Decimal("10.0")
MAX_POSITION_SLOTS = 20
MAX_DAILY_LOSS_PCT = Decimal("25.0")
#: The largest share of capital ONE position may take. Applied to
#: ``risk.per_trade.max_position_pct`` (it was defined and applied to nothing
#: until the 10 Nov 2026 audit, RSK-009).
MAX_POSITION_PCT = Decimal("100.0")
MIN_STRATEGY_TRIALS = 50  # below this, statistics are meaningless
MAX_PBO_ALLOWED = Decimal("0.6")  # Probability of Backtest Overfitting
MAX_ACTIVE_STRATEGIES = 12
MIN_EXIT_BUFFER_MINUTES = 1
#: Audit RSK-009: each of the next four was bounded on one side only, so a
#: config could switch the control it feeds off by making it unreachable.
#: ``consecutive_loss_halt: 1000`` is a streak halt that never trips;
#: ``exit_buffer_minutes: 300`` puts the time exit before the session opens;
#: ``min_minutes_to_squareoff: 1`` makes the runway check pass at 15:14.
MAX_CONSECUTIVE_LOSS_HALT = 5
MAX_EXIT_BUFFER_MINUTES = 30
MIN_ENTRY_RUNWAY_MINUTES = 15
MAX_ENTRY_RUNWAY_MINUTES = 180
#: The widest trigger-to-live-price gap the sizer will size on (RSK-002).
MAX_PRICE_DIVERGENCE_PCT = Decimal("5.0")
#: The windows no configuration may remove (RSK-009). Config may ADD windows;
#: each of these must be covered by one of the configured windows, so an
#: empty or partial ``no_trade_windows`` list is refused rather than read as
#: "no blackout".
MANDATORY_NO_TRADE_WINDOWS: tuple[tuple[time, time], ...] = (
    (time(9, 15), time(9, 20)),  # the opening auction's aftermath
    (time(15, 0), time(15, 30)),  # no new entries into the close
)
#: What ``ALGOTRADER_ENV`` must be for ``system.mode: live`` to load (RSK-008).
LIVE_ENVIRONMENT = "production"
#: The environment variable that must carry the loaded file's
#: :meth:`AppConfig.config_hash` before live mode loads (RSK-008). A second,
#: out-of-band confirmation that THIS file - not one an edit or an
#: ``ALGOTRADER_CONFIG`` redirect produced - is the one meant to trade.
CONFIRM_LIVE_VARIABLE = "ALGOTRADER_CONFIRM_LIVE"
#: The oldest a Recommendation may be when the execution service acts on it
#: (E15-S13). The entry is a MARKET order sized from the trigger price, so a
#: signal acted on late is sized for a price that has moved - and the stop
#: distance is measured from a price the fill never saw.
MAX_SIGNAL_AGE_SECONDS = 300


def _assert_below_registration_threshold(cap: int, threshold: int) -> None:
    """Refuse to load if the hard cap has been raised into the registration regime.

    Split out of the module body so the condition and its message are
    reachable by a test. Left inline, the guard only executes when someone
    edits the constants above — which meant a typo in it (``<=`` for ``>=``,
    or the operands swapped) would be discovered on the one day it was needed
    and never before. A mutation that deleted the guard entirely survived the
    suite, which is what surfaced this.
    """
    if cap >= threshold:
        raise RuntimeError(
            f"MAX_ORDERS_PER_SECOND is {cap}, at or above SEBI's "
            f"algo-registration threshold of {threshold}/sec. That is not a "
            f"rate change, it is a change of regulatory regime: the algo must "
            f"be registered with the exchange through the broker and every "
            f"order must then carry the registered Algo-ID. Raising this "
            f"constant requires that registration to exist first."
        )


_assert_below_registration_threshold(MAX_ORDERS_PER_SECOND, SEBI_ALGO_REGISTRATION_OPS)

Pct = Annotated[Decimal, Field(ge=0, le=100)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------


class SystemConfig(_Model):
    mode: SystemMode = SystemMode.PAPER
    #: The market's timezone. Fixed (audit ENG-003): every market-hours
    #: computation uses ``common.calendar.IST``, so another value here would
    #: change nothing while reading as a setting.
    timezone: Literal["Asia/Kolkata"] = "Asia/Kolkata"
    deployment_region: str = "ap-south-1"
    static_ip: str = ""
    log_level: str = "INFO"

    @field_validator("deployment_region")
    @classmethod
    def _must_be_india(cls, v: str) -> str:
        """Host in India - a SELF-IMPOSED control, not a cited SEBI rule.

        Corrected 16 Nov 2026 (audit HAL-001). This said "SEBI requires algos to
        be hosted on Indian servers", and neither the SEBI circular nor the
        exchange standards, as far as they have been read, say that. The audit
        reports a different and potentially material standard - that retail
        algos "should be hosted on brokers' cloud servers" - whose application
        to a client's own sub-threshold algo is an open question for Zerodha
        and counsel (PRE_LIVE_CHECKLIST.md, owner action). The India-region
        check stays because it costs nothing and keeps data and latency local;
        it is not evidence of compliance with any hosting rule.
        """
        india_regions = {
            "ap-south-1",
            "ap-south-2",
            "centralindia",
            "southindia",
            "asia-south1",
            "asia-south2",
            "in-mumbai",
            "in-delhi",
        }
        if v.lower() not in india_regions:
            raise ValueError(
                f"deployment_region {v!r} is not an India region. This system "
                f"runs in India by its own rule (see the docstring for what is and "
                f"is not a regulatory requirement). Allowed: {sorted(india_regions)}"
            )
        return v


class DatabaseConfig(_Model):
    """PostgreSQL / TimescaleDB connection *structure*.

    There is deliberately **no password field here.**  This object is loaded
    from ``config/system.yaml``, which is version controlled — a password in it
    would be a committed credential.  The password comes from the secrets
    provider (``POSTGRES_PASSWORD``) and is joined to these fields at engine
    construction time by :meth:`dsn`.

    One credential, one source.  See :meth:`dsn` for the override rule.

    .. note::
       The pool and timeout fields below are **declared here but not yet
       applied** — nothing constructs an engine until E01-S02 builds the
       session factory, which is where they get consumed.  ``make doctor``
       reports them as *configured*, not as *enforced*.  Do not read a green
       doctor line as proof that a statement timeout is active.
    """

    host: str = "localhost"
    port: int = Field(default=5432, ge=1, le=65535)
    name: str = "algotrader"
    user: str = "algotrader"

    # Pool sizing — LOW_LEVEL_ARCHITECTURE.md §6.3.  Ten connections covers the
    # nine services plus migrations; overflow absorbs the pre-market burst.
    pool_size: int = Field(default=10, ge=1, le=50)
    max_overflow: int = Field(default=5, ge=0, le=50)
    pool_pre_ping: bool = True
    pool_recycle_seconds: int = Field(default=1800, ge=60)

    # A query that runs longer than this is a bug, not a slow query.  The
    # pre-market warm-up (BP-2) is the longest legitimate read and is measured
    # in seconds, not minutes.
    statement_timeout_ms: int = Field(default=30_000, ge=1_000, le=300_000)

    echo_sql: bool = False

    @field_validator("echo_sql")
    @classmethod
    def _echo_is_dev_only(cls, v: bool) -> bool:
        """SQL echo prints bound parameters, which include order values.

        Harmless locally, but it defeats the log redactor if it is ever left on
        in a deployed environment, so it is refused outside development - and
        development must be SAID. An unset ``ALGOTRADER_ENV`` used to count as
        development, so a deployment that forgot the variable could echo
        (audit NR-005/NR-010, 17 Nov 2026).
        """
        import os

        if v and os.environ.get("ALGOTRADER_ENV") != "development":
            raise ValueError(
                "database.echo_sql may only be true when ALGOTRADER_ENV=development is "
                "set explicitly; echoed SQL contains bound parameters and bypasses log "
                "redaction"
            )
        return v

    def dsn(self, password: str | None = None) -> str:
        """Build the SQLAlchemy URL.

        **Precedence, and it is explicit rather than silent:**

        1. If ``DATABASE_URL`` is set in the environment it wins *entirely* and
           every field on this object is ignored.  This is how the containers
           reach ``timescaledb`` instead of ``localhost``, and how tests point
           at an ephemeral testcontainer.
        2. Otherwise the URL is assembled from these fields plus ``password``.

        The old failure mode this replaces: ``.env`` carried a full
        ``DATABASE_URL`` *and* a ``POSTGRES_PASSWORD``, so the credential had two
        sources that silently disagreed the moment either changed.  Now there is
        one source, and an override that announces itself — ``make doctor``
        reports which of the two is in effect.

        Returns a plain ``str`` because SQLAlchemy requires one.  Treat the
        result as a secret: never log it.  Use :meth:`safe_dsn` for display.
        """
        import os
        from urllib.parse import quote

        override = os.environ.get("DATABASE_URL")
        if override:
            return override

        if password is None:
            raise ValueError(
                "no database password supplied and DATABASE_URL is not set. "
                "Set POSTGRES_PASSWORD in .env (see .env.example), or set "
                "DATABASE_URL to override the connection entirely."
            )
        # The password is percent-encoded: an unescaped '@' or '/' in a
        # generated password otherwise silently truncates the host.
        return (
            f"postgresql+psycopg://{quote(self.user)}:{quote(password, safe='')}"
            f"@{self.host}:{self.port}/{self.name}"
        )

    def safe_dsn(self) -> str:
        """The connection target with no credential in it — safe to log."""
        import os

        if os.environ.get("DATABASE_URL"):
            return "<from DATABASE_URL override>"
        return f"postgresql+psycopg://{self.user}@{self.host}:{self.port}/{self.name}"


class RedisConfig(_Model):
    """Redis connection structure.  Password handled as in :class:`DatabaseConfig`.

    ``maxmemory``/``maxmemory-policy`` are deliberately **not** here — they are
    server-side settings owned by ``ops/docker-compose.yml``.  The relevant one
    is ``noeviction``, which means an untrimmed stream fills the 2 GB ceiling
    and Redis then *refuses writes* rather than silently dropping data.  That is
    why ``maxlen`` is a required argument on the stream publisher (E01-S04).
    """

    host: str = "localhost"
    port: int = Field(default=6379, ge=1, le=65535)
    db: int = Field(default=0, ge=0, le=15)
    max_connections: int = Field(default=20, ge=1, le=100)
    socket_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    socket_connect_timeout_seconds: float = Field(default=3.0, gt=0, le=60)

    # Streams are capped so a stalled consumer cannot exhaust `noeviction`
    # memory.  This is the default cap; hot streams override per-stream.
    default_stream_maxlen: int = Field(default=10_000, ge=100, le=1_000_000)

    def dsn(self, password: str | None = None) -> str:
        """Build the Redis URL.  ``REDIS_URL`` overrides entirely, as above."""
        import os
        from urllib.parse import quote

        override = os.environ.get("REDIS_URL")
        if override:
            return override

        auth = f":{quote(password, safe='')}@" if password else ""
        return f"redis://{auth}{self.host}:{self.port}/{self.db}"

    def safe_dsn(self) -> str:
        """The connection target with no credential in it — safe to log."""
        import os

        if os.environ.get("REDIS_URL"):
            return "<from REDIS_URL override>"
        return f"redis://{self.host}:{self.port}/{self.db}"


class BrokerAuthConfig(_Model):
    method: str = "oauth_2fa"
    daily_reauth_time: time = time(7, 0)
    credentials_source: str = "env"


class BrokerConfig(_Model):
    primary: str = "angelone"
    fallback: str | None = "fyers"

    #: The REGISTERED Algo-ID. A self-developed algo below
    #: :data:`SEBI_ALGO_REGISTRATION_OPS` is not registered, so it has none,
    #: and ``_live_mode_requires_compliance`` requires one only at or above the
    #: threshold. Its orders are still tagged with a GENERIC algo ID, and who
    #: sends that is the open question ``algo_tagging_confirmation`` records
    #: (audit LEG-001; until 21 Nov 2026 this comment called empty "the
    #: CORRECT value", which nothing had confirmed). If the broker's answer
    #: says the client sends a value, it is encoded then - not guessed now.
    algo_id: str = ""
    #: The broker's WRITTEN answer to "what must a client-developed,
    #: sub-threshold algo send on an API order?" - a support ticket number, an
    #: email date, or a documentation URL with its retrieval date (audit
    #: LEG-001). The exchanges tag such orders with a generic algo ID; whether
    #: Kite applies it server-side or expects the client to send something is
    #: not confirmed. Live mode refuses to load while this is empty: the
    #: question has a cheap answer and an expensive wrong guess.
    algo_tagging_confirmation: str = ""
    auth: BrokerAuthConfig = Field(default_factory=BrokerAuthConfig)


class HardFilters(_Model):
    """India-specific exclusions.  See INDIA_FEATURES_AND_CONFIG.md §5.2."""

    exclude_t2t: bool = True
    exclude_asm_gsm: bool = True
    exclude_fno_ban: bool = True
    exclude_earnings_today: bool = True
    min_circuit_band_pct: Decimal = Decimal("10")
    min_price: Decimal = Decimal("100")
    min_avg_volume_20d: int = 500_000
    min_market_cap_cr: Decimal = Decimal("5000")
    min_avg_daily_range_pct: Decimal = Decimal("1.5")
    max_spread_pct: Decimal = Decimal("0.05")

    @model_validator(mode="after")
    def _t2t_cannot_be_disabled(self) -> Self:
        if not self.exclude_t2t:
            raise ValueError(
                "exclude_t2t cannot be disabled: intraday trading is structurally "
                "impossible in the Trade-to-Trade segment (compulsory delivery)"
            )
        return self


class ScoringWeights(_Model):
    trend_alignment: Decimal = Decimal("0.25")
    relative_strength: Decimal = Decimal("0.20")
    volatility_fitness: Decimal = Decimal("0.15")
    volume_expansion: Decimal = Decimal("0.15")
    level_proximity: Decimal = Decimal("0.15")
    catalyst_news: Decimal = Decimal("0.10")

    @model_validator(mode="after")
    def _sums_to_one(self) -> Self:
        total = sum(
            (
                self.trend_alignment,
                self.relative_strength,
                self.volatility_fitness,
                self.volume_expansion,
                self.level_proximity,
                self.catalyst_news,
            ),
            Decimal(0),
        )
        if abs(total - Decimal(1)) > Decimal("0.001"):
            raise ValueError(f"scoring weights must sum to 1.0, got {total}")
        return self


class UniverseConfig(_Model):
    base: str = "nifty200"
    custom_symbols: list[str] = Field(default_factory=list)
    hard_filters: HardFilters = Field(default_factory=HardFilters)
    scoring_weights: ScoringWeights = Field(default_factory=ScoringWeights)
    shortlist_size: int = Field(default=15, ge=1, le=100)
    final_watchlist_size: int = Field(default=8, ge=1, le=50)

    @model_validator(mode="after")
    def _watchlist_fits_shortlist(self) -> Self:
        if self.final_watchlist_size > self.shortlist_size:
            raise ValueError("final_watchlist_size cannot exceed shortlist_size")
        return self


class PerTradeRisk(_Model):
    risk_pct: Decimal = Field(default=Decimal("1.0"), gt=0)
    #: The only sizing method that exists (``execution/sizer.py``). A string
    #: field accepted any word and changed nothing (audit ENG-003); a Literal
    #: makes "configured" and "implemented" the same set.
    sizing_method: Literal["atr_based"] = "atr_based"
    atr_multiplier_stop: Decimal = Field(default=Decimal("1.5"), gt=0)
    max_position_pct: Pct = Decimal("20")
    #: The smallest position worth a slot, as a share of capital (audit
    #: RSK-012). Below it the sizer refuses rather than open a token position
    #: that takes one of the day's slots and pays a full round trip of costs
    #: for almost no exposure. 2% of 5,00,000 is Rs 10,000.
    min_position_pct: Pct = Decimal("2")
    #: How far the trigger price may sit from the live price, as a percentage
    #: of the live price, before the signal is refused (audit RSK-002). Capped
    #: in code: a band wide enough to admit a 10x scale error is no band.
    max_price_divergence_pct: Decimal = Field(
        default=Decimal("1.0"), gt=0, le=MAX_PRICE_DIVERGENCE_PCT
    )
    target_r_multiple: Decimal = Field(default=Decimal("2.0"), gt=0)
    #: Must be unset. Nothing moves a stop after entry - no trailing stop is
    #: implemented, and ``Position`` requires a long's stop below its entry,
    #: so one past breakeven is not representable (audit NR-009). The field
    #: defaulted to 1.0 and promised a trailing stop after 1R that never
    #: happened; it now refuses any value until the feature exists.
    trailing_stop_after_r: Decimal | None = None

    @field_validator("trailing_stop_after_r")
    @classmethod
    def _trailing_not_implemented(cls, v: Decimal | None) -> Decimal | None:
        if v is not None:
            raise ValueError(
                "risk.per_trade.trailing_stop_after_r must be unset: no trailing stop is "
                "implemented, so a value here would describe a control that does not run"
            )
        return v

    @field_validator("max_position_pct")
    @classmethod
    def _position_within_hard_bound(cls, v: Decimal) -> Decimal:
        if v > MAX_POSITION_PCT:
            raise ValueError(f"max_position_pct {v} exceeds the hard bound of {MAX_POSITION_PCT}%")
        return v

    @model_validator(mode="after")
    def _minimum_below_maximum(self) -> Self:
        if not 0 < self.min_position_pct < self.max_position_pct:
            raise ValueError(
                f"min_position_pct {self.min_position_pct} must be above 0 and below "
                f"max_position_pct {self.max_position_pct}, or no position could ever fit"
            )
        return self

    @field_validator("risk_pct")
    @classmethod
    def _within_hard_bound(cls, v: Decimal) -> Decimal:
        if v > MAX_RISK_PCT_PER_TRADE:
            raise ValueError(
                f"risk_pct {v} exceeds the hard safety bound of "
                f"{MAX_RISK_PCT_PER_TRADE}%. This limit is defined in code and "
                f"cannot be raised by configuration."
            )
        return v


class PortfolioRisk(_Model):
    max_daily_loss_pct: Decimal = Field(default=Decimal("3.0"), gt=0)
    max_sector_exposure_pct: Pct = Decimal("40")
    max_correlated_positions: int = Field(default=2, ge=1)
    #: What counts as "correlated" for :func:`build_correlation_check`, compared
    #: against the ABSOLUTE correlation of daily log returns over 60 sessions.
    #:
    #: Added for E14-S04, which found that ``max_correlated_positions`` had no
    #: companion threshold — the count of correlated names was configurable and
    #: the definition of correlated was not. ``StrategyValidationConfig.
    #: max_correlation_to_active`` looks like it would serve, and must not:
    #: that one is about whether a new STRATEGY duplicates an active strategy
    #: in the overfitting gauntlet. Two unrelated concepts sharing one number
    #: would couple a portfolio limit to a research limit, so that tuning
    #: either would silently move the other.
    correlation_threshold: Decimal = Field(default=Decimal("0.7"), gt=0, le=1)
    max_net_directional_exposure_pct: Pct = Decimal("60")
    consecutive_loss_halt: int = Field(default=3, ge=1, le=MAX_CONSECUTIVE_LOSS_HALT)

    @field_validator("max_daily_loss_pct")
    @classmethod
    def _within_hard_bound(cls, v: Decimal) -> Decimal:
        if v > MAX_DAILY_LOSS_PCT:
            raise ValueError(
                f"max_daily_loss_pct {v} exceeds the hard bound of {MAX_DAILY_LOSS_PCT}%"
            )
        return v


class SquareOffTimes(_Model):
    """The broker's auto square-off times - facts, not tunables (audit ENG-003).

    ``MarketCalendar`` computes every deadline from its own constants
    (``common/calendar.py``) and nothing read these, so an edit here changed
    the documentation of the time exit and not the time exit. They must now
    equal the calendar's; a change of exchange schedule is a code change to
    the calendar, with its tests.
    """

    cas_stocks: time = time(15, 10)
    non_cas_stocks: time = time(15, 20)
    fno: time = time(15, 25)

    @model_validator(mode="after")
    def _are_the_calendars(self) -> Self:
        from algotrader.common import calendar

        expected = {
            "cas_stocks": calendar.SQUAREOFF_CAS_STOCKS,
            "non_cas_stocks": calendar.SQUAREOFF_NON_CAS,
            "fno": calendar.SQUAREOFF_FNO,
        }
        wrong = {k: v for k, v in expected.items() if getattr(self, k) != v}
        if wrong:
            raise ValueError(
                f"risk.square_off_times {sorted(wrong)} differ from the calendar's "
                f"{ {k: v.strftime('%H:%M') for k, v in wrong.items()} }; the deadlines "
                f"are computed from common/calendar.py, so a different value here would "
                f"describe a time exit that does not happen"
            )
        return self


class RiskConfig(_Model):
    capital: Decimal = Field(default=Decimal("500000"), gt=0)
    position_slots: int = Field(default=5, ge=1)
    capital_per_slot_pct: Pct = Decimal("20")
    per_trade: PerTradeRisk = Field(default_factory=PerTradeRisk)
    portfolio: PortfolioRisk = Field(default_factory=PortfolioRisk)
    exit_buffer_minutes: int = Field(
        default=5, ge=MIN_EXIT_BUFFER_MINUTES, le=MAX_EXIT_BUFFER_MINUTES
    )
    #: How much runway a NEW entry must have before this stock's square-off
    #: deadline (E14-S06, check 14).
    #:
    #: Not the same thing as ``exit_buffer_minutes``, which is already
    #: subtracted when the deadline is computed and protects against the
    #: BROKER's forced square-off. This one asks whether the trade has time to
    #: work at all: entering at 15:04 against a 15:05 CAS deadline is a
    #: position that will be closed at whatever the price happens to be.
    #:
    #: Nor is it redundant with the 15:00-15:30 no-trade window. A CAS stock's
    #: deadline is 15:10 minus the buffer = 15:05, so at 14:59 — still inside
    #: the tradable window — the runway is six minutes.
    #:
    #: **30 is a judgement, not a derived number.** It is two bars of the
    #: slowest supported interval (15m), which is thin; a 15-minute strategy
    #: arguably wants 45. It is generous for a 1-minute one. The effective
    #: interval is adaptive and not known at check time, so a single configured
    #: floor is the honest mechanism — revisit if the system settles on one
    #: timeframe.
    min_minutes_to_squareoff: int = Field(
        default=30, ge=MIN_ENTRY_RUNWAY_MINUTES, le=MAX_ENTRY_RUNWAY_MINUTES
    )
    square_off_times: SquareOffTimes = Field(default_factory=SquareOffTimes)

    @field_validator("position_slots")
    @classmethod
    def _slots_within_bound(cls, v: int) -> int:
        if v > MAX_POSITION_SLOTS:
            raise ValueError(f"position_slots {v} exceeds the hard bound of {MAX_POSITION_SLOTS}")
        return v

    @model_validator(mode="after")
    def _correlated_positions_fit_the_slots(self) -> Self:
        """A correlation cap above the slot count can never bind (RSK-009)."""
        if self.portfolio.max_correlated_positions > self.position_slots:
            raise ValueError(
                f"max_correlated_positions {self.portfolio.max_correlated_positions} "
                f"exceeds position_slots {self.position_slots}; a cap that can never be "
                f"reached is not a cap"
            )
        return self

    @model_validator(mode="after")
    def _slot_allocation_coherent(self) -> Self:
        allocated = self.capital_per_slot_pct * self.position_slots
        if allocated > Decimal(100):
            raise ValueError(
                f"{self.position_slots} slots x {self.capital_per_slot_pct}% = "
                f"{allocated}% of capital, which exceeds 100%"
            )
        return self

    @property
    def capital_per_slot(self) -> Decimal:
        return self.capital * self.capital_per_slot_pct / Decimal(100)

    @property
    def risk_per_trade_rupees(self) -> Decimal:
        return self.capital * self.per_trade.risk_pct / Decimal(100)

    @property
    def daily_loss_limit_rupees(self) -> Decimal:
        return self.capital * self.portfolio.max_daily_loss_pct / Decimal(100)


class ExecutionConfig(_Model):
    interval_mode: str = "adaptive"
    interval_floor: Timeframe = Timeframe.M5
    interval_ceiling: Timeframe = Timeframe.M15
    latency_headroom_multiplier: Decimal = Field(default=Decimal("2.0"), ge=1)
    recalibrate_interval_daily: bool = True
    #: Read by :meth:`algotrader.broker.ratelimit.RateLimitConfig.from_execution`,
    #: which builds the one order limiter (audit RSK-010).
    max_orders_per_second: int = Field(default=5, ge=1)
    # There is no ``order_type`` here, deliberately (audit ENG-003). The file
    # said ``order_type: limit`` with a 0.05% offset and a 30 s timeout while
    # the gateway builds MARKET entries with market protection, and nothing
    # read any of the three. MARKET is the decision (``execution/gateway.py``,
    # ``build_entry``): the trigger has already been reached, and a limit that
    # does not fill leaves the risk budget committed to a position that does
    # not exist. Market protection is what bounds the slippage.
    #: Entries are refused inside each window. Every window in
    #: :data:`MANDATORY_NO_TRADE_WINDOWS` must be covered by one of these, so
    #: the default is those windows and an empty list is refused (RSK-009,
    #: RSK-014).
    no_trade_windows: list[tuple[time, time]] = Field(
        default_factory=lambda: list(MANDATORY_NO_TRADE_WINDOWS)
    )

    #: Market protection for MARKET/SL-M orders. -1 = broker auto-protection.
    #: Zerodha rejects unprotected market orders from 1 Apr 2026, and 0 is
    #: explicitly rejected, so this may not be zero.
    market_protection: Decimal = Decimal("-1")

    #: How old a Recommendation may be when execution acts on it. Older ones
    #: are refused as STALE_DATA rather than traded late (E15-S13).
    max_signal_age_seconds: int = Field(default=60, ge=1, le=MAX_SIGNAL_AGE_SECONDS)

    @field_validator("max_orders_per_second")
    @classmethod
    def _sebi_safe(cls, v: int) -> int:
        if v > MAX_ORDERS_PER_SECOND:
            raise ValueError(
                f"max_orders_per_second {v} exceeds the hard cap of "
                f"{MAX_ORDERS_PER_SECOND}. SEBI's algo-registration threshold "
                f"is {SEBI_ALGO_REGISTRATION_OPS} orders/sec per segment; we "
                f"stay at half that deliberately, which is also what keeps "
                f"this system out of the registration regime entirely."
            )
        return v

    @field_validator("market_protection")
    @classmethod
    def _protection_not_zero(cls, v: Decimal) -> Decimal:
        if v == 0:
            raise ValueError(
                "market_protection of 0 is rejected by the broker. Use -1 for "
                "auto-protection or a positive percentage."
            )
        if v < 0 and v != Decimal("-1"):
            raise ValueError("the only valid negative market_protection is -1 (auto)")
        return v

    @model_validator(mode="after")
    def _floor_below_ceiling(self) -> Self:
        if self.interval_floor.seconds > self.interval_ceiling.seconds:
            raise ValueError("interval_floor must not exceed interval_ceiling")
        return self

    @model_validator(mode="after")
    def _windows_are_real_and_cover_the_mandatory_ones(self) -> Self:
        """Each window must have start < end, and the mandatory ones must stay.

        A ``(15:30, 15:00)`` window matched nothing and read as a blackout in
        review; an empty list removed the opening and closing blocks without a
        word (audit RSK-009).
        """
        for start, end in self.no_trade_windows:
            if not start < end:
                raise ValueError(
                    f"no_trade_window {start:%H:%M}-{end:%H:%M} does not start before it "
                    f"ends, so it would block nothing"
                )
        for start, end in MANDATORY_NO_TRADE_WINDOWS:
            if not any(s <= start and end <= e for s, e in self.no_trade_windows):
                raise ValueError(
                    f"no_trade_windows must cover {start:%H:%M}-{end:%H:%M}; configuration may "
                    f"add windows but cannot remove a mandatory one"
                )
        return self


class AIModels(_Model):
    """Model IDs for the PLANNED AI layer - read by nothing yet (E10-S01).

    Unverified (audit HAL-003): no one has checked these names against
    Anthropic's published model list, and nothing rejects a wrong one. E10-S01,
    which builds the client, verifies each on its build date and validates the
    IDs at load; the ``anthropic`` SDK stays an optional extra until then.
    """

    deep_synthesis: str = "claude-opus-5"
    session_reasoning: str = "claude-sonnet-5"
    news_triage: str = "claude-haiku-4-5"


class AIConfidence(_Model):
    min_to_act: Decimal = Field(default=Decimal("0.65"), ge=0, le=1)
    min_for_full_size: Decimal = Field(default=Decimal("0.80"), ge=0, le=1)
    treat_conflict_as_no_trade: bool = True

    @model_validator(mode="after")
    def _thresholds_ordered(self) -> Self:
        if self.min_for_full_size < self.min_to_act:
            raise ValueError("min_for_full_size must be >= min_to_act")
        return self


class AICostControls(_Model):
    daily_token_budget: int = Field(default=2_000_000, gt=0)
    alert_at_pct: Pct = Decimal("80")
    hard_stop_at_pct: Pct = Decimal("100")


class AIConfig(_Model):
    provider: str = "anthropic"
    models: AIModels = Field(default_factory=AIModels)
    confidence: AIConfidence = Field(default_factory=AIConfidence)
    cost_controls: AICostControls = Field(default_factory=AICostControls)
    session_timeout_sec: int = Field(default=15, ge=1)
    fallback_on_timeout: str = "skip_trade"

    @field_validator("fallback_on_timeout")
    @classmethod
    def _must_fail_closed(cls, v: str) -> str:
        if v != "skip_trade":
            raise ValueError(
                "fallback_on_timeout must be 'skip_trade'. The system fails "
                "closed: an AI timeout never results in an unreviewed order."
            )
        return v


class StrategyValidationConfig(_Model):
    """The overfitting gauntlet.  See STRATEGY_ENGINE.md §5."""

    min_trades: int = Field(default=100, ge=MIN_STRATEGY_TRIALS)
    min_regimes: int = Field(default=2, ge=1)
    max_pbo: Decimal = Field(default=Decimal("0.5"), gt=0, le=1)
    min_deflated_sharpe_confidence: Decimal = Field(default=Decimal("0.95"), gt=0, lt=1)
    max_correlation_to_active: Decimal = Field(default=Decimal("0.8"), gt=0, le=1)
    parameter_sensitivity_pct: Decimal = Field(default=Decimal("20"), gt=0)
    holdout_months: int = Field(default=6, ge=1)

    @field_validator("max_pbo")
    @classmethod
    def _pbo_within_bound(cls, v: Decimal) -> Decimal:
        if v > MAX_PBO_ALLOWED:
            raise ValueError(
                f"max_pbo {v} exceeds the hard bound of {MAX_PBO_ALLOWED}. A PBO "
                f"above 0.5 means the in-sample ranking is more likely than not "
                f"to fail out-of-sample."
            )
        return v


class StrategyPromotionConfig(_Model):
    shadow_min_sessions: int = Field(default=20, ge=1)
    paper_min_trades: int = Field(default=30, ge=1)
    paper_min_expectancy_r: Decimal = Decimal("0.15")
    require_human_approval: bool = True

    @field_validator("require_human_approval")
    @classmethod
    def _cannot_be_disabled(cls, v: bool) -> bool:
        if not v:
            raise ValueError(
                "require_human_approval cannot be disabled. Promotion of a "
                "strategy to live capital is always a human decision, at every "
                "autonomy level. See STRATEGY_ENGINE.md §4.1."
            )
        return v


class AIGenerationConfig(_Model):
    enabled: bool = False  # Phase 2 — off until the gauntlet exists
    cadence: str = "weekly"
    max_proposals_per_cycle: int = Field(default=5, ge=1, le=20)
    max_active_ai_strategies: int = Field(default=3, ge=0)
    model: str = "claude-opus-5"
    modes: list[str] = Field(default_factory=lambda: ["observation", "journal"])
    require_hypothesis: bool = True
    min_journal_trades: int = Field(default=50, ge=1)

    @field_validator("cadence")
    @classmethod
    def _not_continuous(cls, v: str) -> str:
        if v == "continuous":
            raise ValueError(
                "Continuous strategy generation is not permitted. Each cycle "
                "adds to the global trial count and raises the Deflated Sharpe "
                "bar; generating continuously is the recursive-overfitting "
                "failure mode. Use 'weekly' or 'monthly'."
            )
        return v

    @field_validator("require_hypothesis")
    @classmethod
    def _hypothesis_mandatory(cls, v: bool) -> bool:
        if not v:
            raise ValueError(
                "require_hypothesis cannot be disabled. A strategy whose author "
                "cannot state an economic mechanism is data mining."
            )
        return v


class StrategyEngineConfig(_Model):
    enabled: bool = True
    max_active: int = Field(default=6, ge=1)
    max_active_per_regime: int = Field(default=3, ge=1)
    max_shadow: int = Field(default=10, ge=0)
    validation: StrategyValidationConfig = Field(default_factory=StrategyValidationConfig)
    promotion: StrategyPromotionConfig = Field(default_factory=StrategyPromotionConfig)
    ai_generation: AIGenerationConfig = Field(default_factory=AIGenerationConfig)

    @field_validator("max_active")
    @classmethod
    def _within_bound(cls, v: int) -> int:
        if v > MAX_ACTIVE_STRATEGIES:
            raise ValueError(f"max_active {v} exceeds the hard bound of {MAX_ACTIVE_STRATEGIES}")
        return v


class AutonomyConfig(_Model):
    """The autonomy ladder (``MVP_UI_AND_LEGAL.md §3.1``).

    ``level`` is ENFORCED on every entry by :mod:`algotrader.execution.autonomy`
    (audit RSK-001: it was validated and shipped as "L1 / alert only" while the
    execution service traded on ``system.mode`` alone). L0 and L1 place
    nothing; L2 needs a per-trade approval and L3 needs the envelope below, and
    because neither the approval flow (E16-S03) nor the envelope evaluation
    (E16-S02) exists yet, both refuse every entry. L4 places paper orders
    only. The envelope numbers are therefore read by nothing yet - they are
    E16-S02's inputs, and the level that would use them refuses until it
    exists.
    """

    level: AutonomyLevel = AutonomyLevel.L1_ALERT
    max_position_value_pct: Pct = Decimal("20")
    min_ai_confidence: Decimal = Field(default=Decimal("0.70"), ge=0, le=1)
    min_timeframe_agreement: int = Field(default=2, ge=0, le=3)
    max_india_vix: Decimal = Decimal("22")
    require_symbol_traded_before: bool = True
    escalation_timeout_sec: int = Field(default=60, ge=10)


class NotificationConfig(_Model):
    channels: list[str] = Field(default_factory=lambda: ["telegram"])
    premarket_briefing: bool = True
    trade_alerts: bool = True
    risk_breach_alerts: bool = True
    eod_report: bool = True
    #: Every entry waits for a human's approval, at every autonomy level.
    #: Enforced by :mod:`algotrader.execution.autonomy` (audit RSK-001): there
    #: is no approval channel yet (E16-S03), so while this is true no entry is
    #: sent - which is what an approval that never arrives means.
    require_approval_before_entry: bool = True
    recipients: list[str] = Field(default_factory=list, max_length=1)

    @model_validator(mode="after")
    def _single_recipient(self) -> Self:
        """Regulatory control, not a preference.

        Broadcasting trade signals to more than one person can trigger SEBI
        Research Analyst obligations.  See MVP_UI_AND_LEGAL.md §2.3.
        """
        if len(self.recipients) > 1:
            raise ValueError(
                "More than one notification recipient configured. Sharing trade "
                "signals with others can trigger SEBI Research Analyst "
                "obligations. Personal use means a single recipient."
            )
        return self


class AppConfig(_Model):
    """Root configuration object."""

    system: SystemConfig = Field(default_factory=SystemConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    broker: BrokerConfig = Field(default_factory=BrokerConfig)
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    strategy_engine: StrategyEngineConfig = Field(default_factory=StrategyEngineConfig)
    autonomy: AutonomyConfig = Field(default_factory=AutonomyConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)

    @model_validator(mode="after")
    def _order_rate_within_broker_limit(self) -> Self:
        """The binding constraint is min(SEBI cap, broker API limit).

        SEBI's threshold is 10 orders/sec and we cap at 5 — but a broker may
        allow far less, so a config carried over from a different broker is
        actively wrong and would get throttled by the broker rather than caught
        by us.

        Zerodha Kite Connect permits **10 orders/sec, enforced account-wide**,
        returning HTTP 429 above it — per Zerodha staff on their own developer
        forum.  (An earlier revision of this docstring said "roughly 3/sec",
        which came from a third-party blog and was wrong.  The authoritative
        per-broker figures live in ``broker/profiles.py``, which this validator
        reads — do not restate them here.)
        """
        from algotrader.broker.profiles import get_profile

        try:
            profile = get_profile(self.broker.primary)
        except ValueError as exc:
            raise ValueError(str(exc)) from None

        if self.execution.max_orders_per_second > profile.max_orders_per_second:
            raise ValueError(
                f"execution.max_orders_per_second is "
                f"{self.execution.max_orders_per_second}, but "
                f"{profile.display_name} permits only "
                f"{profile.max_orders_per_second}/sec. Exceeding the broker's "
                f"own API limit gets you throttled regardless of what SEBI "
                f"allows. Lower it to {profile.max_orders_per_second} or below."
            )
        return self

    @model_validator(mode="after")
    def _live_mode_requires_compliance(self) -> Self:
        """The two SEBI obligations that bind before live trading.

        **Static IP is unconditional.** It applies to every API order
        regardless of rate, and an order from an unwhitelisted address is
        rejected outright.

        **An Algo-ID is required only at or above the registration
        threshold**, and this is a correction (9 Sep 2026). The gate used to
        demand one in every live configuration, which would have blocked
        go-live permanently on a requirement that does not apply here: below
        :data:`SEBI_ALGO_REGISTRATION_OPS` there is no registration, so there
        is no registered Algo-ID to obtain, and the broker/exchange tags the
        order with a generic identifier instead.

        The evidence, recorded because the previous belief was confidently
        wrong and the same reasoning must be re-checkable:

        * SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/0000013 (4 Feb 2025) I(c) --
          algos self-developed by retail investors "shall also be registered
          with the Exchange, through their broker, **only if they cross the
          specified order per second threshold**".
        * The same circular I(d) has brokers categorise "all orders **above
          the specified threshold** as algo orders". Below it, the order is
          not categorised as an algo order in the first place.
        * I(b) scopes the tagging duty to APIs "extended by brokers **to algo
          providers**". A personal algo is not an algo provider; II(b)'s "all
          algo orders shall be tagged" sits under the stock broker's
          responsibilities, and the tag it refers to is the one the broker
          applies.
        * Zerodha's own guidance describes sub-threshold client algos as
          "tagged with generic ID (unregistered, <=10 OPS)" and asks the
          developer only for a dedicated static IP.
        * ``kiteconnect.KiteConnect.place_order`` documents ``algo_id`` as
          "an optional algo ID to associate with the order", default ``None``.

        **What would make this wrong.** Raising
        :data:`MAX_ORDERS_PER_SECOND` to or past the threshold -- which the
        module-level guard already refuses -- or SEBI extending registration
        below it. Both are changes to the constants above, and both bring
        this branch back to life.
        """
        if self.system.mode is SystemMode.LIVE:
            if not self.system.static_ip:
                raise ValueError(
                    "live mode requires system.static_ip to be set and whitelisted "
                    "with the broker (SEBI requirement, and it applies at every "
                    "order rate)"
                )
            self._live_static_ip_is_public()
            if not self.broker.algo_tagging_confirmation.strip():
                raise ValueError(
                    "live mode requires broker.algo_tagging_confirmation: the broker's "
                    "written answer on how a sub-threshold client algo's orders are "
                    "tagged (audit LEG-001). Record the ticket, email or URL and date"
                )
            if self.autonomy.level is AutonomyLevel.L4_FULL_AUTO:
                # Audit RSK-001. MVP_UI_AND_LEGAL.md 3.1: L4 'only after months of
                # L3 evidence'. L3 cannot run until its envelope exists (E16-S02),
                # so full autonomy on live capital is refused outright - here,
                # where configurations are validated, because paper and live may
                # differ in nothing else (tests/security/test_paper_isolation.py).
                raise ValueError(
                    "autonomy L4 is not permitted with system.mode live: full autonomy "
                    "on live capital needs months of L3 evidence, and L3 cannot run yet"
                )
            self._live_is_deliberate()
            if (
                self.execution.max_orders_per_second >= SEBI_ALGO_REGISTRATION_OPS
                and not self.broker.algo_id
            ):
                raise ValueError(
                    f"live mode at {self.execution.max_orders_per_second} "
                    f"orders/sec is at or above SEBI's algo-registration "
                    f"threshold of {SEBI_ALGO_REGISTRATION_OPS}/sec, so the algo "
                    f"must be registered with the exchange through the broker "
                    f"and broker.algo_id must carry the registered Algo-ID"
                )
        return self

    def _live_static_ip_is_public(self) -> None:
        """The whitelisted address must be a real public IP (audit RSK-008).

        The gate used to be ``if not static_ip``, so ``"x"`` - or a private
        address no broker could ever have whitelisted - satisfied it.
        """
        try:
            address = ipaddress.ip_address(self.system.static_ip.strip())
        except ValueError:
            raise ValueError(
                "system.static_ip is not an IP address; live mode needs the public "
                "address the broker has whitelisted"
            ) from None
        if not address.is_global:
            raise ValueError(
                f"system.static_ip {address} is not a public address (private, "
                f"loopback, reserved or documentation range); the broker whitelists "
                f"the address orders arrive FROM, which is public"
            )

    def _live_is_deliberate(self) -> None:
        """Live needs the production environment AND this file's confirmation.

        Audit RSK-008: one YAML line - or an ``ALGOTRADER_CONFIG`` pointing at
        another file - enabled real orders. Two independent conditions now
        have to agree with the file being loaded:

        * ``ALGOTRADER_ENV`` is ``production`` - a development machine cannot
          load a live configuration by accident;
        * ``ALGOTRADER_CONFIRM_LIVE`` equals this configuration's
          :meth:`config_hash` - the operator confirmed THIS file, and any
          later edit to it (or a different file) changes the hash and refuses.
        """
        environment = os.environ.get("ALGOTRADER_ENV", "")
        if environment != LIVE_ENVIRONMENT:
            raise ValueError(
                f"live mode requires ALGOTRADER_ENV={LIVE_ENVIRONMENT}; it is "
                f"{environment or 'unset'}"
            )
        expected = self.config_hash()
        confirmed = os.environ.get(CONFIRM_LIVE_VARIABLE, "").strip()
        if confirmed != expected:
            raise ValueError(
                f"live mode requires {CONFIRM_LIVE_VARIABLE} to equal this "
                f"configuration's hash ({expected}); it is "
                f"{'unset' if not confirmed else 'a different value'}. Confirm the file "
                f"you mean to trade with, after its last edit"
            )

    def config_hash(self) -> str:
        """Stable hash, recorded in each day's plan.

        Lets every trade be traced back to the exact configuration that
        produced it.
        """
        import hashlib
        import json

        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load and validate configuration.

    Raises with a full list of problems rather than the first one — a
    misconfiguration should be fixable in one pass, not one restart at a time.
    """
    import os

    resolved = Path(path or os.environ.get("ALGOTRADER_CONFIG", "config/system.yaml"))
    if not resolved.exists():
        raise FileNotFoundError(f"config file not found: {resolved}")

    with resolved.open(encoding="utf-8") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh) or {}

    return AppConfig.model_validate(raw)
