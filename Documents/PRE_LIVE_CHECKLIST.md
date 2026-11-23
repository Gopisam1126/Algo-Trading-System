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
| B1 | **Get Zerodha's WRITTEN answer: what must a client-developed, sub-threshold algo send on an API order?** | ⚠️ **REOPENED 16 Nov 2026 (audit LEG-001); researched 21 Nov 2026 - the expected answer is "nothing".** The exchange's procedure puts the generic algo ID on the **broker**: members send `"99999"` in the Algo ID field of sub-threshold API orders (NSE/INVG/69255 para 8.2.4, 22 Jul 2025, repeated in NSE/INVG/73992 para 8.4.7, 30 Apr 2026; BSE uses sixteen nines), and NSE rejects mismatched orders since 4 Aug 2025. Zerodha staff on the Kite Connect forum, 23 Jul 2026 (comment 52674): below 10 OPS you "do not need to provide an Algo ID or pass any value in the corresponding field" (repeated 12 Aug 2026, thread 16176). No developer has reported an algo-ID rejection since go-live (1 Apr 2026). **What is still missing is a written answer attributable to Zerodha** - forum replies are informal - and the tag cannot be observed from the client side. **Action:** send the ticket in [RESEARCH_ALGO_TAGGING_AND_HOSTING.md](RESEARCH_ALGO_TAGGING_AND_HOSTING.md) ("Message to paste into a Zerodha support ticket"); record the ticket number, date and the quoted sentence in `broker.algo_tagging_confirmation` (live mode refuses to load while it is empty). The code already sends an `algo_id` only when one is configured, so no change is expected. Reopen if Zerodha documents `algo_id` as required, a circular moves the duty to the client, or developers start reporting algo-ID rejections. | You → Zerodha |
| B10 | **Get Zerodha's written answer on hosting (and counsel's on the one residual point)** | ⚠️ **OPEN (audit HAL-001); researched 21 Nov 2026 - self-hosting is permitted on the evidence; it does NOT block B6.** I(h) of NSE/INVG/67858 ("All Retail Algorithms, including those provided by empanelled Algo providers should be hosted on brokers' cloud servers") is a broker obligation, and "Retail Algorithm" is undefined. NSE's FAQ of 3 Nov 2025, Q5: a "Tech Savvy client is required to host the Algo using a static IP at their end where Algo logic resides instead of hosting the Algo on Trading Member's cloud server." Zerodha staff, 30 Jul 2026 (comment 52689): strategies at or under 10 OPS may run on the user's "preferred infrastructure, including cloud providers such as AWS, GCP, Azure", from the registered static IP. Every broker that addresses it applies broker-cloud hosting to third-party / empanelled algos only. **Residual risk:** NSE/INVG/73992 (30 Apr 2026) restates the broad rule (para 8.4.8.16, para 8.9) without Q5, the FAQ says circulars prevail, and BSE's FAQ could not be retrieved. **Location:** no text requires a client's server or IP to be in India (the India rules bind the broker's routing server); India-region hosting is **this system's own rule**. **Action:** questions 3-4 of the same ticket; the narrow counsel question in the research record. | You → Zerodha, counsel |
| ~~B2~~ | ~~Verify the installed `kiteconnect` exposes `market_protection`~~ | ✅ **CLOSED.** 5.1.0 on PyPI omitted it (zerodha/pykiteconnect#225), but the installed 5.2.1 exposes it, and `make doctor` verifies it at runtime rather than trusting this row. Without it MARKET and SL-M orders are rejected — including the square-off exit, which means positions do not close. | Done |
| ~~B3~~ | ~~Populate `config/nse_holidays.yaml`~~ | ✅ **CLOSED 24 Aug 2026.** Full 2026 list, cross-checked against three publications of the circular; 19 dates, 245 trading sessions, `verified_against_nse_circular: true`. `make doctor` now reports it verified. **Renew every December** — the list is published one year at a time and the calendar refuses to answer for an uncovered year. | Done |
| B6 | **Procure an India-region VPS with a dedicated static IP, and whitelist it at developers.kite.trade** → profile → IP Whitelist. B10's research (21 Nov 2026) supports self-hosting, so this need not wait; if B10 ever went the other way the fallback is a broker-hosted runtime, not a different VPS | The check applies to **order requests only** (place, modify, cancel) — market data, WebSocket, order book, positions and GTT work from any address, so this does not block development. Zerodha's rules (from 1 Apr 2026): **up to two IPs per developer profile** (a primary, required, and an optional secondary), **shared by all its apps**; **one change per calendar week**; IPv4 or IPv6 — register the family the host actually leaves on, because users who registered IPv6 while egressing on IPv4 were rejected; an IP is unique to one account except within immediate family, on a declaration (misuse can suspend the developer account). Every order from an unregistered IP is rejected ("No IPs are configured for this app", "IP (x) is not allowed to place orders for this app"). Also resolves B8. | You |
| B8 | **Run `python scripts/check_data_reachability.py` on that host** | The production host's pre-market must reach NSE's files (bhavcopy, surveillance lists, price bands). From the development host, an Indian ISP, every one the data stories need answers (23 Nov 2026; NSE's home page answers 403), so development does not wait on this - whether NSE treats a datacentre address the same is unmeasured. The probe is read-only, needs no credentials, and is safe during market hours. Exit 0 means that host's pre-market can fetch its data. | You, after B6 |
| ~~B4~~ | ~~Confirm historical data pricing and entitlement~~ | ✅ **CLOSED** (tracker Blockers sheet) — Kite Connect at ₹500/month bundles the WebSocket and historical data. Corrected here 21 Nov 2026: this row still read open. | Done |
| B5 | **Decide the daily login procedure** | Zerodha's redirect auth cannot complete unattended. Accepted as a manual step — confirm the mechanism (phone link → callback → token stored) actually works end to end. | You |
| B9 | **`kiteconnect` runs against an `autobahn` it does not declare** | Residual of B7. `kiteconnect` hard-pins `autobahn==19.11.2`, which carries CVE-2020-35678; we install 26.10.2 over it post-resolution, so no environment we build contains the vulnerable version. The pin is declarative rather than a runtime requirement — but the ticker runs against a version its own metadata does not sanction, so a future `kiteconnect` release could change what it needs without the pin telling us. `tests/security/test_dependency_hygiene.py` watches it. Accepted risk, re-check on every `kiteconnect` upgrade. | Watched by tests |

---

## 1. Regulatory (SEBI)

- [ ] Static IP procured and **whitelisted** at `developers.kite.trade` → profile → IP Whitelist
- [ ] `make doctor` confirms actual egress IP matches `EXPECTED_EGRESS_IP`
- [ ] Hosting answered in writing (B10) — and the host is in an **India region** (config validation enforces that; it is this system's own rule, not a cited regulation - audit HAL-001)
- [ ] The egress IP the host actually uses — address **and** family (IPv4 / IPv6) — is the one registered. A mismatch rejects every order, and fixing a wrong registration can wait a calendar week
- [ ] Daily re-authentication working for **20 consecutive sessions**
- [ ] Algo-ID tagging answered in writing (B1) and recorded in `broker.algo_tagging_confirmation` (live mode refuses to load without it). Then two checks on a real order, because the exchange's tag is not visible to the client: **the outgoing request carries no `algo_id`**, and **the order is accepted** (a third, if Zerodha names where the tag can be seen)
- [ ] Order rate demonstrably capped under a deliberate flood test — this is now **the** control that keeps the system out of the registration regime, not just a throttling nicety. Zerodha counts **10 per second per client ID, across all apps and across NSE and BSE together**, plus 400 per minute and 5,000 per day; excess requests are rejected with HTTP 429, not queued, and **invalid requests count** - so a retry loop spends the budget the exit path needs. The cap (`MAX_ORDERS_PER_SECOND`, 5) is one budget for the whole account, never per exchange
- [ ] Every MARKET and SL-M order - the square-off exit above all - carries a non-zero `market_protection` and is accepted on a real order (Zerodha rejects 0, and warns that an order outside the exchange's price-protection range may be rejected)
- [ ] **Registration threshold re-checked this year** (every December, with the holiday file; last read 16 Nov 2026: 10 OPS per exchange and segment). The figure is the exchanges' (NSE/INVG/67858 Annexures B.2/F), not SEBI's; the exchanges may revise it and a broker may set a lower client limit. If it moves, `SEBI_ALGO_REGISTRATION_OPS` changes with it - and `config.py` refuses to import while `MAX_ORDERS_PER_SECOND` is not below it (`_assert_below_registration_threshold`) (audit HAL-002). In the same review, look for a revised NSE retail-algo FAQ, a new NSE consolidated circular or a BSE equivalent, and any Zerodha documentation of `algo_id`, and re-read B1 and B10 against them - **on the rendered pages, not extracted text**: NSE/INVG/67858 stores words in a layer text tools cannot read (its B.3 extracts as "require registration" where the page says "wouldn't"), and NSE/INVG/73992 para 8.3.2.2(3) misprints the same clause as "would require registration"
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
- [ ] EOD reconciliation runs and reports cleanly - built (E15-S15: final cycle, flat-book verdict, statistics; LOW_LEVEL_ARCHITECTURE.md §8.4); tick when a paper session's verdict is FLAT and its net matches the paper account
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
