// Options page logic (§7, §12): the enrollment settings + the execute_js opt-in.
//
// Every setting lives in chrome.storage.local (per-profile, NEVER in the bundle):
//   allowExecuteJs — the authoritative execute_js gate (§12), default OFF
//   serviceAddress — the wss/ws service URL (the shared token is gone, §7)
//   browserName    — the name this browser enrols under; it BECOMES the instance id
//   enrollCode     — the ~10-min window code, submitted once to enroll
// The enroll state is fetched from the SW's get_connection_state (durable-fact derived,
// §7). Kept in an external module because the extension_pages CSP forbids inline scripts.
//
// Enrolment is ONE step (§6): a valid code into an open window enrols this browser on the
// spot, under the name below. So this page is also where a REFUSAL is read — the admin
// console has no list of refused attempts to look at — which is why the name is validated
// here before anything is sent. Every refusal is shown AT the field it is about (the
// input turns red and the line under it carries the reason); the page-level status row
// is left for outcomes, so nothing is said twice.
//
// Pure helpers are exported for unit tests; DOM wiring runs only in a document.

// Storage keys — mirror src/constants.js (options.js is loaded raw in the page, not
// bundled, so it does not import to avoid module-resolution surprises under the CSP).
const ALLOW_EXECUTE_JS_KEY = "allowExecuteJs";
// The debugger opt-in (§12), default OFF like execute_js. Nothing reads it as a gate yet
// — it is reported in `hello` so an agent can see this copy's capabilities up front — but
// the switch ships with the report so the owner decides BEFORE the first consumer exists.
const ALLOW_DEBUGGER_KEY = "allowDebugger";
const SERVICE_ADDRESS_KEY = "serviceAddress";
const INSTANCE_NAME_KEY = "browserName";
const ENROLL_CODE_KEY = "enrollCode";
const ENROLL_STATE_KEY = "enrollState";

// The instance-id charset, mirroring extension/src/constants.js INSTANCE_NAME_RE and,
// behind it, the service's src/ext/protocol.py INSTANCE_ID_RE. Checked here so a bad name
// is refused instantly and locally instead of costing a round trip and burning the staged
// window code; the service re-checks it and answers enroll_rejected{bad_id} for a client
// that skipped this. test/options.test.js runs this and the src/ copy over one table.
const INSTANCE_NAME_RE = /^[A-Za-z0-9._-]{1,64}$/;

export function instanceNameError(raw) {
  const value = typeof raw === "string" ? raw.trim() : "";
  if (!value) return "empty";
  return INSTANCE_NAME_RE.test(value) ? null : "charset";
}

// The refusal, spelled out — including what to type instead. A name that fails is NOT
// saved: keeping it would leave the field looking accepted over a value that can never
// enrol. It is rendered in red UNDER the field, so it says the rule rather than opening
// with "Refused:" — the place and the colour already say that much.
export function instanceNameErrorText(code) {
  switch (code) {
    case "empty":
      return "Enter the browser name first — it becomes this instance's id (e.g. work-laptop).";
    case "charset":
    default:
      return (
        "The name becomes this instance's id: 1-64 characters of A-Z a-z 0-9 . _ - " +
        "with no spaces (e.g. work-laptop or Prox.2)."
      );
  }
}

// --- the service-address gate (§7) -----------------------------------------
// KEEP IN SYNC with extension/src/service-address.js — that file carries the rationale
// (option A puts the RAW secret on the wire, so TLS is the only thing hiding it, so a
// `ws://`/`http://` address is refused rather than hinted at, and a scheme-less address
// gets the only scheme that could have been meant). The check is duplicated because this
// page is loaded raw under the extension_pages CSP and imports nothing;
// test/options.test.js runs BOTH implementations over the same tables so they cannot drift.
const LOOPBACK_HOSTS = ["localhost", "127.0.0.1", "[::1]"];
const HAS_SCHEME = /^[a-z][a-z0-9+.-]*:\/\//i;

export function normalizeServiceAddress(raw) {
  const value = typeof raw === "string" ? raw.trim() : "";
  if (!value) return "";
  // An explicit scheme is kept — EXCEPT https://, which is upgraded to wss://.
  // Refusing https:// was pedantry, not safety: it is the same TLS the gate
  // demands, on the same host, and the operator who pastes the service URL out
  // of the address bar means exactly the socket we would dial. http:// stays
  // refused — that one really is plaintext, which is what this gate exists for.
  if (/^https:\/\//i.test(value)) return value.replace(/^https:/i, "wss:");
  if (HAS_SCHEME.test(value)) return value; // explicit scheme: never rewritten
  const bare = value.replace(/^\/+/, "");
  let hostname;
  try {
    hostname = new URL("wss://" + bare).hostname;
  } catch {
    return value;
  }
  if (!hostname) return value;
  return (LOOPBACK_HOSTS.includes(hostname) ? "ws://" : "wss://") + bare;
}

export function serviceAddressError(raw) {
  const value = normalizeServiceAddress(raw);
  if (!value) return "empty";
  let url;
  try {
    url = new URL(value);
  } catch {
    return "malformed";
  }
  if (!url.hostname) return "malformed";
  if (url.protocol === "wss:") return null;
  if (url.protocol === "ws:") {
    return LOOPBACK_HOSTS.includes(url.hostname) ? null : "insecure";
  }
  if (url.protocol === "http:" || url.protocol === "https:") return "http-scheme";
  return "malformed";
}

// The refusal reason, spelled out for the operator — including what to type INSTEAD,
// since the field no longer asks for a scheme. "Saved" is not an option for a rejected
// address: silently keeping it would leave the field looking accepted.
//
// These are the lines that replaced the paragraph about schemes that used to sit above
// the field: one sentence, shown in red UNDER the input that broke, and only when it
// broke. Each one names wss:// — whatever went wrong, that is what the operator has to
// end up with.
export function addressErrorText(code) {
  switch (code) {
    case "insecure":
      return (
        "Unencrypted. The instance secret travels over this connection — use " +
        "curator.example or an explicit wss://."
      );
    case "http-scheme":
      return (
        "Wrong scheme: that is the address of a web page, not of this service's " +
        "socket. Use curator.example, or an explicit wss:// URL."
      );
    case "empty":
      return "Enter the service address first — e.g. curator.example (it becomes wss://curator.example).";
    case "malformed":
    default:
      return (
        "Not an address. Enter a host like curator.example:8443 (it becomes " +
        "wss://curator.example:8443), or a full wss:// URL."
      );
  }
}

// The refusal reasons the service can answer an enroll_request with
// (src/ext/protocol.py), spelled out for the human who has to fix them. A bare
// "заявка отклонена: id_taken" names the wire constant, not the problem; these say what
// to change. An unknown reason falls back to the raw string rather than being swallowed.
const ENROLL_REJECT_TEXT = {
  id_taken: "имя уже занято другим активным браузером — выберите другое",
  bad_id: "имя не подходит: 1-64 символа из A-Z a-z 0-9 . _ - без пробелов",
  bad_code: "неверный код регистрации — возьмите новый в консоли",
  closed: "окно регистрации закрыто — попросите открыть новое",
  protocol: "версия протокола не совпала — обновите расширение или сервис",
};

export function enrollRejectText(reason) {
  return ENROLL_REJECT_TEXT[reason] || String(reason);
}

// A human label for an enroll state (§7). Unknown/empty reads as "не зарегистрирован".
//
// There is no "ожидает одобрения" anymore, in the label or in the states behind it: an
// enroll_request is accepted or refused on the spot (§6), so a browser is either enrolled
// or it is not — and if it is not, the interesting fact is the REASON, which is what a
// stored `reject` carries.
export function enrollLabel(state, reject) {
  if (reject && (state === "needs-enroll" || !state)) {
    return "не зарегистрирован — " + enrollRejectText(reject);
  }
  // QUARANTINE gets its own line because `getEnrollState` resolves `quarantined` BEFORE
  // everything but `revoked`: a quarantined instance still holds a valid old secret, so
  // its state is never needs-enroll and the reject above would never be reached on the one
  // path that cannot recover by itself. A terminal reject wipes the staged code, the probe
  // stops asking, and nothing moves until the operator acts — which the label has to say,
  // or the screen keeps reading "требуется повторная регистрация" over an attempt that
  // already came back rejected.
  if (reject && state === "quarantined") {
    return "повторная регистрация отклонена: " + enrollRejectText(reject);
  }
  switch (state) {
    case "approved":
      return "активен";
    case "revoked":
      return "отозван";
    case "quarantined":
      return "неизвестный инстанс — требуется повторная регистрация";
    case "needs-enroll":
    default:
      return "не зарегистрирован";
  }
}

// --- DOM wiring (browser only) ---------------------------------------------
// A refusal belongs NEXT TO the field it is about: `<field>-error` carries the reason and
// the input carries `is-invalid` (the red border in ui.css). The page-level status row
// keeps the outcomes ("Service address saved", "Enrollment sent — …") so a refusal is
// never printed in two places at once, and never has to be hunted for at the bottom of
// the page. The text comes from the SAME gate that decides whether the value may be
// stored (serviceAddressError / instanceNameError) — there is no second validation here.
function fieldRefusal(doc, id) {
  const input = doc.getElementById(id);
  const line = doc.getElementById(id + "-error");
  return (text) => {
    if (line) line.textContent = text || "";
    if (!input) return;
    input.classList.toggle("is-invalid", Boolean(text));
    input.setAttribute("aria-invalid", text ? "true" : "false");
  };
}

export async function init(doc, chromeApi) {
  const el = (id) => doc.getElementById(id);
  const status = el("status");
  const setStatus = (t) => {
    if (status) status.textContent = t;
  };

  const checkbox = el("allow-execute-js");
  const debuggerBox = el("allow-debugger");
  const addressInput = el("service-address");
  const nameInput = el("browser-name");
  const codeInput = el("enroll-code");
  const submitBtn = el("submit-enroll");
  const stateOut = el("enroll-state");

  const showAddressRefusal = fieldRefusal(doc, "service-address");
  const showNameRefusal = fieldRefusal(doc, "browser-name");
  const showCodeRefusal = fieldRefusal(doc, "enroll-code");

  // Load current values.
  const got = await chromeApi.storage.local.get([
    ALLOW_EXECUTE_JS_KEY,
    ALLOW_DEBUGGER_KEY,
    SERVICE_ADDRESS_KEY,
    INSTANCE_NAME_KEY,
    ENROLL_CODE_KEY,
  ]);
  if (checkbox) checkbox.checked = !!got[ALLOW_EXECUTE_JS_KEY];
  if (debuggerBox) debuggerBox.checked = !!got[ALLOW_DEBUGGER_KEY];
  if (addressInput) addressInput.value = got[SERVICE_ADDRESS_KEY] || "";
  if (nameInput) nameInput.value = got[INSTANCE_NAME_KEY] || "";
  if (codeInput) codeInput.value = got[ENROLL_CODE_KEY] || "";

  // Enroll state from the SW (durable-fact derived, §7). `addressError` is reported here
  // too: a profile whose stored address predates this gate (or a bundle bootstrap with a
  // ws:// serviceUrl) is refused by the SW, and the operator must see WHY rather than an
  // address that is filled in but does nothing.
  const refreshState = async () => {
    let st = null;
    try {
      st = await chromeApi.runtime.sendMessage({ type: "get_connection_state" });
    } catch {
      st = null;
    }
    if (stateOut) {
      stateOut.textContent = enrollLabel(st && st.enrollState, st && st.enrollReject);
      // The pill's dot is green only for a browser that IS enrolled; every other state
      // (including "not enrolled at all") leaves it grey. A dot that is always green
      // would contradict the words right next to it.
      stateOut.classList.toggle("is-enrolled", Boolean(st) && st.enrollState === "approved");
    }
    return st;
  };
  const first = await refreshState();
  if (first && first.addressError) showAddressRefusal(addressErrorText(first.addressError));

  // The verdict arrives on the SOCKET, not on the submit call: `submit_enrollment` returns
  // as soon as the frame is on its way, and `enroll_accepted`/`enroll_rejected` lands a
  // moment later in a worker that is not this page. The SW writes both into the durable
  // enroll facts, so watching that key is how this page shows the answer without a reload
  // — and showing it is the point, because the admin console has no list of refusals.
  // Guarded: `storage.onChanged` is optional in the test env, and its absence only costs
  // the live update, never the page.
  const onChanged = chromeApi.storage && chromeApi.storage.onChanged;
  if (onChanged && typeof onChanged.addListener === "function") {
    onChanged.addListener((changes, area) => {
      if (area !== "local" || !changes || !changes[ENROLL_STATE_KEY]) return;
      refreshState().catch(() => {});
    });
  }

  // Persist-on-change for the plain settings.
  if (checkbox) {
    checkbox.addEventListener("change", async () => {
      await chromeApi.storage.local.set({ [ALLOW_EXECUTE_JS_KEY]: checkbox.checked });
      setStatus(checkbox.checked ? "execute_js enabled on this copy" : "execute_js disabled on this copy");
    });
  }
  if (debuggerBox) {
    debuggerBox.addEventListener("change", async () => {
      await chromeApi.storage.local.set({ [ALLOW_DEBUGGER_KEY]: debuggerBox.checked });
      setStatus(
        debuggerBox.checked
          ? "debugger enabled on this copy — the «идёт отладка» bar will show while attached"
          : "debugger disabled on this copy",
      );
    });
  }
  if (addressInput) {
    addressInput.addEventListener("change", async () => {
      // The operator may type just the address; the scheme is derivable, so it is added
      // here (wss://, or ws:// on loopback) and the STORED value is that full URL — the
      // SW dials what is stored.
      const value = normalizeServiceAddress(addressInput.value);
      // An empty field is a legitimate "not configured yet" — store it (clearing the
      // setting) without shouting. Anything else must pass the scheme gate BEFORE it is
      // persisted: a stored ws:// address is not a warning to act on later, it is the
      // exact state in which the next hello publishes the secret.
      const error = value ? serviceAddressError(value) : null;
      if (error) {
        showAddressRefusal(addressErrorText(error));
        return;
      }
      showAddressRefusal("");
      await chromeApi.storage.local.set({ [SERVICE_ADDRESS_KEY]: value });
      // Show what was actually stored: the operator typed a host and must be able to
      // SEE which scheme it was saved with, not have to ask.
      addressInput.value = value;
      setStatus(value ? "Service address saved" : "Service address cleared");
    });
  }
  if (nameInput) {
    nameInput.addEventListener("change", async () => {
      const value = nameInput.value.trim();
      // An empty field is a legitimate "not set yet" — store it (clearing the setting)
      // without shouting. Anything else must pass the id gate BEFORE it is persisted: a
      // stored name that cannot be an instance id is not a warning to act on later, it is
      // an enrolment that will be refused after burning the one-shot window code.
      const error = value ? instanceNameError(value) : null;
      if (error) {
        showNameRefusal(instanceNameErrorText(error));
        return;
      }
      showNameRefusal("");
      await chromeApi.storage.local.set({ [INSTANCE_NAME_KEY]: value });
      nameInput.value = value;
      setStatus(value ? "Browser name saved" : "Browser name cleared");
    });
  }
  if (codeInput) {
    codeInput.addEventListener("change", async () => {
      const code = codeInput.value.trim();
      // Any code at all clears the "enter the code first" line: the only thing this
      // field can be refused for is being empty at submit time.
      if (code) showCodeRefusal("");
      await chromeApi.storage.local.set({ [ENROLL_CODE_KEY]: code });
    });
  }

  // Submit enrollment: persist the code, then ask the SW to send the enroll_request
  // with it now (§7 / acc 5).
  if (submitBtn) {
    submitBtn.addEventListener("click", async () => {
      const code = codeInput ? codeInput.value.trim() : "";
      if (!code) {
        showCodeRefusal("Enter the enrollment code first — it comes from /admin.");
        return;
      }
      showCodeRefusal("");
      // Submitting against a refused (or missing) address would send the one-time window
      // code and the fresh secret nowhere — or, without the gate, into the clear. Refuse
      // here too: the address field may hold an unsaved value the change handler rejected.
      // The ADDRESS is checked before the name because it is the more fundamental refusal:
      // with nowhere to send the frame, the name it would carry is beside the point.
      const address = normalizeServiceAddress(addressInput ? addressInput.value : "");
      const addressError = serviceAddressError(address);
      if (addressError) {
        showAddressRefusal(addressErrorText(addressError));
        return;
      }
      showAddressRefusal("");
      // The name IS the instance id (§6), and it travels in the enroll_request. Refuse
      // here too: the field may hold an unsaved value the change handler rejected, and
      // submitting it would spend the one-shot window code on a certain bad_id.
      const name = nameInput ? nameInput.value.trim() : "";
      const nameError = instanceNameError(name);
      if (nameError) {
        showNameRefusal(instanceNameErrorText(nameError));
        return;
      }
      showNameRefusal("");
      submitBtn.disabled = true;
      // Persist the (validated, scheme-completed) address and the (validated) name
      // together with the code: the operator may have typed all three and clicked
      // straight through, and the SW resolves every one of them from storage, not from
      // this page.
      await chromeApi.storage.local.set({
        [SERVICE_ADDRESS_KEY]: address,
        [INSTANCE_NAME_KEY]: name,
        [ENROLL_CODE_KEY]: code,
      });
      if (addressInput) addressInput.value = address;
      try {
        const res = await chromeApi.runtime.sendMessage({ type: "submit_enrollment", code });
        // "Submitted" is a claim about US, not about the service: the socket may not have
        // opened at all. Say what actually happens — the request is re-sent until the
        // service confirms it (connection.js `_sendOpening`) — so a silent "waiting for
        // approval" over a request nobody received is not what the operator reads.
        setStatus(
          res && res.ok
            ? "Enrollment sent — the answer arrives on the same connection; watch the state above"
            : "Submit failed" + (res && res.error ? ": " + res.error : ""),
        );
        // Refresh the shown state. This is the state BEFORE the verdict in the usual
        // case; the storage.onChanged watcher above repaints it when the answer lands.
        await refreshState();
      } catch (e) {
        setStatus("Submit failed: " + (e && e.message));
      } finally {
        submitBtn.disabled = false;
      }
    });
  }
}

// Auto-run in the extension page; never at import under vitest (node, no document).
if (typeof document !== "undefined" && document.getElementById("submit-enroll")) {
  init(document, chrome).catch((e) => console.error("[options]", e));
}
