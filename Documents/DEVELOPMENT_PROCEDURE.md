# DEVELOPMENT PROCEDURE

**The mandatory sequence for every backlog item in this project.**

This is not a style guide. It is the procedure that stands between a change and
a production incident in a system that places real orders with real money. Skipping
a phase is a decision to accept the risk that phase removes.

> **The rule this whole document exists to enforce:**
> **Static review does not catch behaviour. Only execution does.**
>
> Six real defects in this repository passed a clean code read and were caught only
> by running something: two Pydantic validators that were silently inert, a log
> redactor that leaked, an ORM enum with no constraint attached, a NOT NULL column
> with no sequence, and a migration that could not run on Windows at all. Every one
> of them *looked* correct.

---

## 0. Before you start — Definition of Ready

An item may not be started until **all** of these are true. If any is false, the
item is not ready and the honest move is to say so rather than start anyway.

| # | Gate | How to satisfy it |
|---|---|---|
| R1 | The item exists in `BACKLOG_Tracker.xlsx` with an ID, estimate, risk tier and priority | If you are doing work that has no story, **create the story first** — see §7.3 |
| R2 | Its `Dependencies` are `Closed` | Check the tracker, not your memory |
| R3 | It is not blocked by an open entry in the `Blockers` or `E01 Blockers` sheet | An absolute blocker means **stop and document**, never work around |
| R4 | The design spec covering it has been read *this session* | `EPIC01_TECHNICAL_SPEC.md`, `LOW_LEVEL_ARCHITECTURE.md`, etc. |
| R5 | You can state the acceptance criteria without looking them up | If you cannot, you do not yet understand the item |

---

## 1. Phase 1 — Analysis pass (**conflict and bug proofing**)

**Purpose: find the defects before writing the code that inherits them.**

Do this *before* the first line. Every hour here has repeatedly saved several later.

### 1.1 Read the specification section for this item, in full

Not skimmed. The traps are in the prose, not the code blocks.

### 1.2 Cross-check the spec against what already exists

This is where most defects hide — not inside the spec, but *between* the spec and
the code written before it.

- [ ] **Do the spec's names match the existing models?** Field-by-field. A spec
      saying `ts` and a model saying `open_ts` is a bug waiting for the repository layer.
- [ ] **Do the spec's types match?** Column widths against actual enum values.
      Precision against the domain model's `Decimal` places.
- [ ] **Is the spec's SQL/API actually valid** for the pinned version? Run it.
      The spec's `GENERATED ALWAYS AS (...) STORED AS col` was invalid PostgreSQL.
- [ ] **Does the spec assume infrastructure that exists?** `E01-S01 task 1` assumed a
      database config object that had never been written.
- [ ] **Does a library default differ from what the spec assumes?**
      `SQLAlchemy Enum(create_constraint=...)` flipped to `False` in 1.4 and silently
      removed every enum constraint.

### 1.3 Check the version reality

- [ ] What version of each relevant library is **actually installed**? (`pip show`, not `pyproject`)
- [ ] Has anything changed a **default** between the docs' version and yours?
- [ ] For anything version-dependent, can the code **adapt at runtime** rather than
      pin a decision you would have to revisit? (The TimescaleDB DDL detects its own
      version — that decoupled the whole sprint from an unmade pin decision.)

### 1.4 Record the findings

Every conflict found goes into the spec as a numbered correction (e.g.
`EPIC01_TECHNICAL_SPEC.md §2.3 A–G`), **before** coding. A finding that lives only in
your head is a finding the next person re-discovers the expensive way.

**Exit criterion:** you can list what the spec gets wrong, or state that you checked
and it is correct. "I didn't find anything" is only acceptable after the checks above.

---

## 2. Phase 2 — Design decisions

For each non-obvious choice, record **the decision, the alternative, and why**, in
the docstring of the thing itself — not in a separate document that will drift.

Decisions that must always be recorded:
- Anything that makes a failure **structurally impossible** vs merely discouraged
  (a required argument, a NOT NULL, a missing grant)
- Anything where you chose the **less obvious** option
- Anything that will look like a mistake to a reader who lacks your context

**The test of a good decision comment:** it explains why the *obvious* alternative is
wrong. "Uses X" is worthless; "Uses X because Y silently does Z" is the whole point.

---

## 3. Phase 3 — Build

### 3.1 Ordering within an item

1. **Types and interfaces first** — protocols, schemas, column types
2. **The safety-critical constraint next** — the `NOT NULL`, the required argument,
   the missing grant. Build the thing that makes the bad state impossible *before*
   the thing that uses it.
3. **The happy path**
4. **The failure paths** — timeout, absent, malformed, concurrent

### 3.2 Non-negotiables (from `CLAUDE.md`, restated because they are violated by drift)

- `Decimal` for money, never `float`
- Timezone-aware UTC everywhere; IST only at display and market-hours boundaries
- No `eval`/`exec`/dynamic import in the strategy path
- Secrets via `SecretString`; never logged, never in an exception message
- Fail **closed**: timeout → skip the trade; stale data → block entries
- Every position has a stop and a time exit

### 3.3 Make the dangerous thing impossible, not documented

| Weak | Strong |
|---|---|
| "Remember to pass a TTL" | TTL is a required positional argument |
| "Don't delete audit rows" | The role has no `DELETE` grant |
| "The AI shouldn't set quantity" | The type has no `quantity` field |
| "Trim your streams" | `maxlen` is a required argument on `publish()` |

If you find yourself writing a comment telling a future caller to be careful,
**stop and change the signature instead.**

---

## 4. Phase 4 — Testing

### 4.1 The probe rule

> **For every claim the code makes, write the test that fails if the claim is false.**

A test that asserts a constraint was *declared* is worthless. A test that constructs
the violation and requires the database to refuse it is the real thing.

| Claim | Probe |
|---|---|
| "this validator rejects X" | Construct X; assert it raises |
| "secrets are redacted" | Pass a real-shaped secret; grep the output |
| "audit rows cannot be deleted" | `SET ROLE`; run the `DELETE`; require the error |
| "the lock needs a TTL" | Call without it; require `TypeError` |
| "config rejects bad values" | Set the bad value; assert it fails to load |

### 4.2 Always include the control

A rejection test can pass for the wrong reason — the whole feature being absent.
**Pair every "this is refused" test with a "this is accepted" test.**

This is not theoretical: the append-only tests would pass if the role did not exist
at all. The control (`INSERT` still works, `DELETE FROM orders` still works) is what
proves the restriction is *targeted*.

### 4.3 Test tiers by risk

| Risk | Required |
|---|---|
| 🟢 Low | Unit tests on the happy path and the obvious failure |
| 🟠 Correctness-critical | The above, plus each documented failure mode, plus an integration test against the real dependency |
| 🔴 **Safety-critical** | All the above, **plus** a property-based test (Hypothesis) over generated inputs, **plus** an explicit tamper/abuse test, **plus** an outage/concurrency test, **plus a second read on a different day** |

### 4.4 🔴 stories: additional rules

- **Never merge a 🔴 story at the end of a working session.** Fatigue and
  safety-critical code are a bad combination. If you are near the end, stop and
  leave it uncommitted or on a branch.
- The property test must generate *sequences*, not single values — chains, orderings
  and interleavings are where these defects live.

---

## 5. Phase 5 — Security review

Run **all** of these for every item. They are cheap; the failures are not.

### 5.1 Automated

```bash
python -m pytest tests/security/ -q      # safety invariants + redaction
python -m pip_audit                       # dependency CVEs
ruff check .                              # includes bandit (S) rules
gitleaks detect --source . --verbose      # history secret scan
```

### 5.2 Manual — ask each question of the diff

- [ ] **Does anything new carry a credential?** DSNs, URLs, tokens. If yes: is it
      redacted in logs? *Probe it — do not assume.* The connection-URI leak was found
      exactly this way and the first fix still missed the `redis://:pw@host` form.
- [ ] **Any new string interpolation into SQL / shell / a prompt?** If yes, is the
      interpolated value reachable from user input, market data, or news content?
      If it cannot be parameterised (DDL identifiers cannot), **say so in a comment**
      so the next reviewer does not have to re-derive it.
- [ ] **Any new external input?** News, broker responses, config. Is it parsed into a
      validated model before anything reads it?
- [ ] **Does this widen a permission?** New grants, new network egress, new file writes.
- [ ] **Does this weaken an existing invariant?** Check `tests/security/` still passes
      — and check whether a *new* invariant should be added there.
- [ ] **What happens if this component is down / slow / returns garbage?**
      Fail closed, or is there a path where it fails open?

### 5.3 The pre-commit secret sweep — mandatory, every time

```bash
git add -A
git diff --cached | grep -nEi 'password[[:space:]]*=|BEGIN (RSA|OPENSSH|EC) PRIVATE|AKIA[0-9A-Z]{16}|sk-ant-'
git status --porcelain | grep -Ei '\.env$|\.venv|/data/|\.pem$|\.key$'
```

Verify ignore rules by **probe**, not by reading `.gitignore`: create a throwaway
`.env.probe`, confirm `git check-ignore` catches it, delete it.

---

## 6. Phase 6 — Quality gate

```bash
make check          # ruff + mypy + full test suite  → MUST exit 0
make test-safety    # the four invariants
make doctor         # config and compliance posture
```

**A red gate blocks the commit.** If the gate was already red before your change,
that is a separate defect — raise it as its own story (this is exactly what E01-S08
was) rather than adding to it or silently absorbing it into an unrelated diff.

Distinguish clearly, in writing, between:
- errors **you introduced** → fix now, in this item
- errors that **pre-existed** → separate story, measured and reported honestly

---

## 7. Phase 7 — Update the tracker

**The tracker is the project's memory. An undocumented change did not happen.**

### 7.1 On the `Backlog` sheet, for the item just finished

| Column | Set to |
|---|---|
| `Status` | `Closed` (only when the full DoD is met; `Development Completed` if awaiting QA) |
| `% Complete` | `100` |
| `Notes` | **What was actually delivered**, the decisions taken and why, and **every bug found by executing** — with the mechanism, not just the symptom |
| `Comments` | Open questions, follow-ups, things the next person needs |

The `Notes` field is the highest-value artefact you produce. Write it for someone
who has your problem in six months and none of your context.

### 7.2 Elsewhere in the tracker

- **`Sprint Plan`** — re-state remaining days if the scope moved. Do not let a stale
  plan claim a sprint fits when it does not.
- **`Blockers` / `E01 Blockers`** — any new blocker, with an exposure assessment
  (see §8)
- **`Dashboard`** — formula-driven; it updates itself. Never hand-edit it.

### 7.3 Work that has no story

If you did necessary work that was not in the backlog, **add the story
retrospectively and mark it `Closed`.** Do not let it hide inside another item's
estimate — that is how a 2-day story silently becomes 3 and nobody learns anything.
`E01-S07` exists for exactly this reason.

### 7.4 Range integrity when adding rows

Inserting rows does **not** move formulas, conditional formatting, data validations
or the autofilter. After any insert, widen all of them and verify no stale
references remain. Take a backup outside the repo first.

---

## 8. Blocker protocol

A **blocker** is something you cannot resolve yourself and that prevents correct
completion. It is not "this is hard" or "I would need to think".

### 8.1 When you hit one

1. **Stop working on that item.** Do not build a workaround that will be forgotten.
2. **Finish everything else that does not depend on it** — a blocker on one item
   almost never blocks the whole epic.
3. **Record it** in the appropriate blockers sheet with:
   - What is blocked, and what specifically cannot proceed
   - **Exposure assessment** — what is the actual risk if this stays open?
   - Whether it blocks **development** or only **going live** (a critical distinction:
     most external blockers only gate live trading)
   - The concrete action required, and by whom
4. **Set the item's status** to `Blocked`, and name the blocker in `Blocked By`.

### 8.2 Blocker vs. decision

| It is a blocker if | It is a decision if |
|---|---|
| It needs information only a third party has | You could pick a sensible default and note the assumption |
| It needs a credential or account you do not have | It changes behaviour but either choice is defensible |
| Proceeding either way would be **unsafe or wasted work** | Proceeding under a stated assumption is recoverable |

**Prefer making the decision.** Record the assumption, proceed, and flag it. Blocking
with nothing delivered is the expensive option — reserve it for cases where being
wrong is unsafe or makes the work useless.

**Better still: design the blocker away.** The TimescaleDB pin was a blocker until the
migration was made version-adaptive; then it was neither a blocker nor a decision.

### 8.3 What "Moved to Backlog" means, and how to resolve it

`Moved to Backlog` records that an item was **deferred out of the current plan** —
and, critically, **why**. The status alone is not a decision; the `Notes` field holds
the reason, and the reason is what you re-evaluate.

**When picking such an item back up, check whether the original reason still holds:**

| Original reason | Still deferred if | Pull it back in when |
|---|---|---|
| Sprint capacity | The sprint is still full | Capacity exists, or the sprint boundary moved |
| Dependency not ready | The dependency is still open | The dependency closed |
| Priority (P2/P3) | Higher-priority work remains | It is genuinely next |
| **Blocked** | The blocker is open | The blocker closed — and then it should be `Active`, not `Moved to Backlog` |

If the reason no longer holds, **change the status and say so in `Notes`**, citing the
original reason and why it lapsed. A deferred item that is silently resurrected loses
the record of why it was ever deferred.

---

## 9. Phase 8 — Commit and push

### 9.1 One item per commit where possible

A commit that closes one story is reviewable. A commit that closes four is archaeology.

### 9.2 The commit message must record

- **What** changed, in one line
- **Why** the non-obvious decisions were made
- **Every bug found by executing**, with the mechanism — these are the highest-value
  lines in the whole history
- Honest state: what is *not* done, what remains

### 9.3 Never

- Commit a `.env`, a credential, or anything under `data/`
- Skip hooks (`--no-verify`) or bypass signing
- Push to `main`, `QA` or `PROD` directly — work lands on `DEV` and promotes by PR
- Force-push a shared branch

---

## 10. The 90% usage safe-stop protocol

Long sessions end unpredictably — a usage limit, a context limit, an interruption.
**The goal is that the repository is never left in a state that costs someone time
to recover from.**

### 10.1 What a "safe point" is

A safe point is **not** "I finished typing". All of these must be true:

- [ ] The full quality gate passes (`ruff`, `mypy`, all tests)
- [ ] No half-built module is importable but broken
- [ ] The tracker reflects reality — including what is **not** done
- [ ] Work is committed **and pushed**
- [ ] The next person can tell, from the tracker alone, exactly where to resume

**A clean stop after two stories beats a messy stop after four.**

### 10.2 Checkpoint boundaries

Treat these as the only acceptable stopping points:

1. **After Phase 7** of any item (built, tested, security-reviewed, tracker updated)
2. **After the analysis pass** of an item, if the findings are recorded in the spec
3. **Never** mid-build with a broken import, a failing test, or a half-written migration

### 10.3 Approaching the limit

Usage percentage is not directly observable from inside a session. Therefore:

- **Work in whole items, smallest-first among those ready.** Finishing three small
  stories cleanly is worth more than three-quarters of a large one.
- **Do not start an item you cannot plausibly finish** and reach a checkpoint.
- **When told the limit is near, stop at the next checkpoint boundary — not later.**
  Do not attempt "just one more thing".

### 10.4 The wind-down sequence — do this in order

1. **Stop building.** Do not start anything new.
2. If mid-item: either finish to a checkpoint, or **revert the partial work** and
   record what was learned in the tracker. Do not commit a half-built module.
3. Run the full quality gate. Fix or revert anything red.
4. Run the security sweep (§5.3).
5. Update the tracker — **especially the honest status of what is incomplete**.
6. Clean up local resources: stop dev containers, remove throwaway files, confirm no
   stray `.env`.
7. Commit and push.
8. **Report**: what is done, what remains, what the next step is, and any blocker.

### 10.5 The handoff statement

End every session with, explicitly:

> **Done:** … **Not done:** … **Next step:** … **Blocked on:** …

Vague completion claims are worse than none. If tests fail, say so with the output.
If something was skipped, say that.

---

## 11. Per-item checklist

Copy this per backlog item.

```
ITEM: ____________          RISK: 🔴 / 🟠 / 🟢          EST: ___ d

READY
  [ ] R1 story exists in tracker      [ ] R4 spec read this session
  [ ] R2 dependencies Closed          [ ] R5 acceptance criteria known
  [ ] R3 not blocked

ANALYSIS
  [ ] Spec section read in full
  [ ] Names / types / SQL cross-checked against existing code
  [ ] Installed library versions and changed defaults checked
  [ ] Conflicts recorded in the spec as numbered corrections

BUILD
  [ ] Safety constraint built before the code that uses it
  [ ] Dangerous states made impossible, not documented
  [ ] Decisions recorded in docstrings with the rejected alternative

TEST
  [ ] A probe for every claim
  [ ] A control for every rejection test
  [ ] Risk-tier requirements met (🔴 → property + tamper + outage + second read)
  [ ] Tests actually run and green

SECURITY
  [ ] tests/security/ green      [ ] pip-audit reviewed
  [ ] Credential handling probed [ ] Injection surface reviewed
  [ ] Pre-commit secret sweep    [ ] Fail-closed verified

GATE
  [ ] ruff  [ ] mypy  [ ] full suite  [ ] doctor

TRACKER
  [ ] Status + % Complete        [ ] Notes: delivered, decisions, bugs-by-execution
  [ ] Blockers sheet updated     [ ] Retrospective stories added if needed
  [ ] Sprint Plan remaining days re-stated

CLOSE
  [ ] Dev resources cleaned up   [ ] Committed  [ ] Pushed
  [ ] Handoff statement written
```

---

## 12. Lessons this procedure encodes

Each of these cost real time in this repository. The phase that would have caught it
is named.

| Defect | Why review missed it | Phase that catches it |
|---|---|---|
| Pydantic **field** validator could not see later fields — OHLC check was inert | Reads correctly | 4.1 probe |
| Log redactor's JWT pattern required a longer header than real tokens have | Reads correctly | 4.1 probe |
| Log redactor had **no pattern at all** for connection URIs | Absence is invisible in review | 5.2 credential probe |
| First fix for the above missed `redis://:pw@host` (no username) | Tested the handwritten case, not the generated one | 4.1 probe *against real producers* |
| `SQLAlchemy Enum` had `create_constraint=False` by default → no validation | Library default changed | 1.3 version reality |
| `decision_log.id` NOT NULL with no sequence → every insert failed | Only a PK gets an implicit sequence | 4.1 probe |
| Async psycopg cannot use Windows' default event loop | Invisible on Linux and in CI | 4.1 run it on the target platform |
| `make up` never passed compose an `--env-file` | Error message blames the wrong thing | 4.1 probe |
| Spec's `order_fills` DDL was invalid PostgreSQL | Looked like SQL | 1.2 run the spec's SQL |
| `make check` was already red, so it could not gate anything | Nobody ran it | 6 quality gate |
| Adjustment factors were only *approximately* order-independent — `Decimal` quotient multiplication rounds at 28 digits | Every scalar test passed; the error is in the last digits | 4.3 **property** test over generated orderings |
| `restore_bars` reinserted archived bars carrying factors frozen before later corporate actions | Archive/restore is a second write path that does not look like one | 1.1 trace every path rows LEAVE **and re-enter** by |
| Two concurrent `recompute_factors` calls could lose a newly-announced action | Both writers are self-consistent, so row locks see nothing wrong | 1.1 serialise on the **read** when the write depends on it |
| A 100k-row insert benchmark drifted 7.9 s → 10.9 s purely from dead tuples left by earlier tests | Passed alone, failed in suite — read as flake | 4.1 make the measurement independent of suite order |
| `gsm_stage` / `asm_category` existed in the schema but were in neither the upsert's conflict set nor the read projection | A half-wired column looks identical to a wired one | 4.2 read the column back through the repository |

---

*Applies to every item in `BACKLOG_Tracker.xlsx`. Where this document and a design
document disagree on process, this one wins; where they disagree on system
behaviour, the design document wins and this file should be corrected.*
