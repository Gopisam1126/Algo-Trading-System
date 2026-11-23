# Project Guide — an AI-assisted intraday trading system for NSE/BSE

**One document for the whole project:** what it is, how a trading day runs, how
the parts work, how it is built, tested and shipped, what is done, what is left
and what is needed from the owner. Written 23 Nov 2026.

It simplifies; it does not replace the design documents. Where this guide and a
design document disagree about *design*, the design document wins. About
*status*, `MASTER_REFERENCE.md §2` wins. The test count is whatever the latest
CI run says - no document carries one.

**Status markers used below:** ✅ built and tested (as a component) · 🟡 partly
built · ⬜ not built.

---

## Contents

1. [The system in one page](#1-the-system-in-one-page)
2. [The six goals, mapped to the system](#2-the-six-goals-mapped-to-the-system)
3. [Where it stands](#3-where-it-stands)
4. [How a trading day runs](#4-how-a-trading-day-runs)
5. [Architecture](#5-architecture)
6. [How the parts work](#6-how-the-parts-work)
7. [Rules that never bend](#7-rules-that-never-bend)
8. [Compliance and tax](#8-compliance-and-tax)
9. [The MVP and the road to live capital](#9-the-mvp-and-the-road-to-live-capital)
10. [Development - how work is done](#10-development--how-work-is-done)
11. [Testing](#11-testing)
12. [CI/CD - from a commit to QA to PROD](#12-cicd--from-a-commit-to-qa-to-prod)
13. [Configuration and secrets](#13-configuration-and-secrets)
14. [What is left, in numbers](#14-what-is-left-in-numbers)
15. [What is needed from the owner](#15-what-is-needed-from-the-owner)
16. [Where to find things](#16-where-to-find-things)
17. [Glossary](#17-glossary)

---

## 1. The system in one page

A **personal, self-developed intraday trading system** for Indian equities
(NSE, optionally BSE). It trades the owner's own capital through Zerodha's
**Kite Connect** API, places fewer than 10 orders per second (it is capped at 5),
and never shares its signals with anyone.

What it does, in one sentence: every morning it narrows the market to a short
list of stocks; during the session it computes technical indicators on each,
lets declarative **strategies** propose entries, asks an **AI model to review**
each proposal, and lets a **deterministic risk engine** - never the AI - decide
whether to trade and how many shares. Every position gets a **protective stop
and a time exit**, the broker's records are **reconciled every 30 seconds**, and
every decision is written to a **tamper-evident audit log**.

Three properties shape everything:

| Property | What it means in practice |
|---|---|
| **The AI advises; code decides** | The AI's output (a `Recommendation`) cannot carry a quantity, a rupee amount or a stop price. The type makes that impossible. Sizing and risk are plain, tested code. |
| **Fail closed** | A missing input, a timeout, a component that is down or a record that cannot be read means *no new risk* - never "probably fine". |
| **Safety is structural** | Where possible, a dangerous state cannot even be represented in code; next best, constructing it raises; last resort, a test asserts it. A warning comment is never enough. |

**What it is not:** not high-frequency (orders are a handful a day), not a
signal service (one recipient - the owner), and not an AI that trades by itself.

**Scale:** a Nifty 200 universe by default, up to 5 open positions at once
(configurable to a hard maximum of 20), 5-minute decisions, one India-region
host (the India region is this system's own rule, not a regulation - see §8).

---

## 2. The six goals, mapped to the system

The project's working goals, and where each lives today.

| # | Goal | Where it lives | State |
|---|---|---|---|
| 1 | **Fetch live data** | `ingest/` - a Kite WebSocket client written against Kite's documented binary protocol, with reconnection that reports gaps instead of hiding them, and live quote state | ✅ as a component. Never yet run against real Kite (needs the owner's Kite Connect app, §15), and no process runs it yet (E23-S03) |
| 2 | **Clean the data** | `ingest/cleaning` - tick validation, de-duplication, an outlier filter that cannot lock up, and only continuous-session ticks make bars; stored-data validation is E03-S04 | ✅ live path · ⬜ stored-data checks |
| 3 | **Do the calculations** | `indicators/` - EMA, SMA, RSI, ATR, MACD, Bollinger, VWAP and volume ratio, each verified against TA-Lib; warm-up tracking; a multi-timeframe snapshot; pivots and the opening range | ✅ · ⬜ ADX (E06-S02), relative strength (E06-S07), patterns (E06-S08) |
| 4 | **Analyse the data with the calculations** | `strategy/` (a declarative strategy language and 27 vetted building blocks) and `signals/` (the strategy loop) ✅; the pre-market analysis (E11), scoring (E07), news (E08), macro context and market regime (E09) and AI review (E10, E13-S03) | 🟡 strategies run; the surrounding analysis is ⬜ |
| 5 | **Decide whether to buy or sell** | Strategies emit LONG (buy) or SHORT (sell first) triggers ✅; the AI verdict ⬜ (E13-S03); the risk engine approves or rejects and sizes every trade ✅ | 🟡 |
| 6 | **Trade automatically and manually** | *Automatic:* the execution service - risk, order, stop, book, reconciliation and exits - ✅ on the paper broker; a runnable process ⬜ (E23-S03); live needs the owner's items. *Manual:* the autonomy ladder lets the owner trade on alerts (L1, today's setting) or approve each trade (L2: E16-S03, E19-S03 ⬜); an operator close command ⬜ (E14-S13). Placing a manual order *through the system* is not yet a story | 🟡 |

### What the system requires beyond those six

These requirements were already decided, with their reasoning, in the design
documents. They are listed here so nothing is mistaken for optional.

- **Risk control:** 14 pre-trade checks (§6.5), ATR-based sizing with every cap
  applied (§6.6), position slots, a daily loss limit and a losing-streak limit
  that latch until a human clears them, a kill switch.
- **Exits:** a protective stop on every position, a target exit at 2R, and a
  time exit before the broker's own per-stock square-off.
- **Pre-market planning:** a daily pipeline from 05:30 to a locked plan at 09:12
  (universe, hazard lists, multi-timeframe analysis, scoring, news and macro, an
  AI-written thesis, a gap adjustment).
- **Strategy discipline:** strategies are data, not code; every backtest counts
  as a trial; a 12-check validation gauntlet; shadow and paper stages; a human
  approval before any strategy trades live; automatic demotion on decay.
- **Compliance:** order-rate cap, static IP, market protection on market
  orders, algo-ID tagging question (B1), hosting question (B10), single
  notification recipient, tax reporting (§8).
- **Autonomy:** a ladder from observe-only (L0) to full auto (L4), with
  automatic demotion and actions that never become automatic (§6.15).
- **Operations:** health heartbeats, reconciliation, end-of-day verdict,
  observability (Prometheus/Grafana), backups, runbooks, a dashboard reachable
  only by the owner, alerts to one Telegram chat.
- **Security:** one service holds trading credentials, secrets never render,
  log redaction, privilege-separated database roles, a hash-chained audit log,
  prompt-injection defences for news.

---

## 3. Where it stands

**Built and tested as components, not yet composed into processes.** The
deterministic path runs from a tick to an approved, sized decision, through the
order gateway to a broker order, with a protective stop, a position book,
reconciliation, exits and an end-of-day verdict - all exercised against a strict
fake Kite account and a paper broker. **Nothing has placed a real order**, and no
package has a `__main__`, so no service runs as a process yet (E23-S03).

| Package | State | What is there |
|---|---|---|
| `common/` | Components | config with hard bounds, `SecretString`, redacting logs, NSE calendar (dated CAS schedule), domain models, TimescaleDB schema and repositories, hash-chained audit, Redis primitives and streams, retention, health heartbeats |
| `broker/` | Components | Kite auth and daily re-auth, read-only/trading split, error taxonomy, instrument sync, live margin, rate limiter (capped at 5 OPS), paper broker |
| `ingest/` | Components | Kite WebSocket client, cleaning, session-aligned bars, quote state |
| `indicators/` | Components | incremental indicators, warm-up, multi-timeframe snapshot, levels, state publishing |
| `strategy/` | Components | the strategy language, 27 primitives and their evaluators, the approval-gated registry, the trial registry |
| `signals/` | Components | the strategy loop and bar source; its LIVE/PAPER routes end at a port the AI review will fill |
| `execution/` | Components, composed in one service object | 14-check risk engine, sizer, slots, halts and loss latch, order gateway and state machine, protective stops, position book, reconciliation, exit timer, autonomy gate, day ledger, end-of-day verdict and statistics, execution service |
| `premarket/` | Components | the base universe (Nifty 50/100/200/500, F&O stocks or a custom list, resolved against the instrument master); NSE's daily bhavcopy stored as daily bars, with an archive and a backfill command; no pipeline yet |
| `ai/` `macro/` `orchestrator/` `api/` `notifier/` | **Empty** | `__init__.py` only |

**What stands between this and a paper session:** the AI review and the
`Recommendation` emitter (E13-S03/S04, which need the AI client, E10-S01); a
market context with a regime (E09); the day's locked plan (E11-S08); a news
source (the shipped strategy enters only when no material news has landed, so
it never fires until something reports news); market data (Kite Connect); and
the process entrypoints (E23-S03).

---

## 4. How a trading day runs

All times IST. The right-hand column is today's state.

| Time | Step | What happens | State |
|---|---|---|---|
| 05:30 | Data sync | Instrument master; bhavcopy (end-of-day prices); corporate actions; hazard lists - ASM/GSM surveillance, T2T, F&O ban, circuit bands, CAS scope | instrument sync ✅ · bhavcopy ✅ (E03-S01; the 05:30 schedule is E11-S02) · rest ⬜ (E03, E04) |
| 06:30 | Universe | The base universe (Nifty 200 by default), then hard filters (T2T, surveillance, bans, bands, price, volume, market cap, range, spread) | base universe ✅ (E07-S01) · filters ⬜ (E07-S02) |
| 07:00 | Broker login | The owner logs in to Zerodha once a day, by hand - an exchange rule; the system cannot do it unattended | auth code 🟡 · login helper ⬜ |
| 07:30 | Multi-timeframe analysis | Weekly, daily and hourly indicators for the eligible universe | ⬜ (E11-S04) |
| 08:00 | Scoring | Tradeability score, ranked shortlist (top 15) | ⬜ (E07-S03/S04) |
| 08:15 | News and macro | Overnight news, global cues, India VIX, FII/DII flows, sector strength, calendar | ⬜ (E08, E09) |
| 08:45 | AI deep synthesis | The top model writes a daily thesis and a playbook per stock; on timeout the plan is score-only | ⬜ (E11-S06) |
| 09:00 | Pre-open | GIFT Nifty and pre-open prices; stocks that gapped past their entry are dropped | ⬜ (E11-S07, E09-S01) |
| 09:12 | Plan lock | The day's watchlist, bias and limits are frozen | ⬜ (E11-S08) |
| 09:13 | Briefing | Delivered to the owner's Telegram | ⬜ (E19-S02) |
| 09:15-09:20 | Observation | No trading - the open is noisy (a mandatory no-trade window) | ✅ risk check 4 |
| 09:20-15:00 | Trading | Each closed bar: indicators, strategies, AI review, risk engine, order, stop | ✅ except the AI review |
| every 30 s | Reconciliation | The broker's positions and orders against ours; the broker wins | ✅ |
| every 1 s | Exit timer | Target exits on a fresh price; time exits at each stock's deadline | ✅ |
| 15:00 | No new entries | Manage what is open | ✅ |
| per stock | Time exit | Our deadline: CAS 15:10, non-CAS 15:20, F&O 15:25, minus a buffer - before Zerodha's own square-off (15:12 / 15:25 / 15:26) | ✅ |
| after the books close | End of day | 15:35 (15:40 from 7 Dec 2026, when the closing auction moves): one final reconciliation, the flat-book verdict, the day's statistics; a book that is not flat halts the next session | ✅ (E15-S15) |
| 16:00 | Backup | Database, config and audit export, off the host | ⬜ (E23-S04) |

---

## 5. Architecture

### 5.1 The big picture

```
                       PRE-MARKET (05:30-09:12)          MACRO / NEWS (~every 20 min)
                       universe, analysis, scoring,      VIX, flows, sectors, news,
                       AI thesis -> LOCKED PLAN          regime -> MARKET CONTEXT
                                  |                                 |
                                  v                                 v
 Kite WebSocket --> INGEST --> bar streams --> SIGNAL LOOP --------------------------+
 (live ticks)       clean,     (Redis)         indicators, multi-timeframe snapshot, |
                    bars                       strategies -> trigger                 |
                                                     |                               |
                                                     v                               |
                                               AI REVIEW (designed)                  |
                                                     |  Recommendation (no sizing)   |
                                                     v                               |
                                               stream:signals                        |
                                                     |                               |
                                                     v                               |
                          EXECUTION SERVICE - the only holder of trading credentials |
                          risk engine (14 checks) -> sizer -> slot -> order gateway -+--> Zerodha
                          protective stop -> position book                           |
                          reconciliation (30 s) <-- broker orders and positions <----+
                          exit timer (1 s) . halts . end-of-day verdict
                                                     |
                                                     v
                          AUDIT LOG (hash-chained) . metrics . alerts (one recipient)
```

### 5.2 Three clock speeds that never block one another

| Loop | Cadence | What runs in it |
|---|---|---|
| Fast (market) | each closed bar, ~5 min | ingest, clean, indicators, strategy, AI review, risk, order |
| Macro / news | ~20 min | news, sentiment, VIX, FII/DII, regime |
| Slow (batch) | daily and weekly | pre-market pipeline, end of day, strategy validation |

The fast loop reads the latest cached output of the slower ones; it never waits
for them.

### 5.3 Services

`ops/docker-compose.yml` defines thirteen containers on one host. None of the
application services can run yet - they have no process entrypoint (E23-S03).

| Service | Job | Trading credentials? | AI tier |
|---|---|---|---|
| `orchestrator` | Day lifecycle, scheduling, kill switch, health | No | - |
| `market-ingest` | WebSocket, tick cleaning, bars | No (read-only data) | - |
| `signal-engine` | Indicators and the strategy loop, AI review | No | mid (in-session) |
| `execution-svc` | Risk checks, sizing, orders, positions, reconciliation, exits | **Yes - the only one** | - |
| `macro-svc` | News and macro into the market context | No | small (news triage) |
| `premarket-job` | Pre-market analysis, scoring, the daily plan | No | top (daily thesis) |
| `strategy-svc` | Registry and validation (off the critical path) | No | top |
| `api-server` | Dashboard and controls, bound to 127.0.0.1 | Proxies | - |
| `notifier` | Telegram alerts, one recipient | No | - |
| `timescaledb`, `redis`, `prometheus`, `grafana` | Storage, messaging, metrics, dashboards | - | - |

There is no separate indicator service: the indicator engine runs inside the
strategy loop (an earlier design had a `ti-engine`). **Exactly one service holds
broker write credentials**, so compromising the service that reads AI output
still cannot place an order. Each secret is passed only to the one service that
needs it (`tests/security/test_compose_secrets.py` holds that).

### 5.4 The code

```
Code/
  src/algotrader/
    common/      config, models, calendar, logging, db, redis, audit, health
    broker/      Kite adapter (auth, trading, instruments, errors), rate limiter, paper broker
    ingest/      WebSocket client, cleaning, bars, quotes
    indicators/  incremental indicators, warm-up, snapshot, levels, publishing
    strategy/    DSL, primitives, runtime, registry
    signals/     strategy loop, bar source
    execution/   risk/ (14 checks), sizer, slots, halt, gateway, order_state,
                 protective_stop, positions, reconciliation, squareoff,
                 day_ledger, day_stats, end_of_day, charges, live_inputs,
                 session_context, autonomy, loss_latch, service
    premarket/   universe (base universe), bhavcopy (daily bars from NSE's file)
    ai/ macro/ orchestrator/ api/ notifier/     (empty)
  config/        system.yaml, strategies/*.yaml, nse_holidays.yaml
  migrations/    Alembic migrations (the schema's history)
  ops/           docker-compose.yml, Dockerfile, Postgres init scripts
  scripts/       doctor.py, halt.py, check_data_reachability.py, coverage_gate.py, validate_strategies.py
  tests/         unit, property, integration, security, sit, chaos, replay, fixtures
Documents/       design documents, BACKLOG_Tracker.xlsx (system of record), this guide
scripts/         promote.py (DEV -> QA), hooks/ (pre-commit), tracker/ (artifact builder)
.github/workflows/ ci.yml, promote.yml
.claude/skills/sdlc/   the delivery lifecycle
```

### 5.5 Data

**TimescaleDB (PostgreSQL)** - the durable record. Business rules BR-1 to BR-20
are database constraints, not just application code. Key tables:

| Table | Holds |
|---|---|
| `instruments` | the instrument master (symbol, token, tick size, lot size, sector, active) |
| `instrument_daily_status` | hazard flags per symbol **per day** (T2T, ASM, GSM, F&O ban, CAS) - history matters for backtests |
| `ohlcv` | bars, raw prices immutable; corporate-action factors applied on read |
| `corporate_action` | splits, bonuses, dividends - the source of every adjustment |
| `orders`, `positions` | every order this system placed (recorded before it is sent) and every position (a stop price is mandatory) |
| `decision_log` | the hash-chained audit trail; the application cannot rewrite it |
| `strategy`, `strategy_approval`, `strategy_trial` | immutable strategy documents, human approvals, every backtest run |
| `shadow_signal`, `trade_journal` | what shadow strategies would have done; the journal (E15-S11) |

**Redis** - the live state: bar streams and `stream:signals` (durable, consumer
groups, dead-letter), published indicator state, quotes, service heartbeats,
the three halt latches (no expiry, ever), slot locks.

### 5.6 Security, in brief

- **Privilege separation:** services log in as `algotrader_svc`, a role that owns
  nothing and cannot rewrite the audit trail; the table owner runs migrations
  only; a strategy approval can be written only by `algotrader_approver`.
- **Secrets:** held only in `SecretString`, which renders as `***` in every way
  Python can print it; logs are scrubbed by a redactor that is tested with
  real-shaped tokens; the `.env` file is never committed.
- **Network:** the core network has no internet route; the dashboard binds to
  127.0.0.1; egress is allow-listed (E23-S03 verifies all of this on the host).
- **News is untrusted input:** sanitised, then read only through a structured AI
  call with bounded fields (E08-S03, E08-S05).
- **The repository is public**, so nothing secret or personal belongs in it.

---

## 6. How the parts work

### 6.1 Live data and cleaning

The WebSocket client speaks Kite's documented binary packet format directly (not
Kite's ticker library, whose old dependency carried a vulnerability). A
reconnect never hides a gap: it emits a `FeedGap`, and the strategy side treats
the symbol as stale. Ticks are validated, de-duplicated and filtered for
outliers (with a baseline that follows the market, so a genuine big move does
not lock the filter). Only continuous-session ticks make bars; bars align to the
09:15 session start, so a 15-minute bar runs 09:15-09:30.

### 6.2 Indicators

Incremental - each new bar updates every indicator in constant time - and each
one matched against TA-Lib. An indicator is not *ready* until it has enough
bars; trading on a 20-period average built from three bars is refused. The
multi-timeframe snapshot carries the time each timeframe was last updated, so a
stalled hourly series cannot be read as current.

### 6.3 Strategies

A strategy is a **YAML document**, not code (`config/strategies/orb_classic.yaml`
ships as the example). It combines vetted building blocks ("primitives", such as
*price breaks a level* or *timeframes agree*) and must declare entry conditions,
a stop and a time exit - a document without both exits does not parse. There is
no `eval` and no dynamic import anywhere in this path.

Registered documents are **immutable** (a change is a new version) and move
through a lifecycle:

```
DRAFT -> (validation gauntlet, 12 checks) -> SHADOW (evaluates live, places nothing)
      -> PAPER (full path, paper capital) -> AWAITING_APPROVAL -> human approval
      -> ACTIVE -> DEGRADED (half size, automatic) -> RETIRED (automatic)
```

Promotion to live capital always needs a human; demotion never does. **Every
backtest is a trial** - testing one idea at 20 settings is 20 trials - and the
trial registry is append-only, because deleting failures would make the
overfitting correction (the Deflated Sharpe Ratio) lie.

### 6.4 The AI layer (designed, not built)

| Tier | Used for | Cadence |
|---|---|---|
| Top model | pre-market thesis and playbooks | once a day |
| Mid model | in-session review of each trigger | per trigger |
| Small model | news triage | per article batch |

The configured model IDs are unverified (audit HAL-003) and are checked when
E10-S01 is built. Every call uses structured output (a fixed schema); a refusal,
a timeout or an unparseable answer **skips the trade**. Prompts are layered so
the fixed part is cached. A daily token budget degrades the system to
score-only planning when exhausted, and never touches risk management.

### 6.5 The risk engine - 14 checks, in order, first failure wins

| Group | Checks |
|---|---|
| Pre-conditions | 1 kill switch · 2 health of every trading service · 3 trading window · 4 no-trade windows (09:15-09:20, 15:00-15:30) |
| The symbol | 5 tradable today (hazard lists) · 6 a free slot · 7 not already held |
| The portfolio | 8 correlation with held positions · 9 sector exposure · 10 net long/short exposure |
| Loss limits | 11 daily loss (a latch) · 12 consecutive losses (a latch) |
| Margin and time | 13 live broker margin · 14 enough time before the stock's square-off |

A check that *crashes* is a rejection with its own reason, `RISK_ENGINE_FAULT` -
never a pass. Every input the engine reads must be stated: "unknown" is refused,
never defaulted (a kill switch that nobody asked about is not "off").

### 6.6 Sizing

1. **Risk budget** = capital × risk per trade (₹5,00,000 × 1% = ₹5,000).
2. **Stop distance** = ATR × 1.5 (on the strategy's own timeframe).
3. **Shares** = budget ÷ stop distance - then the **smallest** of every cap:
   position value (20% of capital), the slot's capital, live broker margin,
   sector headroom and net-directional headroom.
4. **Floor** to a whole lot. Flooring, not rounding, is what makes "risk never
   exceeds the budget" true.
5. Refuse a position below the minimum size (2% of capital), and refuse a
   trigger more than 1% away from the live price.

The cap that bound is recorded, so a surprisingly small position is explainable.
Example: ATR ₹8 → stop distance ₹12 → 416 shares by risk; at ₹1,200 a share the
20% cap allows 83, so the position is 83 shares, bound by the position cap.

### 6.7 Placing an order safely

- Every order gets a **deterministic id** (a hash of the decision), so the same
  decision cannot become two orders.
- The order is **recorded before it is sent**, and the broker id attached after.
- After an ambiguous failure (timeout, 5xx) the system **asks the broker by id
  and adopts its answer** - it never blindly retries, because a retry after a
  timeout is how duplicate positions happen.
- Orders follow a state machine with an explicit table of legal moves; a filled
  order can never become cancelled.
- Market and stop-market orders carry **market protection** (Zerodha rejects
  them without it). The order rate is configured at 3 per second and can never
  exceed 5, one budget for the whole account.

### 6.8 Protecting a position

When reconciliation sees an entry filled, it books the position from the plan
registered when the entry was sent, and **attaches the protective stop at
once**: place it, verify the broker shows it working, and if that fails, exit
at market; if the exit fails too, halt the session (`NAKED_POSITION`). The only
way to obtain a "protected position" object is through the good end of that
sequence. A position restored after a restart is *unverified* until the broker
confirms its stop.

### 6.9 Watching the book - reconciliation

Every 30 seconds: read the broker's positions, then its orderbook, then our
records - and act only on a complete view (three failed reads in a row halt the
day). The broker is the legal record and wins every disagreement. Any intraday
(MIS) fill our orders do not explain **halts** the system - so the owner trading
MIS by hand on the same account will halt it (a deliberate policy). A holding
our book has that the broker does not is never "exited" (that would sell shares
we do not own); it halts too.

### 6.10 Getting out

Three exits, in precedence: a **due deadline** wins (time exit), then a stop
already filling, then the **target** (2R by default, on a fresh price only). A
time exit cancels the stop, waits until the broker shows it dead, then sends a
protected market exit sized to the smaller of our book and the broker's holding.

### 6.11 Halts and the kill switch

Three latches in Redis - the kill switch, the daily loss limit and the losing
streak - each recording why, when and by whom it was armed. **The first reason
wins** and **nothing expires**: a halt is cleared only by a named person with
`scripts/halt.py clear`. A halt stops new entries; it never stops exits, and it
never liquidates by itself (closing positions is a separate, deliberate command,
E14-S13).

### 6.12 End of day

After the books close (15:35, or 15:40 from 7 Dec 2026), the service runs one
final reconciliation, reads the account again and judges it. Anything left - a
position in the book or the database, shares at the broker, a working order, an
unsettled record, or a read that failed - is **NOT FLAT**: it is named, audited,
and arms the kill switch, so the next session refuses every signal until the
owner has looked. The day's statistics (trades, wins and losses, gross and net
P&L after charges, R-multiples) use the same arithmetic as the daily loss limit,
so the two can never disagree.

### 6.13 Paper mode

`broker/paper.py` is a simulated Kite *client* placed behind the real adapter,
so a paper session runs exactly the code that trades: fills on the far side of
the spread, partial fills to the visible depth, stop-market triggers, margin
rejections and Zerodha's intraday charges. Every order-path object reports
whether it is LIVE or PAPER, and the service refuses a mixture.

### 6.14 The base universe

`premarket/universe.py` builds the day's starting list from `universe.base`:
a Nifty 50/100/200/500 constituent file (from niftyindices.com, or NSE's archive
when the publisher answers with a web page), the F&O stocks (unexpired stock
futures whose underlying is an NSE equity), or a custom list. Every symbol is
resolved against the instrument master; what does not resolve is named, and a
malformed file, an implausible count or a stale master refuses the build instead
of producing a quietly shorter list.

### 6.15 Autonomy - automatic and manual

| Level | The system | The owner |
|---|---|---|
| L0 Observe | analyses and logs | reads the daily report |
| L1 Alert (**today's setting**) | sends the plan and signals with reasons | places the trade by hand, if the owner agrees with it |
| L2 Approve | prepares each order and waits | approves or rejects each trade (a timeout skips it) |
| L3 Supervised auto (the design target) | trades inside an envelope, escalates outside it | handles escalations |
| L4 Full auto | trades everything, reports after | reviews at the end of the day |

The level is enforced in code today (L2 and L3 refuse until their controls
exist). The system demotes itself on losses or AI disagreement. Some things
**never become automatic**: changing risk limits, adding capital, adding a
broker or credential, resuming after a halt, closing positions after a halt, and
moving from paper to live.

---

## 7. Rules that never bend

The six invariants, each enforced in code and tested in `tests/security/`:

1. A `Recommendation` never gains a sizing field.
2. Hard bounds are code constants, not configuration.
3. No `eval`, `exec` or dynamic import in the strategy path, ever.
4. Secrets never render - only `SecretString` holds a credential.
5. Every position has a stop and a time exit.
6. Fail closed.

**Hard bounds** (`common/config.py`; configuration can only tighten them):

| Constant | Value |
|---|---|
| `MAX_ORDERS_PER_SECOND` | 5 (the exchanges' registration threshold, `SEBI_ALGO_REGISTRATION_OPS`, is 10; the module refuses to import if the cap ever reaches it) |
| `MAX_RISK_PCT_PER_TRADE` | 10.0 |
| `MAX_POSITION_SLOTS` | 20 |
| `MAX_DAILY_LOSS_PCT` | 25.0 |
| `MAX_CONSECUTIVE_LOSS_HALT` | 5 |
| `MAX_PRICE_DIVERGENCE_PCT` | 5.0 |
| `MIN_STRATEGY_TRIALS` / `MAX_PBO_ALLOWED` / `MAX_ACTIVE_STRATEGIES` | 50 / 0.6 / 12 |

**Conventions:** `Decimal` for every price and rupee; timezone-aware UTC
everywhere, IST only at display and market-hours boundaries; frozen models that
refuse unknown fields; per-stock square-off deadlines stored on the position at
open and never recomputed.

---

## 8. Compliance and tax

This is a summary for orientation, not legal advice.

**SEBI's retail algo framework** (circular of 4 Feb 2025; in force since 1 Apr
2026) and the exchanges' implementing circulars, as they apply to a personal
algorithm below 10 orders per second:

| Topic | Position |
|---|---|
| Registration | Not required below 10 orders/sec. The system is capped at 5. |
| Static IP | Required for **order** requests (place, modify, cancel) from 1 Apr 2026; data, WebSocket, orderbook and positions work from any address. Zerodha: up to two IPs per developer account, shared by its apps, one change per calendar week. |
| Algo-ID tag (B1) | Closed in Sep 2026, **reopened** 16 Nov 2026 by audit LEG-001. Research (21 Nov 2026) finds the *broker* writes the exchange's generic ID ("99999") on sub-threshold orders, so the client sends nothing - but live mode refuses to start until Zerodha's written answer is recorded (`broker.algo_tagging_confirmation`). |
| Hosting (B10) | NSE's FAQ (3 Nov 2025) lets a client host the algorithm "at their end" with a static IP; Zerodha staff say any infrastructure is fine below 10 OPS. Needs Zerodha's written answer. No text requires the client's server to be in India - running in an India region is this system's own rule (HAL-001). |
| Order limits | Zerodha counts 10 orders/sec per client ID across NSE and BSE together, plus 400 a minute and 5,000 a day; rejected and invalid requests count. |
| Market protection | Every market and stop-market order carries it (Zerodha rejects zero). |
| Advice | Signals go to **one** recipient, the owner. Sharing them could make this regulated investment advice; the config refuses a second recipient. |
| Daily login | Manual, once a day - the exchange requires it. |

**Tax:** intraday equity trading is speculative business income (ITR-3).
Turnover is the absolute sum of each trade's profit or loss. Charges (brokerage,
STT, exchange, SEBI fee, stamp duty, GST) are modelled in
`execution/charges.py` from Zerodha's published schedule and must be checked
against a real contract note. A chartered accountant should review the tax
reports (E21).

---

## 9. The MVP and the road to live capital

**MVP** (`MVP_UI_AND_LEGAL.md §4`): *the smallest system that can run a full
trading day unattended at L2/L3 on paper capital, with complete auditability and
safe failure.* Anything not needed for that comes after.

**What the MVP still needs**, roughly in build order:

1. Process entrypoints and a first local run of the compose topology (E23-S03).
2. Market data: the owner's Kite Connect app; history backfill (E03); hazard
   lists (E04); the universe filters and scoring (E07).
3. Market context and regime (E09); news (E08), or a strategy without a news
   guard.
4. The AI client, review and `Recommendation` emission (E10, E13-S03/S04).
5. The pre-market pipeline to a locked plan (E11).
6. Approvals and alerts (E16, E19); the dashboard (E17).

**The road to real capital** (`PRE_LIVE_CHECKLIST.md` - count its ticked items;
on 23 Nov 2026, one):

```
paper trading across >= 2 market regimes
  -> review every paper trade against its plan
  -> L2 approval mode, live capital, smallest size
  -> >= 20 approved trades -> L3 supervised auto, still small
  -> scale only after a sustained, understood record
```

A strategy reaches live capital only after the gauntlet, at least 20 shadow
sessions with at least 80% live-versus-backtest agreement, at least 30 paper
trades with positive expectancy after costs, and a human approval.
**MVP-complete does not mean ready for capital** - only evidence does.

---

## 10. Development - how work is done

### 10.1 The delivery lifecycle (`/sdlc`)

Every backlog item goes through the same phases (`.claude/skills/sdlc/SKILL.md`):

| Phase | Question |
|---|---|
| P0 Orient | What is true now? (git, CI, tracker, code - in that order of authority) |
| P1 Business analysis | Is this the right thing, and is it ready? Falsifiable acceptance criteria; scope written down |
| P2 Research | Is every fact the design rests on verified? Primary sources, triangulated, dated |
| P3 Architecture | What does this make hard later? A design record; dangerous states unrepresentable |
| P4 Development | Contracts first, then the failing probe, then the code, then the control |
| P5-P8 QA rounds | Component; integration with real neighbours; adversarial (bandit, pip-audit, gitleaks, STRIDE); regression, mutation and the full gate |
| P9 Sign-off | Commit with findings, push, CI green, promote DEV → QA |
| P10-P11 SIT | Run the system tests on the QA tree; log every defect |
| P12 PROD | **Stop.** Report and ask - PROD is never promoted without the owner's explicit permission |

**Story statuses:** New → Development Completed (code done, something recorded
as remaining) → QA-Success (promoted, CI green on DEV and QA, SIT clean) →
Closed (accepted by the owner, in practice reached PROD).

### 10.2 Standards worth knowing

- **Structure over discipline:** make the bad state unrepresentable; next,
  refuse it at construction; next, raise at runtime; next, test it. A comment
  is the weakest control.
- **Absence is explicit:** `None` means unknown and is refused; `or 0`,
  `or False` and a bare `except: pass` are where unknown silently becomes fine.
- **Probe every safety claim:** build the invalid thing and watch it fail; pass
  a real-shaped secret and grep the output. Static review missed real bugs here;
  running the claim found them in seconds.
- `Documents/ENGINEERING_STANDARD.md` is the reference; `Code/CLAUDE.md` records
  every belief that turned out false, so it is not believed twice.

### 10.3 Local setup (Windows development machine)

```bash
cd Code
py -3.11 -m venv .venv && .venv/Scripts/activate
python -m pip install --upgrade pip setuptools wheel
pip install -c constraints.txt -e ".[dev]"
pip install --no-deps --upgrade "autobahn==26.10.2"   # replaces a vulnerable pin
cp .env.example .env                                  # then fill in the passwords
# Docker Desktop must be running - integration and system tests use containers
```

`make` is not installed on Windows, so the direct commands are used:

```bash
ruff check . && ruff format --check .
mypy src
REQUIRE_DOCKER=1 pytest tests/ -q --cov=src/algotrader --cov-report=json:coverage.json
python scripts/coverage_gate.py coverage.json
python scripts/doctor.py        # pre-flight; Datastores fails on a dev machine by design
```

`Code/SETUP.md` has the longer version.

### 10.4 The tracker

`Documents/BACKLOG_Tracker.xlsx` is the system of record: sheets `Backlog`,
`Blockers`, `QA Results`, `Security Findings`, `SIT Defects`. Where the tracker
and the code disagree, the code is right and the tracker is corrected.

---

## 11. Testing

| Layer | Where | What it proves |
|---|---|---|
| Unit | `tests/unit/` | each component's rules, with a control test beside every refusal |
| Property | `tests/property/` | invariants over generated inputs (Hypothesis) - e.g. risk never exceeds the budget, for any input |
| Integration | `tests/integration/` | real Postgres and Redis in containers, the real Kite adapter over a strict fake account or the paper broker |
| Security | `tests/security/` | the six invariants, redaction, paper isolation, secrets per container, dependency hygiene |
| System (SIT) | `tests/sit/` | whole simulated trading sessions - restarts, outages, kill switches, deadlines - run against the QA branch |
| Mutation | scratch harnesses per safety-critical story | inject plausible defects into the source; every one must be caught |
| Documents | `tests/unit/test_doc_claims.py` and a pre-commit hook | no document claims what the code has falsified |

**Green means every test passed with zero skipped** (`REQUIRE_DOCKER=1` turns a
missing Docker into a failure instead of hundreds of silent skips).
`scripts/coverage_gate.py` requires every line of `execution/risk/` and
`execution/sizer.py` to be covered and keeps a floor under the package total
(`TOTAL_FLOOR`). Coverage says a line ran; mutation testing says a test would
notice if it were wrong - both are used.

---

## 12. CI/CD - from a commit to QA to PROD

### 12.1 Branches

| Branch | Purpose | Gate to enter |
|---|---|---|
| `DEV` | where all work lands | CI green |
| `QA` | validation and paper verification | CI green on the exact commit, promoted by `scripts/promote.py` |
| `PROD` | real capital | the pre-live checklist signed off, and the owner's explicit permission |
| `main` | baseline | - |

### 12.2 Continuous integration (`.github/workflows/ci.yml`)

Runs on every push and pull request to DEV, QA, PROD and main:

| Job | Does |
|---|---|
| Lint, format, types | `ruff check .`, `ruff format --check .`, `mypy src` |
| Tests | the whole suite with containers, then the coverage gate |
| Security | safety invariants and pen-test suite, dependency audit (pip-audit), static analysis (bandit), secret scan (gitleaks) |
| Container image | builds the runtime image |
| **CI gate** | passes only if all four pass - the one status to require |

### 12.3 Promotion

- **DEV → QA:** `python scripts/promote.py --from DEV --to QA`. It refuses
  anything else (PROD above all), refuses unless CI passed DEV's head commit,
  refuses on divergence, strips `Documents/`, `scripts/tracker/` and `.claude/`
  from the QA tree, never force-pushes, and verifies the pushed tree. When DEV is
  ahead only by documents or the tracker, it says "nothing to do".
- **QA → PROD:** the owner's route, `.github/workflows/promote.yml`. Its PROD
  path exits with an error until the pre-live checklist is signed off.
- **Branch protection on PROD** is the owner's switch and is recommended - it is
  the only gate that also stops a hand-typed `git push`. `DEPLOYMENT.md §5` gives
  the rules. It deliberately does **not** require the CI-gate status on QA:
  `promote.py` pushes a fresh promotion commit with no status yet, so that rule
  would refuse every promotion (corrected 23 Nov 2026).
- **Rollback:** `DEPLOYMENT.md §8`.

---

## 13. Configuration and secrets

| File | Holds | Notes |
|---|---|---|
| `Code/config/system.yaml` | every tunable: mode, risk, universe, execution, AI, autonomy | validated in three gates at load; a key nothing reads fails a test (`test_config_honesty.py`) |
| `Code/config/strategies/*.yaml` | strategy documents | validated and compiled at load |
| `Code/config/nse_holidays.yaml` | the trading calendar | covers 2026; the calendar refuses an uncovered year - **renew every December** |
| `Code/.env` | passwords and API keys | never committed; each value reaches only the container that uses it |

Today's key settings: paper mode; ₹5,00,000 capital; 5 slots; 1% risk per trade
(₹5,000); 3% daily loss limit (₹15,000); ATR × 1.5 stop; 2R target; at most
3 orders per second (hard cap 5); autonomy L1 (alert only).

---

## 14. What is left, in numbers

From the tracker on 23 Nov 2026 (after that day's story review and E03-S01,
which raised E04-S09), each open story classified by what it needs:

| | Stories | Est. days |
|---|---|---|
| Done (QA-Success or Closed) | 82 of 218 | - |
| Removed (E21-S01, cancelled - the broker applies the generic algo ID) | 1 | - |
| Can be **completed now**, with nothing from the owner | 87 | ~115 |
| Can be **built now**; one real-world check needs an owner item | 28 | ~42 |
| **Needs an owner item to start** (12 of them wait only on decisions about news sources) | 20 | ~26 |

The review also split E23-S03: its new first half, **E23-S08 (process
entrypoints and a first local run of the stack)**, needs no VPS and is the step
that turns the components into a running system.

From this host the NSE files the data stories need are reachable (bhavcopy,
surveillance lists, price bands, index closes, the F&O ban list, corporate
actions, FII/DII), so the data epics (E03, E04, E09) do not wait for the
production host.

---

## 15. What is needed from the owner

| When | Item | Unblocks |
|---|---|---|
| Now | TOTP on the Zerodha account | API order placement |
| Now | **Kite Connect app, Connect plan (₹500/month)** - API key and secret in `.env` | live data, history, the daily login - goals 1-3 for real |
| Now | India-region server with a static IP, whitelisted in the Kite developer profile | live orders; the production deployment |
| Now | Anthropic API key (with a spend limit) | the AI layer |
| Now | Telegram bot token and chat id | alerts, approvals, the kill switch from a phone |
| Now | GitHub 2FA and branch rules | protects PROD |
| Now | Decisions: capital amount; any earlier backtests; NSE only or BSE too | sizing; the trial count; order routing |
| Before live | Zerodha's written answers (B1 tagging, B10 hosting); optional counsel; a CA | live mode; tax |
| Later | news provider, off-host backup storage, strategy approvals, 20 real daily logins | the remaining epics |

Step-by-step instructions for each were given on 23 Nov 2026; `PRE_LIVE_CHECKLIST.md`
and `RESEARCH_ALGO_TAGGING_AND_HOSTING.md` hold the regulatory detail and the
ready-to-send support ticket.

---

## 16. Where to find things

| Document | Read it for |
|---|---|
| `MASTER_REFERENCE.md` | the whole system in depth; §2 is the status authority |
| `LOW_LEVEL_ARCHITECTURE.md` | component designs, state machines, reconciliation, the runbook |
| `STRATEGY_ENGINE.md` | strategies, the gauntlet, the trial registry |
| `INDIA_FEATURES_AND_CONFIG.md` | Indian market specifics, the daily timeline, configuration |
| `MVP_UI_AND_LEGAL.md` | the MVP, the autonomy ladder, the UI, the legal framework |
| `PRE_LIVE_CHECKLIST.md` | the gate before real capital |
| `DEPLOYMENT.md` | branches, CI, promotion, rollback |
| `ENGINEERING_STANDARD.md` | invariants, the verification ladder, anti-patterns |
| `RESEARCH_ALGO_TAGGING_AND_HOSTING.md` | the B1/B10 research and the support ticket |
| `BACKLOG_Tracker.xlsx` | every story, blocker, QA result and defect |
| `Code/CLAUDE.md` | conventions and the record of beliefs that turned out false |
| `Code/SETUP.md` | the development machine |

---

## 17. Glossary

| Term | Meaning |
|---|---|
| **MIS** | Zerodha's intraday product: positions must close the same day (the broker squares off what is left) |
| **CAS** | Closing Auction Session for stocks with derivatives; from 7 Dec 2026, 15:30-15:40 |
| **Square-off** | closing an intraday position; ours happens before the broker's |
| **T2T / BE series** | trade-to-trade: every trade must be delivered, so intraday is impossible - excluded absolutely |
| **ASM / GSM** | exchange surveillance lists (extra margin, narrow bands) - excluded |
| **F&O ban** | a derivative in its ban period; equity is flagged |
| **Circuit band** | a stock's daily price limit (2%, 5%, 10%, 20%) |
| **Bhavcopy** | NSE's end-of-day price file (now in the UDiFF format) |
| **SL-M** | stop-loss market order - the protective stop |
| **Market protection** | a price guard on market orders that Zerodha requires |
| **OPS** | orders per second; registration threshold 10, this system's cap 5 |
| **Slot** | one of the N concurrent positions the capital is split into |
| **ATR** | Average True Range - the volatility measure that sets the stop distance |
| **R / R-multiple** | a trade's result divided by the risk it was opened with |
| **Latch** | a halt that stays on until a named human clears it |
| **Reconciliation** | comparing our records with the broker's; the broker wins |
| **Paper** | simulated trading through the same code as live |
| **Shadow** | a strategy that evaluates live but places no orders |
| **Gauntlet** | the 12 validation checks a strategy must pass |
| **DSR** | Deflated Sharpe Ratio - Sharpe corrected for how many trials were run |
| **PBO** | Probability of Backtest Overfitting |
| **CPCV** | combinatorial purged cross-validation - walk-forward testing without leakage |
| **SIT** | system integration testing - whole sessions on the QA branch |
| **Autonomy level** | L0 observe ... L4 full auto (§6.15) |
