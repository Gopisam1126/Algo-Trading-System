# MVP Feature Specification, User Interface Design & Legal Framework
## AI-Driven Algorithmic Trading Platform — Personal Use, Indian Markets

**Document type:** Product specification — scope, interface design, regulatory framing
**Status:** Design complete, pre-implementation
**Version:** 1.0 — 2026-08-04

**Document set — read in this order:**
1. [ARCHITECTURE_RESEARCH.md](ARCHITECTURE_RESEARCH.md) — the *why*
2. [INDIA_FEATURES_AND_CONFIG.md](INDIA_FEATURES_AND_CONFIG.md) — the *what* (market rules, config)
3. [LOW_LEVEL_ARCHITECTURE.md](LOW_LEVEL_ARCHITECTURE.md) — the *how* (services, schemas, security)
4. **This document** — the *scope, the screens, and the law*
5. [STRATEGY_ENGINE.md](STRATEGY_ENGINE.md) — strategy DSL, registry lifecycle, AI generation, validation gauntlet
6. [VERIFICATION_REPORT.md](VERIFICATION_REPORT.md) — cross-document audit
7. [PRE_LIVE_CHECKLIST.md](PRE_LIVE_CHECKLIST.md) — the consolidated gate before real capital

---

## Table of Contents

| § | Section |
|---|---|
| 1 | System Understanding — The Three Pipelines |
| 2 | **Legal & Regulatory Framework (Personal Use)** |
| 3 | Autonomy Model — Minimal Intervention, Safely |
| 4 | Feature Catalogue — MVP vs. Later |
| 5 | The News Analysis Engine (detailed design) |
| 6 | UI Architecture & Technology |
| 7 | Screen-by-Screen Specification |
| 8 | Data Visualization Standards |
| 9 | Admin & Configuration Interface |
| 10 | Mobile / Telegram Interface |
| 11 | UI Security |
| 12 | MVP Build Plan & Definition of Done |

---

## 1. System Understanding — The Three Pipelines

The brief describes three concurrent pipelines that converge at execution. Formalized:

### Pipeline A — Market Data → Technical Analysis → AI

```
WebSocket ticks
    → Validate (null/zero price, timestamp skew, negative volume)
    → Deduplicate (LRU on symbol+ts+price+volume — catches reconnect replay)
    → Outlier filter (>5×ATR move rejected; one bad print must not corrupt an EMA)
    → Normalize (UTC, Decimal, canonical schema)
    → Bar construction (1m → 5m → 15m → 1h → D → W, session-aligned to 09:15)
    → Incremental indicators (O(1) per bar, per symbol, per timeframe)
    → Level detection (S/R, pivots, prior day H/L, opening range)
    → Multi-timeframe snapshot
    → [Tier 1] Deterministic strategy evaluation → Trigger?
    → [Tier 2] AI review of the trigger  ─────────────────┐
                                                          │
```

### Pipeline B — News → Cleaning → Contextual Scoring

```
News feeds (allowlisted providers only)
    → Fetch + deduplicate (near-duplicate detection across syndication)
    → Sanitize (prompt-injection defence — LLD §10.6)
    → Entity resolution (article → ticker(s), sector, macro)
    → [AI triage — Haiku] Structured extraction: event type, polarity,
      impact scope, magnitude, novelty vs. prior coverage
    → Historical contextualization (compare against the symbol's rolling
      news history: is this new information or an echo?)
    → Time decay (exponential half-life by event class)
    → Aggregate → NewsScore per symbol + MacroScore per sector/market
                                                          │
                                                          │
```

### Pipeline C — Macro Condition Monitoring

```
GIFT Nifty · India VIX · FII/DII flows · sector rotation · economic calendar
    → Collect concurrently, each with its own timeout and staleness flag
    → Classify regime (risk-on / risk-off / high-vol / neutral)
    → MarketContext object, cached, refreshed every ~20 min
                                                          │
                                                          ▼
```

### Convergence → Decision → Multi-Stock Execution

```
            ┌─────────────────────────────────────────────┐
            │  Technical (A) + News (B) + Macro (C)       │
            │  + Daily Plan (pre-market AI synthesis)     │
            └──────────────────┬──────────────────────────┘
                               ▼
                   Recommendation (no size, no order)
                               ▼
                   ┌───────────────────────┐
                   │  RISK ENGINE          │  ← deterministic, no AI
                   │  14 sequential checks │
                   │  ATR-based sizing     │
                   └───────────┬───────────┘
                               ▼
                   Slot allocation (N concurrent stocks)
                               ▼
                   Order + protective stop, per symbol, in parallel
```

**The load-bearing property:** pipelines A, B, and C run on *independent clocks*. A is bar-paced (seconds to minutes), B is news-paced (~20 min), C is macro-paced (~20–60 min). They never block one another. Convergence happens by reading the latest cached output of B and C at the moment A produces a trigger — never by waiting for them.

---

## 2. Legal & Regulatory Framework (Personal Use)

> **This is a summary of research, not legal or tax advice.** The rules below are stated as they were researched in August 2026. Engage a Chartered Accountant before your first filing, and confirm the SEBI/broker specifics with your broker in writing.

### 2.1 The core question: is this legal?

**Yes — trading your own capital through a broker's API using an algorithm you wrote is permitted**, provided you stay inside a specific lane. The lane has four walls:

| Wall | Requirement | Consequence of crossing |
|---|---|---|
| **Whose money** | Your own account, and (with permission) immediate family — spouse, dependent children, dependent parents | Managing others' money without registration is a serious offence |
| **Whose advice** | You act on your own signals only. You do not publish, sell, or share them | Publishing recommendations can trigger Research Analyst regulations |
| **How fast** | Below the exchange order-rate threshold (10 orders/sec per segment) | Above it, the algo requires exchange registration |
| **How connected** | Through a registered broker, never direct to exchange, on a static whitelisted IP, from Indian servers | Non-compliance means the broker cuts API access |

### 2.2 SEBI retail algo framework — what applies to you

Fully mandatory since 1 April 2026. Recapping the operational requirements (full detail in companion doc §1):

- **Self-developed algos for personal use are explicitly permitted** without registering the algorithm, as long as the order-rate threshold isn't breached.
- Below 10 orders/second you still need a **Generic Algo-ID** from the exchange (obtained via your broker), but not individual algo approval.
- **Static IP whitelisting**, **India-hosted servers**, **OAuth + 2FA**, and **daily session logout before pre-open** are all mandatory. These are architectural constraints, not paperwork (LLD §1.1, C1–C3).
- The broker is the *principal* — legally responsible for every algo on their platform. This means their terms of service matter as much as SEBI's rules. **Read them.**

### 2.3 The line you must not cross: advisory regulations

This is the most commonly misunderstood boundary, and it's easy to cross accidentally.

**Safe (personal use):**
- Running the system on your own account
- Extending it to immediate family accounts with broker permission and 2FA-verified consent
- Discussing your approach generally, sharing the code as open source

**Not safe without registration:**
- Sending your system's buy/sell signals to friends, a Telegram group, or subscribers
- Charging anyone for access to the signals or the system's output
- Managing anyone else's account, even informally, even for free
- Publishing the AI's trade rationales publicly in a way that reads as recommendations

**The system design implication:** the alert/notification layer must be **single-recipient by construction**. No broadcast channels, no group chats, no public webhooks. This is enforced in config validation (§9.5) — the notifier refuses to start if more than one recipient is configured, unless an explicit `family_accounts` block with a compliance acknowledgement is present.

**A subtle trap:** Research Analyst regulations impose personal trading restrictions on registered analysts — no trading in a recommended security for 30 days before and 5 days after publishing. If you ever *do* register or publish, your own algo becomes entangled in those windows. Staying purely personal keeps this entirely out of scope.

### 2.4 Taxation — the part that shapes a feature

Indian tax treatment of intraday trading has quirks that make **automated tax reporting a genuine MVP feature**, not a nice-to-have.

| Aspect | Treatment |
|---|---|
| **Income head** | Intraday equity (no delivery) = **speculative business income**, reported under "Profits and Gains of Business or Profession" |
| **F&O** | **Non-speculative** business income — different bucket, different rules |
| **Return form** | **ITR-3** (business income) |
| **Turnover definition** | For intraday, turnover is the **absolute sum of profits and losses**, *not* transaction value. ₹2,000 profit + ₹1,500 loss = ₹3,500 turnover — a definition almost nobody computes correctly by hand |
| **Tax audit threshold** | Applies above ₹10 crore turnover, provided cash receipts and payments each stay under 5% of transactions |
| **Loss set-off** | Speculative losses offset **only** speculative gains. Cannot offset salary. Carried forward **4 years** (F&O losses: 8 years) — and only if the return is filed by the due date |
| **Filing deadline (AY 2026-27)** | ~31 August 2026 without audit; ~31 October 2026 with audit |
| **Advance tax** | Quarterly: 15 June, 15 September, 15 December, 15 March. (Presumptive-scheme taxpayers pay in one instalment by 15 March, but presumptive taxation under 44AD is generally not applicable to speculative trading — confirm with your CA) |

**Deductible business expenses** — and this matters for how you budget the system:

Brokerage, STT, GST on brokerage, exchange transaction charges, SEBI turnover fees, internet costs, **trading software and market data subscriptions**, professional/advisory fees, and depreciation on computing equipment. **Your VPS hosting, your Anthropic API spend, and your market data subscriptions are deductible business expenses against trading income.** Track them from day one — the system should record them (§4.4, F-31).

**Feature implications (all in MVP):**
- Every fill records STT, brokerage, GST, exchange charges, and stamp duty separately — not as a lumped "charges" figure. Reconstructing this at year-end from broker contract notes is painful.
- Turnover is computed using the intraday definition (absolute sum of P&L), continuously.
- A **Tax Report** export produces the ITR-3-relevant figures: speculative turnover, gross profit, gross loss, net speculative income, itemized expenses, and carried-forward loss position.
- Records must be retained. The system's immutable audit log serves this purpose — with the practical benefit that if the tax department ever asks how a trade was decided, you have the AI rationale, the indicator state, and the risk decision, timestamped.

### 2.5 Compliance features (MVP)

| ID | Feature | Why |
|---|---|---|
| L-1 | Algo-ID attached to every order | SEBI mandatory since 1 Apr 2026 |
| L-2 | Order-rate limiter (hard cap 5/sec) | Keeps you under the 10 OPS registration threshold |
| L-3 | Static IP verification at startup — refuse to start if the egress IP doesn't match config | Prevents accidental non-compliant operation after an infra change |
| L-4 | Daily re-auth with audit trail | SEBI session rule |
| L-5 | Immutable decision log with hash chaining | Regulatory record-keeping + tax substantiation |
| L-6 | Single-recipient notification enforcement | Prevents accidental drift into advisory territory |
| L-7 | Charge-level fill accounting | Tax computation accuracy |
| L-8 | Tax report generator (speculative turnover, P&L, expenses, carry-forward) | ITR-3 preparation |
| L-9 | Compliance self-check panel in admin UI | One screen showing every constraint's live status |

---

## 3. Autonomy Model — Minimal Intervention, Safely

The brief asks for minimal user intervention. The research on human-in-the-loop agent design converges on a clear principle: **the goal is not fewer checkpoints, it's checkpoints placed only where they change the outcome.** Governance, not capability, is the dominant failure mode — and the agent needs a boundary it cannot cross without a human, where crossing is an explicit, auditable event.

### 3.1 The autonomy ladder

The system ships with a configurable autonomy level. You climb it as trust is earned, and the system can *demote itself* automatically.

| Level | Name | System does | You do | When to use |
|---|---|---|---|---|
| **L0** | Observe | Analyzes, logs, no alerts | Read the daily report | First 2 weeks — validating analysis quality |
| **L1** | Alert | Sends the pre-market plan + live signals with rationale | Trade manually if you agree | Weeks 3–6 — validating signal quality |
| **L2** | Approve | Prepares complete orders, waits for your tap | Approve/reject per trade (30s window) | Weeks 7–10 — validating execution |
| **L3** | **Supervised auto** | Executes inside the envelope, escalates outside it | Handle escalations only (~1–3/week) | **The target steady state** |
| **L4** | Full auto | Executes everything, alerts after the fact | Review EOD | Only after months of L3 evidence |

**L3 is the design target.** Full autonomy (L4) removes the one control that catches the cases the system was never designed for. L3 gives you near-zero routine intervention while preserving a boundary.

### 3.2 The autonomy envelope (L3)

Inside the envelope → automatic. Outside → escalate to you with full context and a 60-second decision window; on timeout, **skip the trade** (never default to acting).

```yaml
autonomy:
  level: L3
  envelope:
    max_position_value_pct:      20      # of capital
    min_ai_confidence:           0.70
    min_timeframe_agreement:     2       # of 3
    max_india_vix:               22      # above this, escalate
    require_symbol_traded_before: true   # first-ever trade in a name escalates
    max_daily_trades_before_escalate: 8
    max_consecutive_losses_before_escalate: 2

  auto_escalate_on:
    - news_event_unmatched_to_thesis   # material news the morning plan didn't anticipate
    - regime_change_intraday           # macro classifier flips mid-session
    - correlation_spike                # holdings suddenly moving together
    - broker_reject_streak             # ≥2 rejections
    - reconciliation_drift             # broker state ≠ our state
    - unknown_position                 # ← auto-halt, not escalate

  auto_demote_to_L2_on:
    - daily_loss_gt_pct: 2.0
    - consecutive_losses: 3
    - ai_disagreement_rate_gt: 0.4     # AI vetoing most Tier-1 triggers = models disagree
    - win_rate_7d_lt: 0.30
```

**Auto-demotion is the important half.** A system that only climbs the ladder is a system that keeps its autonomy through a losing streak. When performance degrades or the models start disagreeing with each other, the system should ask for supervision without being told to.

### 3.3 What never becomes automatic

Regardless of level:
- Changing risk limits or autonomy config (restart-only, git-tracked — LLD §10.11)
- Increasing capital allocation
- Adding a new broker or credential
- Resuming after a kill-switch halt
- Closing positions after a halt (halting stops *new* entries; liquidating is always a human call)
- Moving from paper to live capital

### 3.4 Notification budget

Minimal intervention means minimal *notifications*, not just minimal decisions. Alert fatigue causes missed P0s.

| Priority | Daily budget | Examples |
|---|---|---|
| **P0 — act now** | ~0 (exceptional) | Kill switch, unknown position, auth failure, loss limit |
| **P1 — decide** | ≤ 3 | Escalations requiring approval |
| **P2 — know** | ≤ 6 | Trade opened/closed, plan ready |
| **P3 — dashboard only** | unlimited | Signal rejections, cache stats |

The notifier enforces these budgets: P2 alerts beyond budget are **batched into a digest** rather than sent individually.

---

## 4. Feature Catalogue — MVP vs. Later

**MVP definition:** the smallest system that can run a full trading day unattended at L2/L3 on paper capital, with complete auditability and safe failure. Anything not required for that is post-MVP.

Legend: **★ MVP** · ○ Phase 2 · ◇ Phase 3+

### 4.1 Pipeline A — Data, cleaning, technical analysis

| ID | Feature | Pri | Notes |
|---|---|---|---|
| A-1 | WebSocket tick ingestion with auto-reconnect | ★ | Foundation |
| A-2 | Tick validation, dedup, outlier rejection | ★ | One bad print corrupts indicators forever |
| A-3 | Multi-timeframe bar construction (session-aligned) | ★ | 1m/5m/15m/1h/D/W |
| A-4 | Incremental indicator engine (O(1) per bar) | ★ | EMA, RSI, MACD, ATR, BB, VWAP, volume |
| A-5 | Indicator warm-up from history with `is_ready` gate | ★ | Prevents trading off a 20-EMA built from 3 bars |
| A-6 | Support/resistance + pivot + prior-day levels | ★ | Needed for stop placement |
| A-7 | Opening Range (09:15–09:30) computation | ★ | Core to the primary strategy |
| A-8 | Multi-timeframe confluence scoring | ★ | The confluence principle, made numeric |
| A-9 | Corporate action adjustment | ★ | Unadjusted history produces phantom breakouts |
| A-10 | Historical data sync (bhavcopy + intraday) | ★ | Feeds pre-market analysis |
| A-11 | Chart pattern recognition (flags, triangles) | ○ | |
| A-12 | Candlestick pattern detection | ○ | |
| A-13 | Volume profile / market profile | ◇ | |

**Strategy Engine** — full specification in [STRATEGY_ENGINE.md](STRATEGY_ENGINE.md)

| ID | Feature | Pri | Notes |
|---|---|---|---|
| S-1 | Strategy DSL schema + compiler (declarative, not code) | ★ | The AI never writes executable code — it composes from vetted primitives |
| S-2 | Primitive library (~40 vetted primitives) | ★ | Human-reviewed; the AI cannot add to it |
| S-3 | Strategy registry + lifecycle state machine | ★ | DRAFT → VALIDATING → SHADOW → PAPER → APPROVED → ACTIVE → RETIRED |
| S-5 | Backtest harness with realistic India costs | ★ | STT, brokerage, GST, exchange charges, stamp duty, slippage |
| S-6 | Trial registry (append-only, counts every attempt) | ★ | Without an honest trial count, overfitting correction is meaningless |
| S-7 | Validation gauntlet G1–G5, G8, G12 | ★ | Hypothesis, compile, sample size, walk-forward, regime coverage, tradability |
| S-8 | **Deflated Sharpe Ratio + Probability of Backtest Overfitting** | ★ | The two checks that make automated strategy generation survivable |
| S-9 | Shadow mode (records signals, places no orders) | ★ | Catches failures a backtest cannot — signals at untradeable prices |
| S-10 | Degradation monitoring + automatic retirement | ★ | Auto-demote is automatic; promotion never is |
| S-4 | User-authored strategies (YAML editor, live validation) | ★ | |
| S-11 | Strategy admin UI | ★ | §9.5 |
| S-12 | AI generation — journal mode ("from previous experience") | ○ | **Phase 2 — only after the gauntlet exists** |
| S-13 | AI generation — observation mode (continuous market monitoring) | ○ | Phase 2 |
| S-14 | Gauntlet G9–G11 (locked holdout, correlation, sensitivity) | ○ | Phase 2 |
| S-16 | Visual strategy builder | ◇ | |
| A-14 | Options-derived signals (PCR, OI, max pain) | ◇ | Only if F&O is enabled |
| A-15 | Tick-level order book analysis | ◇ | Requires L2 data subscription |

### 4.2 Pipeline B — News ingestion, cleaning, scoring

| ID | Feature | Pri | Notes |
|---|---|---|---|
| B-1 | Multi-source news ingestion (allowlisted providers) | ★ | |
| B-2 | Near-duplicate detection across syndication | ★ | Same story from 6 outlets ≠ 6× the signal |
| B-3 | Prompt-injection sanitization | ★ | Security-critical (LLD §10.6) |
| B-4 | Entity resolution — article → ticker/sector/macro | ★ | |
| B-5 | AI structured extraction (event type, polarity, scope, magnitude) | ★ | See §5 |
| B-6 | **Novelty scoring against the symbol's news history** | ★ | The brief's "based on previous news and the latest" |
| B-7 | Time-decay weighting by event class | ★ | Yesterday's earnings ≠ this morning's earnings |
| B-8 | Composite NewsScore per symbol + MacroScore | ★ | |
| B-9 | Corporate announcement / earnings calendar ingestion | ★ | Drives the earnings-day hard filter |
| B-10 | Source credibility weighting | ○ | |
| B-11 | Cross-source corroboration scoring | ○ | Two independent sources > one |
| B-12 | Sentiment trajectory (is the story improving?) | ○ | |
| B-13 | Social media ingestion | ◇ | High injection risk; excluded from MVP deliberately |

### 4.3 Pipeline C — Macro, plan, and AI

| ID | Feature | Pri | Notes |
|---|---|---|---|
| C-1 | GIFT Nifty gap prediction | ★ | Direction reliable ~85–90%; magnitude is not |
| C-2 | India VIX regime classification | ★ | |
| C-3 | FII/DII flow ingestion | ★ | |
| C-4 | Economic calendar + event blackout windows | ★ | Stand down around RBI/CPI |
| C-5 | Sector rotation ranking (EOD/multi-day, not intraday) | ★ | Intraday sector ranks are noise |
| C-6 | Market Condition Context object with staleness flags | ★ | |
| C-7 | Universe hard filters (T2T/ASM/GSM/ban/circuit/liquidity) | ★ | India-specific; skipping these breaks the system |
| C-8 | Tradeability Score (6-component weighted) | ★ | |
| C-9 | Pre-market AI deep synthesis → thesis + playbooks | ★ | The centrepiece |
| C-10 | Pre-open gap adjustment & re-rank | ★ | |
| C-11 | In-session AI trigger review | ★ | |
| C-12 | AI thesis-invalidation monitoring | ○ | |
| C-13 | Trade journal → next-day AI context loop | ○ | High value, but needs history first |
| C-14 | Multi-agent specialization | ◇ | |
| C-15 | EOD AI self-review | ◇ | |

### 4.4 Execution, risk & compliance

| ID | Feature | Pri | Notes |
|---|---|---|---|
| E-1 | 14-check deterministic risk engine | ★ | |
| E-2 | ATR-based position sizing with all clamps recorded | ★ | |
| E-3 | Slot-based multi-stock capital allocation | ★ | The "multiple stocks at once" mechanism |
| E-4 | Correlation guard + sector exposure cap | ★ | 4 PSU banks ≠ 4 independent bets |
| E-5 | Automatic protective stop on every entry | ★ | Non-negotiable invariant |
| E-6 | Per-stock square-off deadline (CAS-aware) | ★ | 15:10 / 15:20 / 15:25 |
| E-7 | Idempotent orders (deterministic client_order_id) | ★ | Prevents duplicate positions after a timeout |
| E-8 | Broker reconciliation loop (30s) | ★ | |
| E-9 | Kill switch (auto + manual, phone-reachable) | ★ | |
| E-10 | Paper trading mode with production-identical path | ★ | |
| E-11 | Live margin verification before entry | ★ | Peak margin rules; no assumed leverage |
| E-12 | Order-rate limiter | ★ | L-2 |
| E-13 | Immutable decision audit log | ★ | L-5 |
| E-14 | Trailing stop / partial profit booking | ○ | |
| E-15 | Bracket / cover order support | ○ | Broker-dependent |
| E-16 | Smart order routing / iceberg | ◇ | |

| ID | Compliance & finance | Pri | |
|---|---|---|---|
| F-30 | Charge-level fill accounting (STT/brokerage/GST/exchange/stamp) | ★ | Tax accuracy |
| F-31 | Business expense register (VPS, API, data subscriptions) | ★ | Deductible — track from day one |
| F-32 | Intraday turnover computation (absolute sum of P&L) | ★ | The definition almost nobody gets right |
| F-33 | Tax report export (ITR-3 figures + carry-forward position) | ★ | |
| F-34 | Compliance status panel | ★ | L-9 |
| F-35 | Contract note reconciliation | ○ | |

### 4.5 Interface

| ID | Feature | Pri | Notes |
|---|---|---|---|
| U-1 | Live dashboard (positions, P&L, health, plan progress) | ★ | |
| U-2 | Pre-market plan view | ★ | The morning read |
| U-3 | Symbol detail (chart + indicators + AI rationale + news) | ★ | |
| U-4 | Trade history & journal | ★ | |
| U-5 | **Admin / configuration UI** | ★ | §9 |
| U-6 | Audit log explorer (searchable by correlation ID) | ★ | |
| U-7 | Manual controls (kill switch, halt, close position) | ★ | |
| U-8 | Telegram bot (alerts + approvals + kill switch) | ★ | The primary interface during market hours |
| U-9 | Compliance status panel | ★ | |
| U-10 | Performance analytics (equity curve, R-multiple, by-setup) | ○ | |
| U-11 | Backtest runner UI | ○ | |
| U-12 | Config change diff & history viewer | ○ | |
| U-13 | Strategy builder (visual) | ◇ | |

**MVP totals: 52 features.** Deliberately excluded from MVP: chart patterns, options signals, social media, multi-agent AI, visual strategy builder, smart routing. Each is a real feature; none is required to run a safe, auditable trading day.

---

## 5. The News Analysis Engine (detailed design)

The brief specifically calls for news analysis "based on previous news and the latest" producing a "relevant score." This deserves its own design, because the naive version — average today's sentiment scores — is close to useless.

### 5.1 Why a single sentiment number is insufficient

Three findings from the research drive the design:

1. **Surface sentiment and event semantics are partially orthogonal.** "Company announces layoffs" and "company announces restructuring" can score identically on polarity while meaning very different things for a stock. The field is moving toward multi-dimensional representations that explicitly model **event type, impact scope, and temporal dynamics** — not one scalar.
2. **Attention drives price response, not just content.** Salience — how much the market is *looking* at this — causally affects volume and price. A minor story everyone is reading beats a major story nobody noticed.
3. **Macro and firm-level news interact rather than substitute.** They warrant separate input streams, not one blended score.

### 5.2 The `NewsSignal` schema (AI structured output)

```python
class NewsSignal(BaseModel):
    # --- Identity ---
    article_id:       str
    cluster_id:       str            # groups near-duplicates across outlets
    symbols:          list[str]      # resolved tickers
    sector:           str | None
    scope:            Literal["FIRM", "SECTOR", "MACRO"]   # separate streams

    # --- Event semantics (not just polarity) ---
    event_type:       Literal[
        "EARNINGS", "GUIDANCE", "MERGER_ACQUISITION", "REGULATORY", "LEGAL",
        "MANAGEMENT_CHANGE", "PRODUCT", "CONTRACT_WIN", "DOWNGRADE_UPGRADE",
        "MACRO_POLICY", "COMMODITY", "OTHER"
    ]
    polarity:         Annotated[Decimal, Field(ge=-1, le=1)]
    magnitude:        Annotated[Decimal, Field(ge=0, le=1)]   # how big a deal
    certainty:        Annotated[Decimal, Field(ge=0, le=1)]   # rumour vs. confirmed
    horizon:          Literal["INTRADAY", "DAYS", "WEEKS", "STRUCTURAL"]

    # --- Provenance & integrity ---
    source:           str
    published_at:     datetime
    corroborating_sources: int
    injection_flagged: bool          # sanitizer fired on this content

    summary:          Annotated[str, Field(max_length=200)]
```

Note what is bounded: every field is an enum, a bounded number, or a length-capped string. Nothing free-form escapes into a downstream prompt. This is the containment boundary from LLD §10.6, expressed as a schema.

### 5.3 Novelty scoring — "previous news and the latest"

For each new article, compute novelty against the symbol's rolling 30-day news history:

```
novelty = w1 · (1 − max_similarity_to_recent_articles)      # semantic newness
        + w2 · event_type_rarity_for_this_symbol            # unusual event class
        + w3 · polarity_surprise                            # direction flips prior tone
        + w4 · (1 / cluster_size)                           # unique vs. widely syndicated
```

Interpretation matters here: the fourth term is deliberately *inverse* to syndication count for novelty, but syndication count feeds **salience** positively (§5.5). A story carried everywhere is less novel but more attended-to — the two effects are separated rather than conflated into one number.

### 5.4 Time decay by event class

Different news decays at different rates. One global half-life is wrong.

| Event class | Half-life | Reasoning |
|---|---|---|
| `EARNINGS`, `GUIDANCE` | 2 trading days | Priced in fast, but revisited |
| `REGULATORY`, `LEGAL` | 10 trading days | Slow-burn, uncertain resolution |
| `MERGER_ACQUISITION` | 15 trading days | Structural |
| `MANAGEMENT_CHANGE` | 5 trading days | |
| `CONTRACT_WIN`, `PRODUCT` | 3 trading days | |
| `DOWNGRADE_UPGRADE` | 2 trading days | |
| `MACRO_POLICY` | 20 trading days | Regime-shaping |
| `OTHER` | 1 trading day | Default to fast decay |

```
weight(article, t) = 0.5 ^ ((t − published_at) / half_life(event_type))
```

### 5.5 Composite score

```
salience  = log1p(cluster_size) · source_credibility · corroboration_factor

contribution(article) = polarity · magnitude · certainty · novelty · salience · decay_weight

NewsScore(symbol)  = clamp(Σ contribution over FIRM-scope articles,  −1, +1)
SectorScore(sector)= clamp(Σ contribution over SECTOR-scope articles, −1, +1)
MacroScore()       = clamp(Σ contribution over MACRO-scope articles,  −1, +1)
```

Three separate scores, per finding (3) above — never blended into one. The Tradeability Score consumes `NewsScore` as its catalyst component; `MacroScore` feeds regime classification; `SectorScore` feeds relative strength.

### 5.6 Guardrails

- **A news score can never, by itself, trigger a trade.** It is 10% of the Tradeability Score. Technical confluence is 25%. This bounds the damage from any news-layer failure or injection.
- **A stale news pipeline degrades to `NewsScore = 0`**, not to the last known value. Acting on yesterday's sentiment as if it were today's is worse than acting on none.
- **`injection_flagged` articles are excluded from scoring** and surfaced in the admin UI. A cluster of flagged articles on one ticker is itself worth investigating.
- **Earnings-day symbols are hard-filtered out** regardless of sentiment. A technical thesis is meaningless against a binary event.

---

## 6. UI Architecture & Technology

### 6.1 The user model

One user. One device at a time, usually. Two contexts:

| Context | Device | Frequency | Needs |
|---|---|---|---|
| **Morning review** | Desktop | Daily, ~10 min | Density, charts, the full plan |
| **Market hours** | Phone | Reactive, seconds | Alerts, approve/reject, kill switch |
| **Evening review** | Desktop | Daily, ~10 min | Journal, performance, config tuning |
| **Weekend tuning** | Desktop | Weekly | Config, backtests, analytics |

**This shapes everything.** The phone interface is not a responsive shrink of the dashboard — during market hours you need three things: *what happened*, *approve or reject*, *stop everything*. Telegram delivers those better than any web UI, because it's already on the lock screen (§10).

### 6.2 Technology decisions

| Layer | Choice | Reasoning |
|---|---|---|
| **Server** | FastAPI (already in the stack — LLD §3.1) | Same process family, Pydantic models shared with the backend, no separate API contract to maintain |
| **Page rendering** | **Jinja2 + HTMX** for everything except the live dashboard | Research is consistent: HTMX is well-suited to CRUD, forms, and admin panels — which is most of this UI. ~14KB, no build pipeline, no separate frontend deployment. For a solo developer this removes an entire toolchain. |
| **Live dashboard** | HTMX shell + **Alpine.js** islands + **SSE** | The honest caveat from the research: real-time dashboards with streaming visualizations need more than HTML swapping. So the live-updating parts are small JS islands, not a full SPA. SSE (not WebSocket) because the flow is one-directional server→client and SSE reconnects automatically. |
| **Charts** | **TradingView `lightweight-charts`** | 45KB gzipped, canvas-rendered, 60+ FPS with thousands of points, purpose-built for financial time series, and `series.update()` is designed exactly for streaming ticks. Recharts renders SVG through React components — wrong tool for a streaming candlestick. uPlot is smaller and faster but documentation is sparse. |
| **Non-financial charts** | Inline SVG or `uPlot` | Equity curve, R-multiple distribution, exposure bars. Small, no dependency weight. |
| **CSS** | Tailwind (build step) or hand-written custom properties | Either is fine; the design tokens in §8 matter more than the framework |
| **Auth** | **WebAuthn passkey** primary, TOTP fallback | Passkeys are phishing-resistant by design — the private key never leaves the device and credentials are origin-bound. For a system that can move money, this is worth the setup. |

**Explicitly rejected: a React SPA.** For a complex, highly interactive real-time dashboard React handles interactions more smoothly after initial load — but that advantage is bought with a build pipeline, a separate deployment artifact, a state-management layer, and an API contract to keep in sync, all maintained by one person who also has a trading system to run. The hybrid — HTMX everywhere, small JS islands where genuinely needed — is the right trade at this scale. Revisit if the UI grows a genuinely complex interactive surface.

### 6.3 Page structure

```
/                        Live Dashboard          (SSE-updating)
/plan                    Today's Plan            (static after 09:12)
/plan/{date}             Historical plan
/symbol/{symbol}         Symbol Detail           (chart + AI + news)
/positions               Positions & Orders      (SSE-updating)
/journal                 Trade Journal
/analytics               Performance Analytics
/audit                   Audit Log Explorer
/admin                   Admin Home
  /admin/config          Configuration Editor
  /admin/universe        Universe & Filters
  /admin/strategies      Strategy Registry (list, states, DSR/PBO, vs-backtest)
  /admin/strategies/{id} Strategy Detail (definition · hypothesis · validation · live)
  /admin/strategies/new  Strategy Builder (YAML editor, live validation)
  /admin/trials          Trial Log (search cost, current DSR bar)
  /admin/risk            Risk Limits
  /admin/ai              AI Settings & Budget
  /admin/news            News Sources & Injection Monitor
  /admin/autonomy        Autonomy Level & Envelope
  /admin/compliance      Compliance Status
  /admin/system          Services, Health, Logs
  /admin/tax             Tax Report & Expense Register
```

---

## 7. Screen-by-Screen Specification

### 7.1 Live Dashboard (`/`)

The one screen that must answer "is everything OK?" in under two seconds.

```
┌────────────────────────────────────────────────────────────────────────────┐
│  ● TRADING   L3 Supervised   Interval 5m   14:23:07 IST      [HALT] [KILL] │
├────────────────────────────────────────────────────────────────────────────┤
│  Day P&L          Open Risk        Slots        Win/Loss    Drawdown       │
│  ▲ +₹4,280        ₹12,500          3 / 5        4W / 2L     -0.8%          │
│  +0.86%           2.5% of cap                   67%         limit -3.0%    │
├────────────────────────────────────────────────────────────────────────────┤
│  OPEN POSITIONS                                                            │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │ Sym      Side  Qty   Entry    LTP     P&L      R     Stop    Exit by  │  │
│  │ TATAMOT  LONG  120   712.40   718.90  ▲+780   +0.9  705.20   15:10 ⚠ │  │
│  │ INFY     LONG   45  1584.00  1591.20  ▲+324   +0.5 1570.00   15:20    │  │
│  │ HDFCBANK SHORT  60  1642.00  1638.50  ▲+210   +0.3 1655.00   15:10 ⚠ │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                     ⚠ = CAS stock, earlier square-off       │
├─────────────────────────────────┬──────────────────────────────────────────┤
│  WATCHLIST (from plan)          │  MARKET CONTEXT                          │
│  ┌───────────────────────────┐  │  Regime      RISK-ON                     │
│  │ RELIANCE  ○ waiting  82   │  │  Nifty       24,180  ▲ +0.42%            │
│  │ SBIN      ● TRIGGERED 79  │  │  India VIX   13.2    ▼ −4.1%             │
│  │ ICICIBANK ○ waiting  76   │  │  FII (cash)  +₹1,240 cr                  │
│  │ MARUTI    ✕ invalidated   │  │  Sector lead IT ▲ · Auto ▲ · FMCG ▼      │
│  └───────────────────────────┘  │  News        ⚠ 2 flagged (see admin)     │
├─────────────────────────────────┼──────────────────────────────────────────┤
│  ACTIVITY (live)                │  SYSTEM HEALTH                           │
│  14:22 SBIN triggered ORB long  │  Feed        ● 0.3s lag                  │
│  14:22 AI review: CONFIRM 0.78  │  Broker      ● session valid to 09:00    │
│  14:20 RELIANCE rejected —      │  AI          ● 3.2s p95 · ₹142 today     │
│        sector exposure limit    │  Redis/DB    ● ●                         │
└─────────────────────────────────┴──────────────────────────────────────────┘
```

**Design decisions:**
- **Status bar is always visible and colour-coded**, but never colour-*alone* — the ● glyph plus the word "TRADING" carries the state.
- **Kill switch is always present, top-right, and requires a confirm step.** Never behind a menu.
- The **`⚠` on square-off times** is deliberate: the CAS regime means different stocks close at different times, and that's exactly the kind of thing that's easy to forget and expensive to forget.
- The **Activity feed shows rejections, not just actions.** "Why isn't it trading?" is the most common question a system like this provokes, and the answer should be on the main screen.
- **Everything updates via one SSE stream**, not per-widget polling.

### 7.2 Today's Plan (`/plan`)

Delivered at 09:13 and reviewed before the open. The most valuable screen in the system during Phase 3 (alert-only).

```
┌────────────────────────────────────────────────────────────────────────────┐
│  PLAN — Tuesday, 4 August 2026            Generated 08:52 · Opus · 47s     │
├────────────────────────────────────────────────────────────────────────────┤
│  MARKET THESIS                                                             │
│                                                                            │
│  Expected regime: RISK-ON, moderate conviction                             │
│                                                                            │
│  US closed higher overnight (S&P +0.6%), Asia following. GIFT Nifty        │
│  indicates a +85pt gap up. India VIX at 13.2 is in the lower quartile of   │
│  its 30-day range, favouring trend-continuation over mean-reversion.       │
│  FII flows turned positive over the last three sessions.                   │
│                                                                            │
│  Bias:          Long-favoured, particularly IT and Auto                    │
│  Nifty levels:  Support 24,050 / 23,920 · Resistance 24,310 / 24,450      │
│  Invalidation:  A close below 24,050 or VIX above 16 would void this read  │
│  Events today:  None scheduled                                             │
│                                                                            │
│  ⓘ Gap-up opens frequently fade in the first 30 minutes. The system does   │
│    not enter before 09:20 and requires opening-range confirmation.         │
├────────────────────────────────────────────────────────────────────────────┤
│  RANKED CANDIDATES                              8 active · 2 gap-invalid   │
│  ┌──────────────────────────────────────────────────────────────────────┐ │
│  │ #  Symbol     Score  Bias   Conf   Setup            Gap     Status    │ │
│  │ 1  TATAMOT     87    LONG   0.82   ORB breakout    +0.4%   ● ACTIVE  │ │
│  │ 2  RELIANCE    82    LONG   0.75   Trend pullback  +0.2%   ● ACTIVE  │ │
│  │ 3  SBIN        79    LONG   0.71   S/R bounce      +0.1%   ● ACTIVE  │ │
│  │ 4  MARUTI      77    LONG   0.68   ORB breakout    +2.6%   ✕ GAP     │ │
│  │ 5  INFY        76    LONG   0.74   Trend cont.     +0.5%   ● ACTIVE  │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
│                                                    [Expand all playbooks]  │
└────────────────────────────────────────────────────────────────────────────┘
```

Expanding a candidate shows the full playbook: the setup, why it's attractive, what confirms it, what invalidates it, the multi-timeframe read, the score breakdown by component, and relevant news.

**Two design points worth noting.** First, the **score breakdown is always available** — a bare "87" is unaccountable; showing that it's 22/25 trend + 18/20 relative strength + 12/15 volatility + … makes the ranking debuggable. Second, **gap-invalidated candidates stay visible** rather than disappearing. Seeing that MARUTI was ranked #4 and then gapped 2.6% past its entry is information; silently dropping it is not.

### 7.3 Symbol Detail (`/symbol/{symbol}`)

Chart + evidence, in one view.

```
┌────────────────────────────────────────────────────────────────────────────┐
│  TATAMOTORS   ₹718.90  ▲ +6.50 (+0.91%)        [5m] 15m  1h  1D   ● LONG  │
├────────────────────────────────────────────────────────────────────────────┤
│                                                                            │
│         [ lightweight-charts candlestick + EMA overlays + volume ]         │
│         [ Opening range band · S/R levels · entry / stop markers ]         │
│                                                                            │
├──────────────────────────────┬─────────────────────────────────────────────┤
│  MULTI-TIMEFRAME             │  AI ASSESSMENT              CONFIRM · 0.82  │
│  Weekly    ▲ Uptrend         │                                             │
│  Daily     ▲ Uptrend         │  All three timeframes agree on an uptrend,  │
│  Hourly    ▲ Uptrend         │  and price is holding above the opening     │
│  Agreement 3 / 3   ●●●       │  range high on above-average volume. The    │
│                              │  auto sector is leading today, and the      │
│  RSI(14)      58.4           │  overnight news flow is mildly positive.    │
│  ATR(14)      12.80          │                                             │
│  Vol vs 20d   1.8×           │  Supporting: 3/3 timeframe agreement ·      │
│  Dist to R1   +1.2%          │  sector leadership · volume expansion       │
│                              │  Risks: extended from the 20-EMA · index    │
│  Score        87 / 100       │  approaching resistance at 24,310           │
├──────────────────────────────┼─────────────────────────────────────────────┤
│  NEWS  (score +0.42)         │  POSITION                                   │
│  ▲ 07:40 Q1 revenue beats    │  Entry   712.40 × 120    Stop  705.20       │
│    est. · EARNINGS · 0.8     │  Now     718.90          Target 726.80      │
│  ▲ 06:15 Analyst upgrade     │  P&L     ▲ +₹780 (+0.9R) Exit by 15:10 ⚠   │
│    · UPGRADE · 0.6           │                                             │
│  ○ Yesterday: 3 articles,    │  [Move stop to breakeven] [Close position]  │
│    decayed to 0.1            │                                             │
└──────────────────────────────┴─────────────────────────────────────────────┘
```

The news panel deliberately shows **decayed contributions of older news**, not just today's headlines — that's the "previous news and the latest" requirement made visible.

### 7.4 Audit Log Explorer (`/audit`)

Search by `correlation_id` and the full life of one trade unrolls: pre-market candidacy → trigger → AI review (with the exact prompt hash and token usage) → each of the 14 risk checks with pass/fail → sizing computation showing which clamp bound → order → fill → stop placement → exit. Every stage timestamped with its latency.

This is the screen that turns "why did it do that?" from an investigation into a lookup — and it doubles as the tax-substantiation record (§2.4).

---

## 8. Data Visualization Standards

These are enforceable rules, not aesthetic preferences. Each one prevents a specific, common failure.

### 8.1 Non-negotiables

| Rule | Why |
|---|---|
| **Never a dual-axis chart.** Two measures of different scale → two stacked charts, small multiples, or index both to a common base | The single most common charting mistake; dual axes let you imply any correlation by choosing scales |
| **Categorical colours in a fixed order, never cycled.** Colour follows the *entity*, not its rank | Filtering the symbol list must not repaint the survivors — if RELIANCE is blue, it's blue on every screen, always |
| **Sequential = one hue, light→dark. Diverging = two hues + a neutral grey midpoint.** Never a rainbow, never a hue at the diverging midpoint | Rainbow scales imply ordering that doesn't exist and are unreadable under CVD |
| **Status colours are reserved** (good / warning / serious / critical) and never reused as a series colour | If green means "healthy" in one widget and "series 3" in another, both become unreadable |
| **Validate the palette computationally.** Run the checker for adjacent-pair CVD separation, lightness band, chroma floor, and contrast — against both light and dark surfaces | Colourblind-safety is computable; eyeballing it is how it goes wrong |
| **Legend always present for ≥2 series; ≤4 series also direct-labelled** | Identity must never be colour-alone |
| **Text wears text tokens, never the series colour** | A coloured swatch beside neutral-ink text carries identity without sacrificing legibility |
| **Thin marks, recessive gridlines, selective labels** — never a number on every point | Density without noise |

### 8.2 The red/green problem — a specific, important case

Indian markets use the same convention as most: green up, red down. This is simultaneously the most expected encoding and **the single worst colour pair for colour-vision deficiency** — red/green confusion is the most common form.

**The rule: direction is never encoded by colour alone.** Every P&L figure, every price change, every position row carries:

1. An **arrow glyph** — ▲ / ▼
2. An **explicit sign** — `+₹780` / `−₹340`
3. Colour, as *reinforcement only*

```
✓  ▲ +₹780 (+0.91%)     in green
✓  ▼ −₹340 (−0.42%)     in red
✗  ₹780                 in green          ← unreadable for ~8% of men
```

The same principle applies to the status dots: `● TRADING` not just `●`.

### 8.3 Palette assignment by job

| Job | Where used | Encoding |
|---|---|---|
| **Categorical** | Symbols in a multi-line chart, sectors in an exposure bar, strategies in a breakdown | Fixed hue order from the design system. Beyond 8 series → fold into "Other," or use small multiples |
| **Sequential** | Score heatmaps, volume intensity, correlation magnitude | Single hue, light→dark |
| **Diverging** | **P&L, relative strength, sentiment scores, gap %** — anything with a meaningful zero | Two hues + neutral grey midpoint. The grey at zero matters: it makes "flat" visually distinct from "slightly positive" |
| **Status** | Service health, position state, compliance checks, order status | Reserved four-value palette, always with an icon and a word |

Note that **P&L is inherently diverging**, not categorical — profit and loss are poles around a meaningful zero. Treating it as two arbitrary colours loses that structure.

### 8.4 Chart inventory

| Chart | Type | Notes |
|---|---|---|
| Price | Candlestick + volume subplot | `lightweight-charts`; `series.update()` for streaming |
| Equity curve | Line, single series | No legend needed — the title names it |
| Intraday P&L | Area, diverging fill around zero | Grey baseline at zero |
| R-multiple distribution | Histogram, diverging | Negative R left, positive right, grey at zero |
| Sector exposure | Horizontal bars, categorical | Sorted by magnitude, limit line marked |
| Score breakdown | Stacked horizontal bar, categorical | 2px surface gap between segments |
| Win rate by setup | Grouped bars | Not a pie chart — comparison of magnitudes |
| Slot utilization | Segmented meter | Status colours |
| Latency | Histogram with p50/p95/p99 markers | Feeds the adaptive interval |

**No pie charts anywhere.** Every use case in this system is a magnitude comparison, which bars do better.

### 8.5 Real-time rendering discipline

Research is emphatic that lag, flickering, or stale visual states during volatile conditions are trust-breaking. Concretely:

- **Incremental updates only** — `series.update()` with the new point, never a full re-render.
- **Throttle to 4 Hz maximum.** Ticks may arrive faster; the human eye gains nothing above ~250ms refresh, and the CPU cost is real.
- **Explicit stale state.** If the feed lags beyond 5 seconds, the chart visibly dims and shows "Data delayed 12s" — never silently display stale data as live.
- **Reconnect is visible.** SSE reconnection shows a brief banner, not a silent gap.
- **Never animate a price change.** Animation implies smooth transition; prices jump.

### 8.6 Dark mode

Design it, don't invert it. Dark mode gets its own steps from the same hue ramps, validated against the dark surface. An automatically inverted light palette fails contrast and CVD checks — validation must be run separately for each mode.

Given that this system is used at 06:00 and 15:30, and dark mode is standard in trading tools, this is worth doing properly rather than as a CSS filter.

---

## 9. Admin & Configuration Interface

The admin UI is where the system's behaviour is defined. It therefore needs to be both usable and *hard to use dangerously*.

### 9.1 The governing principle

**Config is git-tracked YAML; the UI is a validated editor for it, not a database of settings.**

Editing through the UI produces a git commit with a diff and a message. This gives history, review, rollback, and blame for free, and means the running config always matches something reviewable. A settings table in a database gives none of that.

### 9.2 Admin home (`/admin`)

```
┌────────────────────────────────────────────────────────────────────────────┐
│  ADMINISTRATION                                    Config v47 · 2 Aug 2026 │
├────────────────────────────────────────────────────────────────────────────┤
│  ⚠  Configuration changes require a restart and take effect the next       │
│     trading day. Risk limits cannot be modified during an active session.  │
├──────────────────────────┬─────────────────────────────────────────────────┤
│  COMPLIANCE           ●  │  SYSTEM HEALTH                              ●   │
│  ✓ Static IP verified    │  orchestrator   ● up 14d    market-ingest  ● up │
│  ✓ India region          │  ti-engine      ● up 14d    signal-engine  ● up │
│  ✓ Algo-ID configured    │  execution-svc  ● up 14d    macro-svc      ● up │
│  ✓ Rate limit 5/10 OPS   │  api-server     ● up 14d    notifier       ● up │
│  ✓ Session re-auth 07:00 │  redis          ● 412 MB    timescaledb    ● up │
│  ✓ Single recipient      │                                                 │
│  ✓ Audit chain intact    │  Disk 34% · Mem 5.2/16 GB · CPU 12%             │
├──────────────────────────┴─────────────────────────────────────────────────┤
│  CONFIGURATION SECTIONS                                                    │
│  [Universe & Filters]  [Strategies]  [Risk Limits]  [AI Settings]          │
│  [News Sources]  [Autonomy]  [Execution]  [Notifications]  [Tax]          │
└────────────────────────────────────────────────────────────────────────────┘
```

### 9.3 Configuration editor pattern

Every config section follows one layout:

```
┌────────────────────────────────────────────────────────────────────────────┐
│  RISK LIMITS                                        [Revert] [Validate] [Save] │
├────────────────────────────────────────────────────────────────────────────┤
│  CAPITAL & SLOTS                                                           │
│                                                                            │
│  Trading capital              [   500000 ] ₹                               │
│  Position slots               [        5 ]     max 20                      │
│    ⓘ Capital is divided into equal slots; one stock per slot. More slots   │
│      means smaller positions and wider diversification.                    │
│  Capital per slot                  ₹100,000     (computed)                 │
│                                                                            │
│  PER TRADE                                                                 │
│  Risk per trade               [      1.0 ] %   of capital                  │
│    → ₹5,000 risked per trade · ~₹25,000 total at full allocation           │
│  Sizing method                [ ATR-based ▾]                               │
│  ATR stop multiplier          [      1.5 ]                                 │
│  Target R multiple            [      2.0 ]                                 │
│                                                                            │
│  PORTFOLIO                                                                 │
│  Max daily loss               [      3.0 ] %  → halts trading at −₹15,000  │
│  Max sector exposure          [       40 ] %                               │
│  Max correlated positions     [        2 ]                                 │
│  Consecutive loss halt        [        3 ]                                 │
│                                                                            │
│  ┌──────────────────────────────────────────────────────────────────────┐ │
│  │ IMPACT PREVIEW                                                       │ │
│  │ At these settings, backtested over the last 60 sessions:             │ │
│  │   Avg position size  ₹94,200      Trades/day  3.2                    │ │
│  │   Max drawdown       −4.1%        Days halted  2                     │ │
│  │ ⚠ Raising risk per trade to 1.5% would have breached the daily loss  │ │
│  │   limit on 4 additional days.                                        │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────────────────┘
```

**Four patterns worth adopting throughout:**

1. **Derived values shown live.** "1.0%" is abstract; "→ ₹5,000 risked per trade" is not. Every percentage shows its rupee consequence as you type.
2. **Inline explanation, not a separate help page.** The `ⓘ` text explains the *trade-off*, not the definition.
3. **Impact preview.** Where a backtest can answer "what would this setting have done," show it. This is the difference between tuning and guessing.
4. **Warnings for dangerous directions**, phrased as consequences rather than prohibitions.

### 9.4 Validation layers

Configuration passes three gates before it can take effect:

| Gate | Enforces | Behaviour on failure |
|---|---|---|
| **Client-side** | Types, ranges, required fields | Inline error, Save disabled |
| **Server-side (Pydantic)** | Cross-field constraints (weights sum to 1.0; interval floor ≤ ceiling; slots × slot-capital ≤ capital) | Detailed error list |
| **Hard bounds (code)** | Absolute safety limits — `risk_pct ≤ 10`, `slots ≤ 20`, `max_daily_loss ≤ 25`, `orders_per_sec ≤ 5` | **Rejected regardless of what the file says** |

The third gate is the important one and it exists in code, not config: **configuration can tune the system; it can never disable safety.** A compromised or mistaken config file cannot raise the order rate above the SEBI-safe cap or set a 50% per-trade risk.

### 9.5 Section-specific notes

| Section | Key controls | Special handling |
|---|---|---|
| **Universe & Filters** | Base index, hard filters, scoring weights, shortlist size | Weights must sum to 1.0; live preview shows how many symbols survive each filter with today's data — invaluable for diagnosing an empty universe |
| **Strategies** | Registry list, lifecycle states, per-strategy detail, approval queue | Shows DSR, PBO, and **realized-vs-backtest ratio** — the single most important column. Promotion to ACTIVE requires re-authentication *and* typing the strategy name; deliberately more friction than a tap, because this is the gate between the AI's proposals and your money. Full spec: [STRATEGY_ENGINE.md §12](STRATEGY_ENGINE.md) |
| **Trial log** | Read-only | Total trials, pass rate, and the DSR bar those trials imply. Makes the search cost visible — a fast-climbing trial count raises the bar for every future strategy, and that should be uncomfortable to look at |
| **Risk Limits** | §9.3 | Immutable during an active session |
| **AI Settings** | Model per tier, effort, confidence thresholds, token budget | Live spend gauge; changing a model warns that prompt cache will be cold |
| **News Sources** | Provider allowlist, refresh interval, decay half-lives, event weights | **Injection monitor**: count of sanitizer hits by source, with samples. A source with rising hits is a source to drop |
| **Autonomy** | Level, envelope, escalation triggers, demotion rules | Raising the level requires typing the level name to confirm |
| **Notifications** | Channel config, priority budgets, quiet hours | **Enforces single recipient** (L-6). Adding a second requires an explicit family-account compliance acknowledgement |
| **Tax** | Expense register, financial year, report generation | Categorized expense entry; turnover computed live |
| **Compliance** | Read-only status of all constraints | Cannot be edited — it reports, it doesn't configure |

### 9.6 Change control

- Every save produces a git commit with a diff view shown before confirmation.
- The config hash is recorded in each day's plan, so every trade traces to the exact config that produced it.
- A **"what changed since last trading day"** banner appears on the dashboard when config differs from the last session's hash.
- Rollback is a git revert plus restart, exposed as a one-click action on the config history page.

---

## 10. Mobile / Telegram Interface

During market hours, Telegram is the primary interface. It's already on the lock screen, it delivers reliably, it supports inline buttons, and it doesn't require you to open a laptop.

### 10.1 Message design

```
📋 PRE-MARKET PLAN — 4 Aug
Regime: RISK-ON · GIFT +85 · VIX 13.2

Top 3 of 8 candidates:
1. TATAMOT  87  LONG  ORB breakout
2. RELIANCE 82  LONG  Trend pullback
3. SBIN     79  LONG  S/R bounce

Bias: Long-favoured, IT & Auto leading
Invalidation: Nifty < 24,050 or VIX > 16

[ View full plan ]
```

```
🔔 APPROVAL NEEDED — 60s
TATAMOTORS · LONG
Entry ₹712.40 · Stop ₹705.20 · Qty 120
Risk ₹864 (0.17% of capital)

3/3 timeframes agree · AI 0.82
Volume 1.8× · Auto sector leading

[ ✅ Approve ]  [ ❌ Reject ]  [ 📊 Details ]
```

```
✅ CLOSED — TATAMOTORS
+₹1,140 (+1.3R) · target hit 14:52
Held 2h 31m · Day total ▲ +₹4,280
```

### 10.2 Commands

| Command | Action |
|---|---|
| `/status` | One-message summary: P&L, positions, slots, health |
| `/positions` | Open positions with live P&L |
| `/plan` | Today's plan summary |
| `/pause` | Stop new entries; manage existing |
| `/resume` | Resume (requires confirmation) |
| `/kill` | **Kill switch** — two-step confirm |
| `/close SYMBOL` | Close one position (confirm) |
| `/closeall` | Close everything (two-step confirm) |
| `/why SYMBOL` | The AI rationale for the current view |
| `/health` | Service status |

### 10.3 Rules

- **Every destructive command requires confirmation**, and `/kill` and `/closeall` require two steps.
- **Approval requests expire.** On timeout, the trade is skipped — never auto-approved.
- **Rate-limited per §3.4 budgets.** Excess P2 messages batch into a digest.
- **No sensitive data in messages** — no account numbers, no credentials, no absolute capital where a percentage will do. Telegram is a third party.
- **Single recipient enforced** (L-6).

---

## 11. UI Security

The web UI is the highest-value target in the system — it can halt trading and change risk limits. Treated accordingly.

| Control | Implementation |
|---|---|
| **Not internet-facing** | Binds to `127.0.0.1` only. Access via WireGuard or an SSH tunnel. This single decision removes most of the attack surface |
| **Authentication** | WebAuthn passkey primary — phishing-resistant by design, origin-bound, private key never leaves the device. TOTP as a recovery fallback |
| **Session management** | Short-lived tokens with rotation, `HttpOnly` + `Secure` + `SameSite=Strict` cookies, absolute session timeout, server-side revocation |
| **CSRF** | Token on every mutating request; HTMX configured to send it globally |
| **Re-authentication for dangerous actions** | Kill switch, config save, autonomy change, and close-all each require a fresh authentication step regardless of session age |
| **Read-only mode** | A separate credential grants view-only access, for reviewing on a device you trust less |
| **Rate limiting** | Per-endpoint, with aggressive limits on auth and control endpoints |
| **Content Security Policy** | Strict; no inline scripts, no external origins. All assets self-hosted — no CDN |
| **No secrets rendered** | Config editor shows `vault://path/to/secret`, never a value. The API layer refuses to serialize a `SecretString` |
| **Audit** | Every UI action logged with user, timestamp, IP, before/after state |
| **Telegram binding** | Bot responds only to one hard-configured chat ID; every other sender is ignored and logged |

**The most valuable control on this list is the first one.** A dashboard that isn't reachable from the internet cannot be attacked from the internet. Everything else is defence in depth behind that.

---

## 12. MVP Build Plan & Definition of Done

### 12.1 Sequence

Each phase produces something usable. No phase depends on a later one.

| Phase | Weeks | Deliverable | Value at completion |
|---|---|---|---|
| **0. Foundation** | 1–2 | Repo, config schema, DB migrations, secrets, Docker Compose, CI | Runnable skeleton |
| **1. Data** | 3–4 | Broker auth + daily re-auth, WebSocket ingest, cleaning, bar building, historical sync, instrument/hazard fetchers | Clean market data, stored |
| **2. Analysis** | 5–6 | Indicator engine, level detection, hard filters, Tradeability scoring | **Ranked watchlist each morning — reviewable by eye** |
| **3. Plan** | 7–8 | Macro collectors, news pipeline with scoring, pre-market AI synthesis, Telegram briefing | **⭐ A researched daily plan by 09:15. Genuinely useful even if you never automate** |
| **4. Signals** | 9–10 | Strategy plugins, in-session AI review, live dashboard, symbol detail | Live alerts with rationale |
| **5. Execution (paper)** | 11–13 | Risk engine, sizing, slots, order gateway, position manager, reconciler, kill switch | **Full loop on paper capital** |
| **6. Interface** | 14–15 | Admin UI, config editor, audit explorer, journal, tax report | Operable without touching files |
| **7. Hardening** | 16–18 | Chaos tests, security checklist, property tests, runbooks | Ready for real capital |
| **8. Live** | 19+ | L2 approval mode, small size, gradual scale-up | Live, supervised |

**Phase 3 is the inflection point.** From that week onward the system pays for itself as a research assistant, regardless of whether execution is ever automated. Structure the build so that value lands early.

### 12.2 Definition of Done for MVP

The system is MVP-complete when **all** of these hold:

**Functional**
- [ ] Runs a full trading day 05:30 → 16:00 unattended, on paper capital
- [ ] Produces a ranked, reasoned plan before 09:15 every day for 20 consecutive sessions
- [ ] Trades ≥3 symbols concurrently through slot allocation
- [ ] Every position has a protective stop from the moment of fill
- [ ] Every position exits on our schedule, before the broker's per-stock deadline
- [ ] News pipeline produces scores incorporating novelty and decay
- [ ] Complete audit trail: any trade traceable from candidacy to exit

**Safety**
- [ ] All chaos scenarios pass (LLD §12.3)
- [ ] Duplicate-order prevention verified by simulated timeout + reconnect
- [ ] Kill switch tested end-to-end from a phone in under 10 seconds
- [ ] Risk engine property tests pass on all generated inputs
- [ ] System fails closed on every simulated component failure
- [ ] Prompt injection test suite passes

**Compliance**
- [ ] Static IP verified and broker-whitelisted; startup check active
- [ ] Deployed in an India region
- [ ] Algo-ID attached to every order
- [ ] Order rate demonstrably capped at 5/sec under a flood test
- [ ] Daily re-auth working for 20 consecutive sessions
- [ ] Single-recipient notification enforced
- [ ] Tax report generates correct speculative turnover on known test data

**Security**
- [ ] Full security checklist passed (LLD §10.12)
- [ ] `gitleaks` clean over full history
- [ ] Dashboard unreachable from the public internet (verified externally)
- [ ] Passkey auth working; re-auth required for dangerous actions

**Interface**
- [ ] Every MVP screen implemented and usable on the intended device
- [ ] Charts validated for CVD in both light and dark mode
- [ ] No direction encoded by colour alone anywhere
- [ ] Config editable through UI with validation and git history
- [ ] Telegram interface covers status, approval, and kill switch

### 12.3 What "done" explicitly does not mean

MVP-complete does **not** mean ready for meaningful live capital. That requires, additionally: a sustained paper-trading track record across different market regimes (trending, choppy, high-volatility), a positive expectancy demonstrated over a meaningful sample of trades rather than a good week, and a period at L2 approval mode on small live size before L3.

The gap between "the software works" and "the strategy works" is the largest risk in this entire project, and no amount of engineering closes it. Only evidence does.

---

## Appendix A — Sources

UI/UX and dashboards:
- https://lollypop.design/blog/2026/june/trading-app-design/
- https://www.aufaitux.com/blog/dashboard-ui-ux-design-for-investment-data-platform/
- https://www.uxpin.com/studio/blog/dashboard-design-principles/
- https://openwebsolutions.in/blog/high-performance-trading-dashboard-react-websockets/

Charting and frontend:
- https://www.index.dev/skill-vs-skill/tradingview-vs-lightweight-charts-vs-chartjs
- https://tradingview.github.io/lightweight-charts/tutorials/demos/realtime-updates
- https://devtoolswatch.com/en/htmx-vs-react-2026
- https://brenthaskins.com/blog/react-vs-htmx-2026-framework-debate

Authentication:
- https://www.authgear.com/post/authentication-solutions-guide/
- https://devtoollab.com/blog/passkeys-passwordless-auth-guide
- https://supertokens.com/blog/self-hosted-auth-solutions-in-2026

News sentiment methodology:
- https://arxiv.org/html/2607.28496 (Beyond Sentiment: Structured Information Extraction from Financial News)
- https://www.sciencedirect.com/science/article/pii/S0020025526006420
- https://arxiv.org/html/2507.09739v1
- https://link.springer.com/article/10.1007/s42521-025-00162-3

Human-in-the-loop / autonomy:
- https://galileo.ai/blog/human-in-the-loop-agent-oversight
- https://www.digitalapplied.com/blog/human-in-the-loop-escalation-design-ai-agents-2026
- https://www.strata.io/blog/agentic-identity/practicing-the-human-in-the-loop/

Indian taxation:
- https://tax2win.in/guide/intraday-gain-loss-in-itr
- https://1finance.co.in/blog/itr-for-fo-and-intraday-traders-ay-2026-27/
- https://taxsocial.pro/article/fno-intraday-trading-taxation-ay-2026-27-itr-3-audit-44ad
- https://www.industax.com/blog/intraday-trading-tax-india-itr-3-guide
- https://www.kkscapital.com/blog/advance-tax-due-dates-calculation-india/

SEBI regulations:
- https://taxguru.in/sebi/comprehensive-analysis-sebi-s-faqs-research-analysts.html
- https://cskruti.com/when-sebi-research-analyst-regulations-do-not-apply-to-you/
- https://www.quantinsti.com/articles/algorithmic-trading-india/

MVP scoping:
- https://www.biz4group.com/blog/build-trading-platform-mvp

---

*End of document. Regulatory and tax details are researched summaries as of August 2026, not professional advice — confirm SEBI/broker specifics in writing with your broker, and engage a Chartered Accountant before your first filing.*
