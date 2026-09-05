# Cortex — developer shortcuts
# Run `make` with no arguments to see the target list.

UV            := uv
UV_RUN        := $(UV) run --project cortex --locked --extra dev --extra codegen
PY            := $(UV_RUN) python
PYTEST        := $(UV_RUN) pytest
RUFF          := $(UV_RUN) ruff
MYPY          := $(UV_RUN) mypy
EXT_DIR       := cortex/apps/browser_extension

.DEFAULT_GOAL := help

.PHONY: help setup dev test test-unit test-eval lint format typecheck \
        codegen codegen-check config-sync contracts version-sync version-check audit ci ext ext-dev \
        ext-edge dmg clean wiki precommit

help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ─── Bootstrap ────────────────────────────────────────────────────────

setup: ## Create venv, install Python + pnpm deps, seed storage
	$(UV) sync --project cortex --locked --extra dev --extra codegen
	cd $(EXT_DIR) && pnpm install --frozen-lockfile
	cd cortex/apps/vscode_extension && npm ci
	$(PY) -m cortex.scripts.seed_config --root .
	@echo ""
	@echo "✓ Setup complete. Next:"
	@echo "    cp cortex/.env.example .env"
	@echo "    make precommit   # one-time pre-commit hook install"
	@echo "    make dev"

precommit: ## Install pre-commit hooks (schema-codegen drift gate)
	$(UV_RUN) pre-commit install

# ─── Run ──────────────────────────────────────────────────────────────

dev: ## Start the daemon (FastAPI :9472, WebSocket :9473)
	$(PY) -m cortex.scripts.run_dev

# ─── Tests / quality ──────────────────────────────────────────────────

test: ## Full pytest suite — mirrors ci.yml exactly (unit+integration+services+state_engine+eval+physio+performance; desktop_shell isolated)
	QT_QPA_PLATFORM=offscreen $(PYTEST) cortex/tests/ --ignore=cortex/tests/unit/test_desktop_shell.py

test-unit: ## Unit tests only (legacy desktop-shell stubs run in their isolation wrapper)
	QT_QPA_PLATFORM=offscreen $(PYTEST) cortex/tests/unit/ --ignore=cortex/tests/unit/test_desktop_shell.py

test-eval: ## Deterministic policy, MRT, OPE diagnostics, and inference eval suite
	$(PYTEST) cortex/tests/eval/ cortex/tests/state_engine/

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

config-sync: ## Regenerate the configuration reference and safe .env template
	$(PY) -m cortex.scripts.sync_config_docs --apply

contracts: ## Verify local docs links, config keys, live messages, and generated surfaces
	$(PY) -m cortex.scripts.verify_repository_contracts

version-sync: ## Synchronize generated versions from cortex/pyproject.toml
	$(PY) -m cortex.scripts.sync_versions --apply

version-check: ## Fail if any runtime/manifest version diverges
	$(PY) -m cortex.scripts.sync_versions --check

audit: ## Emit and enforce Python/browser/VS Code dependency audit reports
	mkdir -p audit-results
	$(UV_RUN) pip-audit --format json --output audit-results/python.json
	$(PY) cortex/scripts/verify_dependency_audit.py --ecosystem pip --report audit-results/python.json --summary-out audit-results/python-summary.json
	cd $(EXT_DIR) && (pnpm audit --json > ../../../audit-results/browser.json || true)
	$(PY) cortex/scripts/verify_dependency_audit.py --ecosystem pnpm --report audit-results/browser.json --exceptions cortex/security/node-audit-exceptions.json --summary-out audit-results/browser-summary.json
	cd cortex/apps/vscode_extension && (npm audit --json > ../../../audit-results/vscode.json || true)
	$(PY) cortex/scripts/verify_dependency_audit.py --ecosystem npm --report audit-results/vscode.json --summary-out audit-results/vscode-summary.json

ci: lint typecheck test codegen-check version-check contracts ## Run local Python/contract CI gates

# ─── Browser extension ────────────────────────────────────────────────

ext: ## Build Chrome MV3 production bundle
	cd $(EXT_DIR) && pnpm exec plasmo build

ext-dev: ## Plasmo hot-reload dev mode
	cd $(EXT_DIR) && pnpm dev

ext-edge: ## Build Edge MV3 production bundle
	cd $(EXT_DIR) && pnpm exec plasmo build --target=edge-mv3

# ─── Packaging ────────────────────────────────────────────────────────

dmg: ## Build an architecture-named local DMG (ad-hoc unless release credentials are set)
	./cortex/scripts/build_macos_app.sh

# ─── Hygiene ──────────────────────────────────────────────────────────

clean: ## Remove build artifacts (keeps .venv and node_modules)
	rm -rf build/ dist/ .pytest_cache/ .mypy_cache/ .ruff_cache/
	rm -rf cortex/*.egg-info cortex/__pycache__
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	find . -name '*.pyc' -delete

# ─── Wiki ─────────────────────────────────────────────────────────────

# GitHub renders a wiki from the wiki repository's ``master`` branch only.
# Publish the ``wiki/`` directory as an orphan history so the wiki never
# receives (or displays) the full source tree.
WIKI_REMOTE ?= https://github.com/StevenWang-CY/cortex.wiki.git

wiki: ## Publish wiki/ pages to the GitHub wiki (master branch of the wiki repository)
	@test -d wiki || (echo "wiki/ directory is missing" >&2; exit 1)
	git subtree split --prefix=wiki -b wiki-pages >/dev/null
	git push --force $(WIKI_REMOTE) wiki-pages:master
	git branch -D wiki-pages >/dev/null
