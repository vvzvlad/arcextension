"""Synchronous ``fn(conn)`` bodies for the ``rules`` table (§8).

Every mutating function here runs INSIDE one ``Database.write`` transaction (Фаза 2
contract: no ``await``, one BEGIN/COMMIT). All SQL is parameterized; no id is ever
string-interpolated. Reads are plain callables usable from ``Database.read`` too.

The ``rules`` schema (§4) is::

    id INTEGER PRIMARY KEY AUTOINCREMENT, pattern TEXT NOT NULL,
    instance_id TEXT NOT NULL, singleton INTEGER DEFAULT 0, canonical_url TEXT,
    note TEXT, invalid INTEGER DEFAULT 0, created_at INTEGER NOT NULL
"""

from __future__ import annotations

import sqlite3

from src.rules.matcher import InvalidPattern, compile_pattern

_RULE_COLUMNS = (
    "id",
    "pattern",
    "instance_id",
    "singleton",
    "canonical_url",
    "note",
    "invalid",
    "created_at",
)
_SELECT = "SELECT " + ", ".join(_RULE_COLUMNS) + " FROM rules"


def _rows(conn: sqlite3.Connection):
    conn.row_factory = sqlite3.Row


def list_rules(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All rules ordered by id (deterministic; the ladder tie-breaks on id anyway)."""
    _rows(conn)
    return conn.execute(_SELECT + " ORDER BY id").fetchall()


def get_rule(conn: sqlite3.Connection, rule_id: int) -> sqlite3.Row | None:
    _rows(conn)
    return conn.execute(_SELECT + " WHERE id = ?", (rule_id,)).fetchone()


def insert_rule(
    conn: sqlite3.Connection,
    *,
    pattern: str,
    instance_id: str,
    singleton: bool = False,
    canonical_url: str | None = None,
    note: str | None = None,
    created_at: int,
) -> int:
    """Insert one rule; return its new id. Raises :class:`InvalidPattern` if the
    pattern is not the ``hostPattern[:port]`` grammar — the caller turns that into a
    422 (§8 "validation at save")."""
    compile_pattern(pattern)  # save-time gate (§8)
    cur = conn.execute(
        "INSERT INTO rules (pattern, instance_id, singleton, canonical_url, note, "
        "invalid, created_at) VALUES (?, ?, ?, ?, ?, 0, ?)",
        (pattern, instance_id, 1 if singleton else 0, canonical_url, note, created_at),
    )
    return int(cur.lastrowid)


def update_rule(
    conn: sqlite3.Connection,
    rule_id: int,
    *,
    pattern: str,
    instance_id: str,
    singleton: bool = False,
    canonical_url: str | None = None,
    note: str | None = None,
) -> None:
    """Update a rule in place. A valid pattern clears ``invalid`` (the editor just
    fixed it); an invalid pattern raises before any write (§8)."""
    compile_pattern(pattern)
    conn.execute(
        "UPDATE rules SET pattern = ?, instance_id = ?, singleton = ?, "
        "canonical_url = ?, note = ?, invalid = 0 WHERE id = ?",
        (pattern, instance_id, 1 if singleton else 0, canonical_url, note, rule_id),
    )


def delete_rule(conn: sqlite3.Connection, rule_id: int) -> None:
    conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))


def known_instance_ids(conn: sqlite3.Connection) -> set[str]:
    """Ids of ACTIVE instances (issue #35 §6).

    Filtered to ``status='active'`` so a revoked/pending instance is no longer a legal
    rule/exemption target: its rules go ``invalid=1`` on the next ``revalidate_rules`` and
    a new rule/exemption pointing at it is rejected at save. Its THREE consumers each
    exempt the configured MAIN INDEPENDENTLY (``curator._revalidate_rules`` unions
    ``{main}``; ``api.rules`` and ``api.exemptions`` check ``!= main``), so a legal
    ``X -> main`` rule stays valid even when MAIN has no active row — the
    ``curator-main-instance-never-seen`` / unruled-drain cascade is preserved."""
    return {
        r[0]
        for r in conn.execute(
            "SELECT id FROM instances WHERE status = 'active'"
        ).fetchall()
    }


def count_rules(conn: sqlite3.Connection) -> int:
    """Total rules (curator_rules_total, §12)."""
    return int(conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0])


def count_invalid(conn: sqlite3.Connection) -> int:
    """Rules flagged invalid (curator_rules_invalid, §12)."""
    return int(conn.execute("SELECT COUNT(*) FROM rules WHERE invalid = 1").fetchone()[0])


def revalidate_rules(conn: sqlite3.Connection, known_instance_ids: set[str]) -> int:
    """Mark rules ``invalid=1`` whose target instance is gone or whose pattern no
    longer compiles; clear the flag for rules that are once again valid. Returns the
    number of rows whose ``invalid`` value changed (§8, §12).

    This is the "validated on every pass" half of §8/§12: retiring/deleting an
    instance orphans its rules, and a stored pattern can stop compiling — both must
    end up ``invalid=1`` (excluded from matching, counted, surfaced in the editor)
    without ever crashing a pass.
    """
    _rows(conn)
    changed = 0
    for row in conn.execute("SELECT id, pattern, instance_id, invalid FROM rules"):
        should_be_invalid = row["instance_id"] not in known_instance_ids
        if not should_be_invalid:
            try:
                compile_pattern(row["pattern"])
            except InvalidPattern:
                should_be_invalid = True
        new_flag = 1 if should_be_invalid else 0
        if new_flag != row["invalid"]:
            conn.execute(
                "UPDATE rules SET invalid = ? WHERE id = ?", (new_flag, row["id"])
            )
            changed += 1
    return changed
