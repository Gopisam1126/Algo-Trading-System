---
name: sdlc
description: Drive one backlog item end-to-end for the AI Algo Trading system — orient, business analysis, research and blocker resolution, architecture, development, four rounds of QA, promote to the QA branch, then SIT with defects logged to the tracker and artifact. Stops hard before PROD. Use when asked to build the next thing, take a story to QA, continue development, or run the full delivery lifecycle.
---

# SDLC — the delivery lifecycle for this project

**Invoked with no argument, this means: find the next item, take it all the way
to SIT, and stop.** With an argument (`/sdlc E14-S02`), it means that item.

This is a system that places real orders with real money. The lifecycle below
borrows the controls large engineering organisations converge on — Google's
design docs and testing pyramid, Microsoft's SDL and bug bar, Meta's SEV
classification and shadow testing, Amazon's operational readiness review and
correction-of-errors — and keeps only what earns its place at one-operator
scale.

`Documents/ENGINEERING_STANDARD.md` is the **reference**: the invariants, the
verification ladder, the anti-pattern catalogue. This file is the **driver**:
the ordered phases, their exit gates, and what to do when one will not pass.
Where they disagree on process, the standard wins and this file is corrected.

---

## The two rules that override everything

### 1. Never push to PROD. Ever. Without explicit permission.

Not "unless CI is green". Not "unless SIT passed". **Only when the user says
so, in this conversation, in response to being asked.**

`promote.yml` already carries an unconditional `exit 1` on the PROD path. That
gate stays. Do not remove it, do not work around it, do not suggest removing
it. When SIT is clean, report that PROD is *ready* and **stop**.

### 2. Do not get stuck.

Getting stuck is a failure mode, and §11 below is the procedure for not
having it. The short version: decide what you can decide, escalate only what
genuinely needs money, credentials, identity or a business choice, and always
leave the tree green.

---

## Phase 0 — Orient

**Never work from memory or from a previous turn's summary.** Establish ground
truth from the code, the tracker and CI, in that order of authority.

```bash
git fetch -q origin
git rev-parse --short origin/DEV origin/QA HEAD
git status --short
git log --no-merges --oneline origin/QA..origin/DEV     # unpromoted work
```

Then, in the repo:

- **What is built** — line counts per package under `Code/src/algotrader`. An
  `__init__.py`-only package is *empty*, whatever the tracker says.
- **What CI says** — the latest run per branch, via the GitHub Actions API.
- **What the tracker says** — `Documents/BACKLOG_Tracker.xlsx`, sheets
  `Backlog`, `Blockers`, `QA Results`, `Security Findings`, `SIT Defects`.

**Reconcile before proceeding.** Where the tracker and the code disagree, the
code is right and the tracker gets corrected — record the correction.

**Exit gate:** you can state, in one sentence each, what is built, what is
next, and what is blocking. If a previous run left work unpromoted or CI red,
that is the next item — finish it before starting anything new.

---

## Phase 1 — Business analysis

You are the BA. The question is not "can this be built" but **"is this the
right thing, and is it ready".**

Read the full story row: title, user story, tasks, acceptance criteria,
dependencies, risk tier, estimate, notes, build concerns.

### Definition of Ready

| | Gate |
|---|---|
| R1 | The item exists in the tracker with an ID, estimate, risk tier and priority. Work with no story? **Create the story first.** |
| R2 | Dependencies are `Closed` — checked in the tracker, not remembered |
| R3 | No open blocker covers it |
| R4 | The governing design document has been read **this session** |
| R5 | You can state the acceptance criteria without looking them up |

**If R2 fails, do not start — but do not stall either.** Verify whether the
dependency is real. A *build* dependency is something you cannot compile or
test without. A *runtime wiring* dependency is something the finished feature
needs in production. Only the first blocks.

> This has already happened once. E14-S01 declared a dependency on E13-S04,
> which chains to the entire AI layer — meaning the deterministic risk engine
> could not start until an AI client existed. It needed the `Recommendation`
> *type*, not the emitter. The dependency was corrected with the reasoning
> recorded on the story, and the work proceeded the same day.

Correct a mis-stated dependency in the tracker, write down why, and continue.

### Value and scope

- **What breaks if this is wrong?** That answer sets the test depth.
- **What is explicitly out of scope?** Write it down; scope creep in a
  safety-critical path is how a 1.5-day story becomes a week.
- **Is the acceptance criterion testable as written?** "Works correctly" is
  not. Rewrite it in the tracker if it is not falsifiable.

**Exit gate:** DoR satisfied (or a correction recorded), criteria are
falsifiable, scope boundary written.

---

## Phase 2 — Research and blocker resolution

**Do this before designing, not when stuck.**

### The research standard

- **Primary sources first** — the vendor's own docs and forum, the installed
  package, the actual regulation. Then aggregators.
- **Triangulate anything that becomes code.** Two sources agreeing is weak;
  two disagreeing is *information*, and the disagreement usually encodes a
  real distinction.

  > The NSE 2026 holiday list needed three publications. Two disagreed — one
  > omitted Guru Nanak Jayanti, the other the Maharashtra election closure.
  > The explanation mattered: one was in the annual circular, the other a
  > separate special closure. Both were real.

- **Inspect the installed artifact, not the documentation.** Two blockers here
  closed by reading the SDK on disk. One "unfixable" CVE closed by discovering
  a version pin was declarative rather than a runtime requirement.
- **Record the date.** Regulatory and API facts expire.

### Blocker protocol

A blocker is something that **cannot be resolved by writing code**. Not hard —
impossible.

1. **Research it properly before escalating.** Most "blockers" are
   unresearched questions.
2. **Escalate only what genuinely needs the user:** money, credentials,
   identity, a business decision.
3. **De-risk what you cannot resolve.** If procurement is required, build the
   one-command check that answers it the day it arrives.
4. Record it in the `Blockers` sheet: what it blocks, what would resolve it,
   who owns it.

**Exit gate:** every fact the design depends on is either verified or recorded
as an explicit assumption with its risk.

---

## Phase 3 — Architecture

You are the architect. The question is **"what does this make hard later".**

### Design record

For anything beyond a local change, write down — in the module docstring or an
ADR — four things:

- **The decision**, in one sentence.
- **The alternative rejected**, and what it would have cost.
- **The failure it prevents**, concretely.
- **What would make this decision wrong**, so a future reader can tell whether
  it still holds.

### Structure over discipline

Reach for the highest rung available:

| Strength | Mechanism |
|---|---|
| Strongest | The dangerous state cannot be represented |
| | Construction validates (holding the object proves the property) |
| | A runtime check that raises |
| | A test asserts it |
| Weakest | A comment says so |

If you write a warning comment, ask what it would take to move one rung up.

### Architecture review checklist

- **Layering** — does anything lower import something higher? Run the import
  graph, do not eyeball it.
- **Purity** — can this path reach I/O, a clock, or randomness? If it must be
  replayable, assert that structurally.
- **Capability** — does the thing you consume actually produce what you plan
  to read? Bind to real capability at load time, loudly.
- **Schema fit** — do the names and values you are about to persist fit the
  columns? Check widths.

  > `decision_log.stage` is `String(28)`. Three of the fourteen risk-check
  > names in the architecture doc are longer. Using them would have failed the
  > audit insert *at the moment a rejection happened* — precisely when the
  > record is wanted.

- **Absence** — is every "missing value" path modelled explicitly rather than
  defaulted? `or 0`, `or False` and `except: pass` are where unknown silently
  becomes fine.

**Exit gate:** design record written; the dangerous states are unrepresentable
or guarded; no new cycle; no undeclared capability.

---

## Phase 4 — Development

### Order of work

1. **Types and contracts first** — they constrain everything downstream.
2. **The failing probe** for the riskiest claim.
3. The implementation.
4. **The control test** (see QA-1).
5. Wiring, last.

### Non-negotiable invariants

Enforced in code, tested in `tests/security/test_safety_invariants.py`. Wanting
to relax one means you have probably found a design conflict — stop and raise
it.

1. `Recommendation` never gains a sizing field.
2. Hard bounds in `common/config.py` are code, not configuration.
3. No `eval`, `exec` or dynamic import in the strategy path, ever.
4. Secrets never render — `SecretString`, and nothing else, holds a credential.
5. Every position has a stop and a time exit.
6. **Fail closed.**

Plus: `Decimal` for all money; timezone-aware UTC everywhere with IST only at
display and market-hours boundaries; bars aligned to the 09:15 session start;
per-stock square-off deadlines.

**Exit gate:** `ruff check`, `ruff format --check`, `mypy src` all clean.

---

## Phase 5 — QA round 1: component

You are the QA engineer. Test each acceptance criterion **one at a time**.

- **The probe rule.** For every safety-relevant claim, write the test that
  would catch it being false. Construct the invalid thing and assert it
  raises; pass a real-shaped secret and grep the output.
- **The control.** Every restrictive test needs its opposite. A validator that
  rejects everything passes every hostile-input test.
- **Property-based tests** where the input space is larger than your
  imagination — money arithmetic, ordering, round-trips, truth tables with
  more than a handful of rows.

**Exit gate:** every acceptance criterion has a named test; controls present;
the component suite is green.

---

## Phase 6 — QA round 2: integration

**A component tested against its own fixtures agrees with itself by
construction.** Interface mismatches only appear when two real components meet.

- Wire the real neighbours, not mocks, wherever it is affordable.
- Exercise the seams: what one side produces and the other consumes.
- Check the *sequence*, not just the endpoint.

> The single end-to-end test in this repository found a HIGH-severity defect on
> its first run: the strategy evaluator traded straight through a simulated
> feed gap, because both halves existed and nothing consulted `all_ready`.

**Exit gate:** at least one test exercises this item together with its real
upstream and downstream.

---

## Phase 7 — QA round 3: adversarial and security

You are the pentester. **Assume the input is hostile and the author is
compromised.** Ask what the worst outcome is.

Run the automated gates:

```bash
bandit -r src/ --severity-level medium --confidence-level medium
pip-audit --skip-editable          # NOT --strict; see DEPLOYMENT.md
pytest tests/security/ -q
```

Then STRIDE-lite against the diff:

- **Spoofing** — can input claim to be something it is not?
- **Tampering** — can a value change between write and read? Snapshots
  round-trip through Redis; a corrupted one produced a *diverging* EMA that
  emitted plausible numbers forever.
- **Repudiation** — is the audit chain intact? Any NULL timestamps?
- **Information disclosure** — can a credential reach a log, an exception, a
  pickle, a crash dump, or a URL? `__repr__`, `__str__`, `__format__` and
  `__reduce__` are four separate holes.
- **Denial of service** — is anything unbounded? Collections, retries, loops,
  and the length of text echoed into an error message.
- **Elevation** — can data become code? Can a parameter select a code path by
  name?

A finding that is genuinely safe gets a **suppression backed by an executed
check**, never a bare comment.

**Exit gate:** SAST and dependency audit clean; every STRIDE question answered;
findings recorded in `Security Findings`.

---

## Phase 8 — QA round 4: regression, mutation and the full gate

You are the QA lead. The question is **"does the whole thing still hold".**

```bash
cd Code
ruff check src/ tests/ scripts/ && ruff format --check src/ tests/
mypy src/
pytest tests/ -q --cov=src/algotrader --cov-report=term
make test-safety
make doctor
```

### Mutation testing — required for 🔴 safety-critical items

**Coverage says a line ran. It does not say a test would notice if that line
were wrong.** Inject plausible defects into real source and confirm the suite
fails. A surviving mutation is a hole in the *tests*.

Write the harness to `scratchpad/`, run it, revert every mutation, and record
the result. When a mutation survives, **verify why before fixing** — one
survivor here turned out benign, because Pydantic already copied the list the
mutation targeted.

> Two survivors on the first run of the strategy suite were the same shape:
> the tests called a helper directly rather than the path that calls it, so
> reverting the caller changed nothing any assertion could see. **Test the
> public entry point, not just the helper it delegates to.**

### Regression rules

- **Coverage must not regress.** Compare against the previous run.
- **A test that passes alone and fails in the suite is a finding, not a
  flake.** It has caught dead-tuple perf drift, a fixture wiping a shared
  table, and alembic silently disabling logging process-wide.
- **No test may depend on a defect existing.** Two BR-20 tests used the
  incomplete holiday list as their fixture and went quiet when it was fixed.

**Exit gate:** every gate green in one run; mutation score recorded; coverage
flat or better.

---

## Phase 9 — QA sign-off and promotion

### Sign-off checklist

| | Check |
|---|---|
| S1 | All four QA rounds passed |
| S2 | Findings recorded in `QA Results` and `Security Findings` |
| S3 | Story status updated; any new stories created for discovered work |
| S4 | Documents updated **in the same commit** as the behaviour they describe |
| S5 | Commit message records what was found, including falsified beliefs |
| S6 | Pre-commit sweep: no `.env`, no credential, nothing under `data/` |

### Commit and push

```bash
git add -A && git commit -F - <<'EOF'
<what changed and the failure it prevents>
<what was found while doing it, including defects in existing code>
<what was believed and turned out false>
<test counts before and after>

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
git push origin DEV
```

**Wait for CI to go green on DEV before promoting.** Poll the Actions API; do
not assume.

### Promote

```bash
python scripts/promote.py --from DEV --to QA
```

This strips `Documents/` from the QA tree, refuses on divergence, never
force-pushes, and verifies the pushed tree against a freshly recomputed
expected tree. **Do not hand-roll a promotion with `commit-tree`.**

**Exit gate:** CI green on both DEV and QA; `Documents/` absent from QA.

---

## Phase 10 — SIT (system integration testing)

**SIT runs against the promoted QA branch and asks a different question from
QA: does the SYSTEM behave, under conditions resembling a real session?**

QA asks "is this component correct". SIT asks "does the assembled thing do
the right thing when something goes wrong".

### What SIT covers

- **Realistic scenario replay** — a full session shape, not a fixture.
  Warm-up from history, session open, the item under test in its real
  sequence, square-off.
- **Degraded dependencies** — kill the feed mid-session; empty the snapshot;
  make the broker time out; corrupt a cached value. **The system must refuse,
  not guess.**
- **Boundary conditions of the trading day** — 09:15 open, the 09:15–09:20
  no-trade window, the per-stock square-off deadline, a holiday, the Muhurat
  Sunday session.
- **Cross-cutting invariants** — no order without a stop; no order path
  bypassing risk; the audit chain intact end-to-end; no secret in any log
  produced during the run.
- **Idempotency and restart** — kill the process mid-flight and restart. State
  restored from a snapshot must equal state rebuilt from history.

### SIT is allowed to find things QA cannot

That is the point. A SIT defect is not a QA failure — it is evidence that the
seam only exists in the assembled system. Record it as such.

**Exit gate:** every SIT scenario either passes or has a logged defect with a
severity. **A CRITICAL or HIGH SIT defect blocks the PROD recommendation.**

---

## Phase 11 — SIT defect logging

Every SIT defect goes to **both** places, in the same working session.

### 1. The backlog file — `Documents/BACKLOG_Tracker.xlsx`, sheet `SIT Defects`

| Column | Content |
|---|---|
| SIT ID | `SIT-<NNN>`, sequential |
| Date | ISO date |
| Story | The story under test |
| Severity | See the bug bar below |
| Component | Package or module |
| Status | `Open` / `Fixed` / `Accepted risk` / `Deferred` |
| What happened | Observed behaviour, concretely — values, not adjectives |
| Why component tests missed it | **The most valuable column.** This is the process finding |
| Root cause | The mechanism, not the symptom |
| Fix | What changed |
| Regression test | The test that now catches it |
| Backlog item raised | Story ID, if the fix is its own work |

Then: **raise a backlog story** for any defect not fixed in the same session,
and add a row to `QA Results` if the fix changed behaviour.

### 2. The tracker artifact

Regenerate and republish so the live view matches the workbook:

```bash
python scripts/tracker/export_workbook.py   # workbook -> tracker_data.json
python scripts/tracker/build_tracker.py     # json -> scripts/tracker/tracker.html
```

Then publish `scripts/tracker/tracker.html` to the **same URL** —
`https://claude.ai/code/artifact/624dca55-aa60-47a1-94c2-73be9571cdaf` — via
the Artifact tool with `url` set. Never create a second tracker artifact.

Both scripts resolve their paths from their own location and live in the repo,
**not** in a scratchpad. That is deliberate: the earlier versions lived in a
session temp directory and were lost when it was cleared. A skill that
instructs running a file cannot point at one that evaporates.

### The bug bar

Adapted from Microsoft SDL, calibrated to a system that moves real money.

| Severity | Meaning | Gate |
|---|---|---|
| **CRITICAL** | Could place, size or fail to protect an order incorrectly. Any fail-open. Any secret disclosure. | **Blocks PROD. Fix before anything else.** |
| **HIGH** | Wrong-but-plausible output reaching a decision; audit chain gap; a safety gate that can be bypassed | **Blocks PROD.** Fix this session |
| **MEDIUM** | Degrades correctness or observability without a direct money path; unbounded resource | Fix or raise a story with a date |
| **LOW** | Cosmetic, or a correctness issue in a non-trading path | Raise a story |

**A silent wrong answer is always at least HIGH.** A crash is usually lower
than a plausible wrong number, because a crash announces itself.

---

## Phase 12 — PROD

**STOP.**

Report:

- What was delivered, and the acceptance criteria it satisfies.
- QA results across all four rounds, with the mutation score.
- SIT results, and every defect with its severity and status.
- Whether `Documents/PRE_LIVE_CHECKLIST.md` is satisfied.
- What still needs the user: credentials, static IP, broker paperwork.

Then say plainly whether PROD is *ready*, and **ask**. Do not promote. Do not
prepare the promotion command as if it were about to run.

---

## §11 — Not getting stuck

The failure mode this section exists to prevent is a phase that will not pass
and a session that ends with nothing delivered.

### Decide what you can decide

| Situation | Do |
|---|---|
| Two reasonable designs | Pick the one that makes the dangerous state unrepresentable. Record the alternative. |
| A spec and the code disagree | The spec describes intent, the code describes reality. Follow the spec, fix the code, record the gap. |
| A dependency looks wrong | Verify it. Correct it with justification. Continue. |
| A test fails and you cannot see why | Reproduce it in isolation, then bisect. A suite-order failure is a real finding. |
| Research is inconclusive after three sources | Record what was tried, state the residual uncertainty *in the code at the place it matters*, and take the safest branch. |
| A gate cannot pass | **Split the story.** Deliver the part that passes; raise a story for the rest. |

### Escalate only these

Money. Credentials. Identity/KYC. A business decision only the owner can make.
Everything else is research.

### Time-box

If a single obstacle has consumed more than roughly a fifth of the story's
estimate, stop and reassess: is this the right approach, is the story too big,
is this actually a blocker? Say so rather than grinding.

### Always leave it green

A clean stop with less delivered beats a broken tree with more. Before ending
any session: full gate, tracker updated, documents updated, committed, pushed,
promoted if green. Then state what was done, what was found, what is next, and
what is blocked.

---

## Quick reference

```
P0  Orient           git + code + CI + tracker; reconcile
P1  Business         DoR R1-R5; falsifiable criteria; scope
P2  Research         triangulate; resolve or escalate blockers
P3  Architecture     design record; structure over discipline
P4  Development      contracts -> probe -> code -> control
P5  QA-1 component   probe rule, control, properties
P6  QA-2 integration real neighbours, real seams
P7  QA-3 adversarial bandit, pip-audit, STRIDE
P8  QA-4 regression  full gate, mutation on 🔴, coverage
P9  Sign-off         commit, push, CI green, promote.py
P10 SIT              QA branch, degraded deps, session shape
P11 SIT logging      SIT Defects sheet + artifact republish
P12 PROD             STOP. Report. Ask.
```

**Reference:** `Documents/ENGINEERING_STANDARD.md` (invariants, verification
ladder, anti-pattern catalogue) · `Code/CLAUDE.md` (current state, things that
turned out false) · `Documents/MASTER_REFERENCE.md` (the system) ·
`Documents/PRE_LIVE_CHECKLIST.md` (the gate before real capital).
