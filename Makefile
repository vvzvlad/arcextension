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
# Generate a themed Brave instance (own --user-data-dir, extension copy,
# instance.json, .app). The token is a SECRET — pass it via the EXT_TOKEN env,
# never on the command line. Note the assignment goes BEFORE `make`: written after
# it, `EXT_TOKEN=…` is a make argument and lands in argv (visible in `ps`, recorded
# in shell history) instead of the environment. See tools/README.md for the manual
# acceptance list.
#   EXT_TOKEN=… make instance INSTANCE_ID=main SERVICE_URL=wss://host \
#                 OUT=~/instances TITLE="Curator Main"
.PHONY: instance
instance: install ## Generate an instance (vars: INSTANCE_ID SERVICE_URL OUT [TITLE]; env EXT_TOKEN)
	$(PY) -m tools.generate_instance generate \
		--instance-id "$(INSTANCE_ID)" --service-url "$(SERVICE_URL)" --out "$(OUT)" \
		$(if $(TITLE),--title "$(TITLE)",)

# Re-stamp EVERY instance under OUT (the §13 rotation path): rotate EXT_TOKEN AND
# refresh each copy's extension code from this repo's extension/. Both halves matter
# — the bundle is duplicated per instance and protocolVersion is compared by exact
# equality (§6), so rotating without carrying the code would reject every instance on
# hello after a PROTOCOL_VERSION bump. Restart the browsers afterwards so the SW
# re-reads instance.json. EXT_TOKEN goes BEFORE `make` (see the note above).
#   EXT_TOKEN=… make restamp OUT=~/instances
.PHONY: restamp
restamp: install ## Rotate EXT_TOKEN + refresh code across ALL instances under OUT (env EXT_TOKEN)
	$(PY) -m tools.generate_instance restamp --out "$(OUT)"

# --- Housekeeping ------------------------------------------------------------
.PHONY: clean
clean: ## Remove the venv and Python caches
	rm -rf $(VENV) .pytest_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
