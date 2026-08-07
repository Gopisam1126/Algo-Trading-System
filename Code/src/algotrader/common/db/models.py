"""SQLAlchemy models — the schema, and where the business rules are enforced.

Read `EPIC01_TECHNICAL_SPEC.md §3` alongside this file. Every `CHECK`,
`UNIQUE` and `NOT NULL` below traces to a numbered business rule (BR-n), and
the rule is cited on the constraint. They are in the database rather than only
in application code because application code can be bypassed by a migration, a
manual fix, or a bug — and the database is the last line.

**Two things here are load-bearing and easy to undo by accident:**

1. ``stop_price`` on ``positions`` is ``NOT NULL`` (BR-1). A position without a
   protective stop is the single most expensive failure this system can have.
   Nothing may create one, ever, including a hand-written UPDATE.
2. The two partial unique indexes on ``positions`` are what make slot
   discipline a guarantee rather than a hope. The Redis lock is the fast path;
   these are the enforcement.

**Enum columns.** Small, stable, safety-critical vocabularies (side, direction,
timeframe, product) are `Enum(..., native_enum=False)`, which renders as
VARCHAR plus a CHECK constraint — an invalid value becomes impossible rather
than merely improbable. Larger and more volatile vocabularies (order status,
reject reason) are plain strings, so that adding a value does not force a
migration. That split is deliberate; see §2.3 F.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from enum import Enum as PyEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from algotrader.common.db.base import Base
from algotrader.common.db.types import (
    Charge,
    Money,
    Pct5,
    Pct6,
    Pnl,
    Prob,
    Stat,
    Ts,
)
from algotrader.common.enums import (
    Direction,
    Exchange,
    OrderIntent,
    OrderType,
    PositionStatus,
    Product,
    Side,
    Timeframe,
)

#: JSONB payloads. Concrete type arguments so mypy can check callers; the
#: contents are validated by Pydantic at the repository boundary, never here.
#: A JSONB column is a schema escape hatch, and the only thing keeping it
#: honest is that nothing reads it without parsing it into a model first.
JsonDict = dict[str, Any]


def _enum(py_enum: type[PyEnum], name: str) -> Enum:
    """A VARCHAR column constrained to the enum's *values*.

    ``values_callable`` matters: without it SQLAlchemy stores member *names*
    (``SELL``) rather than *values* (``SELL``) — identical for most of these
    enums but not for :class:`SystemMode`, whose values are lowercase. Being
    explicit means the column always holds exactly what ``.value`` returns,
    which is what every other layer round-trips.

    ``length`` adds deliberate headroom. By default SQLAlchemy sizes the VARCHAR
    to the longest member, which for :class:`Side` is exactly 4 characters. A
    bad value then trips *length truncation* before it reaches the CHECK, and
    the error you get is ``value too long for type character varying(4)`` —
    which says nothing about which column or what was expected. With headroom
    the CHECK fires instead and names the constraint. Same rejection either way;
    one of them is debuggable at 09:20 on a trading morning.

    ``create_constraint=True`` is REQUIRED and is the whole point of this
    helper. SQLAlchemy changed the default to **False** in 1.4, so
    ``Enum(native_enum=False)`` on its own produces a plain VARCHAR with *no
    validation whatsoever* — the column will happily accept ``'SIDEWAYS'`` as a
    side. Before the headroom above was added, the length limit had been acting
    as the only guard by accident; widening the column removed it and let an
    invalid value straight in. The test asserting that an invalid side is
    rejected is what caught that.
    """
    values = [str(member.value) for member in py_enum]
    return Enum(
        py_enum,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda e: [m.value for m in e],
        length=max(len(v) for v in values) + 16,
    )


# ---------------------------------------------------------------------------
# Reference & instrument data
# ---------------------------------------------------------------------------


class Instrument(Base):
    """The symbol master, refreshed daily from the broker.

    Plain table: small, and queried by id. This is the only table that maps a
    ticker string to the integer ``symbol_id`` every other table uses; see
    §2.3 E for why that translation is centralised in one repository.
    """

    __tablename__ = "instruments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tradingsymbol: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange: Mapped[str] = mapped_column(_enum(Exchange, "exchange_enum"), nullable=False)
    broker_token: Mapped[str] = mapped_column(String(32), nullable=False)
    isin: Mapped[str | None] = mapped_column(String(12))
    name: Mapped[str | None] = mapped_column(String(128))
    lot_size: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    tick_size: Mapped[Decimal] = mapped_column(Money, nullable=False)
    sector: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    first_seen: Mapped[dt.date] = mapped_column(
        Date, nullable=False, server_default=func.current_date()
    )
    last_seen: Mapped[dt.date] = mapped_column(
        Date, nullable=False, server_default=func.current_date()
    )

    __table_args__ = (
        UniqueConstraint("exchange", "tradingsymbol", name="uq_instrument"),
        CheckConstraint("lot_size >= 1", name="lot_size_positive"),
        CheckConstraint("tick_size > 0", name="tick_size_positive"),
        Index("ix_instruments_token", "broker_token"),
    )


# Partial indexes live outside the class because `postgresql_where` needs a
# column expression, which is not available while the class body is executing.
# Sector lookups only ever care about tradable instruments.
Index(
    "ix_instruments_sector",
    Instrument.sector,
    postgresql_where=text("is_active"),
)


class InstrumentDailyStatus(Base):
    """India hazard flags, per symbol **per day** (BR-8).

    History matters and a single current-state row would destroy it: a backtest
    must know whether a symbol was in ASM on the day being simulated, not
    whether it is today. Getting this wrong makes every historical result
    optimistic, because today's clean symbols were not always clean.
    """

    __tablename__ = "instrument_daily_status"

    symbol_id: Mapped[int] = mapped_column(Integer, ForeignKey("instruments.id"), primary_key=True)
    trade_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)

    is_t2t: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    is_asm: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    is_gsm: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    is_fno_ban: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    #: Drives the per-stock square-off deadline (15:10 CAS vs 15:20 non-CAS).
    is_cas_stock: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    circuit_band_pct: Mapped[Decimal | None] = mapped_column(Pct5)
    upper_circuit: Mapped[Decimal | None] = mapped_column(Money)
    lower_circuit: Mapped[Decimal | None] = mapped_column(Money)
    has_earnings_today: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    prev_close: Mapped[Decimal | None] = mapped_column(Money)
    fetched_at: Mapped[dt.datetime] = mapped_column(Ts, nullable=False, server_default=func.now())

    __table_args__ = (Index("ix_ids_date", "trade_date"),)


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


class Ohlcv(Base):
    """The bar store. **Hypertable**, partitioned on ``ts``.

    ``ts`` is the bar OPEN time in UTC, aligned to the 09:15 IST session start
    rather than to wall-clock hours — a 15-minute bar runs 09:15–09:30, not
    09:00–09:15. The domain model calls this field ``open_ts``; the column keeps
    the shorter ``ts`` because it is the partitioning key and that convention is
    worth preserving (§2.3 D).
    """

    __tablename__ = "ohlcv"

    symbol_id: Mapped[int] = mapped_column(Integer, ForeignKey("instruments.id"), primary_key=True)
    timeframe: Mapped[str] = mapped_column(_enum(Timeframe, "timeframe_enum"), primary_key=True)
    ts: Mapped[dt.datetime] = mapped_column(Ts, primary_key=True)

    open: Mapped[Decimal] = mapped_column(Money, nullable=False)
    high: Mapped[Decimal] = mapped_column(Money, nullable=False)
    low: Mapped[Decimal] = mapped_column(Money, nullable=False)
    close: Mapped[Decimal] = mapped_column(Money, nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    trade_count: Mapped[int | None] = mapped_column(Integer)
    vwap: Mapped[Decimal | None] = mapped_column(Money)
    is_adjusted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    __table_args__ = (
        CheckConstraint("open > 0 AND high > 0 AND low > 0 AND close > 0", name="prices_positive"),
        CheckConstraint("volume >= 0", name="volume_non_negative"),
        CheckConstraint("trade_count IS NULL OR trade_count >= 0", name="trade_count_non_negative"),
        # The same invariant that was silently inert in the Pydantic model until
        # the second audit — a field validator could not see fields declared
        # after it, so `close > high` was never actually checked. Enforced in
        # both places on purpose: this is the one data-quality rule whose
        # violation corrupts every downstream indicator without raising.
        CheckConstraint(
            "high >= low AND high >= open AND high >= close AND low <= open AND low <= close",
            name="ohlc_coherent",
        ),
    )


# ---------------------------------------------------------------------------
# Daily plan
# ---------------------------------------------------------------------------


class DailyPlan(Base):
    """The session's contract, produced by the 08:45 pre-market run.

    ``locked_at`` implements BR-14: once set, the plan is immutable. That is
    enforced in the repository rather than by a constraint, because "no writes
    after this timestamp" is not expressible as a row-level CHECK.
    """

    __tablename__ = "daily_plan"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_date: Mapped[dt.date] = mapped_column(Date, nullable=False, unique=True)
    generated_at: Mapped[dt.datetime] = mapped_column(Ts, nullable=False)
    locked_at: Mapped[dt.datetime | None] = mapped_column(Ts)
    #: Ties every trade back to the exact configuration that produced it.
    config_hash: Mapped[str] = mapped_column(String(32), nullable=False)

    market_thesis: Mapped[JsonDict] = mapped_column(JSONB, nullable=False)
    macro_snapshot: Mapped[JsonDict] = mapped_column(JSONB, nullable=False)

    model_used: Mapped[str | None] = mapped_column(String(64))
    #: False means the AI was unavailable and this is a score-only day.
    ai_available: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer)
    generation_ms: Mapped[int | None] = mapped_column(Integer)


class PlanCandidate(Base):
    """A ranked, scored symbol within a day's plan."""

    __tablename__ = "plan_candidate"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("daily_plan.id", ondelete="CASCADE"), nullable=False
    )
    symbol_id: Mapped[int] = mapped_column(Integer, ForeignKey("instruments.id"), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    tradeability_score: Mapped[Decimal] = mapped_column(Pct5, nullable=False)
    #: Per-component, so a score can be explained rather than just asserted.
    score_breakdown: Mapped[JsonDict] = mapped_column(JSONB, nullable=False)
    direction_bias: Mapped[str] = mapped_column(_enum(Direction, "direction_enum"), nullable=False)
    ai_confidence: Mapped[Decimal | None] = mapped_column(Prob)
    ai_rationale: Mapped[str | None] = mapped_column(Text)
    playbook: Mapped[JsonDict | None] = mapped_column(JSONB)
    #: Filled at 09:02 once the opening print is known.
    gap_pct: Mapped[Decimal | None] = mapped_column(Pct6)
    status: Mapped[str] = mapped_column(String(20), nullable=False)

    __table_args__ = (
        UniqueConstraint("plan_id", "symbol_id", name="uq_plan_symbol"),
        CheckConstraint(
            "tradeability_score >= 0 AND tradeability_score <= 100", name="score_in_range"
        ),
        CheckConstraint(
            "ai_confidence IS NULL OR (ai_confidence >= 0 AND ai_confidence <= 1)",
            name="confidence_in_range",
        ),
        Index("ix_candidate_plan_rank", "plan_id", "rank"),
    )


# ---------------------------------------------------------------------------
# Orders, fills, positions
# ---------------------------------------------------------------------------


class Order(Base):
    """An order as this system knows it — not as the broker knows it.

    ``client_order_id`` is our idempotency key and is UNIQUE (BR-2). That
    constraint is what makes "query, don't blindly retry" safe after an
    ambiguous timeout: without it, a race can still double-insert and the
    recovery path would create a second real position.
    """

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    broker_order_id: Mapped[str | None] = mapped_column(String(64))
    #: SEBI: every algorithmic order carries an exchange-assigned Algo-ID.
    #: Client-supplied per order — confirmed against the Kite SDK signature.
    algo_id: Mapped[str | None] = mapped_column(String(64))
    correlation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    symbol_id: Mapped[int] = mapped_column(Integer, ForeignKey("instruments.id"), nullable=False)
    side: Mapped[str] = mapped_column(_enum(Side, "side_enum"), nullable=False)
    order_type: Mapped[str] = mapped_column(_enum(OrderType, "order_type_enum"), nullable=False)
    product: Mapped[str] = mapped_column(_enum(Product, "product_enum"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(Money)
    trigger_price: Mapped[Decimal | None] = mapped_column(Money)
    #: Mandatory on MARKET/SL-M from 1 Apr 2026: -1 (auto) or a percentage.
    market_protection: Mapped[Decimal | None] = mapped_column(Pct6)

    #: Free-form rather than a CHECK: OrderStatus is the most likely vocabulary
    #: to gain a value, and a migration per broker quirk is not worth it.
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    filled_quantity: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    average_price: Mapped[Decimal | None] = mapped_column(Money)
    intent: Mapped[str] = mapped_column(_enum(OrderIntent, "order_intent_enum"), nullable=False)
    placed_at: Mapped[dt.datetime] = mapped_column(Ts, nullable=False)
    last_update_at: Mapped[dt.datetime] = mapped_column(Ts, nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("client_order_id", name="uq_client_order"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("filled_quantity >= 0", name="filled_non_negative"),
        # BR-12: catches a broker-response parsing bug before it corrupts
        # position sizing.
        CheckConstraint("filled_quantity <= quantity", name="fill_not_over"),
        Index("ix_orders_broker_id", "broker_order_id"),
        Index("ix_orders_correlation", "correlation_id"),
    )


# Reconciliation scans only live orders, never the whole history — and the
# whole history is the part that grows.
Index(
    "ix_orders_open",
    Order.status,
    postgresql_where=text("status NOT IN ('FILLED', 'CANCELLED', 'REJECTED')"),
)


class OrderFill(Base):
    """One fill of an order, with charges itemised (BR-13).

    Separate table because an order can fill in parts, and separate *columns*
    per charge because both tax computation and contract-note reconciliation
    need the breakdown — a single ``charges`` total cannot be reconciled
    against anything.
    """

    __tablename__ = "order_fills"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("orders.id"), nullable=False)
    fill_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    fill_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    filled_at: Mapped[dt.datetime] = mapped_column(Ts, nullable=False)

    brokerage: Mapped[Decimal] = mapped_column(Charge, nullable=False, server_default="0")
    stt: Mapped[Decimal] = mapped_column(Charge, nullable=False, server_default="0")
    exchange_charges: Mapped[Decimal] = mapped_column(Charge, nullable=False, server_default="0")
    gst: Mapped[Decimal] = mapped_column(Charge, nullable=False, server_default="0")
    sebi_charges: Mapped[Decimal] = mapped_column(Charge, nullable=False, server_default="0")
    stamp_duty: Mapped[Decimal] = mapped_column(Charge, nullable=False, server_default="0")

    __table_args__ = (
        CheckConstraint("fill_qty > 0", name="fill_qty_positive"),
        Index("ix_fills_order", "order_id"),
    )


class Position(Base):
    """An open or closed position.

    Two things here are non-negotiable:

    - ``stop_price`` is ``NOT NULL`` (BR-1). There is no way to represent a
      position without a protective stop, deliberately.
    - ``squareoff_deadline`` is ``NOT NULL`` (BR-7). Without it a position gets
      force-closed by the broker at whatever price the auction produces.
    """

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    correlation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    symbol_id: Mapped[int] = mapped_column(Integer, ForeignKey("instruments.id"), nullable=False)
    strategy_id: Mapped[str | None] = mapped_column(String(64))
    slot_index: Mapped[int] = mapped_column(Integer, nullable=False)
    direction: Mapped[str] = mapped_column(
        _enum(Direction, "position_direction_enum"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    #: BR-1 — NEVER NULL.
    stop_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    target_price: Mapped[Decimal | None] = mapped_column(Money)
    opened_at: Mapped[dt.datetime] = mapped_column(Ts, nullable=False)
    #: BR-7 — per stock, not global: 15:10 CAS / 15:20 non-CAS / 15:25 F&O.
    squareoff_deadline: Mapped[dt.datetime] = mapped_column(Ts, nullable=False)

    status: Mapped[str] = mapped_column(
        _enum(PositionStatus, "position_status_enum"), nullable=False
    )
    closed_at: Mapped[dt.datetime | None] = mapped_column(Ts)
    exit_price: Mapped[Decimal | None] = mapped_column(Money)
    exit_reason: Mapped[str | None] = mapped_column(String(24))
    realized_pnl: Mapped[Decimal | None] = mapped_column(Pnl)
    #: Full names, matching the Pydantic model. The spec abbreviated these for
    #: no reason and the mismatch would have cost someone an afternoon.
    max_favourable_excursion: Mapped[Decimal | None] = mapped_column(Money)
    max_adverse_excursion: Mapped[Decimal | None] = mapped_column(Money)

    __table_args__ = (
        CheckConstraint("slot_index >= 0", name="slot_non_negative"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("entry_price > 0", name="entry_price_positive"),
        CheckConstraint("stop_price > 0", name="stop_price_positive"),
        # A CLOSED position that cannot say when, at what price, or why is an
        # unauditable hole in the trade record.
        CheckConstraint(
            "status <> 'CLOSED' OR "
            "(closed_at IS NOT NULL AND exit_price IS NOT NULL AND exit_reason IS NOT NULL)",
            name="closed_complete",
        ),
    )


# The partial unique indexes are defined outside __table_args__ because they
# need column expressions. These two ARE the slot-discipline guarantee: the
# Redis lock is the fast path, but a lock can be lost and an index cannot.
Index(
    "uq_open_slot",
    Position.slot_index,
    unique=True,
    postgresql_where=text("status = 'OPEN'"),
)
Index(
    "uq_open_symbol",
    Position.symbol_id,
    unique=True,
    postgresql_where=text("status = 'OPEN'"),
)
Index(
    "ix_positions_open",
    Position.status,
    postgresql_where=text("status <> 'CLOSED'"),
)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class DecisionLog(Base):
    """Append-only, hash-chained decision record. **Hypertable**.

    BR-3: this table is append-only, and that is enforced by the *absence of an
    UPDATE/DELETE grant* on the application role — not by application
    convention. See the role-grants migration. An application-level promise can
    be broken by a bug; a missing grant cannot.

    The hash chain itself (``prev_hash``/``row_hash``) is written by E01-S05,
    deferred to Sprint 2. The columns exist now so that the chain can be added
    without a table rewrite.
    """

    __tablename__ = "decision_log"

    #: Not the primary key — a hypertable's PK must contain the partitioning
    #: column, so the PK is (ts, seq). This stays for human reference.
    #:
    #: ``Identity()`` is REQUIRED here and is not decoration. SQLAlchemy only
    #: attaches an implicit sequence to an autoincrementing *primary key*; on a
    #: non-PK column ``autoincrement=True`` is silently ignored, leaving a NOT
    #: NULL column with no default. Every INSERT then fails with "null value in
    #: column id violates not-null constraint" — which is exactly what happened
    #: the first time this table was written to.
    id: Mapped[int] = mapped_column(BigInteger, sa.Identity(always=False), nullable=False)
    ts: Mapped[dt.datetime] = mapped_column(Ts, primary_key=True, server_default=func.now())
    #: Chain ordering, assigned under an advisory lock (§11.2).
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    correlation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    stage: Mapped[str] = mapped_column(String(28), nullable=False)
    symbol_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("instruments.id"))
    outcome: Mapped[str] = mapped_column(String(12), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(48))
    payload: Mapped[JsonDict] = mapped_column(JSONB, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    service: Mapped[str] = mapped_column(String(24), nullable=False)

    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    row_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        # "Trace one trade end to end" — the audit explorer's main query.
        Index("ix_audit_correlation", "correlation_id", "ts"),
    )


# "Why isn't it trading?" is the highest-value debugging query in the system.
Index(
    "ix_audit_reason",
    text("reason_code"),
    text("ts DESC"),
    postgresql_where=text("outcome = 'REJECT'"),
)


# ---------------------------------------------------------------------------
# Strategy registry
# ---------------------------------------------------------------------------


class Strategy(Base):
    """A strategy definition — declarative data, never code.

    BR-5 is the constraint that matters: a strategy cannot reach ``ACTIVE``
    without a recorded human approval, and that is a CHECK rather than an
    application rule so that a bad UPDATE cannot promote one either.
    """

    __tablename__ = "strategy"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    parent_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("strategy.id"))
    origin: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)

    dsl: Mapped[JsonDict] = mapped_column(JSONB, nullable=False)
    dsl_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hypothesis: Mapped[JsonDict] = mapped_column(JSONB, nullable=False)
    #: Frozen BEFORE any backtest runs. This ordering is what stops a
    #: hypothesis being written to fit results already seen.
    hypothesis_frozen_at: Mapped[dt.datetime] = mapped_column(Ts, nullable=False)
    applicable_regimes: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(Ts, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[dt.datetime | None] = mapped_column(Ts)
    state_changed_at: Mapped[dt.datetime] = mapped_column(Ts, nullable=False)
    retirement_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # BR-5: the human approval gate, enforced by the database.
        CheckConstraint(
            "state <> 'ACTIVE' OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)",
            name="active_needs_approval",
        ),
    )


Index(
    "ix_strategy_state",
    Strategy.state,
    postgresql_where=text("state IN ('ACTIVE', 'DEGRADED', 'SHADOW', 'PAPER')"),
)


class StrategyTrial(Base):
    """Every backtest ever run, including parameter sweeps. Append-only (BR-4).

    Deleting a failed trial corrupts the Deflated Sharpe denominator and
    inflates every future validation — which is why the application role has
    INSERT but no DELETE. There is deliberately **no foreign key** to
    ``strategy``: a trial can exist for a candidate that was never registered,
    and those trials still count toward the denominator.
    """

    __tablename__ = "strategy_trial"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    trial_ts: Mapped[dt.datetime] = mapped_column(Ts, nullable=False, server_default=func.now())
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    origin: Mapped[str] = mapped_column(String(32), nullable=False)
    generation_batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    observed_sharpe: Mapped[Decimal | None] = mapped_column(Stat)
    deflated_sharpe: Mapped[Decimal | None] = mapped_column(Stat)
    pbo: Mapped[Decimal | None] = mapped_column(Prob)
    trade_count: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(12), nullable=False)
    failed_check: Mapped[str | None] = mapped_column(String(8))
    report: Mapped[JsonDict] = mapped_column(JSONB, nullable=False)

    __table_args__ = (Index("ix_trial_hash", "strategy_hash"),)


#: Newest trials first — the DSR count and the "what did I just run" view.
Index("ix_trial_ts", text("trial_ts DESC"))


class StrategyValidation(Base):
    """One run of the validation gauntlet."""

    __tablename__ = "strategy_validation"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(64), ForeignKey("strategy.id"), nullable=False)
    run_at: Mapped[dt.datetime] = mapped_column(Ts, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    checks: Mapped[JsonDict] = mapped_column(JSONB, nullable=False)

    observed_sharpe: Mapped[Decimal | None] = mapped_column(Stat)
    deflated_sharpe: Mapped[Decimal | None] = mapped_column(Stat)
    pbo: Mapped[Decimal | None] = mapped_column(Prob)
    #: The DSR denominator at the moment of the run, recorded so the result
    #: stays interpretable after more trials accumulate.
    trial_count_at_run: Mapped[int] = mapped_column(Integer, nullable=False)
    trade_count: Mapped[int | None] = mapped_column(Integer)
    max_drawdown_pct: Mapped[Decimal | None] = mapped_column(Pct6)
    regimes_covered: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    holdout_result: Mapped[JsonDict | None] = mapped_column(JSONB)
    equity_curve: Mapped[JsonDict | None] = mapped_column(JSONB)


class StrategyPerformance(Base):
    """Daily realised performance per strategy."""

    __tablename__ = "strategy_performance"

    strategy_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("strategy.id"), primary_key=True
    )
    as_of: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    trades: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    wins: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    realized_pnl: Mapped[Decimal | None] = mapped_column(Pnl)
    avg_r: Mapped[Decimal | None] = mapped_column(Pct6)
    realized_sharpe: Mapped[Decimal | None] = mapped_column(Stat)
    #: Realised vs backtested performance — the key degradation signal.
    vs_backtest_ratio: Mapped[Decimal | None] = mapped_column(Pct6)


class ShadowSignal(Base):
    """A signal a shadow-mode strategy would have taken, and what happened."""

    __tablename__ = "shadow_signal"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(64), ForeignKey("strategy.id"), nullable=False)
    symbol_id: Mapped[int] = mapped_column(Integer, ForeignKey("instruments.id"), nullable=False)
    signalled_at: Mapped[dt.datetime] = mapped_column(Ts, nullable=False)
    direction: Mapped[str] = mapped_column(
        _enum(Direction, "shadow_direction_enum"), nullable=False
    )
    price_at_signal: Mapped[Decimal] = mapped_column(Money, nullable=False)
    hypothetical_stop: Mapped[Decimal] = mapped_column(Money, nullable=False)
    hypothetical_outcome: Mapped[JsonDict | None] = mapped_column(JSONB)


# ---------------------------------------------------------------------------
# Trade journal
# ---------------------------------------------------------------------------


class TradeJournal(Base):
    """Per-trade qualitative record — the input to strategy review."""

    __tablename__ = "trade_journal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("positions.id"))
    trade_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    setup_type: Mapped[str] = mapped_column(String(32), nullable=False)
    strategy_id: Mapped[str | None] = mapped_column(String(64))
    market_regime: Mapped[str] = mapped_column(String(20), nullable=False)
    ai_confidence: Mapped[Decimal | None] = mapped_column(Prob)
    outcome: Mapped[str] = mapped_column(String(8), nullable=False)
    r_multiple: Mapped[Decimal | None] = mapped_column(Pct6)
    thesis_held: Mapped[bool | None] = mapped_column(Boolean)
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_journal_setup", "setup_type", "market_regime"),)


Index("ix_journal_date", text("trade_date DESC"))


#: Tables the application role must be able to modify. The two that are
#: deliberately absent — ``decision_log`` and ``strategy_trial`` — are what
#: BR-3 and BR-4 come down to.
MUTABLE_TABLES: tuple[str, ...] = (
    "instruments",
    "instrument_daily_status",
    "ohlcv",
    "daily_plan",
    "plan_candidate",
    "orders",
    "order_fills",
    "positions",
    "trade_journal",
    "strategy",
    "strategy_validation",
    "strategy_performance",
    "shadow_signal",
)

#: Append-only tables: SELECT and INSERT only, no UPDATE, no DELETE.
APPEND_ONLY_TABLES: tuple[str, ...] = ("decision_log", "strategy_trial")

#: Tables that become TimescaleDB hypertables, with their chunk intervals.
HYPERTABLES: dict[str, str] = {
    "ohlcv": "7 days",
    "decision_log": "30 days",
}
