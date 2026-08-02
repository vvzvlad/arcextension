import { describe, it, expect, vi } from "vitest";
import { firstEight, enrollLabel, init } from "../pages/options.js";

describe("firstEight (§7 — operator identifies their own request)", () => {
  it("returns the first 8 chars of installUuid, '—' when absent", () => {
    expect(firstEight("d01784bd-a594-4766-a521-b52c4e71c010")).toBe("d01784bd");
    expect(firstEight("")).toBe("—");
    expect(firstEight(undefined)).toBe("—");
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

describe("options init (§7)", () => {
  it("renders the installUuid first-8 + enroll state, and submit sends submit_enrollment with the code", async () => {
    const doc = fakeDoc(IDS);
    const stored = { installUuid: "d01784bd-a594-4766-a521-b52c4e71c010" };
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
          if (msg.type === "get_connection_state") return { enrollState: "needs-enroll" };
          if (msg.type === "submit_enrollment") return { ok: true };
          return null;
        },
      },
    };

    await init(doc, chromeApi);
    // installUuid first-8 rendered (the operator quotes it to the admin).
    expect(doc.els["install-uuid"].textContent).toBe("d01784bd");
    expect(doc.els["enroll-state"].textContent).toBe("не зарегистрирован");

    // The operator types the window code and presses submit → the code feeds the
    // enroll_request via the SW message (and is persisted).
    doc.els["enroll-code"].value = "WIN-CODE";
    await doc.els["submit-enroll"]._handlers.click();
    const submit = sent.find((m) => m.type === "submit_enrollment");
    expect(submit).toEqual({ type: "submit_enrollment", code: "WIN-CODE" });
    expect(stored.enrollCode).toBe("WIN-CODE");
  });

  it("refuses to submit an empty code (no message sent)", async () => {
    const doc = fakeDoc(IDS);
    const sent = [];
    const chromeApi = {
      storage: { local: { get: async () => ({}), set: async () => {} } },
      runtime: { sendMessage: async (m) => (sent.push(m), null) },
    };
    await init(doc, chromeApi);
    sent.length = 0; // ignore the initial get_connection_state
    doc.els["enroll-code"].value = "   ";
    await doc.els["submit-enroll"]._handlers.click();
    expect(sent.find((m) => m.type === "submit_enrollment")).toBeUndefined();
    expect(doc.els.status.textContent).toMatch(/code/i);
  });
});
