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

# --- Prometheus alert rules (§12) --------------------------------------------
# `check rules` validates the SCHEMA (both Prometheus and vmalert unmarshal it
# strictly — one unknown field rejects the whole file and leaves the curator with
# NO alerts at all), `test rules` runs the fire/stay-quiet cases in
# deploy/alerts_test.yml. Both are needed: a rule can be schema-valid and still
# never fire.
#
# A MISSING promtool SKIPS with an explanation and exits 0, but ONLY outside CI.
# Rationale: promtool is not a Python dep and cannot be installed by `make
# install`, so hard-failing would make the target unusable on a laptop that has
# no Prometheus — and an unusable target gets replaced by the ad-hoc command this
# target exists to abolish. CI is the opposite case: there "skipped" must never
# pass for "checked", so when CI=true a missing promtool is a hard failure.
.PHONY: alerts
alerts: ## Validate deploy/alerts.yml (promtool check + test rules)
	@command -v promtool >/dev/null 2>&1 || { \
		echo "promtool not found — alert rules NOT validated."; \
		echo "  install: brew install prometheus   (or use the prometheus/promtool release tarball)"; \
		test "$(CI)" != "true" || { echo "CI=true: refusing to skip."; exit 1; }; \
		exit 0; \
	}; \
	promtool check rules deploy/alerts.yml && \
	promtool test rules deploy/alerts_test.yml

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
# 1) Build the ONE universal bundle the whole fleet loads. No signing key: the
#    chrome-extension:// id is the load-path hash and nothing reads it.
#      make bundle OUT=~/dist
.PHONY: bundle
bundle: install ## Build the shared universal bundle (vars: OUT)
	$(PY) -m tools.generate_instance bundle --out "$(OUT)"

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
