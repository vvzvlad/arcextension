# Instance generator (§13)

`tools/instancegen` builds a **themed Brave instance**: a `.app` wrapper with its
own `--user-data-dir`, an icon, a per-instance copy of the extension bundle, and
the `instance.json` that a fresh, empty profile has no other way to receive. It is
a dev/ops tool — **not** part of the runtime service in `src/`.

Design references: `docs/architecture.md` **§13 (Инстансы)**, **§6** (config
source), **§12** (`EXT_TOKEN`). Read those before deploying.

## Why a generator (and not "copy the .app")

A fresh `--user-data-dir` starts with an **empty** `chrome.storage.local`, so the
extension's `serviceUrl` (§3) and `EXT_TOKEN` (§12) cannot appear "by themselves".
The generator writes an `instance.json` **next to the bundle** with the FOUR
required fields — the SW reads it via `chrome.runtime.getURL` on every start and
treats it as authoritative over storage:

```json
{ "instanceId": "…", "title": "…", "serviceUrl": "wss://host", "token": "…", "allowExecuteJs": false }
```

All four are required; none is defaulted away. Each instance gets its **own copy**
of the bundle because `instance.json` lives inside it and must differ.

**Cloning a `.app` does NOT add an instance.** A clone shares the same
`instanceId` but each profile mints its own `install_uuid` (in the profile, not
the bundle) on first run; the service rejects the second one as
`duplicate_instance` (§6). To add an instance, run the generator again with a new
`instanceId`.

## Usage

The token is a secret — pass it via the `EXT_TOKEN` env, never on the command line.
There is deliberately **no `--token` option**: argv is world-readable in `ps` output
and is recorded in shell history. Note the assignment goes **before** the command, so
it is an environment variable rather than an argument. For the file case there is
`--token-file PATH`, which puts a *path* — not the secret — in argv.
`make instance` / `make restamp` wrap the CLI (`tools/generate_instance.py`).

```bash
# Create an instance
EXT_TOKEN=… make instance \
    INSTANCE_ID=main SERVICE_URL=wss://curator.example.com \
    OUT=~/arcextension-instances TITLE="Curator Main"

# …or the CLI directly (more options: --key-file, --icon, --brave-binary,
#   --allow-execute-js, --overwrite, --token-file)
EXT_TOKEN=… .venv/bin/python -m tools.generate_instance generate \
    --instance-id main --service-url wss://curator.example.com \
    --out ~/arcextension-instances --title "Curator Main"
```

The command prints the derived **extension id** and the exact
`chrome-extension://<id>` origin to add to `EXT_ALLOWED_ORIGINS` (§12). All
instances share **one** id/origin — one entry covers every instance.

### Re-stamp: token rotation **and** code updates

Re-stamp is a **single** operation over all instances — not a walk of N options
pages (during which every instance would be silently dead):

```bash
EXT_TOKEN=<new-token> make restamp OUT=~/arcextension-instances
# then restart each browser so the SW re-reads instance.json
```

It does two things to every instance:

1. **Rotates the config.** Rewrites `token` in each `instance.json`, **preserving**
   each `instanceId` and the profile (so `install_uuid` survives → a restart
   reconnects, not a duplicate). `instanceId` is **immutable**; a "rename" changes
   only `title` (delivered via `hello`). `restamp --service-url` also moves the
   `serviceUrl` and re-stamps the manifest host.
2. **Refreshes the extension code** from this repo's `extension/` (override with
   `--extension-dir`). This is not optional in practice. Each instance owns a *copy*
   of the bundle — because `instance.json` lives inside it — while `protocolVersion`
   is compared by **exact equality** (§6). So shipping an extension update that bumps
   `PROTOCOL_VERSION` and then rotating the token *without* carrying the code would
   leave every copy on the old code, each rejected on `hello` **forever**, visible
   only in the status bar (§13). Preserved across the refresh: the pinned manifest
   `key` (so the extension id/origin never moves) and the profile.

`--no-code-update` rotates config only. Do not use it after a protocol bump — that
is the exact failure it re-opens. The new bundle is staged beside the old one and
swapped in only once complete, so a failed copy leaves a working instance behind.

## The signing key (extension id pinning)

The unpacked extension id is normally a hash of the **load path**, so it would
move on rename and differ per instance (breaking `EXT_ALLOWED_ORIGINS`/CORS).
Stamping a `key` (base64 SPKI-DER public key) into the manifest pins the id to the
KEY instead.

The generator creates an RSA-2048 keypair **once** and persists the private key at
`<OUT>/.instancegen/signing_key.pem` (mode `0600`), reusing it for every instance
and every re-stamp so the id is stable and identical across all instances.

- **Never commit** the key or any real `instance.json` (the token is a secret).
  The repo ships only `extension/instance.example.json` and a manifest with a
  placeholder key; `extension/instance.json` is gitignored.
- **Do not lose** `<OUT>/.instancegen/signing_key.pem`: regenerating it changes
  the extension id/origin. Bring your own with `--key-file` (a PEM private key or
  a raw base64 public key) to keep a fixed id across machines.

## Icon and `.app` on non-macOS (CI)

The generator always emits the full `.app` file tree (launcher, `Info.plist`) and
a real per-instance PNG **icon source** (a solid colour derived from the title,
generated with stdlib `zlib` — no PIL/mac tools). The **only** mac-specific step
is turning that PNG into a proper `AppIcon.icns` via `sips` + `iconutil`; off
macOS `build_icns` is a reported no-op and the `.app` still runs with the generic
app icon. Regenerate the `.icns` on a mac for the themed Dock icon.

## `--load-extension` caveats (Brave, not Chrome)

The launcher execs the **system Brave**. `--load-extension` is gated behind
`BUILDFLAG(GOOGLE_CHROME_BRANDING)`: **Google Chrome refuses it** ("--load-extension
is not allowed in Google Chrome"). It also does **not** work with **Enhanced Safe
Browsing** enabled or under the `ExtensionInstallTypeBlocklist` policy (§13).

## Manual acceptance (needs a real browser + service — NOT covered by pytest)

The pure core is unit-tested (`tests/test_instancegen.py`). The operational
acceptance below requires a live browser and service and must be run by hand:

1. **New instance connects with zero manual edits.** Generate an instance, launch
   its `.app`, and confirm it appears in `GET /api/state` — no options-page edit.
2. **Re-stamp rotates everywhere.** Rotate the token, restart the browsers, and
   confirm all instances reconnect (`hello_ack ok`) with the new token.
3. **Clone is rejected, the original is unharmed.** Copy an instance's `.app` (or
   its dir) to a second machine/profile, launch both; the second is rejected with
   `reject_reason='duplicate_instance'` in the status bar while the first keeps
   its socket. (This is the `install_uuid` guarantee — the generator relies on it,
   it is not re-implemented here.)
