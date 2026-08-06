// "Rule from this tab" popup (§8, §10). On any tab it offers a rule for the tab's
// origin, lets the human pick BOTH halves of that rule — the host pattern and the
// TARGET browser — shows the server-side impact preview for whatever is currently in
// the form, and saves it through the §8 confirm gate.
//
// The stored `pattern` is a HOST pattern (the matcher grammar is hostPattern[:port];
// a full URL is rejected 422, §8) — the tab's `<origin>/*` is only how the human reads
// the tab it came from, and the matcher ignores the path anyway. The pattern field is
// prefilled with the tab's host (+ explicit non-default port) and editable, because the
// scenario the popup is FOR is "I am looking at this site and deciding where it lives":
// hard-wiring the bare host meant filing a rule you then had to go and edit.
//
// The target defaults to THIS copy's SERVER-assigned instanceId (§7, from the service
// worker) — the sensible default, since you usually curate the browser you are in — and
// the other choices come from GET /api/state, the same mirror the startpage renders.
//
// Preview and save are the SERVER's matcher (§8: the browser never duplicates it —
// a second implementation "would lie on IDN and IPv6"), and so is pattern VALIDATION:
// a bad pattern comes back as a 422 whose text is shown verbatim. The script is
// external because the extension_pages CSP forbids inline scripts, and every server
// string reaches the DOM through textContent only.
//
// Pure functions are exported for unit tests; DOM wiring runs only in a document.

// Debounce for the pattern field: a preview is a WHOLE-PASS simulation on the server
// (it actively refreshes every instance's snapshot, src/api/rules.py), so one request
// per keystroke would hammer the fleet. Picking in the target <select> is a single
// deliberate act and recomputes immediately.
export const PATTERN_DEBOUNCE_MS = 250;

const SAVE_LABEL = "Save rule";
const CONFIRM_LABEL = "Confirm and save";
// While a preview is in flight (or the shown numbers belong to a rule the human has
// already edited away from) the button must not imply the impact next to it is the
// impact of what it would save.
const SAVE_PENDING_LABEL = "Save rule (computing impact…)";
const PENDING_IMPACT = "Computing the impact of this rule…";

// Derive the HTTP base from the wss service URL the SW resolved. ws→http, wss→https;
// trailing slashes trimmed. (`ws://` survives only for a loopback dev address — see
// src/service-address.js, which is what admits it in the first place.)
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

// Read a response body ONCE and parse it locally. The answers this popup must render
// arrive in two different media types: the §8 confirm 409 is JSON (the impact payload)
// while 422 / 423 / 5xx are Starlette PlainTextResponses carrying the human-readable
// reason (src/app.py `_http_exception`). A real Response body can be consumed only
// once, so reading the text and parsing it here is what gets both — and what keeps an
// unparseable body available verbatim instead of turning it into `null`.
export async function readResponse(resp) {
  let text = "";
  try {
    text = await resp.text();
  } catch {
    return { body: null, text: "" };
  }
  try {
    const parsed = JSON.parse(text);
    return { body: parsed && typeof parsed === "object" ? parsed : null, text };
  } catch {
    return { body: null, text };
  }
}

function statusOf(resp) {
  return typeof resp.status === "number" ? resp.status : 200;
}

// The most useful sentence the server gave us about a failure. A dict-detail body
// (`{error, message}`) and a plain-text `detail` are both rendered by the service, so
// take whichever is there and fall back to the bare status.
export function serverMessage(status, body, text) {
  if (body) {
    for (const key of ["detail", "message", "error"]) {
      if (typeof body[key] === "string" && body[key]) return body[key];
    }
  }
  const trimmed = String(text || "").trim();
  if (trimmed) return trimmed.length > 400 ? trimmed.slice(0, 400) + "…" : trimmed;
  return "HTTP " + status;
}

// --- the target browser list ------------------------------------------------
// GET /api/state is the fleet mirror the startpage already renders (§10); its
// `instances` array is where the pickable browsers come from.
export async function fetchInstances(fetchFn, base, token) {
  const resp = await fetchFn(base + "/api/state", {
    headers: { Authorization: "Bearer " + token },
  });
  const status = statusOf(resp);
  const { body, text } = await readResponse(resp);
  if (status >= 300) throw new Error(serverMessage(status, body, text));
  if (!body || !Array.isArray(body.instances)) {
    throw new Error("the browser list could not be read");
  }
  return body.instances;
}

// The label for one browser in the <select>. It used to pair a display title with the id
// ("Prox (prox)"); there is no separate title anymore — the id IS the name (§6) — so the
// id stands alone and the only decoration left is marking which row is us.
export function optionLabel(id, currentId) {
  return id === currentId ? id + " — this browser" : id;
}

// The <select> options, ALWAYS containing the browser this popup runs in.
// /api/state lists only ACTIVE instances (src/db/state.py), so a revoked copy is not in
// it — and neither is anything at all when the call fails. Since targeting THIS browser
// is what the popup used to do unconditionally, it must never become unreachable: it is
// added back at the top when the list omits it.
export function instanceChoices(instances, currentId) {
  const out = [];
  const seen = new Set();
  for (const inst of instances || []) {
    const id = inst && typeof inst.id === "string" ? inst.id : null;
    if (!id || seen.has(id)) continue;
    seen.add(id);
    out.push({ id, label: optionLabel(id, currentId) });
  }
  if (!seen.has(currentId)) {
    out.unshift({ id: currentId, label: optionLabel(currentId, currentId) });
  }
  return out;
}

// POST the candidate to the server-side preview (§8). Returns the parsed payload and
// THROWS the server's own words on a non-2xx: pattern validation lives on the server
// (`validate_pattern` → 422 with a readable reason) and re-implementing it here is the
// duplicate matcher §8 forbids, so a rejected pattern is something to SHOW, not to hide.
export async function requestPreview(fetchFn, base, token, rule) {
  const resp = await fetchFn(base + "/api/rules/preview", {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({ pattern: rule.pattern, instance_id: rule.instance_id }),
  });
  const status = statusOf(resp);
  const { body, text } = await readResponse(resp);
  if (status >= 300) throw new Error(serverMessage(status, body, text));
  if (!body) throw new Error("the preview answer could not be read");
  return body;
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
  const { body, text } = await readResponse(resp);
  return { status: resp.status, body, text };
}

// Resolve the /api base + Bearer + target instanceId (§7). The SW is the ONLY source:
// the address setting, the RAW instance secret as the Bearer (slice C / option A — the
// server hashes it) and the SERVER-assigned instance id, learned from a successful hello.
//
// There is deliberately NO instance.json fallback. It used to read `config.token` and
// `config.instanceId`, and under enrollment neither field exists ANYWHERE anymore — the
// shared token is gone and the id is not self-reported. The "fallback" could therefore
// only produce `{token: undefined, instanceId: undefined}`, i.e. a rule targeted at
// `undefined` saved with no credential: a guaranteed 401 dressed up as a working path.
// Throwing here instead makes the real condition — this copy is not enrolled yet — the
// thing the popup shows.
export async function loadPopupContext(chromeApi) {
  let cred = null;
  let ident = null;
  try {
    if (chromeApi.runtime && chromeApi.runtime.sendMessage) {
      cred = await chromeApi.runtime.sendMessage({ type: "get_credential" });
      ident = await chromeApi.runtime.sendMessage({ type: "get_identity" });
    }
  } catch {
    throw new Error("the extension service worker is not answering");
  }
  if (!cred || !cred.serviceUrl) {
    // Two DIFFERENT conditions collapse into `serviceUrl: null`, because the TLS gate
    // (src/service-address.js) resolves a REFUSED address to null exactly like an absent
    // one. Telling an operator who typed `ws://host` that "no service address is
    // configured" points them at a field they already filled in; the options page and the
    // startpage were both fixed to read `addressError`, and this was the third surface.
    if (cred && cred.addressError) {
      throw new Error(
        "the configured service address was refused (" + cred.addressError +
        ") — fix it in the extension options",
      );
    }
    throw new Error("no service address is configured — open the extension options");
  }
  if (!cred.secret || !ident || !ident.instanceId) {
    throw new Error("this browser is not enrolled yet — enroll it in the extension options");
  }
  return {
    base: httpBaseFromServiceUrl(cred.serviceUrl),
    token: cred.secret,
    instanceId: ident.instanceId,
  };
}

export async function getActiveTab(chromeApi) {
  const tabs = await chromeApi.tabs.query({ active: true, currentWindow: true });
  return tabs && tabs[0];
}

// What the human is supposed to DO about a refused save. The server's own sentence
// says what went wrong; these say where the fix lives, so nobody has to go and ask.
export function saveFailureHint(status) {
  if (status === 401 || status === 403) {
    return " This browser's credential was refused — re-enroll it in the extension options.";
  }
  if (status === 423) {
    return " The curator is stopped: press Start on the start page and click again.";
  }
  if (status === 422) {
    return " Fix the pattern above and click again — nothing was saved.";
  }
  if (status >= 500) {
    return " The service did not complete the request; nothing was saved. Click again to retry.";
  }
  return "";
}

// --- reading the server's impact payload ------------------------------------
// A 409 confirm body names the uncountable instances in `not_counted`; a plain preview
// carries them as `instances[].counted === false`. Accept both.
function notCountedIds(p) {
  const ids = Array.isArray(p.not_counted)
    ? p.not_counted.map((i) => (i && i.id) || i)
    : (p.instances || []).filter((i) => i && !i.counted).map((i) => i.id);
  return ids.filter((id) => typeof id === "string" && id);
}

// WHY the server is asking for confirmation, in the human's terms.
//
// `_requires_confirm` (src/api/rules.py) gates on THREE independent grounds and only
// one of them is a number: (a) relocations+closures > 0, (b) the rule set crosses the
// empty↔non-empty boundary, (c) some instance could not be counted. Reporting only (a)
// produced the screenshot this exists to prevent: "Would relocate 0 tab(s) and close 0
// tab(s)" next to an armed "Confirm and save" and a status claiming "this rule has an
// impact" — zeros, an armed gate, and not a word about the real reason (the human's
// FIRST rule, which switches the fleet-wide "unruled → main" drain on).
//
// The payload does not name the ground, so it is derived. `enables_drain` /
// `disables_curation` describe the CANDIDATE rule set only (`enables_drain` is just
// `has_active_rules(candidate)` — true for every non-empty set, NOT "this is the first
// rule"), so the boundary crossing is identified by ELIMINATION: this popup only ever
// CREATEs, and if confirmation is required while the counts are zero and every instance
// was counted, then (b) is the only ground left — and for a create (b) can only mean
// "there were no active rules before this one". That elimination is sound here and
// nowhere else: DELETE is gated unconditionally, which is why the startpage passes its
// op in (App.vue).
export function confirmGround(preview) {
  const p = preview || {};
  const impact = (p.relocations || 0) + (p.closures || 0);
  const uncounted = notCountedIds(p);
  if (p.disables_curation) return "disables_curation";
  if (impact > 0) return "impact";
  if (uncounted.length) return "uncounted";
  if (p.enables_drain) return "first_rule";
  return "unknown";
}

// One line naming the ground, for the status row next to the armed button.
export function confirmHeadline(preview) {
  const p = preview || {};
  switch (confirmGround(p)) {
    case "disables_curation":
      return "Confirm: this leaves no active rule and turns curation off entirely";
    case "impact":
      return (
        "Confirm: " + (p.relocations || 0) + " tab(s) would move and " +
        (p.closures || 0) + " would close on the next pass"
      );
    case "uncounted":
      return "Confirm: some browsers could not be counted, so the real impact is unknown";
    case "first_rule":
      return "Confirm: this is the first active rule and it starts the fleet-wide “no rule → main” move";
    default:
      return "Confirm: the server requires confirmation for this change";
  }
}

// The impact block: the counts, what the counts actually mean, and the reason
// confirmation is being asked for. `requiresConfirm` overrides the payload's own flag
// (a 409 body IS the confirmation request, whether or not it echoes the field).
export function summarize(preview, { requiresConfirm = null } = {}) {
  const p = preview || {};
  const r = p.relocations || 0;
  const c = p.closures || 0;
  const uncounted = notCountedIds(p);
  const gated = requiresConfirm === null ? Boolean(p.requires_confirm) : Boolean(requiresConfirm);
  const lines = [
    `Would relocate ${r} tab(s) and close ${c} tab(s) on the next pass.`,
    // A zero here does NOT mean "the rule does not match". The simulation counts the
    // tabs the next pass would actually TOUCH, and §7 step 4 / preview.py `_guarded`
    // skip a tab that is not idle long enough yet (and pinned / audible / on-screen
    // ones, always). A site you were reading a minute ago matches and still counts 0.
    "That is what the NEXT pass would move — not everything the pattern matches: " +
      "a tab you used recently, or one that is pinned, playing audio or on screen, " +
      "is not counted yet.",
  ];
  const ground = confirmGround(p);
  if (ground === "disables_curation") {
    lines.push(
      "This leaves NO active rule: curation stops completely — nothing is relocated " +
        "or closed anywhere until a rule exists again.",
    );
  } else if (gated && ground === "first_rule") {
    lines.push(
      "This would be the FIRST active rule, and that is why confirmation is required " +
        "even with zeros above: saving it switches curation on for the whole fleet — " +
        "from the next pass EVERY tab with no matching rule is moved to the main " +
        "browser, not just the tabs matching this pattern.",
    );
  }
  if (uncounted.length) {
    lines.push(
      "Not counted (stale/disconnected): " + uncounted.join(", ") +
        ". Their tabs are left out of the numbers above, so the real impact can be larger.",
    );
  }
  return lines.join("\n");
}

// --- DOM wiring (browser only) ---------------------------------------------
function renderChoices(doc, select, choices, selectedId) {
  if (!select) return;
  for (const choice of choices) {
    const opt = doc.createElement("option");
    opt.value = choice.id;
    // An instance id is SERVER data: textContent, never innerHTML.
    opt.textContent = choice.label;
    select.appendChild(opt);
  }
  select.value = selectedId; // default: the browser this popup is open in
}

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
  const setImpact = (t) => {
    if (els.impact) els.impact.textContent = t;
  };
  const setLabel = (t) => {
    if (els.save) els.save.textContent = t;
  };

  let base, token, ownInstanceId, tabRule;
  try {
    const ctx = await loadPopupContext(chromeApi);
    base = ctx.base;
    token = ctx.token;
    ownInstanceId = ctx.instanceId;
    const tab = await getActiveTab(chromeApi);
    if (!tab || !tab.url) throw new Error("no active tab URL");
    tabRule = buildRule(tab.url, ctx.instanceId);
  } catch (e) {
    setStatus("Cannot build a rule for this tab: " + (e && e.message));
    if (els.save) els.save.disabled = true;
    return;
  }

  if (els.origin) els.origin.textContent = tabRule.label;
  if (els.pattern) els.pattern.value = tabRule.pattern;

  // The fleet list is a convenience, never a precondition: if /api/state cannot be
  // read the popup still files the rule it was opened for, at THIS browser — the
  // behaviour it had before the target became pickable — and says so.
  let choices;
  try {
    choices = instanceChoices(await fetchInstances(fetchFn, base, token), ownInstanceId);
  } catch (e) {
    choices = instanceChoices([], ownInstanceId);
    setStatus(
      "Could not load the list of browsers (" + (e && e.message) +
        ") — only this browser can be picked.",
    );
  }
  renderChoices(doc, els.target, choices, ownInstanceId);

  // --- preview / confirm state ---------------------------------------------
  // `epoch` counts EDITS to what would be saved. Every preview captures it at send
  // time and renders only if it is still current, because responses can land out of
  // order — a slow preview for pattern A arriving after a fast one for B would put A's
  // numbers on screen under B — and because an edit made while a request is in flight
  // must invalidate that request even before its replacement is sent (the pattern field
  // is debounced, so there IS a window with no newer request yet).
  let epoch = 0;
  let confirmArmed = false;
  let saved = false;
  let debounceTimer = null;

  const currentRule = () => ({
    pattern: els.pattern ? String(els.pattern.value || "").trim() : tabRule.pattern,
    instance_id: els.target && els.target.value ? els.target.value : ownInstanceId,
  });

  function disarm() {
    confirmArmed = false;
    setLabel(SAVE_LABEL);
  }

  async function runPreview() {
    const mine = epoch;
    const rule = currentRule();
    let text;
    try {
      text = summarize(await requestPreview(fetchFn, base, token, rule));
    } catch (e) {
      // A failed preview must NOT become a confirmed impact: the human has been shown
      // nothing, so the first Save goes WITHOUT confirm_impact and the server's own 409
      // asks the question (below). This is the case §8's gate is for. A 422 for a bad
      // pattern lands here too, carrying the server's own wording — so say which of the
      // two this is instead of leaving the human to guess whether the rule is wrong or
      // the service is unreachable.
      text =
        "Preview unavailable: " + (e && e.message) +
        "\nYou can still save: the server recomputes the impact and asks before it " +
        "commits anything.";
    }
    if (mine !== epoch || saved) return; // superseded by a newer edit / already saved
    setImpact(text);
    setLabel(confirmArmed ? CONFIRM_LABEL : SAVE_LABEL);
    if (els.save) els.save.disabled = false;
  }

  // Any change to the pattern or the target changes WHAT would be saved, so it must
  // take the armed confirmation with it. Otherwise the two-step gate becomes the blind
  // confirmation it exists to prevent: see the impact of pattern A, switch to B, click
  // "Confirm and save" without looking. The numbers on screen go stale at the same
  // instant, and the button stops claiming they are current until the new preview lands.
  function invalidate({ debounce }) {
    if (saved) return Promise.resolve();
    epoch += 1;
    disarm();
    if (els.save) els.save.disabled = true;
    setLabel(SAVE_PENDING_LABEL);
    setImpact(PENDING_IMPACT);
    if (debounceTimer !== null) {
      clearTimeout(debounceTimer);
      debounceTimer = null;
    }
    if (!debounce) return runPreview();
    debounceTimer = setTimeout(() => {
      debounceTimer = null;
      runPreview();
    }, PATTERN_DEBOUNCE_MS);
    return Promise.resolve();
  }

  function freeze() {
    // A saved rule is final for this popup: another click would file a duplicate, and
    // editing the form after the write would preview a rule this popup can no longer
    // create. Leaving the button disabled is the old contract; the fields follow it.
    saved = true;
    if (debounceTimer !== null) {
      clearTimeout(debounceTimer);
      debounceTimer = null;
    }
    if (els.pattern) els.pattern.disabled = true;
    if (els.target) els.target.disabled = true;
  }

  if (els.pattern) {
    els.pattern.addEventListener("input", () => invalidate({ debounce: true }));
  }
  if (els.target) {
    els.target.addEventListener("change", () => invalidate({ debounce: false }));
  }

  if (els.save) {
    // The §8 confirm gate is TWO actions, never one (same contract as the startpage,
    // App.vue onSave): the first click saves WITHOUT confirm_impact; a 409 shows the
    // impact the server computed and arms the button; only the second, deliberate
    // click sends confirm_impact:true. Sending it up front would echo-confirm the
    // server's question with the human never having seen an impact.
    els.save.addEventListener("click", async () => {
      els.save.disabled = true;
      setStatus(confirmArmed ? "Confirming…" : "Saving…");
      // Snapshot the edit counter BEFORE the request. The server's whole-pass preview
      // runs for a second or two — long enough for the human to widen the pattern — and
      // the 409 handler below arms the gate AFTER that await. Without this check the
      // arriving 409 re-arms a gate the edit had just cleared, and the next click sends
      // confirm_impact:true for a rule the server never previewed (§8). The startpage
      // guards the same window with `draftRevision` (App.vue onSave).
      const sentEpoch = epoch;
      try {
        const { status, body, text } = await saveRule(fetchFn, base, token, currentRule(), {
          confirmImpact: confirmArmed,
        });
        if (epoch !== sentEpoch) {
          // The rule changed under the request: whatever came back describes the old
          // one. Stay disarmed; the preview that the edit scheduled owns the button (it
          // is the one that will re-enable it, so the label keeps saying so meanwhile).
          disarm();
          setLabel(SAVE_PENDING_LABEL);
          setStatus(
            "The rule changed while saving, so that answer was about the previous one. " +
              "Nothing was saved — review the new impact and click again.",
          );
          return;
        }
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
          setImpact(summarize(body, { requiresConfirm: true }));
          els.save.textContent = CONFIRM_LABEL;
          els.save.disabled = false;
          // Name the ACTUAL ground. "This rule has an impact" over two zeros was a lie
          // whenever the gate fired on the empty→non-empty boundary or an uncounted
          // instance (src/api/rules.py `_requires_confirm`).
          setStatus(confirmHeadline(body) + " — review the details above, then click again to confirm.");
          return;
        }
        confirmArmed = false;
        if (status < 300) {
          freeze();
          // Say what happens next, not just that it worked: the form is frozen on
          // purpose (a second click would file the same rule twice), so the way to add
          // another one has to be on the screen.
          setStatus("Rule saved. Reopen the popup to add another rule.");
          return; // saved: leaving the button disabled prevents a duplicate rule
        }
        // ANY other non-2xx (422 bad pattern, 423 paused, 5xx) is RETRYABLE — a paused
        // curator resumes, a typo gets fixed. Leaving the button disabled meant the
        // human had to close and reopen the popup to try again. The server's own words
        // ride along: for 422 they name what is wrong with the pattern.
        els.save.textContent = SAVE_LABEL;
        els.save.disabled = false;
        setStatus(
          "Save failed (HTTP " + status + "): " + serverMessage(status, body, text) +
            saveFailureHint(status),
        );
      } catch (e) {
        // Disarm the LABEL together with the flag: leaving "Confirm and save" on a
        // button that will now send an unconfirmed probe makes the button lie about
        // what the next click does.
        confirmArmed = false;
        els.save.textContent = SAVE_LABEL;
        setStatus("Save failed: " + (e && e.message));
        els.save.disabled = false;
      }
    });
  }

  // First preview: the same path every later edit takes.
  await invalidate({ debounce: false });
}

// Auto-run in the extension page; never at import under vitest (node, no document).
if (typeof document !== "undefined" && document.getElementById("save")) {
  init(document, chrome, fetch).catch((e) => console.error("[popup]", e));
}
