# Strategy Engine Specification
## User-Authored & AI-Generated Strategies with Overfitting Defence

**Document type:** Subsystem design specification
**Status:** Design complete, pre-implementation
**Version:** 1.0 — 2026-08-04

> 📘 **New here? Start with [MASTER_REFERENCE.md](MASTER_REFERENCE.md)** — the single onboarding document covering the whole system.

**Document set:**
1. [ARCHITECTURE_RESEARCH.md](ARCHITECTURE_RESEARCH.md) — the *why*
2. [INDIA_FEATURES_AND_CONFIG.md](INDIA_FEATURES_AND_CONFIG.md) — the *what* (market rules, config)
3. [LOW_LEVEL_ARCHITECTURE.md](LOW_LEVEL_ARCHITECTURE.md) — the *how* (services, schemas, security)
4. [MVP_UI_AND_LEGAL.md](MVP_UI_AND_LEGAL.md) — scope, screens, law
5. **This document** — the strategy lifecycle: authoring, AI generation, validation, promotion, retirement
6. [VERIFICATION_REPORT.md](VERIFICATION_REPORT.md) — cross-document audit
7. [PRE_LIVE_CHECKLIST.md](PRE_LIVE_CHECKLIST.md) — the consolidated gate before real capital

---

## Table of Contents

| § | Section |
|---|---|
| 1 | The Requirement and the Danger |
| 2 | Design Principles |
| 3 | The Strategy DSL — strategies are data, not code |
| 4 | Strategy Lifecycle & State Machine |
| 5 | **The Validation Gauntlet** (overfitting defence) |
| 6 | The Trial Registry |
| 7 | AI Strategy Generation |
| 8 | Continuous Monitoring & Auto-Retirement |
| 9 | Runtime Integration |
| 10 | Data Model |
| 11 | Configuration |
| 12 | User Interface |
| 13 | Security Considerations |
| 14 | Feature Priorities & Build Sequence |

---

## 1. The Requirement and the Danger

### 1.1 What was asked for

Two strategy sources, running simultaneously:

1. **User-authored** — you define a strategy and the system trades it.
2. **AI-generated** — the AI continuously monitors markets and stocks, and *from previous experience* automatically generates and configures strategies, saves them to the system, and uses them accordingly.

### 1.2 Why this is the most dangerous feature in the entire system

Every other component in this platform has a bounded failure mode. A bad tick corrupts one indicator. A prompt injection shifts one sentiment score by a bounded amount. A broker timeout is resolved by reconciliation. **Automated strategy generation has an unbounded failure mode, and worse, a self-reinforcing one.**

The research is unambiguous on this. Automated strategy generation produces hundreds or thousands of candidates and ranks them by performance — and **backtest optimizers that search for parameter combinations maximizing historical performance are, by construction, overfitting machines**. Reporting only the winners is selection bias, and not controlling for the number of trials produces systematically over-optimistic expectations.

The automation makes it worse in a specific way: *the system does not just overfit — it recursively reinforces its overfitting.* An AI that generates strategies, observes which ones scored well on history, and uses that observation to generate the next batch is running a feedback loop with no ground truth in it. It will converge, confidently, on noise.

**Concretely, here is how this feature destroys an account if built naively:**

1. AI generates 200 strategy variants over three months.
2. It backtests each and keeps the ones with Sharpe > 2.
3. Roughly 10 pass. They look excellent.
4. But with 200 trials against noisy financial data, **you would expect several apparent Sharpe > 2 results from pure chance alone.**
5. Those strategies go live. They have no edge. They lose money at exactly the rate their overfitting predicts.
6. The AI, observing the losses, generates more strategies to compensate — and the loop tightens.

The entire design below exists to make that sequence structurally impossible.

### 1.3 The two governing decisions

**Decision 1 — The AI never writes executable code.** It composes strategies from a vetted primitive library using a declarative DSL. This is the exact parallel of the platform's existing principle that the LLM never computes position size (LLD §1.1, C4): *the AI expresses intent in a constrained vocabulary; deterministic code interprets it.*

**Decision 2 — Statistical validation is mandatory, automated, and unbypassable.** No strategy from any source reaches live capital without passing a gauntlet that explicitly corrects for the number of trials attempted. Promotion to live is additionally a human decision.

---

## 2. Design Principles

| # | Principle | Consequence |
|---|---|---|
| P1 | **Strategies are declarative data, never code** | No `eval`, no `exec`, no dynamic import, no AI-authored Python. Composition from a closed primitive library only |
| P2 | **Hypothesis before results** | The AI must state an economic mechanism *before* seeing any backtest output. A strategy with no plausible mechanism is data mining, and this ordering makes that detectable |
| P3 | **Every trial is counted, forever** | The trial registry is global and permanent. Statistical corrections are meaningless without an honest denominator |
| P4 | **The holdout is sacred** | A locked out-of-sample period the generator never sees, used exactly once per strategy. Touching it twice invalidates it |
| P5 | **Generation is bounded and paced** | Weekly cadence, capped proposals per cycle. Prevents the search-until-something-passes failure |
| P6 | **Promotion to live is always human** | Consistent with MVP doc §3.3. The AI can propose, validate, shadow, and paper-trade — it cannot arm itself |
| P7 | **Degradation auto-retires** | A strategy that stops matching its validated expectation is demoted automatically, without waiting for a human |
| P8 | **Provenance is permanent** | Every strategy records its origin, hypothesis, validation evidence, and full trade history for its lifetime |

---

## 3. The Strategy DSL — Strategies Are Data

### 3.1 Shape

A strategy is a YAML/JSON document validated against a Pydantic schema. It references only **vetted primitives** from a closed library.

```yaml
id: orb_highvol_v3
name: "Opening Range Breakout — High Volatility Regime"
version: 3
origin: AI_PROPOSED_JOURNAL          # USER_AUTHORED | AI_PROPOSED_OBSERVATION | AI_PROPOSED_JOURNAL
created_at: 2026-07-14T18:22:00Z
parent_id: orb_highvol_v2            # lineage, if evolved from another

# --- Required BEFORE any backtest is run (principle P2) ---
hypothesis:
  mechanism: >
    On high-VIX days the opening range is proportionally wider, so a breakout
    clearing it represents a larger commitment of capital than on a calm day.
    Institutional participation concentrated in the first hour means such
    breakouts have follow-through rather than immediately mean-reverting.
  why_it_should_persist: >
    This is a structural consequence of how intraday volatility scales with
    participation, not a calendar or symbol-specific artefact. It should not
    arbitrage away because it reflects genuine risk-taking, not a pricing error.
  expected_failure_mode: >
    Fails in high-VIX-but-rangebound conditions (elevated fear, no direction),
    where breakouts reverse. The regime filter below is intended to exclude this.

applicability:
  regimes:    [HIGH_VOL, TRENDING]
  timeframe:  5m
  min_price:  100
  sectors:    ANY

entry:
  all_of:
    - primitive: price_breaks_level
      params: {level: opening_range_high, buffer_pct: 0.05, direction: above}
    - primitive: volume_ratio_above
      params: {window: 20, threshold: 1.5}
    - primitive: timeframe_agreement_at_least
      params: {count: 2, of: [1h, 1d, 1w]}
    - primitive: india_vix_between
      params: {min: 15, max: 28}
    - primitive: index_not_opposing
      params: {index: NIFTY, tolerance_pct: 0.3}

exit:
  stop:   {primitive: atr_stop,            params: {multiplier: 1.5}}
  target: {primitive: r_multiple_target,   params: {r: 2.0}}
  trail:  {primitive: trail_after_r,       params: {activate_at_r: 1.0, atr_mult: 1.0}}
  time:   {primitive: squareoff_deadline}          # always present, non-removable

constraints:
  max_entries_per_day: 1
  entry_window: ["09:30", "11:00"]
  min_bars_since_open: 3
```

### 3.2 The primitive library

Primitives are hand-written, unit-tested Python functions with declared parameter schemas and bounds. **The AI may only reference primitives that exist; it cannot define new ones.** Adding a primitive is a human code change with review.

| Category | Examples |
|---|---|
| **Price/level** | `price_breaks_level`, `price_within_pct_of_level`, `price_rejects_level`, `gap_from_prev_close` |
| **Trend** | `ma_slope_positive`, `price_above_ma`, `ma_crossover`, `adx_above` |
| **Momentum** | `rsi_between`, `rsi_divergence`, `macd_histogram_sign`, `roc_above` |
| **Volatility** | `atr_pct_between`, `bollinger_position`, `volatility_expanding` |
| **Volume** | `volume_ratio_above`, `volume_trend`, `obv_confirming` |
| **Multi-timeframe** | `timeframe_agreement_at_least`, `higher_tf_trend_is` |
| **Market context** | `india_vix_between`, `regime_is`, `index_not_opposing`, `sector_rank_top_n`, `fii_flow_sign` |
| **News** | `news_score_above`, `no_material_news`, `event_type_absent` |
| **Time** | `within_window`, `min_bars_since_open`, `bars_until_squareoff_above` |
| **Exit** | `atr_stop`, `structure_stop`, `r_multiple_target`, `trail_after_r`, `squareoff_deadline` |

**Compiler guarantees.** The `StrategyCompiler` validates every primitive name against the registry, every parameter against its declared type and bounds, structural sanity (an entry needs at least one condition; exits must include a stop and a time exit), and applicability coherence. It emits a callable `Strategy` object satisfying the existing `Strategy` protocol (LLD §5.6) — so **the runtime does not know or care whether a strategy was written by a human or composed by an AI.** They are the same type.

Two exit primitives are **non-removable** by any strategy from any source: `atr_stop` (or another stop primitive) and `squareoff_deadline`. A strategy cannot express "no stop" or "hold past the deadline" because the DSL has no way to say it.

### 3.3 Why declarative rather than generated code

| Concern | Generated Python | Declarative DSL |
|---|---|---|
| Arbitrary code execution | Requires sandboxing; sandbox escapes are a real class of vulnerability | **Structurally impossible** — there is no code to execute |
| Auditability | Must read and understand arbitrary code | Reads as a specification; diffable |
| Failure modes | Unbounded (infinite loops, memory, syscalls) | Bounded by the primitive library |
| Prompt-injection blast radius | Attacker could reach code execution | Attacker can at most compose a bad-but-valid strategy, which still faces the validation gauntlet |
| SEBI "white box" alignment | Poor — opaque generated logic | Good — the strategy *is* its own documentation |

The last row matters practically: SEBI's framework treats transparent, rule-based algorithms more favourably than black boxes. A declarative strategy specification is about as white-box as an algorithm gets.

---

## 4. Strategy Lifecycle & State Machine

```
                    ┌─────────┐
   user authors ───►│  DRAFT  │◄─── AI proposes
                    └────┬────┘
                         │ submit
                         ▼
                   ┌───────────┐
                   │VALIDATING │  ← the gauntlet (§5). Automated, unbypassable.
                   └─────┬─────┘
                  fail   │   pass
              ┌──────────┴──────────┐
              ▼                     ▼
        ┌──────────┐          ┌──────────┐
        │ REJECTED │          │  SHADOW  │  evaluates live, records what it
        │(archived,│          │          │  would have done. Places no orders.
        │ counted) │          └────┬─────┘
        └──────────┘               │ ≥ N sessions, live ≈ backtest
                                   ▼
                             ┌──────────┐
                             │  PAPER   │  full execution path, paper capital
                             └────┬─────┘
                                  │ ≥ M trades, positive expectancy
                                  ▼
                            ┌─────────────┐
                            │AWAITING_    │  ◄── human approval gate (P6)
                            │APPROVAL     │
                            └──────┬──────┘
                                   │ human approves
                                   ▼
                             ┌──────────┐
                    ┌───────►│  ACTIVE  │  live capital
                    │        └────┬─────┘
         recovers   │             │ performance degrades
                    │             ▼
                    │       ┌──────────┐
                    └───────│ DEGRADED │  size reduced, monitored
                            └────┬─────┘
                                 │ continues degrading
                                 ▼
                           ┌──────────┐      ┌─────────────┐
                           │ RETIRED  │      │ QUARANTINED │ ← emergency, any state
                           └──────────┘      └─────────────┘
```

### 4.1 Gate requirements

| Transition | Requirement |
|---|---|
| `DRAFT → VALIDATING` | Schema valid, compiles, hypothesis present and non-empty |
| `VALIDATING → SHADOW` | **All gauntlet checks pass** (§5) |
| `VALIDATING → REJECTED` | Any check fails. Recorded in the trial registry regardless |
| `SHADOW → PAPER` | ≥ 20 shadow sessions; live-vs-backtest signal agreement ≥ 80%; no execution-feasibility problems (signals at untradeable prices, etc.) |
| `PAPER → AWAITING_APPROVAL` | ≥ 30 paper trades; positive expectancy after realistic costs; max drawdown within limits |
| `AWAITING_APPROVAL → ACTIVE` | **Human approval only.** Never automatic, at any autonomy level |
| `ACTIVE → DEGRADED` | Automatic on degradation triggers (§8) |
| `DEGRADED → RETIRED` | Automatic on continued degradation, or human decision |
| `* → QUARANTINED` | Emergency: anomalous behaviour, suspected data issue, or kill-switch event |

**SHADOW is the most underrated state.** A strategy in shadow mode consumes live market data and records every signal it would have generated, with the price and time — but places no orders. This catches the class of failure a backtest cannot: signals that fire at prices you could never actually get, signals that fire after the move already happened, and signals whose live frequency differs wildly from backtest frequency. It costs nothing and runs in parallel with everything else.

---

## 5. The Validation Gauntlet

This is the heart of the design. Every check is automated and mandatory. There is no override flag, for any strategy origin, including user-authored ones.

### 5.1 The checks

| # | Check | Method | Fail condition |
|---|---|---|---|
| **G1** | **Hypothesis present** | Non-empty mechanism, persistence rationale, and expected failure mode, recorded *before* backtest execution | Missing or generic boilerplate |
| **G2** | **Compiles and is well-formed** | Primitive validation, bounds, structural sanity | Any error |
| **G3** | **Minimum sample** | Trade count across the full backtest | < 100 trades (configurable). Below this, nothing is statistically meaningful |
| **G4** | **Realistic cost modelling** | STT, brokerage, GST, exchange charges, stamp duty, plus slippage and market impact modelled per India's actual charge structure | Not applicable — costs are always applied; a strategy profitable only gross is caught by G6 |
| **G5** | **Purged & embargoed walk-forward** | Combinatorial Purged Cross-Validation (CPCV). Train/test blocks with purging of overlapping-label samples and an embargo period after each test block | Fails if performance is not consistent across folds |
| **G6** | **Deflated Sharpe Ratio** | DSR corrects the observed Sharpe for selection bias, the number of trials, sample length, skew and kurtosis of returns | DSR ≤ 0.95 confidence of being genuinely > 0 |
| **G7** | **Probability of Backtest Overfitting** | PBO via combinatorially symmetric cross-validation | PBO > 0.5 — i.e. more likely than not that the in-sample ranking does not survive out-of-sample |
| **G8** | **Regime coverage** | Backtest must span ≥ 2 distinct market regimes with acceptable performance in each | Profitable in only one regime → applicability must be narrowed to that regime, or rejected |
| **G9** | **Locked holdout** | A held-out period the generator has never seen, evaluated exactly once | Performance degrades materially versus validation |
| **G10** | **Correlation to existing strategies** | Return-stream correlation against all ACTIVE strategies | ρ > 0.8 — it adds concentration, not diversification |
| **G11** | **Parameter sensitivity** | Perturb every parameter ±20%; performance must degrade smoothly | A sharp cliff means the parameters are fitted to noise |
| **G12** | **India tradability** | Signals respect T2T/ASM/GSM exclusions, circuit bands, square-off deadlines, and liquidity floors | Any violation |

### 5.2 Why G6 and G7 are the load-bearing checks

Sharpe ratio is the near-universal strategy metric and it is **systematically inflated by search**. If you try enough strategies, some will show a high Sharpe from chance alone. The **Deflated Sharpe Ratio** exists precisely to correct for this: it adjusts the observed Sharpe for the number of trials, the sample length, and the non-normality of returns, producing the probability that the true Sharpe exceeds zero.

**DSR requires an honest trial count.** This is why §6's trial registry is not optional bookkeeping — it is the input that makes G6 meaningful. A system that runs 500 backtests and reports the Sharpe of the best one without deflating it is not doing statistics; it is reporting the maximum of 500 random draws.

**PBO** answers a complementary question: given how this strategy was selected, what is the probability its in-sample superiority fails to carry out-of-sample? Above 0.5, the selection process is worse than a coin flip, which is a clear signal to reject.

Research comparing out-of-sample methods finds **Combinatorial Purged Cross-Validation superior at mitigating overfitting**, producing both lower PBO and better DSR statistics than simpler splits — hence G5's specific method.

### 5.3 Purging and embargo — a subtle but decisive detail

Standard k-fold cross-validation **leaks** on financial time series. If a trade's outcome is determined over several bars, a training sample whose outcome window overlaps a test sample gives the model information it could not have had. **Purging** removes training samples whose label windows overlap the test set; **embargo** additionally drops a buffer immediately after each test block, because serial correlation means adjacent samples are near-duplicates.

Without purging and embargo, a walk-forward test reports optimistic numbers and looks rigorous while being wrong. This is the most common way a careful-seeming validation still fails.

### 5.4 What happens on failure

A rejected strategy is **not deleted.** It is archived with its full validation report and — critically — **counted in the trial registry**. Deleting failures would corrupt the trial count and inflate every future DSR calculation. The record of what failed is as statistically important as the record of what passed.

---

## 6. The Trial Registry

```sql
CREATE TABLE strategy_trial (
    id                  BIGSERIAL PRIMARY KEY,
    trial_ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
    strategy_id         VARCHAR(64) NOT NULL,
    strategy_hash       VARCHAR(64) NOT NULL,   -- content hash of the compiled DSL
    origin              VARCHAR(32) NOT NULL,
    generation_batch_id UUID,                   -- groups one AI generation cycle
    observed_sharpe     NUMERIC(8,4),
    deflated_sharpe     NUMERIC(8,4),
    pbo                 NUMERIC(5,4),
    trade_count         INTEGER,
    outcome             VARCHAR(16) NOT NULL,   -- PASSED | REJECTED
    failed_check        VARCHAR(16),            -- G1..G12
    report              JSONB NOT NULL
);
CREATE INDEX ON strategy_trial (strategy_hash);
CREATE INDEX ON strategy_trial (trial_ts DESC);
```

**Rules:**
- Every gauntlet run writes a row. No exceptions, no deletions.
- **Parameter sweeps count individually.** Testing one strategy at 20 parameter combinations is 20 trials, not one. This is the single most commonly violated rule in retail quant work and the one that most inflates results.
- The `strategy_hash` deduplicates genuinely identical re-runs while still counting distinct variants.
- The **effective trial count** for DSR is the count of distinct hashes within the relevant search family.

The registry is also the honest answer to "how much searching have we done?" — a number that should be visible in the admin UI and that should make you uncomfortable when it grows fast.

---

## 7. AI Strategy Generation

### 7.1 Two generation modes

**Mode A — Observation-driven (`AI_PROPOSED_OBSERVATION`).** A weekly job feeds the top-tier model a structured summary of recent market behaviour: which setups recurred, which stocks moved and under what conditions, how regimes shifted, which patterns preceded moves. The model proposes strategies that would have captured recurring, *mechanistically explicable* behaviour.

**Mode B — Journal-driven (`AI_PROPOSED_JOURNAL`).** This is the "from previous experience" requirement. The model receives the trade journal — every trade taken, its setup type, regime, AI confidence, outcome, R-multiple, MFE/MAE, and whether the thesis held — and proposes either refinements to existing strategies or new ones addressing observed failure patterns.

Journal-driven generation is the more valuable of the two, because it operates on *the system's own realized outcomes* rather than on history it might be pattern-matching by chance. It is also better grounded: "this setup failed 7 of 9 times when VIX was above 25" is an observation about the system's actual behaviour, not a discovered correlation in a price series.

### 7.2 The hypothesis-first protocol

This is the strongest cheap control available, and the ordering is the entire point:

```
1. AI receives market/journal observations. It does NOT receive backtest results.
2. AI proposes a strategy AND states:
     - the economic mechanism
     - why it should persist rather than arbitrage away
     - the conditions under which it should fail
3. Hypothesis is written to the database and FROZEN.
4. Only then does the gauntlet run.
5. Results are compared against the stated expected failure mode.
```

A strategy whose author can articulate why it should work, and whose realized failures match its predicted failures, is meaningfully different from one that merely scored well. This ordering makes the difference visible instead of leaving it to judgement.

If the AI cannot state a mechanism, it cannot propose the strategy. The schema requires the field, and a generic non-answer fails G1.

### 7.3 Bounded generation

```yaml
strategy_engine:
  ai_generation:
    enabled: true
    cadence: weekly              # NOT continuous — see below
    run_on: "SAT 10:00"
    max_proposals_per_cycle: 5
    max_active_ai_strategies: 3
    model: claude-opus-5
    modes: [observation, journal]
    require_hypothesis: true      # cannot be disabled
    min_journal_trades: 50        # won't run journal mode without enough history
```

**Why weekly, not continuous.** The brief asks for continuous monitoring, and the system does monitor continuously — the *observation collection* runs all week. But **generation** is deliberately batched, because continuous generation is exactly the recursive-overfitting failure the research warns about. Each generation cycle adds to the global trial count and therefore raises the DSR bar for every future strategy. Generating continuously would inflate the trial count until nothing could pass — which is the statistically correct outcome, but a badly designed way to reach it.

Capping proposals per cycle bounds the multiple-comparisons problem at its source rather than trying to correct for it afterwards.

### 7.4 What the AI cannot do

| Cannot | Enforced by |
|---|---|
| Write or execute code | DSL only; no code path exists |
| Define new primitives | Registry is a human-reviewed code artefact |
| Bypass any gauntlet check | No override flag exists in the schema |
| See the locked holdout | Holdout data is not in the generation context |
| Promote a strategy to live | State machine requires human approval |
| Modify risk limits or sizing | Out of scope of the DSL entirely |
| Remove a stop or the time exit | Non-removable exit primitives |
| Delete or amend trial records | Append-only table, insert-only DB role |

---

## 8. Continuous Monitoring & Auto-Retirement

A validated strategy is not permanently valid. Markets change; edges decay.

### 8.1 Degradation triggers

Each ACTIVE strategy is monitored against its own validated expectation:

```yaml
degradation:
  rolling_window_trades: 30
  triggers:
    sharpe_below_pct_of_backtest: 50      # realized < half of validated
    consecutive_losses: 6
    drawdown_exceeds_backtest_max: true   # worse than anything in validation
    win_rate_below_pct_of_backtest: 60
    regime_no_longer_applicable: true     # its regime hasn't occurred in 60 sessions
  on_degrade:
    action: reduce_size                   # ACTIVE → DEGRADED, 50% size
    notify: true
  on_continued_degrade:
    action: retire                        # DEGRADED → RETIRED
    after_trades: 15
```

### 8.2 Why automatic demotion matters more than automatic promotion

The system deliberately requires a human to promote a strategy to live capital but **allows it to demote one without asking**. That asymmetry is intentional: the cost of a wrongly-demoted good strategy is a missed opportunity; the cost of a wrongly-retained bad one is money. Where the costs are asymmetric, the automation should be too.

This mirrors the autonomy ladder's auto-demotion rule (MVP doc §3.2) — the same principle applied at strategy granularity rather than system granularity.

### 8.3 Live-vs-backtest divergence reporting

For every ACTIVE strategy, the system continuously compares realized statistics against the validated backtest: win rate, average R, trade frequency, average holding time, slippage versus modelled. **Divergence in trade frequency is the earliest warning sign** — it usually means market conditions have shifted out of the strategy's applicable regime before the P&L has degraded enough to notice.

---

## 9. Runtime Integration

### 9.1 How this changes `signal-engine`

LLD §5.6 currently loads strategies as config-declared plugin classes. That becomes a registry lookup:

```python
class SignalEngine:
    registry: StrategyRegistry          # replaces the static plugin list

    async def on_bar(self, bar: Bar) -> None:
        symbol = bar.symbol
        if symbol not in self.plan.active_symbols:
            return
        snapshot = await self.load_snapshot(symbol)
        if not snapshot.all_ready:
            return

        # ACTIVE strategies place orders; SHADOW/PAPER strategies record only.
        for strategy in self.registry.runnable_for(
            regime=self.context.regime, timeframe=bar.timeframe
        ):
            trigger = strategy.evaluate(snapshot, self.plan.playbook_for(symbol))
            if trigger is None:
                continue
            await self.route(strategy, trigger, snapshot)
```

`route()` dispatches by strategy state: ACTIVE proceeds to AI review and the risk engine; PAPER goes to the paper execution path; SHADOW is recorded to `shadow_signal` and goes no further. **The risk engine is unchanged and unaware of strategy state** — it evaluates any recommendation reaching it identically, which preserves the single-path property that makes it testable.

### 9.2 Regime-aware selection

Strategies declare their applicable regimes. `runnable_for()` filters accordingly, so a mean-reversion strategy validated in low-volatility conditions simply is not evaluated on a high-VIX day. This is a meaningful improvement over the flat always-on strategy list in the current config — and it means the AI's regime-specific proposals are actually used regime-specifically.

### 9.3 Slot competition

When multiple strategies fire on the same symbol, or more strategies fire than there are free slots, priority is: Tradeability Score first, then the strategy's own validated expectancy, then registration order as a deterministic tiebreak. Recorded in the audit log so the choice is explainable.

---

## 10. Data Model

```sql
CREATE TABLE strategy (
    id                  VARCHAR(64) PRIMARY KEY,
    name                VARCHAR(128) NOT NULL,
    version             INTEGER      NOT NULL,
    parent_id           VARCHAR(64) REFERENCES strategy(id),
    origin              VARCHAR(32)  NOT NULL,
    state               VARCHAR(24)  NOT NULL,
    dsl                 JSONB        NOT NULL,   -- the full strategy document
    dsl_hash            VARCHAR(64)  NOT NULL,
    hypothesis          JSONB        NOT NULL,   -- frozen before validation (P2)
    hypothesis_frozen_at TIMESTAMPTZ NOT NULL,
    applicable_regimes  TEXT[]       NOT NULL,
    created_at          TIMESTAMPTZ  NOT NULL,
    created_by          VARCHAR(64)  NOT NULL,   -- 'user' | model id
    approved_by         VARCHAR(64),             -- NULL until human approval
    approved_at         TIMESTAMPTZ,
    state_changed_at    TIMESTAMPTZ  NOT NULL,
    retirement_reason   TEXT
);

CREATE TABLE strategy_validation (
    id                  BIGSERIAL PRIMARY KEY,
    strategy_id         VARCHAR(64) NOT NULL REFERENCES strategy(id),
    run_at              TIMESTAMPTZ NOT NULL,
    passed              BOOLEAN     NOT NULL,
    checks              JSONB       NOT NULL,   -- G1..G12 individual results
    observed_sharpe     NUMERIC(8,4),
    deflated_sharpe     NUMERIC(8,4),
    pbo                 NUMERIC(5,4),
    trial_count_at_run  INTEGER     NOT NULL,   -- the DSR denominator, recorded
    trade_count         INTEGER,
    max_drawdown_pct    NUMERIC(6,3),
    regimes_covered     TEXT[],
    holdout_result      JSONB,
    equity_curve        JSONB
);

CREATE TABLE strategy_performance (          -- rolling live/paper/shadow stats
    strategy_id         VARCHAR(64) NOT NULL REFERENCES strategy(id),
    as_of               DATE        NOT NULL,
    state               VARCHAR(24) NOT NULL,
    trades              INTEGER     NOT NULL,
    wins                INTEGER     NOT NULL,
    realized_pnl        NUMERIC(14,2),
    avg_r               NUMERIC(6,3),
    realized_sharpe     NUMERIC(8,4),
    vs_backtest_ratio   NUMERIC(6,3),        -- realized ÷ validated — the key number
    PRIMARY KEY (strategy_id, as_of)
);

CREATE TABLE shadow_signal (                 -- SHADOW-state signals, never executed
    id                  BIGSERIAL PRIMARY KEY,
    strategy_id         VARCHAR(64) NOT NULL REFERENCES strategy(id),
    symbol_id           INTEGER     NOT NULL REFERENCES instruments(id),
    signalled_at        TIMESTAMPTZ NOT NULL,
    direction           VARCHAR(5)  NOT NULL,
    price_at_signal     NUMERIC(14,4) NOT NULL,
    hypothetical_stop   NUMERIC(14,4) NOT NULL,
    hypothetical_outcome JSONB               -- filled by EOD evaluation
);
```

Plus `strategy_trial` from §6.

---

## 11. Configuration

```yaml
strategy_engine:
  enabled: true

  registry:
    max_active: 6
    max_active_per_regime: 3
    max_shadow: 10

  validation:
    min_trades: 100
    min_regimes: 2
    max_pbo: 0.5
    min_deflated_sharpe_confidence: 0.95
    max_correlation_to_active: 0.8
    parameter_sensitivity_pct: 20
    holdout_months: 6                  # never seen by the generator
    walk_forward:
      method: cpcv                     # combinatorial purged CV
      n_splits: 8
      embargo_pct: 2.0
    costs:
      brokerage_per_order: 20
      stt_pct: 0.025
      gst_pct: 18
      exchange_charges_pct: 0.00345
      stamp_duty_pct: 0.003
      slippage_bps: 5
      impact_model: linear

  promotion:
    shadow_min_sessions: 20
    shadow_min_agreement_pct: 80
    paper_min_trades: 30
    paper_min_expectancy_r: 0.15
    require_human_approval: true       # cannot be disabled

  ai_generation:
    enabled: true
    cadence: weekly
    run_on: "SAT 10:00"
    max_proposals_per_cycle: 5
    max_active_ai_strategies: 3
    model: claude-opus-5
    modes: [observation, journal]
    require_hypothesis: true
    min_journal_trades: 50

  degradation:
    rolling_window_trades: 30
    sharpe_below_pct_of_backtest: 50
    consecutive_losses: 6
    win_rate_below_pct_of_backtest: 60
    on_degrade: reduce_size
    on_continued_degrade: retire
    continued_degrade_after_trades: 15
```

**Hard-coded bounds (config cannot exceed these — LLD §10.11 pattern):** `min_trades ≥ 50`, `max_pbo ≤ 0.6`, `max_active ≤ 12`, `require_human_approval` is not overridable, and `require_hypothesis` is not overridable.

---

## 12. User Interface

### 12.1 Strategy list (`/admin/strategies`)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  STRATEGIES                          6 active · 3 shadow · 1 awaiting you    │
│                                              [+ New strategy]  [Trial log]   │
├──────────────────────────────────────────────────────────────────────────────┤
│  ⚠ 1 strategy awaits your approval · 1 degraded                              │
├──────────────────────────────────────────────────────────────────────────────┤
│  Name                    Origin   State      DSR   Trades  vs BT   Regime    │
│  ─────────────────────────────────────────────────────────────────────────── │
│  ORB Classic             User     ● ACTIVE   1.42    247   1.02    ANY       │
│  Trend Continuation MTF  User     ● ACTIVE   1.18    189   0.94    TRENDING  │
│  ORB High-Vol v3         AI-Jrnl  ● ACTIVE   1.31     64   1.11    HIGH_VOL  │
│  S/R Bounce Low-Vol      AI-Obs   ◐ DEGRADED 1.09     41   0.38 ▼  LOW_VOL   │
│  Gap-Fade v2             AI-Obs   ◑ PAPER    1.22     28   1.05    ANY       │
│  Volume Thrust           AI-Jrnl  ○ SHADOW   1.15     12     —     TRENDING  │
│  Sector Rotation Momo    AI-Obs   ⏸ AWAITING 1.51      —     —     TRENDING  │
│  RSI Divergence v1       AI-Obs   ✕ REJECTED 0.71      —     —     —         │
│                                              └ failed G7: PBO 0.63           │
└──────────────────────────────────────────────────────────────────────────────┘
```

`vs BT` — realized performance divided by validated backtest — is the single most important column. A value near 1.0 means the strategy is behaving as validated. The `0.38 ▼` on the degraded row is the number that triggered demotion.

### 12.2 Strategy detail

Four tabs: **Definition** (the DSL, human-readable, with a diff against the parent version), **Hypothesis** (the frozen mechanism, with realized failures compared against predicted ones), **Validation** (every gauntlet check with its numbers, the equity curve, fold-by-fold walk-forward results, holdout result, and the trial count at validation time), and **Live Performance** (realized versus backtest, trade list, degradation status).

### 12.3 Approval screen

When a strategy reaches `AWAITING_APPROVAL`, the notification and screen present: the hypothesis in plain language, the validation summary with DSR and PBO stated explicitly, the shadow and paper records, correlation to existing active strategies, and the proposed initial allocation. Approval requires re-authentication (MVP doc §11) and typing the strategy name — deliberately more friction than a single tap, because this is the gate between the AI's proposals and your money.

### 12.4 Trial log

A running view of the trial registry: total trials, trials this month, pass rate, and the current DSR threshold implied by the trial count. Its purpose is to make the search cost visible. **If the trial count is climbing quickly, the bar for every future strategy is rising — and that should be visible rather than buried in a statistic.**

### 12.5 Strategy builder (user-authored)

MVP ships a **YAML editor with live validation**: primitive autocomplete, inline bounds checking, compile-on-type, and a "quick backtest" preview (which counts as a trial, and says so before you run it). A visual block-based builder is Phase 3+ — the YAML editor is sufficient and unambiguous, and building a visual editor before the primitive library has stabilized would be premature.

---

## 13. Security Considerations

| Threat | Mitigation |
|---|---|
| **AI-generated code execution** | Structurally impossible — DSL only, no code path (P1) |
| **Prompt injection steering strategy generation** | Generation input is structured statistics from our own database, not free text. News content reaches it only as bounded `NewsSignal` fields. A successful injection could at most produce a bad-but-valid strategy, which still faces the full gauntlet |
| **Strategy DSL as an injection vector** | Strict schema validation; no string interpolation into queries or prompts; primitive names validated against an allowlist |
| **Trial registry tampering** (to inflate DSR) | Append-only; DB role has INSERT but not UPDATE/DELETE; hash-chained like the decision log |
| **Holdout contamination** | Holdout data is physically excluded from the generation context; access is logged; a second read of the same holdout for the same strategy is rejected |
| **Approval bypass** | Human approval enforced in the state machine, not in config; requires re-authentication |
| **Resource exhaustion via validation** | Gauntlet runs in a resource-limited worker with timeouts; queued, never inline with trading |

---

## 14. Feature Priorities & Build Sequence

| ID | Feature | Priority | Phase |
|---|---|---|---|
| S-1 | Strategy DSL schema + compiler | ★ MVP | 4 |
| S-2 | Primitive library (~40 primitives) | ★ MVP | 4 |
| S-3 | Strategy registry + state machine | ★ MVP | 4 |
| S-4 | User-authored strategies via YAML editor | ★ MVP | 6 |
| S-5 | Backtest harness with realistic India costs | ★ MVP | 5 |
| S-6 | Trial registry | ★ MVP | 5 |
| S-7 | Gauntlet G1–G5, G8, G12 | ★ MVP | 5 |
| S-8 | **Gauntlet G6, G7 (DSR, PBO)** | ★ MVP | 5 |
| S-9 | Shadow mode | ★ MVP | 5 |
| S-10 | Degradation monitoring + auto-retire | ★ MVP | 7 |
| S-11 | Strategy admin UI | ★ MVP | 6 |
| S-12 | AI generation — journal mode | ○ Phase 2 | 9 |
| S-13 | AI generation — observation mode | ○ Phase 2 | 9 |
| S-14 | Gauntlet G9–G11 (holdout, correlation, sensitivity) | ○ Phase 2 | 9 |
| S-15 | Strategy versioning + lineage | ○ Phase 2 | 9 |
| S-16 | Visual strategy builder | ◇ Phase 3 | — |
| S-17 | Genetic/evolutionary refinement of validated strategies | ◇ Phase 3 | — |

**The sequencing is deliberate and the ordering is the safety argument.** The validation gauntlet (S-5 through S-9) is MVP; AI generation (S-12, S-13) is not. **Build the thing that rejects bad strategies before building the thing that generates them.** Ship with three or four hand-written strategies that have passed the gauntlet, let it run, and only then turn on generation — by which point you also have the journal history that makes journal-mode generation worth anything.

Turning on AI generation before the gauntlet exists would be building the most dangerous half of the feature first.

---

## Appendix A — Sources

Overfitting and validation methodology:
- https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551 (Bailey & López de Prado — The Deflated Sharpe Ratio)
- https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253 (The Probability of Backtest Overfitting)
- https://en.wikipedia.org/wiki/Deflated_Sharpe_ratio
- https://www.sciencedirect.com/science/article/abs/pii/S0950705124011110 (Backtest overfitting in the ML era — OOS method comparison)
- https://arxiv.org/pdf/2604.15531 (Spurious Predictability in Financial Machine Learning)
- https://arxiv.org/pdf/2512.12924 (Rigorous Walk-Forward Validation Framework)

LLM/automated strategy generation:
- https://arxiv.org/pdf/2605.23007 (MadEvolve — Evolutionary Optimization of Trading Systems with LLMs)
- https://arxiv.org/pdf/2511.18850 (Cognitive Alpha Mining via LLM-Driven Code-Based Evolution)
- https://arxiv.org/html/2409.06289v2 (Automate Strategy Finding with LLM in Quant Investment)
- https://arxiv.org/pdf/2605.19337 (Agentic Trading: When LLM Agents Meet Financial Markets)
- https://quanttradingtools.com/automated-strategy-generation/
- https://artificialintelligenceherald.com/ai/llm-evolutionary-trading-algoevolve-2026

---

*End of document. The validation thresholds in §11 are starting points from the literature, not tuned values — expect to revise them once the trial registry has enough history to show what passes and what those strategies then do live.*
