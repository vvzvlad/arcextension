"""Snapshot application: the single sync ``fn(conn)`` handed to ``Database.write``.

The whole of §6 "Снимок" happens in ONE transaction (Фаза 2 contract: no
``await`` inside): optional session-change clear, UPSERT of every TabInfo, replace
of the window list, the instance-level update, and finally the ``sent_at``-bounded
delete. The three load-bearing invariants live here so a mutation test that
removes one of them reddens a direct call:

* **session-change clear** — a ``tab_id`` is only meaningful inside a session
  (§5); a changed ``sessionId`` means the browser restarted, so all tabs of this
  instance are dropped before the new snapshot is applied.
* **UPSERT, never bare INSERT** — a snapshot may land AFTER a future phase-A
  ``open_tab`` already inserted the copy row; a bare INSERT would hit the PK and
  abort the whole transaction (§6).
* **`sent_at`-bounded delete** — only tabs last written at or before the request's
  server send-time are eligible for deletion; a fresher row (a phase-A copy
  inserted after ``sent_at``) is protected, otherwise the next pass opens a second
  copy and `main` grows unbounded (§6).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from src.ext.protocol import tab_info_to_row

# The SAME ceiling the hello path enforces (``src.ext.channel._MAX_SESSION_ID``), applied
# here because this is the OTHER writer of ``instances.session_id`` and an invariant only
# one writer honours is not an invariant. The hello check was justified by "the mirror
# compares the value verbatim, so it must never be truncated or coerced" — which is a
# statement about the COLUMN, not about one frame type, and snapshots overwrite that column
# on every pass. Duplicated as a literal rather than imported: channel.py imports this
# module, so reaching back for the constant would close an import cycle.
_MAX_SESSION_ID = 200


def _valid_session_id(value: Any) -> bool:
    """Is ``value`` storable in ``instances.session_id``?

    ``None`` IS valid and meaningful — it is how a client reports "no session", and the
    caller treats a change to/from it as a session change. Anything that is not a str, or a
    str past the ceiling, is not a session id at all.
    """
    return value is None or (isinstance(value, str) and len(value) <= _MAX_SESSION_ID)


_UPSERT_TAB = """
INSERT INTO tabs (instance_id, tab_id, window_id, url, title, fav_icon_url,
                  pinned, active, opened_at, last_active_at, age_unknown,
                  self_navigating, audible, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(instance_id, tab_id) DO UPDATE SET
    window_id = excluded.window_id,
    url = excluded.url,
    title = excluded.title,
    fav_icon_url = excluded.fav_icon_url,
    pinned = excluded.pinned,
    active = excluded.active,
    opened_at = excluded.opened_at,
    last_active_at = excluded.last_active_at,
    age_unknown = excluded.age_unknown,
    self_navigating = excluded.self_navigating,
    audible = excluded.audible,
    updated_at = excluded.updated_at
"""


def apply_snapshot(
    conn: sqlite3.Connection,
    instance_id: str,
    snapshot: dict[str, Any],
    sent_at: int,
    now: int,
    expected_epoch: int,
) -> None:
    """Apply one ``snapshot`` frame in the caller's open write transaction.

    ``sent_at`` is the server send-time of the ``snapshot_request`` whose ``id``
    came back (it bounds the delete AND is stored as ``instances.snapshot_at``).
    ``now`` is the current server time (ages are computed against it).
    ``expected_epoch`` is the ``conn_epoch`` the applying socket owns.
    """
    new_session = snapshot.get("sessionId")

    # --- epoch guard (§6): discard a snapshot from a superseded socket -------
    # (the session/window-id hygiene below needs the stored row, so it comes after)
    # Re-check the epoch INSIDE this write transaction (atomic with the writes):
    # a hello that evicted this socket between the channel's synchronous identity
    # check and this write (a point of `await`) would otherwise let a stale
    # snapshot land, keyed by instance_id, and roll snapshot_at back. A mismatch
    # (or a vanished instance) => no-op.
    row = conn.execute(
        "SELECT conn_epoch, session_id FROM instances WHERE id = ?", (instance_id,)
    ).fetchone()
    if row is None or row[0] != expected_epoch:
        return
    stored_session = row[1]

    # --- field hygiene on the two instance-level values (§6) ----------------
    # Same rule as the hello path, on the same columns. A snapshot arrives on an
    # AUTHENTICATED socket, but authenticated is not trusted, and unlike hello this code
    # cannot answer REJECT_PROTOCOL — it runs inside the write transaction, where the only
    # tool is to skip the bad value (exactly what the tab/window loops below do, and for
    # the same reason: a raised sqlite3.InterfaceError would abort the WHOLE snapshot and
    # drop a live instance out of curation until it reconnects).
    #
    # An unusable sessionId is NOT read as "session changed" — that would wipe every tab of
    # the instance on a malformed frame. It carries no information, so the stored session
    # stands and the tab table is left alone.
    if not _valid_session_id(new_session):
        new_session = stored_session
    # focused_window_id is an INTEGER column. A dict/list would reach sqlite3 as a bound
    # parameter it cannot adapt; a bool is not a window id. Unusable => NULL ("no focused
    # window"), which is already the column's benign default.
    focused_window_id = snapshot.get("focusedWindowId")
    if not isinstance(focused_window_id, int) or isinstance(focused_window_id, bool):
        focused_window_id = None

    # --- session-change clear (§5) ------------------------------------------
    if stored_session != new_session:
        # Browser restarted: tab ids from the dead session are meaningless.
        conn.execute("DELETE FROM tabs WHERE instance_id = ?", (instance_id,))

    # --- UPSERT every tab (never a bare INSERT) -----------------------------
    tabs = snapshot.get("tabs") or []
    tab_ids: list[int] = []
    for tab in tabs:
        tab_id = tab.get("tabId")
        # tab_id is INTEGER NOT NULL; a tab with a missing/non-int id cannot be
        # stored, and letting it hit the constraint would abort the WHOLE
        # transaction and drop an authenticated instance out of curation until it
        # reconnects. Skip it instead (bool is not a valid id either).
        if not isinstance(tab_id, int) or isinstance(tab_id, bool):
            continue
        conn.execute(_UPSERT_TAB, tab_info_to_row(instance_id, tab, now))
        tab_ids.append(tab_id)

    # --- replace the window list --------------------------------------------
    # window_id/type are NOT NULL; a malformed (non-int id, missing type) or
    # DUPLICATE window must not hit the constraint / PK and abort the whole
    # snapshot transaction (which would drop the instance out of curation) — skip it.
    conn.execute("DELETE FROM windows WHERE instance_id = ?", (instance_id,))
    seen_windows: set[int] = set()
    for win in snapshot.get("windows") or []:
        win_id = win.get("id")
        win_type = win.get("type")
        if not isinstance(win_id, int) or isinstance(win_id, bool):
            continue
        if not isinstance(win_type, str):
            continue
        if win_id in seen_windows:
            continue
        seen_windows.add(win_id)
        conn.execute(
            "INSERT INTO windows (instance_id, window_id, type, state) "
            "VALUES (?, ?, ?, ?)",
            (instance_id, win_id, win_type, win.get("state")),
        )

    # --- instance-level update ----------------------------------------------
    # snapshot_at is the request's server send-time (sent_at), NOT `now`: the
    # pass keys its readiness off the id it sent, and a user Cmd+T snapshot must
    # not masquerade as a fresher pass state (§6).
    conn.execute(
        "UPDATE instances SET focused_window_id = ?, session_id = ?, "
        "snapshot_at = ?, last_seen_at = ? WHERE id = ?",
        (focused_window_id, new_session, sent_at, now, instance_id),
    )

    # --- `sent_at`-bounded delete (AFTER the upserts) -----------------------
    # Placeholders only — never string-interpolate ids. A tab created after
    # chrome.tabs.query began could not be in the snapshot; the `updated_at <=
    # sent_at` bound spares any row written after the request went out.
    if tab_ids:
        placeholders = ",".join("?" for _ in tab_ids)
        conn.execute(
            f"DELETE FROM tabs WHERE instance_id = ? "
            f"AND tab_id NOT IN ({placeholders}) AND updated_at <= ?",
            (instance_id, *tab_ids, sent_at),
        )
    else:
        # No NOT IN () — that is invalid SQL; an empty snapshot still deletes
        # everything old enough to be in scope.
        conn.execute(
            "DELETE FROM tabs WHERE instance_id = ? AND updated_at <= ?",
            (instance_id, sent_at),
        )
