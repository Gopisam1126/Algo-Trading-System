# ENGINEERING STANDARD

**The mandatory process for every piece of development and research in this
project.**

> **Invocation.** When this file is named — "follow the engineering standard",
> "per ENGINEERING_STANDARD" — it means: execute this document, in order, for
> the work at hand. Not as inspiration. As a procedure with gates.

**Version 1.0 · 24 August 2026 · Supersedes `DEVELOPMENT_PROCEDURE.md`**

---

## Why this document is shaped the way it is

Large engineering organisations converge on the same handful of controls
because the same handful of failures keep happening. This standard borrows the
ones that fit a single-operator, safety-critical system and discards the ones
that only make sense at organisational scale.

| Practice | Where it comes from | What it is here |
|---|---|---|
| Design doc before code | Google | §2 Design Record, for anything non-trivial |
| Security Development Lifecycle, threat modelling | Microsoft SDL | §5, with a STRIDE-lite pass |
| Correction of Errors, blameless postmortem | Amazon COE | §9, and the anti-pattern catalogue in §11 |
| Operational Readiness Review | Amazon ORR | §8 promotion gate |
| Testing pyramid + mutation testing | Google, industry | §4 the verification ladder |
| Error budgets, fail-closed defaults | Google SRE | §1 invariants |
| Trunk-based development, gated promotion | DORA | §7 |
| Architecture Decision Records | Thoughtworks | §2.3 |

**What makes this project different from a normal service:** a bug does not
show an error page, it loses money silently. Almost every rule below exists
because a *silent wrong answer* is worse than a crash, and the system must be
biased towards refusing to act.

---

## 0. The five laws

Everything else is elaboration.

1. **Static review does not catch behaviour. Only execution does.**
   Six defects in this repository passed a clean read and were caught by
   running something. If you assert a property, write the probe that would
   catch it being false.

2. **Fail closed, always.** Missing data, a timeout, an unreachable component,
   an unparseable value — every one of them means *do not trade*. A default
   that lets the system proceed on absent information is a bug even when it
   never fires.

3. **Make the dangerous thing impossible, not documented.** A comment saying
   "do not call this in the trading path" is a wish. A type that cannot
   express the dangerous state is a guarantee. Prefer structure over
   discipline every time.

4. **Declared is not implemented; present is not used; covered is not
   detected.** Three separate illusions, all of which have bitten this
   codebase. §11 has the receipts.

5. **Say what is true, including about your own work.** If a claim in a
   document turns out to be wrong, correct the document and record what the
   wrong belief cost. The value of this file is entirely in its honesty.

---

## 1. Non-negotiable invariants

These are enforced in code and tested in `tests/security/test_safety_invariants.py`.
Wanting to relax one means you have probably found a design conflict, not an
obstacle. Stop and raise it.

1. `Recommendation` never gains a sizing field.
2. Hard bounds in `common/config.py` are code, not configuration.
3. No `eval`, `exec` or dynamic import in the strategy path, ever.
4. Secrets never render. `SecretString`, and nothing else, holds a credential.
5. Every position has a stop and a time exit — enforced by the schema, so a
   strategy that omits either does not parse.
6. Fail closed.

**Conventions that are not negotiable either:** `Decimal` for all money;
timezone-aware UTC everywhere with IST only at display and market-hours
boundaries; bars aligned to the 09:15 session start rather than wall-clock
hours; per-stock square-off deadlines.

---

## 2. Phase 1 — Intake and design

### 2.1 Definition of Ready

An item may not start until all are true. If one is false, say so rather than
starting anyway.

| | Gate |
|---|---|
| R1 | The item exists in `BACKLOG_Tracker.xlsx` with an ID, estimate, risk tier and priority. Work with no story? **Create the story first.** |
| R2 | Its dependencies are `Closed` — checked in the tracker, not from memory |
| R3 | No open blocker covers it. An absolute blocker means stop and document, never work around |
| R4 | The governing design document has been read **this session** |
| R5 | You can state the acceptance criteria without looking them up |

### 2.2 The analysis pass — conflict and bug proofing

Before writing code, spend the time to find the conflict. It is always cheaper
here than after.

1. **Read the specification section in full.** Not the summary.
2. **Cross-check the spec against what exists.** Specs drift. Run any SQL or
   snippets the spec contains — this repository has shipped a spec whose DDL
   was not valid PostgreSQL.
3. **Trace every path data enters and LEAVES by.** Archive/restore, backfill,
   migration and repair paths are second write paths that do not look like
   write paths. A rule enforced on one and not the others is not enforced.
4. **Check the version reality.** Library defaults change. Inspect the
   *installed* package, not the documentation — two blockers in this project
   were closed simply by reading the SDK actually on disk.
5. **Ask what a wrong answer would look like.** If the failure mode is a
   plausible number rather than an exception, that dictates the whole test
   strategy.

### 2.3 Design Record

For anything beyond a local change, write down — in the module docstring or an
ADR — the following four things. Whoever reads this in six months needs the
*why*, and the why is what erodes first.

- **The decision**, in one sentence.
- **The alternative rejected**, and what it would have cost.
- **The failure it prevents**, concretely.
- **What would make this decision wrong**, so a future reader can tell whether
  it still holds.

### 2.4 Capability check

Before designing against a component: does it actually produce what you plan to
consume? Both a declared timeframe the engine never computes and a declared
primitive with no implementation have shipped here. Enumerate the real
capability and bind to it — at load time, loudly.

---

## 3. Phase 2 — Build

### 3.1 Order of work within an item

1. Types and contracts first — they constrain everything downstream.
2. The failing probe for the riskiest claim.
3. The implementation.
4. The control test (§4.3).
5. Wiring, last.

### 3.2 Structure over discipline

Ranked by strength. Always reach for the highest rung available.

| Strength | Mechanism | Example from this codebase |
|---|---|---|
| Strongest | The state cannot be represented | `ExitRules` requires a stop, so "no stop" does not parse |
| | Construction validates | `StrategyEvaluator.__init__` verifies capability, so holding one proves the strategy can run |
| | Runtime check that raises | Column identifiers matched against an allowlist before SQL interpolation |
| | Test asserts it | Most things |
| Weakest | A comment says so | Almost never sufficient alone |

If you write a comment warning about something, ask what it would take to move
one rung up.

### 3.3 Three-valued thinking

Where a value can be *absent*, model absence explicitly rather than collapsing
it into a default. The strategy runtime answers True / False / **UNKNOWN**, and
that third state is load-bearing: two-valued logic would make `none_of` read
"I could not evaluate this" as "the forbidden thing is absent", and **missing
data would become permission to trade.**

Whenever you are about to write `or 0`, `or False`, or `except: pass`, check
whether you are silently converting *unknown* into *fine*.

---

## 4. Phase 3 — The verification ladder

Climb as far as the risk warrants. 🔴 safety-critical items go to the top.

### Rung 1 — The probe rule

For every safety-relevant claim, write the test that would catch it being
false.

| Claim | Probe |
|---|---|
| "this validator rejects X" | Construct X; assert it raises |
| "secrets are redacted" | Pass a real-shaped secret; grep the output |
| "config rejects bad values" | Set the bad value; assert it fails |
| "the deadline is respected" | Compare against the real broker deadline |
| "this primitive is implemented" | Assert declared and implemented are the same set |

### Rung 2 — The control

Every restrictive test needs its opposite. A validator that rejects everything
passes every hostile-input test. When symbol validation was added, the control
was that `M&M`, `BAJAJ-AUTO` and `L&TFH` still work.

### Rung 3 — Property-based tests

Use where the input space is larger than your imagination: money arithmetic,
ordering, serialisation round-trips, and any truth table with more than a
handful of rows. The tri-state composition here has 3ⁿ rows; example tests
would have covered the ones I happened to think of.

### Rung 4 — Mutation testing

**Coverage says a line ran. It does not say a test would notice if that line
were wrong.** For safety-critical paths, inject plausible defects into real
source and confirm the suite fails.

A surviving mutation is a hole in the *tests*, not the code. The two survivors
in this project were both the same shape: the test called a helper directly
rather than the path that calls it, so reverting the caller to buggy behaviour
changed nothing any assertion could see.

**Test the public entry point, not just the helper it delegates to.**

### Rung 5 — Integration and end-to-end

A component tested against its own fixtures agrees with itself by
construction. Interface mismatches only appear when two real components meet.
The single end-to-end test in this repository found a HIGH-severity defect on
its first run that no component test could have seen.

### Rung 6 — Adversarial and chaos

For anything touching money or credentials: assume the input is hostile and ask
what the worst outcome is. Degrade a dependency — kill the feed, empty the
snapshot, corrupt the cache — and confirm the system refuses rather than
guesses.

### 4.1 Test hygiene

- **A test must not depend on a defect existing.** Two BR-20 tests here used
  the incomplete holiday list as their fixture; fixing the list broke them.
  They were asserting the gate fires while proving nothing about the gate.
- **A test that passes alone and fails in the suite is a finding, not a
  flake.** It has caught dead-tuple perf drift, a fixture wiping a shared
  table, and alembic silently disabling logging process-wide.
- **Name the test after the behaviour**, so a failure reads as a sentence
  about the system.
- **Put the reasoning in the docstring** — what breaks if this fails, and how
  it was found. A test nobody understands gets deleted during the next
  refactor.

---

## 5. Phase 4 — Security

### 5.1 Automated, every time

```bash
ruff check . && ruff format --check .
mypy src
bandit -r src/ --severity-level medium --confidence-level medium
pip-audit --skip-editable            # NOT --strict; see DEPLOYMENT.md
pytest tests/security/ -q
```

A finding that is genuinely safe gets a **suppression backed by an executed
check**, never a bare comment. When SQL identifiers had to be interpolated, the
fix was an allowlist match immediately before use — so the suppression is
justified by code that runs.

### 5.2 STRIDE-lite, against the diff

Ask each of these about what changed:

- **Spoofing** — can input claim to be something it is not? Symbols come from
  the broker's dump and reach log lines, Redis keys and order payloads.
- **Tampering** — can a value be changed between write and read? Snapshots
  round-trip through Redis; a corrupted one produced a *diverging* EMA that
  emitted plausible numbers forever.
- **Repudiation** — is the audit chain intact? Does anything write a NULL
  timestamp?
- **Information disclosure** — can a credential reach a log, an exception, a
  pickle, a crash dump, or a URL? Check every rendering path separately;
  `__repr__`, `__str__`, `__format__` and `__reduce__` are four different
  holes.
- **Denial of service** — is anything unbounded? Collections, retries, loops,
  and the size of text echoed into an error message.
- **Elevation** — can data become code? Can a parameter select a code path by
  name?

### 5.3 The pre-commit sweep — mandatory

Never commit `.env`, a credential, or anything under `data/`. Grep the diff for
real-shaped secrets, not just the word "password".

### 5.4 Supply chain

Dependencies are attack surface whether or not you call them. A vulnerable
package that is *present and importable* is a finding — "we do not use it" is
not a control. Verify by importing and inspecting `sys.modules`, not by reading
your own import statements.

---

## 6. Phase 5 — The multi-role review

Before an item is done, review it deliberately from each seat. The value is in
the *switch*: each role asks questions the others do not.

| Role | The question | What it has actually found here |
|---|---|---|
| **Business Analyst** | Does this meet the acceptance criteria as WRITTEN, and is the story really delivered? | A "Closed" story that had shipped declarations with no implementation; a config block parsed and never consulted |
| **Senior Architect** | What does this make hard later? Any cycles, wrong layering, or purity violations? | `abs()` turning maximum disagreement into maximum confluence; randomness in a path required to be replayable |
| **Lead Developer** | Boundary conditions, division by zero, mixed numeric types, error paths | An inverted range reporting a negative width that would reach position sizing |
| **Pentester** | Assume the author is hostile. What is the worst outcome? | NaN crashing the signal path; `-Infinity` disabling a filter in one word; unbounded text echoed into logs |
| **QA Engineer** | Test each story's criteria one at a time | Performance criteria that had never been measured; criteria not met and quietly assumed |
| **QA Lead** | Does it hold END TO END, with real components? | The evaluator trading straight through a feed gap |

---

## 7. Phase 6 — Quality gate and commit

### 7.1 The gate

```bash
make check          # lint + types + tests
make test-safety    # the invariants
make doctor         # config and compliance posture
```

Everything green. A red gate cannot gate anything, so a persistently red check
is worse than no check — fix it or delete it, never normalise it.

### 7.2 The commit message

Record, in prose:

- **What changed and why**, in terms of the failure it prevents.
- **What was found while doing it**, including defects in existing code.
- **What was believed and turned out false.**
- Test counts before and after.

Never skip hooks. Never bypass signing. If a hook fails, fix the cause.

---

## 8. Phase 7 — Promotion (the readiness review)

Promotion to QA asserts *this commit is deployable*. Before promoting:

| | Check |
|---|---|
| P1 | Full suite green, including security tests |
| P2 | `bandit` and `pip-audit` clean |
| P3 | Coverage has not regressed |
| P4 | Mutation testing re-run if a safety-critical path changed |
| P5 | Tracker updated — findings recorded, blockers moved |
| P6 | `git log --no-merges origin/DEV..origin/QA` is **empty** (QA has not diverged) |
| P7 | The promotion message states what was verified, not just what changed |

After promoting, confirm the trees are identical.

---

## 9. Blockers and corrections

### 9.1 Blocker protocol

A blocker is something that **cannot be resolved by writing code**. Not a hard
problem — an impossible one.

1. Record it in the `Blockers` sheet with what it blocks and what would resolve it.
2. **Research it properly before escalating.** Most "blockers" are unresearched
   questions. Two here dissolved on inspecting the installed SDK; one dissolved
   on realising a version pin was declarative.
3. Escalate only what genuinely needs the user: money, credentials, identity,
   a business decision.
4. **De-risk what you cannot resolve.** If procurement is needed, build the
   one-command check that answers it the day it arrives.

### 9.2 The research standard

- **Prefer primary sources**, then the vendor's own forum, then aggregators.
- **Triangulate anything that will become code.** Two sources agreeing is
  weak; two disagreeing is *information*. The NSE holiday list needed three,
  and the disagreement turned out to encode a real distinction between the
  annual circular and a special election closure.
- **State the residual uncertainty** in the code, at the place it matters.
- **Record the date.** Regulatory and API facts expire.

### 9.3 Correction of errors

When something was wrong, write down: what was believed, what was actually
true, how the wrong belief survived, and what now makes it detectable. Add it
to §11. No blame — the mechanism is the interesting part.

---

## 10. Documentation duties

Documentation drifts silently and then misleads confidently.

- **When behaviour changes, update the governing document in the same commit.**
- **When a document is found wrong, fix it and say what the wrong belief cost.**
- **`CLAUDE.md` is loaded into every session** — stale content there is worse
  than stale content anywhere else.
- **The tracker is the system of record** for status, findings and blockers.
  Update `QA Results`, `Security Findings` and `Blockers` as part of the work,
  not afterwards.
- Where this document and a design document disagree on *process*, this one
  wins. On *system behaviour*, the design document wins and this file is
  corrected.

---

## 11. The anti-pattern catalogue

Every entry cost real time here. The rung or phase that catches it is named.

### Illusions of completeness

| Defect | Why review missed it | Caught by |
|---|---|---|
| 27 primitives declared, none implemented — strategies validated and never fired | Absence produces no error; looks like a quiet market | §2.4 capability check; "declared == implemented" probe |
| `Applicability` parsed, hashed, and consulted by nothing | A half-wired field looks identical to a wired one | §6 BA role |
| `gsm_stage`/`asm_category` in the schema but in neither the upsert nor the read | Same shape | Rung 2 — read the column back |
| Registry default named timeframes the engine never computes | The default was never exercised | §2.4 |
| Two production modules at 0% coverage, one the live feed | Reviewed, shipped, never run | Coverage as a gate |

### Silent wrong answers

| Defect | Why review missed it | Caught by |
|---|---|---|
| Pydantic **field** validator could not see later fields — the OHLC check was inert | Reads correctly | Rung 1 |
| `restore()` accepting `period: -5` → a *diverging* EMA emitting plausible numbers | Nothing raises; the value is wrong, not absent | Rung 1 |
| `abs(trend_agreement())` — maximum disagreement reported as maximum confluence | The sign carried the meaning | §6 architect |
| Unbounded calendar walk returning a date 166 days away | Nothing raised | Rung 1 + bounds |
| Adjustment factors only *approximately* order-independent | Every scalar test passed | Rung 3 property test |
| An inverted opening range reporting a NEGATIVE width | Could not arise from ticks — but could from a restore | §6 developer role |

### Fail-open

| Defect | Why review missed it | Caught by |
|---|---|---|
| Evaluator traded through a feed gap because nothing consulted `all_ready` | Both halves existed; the wiring did not | Rung 5 end-to-end |
| `none_of` would have read UNKNOWN as "absent" — missing data as permission | Only visible in the composition | §3.3 + Rung 3 |
| NaN comparing False against everything; `-Infinity` disabling a filter | `Decimal("NaN")` constructs fine and fails only on comparison | §5.2 pentest |
| Parameters validated *after* the data lookup — a broken strategy hid behind a quiet market | Ordering | §5.2 |

### Environment and supply chain

| Defect | Why review missed it | Caught by |
|---|---|---|
| `autobahn` believed unimported; `kiteconnect/__init__` imports `.ticker` unconditionally | "We do not use it" is not a control | §5.4 — `pip-audit` + `sys.modules` |
| Alembic's `fileConfig` disabling every existing logger | In production: a system that keeps trading with no record | §4.1 suite-order finding |
| `SQLAlchemy Enum` defaulting to `create_constraint=False` | Library default changed | §2.2 version reality |
| Async psycopg cannot use Windows' default event loop | Invisible on Linux and CI | §4 run on the target platform |
| A perf benchmark drifting 7.9s → 10.9s from dead tuples | Passed alone, failed in suite | §4.1 |

### Test defects

| Defect | Why it survived | Caught by |
|---|---|---|
| Tests using the holiday-list defect as their fixture | Passed for the wrong reason; went quiet when fixed | §4.1 |
| Tests calling a helper rather than the path that calls it | Coverage was 100% | Rung 4 mutation |
| A fixture wiping a shared table | Passed alone | §4.1 |

---

## 12. Per-item checklist

```
INTAKE
[ ] Definition of Ready satisfied (R1-R5)
[ ] Spec read in full, this session
[ ] Every write path traced, including archive/restore/backfill
[ ] Installed library versions inspected, not just documented
[ ] Capability check: does the thing I consume actually produce it?

DESIGN
[ ] Design Record written: decision, alternative, failure prevented, falsifier
[ ] Dangerous states made unrepresentable where possible
[ ] Absence modelled explicitly, not defaulted

BUILD
[ ] Types and contracts first
[ ] Failing probe written before the fix
[ ] Decimal for money; tz-aware UTC; session-aligned bars
[ ] No eval/exec/dynamic import in the strategy path

VERIFY
[ ] Rung 1 probes for every safety claim
[ ] Rung 2 control tests
[ ] Rung 3 properties where the input space is large       (risk-dependent)
[ ] Rung 4 mutation testing on safety-critical paths       (🔴 items)
[ ] Rung 5 end-to-end with real components                 (🔴 items)
[ ] Rung 6 adversarial/degraded-dependency                 (🔴 items)
[ ] No test depends on a defect existing
[ ] Full suite green in ONE run, not just individually

SECURITY
[ ] ruff, mypy, bandit, pip-audit, security suite
[ ] STRIDE-lite against the diff
[ ] Every rendering path checked for credentials
[ ] Nothing unbounded
[ ] Pre-commit secret sweep

REVIEW
[ ] Six roles, deliberately switched

SHIP
[ ] make check / test-safety / doctor
[ ] Tracker: QA Results, Security Findings, Blockers
[ ] Documents updated in the same commit
[ ] Commit message records findings and falsified beliefs
[ ] Promotion checks P1-P7; trees identical afterwards
```

---

## 13. Stopping safely

When approaching a usage or time limit, stop at a **safe point** rather than
mid-change:

1. Finish the smallest coherent unit — never leave a half-applied refactor.
2. Run the full gate.
3. Update the tracker and documents.
4. Commit with a message that stands alone.
5. Push, promote if the gate is green.
6. State plainly: what was done, what was found, what is next, what is blocked.

A clean stop with less delivered beats a broken tree with more.

---

*Applies to every item in `BACKLOG_Tracker.xlsx` and to all research. This
document is expected to grow: when something new goes wrong, §11 gains a row.*
