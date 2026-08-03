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

### Which revision is running — ask `/healthz` FIRST

Because the deploy is "watchtower pulls `:develop` whenever it likes", the tag names
nothing: `:develop` today and `:develop` yesterday are different images. So when a
button does nothing, there are **two** candidate causes that look identical in the
logs — *the code is broken* and *the new code is not deployed yet* — and separating
them is step zero of every diagnosis, before reading a single log line:

```bash
curl -s https://<host>/healthz
# {"status":"ok","revision":"0f2c9a1b3d4e5f60718293a4b5c6d7e8f9012345"}

git rev-parse HEAD        # ...the same sha? then you are looking at your code.
```

- **No token needed** — `/healthz` is the one unauthenticated route (§4 below), which
  is the point: a diagnostic that first asks for a credential is one that does not get
  used at 2am. Nothing but the build id is disclosed.
- **The same string is in the `/admin` console**, top right, rendered into the page
  itself (no API call) — so it is readable even when the console's own JSON calls are
  failing.
- **`revision: "unknown"`** means the image was built without the stamp (a hand-rolled
  `docker build` with no `--build-arg`, or a local `make run`). It is not an error, but
  from a ghcr image it means CI did not build it — treat the container as unidentified.
- **Without HTTP at all** (the container will not start): the same value is the standard
  OCI label — `docker inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' curator`.

The value is baked at IMAGE BUILD time (`Dockerfile`: `ARG BUILD_REVISION` → `ENV`, fed
by CI from `github.sha`). **Do not set `BUILD_REVISION` in compose or `.env`** — there is
nothing to configure, and a hand-set value makes the service report a revision it is not
running, which is strictly worse than reporting none.

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
- **One volume**, `curator` → `/app/data`, holding **all** mutable state (the project
  convention is "all mutable state goes under `data/`"):
  - `/app/data/curator.db` (+ WAL) — the DB;
  - `/app/data/backups` — the nightly `VACUUM INTO` copies (`BACKUP_DIR`);
  - `/app/data/restore/continuity-marker` — the continuity marker
    (`RESTORE_MARKER_PATH`, §8).

  The entrypoint runs as root before dropping to `app` via gosu: it `chown -R`s
  `/app/data`, `mkdir -p` + `chown`s an absolute `BACKUP_DIR`, and **creates the
  continuity marker with a fresh uuid if it is not there** (never rewriting an existing
  one — §8). The marker and its directory are left **root-owned** (`644` / `755`): the
  service must be able to read the marker and must not be able to rewrite it.

### ⚠️ What one volume costs — read this before you plan your backups

Two prices, both accepted deliberately; neither is an oversight.

- **A rollback of the VOLUME AS A WHOLE is undetectable.** Three separate volumes used
  to protect against exactly that: restore a storage/ZFS/`docker volume` snapshot of the
  DB volume and the marker, living elsewhere, stayed put — so the curator saw a changed
  fingerprint and stopped for confirmation. With one volume the snapshot brings the
  marker back too, at its old value, and the first pass after such a rollback looks
  perfectly continuous: it acts on yesterday's rules and stale `relocate` rows with **no
  `dry_run` and no click**. The reason this is acceptable and not a hole: the documented
  restore procedure below puts back a **file** (`curator.db`), not the volume — under it
  the marker is untouched by the copy and step 4 changes it, so the detector works
  exactly as designed. **If you restore volume snapshots instead, the detector does not
  cover you** — pause the service by hand first (§8, "If you forget step 4"), or keep the
  marker on a mount of its own and point `RESTORE_MARKER_PATH` at it.
- **Backups share free space with the DB.** A DB that grows until the volume is full
  also takes the nightly copy down with it — `VACUUM INTO` needs room for a full copy
  beside the original. `curator-backup-stale` (`deploy/alerts.yml`,
  `curator_backup_age_seconds > 26h`) is the alert that catches it; size the volume for
  the DB **plus** the retained copies, not for the DB alone.

There is **no manual marker step at install** any more — the entrypoint creates the file.
The **restore** step (§8, step 4) stays manual, and by design: the service cannot know it
was rolled back.

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
authenticates with a **per-install secret** it generates itself; the operator's whole part
is opening a short window on `/admin` and handing over its code. Themed browser instances are built with the
**instance generator** — a two-step, token-free flow (`make bundle` then `make instance`,
see `tools/README.md`):

- `make bundle OUT=dist` builds the **one** universal bundle the whole fleet loads. No
  key, no other variable: it is a copy of `extension/` and nothing is stamped into it.
- `make instance INSTANCE_ID=… BUNDLE_DIR=dist OUT=…` wraps that shared bundle in a
  per-instance `.app` (own profile, `.app`, launcher). It writes **no** extension copy and
  **no** `instance.json` — the launcher's `--load-extension` points at the shared bundle.
- **Cloning a `.app` does not add an instance** — a clone mints a new `install_uuid` in
  its fresh profile and is simply an un-enrolled install; it must enroll separately (the
  `install_uuid` guarantee, §6). Run the generator again with a new `instanceId`.

### Adding a browser (the enrollment procedure)

An operator adds an instance by opening a short window and handing over its code. **The
open window is the whole permission** — there is no approval step, and nothing to come
back to the console for.

1. **Name the browser.** In the new instance's extension settings, enter the service
   address and the **browser name**. That name becomes the instance's `instance_id`
   verbatim, so it must be 1-64 characters of `A-Za-z0-9._-` (no spaces) and must not
   already belong to a live instance. The settings page refuses anything else before it
   sends, and tells you what to type instead.
2. **Open a window.** On `/admin`, open an enrollment window (`ENROLL_WINDOW_MIN`,
   default 10 min). `/admin` shows a short **enrollment code** for the open window.
3. **Take the code into the extension and submit.** The extension sends its
   `install_uuid`, a freshly generated per-install secret and the name from step 1. If the
   window is open and the code is right, the instance is created **active** on the spot and
   the extension's status line flips from «не зарегистрирован» to «активен». From then on
   its `hello` (and its `/api/*` calls) authenticate by that secret.
4. **Close the window** (`DELETE /admin/enroll/window`, or the button) when you are done.
   Leaving it open leaves the door open — `curator-enroll-window-held-open` fires after
   65 minutes precisely because of that.

> **Why there is no Approve button anymore.** The window with its one-shot code already
> answers "who may connect"; approval existed only to assign the `instance_id`, and the
> browser now brings it. The single real objection — two browsers claiming one name — is
> refused loudly (`id_taken`) instead of costing a manual step on every ordinary addition.
>
> **What you give up, and where to look instead.** A REFUSED attempt no longer appears in
> any console list. Its reason is shown in the settings page of the browser that was
> refused — that is the place to look — and the fact is counted in
> `curator_auth_rejections_total{reason="enroll_*"}`. The name collision has its own alert,
> `curator-enroll-id-taken`, with a threshold of 0 (deploy/alerts.yml), so the one refusal
> that means "a browser cannot join and nobody would otherwise notice" still pages.

### When enrolment is refused

The extension shows the reason in its settings; each maps to one action:

- **`имя уже занято` (`id_taken`)** — a LIVE instance already has that name. Pick another
  name in the extension settings, or revoke the old instance on `/admin` first if it is
  the one being replaced. A **revoked** name is free to reuse: re-enrolling under it
  reactivates that row, which is exactly how a revoked MAIN comes back.
- **`имя не подходит` (`bad_id`)** — the name is outside `A-Za-z0-9._-` or longer than 64.
  Fix it in the settings. (The extension normally refuses to send such a name at all, so
  seeing this means an old build.)
- **`неверный код регистрации` (`bad_code`)** — the code was wrong or belongs to an older
  window. Open a new window on `/admin` (each open mints a **new** code) and type that one.
- **`окно регистрации закрыто` (`closed`)** — same fix: open a window, then submit.
- **`версия протокола не совпала` (`protocol`)** — the extension and the service disagree
  on `PROTOCOL_VERSION`. Update one of them; the extension keeps the staged code and
  retries by itself.

A refusal for a wrong code, a closed window or a bad name is **terminal for that attempt**:
the extension clears the staged code and stops retrying until a human acts. That is
deliberate — it stops a browser from hammering a service that can only refuse it — but it
means recovery always starts at the browser, not at `/admin`.

### Revoking a browser

On `/admin`, **revoke** the instance. Revocation flips its row out of `active` at once
(the resolution is never cached, §12), so the next `hello` and every `/api/*` call from
that secret are rejected immediately — a lost or decommissioned laptop is cut off without
touching any other instance. To bring it back, enroll it again (open a window, type the
code in the browser) — under the SAME name if you want the same identity back: a revoked
row is reactivated rather than refused.

> **Release note — MAIN must re-enroll after migration.** The former shared /ext token
> is **gone**. After upgrading into the enrollment release, **every** existing instance —
> including `MAIN_INSTANCE_ID` — must re-enroll: migration step 2 runs
> `UPDATE instances SET status='revoked' WHERE secret_hash IS NULL`, and before enrollment
> *no* row had a `secret_hash`, so **every** row goes to `revoked`. Nothing authenticates
> until each one re-enrols through an open window, and the stock branch stays disabled
> until MAIN has. Re-enrolling MAIN under its EXISTING id is what restores it: the enrol
> upsert reactivates a `revoked` row rather than creating a second one, so quarantines,
> exemptions and rules that reference `main` are not orphaned. Type `main` (or whatever
> `MAIN_INSTANCE_ID` is set to) as the browser name in that instance's extension settings.
>
> **What the alert does.** `curator_main_instance_never_seen` reads **1** while MAIN is
> un-enrolled, so `curator-main-instance-never-seen` fires ~15 min after the upgrade and
> is your reminder; it clears once MAIN has re-enrolled and reconnected.
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

Hence `RESTORE_MARKER_PATH` (`/app/data/restore/continuity-marker`): a file that is
**not the DB and not part of a backup copy**. The service hashes its contents into the
fingerprint, so writing a new value is what makes the restore visible. Keep the value
inside the DB and it rolls back with the DB; keep it inside `BACKUP_DIR` and it travels
inside the copy. Both silently restore the old value and the break goes undetected.

It sits on the **same volume** as the DB — one volume holds all mutable state (§5) — and
that is a deliberate trade: the separation this detector needs is at **file** level and
the documented procedure below restores a **file**, so the marker survives it. A rollback
of the whole volume defeats the detector; §5 spells out that cost and what to do if that
is how you restore.

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

The marker and its directory are left **root-owned** (`644` / `755`) by the entrypoint on
purpose: the service reads the marker as uid 1000 and must **not** be able to rewrite it,
because a service able to "heal" the value could silently disable the very detection this
exists for. (That used to be a `:ro` mount of a dedicated volume; with one volume,
ownership is what carries the guarantee — and the directory matters as much as the file,
since write access to a directory is enough to unlink and recreate what is inside it.)
Writing stays an **operator** action, from a root shell in the container — which
`docker compose run --entrypoint sh curator` gives you, since the image has no `USER`
directive.

### Writing the marker (during a restore)

**At install there is nothing to do here.** The entrypoint creates the marker on the
first start if the file is absent: a fresh uuid, mode `644`, owner `root`. It **never**
rewrites an existing one.

**What stays manual — and why it cannot be automated.** During a restore the operator
must write a **NEW** value into the marker (step 4 of the procedure below). No amount of
entrypoint cleverness can do this: the service cannot know it was rolled back — that is
precisely why the marker is external. A service that rewrote the marker on every start
would erase the only evidence of its own rollback and the detector would report continuity
after every restore. So: **create automatically, change by hand.** Do not "finish the
automation" here.

Write a fresh uuid from a root shell in the service container:

```bash
docker compose run --rm --entrypoint sh curator \
  -c 'cat /proc/sys/kernel/random/uuid > /app/data/restore/continuity-marker &&
      chmod 644 /app/data/restore/continuity-marker'
```

⚠️ **The `chmod 644` is not cosmetic — do not drop it.** You write this file as **root**,
but the service reads it as **uid 1000**. Under a `umask 077` (common on hardened
hosts) a *newly created* file lands `600 root:root`, which uid 1000 can never read —
and an unreadable marker means restore detection is **silently off**, because the
`unreadable` state is deliberately not comparable (see the marker states above). The
redirect above truncates an existing `644` file rather than creating one, so the mode
normally survives; the `chmod` is there for the case where it does not.

Verify it landed, and check the permissions while you are there:

```bash
docker compose run --rm --entrypoint sh curator \
  -c 'ls -l /app/data/restore/continuity-marker; cat /app/data/restore/continuity-marker'
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

**On a NEW install there is no timing to get right any more.** The marker exists before
the application process does: the entrypoint creates it as root, in the same startup that
chowns `/app/data`, so the very first pass already sees a digest instead of `missing`. The
old failure mode — a pass records "the marker is `missing`", the file appears later, and
that appearance costs a dry_run + confirmation — is gone with the manual step.

> Note this does **not** buy you a click-free first run. A brand-new install whose
> browser has already connected takes its *own* first-run break by §7 (no stored
> fingerprint + a populated DB), so expect one `dry_run` + confirmation regardless.
> Having the marker from the start avoids a **second**, avoidable one later.
>
> **Deleting the marker is still a thing that can happen**, so the `missing` state has
> not gone away and the code still handles it (`clock.MARKER_MISSING`). If someone
> removes the file while the service runs, the next pass records `missing`; the next
> container start recreates it with a fresh uuid, and that appearance is a break like any
> other — one `dry_run`, one click.

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

2. **Pick the copy.** Backups are `curator-<timestamp>.db` under `/app/data/backups`
   (nightly `VACUUM INTO`, newest last):

   ```bash
   docker compose run --rm --entrypoint sh curator -c 'ls -la /app/data/backups'
   ```

3. **Put the copy in place** as `/app/data/curator.db`. Remove the stale WAL/SHM
   sidecars — a `VACUUM INTO` copy is self-contained, and leaving an old `-wal` next
   to a *different* database is how a restore corrupts itself:

   ```bash
   docker compose run --rm --entrypoint sh curator -c '
     cp /app/data/backups/curator-<timestamp>.db /app/data/curator.db &&
     rm -f /app/data/curator.db-wal /app/data/curator.db-shm &&
     chown app:app /app/data/curator.db'
   ```

   ⚠️ Copy the **file**. Do not restore the volume (or a storage snapshot of it) —
   that rolls the marker back together with the DB and the break becomes undetectable
   (§5, "What one volume costs").

4. **Write a NEW value into the marker** — the step that makes the restore
   *detectable*, and the one step the service cannot do for you (it does not know it was
   rolled back). Use the command in **«Writing the marker»** above. Only the *change*
   matters, so a fresh uuid is enough. Do this on **every** restore, including a
   re-restore of the same copy. The entrypoint will **not** do it: it only creates a
   marker that is absent, and after step 3 the marker is still there with its old value.

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
