# Instance generator (§13)

`tools/instancegen` builds themed Brave instances under **enrollment** (§7/§13, issue
#35). It is a two-step, **token-free** flow:

1. **`bundle`** — build the ONE universal extension bundle the whole fleet loads. It is
   a copy of `extension/` and nothing else: no token, no service URL, no `instanceId`
   and no signing key are baked in.
2. **`generate`** — wrap that shared bundle in a per-instance `.app` (its own
   `--user-data-dir`, an icon, and a launcher whose `--load-extension` points at the
   **shared** bundle). It writes **no** extension copy and **no** `instance.json`.

By default the launcher **also** loads the **main Brave profile's** store-installed
extensions (Bitwarden, DeepL, …) unpacked, alongside the shared bundle — `--load-extension`
takes a comma-separated list, and the launcher builds that list **at every launch** by
globbing the main profile's `Extensions/<id>/<version>_0/` dirs. It is still one shared
bundle and still no per-instance copy of anything: those dirs are **read** from the main
profile, never copied and never written to. Turn it off with `--no-sync-extensions`, point
it elsewhere with `--sync-extensions PATH`. Extension **state** does not come along (see
the caveats below).

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

With extension sync on, the flag carries ~27 unpacked extensions instead of one, and that
has visible costs:

- **A bigger developer-mode nag bubble on every launch.** Brave/Chromium warns about
  extensions running in developer mode, and the bubble lists them — with the whole synced
  set it is a long list, on every single start of every instance.
- **Extension STATE is not carried by the launcher.** The vault session, per-extension
  settings and local storage live in the profile's `Local Extension Settings`, which is
  empty in a fresh instance: the extensions arrive installed but **logged-out and
  unconfigured**. That is the default because it is per instance, not fleet-wide — see
  `make instance-state` below to copy it into a chosen instance once.
- **The version loaded is the newest on DISK, not necessarily the one the main browser has
  ACTIVE.** Chromium unpacks an update ahead of time and activates it later
  (`idle_install_info` in `Secure Preferences`); measured on the owner's real profile, 2 of
  26 diverged on the day this was written. Usually that only means "slightly newer" — but
  `idle_install_info` is also where an update requesting **new permissions** waits for the
  user's approval, and a `--load-extension` extension is granted its manifest's permissions
  **with no prompt**.
- **A directory in `Extensions/` does not mean the extension is ENABLED.** Disabling one in
  `brave://extensions` writes `state`/`disable_reasons` into prefs and leaves the directory;
  an uninstalled one lingers until garbage collection. The glob loads both and
  `--load-extension` activates unconditionally, so an extension disabled in the main browser
  is **alive in every instance**. The owner is not the only writer of `disable_reasons` —
  the browser sets it too (a Web Store blocklisting, e.g. an extension pulled for
  **malware**, the greylist, enterprise policy) and the directory is deliberately kept so
  the extension can be restored, so **a killswitch that disabled an extension in the main
  browser is bypassed in the instances**. On a profile that carries a password manager and a
  crypto wallet that is a different class of consequence from "I turned it off and it still
  runs".

Reading `Secure Preferences` was **considered and rejected — not impossible**.
`/usr/bin/plutil` ships in the **base** macOS install (a real Mach-O, unlike the
`/usr/bin/python3` shim), reads Chromium's JSON and answers both questions directly:
`plutil -extract "extensions.settings.<id>.path" raw -o - "Secure Preferences"` gives the
**active** version dir and `…disable_reasons` the enablement state, 26 sequential calls in
0.22 s wall, with a missing/corrupt file answering empty on stdout and complaining on
stderr. The mtime glob stays anyway, on three reasons:

1. **The shipped branch would leave CI.** The tests run the generated launcher end-to-end on
   any machine. `plutil` is macOS-only, so the launcher would become
   `plutil … || <mtime fallback>` and a Linux runner would only ever exercise the fallback —
   green CI on a branch that never runs on the target platform. That is the disease
   `_tiny_repo_extension`'s docstring was written against, with the **platform** deciding
   whether the assertion runs, and the untested branch would be the shipped one.
2. **The fallback survives regardless.** `Secure Preferences` is written lazily and can be
   caught mid-flush (verified: `plutil` answers empty on a truncated file), so the glob stays
   as the fallback either way — both costs above merely become rarer, at double the
   complexity in the one script that must never fail.
3. **It binds to undocumented Chromium internals.** `extensions.settings.<id>.path` and
   `disable_reasons` are private schema, not an API; a rename would fall back to the
   heuristic **silently** — a new silent divergence replacing the one it removed.

## Copying extension state into an instance (`make instance-state`)

```bash
# Look before you leap: lists the ids, what ARRIVES, what it DELETES at the
# destination, the totals and every exclusion with its reason — and writes nothing.
# Safe to run with Brave up.
make instance-state INSTANCE_DIR=~/Applications/infra DRY_RUN=1

# The real thing (quit Brave first).
make instance-state INSTANCE_DIR=~/Applications/infra
make instance-state INSTANCE_DIR=~/Applications/infra ONLY=<bitwarden id>
```

It copies the store extensions' state — `Local Extension Settings/<id>` and, when it
exists, `Sync Extension Settings/<id>` — from the main profile (`FROM=<Default dir>`) into
one instance, so Bitwarden and friends come up logged in. The destination path is identical
to the source's because the ids are (each store manifest carries a `key`, so Chromium
hashes that, not the load path). **Quit Brave first** — the main browser *and* every
instance `.app`: these are live LevelDB databases, a snapshot taken under their own writer
can be corrupt, and the command refuses to run while any Brave process is alive. There is
deliberately **no `--force`**. The refusal names each process by **pid**, and says which of
them are browsers to quit and which are helpers/PWA shims to kill. The check **fails
closed** on every outcome it cannot positively read as "nothing matched" — a `pgrep` that
errors (exit 2/3), one that claims a match and prints nothing, a line it cannot parse, and
an empty set of binary names to search for (which would mean `pgrep` never ran at all).

Seven things it is important not to misread:

- **The destination is not empty, and the run DELETES what is there.** Each `<id>` directory
  is replaced whole, so whatever the instance had stored for that extension — its own
  wallet, its own logged-in vault — is gone, with no backup and no undo. The plan therefore
  sizes **both** sides: every row ends in `DELETES <n>` or `replaced nothing`, a row that
  destroys a known wallet/vault (MetaMask, Bitwarden) gets a line of its own naming what it
  is, and a `DELETES:` total sits next to the `total:` of what arrives. Read it before
  running: on the owner's `infra` instance the plan deletes **95.9 MB** across all 21 ids,
  including that instance's own **19.4 MB MetaMask seed vault** and **7.3 MB Bitwarden
  vault**. (The *arriving* figure moves between dry runs — ~91–100 MB — because the source
  is a set of live LevelDBs compacting under the running browser. One more reason the real
  run refuses while Brave is up.)
  (The id list that decides this is the safety filter's; the wallet/vault emphasis comes
  from a small id → label map that **only** affects wording and never what is copied.)
- **It is a ONE-TIME COPY and cannot be a live sync.** A LevelDB has a single writer and is
  lock-protected, so two browsers cannot share one directory — a symlink would only make
  the instance see broken storage. The two profiles hold **independent** copies afterwards:
  a vault entry added in one does not appear in the other. Re-run to re-align (it
  overwrites, never merges).
- **Locally independent is not server-side independent.** The copied Bitwarden storage
  carries its `appId` — the device identifier the server ties a device and its refresh token
  to (confirmed present in the real storage) — so after the copy the two profiles present
  the **same device identity**. What that does to a logout, a "deauthorize sessions" or a
  device-approval prompt in one profile was **not verified**; assume they are one device
  until you have checked. (The previous wording asserted that "logging out here does not log
  the others out". That was only ever true of the local databases.)
- **Two layers decide what may be copied**, and they are the safety filter, not tidiness.
  (1) The source must really have the extension **installed unpacked** —
  `Extensions/<id>/<version>/manifest.json`, the launcher's own definition of installed —
  and symlinks are not followed, so an empty or half-removed `Extensions/<id>` does not
  qualify. (2) The extension **this instance loads unpacked** is excluded by its derived id:
  Chromium's id for an unpacked dir is `sha256(absolute load path)`, first 16 bytes, each
  nibble mapped `0-15 → a-p`, and that id is read out of the instance's own launcher. The
  curator extension is loaded unpacked from a shared directory, so it has no `Extensions/`
  dir in any profile while carrying the **same** id in all of them, and its
  `chrome.storage.local` holds *that instance's* `install_uuid` and per-install secret — a
  blanket copy would overwrite the instance's identity with the main browser's and the
  service would see a different install. No id is hard-coded by either layer (on the owner's
  profile: 21 of 28 state dirs are eligible, and the curator is excluded by both).
  Layer (b) reads the instance's launcher, so a **missing, unreadable or
  `--load-extension`-less launcher makes it yield nothing** — the run then proceeds on layer
  (a) alone. That is a deliberate degradation and it is now a **printed** one: the output
  carries a `identity guard layer (b) UNAVAILABLE` note with the cause, instead of leaving a
  weaker guard to be inferred.
- **An excluded id is not automatically an identity.** Only the id this instance loads
  unpacked gets the "this install's identity (install_uuid + enrollment secret)" wording.
  The other six exclusions on the owner's profile are Chrome **component** extensions (Web
  Store `mnojpmjdmbbfmejpflffifhffcmidifd`, Docs Offline `ghbmnnjooekpmoecnnnilnnbdlolhkhi`,
  …) — they have state and no `Extensions/<id>`, they are excluded for that reason, and they
  are described as components. When layer (b) is unavailable the two cannot be told apart,
  and the reason says exactly that rather than picking one and sounding certain.
- **It removes the login, not necessarily the unlock.** The account and the encrypted vault
  come along, so email + master password + 2FA are not needed again. Whether the vault comes
  up **unlocked** is the Bitwarden vault-timeout setting's business: with «Never» + «Lock»
  the derived key is persisted and it should; otherwise the master password is asked once.
- **Bitwarden is not the only one, and the vaults then exist in one more profile on this
  disk.** MetaMask (`nkbihfbeogaeaoehlefnkodbefgpgknn`) is store-installed and therefore
  eligible too, and its `chrome.storage.local` holds the wallet's **encrypted seed vault**.
  Use `ONLY=` if you want the password manager without the wallet.

Each `<id>` directory is **replaced**, not merged (mixing fresh `.ldb` files with a stale
`MANIFEST` yields a database that is neither), through the same stage-and-swap as
`bundle --force`, so an interrupted copy leaves *that id's* previous state intact. The
source profile is **only ever read**.

**The commit is per id, not per run.** If the copy dies half-way (a full disk, a permission
error), the ids it already finished are already replaced and their previous state is gone.
That list is printed on stderr before the error propagates, so a half-migrated profile is at
least a *known* half-migrated profile — and re-running is safe, because the copy overwrites.
The total size is checked against `df` on the destination up front (the copy plus the one
tree being staged), so "no space left on device" half-way through should not happen at all.

**Stale staging dirs are swept at the start of every run.** The stage-and-swap builds into
`<destination>/.rebuild-XXXX/new/`; a `SIGKILL` or a power cut between the build and the
swap leaves that directory behind holding a **partial copy of the vault**, next to the real
one, and nothing else ever removes it. Every run deletes the `.rebuild-*` dirs it finds
beside the two destinations before it starts (`--dry-run` lists them instead).

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
   `--bundle-dir` and confirm they share one `chrome-extension://<id>` origin for the
   curator extension. ⚠️ **Grepping for `--load-extension` no longer shows the answer**:
   with sync on (the default) the launchers read `--load-extension="$EXTS"`, and `$EXTS`
   is built a few lines above from `EXTS=<bundle>` plus the main profile's dirs. Compare
   the `EXTS=` line (the shared bundle) — or run both launchers and read the flag off
   `brave://version`, which is what the browser actually got.
2a. **The synced extensions arrive, and the main profile is untouched.** Launch an
   instance and confirm on `brave://extensions` that the main profile's extensions are
   there with their real ids, logged-out. Then confirm the main browser is unharmed: it
   keeps running normally and nothing under its `Extensions/` changed (the launcher only
   reads it — pinned by `test_launcher_never_writes_into_the_main_profile`).
3. **Clone is rejected, the original is unharmed.** Copy an enrolled instance's `.app`
   (or its dir) to a second machine/profile, launch both; the second mints a new
   `install_uuid` and is **not** already enrolled — it must re-enroll rather than take
   over the first's socket. (This is the `install_uuid` guarantee — the generator relies
   on it, it is not re-implemented here.)
