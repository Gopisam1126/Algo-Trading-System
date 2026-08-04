# AI-Driven Algorithmic Trading Platform — Research & Architecture Context Document

**Status:** Pre-implementation research. No code written yet.
**Purpose:** Single source of truth for the *why* behind every future architecture/implementation decision. Read this before writing code; update it when decisions change.

> **⚠️ Revision note (2026-08-04, v1.1):** This document was originally written before the project scoped to **Indian markets (NSE/BSE)**. It recommended US brokers (Alpaca, Interactive Brokers), US data providers, and framed regulation around the US PDT rule — all of which were wrong for this project and contradicted the companion documents. Those sections have been corrected. The reasoning content (AI layering, latency analysis, multi-timeframe methodology, confluence) was market-agnostic and stands unchanged. See [VERIFICATION_REPORT.md](VERIFICATION_REPORT.md) for the full audit.
**Document set — read in this order:**
1. **This document** — the *why*: research findings, AI strategy, latency reasoning, general architecture
2. [INDIA_FEATURES_AND_CONFIG.md](INDIA_FEATURES_AND_CONFIG.md) — the *what*: NSE/BSE market rules, SEBI algo compliance, pre-market engine, stock scoring model, feature catalogue, configuration schema
3. [LOW_LEVEL_ARCHITECTURE.md](LOW_LEVEL_ARCHITECTURE.md) — the *how*: service decomposition, database schemas, tech stack decisions, AI integration, security architecture, deployment
4. [MVP_UI_AND_LEGAL.md](MVP_UI_AND_LEGAL.md) — the *scope, screens, and law*: MVP feature list, autonomy model, news scoring engine, UI/admin design, Indian legal & tax framework, build plan
5. [STRATEGY_ENGINE.md](STRATEGY_ENGINE.md) — the strategy lifecycle: DSL, user-authored & AI-generated strategies, the validation gauntlet, overfitting defence
6. [VERIFICATION_REPORT.md](VERIFICATION_REPORT.md) — cross-document audit, findings, and corrections applied
7. [PRE_LIVE_CHECKLIST.md](PRE_LIVE_CHECKLIST.md) — the consolidated gate before real capital
**Last updated:** 2026-08-04
**Author context:** Compiled from web research (Aug 2026) + first-principles reasoning, based on the user's original brief (see "Original Brief" below).

---

## 0. Original Brief (verbatim intent, paraphrased)

Build a platform that:
1. Pulls real-time stock data.
2. Cleans it and computes technical indicators/calculations with low latency.
3. Feeds the processed data to an AI model that analyzes patterns and generates trade decisions/alerts.
4. Executes trades.
5. Continuously monitors world news and macro conditions *before* making decisions, so trades are context-aware, not just chart-aware.
6. Chooses its own decision cadence (e.g., 5-min bars) based on how long its own pipeline actually takes, rather than defaulting to 1-minute bars.
7. Performs multi-timeframe analysis to make better, more confident calls.
8. Should approximate how a professional discretionary trader reasons — but scaled across many tickers simultaneously.

This document researches each of these pieces and proposes a concrete architecture. **No implementation yet** — this is the reference doc the build will follow.

---

## 1. Guiding Philosophy (read this first)

A few conclusions came out of research that should shape every downstream decision:

1. **This is not HFT, and it shouldn't try to be.** True high-frequency trading operates in microseconds/nanoseconds and requires exchange colocation, FPGAs, and kernel-bypass networking — a different industry with different economics (firms spend millions on physical proximity to exchanges). A cloud-based, AI-reasoning system is structurally incapable of competing there, and shouldn't try. Instead, this platform competes on **analysis quality and breadth** (many tickers, multiple timeframes, news context) at a **decision cadence measured in seconds-to-minutes**, not microseconds. This is actually a *more* defensible retail edge than trying to out-speed HFT firms.
2. **The user's instinct to derive the trading interval from actual pipeline latency, rather than assuming 1-minute, is correct and is a form of a well-known pattern: never let your decision loop run faster than your decision loop actually completes.** This document formalizes it in §8 as a *closed-loop-latency-aware scheduler*.
3. **LLMs should not do arithmetic that matters, and should not be in the hard-real-time hot path.** Every credible source on LLM-in-trading agrees: use LLMs for *reasoning, synthesis, and language-shaped tasks* (news interpretation, regime classification, explaining "why," weighing conflicting signals) — not for computing RSI or deciding exact position size. Keep numeric truth in deterministic code; let the AI reason over the *outputs* of that code.
4. **A hybrid architecture beats a pure-LLM or pure-quant approach.** Research (both academic and industry) converges on: fast deterministic quant layer for signal generation → LLM/agent layer for contextual reasoning, cross-timeframe synthesis, and news/regime interpretation → deterministic risk/execution layer that the AI cannot bypass. This is elaborated in §6.
5. **Realistic expectations.** No credible source claims LLMs alone produce reliable alpha; realistic uplift from adding AI reasoning on top of a solid quant baseline is described in industry writeups as single-digit percentage improvement over baseline, not "80% win rate" marketing claims. Treat the AI layer as a *decision-support and pattern-synthesis engine*, not an oracle.

---

## 2. High-Level Architecture

```
                         ┌─────────────────────────────────────────────┐
                         │            MACRO / NEWS LAYER                │
                         │  (runs on its own slow cadence, ~15-60 min)  │
                         │  - News APIs, sentiment, economic calendar   │
                         │  - Market regime classifier (risk-on/off)    │
                         │  - Produces a "Market Condition Context"     │
                         │    object consumed by every other layer      │
                         └───────────────────┬───────────────────────────┘
                                             │ context injected
   ┌─────────────┐    ┌──────────────┐    ┌─▼──────────────┐    ┌───────────────┐    ┌──────────────┐
   │ DATA INGEST │───▶│ DATA CLEAN / │───▶│ QUANT / TI      │───▶│ AI REASONING   │───▶│ RISK GATE +   │
   │ (WebSocket  │    │ NORMALIZE    │    │ ENGINE          │    │ LAYER          │    │ EXECUTION     │
   │ streams,    │    │ (dedupe,     │    │ (indicators,    │    │ (LLM agent(s), │    │ (deterministic│
   │ multi-      │    │ gap-fill,    │    │ multi-timeframe │    │ pattern synth, │    │ position sizing,│
   │ ticker)     │    │ adjust splits)│   │ bar aggregation,│    │ confidence      │    │ stop-loss,    │
   │             │    │              │    │ streaming/      │    │ scoring,        │    │ kill-switch,  │
   │             │    │              │    │ incremental)    │    │ trade rationale)│    │ broker API)   │
   └─────────────┘    └──────────────┘    └─────────────────┘    └────────────────┘    └───────┬───────┘
                                                                                                  │
                                                                                          ┌────────────────────┐
                                                                                          │   BROKER / EXEC     │
                                                                                          │ (Angel One / Fyers  │
                                                                                          │  / Zerodha — India) │
                                                                                          └───────┬────────────┘
                                                                                                  │
                                                                                          ┌───────▼────────┐
                                                                                          │ MONITORING /    │
                                                                                          │ LOGGING /       │
                                                                                          │ BACKTEST FEEDBACK│
                                                                                          └────────────────┘
```

**Key architectural principle: three independent loop speeds, not one.**

| Loop | Cadence | What runs in it |
|---|---|---|
| **Fast/Market loop** | Seconds to the platform's derived interval (see §8) | Data ingest → clean → TI calc → AI reasoning → trade decision → execution |
| **Macro/News loop** | 15–60 min (news moves slower than price) | News ingestion, sentiment scoring, economic calendar checks, regime classification |
| **Slow/Batch loop** | Daily / pre-market | Universe selection, model recalibration, risk-limit resets, EOD reconciliation, strategy re-validation and retraining (see [STRATEGY_ENGINE.md](STRATEGY_ENGINE.md)) |

The macro loop's output is a cached **"Market Condition Context"** object (e.g., `{regime: "risk-off", vix_level: 24.3, macro_events_today: [...], sector_sentiment: {...}}`) that the fast loop reads on every cycle without waiting on it — decoupling a slow, language-heavy process from the time-sensitive trading loop. This directly answers the brief's requirement to "understand market condition beforehand" without letting news-fetch latency blow the trading loop's budget.

---

## 3. Data Ingestion Layer

### 3.1 Real-time market data provider comparison — **India (NSE/BSE)**

> **Corrected in v1.1.** This section originally listed US providers (Alpaca, Polygon.io, Databento, IEX Cloud, Finnhub). None of them serve Indian equities. The India-specific comparison is maintained in full in [INDIA_FEATURES_AND_CONFIG.md §3](INDIA_FEATURES_AND_CONFIG.md) — summarized here for continuity.

In India, market data and execution come bundled through the **broker's API**; there is no equivalent of the independent US data-vendor market at retail price points.

| Provider | Order rate | Data cost | Notes |
|---|---|---|---|
| **Angel One SmartAPI** | ~10 orders/sec | Free | Reliable WebSocket, practical rate limits. Strong default. |
| **Fyers API** | — | Free | Free minute-level history (~1–2 years) — removes a real cost barrier for the pre-market historical analysis this system depends on. |
| **Zerodha Kite Connect** ⭐ | **10 orders/sec** account-wide (429 above) | ~₹500/mo (data APIs; order placement reported free) | **SELECTED.** Most mature ecosystem and documentation. Browser-redirect daily auth; static IP applies to order endpoints only. |
| **DhanHQ** | — | ₹0 orders / ₹499 data | Native TradingView integration. |
| **Upstox API v2** | — | Free | Execution-focused; 40–80 ms order round-trips reported. |
| **NSE Bhavcopy** | n/a | Free | Daily EOD; the basis of the long-run local archive. |

**Decision (v1.2): Zerodha Kite Connect is the primary broker**, with **Fyers** as secondary for data redundancy and free intraday history. WebSocket stability at the open and on expiry days is a known weak point across Indian broker APIs, so a second connection is cheap insurance.

> **Corrected in v1.2.** An earlier revision recorded Zerodha at ~3 orders/sec from a secondary source and recommended Angel One partly on that basis. Zerodha staff state on the Kite Connect developer forum that **10 OPS is enforced account-wide** with HTTP 429 above it — matching SEBI's threshold rather than being a third of it. Verified operational detail is maintained in code at `Code/src/algotrader/broker/profiles.py`, which is the authoritative copy.

### 3.2 REST vs. WebSocket
- **WebSocket is mandatory for the fast loop.** Polling via REST introduces avoidable per-request latency and rate-limit pressure; WebSocket pushes trades/quotes/bar-updates as they occur. Every credible 2026 source treats WebSocket streaming as baseline, not optional, for anything faster than end-of-day.
- REST is fine for: historical backtesting pulls, reference data (splits/dividends), and the slow/macro loop.

### 3.3 Multi-ticker considerations
- A single WebSocket connection can typically subscribe to many symbols; the ingestion service should maintain **one connection per provider**, fan out to an internal message bus (see §12), and let downstream consumers subscribe per-ticker. Avoid one-connection-per-ticker — it doesn't scale and providers rate-limit connection counts.

---

## 4. Data Cleaning & Normalization Layer

Real-time feeds are not analysis-ready. This layer must, before anything touches an indicator calculation:

1. **De-duplicate** — WebSocket reconnects/replays can double-deliver ticks.
2. **Gap detection & handling** — flag/interpolate or explicitly mark missing bars (e.g., after a reconnect) rather than silently computing indicators across a gap.
3. **Corporate action adjustment** — splits/dividends must adjust historical series consistently, or indicators (moving averages especially) will show false breakouts/gaps.
4. **Outlier/bad-tick filtering** — exchanges occasionally publish erroneous prints; a simple bounds/z-score filter against recent volatility prevents one bad tick from corrupting an indicator or triggering a false signal.
5. **Timestamp normalization** — all sources into a single timezone/format (UTC recommended internally, convert to exchange-local only for display/market-hours logic).
6. **Bar construction** — raw trade/quote ticks need to be aggregated into OHLCV bars at each timeframe the system uses (see §9). This should be done incrementally/in a streaming fashion, not by re-slicing history every cycle (see §5.1).

This layer should be a clearly separated stage (not inlined into ingestion or the indicator engine) so it can be unit-tested against known-bad data independently.

---

## 5. Quant / Technical Indicator Engine (Low-Latency Core)

### 5.1 Streaming/incremental computation, not batch recomputation
The single biggest low-latency win available to a system like this: **compute indicators incrementally** (update the existing EMA/RSI/etc. with the new tick) instead of recomputing over the whole lookback window every cycle. Research surfaced purpose-built libraries for exactly this:

| Library | Language | Notes |
|---|---|---|
| **TA-Lib** | C/C++ core, Python/Rust/Java bindings | 200 indicators, has a genuine streaming/incremental API — the industry-standard baseline. |
| **pandas-ta** | Python | 150+ indicators, easy prototyping; batch-oriented (recomputes over a DataFrame) — fine for backtesting/research, not ideal for the hot path at scale. |
| **streaming-indicators** | Python | Purpose-built for tick-by-tick streaming updates rather than static data. |
| **RTTA** | Python (optimized) | Benchmarked at ~36ns per EMA update vs. ~465ns for a comparable incremental library — approaching bare-function-call speed. Worth evaluating if Python-level latency becomes a bottleneck. |
| **ta-rs / rsta** | Rust | For if/when a hot-path component needs to move out of Python entirely; validated against pandas-ta output for correctness. |

**Recommendation:** Prototype in Python with `pandas-ta`/TA-Lib for correctness and speed-of-development; if profiling later shows the indicator layer (not the AI call, which will dominate — see §13) is the bottleneck, port just that hot path to a Rust or TA-Lib streaming implementation. Don't pre-optimize this before measuring — see §13's latency budget, where the AI call will almost certainly dwarf indicator computation time regardless of language.

### 5.2 What this layer computes
Standard TI set (trend, momentum, volatility, volume) computed **per timeframe** the system tracks (see §9): moving averages (SMA/EMA), MACD, RSI, Bollinger Bands, ATR (volatility, useful for stop-loss sizing), VWAP, volume profile/anomalies, support/resistance or pivot levels. This is deterministic, well-understood code — no AI involved at this stage. The output is a structured feature set per ticker per timeframe, which becomes the *input* to the AI reasoning layer, not raw ticks.

---

## 6. AI Reasoning Layer — "Which AI is best for this?"

This was the most heavily researched question. Findings:

### 6.1 There is no single "best AI" — different techniques solve different sub-problems
Academic and industry sources consistently describe **two largely separate threads** that need to be combined, not one model that does everything:
- **Supervised/forecasting models** (gradient-boosted trees, LSTM/GRU, Temporal Fusion Transformers) — good at predicting short-horizon price movement or classifying setups from numeric features. Fast, cheap, deterministic-ish, but not language-aware and can't reason about news or explain themselves.
- **Reinforcement learning** (DQN, PPO, SAC) — a natural fit for *sequential decision-making under changing conditions* (when to enter/exit/size), because trading genuinely is a sequential decision problem. Harder to train reliably, needs a good simulator/backtest environment, and is opaque (no rationale).
- **LLMs / language-reasoning agents** (Claude, GPT, etc.) — excel at synthesizing *unstructured, multi-modal context* (news, macro commentary, cross-timeframe technical narratives) into a human-readable rationale, and at reasoning over the *outputs* of the above two rather than raw ticks. Weak/risky at precise numeric computation and inconsistent run-to-run (asking the same question twice can yield different answers) — a real liability for anything touching position sizing or exact stop levels.

Emerging research (2025–2026) explicitly combines these: LLM-guided RL (LLM sets high-level strategy/guidance, RL agent executes tactically), and multi-agent LLM frameworks that assign specialized agents to Indicator/Pattern/Trend/Risk/News roles and let a coordinating agent synthesize their outputs (e.g., the published "TradingAgents" and "QuantAgent" multi-agent frameworks) — reported (in early research, not production-proven) annualized returns and Sharpe ratios well above buy-and-hold, though these are backtest results and should be treated skeptically until validated on this system's own instruments and time period.

### 6.2 Concrete recommendation for this platform
**Hybrid, tiered architecture:**

1. **Tier 1 — Deterministic quant layer** (§5): computes indicators, and optionally a lightweight ML classifier (gradient-boosted trees are a reasonable, well-understood starting point) that outputs a numeric "setup score" per ticker/timeframe. Cheap, fast, explainable, runs every fast-loop cycle for every ticker.
2. **Tier 2 — AI reasoning layer (LLM agent)**: only invoked for tickers that clear a Tier-1 threshold (don't burn AI-call latency/cost on every ticker every cycle — see §8/§13). Given: the structured TI features across all tracked timeframes, the cached Market Condition Context from the news/macro loop, and (optionally) recent price-action described numerically. Asked to: synthesize a directional read, flag confluence/divergence across timeframes, produce a confidence score and a plain-language rationale. **Never asked to compute numbers itself** — it consumes numbers the quant layer already computed and reasons about *what they mean together*, per the industry consensus in §6.1 and §1.3.
3. **Tier 3 — Deterministic risk/execution gate** (§10): the AI's output is a *recommendation with confidence*, not an order. A separate, non-AI, testable module applies position sizing, stop-loss placement, max-exposure and drawdown limits, and only then sends the order. This is the single most repeated piece of advice in the research: keep the LLM out of the arithmetic and out of final authority over money movement.

### 6.3 Which specific model
- Research comparing frontier LLMs for trading tasks found: strong reasoning/macro-synthesis from Claude and GPT-class models, with tradeoffs in speed, cost, and each vendor's safety guardrails (some models will refuse to output a bare "BUY/SELL" instruction framed as financial advice — the system's prompt design should frame the request as "analysis and rationale for a system that a human/deterministic layer will act on," not "tell me what to do with my money," which both sidesteps the guardrail issue honestly and matches the correct architecture in §6.2 anyway).
- Given this document is itself being produced inside Claude Code, and Anthropic has a dedicated **Claude for Financial Services** offering (agent templates, connectors, and finance-tuned skills for exactly this domain, launched 2026) plus documented strong performance on financial-agent benchmarks, **Claude is a reasonable default for the reasoning layer**, with the current model family (as of Aug 2026) offering a natural cost/quality tier split worth using deliberately rather than picking one model for everything:
  - **Cheaper/faster model** (e.g., a Haiku-class model) for high-frequency, low-stakes sub-tasks: news headline triage, sentiment scoring, simple classification.
  - **Mid-tier model** (Sonnet-class) for the per-ticker Tier-2 reasoning pass on tickers that clear the Tier-1 filter — the main workhorse of the fast loop.
  - **Top-tier/deep reasoning model** (Opus/Fable-class) reserved for the slow loop: end-of-day/pre-market full market-condition synthesis, strategy review, or cases where Tier-2 confidence is borderline and a second, more careful opinion is worth the extra latency/cost.
  - This tiering is a cost-and-latency architecture decision as much as a quality one — see §13.
- This is a fast-moving space; **re-verify current model lineup, pricing, and any finance-specific offerings at implementation time** rather than trusting this document's snapshot indefinitely.

### 6.4 What NOT to do
- Don't let the LLM output an order size or exact stop price directly into the execution path.
- Don't call the LLM on every ticker on every fast-loop tick if it can be avoided — it will dominate the latency budget and cost (§8, §13).
- Don't trust a single LLM call's consistency for anything binary/irreversible; where feasible, treat low-confidence or borderline outputs as "no trade" rather than forcing a decision.
- Don't treat backtested multi-agent-LLM paper results (e.g., the 53%+ annualized return figures surfacing in recent papers) as a "real edge" until independently validated on this platform's own tickers/timeframes/costs. Papers are proof-of-concept, not a production guarantee.

---

## 7. News & Macro Market Condition Monitoring Layer

Addresses the brief's requirement: "continuously monitor world news and should understand the market condition beforehand."

### 7.1 Data sources (researched)
| Provider | Notes |
|---|---|
| **Alpha Vantage News & Sentiment** | Real-time + historical news with built-in sentiment for equities/forex/crypto. |
| **Finnhub** | Normalized sentiment score (-1 to +1) per article, ticker-tagged. |
| **Marketaux** | Sentiment with confidence intervals, multilingual. |
| **Financial Modeling Prep (FMP) News API** | Structured, machine-readable news from trusted publishers, good base for a custom NLP sentiment layer. |
| **EODHD / APITube / NewsAPI.ai** | Entity extraction (ticker/company/person linking), useful for routing a headline to the right symbols automatically. |

**Important caveat surfaced in research:** news API latency typically ranges from minutes to hours — these are *not* suitable for millisecond-sensitive decisions, which is exactly why this layer belongs in its own slow loop (§2), feeding a cached context object rather than being polled inline in the fast loop.

### 7.2 What this layer should produce
- **Per-ticker sentiment score** from recent headlines (feeds into Tier-2 AI reasoning as context).
- **Macro regime signal** — e.g., a simple risk-on/risk-off classifier using VIX level, major index trend, and yield-curve/rate context, refreshed on the macro cadence.
- **Economic calendar awareness** — flag scheduled high-impact events (FOMC meetings, CPI/jobs reports, earnings for tracked tickers) so the system can widen stops, reduce size, or stand down entirely around known volatility events — something a professional discretionary trader does instinctively and this system should replicate explicitly.
- All of the above collapse into the single **Market Condition Context** object referenced in §2, timestamped and cached, read (not recomputed) by every fast-loop cycle.

---

## 8. Adaptive Decision-Interval Scheduling (the brief's core insight, formalized)

This is the user's own proposal and it deserves to be treated as a first-class design principle, not an afterthought.

### 8.1 The principle
A system's trading interval should never be shorter than the time its own pipeline actually needs to go from "new data available" to "decision made." If the full pipeline (fetch → clean → TI calc → AI reasoning → risk check) reliably takes ~5 minutes end-to-end including a fresh read of market condition, a 1-minute bar strategy is not just suboptimal — it's structurally incoherent, because the system would still be reasoning about bar N when bars N+1 through N+4 have already closed. This matches general low-latency-systems wisdom (§ research on tick-to-trade): the decision loop's own latency defines the fastest timeframe it can honestly operate on.

### 8.2 Proposed mechanism: latency-aware interval selection
1. **Instrument every stage** of the fast loop (ingest→clean, clean→TI, TI→AI call, AI→risk gate, risk gate→order) and record p50/p95/p99 latency continuously in production, not just once at design time — network and API latency vary.
2. **Define the system's effective decision latency** as a rolling p95 (not average — a scheduler should be resilient to the slow cases, not just the typical case) of the full pipeline.
3. **Round up to the nearest supported bar interval** (e.g., 1m/5m/15m/1h — standard bar sizes, not an arbitrary custom number, so the TI calculations and any charting stay meaningful) that comfortably exceeds that p95 latency with margin (e.g., at least 1.5–2x headroom, so a transient slow cycle doesn't cause the system to act on stale/incomplete data).
4. **Make this per-ticket-batch, not global, if needed** — if the system is analyzing 50 tickers, the achievable cadence depends on how many can be processed within the AI-call budget per cycle (see §13); it may be more honest to run a smaller "priority watchlist" on a faster interval and a broader universe on a slower one, rather than pretending one global interval fits both.
5. **Re-evaluate periodically** (e.g., daily), because API latencies, model latencies, and ticker-count all change over time — this shouldn't be a constant hardcoded once and forgotten.

### 8.3 Why this beats a fixed interval
- Avoids the common retail-algo mistake of chasing 1-minute bars with a pipeline that can't honestly analyze that fast, which produces decisions based on stale/incomplete reasoning.
- Gives a principled, defensible answer to "why 5 minutes and not 1 or 15" — it's derived from measured system behavior, not a guess.
- Naturally scales down to a faster interval automatically if the pipeline is optimized later (cheaper/faster AI model, better code) — the system's cadence improves itself as engineering improves, without a manual reconfiguration.

---

## 9. Multi-Timeframe Analysis (MTFA)

Addresses the brief's requirement to "conduct analysis based on different timeframes and make a better prediction."

### 9.1 The professional pattern (confirmed by research)
Professional discretionary traders use a **top-down approach**: start from a higher timeframe to establish the dominant trend/context, then move to progressively lower timeframes to refine entry/exit timing. A common, well-validated combination is roughly **Daily → 4h/1h → entry timeframe**, adapted here to: **Daily/Weekly (macro trend context) → Hourly (intermediate structure) → the system's derived fast-loop interval from §8 (entry/exit timing)**.

### 9.2 Confluence as the core signal-quality mechanism
The key finding: **the highest-probability setups are where multiple timeframes agree ("confluence")** — e.g., daily uptrend + hourly pullback to support + the fast-interval bar showing a reversal signal, all pointing the same direction. Disagreement across timeframes (e.g., daily downtrend but fast-interval bar looks bullish) should reduce the AI layer's confidence score or veto the trade outright, rather than being averaged away silently. This should be an explicit, inspectable field in the AI reasoning output ("timeframe agreement: 3/3" or "conflict: daily bearish vs. entry-frame bullish — reduced confidence"), not a hidden internal weighting — both for debuggability and because it mirrors exactly how a human professional would explain a trade.

### 9.3 Implementation implication
The TI engine (§5) must maintain **separate incrementally-updated indicator states per timeframe per ticker** (not just one), and the AI reasoning layer's input schema must include all relevant timeframes' feature sets side by side so the model can reason about agreement/conflict explicitly, rather than being fed a single flattened feature vector that hides the multi-timeframe structure.

---

## 10. Trade Decision & Risk/Execution Layer

This is deliberately **non-AI, deterministic, and the final authority** — per the repeated research finding that LLM output variability is a real liability in a "zero-tolerance" execution context.

### 10.1 Responsibilities
- **Position sizing**: percentage-of-capital, volatility-adjusted (e.g., ATR-based, from §5.2), or a defined model like fractional Kelly — chosen deliberately, not left to the AI to suggest a dollar amount.
- **Stop-loss placement**: technical (below/above a structural level) and/or volatility-based (ATR multiple); always attached automatically, never optional.
- **Portfolio-level risk limits**: max concurrent positions, max sector/correlation exposure, max daily drawdown — with a **kill switch** that halts new trades (not necessarily open positions) if breached.
- **Regulatory awareness (India)**: SEBI's **peak margin rules** require full SPAN + Exposure margin at all times, not just end-of-day, with clearing corporations taking four random intraday snapshots. Retail intraday leverage is heavily curtailed as a result. The risk layer must therefore size positions against **real available margin fetched from the broker's API**, never against an assumed leverage multiple. Per-stock square-off deadlines (15:10 CAS / 15:20 non-CAS / 15:25 F&O) are also a risk-layer concern, not just an execution detail — see [INDIA_FEATURES_AND_CONFIG.md §2](INDIA_FEATURES_AND_CONFIG.md).
- **Audit trail**: every decision (AI rationale + confidence + risk-gate outcome + final order or rejection reason) logged immutably — necessary both for debugging and for building trust in what the AI layer is actually doing.

### 10.2 Broker/execution API — **India**

> **Corrected in v1.1.** Originally recommended Alpaca and Interactive Brokers. Alpaca does not serve Indian markets; IBKR does but is not the pragmatic retail choice here. See §3.1 above and [INDIA_FEATURES_AND_CONFIG.md §3](INDIA_FEATURES_AND_CONFIG.md).

Execution runs through the same Indian broker API that supplies data (§3.1) — **Angel One SmartAPI** as the default, **Fyers** as fallback. Under SEBI's framework the broker is the *principal* and is legally responsible for every algo on its platform, so the broker's own API terms matter as much as SEBI's rules.

**No live capital should be risked until the system has been validated in paper trading over a meaningful sample of trades across different market regimes.** Indian broker sandboxes vary in fidelity — verify that yours reflects realistic rejections, partial fills, and margin behaviour rather than accepting everything.

---

## 11. Backtesting & Validation

Not explicitly requested in the brief but necessary before any live capital is involved — the platform's AI-reasoning outputs need a way to be validated against history before being trusted live.

Off-the-shelf frameworks were surveyed — **Backtrader** (pure Python, easy start, slow on minute data), **Zipline/zipline-reloaded** (factor research), **QuantConnect LEAN** (end-to-end platform), **PyBroker** (walk-forward discipline for ML strategies).

> **Decision (v1.1):** none of these is adopted as the primary harness. [LOW_LEVEL_ARCHITECTURE.md §17 D6](LOW_LEVEL_ARCHITECTURE.md) selects a **custom replay harness** that executes the *production* strategy code against recorded historical snapshots. The reason is decisive: strategies in this system are pure functions over a `MultiTimeframeSnapshot` (LLD §5.6), so replaying real snapshots tests the exact code that will trade. A third-party framework would require re-implementing every strategy in its own idiom, and the reimplementation is where backtest/live divergence hides. PyBroker remains a useful reference for walk-forward *methodology*, which the Strategy Engine adopts — see [STRATEGY_ENGINE.md §5](STRATEGY_ENGINE.md).

**Critical for this platform specifically:** because the AI reasoning layer is *non-deterministic* (LLM outputs vary run-to-run) and *not free* (API cost per call), a standard price-only backtest isn't sufficient — the validation harness caches and replays historical LLM outputs keyed by prompt hash rather than re-calling the API on every run. This makes backtests deterministic and free, and confines live API calls to periodic validation. Designed deliberately, not discovered by surprise API bills.

---

## 12. Infrastructure & Tech Stack

Reflects the researched 2026 patterns for real-time trading systems, right-sized for a cloud-based (not colocated) system:

- **Message bus**: Kafka (durable, replayable log) and Redis (sub-ms fanout) both showed up repeatedly in the research. **Decision (v1.1): Redis Streams only — Kafka is rejected.** Benchmarks put Redis Streams at ~0.8 ms p99 end-to-end versus Kafka's ~12.5 ms, and Kafka's durability-at-massive-scale solves problems a system producing a few thousand messages per minute does not have, at real operational cost. Redis Streams (not plain Pub/Sub) provide consumer groups, acknowledgement, and replay-after-crash — which is the durability property that actually mattered. Full reasoning: [LOW_LEVEL_ARCHITECTURE.md §3.1](LOW_LEVEL_ARCHITECTURE.md).
- **In-memory store**: Redis (or similar) for the current indicator state per ticker/timeframe (§5.1's incremental state needs to live somewhere fast, not be recomputed from a database every cycle).
- **Time-series database**: TimescaleDB (Postgres-based, easier ops) or InfluxDB for historical bar storage, backtesting data, and audit logs.
- **Orchestration language**: Python is the pragmatic default for the whole system given the ecosystem (data libs, broker SDKs, AI SDKs) — reserve a lower-level language (Rust, per §5.1) only for a proven hot-path bottleneck, not preemptively.
- **Deployment**: containerized (Docker) services per layer in §2, so the fast loop, macro loop, and slow loop can scale/fail independently — a stalled news-API call should never be able to block the trading loop, which argues structurally for separate processes/containers, not just separate code paths in one process.
- **Cloud region**: choose a region close to the broker/data provider's infrastructure to minimize network latency, even though this system isn't chasing HFT-grade latency — every non-essential millisecond still adds up across many API round-trips per cycle.

---

## 13. Latency Budget (rough expectations to design against)

Approximate, to be replaced with real measurements once built — but useful for sizing expectations before writing code:

| Stage | Rough latency (cloud-based, non-colocated) |
|---|---|
| WebSocket tick delivery | ~10–100ms (provider + network dependent) |
| Data cleaning/normalization | ~1–10ms per ticker (in-memory) |
| Incremental TI calculation | ~microseconds–low ms per ticker if done incrementally (§5.1); much worse if naively recomputed over a full window every cycle — this is the single biggest avoidable self-inflicted latency mistake |
| **LLM/AI reasoning call** | **~1–10+ seconds per call**, depending on model tier and prompt/context size — **this will almost certainly be the dominant term in the latency budget**, not the data or TI stages |
| News/macro fetch | Minutes (by nature — this is why it's decoupled into its own loop, §2/§7.1) |
| Risk gate + order submission | ~10–200ms (broker API dependent) |

**Implication:** the AI call is the real bottleneck, not the data pipeline — which directly validates the brief's premise that the achievable interval is meaningfully longer than 1 minute once AI reasoning is in the loop, especially across multiple tickers where AI calls may need to be batched or rate-limited. This is also the strongest argument for the Tier-1/Tier-2 filtering in §6.2 (don't call the AI on every ticker every cycle) and for the tiered model selection in §6.3 (use the fastest adequate model for the routine pass).

---

## 14. Regulatory & Compliance Notes

> **Corrected in v1.1.** This section originally described the US **FINRA Pattern Day Trader rule**, which has no application to Indian markets. The governing regime is **SEBI**. Full treatment: [INDIA_FEATURES_AND_CONFIG.md §1](INDIA_FEATURES_AND_CONFIG.md) (algo framework) and [MVP_UI_AND_LEGAL.md §2](MVP_UI_AND_LEGAL.md) (legality, advisory boundary, taxation).

- **SEBI's retail algo framework** has been fully mandatory since **1 April 2026**. Self-developed algos trading your own capital (and immediate family's, with permission) are permitted **without registering the algorithm**, provided the order rate stays below **10 orders/second per segment**.
- Four architectural constraints follow directly from it: **static broker-whitelisted IP**, **India-hosted servers**, **OAuth + 2FA with daily session logout before pre-open**, and an exchange-assigned **Algo-ID on every order**. These are not paperwork — they shape deployment (LLD §13).
- Broker **terms of service** matter as much as SEBI's rules, because the broker is the legal *principal* for every algo on its platform. Review them before finalizing the loop cadence.
- **The advisory boundary is the line to watch.** Acting on your own signals is personal use. Publishing, selling, or sharing those signals — even free, even to friends — can trigger Research Analyst regulations. The system enforces single-recipient notifications structurally to prevent accidental drift (MVP doc §2.3).
- **Taxation is India-specific and shapes a feature**: intraday equity is *speculative business income* filed on ITR-3, with turnover defined as the absolute sum of profits and losses. See MVP doc §2.4.

---

## 15. Realistic Expectations & Risks

- **AI predictions are directional signals, not guarantees** — treat Tier-2 output as one weighted input to the risk-gated decision, never as ground truth.
- **LLM run-to-run inconsistency is real** — design the system to be comfortable defaulting to "no trade" on low-confidence or conflicting output rather than forcing a decision every cycle.
- **Backtested multi-agent LLM results in recent papers (§6.1) are early-stage research, not production-proven** — validate independently before trusting.
- **News API latency (minutes-to-hours) means the system is reacting to news, not front-running it** — that's fine and expected; it should not be architected as if it can react to breaking news in seconds.
- **Cost management**: AI API calls, premium market data, and news API subscriptions all have real recurring cost that scales with ticker count and call frequency — the Tier-1 filtering (§6.2) and model tiering (§6.3) aren't just latency optimizations, they're the main cost-control lever, and should be budgeted for explicitly before scaling ticker count.

---

## 16. Open Decisions (need input before implementation)

These are choices this document intentionally left open because they're the user's call, not a research question:

1. **Universe size/scope** — how many tickers, and which (a fixed watchlist vs. a dynamically screened universe)? This materially affects the achievable interval (§8) and cost (§13/§15). *(Now specified: Nifty 200 default — INDIA_FEATURES_AND_CONFIG.md §5.1.)*
2. **Capital/account scale and broker choice** — Angel One alone, or Angel One + Fyers for redundancy from day one? *(Corrected in v1.1 — originally read "Alpaca alone, or Alpaca + IBKR".)*
3. **Risk tolerance parameters** — max position size, max daily drawdown, per-trade risk % — needed to build §10 concretely.
4. **How much human-in-the-loop is wanted initially** — should the system only *alert* a human to act on (matching the brief's "lets user know" phrasing literally) before any version is trusted to auto-execute? Strongly recommended as the first milestone regardless, given §15.
5. **Budget ceiling** for data/news/AI API costs, which determines model tier defaults (§6.3) and ticker-count ceiling.

---

## 17. Phased Roadmap

> **Superseded in v1.1.** This document originally carried its own Phase 0–6 numbering, which conflicted with two other schemes in the companion documents (three incompatible phase numberings across four documents — flagged as V8 in the verification report). **The single authoritative build plan is now [MVP_UI_AND_LEGAL.md §12.1](MVP_UI_AND_LEGAL.md)** — Phases 0–8 with week estimates and a Definition of Done. [INDIA_FEATURES_AND_CONFIG.md §10](INDIA_FEATURES_AND_CONFIG.md) mirrors it.

The sequencing principle that this section originally established still holds and is worth restating, because it drives the whole plan: **build the quant layer first and measure its real latency before adding AI; run alert-only before paper; run paper before approval mode; run approval mode before autonomy.** Each stage validates a different failure mode, and skipping one means discovering its failures with real money.

The inflection point is **Phase 3** — pre-market AI synthesis delivering a researched daily plan by 09:15. From that week onward the system is useful as a research assistant regardless of whether execution is ever automated.

---

## 18. Glossary

- **TI** — Technical Indicator (RSI, MACD, moving averages, etc.)
- **MTFA** — Multi-Timeframe Analysis
- **Tick-to-trade** — latency from a market data event to an order being sent
- **Confluence** — agreement of signals across multiple timeframes/indicators
- **Regime** — the broad market condition (risk-on/risk-off, trending/choppy, high/low volatility)
- **Kill switch** — an automated hard-stop that halts new trading activity when a risk limit is breached
- **ASM / GSM** — Additional / Graded Surveillance Measure; NSE/BSE watchlists carrying punitive margins
- **T2T** — Trade-to-Trade segment; compulsory delivery, intraday prohibited
- **CAS** — Closing Auction Session (NSE, live since 03 Aug 2026); changes square-off timing per stock
- **MWPL** — Market-Wide Position Limit; breaching 95% triggers an F&O ban period
- **OPS** — Orders Per Second; SEBI's algo-registration threshold is 10 per segment
- **DSR** — Deflated Sharpe Ratio; corrects a backtest's Sharpe for the number of trials attempted ([STRATEGY_ENGINE.md](STRATEGY_ENGINE.md))
- **PBO** — Probability of Backtest Overfitting

*(v1.1: the **PDT** — Pattern Day Trader — entry was removed. It is a US FINRA rule with no application to Indian markets.)*

---

## 19. Sources (Aug 2026 web research)

Market data APIs:
- https://aifinhub.io/articles/market-data-apis-compared-2026/
- https://bullalert.ai/blog/best-stock-market-data-apis-2026/
- https://qveris.ai/guides/market-data-api-for-ai-agents/
- https://www.alphanume.com/blog/best-market-data-apis-for-algorithmic-trading-in-2026

System architecture / latency:
- https://openwebsolutions.in/blog/real-time-trading-platform-architecture-ai-event-streaming/
- https://www.pyquantnews.com/free-python-resources/event-driven-architecture-in-python-for-trading
- https://www.tuvoc.com/blog/low-latency-trading-systems-guide/
- https://ashutoshkumars1ngh.medium.com/how-to-design-a-real-time-stock-trading-system-using-kafka-redis-and-timescaledb-2e64ccac64b3
- https://aws.amazon.com/blogs/web3/optimize-tick-to-trade-latency-for-digital-assets-exchanges-and-trading-platforms-on-aws/

AI/LLM for trading:
- https://www.quantvps.com/blog/algorithmic-trading-with-llm
- https://fxnx.com/en/blog/gpt-vs-claude-prop-firm-challenge-showdown
- https://arxiv.org/pdf/2510.19173 (News-Aware Direct Reinforcement Trading)
- https://arxiv.org/pdf/2509.01393 (Adaptive Alpha Weighting with PPO)
- https://arxiv.org/html/2412.20138v3 (TradingAgents: Multi-Agent LLM Financial Trading Framework)
- https://arxiv.org/abs/2509.09995 (QuantHarness/QuantAgent)
- https://www.anthropic.com/news/claude-for-financial-services
- https://www.anthropic.com/news/finance-agents

Multi-timeframe analysis:
- https://tradeciety.com/how-to-perform-a-multiple-time-frame-analysis
- https://ninjatrader.com/futures/blogs/top-down-analysis-trading-guide/

News/sentiment APIs:
- https://newsdata.io/blog/best-stock-news-api/
- https://apitube.io/blog/post/best-financial-news-api-trading
- https://site.financialmodelingprep.com/education/news/nlppowered-sentiment-analyzer-using-fmp-news-api

Risk management / regulation:
- https://www.luxalgo.com/blog/risk-management-strategies-for-algo-trading/
- https://www.moomoo.com/us/learn/detail-pdt-rules-25k-limit-removed-118225-260451094
- https://www.tradersmagazine.com/featured_articles/regulators-end-pdt-rule/

Brokers:
- https://brokerchooser.com/best-brokers/best-brokers-for-algo-trading
- https://algotest.in/blog/best-brokers-for-algo-trading-in-india/
- https://indianbrokertest.com/best-trading-apis-in-india/

*(v1.1: US broker comparison sources removed — they were researched before the India scoping. India broker sources above.)*

Technical indicator libraries:
- https://ta-lib.org/
- https://pypi.org/project/streaming-indicators/0.0.9
- https://pypi.org/project/rtta/0.0.9

Backtesting:
- https://python.financial/
- https://waylandz.com/quant-book-en/Quant-Framework-Comparison/

---

*End of document. This is a living reference — update sections as decisions are made, technologies are validated hands-on, or research assumptions change (especially §6.3's model recommendations and §3.1/§7.1's provider pricing, which move fast).*
