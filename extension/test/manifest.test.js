import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// The manifest is JSON; assert the enrollment host_permissions change (§7, issue #35).
const manifest = JSON.parse(
  readFileSync(fileURLToPath(new URL("../manifest.json", import.meta.url)), "utf8"),
);

// The permission list is an INSTALL-TIME PROMPT, not an implementation detail: adding an
// entry changes what Chrome asks every operator for on update ("Читать и изменять
// закладки", "Читать историю просмотров"), and a permission that quietly disappears
// takes a whole feature down with it (the startpage's two side columns render empty, and
// nothing anywhere says why). Assert the EXACT set — a subset check would let both
// accidents through.
describe("manifest permissions (§10 startpage columns)", () => {
  it("is exactly the documented set — nothing added, nothing dropped", () => {
    expect([...manifest.permissions].sort()).toEqual(
      ["alarms", "bookmarks", "history", "idle", "scripting", "storage", "tabs"].sort(),
    );
  });

  it("the //permissions comment says why bookmarks/history are here and what they cost", () => {
    // Whoever tightens this list next must be able to read, in place, that these two are
    // the startpage's LOCAL sources (§10) and that they widen the install-time warning.
    const comment = manifest["//permissions"];
    expect(comment).toMatch(/bookmarks/);
    expect(comment).toMatch(/history/);
    expect(comment).toMatch(/§10/);
    expect(comment).toMatch(/install-time warning/i); // says what the operator will see
  });
});

describe("manifest host_permissions (§7)", () => {
  it("drops the two per-<host> patterns and keeps ONLY <all_urls>", () => {
    const hp = manifest.host_permissions;
    // The universal build has no generator to stamp <host>, so the ws/https host
    // patterns are gone; the CORS/execute_js grant rides on <all_urls>.
    expect(hp).toContain("<all_urls>");
    expect(hp.some((p) => p.includes("<host>"))).toBe(false);
    expect(hp.some((p) => p.startsWith("https://"))).toBe(false);
    expect(hp.some((p) => p.startsWith("wss://"))).toBe(false);
    expect(hp).toHaveLength(1);
  });

  it("the //host_permissions comment names BOTH the /api fetch and execute_js", () => {
    // The reworded comment must warn that <all_urls> now also carries the /api fetch —
    // a future tightening that only accounts for execute_js would break the startpage.
    const comment = manifest["//host_permissions"];
    expect(comment).toMatch(/api/i);
    expect(comment).toMatch(/execute_js/);
  });
});
