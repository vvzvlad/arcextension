// Vite build for the newtab startpage (§10).
//
// THREE load-bearing choices (each guards one "silently empty page", §10):
//   * base: './'         — assets referenced RELATIVE, so `<script src="./assets/…">`
//                          resolves under chrome-extension://<id>/startpage/ instead
//                          of the extension root (default '/' => 404 => blank page).
//   * @vitejs/plugin-vue — precompiles SFCs to render functions at BUILD time, so the
//                          runtime compiler (which needs `new Function` => blocked by
//                          `script-src 'self'`) is never bundled. No string `template:`.
//   * outDir ../extension/startpage — the built page ships INSIDE the extension; all
//                          assets are local (strict CSP `script-src 'self'`).
//
// The Vite MAJOR is pinned in package.json; the gate greps are re-measured on it
// (§10 — a wrong gate is silently always-green/red).

import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  base: "./",
  plugins: [vue()],
  build: {
    outDir: fileURLToPath(new URL("../extension/startpage", import.meta.url)),
    emptyOutDir: true,
    // One entry chunk keeps the built-dist render smoke test importable without
    // chasing split vendor chunks; the app is tiny so there is nothing to gain
    // from splitting.
    rollupOptions: {
      output: { manualChunks: undefined },
    },
  },
  test: {
    // happy-dom gives the render smoke test + component tests a real DOM; the mock
    // chrome + fetch are injected per test (never a network or a real extension).
    environment: "happy-dom",
    include: ["test/**/*.test.js"],
  },
});
