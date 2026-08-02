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

// Save the rule (§10). The user has seen the preview, so we confirm the impact.
export async function saveRule(fetchFn, base, token, rule) {
  const resp = await fetchFn(base + "/api/rules", {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({
      pattern: rule.pattern,
      instance_id: rule.instance_id,
      confirm_impact: true,
    }),
  });
  let body = null;
  try {
    body = await resp.json();
  } catch {
    body = null;
  }
  return { status: resp.status, body };
}

// Read instance.json (authoritative config, §6) via the extension URL.
export async function loadConfig(fetchFn, getURL) {
  const resp = await fetchFn(getURL("instance.json"));
  return await resp.json();
}

export async function getActiveTab(chromeApi) {
  const tabs = await chromeApi.tabs.query({ active: true, currentWindow: true });
  return tabs && tabs[0];
}

function summarize(preview) {
  const r = preview.relocations || 0;
  const c = preview.closures || 0;
  const notCounted = (preview.instances || []).filter((i) => i && !i.counted);
  let text = `Would relocate ${r} tab(s) and close ${c} tab(s) on the next pass.`;
  if (notCounted.length) {
    text +=
      " Not counted (stale/disconnected): " +
      notCounted.map((i) => i.id).join(", ") +
      ".";
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
    const config = await loadConfig(fetchFn, chromeApi.runtime.getURL);
    base = httpBaseFromServiceUrl(config.serviceUrl);
    token = config.token;
    const tab = await getActiveTab(chromeApi);
    if (!tab || !tab.url) throw new Error("no active tab URL");
    rule = buildRule(tab.url, config.instanceId);
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
    if (els.impact) els.impact.textContent = "Preview unavailable: " + (e && e.message);
  }

  if (els.save) {
    els.save.disabled = false;
    els.save.addEventListener("click", async () => {
      els.save.disabled = true;
      setStatus("Saving…");
      try {
        const { status } = await saveRule(fetchFn, base, token, rule);
        setStatus(status < 300 ? "Rule saved." : "Save failed (HTTP " + status + ").");
      } catch (e) {
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
