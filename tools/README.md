# Instance generator (§13)

`tools/instancegen` builds themed Brave instances under **enrollment** (§7/§13, issue
#35). It is a two-step, **token-free** flow:

1. **`bundle`** — build the ONE universal extension bundle the whole fleet loads. It is
   a copy of `extension/` and nothing else: no token, no service URL, no `instanceId`
   and no signing key are baked in.
2. **`generate`** — wrap that shared bundle in a per-instance `.app` (its own
   `--user-data-dir`, an icon, and a launcher whose `--load-extension` points at the
   **shared** bundle). It writes **no** extension copy and **no** `instance.json`.

The service address and the per-install secret are **not** in the build at all — each
profile enters them through the extension's **enrollment settings UI** and the operator
approves the request on `/admin` (see `deploy/DEPLOY.md`). It is a dev/ops tool — **not**
part of the runtime service in `src/`.

Design references: `docs/architecture.md` **§13 (Инстансы)**, **§6** (config source),
**§12** (token model). Read those before deploying.

## Why a generator (and not "copy the .app")

A fresh `--user-data-dir` starts with an **empty** `chrome.storage.local`, so each
profile mints its **own** `install_uuid` on first run (in the profile, not the bundle).
That per-install identity is what enrollment binds a secret to, and it is why **cloning
a `.app` does NOT add an instance**: a clone shares the bundle but its fresh profile is a
different install — it must enroll separately. To add an instance, run `generate` again
with a new `--instance-id` (which names the new `.app`/profile) and enroll it.

## Usage

Neither step takes a token — there is deliberately **no `--token`/`--token-file` option**
anywhere, because there is no shared secret to pass. `make bundle` / `make instance` wrap
the CLI (`tools/generate_instance.py`).

```bash
# 1. Build the shared universal bundle ONCE.
make bundle OUT=dist
#   …or the CLI directly:
.venv/bin/python -m tools.generate_instance bundle --out dist

# 2. Wrap it in per-instance .apps. The launcher loads the SHARED bundle — no copy.
make instance INSTANCE_ID=main BUNDLE_DIR=dist \
    OUT=~/Applications TITLE="Curator Main"
#   …or the CLI directly (more options: --icon, --brave-binary, --overwrite):
.venv/bin/python -m tools.generate_instance generate \
    --instance-id main --bundle-dir dist \
    --out ~/Applications --title "Curator Main"
```

Neither step prints an extension id, because nothing consumes one. All instances load
the same shared bundle and therefore share one `chrome-extension://<id>` — Chromium's
hash of that dir's absolute path — but no origin is checked anywhere: `/ext` does not vet
`hello.origin` and `/api/*` CORS accepts any origin (`src/api/cors.py`). Moving or
renaming the bundle changes the id and breaks nothing.

`--service-url` on `generate` is accepted for backward-compatibility but is
**informational only**: the address is entered per profile during enrollment, never
stamped into the build.

## Adding / enrolling an instance

1. Run `generate` for the new `--instance-id` and launch its `.app`.
2. Open the extension's settings, enter the service address, and take the enrollment
   **code** the server shows while a window is open.
3. Approve the request on `/admin` within the enrollment window (`deploy/DEPLOY.md`).

There is no "re-stamp" step anymore: the token that `restamp` used to rotate no longer
exists, and code updates ship by rebuilding the **one** shared bundle (all instances load
it, so there is nothing per-instance to refresh).

## Icon and `.app` on non-macOS (CI)

`generate` always emits the full `.app` file tree (launcher, `Info.plist`) and a real
per-instance PNG **icon source** (a solid colour derived from the title, generated with
stdlib `zlib` — no PIL/mac tools). The **only** mac-specific step is turning that PNG into
a proper `AppIcon.icns` via `sips` + `iconutil`; off macOS `build_icns` is a reported
no-op and the `.app` still runs with the generic app icon. Regenerate the `.icns` on a mac
for the themed Dock icon.

## `--load-extension` caveats (Brave, not Chrome)

The launcher execs the **system Brave**. `--load-extension` is gated behind
`BUILDFLAG(GOOGLE_CHROME_BRANDING)`: **Google Chrome refuses it** ("--load-extension is
not allowed in Google Chrome"). It also does **not** work with **Enhanced Safe Browsing**
enabled or under the `ExtensionInstallTypeBlocklist` policy (§13).

## Manual acceptance (needs a real browser + service — NOT covered by pytest)

The pure core is unit-tested (`tests/test_instancegen.py`,
`tests/test_enroll_metrics_and_bundle.py`). The operational acceptance below requires a
live browser and service and must be run by hand:

1. **New instance enrolls and connects.** Generate an instance, launch its `.app`, enter
   the address, take the code, approve it on `/admin` — confirm it appears in
   `GET /api/state`.
2. **Two instances share one bundle.** Generate a second instance against the same
   `--bundle-dir`; confirm both launchers `--load-extension` the **same** directory
   (`readlink`/`grep` the launchers) and share one `chrome-extension://<id>` origin.
3. **Clone is rejected, the original is unharmed.** Copy an enrolled instance's `.app`
   (or its dir) to a second machine/profile, launch both; the second mints a new
   `install_uuid` and is **not** already enrolled — it must re-enroll rather than take
   over the first's socket. (This is the `install_uuid` guarantee — the generator relies
   on it, it is not re-implemented here.)
