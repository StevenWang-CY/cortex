# Cortex — developer shortcuts
# Run `make` with no arguments to see the target list.

VENV          := .venv
PY            := $(VENV)/bin/python
PIP           := $(VENV)/bin/pip
PYTEST        := $(VENV)/bin/pytest
RUFF          := $(VENV)/bin/ruff
MYPY          := $(VENV)/bin/mypy
EXT_DIR       := cortex/apps/browser_extension

.DEFAULT_GOAL := help

.PHONY: help setup dev test test-unit test-eval lint format typecheck \
        codegen codegen-check ci ext ext-dev ext-edge dmg clean wiki precommit

help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ─── Bootstrap ────────────────────────────────────────────────────────

setup: ## Create venv, install Python + pnpm deps, seed storage
	python3 -m venv $(VENV)
	$(PIP) install -U pip
	$(PIP) install -e "./cortex[dev]"
	cd $(EXT_DIR) && pnpm install
	$(PY) -m cortex.scripts.seed_config --root .
	@echo ""
	@echo "✓ Setup complete. Next:"
	@echo "    cp cortex/.env.example .env"
	@echo "    make precommit   # one-time pre-commit hook install"
	@echo "    make dev"

precommit: ## Install pre-commit hooks (schema-codegen drift gate)
	$(PIP) install pre-commit
	$(VENV)/bin/pre-commit install

# ─── Run ──────────────────────────────────────────────────────────────

dev: ## Start the daemon (FastAPI :9472, WebSocket :9473)
	$(PY) -m cortex.scripts.run_dev

# ─── Tests / quality ──────────────────────────────────────────────────

test: ## Full pytest suite — mirrors ci.yml exactly (unit+integration+services+state_engine+eval+physio+performance; desktop_shell isolated)
	QT_QPA_PLATFORM=offscreen $(PYTEST) cortex/tests/ --ignore=cortex/tests/unit/test_desktop_shell.py
	QT_QPA_PLATFORM=offscreen $(PYTEST) cortex/tests/unit/test_desktop_shell.py

test-unit: ## Unit tests only (desktop_shell pass runs last to avoid PySide6 sys.modules pollution)
	$(PYTEST) cortex/tests/unit/ --ignore=cortex/tests/unit/test_desktop_shell.py
	$(PYTEST) cortex/tests/unit/test_desktop_shell.py

test-eval: ## AMIP / IPS / safety-floor / calibration eval suite
	$(PYTEST) cortex/tests/eval/ cortex/tests/state_engine/test_calibration.py

lint: ## ruff
	$(RUFF) check cortex/

format: ## ruff --fix
	$(RUFF) check --fix cortex/

typecheck: ## mypy --strict (byte-identical to ci.yml/release.yml ci-gate)
	$(MYPY) --config-file cortex/pyproject.toml cortex/ --strict

codegen: ## Regenerate cortex_schemas.d.ts from Pydantic models
	$(PY) -m cortex.scripts.generate_ts_schemas

codegen-check: ## Drift gate — fails if cortex_schemas.d.ts is stale
	$(PY) -m cortex.scripts.generate_ts_schemas --check

ci: lint typecheck test codegen-check ## Run everything CI runs

# ─── Browser extension ────────────────────────────────────────────────

ext: ## Build Chrome MV3 production bundle
	cd $(EXT_DIR) && npx plasmo build

ext-dev: ## Plasmo hot-reload dev mode
	cd $(EXT_DIR) && pnpm dev

ext-edge: ## Build Edge MV3 production bundle
	cd $(EXT_DIR) && npx plasmo build --target=edge-mv3

# ─── Packaging ────────────────────────────────────────────────────────

dmg: ## Build Cortex.dmg (signed if CORTEX_SIGN_IDENTITY is set)
	./cortex/scripts/build_macos_app.sh

# ─── Hygiene ──────────────────────────────────────────────────────────

clean: ## Remove build artifacts (keeps .venv and node_modules)
	rm -rf build/ dist/ .pytest_cache/ .mypy_cache/ .ruff_cache/
	rm -rf cortex/*.egg-info cortex/__pycache__
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	find . -name '*.pyc' -delete

# ─── Wiki ─────────────────────────────────────────────────────────────

wiki: ## Push wiki .md files to the wiki remote (origin)
	git push origin main
