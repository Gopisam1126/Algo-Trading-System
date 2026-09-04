# DEVELOPMENT BACKLOG
## User Stories & Tasks — Complete Project Coverage

**Created:** 2026-08-05 · **Status:** Phase 0 complete, Phase 1 ready to start
**Companion:** [MASTER_REFERENCE.md](MASTER_REFERENCE.md) for system context

---

## HOW TO USE THIS BACKLOG

### Story ID scheme

```
E{epic}-S{story}     e.g. E05-S03  = Epic 5, Story 3
```

### Classification

Every story carries four classifiers so you can slice the backlog whichever way
you need on a given day.

| Classifier | Values | Use it to answer |
|---|---|---|
| **Priority** | `P0` blocker · `P1` MVP · `P2` post-MVP · `P3` nice-to-have | "Can I ship without this?" |
| **Phase** | `0`–`8` per the build plan | "When does this land?" |
| **Risk** | `🔴` safety-critical · `🟠` correctness-critical · `🟢` low | "How careful must I be?" |
| **Type** | `FEAT` · `INFRA` · `SEC` · `TEST` · `OPS` · `DOC` | "What kind of work is this?" |

**🔴 safety-critical** means a defect can lose money or breach regulation.
These require: property-based tests, 100% line coverage, and a second read
before merge. Never merge one at the end of a session.

### Estimates

Days of focused solo work. A "1" is a morning; a "5" is a full week.
Multiply by 1.5 if you are learning the domain as you go.

### Definition of Done (applies to every story)

- [ ] Code written and passing `make check` (lint + types + tests)
- [ ] Tests written **that fail if the story's claim is false** — not tests that
      assert the code shape (see MASTER_REFERENCE §14.8)
- [ ] 🔴 stories: property-based test covering the invariant
- [ ] Docstrings explain *why*, not *what*
- [ ] No secret, no `float` in a money path, no naive datetime
- [ ] `make doctor` still exits 0
- [ ] Relevant design document updated if behaviour diverged from spec

### Epic map

| Epic | Name | Phase | Stories | Est. days |
|---|---|---|---|---|
| E00 | Foundation | 0 | 10 | ✅ done |
| E01 | Persistence & Data Layer | 1 | 6 | 8 |
| E02 | Broker Integration | 1 | 9 | 12 |
| E03 | Historical Data | 1 | 6 | 8 |
| E04 | India Hazard Lists | 1 | 7 | 6 |
| E05 | Market Data Ingestion | 1 | 8 | 10 |
| E06 | Technical Analysis Engine | 2 | 8 | 12 |
| E07 | Universe & Scoring | 2 | 6 | 7 |
| E08 | News Intelligence | 3 | 9 | 12 |
| E09 | Macro Context | 3 | 8 | 8 |
| E10 | AI Layer | 3 | 8 | 10 |
| E11 | Pre-Market Engine | 3 | 8 | 10 |
| E12 | Strategy Engine | 5 | 12 | 18 |
| E13 | Signal Engine | 4 | 6 | 8 |
| E14 | Risk Engine | 5 | 9 | 14 |
| E15 | Execution & Positions | 5 | 11 | 16 |
| E16 | Autonomy & Approval | 5 | 6 | 7 |
| E17 | Dashboard UI | 4/6 | 9 | 14 |
| E18 | Admin UI | 6 | 8 | 12 |
| E19 | Notifications | 3/4 | 6 | 6 |
| E20 | Observability | 6 | 6 | 7 |
| E21 | Compliance & Tax | 6 | 7 | 9 |
| E22 | Testing & Validation | 7 | 8 | 12 |
| E23 | Deployment & Ops | 7/8 | 7 | 8 |
| | **TOTAL** | | **188** | **~234** |

> ~234 focused days ≈ 11 months solo at 5 days/week, or ~7 months at the pace
> implied by the 19-week plan if you work longer days. The 19-week estimate in
> the build plan assumes MVP scope only (P0+P1), which is roughly 150 days.

---
---

# EPIC 00 — FOUNDATION ✅ COMPLETE

*Phase 0 · 10 stories · All done*

| ID | Story | Risk | Status |
|---|---|---|---|
| E00-S01 | Project scaffold, packaging, tooling | 🟢 | ✅ |
| E00-S02 | Domain models with Decimal money and tz-aware UTC | 🟠 | ✅ |
| E00-S03 | Config validation with hard bounds in code | 🔴 | ✅ |
| E00-S04 | Secrets handling that cannot render | 🔴 | ✅ |
| E00-S05 | Structured logging with mandatory redaction | 🔴 | ✅ |
| E00-S06 | NSE calendar with per-stock square-off | 🔴 | ✅ |
| E00-S07 | Broker protocol, read-only vs trading split | 🔴 | ✅ |
| E00-S08 | Strategy DSL with vetted primitive registry | 🔴 | ✅ |
| E00-S09 | Docker topology with network segmentation | 🟠 | ✅ written, not run |
| E00-S10 | Pre-flight doctor script | 🟢 | ✅ |

**Carried debt from Phase 0:**
- `nse_holidays.yaml` incomplete — see E04-S07
- Docker topology written but never started — see E23-S03

---
---

# EPIC 01 — PERSISTENCE & DATA LAYER

*Phase 1 · 6 stories · 8 days · Foundation everything else sits on*

### E01-S01 · Database schema and migrations
`P0` `Phase 1` `🟠` `INFRA` · **2 days** · deps: none

> As a developer, I want the full database schema defined as versioned
> migrations, so that schema changes are reviewable and reversible rather than
> applied by hand.

**Tasks**
- [ ] Set up Alembic with async SQLAlchemy 2.0
- [ ] SQLAlchemy models mirroring LOW_LEVEL_ARCHITECTURE §4.2
- [ ] Migration: instruments, instrument_daily_status
- [ ] Migration: ohlcv as a TimescaleDB hypertable
- [ ] Migration: orders, positions, decision_log, trade_journal
- [ ] Migration: daily_plan, plan_candidate
- [ ] Migration: strategy, strategy_validation, strategy_trial, strategy_performance, shadow_signal
- [ ] Compression policy on ohlcv (>90 days)
- [ ] `make migrate` target

**Acceptance**
- `alembic upgrade head` then `downgrade base` runs clean on an empty DB
- Hypertables confirmed via `SELECT * FROM timescaledb_information.hypertables`
- `decision_log` and `strategy_trial` roles have INSERT but not UPDATE/DELETE

---

### E01-S02 · Repository layer
`P0` `Phase 1` `🟢` `INFRA` · **2 days** · deps: E01-S01

> As a developer, I want data access behind repository interfaces, so that
> services never write raw SQL and can be tested against fakes.

**Tasks**
- [ ] `Repository` protocol per aggregate (instruments, bars, orders, positions, plan, strategy)
- [ ] Async session management with proper transaction boundaries
- [ ] Bulk insert path for OHLCV (batched, not row-by-row)
- [ ] Connection pooling configured
- [ ] In-memory fakes for unit tests

**Acceptance**
- No service module imports `sqlalchemy` directly
- Bulk insert of 100k bars completes in < 10s

---

### E01-S03 · Redis client and keyspace helpers
`P0` `Phase 1` `🟠` `INFRA` · **1.5 days** · deps: none

> As a developer, I want typed helpers for the Redis keyspace, so that key
> naming cannot drift between services.

**Tasks**
- [ ] Async Redis client with connection pooling and retry
- [ ] Key builders for every pattern in LOW_LEVEL_ARCHITECTURE §4.3
- [ ] Typed get/set for state hashes (Pydantic in, Pydantic out)
- [ ] Distributed lock helper with mandatory TTL
- [ ] Token bucket rate limiter
- [ ] Sorted-set timer helper

**Acceptance**
- Every key in the design doc has a builder function
- A lock without a TTL is impossible to create (TTL is a required arg)

---

### E01-S04 · Event stream abstraction
`P0` `Phase 1` `🟠` `INFRA` · **1.5 days** · deps: E01-S03

> As a developer, I want a typed wrapper over Redis Streams with consumer
> groups, so that no event is lost when a consumer restarts.

**Tasks**
- [ ] `EventStream` publish/subscribe with Pydantic envelopes
- [ ] Consumer group creation and management
- [ ] Explicit acknowledgement
- [ ] Pending-entry recovery on restart
- [ ] Dead-letter handling after N failures
- [ ] `schema_version` on every envelope

**Acceptance**
- Kill a consumer mid-stream; on restart it processes the unacked backlog
- Messages published while a consumer is down are delivered on return

---

### E01-S05 · Audit log writer with hash chaining
`P0` `Phase 1` `🔴` `SEC` · **1 day** · deps: E01-S01

> As an operator, I want every decision written to an append-only hash-chained
> log, so that tampering is detectable and I can substantiate any trade.

**Tasks**
- [ ] `AuditWriter` — append-only, never updates
- [ ] Hash chain: each row includes the previous row's hash
- [ ] `correlation_id` on every entry
- [ ] Chain verification utility
- [ ] Buffer to disk if the DB is unavailable, replay on recovery

**Acceptance**
- Modifying a historical row is detected by the verifier
- DB outage does not lose audit entries

---

### E01-S06 · Data retention and archival
`P2` `Phase 6` `🟢` `OPS` · **0.5 day** · deps: E01-S01

> As an operator, I want old tick data archived to Parquet, so that the
> database stays small without losing replay capability.

**Tasks**
- [ ] Nightly job: ticks older than 24h → Parquet
- [ ] Compression policy verification
- [ ] Restore-from-archive utility

---
---

# EPIC 02 — BROKER INTEGRATION

*Phase 1 · 9 stories · 12 days · **Blocked on B1/B4 (see §Blockers)***

### E02-S01 · Kite Connect authentication — redirect flow
`P0` `Phase 1` `🔴` `SEC` · **2 days** · deps: none

> As a trader, I want the daily Zerodha login to complete through a link I tap
> on my phone, so that the system has a valid session before pre-open without
> me sitting at a laptop.

**Tasks**
- [ ] Generate the Kite login URL with `api_key`
- [ ] Local callback endpoint receiving `request_token`
- [ ] Exchange `request_token` + `api_secret` → `access_token`
- [ ] Store token as `SecretString` in Redis with TTL to next pre-open
- [ ] Send the login link via Telegram at the configured time
- [ ] Confirm success back to Telegram
- [ ] Alert loudly on failure — this blocks the trading day

**Acceptance**
- Full flow completes from a phone in under 60 seconds
- Token never appears in any log
- Expired token is detected before the pre-market job starts

---

### E02-S02 · Session lifecycle and daily re-auth scheduling
`P0` `Phase 1` `🔴` `SEC` · **1 day** · deps: E02-S01

> As a compliance-conscious operator, I want sessions to expire and re-establish
> daily, so that the SEBI auto-logout requirement is met by design.

**Tasks**
- [ ] Scheduled trigger at `broker.auth.daily_reauth_time`
- [ ] Session validity check before every trading-day transition
- [ ] Retry with backoff ×3, then halt the day
- [ ] `auth.refreshed` / `auth.failed` events
- [ ] Health gate integration — no valid session means no trading

**Acceptance**
- 20 consecutive sessions authenticate successfully
- A failed auth prevents the session reaching `TRADING`

---

### E02-S03 · Read-only market data adapter
`P0` `Phase 1` `🟠` `FEAT` · **1.5 days** · deps: E02-S01

> As a developer, I want a read-only Kite adapter, so that data services
> physically cannot place orders.

**Tasks**
- [ ] Implement `MarketDataAdapter` for Kite
- [ ] `fetch_instruments`, `fetch_historical`, quote endpoints
- [ ] Apply `ReadOnlyGuard` so trading methods raise
- [ ] Map Kite errors to the adapter's error taxonomy

**Acceptance**
- Calling `place_order` on this adapter raises `PermissionError`
- Data endpoints work from a non-whitelisted IP (verifying the order-only scope)

---

### E02-S04 · Trading adapter with market protection
`P0` `Phase 5` `🔴` `FEAT` · **2 days** · deps: E02-S03, E14-S01

> As a trader, I want orders placed correctly with market protection attached,
> so that MARKET and SL-M orders are not rejected by the broker.

**Tasks**
- [ ] Implement `TradingAdapter` for Kite
- [ ] Attach `market_protection` on MARKET/SL-M
- [ ] Attach Algo-ID per the confirmed mechanic (**blocked on B1**)
- [ ] `place_order`, `modify_order`, `cancel_order`
- [ ] `fetch_orderbook`, `fetch_positions`, `find_by_client_order_id`
- [ ] Map broker errors → `OrderRejectedError` / `AmbiguousOrderError` / `RateLimitError`
- [ ] Verify installed SDK exposes `market_protection`, fail startup if not

**Acceptance**
- A MARKET order without protection is impossible to construct
- A timeout raises `AmbiguousOrderError`, never a silent retry

---

### E02-S05 · Order rate limiting
`P0` `Phase 5` `🔴` `SEC` · **1 day** · deps: E01-S03, E02-S04

> As a compliance-conscious operator, I want the order rate capped in the
> adapter, so that no caller can breach the limit regardless of its behaviour.

**Tasks**
- [ ] Redis token bucket wrapping `place_order`
- [ ] Separate bucket for data endpoints
- [ ] Backpressure: queue with depth limit
- [ ] Reject new signals rather than delay orders unboundedly
- [ ] `orders_rate_limited_total` metric

**Acceptance**
- A deliberate flood of 100 orders/sec results in ≤ 3/sec reaching the broker
- Queue depth beyond threshold rejects rather than buffers

---

### E02-S06 · Instrument master synchronisation
`P0` `Phase 1` `🟠` `FEAT` · **1 day** · deps: E01-S02, E02-S03

> As a system, I need the current instrument master, so that symbols map to
> broker tokens, lot sizes and tick sizes correctly.

**Tasks**
- [ ] Daily fetch of the instrument dump
- [ ] Upsert into `instruments`
- [ ] Detect and log symbol changes, delistings, new listings
- [ ] Tick-size rounding helper verified against real instruments

**Acceptance**
- A limit price is always snapped to a valid tick before submission

---

### E02-S07 · Live margin retrieval
`P0` `Phase 5` `🔴` `FEAT` · **0.5 day** · deps: E02-S04

> As a risk engine, I need real available margin from the broker, so that
> sizing never assumes leverage that SEBI peak-margin rules do not permit.

**Tasks**
- [ ] `fetch_margins` returning `MarginSnapshot`
- [ ] Cache with a short TTL (margin moves during the session)
- [ ] Staleness guard — reject sizing on stale margin data

**Acceptance**
- Sizing uses live margin, never a computed leverage multiple

---

### E02-S08 · Fallback broker adapter (Fyers)
`P2` `Phase 2` `🟢` `FEAT` · **2 days** · deps: E02-S03

> As an operator, I want a second data source, so that a Zerodha WebSocket
> outage at the open does not blind the system.

**Tasks**
- [ ] Fyers auth and read-only adapter
- [ ] Failover logic on primary feed staleness
- [ ] Cross-source price divergence detection

---

### E02-S09 · Broker error taxonomy handling
`P0` `Phase 5` `🔴` `FEAT` · **1 day** · deps: E02-S04

> As a system, I want every broker failure classified correctly, so that an
> ambiguous outcome never results in a duplicate order.

**Tasks**
- [ ] Map every Kite error code to the taxonomy
- [ ] `AmbiguousOrderError` → query-by-`client_order_id` recovery path
- [ ] Never retry on `OrderRejectedError`
- [ ] Exponential backoff on `RateLimitError`
- [ ] Log every unmapped error code loudly for triage

**Acceptance**
- Simulated timeout + reconnect produces exactly one position (chaos test)

---
---

# EPIC 03 — HISTORICAL DATA

*Phase 1 · 6 stories · 8 days · Broker-agnostic, start immediately*

### E03-S01 · NSE bhavcopy fetcher and archiver
`P0` `Phase 1` `🟠` `FEAT` · **1.5 days** · deps: E01-S02

> As a system, I need daily EOD data stored locally, so that pre-market analysis
> does not depend on a rate-limited API every morning.

**Tasks**
- [ ] Download bhavcopy for a given date
- [ ] Parse and normalise to the `Bar` model
- [ ] Idempotent upsert (re-running a date is safe)
- [ ] Backfill utility for a date range
- [ ] Handle holidays and missing files gracefully
- [ ] Schedule at 05:30

**Acceptance**
- Backfilling 2 years completes and is re-runnable without duplicates

---

### E03-S02 · Corporate action adjustment
`P0` `Phase 1` `🔴` `FEAT` · **2 days** · deps: E03-S01

> As an analyst, I want historical prices adjusted for splits and bonuses, so
> that moving averages do not show phantom breakouts.

**Tasks**
- [ ] Fetch corporate action data
- [x] Store actions in `corporate_action`, the source of truth for adjustment
- [x] Derive `price_adj_factor` / `volume_adj_factor` per bar; raw OHLC untouched
- [x] Re-adjustment when a new action is announced — full recompute, idempotent
- [x] Verify against a known historical split

**Acceptance**
- A known 1:5 split produces a continuous price series across the event
- 🔴 Unadjusted data can never reach the indicator engine

**Status** — the schema, adjustment engine and BR-16 enforcement are built and
tested; only the *feed* remains, which is why this story is not closed. Fetching
corporate actions needs a source (broker API or exchange filing), so it carries
the same credential dependency as the rest of E03.

---

### E03-S03 · Intraday history backfill
`P0` `Phase 1` `🟠` `FEAT` · **1.5 days** · deps: E02-S03

> As a backtester, I need minute-level history, so that strategies can be
> validated on realistic intraday data.

**Tasks**
- [ ] Paged historical fetch respecting rate limits
- [ ] Backfill orchestration across the universe
- [ ] Resume from interruption
- [ ] Progress reporting (this takes hours)

---

### E03-S04 · Data quality validation
`P0` `Phase 1` `🟠` `TEST` · **1.5 days** · deps: E03-S01

> As a developer, I want stored data validated, so that silent corruption is
> caught before it reaches analysis.

**Tasks**
- [ ] Gap detection (missing sessions, missing bars within a session)
- [ ] OHLC coherence check across the whole store
- [ ] Volume sanity (zero-volume bars flagged)
- [ ] Price continuity (unexplained jumps beyond circuit limits)
- [ ] Report generated after every sync

**Acceptance**
- A deliberately corrupted row is reported by the next validation run

---

### E03-S05 · Multi-timeframe aggregation from stored data
`P0` `Phase 2` `🟠` `FEAT` · **1 day** · deps: E03-S01

> As the pre-market job, I need higher timeframes derived from daily data, so
> that weekly and monthly analysis is available without separate downloads.

**Tasks**
- [ ] Aggregate daily → weekly with correct session boundaries
- [ ] Handle partial weeks at period edges
- [ ] Verify aggregation against broker-supplied weekly bars

---

### E03-S06 · Tick archival to Parquet
`P2` `Phase 6` `🟢` `OPS` · **0.5 day** · deps: E05-S01

> As a developer, I want raw ticks archived, so that the replay harness can
> reproduce a session exactly.

---
---

# EPIC 04 — INDIA HAZARD LISTS

*Phase 1 · 7 stories · 6 days · **Easy to get wrong, hard to notice***

> These are the India-specific exclusions with no US equivalent. An algo that
> ignores them places orders that get rejected, or worse, incurs penalties.
> Build them early and eyeball the output for two weeks before anything depends
> on them.

### E04-S01 · ASM/GSM surveillance list fetcher
`P0` `Phase 1` `🔴` `FEAT` · **1 day** · deps: E01-S02

> As a risk-aware trader, I want surveillance-listed stocks excluded, so that I
> never trade a name carrying 100% margin or reduced circuit bands.

**Tasks**
- [ ] Fetch ASM (short-term and long-term) lists
- [ ] Fetch GSM list with stage
- [ ] Upsert into `instrument_daily_status`
- [ ] Alert on newly-added names currently held
- [ ] Handle source-format changes without silent failure

**Acceptance**
- A stock added to ASM overnight is excluded from today's universe
- 🔴 A fetch failure blocks the trading day rather than proceeding blind

---

### E04-S02 · T2T (Trade-to-Trade) list
`P0` `Phase 1` `🔴` `FEAT` · **0.5 day** · deps: E01-S02

> As a trader, I want T2T stocks excluded absolutely, so that I never place an
> intraday order that becomes an unintended delivery obligation.

**Tasks**
- [ ] Fetch the T2T segment list
- [ ] Mark `is_t2t` in daily status
- [ ] Hard filter — not configurable (config validation already enforces this)

**Acceptance**
- 🔴 No T2T symbol can reach the watchlist under any configuration

---

### E04-S03 · F&O ban list
`P0` `Phase 1` `🔴` `FEAT` · **0.5 day** · deps: E01-S02

> As a trader, I want ban-period stocks flagged, so that I avoid a penalty of
> 1% of position value (min ₹5,000, max ₹1,00,000).

**Tasks**
- [ ] Fetch the daily ban list
- [ ] Mark `is_fno_ban`
- [ ] Exclude from F&O trading; flag (not exclude) for equity

---

### E04-S04 · Circuit bands and price limits
`P0` `Phase 1` `🟠` `FEAT` · **1 day** · deps: E01-S02

> As a trader, I want per-stock circuit bands known, so that narrow-band stocks
> are excluded and orders never target an unreachable price.

**Tasks**
- [ ] Fetch band percentages per symbol
- [ ] Compute upper/lower circuit prices from previous close
- [ ] Filter by `min_circuit_band_pct`
- [ ] Reject orders outside band at validation time

---

### E04-S05 · CAS classification
`P0` `Phase 1` `🔴` `FEAT` · **1 day** · deps: E01-S02

> As a position manager, I need to know which stocks are in CAS scope, so that
> each position uses the correct square-off deadline.

**Tasks**
- [ ] Determine CAS-scope (Category I / F&O) symbols
- [ ] Mark `is_cas_stock`
- [ ] Wire into `MarketCalendar.squareoff_deadline`
- [ ] Alert on classification changes

**Acceptance**
- 🔴 A CAS stock's deadline is 15:10, a non-CAS stock's is 15:20, per position

---

### E04-S06 · Earnings and corporate action calendar
`P1` `Phase 1` `🟠` `FEAT` · **1 day** · deps: E01-S02

> As a trader, I want earnings-day stocks excluded, so that a technical thesis
> is not invalidated by a binary event.

**Tasks**
- [ ] Fetch results calendar
- [ ] Fetch dividend/split/bonus announcements
- [ ] Mark `has_earnings_today`
- [ ] Configurable exclusion window (before/after)

---

### E04-S07 · NSE holiday calendar — complete it
`P0` `Phase 1` `🔴` `OPS` · **1 day** · deps: none · **carried debt**

> As a system, I need the complete NSE holiday list, so that I do not treat a
> market holiday as a trading day.

**Tasks**
- [ ] Transcribe the full NSE holiday circular into `nse_holidays.yaml`
- [ ] Set `verified_against_nse_circular: true`
- [ ] Decide Diwali Muhurat session policy (default: stand down)
- [ ] Annual refresh reminder documented in the runbook
- [ ] Consider automated fetch with manual verification

**Acceptance**
- 🔴 `make doctor` reports the calendar as verified
- Every holiday in the circular is excluded by `is_trading_day`

---
---

# EPIC 05 — MARKET DATA INGESTION

*Phase 1 · 8 stories · 10 days*

### E05-S01 · WebSocket client with reconnection
`P0` `Phase 1` `🟠` `FEAT` · **2 days** · deps: E02-S03

> As a system, I want a resilient tick stream, so that a dropped connection does
> not silently stop the market data flow.

**Tasks**
- [ ] KiteTicker integration with async bridging
- [ ] Subscribe/unsubscribe for the day's watchlist
- [ ] Exponential backoff reconnection
- [ ] **On reconnect: mark indicator state STALE**, do not resume silently
- [ ] Heartbeat monitoring with staleness detection
- [ ] Rebuild the in-progress bar from a REST snapshot after a gap

**Acceptance**
- A 5-minute forced disconnect recovers and flags the gap
- 🟠 Indicators are marked stale rather than computing across the hole

---

### E05-S02 · Tick validation
`P0` `Phase 1` `🟠` `FEAT` · **1 day** · deps: E05-S01

> As a data pipeline, I want malformed ticks rejected at the edge, so that
> nothing downstream has to defend against them.

**Tasks**
- [ ] Reject null/zero/negative price
- [ ] Reject negative volume
- [ ] Reject timestamp outside ±5s of local clock (broker clock skew)
- [ ] Metric per rejection reason
- [ ] Log rejections — a cluster indicates a feed problem

---

### E05-S03 · Deduplication
`P0` `Phase 1` `🟠` `FEAT` · **0.5 day** · deps: E05-S02

> As a data pipeline, I want duplicate ticks dropped, so that WebSocket replay
> after a reconnect does not double-count volume.

**Tasks**
- [ ] Bounded LRU per symbol on (symbol, exchange_ts, ltp, volume)
- [ ] Size-capped to bound memory
- [ ] Metric for duplicates dropped

---

### E05-S04 · Outlier filtering
`P0` `Phase 1` `🔴` `FEAT` · **1 day** · deps: E05-S03

> As an analyst, I want bad prints rejected, so that one erroneous tick does not
> permanently corrupt an EMA.

**Tasks**
- [ ] Reject if move exceeds `max(5 × ATR%, 2%)`
- [ ] Cross-check against circuit limits — a price outside them is impossible
- [ ] Log rather than silently drop
- [ ] Alert on a cluster of rejections for one symbol

**Acceptance**
- 🔴 A single injected bad print does not change any indicator value

---

### E05-S05 · Normalisation
`P0` `Phase 1` `🟠` `FEAT` · **0.5 day** · deps: E05-S04

> As a developer, I want broker payloads converted to canonical types at the
> boundary, so that no broker-specific shape leaks downstream.

**Tasks**
- [ ] Kite payload → `Tick` model
- [ ] All timestamps → UTC
- [ ] All prices → `Decimal`

---

### E05-S06 · Bar builder with session alignment
`P0` `Phase 1` `🔴` `FEAT` · **2 days** · deps: E05-S05

> As an analyst, I want bars aligned to the session start, so that a 15-minute
> bar runs 09:15–09:30 and indicators are not silently offset.

**Tasks**
- [ ] In-progress bar per (symbol, timeframe)
- [ ] Boundary detection via `MarketCalendar.bar_open_time`
- [ ] Seal and publish on boundary crossing
- [ ] Emit in-progress updates with `is_final=False`
- [ ] Cascade 1m → 5m → 15m → 1h within the builder

**Acceptance**
- 🔴 Bar boundaries match the calendar exactly across a full session
- Consumers can distinguish final from in-progress bars

---

### E05-S07 · Synthetic bar handling
`P0` `Phase 1` `🟠` `FEAT` · **0.5 day** · deps: E05-S06

> As an analyst, I want no-trade intervals marked, so that indicators do not
> treat a data gap as a price move.

**Tasks**
- [ ] Carry-forward bar when no trades occur
- [ ] Flag `synthetic=True`
- [ ] Indicator engine skips or handles synthetics explicitly

---

### E05-S08 · Quote state publishing
`P0` `Phase 1` `🟢` `FEAT` · **0.5 day** · deps: E05-S05

> As the dashboard and risk engine, I need current quotes, so that P&L and
> spread checks use live prices.

**Tasks**
- [ ] Write `state:quote:{symbol}` on each tick
- [ ] Include bid/ask for spread checks
- [ ] Publish to `stream:ticks` for archival

---
---

# EPIC 06 — TECHNICAL ANALYSIS ENGINE

*Phase 2 · 8 stories · 12 days*

### E06-S01 · Incremental indicator framework
`P0` `Phase 2` `🔴` `FEAT` · **2 days** · deps: E05-S06

> As a system, I want indicators updated in O(1) per bar, so that adding symbols
> does not degrade latency.

**Tasks**
- [ ] `Indicator` protocol: `update(bar) -> value`, `warm_up(bars)`, `is_ready`
- [ ] TA-Lib streaming API wrapper
- [ ] `IndicatorSet` per (symbol, timeframe)
- [ ] State serialisation to Redis
- [ ] Restore state on restart without full re-warm-up

**Acceptance**
- 🔴 Incremental values match batch computation to 4 decimal places
- 200 symbols × 6 timeframes update in < 50ms total

---

### E06-S02 · Core indicator implementations
`P0` `Phase 2` `🟠` `FEAT` · **2 days** · deps: E06-S01

**Tasks**
- [ ] EMA (20, 50, 200), SMA
- [ ] RSI(14)
- [ ] MACD with signal and histogram
- [ ] ATR(14) — **critical, drives position sizing**
- [ ] Bollinger Bands
- [ ] VWAP (session-anchored)
- [ ] Volume ratio vs 20-period average
- [ ] ADX

**Acceptance**
- Every indicator verified against a reference implementation on the same data

---

### E06-S03 · Warm-up orchestration
`P0` `Phase 2` `🔴` `FEAT` · **1.5 days** · deps: E06-S01, E03-S01

> As a trader, I want indicators seeded from history before trading, so that a
> 200-EMA is never computed from 3 bars.

**Tasks**
- [ ] Load lookback from TimescaleDB at session start
- [ ] Parallel warm-up in `ProcessPoolExecutor`
- [ ] `is_ready` false until the longest-period indicator has enough data
- [ ] Report warm-up completion per symbol
- [ ] Block trading until the watchlist is warm

**Acceptance**
- 🔴 A symbol with insufficient history is excluded, not traded on bad values

---

### E06-S04 · Multi-timeframe snapshot assembly
`P0` `Phase 2` `🟠` `FEAT` · **1 day** · deps: E06-S02

**Tasks**
- [ ] Assemble `MultiTimeframeSnapshot` from per-timeframe state
- [ ] `all_ready` gate
- [ ] Publish to Redis for `signal-engine` to read without IPC

---

### E06-S05 · Support/resistance and pivot detection
`P0` `Phase 2` `🟠` `FEAT` · **2 days** · deps: E06-S02

> As a strategy, I need structural levels, so that stops can be placed at points
> that invalidate the thesis rather than at arbitrary distances.

**Tasks**
- [ ] Classic pivot points (P, R1-R3, S1-S3)
- [ ] Prior-day high/low/close
- [ ] Swing high/low detection
- [ ] Level clustering (nearby levels merge)
- [ ] Level strength scoring by touch count

---

### E06-S06 · Opening range computation
`P0` `Phase 2` `🔴` `FEAT` · **1 day** · deps: E06-S02

> As the primary strategy, I need the 09:15–09:30 range, so that breakouts can
> be detected against a well-defined reference.

**Tasks**
- [ ] Track high/low from 09:15 to 09:30
- [ ] Seal at 09:30 and publish
- [ ] Compute range as % of price
- [ ] Handle the gap-open case

**Acceptance**
- 🔴 Range is sealed exactly at 09:30 and never mutates afterwards

---

### E06-S07 · Relative strength calculation
`P0` `Phase 2` `🟠` `FEAT` · **1.5 days** · deps: E06-S02

> As a scorer, I need stock-vs-sector and sector-vs-index strength, so that
> leaders in leading sectors rank higher.

**Tasks**
- [ ] Sector index mapping per symbol
- [ ] Rolling relative performance
- [ ] Sector-vs-Nifty ranking
- [ ] Normalise to a 0–1 score

---

### E06-S08 · Chart and candlestick patterns
`P2` `Phase 6` `🟢` `FEAT` · **1 day** · deps: E06-S02

Flags, triangles, double tops/bottoms; TA-Lib candlestick recognition.

---
---

# EPIC 07 — UNIVERSE & SCORING

*Phase 2 · 6 stories · 7 days*

### E07-S01 · Base universe loading
`P0` `Phase 2` `🟢` `FEAT` · **0.5 day** · deps: E02-S06

**Tasks**
- [ ] Load Nifty 50/100/200/500 constituents
- [ ] F&O universe
- [ ] Custom symbol list support
- [ ] Handle index reconstitution

---

### E07-S02 · Hard filter pipeline
`P0` `Phase 2` `🔴` `FEAT` · **1.5 days** · deps: E04 (all), E07-S01

> As a trader, I want structurally untradeable stocks removed before scoring, so
> that no ineligible name can reach an order.

**Tasks**
- [ ] Apply all 10 filters in order (cheapest first)
- [ ] Record which filter excluded each symbol
- [ ] **Live preview**: how many survive each filter today
- [ ] Alert if the surviving universe is implausibly small

**Acceptance**
- 🔴 Every filter in config is applied; none can be silently skipped
- The exclusion reason for any symbol is queryable

---

### E07-S03 · Tradeability score components
`P0` `Phase 2` `🟠` `FEAT` · **2 days** · deps: E06-S04, E06-S07

**Tasks**
- [ ] Multi-timeframe trend alignment (25%)
- [ ] Relative strength (20%)
- [ ] Volatility fitness — scored as a *band*, not more-is-better (15%)
- [ ] Volume expansion (15%)
- [ ] Level proximity (15%)
- [ ] Catalyst/news (10%) — stub until E08
- [ ] Weighted composite, 0–100

**Acceptance**
- Weights come from config and are verified to sum to 1.0
- Each component is independently unit-tested

---

### E07-S04 · Ranking and shortlisting
`P0` `Phase 2` `🟢` `FEAT` · **0.5 day** · deps: E07-S03

**Tasks**
- [ ] Rank surviving universe by score
- [ ] Take top N (`shortlist_size`)
- [ ] Tie-breaking rule (deterministic)

---

### E07-S05 · Score explainability
`P0` `Phase 2` `🟠` `FEAT` · **1 day** · deps: E07-S03

> As a trader, I want to see why a stock scored what it did, so that a bare
> number is not unaccountable.

**Tasks**
- [ ] Persist per-component contributions
- [ ] Render breakdown in the UI and briefing
- [ ] Explain rank changes day over day

---

### E07-S06 · Universe validation report
`P1` `Phase 2` `🟢` `TEST` · **1.5 days** · deps: E07-S04

> As a developer, I want to compare the ranked watchlist against what actually
> moved, so that the scoring model can be tuned on evidence.

**Tasks**
- [ ] Daily: capture the ranked list
- [ ] EOD: compare against realised ranges and volumes
- [ ] Hit-rate metric: how often did top-10 names actually move?
- [ ] Report over rolling 20 sessions

---
---

# EPIC 08 — NEWS INTELLIGENCE

*Phase 3 · 9 stories · 12 days*

### E08-S01 · News source integration
`P1` `Phase 3` `🟠` `FEAT` · **1.5 days** · deps: E01-S02

**Tasks**
- [ ] Allowlisted provider clients
- [ ] Polling on `news.refresh_interval_min`
- [ ] Per-source timeout and failure isolation
- [ ] Raw article persistence for audit

**Acceptance**
- A dead source degrades its contribution, never fails the refresh

---

### E08-S02 · Near-duplicate detection and clustering
`P1` `Phase 3` `🟠` `FEAT` · **1.5 days** · deps: E08-S01

> As an analyst, I want syndicated copies of the same story grouped, so that one
> event carried by six outlets is not counted six times.

**Tasks**
- [ ] Similarity-based clustering
- [ ] `cluster_id` assignment
- [ ] Cluster size feeds salience (positively) and novelty (inversely)

---

### E08-S03 · Prompt injection sanitisation 🔴
`P0` `Phase 3` `🔴` `SEC` · **2 days** · deps: E08-S01

> As a security-conscious operator, I want news content sanitised before it
> reaches any prompt, so that an attacker publishing crafted content cannot
> steer trading decisions.

**Tasks**
- [ ] Strip/escape XML-ish tags and role markers
- [ ] Detect instruction-injection phrases ("ignore previous", "system:")
- [ ] Strip bidirectional Unicode overrides and zero-width characters
- [ ] Length truncation
- [ ] Set `injection_flagged` and **exclude flagged articles from scoring**
- [ ] Metric per source — a rising rate is a source to drop
- [ ] Adversarial test corpus (see E22-S06)

**Acceptance**
- 🔴 Every payload in the adversarial corpus is neutralised
- Flagged articles are visible in the admin UI

---

### E08-S04 · Entity resolution
`P1` `Phase 3` `🟠` `FEAT` · **1 day** · deps: E08-S02

**Tasks**
- [ ] Article → ticker(s) mapping
- [ ] Sector and macro classification
- [ ] Ambiguity handling (company names that are common words)

---

### E08-S05 · AI news triage
`P1` `Phase 3` `🔴` `FEAT` · **1.5 days** · deps: E08-S03, E10-S01

> As a system, I want structured extraction from each article, so that only
> bounded fields propagate downstream.

**Tasks**
- [ ] Haiku call with `NewsSignal` structured output
- [ ] Event type, polarity, magnitude, certainty, horizon, scope
- [ ] Batch articles to control cost
- [ ] Handle refusals and schema failures by skipping the article

**Acceptance**
- 🔴 No free-form field from news reaches any downstream prompt

---

### E08-S06 · Novelty scoring
`P1` `Phase 3` `🟠` `FEAT` · **1.5 days** · deps: E08-S05

> As an analyst, I want new information distinguished from echo, so that the
> tenth repetition of a story does not score like the first.

**Tasks**
- [ ] Rolling 30-day news history per symbol
- [ ] Semantic similarity to recent articles
- [ ] Event-type rarity for this symbol
- [ ] Polarity surprise vs prior tone
- [ ] Inverse cluster size
- [ ] Weighted novelty score

---

### E08-S07 · Time decay by event class
`P1` `Phase 3` `🟠` `FEAT` · **0.5 day** · deps: E08-S05

**Tasks**
- [ ] Exponential decay using per-class half-lives from `NewsEventType`
- [ ] Decay measured in *trading* days, not calendar days
- [ ] Verify a weekend does not over-decay

---

### E08-S08 · Composite score assembly
`P1` `Phase 3` `🟠` `FEAT` · **1.5 days** · deps: E08-S06, E08-S07

**Tasks**
- [ ] Salience: log cluster size × source credibility × corroboration
- [ ] Contribution = polarity × magnitude × certainty × novelty × salience × decay
- [ ] **Three separate scores** — NewsScore (firm), SectorScore, MacroScore
- [ ] Clamp to [-1, +1]
- [ ] Stale pipeline → score 0, never last-known-value

**Acceptance**
- Scores are never blended into one number
- A stale pipeline is visibly stale, not silently old

---

### E08-S09 · News UI surface
`P1` `Phase 4` `🟢` `FEAT` · **1 day** · deps: E08-S08, E17-S05

Show today's articles and decayed older contributions on symbol detail.

---
---

# EPIC 09 — MACRO CONTEXT

*Phase 3 · 8 stories · 8 days*

### E09-S01 · GIFT Nifty gap prediction
`P1` `Phase 3` `🟠` `FEAT` · **1 day**

> As a trader, I want the expected opening gap, so that the pre-market plan
> accounts for where the market will actually open.

**Tasks**
- [ ] Fetch GIFT Nifty at ~09:00
- [ ] Compute premium/discount vs previous Nifty close
- [ ] **Direction only** — magnitude is unreliable (~85–90% direction accuracy)

---

### E09-S02 · India VIX ingestion
`P1` `Phase 3` `🟢` `FEAT` · **0.5 day**

Current level, 30-day percentile, direction. Feeds regime and sizing.

---

### E09-S03 · FII/DII flow data
`P1` `Phase 3` `🟢` `FEAT` · **1 day**

Daily cash and F&O net figures; rolling 3-day trend.

---

### E09-S04 · Sector rotation ranking
`P1` `Phase 3` `🟠` `FEAT` · **1.5 days**

**Tasks**
- [ ] Fetch 24+ NSE sectoral indices
- [ ] **EOD/multi-day strength, not intraday** — intraday ranks are noise
- [ ] Relative strength vs Nifty
- [ ] Breadth per sector

---

### E09-S05 · Economic calendar and event blackout
`P1` `Phase 3` `🔴` `FEAT` · **1.5 days**

> As a risk-aware trader, I want to stand down around scheduled volatility
> events, so that a technical thesis is not run over by an RBI announcement.

**Tasks**
- [ ] Fetch RBI policy, CPI, GDP, budget dates
- [ ] Blackout windows from config
- [ ] Union Budget full-day halt
- [ ] Block new entries during blackout

**Acceptance**
- 🔴 No entry is placed inside a configured blackout window

---

### E09-S06 · Market regime classification
`P1` `Phase 3` `🟠` `FEAT` · **1.5 days** · deps: E09-S02, E09-S04

**Tasks**
- [ ] Rule-based classifier: VIX + index trend + breadth + flows
- [ ] Output: RISK_ON / RISK_OFF / HIGH_VOL / LOW_VOL / TRENDING / RANGEBOUND
- [ ] Hysteresis to prevent flapping
- [ ] Log regime changes

---

### E09-S07 · MarketContext assembly with staleness
`P1` `Phase 3` `🔴` `FEAT` · **1 day** · deps: E09-S01..S06

> As a consumer, I want every macro field to carry its own age, so that I cannot
> read stale data without knowing.

**Tasks**
- [ ] Concurrent collection with per-collector timeout
- [ ] `FieldWithStaleness[T]` wrapper — value plus `as_of` plus `is_stale`
- [ ] Failed collector → `None` + flag, never an exception
- [ ] `degraded_fields` list on the context
- [ ] Cache in Redis with TTL

**Acceptance**
- 🔴 A dead collector produces a usable context with an explicit gap

---

### E09-S08 · Intermarket signals
`P2` `Phase 6` `🟢` `FEAT` · **1 day**

USD/INR and crude effects on sector expectations.

---
---

# EPIC 10 — AI LAYER

*Phase 3 · 8 stories · 10 days*

### E10-S01 · Anthropic client wrapper
`P0` `Phase 3` `🔴` `FEAT` · **1.5 days**

**Tasks**
- [ ] Async client with per-tier model selection
- [ ] Timeout per tier from config
- [ ] Retry with backoff on transient errors
- [ ] **Check `stop_reason` before reading `content`**
- [ ] Structured request/response logging with token counts

**Acceptance**
- 🔴 A refusal never crashes the caller

---

### E10-S02 · Prompt template system with caching
`P0` `Phase 3` `🔴` `FEAT` · **2 days** · deps: E10-S01

> As a cost-conscious operator, I want prompts structured for cache hits, so
> that repeated calls do not pay full price.

**Tasks**
- [ ] Three-layer structure: frozen system / daily context / volatile per-call
- [ ] `cache_control` breakpoints at the right boundaries
- [ ] Versioned templates in `ai/prompts/`
- [ ] **No timestamps, UUIDs, or unsorted dicts in the cached prefix**
- [ ] Assert prompt determinism in tests

**Acceptance**
- 🔴 `cache_read_input_tokens` > 0 on the second identical-prefix call
- Alert if the cache hit rate drops below threshold

---

### E10-S03 · Structured output schemas
`P0` `Phase 3` `🔴` `FEAT` · **1 day** · deps: E10-S01

**Tasks**
- [ ] `AIReview`, `DailyThesis`, `Playbook`, `NewsSignal` Pydantic models
- [ ] `messages.parse()` integration
- [ ] Schema validation failure → skip, log raw response
- [ ] No `temperature`/`top_p` (rejected by current models)

---

### E10-S04 · Token budget and cost control
`P0` `Phase 3` `🔴` `FEAT` · **1 day** · deps: E10-S01

**Tasks**
- [ ] Redis counter for daily token spend
- [ ] Alert at `alert_at_pct`
- [ ] **Hard stop at 100% → degrade to score-only mode, keep trading**
- [ ] Per-tier cost attribution
- [ ] Daily cost report

**Acceptance**
- 🔴 Budget exhaustion degrades capability; it never halts risk management

---

### E10-S05 · In-session AI review
`P0` `Phase 4` `🔴` `FEAT` · **1.5 days** · deps: E10-S02, E10-S03

**Tasks**
- [ ] Render trigger + MTF snapshot + market context into the volatile layer
- [ ] Sonnet call with `AIReview` output
- [ ] Timeout → **skip trade** (fail closed)
- [ ] Bounded concurrency via semaphore
- [ ] Full request/response to audit log

---

### E10-S06 · Response caching for backtest
`P1` `Phase 5` `🟠` `FEAT` · **1.5 days** · deps: E10-S01

> As a backtester, I want historical AI outputs replayed rather than re-called,
> so that backtests are deterministic and free.

**Tasks**
- [ ] Cache keyed by prompt hash
- [ ] Replay mode reads cache, never calls the API
- [ ] Cache-miss policy in replay: fail loudly, do not silently call

---

### E10-S07 · Refusal handling and fallback
`P1` `Phase 4` `🔴` `SEC` · **1 day** · deps: E10-S01

**Tasks**
- [ ] Detect `stop_reason == "refusal"` with category
- [ ] Log and skip the trade
- [ ] Alert if refusals cluster (may indicate a prompt problem)

---

### E10-S08 · Model tier routing and evaluation
`P2` `Phase 6` `🟢` `FEAT` · **0.5 day**

A/B a cheaper tier for in-session review and compare decision quality.

---
---

# EPIC 11 — PRE-MARKET ENGINE

*Phase 3 · 8 stories · 10 days · **The centrepiece***

### E11-S01 · Pipeline orchestration with checkpointing
`P0` `Phase 3` `🔴` `FEAT` · **2 days** · deps: E01-S02

> As an operator, I want the pre-market pipeline to resume from where it failed,
> so that a crash at 08:50 does not mean recomputing three hours of work with
> 25 minutes until the open.

**Tasks**
- [ ] Stage abstraction with per-stage deadline
- [ ] Checkpoint each stage output keyed by (date, stage)
- [ ] Resume from the last completed stage
- [ ] Overall deadline: hard stop at 09:12
- [ ] Alert on stage overrun

**Acceptance**
- 🔴 Killing the job mid-pipeline and restarting resumes correctly
- The plan is published by 09:12 or the day is flagged degraded

---

### E11-S02 · Data sync stage (05:30)
`P0` `Phase 3` `🟠` `FEAT` · **0.5 day** · deps: E03-S01, E04 (all)

---

### E11-S03 · Universe construction stage (06:30)
`P0` `Phase 3` `🟠` `FEAT` · **0.5 day** · deps: E07-S02

---

### E11-S04 · Multi-timeframe analysis stage (07:30)
`P0` `Phase 3` `🟠` `FEAT` · **2 days** · deps: E06-S03

> As the heaviest compute of the day, this must complete in 45 minutes across
> the eligible universe.

**Tasks**
- [ ] `ProcessPoolExecutor` fan-out across symbol slices
- [ ] Weekly/daily/hourly indicator computation from stored history
- [ ] Serialise snapshots back to the parent
- [ ] Progress reporting
- [ ] Graceful degradation if a symbol's history is insufficient

**Acceptance**
- 200 symbols × 3 timeframes complete in < 30 minutes

---

### E11-S05 · News and macro sweep stage (08:15)
`P0` `Phase 3` `🟠` `FEAT` · **0.5 day** · deps: E08, E09

---

### E11-S06 · AI deep synthesis stage (08:45) ⭐
`P0` `Phase 3` `🔴` `FEAT` · **2 days** · deps: E10-S02, E11-S04

> As a trader, I want a written market thesis and per-stock playbooks before the
> open, so that the session is execution of a plan rather than analysis under
> pressure.

**Tasks**
- [ ] Assemble shortlist + MTF features + news + macro + journal into the prompt
- [ ] Opus call with `DailyThesis` + `Playbook[]` structured output
- [ ] Thesis: regime, bias, key levels, **invalidation conditions**
- [ ] Playbook per candidate: setup, why, confirms, invalidates, direction
- [ ] AI may veto or re-rank within the shortlist
- [ ] Persist with token usage and latency
- [ ] **Deadline 09:00 → proceed score-only, mark `ai_unavailable`**

**Acceptance**
- 🔴 A timeout produces a valid score-only plan, not a missing plan
- Every candidate's playbook is traceable to its inputs

---

### E11-S07 · Pre-open gap adjustment (09:02)
`P0` `Phase 3` `🔴` `FEAT` · **1.5 days** · deps: E09-S01, E11-S06

> As a trader, I want the plan re-ranked with actual opening prices, so that a
> stock that gapped past its entry is not traded on a stale thesis.

**Tasks**
- [ ] Read pre-open equilibrium prices
- [ ] Compute per-stock gap vs previous close
- [ ] **Invalidate** if gap exceeds `invalidate_if_gap_exceeds_pct`
- [ ] Promote gap-and-go candidates
- [ ] Keep invalidated candidates visible with reason

**Acceptance**
- 🔴 A candidate that gapped past its entry cannot be traded on the old plan

---

### E11-S08 · Plan lock and briefing delivery (09:12)
`P0` `Phase 3` `🔴` `FEAT` · **1 day** · deps: E11-S07, E19-S02

**Tasks**
- [ ] Freeze the plan — immutable for the session
- [ ] Record config hash with the plan
- [ ] Persist to DB and Redis
- [ ] Verify broker auth, margin, connectivity
- [ ] Deliver briefing to Telegram by 09:13

**Acceptance**
- 🔴 The plan cannot be modified after lock
- Briefing arrives before 09:15 on 20 consecutive sessions

---
---

# EPIC 12 — STRATEGY ENGINE

*Phase 5 · 12 stories · 18 days · **Build the gauntlet before generation***

### E12-S01 · Strategy registry and persistence
`P0` `Phase 5` `🟠` `FEAT` · **1.5 days** · deps: E01-S01

**Tasks**
- [ ] Persist `StrategyDocument` to the `strategy` table
- [ ] Load and compile on startup
- [ ] Version and lineage tracking
- [ ] Content-hash deduplication

---

### E12-S02 · Lifecycle state machine
`P0` `Phase 5` `🔴` `FEAT` · **1.5 days** · deps: E12-S01

**Tasks**
- [ ] Implement the state machine from STRATEGY_ENGINE §4
- [ ] Gate conditions per transition
- [ ] **Human approval required for ACTIVE — not overridable**
- [ ] State change audit trail
- [ ] Illegal transitions rejected

**Acceptance**
- 🔴 No code path promotes a strategy to ACTIVE without a recorded human approval

---

### E12-S03 · Backtest harness
`P0` `Phase 5` `🔴` `FEAT` · **3 days** · deps: E06-S04, E10-S06

> As a developer, I want backtests that execute the *production* strategy code
> against recorded snapshots, so that there is no reimplementation gap where
> backtest/live divergence hides.

**Tasks**
- [ ] Replay `MultiTimeframeSnapshot` sequences from history
- [ ] Execute the real `Strategy.evaluate`
- [ ] Simulate fills with realistic slippage
- [ ] **Full India cost model**: brokerage, STT, GST, exchange, stamp duty
- [ ] Respect square-off deadlines and hazard exclusions
- [ ] Equity curve, trade list, statistics

**Acceptance**
- 🔴 The same strategy code runs in backtest and live
- Costs match a real contract note on a sample trade

---

### E12-S04 · Trial registry
`P0` `Phase 5` `🔴` `SEC` · **1 day** · deps: E01-S01

> As a statistician, I want every backtest counted forever, so that overfitting
> corrections have an honest denominator.

**Tasks**
- [ ] Write a row for every gauntlet run, pass or fail
- [ ] **Parameter sweeps count individually**
- [ ] Content-hash deduplication of identical re-runs
- [ ] Effective trial count query for DSR
- [ ] Append-only DB role

**Acceptance**
- 🔴 Deleting a trial row is impossible with the application's DB role
- Testing one strategy at 20 settings records 20 trials

---

### E12-S05 · Gauntlet G1–G4 (structural)
`P0` `Phase 5` `🔴` `FEAT` · **1.5 days** · deps: E12-S03

Hypothesis present · compiles · minimum 100 trades · realistic costs applied.

---

### E12-S06 · Gauntlet G5 — purged/embargoed walk-forward
`P0` `Phase 5` `🔴` `FEAT` · **2.5 days** · deps: E12-S03

> As a statistician, I want CPCV with purging and embargo, so that label leakage
> does not make a bad strategy look good.

**Tasks**
- [ ] Combinatorial purged cross-validation splitter
- [ ] Purge training samples whose label windows overlap the test set
- [ ] Embargo period after each test block
- [ ] Per-fold performance consistency check

**Acceptance**
- 🔴 A deliberately leaky strategy is caught by purging

---

### E12-S07 · Gauntlet G6/G7 — DSR and PBO
`P0` `Phase 5` `🔴` `FEAT` · **2.5 days** · deps: E12-S04, E12-S06

> As a statistician, I want Sharpe deflated for the number of trials and the
> probability of overfitting computed, so that a lucky backtest cannot reach
> live capital.

**Tasks**
- [ ] Deflated Sharpe Ratio using the trial count, sample length, skew, kurtosis
- [ ] PBO via combinatorially symmetric cross-validation
- [ ] Reject on DSR confidence < 0.95 or PBO > 0.5
- [ ] Persist both with the trial count used

**Acceptance**
- 🔴 A strategy fitted to noise fails at least one of these
- Verified against a known-random strategy (should fail) and a known-real edge

---

### E12-S08 · Gauntlet G8–G12
`P1` `Phase 5` `🔴` `FEAT` · **2 days** · deps: E12-S07

Regime coverage · locked holdout (read once) · correlation to active ·
parameter sensitivity (±20%) · India tradability.

---

### E12-S09 · Shadow mode
`P0` `Phase 5` `🟠` `FEAT` · **1.5 days** · deps: E12-S02, E13-S01

> As a validator, I want strategies to record what they *would* have done, so
> that failures a backtest cannot show — signals at untradeable prices — surface
> before any capital is involved.

**Tasks**
- [ ] Evaluate SHADOW strategies in the live loop
- [ ] Record to `shadow_signal`, place no orders
- [ ] EOD: evaluate hypothetical outcomes
- [ ] Live-vs-backtest agreement metric
- [ ] Promotion gate at ≥ 20 sessions, ≥ 80% agreement

---

### E12-S10 · Degradation monitoring and auto-retirement
`P0` `Phase 5` `🔴` `FEAT` · **1.5 days** · deps: E12-S02

> As a trader, I want a decaying strategy demoted without asking me, so that a
> losing streak does not persist through inattention.

**Tasks**
- [ ] Rolling window performance vs validated expectation
- [ ] Triggers: Sharpe ratio, consecutive losses, drawdown, win rate, regime absence
- [ ] ACTIVE → DEGRADED (50% size) automatically
- [ ] DEGRADED → RETIRED after continued decay
- [ ] `vs_backtest_ratio` tracked daily
- [ ] Alert on every demotion

**Acceptance**
- 🔴 Demotion is automatic; promotion is never automatic

---

### E12-S11 · AI strategy generation — journal mode
`P2` `Phase 9` `🔴` `FEAT` · **2.5 days** · deps: E12-S07, E12-S10

> As a trader, I want the AI to propose strategies from my own trade history, so
> that the system learns from what actually happened rather than from patterns
> in a price series.

**Tasks**
- [ ] Assemble journal summary: setups, regimes, confidence, outcomes, R-multiples
- [ ] **Hypothesis-first protocol**: AI states mechanism BEFORE seeing results
- [ ] Freeze hypothesis to DB before the gauntlet runs
- [ ] Emit `StrategyDocument` in the DSL — never code
- [ ] Weekly cadence, max 5 proposals
- [ ] Every proposal enters the gauntlet like any other

**Acceptance**
- 🔴 The AI cannot bypass any gauntlet check
- 🔴 A proposal without a substantive hypothesis is rejected at parse time

---

### E12-S12 · AI strategy generation — observation mode
`P2` `Phase 9` `🔴` `FEAT` · **2 days** · deps: E12-S11

Market-behaviour summary → strategy proposals. Same constraints as S11.

---
---

# EPIC 13 — SIGNAL ENGINE

*Phase 4 · 6 stories · 8 days*

### E13-S01 · Strategy evaluation loop
`P0` `Phase 4` `🔴` `FEAT` · **2 days** · deps: E06-S04, E12-S01

**Tasks**
- [ ] Consume final bars from the stream
- [ ] Filter to the day's plan symbols
- [ ] Gate on `all_ready`
- [ ] Evaluate runnable strategies (regime-filtered)
- [ ] Route by strategy state: ACTIVE → full path, PAPER → paper, SHADOW → record

**Acceptance**
- 🔴 A symbol not in today's plan is never evaluated
- 🔴 A strategy in SHADOW state never produces an order

---

### E13-S02 · Regime-aware strategy selection
`P0` `Phase 4` `🟠` `FEAT` · **1 day** · deps: E09-S06, E13-S01

Only evaluate strategies whose `applicability.regimes` includes the current regime.

---

### E13-S03 · AI review integration
`P0` `Phase 4` `🔴` `FEAT` · **1.5 days** · deps: E10-S05, E13-S01

**Tasks**
- [ ] Call AI review only for triggers that fired
- [ ] Confidence gate: below `min_to_act` → reject with reason
- [ ] Verdict gate: VETO → reject with reason
- [ ] Timeout → skip
- [ ] Every decision to the audit log

---

### E13-S04 · Recommendation emission
`P0` `Phase 4` `🔴` `FEAT` · **1 day** · deps: E13-S03

**Tasks**
- [ ] Build `Recommendation` from trigger + review
- [ ] Publish to `stream:signals`
- [ ] Correlation ID threading

**Acceptance**
- 🔴 `Recommendation` carries no sizing information (enforced by the type)

---

### E13-S05 · Concurrency guards
`P0` `Phase 4` `🔴` `FEAT` · **1 day** · deps: E01-S03, E13-S01

**Tasks**
- [ ] `lock:symbol:{symbol}` before emitting
- [ ] Prevent two strategies double-entering the same symbol
- [ ] Lock TTL so a crash cannot deadlock

---

### E13-S06 · Thesis invalidation monitoring
`P2` `Phase 6` `🟠` `FEAT` · **1.5 days** · deps: E11-S06

AI flags when the day's premise has broken; system stops new entries.

---
---

# EPIC 14 — RISK ENGINE 🔴

*Phase 5 · 9 stories · 14 days · **Highest-risk epic. 100% coverage required.***

### E14-S01 · Risk check framework
`P0` `Phase 5` `🔴` `FEAT` · **1.5 days** · deps: E00-S02

> Dependency corrected 25 Aug 2026. It read `E13-S04` (Recommendation
> emission), which taken literally chained the deterministic risk engine
> behind the entire AI layer. This story needs the `Recommendation` *type*,
> not the thing that emits it.

**Tasks**
- [ ] Ordered, fail-fast check pipeline
- [ ] Each check returns pass/fail with a `RejectReason`
- [ ] Every evaluation to the audit log
- [ ] `signals_rejected_total{check}` metric

**Acceptance**
- 🔴 No order path bypasses the pipeline
- Any rejection is explainable from the audit log

---

### E14-S02 · Pre-condition checks (1–4)
`P0` `Phase 5` `🔴` `FEAT` · **1 day** · deps: E14-S01

Kill switch · health gate · trading window · no-trade window.

**Tasks**
- [ ] `check_kill_switch` (C8) — reads `ctx.kill_switch_active`; E14-S09 owns
      the switch itself, so this check cannot clear what it tests
- [ ] `check_health_gate` — any unhealthy service rejects, detail names which
- [ ] `check_trading_window` — `MarketCalendar.is_market_open(ctx.now)`
- [ ] `check_no_trade_window` — `config.execution.no_trade_windows`
- [ ] A factory building the four in order, closing over calendar and config

**Acceptance**
- 🔴 AC1 An active kill switch rejects with `KILL_SWITCH_ACTIVE`, and does so
  **first** — no later check runs
- 🔴 AC2 Any unhealthy service rejects with `HEALTH_GATE_FAILED`, detail names it
- AC3 Outside continuous trading — weekend, holiday, pre-open, post-close —
  rejects with `OUTSIDE_TRADING_WINDOW`
- AC4 Inside a configured `no_trade_window`, rejects with `NO_TRADE_WINDOW` and
  the detail names **which** window matched
- AC5 **(control)** A normal mid-session moment, switch off and all services
  healthy, passes all four. Without this, four checks that rejected everything
  would satisfy AC1–AC4 perfectly
- AC6 Registration order is kill_switch, health_gate, trading_window,
  no_trade_window — cheapest and most absolute first
- AC7 An uncovered holiday year makes the calendar **raise**, which the
  framework turns into a rejection rather than a pass (fail closed)

> The trading window and the no-trade windows both describe "when not to
> trade" and overlap at the open. They are split on **source**, not time:
> `trading_window` asks the calendar whether the market is open at all;
> `no_trade_window` asks config whether we are choosing to sit a period out.
> Overlap is then harmless, and the two rejections mean different things to
> an operator.

---

### E14-S03 · Symbol eligibility checks (5–7)
`P0` `Phase 5` `🔴` `FEAT` · **1 day** · deps: E14-S01

Symbol tradable (re-verified at order time) · slot available · not already held.

> Dependency corrected 2 Sep 2026. It read `E14-S01, E04`, which blocks the
> whole story behind an epic of data fetchers. Every value these three checks
> read is already on `RiskContext`. E04 is what *populates* check 5's input in
> production — runtime wiring, not a build dependency. Building the check
> first is also the safer order: with eligibility unknown it fails closed, so
> until E04 exists the system refuses unverified symbols rather than trading
> blind.

**Tasks**
- [ ] `check_symbol_tradable` — reads `ctx.symbol_restrictions`; UNKNOWN rejects
- [ ] `check_slot_available` — `ctx.slots_available > 0`
- [ ] `check_symbol_not_already_held` — `ctx.holds()`, either direction
- [ ] `build_eligibility_checks` + `ELIGIBILITY_ORDER`, after the pre-conditions

**Acceptance**
- 🔴 AC1 Any blocking restriction rejects with `SYMBOL_NOT_TRADABLE`, and the
  detail **names** the restrictions
- 🔴 AC2 Eligibility never established **rejects**. "Not checked" must never
  read as "no restrictions" — fail closed
- AC3 A symbol checked and found clean passes
- 🔴 AC4 No free slot rejects with `NO_SLOT_AVAILABLE`; the detail gives
  used/total, because contention and misconfiguration need different responses
- 🔴 AC5 A symbol already held rejects with `ALREADY_HOLDING` regardless of the
  direction held — a reversal is not an entry
- AC6 **(control)** A clean symbol, a free slot and a flat book pass all three
- AC7 Order is symbol_tradable, slot_available, symbol_not_already_held, and
  the set runs **after** the four pre-conditions

> **The contract E04 must meet.** `ctx.symbol_restrictions` carries only what
> BLOCKS trading. Which surveillance flags qualify is E04's decision, made
> where the data is — a risk check should not have to encode NSE's
> surveillance rules to read a flag. Three states: `None` never checked
> (reject), `()` checked and clean, non-empty blocked.

---

### E14-S04 · Portfolio exposure checks (8–10)
`P0` `Phase 5` `🔴` `FEAT` · **2 days** · deps: E14-S01

> **Sector is the primary control, correlation the secondary one.** Four PSU
> banks can show only *moderate* pairwise correlation and still be one bet, so
> a correlation guard alone would let them through.
>
> **Window and frequency, answered 2 Sep 2026:** Pearson correlation of daily
> log returns over 60 trading sessions, recomputed pre-market and never
> intraday. Daily rather than intraday because a correlation estimated from
> high-frequency samples of two separately-traded names is biased *toward
> zero* — their prints are not synchronous — which would understate exactly
> the linkage this guard exists to catch. 60 sessions is a judgement, not a
> derived fact: shorter flaps, longer misses regime change.
>
> **These checks cannot make the caps binding** — they run before sizing, so
> they cannot know the candidate's notional, and §5.7's sizing formula has no
> sector or net-directional clamp. See **E14-S10**.

**Tasks**
- [ ] Correlation guard — reject N correlated names
- [ ] Rolling correlation matrix over the watchlist
- [ ] Sector exposure cap
- [ ] Net directional exposure cap

**Acceptance**
- 🔴 Four PSU banks cannot occupy four slots

---

### E14-S05 · Loss limit checks (11–12)
`P0` `Phase 5` `🔴` `FEAT` · **1 day** · deps: E14-S01

> **A pure predicate over the live figures un-halts itself**, and §8.1 forbids
> that: *"HALTED is terminal for the day and is only exited by explicit
> operator action. There is no automatic un-halt."* A daily loss limit trips
> precisely when losing positions are open; one closing at a profit lifts
> `realised_pnl_today` back over the threshold. `consecutive_losses` is worse
> — one winning close resets it to zero.
>
> So each check rejects if **either** the live figure breaches **or** a latch
> is set. Neither half alone is enough: the live figure auto-un-halts, and the
> latch has a window before it is written. This story READS the latches;
> setting them is **E14-S09**, exactly as check 1 reads `kill_switch_active`.
>
> Scoped to **new entries** (§1136, §1373) — a loss limit that also blocked
> square-off would strand losing positions overnight.

**Acceptance**
- 🔴 A realised loss at or beyond `max_daily_loss_pct` of capital rejects
- 🔴 A **profit** of the same magnitude does not. A sign inversion would halt
  on good days and trade freely on bad ones
- 🔴 A breached day does not resume when losses partly recover
- 🔴 `consecutive_losses` at the limit rejects
- 🔴 The streak halt does not clear when a win resets the counter to zero
- **(control)** A flat or profitable day with no streak passes both
- The threshold uses **configured** capital, so 3% is the same rupees at 15:00
  as at 09:20

Daily loss limit → halt · consecutive loss limit → halt.

---

### E14-S06 · Margin and timing checks (13–14)
`P0` `Phase 5` `🔴` `FEAT` · **1 day** · deps: E02-S07

> **Staleness is E02-S07's job, not this check's.** `margin_for_sizing()`
> raises on a snapshot older than its TTL, closing the
> time-of-check/time-of-use gap — margin falls after every fill, so a stale
> reading can authorise a position the account cannot carry. By the time a
> number reaches `RiskContext.available_margin` it is **fresh or absent**;
> this check rejects absent. Re-implementing the TTL here would give two
> places to disagree about how old is too old.
>
> **Check 13 runs before sizing**, so it cannot verify margin for "the
> intended position" — there is no quantity yet. It refuses when margin is
> unknown, or will not cover a single share. Unlike the sector caps, the
> proportional limit is *not* missing: §5.7's formula already clamps on
> `available_margin / margin_per_share`, so E14-S07 completes it.
>
> **New config: `risk.min_minutes_to_squareoff` (30).** Not the same as
> `exit_buffer_minutes`, which is already subtracted when the deadline is
> computed. And not redundant with the 15:00 blackout: a CAS name's deadline
> is 15:05, so at 14:59 — inside the tradable window — six minutes remain.
> 30 is a judgement (two bars of the slowest supported interval), recorded as
> an assumption rather than presented as derived.

**Acceptance**
- 🔴 Margin that is **unknown** rejects — as a fault, not a business rejection
- 🔴 Margin below the cost of one share rejects with `INSUFFICIENT_MARGIN`
- 🔴 An unknown `margin_per_share` rejects; affordability is a ratio
- 🔴 A non-positive `margin_per_share` is refused at **construction** — the
  sizer divides by it, so zero is a crash and negative is a negative quantity
- 🔴 Less runway than configured rejects with `TOO_CLOSE_TO_SQUAREOFF`
- 🔴 A deadline already **passed** rejects
- **(control)** Known margin, an affordable share and ample runway pass both
- Order is margin then timing, per §5.7, running last

> **This story completes the fourteen.** `all_check_ids()` is the full list,
> derived from the group constants so it cannot drift. A real pipeline trigger
> clears all fourteen — and is still refused, because there is no sizer.

**Tasks**
- [ ] Live broker margin sufficient for the intended position
- [ ] Enough runway before the per-stock square-off deadline

---

### E14-S07 · ATR-based position sizing
`P0` `Phase 5` `🔴` `FEAT` · **2.5 days** · deps: E14-S06

> As a trader, I want position size derived from volatility and risk budget, so
> that every trade risks the same rupee amount regardless of the stock's price.

**Tasks**
- [ ] `risk_amount = capital × risk_pct`
- [ ] `stop_distance = ATR × multiplier`
- [ ] `raw_qty = risk_amount / stop_distance`
- [ ] Apply clamps: position cap, slot cap, margin cap
- [ ] Round down to lot size
- [ ] **Record which clamp bound the result**
- [ ] Reject if quantity rounds to zero

**Acceptance**
- 🔴 Property test: risk never exceeds `risk_pct` for ANY generated input
- 🔴 A surprisingly small position is explainable from the binding constraint

---

### E14-S08 · Slot management
`P0` `Phase 5` `🔴` `FEAT` · **1.5 days** · deps: E01-S03

**Tasks**
- [ ] Slot allocation with Redis lock + TTL
- [ ] Priority queue when signals exceed free slots (score, then expectancy)
- [ ] Slot recycling on position close
- [ ] Leak detection: slot count vs actual positions

**Acceptance**
- 🔴 Concurrent signals cannot over-allocate slots

---

### E14-S09 · Kill switch
`P0` `Phase 5` `🔴` `SEC` · **1.5 days** · deps: E14-S01

**Tasks**
- [ ] Manual trigger via Telegram and dashboard
- [ ] Auto-triggers: loss limit, broker disconnect, stale feed, AI unavailable, margin shortfall, unknown position
- [ ] **Semantics: halts new entries and cancels pending; does NOT auto-liquidate**
- [ ] Separate explicit command to close positions
- [ ] Requires human action to un-halt

**Acceptance**
- 🔴 Triggered from a phone in under 10 seconds
- 🔴 Open positions are not blindly market-closed

---
---

### E14-S10 · Sizer clamps to exposure headroom
`P0` `Phase 5` `🔴` `FEAT` · **0.5 days** · deps: E14-S04, E14-S07

> Raised 2 Sep 2026 while building E14-S04, which found the gap.
> `max_sector_exposure_pct` (40) and `max_net_directional_exposure_pct` (60)
> are configured and **enforced by nothing**. The risk checks run before
> sizing, so they can only refuse a book already at a cap; the sizer clamps on
> position value, slot capital and broker margin, and not on these.

**Tasks**
- [ ] Sector headroom joins the sizing clamp set
- [ ] Net-directional headroom joins the sizing clamp set
- [ ] `binding_constraint` records which of them bound

**Acceptance**
- 🔴 A position sized into a sector at 35% of capital, cap 40%, is clamped so
  the resulting book is at or below 40% — not merely allowed because 35 < 40
- 🔴 The same for net directional exposure
- `binding_constraint` names the clamp, so a small position is explainable
- **(control)** A position with ample headroom is unaffected by either clamp

---

# EPIC 15 — EXECUTION & POSITIONS 🔴

*Phase 5 · 11 stories · 16 days*

### E15-S01 · Order gateway
`P0` `Phase 5` `🔴` `FEAT` · **2 days** · deps: E02-S04, E14-S07

**Tasks**
- [ ] Build `OrderRequest` from `RiskDecision`
- [ ] Attach Algo-ID and market protection
- [ ] Round prices to tick size
- [ ] Single-threaded serialised submission
- [ ] Rate limiter integration

---

### E15-S02 · Idempotency
`P0` `Phase 5` `🔴` `SEC` · **1 day** · deps: E15-S01

**Tasks**
- [ ] `client_order_id = sha256(correlation|symbol|side|intent|date)[:32]`
- [ ] Persist before submission
- [ ] On ambiguous failure: **query by client_order_id, never retry**

**Acceptance**
- 🔴 Chaos test: timeout + reconnect produces exactly one order

---

### E15-S03 · Order state machine
`P0` `Phase 5` `🔴` `FEAT` · **1.5 days** · deps: E15-S01

Full lifecycle per LOW_LEVEL_ARCHITECTURE §8.2 with illegal transitions rejected.

---

### E15-S04 · Protective stop attachment
`P0` `Phase 5` `🔴` `FEAT` · **1.5 days** · deps: E15-S03

> As a trader, I want a stop placed immediately after entry fill, so that no
> position is ever naked.

**Tasks**
- [ ] Place stop on entry fill confirmation
- [ ] **If stop placement fails → close position at market immediately**
- [ ] Verify stop is live before considering the position established
- [ ] Alert on any stop failure

**Acceptance**
- 🔴 A position without a live stop cannot persist for more than one cycle

---

### E15-S05 · Position manager
`P0` `Phase 5` `🔴` `FEAT` · **2 days** · deps: E15-S04

**Tasks**
- [ ] Track open positions with live P&L
- [ ] MFE/MAE tracking for the journal
- [ ] R-multiple computation
- [ ] Position state in Redis for fast reads

---

### E15-S06 · Square-off timer 🔴
`P0` `Phase 5` `🔴` `FEAT` · **1.5 days** · deps: E15-S05, E04-S05

> As a trader, I want every intraday position closed on my schedule, so that the
> broker never force-closes at an arbitrary price.

**Tasks**
- [ ] Compute per-stock deadline at position open
- [ ] Store in `timer:squareoff` sorted set
- [ ] Single polling loop for all positions
- [ ] Market order with protection at deadline
- [ ] Alert if a position remains after its deadline

**Acceptance**
- 🔴 Property test: our deadline always precedes the broker's, every stock class
- 🔴 No position survives its deadline in a full-session replay

---

### E15-S07 · Exit precedence
`P0` `Phase 5` `🔴` `FEAT` · **1 day** · deps: E15-S06

Order: kill switch → deadline → stop → target → trailing → thesis invalidation → manual.

---

### E15-S08 · Trailing stops and partial exits
`P1` `Phase 6` `🟠` `FEAT` · **1.5 days** · deps: E15-S05

Activate trailing after N R; partial booking at configured levels.

---

### E15-S09 · Reconciliation loop
`P0` `Phase 5` `🔴` `SEC` · **2 days** · deps: E15-S03

> As an operator, I want local state continuously compared against the broker's,
> so that drift is detected rather than discovered.

**Tasks**
- [ ] Every 30s during market hours, plus on every reconnect
- [ ] Diff orders and positions
- [ ] **Broker state wins** — it is the legal record
- [ ] `RECONCILIATION_DRIFT` audit event per difference
- [ ] **Unknown position → kill switch + P0 alert immediately**

**Acceptance**
- 🔴 An unknown position halts the system within one reconciliation cycle

---

### E15-S10 · Paper trading mode
`P0` `Phase 5` `🔴` `FEAT` · **1.5 days** · deps: E15-S01

> As a validator, I want paper trading to use the identical code path, so that
> what I validate is what will run live.

**Tasks**
- [ ] Simulated broker with realistic fills, rejections, partial fills
- [ ] Same risk engine, same order gateway, same position manager
- [ ] Only the adapter differs
- [ ] Simulated slippage and costs

**Acceptance**
- 🔴 Switching paper→live changes exactly one dependency injection

---

### E15-S11 · EOD reconciliation and journal
`P0` `Phase 5` `🟠` `FEAT` · **1 day** · deps: E15-S09

Final reconciliation, journal entries with setup/regime/outcome attribution,
daily statistics.

---
---

# EPIC 16 — AUTONOMY & APPROVAL

*Phase 5 · 6 stories · 7 days*

### E16-S01 · Autonomy level management
`P0` `Phase 5` `🔴` `FEAT` · **1 day** · deps: E14-S01

L0–L4 levels controlling system behaviour; level change requires re-auth.

---

### E16-S02 · Envelope evaluation
`P0` `Phase 5` `🔴` `FEAT` · **1.5 days** · deps: E16-S01

**Tasks**
- [ ] Evaluate each trade against the autonomy envelope
- [ ] Inside → auto-execute; outside → escalate
- [ ] Record which envelope condition triggered escalation

---

### E16-S03 · Escalation and approval flow
`P0` `Phase 5` `🔴` `FEAT` · **2 days** · deps: E16-S02, E19-S03

**Tasks**
- [ ] Send approval request with full context
- [ ] 60-second window
- [ ] **Timeout → skip the trade** (never auto-approve)
- [ ] Approve/reject handling
- [ ] Audit the decision and who made it

**Acceptance**
- 🔴 An unanswered approval request never results in a trade

---

### E16-S04 · Auto-demotion
`P0` `Phase 5` `🔴` `FEAT` · **1 day** · deps: E16-S01

> As a trader, I want the system to ask for more supervision when it is doing
> badly, so that autonomy is not retained through a losing streak.

**Tasks**
- [ ] Triggers: daily loss %, consecutive losses, AI disagreement rate, 7-day win rate
- [ ] Demote L3 → L2 automatically
- [ ] Alert with the reason
- [ ] Manual re-promotion only

---

### E16-S05 · Escalation trigger conditions
`P0` `Phase 5` `🟠` `FEAT` · **1 day** · deps: E16-S02

News unmatched to thesis · intraday regime change · correlation spike · broker
reject streak · reconciliation drift.

---

### E16-S06 · Autonomy audit trail
`P1` `Phase 6` `🟢` `FEAT` · **0.5 day**

History of level changes, escalations, approvals, demotions.

---
---

# EPIC 17 — DASHBOARD UI

*Phase 4/6 · 9 stories · 14 days*

### E17-S01 · Application shell and authentication
`P0` `Phase 4` `🔴` `SEC` · **2 days**

**Tasks**
- [ ] FastAPI + Jinja2 + HTMX skeleton
- [ ] **Bind to 127.0.0.1 only**
- [ ] WebAuthn passkey registration and login
- [ ] TOTP fallback
- [ ] Session management: rotation, HttpOnly/Secure/SameSite=Strict, absolute timeout
- [ ] CSRF token on every mutating request
- [ ] Strict CSP, no external origins, no CDN

**Acceptance**
- 🔴 External port scan shows the dashboard closed
- 🔴 No inline scripts; CSP violations logged

---

### E17-S02 · SSE live update channel
`P0` `Phase 4` `🟠` `FEAT` · **1.5 days** · deps: E17-S01

Single stream for positions, P&L, health, activity. Auto-reconnect with visible
banner. Throttled to 4 Hz.

---

### E17-S03 · Live dashboard
`P0` `Phase 4` `🟠` `FEAT` · **2.5 days** · deps: E17-S02

**Tasks**
- [ ] Status bar with mode, autonomy, interval, clock
- [ ] **Kill switch always visible, top-right, with confirm**
- [ ] Metrics row: P&L, open risk, slots, win/loss, drawdown
- [ ] Positions table with per-stock square-off warning
- [ ] Watchlist with trigger status
- [ ] Market context panel
- [ ] **Activity feed showing rejections, not just actions**
- [ ] System health panel

**Acceptance**
- "Is everything OK?" answerable in under 2 seconds
- Direction never encoded by colour alone (arrow + sign + colour)

---

### E17-S04 · Chart component
`P0` `Phase 4` `🟠` `FEAT` · **2 days** · deps: E17-S02

**Tasks**
- [ ] `lightweight-charts` integration, self-hosted
- [ ] Candlestick + volume subplot
- [ ] EMA overlays, opening range band, S/R levels
- [ ] Entry/stop/target markers
- [ ] `series.update()` for streaming — never full re-render
- [ ] **Visible stale state** when the feed lags

---

### E17-S05 · Symbol detail view
`P1` `Phase 4` `🟢` `FEAT` · **2 days** · deps: E17-S04

Chart + multi-timeframe panel + AI assessment + news (with decayed older items) +
position panel with manual actions.

---

### E17-S06 · Daily plan view
`P1` `Phase 4` `🟢` `FEAT` · **1.5 days** · deps: E11-S08

Thesis, ranked candidates with score breakdown, expandable playbooks,
gap-invalidated candidates kept visible with reason.

---

### E17-S07 · Positions and orders view
`P1` `Phase 4` `🟢` `FEAT` · **1 day**

---

### E17-S08 · Data visualisation standards
`P1` `Phase 6` `🟠` `FEAT` · **1 day**

**Tasks**
- [ ] Palette validated for CVD in light and dark
- [ ] **Direction never colour-alone** — arrow + sign + colour
- [ ] P&L as diverging scale with grey midpoint
- [ ] No dual-axis charts anywhere
- [ ] Status colours reserved, never reused as series colours

---

### E17-S09 · Performance analytics
`P2` `Phase 6` `🟢` `FEAT` · **1.5 days**

Equity curve, R-multiple distribution, win rate by setup and regime.

---
---

# EPIC 18 — ADMIN UI

*Phase 6 · 8 stories · 12 days*

### E18-S01 · Admin shell and compliance panel
`P1` `Phase 6` `🟠` `FEAT` · **1.5 days** · deps: E17-S01

Live status of every compliance constraint; read-only.

---

### E18-S02 · Configuration editor
`P1` `Phase 6` `🔴` `FEAT` · **2.5 days** · deps: E18-S01

**Tasks**
- [ ] Section-based editor mirroring `system.yaml`
- [ ] **Derived values shown live** ("1.0% → ₹5,000 per trade")
- [ ] Inline explanation of trade-offs
- [ ] Three-gate validation with clear errors
- [ ] **Save produces a git commit with a diff shown before confirmation**
- [ ] Re-authentication required to save
- [ ] Immutable during an active session

**Acceptance**
- 🔴 Hard bounds cannot be exceeded from the UI
- Every change is a reviewable commit

---

### E18-S03 · Universe and filter management
`P1` `Phase 6` `🟢` `FEAT` · **1.5 days**

Filter config with live preview of survivors per filter; weight editor enforcing
sum to 1.0.

---

### E18-S04 · Strategy management
`P1` `Phase 6` `🔴` `FEAT` · **2.5 days** · deps: E12-S02

**Tasks**
- [ ] Registry list with state, DSR, PBO, **vs-backtest ratio**
- [ ] Detail tabs: definition, hypothesis, validation, live performance
- [ ] YAML editor with live validation and primitive autocomplete
- [ ] **Approval screen requiring re-auth AND typing the strategy name**
- [ ] Trial log showing search cost and the implied DSR bar

**Acceptance**
- 🔴 Promotion to ACTIVE requires deliberate, high-friction confirmation

---

### E18-S05 · Risk limits editor
`P1` `Phase 6` `🔴` `FEAT` · **1.5 days** · deps: E18-S02

With impact preview from backtest where available.

---

### E18-S06 · AI settings and budget
`P1` `Phase 6` `🟢` `FEAT` · **1 day**

Model selection, thresholds, live spend gauge, cache hit rate.

---

### E18-S07 · News sources and injection monitor
`P1` `Phase 6` `🔴` `SEC` · **1 day** · deps: E08-S03

**Tasks**
- [ ] Provider allowlist management
- [ ] Decay half-life configuration
- [ ] **Sanitiser hit counts by source with samples**
- [ ] A source with rising hits is a source to drop

---

### E18-S08 · Audit log explorer
`P1` `Phase 6` `🟠` `FEAT` · **1.5 days** · deps: E01-S05

Search by correlation ID; full trade life from candidacy to exit with every
stage, latency, and rejection reason.

---
---

# EPIC 19 — NOTIFICATIONS

*Phase 3/4 · 6 stories · 6 days*

### E19-S01 · Telegram bot foundation
`P0` `Phase 3` `🔴` `SEC` · **1.5 days**

**Tasks**
- [ ] Bot setup with token from secrets
- [ ] **Respond only to one hard-configured chat ID** — ignore and log all others
- [ ] **Single recipient enforced** (config validation already does this)
- [ ] Message templating

**Acceptance**
- 🔴 A message from any other chat ID is ignored and logged

---

### E19-S02 · Pre-market briefing delivery
`P0` `Phase 3` `🟢` `FEAT` · **1 day** · deps: E11-S08, E19-S01

---

### E19-S03 · Trade alerts and approval buttons
`P0` `Phase 4` `🔴` `FEAT` · **1.5 days** · deps: E19-S01

**Tasks**
- [ ] Trade opened/closed alerts with rationale
- [ ] Approval requests with inline buttons and countdown
- [ ] **Expiry → skip** (never auto-approve)
- [ ] No sensitive data — no account numbers, no absolute capital

---

### E19-S04 · Command handlers
`P0` `Phase 4` `🔴` `FEAT` · **1.5 days** · deps: E19-S01

`/status` `/positions` `/plan` `/pause` `/resume` `/kill` `/close` `/closeall`
`/why` `/health`. Destructive commands require confirmation; `/kill` and
`/closeall` require two steps.

---

### E19-S05 · Alert budgets and batching
`P1` `Phase 4` `🟢` `FEAT` · **0.5 day** · deps: E19-S03

P0 immediate · P1 ≤3/day · P2 ≤6/day batched into a digest beyond budget.

---

### E19-S06 · Email fallback
`P2` `Phase 6` `🟢` `FEAT` · **0.5 day**

---
---

# EPIC 20 — OBSERVABILITY

*Phase 6 · 6 stories · 7 days*

### E20-S01 · Metrics instrumentation
`P1` `Phase 6` `🟠` `OPS` · **1.5 days**

Latency histograms per stage · data quality counters · AI metrics including
cache hit ratio · trading counters · risk gauges · health.

---

### E20-S02 · Latency profiler and adaptive interval
`P1` `Phase 6` `🔴` `FEAT` · **2 days** · deps: E20-S01

> As a system, I want my trading interval derived from measured latency, so that
> I never act on analysis I have not finished.

**Tasks**
- [ ] Record per-stage latency in a rolling window
- [ ] Compute p95 of the full pipeline
- [ ] Apply headroom multiplier
- [ ] Round up to the nearest supported interval
- [ ] Recalibrate daily; publish to `control:interval`

**Acceptance**
- 🔴 Degraded latency automatically steps the interval down, never up

---

### E20-S03 · Grafana dashboards
`P1` `Phase 6` `🟢` `OPS` · **1.5 days** · deps: E20-S01

---

### E20-S04 · Alerting rules
`P1` `Phase 6` `🟠` `OPS` · **1 day** · deps: E20-S01

P0/P1/P2/P3 routing per the priority scheme.

---

### E20-S05 · Health check framework
`P0` `Phase 5` `🔴` `OPS` · **1 day**

Per-service `/health`; heartbeat to Redis with TTL; aggregated health gate that
blocks trading when any critical service is unhealthy.

---

### E20-S06 · Structured logging rollout
`P1` `Phase 6` `🟢` `OPS` · **0.5 day**

Correlation ID binding across all services.

---
---

# EPIC 21 — COMPLIANCE & TAX

*Phase 6 · 7 stories · 9 days*

### E21-S01 · Algo-ID attachment
`P0` `Phase 5` `🔴` `SEC` · **0.5 day** · **blocked on B1**

Attach per the confirmed mechanic; verify present on every order.

---

### E21-S02 · Static IP verification at startup
`P0` `Phase 5` `🔴` `SEC` · **0.5 day**

Compare actual egress IP against config; **refuse to start on mismatch** in live
mode.

---

### E21-S03 · Charge-level fill accounting
`P0` `Phase 6` `🔴` `FEAT` · **2 days** · deps: E15-S03

> As a taxpayer, I want every charge recorded separately, so that year-end
> reconstruction from contract notes is unnecessary.

**Tasks**
- [ ] Record STT, brokerage, GST, exchange charges, stamp duty, SEBI fees per fill
- [ ] Reconcile against the broker contract note
- [ ] Alert on divergence

**Acceptance**
- 🔴 Charges match a real contract note to the paisa

---

### E21-S04 · Intraday turnover computation
`P0` `Phase 6` `🔴` `FEAT` · **1 day** · deps: E21-S03

> As a taxpayer, I want turnover computed as the **absolute sum of profits and
> losses**, so that the ITR-3 figure is correct.

**Acceptance**
- 🔴 ₹2,000 profit + ₹1,500 loss = ₹3,500 turnover (verified on test data)

---

### E21-S05 · Business expense register
`P1` `Phase 6` `🟢` `FEAT` · **1 day**

Categorised entry for VPS, API spend, data subscriptions — all deductible.

---

### E21-S06 · Tax report generator
`P1` `Phase 6` `🟠` `FEAT` · **2 days** · deps: E21-S04, E21-S05

Speculative turnover, gross profit/loss, net income, itemised expenses,
carry-forward position, financial-year selection, export.

---

### E21-S07 · Compliance status panel
`P1` `Phase 6` `🟢` `FEAT` · **1 day** · deps: E18-S01

Live status of every constraint with a pre-live readiness summary.

---

### E21-S08 · Audit chain verification utility
`P1` `Phase 6` `🔴` `SEC` · **1 day** · deps: E01-S05

Verify the hash chain end to end; detect any retroactive modification.

---
---

# EPIC 22 — TESTING & VALIDATION

*Phase 7 · 8 stories · 12 days*

### E22-S01 · Property-based risk engine tests 🔴
`P0` `Phase 5` `🔴` `TEST` · **2 days** · deps: E14-S07

**Tasks**
- [ ] Position size never exceeds configured risk, for any input
- [ ] Never exceeds slot count
- [ ] Approved order always has a stop
- [ ] Square-off always precedes broker deadline
- [ ] Sizing clamps are monotonic

---

### E22-S02 · Integration test suite
`P1` `Phase 7` `🟠` `TEST` · **2 days**

Testcontainers for Redis and Postgres; every inter-service contract exercised.

---

### E22-S03 · Replay harness
`P1` `Phase 7` `🟠` `TEST` · **2.5 days** · deps: E03-S06

Replay a recorded session through the full pipeline; assert deterministic
outputs; regression on every change.

---

### E22-S04 · Chaos test suite
`P0` `Phase 7` `🔴` `TEST` · **2.5 days**

All nine scenarios from LOW_LEVEL_ARCHITECTURE §12.3 — kill each service, Redis
restart, WebSocket drop, AI outage, broker 500, clock jump, disk full, duplicate
tick flood.

**Acceptance**
- 🔴 Every scenario fails closed; none opens new risk

---

### E22-S05 · Contract tests
`P1` `Phase 7` `🟢` `TEST` · **1 day**

Recorded fixtures for broker and AI APIs; detect upstream shape drift.

---

### E22-S06 · Prompt injection corpus 🔴
`P0` `Phase 7` `🔴` `TEST` · **1.5 days** · deps: E08-S03

Adversarial news snippets: instruction override, role confusion, delimiter
escape, Unicode obfuscation, nested encoding. Runs in CI because prompt changes
can silently reopen a closed hole.

---

### E22-S07 · Coverage enforcement
`P0` `Phase 7` `🟠` `TEST` · **0.5 day**

100% line coverage gate on `execution/risk_engine.py` and `execution/sizer.py`.

---

### E22-S08 · Load and soak testing
`P2` `Phase 7` `🟢` `TEST` · **1 day**

Full universe tick rates; multi-day soak for memory leaks.

---
---

# EPIC 23 — DEPLOYMENT & OPS

*Phase 7/8 · 7 stories · 8 days*

### E23-S01 · VPS provisioning and hardening
`P0` `Phase 7` `🔴` `OPS` · **1.5 days**

India-region host · static IP · firewall default-deny · SSH key-only, root
disabled, fail2ban · unattended security upgrades outside market hours.

---

### E23-S02 · Static IP whitelisting
`P0` `Phase 7` `🔴` `SEC` · **0.5 day** · deps: E23-S01

Register at `developers.kite.trade`; verify orders succeed from the whitelisted
IP and fail from elsewhere.

---

### E23-S03 · Docker Compose deployment
`P0` `Phase 7` `🔴` `OPS` · **1.5 days** · deps: E23-S01

**Tasks**
- [ ] First actual run of the topology
- [ ] Verify `core` network has no internet access
- [ ] Verify egress allowlists hold
- [ ] Startup ordering and health gates
- [ ] Resource limits tuned

**Acceptance**
- 🔴 A container on `core` cannot reach the internet (verified by attempt)

---

### E23-S04 · Backup and restore
`P0` `Phase 7` `🔴` `OPS` · **1 day**

Nightly DB dump + config + audit export to off-host storage with separate
credentials. **Restore drill required.**

---

### E23-S05 · CI pipeline
`P1` `Phase 7` `🟢` `OPS` · **1 day**

`make check` on every PR; gitleaks; pip-audit; safety-invariant tests as a
required check on `QA` and `PROD`.

---

### E23-S06 · Runbooks
`P1` `Phase 7` `🟠` `DOC` · **1.5 days**

Auth failure · feed loss · broker outage · unknown position · kill switch
triggered · restore from backup · daily operational checklist.

---

### E23-S07 · Secrets backend migration
`P2` `Phase 8` `🔴` `SEC` · **1 day**

SOPS+age → Vault when a second host appears.

---
---

# CROSS-CUTTING BLOCKERS

These gate multiple epics and are not code. Resolve first.

| ID | Blocker | Blocks | Action |
|---|---|---|---|
| **B1** | Algo-ID attachment mechanic unconfirmed | E02-S04, E21-S01 | Ask Zerodha: `tag` field or broker-injected? |
| **B2** | `pykiteconnect` lacks `market_protection` | E02-S04, E15-S01 | Install from git main, or wait for release |
| **B3** | NSE holiday list incomplete | E04-S07, all scheduling | Transcribe the circular |
| **B4** | Historical data pricing unconfirmed | E03-S03 budget | Ask Zerodha |
| **B5** | Daily login mechanism untested | E02-S01 | Prototype the phone→callback flow |
| **B6** | Static IP not procured | E23-S02, all live trading | Select provider, procure |

---

# SUGGESTED SPRINT SEQUENCE

Two-week sprints, solo. Each ends with something demonstrable.

| Sprint | Focus | Key deliverable |
|---|---|---|
| **1** | E01 + B1–B6 resolution | Schema live, blockers answered |
| **2** | E03 + E04 | Historical data flowing, hazard lists daily |
| **3** | E02-S01..S03, E05-S01..S05 | **Live ticks arriving and cleaned** |
| **4** | E05-S06..S08, E06-S01..S03 | **Bars building, indicators warm** |
| **5** | E06-S04..S07, E07 | **Ranked watchlist every morning** |
| **6** | E09, E19-S01..S02 | Macro context + Telegram working |
| **7** | E08, E10-S01..S04 | News scored, AI layer live |
| **8** | E11 | **★ Pre-market plan by 09:15 — the inflection point** |
| **9** | E10-S05, E13, E17-S01..S03 | Live signals with rationale, dashboard |
| **10** | E14 | **Risk engine complete, property-tested** |
| **11** | E15-S01..S07 | Order execution on paper |
| **12** | E15-S08..S11, E16 | Position management, autonomy, approval |
| **13** | E12-S01..S08 | Strategy registry + validation gauntlet |
| **14** | E12-S09..S10, E20 | Shadow mode, observability |
| **15** | E17-S04..S09, E18 | UI complete |
| **16** | E21, E22-S01..S04 | Compliance, tax, chaos tests |
| **17** | E22-S05..S08, E23 | Hardening, deployment |
| **18** | Paper trading | **Multi-week validation across regimes** |
| **19+** | PRE_LIVE_CHECKLIST | Approval mode, small live size |

---

# WORKING NOTES

**Start every 🔴 story by writing the test that proves the invariant.** Two real
bugs shipped past a clean code read in Phase 0; both took under a minute to find
by *executing* the claim. Code review catches intent; only execution catches
behaviour.

**Never merge a 🔴 story at the end of a session.** Sleep on it, re-read the diff
in the morning.

**Phase 3 (Sprint 8) is the inflection point.** From that week the system pays
for itself as a research assistant whether or not execution is ever automated.
If the schedule slips, protect this milestone over everything after it.

**The riskiest sequencing mistake would be building E12-S11/S12 (AI strategy
generation) before E12-S05..S08 (the gauntlet).** That is building the dangerous
half of the feature first.

---

*Living document. Update story status as work completes; add stories as
discovery reveals them. Estimates are guesses until the first sprint calibrates
them.*
