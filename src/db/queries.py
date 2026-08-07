"""Synchronous ``fn(conn)`` bodies for ``Database.write`` used by the /ext channel.

Every function here is a plain synchronous callable that runs INSIDE one
``Database.write`` transaction (Фаза 2 contract: no ``await``, one BEGIN/COMMIT).
They are kept out of ``channel.py`` so the exact SQL of the two load-bearing
invariants — the epoch-guarded disconnect UPDATE and the hello upsert that reads
its own new ``conn_epoch`` back in the same transaction — is in one place and
mutation-testable.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass


def sha256_hex(raw_secret: str) -> str:
    """sha256 of a RAW secret as lowercase hex — the ONE shared server-side hasher.

    Credential model (issue #35, option A): the client sends its RAW secret over TLS;
    the server hashes it HERE, both when storing it at enroll and when resolving it at
    every hello / ``/api`` Bearer. Only the sha256 is ever persisted (``secret_hash``
    columns), so a DB-only leak yields hashes, not usable credentials. Using the SAME
    function at store and at resolve is what guarantees they always match.
    """
    return hashlib.sha256(raw_secret.encode("utf-8")).hexdigest()


class RevokeMainRefused(Exception):
    """Revoking the configured MAIN requires ``replacement == MAIN_INSTANCE_ID`` (§5).

    ``replacement`` is a PRECONDITION on the CURRENT env MAIN, not a runtime override:
    there is nowhere to persist a new MAIN — MAIN_INSTANCE_ID is env config the
    service cannot rewrite. Task E maps this refusal to HTTP 409 (acceptance 9).
    """


@dataclass
class RevokeResult:
    """Outcome of :func:`revoke_instance` — enough for the caller (Task E) to know it
    succeeded and then close the live socket best-effort AFTER commit (async, outside
    the txn); ``instance_id`` is how the /admin handler finds the registry entry."""

    instance_id: str
    revoked: bool   # a row transitioned to 'revoked' this call (rowcount > 0)
    was_main: bool  # the target was the configured MAIN (a guarded revoke)


# Revoke in ONE transaction (§5). session_id=NULL is REQUIRED: today mark_disconnected
# does NOT clear it, so the mirror keeps treating a relocation as live forever
# (mirror.load_mirror keeps a relocate live only while both endpoints' sessions still
# match — a NULL session drops it out of live_relocations at once). The status='revoked'
# itself is the retire INTENT the pass scans for.
#
# connected/focused_window_id are cleared HERE as well, not left to the caller's
# best-effort socket close: that close is a no-op when there is no registry entry
# (``admin._close_live_socket`` returns early), so revoking an OFFLINE-but-stale
# instance — the row was left ``connected=1`` by a process kill, which is exactly the
# state a crash leaves behind — used to keep the flag set FOREVER. Nothing else ever
# clears it: ``mark_disconnected`` is epoch-guarded and only ever runs from a socket
# finalizer, and there is no socket. A revoked instance must never read as connected on
# any surface (/metrics' half-open-socket rule, /api/state, the console), so the revoke
# transaction owns the flag. A LIVE socket's own finalizer later runs the epoch-guarded
# UPDATE against the same (unchanged) epoch and simply writes 0 again — idempotent.
_REVOKE_UPDATE = """
UPDATE instances SET
    status = 'revoked',
    revoked_at = ?,
    session_id = NULL,
    connected = 0,
    focused_window_id = NULL
WHERE id = ?
"""


def revoke_instance(
    conn: sqlite3.Connection,
    instance_id: str,
    *,
    now: int,
    main_instance_id: str,
    replacement: str | None = None,
) -> RevokeResult:
    """Revoke ``instance_id`` in ONE transaction (issue #35 §5). Sync ``fn(conn)``.

    Sets ``status='revoked'``, ``revoked_at=now`` and — REQUIRED — ``session_id=NULL``
    (see ``_REVOKE_UPDATE``). Records NO relocation retirement here: the curator PASS's
    retire step (runner) scans for the ``status='revoked'`` intent and marks the live
    relocations ``abandoned`` — so this handler does NO async I/O and touches no ``actions``
    rows (contract: async I/O outside the txn).

    MAIN guard: revoking the configured MAIN is refused unless ``replacement`` equals the
    current ``main_instance_id``; raises :class:`RevokeMainRefused` (Task E → 409) BEFORE
    any write. A non-main revoke ignores ``replacement``.

    The live-socket close is left to the caller AFTER commit; :class:`RevokeResult`
    carries ``instance_id`` so the /admin handler can look up the registry and close it.
    ``revoked=False`` means no such row existed (the caller maps that to 404).
    """
    is_main = instance_id == main_instance_id
    if is_main and replacement != main_instance_id:
        raise RevokeMainRefused(
            f"revoking MAIN ({instance_id!r}) requires replacement == the current "
            f"MAIN_INSTANCE_ID; got {replacement!r}"
        )
    cur = conn.execute(_REVOKE_UPDATE, (now, instance_id))
    return RevokeResult(
        instance_id=instance_id, revoked=cur.rowcount > 0, was_main=is_main
    )


def instance_status(conn: sqlite3.Connection, instance_id: str) -> str | None:
    """Return the instance's ``status`` ('active' | 'revoked' | 'pending') or ``None``
    when no row exists. Used by :func:`src.ext.commands.send_command` as the §5 revoke
    safety net: a command to a non-active target fails like a dead connection."""
    row = conn.execute(
        "SELECT status FROM instances WHERE id = ?", (instance_id,)
    ).fetchone()
    return None if row is None else row[0]


# hello success: bump an ALREADY-ENROLLED instance row and return the NEW conn_epoch.
# UPDATE-only (§3): a hello NEVER creates a row — the row is created by :func:`enroll_instance`
# from an enroll_request that carried a valid code into an OPEN window, with
# status='active' and a secret_hash; an anon who only knows the public PROTOCOL_VERSION
# must not be able to conjure an `instances` row. A reconnect does conn_epoch+1, sets
# connected=1 and clears the reject fields. The ``AND status='active'`` guard means a hello
# for a revoked/absent id matches nothing (the channel resolves the secret first, but the
# row can vanish or be revoked between resolve and this write — the None-guard below
# handles that race).
_HELLO_UPSERT = """
UPDATE instances SET
    conn_epoch = conn_epoch + 1,
    connected = 1,
    session_id = ?,
    allow_execute_js = ?,
    last_seen_at = ?,
    reject_reason = NULL,
    reject_at = NULL
WHERE id = ? AND status = 'active'
"""


def hello_upsert(
    conn: sqlite3.Connection,
    instance_id: str,
    session_id: str | None,
    allow_execute_js: bool,
    now: int,
) -> int | None:
    """Register a successful hello on an already-active row; return its new
    ``conn_epoch``, or ``None`` if no active row exists.

    No longer creates rows (§3): a hello for a non-active id (deleted / revoked /
    never-approved) updates nothing. The subsequent ``SELECT`` then reads back ``None``,
    which is returned as a sentinel so the caller rejects cleanly instead of doing
    ``int(None)`` — the row may have been revoked/deleted between the channel's
    ``resolve_secret`` and this write.
    """
    conn.execute(
        _HELLO_UPSERT,
        (session_id, 1 if allow_execute_js else 0, now, instance_id),
    )
    row = conn.execute(
        "SELECT conn_epoch FROM instances WHERE id = ? AND status = 'active'",
        (instance_id,),
    ).fetchone()
    if row is None:
        return None
    return int(row[0])


# Record the reason of the most recent rejection. UPDATE-only (§3): a rejection for an
# id with no existing row is a SILENT no-op. This is the point of dropping the INSERT
# branch — an anon knowing only the public ``PROTOCOL_VERSION`` (no valid secret) can no
# longer flood the `instances` table with junk rows via rejected hellos. For an existing
# row ONLY the reject fields are touched — connected / conn_epoch / focused_window_id /
# status are left exactly as they are, so recording a duplicate/revoked rejection never
# disturbs the live socket that already owns the id (§6).
_RECORD_REJECTION = """
UPDATE instances SET reject_reason = ?, reject_at = ? WHERE id = ?
"""


def record_rejection(
    conn: sqlite3.Connection, instance_id: str, reason: str, now: int
) -> None:
    conn.execute(_RECORD_REJECTION, (reason, now, instance_id))


def resolve_secret(
    conn: sqlite3.Connection, raw_secret: str
) -> tuple[str, str] | None:
    """Resolve a RAW secret to ``(instance_id, status)`` by hashing it, or ``None``.

    Option A credential model (issue #35): the client sends the RAW secret over TLS; the
    server hashes it HERE (:func:`sha256_hex`) and matches the stored ``secret_hash`` via
    the UNIQUE ``instances_secret_hash`` index (§1). Because the DB stores only the sha256,
    a leak of the DB yields hashes, not usable secrets — presenting the stored hash value
    itself hashes to something else and matches nothing. ``None`` means no instance carries
    this secret at all (the client is unknown / not yet approved). The status is returned
    raw ('active' / 'revoked' / 'pending') so the channel can map it to the right
    client-facing verdict (unknown vs revoked, §7).
    """
    row = conn.execute(
        "SELECT id, status FROM instances WHERE secret_hash = ?",
        (sha256_hex(raw_secret),),
    ).fetchone()
    if row is None:
        return None
    return (row[0], row[1])


def count_active_instances(conn: sqlite3.Connection) -> int:
    """Number of approved (active) instances. Exposed for completeness / later use."""
    row = conn.execute(
        "SELECT COUNT(*) FROM instances WHERE status = 'active'"
    ).fetchone()
    return int(row[0])


# --- enrolment: the open window IS the permission (§6) -----------------------
# Create-or-REACTIVATE the instance row the enroll_request asked for, in ONE statement.
# A brand-new id INSERTs; an EXISTING revoked id is UPDATEd back to 'active' — that is the
# ONLY path that restores a revoked MAIN (migration 2 retires every pre-enrolment row).
# The ``WHERE instances.status != 'active'`` guard makes an enrolment onto an ALREADY-active
# id a no-op (rowcount 0 → ENROLL_ID_TAKEN → the refusal the client shows), so an enrolment
# never silently overwrites a live instance — the ONE objection the retired approval step
# actually answered. ``conn_epoch`` / ``connected`` are LEFT untouched on the update path so
# a reactivation does not disturb a socket that somehow still holds the id.
#
# This is verbatim the semantics of the removed ``_APPROVE_UPSERT``; only the actor changed
# (the channel, inside the open window, instead of an /admin call afterwards) and the
# ``title`` column it also wrote is gone.
_ENROLL_UPSERT = """
INSERT INTO instances (id, status, secret_hash, install_uuid, enrolled_at)
VALUES (?, 'active', ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    status = 'active',
    secret_hash = excluded.secret_hash,
    install_uuid = excluded.install_uuid,
    enrolled_at = excluded.enrolled_at
WHERE instances.status != 'active'
"""

# Outcomes of :func:`enroll_instance`. Strings rather than a bool because the caller maps
# each onto a different client-facing verdict.
ENROLL_OK = "ok"
ENROLL_ID_TAKEN = "id_taken"
ENROLL_SECRET_TAKEN = "secret_taken"


def enroll_instance(
    conn: sqlite3.Connection,
    *,
    instance_id: str,
    secret_hash: str,
    install_uuid: str,
    now: int,
) -> str:
    """Enrol ``instance_id`` with ``secret_hash`` in ONE write txn; return the outcome.

    Returns :data:`ENROLL_OK`, :data:`ENROLL_ID_TAKEN` (the id belongs to a LIVE instance)
    or :data:`ENROLL_SECRET_TAKEN` (a DIFFERENT active id already carries this secret — the
    ``UNIQUE(secret_hash)`` index of §1, caught here rather than let out as a raw
    ``IntegrityError``, because the channel has to answer a frame either way).

    Both guards are decided INSIDE the caller's ``Database.write`` transaction, which is
    what makes two browsers racing on the same name resolve to exactly one active row: a
    pre-read in its own transaction could never be authoritative.

    ``ENROLL_SECRET_TAKEN`` is not reachable by an honest client — a secret is 32 random
    bytes generated per install — so it has no reason string of its own on the wire; the
    channel maps it onto the same ``id_taken`` refusal, since from the peer's side the
    identity it asked for is likewise unavailable.
    """
    try:
        cur = conn.execute(
            _ENROLL_UPSERT, (instance_id, secret_hash, install_uuid, now)
        )
    except sqlite3.IntegrityError:
        # UNIQUE(secret_hash): another active instance already carries this secret.
        return ENROLL_SECRET_TAKEN
    if cur.rowcount == 0:
        # The id exists and is already 'active' — refuse rather than clobber it.
        return ENROLL_ID_TAKEN
    return ENROLL_OK


_LIST_INSTANCES = """
SELECT id, status, connected, last_seen_at, enrolled_at, revoked_at
FROM instances
ORDER BY id
"""


def list_instances(conn: sqlite3.Connection) -> list[dict]:
    """Return EVERY instance (all statuses) for the operator console.

    Unlike the curator's ``status='active'`` reads, this lists revoked rows too so the
    operator can see a revoked MAIN awaiting re-enrolment or a retired browser. There is
    no separate display name: the id IS the name (§6).
    """
    rows = conn.execute(_LIST_INSTANCES).fetchall()
    return [
        {
            "id": r[0],
            "status": r[1],
            # revoke clears status/session but leaves the `connected` column to the
            # channel's async socket teardown (not guaranteed if there is no live socket),
            # so report `connected` only for an ACTIVE row — a revoked instance is
            # never "connected" for the operator console, whatever the stale flag says.
            "connected": bool(r[2]) and r[1] == "active",
            "last_seen_at": r[3],
            "enrolled_at": r[4],
            "revoked_at": r[5],
        }
        for r in rows
    ]


def insert_admin_audit(
    conn: sqlite3.Connection,
    *,
    now: int,
    action: str,
    initiator: str,
    install_uuid: str | None = None,
    instance_id: str | None = None,
    detail: str | None = None,
) -> int:
    """Append one ``admin_audit`` row (approve / reject / revoke / window_open …).

    The security trail of operator/admin actions (schema §1) — deliberately OUTSIDE
    retention, so it is never swept. ``initiator`` names who acted, and THIS is the
    live vocabulary — the column comment in ``_V2_STATEMENTS`` is frozen at what
    migration step 2 shipped and cannot be corrected in place:

    * ``admin``  — an ADMIN_TOKEN caller: the console over ``/admin``, and equally curl or
      the MCP agent presenting that token to ``POST /api/enroll/window``. The token is the
      identity here, not the door;
    * ``system`` — the service's own enrollment writes (the ``enroll`` row a browser's
      accepted ``enroll_request`` produces, which no human issued);
    * ``user``   — an enrolled instance acting as the human at the keyboard, i.e. the
      startpage button behind ``POST /api/enroll/window`` (§13).

    ``admin`` and ``user`` both write ``window_open``, so the initiator is the one signal
    telling a window armed by the ADMIN_TOKEN apart from one armed by a browser's own
    secret (:func:`src.api.guards.initiator_for`). Returns the new row id.
    """
    cur = conn.execute(
        "INSERT INTO admin_audit (ts, action, install_uuid, instance_id, initiator, detail) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (now, action, install_uuid, instance_id, initiator, detail),
    )
    return int(cur.lastrowid)


def mark_disconnected(
    conn: sqlite3.Connection, instance_id: str, conn_epoch: int
) -> None:
    """Epoch-guarded disconnect write — THE headline invariant (§6).

    Every disconnect path (clean close, error, heartbeat miss, eviction of the
    old socket) calls this with the epoch THAT socket owned. The
    ``AND conn_epoch = ?`` guard is what makes a half-dead old socket finalized
    30s late a no-op: by then a healthy newer connection has bumped the epoch, so
    this UPDATE matches nothing and does NOT clear the live connection. Dropping
    the guard would strand the instance out of curation forever on a working link.
    """
    conn.execute(
        "UPDATE instances SET connected = 0, focused_window_id = NULL "
        "WHERE id = ? AND conn_epoch = ?",
        (instance_id, conn_epoch),
    )
