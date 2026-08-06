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
  - `/app/data/backups` — the nightly `VACUUM INTO` copies (`BACKUP_DIR`).

  The entrypoint runs as root before dropping to `app` via gosu: it `chown -R`s
  `/app/data` and `mkdir -p` + `chown`s an absolute `BACKUP_DIR`.

  There is no continuity marker anymore. Restore-from-backup **detection** (the old
  external-marker fingerprint) was replaced by the per-pass action threshold
  (`MAX_ACTIONS_PER_PASS`, §7): whatever a restore breaks shows up as an oversized
  plan on the next pass, which is deferred behind one confirming click on the
  startpage — the same brake that catches every other mass-action cause.

### ⚠️ What one volume costs — read this before you plan your backups

- **Backups share free space with the DB.** A DB that grows until the volume is full
  also takes the nightly copy down with it — `VACUUM INTO` needs room for a full copy
  beside the original. `curator-backup-stale` (`deploy/alerts.yml`,
  `curator_backup_age_seconds > 26h`) is the alert that catches it; size the volume for
  the DB **plus** the retained copies, not for the DB alone.

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
> **running, not stopped**, for one full `PASS_INTERVAL` — and only *then* stop it and
> pull the new image. The order is the whole point, and reversing it produces the
> opposite of what it promises: the stop halts the **pass**, and phase B runs inside a
> pass (`run_pass` returns `{"status": "stopped"}` up front and
> `_acquire_unless_stopped` does not even take the lease), so a curator stopped first
> closes **zero** phase B's — the maximum number of half-done relocations instead of the
> minimum. Stop only once the interval has elapsed, so that no fresh pass starts in the
> middle of the `docker pull`.
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
already re-opened.

**How the service protects itself.** The old external continuity marker (and its
fingerprint machinery) is gone. The protection is now the per-pass action threshold
(`MAX_ACTIONS_PER_PASS`, §7): a pass whose plan wants more than the threshold's worth
of relocations + closes does not execute it — the plan is armed as `resume_pending`
and waits for one confirming click, and every subsequent pass recomputes and refreshes
that plan. A restore whose stale plan is LARGE is therefore caught by the same brake
that catches every other mass-action cause. A restore whose stale plan is small acts
without a click — by design: a handful of actions is cheap to undo from the archive,
and the threshold, unlike the marker, needs no operator ritual to stay armed.

The **stop** verb (`POST /api/pause`, indefinite; `DELETE /api/pause` starts again and
runs a confirming pass at once) is your manual hold during the swap.

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

4. **Start the service, then immediately stop the curator.** The periodic driver
   sleeps one `PASS_INTERVAL` (default 5 min) *before* its first pass, and that sleep
   is your entire window — the stop must be set inside it. It cannot be set earlier:
   the stop flag lives in the DB you just replaced.

   ```bash
   docker compose up -d curator
   # …then, without waiting:
   curl -sS -K - -X POST https://<host>/api/pause <<EOF
   header = "Authorization: Bearer $ADMIN_TOKEN"
   EOF
   ```

   The token goes in a `-K` config read from **stdin**, never in `curl -H …`: an
   argument is visible in `ps` to every user on the box for the lifetime of the request.
   `$ADMIN_TOKEN` is expanded by the shell into the heredoc, so it never reaches `argv`
   and nothing is written to disk.

   The stop blocks mutating passes but **not** `dry_run` (§7), which is exactly the
   combination this step needs: nothing acts, and you can still look. It is
   **indefinite** — nothing expires underneath you while you read.

5. **Read the plan, then release deliberately.**

   ```bash
   # What WOULD it do? (works while stopped)
   curl -sS -K - -X POST -d '{"dry_run":true}' https://<host>/api/run_pass <<EOF
   header = "Authorization: Bearer $ADMIN_TOKEN"
   EOF
   ```

   The plan carries `total` and `threshold`, so the dry run already tells you which
   way the release will go. If the plan looks wrong, keep the stop on and investigate
   — nothing is acting.

   Only when it looks right, start the curator. ⚠️ `DELETE /api/pause` **runs a
   confirming pass immediately** (§7): it is a deliberate action, not a cleanup step —
   the pass it triggers executes the plan you just read (recomputed at that moment),
   whatever its size. If you would rather keep the threshold's second opinion, do NOT
   use the start verb to release a plan you have not read.

   ```bash
   curl -sS -K - -X DELETE https://<host>/api/pause <<EOF
   header = "Authorization: Bearer $ADMIN_TOKEN"
   EOF
   ```

   After the start, an over-threshold plan that arrives on a LATER pass still defers
   (`resume_pending` in `GET /api/state`, `curator_resume_pending` on `/metrics`, the
   startpage status bar) and waits for `POST /api/run_pass {"confirm_pending": true}`.

Treat anything the curator did between the restore and the stop as suspect: closed
tabs are not recoverable from the plan, only from the actions archive (`restore`).
