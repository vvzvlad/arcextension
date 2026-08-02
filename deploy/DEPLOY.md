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
  `EXT_ALLOWED_ORIGINS=chrome-extension://<id1>,chrome-extension://<id2>`.
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
