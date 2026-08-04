# Cross-Document Verification Report

**Date:** 2026-08-04
**Scope:** Full consistency and factual audit of all project documents, plus integration of the Strategy Engine
**Auditor:** Full re-read of every document against every other, plus re-verification of external facts

---

## 1. Summary

| Severity | Count | Status |
|---|---|---|
| **Critical** (would cause building the wrong thing) | 4 | ✅ All fixed |
| **Medium** (contradictions between documents) | 6 | ✅ All fixed |
| **Low** (typos, minor drift) | 3 | ✅ All fixed |
| **Gaps** (missing capability) | 1 major | ✅ New document written |
| **Verified correct** | 14 checks | ✅ No action needed |

**Headline finding:** [ARCHITECTURE_RESEARCH.md](ARCHITECTURE_RESEARCH.md) was written before the project scoped to Indian markets and still recommended **Alpaca and Interactive Brokers as brokers, US data vendors, and the US Pattern Day Trader rule as the regulatory frame.** Anyone reading the documents in the prescribed order would have started by building the wrong broker integration against the wrong regulator. This has been corrected throughout.

---

## 2. Critical findings

### V1 — Document 1 recommended US brokers that do not serve Indian markets
**Where:** ARCHITECTURE_RESEARCH.md §3.1, §10.2, §2 diagram, §16.2
**Problem:** Alpaca recommended as "best default starting point" for both data and execution. Alpaca does not offer NSE/BSE access at all. Documents 2–4 correctly specify Angel One / Fyers / Zerodha. A reader following the documents in order would integrate the wrong broker.
**Fix:** All four locations rewritten to India providers, with a pointer to the full comparison in INDIA_FEATURES_AND_CONFIG.md §3. Revision notes added inline so the change is traceable.

### V2 — Document 1's data provider table was entirely US
**Where:** ARCHITECTURE_RESEARCH.md §3.1
**Problem:** Polygon.io, Databento, IEX Cloud, Finnhub — none serve Indian retail equities at these price points. The table also implied an independent data-vendor market that does not exist in India, where data comes bundled with the broker API.
**Fix:** Replaced with the India table (Angel One, Fyers, Zerodha, Dhan, Upstox, NSE Bhavcopy) and a note explaining the structural difference.

### V3 — Regulatory framing was the US PDT rule
**Where:** ARCHITECTURE_RESEARCH.md §10.1, §14, §18 glossary
**Problem:** Three separate places discussed FINRA's Pattern Day Trader $25,000 rule and its 2026 phase-out. This is a US rule with **no application whatsoever** to Indian markets. The actual binding constraints — SEBI's algo framework, peak margin rules, per-stock square-off deadlines — were absent from Document 1 entirely.
**Fix:** §10.1 rewritten around SEBI peak margin and square-off deadlines. §14 rewritten around the SEBI algo framework, the advisory boundary, and Indian taxation. PDT removed from the glossary and replaced with India-relevant terms (ASM/GSM, T2T, CAS, MWPL, OPS) plus the new statistical terms (DSR, PBO).

### V4 — Open decision asked the wrong question
**Where:** ARCHITECTURE_RESEARCH.md §16.2
**Problem:** "Alpaca alone, or Alpaca + IBKR from day one?" — a question with no valid answer for this project.
**Fix:** Reframed as "Angel One alone, or Angel One + Fyers for redundancy?" Also noted that §16.1 (universe scope) has since been answered by INDIA_FEATURES_AND_CONFIG.md §5.1.

---

## 3. Medium findings — contradictions between documents

### V5 — Backtesting framework contradiction
**Conflict:** Document 1 §11 recommended Backtrader / Zipline / QuantConnect / PyBroker. Document 3 §17 (D6) selected a **custom replay harness** and gave a specific reason: strategies are pure functions over a snapshot, so replaying real snapshots tests the exact production code, whereas a third-party framework requires reimplementing every strategy — and the reimplementation is where backtest/live divergence hides.
**Fix:** Document 1 §11 now records the decision and the reasoning, retaining the framework survey as context. PyBroker is credited as a methodology reference, which the Strategy Engine adopts.

### V6 — Message bus contradiction
**Conflict:** Document 1 §12 suggested "Kafka and/or Redis." Document 3 §3.1 explicitly **rejected Kafka** with benchmark figures (Redis Streams ~0.8 ms p99 vs Kafka ~12.5 ms) and an operational-complexity argument.
**Fix:** Document 1 §12 now states the Redis-only decision with the reasoning, and notes the Streams-vs-Pub/Sub distinction that makes it durable enough.

### V7 — Three incompatible phase numbering schemes
**Conflict:** Document 1 §17 used Phase 0–6. Document 2 §10 used Phase 1–7. Document 4 §12.1 used Phase 0–8 with week estimates. The same phase number meant different work in each document, and cross-references between them were therefore misleading.
**Fix:** Document 4 §12.1 declared the single authoritative plan. Document 1 §17 now points to it and retains only the sequencing *principle* (quant before AI, alert before paper, paper before approval, approval before autonomy). Document 2 §10 marked as a mirror.

### V8 — Indicator library guidance drifted
**Conflict:** Document 1 §5.1 recommended prototyping with pandas-ta. Document 3 §3.1 refined this: TA-Lib's streaming C API for the hot path, pandas-ta for backtesting/research only, never in the live path.
**Fix:** Document 3's position is the operative one; Document 1's recommendation is compatible in spirit (it says to profile before optimizing) and is left as written, since it correctly describes the prototyping stage.

### V9 — Confidence threshold ambiguity
**Conflict:** Document 2 config sets `ai.confidence.min_to_act: 0.65`. Document 4's autonomy envelope sets `min_ai_confidence: 0.70`.
**Assessment:** These are **not** in conflict — they are different gates. 0.65 is the threshold to act *at all*; 0.70 is the threshold to act *without escalating to the human*. A trade at confidence 0.67 is valid but escalates at autonomy level L3.
**Fix:** Documented here so a future reader doesn't "fix" one to match the other. No document change needed.

### V10 — Strategy features scattered as low-priority afterthoughts
**Where:** Document 2 §6.6 listed "walk-forward strategy re-validation" and "automatic strategy disabling" as P2 items.
**Problem:** With a strategy engine that can generate its own strategies, these are not enhancements — they are the safety controls that make the feature survivable.
**Fix:** Both are now core to STRATEGY_ENGINE.md (§5 and §8, MVP priority). Document 2's entries updated to reference it.

---

## 4. Low findings

| ID | Issue | Fix |
|---|---|---|
| **V11** | Garbled text in Document 1 §2 table: *"EOD reconciliation, backtmajor retraining"* | Corrected to "strategy re-validation and retraining," with a pointer to the Strategy Engine |
| **V12** | Document 1 §2 architecture diagram box read "(Alpaca/IBKR)" | Redrawn as "(Angel One / Fyers / Zerodha — India)" |
| **V13** | Document 1 header did not indicate it predated the India scoping | Revision note added at the top, pointing here |

---

## 5. Verified correct — no action needed

These were explicitly checked and found consistent across all documents:

| # | Check | Result |
|---|---|---|
| 1 | Model IDs (`claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4-5`) | ✅ Consistent in docs 2, 3, 4; all valid current IDs |
| 2 | Model tiering rationale (Opus pre-market, Sonnet in-session, Haiku triage) | ✅ Consistent |
| 3 | Square-off times (15:10 CAS / 15:20 non-CAS / 15:25 F&O) | ✅ Consistent in docs 2, 3, 4 |
| 4 | Order rate cap (5/sec system vs 10/sec SEBI threshold) | ✅ Consistent |
| 5 | Scoring weights sum | ✅ 0.25+0.20+0.15+0.15+0.15+0.10 = **1.00** exactly |
| 6 | Slot arithmetic | ✅ 5 slots × 20% = 100% |
| 7 | Risk arithmetic in UI mockup | ✅ 1.0% of ₹500,000 = ₹5,000/trade; ×5 slots = ₹25,000 max |
| 8 | Daily loss limit arithmetic | ✅ 3.0% of ₹500,000 = ₹15,000 |
| 9 | SEBI constraints (10 OPS, static IP, India hosting, daily re-auth, Algo-ID) | ✅ Consistent in docs 2, 3, 4 |
| 10 | "AI never sizes positions" principle | ✅ Enforced identically — `Recommendation` type has no quantity field |
| 11 | Fail-closed behaviour on every AI failure path | ✅ Consistent |
| 12 | Tax treatment (intraday = speculative, ITR-3, turnover = absolute sum of P&L) | ✅ Correct per research |
| 13 | Prompt-injection defence (sanitize → structured output → bounded score) | ✅ Consistent between docs 3 and 4 |
| 14 | Kill switch semantics (halts new entries, does not auto-liquidate) | ✅ Consistent |

---

## 6. The gap: no Strategy Engine

**Finding:** All four documents assumed a small, static, hand-configured strategy list. Document 2's config had a flat `strategy.enabled_strategies` array of three names. Document 3 §5.6 loaded them as config-declared plugins. **There was no concept of a strategy registry, versioning, lifecycle, validation, or provenance — and no mechanism for either user-authored or AI-generated strategies.**

**Resolution:** [STRATEGY_ENGINE.md](STRATEGY_ENGINE.md) — a full subsystem specification.

**The central design decision, and why:** AI-generated strategies are the most dangerous feature in this system, because unlike every other component their failure mode is unbounded *and self-reinforcing*. The research is explicit that automated strategy generation does not merely overfit — it recursively reinforces its overfitting, because an AI that observes which of its own strategies scored well on history and generates the next batch accordingly is running a feedback loop with no ground truth in it.

Two structural controls answer this:

1. **The AI never writes code.** It composes strategies from a vetted primitive library using a declarative DSL — the exact parallel of the existing "AI never computes position size" principle.
2. **A mandatory statistical gauntlet** that corrects for the number of trials attempted, using the Deflated Sharpe Ratio and the Probability of Backtest Overfitting, with purged and embargoed walk-forward validation. Backed by a permanent, append-only trial registry — because DSR is meaningless without an honest count of how much searching has been done.

**Build-order consequence:** the validation gauntlet is MVP; AI generation is Phase 2. **Build the thing that rejects bad strategies before the thing that generates them.** Turning on generation first would be building the most dangerous half of the feature first.

---

## 7. Changes applied

| Document | Changes |
|---|---|
| **ARCHITECTURE_RESEARCH.md** | v1.1 — revision note added; §2 diagram and table fixed; §3.1 replaced with India providers; §5.1 unchanged (verified compatible); §10.1 rewritten to SEBI peak margin; §10.2 replaced with India brokers; §11 backtesting decision recorded; §12 Kafka rejected; §14 rewritten to SEBI/tax; §16.2 corrected; §17 superseded by the authoritative plan; §18 glossary corrected and extended |
| **INDIA_FEATURES_AND_CONFIG.md** | Strategy engine config block added; §6.6 P2 entries updated; document set links updated |
| **LOW_LEVEL_ARCHITECTURE.md** | `strategy-svc` added to the service map; strategy tables referenced; D6 resolved; document set links updated |
| **MVP_UI_AND_LEGAL.md** | Strategy engine features added to the catalogue; `/admin/strategies` added to the page structure; document set links updated |
| **STRATEGY_ENGINE.md** | **New** — full subsystem specification |
| **VERIFICATION_REPORT.md** | **New** — this document |

---

## 8. Standing risks not resolved by this audit

Honest accounting of what remains uncertain:

1. **External facts have a shelf life.** SEBI guidance, NSE timings (CAS went live 3 August 2026 — one day before these documents), broker API terms, tax thresholds, and Anthropic's model lineup were all researched in August 2026. Re-verify before implementation; the CAS and SEBI items in particular are actively moving.
2. **No code exists yet**, so no claim here has been tested against a running system. Latency budgets, cost estimates, and validation thresholds are informed estimates awaiting measurement.
3. **The strategy validation thresholds are literature defaults, not tuned values.** PBO < 0.5 and DSR confidence > 0.95 are reasonable starting points, but the right values for this system's data and instruments are an empirical question the trial registry will eventually answer.
4. **The gap between "the software works" and "the strategy makes money" is not addressed by any amount of design.** Only a paper-trading track record across multiple market regimes can close it.

---

---

## 9. Second audit — 2026-08-04 (post-Zerodha, post-scaffold)

A second full pass covering the code scaffold, which did not exist at the first
audit. Static scan, behavioural probing, container posture, and document
cross-check. **Two live bugs found and fixed** — both in code that reads
correctly but did not behave as written.

### B1 — CRITICAL: OHLC bar validation was partially inert

`Bar._high_is_highest` and `_low_is_lowest` were Pydantic **field** validators.
A field validator only sees fields declared *before* it, so when `high` was
validated, `low` and `close` did not yet exist — those comparisons silently did
nothing.

**Consequence:** a bar with `close > high` or `close < low` validated
successfully and would have reached the indicator engine. That is precisely the
corrupt-bar class the validation existed to prevent, and it would have
propagated into every downstream EMA and ATR with no error raised.

**Fix:** replaced with a `model_validator(mode="after")`, which sees all fields.
Five-case regression test added. Audited every other validator in the codebase —
all remaining cross-field logic already uses `model_validator`, so this was the
only instance.

### B2 — Log redaction: short JWTs leaked

The JWT pattern required 20+ characters after `eyJ`. Real tokens with short
headers (e.g. `eyJhbGciOiJIUzI1NiJ9`, 17 chars after the prefix) did not match
and would have been logged in full.

**Fix:** match the three-part `header.payload.signature` structure instead of
assuming a header length. Also added Bearer-token and base32 TOTP-seed patterns.
Twelve redaction paths now tested, plus false-positive guards confirming
ordinary trading text survives intact.

### Verified clean

| Area | Result |
|---|---|
| `eval` / `exec` / `__import__` / `shell=True` / `os.system` in source | None |
| YAML loading | `safe_load` everywhere; `!!python/object`, `!!python/name`, `!!python/module` all blocked |
| `float` in money or price paths | None — `Decimal` throughout, exact arithmetic verified |
| Naive `datetime` construction | None |
| SecretString leak paths | 10 tested (str, repr, f-string, format, %-format, join, exception, list, dict, pickle) — all redacted |
| Config hard bounds | Order rate, risk %, market protection, T2T filter, human approval all reject as designed |
| `Recommendation` sizing surface | No quantity/size/capital/stop fields; `extra="forbid"` blocks injection |
| Square-off deadlines | Ours precedes broker's for every stock class |
| Container posture | Non-root, capabilities dropped, no privileged/host-network, `core` network `internal: true`, images pinned by tag, all ports bound to 127.0.0.1 |
| `.gitignore` coverage | `.env` ignored at any depth; secrets, keys, market data all excluded |
| Repository secret scan | Clean — no keys, no `.env`, no private keys |

### Document drift corrected

The Zerodha switch left five stale claims across the specifications:

1. Zerodha listed at "~3 orders/sec" — actually **10 OPS account-wide** per Zerodha staff.
2. Angel One still named as the primary recommendation.
3. Config examples showing `primary: angelone` and `max_orders_per_second: 5`.
4. **Market protection undocumented** — a code-breaking requirement absent from every doc.
5. **Static IP scope undocumented** — it applies to order endpoints only, which is
   architecturally useful and was not recorded.

All corrected. A new §3.3 in INDIA_FEATURES_AND_CONFIG.md holds the verified
Zerodha constraint table, and a new constraint **C9** (market protection) was
added to LOW_LEVEL_ARCHITECTURE.md §1.1.

**Test count: 60 → 112.**

### Still unresolved

- **Algo-ID attachment mechanics.** Sources disagree on whether the developer
  supplies it via the order `tag` field or the broker injects it. One line of
  code either way, but it must be confirmed with Zerodha before live.
- **`pykiteconnect` 5.1.0 lacks `market_protection`.** A plain
  `pip install kiteconnect` yields a version that cannot place compliant market
  orders. Verify the installed version before live trading.
- **NSE holiday list is an empty placeholder.** Until populated, every weekday
  is treated as a trading day.

---

*End of report.*
