import { describe, it, expect, vi } from "vitest";
import {
  addressErrorText,
  enrollLabel,
  init,
  installUuidPrefix,
  normalizeServiceAddress as optionsNormalizeServiceAddress,
  serviceAddressError as optionsServiceAddressError,
} from "../pages/options.js";
import {
  normalizeServiceAddress as swNormalizeServiceAddress,
  serviceAddressError as swServiceAddressError,
} from "../src/service-address.js";
import { INSTALL_UUID_PREFIX_LEN } from "../src/constants.js";

const UUID = "d01784bd-a594-4766-a521-b52c4e71c010";

describe("installUuidPrefix (§7 — operator identifies their own request)", () => {
  it("shows enough of installUuid to tell two installs apart, '—' when absent", () => {
    // The /admin list prints the SAME prefix (templates/app.js) — that is the only way to
    // compare them. 8 chars was too few: same person, same browser name, same fleet-wide
    // origin, so the prefix is the ONLY discriminator and it decides who gets a credential.
    expect(installUuidPrefix(UUID)).toBe("d01784bd-a594-4766");
    expect(installUuidPrefix(UUID)).toHaveLength(INSTALL_UUID_PREFIX_LEN);
    expect(installUuidPrefix("")).toBe("—");
    expect(installUuidPrefix(undefined)).toBe("—");
  });
});

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
    ["https://curator.example", "http-scheme"],
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
  it("maps each enroll state to a human label", () => {
    expect(enrollLabel("pending")).toBe("ожидает одобрения");
    expect(enrollLabel("revoked")).toBe("отозван");
    expect(enrollLabel("approved")).toBe("одобрен");
    expect(enrollLabel("needs-enroll")).toBe("не зарегистрирован");
    expect(enrollLabel(null)).toBe("не зарегистрирован");
  });

  it("surfaces an enroll_rejected reason instead of an eternal 'waiting' (§7)", () => {
    // A pending request the server rejected must say WHY, not "ожидает одобрения".
    expect(enrollLabel("pending", "bad_code")).toBe("заявка отклонена: bad_code");
    expect(enrollLabel("needs-enroll", "closed")).toBe("заявка отклонена: closed");
    // An approved instance ignores a stale reject.
    expect(enrollLabel("approved", "bad_code")).toBe("одобрен");
  });

  it("surfaces the reason on the QUARANTINE path, which cannot recover by itself", () => {
    // The regression this pins: a quarantined instance keeps a valid old secret, so
    // getEnrollState answers `quarantined` and never `pending` — gate the reject on
    // pending-only and the operator who typed a wrong code sees the same
    // "требуется повторная регистрация" as before submitting, forever. The staged code was
    // wiped with the reject, so the probe will not retry and only a new code moves this.
    const label = enrollLabel("quarantined", "bad_code");
    expect(label).toContain("заявка отклонена: bad_code");
    expect(label).toContain("новый код");
    // Without a reject the plain quarantine label is untouched.
    expect(enrollLabel("quarantined")).toBe(
      "неизвестный инстанс — требуется повторная регистрация",
    );
  });
});

// --- DOM wiring: the settings UI feeds the enroll_request -------------------
function fakeEl(extra = {}) {
  return {
    value: "",
    textContent: "",
    checked: false,
    disabled: false,
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
  "browser-name",
  "enroll-code",
  "submit-enroll",
  "install-uuid",
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
  it("renders the installUuid prefix + enroll state, and submit sends submit_enrollment with the code", async () => {
    const doc = fakeDoc(IDS);
    const { chromeApi, sent, stored } = fakeChrome({ installUuid: UUID });

    await init(doc, chromeApi);
    // The identity the operator quotes to the admin — the same prefix /admin prints.
    expect(doc.els["install-uuid"].textContent).toBe("d01784bd-a594-4766");
    expect(doc.els["enroll-state"].textContent).toBe("не зарегистрирован");

    // The operator types the address + the window code and presses submit → the code
    // feeds the enroll_request via the SW message (and both are persisted).
    doc.els["service-address"].value = "wss://curator.example";
    doc.els["enroll-code"].value = "WIN-CODE";
    await doc.els["submit-enroll"]._handlers.click();
    const submit = sent.find((m) => m.type === "submit_enrollment");
    expect(submit).toEqual({ type: "submit_enrollment", code: "WIN-CODE" });
    expect(stored.enrollCode).toBe("WIN-CODE");
    expect(stored.serviceAddress).toBe("wss://curator.example");
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
    expect(doc.els.status.textContent).toMatch(/code/i);
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
    expect(doc.els.status.textContent).toMatch(/wss:\/\//);
    expect(doc.els.status.textContent).toMatch(/clear/i); // "…would travel in the clear"
  });

  it("stores a wss:// address, and a loopback ws:// one (development)", async () => {
    const doc = fakeDoc(IDS);
    const { chromeApi, stored } = fakeChrome();
    await init(doc, chromeApi);

    doc.els["service-address"].value = "  wss://curator.example  ";
    await doc.els["service-address"]._handlers.change();
    expect(stored.serviceAddress).toBe("wss://curator.example"); // trimmed
    expect(doc.els.status.textContent).toBe("Service address saved");

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

    await doc.els["service-address"]._handlers.change();
    expect(stored.serviceAddress).toBe("wss://curator.example:8443");
  });

  it("submitting an enrollment stores the BARE address with its derived scheme", async () => {
    const doc = fakeDoc(IDS);
    const { chromeApi, sent, stored } = fakeChrome({ installUuid: UUID });
    await init(doc, chromeApi);

    doc.els["service-address"].value = "curator.nebula.lc";
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
    expect(doc.els.status.textContent).toMatch(/wss:\/\//);
  });

  it("surfaces an address the SW has already refused (a profile predating the gate)", async () => {
    const doc = fakeDoc(IDS);
    const { chromeApi } = fakeChrome(
      { serviceAddress: "ws://curator.lan:8000" },
      { enrollState: "needs-enroll", hasAddress: false, addressError: "insecure" },
    );
    await init(doc, chromeApi);
    // The field shows the stored value, so "адрес не настроен" would be a lie; the page
    // must name the refusal instead.
    expect(doc.els["service-address"].value).toBe("ws://curator.lan:8000");
    expect(doc.els.status.textContent).toMatch(/wss:\/\//);
  });
});
