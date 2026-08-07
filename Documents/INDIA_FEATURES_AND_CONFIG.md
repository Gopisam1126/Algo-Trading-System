# India Market Edition — Feature Catalogue & System Configuration Specification

> 📘 **New here? Start with [MASTER_REFERENCE.md](MASTER_REFERENCE.md)** — the single onboarding document covering the whole system.

**Companion to:** [ARCHITECTURE_RESEARCH.md](ARCHITECTURE_RESEARCH.md) (read that first — it covers the general architecture, AI layering, and latency reasoning that this document assumes)
**Followed by:** [LOW_LEVEL_ARCHITECTURE.md](LOW_LEVEL_ARCHITECTURE.md) — the technical implementation spec: services, schemas, tech stack, security architecture, deployment
**Then:** [MVP_UI_AND_LEGAL.md](MVP_UI_AND_LEGAL.md) — MVP feature scope, autonomy model, UI/admin design, Indian legal & tax framework
**Also:** [STRATEGY_ENGINE.md](STRATEGY_ENGINE.md) — strategy DSL, AI generation, validation gauntlet · [VERIFICATION_REPORT.md](VERIFICATION_REPORT.md) — cross-document audit
**Scope:** NSE/BSE Indian equities and (optionally) F&O. Multi-stock, intraday-focused.
**Status:** Pre-implementation specification. No code written.
**Last updated:** 2026-08-04

---

## 0. What This Document Adds

The companion document answered *"what should the architecture be."* This one answers three concrete follow-on questions:

1. **What features can actually be implemented** — a catalogue, organized by module, with priority tiers.
2. **What configuration knobs the system should expose** — a full proposed config schema, so the system is tunable without code changes.
3. **How the pre-market preparation engine works** — the centerpiece requirement: the AI should walk into the 9:15 AM open already knowing what it's trading today and why, having done multi-timeframe historical analysis overnight and ranked the day's most tradeable stocks.

Everything here is specific to Indian market structure, which differs from US markets in ways that materially change the design (regulatory algo framework, surveillance lists, circuit bands, square-off timings, F&O ban lists).

---

## 1. ⚠️ The Regulatory Constraint That Shapes Everything (Read Before Designing Anything)

SEBI's retail algo trading framework (circular dated 4 Feb 2025) became **fully mandatory on 1 April 2026** — it is live now. This is not a footnote; it constrains the system's architecture directly.

### 1.1 What the framework requires

| Requirement | Impact on this system |
|---|---|
| **Broker is the "principal"** — every algo on a broker's platform is the broker's responsibility; algo providers must partner with a registered broker and cannot connect directly to exchanges. | Route everything through a broker API. No direct exchange connectivity. |
| **Algo-ID on every order** — from 1 Apr 2026, every algorithmically-placed order carries an exchange-assigned identifier so orders are traceable to their source. | The order-placement module must attach the correct Algo-ID; confirm with your broker how they issue/inject it. |
| **Order-rate threshold: 10 orders/second per segment.** Above this = registered algo, full approval process. Below this = still needs a **Generic Algo ID**, but is exempt from individual approval. | **Design the system to stay well under 10 OPS.** For a multi-minute-interval, multi-stock system this is easy and should be enforced as a hard config limit (see §7 `execution.max_orders_per_second`). Staying under this threshold is a deliberate design goal, not an accident. |
| **Self-developed algos for personal use are permitted** for the trader's own account and immediate family (spouse, dependent children, dependent parents), without registering the algo — provided the order-rate threshold isn't breached. | ✅ This system, used for your own capital, fits in the permitted lane. |
| **Static IP whitelisting** — brokers must block API requests from non-whitelisted/dynamic IPs. Static IP can be shared only within "family" as defined above, with 2FA-verified consent and prior broker permission. | **Deployment must have a static IP.** This rules out naive serverless/autoscaling deployments where the egress IP changes. Plan for a fixed-IP VPS or a NAT gateway with a reserved static IP. |
| **Algos must be hosted on Indian servers.** | **Deploy in an India region** (AWS `ap-south-1` Mumbai, Azure Central India, GCP `asia-south1`, or an Indian VPS provider). This also happens to help latency to NSE/BSE — the compliance constraint and the performance goal point the same direction. |
| **OAuth-based login + 2FA; all API sessions must auto-logout before the next pre-open.** | The system needs a **daily re-authentication step** built into its startup sequence. This is not optional and cannot be worked around by keeping a session alive overnight — design the daily lifecycle (§4) around a fresh login every morning. |
| **White Box vs Black Box** — transparent rule-based algos are easier to approve; black-box (proprietary, non-disclosed logic) requires the provider to hold a SEBI Research Analyst licence and disclose performance periodically. | Relevant only if you ever *distribute* this to others. For personal use it's moot — but note that an LLM-in-the-loop system is inherently harder to classify as "white box," which is one more reason to keep the **deterministic risk/execution layer fully rule-based and auditable** (companion doc §10). |

### 1.2 Practical takeaways
- ✅ Personal-use, self-developed, sub-10-OPS, static-IP, India-hosted → **the permitted lane.** Design to stay inside it deliberately.
- ⚠️ **Confirm the specifics with your chosen broker before building** — brokers implement the framework slightly differently (some require pre-registering the API app, some auto-inject Algo-IDs, some have extra API terms). This is the single highest-value phone call to make before writing code.
- 🚫 Do not design for anything that requires direct exchange connectivity, cross-client algo distribution, or high order rates.

---

## 2. Indian Market Structure — What the System Must Know

### 2.1 Session timings (IST) — **note: NSE changed these very recently**

| Session | Timing | System behaviour |
|---|---|---|
| Block deal window (morning) | 08:45 – 09:00 | Informational only; large block prints can signal institutional interest. |
| **Pre-open session** | **09:00 – 09:15** | Order collection 09:00–09:08, matching 09:08–09:12, buffer to 09:15. Equilibrium price discovered here = the opening price. **Critical input for the system's final pre-market re-rank (§4).** |
| Normal market | 09:15 – 15:30 | Main trading loop. |
| **Closing Auction Session (CAS)** | **From 03 Aug 2026**, for Category I (F&O) stocks: continuous trading ends **15:15**, CAS runs to **15:35** | **This is brand new — it went live yesterday relative to this document's date.** The system must handle two different end-of-day regimes depending on whether a stock is in CAS scope. |
| MIS auto square-off (CAS stocks) | **15:10** | Intraday positions in CAS-scope stocks are force-closed earlier than before. |
| MIS auto square-off (non-CAS stocks) | 15:20 | Standard. |
| F&O auto square-off | 15:25 | If trading derivatives. |

**Design implication:** square-off timing is **per-stock, not global.** The system needs a `is_cas_stock` flag per symbol and must compute each position's own hard-exit deadline. Getting this wrong means the broker force-closes positions at market price at a moment the algo didn't choose — a silent, recurring source of slippage. The system should always exit on its *own* terms, several minutes before the broker's deadline (see `risk.exit_buffer_minutes` in §7).

⚠️ **Verify current timings against NSE circulars at implementation time.** NSE has extended F&O timings and introduced CAS within the last few months; this area is actively changing.

### 2.2 India-specific instrument hazards (hard filters — see §5.2)

These have no US equivalent and will silently break an algo that ignores them:

| Hazard | What it means | System handling |
|---|---|---|
| **F&O ban period** | When a stock's derivatives open interest exceeds 95% of Market-Wide Position Limit, NSE bans *new* F&O positions (square-off only) until OI falls below 80%. Violating it incurs a penalty of 1% of the increased position value (min ₹5,000, max ₹1,00,000). | Fetch the daily ban list pre-market; hard-exclude from F&O trading. Equity/cash trading in the same stock is still permitted but the underlying is usually volatile — flag it. |
| **ASM / GSM surveillance lists** | Additional/Graded Surveillance Measure. Exchanges place volatile or suspect stocks under surveillance, often with 100% margin requirements, reduced circuit bands, or trade-to-trade settlement. | Hard-exclude from the intraday universe. Refresh daily. |
| **T2T (Trade-to-Trade) segment** | Compulsory delivery — **intraday trading is not permitted at all.** Every trade must be settled by delivery. | Hard-exclude from any intraday strategy. An algo that doesn't check this will place orders that get rejected or, worse, become unintended delivery obligations. |
| **Circuit limits / price bands** | Per-stock daily limits (2%, 5%, 10%, 20%). A stock that hits its circuit becomes untradeable in that direction — no liquidity on one side. Exchanges revise these periodically per stock. | Exclude stocks with narrow bands (2%/5%) from intraday strategies — there isn't enough room to profit. Configurable via `universe.min_circuit_band_pct`. |
| **Peak margin rules** | Full SEBI SPAN + Exposure margin required at all times, not just EOD; clearing corporations take 4 random intraday snapshots, and shortfall at any snapshot penalises the broker (passed on to you). Retail intraday leverage is now heavily curtailed. | Position sizing must use **real available margin from the broker's API**, not a theoretical leverage multiple. Never assume intraday leverage. |

### 2.3 Market context signals unique to India

These feed the "Market Condition Context" object (companion doc §7):

- **GIFT Nifty** (formerly SGX Nifty, moved to NSE International Exchange at GIFT City in 2023) — USD-denominated Nifty 50 futures that trade nearly around the clock. The gap between the previous Nifty close and GIFT Nifty at ~09:00 IST is **the** standard pre-open gap predictor. Research indicates it calls the *direction* of the open correctly roughly 85–90% of the time, but the *magnitude* far less reliably (actual opens frequently overshoot/undershoot by 15–40 points). **Design accordingly: use it for directional bias, not for precise gap sizing.**
- **India VIX** — volatility regime. Feeds position sizing (smaller size in high-VIX regimes) and strategy selection (breakouts work better in high volatility; mean-reversion in low).
- **FII/DII daily activity** — foreign vs domestic institutional net buy/sell in cash and F&O. Published daily; a well-established sentiment/flow input for Indian markets. Best treated as a slow-moving contextual signal, not an intraday trigger.
- **Put-Call Ratio (PCR) and participant-wise Open Interest** — options positioning. Commonly-cited heuristic bands: PCR above ~1.3 suggests heavy hedging/caution, below ~0.7 suggests complacency. Use as one contextual input among several; not a standalone signal.
- **Global overnight cues** — US close (S&P/Nasdaq), Asian markets (Nikkei, Hang Seng), USD/INR, crude oil. Crude matters disproportionately for India (energy import dependence → affects Auto, Paints, Aviation, and the rupee).
- **Sector rotation** — NSE publishes 24+ sectoral/thematic indices (Nifty Bank, IT, Auto, FMCG, Pharma, Metal, Energy, Realty…). India-specific rotation drivers: RBI rate decisions (Banking, Realty), rupee direction (IT, Pharma — exporters), monsoon quality (FMCG, Agri), crude (Energy, Auto). Note that *intraday* sector rankings reshuffle constantly and are noisy — **use end-of-day/multi-day sector strength for the pre-market thesis, not minute-by-minute sector ranking.**

---

## 3. Broker & Data Provider Selection (India)

### 3.1 Broker API comparison (researched Aug 2026)

| Broker | Order rate limit | Data cost | Notes |
|---|---|---|---|
| **Angel One SmartAPI** | ~10 orders/sec | Free | Frequently cited as the strongest free option — reliable WebSocket streaming, practical rate limits. Strong default candidate. |
| **Zerodha Kite Connect** ⭐ | **10 orders/sec** account-wide (429 above), ~200 data req/min | ~₹500/mo (data APIs; order placement reported free) | **SELECTED — see §3.3 for verified constraints.** Most mature ecosystem and documentation. Daily token expiry via a browser-redirect flow. |
| **Fyers API** | — | Free | Free API, free minute-level historical data for ~1–2 years via API — genuinely useful for backtesting. Native TradingView integration. |
| **DhanHQ** | — | ₹0 orders / ₹499 for real-time + historical data | Native TradingView integration. |
| **Upstox API v2** | — | Free | Execution-focused; reported order round-trips in the 40–80 ms range. |
| Alice Blue / Shoonya | — | Free | Additional free-API options. |

**Decision: Zerodha Kite Connect (primary) + Fyers (data fallback).** Chosen for ecosystem maturity and documentation quality. Fyers remains in the design as a secondary data feed — its free minute-level history removes a real cost barrier for pre-market analysis, and a second WebSocket is cheap insurance against the open/expiry-day instability common to Indian broker APIs.

> **Corrected.** An earlier revision put Zerodha at ~3 orders/sec from a secondary source. Zerodha staff state **10 OPS enforced account-wide** on the Kite Connect developer forum. See §3.3.

⚠️ **Verify each broker's current SEBI-framework compliance status and API terms directly** — the April 2026 deadline reshaped what brokers permit, and blog comparisons go stale fast.

### 3.3 ⚠️ Zerodha Kite Connect — verified operational constraints

Confirmed against Zerodha's own Kite Connect developer forum (staff replies) and
Z-Connect, August 2026. The machine-readable copy lives in
`Code/src/algotrader/broker/profiles.py` and is the authoritative version.

| Constraint | Detail | Design impact |
|---|---|---|
| **Order rate** | **10 OPS enforced account-wide** (not per app); HTTP 429 above it. Of 15 attempted, 10 place and 5 are blocked. | We run at 3/sec by choice, not necessity. |
| **Market protection** ⚠️ | **MARKET and SL-M orders REQUIRE a `market_protection` parameter from 1 Apr 2026.** Without it the broker rejects them; `0` is also rejected. `-1` requests broker auto-protection. It converts a market order into a limit order and remains subject to exchange LPP ranges. | **Highest-risk item.** The square-off exit is a market order — an unprotected one means the position does not close and the broker force-closes it at whatever price is there. Enforced in `OrderRequest`. |
| **pykiteconnect gap** ⚠️ | Version 5.1.0 on PyPI does **not** expose `market_protection` in `place_order()`; it is on the main branch only (zerodha/pykiteconnect#225). | A plain `pip install kiteconnect` yields a version that cannot place compliant market orders. **Verify before live.** |
| **Static IP scope** ✅ | Applies to **order endpoints only**. Quotes, WebSocket, orderbook and positions stay reachable from any IP. | Maps cleanly onto the read-only vs trading service split — only `execution-svc` must originate from the whitelisted address. |
| **IP registration** | `developers.kite.trade` → profile → "IP Whitelist". IPv4 and IPv6 both accepted. Orders from an unregistered IP are rejected. | One-time setup, verified at startup by `doctor.py`. |
| **IP binding** | Each static IP binds to **one account**. Family sharing is permitted; multiple Zerodha accounts can sit under one developer profile. | Relevant only when extending to family accounts. |
| **Daily auth** | Browser redirect: login URL → `request_token` → exchange with `api_secret` → `access_token`, expiring daily. | **Manual daily login accepted** for this deployment. Sets a floor of one human touchpoint each trading morning. |
| **Algo-ID** | Self-developed algos under 10 OPS receive a **generic** exchange ID, not a unique registered one. | ⚠️ Sources disagree on whether the developer attaches it via the order `tag` field or the broker injects it — **confirm with Zerodha before live**. |
| **Daily order cap** | ~3,000 orders/day for most accounts, extendable on request. | Far above this system's usage. |
| **Cost** | ~₹500/month for data APIs; order placement reported free. Historical data appears to be a separate add-on. | Confirm current pricing directly. |

### 3.2 Historical data for the pre-market engine
The pre-market analysis engine (§4) needs multi-year, multi-timeframe historical data for the full universe:
- **Fyers API** — free minute-level history (~1–2 years) is the most cost-effective starting point.
- **NSE Bhavcopy** — free daily EOD data (open source tooling exists to fetch and archive it); good for building the daily/weekly timeframe history and a long-run local archive.
- **NSE paid EOD subscription** — official binary EOD files for CM/F&O segments, if data integrity becomes critical.
- **Build a local historical store from day one** (TimescaleDB per companion doc §12). Re-pulling history from an API every morning is slow, rate-limited, and fragile; the pre-market engine should read from a local store that a nightly job keeps current.

---

## 4. ⭐ The Pre-Market Preparation Engine (Core Requirement)

**Goal:** by 09:15 the system already has a ranked, fully-analyzed watchlist with a written thesis per stock, entry/exit levels, and position-size allocations — so the live session is *execution of a plan*, not analysis under time pressure.

This is exactly how a professional discretionary trader works: the hard thinking happens before the bell; the session itself is disciplined execution.

### 4.1 Daily timeline (all times IST)

| Time | Stage | What happens |
|---|---|---|
| **05:30 – 06:30** | **Data sync** | Pull previous day's bhavcopy/EOD data into the local store. Apply corporate actions (splits, bonuses, dividends) to the historical series. Refresh instrument master. Fetch updated ASM/GSM lists, T2T list, circuit bands, F&O ban list. |
| **06:30 – 07:30** | **Universe construction** | Apply hard filters (§5.2) to the base universe (e.g., Nifty 500) → produces the *eligible* universe, typically a few hundred names. |
| **07:30 – 08:15** | **Multi-timeframe historical analysis** | For every eligible stock, compute indicator state across **Weekly / Daily / Hourly** timeframes from the local store. Compute trend alignment, relative strength vs. sector and vs. Nifty, volatility profile, key support/resistance levels, volume patterns. Score and rank (§5.3). **This is the heaviest compute of the day — deterministic quant, no AI yet.** |
| **08:15 – 08:45** | **News & macro sweep** | Overnight news per shortlisted stock. Global cues (US close, Asia, crude, USD/INR). Economic calendar check for today (RBI policy, CPI, GDP, results). Earnings/corporate-action calendar for the shortlist. Sector strength ranking. |
| **08:45 – 09:00** | **AI deep synthesis** ⭐ | The **top-tier reasoning model** runs once here — the one place per day where latency doesn't matter and depth does. It receives the ranked shortlist with full multi-timeframe feature sets + news + macro context, and produces: (a) a **Daily Market Thesis** (expected regime, bias, key levels on Nifty/Bank Nifty, what would invalidate the thesis), and (b) a **per-stock playbook** for the top N candidates — the setup, why it's attractive, what confirms it, what invalidates it, and preferred direction. |
| **09:00 – 09:12** | **Pre-open + gap adjustment** | Read GIFT Nifty for directional bias. Consume pre-open session equilibrium prices → compute the actual opening gap per stock. **Re-rank:** stocks that gapped beyond their planned entry get demoted or invalidated; gap-and-go candidates get promoted. |
| **09:12 – 09:15** | **Final plan lock** | Freeze the day's watchlist: ranked stocks, direction bias, capital allocation per slot, per-stock risk limits. Publish plan to the user (§6.5). Confirm broker auth, margin availability, and connectivity. |
| **09:15 – 09:20** | **Observation only** | **No trading.** The first minutes of the Indian open are noisy and spread-heavy. Let the opening range begin forming. |
| **09:20 / 09:30 – 15:00** | **Live trading loop** | The fast loop from the companion doc runs on the derived interval (§8 there). Opening range (09:15–09:30) is now a usable level for breakout strategies. |
| **15:00** | **No new entries** | Stop opening positions; manage existing only. |
| **15:05 / 15:15** | **Own-terms exit** | Close remaining intraday positions *before* broker auto-square-off (15:10 CAS / 15:20 non-CAS), per-stock. |
| **15:35 – 16:30** | **EOD review & learning loop** | Reconcile fills vs. plan. Log every decision with its AI rationale and outcome. Compute the day's stats. Feed outcomes into the journal that tomorrow's pre-market run can reference (§6.6). |

### 4.2 Why this design directly answers the brief
- *"AI should already have the analysis before market opens"* → the 08:45 deep-synthesis stage produces a complete written plan before 09:15.
- *"by conducting historical analysis on different timeframes"* → the 07:30 stage runs Weekly/Daily/Hourly analysis across the whole eligible universe.
- *"select the ones that are the most effective trade stocks for the day"* → the scoring and ranking engine (§5.3) produces the ranked shortlist, then the pre-open gap check refines it with the freshest possible information.
- *"trade multiple stocks at the same time"* → the slot-based capital allocation model (§6.4).

**The key efficiency insight:** the expensive, slow, deep AI reasoning happens **once per day, before the market opens**, where a 60-second model response is completely acceptable. During live hours, the AI only needs to make fast, narrow judgments against a plan it already wrote — a far cheaper and faster call. This resolves the latency problem identified in the companion document (§13) far more elegantly than trying to speed up in-session AI calls.

---

## 5. Stock Selection & Ranking Engine

### 5.1 Base universe options (configurable)
- **Nifty 50** — most liquid, safest starting point, easiest to model.
- **Nifty 100 / 200** — good breadth/liquidity balance. **Recommended default.**
- **Nifty 500** — wider opportunity set, but includes names with thinner liquidity; requires stricter filters.
- **F&O universe (~180–200 stocks)** — all have derivatives, hence institutional interest and good liquidity.
- **Custom watchlist** — user-supplied symbol list.

### 5.2 Hard filters (binary exclusions — applied before any scoring)

A stock failing *any* of these is removed from the day's universe entirely:

| Filter | Default | Rationale |
|---|---|---|
| Not in **T2T** segment | required | Intraday trading structurally impossible. |
| Not in **ASM/GSM** surveillance | required | Punitive margins, reduced bands, erratic behaviour. |
| Not in **F&O ban** list (if trading F&O) | required | Penalty risk; only square-offs permitted. |
| Circuit band ≥ 10% | configurable | 2%/5% band stocks can't move enough to be worth intraday risk. |
| Last close ≥ ₹100 | configurable | Sub-₹100 stocks have wider relative spreads and noisier behaviour. |
| 20-day avg volume ≥ 5,00,000 shares | configurable | Standard India intraday liquidity floor; ensures fills without slippage. |
| Market cap ≥ ₹5,000 cr | configurable | Avoids illiquid microcaps and operator-driven moves. |
| Avg daily range ≥ 1.5% | configurable | Below this there isn't enough movement for intraday profit after costs. |
| No earnings/major corporate action today | configurable | Binary-outcome event risk; a technical thesis is meaningless against an earnings gap. Optionally invert this into a dedicated event-driven strategy later. |
| Bid-ask spread ≤ 0.05% (checked at pre-open) | configurable | Wide spreads silently eat intraday edge. |

### 5.3 Composite scoring model (ranks what survives the filters)

Each surviving stock receives a **0–100 Tradeability Score**, computed deterministically (no AI — this must be fast, explainable, and backtestable). Proposed components with default weights, all configurable:

| Component | Weight | What it measures |
|---|---|---|
| **Multi-timeframe trend alignment** | 25% | Do Weekly, Daily, and Hourly agree on direction? Full agreement (3/3) scores maximum; conflict scores near zero. This is the "confluence" principle from the companion doc §9.2, made numeric. |
| **Relative strength** | 20% | Stock vs. its sector index, and sector vs. Nifty. Rewards leaders in leading sectors (and, for shorts, laggards in lagging sectors). |
| **Volatility fitness** | 15% | ATR as % of price — scored as a *band*, not "more is better." Too low = no profit potential; too high = stops get hit by noise. Rewards the tradeable middle. |
| **Volume expansion** | 15% | Recent volume vs. its own 20-day average. Rising volume confirms genuine participation rather than drift. |
| **Proximity to a decision level** | 15% | Distance to a significant support/resistance/pivot/prior-day-high-low. Stocks *at* a level offer defined risk (tight stop, clear invalidation); stocks in the middle of nowhere don't. |
| **Catalyst/news presence** | 10% | Fresh news, sector momentum, or an event that explains *why* it would move today. Sentiment-scored. |

**Then:** the top N by score (configurable, default ~15) are passed to the AI deep-synthesis stage, which produces per-stock playbooks and may re-order or veto based on qualitative reasoning that the numeric score can't capture (e.g., "this technical setup is textbook, but the news flow contradicts it — skip"). **The AI can veto or demote, and can rank within the shortlist — but the hard filters and the numeric score decide who gets *considered*.** This keeps the expensive, non-deterministic layer applied to a small, pre-qualified set.

---

## 6. Feature Catalogue

Organized by module, with priority tiers: **P0** = required for a working system, **P1** = high value, **P2** = later enhancement.

### 6.1 Data & Ingestion
| P | Feature |
|---|---|
| P0 | WebSocket live tick/quote streaming for the day's watchlist |
| P0 | Nightly EOD/bhavcopy sync into local historical store |
| P0 | Corporate action adjustment (splits, bonus, dividends) on historical series |
| P0 | Instrument master refresh (symbols, tokens, lot sizes, tick sizes) |
| P0 | Daily fetch: ASM/GSM lists, T2T list, F&O ban list, circuit bands |
| P0 | Multi-timeframe bar aggregation (1m → 5m → 15m → 1h → D → W), incrementally maintained |
| P1 | Dual-broker data redundancy (failover if primary WebSocket drops) |
| P1 | Tick data archival for later replay/backtesting |
| P2 | Level-2 / market depth ingestion (only if a strategy needs it) |

### 6.2 Analysis & Signals
| P | Feature |
|---|---|
| P0 | Incremental technical indicator engine, per stock per timeframe |
| P0 | Multi-timeframe confluence scoring |
| P0 | Support/resistance & pivot level detection |
| P0 | Opening Range (09:15–09:30) computation and breakout detection |
| P0 | Relative strength vs. sector and vs. Nifty |
| P1 | Chart pattern recognition (flags, triangles, double tops/bottoms, breakouts) |
| P1 | Candlestick pattern detection |
| P1 | Volume profile / VWAP bands |
| P1 | Gap classification (gap-and-go vs. gap-fill likelihood, based on historical gap behaviour per stock) |
| P2 | Options-derived signals (PCR, OI buildup, max pain) for F&O-enabled stocks |
| P2 | Intermarket correlation (USD/INR, crude → sector impact modelling) |

### 6.3 AI Reasoning
| P | Feature |
|---|---|
| P0 | Pre-market deep synthesis → Daily Market Thesis + per-stock playbooks (top-tier model, once daily) |
| P0 | Structured output schema (JSON) so AI output is machine-consumable, not free text |
| P0 | Confidence scoring on every AI judgment, with a configurable minimum threshold to act |
| P0 | Plain-language rationale logged with every decision (auditability + user trust) |
| P1 | In-session fast confirmation calls (mid-tier model) — "does this trigger still fit the morning thesis?" |
| P1 | News headline triage & sentiment scoring (cheap/fast model, high volume) |
| P1 | Market regime classification (trending / range-bound / high-volatility / risk-off) |
| P1 | Thesis invalidation monitoring — AI flags when the day's premise has broken and the plan should be abandoned |
| P2 | Multi-agent specialization (separate Technical / News / Risk / Sector agents with a coordinator, per the research frameworks in the companion doc §6.1) |
| P2 | End-of-day self-review: AI analyzes the day's wins/losses and proposes adjustments |

### 6.4 Multi-Stock Concurrent Trading ⭐
This is the brief's "trade on multiple stocks at the same time" requirement. The core mechanism is **slot-based capital allocation**:

| P | Feature |
|---|---|
| P0 | **Position slots** — capital divided into N slots (e.g., 5 slots × 20% each). One stock per slot. Prevents over-concentration and makes multi-stock risk bounded and predictable. |
| P0 | **Priority queue** — when more valid signals fire than there are free slots, the highest Tradeability Score wins the slot |
| P0 | **Correlation guard** — refuse to fill multiple slots with highly correlated names (e.g., 4 PSU banks = one bet wearing four hats, not four independent bets) |
| P0 | **Sector exposure cap** — max % of capital in any one sector |
| P0 | **Aggregate margin check** — verify real available margin from the broker API before every entry (peak margin rules mean no assumed leverage) |
| P1 | **Slot recycling** — when a position closes, its slot returns to the pool for the next queued candidate |
| P1 | **Dynamic slot sizing** — allocate more capital to higher-conviction setups instead of equal weighting |
| P1 | **Net directional exposure limit** — cap how net-long or net-short the whole book can get |
| P2 | Pairs/hedged positions (long leader, short laggard within a sector) |

### 6.5 User Interface & Alerting
| P | Feature |
|---|---|
| P0 | **Pre-market briefing** delivered before 09:15 — the day's thesis, ranked watchlist, and per-stock plans (Telegram/email/dashboard) |
| P0 | Real-time trade alerts with the AI's rationale attached |
| P0 | Live position/P&L dashboard |
| P0 | **Manual kill switch** — one action halts all new trading immediately |
| P1 | **Approval mode** — system proposes, user confirms before execution (essential for the early phases; see companion doc §17) |
| P1 | EOD performance report with per-decision breakdown |
| P1 | Mobile-friendly alerts (Telegram bot is the pragmatic choice in the Indian retail context) |
| P2 | Full web dashboard with live charts and annotated signals |

### 6.6 Risk, Execution & Learning
| P | Feature |
|---|---|
| P0 | Deterministic position sizing (volatility/ATR-based) |
| P0 | Automatic stop-loss on every entry, no exceptions |
| P0 | **Per-stock square-off deadline awareness** (15:10 CAS / 15:20 non-CAS / 15:25 F&O) with own-terms exit buffer |
| P0 | Daily loss limit with automatic halt |
| P0 | Order rate limiter (hard cap below 10 OPS for SEBI compliance) |
| P0 | Full immutable audit log of every decision, rationale, order, and fill |
| P1 | Trailing stops and partial profit-booking at R-multiples |
| P1 | Order retry/failure handling and reconciliation (what the algo *thinks* it holds vs. what the broker says) |
| P1 | **Trade journal with outcome attribution** — feeds tomorrow's pre-market AI context ("this setup type has failed 4 of the last 5 times in the current regime") |
| **P0** | **Strategy registry with validation lifecycle** — see [STRATEGY_ENGINE.md](STRATEGY_ENGINE.md). *(Upgraded from P2 in v1.1: with a strategy engine that can generate its own strategies, these are not enhancements — they are the controls that make the feature survivable.)* |
| **P0** | **Walk-forward re-validation with overfitting correction** (Deflated Sharpe, PBO) |
| **P0** | **Automatic strategy demotion/retirement on performance degradation** |
| P1 | User-authored strategies via the declarative DSL |
| P2 | AI strategy generation (journal-driven, then observation-driven) — *only after the validation gauntlet exists* |

---

## 7. Proposed Configuration Schema

The design goal: **everything tunable without touching code.** Config lives in version-controlled YAML; the system validates it at startup and refuses to run on an invalid config.

```yaml
# ============================================================
# system.yaml — AI Algo Trading Platform (India Edition)
# ============================================================

system:
  mode: paper                    # paper | alert_only | approval | live
  timezone: Asia/Kolkata
  deployment_region: ap-south-1  # MUST be India (SEBI requirement)
  static_ip: "x.x.x.x"           # MUST be whitelisted with broker (SEBI requirement)
  log_level: INFO

# ------------------------------------------------------------
broker:
  primary: zerodha               # angelone | zerodha | fyers | dhan | upstox
  fallback: fyers                # optional secondary for data redundancy
  algo_id: ""                    # exchange-assigned; confirm with broker
  auth:
    method: oauth_2fa
    daily_reauth_time: "07:00"   # SEBI: sessions auto-logout before pre-open
    credentials_source: env      # never hardcode secrets in this file

# ------------------------------------------------------------
data:
  historical_store: timescaledb
  history_depth_days: 750        # ~3 years, for multi-timeframe analysis
  timeframes: [1m, 5m, 15m, 1h, 1d, 1w]
  eod_sync_time: "05:30"
  tick_archival: true
  websocket:
    reconnect_max_retries: 10
    reconnect_backoff_sec: 2
    heartbeat_timeout_sec: 30

# ------------------------------------------------------------
universe:
  base: nifty200                 # nifty50 | nifty100 | nifty200 | nifty500 | fno | custom
  custom_symbols: []

  hard_filters:                  # binary exclusions — see §5.2
    exclude_t2t: true
    exclude_asm_gsm: true
    exclude_fno_ban: true
    exclude_earnings_today: true
    min_circuit_band_pct: 10
    min_price: 100
    min_avg_volume_20d: 500000
    min_market_cap_cr: 5000
    min_avg_daily_range_pct: 1.5
    max_spread_pct: 0.05

  scoring_weights:               # must sum to 1.0 — see §5.3
    trend_alignment: 0.25
    relative_strength: 0.20
    volatility_fitness: 0.15
    volume_expansion: 0.15
    level_proximity: 0.15
    catalyst_news: 0.10

  shortlist_size: 15             # passed to AI deep synthesis
  final_watchlist_size: 8        # actively monitored during the session

# ------------------------------------------------------------
premarket:                       # see §4 — the core daily preparation pipeline
  enabled: true
  schedule:
    data_sync:        "05:30"
    universe_build:   "06:30"
    historical_mtf:   "07:30"
    news_macro_sweep: "08:15"
    ai_synthesis:     "08:45"
    preopen_adjust:   "09:02"
    plan_lock:        "09:12"
    briefing_delivery:"09:13"

  historical_analysis:
    timeframes: [1w, 1d, 1h]     # top-down: weekly context → daily trend → hourly structure
    lookback_bars: {1w: 104, 1d: 250, 1h: 500}

  gap_adjustment:
    use_gift_nifty: true
    use_preopen_equilibrium: true
    invalidate_if_gap_exceeds_pct: 2.0   # entry thesis void if it gaps past the plan
    promote_gap_and_go: true

# ------------------------------------------------------------
ai:
  provider: anthropic

  # Tiered models — cost/latency/quality tradeoff (companion doc §6.3)
  models:
    deep_synthesis:   claude-opus-5      # once daily, pre-market, depth over speed
    session_reasoning: claude-sonnet-5   # in-session confirmations
    news_triage:      claude-haiku-4-5   # high-volume, low-stakes classification

  deep_synthesis:
    max_stocks_analyzed: 15
    include_news_context: true
    include_macro_context: true
    include_trade_journal: true          # yesterday's lessons inform today
    output_schema: strict_json

  session_reasoning:
    enabled: true
    trigger: on_signal_only              # never poll every stock every cycle
    timeout_sec: 15
    fallback_on_timeout: skip_trade      # fail safe, never fail open

  confidence:
    min_to_act: 0.65                     # below this → no trade
    min_for_full_size: 0.80              # below this → reduced size
    treat_conflict_as_no_trade: true     # timeframe disagreement → stand down

  cost_controls:
    daily_token_budget: 2000000
    alert_at_pct: 80
    hard_stop_at_pct: 100

# ------------------------------------------------------------
news:
  enabled: true
  refresh_interval_min: 20               # slow loop — decoupled from trading loop
  sources: [economic_calendar, market_news, company_announcements]
  macro_signals:
    gift_nifty: true
    india_vix: true
    fii_dii_flows: true
    usd_inr: true
    crude_oil: true
    sector_rotation: true
  event_blackout:                        # stand down around known volatility events
    rbi_policy:   {minutes_before: 30, minutes_after: 60}
    cpi_gdp_data: {minutes_before: 15, minutes_after: 30}
    union_budget: {full_day_halt: true}

# ------------------------------------------------------------
strategy:
  # NOTE (v1.1): strategies are no longer a static list. They live in a
  # versioned registry with a validation lifecycle — see STRATEGY_ENGINE.md.
  # The block below configures the BUILT-IN seed strategies that ship with
  # the system; user-authored and AI-generated strategies are stored in the
  # database, not here.

  seed_strategies:
    - opening_range_breakout
    - trend_continuation
    - support_resistance_bounce

  opening_range_breakout:
    range_minutes: 15                    # 09:15–09:30
    entry_window: ["09:30", "11:00"]
    min_range_pct: 0.5                   # too tight = noise
    max_range_pct: 2.5                   # too wide = stop is unaffordable
    require_volume_confirmation: true
    require_index_alignment: true        # don't fight the Nifty

  trend_continuation:
    require_mtf_alignment: 3             # all 3 timeframes must agree
    entry_on: pullback_to_ma

  support_resistance_bounce:
    max_distance_from_level_pct: 0.3
    require_rejection_candle: true

# ------------------------------------------------------------
# Strategy Engine — registry, validation gauntlet, AI generation.
# Full specification and rationale: STRATEGY_ENGINE.md
strategy_engine:
  enabled: true

  registry:
    max_active: 6
    max_active_per_regime: 3
    max_shadow: 10

  validation:                          # the overfitting gauntlet — see §5 there
    min_trades: 100
    min_regimes: 2
    max_pbo: 0.5                       # Probability of Backtest Overfitting
    min_deflated_sharpe_confidence: 0.95
    max_correlation_to_active: 0.8
    parameter_sensitivity_pct: 20
    holdout_months: 6                  # never seen by the AI generator
    walk_forward:
      method: cpcv                     # combinatorial purged cross-validation
      n_splits: 8
      embargo_pct: 2.0
    costs:                             # India charge structure, always applied
      brokerage_per_order: 20
      stt_pct: 0.025
      gst_pct: 18
      exchange_charges_pct: 0.00345
      stamp_duty_pct: 0.003
      slippage_bps: 5

  promotion:
    shadow_min_sessions: 20
    paper_min_trades: 30
    paper_min_expectancy_r: 0.15
    require_human_approval: true       # NOT overridable — no config can disable

  ai_generation:
    enabled: false                     # Phase 2 — enable only after the gauntlet
    cadence: weekly                    # batched, never continuous
    max_proposals_per_cycle: 5
    max_active_ai_strategies: 3
    model: claude-opus-5
    modes: [observation, journal]
    require_hypothesis: true           # NOT overridable
    min_journal_trades: 50

  degradation:
    rolling_window_trades: 30
    sharpe_below_pct_of_backtest: 50
    consecutive_losses: 6
    on_degrade: reduce_size
    on_continued_degrade: retire

# ------------------------------------------------------------
execution:
  interval_mode: adaptive                # derived from measured latency (companion §8)
  interval_floor: 5m                     # never faster than this
  interval_ceiling: 15m
  latency_headroom_multiplier: 2.0
  recalibrate_interval_daily: true

  max_orders_per_second: 3               # our conservative choice; Zerodha and
                                         # SEBI both permit 10
  market_protection: -1                  # REQUIRED on MARKET/SL-M — see §3.3
  order_type: limit                      # limit orders reduce slippage vs market
  limit_offset_pct: 0.05
  order_timeout_sec: 30
  on_partial_fill: keep_working

  no_trade_windows:
    - ["09:15", "09:20"]                 # opening noise
    - ["15:00", "15:30"]                 # no new entries near close

# ------------------------------------------------------------
risk:
  capital: 500000                        # INR
  position_slots: 5                      # max concurrent stocks (§6.4)
  capital_per_slot_pct: 20

  per_trade:
    risk_pct: 1.0                        # % of capital risked per trade
    sizing_method: atr_based             # atr_based | fixed_pct | volatility_scaled
    atr_multiplier_stop: 1.5
    max_position_pct: 20
    target_r_multiple: 2.0
    trailing_stop_after_r: 1.0

  portfolio:
    max_daily_loss_pct: 3.0              # → auto-halt for the day
    max_sector_exposure_pct: 40
    max_correlated_positions: 2
    max_net_directional_exposure_pct: 60
    consecutive_loss_halt: 3

  exit_buffer_minutes: 5                 # exit before broker auto square-off
  square_off_times:                      # per-stock, per §2.1
    cas_stocks: "15:10"
    non_cas_stocks: "15:20"
    fno: "15:25"

  kill_switch:
    manual_enabled: true
    auto_triggers:
      - daily_loss_limit_breached
      - broker_connection_lost
      - data_feed_stale_seconds: 60
      - ai_service_unavailable
      - margin_shortfall

# ------------------------------------------------------------
notifications:
  channels: [telegram, email]
  premarket_briefing: true
  trade_alerts: true
  risk_breach_alerts: true
  eod_report: true
  require_approval_before_entry: true    # set false only after paper-trading validation
```

---

## 8. Key Design Decisions Embedded Above (and why)

1. **The heavy AI reasoning runs once, pre-market, not per-tick.** This simultaneously solves the latency problem, the cost problem, and matches how professional traders actually work. It's the single most important structural decision in this document.
2. **Slot-based capital allocation** makes multi-stock trading bounded and predictable rather than an uncontrolled accumulation of positions.
3. **Hard filters before scoring, scoring before AI.** Each stage is cheaper than the next and shrinks the input to it. The AI only ever sees ~15 pre-qualified candidates, never 500 raw symbols.
4. **The AI can veto and rank, but never sizes positions or places orders.** Consistent with the companion document's core principle and with keeping the execution layer auditable under SEBI's white-box preference.
5. **Per-stock square-off deadlines, not a global one** — because NSE's new CAS regime (live since 03 Aug 2026) makes this genuinely stock-dependent.
6. **Config-driven everything** — strategy parameters, filters, weights, and risk limits are all tunable without code changes, which is what makes systematic iteration and backtesting possible.
7. **Fail-safe defaults throughout** — AI timeout → skip the trade; data stale → kill switch; low confidence → no trade; timeframe conflict → stand down. In trading, *not* acting is always a valid and usually cheap outcome; acting on bad information is not.

---

## 9. Open Decisions

Carried forward from the companion doc §16, now with India-specific framing:

1. **Cash/equity intraday only, or F&O too?** F&O offers leverage and better liquidity but adds ban lists, expiry dynamics, Greeks, and materially more complexity. **Recommendation: equity intraday first**, F&O only after the core system is proven.
2. **Which broker?** Requires confirming current SEBI-framework compliance and API terms directly with the broker.
3. **Capital and slot count** — determines whether 5 slots at ₹1L each is realistic or whether fewer, larger positions make more sense given per-trade costs.
4. **Static IP hosting** — Indian VPS vs. cloud with reserved IP. Needs deciding early since it's a compliance prerequisite, not an optimization.
5. **How long in alert-only/approval mode before auto-execution?** Strongly recommend a meaningful sample (weeks of sessions, across different market regimes) before the system places an order unattended.
6. **Intraday only, or hold overnight?** Overnight positions avoid square-off constraints entirely but add gap risk and change the margin picture completely.

---

## 10. Suggested Build Order (India Edition)

Refines the companion doc's roadmap with India specifics:

| Phase | Deliverable |
|---|---|
| **1** | Broker API integration + auth + daily re-login. Historical data store with bhavcopy sync. Instrument master + ASM/GSM/T2T/ban-list fetchers. **Nothing trades yet.** |
| **2** | Multi-timeframe indicator engine + hard filters + scoring model. Output: a ranked watchlist file each morning. Validate by eye against what actually moved that day, for several weeks. |
| **3** | Pre-market AI synthesis + morning briefing delivered to Telegram. **Alert-only — no orders.** This is the first genuinely useful milestone: it's a research assistant that hands you a plan by 09:15. |
| **4** | Live session monitoring + signal detection + trade alerts with rationale. Still no auto-execution. |
| **5** | Risk engine + paper trading execution. Run for a meaningful sample across different market conditions. |
| **6** | Approval mode on live capital (system proposes, you confirm) with small size. |
| **7** | Full automation, only after phases 5–6 demonstrate consistent, understood behaviour. |

**Phase 3 is where the system starts paying for itself** even if you never automate execution — a well-researched, ranked, reasoned daily plan delivered before the open is genuinely valuable on its own, and it validates the AI layer's quality with zero capital at risk.

---

## 11. Sources (Aug 2026 research)

SEBI algo regulations:
- https://www.tradejini.com/blogs/what-sebis-new-algo-trading-rules-mean-for-you
- https://algobulls.com/blog/industry-insights-and-updates/sebi-new-algotrading-regulations-for-retail-investors-2026
- https://www.sahi.com/blogs/sebi-algo-trading-rules-2026-what-every-retail-trader-must-know-before-april
- https://www.angelone.in/knowledge-center/online-share-trading/sebi-algo-trading-rules
- https://www.quantinsti.com/articles/algorithmic-trading-india/
- https://www.caalley.com/news-updates/indian-news/sebi-extends-retail-algo-trading-rollout-full-compliance-by-april-2026

Broker APIs:
- https://algotest.in/blog/best-brokers-for-algo-trading-in-india/
- https://indianbrokertest.com/best-trading-apis-in-india/
- https://indianbrokertest.com/best-brokers-for-algo-trading/

Market timings & CAS:
- https://www.nseindia.com/static/products-services/equity-market-pre-open
- https://anandrathi.com/blog/nse-extends-trading-hours
- https://www.sahi.com/blogs/closing-auction-session-cas-explained-nse-bse-closing-price-rules-2026
- https://www.indmoney.com/learn/indian-stocks/stock-market-timings-india

F&O ban, circuits, margins:
- https://www.stockezee.com/ban-list
- https://www.5paisa.com/nse-ban-list
- https://www.whitestallion.in/blog/sebi-margin-rules-2026-what-indian-fno-traders-need-to-know.html
- https://www.paytmmoney.com/blog/sebi-fo-trading-rules-india/

Pre-market & GIFT Nifty:
- https://marketnetra.in/blog/sgx-nifty-gift-nifty-pre-market-guide
- https://nifty50pulse.in/blog/nifty-50-pre-market-analysis-guide/
- https://www.niftytrader.in/gap-ups-gap-downs

Stock selection:
- https://lakshmishree.com/blog/how-to-select-stocks-for-intraday-trading/
- https://www.wrightresearch.in/blog/how-to-select-stocks-for-intraday-trading-in-india/
- https://www.sahi.com/blogs/how-to-select-a-stock-for-intraday-trading-key-strategies-explained

ORB strategy:
- https://intradaylab.com/blog/nifty-orb-breakout-strategy-backtest
- https://www.stockezee.com/stock-screener/opening-range-breakout

Market context data:
- https://www.niftytrader.in/fii-dii-data
- https://www.stockezee.com/sector
- https://www.nseindia.com/static/products-services/indices-sectoral
- https://zerodha.com/markets/calendar/

Historical data:
- https://www.nseindia.com/static/market-data/eod-historical-data-subscription

---

*Living document — update as broker choice is confirmed, SEBI guidance evolves, and NSE timings/CAS rules settle. The regulatory and market-structure sections in particular are snapshots of a fast-moving landscape and should be re-verified before implementation.*
