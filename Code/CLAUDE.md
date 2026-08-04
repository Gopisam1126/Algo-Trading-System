# Guidance for Claude Code sessions in this repository

## What this is

A personal-use algorithmic trading system for Indian equities (NSE/BSE). It
handles real money. Treat every change to `execution/`, `common/config.py`, or
the strategy validation path as safety-critical.

Design documents are in `../Documents/` — seven of them, numbered in reading
order. `PRE_LIVE_CHECKLIST.md` is the gate before real capital. **Read the relevant one before changing behaviour it specifies.** They
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

Phase 0 complete: domain models, config with hard bounds, secrets, logging with
redaction, NSE calendar, broker protocol (Zerodha Kite Connect primary), strategy
DSL with 27 primitives, and 112 passing tests. Services are stubs. Nothing trades.

Three items are open and tracked in `../Documents/PRE_LIVE_CHECKLIST.md`:
Algo-ID attachment mechanic unconfirmed, `kiteconnect` on PyPI lacks
`market_protection`, and the NSE holiday list is incomplete. `make doctor`
checks the latter two at runtime.

Next up is Phase 1 — broker authentication with daily re-login, WebSocket
ingestion, tick cleaning, and bar construction. `INDIA_FEATURES_AND_CONFIG.md §3`
has the broker comparison; `LOW_LEVEL_ARCHITECTURE.md §5.1–5.2` has the design.
