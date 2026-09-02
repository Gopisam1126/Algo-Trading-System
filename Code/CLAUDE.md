# Guidance for Claude Code sessions in this repository

## What this is

A personal-use algorithmic trading system for Indian equities (NSE/BSE). It
handles real money. Treat every change to `execution/`, `common/config.py`, or
the strategy validation path as safety-critical.

Design documents are in `../Documents/`. **`MASTER_REFERENCE.md` is the entry
point** — read it first; it covers the whole system. The others go deeper on
specific areas. `PRE_LIVE_CHECKLIST.md` is the gate before real capital. **Read the relevant one before changing behaviour it specifies.** They
are not aspirational; they record decisions with reasoning, and the code is
expected to match.

## Non-negotiable invariants

Each is enforced in code and has a test in `tests/security/test_safety_invariants.py`.
If you find yourself wanting to relax one, stop and ask — you have probably
found a design conflict rather than an obstacle.

1. **`Recommendation` must never gain a sizing field.** No `quantity`, no
   rupee amounts, no executable stop price. It is the AI/deterministic
   boundary; sizing happens downstream in `execution/sizer.py` from config and
   live broker margin.

2. **Hard bounds in `common/config.py` are code, not config.** `MAX_ORDERS_PER_SECOND`,
   `MAX_RISK_PCT_PER_TRADE`, `MAX_DAILY_LOSS_PCT`, `require_human_approval`,
   `require_hypothesis`. Configuration tunes the system; it can never disable
   safety.

3. **No `eval`, `exec`, or dynamic import in the strategy path, ever.**
   Strategies are declarative data composed from the vetted primitive registry.
   If a feature seems to need code generation, it needs a new primitive instead
   — which is a reviewed change to `strategy/primitives/registry.py`.

4. **Secrets never render.** Use `SecretString`. Never log, prompt with, or put
   a credential in an exception message.

5. **Every position has a stop and a time exit.** The `ExitRules` schema requires
   both. A strategy that omits either does not parse.

6. **Fail closed.** AI timeout → skip the trade. Data stale → block entries.
   Component down → no new risk. Never fail open.

## Conventions

- **`Decimal` for all prices and money.** Never `float`. Convert only at the
  boundary of numerical libraries.
- **Timezone-aware UTC everywhere.** IST only at the display boundary and in
  market-hours logic. Ruff's DTZ rules are on and will catch naive datetimes.
- **Bars align to the session start (09:15 IST), not wall-clock hours.** A
  15-minute bar runs 09:15–09:30. Use `MarketCalendar.bar_open_time()`.
- **Square-off deadlines are per-stock**, not global — 15:10 CAS / 15:20
  non-CAS / 15:25 F&O, minus a buffer. Use `MarketCalendar.squareoff_deadline()`.
- Pydantic models are `frozen=True, extra="forbid"` unless there is a reason.
- Strategies are pure functions over a `MultiTimeframeSnapshot` — no I/O, no
  randomness. This is what makes them backtestable by replay.

## Verifying your own work

**Static review does not catch behaviour.** Two real bugs shipped past a clean
code read in this repo:

- `Bar` used a Pydantic *field* validator for OHLC coherence. A field validator
  cannot see fields declared after it, so the `close > high` check was inert.
  A corrupt bar validated successfully and would have reached the indicator
  engine with no error raised.
- The log redactor's JWT pattern required a longer header than real tokens
  have, so short JWTs were logged in full.

Both read correctly. Both took under a minute to find by *running* them —
constructing a deliberately invalid bar, feeding a real token through the
redactor.

So when you add or change a safety-relevant claim, write the probe that would
catch it being false:

| Claim | Probe |
|---|---|
| "this validator rejects X" | Construct X; assert it raises |
| "secrets are redacted" | Pass a real-shaped secret; grep the output |
| "config rejects bad values" | Set the bad value; assert it fails |
| "the deadline is respected" | Compare against the real broker deadline |

Prefer a failing test over a comment asserting correctness.

## Before you commit

```bash
make check          # lint + types + tests
make test-safety    # the invariants above
make doctor         # config and compliance posture
```

Never commit a `.env`, a credential, or anything under `data/`.

## Things that will bite you

- **Blind retry after an order timeout creates duplicate positions.** The
  recovery path is always: query the broker by `client_order_id`, adopt its
  answer, resubmit only if genuinely absent. See `LOW_LEVEL_ARCHITECTURE.md §8.2`.
- **Indicator warm-up matters.** `IndicatorSnapshot.ready` is False until enough
  bars exist. Trading off a 20-EMA built from 3 bars is a real bug that looks
  like a working system.
- **Prompt caching is a prefix match.** A timestamp or UUID anywhere in the
  cached prefix silently costs 10× on every call. Verify with
  `usage.cache_read_input_tokens` — if it is zero across repeated calls,
  something is invalidating the cache.
- **News content is untrusted input.** It goes through sanitization, then a
  structured-output AI call with bounded fields. Never concatenate article text
  into a prompt, and never let a free-form field from news reach a downstream
  prompt.
- **Every strategy backtest counts as a trial**, including parameter sweeps.
  Testing one strategy at 20 settings is 20 trials. The trial registry is
  append-only because deleting failures corrupts the Deflated Sharpe denominator.

## AI layer specifics

- Model tiers: `claude-opus-5` (pre-market deep synthesis, once daily),
  `claude-sonnet-5` (in-session confirmation), `claude-haiku-4-5` (news triage).
- Always use structured outputs (`messages.parse()` with a Pydantic model).
  Free-text responses have no place in a system that acts on them.
- Check `stop_reason` **before** reading `content` — a refusal has empty or
  partial content, and indexing `content[0]` will crash.
- Current models reject `temperature`/`top_p`. Steer with the prompt.
- Re-verify the model lineup against the `claude-api` skill before changing it;
  this space moves faster than the documents.

## Current state

**The deterministic path is built from tick to a `RiskDecision`. Nothing
trades, and nothing can — ten of the fourteen risk checks are unwritten and
there is no sizing and no order placement.**

Built and tested (**1,212 tests, 82% coverage** — 965 pass locally, 247 need
Docker):

- **Foundations** — domain models, config with hard bounds, `SecretString`,
  redacting logging, NSE calendar, broker protocol, strategy DSL.
- **E01 persistence** — TimescaleDB schema with BR-1..BR-20 as constraints,
  async repositories, hash-chained audit log, Redis primitives,
  archive/restore, CI with gated promotion to QA.
- **E02 broker** — Kite auth and daily re-auth scheduling, read-only/trading
  split, error taxonomy, instrument sync, live margin, rate limiter capped at
  5 OPS.
- **E05 ingestion** — WebSocket client built on `websockets` against the
  documented binary protocol, reconnection with an explicit `FeedGap`, tick
  validation, dedup, outlier filter, session-aligned bars, quote state.
- **E06 indicators** — incremental EMA/SMA/RSI/ATR/MACD/Bollinger/VWAP/
  VolumeRatio verified against TA-Lib, warm-up orchestration, multi-timeframe
  snapshot, pivots and opening range.
- **Strategy runtime** — the 27 primitives now execute.
- **E14 risk engine, partial** — the ordered fail-fast check pipeline
  (E14-S01) and pre-condition checks 1–4 (E14-S02: kill switch, health gate,
  trading window, no-trade window). A check that *raises* is a REJECTION, not
  a skip, reported as `RISK_ENGINE_FAULT` — the one `RejectReason` that means
  "the system is broken" rather than "the answer is no". Checks 5–14 are not
  written, so the engine cannot yet approve anything and there is nothing
  downstream of it.

**Empty (`__init__.py` only):** `signals/`, `orchestrator/`, `premarket/`,
`api/`, `notifier/`, `ai/`, `macro/`. `execution/` now holds `risk/` and
nothing else — no sizer, no order manager.

### The architectural fact to keep in mind

**Nothing composes the packages that are built.** No module in `src/` imports
both `ingest` and `indicators`. The 1,212 tests are claims about *components*;
there is exactly one test of the *system*, `tests/integration/test_tick_to_trigger.py`,
written deliberately to find what component tests cannot — and it found a
HIGH-severity defect on its first run. Assembly is E11 and E13. Until it
exists, treat every "it works" as scoped to a part.

### Things that turned out to be false

Recorded because each was believed, written down, and wrong.

- **"The 27 primitives are implemented."** They were `PrimitiveSpec` records —
  name, category, parameter bounds — with no function behind any of them, while
  `compile_strategy`'s docstring promised a runtime evaluator that did not
  exist. A strategy would validate, hash, persist, activate, and never fire.
  Now implemented, with a test asserting declared and implemented are the same
  set.
- **"`autobahn` is never imported."** `kiteconnect/__init__.py` imports
  `.ticker` unconditionally, so autobahn and Twisted load into every process
  that touches the broker layer whether or not a ticker is constructed. *Not
  using a package is not the same as not having it.* B7 was closed by
  upgrading to 26.7.1 — the `==19.11.2` pin is declarative, not a runtime
  requirement.
- **"`Applicability` gates the strategy."** It was parsed, validated and folded
  into the content hash, then read by nothing. A TRENDING-only strategy fired
  freely in a rangebound market.
- **"Coverage means the tests would catch it."** Mutation testing injected 15
  plausible defects; two survived, both because the tests exercised a helper
  directly rather than the path that calls it.
- **"The symbol validator is applied."** It was — on `OrderRequest`, and only
  there. The same untrusted broker value reaches `Trigger` and
  `Recommendation` first, and the risk engine logs it on every rejection. A
  newline in a symbol forged two lines reading *"CRITICAL kill switch disarmed
  by operator"* into the log an incident would be reconstructed from. A
  validator applied to one of three boundaries is not a validator: define it
  once, above the first model that needs it.
- **"`HEALTH_GATE_FAILED` means the health gate failed."** It also meant
  "a check raised", "no sizer is configured" and "sizing raised", because
  `RejectReason` had no member for an engine fault and the nearest plausible
  neighbour got borrowed. SIT found it by walking a whole session: 340
  tradable minutes all rejected as HEALTH_GATE_FAILED with every service
  healthy. `signals_rejected_total{reason}` is the metric that turns "why
  isn't it trading?" into a glance, so a wrong label there costs exactly the
  glance it exists to provide. Now `RISK_ENGINE_FAULT`. See SIT-001.
- **"`"integration" in item.keywords` checks the marker."** It also matches
  every ancestor *node name*, so it matched the `tests/integration/`
  **directory**. The only system-level test in the repo — which uses no
  container at all — was gated on Docker and had been reporting as skipped on
  a suite that called itself green. Use `item.get_closest_marker(...)`. A test
  that quietly stops running is worse than one that fails, because nothing
  asks why.

### Corporate actions — read before touching `ohlcv`

- **Raw OHLC is immutable (BR-15).** Adjusted price = `raw * price_adj_factor`,
  applied by the repository on read. Never adjust a stored price in place — the
  second corporate action compounds the first with no error and no failing test.
- **Only `recompute_factors` writes the factor columns.** The ingest path omits
  them from both its column list and its `ON CONFLICT` set on purpose; adding
  them "for symmetry" would reset every adjusted bar on the next backfill.
- **BR-16 is structural.** `BarRepository` has no method returning raw prices.
  `raw_bars_for_audit()` is the deliberate exception and nothing in the trading
  path may call it.

### Blockers

| | State |
|---|---|
| B2 `market_protection` | ✅ Closed — present in kiteconnect 5.2.1 |
| B4 historical data pricing | ✅ Closed — Connect ₹500/mo bundles WebSocket + historical |
| B7 `autobahn` CVE-2020-35678 | ✅ Closed 24 Aug 2026 — upgraded to 26.7.1 |
| B3 NSE holiday list | ✅ Closed 24 Aug 2026 — 2026 verified, 245 sessions. **Renew each December** |
| B1 Algo-ID | 🔍 Mechanic understood; broker assigns it at registration. Paperwork with Zerodha |
| B5 daily login | ⚠️ Needs real credentials |
| B6 static IP | 🔍 **Order endpoints only** — does not block development |
| B8 NSE data access | 🔍 Same fix as B6; `scripts/check_data_reachability.py` answers it in one command |

`make doctor` reports all of this at runtime.

### Next

The keystone is gone, so E12 (backtest harness, gauntlet) and E13 (signal
loop) are unblocked. **E14, the risk engine, is in progress** — S01 (the
framework) and S02 (pre-conditions 1–4) are delivered; S03 symbol eligibility
(5–7), S04 portfolio exposure (8–10) and S05 loss/margin (11–14) are next, and
they are what the engine needs before it can approve anything. All of it is
pure computation with no credentials and no external data. After that, a
vertical slice through paper trading is the first thing that would be evidence
about the system rather than its parts.

### How to do the work

**`/sdlc` is the driver.** Invoked with no argument it means: find the next
item, take it through business analysis, research, architecture, development,
four rounds of QA, promotion to the QA branch and SIT — then stop. With an
argument (`/sdlc E14-S02`) it means that item.
See `.claude/skills/sdlc/SKILL.md`.

**`Documents/ENGINEERING_STANDARD.md` is the reference** it calls into: the
invariants, the verification ladder, and the anti-pattern catalogue that
encodes the failures above.

**PROD is never promoted without explicit permission**, asked for and given in
the conversation. `promote.yml` carries an unconditional `exit 1` on that path;
it stays.
