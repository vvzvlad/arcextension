// Quick-link op application — the OPTIMISTIC mirror of the server semantics (§10).
//
// Enqueuing an op must edit the shown quick_links IMMEDIATELY (§10 "Постановка в
// очередь сразу правит кэш"): an offline-added link would otherwise vanish until
// the next successful flush (days). These pure functions apply one op the same way
// the server does — append position, url upsert, explicit reorder — so the page and
// the eventual server state agree. Kept pure for unit tests.

export function nextPosition(list) {
  return list.reduce((max, l) => Math.max(max, l.position ?? -1), -1) + 1;
}

export function sortQuickLinks(list) {
  return [...list].sort((a, b) => (a.position ?? 0) - (b.position ?? 0));
}

// Apply ONE op to a quick-links array, returning a NEW array (never mutates input).
export function applyOpToQuickLinks(list, op) {
  const links = list.map((l) => ({ ...l }));
  if (!op || typeof op !== "object") return links;

  if (op.op === "add") {
    if (!op.url) return links;
    const existing = links.find((l) => l.url === op.url);
    if (existing) {
      // url is UNIQUE: re-add is last-write-wins on the title, keep the position.
      if (op.title !== undefined) existing.title = op.title;
      return links;
    }
    // Server-side position is APPEND; a pending link carries no server id yet.
    links.push({
      id: op.id ?? null,
      url: op.url,
      title: op.title ?? null,
      position: nextPosition(links),
      pending: true,
    });
    return links;
  }

  if (op.op === "remove") {
    return links.filter((l) => (op.id != null ? l.id !== op.id : l.url !== op.url));
  }

  if (op.op === "reorder") {
    if (!Array.isArray(op.order)) return links;
    const byId = new Map(links.map((l) => [l.id, l]));
    const ordered = [];
    op.order.forEach((id, i) => {
      const l = byId.get(id);
      if (l) {
        l.position = i;
        ordered.push(l);
        byId.delete(id);
      }
    });
    for (const l of byId.values()) ordered.push(l); // links absent from `order`
    return ordered;
  }

  return links;
}
