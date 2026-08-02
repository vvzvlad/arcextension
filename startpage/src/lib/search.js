// Local search — a substring filter, NOT a server query (§10). Case-insensitive over
// title + url; the caller groups the survivors by instance. Pure and tiny.

export function matchesQuery(item, query) {
  if (!query) return true;
  const q = String(query).toLowerCase();
  const title = String((item && item.title) || "").toLowerCase();
  const url = String((item && item.url) || "").toLowerCase();
  return title.includes(q) || url.includes(q);
}
