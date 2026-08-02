# Deploying the curator service

Operator guide for `docker-compose.yml` (the Traefik web-service variant). Design
references are docs/architecture.md **§3** and **§12 «Безопасность»**. Pull the
prebuilt image from `ghcr.io`; do **not** build on prod.

All placeholders in the deploy files are `XXX` — replace them at deploy time from a
secret store. Never commit a real secret.

---

## 1. TLS / WSS — the 1006 trap

The extension connects from a service worker over **`wss://`**. In a service worker
a TLS/certificate error surfaces **only** as a WebSocket `close` with code **1006
and no reason** — no dialog, no JS error text (§3). So a cert problem looks
identical to any other network drop. Diagnose it explicitly:

1. **Open the URL directly in the browser.** Visit `https://<host>/healthz` (and the
   `wss://` origin as `https://`) in a normal tab. The browser shows the *real* cert
   error (expired, wrong host, untrusted CA, self-signed) that the SW hid.
2. **Test in a FRESH browser profile** (acceptance: «серт принимается расширением в
   свежем профиле»). A warm profile may have a manual exception or a cached cert that
   masks the failure the extension actually hits. A brand-new `--user-data-dir` must
   accept the cert with **no** manual exception — if it does not, the cert is not
   browser-trusted and the extension will 1006-loop.
3. **Confirm Traefik served the Let's Encrypt cert, not its default self-signed one.**
   The compose labels set `tls.certresolver: letsEncrypt` on `entrypoints: websecure`.
   If the container was unrouted while unhealthy (see the healthcheck note in
   `docker-compose.yml`), Traefik answers `:443` with its **default self-signed** cert
   and a 404 — which the SW again reports as a bare 1006. Check
   `curl -vI https://<host>/healthz` for the issuer and a `200`.

The service address is extension config (`serviceUrl`), not a design constant (§3).
The only requirement: the cert is accepted by the browser **without a manual
exception**.

---

## 2. Rolling redeploy is the NORMAL mode

Watchtower auto-updates the image (`com.centurylinklabs.watchtower.enable: "true"`)
and `restart: always` restarts the container: a redeploy is routine, not exceptional.
It is safe **by design** and must not be "protected" with clever orchestration:

- **Fencing-epoch lease (Фаза 8).** Each `/ext` connection owns a monotonically
  bumped connection epoch; every disconnect write is epoch-guarded, so a stale socket
  from the old container can never overwrite the new one's state. When the old
  container dies, extensions reconnect (driven by `chrome.alarms`) and resume —
  connections are not "lost", they re-establish (acceptance: «redeploy без потери
  соединений расширений после реконнекта»).
- **`user_version` re-read inside `BEGIN IMMEDIATE` (Фаза 2).** The schema/lease
  version is re-read at the start of every write transaction, so a just-started
  instance and a just-dying one never act on a stale view.

**Do NOT** invent a "start the new container, keep the old one serving, then swap
backwards / stop the new one" ordering. The epoch assumption is that the newest
connection wins; a backwards swap re-animates an older epoch and defeats the guard.
A plain rolling pull (stop old → start new; or watchtower's default) is correct.
The short healthcheck `interval`/`start_period` in `docker-compose.yml` exists so
Traefik re-routes to the new container quickly (see the comment there).

---

## 3. Private network — NOT reachable from the public internet (§12)

The service must not be exposed to the public internet. The image has **no
`EXPOSE`** and compose publishes **no port** — only Traefik reaches the container's
port 8000, over the shared `docker_main_net` (external) network. Enforce that
Traefik itself only serves the curator router on a **non-public path**:

- an **internal entrypoint** (bound to a private/VPN interface), **or**
- an **IP allow-list / forwarded-auth middleware** on the `curator` router, **or**
- reachability gated behind the VPN the extensions dial in on.

The bearer tokens are a second line, not the perimeter: keep the service off the
public internet regardless. Do not add a `ports:` mapping or an `EXPOSE` to "test
from outside" — use the internal network.

---

## 4. CORS ↔ extension-id match — the SILENT failure (§12)

`EXT_ALLOWED_ORIGINS` is one comma-separated allow-list used in **two** places: the
`/ext` hello check **and** the `/api/*` CORS middleware. It MUST list the real
`chrome-extension://<id>` origin(s) of the installed extension(s).

A mismatch is a **silent** failure, not an error:

- WebSocket traffic is not subject to CORS, so **`/ext` still connects** and the
  instance looks healthy everywhere.
- But the startpage's cross-origin `fetch /api/state` is cut at the **CORS
  preflight**, and §10 guarantees the newtab is never empty — so the page keeps
  rendering from **stale cache** («кэш от <время>») that never updates. Nothing looks
  broken.

Signals that expose the mismatch:

- The service compares the `hello.origin` against the list and sets
  **`reject_reason='origin'`** on the instance — visible as one of the four
  status-bar states (§10).
- A rejected CORS preflight increments **`curator_auth_rejections_total`** (the
  `cors_preflight` reason) — alert/inspect via `/metrics`.

Rules for the value:

- Set it to the exact installed extension id(s), e.g.
  `EXT_ALLOWED_ORIGINS=chrome-extension://<id1>,chrome-extension://<id2>`. The
  **instance generator** (§13, `tools/README.md`) pins the id via a manifest `key`
  and prints the exact `chrome-extension://<id>` origin — **all** instances share
  **one** id/origin, so a single entry covers every instance.
- **Empty in prod is wrong.** Empty leaves `/ext` open (accept-any + warning) and
  leaves `/api/*` CORS **CLOSED** (no `Access-Control-Allow-Origin` emitted — the
  secure default, never `*`). The startpage will not be able to call `/api/*` until
  the id is configured. Both cases log a one-time loud warning at startup.
- The header is **never** `Access-Control-Allow-Origin: *` — only an exact listed
  origin is ever echoed.

---

## 5. Tokens & volumes recap

- **`EXT_TOKEN`** — opens `/ext`, `/api/*`, `/mcp`. **`METRICS_TOKEN`** — read-only,
  opens `/metrics` only, and is the token that goes into the plaintext
  `deploy/scrape.yml` (§12). Both are REQUIRED; an empty value fails startup (§4).
- **`curator`** volume → `/app/data` (DB + WAL). **`curator_backups`** volume →
  `/app/backups`, mounted **separately** from the DB (§12), with
  `BACKUP_DIR=/app/backups`. The entrypoint `mkdir -p` + `chown`s an absolute
  `BACKUP_DIR` as root before dropping to `app` via gosu.
- **`curator_restore_marker`** volume → `/app/restore` (read-only for the service),
  with `RESTORE_MARKER_PATH=/app/restore/continuity-marker`. A **third** volume on
  purpose: see §8 below. ⚠️ **Write the marker once at install**, before the first
  `up -d` (§8 «Writing the marker») — until it exists, restore detection is the only
  guard that is not armed.

---

## 6. Operational acceptance (manual — no browser in CI)

These two acceptance items cannot be asserted by the Python test suite (no browser /
no second container in CI). Verify them by hand at deploy time:

- [ ] **Redeploy without losing connections.** Connect the extension, trigger a
      redeploy (or `docker compose up -d --pull always`), confirm the instance
      reconnects and `/api/state` resumes fresh — no data loss (§2 above).
- [ ] **Cert accepted in a fresh profile.** In a brand-new browser profile, the
      extension connects over `wss://` with no manual cert exception (§1 above).

Everything else — CORS allow-list echo (never `*`), `/metrics` needs
`METRICS_TOKEN`, `/api/*` needs `EXT_TOKEN`, the `reject_reason='origin'` path — is
covered by the automated tests (`tests/test_cors.py`, `tests/test_metrics_api.py`,
`tests/test_state_api.py`, `tests/test_ext_channel.py`).

## 7. Instances & token rotation (§13)

Themed browser instances are built with the **instance generator** — see
`tools/README.md` for the full guide. In short:

- `EXT_TOKEN=… make instance INSTANCE_ID=… SERVICE_URL=wss://host OUT=…` creates an
  instance (own profile, extension copy, `instance.json`, `.app`). No manual
  options-page edit is needed — `instance.json` carries all four config fields.
- `EXT_TOKEN=<new> make restamp OUT=…` rotates the token across **all** instances
  **and refreshes each copy's extension code** at once (then restart the browsers).
  Rotating by hand across N options pages would leave every instance silently dead
  in between.
- **Cloning a `.app` does not add an instance** — the clone is rejected as
  `duplicate_instance` (the `install_uuid` guarantee, §6). Run the generator again
  with a new `instanceId`.

The token assignment goes **before** `make`, never after it. Written after, it is a
make *argument*: it lands in `argv`, where it is visible in `ps` output for the whole
run and recorded in shell history. Before, it is an ordinary environment variable —
which is the only thing the generator reads (there is no `--token` option; a
`--token-file PATH` exists for the file case). The same rule applies to the CLI form.

**Refreshing the code is the point, not a bonus.** Each instance owns a *copy* of the
extension bundle (because `instance.json` lives inside it), while `protocolVersion` is
compared by **exact equality** (§6). So an extension update that bumps
`PROTOCOL_VERSION`, followed by a routine token rotation that only rewrote
`instance.json`, would leave every copy on the old code — each rejected on `hello`
forever, and visible **only** in the status bar (§13). `make restamp` therefore copies
the code from this repo's `extension/` by default, preserving the pinned manifest
`key` (same extension id/origin) and the profile (same `install_uuid`).
`--no-code-update` opts out; do not use it after a protocol bump.

Its three operational acceptance checks (new instance connects; re-stamp
reconnects; clone rejected) are a manual list in `tools/README.md`.

---

## 8. Restoring the DB from a backup (§7)

A restore is **not** a plain file copy. The DB comes back holding *yesterday's* world:
rules rolled back, `relocate` rows describing moves that already happened or never
will, `last_active_at` values from before the gap. If the curator resumed a normal
pass on top of that, it would act on a stale plan — closing tabs a newer decision had
already re-opened. §7 requires the first pass after a **continuity break** to be a
`dry_run` awaiting confirmation, and a restore is exactly such a break.

### Why an external marker is required

The curator detects breaks by comparing a **continuity fingerprint**
(`{db_uuid, user_version, IDLE_MINUTES, MAIN_INSTANCE_ID, restore_marker}`) against the
one stored by the last pass. A same-version restore defeats every DB-internal
component: the backup carries its **own** `db_uuid` inside its `settings` table, so the
restored DB looks perfectly continuous (see `WARNING 2` in `src/curator/clock.py`).
**Any** value kept inside the DB travels inside the backup — which is why the fix
cannot live there.

Hence `RESTORE_MARKER_PATH` (`/app/restore/continuity-marker`) on its **own**
`curator_restore_marker` volume: separate from `/app/data` (the DB) *and* from
`/app/backups`. The service hashes the file's contents into the fingerprint, so writing
a new value is what makes the restore visible. Put the marker on the DB volume and it
rolls back with the DB; put it on the backup volume and it travels inside the copy.
Both silently restore the old value and the break goes undetected.

Marker states, per `clock.read_restore_marker`:

- **`RESTORE_MARKER_PATH` empty/unset** — the marker digest is `null`. Restore
  detection is **off**; this is the default.
- **configured and readable** — a sha256 of the file's bytes. Content is free-form;
  only *change* matters.
- **configured, file not there** — a distinct `missing` state, compared normally, so a
  marker that *disappears* breaks continuity exactly once instead of being ignored.
- **configured, read failed** (wedged or not-yet-mounted volume) — an `unreadable`
  sentinel that is deliberately *not* comparable: a flaky mount is a gap in knowledge,
  not evidence of a restore, and must never arm a latch only a human can clear.

> ⚠️ **Turning the marker on (or off) later costs one confirmation.** `null` is a
> *recorded value*, not an absent one — an install that has been running with
> `RESTORE_MARKER_PATH` empty has `null` in its stored fingerprint. Setting the
> variable then changes that component to a digest, which is a continuity break by
> definition: the next pass is a `dry_run` and waits for one click. Clearing the
> variable again does the same in reverse. This is expected and happens **once** per
> change; it is not a symptom of anything. Only the *upgrade* case below is exempt,
> and only because the stored fingerprint has no marker key at all.

The volume is mounted **`:ro`** for the `curator` service on purpose: the service only
reads the marker, and a service able to rewrite it could "heal" the value and silently
disable the very detection this exists for. Writing is therefore an **operator**
action, done from a separate container that mounts the volume read-write (below) —
`docker compose run` cannot do it, because it builds the throwaway container from this
same service definition and inherits the `:ro` mount. Read-only is enforced by the
kernel at mount level; running as root does not bypass it.

### Writing the marker

Used twice: **once at install**, and **after every restore**. Same command both times.

**Get the volume's real name from compose — do not guess it.** Compose prefixes volume
names with the *project* name, and it normalises that name (lower-cased, characters
outside `[a-z0-9_-]` dropped), so a directory called `Arc.Extension` does **not** give
the prefix you would guess. Guessing wrong is silent and expensive: `docker run -v
<wrong-name>:/rw` **creates a brand-new empty volume**, your marker goes into it,
compose keeps using its own empty one, and you believe detection is armed when it is
not.

```bash
# Ask compose itself. `config` needs no containers and works before the first `up`:
docker compose config --format json | \
  python3 -c 'import json,sys; print("\n".join(json.load(sys.stdin)["volumes"]))'
```

If you prefer `docker volume ls`, note that the volume does not exist until compose
creates it — so on a fresh host you must materialise it first, and `--filter name=` is
a *substring* match that will list every project's copy on a host running more than one
(stage + prod). Pick the row whose prefix is this project's:

```bash
docker compose create curator          # creates volumes without starting anything
docker volume ls --filter name=curator_restore_marker
```

Then write a fresh uuid, mounting **that** volume read-write in a throwaway container:

```bash
# Any image with a shell works. The curator image has no USER directive, so `sh` runs
# as root — but on a brand-new host it is not pulled yet, so `alpine` is the smaller
# choice at install time.
docker run --rm -v <volume-name>:/rw alpine \
  sh -c 'cat /proc/sys/kernel/random/uuid > /rw/continuity-marker &&
         chmod 644 /rw/continuity-marker'
```

⚠️ **The `chmod 644` is not cosmetic — do not drop it.** You write this file as **root**,
but the service reads it as **uid 1000**. Under a `umask 077` (common on hardened
hosts) the redirect above creates it `600 root:root`, which uid 1000 can never read —
and an unreadable marker means restore detection is **silently off**, because the
`unreadable` state is deliberately not comparable (see the marker states above).

Verify it landed (this one *can* use the service definition — reading is all `:ro`
allows) and check the permissions while you are there:

```bash
docker compose run --rm --entrypoint sh curator \
  -c 'ls -l /app/restore/continuity-marker; cat /app/restore/continuity-marker'
```

**Then confirm the service can actually read it** — the check that matters, since the
failure is invisible from the outside:

```bash
# 0 = readable (detection armed). 1 = configured but UNREADABLE -> detection is blind.
curl -sS -K - https://<host>/metrics <<EOF | grep '^curator_restore_marker_unreadable'
header = "Authorization: Bearer $METRICS_TOKEN"
EOF
```

A `1` here means a restore would sail through as ordinary work: every other fingerprint
component travels inside the backup and matches, so the first pass would apply
yesterday's policy with no confirmation. The `curator-restore-marker-unreadable` alert
(`deploy/alerts.yml`) fires on this after 15 minutes for exactly that reason. The gauge
reads `0` until a pass has needed the marker, so check it after the service has been up
for at least one `PASS_INTERVAL`.

**On a NEW install, write the marker before the first `docker compose up -d`.** Once a
pass has recorded "the marker is `missing`", creating the file later is a genuine
change of a tracked component — a continuity break, costing one dry_run + confirmation.

> Note this does **not** buy you a click-free first run. A brand-new install whose
> browser has already connected takes its *own* first-run break by §7 (no stored
> fingerprint + a populated DB), so expect one `dry_run` + confirmation regardless.
> Writing the marker early avoids a **second**, avoidable one later.

**On an EXISTING install upgrading into this release, there is nothing to time.** The
stored fingerprint predates the marker component, so `clock._marker_comparable` skips
the comparison entirely: the first pass after the upgrade records the digest **silently
— no break, no `resume_pending`, no click** — and the component starts being compared
from the *second* pass onwards.

> ⚠️ **That costs one blind pass.** A restore performed in the window between the
> upgrade and the first pass is not caught **by the marker**. The other fingerprint
> components (`user_version`, `IDLE_MINUTES`, `MAIN_INSTANCE_ID`) and the clock-step
> guard still apply, so a restore that also moves any of those is still caught — but a
> same-version restore in that window is not. Two ways out, pick either:
>
> - let the curator complete **one** pass after the upgrade before you restore, or
> - restore anyway and rely on the **pause** in step 5 below. Do not rely on `dry_run`
>   alone: `dry_run` inspects, it does not block, and the periodic driver will start an
>   ordinary acting pass one `PASS_INTERVAL` after startup whether you have finished
>   reading or not. The pause is the thing that holds it.

### Restore procedure

1. **Stop the service** so nothing writes while the DB is swapped:

   ```bash
   docker compose stop curator
   ```

2. **Pick the copy.** Backups are `curator-<timestamp>.db` under `/app/backups`
   (nightly `VACUUM INTO`, newest last):

   ```bash
   docker compose run --rm --entrypoint sh curator -c 'ls -la /app/backups'
   ```

3. **Put the copy in place** as `/app/data/curator.db`. Remove the stale WAL/SHM
   sidecars — a `VACUUM INTO` copy is self-contained, and leaving an old `-wal` next
   to a *different* database is how a restore corrupts itself:

   ```bash
   docker compose run --rm --entrypoint sh curator -c '
     cp /app/backups/curator-<timestamp>.db /app/data/curator.db &&
     rm -f /app/data/curator.db-wal /app/data/curator.db-shm &&
     chown app:app /app/data/curator.db'
   ```

4. **Write a NEW value into the marker** — the step that makes the restore
   *detectable*. Use the command in **«Writing the marker»** above (it must mount the
   volume read-write; `docker compose run` inherits the `:ro` mount and fails with
   `Read-only file system`). Only the *change* matters, so a fresh uuid is enough. Do
   this on **every** restore, including a re-restore of the same copy.

5. **Start the service, then immediately pause it.** The periodic driver sleeps one
   `PASS_INTERVAL` (default 5 min) *before* its first pass, and that sleep is your
   entire window — the pause must be set inside it. It cannot be set earlier: the pause
   lives in the DB you just replaced.

   ```bash
   docker compose up -d curator
   # …then, without waiting:
   curl -sS -K - -X POST -d '{"minutes":120}' https://<host>/api/pause <<EOF
   header = "Authorization: Bearer $EXT_TOKEN"
   EOF
   ```

   The token goes in a `-K` config read from **stdin**, never in `curl -H …`: an
   argument is visible in `ps` to every user on the box for the lifetime of the request
   (same rule as §7 above). `$EXT_TOKEN` is expanded by the shell into the heredoc, so
   it never reaches `argv` and nothing is written to disk.

   A pause blocks mutating passes but **not** `dry_run` (§7), which is exactly the
   combination this step needs: nothing acts, and you can still look.

6. **Read the plan, then release deliberately.**

   ```bash
   # What WOULD it do? (works under the pause)
   curl -sS -K - -X POST -d '{"dry_run":true}' https://<host>/api/run_pass <<EOF
   header = "Authorization: Bearer $EXT_TOKEN"
   EOF
   ```

   If the plan looks wrong, keep the pause and investigate — nothing is acting.

   Only when it looks right, lift the pause. ⚠️ `DELETE /api/pause` **runs a pass
   immediately** (§7): it is a deliberate action, not a cleanup step. What that pass
   does depends on whether the break was detected — and the response tells you, in
   `pass.status`:

   ```bash
   curl -sS -K - -X DELETE https://<host>/api/pause <<EOF
   header = "Authorization: Bearer $EXT_TOKEN"
   EOF
   ```

   - `pass.status = "resume_pending"` — the break WAS detected. Nothing acted; the plan
     is armed and waits for your click. Release it:

     ```bash
     curl -sS -K - -X POST -d '{"confirm_pending":true}' https://<host>/api/run_pass <<EOF
     header = "Authorization: Bearer $EXT_TOKEN"
     EOF
     ```

   - anything else — no break was detected, so that pass **acted right away** on the
     restored world. This is the blind window (or a missed step 4). If you are not
     certain the plan you read above was correct, re-arm the pause *before* lifting it
     next time, and see «If you forget step 4» below.

   **How to tell whether the break was detected** — by state, not by logs. The detector
   arms `resume_pending` silently; it writes no log line, and after a correct restore
   the marker reads fine so `clock` logs nothing either. Watching `docker compose logs`
   for a "continuity break" message will mislead you: the message does not exist, so
   its absence tells you nothing. Check the state instead:

   ```bash
   # 1 = a break is armed and waiting for a click; 0 = no latch.
   curl -sS -K - https://<host>/metrics <<EOF | grep '^curator_resume_pending'
   header = "Authorization: Bearer $METRICS_TOKEN"
   EOF
   ```

   The same fact is in `GET /api/state` (`resume_pending`) and in the startpage status
   bar, so a restore left unconfirmed does not hide. A **`0` here after a restore means
   the marker change was not seen** — you are in the blind window (or step 4 was
   missed): keep the pause on and use «If you forget step 4» below.

### If you forget step 4 (or restored in the blind window)

Both have the same effect: the marker never changed as far as the curator is concerned,
so it resumes as if nothing happened and the next pass acts on the restored (stale)
world. Redoing the restore is not required. **Pause first** — it is the only step that
protects you immediately and works regardless of fingerprint state:

```bash
# 1. Stop the bleeding. Nothing mutating runs while this holds.
curl -sS -K - -X POST -d '{"minutes":120}' https://<host>/api/pause <<EOF
header = "Authorization: Bearer $EXT_TOKEN"
EOF

# 2. Read what it wants to do (dry_run is not blocked by the pause).
curl -sS -K - -X POST -d '{"dry_run":true}' https://<host>/api/run_pass <<EOF
header = "Authorization: Bearer $EXT_TOKEN"
EOF

# 3. Write a fresh marker value (see «Writing the marker»), then lift the pause
#    deliberately — DELETE runs a pass at once.
```

> Why pause rather than just write the marker: if the *first* post-upgrade pass has not
> run yet, the stored fingerprint still has no marker key, so a new marker value is
> recorded silently and arms nothing (that is the blind window, by definition). Once
> that first pass has run — which is the case whenever a pass has already acted on the
> stale world, i.e. whenever you actually need this section — the key is present and a
> fresh value does arm the break on the next pass.

Treat anything the curator did between the restore and this point as suspect: closed
tabs are not recoverable from the plan.
