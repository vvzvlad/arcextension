import { describe, it, expect, vi } from "vitest";
import {
  addressErrorText,
  enrollLabel,
  init,
  instanceNameError,
  instanceNameErrorText,
  normalizeServiceAddress as optionsNormalizeServiceAddress,
  serviceAddressError as optionsServiceAddressError,
} from "../pages/options.js";
import {
  normalizeServiceAddress as swNormalizeServiceAddress,
  serviceAddressError as swServiceAddressError,
} from "../src/service-address.js";

// There is no installUuidPrefix test anymore, and no installUuid row on the page to test:
// the "this install's identity" line was removed with the redesign (see constants.js —
// /admin stopped printing the uuid, so the prefix had nothing left to be compared with).

// --- the service address gate (§7) ------------------------------------------
describe("serviceAddressError", () => {
  // Option A puts the RAW secret on the wire (enroll_request, every hello, the /api
  // Bearer), so TLS is the ONLY thing hiding it: an unencrypted address is refused, not
  // warned about. `ws://` survives for loopback because a dev service has no certificate
  // and the traffic never leaves the machine.
  const TABLE = [
    ["wss://curator.example", null],
    ["wss://curator.example:8443/ext", null],
    ["  wss://curator.example  ", null],
    ["ws://localhost:8000", null],
    ["ws://127.0.0.1:8000", null],
    ["ws://[::1]:8000", null],
    ["ws://curator.lan:8000", "insecure"],
    ["ws://localhost.evil.example", "insecure"], // a loopback-LOOKING host is not loopback
    ["http://curator.example", "http-scheme"],
    // https:// is ACCEPTED and dialled as wss:// — same TLS, same host. Pasting the
    // service URL out of the address bar is the normal way to fill this field.
    ["https://curator.example", null],
    ["http://curator.example", "http-scheme"], // plaintext: still refused
    // A SCHEME-LESS address is now the normal way to fill the field: the only scheme
    // that could have been meant is added by the gate itself, so these are accepted
    // (they used to be "malformed" — a demand for a prefix that had no alternative).
    ["curator.example:8000", null],
    ["curator.example", null],
    ["localhost:8000", null], // → ws:// (the loopback development exception)
    ["nonsense", null], // a single-label intranet host is a host
    // …but "not an address" is still not an address.
    ["curator example", "malformed"],
    ["wss://", "malformed"],
    ["", "empty"],
    [null, "empty"],
  ];

  it("accepts wss:// and loopback ws://, refuses everything else with a reason", () => {
    for (const [input, expected] of TABLE) {
      expect(optionsServiceAddressError(input), String(input)).toBe(expected);
    }
  });

  it("the options-page copy and the service-worker copy agree exactly", () => {
    // The check is duplicated because the options page is loaded raw under the
    // extension_pages CSP and imports nothing. Duplication is only safe while the two
    // cannot drift — this is what keeps them honest.
    for (const [input] of TABLE) {
      expect(optionsServiceAddressError(input), String(input)).toBe(
        swServiceAddressError(input),
      );
    }
  });

  it("every refusal has human text naming wss://", () => {
    for (const code of ["insecure", "http-scheme", "malformed", "empty"]) {
      expect(addressErrorText(code)).toMatch(/wss:\/\//);
    }
  });
});

// --- the scheme is derived, not demanded (§7) --------------------------------
describe("normalizeServiceAddress", () => {
  // The operator used to have to type `wss://` in front of an address where no other
  // scheme was ever acceptable. The scheme is a decision this module makes (it IS the
  // security decision), so it makes it — and only for an address that carries none.
  const NORM = [
    ["curator.nebula.lc", "wss://curator.nebula.lc"],
    ["curator.nebula.lc:8443", "wss://curator.nebula.lc:8443"],
    ["  curator.nebula.lc  ", "wss://curator.nebula.lc"], // trimmed first
    // Loopback keeps the development exception the gate already makes, so a dev
    // service with no certificate is reachable by typing exactly what is in the URL bar.
    ["localhost:8000", "ws://localhost:8000"],
    ["127.0.0.1:8000", "ws://127.0.0.1:8000"],
    ["[::1]:8000", "ws://[::1]:8000"],
    ["LOCALHOST:8000", "ws://LOCALHOST:8000"], // host case is the URL parser's business
    // An EXPLICIT scheme is never rewritten: backward compatibility for every address
    // already stored, and the refusals below must keep their reason.
    // ...with ONE exception: https:// is the same TLS on the same host, so it is
    // upgraded rather than refused. This is the address people actually paste.
    ["https://curator.nebula.lc", "wss://curator.nebula.lc"],
    ["https://curator.nebula.lc:8443/ext", "wss://curator.nebula.lc:8443/ext"],
    ["HTTPS://curator.nebula.lc", "wss://curator.nebula.lc"], // scheme case-insensitive
    ["http://curator.nebula.lc", "http://curator.nebula.lc"], // untouched => still refused
    ["wss://curator.nebula.lc", "wss://curator.nebula.lc"],
    ["wss://curator.nebula.lc:8443/ext", "wss://curator.nebula.lc:8443/ext"],
    ["ws://curator.lan:8000", "ws://curator.lan:8000"],
    ["http://curator.example", "http://curator.example"],
    ["", ""],
    [null, ""],
  ];

  it("adds the scheme only when the operator left one out", () => {
    for (const [input, expected] of NORM) {
      expect(optionsNormalizeServiceAddress(input), String(input)).toBe(expected);
    }
  });

  it("the options-page copy and the service-worker copy agree exactly", () => {
    for (const [input] of NORM) {
      expect(optionsNormalizeServiceAddress(input), String(input)).toBe(
        swNormalizeServiceAddress(input),
      );
    }
  });

  it("normalizing never turns a REFUSED address into an accepted one", () => {
    // The point of normalizing inside the gate: an explicit insecure address keeps its
    // scheme, so it keeps its refusal. Rewriting `http://` to `wss://` would silently
    // "fix" a typo into a different service.
    expect(optionsServiceAddressError("http://curator.example")).toBe("http-scheme");
    expect(optionsServiceAddressError("ws://curator.lan:8000")).toBe("insecure");
    // …while the bare form of that same host is accepted, because it says nothing
    // about the transport and therefore gets the safe one.
    expect(optionsServiceAddressError("curator.lan:8000")).toBe(null);
    expect(optionsNormalizeServiceAddress("curator.lan:8000")).toBe("wss://curator.lan:8000");
  });
});

describe("enrollLabel", () => {
  it("maps each enroll state to a human label — and has no 'waiting' state left", () => {
    expect(enrollLabel("revoked")).toBe("отозван");
    expect(enrollLabel("approved")).toBe("активен");
    expect(enrollLabel("needs-enroll")).toBe("не зарегистрирован");
    expect(enrollLabel(null)).toBe("не зарегистрирован");
    // `pending` is GONE (§6): an enroll_request is answered on the spot, so there is no
    // "ожидает одобрения" for a browser to be stuck in. Reddens if the label is
    // reintroduced — it would be shown over a state nothing can ever leave.
    expect(enrollLabel("pending")).toBe("не зарегистрирован");
    for (const state of ["approved", "revoked", "needs-enroll", "quarantined", null]) {
      expect(enrollLabel(state)).not.toContain("ожидает");
    }
  });

  it("surfaces the refusal reason — the only place a human ever sees it", () => {
    // There is no pending list in /admin anymore, so a refused enrolment leaves NO trace a
    // human can look at except this label (and a /metrics counter). It must therefore say
    // what to change, not print the wire constant.
    expect(enrollLabel("needs-enroll", "id_taken")).toContain("имя уже занято");
    expect(enrollLabel("needs-enroll", "bad_id")).toContain("имя не подходит");
    expect(enrollLabel("needs-enroll", "bad_code")).toContain("неверный код");
    expect(enrollLabel("needs-enroll", "closed")).toContain("окно регистрации закрыто");
    expect(enrollLabel(null, "closed")).toContain("окно регистрации закрыто");
    // Still not enrolled, and it says so before the reason.
    expect(enrollLabel("needs-enroll", "id_taken")).toContain("не зарегистрирован");
    // An unknown reason is shown raw rather than swallowed.
    expect(enrollLabel("needs-enroll", "brand_new")).toContain("brand_new");
    // An enrolled instance ignores a stale reject.
    expect(enrollLabel("approved", "bad_code")).toBe("активен");
  });

  it("surfaces the reason on the QUARANTINE path, which cannot recover by itself", () => {
    // The regression this pins: a quarantined instance keeps a valid old secret, so
    // getEnrollState answers `quarantined` and never needs-enroll — gate the reject on
    // needs-enroll only and the operator who typed a wrong code sees the same
    // "требуется повторная регистрация" as before submitting, forever. The staged code was
    // wiped with the reject, so the probe will not retry and only the operator moves this.
    const label = enrollLabel("quarantined", "bad_code");
    expect(label).toContain("повторная регистрация отклонена");
    expect(label).toContain("неверный код");
    // Without a reject the plain quarantine label is untouched.
    expect(enrollLabel("quarantined")).toBe(
      "неизвестный инстанс — требуется повторная регистрация",
    );
  });
});

describe("instanceNameError (the name IS the instance id, §6)", () => {
  it("accepts exactly what the service accepts, and refuses the rest locally", () => {
    // The service enforces src/ext/protocol.py INSTANCE_ID_RE and answers
    // enroll_rejected{bad_id}; this check exists so a bad name is refused BEFORE the
    // one-shot window code is spent on a certain refusal. Same table both sides.
    for (const good of ["main", "a", "A".repeat(64), "work-laptop", "Prox.2", "x_y-z.1"]) {
      expect(instanceNameError(good)).toBe(null);
      expect(instanceNameError("  " + good + "  ")).toBe(null); // trimmed like the field
    }
    for (const bad of ["A".repeat(65), "has space", "имя", "a/b", "a:b"]) {
      expect(instanceNameError(bad)).toBe("charset");
    }
    for (const empty of ["", "   ", null, undefined]) {
      expect(instanceNameError(empty)).toBe("empty");
    }
  });

  it("explains the refusal in terms of what to type", () => {
    expect(instanceNameErrorText("charset")).toMatch(/A-Z a-z 0-9/);
    expect(instanceNameErrorText("charset")).toMatch(/no spaces/i);
    expect(instanceNameErrorText("empty")).toMatch(/name/i);
  });
});

// --- DOM wiring: the settings UI feeds the enroll_request -------------------
// The fake element carries `classList` and `setAttribute` because the page marks a
// refused field with them (the red border + aria-invalid, ui.css `.is-invalid`): a stub
// without them would let the page break in the browser and stay green here.
function fakeEl(extra = {}) {
  const classes = new Set();
  return {
    value: "",
    textContent: "",
    checked: false,
    disabled: false,
    attrs: {},
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
      toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
    },
    setAttribute(name, value) {
      this.attrs[name] = value;
    },
    _handlers: {},
    addEventListener(type, fn) {
      this._handlers[type] = fn;
    },
    ...extra,
  };
}

function fakeDoc(ids) {
  const els = {};
  for (const id of ids) els[id] = fakeEl();
  return { els, getElementById: (id) => els[id] };
}

const IDS = [
  "status",
  "allow-execute-js",
  "service-address",
  "service-address-error",
  "browser-name",
  "browser-name-error",
  "enroll-code",
  "enroll-code-error",
  "submit-enroll",
  "enroll-state",
];

// A fake chrome with an in-memory storage.local and a recorded message channel.
function fakeChrome(stored = {}, connectionState = { enrollState: "needs-enroll" }) {
  const sent = [];
  const chromeApi = {
    storage: {
      local: {
        get: async (keys) => {
          const out = {};
          for (const k of Array.isArray(keys) ? keys : [keys]) {
            if (k in stored) out[k] = stored[k];
          }
          return out;
        },
        set: async (obj) => Object.assign(stored, obj),
      },
    },
    runtime: {
      sendMessage: async (msg) => {
        sent.push(msg);
        if (msg.type === "get_connection_state") return connectionState;
        if (msg.type === "submit_enrollment") return { ok: true };
        return null;
      },
    },
  };
  return { chromeApi, sent, stored };
}

describe("options init (§7)", () => {
  it("renders the enroll state, and submit sends submit_enrollment with the code", async () => {
    const doc = fakeDoc(IDS);
    const { chromeApi, sent, stored } = fakeChrome();

    await init(doc, chromeApi);
    expect(doc.els["enroll-state"].textContent).toBe("не зарегистрирован");
    // The pill's dot follows the state: grey until this browser IS enrolled, or it would
    // read green over the words "не зарегистрирован".
    expect(doc.els["enroll-state"].classList.contains("is-enrolled")).toBe(false);

    // The operator types the address + the window code and presses submit → the code
    // feeds the enroll_request via the SW message (and both are persisted).
    doc.els["service-address"].value = "wss://curator.example";
    doc.els["browser-name"].value = "work-laptop";
    doc.els["enroll-code"].value = "WIN-CODE";
    await doc.els["submit-enroll"]._handlers.click();
    const submit = sent.find((m) => m.type === "submit_enrollment");
    expect(submit).toEqual({ type: "submit_enrollment", code: "WIN-CODE" });
    expect(stored.enrollCode).toBe("WIN-CODE");
    expect(stored.serviceAddress).toBe("wss://curator.example");
    // The name rides to the SW through storage, not through the message.
    expect(stored.browserName).toBe("work-laptop");
  });

  it("refuses to submit a name that cannot be an instance id (no message sent)", async () => {
    // The name goes on the wire as `instanceId` and the service refuses it with bad_id —
    // burning the one-shot window code for nothing. The field may hold an unsaved value
    // the change handler already rejected, so submit re-checks it.
    const doc = fakeDoc(IDS);
    const { chromeApi, sent, stored } = fakeChrome();
    await init(doc, chromeApi);
    sent.length = 0;
    doc.els["service-address"].value = "wss://curator.example";
    doc.els["browser-name"].value = "Bob's Chrome";
    doc.els["enroll-code"].value = "WIN-CODE";
    await doc.els["submit-enroll"]._handlers.click();
    expect(sent.find((m) => m.type === "submit_enrollment")).toBeUndefined();
    // The reason is shown AT the field it is about (the page-level status row is for
    // outcomes), together with the red-border marker ui.css paints.
    expect(doc.els["browser-name-error"].textContent).toMatch(/A-Z a-z 0-9/);
    expect(doc.els["browser-name"].classList.contains("is-invalid")).toBe(true);
    expect(stored.enrollCode).toBeUndefined(); // the code is not spent
  });

  it("does NOT store a name that cannot be an instance id, and says why", async () => {
    const doc = fakeDoc(IDS);
    const { chromeApi, stored } = fakeChrome();
    await init(doc, chromeApi);

    doc.els["browser-name"].value = "Bob's Chrome";
    await doc.els["browser-name"]._handlers.change();
    expect(stored.browserName).toBeUndefined(); // never persisted
    expect(doc.els["browser-name-error"].textContent).toMatch(/no spaces/i);
    expect(doc.els["browser-name"].classList.contains("is-invalid")).toBe(true);
    expect(doc.els["browser-name"].attrs["aria-invalid"]).toBe("true");

    doc.els["browser-name"].value = "  bobs-chrome  ";
    await doc.els["browser-name"]._handlers.change();
    expect(stored.browserName).toBe("bobs-chrome"); // trimmed and saved
    expect(doc.els.status.textContent).toBe("Browser name saved");
    // Fixing the value clears BOTH halves of the refusal — a red border left behind over
    // an accepted name is the same lie as a green dot over a refusal.
    expect(doc.els["browser-name-error"].textContent).toBe("");
    expect(doc.els["browser-name"].classList.contains("is-invalid")).toBe(false);
    expect(doc.els["browser-name"].attrs["aria-invalid"]).toBe("false");
  });

  it("repaints the state when the SW records a verdict (storage.onChanged)", async () => {
    // The verdict lands on the SOCKET, in a worker that is not this page: submit returns
    // before it arrives. Without this watcher the refusal reason — the ONLY thing a human
    // ever sees about a refused enrolment — would need a page reload to appear.
    const doc = fakeDoc(IDS);
    const listeners = [];
    const { chromeApi } = fakeChrome({}, { enrollState: "needs-enroll" });
    chromeApi.storage.onChanged = { addListener: (fn) => listeners.push(fn) };
    let state = { enrollState: "needs-enroll" };
    chromeApi.runtime.sendMessage = async (msg) =>
      msg.type === "get_connection_state" ? state : { ok: true };

    await init(doc, chromeApi);
    expect(doc.els["enroll-state"].textContent).toBe("не зарегистрирован");
    expect(listeners).toHaveLength(1);

    // The SW writes the refusal into the durable enroll facts.
    state = { enrollState: "needs-enroll", enrollReject: "id_taken" };
    await listeners[0]({ enrollState: {} }, "local");
    await new Promise((r) => setTimeout(r, 0));
    expect(doc.els["enroll-state"].textContent).toContain("имя уже занято");

    // An unrelated key (or a different area) does not repaint.
    state = { enrollState: "approved" };
    await listeners[0]({ somethingElse: {} }, "local");
    await listeners[0]({ enrollState: {} }, "sync");
    await new Promise((r) => setTimeout(r, 0));
    expect(doc.els["enroll-state"].textContent).toContain("имя уже занято");
    expect(doc.els["enroll-state"].classList.contains("is-enrolled")).toBe(false);

    // …and the repaint that DOES happen carries the dot with it.
    await listeners[0]({ enrollState: {} }, "local");
    await new Promise((r) => setTimeout(r, 0));
    expect(doc.els["enroll-state"].textContent).toBe("активен");
    expect(doc.els["enroll-state"].classList.contains("is-enrolled")).toBe(true);
  });

  it("refuses to submit an empty code (no message sent)", async () => {
    const doc = fakeDoc(IDS);
    const { chromeApi, sent } = fakeChrome();
    await init(doc, chromeApi);
    sent.length = 0; // ignore the initial get_connection_state
    doc.els["service-address"].value = "wss://curator.example";
    doc.els["enroll-code"].value = "   ";
    await doc.els["submit-enroll"]._handlers.click();
    expect(sent.find((m) => m.type === "submit_enrollment")).toBeUndefined();
    expect(doc.els["enroll-code-error"].textContent).toMatch(/code/i);
    expect(doc.els["enroll-code"].classList.contains("is-invalid")).toBe(true);
  });
});

// --- the address gate, as the operator meets it -----------------------------
describe("options: the service address is validated before it is stored (§7)", () => {
  it("does NOT store a plaintext ws:// address and says why", async () => {
    const doc = fakeDoc(IDS);
    const { chromeApi, stored } = fakeChrome();
    await init(doc, chromeApi);

    doc.els["service-address"].value = "ws://curator.lan:8000";
    await doc.els["service-address"]._handlers.change();

    expect(stored.serviceAddress).toBeUndefined(); // never persisted
    // The refusal is red, under the field, and it names both the problem and the fix.
    // (It used to be a sentence in the page-level status row saying the secret "would
    // travel in the clear"; the wording moved to the field with the paragraph about
    // schemes that used to sit above it.)
    expect(doc.els["service-address-error"].textContent).toMatch(/wss:\/\//);
    expect(doc.els["service-address-error"].textContent).toMatch(/unencrypted/i);
    expect(doc.els["service-address"].classList.contains("is-invalid")).toBe(true);
    expect(doc.els["service-address"].attrs["aria-invalid"]).toBe("true");
  });

  it("stores a wss:// address, and a loopback ws:// one (development)", async () => {
    const doc = fakeDoc(IDS);
    const { chromeApi, stored } = fakeChrome();
    await init(doc, chromeApi);

    doc.els["service-address"].value = "  wss://curator.example  ";
    await doc.els["service-address"]._handlers.change();
    expect(stored.serviceAddress).toBe("wss://curator.example"); // trimmed
    expect(doc.els.status.textContent).toBe("Service address saved");
    expect(doc.els["service-address-error"].textContent).toBe("");
    expect(doc.els["service-address"].classList.contains("is-invalid")).toBe(false);

    doc.els["service-address"].value = "ws://localhost:8000";
    await doc.els["service-address"]._handlers.change();
    expect(stored.serviceAddress).toBe("ws://localhost:8000");
  });

  it("accepts a BARE address and stores it with the scheme it derived", async () => {
    // What the owner asked for: the field takes what you would read off a browser bar.
    // The stored value is the full URL the SW dials, and the field shows it back so the
    // operator can SEE which scheme was applied instead of having to ask.
    const doc = fakeDoc(IDS);
    const { chromeApi, stored } = fakeChrome();
    await init(doc, chromeApi);

    doc.els["service-address"].value = "curator.nebula.lc";
    await doc.els["service-address"]._handlers.change();
    expect(stored.serviceAddress).toBe("wss://curator.nebula.lc");
    expect(doc.els["service-address"].value).toBe("wss://curator.nebula.lc");
    expect(doc.els.status.textContent).toBe("Service address saved");

    doc.els["service-address"].value = "curator.nebula.lc:8443";
    await doc.els["service-address"]._handlers.change();
    expect(stored.serviceAddress).toBe("wss://curator.nebula.lc:8443");

    // Loopback keeps the ws:// development exception, typed the same bare way.
    doc.els["service-address"].value = "localhost:8000";
    await doc.els["service-address"]._handlers.change();
    expect(stored.serviceAddress).toBe("ws://localhost:8000");
  });

  it("an address ALREADY stored with its scheme keeps working untouched", async () => {
    // Backward compatibility: the owner's profile holds `wss://…` from before the field
    // stopped demanding a prefix. It must load, validate and re-save identically.
    const doc = fakeDoc(IDS);
    const { chromeApi, stored } = fakeChrome({ serviceAddress: "wss://curator.example:8443" });
    await init(doc, chromeApi);
    expect(doc.els["service-address"].value).toBe("wss://curator.example:8443");
    expect(doc.els.status.textContent).not.toMatch(/Refused/);
    expect(doc.els["service-address-error"].textContent).toBe("");
    expect(doc.els["service-address"].classList.contains("is-invalid")).toBe(false);

    await doc.els["service-address"]._handlers.change();
    expect(stored.serviceAddress).toBe("wss://curator.example:8443");
  });

  it("submitting an enrollment stores the BARE address with its derived scheme", async () => {
    const doc = fakeDoc(IDS);
    const { chromeApi, sent, stored } = fakeChrome();
    await init(doc, chromeApi);

    doc.els["service-address"].value = "curator.nebula.lc";
    doc.els["browser-name"].value = "work-laptop";
    doc.els["enroll-code"].value = "WIN-CODE";
    await doc.els["submit-enroll"]._handlers.click();

    expect(sent.find((m) => m.type === "submit_enrollment")).toBeTruthy();
    expect(stored.serviceAddress).toBe("wss://curator.nebula.lc");
  });

  it("an empty field still clears the setting without shouting", async () => {
    const doc = fakeDoc(IDS);
    const { chromeApi, stored } = fakeChrome({ serviceAddress: "wss://curator.example" });
    await init(doc, chromeApi);

    doc.els["service-address"].value = "   ";
    await doc.els["service-address"]._handlers.change();
    expect(stored.serviceAddress).toBe("");
    expect(doc.els.status.textContent).toBe("Service address cleared");
  });

  it("refuses to submit an enrollment against a refused address", async () => {
    // Submitting here would hand the one-time window code and a fresh secret to a
    // connection that is never made — while the UI happily reports "pending".
    const doc = fakeDoc(IDS);
    const { chromeApi, sent } = fakeChrome();
    await init(doc, chromeApi);
    sent.length = 0;

    doc.els["service-address"].value = "http://curator.example";
    doc.els["enroll-code"].value = "WIN-CODE";
    await doc.els["submit-enroll"]._handlers.click();

    expect(sent.find((m) => m.type === "submit_enrollment")).toBeUndefined();
    expect(doc.els["service-address-error"].textContent).toMatch(/wss:\/\//);
    expect(doc.els["service-address"].classList.contains("is-invalid")).toBe(true);
  });

  it("surfaces an address the SW has already refused (a profile predating the gate)", async () => {
    const doc = fakeDoc(IDS);
    const { chromeApi } = fakeChrome(
      { serviceAddress: "ws://curator.lan:8000" },
      { enrollState: "needs-enroll", hasAddress: false, addressError: "insecure" },
    );
    await init(doc, chromeApi);
    // The field shows the stored value, so "адрес не настроен" would be a lie; the page
    // must name the refusal instead — on the very field holding the refused value.
    expect(doc.els["service-address"].value).toBe("ws://curator.lan:8000");
    expect(doc.els["service-address-error"].textContent).toMatch(/wss:\/\//);
    expect(doc.els["service-address"].classList.contains("is-invalid")).toBe(true);
  });
});
