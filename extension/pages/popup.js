// "Rule from this tab" popup (§8, §10). On any tab it offers a ready rule
// `<origin>/*` → <thisInstance>, shows the server-side impact preview, and saves.
//
// The stored `pattern` is a HOST pattern (the matcher grammar is hostPattern[:port];
// a full URL is rejected 422, §8) — the `/*` is how the human reads it, and the
// matcher ignores the path anyway. The target instance is THIS copy's instanceId,
// read from instance.json exactly like the service worker does (§6). Preview and
// save are the SERVER's matcher (§8: the browser never duplicates it). The script is
// external because the extension_pages CSP forbids inline scripts.
//
// Pure functions are exported for unit tests; DOM wiring runs only in a document.

// Derive the HTTP base from the wss service URL (instance.json carries the socket
// URL). ws→http, wss→https; trailing slashes trimmed.
export function httpBaseFromServiceUrl(serviceUrl) {
  const base = String(serviceUrl || "").replace(/\/+$/, "");
  if (base.startsWith("wss://")) return "https://" + base.slice("wss://".length);
  if (base.startsWith("ws://")) return "http://" + base.slice("ws://".length);
  return base; // already http(s) or unknown scheme — leave as-is
}

// Build the candidate rule for a tab URL. `pattern` is the host (+ explicit non-
// default port); `label` is the human-facing `<origin>/*`.
export function buildRule(tabUrl, instanceId) {
  const u = new URL(tabUrl);
  if (u.protocol !== "http:" && u.protocol !== "https:") {
    throw new Error("only http/https tabs can become a rule");
  }
  let pattern = u.hostname;
  if (u.port) pattern += ":" + u.port; // URL.port is '' for the default port
  return { pattern, instance_id: instanceId, label: u.origin + "/*" };
}

function authHeaders(token) {
  return { "Content-Type": "application/json", Authorization: "Bearer " + token };
}

// POST the candidate to the server-side preview (§8). Returns the parsed payload.
export async function requestPreview(fetchFn, base, token, rule) {
  const resp = await fetchFn(base + "/api/rules/preview", {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({ pattern: rule.pattern, instance_id: rule.instance_id }),
  });
  return await resp.json();
}

// Save the rule (§10). `confirmImpact` is sent ONLY when the human has actually been
// shown the impact and acted a SECOND time — never by default. The §8 confirm gate
// exists so a rule that would relocate/close tabs cannot be committed blind; a client
// that always sends `confirm_impact:true` echo-confirms the server's own question and
// deletes the gate. The startpage does the same two-step (App.vue onSave/onDelete).
export async function saveRule(fetchFn, base, token, rule, { confirmImpact = false } = {}) {
  const payload = { pattern: rule.pattern, instance_id: rule.instance_id };
  if (confirmImpact) payload.confirm_impact = true;
  const resp = await fetchFn(base + "/api/rules", {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(payload),
  });
  let body = null;
  try {
    body = await resp.json();
  } catch {
    body = null;
  }
  return { status: resp.status, body };
}

// Read instance.json via the extension URL. With enrollment (§7) this is only a
// FALLBACK bootstrap: the credential source moved to the SW (address + raw secret +
// server-assigned id). Kept for a bundle that still ships a serviceUrl.
export async function loadConfig(fetchFn, getURL) {
  const resp = await fetchFn(getURL("instance.json"));
  return await resp.json();
}

// Resolve the /api base + Bearer + target instanceId (§7). PREFER the SW credential
// (the RAW instance secret is the /api Bearer — slice C / option A, the server hashes it —
// and the id is server-assigned, learned from a successful hello); fall back to
// instance.json when the SW channel is unavailable or has nothing yet (e.g. before
// enrollment).
export async function loadPopupContext(chromeApi, fetchFn) {
  try {
    if (chromeApi.runtime && chromeApi.runtime.sendMessage) {
      const cred = await chromeApi.runtime.sendMessage({ type: "get_credential" });
      const ident = await chromeApi.runtime.sendMessage({ type: "get_identity" });
      if (cred && cred.serviceUrl && cred.secret && ident && ident.instanceId) {
        return {
          base: httpBaseFromServiceUrl(cred.serviceUrl),
          token: cred.secret,
          instanceId: ident.instanceId,
        };
      }
    }
  } catch {
    // fall through to the instance.json bootstrap
  }
  const config = await loadConfig(fetchFn, chromeApi.runtime.getURL);
  return {
    base: httpBaseFromServiceUrl(config.serviceUrl),
    token: config.token,
    instanceId: config.instanceId,
  };
}

export async function getActiveTab(chromeApi) {
  const tabs = await chromeApi.tabs.query({ active: true, currentWindow: true });
  return tabs && tabs[0];
}

export function summarize(preview) {
  const p = preview || {};
  const r = p.relocations || 0;
  const c = p.closures || 0;
  // A 409 confirm body names the uncountable instances in `not_counted`; a plain
  // preview carries them as `instances[].counted === false`. Accept both.
  const notCounted = Array.isArray(p.not_counted)
    ? p.not_counted.map((i) => (i && i.id) || i)
    : (p.instances || []).filter((i) => i && !i.counted).map((i) => i.id);
  let text = `Would relocate ${r} tab(s) and close ${c} tab(s) on the next pass.`;
  if (notCounted.length) {
    text += " Not counted (stale/disconnected): " + notCounted.join(", ") + ".";
  }
  return text;
}

// --- DOM wiring (browser only) ---------------------------------------------
export async function init(doc, chromeApi, fetchFn) {
  const els = {
    origin: doc.getElementById("origin"),
    pattern: doc.getElementById("pattern"),
    target: doc.getElementById("target"),
    impact: doc.getElementById("impact"),
    save: doc.getElementById("save"),
    status: doc.getElementById("status"),
  };
  const setStatus = (t) => {
    if (els.status) els.status.textContent = t;
  };

  let base, token, rule;
  try {
    const ctx = await loadPopupContext(chromeApi, fetchFn);
    base = ctx.base;
    token = ctx.token;
    const tab = await getActiveTab(chromeApi);
    if (!tab || !tab.url) throw new Error("no active tab URL");
    rule = buildRule(tab.url, ctx.instanceId);
  } catch (e) {
    setStatus("Cannot build a rule for this tab: " + (e && e.message));
    if (els.save) els.save.disabled = true;
    return;
  }

  if (els.origin) els.origin.textContent = rule.label;
  if (els.pattern) els.pattern.textContent = rule.pattern;
  if (els.target) els.target.textContent = rule.instance_id;

  try {
    const preview = await requestPreview(fetchFn, base, token, rule);
    if (els.impact) els.impact.textContent = summarize(preview);
  } catch (e) {
    // A failed preview must NOT become a confirmed impact: the human has been shown
    // nothing, so the first Save goes WITHOUT confirm_impact and the server's own 409
    // asks the question (below). This is the case §8's gate is for.
    if (els.impact) els.impact.textContent = "Preview unavailable: " + (e && e.message);
  }

  if (els.save) {
    // The §8 confirm gate is TWO actions, never one (same contract as the startpage,
    // App.vue onSave): the first click saves WITHOUT confirm_impact; a 409 shows the
    // impact the server computed and arms the button; only the second, deliberate
    // click sends confirm_impact:true. Sending it up front would echo-confirm the
    // server's question with the human never having seen an impact.
    let confirmArmed = false;
    els.save.disabled = false;
    els.save.addEventListener("click", async () => {
      els.save.disabled = true;
      setStatus(confirmArmed ? "Confirming…" : "Saving…");
      try {
        const { status, body } = await saveRule(fetchFn, base, token, rule, {
          confirmImpact: confirmArmed,
        });
        if (status === 409) {
          // ARM ONLY WITH A BODY WE COULD READ. An unparseable 409 would render
          // "relocate 0, close 0" — zeros where the server refused precisely BECAUSE
          // the impact is non-zero — and arm a confirm for an impact nobody was shown.
          // That is the same blind confirmation the two-step exists to prevent.
          if (!body || typeof body !== "object") {
            confirmArmed = false;
            els.save.disabled = false;
            setStatus(
              "The server needs confirmation but its answer could not be read. " +
                "Reopen the popup to see the impact.",
            );
            return;
          }
          confirmArmed = true;
          if (els.impact) els.impact.textContent = summarize(body);
          els.save.textContent = "Confirm and save";
          els.save.disabled = false;
          setStatus("This rule has an impact — review it above, then click again to confirm.");
          return;
        }
        confirmArmed = false;
        if (status < 300) {
          setStatus("Rule saved.");
          return; // saved: leaving the button disabled prevents a duplicate rule
        }
        // ANY other non-2xx (422 bad pattern, 423 paused, 5xx) is RETRYABLE — a paused
        // curator resumes, a typo gets fixed. Leaving the button disabled meant the
        // human had to close and reopen the popup to try again.
        els.save.textContent = "Save rule";
        els.save.disabled = false;
        setStatus("Save failed (HTTP " + status + ").");
      } catch (e) {
        // Disarm the LABEL together with the flag: leaving "Confirm and save" on a
        // button that will now send an unconfirmed probe makes the button lie about
        // what the next click does.
        confirmArmed = false;
        els.save.textContent = "Save rule";
        setStatus("Save failed: " + (e && e.message));
        els.save.disabled = false;
      }
    });
  }
}

// Auto-run in the extension page; never at import under vitest (node, no document).
if (typeof document !== "undefined" && document.getElementById("save")) {
  init(document, chrome, fetch).catch((e) => console.error("[popup]", e));
}
