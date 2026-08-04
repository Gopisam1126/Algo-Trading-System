# AI Algo Trading Platform — India (NSE/BSE)

Personal-use algorithmic trading system for Indian equities. Pulls real-time
market data, computes technical indicators across multiple timeframes, monitors
news and macro conditions, uses AI for pattern synthesis and reasoning, and
executes risk-gated trades across multiple stocks concurrently.

> **Status: Phase 0 — foundation scaffold.** The safety-critical primitives
> (domain models, config validation, secrets, calendar, strategy DSL) are built
> and tested. The services are not yet implemented. **Nothing trades yet.**

---

## Design documents

Read these before changing anything. They are in [`../Documents/`](../Documents/).

| # | Document | Covers |
|---|---|---|
| 1 | `ARCHITECTURE_RESEARCH.md` | The *why* — research findings, AI layering, latency |
| 2 | `INDIA_FEATURES_AND_CONFIG.md` | The *what* — NSE/BSE rules, SEBI, config schema |
| 3 | `LOW_LEVEL_ARCHITECTURE.md` | The *how* — services, schemas, security, deployment |
| 4 | `MVP_UI_AND_LEGAL.md` | Scope, screens, autonomy model, legal & tax |
| 5 | `STRATEGY_ENGINE.md` | Strategy DSL, AI generation, overfitting defence |
| 6 | `VERIFICATION_REPORT.md` | Cross-document audit |
| 7 | `PRE_LIVE_CHECKLIST.md` | **The gate before real capital** |

---

## Quick start

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
> `brew install ta-lib`; on Windows use a prebuilt wheel. It is optional for
> Phase 0 — nothing in the current scaffold requires it.

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
  macro/                 News + macro context (slow loop)
  premarket/             The daily preparation pipeline
  signals/               Strategy evaluation + AI review
  strategy/              DSL, primitive registry, validation gauntlet
  ai/                    Anthropic client, prompts, budget
  execution/             Risk engine, sizing, orders, positions  ← 100% coverage
  orchestrator/          Lifecycle, scheduling, kill switch
  api/                   FastAPI dashboard (localhost only)
  notifier/              Telegram alerts (single recipient)
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
| `make up` / `make down` | Start / stop all services |

---

## Compliance — read before going live

SEBI's retail algo framework has been mandatory since 1 April 2026. Four
requirements are architectural, not paperwork:

- **Static, broker-whitelisted IP.** Rules out autoscaling deployments where the
  egress IP changes. `make doctor` verifies the actual egress IP matches config.
- **India-hosted servers.** Enforced by config validation — a non-India
  `deployment_region` fails to load.
- **Daily re-authentication** before pre-open; sessions must not persist overnight.
- **Algo-ID on every order.** Required for live mode.

Two further boundaries matter:

- **Order rate stays under 10/sec.** The system caps at 5, enforced in the broker
  adapter's token bucket rather than left to callers.
- **Single notification recipient.** Sharing trade signals with others can trigger
  SEBI Research Analyst obligations. Configuring a second recipient fails validation.

Taxation: intraday equity is *speculative business income* (ITR-3), with turnover
defined as the absolute sum of profits and losses. Your VPS, AI API spend, and data
subscriptions are deductible business expenses — track them from day one. See
`MVP_UI_AND_LEGAL.md §2`. Not tax advice; engage a CA.

---

## Build sequence

| Phase | Weeks | Deliverable |
|---|---|---|
| **0** | 1–2 | **Foundation — this scaffold** ✅ |
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
