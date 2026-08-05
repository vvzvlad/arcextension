# Instance generator (§13)

`tools/instancegen` builds themed Brave instances under **enrollment** (§7/§13, issue
#35). It is a two-step, **token-free** flow:

1. **`bundle`** — build the ONE universal extension bundle the whole fleet loads. It is
   a copy of `extension/` and nothing else: no token, no service URL, no `instanceId`
   and no signing key are baked in.
2. **`generate`** — wrap that shared bundle in a per-instance `.app` (its own
   `--user-data-dir`, an icon, and a launcher whose `--load-extension` points at the
   **shared** bundle). It writes **no** extension copy and **no** `instance.json`.

The service address, the instance NAME and the per-install secret are **not** in the build
at all — each profile enters the first two through the extension's **enrollment settings
UI** and generates the third itself, while the operator's part is opening a short window on
`/admin` and handing over its code (see `deploy/DEPLOY.md`). It is a dev/ops tool — **not**
part of the runtime service in `src/`.

⚠️ **`--instance-id` here names the `.app` and its profile, not the service-side instance.**
The service-side `instance_id` is the **browser name typed in the extension settings** at
enrolment, and it is the only name the service knows (§6). Use the same string for both and
they line up; they are not otherwise connected, and `generate` cannot enrol anything.

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

# 1a. Rebuild it after a code change — SAME dir, so the id (and every profile's
#     storage) survives. `make dev-bundle` also rebuilds the startpage first.
make dev-bundle OUT=dist
#   …or the CLI directly:
.venv/bin/python -m tools.generate_instance bundle --out dist --force

# 2. Wrap it in per-instance .apps. The launcher loads the SHARED bundle — no copy.
make instance INSTANCE_ID=main BUNDLE_DIR=dist \
    OUT=~/Applications TITLE="Curator Main"
#   …or the CLI directly (more options: --icon, --brave-binary, --overwrite):
.venv/bin/python -m tools.generate_instance generate \
    --instance-id main --bundle-dir dist \
    --out ~/Applications --title "Curator Main"
```

⚠️ **`--title` is the macOS `.app` display name and the icon colour seed — nothing else.**
It is not the service-side name: the service has no display title at all (the column was
dropped, §6), and the name it knows is the one typed in the extension settings. Two knobs
that both look like "the name", so: `--title` is what the Dock shows, the extension's
"Browser name" is what `/admin` and the rules show.

Neither step prints an extension id, because nothing consumes one. All instances load
the same shared bundle and therefore share one `chrome-extension://<id>` — Chromium's
hash of that dir's absolute path — but no origin is checked anywhere: `/ext` does not vet
`hello.origin` and `/api/*` CORS accepts any origin (`src/api/cors.py`).

⚠️ **The SERVICE does not care about the id; the BROWSER does.** Moving or renaming a
bundle a browser already loads gives it a new id, hence a new origin and an EMPTY
`chrome.storage.local` — the profile's enrolment (service address + per-install secret)
is gone and that instance has to enrol again. So ship code updates by rebuilding the SAME
directory: `bundle --force` (or `make dev-bundle`) does exactly that — it stages the copy
next to the target and swaps it in, so the path never changes and an interrupted rebuild
cannot leave a half-written, unloadable bundle. Without `--force` an existing `--out` is
still refused.

`--service-url` on `generate` is accepted for backward-compatibility but is
**informational only**: the address is entered per profile during enrollment, never
stamped into the build.

## Adding / enrolling an instance

1. Run `generate` for the new `--instance-id` and launch its `.app`.
2. Open the extension's settings and enter the service address and the **browser name**
   — that name becomes the service-side `instance_id`, so keep it to
   `A-Za-z0-9._-` (1-64 chars, no spaces) and do not reuse a live instance's name.
3. Open a window on `/admin`, take the **code** it shows, type it in the same settings
   page and submit. The instance is active immediately — there is no approval step
   (`deploy/DEPLOY.md`). Close the window afterwards.

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
   the address and a name, open a window on `/admin` and type its code — confirm the
   settings page flips to «активен» and the instance appears in `GET /api/state` under
   that name.
1a. **A taken name is refused loudly.** Try to enrol a second instance under the name of a
   LIVE one: the settings page must show «имя уже занято», the live instance must keep
   working, and `curator_auth_rejections_total{reason="enroll_id_taken"}` must tick.
2. **Two instances share one bundle.** Generate a second instance against the same
   `--bundle-dir`; confirm both launchers `--load-extension` the **same** directory
   (`readlink`/`grep` the launchers) and share one `chrome-extension://<id>` origin.
3. **Clone is rejected, the original is unharmed.** Copy an enrolled instance's `.app`
   (or its dir) to a second machine/profile, launch both; the second mints a new
   `install_uuid` and is **not** already enrolled — it must re-enroll rather than take
   over the first's socket. (This is the `install_uuid` guarantee — the generator relies
   on it, it is not re-implemented here.)
