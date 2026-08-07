# Sprint 1 Setup — what is done, and what you need to do

This covers the four prerequisites that sat *underneath* E01-S01 and were not
in the backlog as stories. They are now built and verified. What remains for
you is short: **one decision and one password.**

Everything below was run on this machine (Windows 11, Python 3.11.9, Docker
28.3.0) and the results are real, not expected.

---

## Part 1 — What is already done

Nothing in this part needs your attention. It is here so you do not redo it.

| # | Gap | Resolution |
|---|---|---|
| 1 | Alembic never initialised | `alembic.ini` + `migrations/env.py` + `migrations/script.py.mako` created and verified |
| 2 | No datastore config object | `DatabaseConfig` / `RedisConfig` added to `common/config.py`; `database:` / `redis:` sections added to `system.yaml` |
| 3 | `.env.example` could not start the stack | `POSTGRES_DB` / `USER` / `PASSWORD` added; the duplicated credential removed |
| 4 | No test fixtures | `tests/conftest.py` with container fixtures; 6 scaffold tests added |

Plus five things found while verifying the above:

- **`make up` could not have worked**, even with a correct `.env` — the compose
  interpolation env file was never passed. Fixed in the `Makefile`.
- **`GRAFANA_PASSWORD` was missing from `.env.example`** while compose requires
  it — the same bug as the Postgres credentials, one service over. Added.

- **A Windows-only blocker in the migration path.** Async psycopg refuses to
  run on Windows' default `ProactorEventLoop`, so `make migrate` failed
  outright with `InterfaceError`. Fixed in `common/db/eventloop.py`, called
  from `migrations/env.py`. This would have stopped E01-S01 on day one and is
  invisible on Linux/macOS, including in CI.
- **The stale Zerodha rate** in the `config.py` docstring (said 3/sec; the real
  figure is 10/sec, already correct in `broker/profiles.py`).
- **TA-Lib is not an install blocker.** It is a hard main dependency needing a
  C library, which looked like a problem for a sprint that never touches
  indicators. It resolved to a prebuilt Windows wheel (`ta-lib 0.7.1`). No
  action needed.

Current state: **118 tests passing** (112 existing + 6 new), `mypy` clean on
the new modules, `ruff` clean on the new files.

---

## Part 2 — What you need to do

### Step 1 · Create your `.env` (2 minutes)

`.env` does not exist yet and is gitignored. Copy the template and set one
value:

```bash
cd Code
cp .env.example .env
```

Generate a password and put it in `POSTGRES_PASSWORD`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

That is the only value needed for Sprint 1. The broker and Anthropic keys are
not required until E02 and E10 respectively — leave them blank for now.

> **How the credential works now.** The connection *structure* (host, port,
> database name, user, pool sizes) lives in `config/system.yaml`, which is
> version controlled. The *password* lives only in `.env`. Previously
> `.env.example` carried a whole `DATABASE_URL` with `changeme` embedded **and**
> compose required a separate `POSTGRES_PASSWORD` — two sources for one
> credential, guaranteed to disagree the moment either changed. There is now
> one source, plus an explicit override (`DATABASE_URL`) that `make doctor`
> reports whenever it is active, so it can never be silently in force.

### Step 2 · Decide the TimescaleDB pin (the one real decision)

This is the Day-1 gate. It must be settled **before the first migration is
written**, because it decides which compression DDL E01-S01 contains.

I tested both versions on your machine rather than citing docs:

| | `2.17.2-pg16` (current pin) | `2.29.1-pg17` (latest) |
|---|---|---|
| PostgreSQL | 16.6 | 17.10 |
| `add_compression_policy` (legacy) | ✅ | ✅ |
| `add_columnstore_policy` (hypercore) | ❌ | ✅ |
| `convert_to_columnstore` | ❌ | ✅ |
| Scaffold tests | 6 passed | 6 passed |

**Recommendation: upgrade to `2.29.1-pg17`.** Three reasons, in order of
weight:

1. **The upgrade is backward-compatible.** 2.29.1 has *both* APIs. The legacy
   `add_compression_policy` still works there, so upgrading cannot strand any
   DDL you write either way.
2. **You have no data and no migrations.** This is the cheapest this decision
   will ever be. After E01-S01 lands it means rewriting migrations; after
   ingestion starts it means a data migration.
3. **The legacy API is scheduled for removal in TimescaleDB 3.0.** Writing new
   DDL against a deprecated API in 2026 buys a rewrite later for no benefit
   now.

The counter-argument is real but weak: 2.17.2 is what the design documents were
written against, and staying put means the docs need no revision. That is not
worth a deprecated API and a Postgres major you would have to jump eventually.

**To apply it**, one line in `ops/docker-compose.yml`:

```yaml
# ops/docker-compose.yml, timescaledb service
image: timescale/timescaledb:2.29.1-pg17     # was 2.17.2-pg16
```

Nothing else changes. `tests/conftest.py` reads the pin *out of compose*, so
the tests follow automatically and can never silently test a different version
than production runs. Both images are already pulled locally.

If you would rather stay on 2.17.2, that is a legitimate choice — just say so,
and E01-S01 must use `add_compression_policy` throughout.

### Step 3 · Install `make` — or use the direct commands

**`make` is not installed on this machine**, and every document in this repo
tells you to run `make install`, `make test`, `make doctor`. On Windows it is
not present by default, so all of those instructions currently fail with
`make: command not found`.

`winget` is available, so:

```powershell
winget install ezwinports.make
```

Then restart your shell. If you would rather not install it, here is the full
mapping — every target, expanded:

| Target | Direct equivalent (run from `Code/`) |
|---|---|
| `make install` | `.venv/Scripts/python -m pip install -e ".[dev]"` |
| `make test` | `.venv/Scripts/python -m pytest` |
| `make test-fast` | `.venv/Scripts/python -m pytest -m "not integration"` |
| `make doctor` | `.venv/Scripts/python scripts/doctor.py` |
| `make lint` | `.venv/Scripts/python -m ruff check .` |
| `make types` | `.venv/Scripts/python -m mypy src` |
| `make migrate` | `.venv/Scripts/python -m alembic upgrade head` |
| `make migration M="msg"` | `.venv/Scripts/python -m alembic revision --autogenerate -m "msg"` |
| `make up` | `docker compose --env-file .env -f ops/docker-compose.yml up -d` |
| `make down` | `docker compose --env-file .env -f ops/docker-compose.yml down` |

> **`--env-file .env` is not optional.** Compose resolves `${VAR}`
> interpolation from an env file in the *project* directory — which is where
> the compose file lives (`ops/`) — not from the `env_file:` entries inside it,
> which only populate the containers' own environment. Without the flag,
> `make up` fails with *"required variable POSTGRES_PASSWORD is missing a
> value"* **even when `Code/.env` exists and is correct**, which is a
> thoroughly misleading error. The `Makefile` has been fixed to pass it; the
> table above reflects that.

Verified: with the flag, all 14 services resolve and `DATABASE_URL` correctly
points at `timescaledb:5432` rather than `localhost`.

### Step 4 · Keep Docker Desktop running while you work

The integration tests start real containers. When Docker is down they **skip
rather than fail**, and pytest says so in its header:

```
docker: NOT running — integration tests will be SKIPPED
```

That is deliberate — a closed Docker Desktop should not look like broken code.
But it does mean a green run with Docker down is not a full green run, because
the skipped tests are precisely the ones that prove datastore behaviour.

> Docker Desktop stops its daemon when you close the window. On Windows it also
> does not start with the machine unless you enable that in
> **Settings → General → Start Docker Desktop when you sign in**. Worth turning
> on for a project where every integration test needs it.

---

## Part 3 · Verify it works

```bash
cd Code
.venv/Scripts/activate          # Windows;  source .venv/bin/activate elsewhere

make doctor                     # config + datastore posture
make test                       # 118 tests
```

`make doctor` now has a **Datastores** section that answers "which database am
I actually about to talk to?" without you having to derive it:

```
Datastores
----------
  [ OK ] postgres: from system.yaml + POSTGRES_PASSWORD  postgresql+psycopg://algotrader@localhost:5432/algotrader
  [ OK ] postgres pool   size=10 overflow=5 timeout=30000ms
  [ OK ] redis: from system.yaml   redis://localhost:6379/0
  [ OK ] redis stream cap   default maxlen=10,000
  [WARN] no migrations written yet   expected during Sprint 1 until E01-S01 lands
```

Three states, all verified:

- no password and no override → **FAIL**, telling you exactly what to set
- password present → **OK**, showing the target with no credential in it
- `DATABASE_URL` set → **WARN**, saying system.yaml is being ignored

The migrations warning is expected and clears when E01-S01 lands.

---

## Part 4 · Decisions still open

Only the pin (Step 2) blocks anything. These four are recorded in
`EPIC01_TECHNICAL_SPEC.md §17` and can be settled inside E01-S01; my
recommendation is given for each, and I will proceed on it unless you say
otherwise.

| | Question | Recommendation |
|---|---|---|
| Q2 | Keep 1-minute bars forever, or roll off after a year? | **Keep.** ~19M rows either way; storage is not the constraint, backtest depth is |
| Q3 | Separate DB roles per service, or one app role? | **One app role now**, split at Phase 7 hardening |
| Q4 | Audit buffer on a container volume or a host mount? | **Host mount** — survives an image rebuild (this is Sprint 2, E01-S05) |
| — | pg16 or pg17, if upgrading | **pg17** — greenfield, longer support window |

Note also that **B1 and B4 do not block E01 at all** — they block E02. Send the
Zerodha email on Day 1 precisely so you are not waiting on it later.

---

## Part 5 · Things worth knowing before you write E01 code

These cost me time; they should not cost you any.

**testcontainers.** Two traps, both verified against 4.15:
- `testcontainers.postgres` and `testcontainers.redis` are **deprecated**
  import paths. Use `testcontainers.community.postgres` / `.redis`.
- `PostgresContainer` defaults to `driver="psycopg2"`, which this project does
  not install. Left at the default, every connection fails with
  `ModuleNotFoundError`. `tests/conftest.py` passes `driver="psycopg"`.

**Alembic autogenerate would try to drop your market data.** A hypertable's
physical chunks live in `_timescaledb_internal` and are not in
`Base.metadata`, so autogenerate sees them as unknown tables and emits
`DROP TABLE` for each. `migrations/env.py` filters them out via
`include_object`. Do not remove that filter.

**Constraint naming is set up front and must not change later.** The convention
in `common/db/base.py` is what makes `downgrade base` testable — without it,
PostgreSQL invents constraint names that differ between the database Alembic
generated *from* and the one it runs *against*. Changing the convention later
renames every constraint in the schema.

**Post-write hooks use `type = module`, not `type = exec`.** `exec` needs bare
`ruff` on `PATH`, which it is not unless the venv is activated, and
`make migration` does not require that. It fails with a bare
`[WinError 2] The system cannot find the file specified`, which does not
mention ruff or PATH at all.

**Versions installed are newer majors than the design docs assume** — alembic
1.19 (docs assume 1.13), pytest 9.1, pytest-asyncio 1.4 (docs assume 0.24),
SQLAlchemy 2.0.51, psycopg 3.3.4, pydantic 2.13.4. All 118 tests pass on them.
`asyncio_default_fixture_loop_scope` had to be added to `pyproject.toml` for
pytest-asyncio 1.x.

---

## Part 6 · Security pass — findings

A full security pass was run before this work was pushed. Method and results:

### Secrets — clean

- No `.env`, `.pem`, `.key` or credential file has **ever** been committed, in
  any commit on any branch.
- No private keys, AWS keys or JWTs anywhere in history.
- Ignore rules verified by *probe* rather than by reading `.gitignore` — a
  test `.env.probe` and `data/probe.db` were created and confirmed ignored,
  then deleted.
- The only credential-shaped string in the whole change is the testcontainer
  password, which is literally named `test_only_not_a_real_secret`.

`gitleaks` is not installed; the history scan above was done manually. Worth
installing if you want `make secrets-scan` to work.

### A real leak found and fixed — connection URIs were not redacted

The log redactor masked Anthropic keys, JWTs, bearer tokens and TOTP seeds —
but **nothing matched `scheme://user:password@host`.** SQLAlchemy, Alembic and
psycopg all put the connection URL into their errors, and `DatabaseConfig.dsn()`
has to return a plain string because SQLAlchemy requires one. A single
`log.error("connecting to %s", dsn)` would have written the database password
to disk in clear text.

Found by feeding a real DSN through the redactor, not by reading it — the same
method that caught the two earlier bugs recorded in `CLAUDE.md`.

Fixed in `common/logging.py`, with one subtlety worth knowing: the first
version of the pattern required a username, so `redis://:password@host` — the
form with no user, which is **exactly what `RedisConfig.dsn()` emits** — still
leaked. Both forms are now covered, along with percent-encoded passwords. Only
the password is replaced; scheme, user and host survive, because a redacted DSN
that still names the database is far more useful when debugging than a wall of
asterisks.

Locked in with 12 regression tests in `tests/security/test_log_redaction.py`,
including one that asserts against the DSNs this codebase actually generates
rather than handwritten strings.

### Dependency audit — one issue you cannot fix, now tracked as B7

`pip-audit` found 8 vulnerabilities in 2 packages. Seven were `setuptools`
(venv bootstrap, not a project dependency) — **fixed**, upgraded 65.5.0 → 83.0.0.

The remaining one matters and is now **blocker B7**:

> `kiteconnect 5.2.1` declares `autobahn[twisted]==19.11.2` — an **exact** pin
> to a 2019 release carrying **CVE-2020-35678** (redirect header injection,
> fixed in 20.12.3). autobahn is the WebSocket transport behind `KiteTicker`,
> i.e. the live market-data path.

It **cannot be fixed locally.** pip will install autobahn 26.7.1 if asked, but
that violates the SDK's declared pin and is a seven-year API jump in the exact
layer `KiteTicker` is built on. Doing that blind to the market-data path is a
worse risk than the CVE, so **it was not attempted.**

Practical exposure is small: the client connects to one known endpoint
(`wss://ws.kite.trade`) over TLS, and the compose design already puts the core
network on `internal: true` with egress filtered to broker hosts. Exploiting a
redirect-injection flaw needs an attacker able to influence a redirect
response. That is mitigation, not a fix.

**Actions:** add it to the Zerodha email alongside B1/B4; at E02-S03 verify TLS
certificate validation is on and the endpoint cannot redirect; re-run
`pip-audit` each sprint; re-evaluate before live trading. This belongs on
`PRE_LIVE_CHECKLIST.md`.

### Two long-standing blockers closed while verifying the SDK

`doctor` printed a malformed version string (`<module 'kiteconnect.__version__'
from ...>` — because `kiteconnect.__version__` is a *submodule*, not a string).
Fixing that meant looking at the installed SDK properly, which closed two items
that had been open since the design phase:

**B2 — `market_protection` — RESOLVED.** kiteconnect **5.2.1** exposes it on
`place_order()`. The gap was in **5.1.0**, which is the version the design
documents were written against. Its own docstring: *"`market_protection` accepts
`-1` for automatic market protection applied by the system as per market
protection guidelines, or a value greater than `0` up to `100` representing a
percentage."* That matches `OrderRequest`'s validator exactly — `-1` or a
positive percentage, `0` rejected — so the model and the broker already agree
with no change needed. `pyproject.toml` now floors the dependency at `>=5.2.1`
so a downgrade cannot silently reintroduce the gap.

**B1 — Algo-ID mechanic — RESOLVED (the value is still open).** `place_order()`
takes an `algo_id` parameter, documented as *"an optional algo ID to associate
with the order"*. So the Algo-ID is **client-supplied per order**, not injected
server-side by the broker — which is exactly what `BrokerConfig.algo_id` and the
live-mode validator already assumed. No design change needed.

What remains is a paperwork question, not an architectural one: *which* generic
exchange-issued ID to send for a sub-10-OPS self-developed algo. That is still
worth asking Zerodha on Day 1, but it no longer blocks E02-S04 from being
**built** — only from going live.

`doctor` now asserts both parameters against the installed signature every run.

### Verified as safe

- **The four safety invariants still hold** — 50 security tests pass.
- **The password does not leak through any config surface**: `model_dump()`,
  `repr()`, `str()` and `config_hash()` were each probed with a known canary
  and none contained it. `config_hash()` is password-independent by
  construction, since the password is not a field on the model.
- **Alembic does not echo the password**, even on connection failure — verified
  with a canary password through a real failing `alembic upgrade head`.
- **No SQL injection surface introduced.** The only raw SQL added is a static
  `CREATE EXTENSION` in the test fixture; `yaml.safe_load` is used for compose
  parsing.
- **`echo_sql` is refused outside development** by a validator, because echoed
  SQL contains bound parameters and bypasses the log redactor.

### Known and accepted

`DATABASE_URL` is passed to containers as an environment variable, so it is
visible to `docker inspect`. That is inherent to compose, and the design
already specifies Vault or SOPS for production secrets rather than `.env`
(`LOW_LEVEL_ARCHITECTURE.md §10.4`). Worth revisiting at Phase 7 hardening.

---

## Part 7 · Pre-existing issue, not introduced here

`ruff check` was **already failing before any of this work** — 10 errors on
files untouched by it (`tests/unit/`, `tests/security/`, `src/algotrader/broker/`,
`src/algotrader/strategy/`, `src/algotrader/common/models/`), and 30 across the
project. They are cosmetic: line length, ambiguous Unicode in docstrings,
unused `noqa` directives, one `assert_raises_exception`.

This matters because `make check` runs `lint types test` and therefore exits
non-zero today, which means it cannot be used as a merge gate until it is
cleaned. Most are auto-fixable:

```bash
ruff check --fix .        # clears 14 of 30
ruff format .
```

I left them alone deliberately — clearing project-wide lint debt is not part of
this task, and doing it silently would have buried it in an unrelated diff.

**Now tracked as `E01-S08` (0.5 d, Sprint 1)**, sequenced before E01-S01 merges
so the sprint has a working merge gate. The scaffolding work in Part 1 is
tracked as `E01-S07` (1 d, Closed) — it was never in the backlog even though
E01-S01 task 1 assumed all of it existed, so recording it stops that day of
work hiding inside the E01-S01 estimate.

---

*Written 7 Aug 2026 alongside the Sprint 1 scaffolding. Companion to
`../Documents/EPIC01_TECHNICAL_SPEC.md`, which specifies the stories this
ground supports.*
