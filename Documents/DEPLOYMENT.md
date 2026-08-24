# DEPLOYMENT PIPELINE

How code moves from a working tree to real capital, and what stands in the way
at each step.

---

## 1. The one thing to understand first

**There are two different meanings of "deploy" in this project, and conflating
them causes bad decisions.**

| | What moves | Contains |
|---|---|---|
| **Branch promotion** | The whole source tree, by merge | Everything — code, tests, design documents, tracker |
| **Artifact deployment** | The container image | **Only `src/` and `config/`** |

The branch is the *record*; the image is the *thing that runs*.

**"Only ship what is necessary" is enforced on the artifact, never by promoting
a partial source tree.** If QA held a different set of files from DEV, QA would
be validating a tree that never existed on DEV — the merge would be untestable,
every subsequent promotion would conflict, and a bug found in QA could not be
reproduced on DEV. So the branch merge is complete and the *image* is minimal.

The CI `image` job asserts that minimality rather than trusting it: it fails the
build if `tests/`, `Documents/`, `migrations/`, `.git`, `.venv` or any `.env`
appears inside `/app`.

---

## 2. Branches

| Branch | Purpose | Gate to enter |
|---|---|---|
| `DEV` | Active development. Work lands here first. | CI green |
| `QA` | Validation and paper-trading verification. | CI green **on the exact commit** |
| `PROD` | Real capital. | `PRE_LIVE_CHECKLIST.md` fully signed off |
| `main` | Baseline. | — |

Promotion is **`DEV → QA → PROD` only**. `DEV → PROD` is rejected by the
promotion workflow, because it would skip validation entirely.

---

## 3. Continuous integration — `.github/workflows/ci.yml`

Runs on every push to a protected branch and every PR into one. Five jobs:

| Job | What it proves | Typical time |
|---|---|---|
| `quality` | `ruff check`, `ruff format --check`, `mypy src` | ~1 min |
| `test` | The full suite, including container-backed integration tests | ~5 min |
| `security` | Safety invariants, pen-test suite, `pip-audit`, gitleaks | ~3 min |
| `image` | The runtime image builds **and ships only what it should** | ~8 min |
| `gate` | A single check for branch protection to require | instant |

**The CI commands are deliberately identical to `make check` and
`make security`.** If CI and local ever disagree, one of them is lying, and it
is whichever you did not just run.

Two design decisions worth knowing:

- **No `services:` block for Postgres/Redis.** The tests use testcontainers,
  which reads its image pins out of `ops/docker-compose.yml`. Declaring service
  containers here as well would mean CI tests a *different* version from the one
  production runs — exactly the drift that reading the pin from compose was
  designed to prevent.
- **`pip-audit` and `bandit` are HARD gates** (since 24 Aug 2026). The audit
  was advisory while blocker B7 was open — `kiteconnect` pins
  `autobahn 19.11.2` (CVE-2020-35678) and a permanently red check trains
  everyone to ignore it, which is worse than no check.

  B7 is now closed, though not the way it was first attempted. A floor of
  `>=20.12.3` in `pyproject.toml` looked like the obvious fix and is
  **unsatisfiable**: `==19.11.2` is a hard pin, so pip fails outright with
  `ResolutionImpossible` rather than warning. That broke every CI job for two
  commits, and it broke them only in CI — locally the package had been
  force-installed over an already-resolved environment, so the environment
  being verified was one `pip install` could never produce.

  The pin is *declarative* rather than a runtime requirement, so the working
  fix is to let resolution succeed and then replace the package:

  ```
  pip install -c constraints.txt -e ".[dev]"
  pip install --no-deps --upgrade "autobahn==26.7.1"
  ```

  applied in every CI install job, `ops/Dockerfile` and `make install`.
  `tests/security/test_dependency_hygiene.py` is the enforcement: it fails on
  a vulnerable autobahn and asserts every install site carries the override. With the known finding gone,
  "advisory" only means the next CVE arrives silently, so the check blocks.

  Two details worth keeping:
  - Use `--skip-editable`, **not** `--strict`. `--strict` treats a *skipped*
    dependency as an error, and the local editable `algotrader` package is
    always skipped because it is not on PyPI — so `--strict` fails every run
    for a reason unrelated to vulnerabilities.
  - `bandit` is gated at **MEDIUM severity and MEDIUM confidence**. Every LOW
    in this codebase is an `assert` used for type narrowing behind an explicit
    guard, and nothing runs under `python -O`. Gating on LOW would be noise,
    and noisy gates get muted.

---

## 4. Promotion — `.github/workflows/promote.yml`

Manual, deliberate, and refuses to promote a commit whose CI is not green.
That refusal is the entire point; without it, promotion is just a merge button.

**To promote DEV → QA:**

1. GitHub → **Actions** → **Promote** → **Run workflow**
2. `from: DEV`, `to: QA`
3. Enter a reason — it goes into the merge commit and is the audit trail

The workflow then:

1. Rejects invalid paths (`DEV → PROD`, `QA → QA`)
2. Resolves the exact SHA at the tip of the source branch
3. **Queries the GitHub API for a successful `CI` run on that SHA** and fails if
   there is none, or if it concluded anything other than `success`
4. Merges with `--no-ff` so the promotion is a visible, revertible event
5. Pushes, and writes a job summary recording commit, reason and actor

**PROD promotion is blocked by a deliberate speed bump.** The workflow fails
with a message pointing at `PRE_LIVE_CHECKLIST.md`. That step is meant to be
removed only by someone who has actually read the file and can say the static
IP is whitelisted, the Algo-ID is confirmed, and paper trading has a record
across more than one market regime.

---

## 5. Branch protection — set this once, in the GitHub UI

CI cannot enforce itself. Under **Settings → Branches**, add a rule for `QA`
and `PROD`:

- ☑ Require status checks to pass — select **`CI gate`**
- ☑ Require branches to be up to date before merging
- ☑ Do not allow bypassing the above settings
- ☑ Restrict who can push (the promotion workflow uses `GITHUB_TOKEN`)

Without this, the promotion workflow is advisory: someone can still push
directly to `QA`. **This is the single highest-value five minutes of setup in
this document** — everything else is automation that a direct push walks around.

---

## 6. Running the QA environment

There is **no QA server yet.** Blocker **B6** — an India-hosted static IP — is
open, and SEBI requires order endpoints to originate from a whitelisted address.
So today "the QA environment" is the container stack, run wherever you are
running it.

```bash
cd Code
cp .env.example .env        # fill in POSTGRES_PASSWORD and GRAFANA_PASSWORD
make up                     # or: docker compose --env-file .env -f ops/docker-compose.yml up -d
make migrate                # apply the schema
make doctor                 # config, compliance and datastore posture
```

`make doctor` is the readiness check. It reports which datastore configuration
source is in effect, whether the broker SDK supports `market_protection` and
`algo_id`, and what remains before live trading.

### What QA is actually for

Not "does it run" — CI already answers that. QA is where the **strategy** is
validated rather than the software:

1. Paper trading across more than one market regime
2. The pre-market plan arriving before 09:15, repeatedly
3. Reconciliation drift measured against the broker, daily
4. The audit chain verifying clean every night

`MVP-complete does not mean ready for real capital.` The gap between "the
software works" and "the strategy works" is the largest risk in this project,
and no amount of engineering closes it — only evidence does.

---

## 7. Do not promote by checking out branches locally

This repository lives in a **OneDrive-synced folder**, and that makes local
branch switching genuinely dangerous.

Switching from `DEV` to `QA` asks git to delete every file `QA` does not have.
OneDrive holds a lock on any file it is syncing — `BACKLOG_Tracker.xlsx` in
particular — so the delete fails partway. Git reports
`unable to unlink ... Invalid argument` as a *warning*, completes the switch,
and leaves the tree half-emptied: source files gone, the locked file still
there, both showing as untracked.

Nothing is lost if the work is pushed, and recovery is `git checkout -f DEV`.
But it looks alarming, and on a branch whose work was *not* pushed it would be
real data loss.

**Use the promotion workflow.** It runs on a GitHub runner with no OneDrive and
no working tree to corrupt. If you must promote from a local clone, do it
without a checkout at all:

```bash
git fetch origin
SRC=$(git rev-parse origin/DEV)

# Safety check: has anyone committed work directly to QA?
# Promotion merge commits live only on QA by construction, so they are expected
# and must be ignored. What must NOT exist is a non-merge commit on QA that is
# absent from DEV — that is real divergence and would be overwritten below.
git log --no-merges --oneline origin/DEV..origin/QA | grep . \
  && echo "QA has commits of its own — STOP, investigate before promoting" \
  || echo "QA is clean (promotion merges only) — safe to promote"

NEW=$(git commit-tree "$SRC^{tree}" -p "$(git rev-parse origin/QA)" -p "$SRC" \
      -m "Promote DEV -> QA: <reason>")
git update-ref refs/heads/QA "$NEW"
git push origin QA
```

> **The obvious check is the wrong one.** An earlier version of this section used
> `git merge-base --is-ancestor QA DEV` to test for divergence. That reports
> "QA has diverged" on *every* promotion after the first, because each promotion
> creates a merge commit that exists only on QA and is therefore never an
> ancestor of DEV. A gate that cries wolf every time is worse than no gate — it
> gets ignored precisely when it finally means something. Test for QA-only
> *work* (`--no-merges`), not for ancestry.

That builds the same `--no-ff` merge commit the workflow does — QA's tree ends
up byte-identical to DEV's — while never touching a single file on disk.

---

## 8. Rollback

Promotion uses `--no-ff`, so every promotion is a single merge commit and
reverting one is a single operation:

```bash
git checkout QA
git revert -m 1 <merge-commit-sha>
git push origin QA
```

`-m 1` keeps QA's side of the merge. The reverted work stays intact on DEV.

Nothing in this repository auto-deploys on push, so a revert on the branch is
sufficient — there is no running deployment to roll back separately. When there
is, that changes, and this section must be rewritten rather than assumed.

---

## 9. The image, and a bug this work uncovered

**`ops/Dockerfile` did not build at all.** Setting up this pipeline was the
first time anyone had built the deployable artifact, and it failed: the stage
that compiled TA-Lib 0.4.0 from source dies partway through `make` because that
release predates modern GCC's stricter C rules. The image was not slow to
build — it was impossible to build, and nothing was checking.

Fixed by deleting the whole stage. TA-Lib has shipped manylinux wheels since
0.6 (verified against PyPI: `0.7.1` ships 12), and a manylinux wheel must vendor
its own shared objects to qualify, so `pip install` alone now yields a
self-contained install. That removed a compiler toolchain and a SourceForge
download from every build. **Build time went from "fails after ~1 minute" to
37 seconds.**

The runtime stage was also moved from Python 3.12 to **3.11, matching what CI
tests.** Shipping an image on a version the suite never ran against means the
artifact is not the thing that was verified. Bumping it requires bumping the CI
matrix first.

Verified on the built image:

| Property | Result |
|---|---|
| Size | 920 MB (dominated by pandas, numpy, pyarrow, TA-Lib) |
| `/app` contents | `src/`, `config/`, `data/` — nothing else |
| `tests/`, `Documents/`, `.git`, `.venv`, `.env` | all absent |
| Runs as | uid 1001, non-root |
| Smoke test | `algotrader` and `talib 0.7.1` both import |

### Migrations are deliberately not in the image

`migrations/` and `alembic.ini` are **not** copied into the runtime image, so a
container cannot run `alembic upgrade head` itself. That is the right default —
an application container that can rewrite its own schema is one bad restart away
from doing so unattended — but it means **schema changes are a separate,
deliberate step**:

```bash
# from a machine with the repo and DATABASE_URL pointing at the target
cd Code && make migrate
```

Run it *before* starting the services that depend on the new schema. When this
becomes a real deployment rather than a local stack, that step needs to become
a job in the pipeline with its own approval — not a thing someone remembers.

---

*Companion to `DEVELOPMENT_PROCEDURE.md`, which governs how an item reaches
`DEV` in the first place. Where the two disagree on process, the procedure wins.*
