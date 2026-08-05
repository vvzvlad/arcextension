import { describe, it, expect } from "vitest";

import {
  cleanTitle,
  groupHistoryByDay,
  groupTabsByWindow,
  hostOf,
  plural,
  windowLabel,
  windowOrdinals,
} from "../src/lib/bookmarks.js";

// --- cleanTitle: the destructive-heuristic regression suite -------------------
// The whole reason this function is a LITERAL suffix list and not a regex over
// "- / : |" is Russian titles: those characters are content there. Every case below
// that expects the title UNCHANGED is a title the obvious regex mangles.
describe("cleanTitle", () => {
  it("cuts a known site tail and keeps the rest verbatim", () => {
    expect(cleanTitle("Команда Obsidian выпустила веб-клипер / Habr", "https://habr.com/ru/news/1/"))
      .toBe("Команда Obsidian выпустила веб-клипер");
    expect(cleanTitle("Причуды науки — Posmotrelisu", "https://posmotre.li/x")).toBe(
      "Причуды науки",
    );
    expect(cleanTitle("Never Gonna Give You Up - YouTube", "https://youtube.com/watch?v=1")).toBe(
      "Never Gonna Give You Up",
    );
  });

  it("NEVER splits on a bare separator inside the title", () => {
    // The killer case: slashes and colons are content here, and a "split on the last
    // ' / '" heuristic would leave "Семь дней".
    const series = "Семь дней / Seven Days / Сезон: 1 / Серии: 1-21";
    expect(cleanTitle(series, "https://posmotre.li/series")).toBe(series);
    // A hyphen inside a Russian compound is not a separator either.
    const clipper = "Команда Obsidian выпустила веб-клипер";
    expect(cleanTitle(clipper, "https://habr.com/ru/news/1/")).toBe(clipper);
    const colon = "Курс: изготовление глазурей";
    expect(cleanTitle(colon, "https://murtille.livejournal.com/1")).toBe(colon);
  });

  it("matches the suffix case-insensitively but only as a whole tail", () => {
    expect(cleanTitle("Кот и его дом - youtube", "https://youtube.com/x")).toBe("Кот и его дом");
    // The same words NOT at the end are content, not a tail.
    expect(cleanTitle("YouTube против авторов", "https://habr.com/x")).toBe(
      "YouTube против авторов",
    );
  });

  it("refuses to cut when what is left is not a title anymore", () => {
    // Remainder of 3 chars or less: the "cleanup" would be the destructive part.
    expect(cleanTitle("Кот - YouTube", "https://youtube.com/x")).toBe("Кот - YouTube");
  });

  it("falls back to the host for an empty, echoing or numeric title", () => {
    expect(cleanTitle("", "https://www.example.com/a")).toBe("example.com");
    expect(cleanTitle(null, "https://example.com/a")).toBe("example.com");
    expect(cleanTitle("example.com", "https://example.com/a")).toBe("example.com");
    expect(cleanTitle("EXAMPLE.COM", "https://example.com/a")).toBe("example.com");
    // A bare address says less than the host does.
    expect(cleanTitle("10.20.30.226:8080", "http://10.20.30.226:8080/panel")).toBe(
      "10.20.30.226:8080",
    );
    expect(cleanTitle("192.168.1.1", "https://router.lan/")).toBe("router.lan");
  });

  it("survives a missing/unparsable url", () => {
    expect(cleanTitle("Заголовок", undefined)).toBe("Заголовок");
    // No title AND no parsable host: the url string itself is the last resort, because
    // the alternative is a clickable row with NO TEXT AT ALL (see below).
    expect(cleanTitle("", "not a url")).toBe("not a url");
    // Genuinely nothing to show is the only case that may answer "".
    expect(cleanTitle("", undefined)).toBe("");
    expect(cleanTitle(null, null)).toBe("");
  });

  // A url with no HOST is not exotic — local files, about:, chrome:// and data: all
  // have one. Before the url fallback these rendered as a link with an empty label: a
  // row that occupies space, highlights on hover, and says nothing.
  it("NEVER returns an empty label while there is a url (host-less schemes)", () => {
    expect(cleanTitle("", "file:///Users/x/doc.pdf")).toBe("file:///Users/x/doc.pdf");
    expect(cleanTitle(null, "about:blank")).toBe("about:blank");
    expect(cleanTitle(undefined, "data:text/plain,hi")).toBe("data:text/plain,hi");
    expect(cleanTitle("   ", "file:///tmp/a.txt")).toBe("file:///tmp/a.txt");
    // A real title still wins over the url, host or no host.
    expect(cleanTitle("Отчёт", "file:///Users/x/doc.pdf")).toBe("Отчёт");
  });
});

describe("hostOf", () => {
  it("drops the www. prefix and keeps the port", () => {
    expect(hostOf("https://www.example.com/a?b=1")).toBe("example.com");
    expect(hostOf("http://10.20.30.226:8080/x")).toBe("10.20.30.226:8080");
  });
  it("answers '' for anything unparsable", () => {
    expect(hostOf("not a url")).toBe("");
    expect(hostOf(null)).toBe("");
  });
});

// --- plural: the 11-14 exception is the one everybody gets wrong ---------------
describe("plural", () => {
  const tab = (n) => plural(n, "вкладка", "вкладки", "вкладок");

  it("agrees with the last digit", () => {
    expect(tab(1)).toBe("вкладка");
    expect(tab(2)).toBe("вкладки");
    expect(tab(4)).toBe("вкладки");
    expect(tab(5)).toBe("вкладок");
    expect(tab(0)).toBe("вкладок");
    expect(tab(21)).toBe("вкладка");
    expect(tab(22)).toBe("вкладки");
    expect(tab(177)).toBe("вкладок");
  });

  it("puts 11-14 in the 'many' form regardless of the last digit", () => {
    expect(tab(11)).toBe("вкладок");
    expect(tab(12)).toBe("вкладок");
    expect(tab(13)).toBe("вкладок");
    expect(tab(14)).toBe("вкладок");
    expect(tab(111)).toBe("вкладок");
    expect(tab(112)).toBe("вкладок");
  });

  it("tolerates junk input", () => {
    expect(tab(null)).toBe("вкладок");
    expect(tab(-1)).toBe("вкладка");
  });
});

// --- window grouping + labels -------------------------------------------------
describe("groupTabsByWindow", () => {
  const tab = (id, windowId, url) => ({ tab_id: id, window_id: windowId, url, title: "t" + id });

  it("keeps the browser's window order and counts each window", () => {
    const groups = groupTabsByWindow([
      tab(1, 7, "https://a.com/1"),
      tab(2, 9, "https://b.com/1"),
      tab(3, 7, "https://a.com/2"),
    ]);
    expect(groups.map((g) => g.windowId)).toEqual([7, 9]);
    expect(groups[0].count).toBe(2);
    expect(groups[0].tabs.map((t) => t.tab_id)).toEqual([1, 3]);
    expect(groups[1].count).toBe(1);
  });

  it("names the dominant host when one owns half the window (and it has >2 tabs)", () => {
    const groups = groupTabsByWindow([
      tab(1, 1, "https://www.avito.ru/a"),
      tab(2, 1, "https://avito.ru/b"),
      tab(3, 1, "https://avito.ru/c"),
      tab(4, 1, "https://other.com/d"),
    ]);
    expect(groups[0].label).toBe("avito.ru — 3 из 4");
  });

  it("lists the top three hosts when there is no majority", () => {
    const groups = groupTabsByWindow([
      tab(1, 1, "https://a.com/1"),
      tab(2, 1, "https://b.com/1"),
      tab(3, 1, "https://c.com/1"),
      tab(4, 1, "https://d.com/1"),
    ]);
    expect(groups[0].label).toBe("a.com · b.com · c.com");
  });

  it("does not claim a majority in a two-tab window", () => {
    // 1 of 2 is technically 50%, but "a.com — 1 из 2" is noise, not information.
    expect(windowLabel([tab(1, 1, "https://a.com/1"), tab(2, 1, "https://b.com/1")])).toBe(
      "a.com · b.com",
    );
  });

  it("groups tabs with no window id together and survives host-less urls", () => {
    // about:blank and a malformed url have no host at all: the label degrades to
    // empty instead of printing "undefined" into a section header.
    const groups = groupTabsByWindow([
      { tab_id: 1, url: "about:blank" },
      { tab_id: 2, url: "not a url" },
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].count).toBe(2);
    expect(groups[0].label).toBe("");
  });

  it("answers an empty list for no tabs", () => {
    expect(groupTabsByWindow([])).toEqual([]);
    expect(groupTabsByWindow(undefined)).toEqual([]);
  });

  // The window NUMBER is a property of the window, not of the filtered view. Number the
  // sections by their position in the filtered list and the headers renumber themselves
  // on every keystroke in the search box — "Окно 4" becomes "Окно 2" and stops naming
  // anything the human can find.
  it("keeps each window's number stable when the list is filtered", () => {
    const all = [
      tab(1, 7, "https://a.com/1"),
      tab(2, 9, "https://b.com/1"),
      tab(3, 11, "https://habr.com/1"),
    ];
    const ordinals = windowOrdinals(all);
    expect([...ordinals.values()]).toEqual([1, 2, 3]);

    // The search box leaves only the third window's tab: it must still be "Окно 3".
    const filtered = groupTabsByWindow([all[2]], ordinals);
    expect(filtered).toHaveLength(1);
    expect(filtered[0].ordinal).toBe(3);
    // …and unfiltered, the numbering is the browser's own window order.
    expect(groupTabsByWindow(all, ordinals).map((g) => g.ordinal)).toEqual([1, 2, 3]);
  });

  it("numbers windows by their own order when no ordinals are supplied", () => {
    const groups = groupTabsByWindow([tab(1, 7, "https://a.com/1"), tab(2, 9, "https://b.com/1")]);
    expect(groups.map((g) => g.ordinal)).toEqual([1, 2]);
    // Tabs with no window id share the null group and still get a number.
    expect(groupTabsByWindow([{ tab_id: 1, url: "about:blank" }])[0].ordinal).toBe(1);
  });
});

// --- history day grouping -----------------------------------------------------
describe("groupHistoryByDay", () => {
  const NOW = new Date(2026, 7, 5, 15, 0, 0).getTime(); // 5 Aug 2026, local
  const at = (dayOffset, hour) =>
    new Date(2026, 7, 5 - dayOffset, hour, 0, 0).getTime();

  it("labels today and yesterday by name and older days by date", () => {
    const groups = groupHistoryByDay(
      [
        { url: "https://a/1", title: "A", lastVisitTime: at(0, 14) },
        { url: "https://b/1", title: "B", lastVisitTime: at(1, 21) },
        { url: "https://c/1", title: "C", lastVisitTime: at(3, 9) },
      ],
      NOW,
    );
    expect(groups.map((g) => g.label)).toEqual(["Сегодня", "Вчера", "02.08"]);
  });

  it("orders days newest first and rows newest first inside a day", () => {
    const groups = groupHistoryByDay(
      [
        { url: "https://old/1", lastVisitTime: at(2, 10) },
        { url: "https://new/1", lastVisitTime: at(0, 9) },
        { url: "https://new/2", lastVisitTime: at(0, 18) },
      ],
      NOW,
    );
    expect(groups[0].label).toBe("Сегодня");
    expect(groups[0].items.map((h) => h.url)).toEqual(["https://new/2", "https://new/1"]);
    expect(groups[1].items).toHaveLength(1);
  });

  it("sinks entries with no timestamp to the bottom instead of dropping them", () => {
    const groups = groupHistoryByDay(
      [{ url: "https://x/1" }, { url: "https://y/1", lastVisitTime: at(0, 12) }],
      NOW,
    );
    expect(groups[0].label).toBe("Сегодня");
    expect(groups[groups.length - 1].label).toBe("Ранее");
    expect(groups[groups.length - 1].items[0].url).toBe("https://x/1");
  });

  it("answers an empty list for no history", () => {
    expect(groupHistoryByDay([], NOW)).toEqual([]);
    expect(groupHistoryByDay(undefined, NOW)).toEqual([]);
  });
});
