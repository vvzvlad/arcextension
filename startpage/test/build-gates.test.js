import { describe, it, expect, beforeAll } from "vitest";
import { readFileSync, readdirSync, statSync, existsSync } from "node:fs";
import { join } from "node:path";

// The THREE "silently empty page" gates (§10), MEASURED ON THE BUILT DIST produced
// by the pinned Vite major. Requires a prior `npm run build` (the Makefile / CI
// sequence builds first). Gate 2 (a runtime string `template:` is not greppable) is
// covered ONLY by the render smoke test below — the authoritative gate.

// vitest runs with cwd = the startpage package dir; the built dist is a sibling of
// this package under ../extension/startpage. (import.meta.url is not a file:// URL
// under vitest's transform, so cwd is the reliable anchor.)
const DIST = join(process.cwd(), "..", "extension", "startpage");

function walk(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...walk(p));
    else out.push(p);
  }
  return out;
}

describe("build gates on the pinned Vite major (§10)", () => {
  beforeAll(() => {
    if (!existsSync(join(DIST, "index.html"))) {
      throw new Error(
        "startpage is not built — run `npm run build` first (dist missing at " + DIST + ")",
      );
    }
  });

  it("gate 1: no runtime compiler — no (new )?Function( / eval( in the bundle", () => {
    // Equivalent to: grep -rE "(new +)?Function\(|eval\(" dist/. On the 8.x
    // rolldown/oxc bundler the minifier can drop `new`, so the regex allows bare
    // `Function(` too (§10).
    const re = /(new +)?Function\(|eval\(/;
    const hits = walk(DIST).filter(
      (f) => /\.(js|mjs|cjs)$/.test(f) && re.test(readFileSync(f, "utf8")),
    );
    expect(hits).toEqual([]);
  });

  it('gate 3: base "./" — index.html references ./assets', () => {
    // Equivalent to: grep -q 'src="\./assets' dist/index.html
    const html = readFileSync(join(DIST, "index.html"), "utf8");
    expect(/src="\.\/assets/.test(html)).toBe(true);
  });

  it("gate 2 / authoritative: the built page renders a NON-EMPTY root", async () => {
    // Give the built app a minimal chrome + an offline fetch so it mounts without a
    // real extension or network; the first paint is local-only, so the root is
    // populated even though init/refresh find nothing.
    globalThis.chrome = {
      runtime: { getURL: (p) => "chrome-extension://mock/" + p, sendMessage: async () => null },
      tabs: {
        query: async () => [],
        getCurrent: async () => ({ id: 1 }),
        update: async () => {},
        remove: async () => {},
      },
      windows: { update: async () => {} },
      storage: { local: { get: async () => ({}), set: async () => {} } },
    };
    globalThis.fetch = async () => {
      throw new Error("offline");
    };
    document.body.innerHTML = '<div id="app"></div>';

    // Execute the built ENTRY named in index.html in this happy-dom global, letting
    // it mount('#app'). The bundle is a self-contained script (no import/export), so
    // the test harness runs it directly — this is the test executing the ALREADY
    // BUILT bundle, not the shipped page using a runtime compiler (that is exactly
    // what gate 1 forbids IN the bundle, and gate 1 above proves it is absent).
    const html = readFileSync(join(DIST, "index.html"), "utf8");
    const m = html.match(/src="(\.\/assets\/[^"]+\.js)"/);
    expect(m).toBeTruthy();
    const entry = join(DIST, m[1].replace("./", ""));
    const code = readFileSync(entry, "utf8");
    // eslint-disable-next-line no-new-func -- harness executes the pre-built bundle
    new Function(code)();

    const root = document.getElementById("app");
    // NON-EMPTY: the whole point of the gate. If the runtime compiler were needed
    // (string template) or base were '/', the built page would render blank here.
    expect(root.innerHTML.trim().length).toBeGreaterThan(0);
    expect(root.querySelector('[data-role="status-bar"]')).toBeTruthy();
  });
});
