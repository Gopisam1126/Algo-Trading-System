.DEFAULT_GOAL := help
SHELL := /bin/bash
PY := python
VENV := .venv
# --env-file is REQUIRED and not decorative.  Compose resolves ${VAR}
# interpolation from an env file in the PROJECT directory, which is where the
# compose file lives (ops/) — not from the `env_file:` entries inside it, which
# only populate the containers' own environment.  Without this flag, `make up`
# fails with "required variable POSTGRES_PASSWORD is missing a value" even when
# Code/.env is present and correctly filled in.
COMPOSE := docker compose --env-file .env -f ops/docker-compose.yml

.PHONY: help
help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	 | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- Setup -----------------------------------------------------------------

.PHONY: venv
venv:  ## Create the virtualenv
	$(PY) -m venv $(VENV)
	@echo "Activate with: source $(VENV)/bin/activate   (Windows: $(VENV)\\Scripts\\activate)"

.PHONY: install
install:  ## Install runtime + dev dependencies
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dev]"

.PHONY: install-research
install-research:  ## Install backtesting / research extras
	$(PY) -m pip install -e ".[dev,research]"

# --- Quality ---------------------------------------------------------------

.PHONY: fmt
fmt:  ## Auto-format and fix lint issues
	ruff format src tests
	ruff check --fix src tests

.PHONY: lint
lint:  ## Lint without fixing
	ruff format --check src tests
	ruff check src tests

.PHONY: types
types:  ## Static type check
	mypy src

.PHONY: test
test:  ## Run the full test suite
	pytest

.PHONY: test-fast
test-fast:  ## Unit + property tests only (no containers)
	pytest tests/unit tests/property -q

.PHONY: test-safety
test-safety:  ## Verify the architecture's safety invariants still hold
	pytest tests/security -v

.PHONY: cov
cov:  ## Test with coverage; risk modules must be at 100%
	pytest --cov --cov-report=term-missing --cov-report=html

.PHONY: check
check: lint types test  ## Everything CI runs

# --- Security --------------------------------------------------------------

.PHONY: secrets-scan
secrets-scan:  ## Scan full git history for leaked secrets
	@command -v gitleaks >/dev/null 2>&1 \
	  || { echo "gitleaks not installed: https://github.com/gitleaks/gitleaks"; exit 1; }
	gitleaks detect --source . --verbose

.PHONY: audit
audit:  ## Audit dependencies for known vulnerabilities
	pip-audit

.PHONY: security
security: secrets-scan audit test-safety  ## Full security gate

# --- Config ----------------------------------------------------------------

.PHONY: validate-config
validate-config:  ## Validate config/system.yaml against the schema + hard bounds
	$(PY) -c "from algotrader.common.config import load_config; \
	c = load_config(); \
	print(f'Config OK  hash={c.config_hash()}  mode={c.system.mode.value}  \
	capital=Rs {c.risk.capital:,.0f}  slots={c.risk.position_slots}  \
	risk/trade=Rs {c.risk.risk_per_trade_rupees:,.0f}  \
	daily loss limit=Rs {c.risk.daily_loss_limit_rupees:,.0f}')"

.PHONY: validate-strategies
validate-strategies:  ## Compile every strategy YAML in config/strategies/
	$(PY) scripts/validate_strategies.py

.PHONY: doctor
doctor:  ## Pre-flight: environment, config, compliance readiness
	$(PY) scripts/doctor.py

# --- Database --------------------------------------------------------------

.PHONY: migrate
migrate:  ## Apply database migrations
	alembic upgrade head

.PHONY: migration
migration:  ## Create a migration: make migration M="add strategy tables"
	alembic revision --autogenerate -m "$(M)"

# --- Runtime ---------------------------------------------------------------

.PHONY: up
up:  ## Start all services
	$(COMPOSE) up -d

.PHONY: down
down:  ## Stop all services
	$(COMPOSE) down

.PHONY: logs
logs:  ## Tail logs (make logs S=signal-engine for one service)
	$(COMPOSE) logs -f $(S)

.PHONY: ps
ps:  ## Show service status
	$(COMPOSE) ps

.PHONY: build
build:  ## Rebuild images
	$(COMPOSE) build

.PHONY: clean
clean:  ## Remove caches and build artefacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage dist build
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
