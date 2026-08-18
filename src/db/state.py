"""Read the ``StateResponse`` mirror for ``GET /api/state`` (§10).

Pure synchronous ``fn(conn)`` bodies runnable from ``Database.read``. The endpoint
returns this straight from the mirror IMMEDIATELY and (separately) kicks a
background single-flight snapshot refresh — this module never does I/O or awaits
(Фаза 2 contract). Every SELECT is static and parameter-free; the shape mirrors
§10's ``StateResponse`` exactly.
"""

from __future__ import annotations

import json
import sqlite3

from src.curator.pause import RESUME_PENDING_KEY, read_stopped_at

# Column lists kept next to their SELECTs so the JSON shape and the SQL never drift
# from §10's StateResponse.
_INSTANCE_COLUMNS = (
    "id",
    "connected",
    "snapshot_at",
    "last_seen_at",
    "reject_reason",
    "reject_at",
    "focused_window_id",
)
_TAB_COLUMNS = (
    "instance_id",
    "tab_id",
    "window_id",
    "url",
    "title",
    "fav_icon_url",
    "pinned",
    "active",
    "audible",
    "last_active_at",
    "age_unknown",
)


# The curated fleet is the ACTIVE fleet (issue #35 §6: the status filter belongs in BOTH
# read surfaces or neither — ``rules.access.known_instance_ids`` and
# ``rules.preview.load_preview_input`` already carry it, and ``/metrics`` now does too).
#
# ``/api/state`` is the mirror the STARTPAGE renders, and a revoked instance is not part
# of it in any sense the page can use: its socket is closed, a jump to its tabs can only
# fail, its mirror can never refresh again, and the row would sit in the status strip
# forever reading "offline for N days" with no way for the human to make it stop. The
# alternative — ship the row with a ``status`` field and let the client hide it — was
# rejected: it needs a client change to avoid exactly that phantom row, and it would leave
# the same dead weight in the tab groups. Administration of retired instances lives in
# ``/admin/instances``, which deliberately lists every status (and is where the operator
# re-approves or inspects them).
_ACTIVE_ONLY = "WHERE status = 'active'"


def _read_instances(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT " + ", ".join(_INSTANCE_COLUMNS) + " FROM instances "
        f"{_ACTIVE_ONLY} ORDER BY id"
    ).fetchall()
    return [
        {
            "id": r["id"],
            "connected": bool(r["connected"]),
            "snapshot_at": r["snapshot_at"],
            "last_seen_at": r["last_seen_at"],
            "reject_reason": r["reject_reason"],
            "reject_at": r["reject_at"],
            "focused_window_id": r["focused_window_id"],
        }
        for r in rows
    ]


def _read_tabs(
    conn: sqlite3.Connection,
    *,
    instance: str | None = None,
    window_id: int | None = None,
    url_contains: str | None = None,
) -> list[dict]:
    """Read the tabs mirror (§10), optionally narrowed by the #46 MCP filters.

    ``_TAB_COLUMNS`` stays the canonical SQL selection (``fav_icon_url`` included) so
    ``build_state`` / ``GET /api/state`` are byte-identical — the MCP adapter drops the
    favicon over the ROWS, not by changing this SQL surface.

    The three filter params are ALL optional (default ``None``): this reader is shared
    with ``build_state``, which calls it with no arguments, so a required param would
    break ``/api/state``. When given they intersect (AND):

    * ``instance``      — exact ``instance_id`` match.
    * ``window_id``     — exact ``window_id`` match. The tabs/windows key is
      ``(instance_id, window_id)``, so a bare window number is ambiguous across
      instances; pass ``instance`` alongside it to name one window unambiguously.
    * ``url_contains``  — case-INsensitive substring of ``url`` (via ``instr`` over
      lowercased operands, so ``%``/``_`` in the needle are literal, not LIKE wildcards).
    """
    conn.row_factory = sqlite3.Row
    # Tabs follow their instance through the SAME filter. Nothing ever deletes a revoked
    # instance's tabs (``apply_snapshot`` is the only writer and it needs a live socket),
    # so leaving them in would render a phantom group on the startpage — full of tabs
    # whose "jump" can only fail. One rule, applied to the whole StateResponse.
    clauses = ["instance_id IN (SELECT id FROM instances WHERE status = 'active')"]
    args: list = []
    if instance is not None:
        clauses.append("instance_id = ?")
        args.append(instance)
    if window_id is not None:
        clauses.append("window_id = ?")
        args.append(window_id)
    if url_contains is not None:
        # instr(lower(url), lower(needle)) > 0 — case-insensitive substring; NULL url
        # yields NULL (falsy), so it is excluded rather than matched.
        clauses.append("instr(lower(url), lower(?)) > 0")
        args.append(url_contains)
    rows = conn.execute(
        "SELECT " + ", ".join(_TAB_COLUMNS) + " FROM tabs "
        "WHERE " + " AND ".join(clauses) + " "
        "ORDER BY instance_id, tab_id",
        args,
    ).fetchall()
    return [
        {
            "instance_id": r["instance_id"],
            "tab_id": r["tab_id"],
            "window_id": r["window_id"],
            "url": r["url"],
            "title": r["title"],
            "fav_icon_url": r["fav_icon_url"],
            "pinned": bool(r["pinned"]),
            "active": bool(r["active"]),
            "audible": bool(r["audible"]),
            "last_active_at": r["last_active_at"],
            "age_unknown": bool(r["age_unknown"]),
        }
        for r in rows
    ]


def _read_windows(conn: sqlite3.Connection) -> list[dict]:
    """One summary record per window (#46 ``list_windows``): ``instance_id``,
    ``window_id``, ``type``, ``state``, ``tab_count`` and a ``focused`` flag.

    Sources are exactly the three the tool advertises: the ``windows`` table, a COUNT
    over ``tabs`` (LEFT JOIN so a window with zero tabs still reports 0), and
    ``instances.focused_window_id`` (the ``focused`` flag). Active fleet only, the same
    status filter the rest of this module applies. A per-window summary — a few hundred
    bytes for a typical fleet — not the per-tab list ``list_tabs`` returns.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT w.instance_id AS instance_id, w.window_id AS window_id, "
        "w.type AS type, w.state AS state, "
        "COUNT(t.tab_id) AS tab_count, "
        "(w.window_id = i.focused_window_id) AS focused "
        "FROM windows w "
        "JOIN instances i ON i.id = w.instance_id "
        "LEFT JOIN tabs t "
        "  ON t.instance_id = w.instance_id AND t.window_id = w.window_id "
        "WHERE i.status = 'active' "
        "GROUP BY w.instance_id, w.window_id, w.type, w.state, i.focused_window_id "
        "ORDER BY w.instance_id, w.window_id"
    ).fetchall()
    return [
        {
            "instance_id": r["instance_id"],
            "window_id": r["window_id"],
            "type": r["type"],
            "state": r["state"],
            "tab_count": r["tab_count"],
            # focused_window_id NULL => the comparison is NULL => not focused.
            "focused": bool(r["focused"]),
        }
        for r in rows
    ]


def _read_active_sessions(conn: sqlite3.Connection) -> dict[str, str | None]:
    """``{instance_id: session_id}`` for every ACTIVE instance (#47 "session epoch").

    Kept OFF :func:`_read_instances` on purpose: the MCP freshness envelope stamps
    ``session_id`` next to each instance so an agent can echo it back as
    ``expected_session``, but the §10 ``StateResponse`` shape (``_read_instances``)
    stays byte-identical. ``session_id`` may be NULL for an instance that never sent a
    usable hello.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT id, session_id FROM instances {_ACTIVE_ONLY}"
    ).fetchall()
    return {r["id"]: r["session_id"] for r in rows}


def _read_capabilities(conn: sqlite3.Connection) -> dict[str, dict]:
    """``{instance_id: {allow_execute_js, ext_version}}`` per ACTIVE instance.

    The §11 capability report: what a copy will ALLOW, as that copy itself declared it in
    its last ``hello``. An agent reads this BEFORE it calls, instead of discovering
    ``js_disabled`` — or a verb an older bundle has never heard of — halfway through a task.
    ``allow_execute_js`` is now the SINGLE JS & Debugger gate (it gates execute_js AND the
    chrome.debugger path); the former ``allow_debugger`` column is gone (migration v6).

    A separate reader for the same reason as :func:`_read_active_sessions`: the §10
    ``StateResponse`` shape (:func:`_read_instances`) must stay byte-identical, so a field
    that only the MCP envelope wants does not go into that projection.

    ``ext_version`` may be NULL for an instance that has not said hello since the column
    landed — genuinely "it has not told us", which is not the same as a version.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, allow_execute_js, ext_version FROM instances "
        f"{_ACTIVE_ONLY}"
    ).fetchall()
    return {
        r["id"]: {
            "allow_execute_js": bool(r["allow_execute_js"]),
            "ext_version": r["ext_version"],
        }
        for r in rows
    }


def _read_quick_links(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, url, title, position FROM quick_links ORDER BY position, id"
    ).fetchall()
    return [
        {"id": r["id"], "url": r["url"], "title": r["title"], "position": r["position"]}
        for r in rows
    ]


def _read_last_pass(conn: sqlite3.Connection) -> tuple[int | None, bool | None]:
    """``(last_pass_at, last_pass_ok)`` from the ``passes`` table (§12: pass facts
    come from ``passes``, never from process memory or ``max(actions.ts)``).

    The most recent pass by ``started_at``; ``last_pass_at`` prefers ``finished_at``
    (a finished pass) and falls back to ``started_at`` (one still running / killed).
    ``last_pass_ok`` is NULL until the pass finished.
    """
    row = conn.execute(
        "SELECT started_at, finished_at, ok FROM passes "
        "ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return (None, None)
    started_at, finished_at, ok = row[0], row[1], row[2]
    last_at = finished_at if finished_at is not None else started_at
    last_ok = None if ok is None else bool(ok)
    return (last_at, last_ok)


def _read_rule_counts(conn: sqlite3.Connection) -> tuple[int, int]:
    """``(rules_total, rules_invalid)`` — the §10/§12 pair; invalid rules are
    excluded from matching but still counted (an orphaned target is invalid)."""
    total = int(conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0])
    invalid = int(
        conn.execute("SELECT COUNT(*) FROM rules WHERE invalid = 1").fetchone()[0]
    )
    return (total, invalid)


def _parse_pending_plan(raw) -> dict | None:
    """The deferred pass plan the runner stashed in ``settings.resume_pending``.

    The runner writes ``json.dumps({"since": <ms>, "plan": {...}})`` — the plan of the
    over-threshold pass that armed the latch (§7, MAX_ACTIONS_PER_PASS), refreshed by
    every subsequent pass. Reducing it to a bare boolean, as ``resume_pending`` does,
    throws away exactly the thing §7 says the human confirms the burst BY: «план …
    выводится в статус-полосу», so the click is informed rather than blind. Returned
    VERBATIM as parsed (``{"since": …, "plan": {relocations, closures, deferred, total,
    threshold, examples…}}``) so no field is lost on the way to the status row.

    Anything unparseable / non-object => ``None``: a malformed latch must degrade to
    "no plan to show", never to a 500 on every ``/api/state``.
    """
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def build_state(conn: sqlite3.Connection, server_now: int) -> dict:
    """Assemble the full ``StateResponse`` (§10) from the mirror in ONE reader
    connection. ``server_now`` is stamped by the caller (server clock)."""
    last_pass_at, last_pass_ok = _read_last_pass(conn)
    rules_total, rules_invalid = _read_rule_counts(conn)
    # Stop visibility (§7 "видимость обязательна"): the startpage renders a "stopped
    # since" row from ``stopped_at`` (null = running — the stop is indefinite, there is
    # no deadline to count down), and the over-threshold ``resume_pending`` latch rides
    # along next to it so the click is informed.
    resume_row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (RESUME_PENDING_KEY,)
    ).fetchone()
    resume_raw = resume_row[0] if resume_row is not None else None
    return {
        "server_now": server_now,
        "last_pass_at": last_pass_at,
        "last_pass_ok": last_pass_ok,
        "rules_total": rules_total,
        "rules_invalid": rules_invalid,
        "stopped_at": read_stopped_at(conn),
        # The boolean stays EXACTLY as it was (clients are built on it); the plan is a
        # new, additive field next to it.
        "resume_pending": bool(resume_raw),
        "pending_plan": _parse_pending_plan(resume_raw),
        "instances": _read_instances(conn),
        "tabs": _read_tabs(conn),
        "quick_links": _read_quick_links(conn),
    }


def connected_snapshot_ages(conn: sqlite3.Connection) -> dict[str, int | None]:
    """``{instance_id: snapshot_at}`` for every ACTIVE, ``connected=1`` instance — the
    input to the single-flight staleness decision in the endpoint (§10). ``snapshot_at``
    may be NULL (connected, never snapshotted) → always stale.

    The status filter is the same rule the rest of this module follows: refreshing the
    mirror of an instance that is no longer curated is work whose result nothing reads.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, snapshot_at FROM instances WHERE connected = 1 AND status = 'active'"
    ).fetchall()
    return {r["id"]: r["snapshot_at"] for r in rows}
