"""The frozen mirror the pass decides against (§7).

The pass captures the world ONCE, after step 3's snapshots land, into these plain
in-memory structures and makes ALL decisions against them. Deciding against a
frozen copy (not re-reading the DB) is what gives two guarantees for free:

* **A foreign snapshot landing mid-pass cannot change a decision** (§6/§7): a human
  Cmd+T updates the ``tabs`` table, but the pass reads its captured mirror, so the
  instance is not re-evaluated and not ejected.
* **Phase-A copies created THIS pass are excluded from step 7 AND step 8** (§7): the
  copies do not exist at capture time, so the pure decision never sees them — no
  same-pass "open then immediately dedup/singleton-collapse", no re-open loop.

Every field mirrors a §4 column. Loading is one reader ``fn(conn)``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TabRow:
    instance_id: str
    tab_id: int
    window_id: int | None
    url: str | None
    title: str | None
    pinned: int
    active: int
    audible: int
    opened_at: int
    last_active_at: int
    age_unknown: int


@dataclass(frozen=True)
class InstanceRow:
    id: str
    connected: int
    focused_window_id: int | None
    session_id: str | None
    snapshot_at: int | None
    conn_epoch: int


@dataclass(frozen=True)
class RelocRow:
    """A live phase-A ``relocate`` row (§7): its copy is opened, phase B pending."""

    id: int
    instance_from: str
    session_id_from: str | None
    tab_id: int | None            # source hint only (discard changes it, §5)
    instance_to: str
    session_id_to: str | None
    tab_id_to: int | None
    url: str | None               # full source URL at phase-A time
    url_norm: str | None
    rule_id: int | None
    rule_pattern: str | None
    src_opened_at: int | None
    src_last_active_at: int | None
    src_age_unknown: int


@dataclass
class Mirror:
    tabs: list = field(default_factory=list)
    instances: dict = field(default_factory=dict)      # id -> InstanceRow
    windows: dict = field(default_factory=dict)        # (inst, win) -> (type, state)
    rules: list = field(default_factory=list)          # sqlite3.Row list
    exemptions: list = field(default_factory=list)     # (inst, url, until)
    quarantine: list = field(default_factory=list)     # (inst, url, until)
    live_relocations: list = field(default_factory=list)  # RelocRow list

    def tabs_of(self, instance_id: str) -> list:
        return [t for t in self.tabs if t.instance_id == instance_id]


def load_mirror(conn: sqlite3.Connection) -> Mirror:
    """Read the whole world into a :class:`Mirror` (a reader ``fn(conn)``)."""
    conn.row_factory = sqlite3.Row
    tabs = [
        TabRow(
            instance_id=r["instance_id"],
            tab_id=r["tab_id"],
            window_id=r["window_id"],
            url=r["url"],
            title=r["title"],
            pinned=r["pinned"],
            active=r["active"],
            audible=r["audible"],
            opened_at=r["opened_at"],
            last_active_at=r["last_active_at"],
            age_unknown=r["age_unknown"],
        )
        for r in conn.execute(
            "SELECT instance_id, tab_id, window_id, url, title, pinned, active, "
            "audible, opened_at, last_active_at, age_unknown FROM tabs"
        ).fetchall()
    ]
    instances = {
        r["id"]: InstanceRow(
            id=r["id"],
            connected=r["connected"],
            focused_window_id=r["focused_window_id"],
            session_id=r["session_id"],
            snapshot_at=r["snapshot_at"],
            conn_epoch=r["conn_epoch"],
        )
        for r in conn.execute(
            "SELECT id, connected, focused_window_id, session_id, snapshot_at, "
            "conn_epoch FROM instances"
        ).fetchall()
    }
    windows = {
        (r["instance_id"], r["window_id"]): (r["type"], r["state"])
        for r in conn.execute(
            "SELECT instance_id, window_id, type, state FROM windows"
        ).fetchall()
    }
    rules = conn.execute(
        "SELECT id, pattern, instance_id, singleton, invalid FROM rules"
    ).fetchall()
    exemptions = [
        (r["instance_id"], r["url"], r["until"])
        for r in conn.execute(
            "SELECT instance_id, url, until FROM exemptions"
        ).fetchall()
    ]
    quarantine = [
        (r["instance_id"], r["url"], r["until"])
        for r in conn.execute(
            "SELECT instance_id, url, until FROM quarantine"
        ).fetchall()
    ]

    # Live relocate rows: phase A done, not restored, phase B NOT yet completed, AND
    # — the §7 liveness rule — BOTH sessions still match their instances' current
    # sessions. A session change already wiped that instance's `tabs` (§5), so a dead
    # row's join simply finds no copy; we filter here so phase B never chases one.
    #
    # "Phase B NOT yet completed" = no SUCCESSFUL `relocate_close` references this
    # relocate. A completed relocation's source is already closed, so without this
    # exclusion the done relocate row re-enters `live_relocations` next pass,
    # `decide` finds no source (closed) and wrongly marks the SUCCESS `abandoned` —
    # corrupting the §10 journal/metrics and letting the stale row capture a new
    # same-URL tab.
    #
    # ⚠️ ONLY a `status='done'` relocate_close retires the row. Phase B writes a
    # `relocate_close status='failed'` on a `precondition_failed` (the source turned
    # active/pinned/audible between snapshot and close); that is a TRANSIENT retry,
    # not a completion. Retiring on a failed close would drop the relocation identity
    # and re-route the source through normal `decide` — and if the copy's url drifted
    # (Grafana slug / OAuth nonce, §7:1000-1004) the full-url dedup misses and phase A
    # opens a SECOND copy. Leaving a failed row live lets phase B retry by `tab_id_to`
    # (drift-resistant: get_tab by id, not url). Indexed by actions_origin (§4).
    live_relocations = []
    for r in conn.execute(
        "SELECT id, instance_from, session_id_from, tab_id, instance_to, "
        "session_id_to, tab_id_to, url, url_norm, rule_id, rule_pattern, "
        "src_opened_at, src_last_active_at, src_age_unknown "
        "FROM actions a WHERE a.kind = 'relocate' AND a.status = 'done' "
        "AND a.restored_at IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM actions rc "
        "WHERE rc.kind = 'relocate_close' AND rc.status = 'done' "
        "AND rc.origin_action_id = a.id)"
    ).fetchall():
        src = instances.get(r["instance_from"])
        dst = instances.get(r["instance_to"])
        if src is None or dst is None:
            continue
        if src.session_id != r["session_id_from"]:
            continue
        if dst.session_id != r["session_id_to"]:
            continue
        live_relocations.append(
            RelocRow(
                id=r["id"],
                instance_from=r["instance_from"],
                session_id_from=r["session_id_from"],
                tab_id=r["tab_id"],
                instance_to=r["instance_to"],
                session_id_to=r["session_id_to"],
                tab_id_to=r["tab_id_to"],
                url=r["url"],
                url_norm=r["url_norm"],
                rule_id=r["rule_id"],
                rule_pattern=r["rule_pattern"],
                src_opened_at=r["src_opened_at"],
                src_last_active_at=r["src_last_active_at"],
                src_age_unknown=r["src_age_unknown"],
            )
        )
    return Mirror(
        tabs=tabs,
        instances=instances,
        windows=windows,
        rules=rules,
        exemptions=exemptions,
        quarantine=quarantine,
        live_relocations=live_relocations,
    )
