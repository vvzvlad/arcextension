# Makefile — single entry point for every repeated action in this project.
# Run `make` (or `make help`) to list the available targets.
#
# All routine commands (environment setup, tests, run, docker build/push) live
# here so they stay documented, consistent and hard to get wrong. Prefer adding
# a target over writing a one-off command in the shell or in CI.

# --- Configuration -----------------------------------------------------------
VENV   ?= .venv
PY     := $(VENV)/bin/python
PIP    := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest

.DEFAULT_GOAL := help

# --- Help --------------------------------------------------------------------
.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

# --- Environment -------------------------------------------------------------
# The project ALWAYS runs inside a local .venv. Every Python target depends on
# the virtualenv, so it is created automatically on first use and reused after —
# the system Python is never used directly.
.PHONY: venv
venv: $(VENV)/bin/python ## Create the local virtualenv (.venv) if missing

$(VENV)/bin/python:
	python3 -m venv $(VENV)

# Sentinel: dependencies are (re)installed only when a requirements file changes,
# not on every `make test` / `make run`.
$(VENV)/.deps-installed: requirements-dev.txt requirements.txt | $(VENV)/bin/python
	$(PIP) install -r requirements-dev.txt
	touch $@

.PHONY: install
install: $(VENV)/.deps-installed ## Create .venv (if missing) and install dev/test deps

.PHONY: env
env: ## Create .env from the template if it does not exist
	@test -f .env || cp .env.example .env

# --- Develop -----------------------------------------------------------------
.PHONY: test
test: install ## Run the test suite (auto-creates .venv if missing)
	$(PYTEST)

.PHONY: run
run: install ## Run the application (auto-creates .venv if missing)
	$(PY) main.py

# --- Startpage (Vue 3 + Vite, §10) -------------------------------------------
# Independent Node toolchain under startpage/, built INTO extension/startpage. The
# build MUST precede the gates + vitest (the render smoke test loads the built
# bundle). Kept separate from the Python `test` target.
.PHONY: startpage
startpage: ## Build the startpage, run the §10 build gates, then the vitest suite
	cd startpage && (npm ci || npm install)
	cd startpage && npm run build
	cd startpage && npm run gates
	cd startpage && npm test

# --- Instance generator (§13) ------------------------------------------------
# Under enrollment (§7/§13) the build is a TWO-step flow and neither step carries a
# token or a service address — both are entered per profile in the extension's
# enrollment settings UI. See tools/README.md for the manual acceptance list.
#
# 1) Build the ONE universal, key-pinned bundle the whole fleet loads. Pass a stable
#    --key-file for a reproducible chrome-extension:// id across rebuilds.
#      make bundle OUT=~/dist [KEY_FILE=~/signing_key.pem]
.PHONY: bundle
bundle: install ## Build the shared universal bundle (vars: OUT [KEY_FILE])
	$(PY) -m tools.generate_instance bundle \
		--out "$(OUT)" \
		$(if $(KEY_FILE),--key-file "$(KEY_FILE)",)

# 2) Wrap that shared bundle in a per-instance .app + empty profile. NO extension copy
#    and NO instance.json are written — the launcher's --load-extension points at the
#    shared BUNDLE_DIR, so every instance loads the same dir (one id/origin).
#      make instance INSTANCE_ID=main BUNDLE_DIR=~/dist OUT=~/instances [TITLE="Curator Main"]
.PHONY: instance
instance: install ## Generate an instance .app (vars: INSTANCE_ID BUNDLE_DIR OUT [TITLE])
	$(PY) -m tools.generate_instance generate \
		--instance-id "$(INSTANCE_ID)" --bundle-dir "$(BUNDLE_DIR)" --out "$(OUT)" \
		$(if $(TITLE),--title "$(TITLE)",)

# --- Housekeeping ------------------------------------------------------------
.PHONY: clean
clean: ## Remove the venv and Python caches
	rm -rf $(VENV) .pytest_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
