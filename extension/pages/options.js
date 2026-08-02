// Options page logic: read/write the execute_js opt-in checkbox (§12).
//
// The checkbox is the authoritative runtime gate for execute_js on THIS copy of
// the extension. It lives in chrome.storage.local under `allowExecuteJs`
// (default OFF), is read fresh by the command dispatcher on every execute_js, and
// its stored value is what `hello` reports to the service. Kept in an external
// module because the extension_pages CSP forbids inline scripts.

const ALLOW_EXECUTE_JS_KEY = "allowExecuteJs";

const checkbox = document.getElementById("allow-execute-js");
const status = document.getElementById("status");

function showStatus(text) {
  if (status) status.textContent = text;
}

// Load the stored value (default OFF when never set).
async function load() {
  const got = await chrome.storage.local.get(ALLOW_EXECUTE_JS_KEY);
  checkbox.checked = !!(got && got[ALLOW_EXECUTE_JS_KEY]);
}

// Persist on every toggle. A copied bundle never inherits another copy's choice —
// this key is in the profile's storage.local, not in the shipped instance.json.
checkbox.addEventListener("change", async () => {
  await chrome.storage.local.set({ [ALLOW_EXECUTE_JS_KEY]: checkbox.checked });
  showStatus(checkbox.checked ? "execute_js enabled on this copy" : "execute_js disabled on this copy");
});

load().catch((e) => showStatus("Failed to load settings: " + e));
