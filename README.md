# Algo-Trading-System

AI-driven algorithmic trading platform for Indian markets (NSE/BSE). Personal use.

Pulls real-time market data, computes technical indicators across multiple
timeframes, monitors news and macro conditions, uses AI for pattern synthesis
and reasoning, and executes risk-gated trades across multiple stocks
concurrently.

> **Status: Phase 0 — foundation.** Safety-critical primitives are built and
> tested (112 tests passing). Services are not yet implemented. **Nothing trades.**

---

## Repository layout

```
Documents/     Design specifications — read these first
Code/          The application (see Code/README.md)
```

## Design documents

Written before implementation, and kept current. They record decisions with
reasoning, not aspirations — the code is expected to match.

| # | Document | Covers |
|---|---|---|
| **0** | **[MASTER_REFERENCE.md](Documents/MASTER_REFERENCE.md)** | **START HERE — everything about the system in one document** |
| 1 | [ARCHITECTURE_RESEARCH.md](Documents/ARCHITECTURE_RESEARCH.md) | The *why* — research, AI layering, latency analysis |
| 2 | [INDIA_FEATURES_AND_CONFIG.md](Documents/INDIA_FEATURES_AND_CONFIG.md) | The *what* — NSE/BSE rules, SEBI compliance, config schema |
| 3 | [LOW_LEVEL_ARCHITECTURE.md](Documents/LOW_LEVEL_ARCHITECTURE.md) | The *how* — services, schemas, security, deployment |
| 4 | [MVP_UI_AND_LEGAL.md](Documents/MVP_UI_AND_LEGAL.md) | Scope, screens, autonomy model, legal & tax framework |
| 5 | [STRATEGY_ENGINE.md](Documents/STRATEGY_ENGINE.md) | Strategy DSL, AI generation, overfitting defence |
| 6 | [VERIFICATION_REPORT.md](Documents/VERIFICATION_REPORT.md) | Cross-document audit |
| 7 | [PRE_LIVE_CHECKLIST.md](Documents/PRE_LIVE_CHECKLIST.md) | **The gate before real capital** |
| 8 | **[ENGINEERING_STANDARD.md](Documents/ENGINEERING_STANDARD.md)** | **The mandatory process for all development and research** |

---

## Architecture in one paragraph

Three pipelines run on independent clocks and converge at execution. Market
data is cleaned, aggregated into multi-timeframe bars, and fed to an
incremental indicator engine. News is deduplicated, sanitized, and scored with
novelty and time decay against each symbol's history. Macro signals classify
the market regime. A deterministic quant layer scores and filters candidates;
an AI layer reasons over those outputs to produce a recommendation with a
rationale; a fully deterministic risk engine sizes the position and places the
order. The heavy AI reasoning runs **once daily before the open**, which is
what makes the latency budget work.

## The four invariants

Enforced in code with tests in `Code/tests/security/`, not left to documentation.

1. **The AI can never size a position or place an order.** The type crossing
   that boundary has no quantity field.
2. **Config can tune the system; it can never disable safety.** Hard bounds are
   code constants.
3. **Secrets cannot render.** `SecretString` redacts itself in every string
   context.
4. **The AI never writes executable code.** Strategies are declarative data
   composed from a vetted primitive library.

---

## Branches

| Branch | Purpose |
|---|---|
| `PROD` | Production — only what has passed QA |
| `QA` | Validation and paper-trading verification |
| `DEV` | Active development — work lands here first |
| `main` | Baseline |

Promotion flows `DEV → QA → PROD` via pull request.

---

## Getting started

```bash
cd Code
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
make install
cp .env.example .env      # fill in — never commit this
make doctor               # pre-flight: environment, config, compliance, secrets
make test
```

Full instructions in [Code/README.md](Code/README.md).

---

## Compliance

SEBI's retail algo framework has been mandatory since 1 April 2026. Personal-use
self-developed algorithms are permitted, inside a specific lane: own capital,
own signals only (not shared), under 10 orders/second, through a registered
broker, on a static whitelisted IP, hosted in India. The system is designed to
stay inside that lane deliberately — several of those constraints are enforced
by config validation rather than left to discipline.

See [MVP_UI_AND_LEGAL.md §2](Documents/MVP_UI_AND_LEGAL.md) for the full
treatment including taxation. Not legal or tax advice.

---

## Licence

Private project. Not investment advice. Trading involves risk of loss.
