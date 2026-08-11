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

# 1a) REBUILD the bundle the browser is already loading, in one command:
#     startpage build + §10 gates + vitest, then the bundle copy on top of it.
#       make dev-bundle            # OUT defaults to dist/ — the dir loaded unpacked
#       make dev-bundle OUT=~/dist
#
#     Why in place and not into a fresh dir: the chrome-extension:// id is Chromium's
#     hash of the loaded dir's ABSOLUTE PATH. A new path is a new id, hence a new origin
#     and an empty chrome.storage.local — the profile's enrolment (serviceUrl + the
#     per-install secret) is gone and the instance has to enroll again. So `--force`
#     rewrites THIS path; the copy is staged and swapped in, so an interrupted run cannot
#     leave a half-written bundle (tools/instancegen/core.py: replace_bundle).
#
#     AFTER this target the browser still runs the OLD code: press «Обновить» / "Reload"
#     on the extension's card at brave://extensions (chrome://extensions). And if the
#     manifest's permission set changed, the browser holds the NEW permissions back until
#     they are confirmed there by hand — until then those capabilities silently do nothing.
#
#     To CONFIRM the reload took: this target prints the version it wrote
#     (`0.1.<commit-count>.<HHMM>`, the trailing component being the build minute, which is
#     what tells two builds of the same commit apart) and the extension card shows exactly
#     that string. Different version on the card => the reload did not happen. The manifest
#     carries NO `version_name`: the page renders that field beside the extension name and
#     a longer stamp there truncated the name itself, so the card falls back to `version`.
#     The short sha, the `-dirty` flag and the full build date are printed on the line
#     below it — the terminal is where the full build identity lives, not the card. The
#     tracked extension/manifest.json is never rewritten by this.
dev-bundle: OUT ?= dist
.PHONY: dev-bundle
dev-bundle: startpage install ## Rebuild the bundle the browser loads, in place (vars: OUT, default dist)
	$(PY) -m tools.generate_instance bundle --out "$(OUT)" --force

# 2) Wrap that shared bundle in a per-instance .app + empty profile. NO extension copy
#    and NO instance.json are written — the launcher's --load-extension points at the
#    shared BUNDLE_DIR, so every instance loads the same dir (one id/origin).
#      make instance INSTANCE_ID=main BUNDLE_DIR=~/dist OUT=~/instances [TITLE="Curator Main"]
#
#    By default the launcher ALSO loads the main Brave profile's store-installed
#    extensions (Bitwarden, DeepL, …) unpacked, keeping their real chrome-extension:// ids
#    and re-reading that profile at every launch, so they follow the main browser's
#    updates instead of freezing at generation time. It does NOT carry their STATE (Local
#    Extension Settings): they arrive logged-out and unconfigured. That is the DEFAULT,
#    not a permanent property — `make instance-state` below copies that state into ONE
#    chosen instance, once, with every Brave quit. What is not on offer is a live shared
#    session: a LevelDB has one writer, so the profiles diverge from the copy on.
#      SYNC_EXTENSIONS=<path>  another profile's Extensions dir
#      NO_SYNC_EXTENSIONS=1    curator bundle only
.PHONY: instance
instance: install ## Generate an instance .app (vars: INSTANCE_ID BUNDLE_DIR OUT [TITLE] [SYNC_EXTENSIONS|NO_SYNC_EXTENSIONS])
	$(PY) -m tools.generate_instance generate \
		--instance-id "$(INSTANCE_ID)" --bundle-dir "$(BUNDLE_DIR)" --out "$(OUT)" \
		$(if $(TITLE),--title "$(TITLE)",) \
		$(if $(SYNC_EXTENSIONS),--sync-extensions "$(SYNC_EXTENSIONS)",) \
		$(if $(NO_SYNC_EXTENSIONS),--no-sync-extensions,)

# 3) Copy the store extensions' STATE from the main profile into ONE instance, so they
#    arrive logged-in instead of logged-out. QUIT BRAVE FIRST — the main browser and every
#    instance .app: these are live LevelDB databases and the target refuses to run while
#    any Brave process is alive (there is no --force).
#      make instance-state INSTANCE_DIR=~/Applications/infra DRY_RUN=1   # look first
#      make instance-state INSTANCE_DIR=~/Applications/infra
#      make instance-state INSTANCE_DIR=~/Applications/infra ONLY=<bitwarden id>
#      FROM=<dir>   another profile's `Default` dir (default: the main Brave profile)
#      DRY_RUN=1    list what WOULD move (ids, sizes, exclusions) and write nothing
#
#    It is a ONE-TIME COPY and cannot be a live sync: a LevelDB has a single writer, so two
#    browsers cannot share one directory (a symlink just makes the instance see broken
#    storage). The two profiles hold independent copies afterwards — a vault change in one
#    does not reach the other; re-run to re-align (it overwrites, it does not merge). Note
#    that the copied Bitwarden storage carries its `appId`, so SERVER-side the two profiles
#    look like ONE device; what that does to a logout or a session revoke is not verified.
#    It copies the account and the ENCRYPTED VAULT, so the full login is gone; whether the
#    vault comes up UNLOCKED depends on the Bitwarden vault-timeout setting. Bitwarden is
#    not the only one — MetaMask is eligible too and its storage holds the encrypted seed
#    vault. Those vaults then exist in one more profile on this disk.
#
#    Only ids the source profile really installed unpacked (`Extensions/<id>/<version>/
#    manifest.json` — the launcher's own definition) are copied, and the extension THIS
#    instance loads unpacked is excluded on top of that by its derived id. Both layers
#    exist to keep the curator's storage — that install's install_uuid and enrollment
#    secret — out of the copy. No id is hard-coded.
#
#    INSTANCE_DIR is validated HERE: an unset variable would otherwise reach the CLI as
#    `--instance-dir ""`, and ONLY is passed whenever it is DEFINED (even empty) so the
#    CLI's own "empty --only is missing configuration, not «copy all 21»" guard is the
#    thing that decides — `$(if $(ONLY),…)` silently turned `ONLY=` into a full copy.
.PHONY: instance-state
instance-state: install ## Copy extension state into an instance, Brave quit (vars: INSTANCE_DIR [FROM] [ONLY] [DRY_RUN])
	@test -n "$(INSTANCE_DIR)" || { \
		echo "INSTANCE_DIR is required: make instance-state INSTANCE_DIR=~/Applications/<instance>"; \
		echo "  (an instance root generated by \`make instance\` — the dir holding profile/)"; \
		exit 1; \
	}
	$(PY) -m tools.generate_instance copy-state \
		--instance-dir "$(INSTANCE_DIR)" \
		$(if $(FROM),--from "$(FROM)",) \
		$(if $(filter-out undefined,$(origin ONLY)),--only "$(ONLY)",) \
		$(if $(DRY_RUN),--dry-run,)

# --- Housekeeping ------------------------------------------------------------
.PHONY: clean
clean: ## Remove the venv and Python caches
	rm -rf $(VENV) .pytest_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
