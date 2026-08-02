// The three "silently empty page" build gates (§10), run over the BUILT dist
// (../extension/startpage). These are the fast greps; the AUTHORITATIVE gate is the
// render smoke test (test/build-gates.test.js) — it covers all three, including
// gate 2 (a string `template:` at runtime is not greppable).
//
// Re-measured on the PINNED Vite major (see package.json). §10 warns the 6.4.3
// numbers do not transfer: 6.4.3 emitted `new Function(`, 8.x's rolldown/oxc emits
// bare `Function(` — hence the `(new +)?` in gate 1's regex (`-E`, escaped paren).

import { readFileSync, readdirSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

const DIST = fileURLToPath(new URL("../../extension/startpage", import.meta.url));

function walk(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...walk(p));
    else out.push(p);
  }
  return out;
}

const failures = [];

// --- Gate 1: no runtime compiler (`new Function(` / `Function(` / `eval(`) -----
// Equivalent to: grep -rE "(new +)?Function\(|eval\(" dist/
const RUNTIME_COMPILER = /(new +)?Function\(|eval\(/;
let gate1Hits = [];
for (const file of walk(DIST)) {
  if (!/\.(js|mjs|cjs)$/.test(file)) continue;
  const text = readFileSync(file, "utf8");
  if (RUNTIME_COMPILER.test(text)) gate1Hits.push(file);
}
if (gate1Hits.length) {
  failures.push(
    `GATE 1 (no runtime compiler): matched (new )?Function(/eval( in:\n  ` +
      gate1Hits.join("\n  "),
  );
}

// --- Gate 3: base './' — assets referenced RELATIVE in index.html --------------
// Equivalent to: grep -q 'src="\./assets' dist/index.html
const indexHtml = readFileSync(join(DIST, "index.html"), "utf8");
if (!/src="\.\/assets/.test(indexHtml)) {
  failures.push(
    'GATE 3 (base "./"): index.html has no `src="./assets...` — a default base "/" ' +
      "would 404 under chrome-extension://",
  );
}

if (failures.length) {
  console.error("BUILD GATES FAILED:\n\n" + failures.join("\n\n"));
  process.exit(1);
}
console.log(
  "BUILD GATES PASSED (§10):\n" +
    "  gate 1 — no (new )?Function(/eval( in the bundle\n" +
    '  gate 3 — index.html references ./assets (base "./")\n' +
    "  gate 2 — covered by the render smoke test (test/build-gates.test.js)",
);
