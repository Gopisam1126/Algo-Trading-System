# SPRINT 2 — TECHNICAL SPECIFICATION
## E04 (India Hazard Lists) and E03 (Historical Data)

Written per `DEVELOPMENT_PROCEDURE.md §1` — the analysis pass — **before any
code**. The most valuable part of this document is §2: four conflicts between
the backlog and the schema that E01 actually built, one of which makes a
safety-critical task unimplementable as written.

> ### STATUS — updated 11 Aug 2026
>
> **Conflict A (§2A) is resolved and built.** It was blocking, it was the
> reason `E03-S02` was deferred out of this sprint, and it is now schema,
> engine and 18 tests on `DEV`. §4's Option 4 is what shipped; §4.2's other
> three options are kept because the reasoning for rejecting them is what
> stops someone reintroducing one.
>
> **Conflicts B and C are resolved too** — `instrument_daily_status` now has
> `gsm_stage` and `asm_category`, wired through the repository in both
> directions.
>
> **What is still Sprint 2 work:** the corporate action *feed* (`E03-S02`
> task 1). The deferral reasoning in §9 still applies to it unchanged — it
> should inherit `E04-S06`'s fetcher rather than build one under time
> pressure. The engine it feeds is already waiting.
>
> Three defects surfaced *during* implementation that this pre-analysis did
> not predict; they are recorded in §4.5 because the pattern — approximately
> right arithmetic, a healing path that did not heal, a lock that was not
> there — is more reusable than the fixes.

---

## 1. WHY THIS SPRINT EXISTS

E01 built a place to put data. This sprint puts the **first real data** in it,
and that data is what stops the system trading things it must not touch.

### 1.1 What these two epics actually deliver

**E04 is a safety system, not a data feed.** Four of its seven stories are
🔴 safety-critical. Each one prevents a specific, expensive failure that has no
US equivalent and that an algorithm ignoring it will hit:

| Hazard | What happens if the system does not know |
|---|---|
| **T2T** | Orders are rejected, or worse become **unintended delivery obligations** — you wake up owning stock you meant to hold for 20 minutes |
| **ASM / GSM** | 100% margin, reduced bands, erratic prices; sizing computed on normal assumptions is wrong |
| **F&O ban** | Penalty of 1% of the increased position value, minimum ₹5,000, maximum ₹1,00,000 |
| **Circuit bands** | A 2% band stock cannot move enough to cover costs; a stock at circuit has **no liquidity on one side** |
| **CAS classification** | The square-off deadline is 15:10, not 15:20. Getting it wrong means the broker force-closes at whatever the auction produces |

**E03 is the memory.** Without a local historical store the pre-market engine
must re-pull years of data from a rate-limited API every morning. The store is
also what every backtest reads, which is why its correctness is not negotiable:
a wrong price in history does not cause an error, it causes a *confident wrong
answer* months later.

### 1.2 Why this sprint can start immediately

Verified against the tracker's dependency column, not assumed:

- **All of E04 depends only on `E01-S02`** (the repository layer, Closed). Its
  data comes from public NSE sources, not the broker.
- **E03 depends on `E01-S02`** except `E03-S03` (intraday backfill), which
  needs `E02-S03`, and `E03-S06` (tick archival), which needs `E05-S01`.

So **12 of E03+E04's 14 days need no broker credentials at all.** Nothing about
the unresolved Kite app blocks this sprint.

---

## 2. ⚠️ PRE-IMPLEMENTATION ANALYSIS — CONFLICTS FOUND

Each item below is a real mismatch between what the backlog asks for and what
E01 actually built. Found by reading the shipped schema, not the design doc.

### A. `is_adjusted BOOLEAN` cannot support re-adjustment — ~~**BLOCKING**~~ **RESOLVED 11 Aug 2026**

`E03-S02` is 🔴 safety-critical and its task 4 is *"Re-adjustment when a new
action is announced."* **That is not implementable against the current schema.**

`ohlcv.is_adjusted` is a boolean. It records *that* an adjustment happened, not
*what* was applied. When a second corporate action arrives you must either:

- know the factor already applied, so you can apply only the delta — the
  boolean does not tell you; or
- start from the raw price — which has been overwritten and is gone.

Applying a second adjustment to already-adjusted prices **compounds silently**.
There is no error, no constraint violation, no failing test. Every backtest
over that symbol is quietly wrong from then on, and the wrongness grows with
each subsequent action.

This is the single most important decision in the sprint and §4 is devoted to it.

### B. GSM has stages; the schema has a boolean

`E04-S01` task 2 says *"Fetch GSM list **with stage**"*. The schema has
`is_gsm BOOLEAN`.

GSM stages are not decoration. They escalate: the lower stages mean a reduced
price band and 100% margin; the higher stages move the stock to periodic call
auction, weekly or monthly trade-to-trade settlement, and permit **no upward
price movement at all**. Collapsing that to a boolean loses the difference
between "restricted" and "structurally untradeable."

Both are excluded from the intraday universe today, so the *filter* still works
— but the day someone asks "why was this excluded, and how badly," the answer
is gone. Schema change required.

### C. ASM short-term and long-term are collapsed

Same shape, lower severity. `E04-S01` task 1 fetches both lists; the schema
records one boolean. Both are hard exclusions, so no filtering behaviour is
lost. What is lost is the audit answer and any later analysis of how often each
list moves. Worth fixing in the same migration as B, at near-zero cost.

### D. There is no table for corporate actions

`E03-S02` task 1 fetches corporate action data and `E04-S06` fetches
dividend/split/bonus announcements. **Nothing in the schema can store them.**
Both stories need a `corporate_action` table before they can begin, and §4's
design makes that table the *source of truth* for adjustment rather than a
by-product.

### E. Two configured filters have no data source

`HardFilters` already enforces, in code:

```
min_market_cap_cr    = 5000
min_avg_volume_20d   = 500_000
```

`min_avg_volume_20d` is computable from bhavcopy once `E03-S01` lands.
**`min_market_cap_cr` is not** — market capitalisation appears nowhere in the
`instruments` table and no story in E03 or E04 fetches it.

So a hard filter that config presents as active is, today, unenforceable. That
is worse than not having it: it reads as a safety control and is not one.
Either a story must supply the data or the filter must be explicitly marked
inert until one does. **Recommend: add it to `E04-S06`'s scope** (it fetches
from the same corporate-information sources) rather than leaving a filter that
silently does nothing.

### F. `E03-S05`'s verification task needs a dependency it does not declare

The story depends on `E03-S01`, but task 3 is *"Verify aggregation against
broker-supplied weekly bars"* — which needs `E02-S03`. The aggregation itself
can be built and unit-tested without the broker; only the cross-check against
an independent source needs it. Split the task rather than the story.

### G. The 🔴 fail-closed criterion is already satisfied — but untested

`E04-S01`'s red acceptance criterion is *"A fetch failure blocks the trading day
rather than proceeding blind."*

**E01 already delivers this by construction.** `v_eligible_today` inner-joins
`instrument_daily_status` on today's IST date. If the hazard fetch fails, no
rows exist for today, the join yields nothing, and the eligible universe is
**empty** — the system trades nothing rather than trading blind.

That is the correct behaviour and it was not designed for this story; it falls
out of the view. Two things are still required:

1. **A test that proves it**, because a behaviour nobody has watched fail is
   indistinguishable from one that does not work.
2. **An alert**, because "traded nothing today" and "traded nothing today
   because the ASM fetch 404'd" look identical from the outside, and the second
   needs a human.

---

## 3. BUSINESS RULES THIS SPRINT MUST ENFORCE

Continuing E01's BR numbering.

| # | Rule | Enforcement | Why |
|---|---|---|---|
| **BR-15** | Raw prices are never mutated | Adjustment stored as a factor, never applied in place | See §4. A lost original cannot be recovered and a compounded adjustment is silent |
| **BR-16** | No unadjusted series reaches the indicator engine | The repository has **no method** returning raw prices | Structural, not conventional — you cannot ask for the wrong thing |
| **BR-17** | A hazard fetch failure yields an empty universe, never a partial one | `v_eligible_today` inner join (already built) + an explicit freshness check | Partial hazard data is more dangerous than none: it looks complete |
| **BR-18** | Hazard status is recorded per symbol per day, never overwritten historically | `PRIMARY KEY (symbol_id, trade_date)` — already enforced (BR-8) | A backtest must see the flags as they were, not as they are |
| **BR-19** | Every corporate action is recorded before any price is adjusted for it | `corporate_action` row is the source of the factor | Makes adjustment reproducible and reversible |
| **BR-20** | The holiday calendar is verified against the circular before live trading | `make doctor` fails when `verified_against_nse_circular` is false | Already wired; `E04-S07` flips it honestly |

---

## 4. THE CORPORATE ACTION DECISION

The centrepiece of the sprint. `E03-S02` is 2 days of estimate for something
that can silently invalidate every backtest in the system.

### 4.1 The problem, concretely

A 1:5 split on a ₹2,500 stock makes it ₹500 overnight. Unadjusted, the history
shows an 80% crash that never happened. Every indicator that reads across the
event — a 200-day EMA, an ATR, any breakout level — is wrong. A strategy
backtested over that window will look either brilliant or catastrophic, and
neither is real.

### 4.2 The four options

| # | Approach | Reads | Lossless | Re-adjustment | Verdict |
|---|---|---|---|---|---|
| 1 | Mutate stored prices in place | fast | **no** | compounds silently | **Reject** |
| 2 | Store raw, adjust on every read | slow | yes | free | Rejected on BP-2 budget |
| 3 | Store raw + adjusted columns | fast | yes | rebuild job | Workable, doubles width |
| 4 | **Store raw + adjustment factors** | fast | yes | **bounded, idempotent** | **Recommended** |

**Option 1 is what the current `is_adjusted` boolean implies, and it is the one
that must not be built.** It is the default anyone reaches for, it works
perfectly until the second corporate action, and the failure is invisible.

**Option 2** is the textbook-correct answer and is genuinely tempting: store
truth, derive everything. It is rejected only because `warm_up_batch` must load
~150 symbols × 3 timeframes inside the 45-minute pre-market window, and joining
an actions table per row per read puts that at risk. Note this is a *measured*
trade-off, not an assumption — if the join turns out cheap, revisit it.

### 4.3 Recommended design — Option 4

Two factor columns on `ohlcv`, defaulting to `1.0`:

```
price_adj_factor   NUMERIC(18,10) NOT NULL DEFAULT 1.0
volume_adj_factor  NUMERIC(18,10) NOT NULL DEFAULT 1.0
```

- **Stored OHLC is always raw**, exactly as the exchange published it.
- **Adjusted price = raw × `price_adj_factor`.**
- **Adjusted volume = raw × `volume_adj_factor`.**

Two factors, not one, because they are **not reciprocal for every action type**:

| Action | Price factor | Volume factor |
|---|---|---|
| 1:5 split | × 0.2 | × 5 |
| 1:1 bonus | × 0.5 | × 2 |
| **Dividend** | × (1 − div/close) | **× 1 — unchanged** |

A single factor would silently corrupt volume on every dividend, and volume
feeds the liquidity filter and the volume-ratio indicator.

**Re-adjustment becomes a recompute, not a patch.** When a new action is
announced, the factors for every bar before its ex-date are recalculated *from
the full action history*, from scratch. That is idempotent: running it twice
produces the same answer, which is precisely what Option 1 cannot promise. A
mis-entered action is fixed by correcting the `corporate_action` row and
re-running — the raw prices were never touched.

**BR-16 is enforced structurally.** `BarRepository` returns adjusted values
only. There is no `latest_n_raw()`. Backtest and indicator code cannot ask for
unadjusted data because no method offers it; a separate, explicitly-named
`raw_bars_for_audit()` exists for reconciliation and is not used by anything in
the trading path.

### 4.4 The verification that makes this trustworthy

`E03-S02`'s acceptance is *"A known 1:5 split produces a continuous price series
across the event."* That is necessary but weak — it proves one case.

Add, per `DEVELOPMENT_PROCEDURE.md §4.3` for a 🔴 story:

- **A property test:** for any generated sequence of actions applied in any
  order, the resulting factors equal those from applying them chronologically.
  Order-independence is the property that makes re-adjustment safe.
- **A reconciliation test:** the adjusted series for a real symbol across a real
  split matches an independent source (broker-supplied adjusted history) within
  a tick. This is the one that catches a wrong *formula*, which a self-consistent
  test never will.
- **An idempotency test:** running re-adjustment twice changes nothing.

### 4.5 What implementation found that this analysis did not

All three were caught by *running* code, not by reading it — which is the point
`CLAUDE.md` makes about static review. Each is now covered by its own test.

**1. Order independence was only approximate.** The property test in §4.4 was
supposed to confirm a design that was already correct. It failed instead:

```
raw=[(1, 1, 2), (2, 1, 2), (3, 1, 3)]
Decimal('0.08333333333333333333333333332') !=
Decimal('0.08333333333333333333333333330')
```

`Decimal` division rounds to 28 significant digits, so multiplying pre-divided
quotients is not exactly associative. Combining the same three splits in two
different orders gave answers differing in the last digits. Every scalar test
passed; only the property test over generated orderings could see it.

Fixed by holding each factor as an exact *ratio* — numerator and denominator
kept separate — and dividing once at the end. Actions arrive from a feed in
whatever order the source lists them, so "nearly order-independent" is not a
property, it is a slow leak.

*Reusable lesson: a property test is worth writing even when you are confident,
and especially then. It is the only kind that explores orderings you did not
think of.*

**2. Restore from archive silently skipped adjustments.** Archived bars are not
in `ohlcv`, so every corporate action recorded while they were away updated the
live bars and missed them. Their stored factors are frozen at archive time.
Restoring them unchanged splices an unadjusted segment onto an adjusted series —
no error, no failing query, a fabricated price gap that looks exactly like a
real move. `restore_bars` now recomputes factors for every restored symbol;
because recompute rebuilds from the full history, one call heals the range no
matter how many actions were missed.

*Reusable lesson: check every path by which rows LEAVE and RE-ENTER a table, not
just the write path. Archive/restore is a second write path wearing a disguise.*

**3. Concurrent recompute could lose an action.** Two recomputes for one symbol
can interleave: A reads the action list, B inserts a newly-announced bonus and
recomputes with both, then A commits factors derived from the shorter list and
the bonus is silently undone. Row locks do not prevent this — both transactions
write the same rows with internally consistent values, so the database sees
nothing wrong. Now takes a per-symbol transaction-scoped advisory lock *before*
the read, the same pattern the audit chain uses.

*Reusable lesson: "both writers are self-consistent" is exactly when row locking
fails to protect you. Serialise on the READ when the write depends on it.*

---

## 5. DATA SOURCES AND THEIR FRAGILITY

E04 reads public NSE files and pages. **Assume every one of them will break.**
They are not versioned APIs with deprecation policies; they are files whose
column order changes without notice.

| Story | Source | Refresh | Known fragility |
|---|---|---|---|
| `E03-S01` | NSE bhavcopy (daily EOD ZIP/CSV) | 05:30 daily | URL and filename format have changed historically |
| `E04-S01` | ASM (short + long term), GSM lists | pre-market | Published as web tables/CSV; layout changes |
| `E04-S02` | T2T segment list | pre-market | Often bundled with surveillance publications |
| `E04-S03` | F&O ban list | daily pre-market | Published as a plain list; format stable but URL moves |
| `E04-S04` | Price band file | daily | Per-symbol band percentages |
| `E04-S05` | CAS-scope classification | on change | Derived from Category-I / F&O eligibility, not a single file |
| `E04-S06` | Results calendar, corporate announcements | daily | Least structured of all |

### 5.1 The rule every fetcher must follow

> **A parse failure must raise. It must never return an empty list.**

This is the difference between a bad day and a very expensive one. An empty ASM
list does not read as "the fetch failed" — it reads as **"no stock is under
surveillance today,"** which is the most permissive possible answer. The same
inversion applies to T2T and the F&O ban list. Every one of these lists is a
*deny* list, so an empty result silently disables the protection.

Therefore each fetcher must assert a **plausibility floor** before it commits
anything: if the ASM list has fewer than a handful of names, or the bhavcopy has
fewer rows than the known universe, treat it as a failed fetch. A structurally
valid file with no rows is still a failure.

---

## 6. SCHEMA CHANGES REQUIRED

One migration, before any E03/E04 story starts. Roughly 1 day including tests,
and it is not in the current estimates.

```sql
-- Conflict D: corporate actions become the source of truth for adjustment.
CREATE TABLE corporate_action (
    id              BIGSERIAL PRIMARY KEY,
    symbol_id       INTEGER      NOT NULL REFERENCES instruments(id),
    action_type     VARCHAR(24)  NOT NULL,   -- SPLIT | BONUS | DIVIDEND | ...
    ex_date         DATE         NOT NULL,
    ratio_from      NUMERIC(12,4),           -- 1:5 split -> from 1, to 5
    ratio_to        NUMERIC(12,4),
    dividend_amount NUMERIC(14,4),
    announced_at    TIMESTAMPTZ,
    source          VARCHAR(32)  NOT NULL,
    fetched_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_action UNIQUE (symbol_id, action_type, ex_date)
);

-- Conflict A: raw prices stay raw; adjustment is a derived factor.
ALTER TABLE ohlcv
    ADD COLUMN price_adj_factor  NUMERIC(18,10) NOT NULL DEFAULT 1.0,
    ADD COLUMN volume_adj_factor NUMERIC(18,10) NOT NULL DEFAULT 1.0;

-- Conflicts B and C: surveillance severity, not just presence.
ALTER TABLE instrument_daily_status
    ADD COLUMN gsm_stage    SMALLINT,        -- NULL when not in GSM
    ADD COLUMN asm_category VARCHAR(16);     -- SHORT_TERM | LONG_TERM | NULL
```

**Note the hypertable constraint.** `ohlcv` is a compressed hypertable.
`ALTER TABLE ... ADD COLUMN` with a non-null default on a compressed hypertable
may be refused, exactly as adding a foreign key was during the QA pass. **Verify
this against the pinned image before writing the migration** — if it is refused,
the columns must be added nullable and backfilled, or compression temporarily
disabled. This is a known trap now, not a surprise.

`is_adjusted` is kept for one release and repurposed: it becomes "a factor other
than 1.0 applies," derivable from the factors themselves. Dropping it can wait
until nothing reads it.

---

## 7. STORY-BY-STORY NOTES

Only where there is something non-obvious to say.

### E04-S07 · Holiday calendar (1 d, 🔴) — **do this first**
Pure desk work, no dependency, and everything that schedules anything reads it.
Also closes blocker **B3**. The Muhurat trading session decision (§task 3)
should default to **stand down**: it is a one-hour ceremonial session with
atypical liquidity, and no strategy has been validated on it.

### E04-S02 / S03 · T2T and F&O ban (0.5 d each, 🔴)
The simplest stories in the sprint and the highest consequence-per-line. Both
are pure deny lists. §5.1's rule is the whole story: an empty list must raise.

### E04-S01 · ASM / GSM (1 d, 🔴)
Needs the schema change from §6 first. Task 4 — *"alert on newly-added names
currently held"* — is the interesting one: a stock going into ASM overnight
while you hold it is a real scenario, and the correct response is to flag for
exit, not to silently keep the position.

### E04-S04 · Circuit bands (1 d)
Computes upper/lower circuit from the previous close, which means it depends on
bhavcopy (`E03-S01`) for `prev_close` — **a dependency the tracker does not
record.** Sequence `E03-S01` before it, or accept that its first run has no
previous close to work from.

### E04-S05 · CAS classification (1 d, 🔴)
Wires into `MarketCalendar.squareoff_deadline(is_cas_stock=...)`, which already
exists and already implements 15:10 / 15:20 / 15:25. This story only has to
supply the flag correctly. The acceptance criterion is per-position, so the test
must open two positions — one CAS, one not — and assert different deadlines.

### E03-S01 · Bhavcopy (1.5 d)
The `bulk_upsert` path from `E01-S02` already handles idempotent re-runs and
switches to `COPY` above 5,000 rows, so *"backfilling 2 years is re-runnable
without duplicates"* is largely inherited. What is new: holiday-aware date
iteration (use `MarketCalendar.trading_days_between`, do not guess), and
distinguishing "file missing because holiday" from "file missing because the
fetch failed."

### E03-S02 · Corporate actions (2 d, 🔴) — **not in this sprint**
See §9.

---

## 8. TESTING REQUIREMENTS

Beyond the standard tier rules in `DEVELOPMENT_PROCEDURE.md §4.3`:

**Every fetcher gets a fixture-based parse test** using a saved copy of the real
file. When NSE changes a format, the test fails with a parse error rather than
the system quietly filtering nothing. Commit the fixtures; they are small and
they are the only record of what the format looked like when it worked.

**Every deny list gets an "empty means failure" test.** Feed a well-formed but
empty response; assert the fetcher raises rather than returning `[]`.

**The fail-closed path gets an end-to-end test** (§2 G): delete today's
`instrument_daily_status` rows, assert `v_eligible_today` is empty, assert the
system selects no candidates.

**The T2T filter gets a configuration-bypass test.** `exclude_t2t` is already
non-overridable in config; the test should attempt to disable it and assert the
config refuses to load, then assert no T2T symbol reaches a watchlist even if
one is injected downstream.

---

## 9. RECOMMENDED SPRINT SCOPE

The buildable work is **12 days**, plus **~1 day** of schema migration from §6.
Thirteen days against a sprint that has been running at eight or nine.

**Sprint 1 was estimated at 8.5 days and overran.** Committing to 13 repeats
that mistake knowingly, so the recommendation is to split.

### Proposed Sprint 2 — 8.5 days

| Order | Story | Days | Why here |
|---|---|---|---|
| 1 | **Schema migration** (§6) | 1.0 | Blocks `E04-S01` and all of E03-S02; verify the compressed-hypertable ALTER first |
| 2 | `E04-S07` Holiday calendar | 1.0 | No dependency; everything scheduling reads it; closes B3 |
| 3 | `E03-S01` Bhavcopy | 1.5 | Supplies `prev_close` that `E04-S04` needs |
| 4 | `E04-S02` T2T | 0.5 | Highest consequence per line |
| 5 | `E04-S03` F&O ban | 0.5 | |
| 6 | `E04-S01` ASM / GSM | 1.0 | Needs the migration |
| 7 | `E04-S04` Circuit bands | 1.0 | Needs bhavcopy `prev_close` |
| 8 | `E04-S05` CAS classification | 1.0 | Wires into the existing calendar |
| 9 | `E04-S06` Earnings calendar + market cap (§2 E) | 1.0 | |

**Deferred to Sprint 3:** `E03-S02` (2 d, 🔴), `E03-S04` (1.5 d), `E03-S05`
(1 d) — joining `E02-S01/S02/S03`.

### Why defer `E03-S02` specifically

It is the only 🔴 story of the three, it is the one this document spends a whole
section on, and it is the one where a quiet mistake invalidates every backtest.
`DEVELOPMENT_PROCEDURE.md §4.4` says a 🔴 story is never merged at the end of a
session; the same reasoning applies to the tail of a sprint. It deserves a fresh
start and a second read, not the last day and a half of a full sprint.

It also gains from waiting: `E04-S06` fetches corporate announcements from the
same sources, so doing that first means `E03-S02` inherits a working fetcher
rather than building one under time pressure.

---

## 10. OPEN QUESTIONS

| # | Question | Recommendation |
|---|---|---|
| Q1 | Can `ohlcv` take new columns while compressed? | **Verify before writing the migration.** Adding an FK was refused during QA; assume nothing |
| Q2 | Adjust for dividends, or only splits and bonuses? | **Splits and bonuses only, initially.** Dividend adjustment is standard for total-return analysis but changes every historical price by a small amount, and intraday strategies do not hold across ex-dates. Record dividends; do not apply them yet |
| Q3 | How far back should the bhavcopy backfill go? | **2 years.** Enough for a 200-day indicator plus a validation holdout, cheap to extend later, and it keeps the first backfill to hours rather than days |
| Q4 | Where does market cap come from (§2 E)? | Fold into `E04-S06`, or explicitly mark `min_market_cap_cr` inert until sourced. **Do not leave it looking active** |
| Q5 | Muhurat session | **Stand down.** One ceremonial hour, atypical liquidity, no validated strategy |
| Q6 | Automated holiday fetch, or manual transcription? | **Manual for the first year**, with `make doctor` enforcing the verified flag. An automated fetch that silently returns a partial list is worse than a file someone read once |

---

## 11. WHAT THIS SPRINT DOES NOT DO

Stated so nobody expects it:

- **No broker connection.** That is E02.
- **No live prices.** That is E05.
- **No indicators.** E06 consumes what this sprint stores.
- **No trading, paper or otherwise.**

At the end of Sprint 2 the system knows which stocks it must not touch, and has
two years of daily history for the rest. It still cannot see a live price.

---

*Companion to `EPIC01_TECHNICAL_SPEC.md`. Governed by
`DEVELOPMENT_PROCEDURE.md`. Update as implementation reveals what this document
got wrong — particularly §4, which should be re-tested against a real split
before the design is trusted.*
