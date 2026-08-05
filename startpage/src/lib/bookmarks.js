// Pure helpers for the three startpage columns (§10): title cleanup, host extraction,
// Russian plurals and the window/day groupings the column headers are built from.
// No chrome.*, no DOM, no clock of its own — everything here is a plain function, so
// every label on the page is unit-testable without mounting anything.

// Site tails a browser-supplied title carries. This is a STRICT LITERAL LIST on
// purpose. The obvious heuristic — "split on the last ' - ', ' / ', ' : ' or ' | '" —
// destroys Russian titles, because there those characters are CONTENT, not a
// separator: "Команда Obsidian выпустила веб-клипер / Habr" would lose the word
// "веб-клипер", and "Семь дней / Seven Days / Сезон: 1 / Серии: 1-21" would be cut
// down to "Семь дней". Only an exact, case-insensitive tail from this list is removed,
// and only when what is left still reads as a title.
const SITE_SUFFIXES = [
  " - YouTube",
  " / Habr",
  " — Википедия",
  " - Google Search",
  " — LiveJournal",
  " — Posmotrelisu",
  " — Викитропы",
  " — Хакер",
  " | Пикабу",
];

// The shortest remainder we still accept as a title. Below it the "cleanup" would be
// the destructive part (a two-letter stub is worse than the original tail).
const MIN_TITLE_LENGTH = 3;

// A title that is nothing but digits, dots and colons is an address, not a name —
// "10.20.30.226:8080" tells the human less than the host does.
const NUMERIC_TITLE = /^[\d.:]+$/;

// Host without the "www." noise. An unparsable/absent url yields "" — callers treat
// that as "no host to show" rather than crashing a whole column.
export function hostOf(url) {
  try {
    return new URL(String(url)).host.replace(/^www\./, "");
  } catch {
    return "";
  }
}

// The title as a human wants to read it in a dense one-line row.
//
// NEVER answers "" while there is a url: a row is a LINK, and a link with no text is
// an invisible click target. Plenty of real urls have no host at all — file:///…,
// about:blank, chrome://newtab, data: — so "fall back to the host" is not a complete
// answer; the url itself is the last resort (this is what the old
// `{{ t.title || t.url }}` template did, and losing it drew blank rows).
//
// `knownHost` lets a caller that ALREADY parsed the url pass the host in instead of
// paying for a second `new URL` per row — the startpage's row views do exactly that
// (App.vue rowView), which is why the whole tab column is one parse per row, not two.
export function cleanTitle(title, url, knownHost = null) {
  const host = knownHost == null ? hostOf(url) : knownHost;
  const raw = String(title == null ? "" : title).trim();
  const asUrl = String(url == null ? "" : url).trim();
  // Empty / host-echo / bare address: the host IS the most informative thing we have.
  if (!raw) return host || asUrl;
  if (host && raw.toLowerCase() === host.toLowerCase()) return host;
  if (NUMERIC_TITLE.test(raw)) return host || raw;
  const lower = raw.toLowerCase();
  for (const suffix of SITE_SUFFIXES) {
    if (!lower.endsWith(suffix.toLowerCase())) continue;
    const cut = raw.slice(0, raw.length - suffix.length).trim();
    if (cut.length > MIN_TITLE_LENGTH) return cut;
  }
  return raw;
}

// Russian plural agreement: 11-14 take the "many" form regardless of the last digit
// (11 вкладок, not 11 вкладка), then the last digit decides.
export function plural(n, one, few, many) {
  const abs = Math.abs(Math.trunc(Number(n) || 0));
  const mod100 = abs % 100;
  if (mod100 >= 11 && mod100 <= 14) return many;
  const mod10 = abs % 10;
  if (mod10 === 1) return one;
  if (mod10 >= 2 && mod10 <= 4) return few;
  return many;
}

// The label under a window's header. A window is usually ABOUT something — a research
// binge on one site — and saying so is worth more than three hostnames: with a clear
// majority (half the tabs or more, and enough tabs for "majority" to mean anything)
// the header names the site and how much of the window it owns. Otherwise it lists
// the top hosts and lets the human recognise the window by its mix.
export function windowLabel(tabs) {
  const list = tabs || [];
  const counts = new Map();
  for (const t of list) {
    const host = hostOf(t && t.url);
    if (!host) continue;
    counts.set(host, (counts.get(host) || 0) + 1);
  }
  if (counts.size === 0) return "";
  // Stable sort: equal counts keep first-seen order, so the label does not flicker
  // between two equally sized hosts on every refresh.
  const ranked = [...counts.entries()].sort((a, b) => b[1] - a[1]);
  const [topHost, topCount] = ranked[0];
  if (list.length > 2 && topCount / list.length >= 0.5) {
    return `${topHost} — ${topCount} из ${list.length}`;
  }
  return ranked
    .slice(0, 3)
    .map(([host]) => host)
    .join(" · ");
}

// The window numbering shown in the column headers, derived ONCE from the UNFILTERED
// tab list. A window's number is a property of the window, not of what the search box
// happens to be showing: number the groups by their position in the filtered list and
// typing "habr" renames "Окно 4" to "Окно 2" under the human's eyes, so the label stops
// identifying anything. Pass the result to groupTabsByWindow as `ordinals`.
export function windowOrdinals(tabs) {
  const ordinals = new Map();
  for (const t of tabs || []) {
    const key = t && t.window_id != null ? t.window_id : null;
    if (!ordinals.has(key)) ordinals.set(key, ordinals.size + 1);
  }
  return ordinals;
}

// Own tabs grouped into the windows they actually live in (§9/§10): the tab column is
// a list of window sections, and a window is the unit the human moves things between.
// Insertion order is preserved — chrome.tabs.query returns tabs in window/index order,
// so the sections come out in the same order the browser shows them.
//
// `ordinals` (see windowOrdinals) supplies the STABLE window number for the header.
// Without it the numbering falls back to this list's own order, which is correct only
// when the list is unfiltered.
export function groupTabsByWindow(tabs, ordinals = null) {
  const byWindow = new Map();
  for (const t of tabs || []) {
    const key = t && t.window_id != null ? t.window_id : null;
    if (!byWindow.has(key)) byWindow.set(key, []);
    byWindow.get(key).push(t);
  }
  const groups = [];
  for (const [windowId, list] of byWindow) {
    const fallback = groups.length + 1;
    const ordinal = ordinals && ordinals.has(windowId) ? ordinals.get(windowId) : fallback;
    groups.push({
      windowId,
      ordinal,
      tabs: list,
      count: list.length,
      label: windowLabel(list),
    });
  }
  return groups;
}

const DAY_MS = 24 * 60 * 60 * 1000;

function dayKey(ms) {
  const d = new Date(ms);
  return `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`;
}

// History grouped by LOCAL calendar day, newest first — the history column's sections.
// `now` is passed in (never read from a global clock) so the "Сегодня"/"Вчера" labels
// are deterministic in tests and the caller decides which clock is authoritative.
export function groupHistoryByDay(items, now = Date.now()) {
  const today = dayKey(now);
  const yesterday = dayKey(now - DAY_MS);
  const byKey = new Map();
  for (const h of items || []) {
    const ts = h && typeof h.lastVisitTime === "number" ? h.lastVisitTime : null;
    const key = ts == null ? "unknown" : dayKey(ts);
    if (!byKey.has(key)) byKey.set(key, { key, items: [], newest: ts });
    const group = byKey.get(key);
    group.items.push(h);
    if (ts != null && (group.newest == null || ts > group.newest)) group.newest = ts;
  }
  const groups = [...byKey.values()].map((g) => ({
    ...g,
    label: dayLabel(g.key, g.newest, today, yesterday),
  }));
  // Newest day first; entries with no timestamp sink to the bottom.
  groups.sort((a, b) => (b.newest ?? -Infinity) - (a.newest ?? -Infinity));
  for (const g of groups) {
    g.items.sort((a, b) => (b.lastVisitTime ?? -Infinity) - (a.lastVisitTime ?? -Infinity));
  }
  return groups;
}

function dayLabel(key, newest, today, yesterday) {
  if (key === today) return "Сегодня";
  if (key === yesterday) return "Вчера";
  if (newest == null) return "Ранее";
  const d = new Date(newest);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getDate())}.${pad(d.getMonth() + 1)}`;
}
