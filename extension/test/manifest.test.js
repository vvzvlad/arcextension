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

// Extension icons (§10). Chrome does NOT accept SVG here, so the shipped artefact is a
// set of PNGs rendered from icons/icon.svg. Two ways this breaks silently and neither
// shows up in any other check: a manifest entry pointing at a file that was never
// committed (Chrome falls back to a grey puzzle piece and logs nothing the operator
// reads), and a PNG whose real pixel size does not match the key it is filed under
// (Chrome rescales it and the toolbar icon turns to mush at 16px, which is the one size
// the design was chosen for). Assert both against the bytes on disk.
describe("extension icons", () => {
  const iconDir = fileURLToPath(new URL("../", import.meta.url));

  // Minimal PNG IHDR reader: bytes 16..24 of a PNG are width and height, big-endian.
  function pngSize(relPath) {
    const buf = readFileSync(iconDir + relPath);
    expect(buf.subarray(0, 8)).toEqual(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]));
    return { width: buf.readUInt32BE(16), height: buf.readUInt32BE(20) };
  }

  it("declares the four sizes Chrome asks for, and each file exists at that size", () => {
    expect(Object.keys(manifest.icons).sort()).toEqual(["128", "16", "32", "48"]);
    for (const [size, relPath] of Object.entries(manifest.icons)) {
      expect(pngSize(relPath)).toEqual({ width: Number(size), height: Number(size) });
    }
  });

  it("the toolbar action icon exists at its declared sizes too", () => {
    for (const [size, relPath] of Object.entries(manifest.action.default_icon)) {
      expect(pngSize(relPath)).toEqual({ width: Number(size), height: Number(size) });
    }
  });

  it("keeps the SVG the PNGs are rendered from", () => {
    // Not shipped to Chrome — kept so the raster set can be regenerated instead of
    // redrawn. Losing it means the next size change starts from a screenshot.
    expect(readFileSync(iconDir + "icons/icon.svg", "utf8")).toContain("<svg");
  });
});
