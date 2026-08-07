# EPIC 01 — PERSISTENCE & DATA LAYER
## Detailed Technical Specification for Development

**Epic:** E01 · **Phase:** 1 · **Sprint:** 1 (Active, 06–10 Aug 2026)
**Stories:** 6 · **Estimate:** 8.5 days · **Status:** New → ready for development
**Tracker:** `BACKLOG_Tracker.xlsx` → Backlog sheet, rows E01-S01..S06

**Read first:** [MASTER_REFERENCE.md](MASTER_REFERENCE.md) §10 (data architecture),
[LOW_LEVEL_ARCHITECTURE.md](LOW_LEVEL_ARCHITECTURE.md) §4 (schemas)

---

## TABLE OF CONTENTS

| § | Section |
|---|---|
| 1 | Why This Epic Exists — Business Context |
| 2 | ⚠️ Corrections to the Design Before You Start |
| 3 | Business Rules the Schema Must Enforce |
| 4 | Volume & Capacity Analysis |
| 5 | Technology Decisions |
| 6 | Complete Data Model |
| 7 | Indexing Strategy |
| 8 | Transaction & Consistency Design |
| 9 | Redis Keyspace Specification |
| 10 | Event Stream Design |
| 11 | Audit Hash Chain Design |
| 12 | Story-by-Story Implementation Guide |
| 13 | Testing Requirements |
| 14 | Failure Modes |
| 15 | Definition of Done |
| 16 | Sprint 1 Sequencing |
| 17 | Open Questions |

---

## 1. WHY THIS EPIC EXISTS — BUSINESS CONTEXT

### 1.1 What this layer serves

Every other epic reads or writes through this one. It is the substrate, and its
defects propagate everywhere:

| Consumer | Needs from this layer | Consequence of failure |
|---|---|---|
| **Pre-market job** (E11) | 3 years of multi-timeframe history for ~200 symbols, read every morning in a 45-minute window | Cannot produce a plan; the day is lost |
| **Indicator engine** (E06) | Warm-up lookback per symbol per timeframe; fast bulk reads | Indicators trade on incomplete data |
| **Signal engine** (E13) | Current plan, indicator snapshots (Redis), strategy registry | Signals fire on stale or wrong state |
| **Risk engine** (E14) | Open positions, daily P&L, slot occupancy — all sub-millisecond | Over-allocation, breached limits |
| **Execution** (E15) | Order idempotency keys, position records, reconciliation state | **Duplicate orders, naked positions** |
| **Strategy engine** (E12) | Trial registry with an honest count | Deflated Sharpe becomes meaningless |
| **Compliance/tax** (E21) | Charge-level fills, immutable audit | Cannot substantiate a trade or file correctly |
| **Dashboard** (E17) | Live state reads at 4 Hz | UI shows stale data during volatility |

### 1.2 The business processes this layer must support

Five distinct access patterns, with genuinely different requirements. Designing
for one and assuming the others follow is the main way this epic goes wrong.

| # | Process | Pattern | Volume | Latency budget |
|---|---|---|---|---|
| **BP-1** | Nightly historical sync | Bulk write | ~200 rows/day EOD; 24M rows on backfill | Minutes (off-hours) |
| **BP-2** | Pre-market analysis read | Bulk read, wide | ~24M rows scanned across 150 symbols × 3 timeframes | **45 min hard deadline** |
| **BP-3** | Live tick/bar write | Small, continuous | ~4k bars/day + ~50k ticks/day | < 10 ms per write |
| **BP-4** | Trading-decision state | Read-mostly, hot | Every 5-min cycle × 8 symbols | **< 1 ms** (Redis) |
| **BP-5** | Audit append | Write-only, append | ~2–5k rows/day | < 50 ms, must not block trading |

**The critical insight:** BP-2 and BP-4 have incompatible requirements. BP-2 is
an analytical scan over millions of rows; BP-4 is a point lookup that must
complete inside a trading cycle. This is why the design splits Postgres
(analytical, durable) from Redis (hot state) rather than trying to serve both
from one store.

### 1.3 What "done" means for the business

At the end of this epic:

- A developer can run `make migrate` on an empty database and get a complete,
  correct schema.
- Historical data can be written in bulk and read fast enough for the pre-market
  deadline.
- Hot trading state lives in Redis with typed access and no key-name drift.
- Events survive a consumer restart.
- Every decision is recorded in a way that tampering is detectable.

Nothing trades. This epic produces no trading behaviour — it produces the
foundation that makes trading behaviour possible and auditable.

---

## 2. ⚠️ CORRECTIONS TO THE DESIGN BEFORE YOU START

Two items in the existing design documents are wrong or outdated. Fix them
during this epic rather than discovering them mid-implementation.

### 2.1 TimescaleDB version pin — a decision to make before the first migration

**Checked against the actual pin:** `ops/docker-compose.yml` specifies
`timescale/timescaledb:2.17.2-pg16`.

That matters, because TimescaleDB **2.18** introduced *hypercore* (a unified
rowstore/columnstore engine) and renamed the compression API:

| API | 2.17.2 (current pin) | 2.18+ |
|---|---|---|
| Enable | `ALTER TABLE … SET (timescaledb.compress, …)` | `ALTER TABLE … SET (timescaledb.enable_columnstore = true, …)` |
| Policy | `add_compression_policy(…)` | `add_columnstore_policy(…)` |
| Hypertable | `by_range()` available (since 2.13) | same |

**So `LOW_LEVEL_ARCHITECTURE.md §4.2` is correct as written for the pinned
version.** I initially flagged it as outdated; that was wrong — the doc matches
2.17.2, and `add_columnstore_policy` does not exist there.

**The real decision: upgrade the pin, or stay?**

**Recommendation: upgrade the pin to a current 2.x before writing the first
migration.** The reasoning is that this is greenfield — there is no data and no
deployed schema, so the upgrade costs nothing today. Doing it after the store
holds 24 M rows means planning a migration for a benefit you could have had for
free. Hypercore is also the direction the product is going, so staying on the
older compression API means writing migrations you will later rewrite.

**If you upgrade**, use the columnstore form:

```sql
ALTER TABLE ohlcv SET (
    timescaledb.enable_columnstore = true,
    timescaledb.segmentby = 'symbol_id, timeframe',
    timescaledb.orderby   = 'ts DESC'
);
SELECT add_columnstore_policy('ohlcv', after => INTERVAL '90 days');
```

**If you stay on 2.17.2**, use the legacy form:

```sql
ALTER TABLE ohlcv SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol_id, timeframe',
    timescaledb.compress_orderby   = 'ts DESC'
);
SELECT add_compression_policy('ohlcv', INTERVAL '90 days');
```

**Either way, confirm before writing the migration.** Thirty seconds in psql
settles it:

```sql
SELECT extversion FROM pg_extension WHERE extname = 'timescaledb';
\df add_columnstore_policy
```

**Action:** decide the pin in E01-S01 task 1, before task 4. Record the decision
and the version in the migration's docstring so the next reader knows which API
family the file belongs to. The DDL in §6 of this document uses the **2.18+
columnstore form**, on the assumption the pin is upgraded — adjust if not.

### 2.2 Driver choice needs deciding, and it is not neutral

`pyproject.toml` currently specifies `psycopg[binary,pool]>=3.2`. The research
consensus favours `asyncpg` for raw async throughput. Both work with
SQLAlchemy 2.0.

| | psycopg3 | asyncpg |
|---|---|---|
| Async support | Native | Native |
| **Sync + async from one driver** | ✅ Yes | ❌ No — needs psycopg2 for Alembic |
| Raw throughput | Good | ~20–30% faster |
| COPY support (bulk insert) | ✅ Excellent | ✅ Good |
| SQLAlchemy URL | `postgresql+psycopg://` | `postgresql+asyncpg://` |

**Checked:** `pyproject.toml` already specifies `psycopg[binary,pool]>=3.2`, with
`sqlalchemy>=2.0` and `alembic>=1.13`. No change needed.

**Recommendation: keep psycopg3.** The throughput difference is irrelevant here
— BP-3 writes a few thousand rows a day, and BP-1's bulk path uses `COPY`
either way, where psycopg3 is excellent. The decisive factor is that psycopg3
serves both the async application and synchronous Alembic migrations with one
driver, removing a whole class of "works in the app, fails in the migration"
problems.

**Action:** record this as a decision in E01-S01. Do not leave it implicit.

---

## 3. BUSINESS RULES THE SCHEMA MUST ENFORCE

This is the "verify the business logic" part. Each rule below is a business
invariant that **must be enforced by the database**, not merely by application
code — because application code can be bypassed by a migration, a manual fix, or
a bug, and the database is the last line.

| # | Business rule | Enforcement | Why at DB level |
|---|---|---|---|
| **BR-1** | A position must always have a protective stop | `stop_price NUMERIC NOT NULL` | A naked position is the single most expensive failure. Nothing may create one, ever. |
| **BR-2** | The same trading decision must produce at most one order | `UNIQUE (client_order_id)` | This is what makes query-don't-retry safe. Without the constraint, a race can still double-insert. |
| **BR-3** | Audit entries can never be modified or deleted | Role has `INSERT` only; no `UPDATE`/`DELETE` grant | Application-level append-only is a convention; a role grant is a guarantee. |
| **BR-4** | Strategy trials can never be deleted | Role has `INSERT` only | Deleting failed trials corrupts the Deflated Sharpe denominator and inflates every future validation. |
| **BR-5** | A strategy cannot be ACTIVE without recorded human approval | `CHECK (state <> 'ACTIVE' OR approved_by IS NOT NULL)` | The human approval gate must be impossible to bypass, including by a bad UPDATE. |
| **BR-6** | No duplicate bars for the same symbol/timeframe/time | `PRIMARY KEY (symbol_id, timeframe, ts)` | A duplicate bar silently double-counts volume and corrupts indicators. |
| **BR-7** | Every position has a square-off deadline | `squareoff_deadline TIMESTAMPTZ NOT NULL` | A position without a deadline will be force-closed by the broker at an arbitrary price. |
| **BR-8** | Hazard flags are per symbol per day | `PRIMARY KEY (symbol_id, trade_date)` | ASM/T2T/ban status changes daily; a single current-state row would lose history and break backtests. |
| **BR-9** | All timestamps are timezone-aware | `TIMESTAMPTZ` everywhere, never `TIMESTAMP` | Naive timestamps in a market with a fixed session are a recurring, silent bug class. |
| **BR-10** | All money and prices are exact | `NUMERIC(14,4)`, never `float8` | Float error accumulates in P&L and breaks tick-size comparison. |
| **BR-11** | An order belongs to exactly one decision | `correlation_id UUID NOT NULL` + FK-ish discipline | Every trade must be traceable end to end for audit and tax. |
| **BR-12** | Filled quantity never exceeds ordered quantity | `CHECK (filled_quantity <= quantity)` | Catches a broker-response parsing bug before it corrupts position sizing. |
| **BR-13** | Charges are recorded per fill, itemised | Separate columns, not a single `charges` total | Tax computation and contract-note reconciliation both need the breakdown. |
| **BR-14** | A plan is immutable once locked | `locked_at TIMESTAMPTZ`; application refuses writes after | The plan is the session's contract; mid-session mutation makes decisions unexplainable. |

### 3.1 Rules deliberately NOT enforced at DB level

| Rule | Why application-level is correct |
|---|---|
| Stop on the correct side of entry (long stop < entry) | Direction-dependent; a CHECK would be awkward and the Pydantic model already enforces it at construction |
| Position size within risk limits | Depends on live margin and config, neither of which the DB knows |
| Order rate limiting | Time-window logic; belongs in the broker adapter's token bucket |

---

## 4. VOLUME & CAPACITY ANALYSIS

Sizing this correctly matters because it determines whether BP-2 makes its
45-minute deadline, and whether Redis stays inside its memory cap.

### 4.1 PostgreSQL — the `ohlcv` table dominates

Assumptions: Nifty 200 base universe · 250 trading days/year · session
09:15–15:30 = 375 minutes.

| Timeframe | Bars/symbol/day | Retention | Rows (200 symbols) |
|---|---|---|---|
| 1m | 375 | 1 year | 18,750,000 |
| 5m | 75 | 1 year | 3,750,000 |
| 15m | 25 | 1 year | 1,250,000 |
| 1h | ~6 | 2 years | 600,000 |
| 1d | 1 | 3 years | 150,000 |
| 1w | 0.2 | 3 years | 31,200 |
| | | **Total** | **~24.5 M rows** |

Row width ≈ 100 bytes including tuple overhead → **~2.45 GB uncompressed**.
With columnstore compression (10–20× on time-series with repeated
`symbol_id`/`timeframe` segments) → **~150–250 MB**.

**Daily growth in live operation** is trivial by comparison: ~200 EOD rows plus
~4,000 intraday bars for the watchlist ≈ **400 KB/day**.

### 4.2 The table that actually grows — `decision_log`

| Source | Rows/day |
|---|---|
| Signal evaluations (8 symbols × 75 cycles × 3 strategies) | ~1,800 |
| AI reviews (only triggers that fired) | ~10–40 |
| Risk decisions | ~10–40 |
| Order/fill/exit events | ~50–150 |
| Reconciliation drift | ~0–10 |
| **Total** | **~2,000–3,000** |

At ~500 bytes (JSONB payload) → **~1.5 MB/day ≈ 375 MB/year**. This becomes the
largest table after two years. Make it a hypertable with a compression policy
from day one, not retrofitted later.

### 4.3 Redis — and the trap

| Key class | Count | Size each | Total |
|---|---|---|---|
| `state:indicator:*` (pre-market peak: 150 symbols × 3 tf) | 450 | ~600 B | 270 KB |
| `state:indicator:*` (session: 8 × 6) | 48 | ~600 B | 29 KB |
| `state:quote:*` | 8 | ~200 B | 2 KB |
| `state:position:*` | ≤5 | ~400 B | 2 KB |
| `plan:*` + candidates | ~20 | ~2 KB | 40 KB |
| `context:market` | 1 | ~4 KB | 4 KB |
| **State subtotal** | | | **< 1 MB** |

**⚠️ The trap: streams are unbounded by default.** `redis.conf` sets
`maxmemory 2gb` with `maxmemory-policy noeviction` — correct, because we must
never have trading state silently evicted. But that means **an untrimmed stream
will eventually OOM Redis and stop the entire system.**

`stream:ticks` at ~50,000 entries/day × ~200 bytes = 10 MB/day. Left untrimmed
for a month that is 300 MB; a year, 3.6 GB — past the cap.

**Mandatory: every `XADD` uses `MAXLEN ~`.** Approximate trimming (`~`) is far
cheaper than exact and is entirely adequate here.

| Stream | MAXLEN | Rationale |
|---|---|---|
| `stream:ticks` | 100,000 | ~2 hours of ticks; archival is the durable path |
| `stream:bars:*` | 10,000 | Several sessions of context |
| `stream:signals` | 10,000 | Low volume, keep generously |
| `stream:orders` | 10,000 | Low volume |
| `stream:audit` | 50,000 | Drained to Postgres continuously |

**Action:** make `maxlen` a required argument in the `EventStream.publish`
signature (E01-S04). A stream that can be published to without a bound should
not be constructible.

### 4.4 Disk budget

| Component | Year 1 | Year 3 |
|---|---|---|
| `ohlcv` compressed | ~250 MB | ~500 MB |
| `decision_log` compressed | ~100 MB | ~300 MB |
| Orders/positions/journal | ~10 MB | ~30 MB |
| Strategy tables | ~50 MB | ~200 MB |
| WAL + indexes + bloat | ~500 MB | ~1 GB |
| Tick archive (Parquet) | ~1 GB | ~3 GB |
| **Total** | **~2 GB** | **~5 GB** |

The 100 GB VPS specification is comfortable. No storage concern.

---

## 5. TECHNOLOGY DECISIONS

| Decision | Choice | Rationale |
|---|---|---|
| **DB driver** | `psycopg3` | One driver for sync migrations and async app (§2.2) |
| **ORM** | SQLAlchemy 2.0 declarative + `AsyncSession` | Typed models; `async_sessionmaker` for scoped sessions |
| **Migrations** | Alembic, async template (`alembic init -t async`) | Official async support; `op.get_bind()` inside migrations |
| **Bulk insert** | `COPY` via psycopg3 | Orders of magnitude faster than `executemany` for the 24 M-row backfill |
| **Hypertables** | `ohlcv`, `decision_log` | Both are time-ordered append with age-based access decay |
| **Chunk interval** | 7 days (`ohlcv`), 30 days (`decision_log`) | Target: chunk fits comfortably in memory; ~470 k rows/chunk for ohlcv |
| **Compression** | Columnstore, segment by `(symbol_id, timeframe)` | Segmenting on the query predicate is what makes compressed scans fast |
| **Redis client** | `redis-py` async with connection pool | Mature, async-native |
| **Serialisation** | Pydantic `model_dump_json()` | Same models as the rest of the system; no second schema |

### 5.1 Why hypertables for only two tables

Hypertables add operational complexity (chunk management, compression policies,
constraints on unique indexes). They pay for themselves when a table is large,
time-ordered, and queried by time range. `ohlcv` and `decision_log` are.
`orders`, `positions`, and the strategy tables are small and queried by ID —
plain tables are correct there.

---

## 6. COMPLETE DATA MODEL

### 6.1 Reference & instrument data

```sql
-- ---------------------------------------------------------------------------
-- instruments: the symbol master. Refreshed daily from the broker.
-- Plain table - small, queried by ID.
-- ---------------------------------------------------------------------------
CREATE TABLE instruments (
    id              SERIAL PRIMARY KEY,
    tradingsymbol   VARCHAR(64)   NOT NULL,
    exchange        VARCHAR(8)    NOT NULL,
    broker_token    VARCHAR(32)   NOT NULL,
    isin            VARCHAR(12),
    name            VARCHAR(128),
    lot_size        INTEGER       NOT NULL DEFAULT 1  CHECK (lot_size >= 1),
    tick_size       NUMERIC(8,4)  NOT NULL            CHECK (tick_size > 0),
    sector          VARCHAR(64),
    is_active       BOOLEAN       NOT NULL DEFAULT TRUE,
    first_seen      DATE          NOT NULL DEFAULT CURRENT_DATE,
    last_seen       DATE          NOT NULL DEFAULT CURRENT_DATE,
    CONSTRAINT uq_instrument UNIQUE (exchange, tradingsymbol)
);
CREATE INDEX ix_instruments_token  ON instruments (broker_token);
CREATE INDEX ix_instruments_sector ON instruments (sector) WHERE is_active;

-- ---------------------------------------------------------------------------
-- instrument_daily_status: the India hazard flags, per symbol PER DAY (BR-8).
-- History matters: a backtest must know whether a symbol was in ASM on the
-- day being simulated, not whether it is today.
-- ---------------------------------------------------------------------------
CREATE TABLE instrument_daily_status (
    symbol_id           INTEGER      NOT NULL REFERENCES instruments(id),
    trade_date          DATE         NOT NULL,
    is_t2t              BOOLEAN      NOT NULL DEFAULT FALSE,
    is_asm              BOOLEAN      NOT NULL DEFAULT FALSE,
    is_gsm              BOOLEAN      NOT NULL DEFAULT FALSE,
    is_fno_ban          BOOLEAN      NOT NULL DEFAULT FALSE,
    is_cas_stock        BOOLEAN      NOT NULL DEFAULT FALSE,   -- drives deadline
    circuit_band_pct    NUMERIC(5,2),
    upper_circuit       NUMERIC(14,4),
    lower_circuit       NUMERIC(14,4),
    has_earnings_today  BOOLEAN      NOT NULL DEFAULT FALSE,
    prev_close          NUMERIC(14,4),
    fetched_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol_id, trade_date)
);
CREATE INDEX ix_ids_date ON instrument_daily_status (trade_date);

-- Convenience view: today's eligible universe, hazards applied.
CREATE VIEW v_eligible_today AS
SELECT i.*
FROM instruments i
JOIN instrument_daily_status s
  ON s.symbol_id = i.id AND s.trade_date = CURRENT_DATE
WHERE i.is_active
  AND NOT s.is_t2t AND NOT s.is_asm AND NOT s.is_gsm;
```

### 6.2 Market data

```sql
-- ---------------------------------------------------------------------------
-- ohlcv: the bar store. HYPERTABLE.
-- ts is the bar OPEN time, UTC, aligned to the 09:15 session start.
-- ---------------------------------------------------------------------------
CREATE TABLE ohlcv (
    symbol_id     INTEGER       NOT NULL REFERENCES instruments(id),
    timeframe     VARCHAR(4)    NOT NULL,
    ts            TIMESTAMPTZ   NOT NULL,
    open          NUMERIC(14,4) NOT NULL CHECK (open  > 0),
    high          NUMERIC(14,4) NOT NULL CHECK (high  > 0),
    low           NUMERIC(14,4) NOT NULL CHECK (low   > 0),
    close         NUMERIC(14,4) NOT NULL CHECK (close > 0),
    volume        BIGINT        NOT NULL CHECK (volume >= 0),
    trade_count   INTEGER       CHECK (trade_count IS NULL OR trade_count >= 0),
    vwap          NUMERIC(14,4),
    is_adjusted   BOOLEAN       NOT NULL DEFAULT FALSE,
    synthetic     BOOLEAN       NOT NULL DEFAULT FALSE,

    -- BR-6: no duplicate bars
    PRIMARY KEY (symbol_id, timeframe, ts),

    -- OHLC coherence at the DB level. The application enforces this too
    -- (models/market.py), but a bad backfill script bypasses the model.
    CONSTRAINT ck_ohlc_coherent CHECK (
        high >= low AND high >= open AND high >= close
                    AND low  <= open AND low  <= close
    )
);

SELECT create_hypertable('ohlcv', by_range('ts', INTERVAL '7 days'));

ALTER TABLE ohlcv SET (
    timescaledb.enable_columnstore = true,
    timescaledb.segmentby = 'symbol_id, timeframe',
    timescaledb.orderby   = 'ts DESC'
);
SELECT add_columnstore_policy('ohlcv', after => INTERVAL '90 days');
```

> **Note on the CHECK constraint:** this is the same invariant that was inert in
> the Pydantic model until the second audit (a field validator could not see
> fields declared after it). Enforcing it in both places is deliberate —
> defence in depth for the one data-quality rule whose violation silently
> corrupts every downstream indicator.

### 6.3 Daily plan

```sql
CREATE TABLE daily_plan (
    id                SERIAL PRIMARY KEY,
    trade_date        DATE         NOT NULL UNIQUE,
    generated_at      TIMESTAMPTZ  NOT NULL,
    locked_at         TIMESTAMPTZ,                    -- BR-14: immutable after
    config_hash       VARCHAR(32)  NOT NULL,          -- traces trades to config
    market_thesis     JSONB        NOT NULL,
    macro_snapshot    JSONB        NOT NULL,
    model_used        VARCHAR(64),
    ai_available      BOOLEAN      NOT NULL DEFAULT TRUE,  -- false = score-only day
    input_tokens      INTEGER,
    output_tokens     INTEGER,
    cache_read_tokens INTEGER,
    generation_ms     INTEGER
);

CREATE TABLE plan_candidate (
    id                 SERIAL PRIMARY KEY,
    plan_id            INTEGER      NOT NULL REFERENCES daily_plan(id) ON DELETE CASCADE,
    symbol_id          INTEGER      NOT NULL REFERENCES instruments(id),
    rank               INTEGER      NOT NULL,
    tradeability_score NUMERIC(5,2) NOT NULL CHECK (tradeability_score BETWEEN 0 AND 100),
    score_breakdown    JSONB        NOT NULL,     -- per-component, for explainability
    direction_bias     VARCHAR(8)   NOT NULL,
    ai_confidence      NUMERIC(4,3) CHECK (ai_confidence IS NULL
                                           OR ai_confidence BETWEEN 0 AND 1),
    ai_rationale       TEXT,
    playbook           JSONB,
    gap_pct            NUMERIC(6,3),              -- filled at 09:02
    status             VARCHAR(20)  NOT NULL,     -- ACTIVE|GAP_INVALIDATED|AI_VETOED|TRADED
    CONSTRAINT uq_plan_symbol UNIQUE (plan_id, symbol_id)
);
CREATE INDEX ix_candidate_plan_rank ON plan_candidate (plan_id, rank);
```

### 6.4 Orders & positions

```sql
CREATE TABLE orders (
    id                BIGSERIAL PRIMARY KEY,
    client_order_id   VARCHAR(64)  NOT NULL,          -- BR-2: our idempotency key
    broker_order_id   VARCHAR(64),
    algo_id           VARCHAR(64),                    -- SEBI
    correlation_id    UUID         NOT NULL,          -- BR-11
    symbol_id         INTEGER      NOT NULL REFERENCES instruments(id),
    side              VARCHAR(4)   NOT NULL,
    order_type        VARCHAR(8)   NOT NULL,
    product           VARCHAR(8)   NOT NULL,
    quantity          INTEGER      NOT NULL CHECK (quantity > 0),
    limit_price       NUMERIC(14,4),
    trigger_price     NUMERIC(14,4),
    market_protection NUMERIC(6,2),                   -- C9: MARKET/SL-M require it
    status            VARCHAR(24)  NOT NULL,
    filled_quantity   INTEGER      NOT NULL DEFAULT 0 CHECK (filled_quantity >= 0),
    average_price     NUMERIC(14,4),
    intent            VARCHAR(12)  NOT NULL,
    placed_at         TIMESTAMPTZ  NOT NULL,
    last_update_at    TIMESTAMPTZ  NOT NULL,
    rejection_reason  TEXT,

    CONSTRAINT uq_client_order UNIQUE (client_order_id),
    CONSTRAINT ck_fill_not_over CHECK (filled_quantity <= quantity)   -- BR-12
);
CREATE INDEX ix_orders_broker_id   ON orders (broker_order_id);
CREATE INDEX ix_orders_correlation ON orders (correlation_id);
CREATE INDEX ix_orders_open        ON orders (status)
    WHERE status NOT IN ('FILLED','CANCELLED','REJECTED');

-- Charges per fill, itemised (BR-13). Separate table: an order can fill in parts.
CREATE TABLE order_fills (
    id                BIGSERIAL PRIMARY KEY,
    order_id          BIGINT       NOT NULL REFERENCES orders(id),
    fill_qty          INTEGER      NOT NULL CHECK (fill_qty > 0),
    fill_price        NUMERIC(14,4) NOT NULL,
    filled_at         TIMESTAMPTZ  NOT NULL,
    brokerage         NUMERIC(10,2) NOT NULL DEFAULT 0,
    stt               NUMERIC(10,2) NOT NULL DEFAULT 0,
    exchange_charges  NUMERIC(10,2) NOT NULL DEFAULT 0,
    gst               NUMERIC(10,2) NOT NULL DEFAULT 0,
    sebi_charges      NUMERIC(10,2) NOT NULL DEFAULT 0,
    stamp_duty        NUMERIC(10,2) NOT NULL DEFAULT 0,
    GENERATED ALWAYS AS (brokerage + stt + exchange_charges
                       + gst + sebi_charges + stamp_duty) STORED AS total_charges
);
CREATE INDEX ix_fills_order ON order_fills (order_id);

CREATE TABLE positions (
    id                     BIGSERIAL PRIMARY KEY,
    correlation_id         UUID         NOT NULL,
    symbol_id              INTEGER      NOT NULL REFERENCES instruments(id),
    strategy_id            VARCHAR(64),
    slot_index             INTEGER      NOT NULL CHECK (slot_index >= 0),
    direction              VARCHAR(5)   NOT NULL,
    quantity               INTEGER      NOT NULL CHECK (quantity > 0),
    entry_price            NUMERIC(14,4) NOT NULL CHECK (entry_price > 0),
    stop_price             NUMERIC(14,4) NOT NULL,          -- BR-1: NEVER NULL
    target_price           NUMERIC(14,4),
    opened_at              TIMESTAMPTZ  NOT NULL,
    squareoff_deadline     TIMESTAMPTZ  NOT NULL,           -- BR-7
    status                 VARCHAR(12)  NOT NULL,
    closed_at              TIMESTAMPTZ,
    exit_price             NUMERIC(14,4),
    exit_reason            VARCHAR(24),
    realized_pnl           NUMERIC(14,2),
    max_favourable_exc     NUMERIC(14,4),
    max_adverse_exc        NUMERIC(14,4),

    CONSTRAINT ck_closed_complete CHECK (
        status <> 'CLOSED'
        OR (closed_at IS NOT NULL AND exit_price IS NOT NULL AND exit_reason IS NOT NULL)
    )
);
CREATE UNIQUE INDEX uq_open_slot   ON positions (slot_index) WHERE status = 'OPEN';
CREATE UNIQUE INDEX uq_open_symbol ON positions (symbol_id)  WHERE status = 'OPEN';
CREATE INDEX ix_positions_open     ON positions (status) WHERE status <> 'CLOSED';
```

> **The two partial unique indexes are the enforcement of slot discipline.**
> `uq_open_slot` makes it impossible for two open positions to occupy one slot;
> `uq_open_symbol` makes double-entry on a symbol impossible. The Redis lock in
> E14-S08 is the fast path; these are the guarantee. Belt and braces, because
> over-allocation is a real-money failure.

### 6.5 Audit log

```sql
-- HYPERTABLE. Append-only, hash-chained (§11).
CREATE TABLE decision_log (
    id              BIGSERIAL,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    seq             BIGINT      NOT NULL,          -- chain ordering
    correlation_id  UUID        NOT NULL,
    stage           VARCHAR(28) NOT NULL,
    symbol_id       INTEGER     REFERENCES instruments(id),
    outcome         VARCHAR(12) NOT NULL,
    reason_code     VARCHAR(48),
    payload         JSONB       NOT NULL,
    latency_ms      INTEGER,
    service         VARCHAR(24) NOT NULL,
    prev_hash       CHAR(64)    NOT NULL,
    row_hash        CHAR(64)    NOT NULL,
    PRIMARY KEY (ts, seq)
);
SELECT create_hypertable('decision_log', by_range('ts', INTERVAL '30 days'));
CREATE INDEX ix_audit_correlation ON decision_log (correlation_id, ts);
CREATE INDEX ix_audit_reason      ON decision_log (reason_code, ts DESC)
    WHERE outcome = 'REJECT';

ALTER TABLE decision_log SET (
    timescaledb.enable_columnstore = true,
    timescaledb.segmentby = 'stage',
    timescaledb.orderby   = 'ts DESC'
);
SELECT add_columnstore_policy('decision_log', after => INTERVAL '90 days');
```

### 6.6 Strategy registry

```sql
CREATE TABLE strategy (
    id                   VARCHAR(64) PRIMARY KEY,
    name                 VARCHAR(128) NOT NULL,
    version              INTEGER      NOT NULL DEFAULT 1,
    parent_id            VARCHAR(64)  REFERENCES strategy(id),
    origin               VARCHAR(32)  NOT NULL,
    state                VARCHAR(24)  NOT NULL,
    dsl                  JSONB        NOT NULL,
    dsl_hash             CHAR(64)     NOT NULL,
    hypothesis           JSONB        NOT NULL,
    hypothesis_frozen_at TIMESTAMPTZ  NOT NULL,     -- BEFORE any backtest
    applicable_regimes   TEXT[]       NOT NULL,
    created_at           TIMESTAMPTZ  NOT NULL,
    created_by           VARCHAR(64)  NOT NULL,
    approved_by          VARCHAR(64),
    approved_at          TIMESTAMPTZ,
    state_changed_at     TIMESTAMPTZ  NOT NULL,
    retirement_reason    TEXT,

    -- BR-5: the human approval gate, enforced by the database
    CONSTRAINT ck_active_needs_approval CHECK (
        state <> 'ACTIVE' OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)
    )
);
CREATE INDEX ix_strategy_state ON strategy (state)
    WHERE state IN ('ACTIVE','DEGRADED','SHADOW','PAPER');

-- BR-4: append-only. Deleting a trial corrupts the DSR denominator.
CREATE TABLE strategy_trial (
    id                  BIGSERIAL PRIMARY KEY,
    trial_ts            TIMESTAMPTZ  NOT NULL DEFAULT now(),
    strategy_id         VARCHAR(64)  NOT NULL,
    strategy_hash       CHAR(64)     NOT NULL,
    origin              VARCHAR(32)  NOT NULL,
    generation_batch_id UUID,
    observed_sharpe     NUMERIC(8,4),
    deflated_sharpe     NUMERIC(8,4),
    pbo                 NUMERIC(5,4),
    trade_count         INTEGER,
    outcome             VARCHAR(12)  NOT NULL,
    failed_check        VARCHAR(8),
    report              JSONB        NOT NULL
);
CREATE INDEX ix_trial_hash ON strategy_trial (strategy_hash);
CREATE INDEX ix_trial_ts   ON strategy_trial (trial_ts DESC);

CREATE TABLE strategy_validation (
    id                 BIGSERIAL PRIMARY KEY,
    strategy_id        VARCHAR(64) NOT NULL REFERENCES strategy(id),
    run_at             TIMESTAMPTZ NOT NULL,
    passed             BOOLEAN     NOT NULL,
    checks             JSONB       NOT NULL,
    observed_sharpe    NUMERIC(8,4),
    deflated_sharpe    NUMERIC(8,4),
    pbo                NUMERIC(5,4),
    trial_count_at_run INTEGER     NOT NULL,      -- the DSR denominator, recorded
    trade_count        INTEGER,
    max_drawdown_pct   NUMERIC(6,3),
    regimes_covered    TEXT[],
    holdout_result     JSONB,
    equity_curve       JSONB
);

CREATE TABLE strategy_performance (
    strategy_id       VARCHAR(64) NOT NULL REFERENCES strategy(id),
    as_of             DATE        NOT NULL,
    state             VARCHAR(24) NOT NULL,
    trades            INTEGER     NOT NULL DEFAULT 0,
    wins              INTEGER     NOT NULL DEFAULT 0,
    realized_pnl      NUMERIC(14,2),
    avg_r             NUMERIC(6,3),
    realized_sharpe   NUMERIC(8,4),
    vs_backtest_ratio NUMERIC(6,3),               -- the key degradation signal
    PRIMARY KEY (strategy_id, as_of)
);

CREATE TABLE shadow_signal (
    id                   BIGSERIAL PRIMARY KEY,
    strategy_id          VARCHAR(64) NOT NULL REFERENCES strategy(id),
    symbol_id            INTEGER     NOT NULL REFERENCES instruments(id),
    signalled_at         TIMESTAMPTZ NOT NULL,
    direction            VARCHAR(5)  NOT NULL,
    price_at_signal      NUMERIC(14,4) NOT NULL,
    hypothetical_stop    NUMERIC(14,4) NOT NULL,
    hypothetical_outcome JSONB
);
```

### 6.7 Trade journal

```sql
CREATE TABLE trade_journal (
    id            SERIAL PRIMARY KEY,
    position_id   BIGINT      REFERENCES positions(id),
    trade_date    DATE        NOT NULL,
    setup_type    VARCHAR(32) NOT NULL,
    strategy_id   VARCHAR(64),
    market_regime VARCHAR(20) NOT NULL,
    ai_confidence NUMERIC(4,3),
    outcome       VARCHAR(8)  NOT NULL,
    r_multiple    NUMERIC(6,3),
    thesis_held   BOOLEAN,
    notes         TEXT
);
CREATE INDEX ix_journal_date  ON trade_journal (trade_date DESC);
CREATE INDEX ix_journal_setup ON trade_journal (setup_type, market_regime);
```

### 6.8 Role grants — where BR-3 and BR-4 actually live

```sql
-- The application role. Note what is deliberately absent.
CREATE ROLE algotrader_app LOGIN PASSWORD :'app_password';

GRANT SELECT, INSERT, UPDATE, DELETE ON
    instruments, instrument_daily_status, ohlcv, daily_plan, plan_candidate,
    orders, order_fills, positions, trade_journal, strategy,
    strategy_validation, strategy_performance, shadow_signal
TO algotrader_app;

-- BR-3 and BR-4: INSERT only. No UPDATE. No DELETE. Ever.
GRANT SELECT, INSERT ON decision_log   TO algotrader_app;
GRANT SELECT, INSERT ON strategy_trial TO algotrader_app;

GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO algotrader_app;

-- Migrations run as a separate owner role, not as the app.
```

> **This is the single most important part of the schema.** Append-only enforced
> by application convention is a promise; enforced by a missing grant it is a
> guarantee. Verify it with a test that attempts a DELETE and asserts it fails
> (§13.1).

---

## 7. INDEXING STRATEGY

| Table | Index | Serves | Notes |
|---|---|---|---|
| `ohlcv` | PK `(symbol_id, timeframe, ts)` | BP-2 warm-up reads, BP-1 upserts | Matches the segmentby columns — compressed scans stay fast |
| `instrument_daily_status` | PK `(symbol_id, trade_date)` | Daily hazard lookup | |
| | `(trade_date)` | "today's whole universe" | |
| `orders` | `UNIQUE (client_order_id)` | **Idempotency lookup after ambiguous failure** | The hot path in recovery |
| | `(broker_order_id)` | Reconciliation | |
| | partial on open statuses | Reconciliation scans only live orders | |
| `positions` | partial `UNIQUE (slot_index) WHERE OPEN` | Slot discipline (BR) | |
| | partial `UNIQUE (symbol_id) WHERE OPEN` | Double-entry prevention | |
| `decision_log` | `(correlation_id, ts)` | **Trace one trade end to end** | The audit explorer's main query |
| | partial `(reason_code, ts DESC) WHERE REJECT` | "why isn't it trading?" | Highest-value debugging query |
| `strategy_trial` | `(strategy_hash)` | Trial dedup, DSR count | |

**Deliberately not indexed:** JSONB payload columns. Nothing queries into them
in the hot path; adding GIN indexes would cost write throughput for no read
benefit. Revisit only if the audit explorer needs payload search.

---

## 8. TRANSACTION & CONSISTENCY DESIGN

### 8.1 The rule that matters most

**Never hold a database transaction open across a broker API call.**

A broker call can take 300 ms or time out after 30 s. Holding a transaction for
that duration blocks vacuum, holds locks, and risks connection-pool exhaustion
at exactly the moment the system is under stress.

```
WRONG                                RIGHT
─────                                ─────
BEGIN                                TX1: BEGIN
  INSERT order (PENDING)               INSERT order (status=SUBMITTING)
  call broker.place_order()   ←BAD   COMMIT
  UPDATE order SET broker_id
COMMIT                               call broker.place_order()   ← no TX held

                                     TX2: BEGIN
                                       UPDATE order SET broker_order_id, status
                                     COMMIT
```

The intermediate `SUBMITTING` state is what makes recovery possible: if the
process dies between TX1 and TX2, reconciliation finds an order in `SUBMITTING`
with no `broker_order_id` and knows to query by `client_order_id`.

### 8.2 Transaction boundaries by operation

| Operation | Boundary | Isolation | Notes |
|---|---|---|---|
| Bulk bar insert (BP-1) | One TX per batch of ~10k | READ COMMITTED | `COPY` into a temp table then upsert |
| Bar write (BP-3) | One TX per bar | READ COMMITTED | Trivial volume |
| Order submission | **Two TXs** (§8.1) | READ COMMITTED | Broker call between them |
| Position open | One TX: position + slot claim | **SERIALIZABLE** | Slot allocation must not race |
| Position close | One TX: position update + journal | READ COMMITTED | |
| Plan publish | One TX: plan + all candidates | READ COMMITTED | Atomic — a half-written plan is unusable |
| **Audit write** | **Its own TX, always** | READ COMMITTED | §8.3 |
| Reconciliation | Read-only snapshot, then per-diff TX | REPEATABLE READ for the read | |

### 8.3 Audit writes must not share a transaction with business logic

If the audit entry is written inside the business transaction, a rollback
erases the record of the attempt — and a failed attempt is often exactly what
you need to investigate later.

```python
# WRONG: the audit vanishes if the business logic rolls back
async with session.begin():
    await audit.write(stage=RISK_CHECK, outcome=REJECT, ...)
    await orders.insert(...)        # raises -> audit is gone too

# RIGHT: independent transactions
await audit.write(...)              # own session, own commit
async with session.begin():
    await orders.insert(...)
```

**Implementation:** `AuditWriter` holds its own session factory and never
accepts a caller's session. Make it impossible to pass one in.

### 8.4 The slot allocation race

Two signals firing on the same cycle can both see "slot 3 is free". Three layers
guard this, in order of cost:

1. **Redis lock** (`lock:slot:3`, `SET NX PX`) — fast path, microseconds
2. **Partial unique index** (`uq_open_slot`) — the guarantee; the second insert
   fails with a unique violation
3. **Reconciliation** — detects any drift that somehow survives both

Application code must **catch the unique violation and treat it as "slot taken"**,
not as an error to surface. This is a normal, expected outcome under concurrency.

---

## 9. REDIS KEYSPACE SPECIFICATION

| Key | Type | TTL | Written by | Read by |
|---|---|---|---|---|
| `state:indicator:{sym}:{tf}` | HASH | session end | ti-engine | signal-engine |
| `state:bar:current:{sym}:{tf}` | HASH | session end | market-ingest | ti-engine |
| `state:quote:{sym}` | HASH | 60 s | market-ingest | risk, UI |
| `state:position:{sym}` | HASH | none (deleted on close) | execution | risk, UI |
| `state:slots` | HASH | session end | execution | risk |
| `plan:{YYYY-MM-DD}` | STRING | 48 h | premarket | signal, UI |
| `plan:candidate:{date}:{sym}` | STRING | 48 h | premarket | signal, UI |
| `context:market` | STRING | **90 min** | macro-svc | all |
| `control:killswitch` | STRING | none | orchestrator, api | **all** |
| `control:mode` | STRING | none | orchestrator | all |
| `control:interval` | STRING | none | orchestrator | signal-engine |
| `control:health:{svc}` | STRING | **30 s** | every service | orchestrator |
| `lock:slot:{i}` | STRING | 60 s | execution | — |
| `lock:symbol:{sym}` | STRING | 60 s | signal-engine | — |
| `ratelimit:orders` | STRING | 1 s | broker adapter | — |
| `timer:squareoff` | ZSET | none | execution | execution |
| `stream:*` | STREAM | MAXLEN (§4.3) | various | consumer groups |

### 9.1 TTL discipline

**Every lock must have a TTL.** A crashed process holding a lock forever would
deadlock slot allocation for the rest of the session. Make TTL a *required*
positional argument in the lock helper — a lock without one should be
impossible to construct, not merely discouraged.

**`control:health:{svc}` TTL is the liveness mechanism.** The key simply expiring
*is* the "service is down" signal. No separate heartbeat-timeout logic needed.

**`context:market` TTL of 90 minutes** is deliberately longer than the 20-minute
refresh: it means a macro-svc outage degrades gracefully (consumers see stale
data with an explicit `as_of`) rather than the key vanishing and consumers
having to handle absence as a separate case.

### 9.2 Serialisation contract

All structured values are Pydantic `model_dump_json()`. Reads validate on the
way out. A value that fails validation is treated as absent and logged — never
returned partially parsed, because half-valid trading state is worse than none.

---

## 10. EVENT STREAM DESIGN

### 10.1 Streams and consumer groups

| Stream | Producer | Consumer group(s) | MAXLEN |
|---|---|---|---|
| `stream:ticks` | market-ingest | `archiver` | 100,000 |
| `stream:bars:{tf}` | market-ingest | `ti-engine` | 10,000 |
| `stream:snapshots` | ti-engine | `signal-engine` | 10,000 |
| `stream:signals` | signal-engine | `execution` | 10,000 |
| `stream:orders` | execution | `notifier`, `api` | 10,000 |
| `stream:audit` | all | `db-writer` | 50,000 |

### 10.2 Envelope

```python
class Envelope(BaseModel):
    message_id: UUID
    correlation_id: UUID
    schema_version: int = 1      # present from day one, not retrofitted
    emitted_at: datetime
    emitted_by: str
    payload: dict
```

`schema_version` exists from the first message. Adding it later, once streams
contain unversioned entries, means writing a compatibility shim you could have
avoided for the cost of one field.

### 10.3 Delivery semantics

**At-least-once.** Consumers must be idempotent. This is not a limitation to
work around — for this system it is the correct choice:

- Processing a bar twice yields the same indicator state (idempotent by nature)
- Processing a signal twice is caught by `lock:symbol` and `uq_open_symbol`
- Processing an audit event twice is caught by `message_id` dedup

Exactly-once would require distributed transactions across Redis and Postgres
for no practical gain.

### 10.4 Recovery on restart

```
1. XGROUP CREATE ... MKSTREAM     (idempotent; ignore BUSYGROUP)
2. XAUTOCLAIM  — reclaim entries pending beyond min-idle-time
3. XREADGROUP with id '0'         — process this consumer's own pending backlog
4. XREADGROUP with id '>'         — then new messages
```

Step 3 is the one that is easy to miss and is exactly what the acceptance
criterion tests: *"Kill a consumer mid-stream; on restart it processes the
unacked backlog."*

### 10.5 Dead-letter handling

After N delivery attempts (default 3), move the entry to `stream:dlq:{name}`
with the failure reason and ACK the original. Alert on any DLQ write — a message
that cannot be processed three times is a bug, not a transient.

---

## 11. AUDIT HASH CHAIN DESIGN

### 11.1 The algorithm

```
row_hash = SHA256(
    prev_hash            ||
    seq                  ||
    ts (ISO-8601, UTC)   ||
    correlation_id       ||
    stage                ||
    outcome              ||
    canonical_json(payload)
)
```

`canonical_json` = `json.dumps(payload, sort_keys=True, separators=(',',':'))`.
Non-deterministic serialisation would break verification for no reason.

Genesis row: `prev_hash = '0' * 64`.

### 11.2 The concurrency problem, and the fix

A hash chain requires a strict order. Multiple services write audit entries
concurrently, so two writers can both read the same `prev_hash` and produce a
fork.

**Solution: a Postgres transaction-scoped advisory lock.**

```sql
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('audit_chain'));
SELECT seq, row_hash FROM decision_log ORDER BY seq DESC LIMIT 1;
INSERT INTO decision_log (seq, prev_hash, row_hash, ...) VALUES (...);
COMMIT;   -- lock released automatically
```

At ~3,000 rows/day (roughly one every 10 seconds during market hours) lock
contention is a non-issue. The transaction-scoped variant (`_xact_`) releases
automatically on commit or rollback, so a crash cannot leak the lock.

**Rejected alternative:** per-`correlation_id` chains. Simpler and lock-free,
but it would not detect deletion of an entire trade's worth of entries — which
is precisely the tampering scenario worth defending against.

### 11.3 Disk buffer for DB outage

Acceptance criterion: *"DB outage does not lose audit entries."*

```
write() → try DB insert
        → on failure: append JSONL to data/audit_buffer/{date}.jsonl
                      (fsync each line), set a degraded flag, alert
        → on recovery: replay buffered lines in order, then resume
```

Buffered entries carry no hash yet — the chain is computed at replay time in
buffer order. The buffer file is itself append-only and fsynced per line;
losing a few milliseconds of throughput is an acceptable price for not losing
the record of what the system did while its database was down.

### 11.4 Verification utility

```bash
python -m algotrader.tools.verify_audit --from 2026-08-01 --to 2026-08-31
```

Walks the chain, recomputes each hash, reports the first divergence with its
`seq` and timestamp. Runs nightly; result recorded in the ops log.

---

## 12. STORY-BY-STORY IMPLEMENTATION GUIDE

### E01-S01 · Database schema and migrations
`P0` · `Correctness-critical` · `INFRA` · **2 days** · deps: none

| # | Task | Detail | Est. |
|---|---|---|---|
| 1 | Alembic async setup | `alembic init -t async`; wire `env.py` to `common/config.py`; **decide and record the driver choice (§2.2)** | 0.25 d |
| 2 | SQLAlchemy models | `common/db/models.py`; `DeclarativeBase` + `AsyncAttrs`; `Numeric(14,4)` for money, `TIMESTAMP(timezone=True)` everywhere | 0.5 d |
| 3 | Migration: reference data | `instruments`, `instrument_daily_status`, `v_eligible_today` | 0.25 d |
| 4 | Migration: ohlcv hypertable | **Verify the current TimescaleDB API against the pinned image first (§2.1)** | 0.25 d |
| 5 | Migration: trading tables | `orders`, `order_fills`, `positions`, `trade_journal` with all CHECK constraints and partial unique indexes | 0.25 d |
| 6 | Migration: plan tables | `daily_plan`, `plan_candidate` | 0.1 d |
| 7 | Migration: strategy tables | Five tables incl. the `ck_active_needs_approval` CHECK | 0.25 d |
| 8 | Columnstore policies | `ohlcv` and `decision_log`, 90-day threshold | 0.1 d |
| 9 | Role grants + `make migrate` | **BR-3/BR-4 grants are the important part** | 0.25 d |

**Watch out for**
- TimescaleDB requires the partitioning column in every unique index. `ohlcv`'s
  PK already includes `ts`; check any index you add later.
- `downgrade` must drop policies before hypertables, or it fails.
- Test `downgrade base` on every migration — the acceptance criterion requires
  it and it is easy to leave broken.

**Acceptance**
- `alembic upgrade head` → `downgrade base` → `upgrade head` clean on an empty DB
- `SELECT * FROM timescaledb_information.hypertables` shows both hypertables
- Connecting as `algotrader_app` and attempting `DELETE FROM decision_log` fails

---

### E01-S02 · Repository layer
`P0` · `Low` · `INFRA` · **2 days** · deps: E01-S01

| # | Task | Detail | Est. |
|---|---|---|---|
| 1 | Repository protocols | One per aggregate; `Protocol` classes in `common/db/repositories/` | 0.5 d |
| 2 | Session management | `async_sessionmaker`; `@asynccontextmanager` unit-of-work; **explicit boundaries per §8.2** | 0.5 d |
| 3 | Bulk insert path | `COPY` via psycopg3 into a temp table, then `INSERT ... ON CONFLICT DO UPDATE` | 0.5 d |
| 4 | Connection pooling | pool size 10, max overflow 5, `pool_pre_ping=True` | 0.25 d |
| 5 | In-memory fakes | Dict-backed implementations of each protocol for unit tests | 0.25 d |

**Key repository methods** (the ones with real requirements behind them):

```python
class BarRepository(Protocol):
    async def bulk_upsert(self, bars: Sequence[Bar]) -> int: ...
    async def latest_n(self, symbol_id: int, tf: Timeframe, n: int) -> list[Bar]: ...
    async def range(self, symbol_id: int, tf: Timeframe,
                    start: datetime, end: datetime) -> list[Bar]: ...
    async def warm_up_batch(self, symbol_ids: Sequence[int], tf: Timeframe,
                            bars_each: int) -> dict[int, list[Bar]]: ...   # BP-2

class OrderRepository(Protocol):
    async def insert_submitting(self, req: OrderRequest) -> int: ...
    async def attach_broker_id(self, client_order_id: str, broker_id: str) -> None: ...
    async def find_by_client_order_id(self, cid: str) -> Order | None: ...   # recovery
    async def open_orders(self) -> list[Order]: ...
```

`warm_up_batch` is the one that must be fast — it is BP-2's workhorse. A
per-symbol loop issuing 150 queries will miss the pre-market deadline; one query
with `WHERE symbol_id = ANY(...)` and a window function will not.

**Acceptance**
- `grep -r "import sqlalchemy" src/algotrader/{ingest,signals,execution,premarket}` returns nothing
- Bulk insert of 100k bars completes in < 10 s (measured, in the test)

---

### E01-S03 · Redis client and keyspace helpers
`P0` · `Correctness-critical` · `INFRA` · **1.5 days** · deps: none

| # | Task | Detail | Est. |
|---|---|---|---|
| 1 | Async client | `redis.asyncio` with pool; retry on `ConnectionError`; health check | 0.25 d |
| 2 | Key builders | One function per pattern in §9; no string literals anywhere else | 0.25 d |
| 3 | Typed state accessors | `get_state(key, Model)` / `set_state(key, model, ttl)` | 0.25 d |
| 4 | Lock helper | **TTL is a required argument** | 0.25 d |
| 5 | Token bucket | Lua script for atomicity | 0.25 d |
| 6 | Timer helper | ZSET add / pop-due / remove | 0.25 d |

**The lock signature is the story's real content:**

```python
async def acquire_lock(redis, key: str, holder: str, ttl_ms: int) -> bool:
    """ttl_ms is REQUIRED and has no default.

    A lock without a TTL survives the death of the process holding it and
    deadlocks slot allocation for the session. Making the argument mandatory
    means that failure cannot be introduced by omission.
    """
```

**Acceptance**
- Every key in §9 has a builder function (test enumerates them)
- `acquire_lock` cannot be called without a TTL (TypeError at call site)

---

### E01-S04 · Event stream abstraction
`P0` · `Correctness-critical` · `INFRA` · **1.5 days** · deps: E01-S03

| # | Task | Detail | Est. |
|---|---|---|---|
| 1 | Publish/subscribe | Pydantic envelopes; **`maxlen` required on publish (§4.3)** | 0.4 d |
| 2 | Consumer groups | Idempotent creation; ignore `BUSYGROUP` | 0.2 d |
| 3 | Acknowledgement | Explicit `XACK` after successful processing only | 0.2 d |
| 4 | Pending recovery | The four-step sequence in §10.4 | 0.4 d |
| 5 | Dead-letter | After 3 attempts → `stream:dlq:*` + alert | 0.2 d |
| 6 | Schema version | On the envelope from message one | 0.1 d |

**Acceptance**
- Integration test: publish 100 → consume 50 → kill consumer → restart →
  the remaining 50 plus the unacked are processed, none lost, none duplicated
  beyond at-least-once
- `publish()` without `maxlen` is a TypeError

---

### E01-S05 · Audit log writer with hash chaining 🔴
`P0` · **Safety-critical** · `SEC` · **1 day** · deps: E01-S01

| # | Task | Detail | Est. |
|---|---|---|---|
| 1 | `AuditWriter` | Own session factory; **refuses a caller-supplied session (§8.3)** | 0.25 d |
| 2 | Hash chain | Advisory lock + canonical JSON (§11.1, §11.2) | 0.3 d |
| 3 | Correlation binding | `correlation_id` required on every write | 0.1 d |
| 4 | Verification utility | `tools/verify_audit.py` | 0.2 d |
| 5 | Disk buffer | JSONL with per-line fsync; replay on recovery (§11.3) | 0.15 d |

**Because this is 🔴 safety-critical, the DoD is stricter:** property-based test
over generated event sequences asserting the chain always verifies; explicit
tamper test; explicit outage test. Do not merge at the end of a session.

**Acceptance**
- Modifying any historical row is detected, and the verifier names the row
- Stopping Postgres mid-run loses zero entries; all replay in order on recovery
- Two services writing concurrently produce a single valid chain

---

### E01-S06 · Data retention and archival
`P2` · `Low` · `OPS` · **0.5 day** · deps: E01-S01 · **Phase 6 — defer**

Nightly ticks → Parquet; verify the columnstore policy is converting chunks;
restore-from-archive utility.

> **Scheduling note:** this is P2/Phase 6 in the tracker but sits in E01. It is
> correctly *not* Sprint 1 work. Leave it `New`; pull it in when tick archival
> is actually needed by the replay harness (E22-S03).

---

## 13. TESTING REQUIREMENTS

### 13.1 The tests that matter most

These verify business rules rather than code shape. Each one fails if a
guarantee is quietly removed.

```python
async def test_audit_log_cannot_be_deleted(app_role_session):
    """BR-3 — enforced by role grant, not by convention."""
    with pytest.raises(ProgrammingError, match="permission denied"):
        await app_role_session.execute(text("DELETE FROM decision_log"))

async def test_strategy_cannot_be_active_without_approval(session):
    """BR-5 — the human approval gate at DB level."""
    with pytest.raises(IntegrityError, match="ck_active_needs_approval"):
        await session.execute(insert(Strategy).values(state="ACTIVE", approved_by=None, ...))

async def test_position_requires_stop(session):
    """BR-1 — a naked position must be impossible."""
    with pytest.raises(IntegrityError):
        await session.execute(insert(Position).values(stop_price=None, ...))

async def test_duplicate_client_order_id_rejected(session):
    """BR-2 — what makes query-don't-retry safe."""
    await orders.insert_submitting(req)
    with pytest.raises(IntegrityError, match="uq_client_order"):
        await orders.insert_submitting(req)     # same decision, same key

async def test_two_positions_cannot_share_a_slot(session):
    """Slot discipline at DB level."""
    await positions.open(slot_index=3, ...)
    with pytest.raises(IntegrityError, match="uq_open_slot"):
        await positions.open(slot_index=3, ...)

async def test_incoherent_bar_rejected_by_database(session):
    """Defence in depth on the one rule whose violation is silent."""
    with pytest.raises(IntegrityError, match="ck_ohlc_coherent"):
        await session.execute(insert(Ohlcv).values(high=100, low=105, ...))
```

### 13.2 Property-based (E01-S05, 🔴)

```python
@given(events=st.lists(audit_event_strategy(), min_size=1, max_size=200))
async def test_hash_chain_always_verifies(events):
    for e in events:
        await writer.write(e)
    assert await verify_chain() == ChainStatus.VALID

@given(events=..., tamper_index=...)
async def test_tampering_always_detected(events, tamper_index):
    ...  # modify one row directly, assert the verifier catches it
```

### 13.3 Performance tests

| Test | Target | Story |
|---|---|---|
| Bulk insert 100k bars | < 10 s | E01-S02 |
| `warm_up_batch` for 150 symbols × 250 bars | < 30 s | E01-S02 (BP-2 budget) |
| Redis state round-trip | < 1 ms p99 | E01-S03 |
| Audit write | < 50 ms p99 | E01-S05 |

### 13.4 Integration harness

Testcontainers for `timescale/timescaledb:latest-pg16` and `redis:7-alpine`.
Migrations run against the real container — a schema that only works against
SQLite is not tested.

---

## 14. FAILURE MODES

| Failure | Detection | Response | Story |
|---|---|---|---|
| Postgres unreachable at startup | Connection failure | Refuse to start; alert | E01-S02 |
| Postgres unreachable mid-session | Query exception | Trading continues on Redis; audit buffers to disk; **no new orders** | E01-S05 |
| Redis unreachable | Connection failure | **Halt trading** — hot state is unavailable | E01-S03 |
| Redis approaching maxmemory | `INFO memory` monitoring | Alert at 80%; verify stream trimming | E01-S04 |
| Stream consumer lag growing | `XPENDING` count | Alert; investigate the slow consumer | E01-S04 |
| Migration fails mid-way | Alembic error | Transactional DDL rolls back; fix and retry | E01-S01 |
| Hash chain broken | Nightly verification | **P0 alert** — possible tampering or a bug | E01-S05 |
| Disk full | Monitoring | Alert; audit buffer will also fail — this is serious | E01-S05 |

---

## 15. DEFINITION OF DONE

### 15.1 Every story

- [ ] `make check` passes (lint + types + tests)
- [ ] Tests written that **fail if the story's claim is false**
- [ ] Docstrings explain *why*
- [ ] No `float` in a money path; no naive datetime; no secret
- [ ] `make doctor` still exits 0
- [ ] Tracker updated: Status, % Complete, actual days

### 15.2 Additionally for 🔴 E01-S05

- [ ] Property-based test over generated sequences
- [ ] Explicit tamper-detection test
- [ ] Explicit DB-outage test with buffer replay
- [ ] Concurrent-writer test
- [ ] Second read of the diff on a different day before merge

### 15.3 Epic-level

- [ ] `alembic upgrade head` from empty produces the complete schema
- [ ] All six BR tests in §13.1 pass
- [ ] Performance targets in §13.3 met and recorded
- [ ] Integration suite green against real containers
- [ ] `docs`: LOW_LEVEL_ARCHITECTURE §4.2 updated with the columnstore correction

---

## 16. SPRINT 1 SEQUENCING

Sprint 1 is nominally 5 working days and carries both E01 and blocker
resolution. E01 alone is 8.5 days, so **the sprint as scoped does not fit.**

Deferring E01-S05 (1 d) and E01-S06 (0.5 d) brings the committed scope to
**7 days** — which still does not fit a 5-day box, and there is no second
developer to parallelise across. Two honest ways out:

- **Option A (recommended) — let Sprint 1 run to 7 working days.** The dates in
  the tracker are indicative, so define Sprint 1 as *"done when E01-S01..S04 are
  done"* rather than as a fixed calendar box. The ordering below is what matters;
  the boundary is not load-bearing.
- **Option B — hold a hard 5-day box.** Sprint 1 becomes E01-S01 (2 d) +
  E01-S03 (1.5 d) + the blocker work, and E01-S02 and E01-S04 join E01-S05 in
  Sprint 2. This is the right choice only if something external actually depends
  on the 5-day boundary.

Do not resolve the gap by compressing estimates. The 7 days is what the work is;
squeezing it produces the same overrun with less visibility.

### 16.1 Recommended order (Option A — 7 working days)

| Day | Work | Rationale |
|---|---|---|
| **1–2** | E01-S01 (2 d) · fire off blocker questions to Zerodha (B1, B4) on day 1 | Get the questions out first — the answers take days to arrive |
| **2** | B3 (transcribe holiday circular), alongside S01 | Independent desk work, fits in the gaps |
| **3–4** | E01-S03 (1.5 d) — independent of S01 | Start it while S01 settles; nothing blocks it |
| **5–6** | E01-S02 (2 d) | Needs S01 done |
| **6–7** | E01-S04 (1.5 d) · sprint review | Needs S03 done |

B6 (static IP) runs in the background across the whole sprint — it is
procurement lead time, not desk work.

**Deferred to Sprint 2:** E01-S05 (audit chain, 1 day) and E01-S06 (P2, Phase 6).

### 16.2 Why defer E01-S05 rather than compress something else

It is the only 🔴 safety-critical story in the epic and carries the strictest
DoD — property tests, tamper tests, outage tests, and a second read on a
different day. Squeezing it into a Friday afternoon violates the working rule
that a 🔴 story is never merged at the end of a session. Nothing in Sprint 2
depends on it, so the deferral is free.

### 16.3 Blocker work in Sprint 1

| Blocker | Sprint 1 action | Your note |
|---|---|---|
| B1 Algo-ID | Email Zerodha; research the forum in parallel | *"Research and arrive at a relevant answer"* |
| B2 SDK gap | **Decision made: wait for release.** The `doctor` check already exists (`scripts/doctor.py:274`) and fails when the installed SDK lacks `market_protection` — no new work needed, the wait is already visible | *"I would prefer wait for release"* |
| B3 Holiday list | Transcribe the NSE circular; set `verified: true` | *"Research and arrive at a relevant answer"* |
| B4 Data pricing | Ask Zerodha alongside B1 | |
| B5 Login flow | Not Sprint 1 — needs E02 | |
| B6 Static IP | **Procedure needs documenting** — see §17 | *"Explain on the procedure for this"* |

> **On B2:** waiting for the release is the right call for a personal system —
> installing from a git main branch means you own the risk of an unreleased
> change. The mitigation is to make the wait *visible* rather than forgotten:
> `doctor` already checks for `market_protection` and fails loudly, so the day
> the release lands you will know, and until then you cannot accidentally go
> live without it.

---

## 17. OPEN QUESTIONS

| # | Question | Impact | Needs |
|---|---|---|---|
| ~~Q1~~ | ~~Confirm the TimescaleDB image version~~ **RESOLVED** | Pin is `2.17.2-pg16` — predates hypercore. Decision required: upgrade the pin (recommended) or use the legacy compression API. See §2.1 | Your call in E01-S01 task 1 |
| Q2 | Retention: keep 1m bars forever or roll off after 1 year? | ~19 M rows either way; storage is not the constraint, backtest depth is | Your call — recommend keeping, it is cheap |
| Q3 | Separate DB roles per service, or one app role? | Tighter blast radius vs operational simplicity | Recommend one app role now, split at Phase 7 hardening |
| Q4 | Audit buffer location — container volume or host mount? | Survives container restart either way; host mount survives image rebuild | Recommend host mount |

### 17.1 B6 — static IP procurement procedure (your note asked for this)

Not a coding task, but it gates all live trading, so here is the sequence:

1. **Choose the host.** Options, in rough order of fit:
   - An Indian VPS provider with a dedicated IPv4 included (simplest, cheapest)
   - AWS `ap-south-1` EC2 with an **Elastic IP** attached
   - Any provider offering a static/reserved IP in an India region
   Requirement is only that the IP is **static and India-hosted** (SEBI).

2. **Provision and note the IP.** After allocation, confirm the actual egress IP
   from the box itself — the assigned IP and the egress IP can differ behind
   NAT:
   ```bash
   curl -s https://api.ipify.org
   ```
   That value, not the console's, is what the broker will see.

3. **Whitelist with Zerodha.** Log in to `developers.kite.trade` → your app's
   profile page → **IP Whitelist** section → add the IP. Both IPv4 and IPv6 are
   accepted.

4. **Record it in config.** Set `system.static_ip` in `system.yaml` and
   `EXPECTED_EGRESS_IP` in `.env`.

5. **Verify.** `make doctor` compares the live egress IP against the configured
   one and fails on mismatch. Then verify the *negative* case too: an order
   attempt from a non-whitelisted address must be rejected — that is what proves
   the whitelist is actually enforcing.

**Two things to know before choosing a provider:**
- The static IP applies to **order endpoints only**. Quotes, WebSocket, and
  positions work from any IP — so a data-only fallback does not need its own
  whitelisted address.
- **Each IP binds to one Zerodha account.** If you later extend to family
  accounts, you need either separate IPs or all accounts under one developer
  profile.

---

*Specification for E01. Update as implementation reveals what the design got
wrong — particularly §2.1, which should be confirmed against the pinned image
before the first migration is written.*
