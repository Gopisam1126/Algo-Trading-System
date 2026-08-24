# Pre-Live Checklist

**Purpose:** One list to work through before the system places an order with real
capital. Items were previously scattered across four documents; this consolidates
them so nothing is discovered by its absence.

**Rule:** every item is either ✅ done, ❌ blocking, or explicitly ⏭️ waived with a
written reason. "Probably fine" is not a state.

---

## 0. Blockers carried forward

Open questions that must be answered before anything else on this list matters.

| # | Item | Why it blocks | Owner |
|---|---|---|---|
| B1 | **Confirm the Algo-ID attachment mechanic with Zerodha** — does the developer supply it via the order `tag` field, or does the broker inject it? | Sources disagree. One line of code either way, but an order rejected for a missing Algo-ID on day one is an avoidable failure. | You → Zerodha |
| B2 | **Verify the installed `kiteconnect` exposes `market_protection`** | 5.1.0 on PyPI omits it (zerodha/pykiteconnect#225). Without it MARKET and SL-M orders are rejected — including the square-off exit, which means positions do not close. `make doctor` now checks this. | You |
| ~~B3~~ | ~~Populate `config/nse_holidays.yaml`~~ | ✅ **CLOSED 24 Aug 2026.** Full 2026 list, cross-checked against three publications of the circular; 19 dates, 245 trading sessions, `verified_against_nse_circular: true`. `make doctor` now reports it verified. **Renew every December** — the list is published one year at a time and the calendar refuses to answer for an uncovered year. | Done |
| B6 | **Procure an India-hosted VPS with a static IP, and whitelist it at developers.kite.trade** | Applies to **order endpoints only** — market data, order book and positions work from any address, so this does not block development. One IP per app; orders from an unregistered IP are rejected outright. Also resolves B8. | You |
| B8 | **Run `python scripts/check_data_reachability.py` on that host** | NSE blocks *overseas* access rather than programmatic access as such. The probe is read-only, needs no credentials, and is safe during market hours. Exit 0 means E03/E04 can proceed. | You, after B6 |
| B4 | **Confirm historical data pricing and entitlement** | The pre-market engine reads ~3 years of multi-timeframe history across ~200 symbols every morning. If it is a paid add-on, that is a recurring cost and an access dependency. | You → Zerodha |
| B5 | **Decide the daily login procedure** | Zerodha's redirect auth cannot complete unattended. Accepted as a manual step — confirm the mechanism (phone link → callback → token stored) actually works end to end. | You |

---

## 1. Regulatory (SEBI)

- [ ] Static IP procured and **whitelisted** at `developers.kite.trade` → profile → IP Whitelist
- [ ] `make doctor` confirms actual egress IP matches `EXPECTED_EGRESS_IP`
- [ ] Deployment host is in an **India region** (config validation enforces this)
- [ ] Daily re-authentication working for **20 consecutive sessions**
- [ ] Algo-ID configured and confirmed present on orders (B1)
- [ ] Order rate demonstrably capped under a deliberate flood test
- [ ] **Single** notification recipient (config validation enforces this)
- [ ] Broker's API terms of service read, and this use case confirmed permitted in writing

## 2. Correctness

- [ ] `make check` green — lint, types, full suite
- [ ] `make test-safety` green — the four architectural invariants
- [ ] Risk-engine property tests pass across generated inputs
- [ ] All chaos scenarios pass (LOW_LEVEL_ARCHITECTURE.md §12.3)
- [ ] Duplicate-order prevention verified by **simulated timeout + reconnect**
- [ ] Every position confirmed to exit before the broker's per-stock deadline
- [x] Holiday calendar verified — 2026 list checked against three publications;
      Republic Day, Holi, Bakri Id, Dussehra, Diwali and Guru Nanak Jayanti all excluded
- [ ] Holiday calendar **renewed for the coming year** (December task; the
      calendar raises rather than guessing for an uncovered year)
- [ ] `python scripts/check_data_reachability.py` exits 0 on the production host
- [ ] Corporate-action adjustment verified against a known split

## 3. Security

Full detail in LOW_LEVEL_ARCHITECTURE.md §10.12.

- [ ] `gitleaks` clean over **full history**, not just HEAD
- [ ] No secret in any config file, `.env`, or Docker image layer
- [ ] Log redaction tested with a **deliberate leak attempt**
- [ ] Dashboard unreachable from the public internet — verified externally
- [ ] Firewall default-deny confirmed by an external port scan
- [ ] Containers non-root, capabilities dropped, resource-limited
- [ ] Egress allowlist verified — a connection to an unlisted host must fail
- [ ] Prompt-injection test suite passes
- [ ] Kill switch tested end to end **from a phone**, under 10 seconds
- [ ] Audit log write-only permissions verified
- [ ] Backup restore drill completed and reconciled against the broker

## 4. Strategy

- [ ] Every ACTIVE strategy passed the full validation gauntlet
- [ ] Deflated Sharpe and PBO recorded, with the **trial count at validation time**
- [ ] Locked holdout evaluated exactly once per strategy
- [ ] Shadow mode run for ≥ 20 sessions with ≥ 80% live-vs-backtest agreement
- [ ] Paper trading ≥ 30 trades with positive expectancy after realistic costs
- [ ] Human approval recorded for each strategy promoted to ACTIVE
- [ ] Degradation monitoring active with auto-retirement configured

## 5. Operations

- [ ] `make doctor` exits 0
- [ ] Prometheus and Grafana reachable; alerts routed to Telegram
- [ ] P0 alerts tested — kill switch, unknown position, auth failure
- [ ] EOD reconciliation runs and reports cleanly
- [ ] Nightly backup verified off-host
- [ ] Runbook written for: auth failure, feed loss, broker outage, unknown position

## 6. Financial

- [ ] Capital amount confirmed and slot arithmetic sanity-checked
- [ ] Charge-level fill accounting verified against a real contract note
- [ ] Speculative turnover computation verified on known test data
- [ ] Business expense register started (VPS, API, data — all deductible)
- [ ] CA engaged and aware of the intended trading activity

## 7. Go-live sequence

Do not skip steps. Each validates a different failure mode.

1. [ ] **Paper trading**, meaningful sample across ≥ 2 market regimes
2. [ ] Review every paper trade against its plan — did it do what you expected?
3. [ ] **L2 approval mode**, live capital, smallest viable size
4. [ ] ≥ 20 approved trades before considering L3
5. [ ] **L3 supervised auto**, still small size
6. [ ] Scale size only after a sustained, understood track record

> **MVP-complete does not mean ready for capital.** The gap between "the software
> works" and "the strategy works" is the largest risk in this project, and no
> amount of engineering closes it. Only evidence does.

---

## Appendix — verification method

A note on *how* to check things on this list, learned from the second audit.

**The static scan found nothing. Both bugs came from executing the claims.**

The OHLC validation read correctly and was inert — a Pydantic field validator
cannot see fields declared after it, so `close > high` passed silently. The log
redactor read correctly and leaked short JWTs, because the pattern assumed a
header length real tokens do not have.

Neither was visible in review. Both took thirty seconds to find by constructing
a deliberately invalid bar and feeding a real token through the redactor.

So for every item above, prefer the executable form:

| Instead of | Do |
|---|---|
| "the config rejects bad values" | Set a bad value; watch it fail |
| "secrets are redacted" | Log a real-shaped secret; grep the output |
| "the kill switch works" | Press it from your phone; time it |
| "positions exit before the deadline" | Run the timer against a real deadline |
| "the dashboard isn't exposed" | Port-scan the host from outside |
| "the egress allowlist holds" | `curl` an unlisted host from inside a container |

**Code review catches intent. Only execution catches behaviour.**
