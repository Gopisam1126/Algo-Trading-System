# AI Algo Trading Platform — India (NSE/BSE)

Personal-use algorithmic trading system for Indian equities, intraday, through
Zerodha Kite Connect. As built it is a **rule-based** engine - strategy rules over
technical indicators, a deterministic risk engine and an order gateway; the AI
review layer is designed and not built.

> **Status:** see [`MASTER_REFERENCE.md` §2](../Documents/MASTER_REFERENCE.md#2-current-status--what-exists-what-doesnt),
> the one authoritative statement. Components are built and tested; no process
> entrypoint exists yet, so nothing runs and **nothing has ever traded**. Test
> counts are deliberately not written here: the latest CI run is the count.

---

## Design documents

Read these before changing anything. They are in [`../Documents/`](../Documents/).

| # | Document | Covers |
|---|---|---|
| **0** | **`MASTER_REFERENCE.md`** | **START HERE — the complete system reference** |
| 1 | `ARCHITECTURE_RESEARCH.md` | The *why* — research findings, AI layering, latency |
| 2 | `INDIA_FEATURES_AND_CONFIG.md` | The *what* — NSE/BSE rules, SEBI, config schema |
| 3 | `LOW_LEVEL_ARCHITECTURE.md` | The *how* — services, schemas, security, deployment |
| 4 | `MVP_UI_AND_LEGAL.md` | Scope, screens, autonomy model, legal & tax |
| 5 | `STRATEGY_ENGINE.md` | Strategy DSL, AI generation, overfitting defence |
| 6 | `VERIFICATION_REPORT.md` | Cross-document audit |
| 7 | `PRE_LIVE_CHECKLIST.md` | **The gate before real capital** |
| 8 | **`ENGINEERING_STANDARD.md`** | **The mandatory process for all development and research** |

---

## Quick start

> **Setting up for the first time?** Read **[SETUP.md](SETUP.md)** too — it
> covers the datastore gotchas that will otherwise cost you an afternoon.

```bash
cd Code
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
make install
cp .env.example .env        # then fill it in — never commit this file
make validate-config
make doctor                 # pre-flight check
make test
```

`make doctor` is the single most useful command here. It verifies your
environment, config, SEBI compliance posture, secrets, and strategy files, and
tells you exactly what is missing.

> **TA-Lib needs a C library** before the Python package will install. On
> Debian/Ubuntu build it from source (see `ops/Dockerfile`); on macOS
> `brew install ta-lib`; on Windows use a prebuilt wheel. Production never
> imports it; the `dev` extra needs it, because the indicator tests verify
> every incremental indicator against TA-Lib.

---

## The four invariants

These are enforced in code, not documentation. Each has a test in
`tests/security/test_safety_invariants.py` — if one of those fails, a safety
guarantee has been silently removed.

**1. The AI can never size a position or place an order.**
`Recommendation` is the only type crossing from the AI layer to the risk
engine, and it has no `quantity`, no rupee amounts, and no executable stop
price. There is no field through which the AI could influence sizing.

**2. Config can tune the system; it can never disable safety.**
Hard bounds live in `common/config.py` as code constants. A config file cannot
raise the order rate above the SEBI-safe cap, set a 50% per-trade risk, disable
the T2T filter, or turn off human approval for strategy promotion.

**3. Secrets cannot leak into logs, prompts, or errors.**
`SecretString` returns `***REDACTED***` from `__str__`, `__repr__`, and
`__format__`, and refuses to pickle. Reading the value requires an explicit
`.reveal()`.

**4. The AI never writes executable code.**
Strategies are declarative documents composed from a vetted primitive library.
There is no `eval`, no `exec`, no dynamic import in the strategy path — arbitrary
code execution isn't mitigated, it's impossible. A strategy also cannot express
"no stop loss" or "hold past the square-off deadline", because the DSL has no
way to say it.

---

## Layout

```
config/
  system.yaml            Main configuration (version controlled, no secrets)
  strategies/            Strategy definitions — data, not code
src/algotrader/
  common/                Models, config, secrets, logging, NSE calendar
  broker/                BrokerAdapter protocol (read-only vs trading split)
  ingest/                WebSocket ingestion, cleaning, bar construction
  indicators/            Incremental TI engine
  macro/                 News + macro context (slow loop)      [empty - planned]
  premarket/             The daily preparation pipeline        [empty - planned]
  signals/               Strategy evaluation loop (AI review: planned)
  strategy/              DSL, primitives, registry (gauntlet: planned)
  ai/                    Anthropic client, prompts, budget     [empty - planned]
  execution/             Risk engine, sizing, orders, positions  ← 100% coverage is the TARGET (LLA §15)
  orchestrator/          Lifecycle and scheduling              [empty - planned]
  api/                   FastAPI dashboard (localhost only)    [empty - planned]
  notifier/              Telegram alerts (single recipient)    [empty - planned]
tests/
  unit/ property/ integration/ replay/ chaos/ security/
ops/
  docker-compose.yml     Nine services, network-segmented
  Dockerfile             Multi-stage, non-root runtime
scripts/
  doctor.py              Pre-flight check
```

---

## Common commands

| Command | Does |
|---|---|
| `make doctor` | Pre-flight: environment, config, compliance, secrets, strategies |
| `make validate-config` | Validate config and print derived risk figures |
| `make test` | Full test suite |
| `make test-safety` | Verify the four invariants above still hold |
| `make check` | Lint + types + tests (what CI runs) |
| `make security` | Secret scan + dependency audit + safety tests |
| `make up` / `make down` | Start / stop the compose stack - **only Redis, TimescaleDB, Prometheus and Grafana stay up today**: no application service has an entrypoint yet, so its nine containers exit at start (audit ARC-001, ENG-015; E23-S03) |

---

## Compliance — read before going live

SEBI's retail algo framework has been mandatory since 1 April 2026. What is
required, and what is still open, is kept in
[`PRE_LIVE_CHECKLIST.md`](../Documents/PRE_LIVE_CHECKLIST.md); in short:

- **Static, broker-whitelisted IP.** Rules out autoscaling deployments where the
  egress IP changes. Live mode refuses to load without `system.static_ip`;
  `make doctor` compares the actual egress IP with `EXPECTED_EGRESS_IP`.
- **Daily re-authentication** before pre-open; sessions must not persist overnight.
- **Algo-ID tagging - OPEN (B1, audit LEG-001).** Below the registration
  threshold there is no registered Algo-ID, but the exchanges tag such orders
  with a generic one, and who sends it is unconfirmed. Live mode refuses to
  load until `broker.algo_tagging_confirmation` records Zerodha's written answer.
- **Hosting - OPEN (B10, audit HAL-001).** The India-region check
  (`deployment_region`) is this system's own rule; no cited regulation
  requires Indian servers. NSE's standard I(h) says retail algos "should be
  hosted on brokers' cloud servers", and whether that applies here needs a
  written answer.

Two further boundaries matter:

- **Order rate stays under the exchanges' 10/sec threshold.** The system caps
  at 5 (`MAX_ORDERS_PER_SECOND`), enforced in the broker adapter's token bucket
  rather than left to callers.
- **Single notification recipient.** Sharing trade signals with others can trigger
  SEBI Research Analyst obligations. Configuring a second recipient fails validation.

Taxation: intraday equity is *speculative business income* (ITR-3), with turnover
defined as the absolute sum of profits and losses. Your VPS, AI API spend, and data
subscriptions are deductible business expenses — track them from day one. See
`MVP_UI_AND_LEGAL.md §2`. Not tax advice; engage a CA.

---

## Build sequence

The original plan, kept for its ordering. **It is not a status** — what is
built is [`MASTER_REFERENCE.md` §2](../Documents/MASTER_REFERENCE.md#2-current-status--what-exists-what-doesnt);
the backlog tracker has each story's state.

| Phase | Weeks | Deliverable |
|---|---|---|
| 0 | 1–2 | Foundation |
| 1 | 3–4 | Broker auth, WebSocket ingest, cleaning, historical sync |
| 2 | 5–6 | Indicator engine, filters, Tradeability scoring |
| 3 | 7–8 | **Pre-market AI plan by 09:15 — first genuinely useful milestone** |
| 4 | 9–10 | Signals, in-session AI review, live dashboard |
| 5 | 11–13 | Risk engine, execution, **paper trading** |
| 6 | 14–15 | Admin UI, config editor, audit explorer, tax report |
| 7 | 16–18 | Chaos tests, security checklist, hardening |
| 8 | 19+ | Approval mode on small live capital |

Phase 3 is the inflection point: from that week the system pays for itself as a
research assistant whether or not execution is ever automated.

**MVP-complete does not mean ready for meaningful live capital.** That requires a
paper-trading track record across different market regimes. The gap between "the
software works" and "the strategy works" is the largest risk in this project, and
no amount of engineering closes it — only evidence does.
