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

### ⚠️ The shipped compose labels are the PUBLIC form — narrow them before you deploy

`docker-compose.yml` is an example, and its router labels describe a **publicly
resolvable** service: `entrypoints: websecure`, `tls.certresolver: letsEncrypt`, and
**not one `middlewares` label**. Nothing in the repo enforces the perimeter this section
demands — "no `ports:` mapping" only keeps the *container* off the host, it says nothing
about who can reach *Traefik*. If you deploy those labels unchanged on a Traefik whose
`websecure` entrypoint is bound to a public interface, the curator is on the public
internet with only its bearer tokens in front of it.

Pick one and add it to the `curator` service's `labels:` before the first `up -d`:

```yaml
      # (a) IP allow-list on the router — the smallest self-contained change:
      traefik.http.routers.curator.middlewares: curator-allowlist@docker
      traefik.http.middlewares.curator-allowlist.ipallowlist.sourcerange: "10.0.0.0/8,192.168.0.0/16"

      # …or (b) move the router to an INTERNAL entrypoint bound to a private/VPN
      # interface (defined in your Traefik static config), and drop letsEncrypt:
      traefik.http.routers.curator.entrypoints: internal
```

**This is load-bearing, not belt-and-braces.** The enrollment window code is six
characters over a 31-symbol alphabet (~30 bits) and lives ~10 minutes, and there is **no
server-side lockout after N bad codes** — `curator-enroll-code-bruteforce`
(`deploy/alerts.yml`) *detects* a burst, it does not *stop* one. Each guess is one cheap
socket that the service closes right after `enroll_rejected`; the only ceiling is
`ENROLL_PREAUTH_MAX` on *concurrency*, not on total attempts. Behind a private perimeter
that is amply safe. On a publicly reachable endpoint the guess rate is bounded by
bandwidth alone, and every window an operator opens is an exposure. The same goes for
`/admin`: it is the surface that mints instance credentials, and it is protected by
`ADMIN_TOKEN` and nothing else.

---

## 4. CORS — there is nothing to configure (and nothing left to get wrong)

**This section used to describe a deployment step and a failure class. Both are gone.**
`EXT_ALLOWED_ORIGINS` no longer exists, `/api/*` CORS accepts **any** origin, and the
`/ext` hello check no longer looks at `origin` at all. If you are following an older
runbook: skip the step, and do not set the variable — nothing reads it.

Why it went, in one line each (the full argument lives in `src/api/cors.py`):

- every `/api/*` route is already behind `require_api_caller` — an `ADMIN_TOKEN` or an
  enrolled instance's secret. CORS was the second lock on that door, never the first;
- `allow_credentials=False`, so nothing ambient (cookie, client cert, HTTP auth) is ever
  attached to a cross-origin call. With credentials off, `*` is the standard answer and
  there is no ambient session for a foreign page to ride;
- CORS is enforced by **browsers only**. A script, `curl` or a bot ignored the list
  entirely, so it never stopped an attacker — only a page, and only a page that had no
  credential anyway;
- the only unauthenticated readable route is `/healthz`, and §3 above requires the
  service to be unreachable from the public internet regardless.

What you get back: the extension **signing key is gone too** (it existed only to pin one
`chrome-extension://<id>` so a single origin could be listed here). There is no secret to
lose, and adding a machine no longer means editing the server's environment.

**One silent failure remains, and it is a code bug, not a deploy step.** `src/api/cors.py`
still declares the `/api/*` methods and request headers explicitly. If the startpage ever
sends a verb or a custom header that is not declared there, the browser's preflight is
refused, `/ext` stays connected, the instance stays green, and only the newtab's fetch
dies — it keeps rendering «кэш от <время>» that never updates. That is what
`curator_auth_rejections_total{reason="cors_preflight"}` counts and what
`curator-cors-preflight-rejected` (§12) alerts on. The fix is to add the method/header in
`src/api/cors.py`, not to change anything on the server.

---

## 5. Tokens & volumes recap

- **`ADMIN_TOKEN`** — opens `/admin` (the enrollment console), `/api/*` (as the
  human/agent caller) and `/mcp`. **`METRICS_TOKEN`** — read-only, opens `/metrics`
  only, and is the token that goes into the plaintext `deploy/scrape.yml` (§12). Both are
  REQUIRED and must DIFFER; an empty value fails startup (§4). There is **no shared /ext
  token** — instances authenticate by a per-install secret entered during enrollment
  (§7).
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
`METRICS_TOKEN`, `/api/*` needs `ADMIN_TOKEN` (or an active-instance secret), the
`reject_reason='origin'` path — is covered by the automated tests
(`tests/test_cors.py`, `tests/test_metrics_api.py`, `tests/test_state_api.py`,
`tests/test_ext_channel.py`).

### Editing the alert rules — `make alerts`

`deploy/alerts.yml` is the alert set the deployed curator runs on and
`deploy/alerts_test.yml` holds its fire / stay-quiet cases. One target validates both:

```bash
make alerts        # promtool check rules  +  promtool test rules
```

Run it after **any** edit to either file. The schema half is not politeness: Prometheus
and vmalert each unmarshal the rule file strictly, so one unknown field rejects the
*whole* file and the curator runs with **no alerts at all** — the failure that looks
exactly like nothing being wrong. The cases half is the other half of the same job: a
rule can be schema-valid and still never fire.

Locally, a missing `promtool` makes the target print how to install it
(`brew install prometheus`) and exit 0 — it is not a Python dependency and `make install`
cannot provide it. In CI it is a hard failure: the `test` job installs a pinned promtool
and runs `make alerts` with `CI=true` (which turns that skip into an error), and the image
`build` job depends on `test` — so rules that do not parse, or that stopped firing, never
reach a published image.

## 7. Instances & enrollment (§13)

Under enrollment there is **no shared token to distribute or rotate**. Each instance
authenticates with a **per-install secret** it generates itself; the operator approves it
once, on `/admin`, during a short window. Themed browser instances are built with the
**instance generator** — a two-step, token-free flow (`make bundle` then `make instance`,
see `tools/README.md`):

- `make bundle OUT=~/dist` builds the **one** universal bundle the whole fleet loads. No
  key, no other variable: it is a copy of `extension/` and nothing is stamped into it.
- `make instance INSTANCE_ID=… BUNDLE_DIR=~/dist OUT=…` wraps that shared bundle in a
  per-instance `.app` (own profile, `.app`, launcher). It writes **no** extension copy and
  **no** `instance.json` — the launcher's `--load-extension` points at the shared bundle.
- **Cloning a `.app` does not add an instance** — a clone mints a new `install_uuid` in
  its fresh profile and is simply an un-enrolled install; it must enroll separately (the
  `install_uuid` guarantee, §6). Run the generator again with a new `instanceId`.

### Adding a browser (the enrollment procedure)

An operator adds an instance by pairing it during a short, deliberately-opened window:

1. **Open a window.** On `/admin`, open an enrollment window (`ENROLL_WINDOW_MIN`,
   default 10 min). `/admin` shows a short **enrollment code** for the open window.
2. **Take the code into the extension.** In the new instance's extension settings, enter
   the service address and the enrollment code, and submit — the extension sends its
   `install_uuid` + a freshly generated per-install secret and lands in the pending list.
   The settings page shows that install's own `install_uuid` prefix — note it, it is what
   you match against the console row in the next step.
3. **Approve it on `/admin`, while the window is still open.** Approve the pending request
   (give it its `instanceId`). Approval binds the secret's hash to that row; from then on
   the instance's `hello` (and its `/api/*` calls) authenticate by that secret.

> **Three independent time bounds. Two of them must hold at the moment you click Approve.**
>
> - The **window** bounds *approval*. `approve` re-reads the window and answers **409**
>   when it is closed — so a stolen hello cannot be approved at an arbitrary later time.
>   The check runs *before* the request lookup, so a closed window is a flat refusal and
>   never doubles as an oracle for which `install_uuid`s are pending.
> - The **code** bounds *who may file a request*: a request is only accepted while a
>   window is open and only with that window's own code, otherwise the service answers
>   `enroll_rejected{closed|bad_code}` and writes **no** row. This is what keeps unknown
>   clients out of the pending list.
> - **`ENROLL_REQUEST_TTL_MIN`** (60 min) bounds how long a filed request survives at all.
>   The service refuses to start unless `ENROLL_REQUEST_TTL_MIN >= ENROLL_WINDOW_MIN`, so
>   a request always outlasts the window it was filed in — otherwise one filed at the start
>   of a window would expire before that window closed, and `approve` would 404 on a row
>   still visible in front of you.
>
> **Practical consequence: a closed window does not just make you hurry — it makes
> `approve` fail.** On a 409 saying the window is closed, open a new one
> (`POST /admin/enroll/window`) and approve inside it. As long as the request is still
> within its TTL it is still in the list, so nothing has to happen at the browser: the new
> window's code exists to *file* requests, and this one is already filed.
>
> **Reject what you do not recognise, promptly.** How you tell your own request from
> someone else's is the **18-character `install_uuid` prefix** (`xxxxxxxx-xxxx-xxxx`):
> the extension's settings page and the `/admin` console print the same 18 characters of
> the same value, so you compare them literally, and the full uuid is on the console cell
> as a tooltip if you want to check every character. Nothing else in the row identifies
> anybody — every instance in the fleet shares one `chrome-extension://` origin by
> construction, and two browsers belonging to the same person carry the same suggested
> title. `POST /admin/enroll/reject` clears a row you did not expect; do not leave it
> sitting in the list on the theory that the window has closed, because the row outlives
> the window and the next window you open is an approval opportunity for it too.

### When you cannot approve (window closed, TTL expired, or you rejected the request)

- **Window closed, request still within its TTL** — the request survives, but `approve`
  answers **409**. Open a new window on `/admin` and approve inside it. The operator does
  **not** have to touch the browser: the request is already filed, and the new window's
  code only matters for filing.
- **Request past `ENROLL_REQUEST_TTL_MIN`** — it stops being returned by
  `GET /admin/enroll/requests` at read time and is physically swept within about one
  `TICK_MS` (60 s) after that; `approve` answers **404**. Re-submitting is then
  **mandatory, not an option** — there is nothing left to approve.
- **The extension re-files the request on its own, but only while it still holds a code.**
  A pending instance re-sends its `enroll_request` when it has never seen an
  `enroll_pending` at all, or when the last confirmation is more than 5 minutes old — so a
  request swept under TTL comes back by itself *if* the staged window code is still valid.
  It is not: a code belongs to one window, and the re-file lands after the window closed,
  which draws `enroll_rejected{closed}` and clears the code. That is the designed outcome —
  it converts a silent wait into a visible "re-stage the code" — but it does mean the
  instance stops on its own and waits for you.
- **The recovery is therefore at the browser, not at `/admin`:** open a **new** window on
  `/admin` (a new window always mints a **new** code — a previous window's code never
  carries over), then in the instance's extension settings enter that new code and press
  submit again. The instance reuses the **same** secret it already generated, so approving
  the new request enrolls the same credential; what expired was the request, not the
  secret. Approve it **promptly** this time — the request only outlives its window by
  `ENROLL_REQUEST_TTL_MIN`.
- **Tell "waiting" from "wrong code" without guessing:** the extension surfaces the last
  `enroll_rejected` reason in its settings UI. An instance stuck on `bad_code` or `closed`
  needs the procedure above; `capacity` means the pending list is full (clear it with
  `reject`); `secret_conflict` means a pending request already exists for that
  `install_uuid` carrying a **different** secret — reject the stale row on `/admin` (or let
  it age out) and the retry is accepted, because the approved credential is deliberately
  frozen at the value the request was created with. An instance showing no reason at all is
  genuinely waiting for you.

### Revoking a browser

On `/admin`, **revoke** the instance. Revocation flips its row out of `active` at once
(the resolution is never cached, §12), so the next `hello` and every `/api/*` call from
that secret are rejected immediately — a lost or decommissioned laptop is cut off without
touching any other instance. To bring it back, enroll it again (open a window, re-submit,
approve).

> **Release note — MAIN must re-enroll after migration.** The former shared /ext token
> is **gone**. After upgrading into the enrollment release, **every** existing instance —
> including `MAIN_INSTANCE_ID` — must re-enroll: migration step 2 runs
> `UPDATE instances SET status='revoked' WHERE secret_hash IS NULL`, and before enrollment
> *no* row had a `secret_hash`, so **every** row goes to `revoked`. Nothing authenticates
> until an operator opens a window and approves each one, and the stock branch stays
> disabled until MAIN has been re-approved. Approving MAIN reuses its existing id: the
> approve upsert reactivates an existing `revoked` row rather than creating a second one,
> so quarantines, exemptions and rules that reference `main` are not orphaned.
>
> **What the alert does.** `curator_main_instance_never_seen` reads **1** while MAIN is
> un-enrolled, so `curator-main-instance-never-seen` fires ~15 min after the upgrade and
> is your reminder; it clears once MAIN is approved and reconnects.
>
> That is true because the gauge keys on MAIN's **`status`** as well as its `last_seen_at`
> — and the distinction matters on an upgrade. The migration touches neither `last_seen_at`
> nor `connected`, so right after it a live install's MAIN row reads
> *(revoked, connected=1, last_seen_at=yesterday)*. Keyed on `last_seen_at` alone the gauge
> would report `0` — "MAIN is fine" — while every `hello` was being rejected, the stock
> branch was dead and the dashboard was green. If you ever see a green board and a disabled
> stock branch at the same time, that combination is the thing to distrust: cross-check
> `GET /admin/instances`, which lists every status.
>
> ⚠️ **The migration retires every in-flight relocation in the fleet — read this before
> upgrading a LIVE install.** A relocation takes two passes (§7): phase A opens the copy in
> the target instance, phase B closes the original in the source. The pass's retire step
> marks as `abandoned` every live `relocate` whose source **or** target is `revoked` — and
> immediately after the migration that is *every* instance, so the first real pass after
> the upgrade retires **all** in-flight relocations at once.
>
> Concretely: a tab that phase A had already copied never gets its phase B close. The copy
> stays in the target, **the original stays open in the source**, and the pair is
> journalled as `abandoned` — no second attempt, no reconciliation. After you re-enroll the
> fleet, the next pass sees two ordinary tabs and decides the source one from scratch, so
> it may be relocated again as a **fresh** `relocate` with a new `actions` row. Nothing is
> lost, but nothing is silently cleaned up either: **expect one round of visible duplicates
> across instances**, and expect the archive to show `abandoned` rows that are not a
> malfunction.
>
> To minimise it, let the in-flight phase B's complete FIRST — leave the curator
> **running, not paused**, for one full `PASS_INTERVAL` — and only *then* pause and pull
> the new image. The order is the whole point, and reversing it produces the opposite of
> what it promises: a live pause stops the **pass**, and phase B runs inside a pass
> (`run_pass` returns `{"status": "paused"}` up front and `_acquire_unless_paused` does
> not even take the lease), so a curator paused first closes **zero** phase B's — the
> maximum number of half-done relocations instead of the minimum. Pause only once the
> interval has elapsed, so that no fresh pass starts in the middle of the `docker pull`.
> Relocations that finished are not in-flight and are unaffected. On the current state —
> nothing deployed — this is hypothetical, and it is written down because this release
> note addresses upgrades.

Its operational acceptance checks (new instance enrolls & connects; two instances share
one bundle; clone re-enrolls rather than taking over) are a manual list in
`tools/README.md`.

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
   header = "Authorization: Bearer $ADMIN_TOKEN"
   EOF
   ```

   The token goes in a `-K` config read from **stdin**, never in `curl -H …`: an
   argument is visible in `ps` to every user on the box for the lifetime of the request.
   `$ADMIN_TOKEN` is expanded by the shell into the heredoc, so it never reaches `argv`
   and nothing is written to disk.

   A pause blocks mutating passes but **not** `dry_run` (§7), which is exactly the
   combination this step needs: nothing acts, and you can still look.

6. **Read the plan, then release deliberately.**

   ```bash
   # What WOULD it do? (works under the pause)
   curl -sS -K - -X POST -d '{"dry_run":true}' https://<host>/api/run_pass <<EOF
   header = "Authorization: Bearer $ADMIN_TOKEN"
   EOF
   ```

   If the plan looks wrong, keep the pause and investigate — nothing is acting.

   Only when it looks right, lift the pause. ⚠️ `DELETE /api/pause` **runs a pass
   immediately** (§7): it is a deliberate action, not a cleanup step. What that pass
   does depends on whether the break was detected — and the response tells you, in
   `pass.status`:

   ```bash
   curl -sS -K - -X DELETE https://<host>/api/pause <<EOF
   header = "Authorization: Bearer $ADMIN_TOKEN"
   EOF
   ```

   - `pass.status = "resume_pending"` — the break WAS detected. Nothing acted; the plan
     is armed and waits for your click. Release it:

     ```bash
     curl -sS -K - -X POST -d '{"confirm_pending":true}' https://<host>/api/run_pass <<EOF
     header = "Authorization: Bearer $ADMIN_TOKEN"
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
header = "Authorization: Bearer $ADMIN_TOKEN"
EOF

# 2. Read what it wants to do (dry_run is not blocked by the pause).
curl -sS -K - -X POST -d '{"dry_run":true}' https://<host>/api/run_pass <<EOF
header = "Authorization: Bearer $ADMIN_TOKEN"
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
