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
    there is nowhere to persist a new MAIN, and the id is part of the continuity
    fingerprint. Task E maps this refusal to HTTP 409 (acceptance 9).
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


# hello success: bump an ALREADY-APPROVED instance row and return the NEW conn_epoch.
# UPDATE-only (§3, issue #35): under enrollment a hello NEVER creates a row — the row
# is created by an operator approval (Task E) with status='active' and a secret_hash;
# an anon who only knows the public PROTOCOL_VERSION must not be able to conjure an
# `instances` row. A reconnect does conn_epoch+1, sets connected=1 and clears the reject
# fields. The ``AND status='active'`` guard means a hello for a revoked/pending/absent id
# matches nothing (the channel resolves the secret first, but the row can vanish or be
# revoked between resolve and this write — the None-guard below handles that race).
_HELLO_UPSERT = """
UPDATE instances SET
    conn_epoch = conn_epoch + 1,
    connected = 1,
    session_id = ?,
    title = ?,
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
    title: str | None,
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
        (session_id, title, 1 if allow_execute_js else 0, now, instance_id),
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


# Record (or refresh) a pending enrollment request. Keyed by install_uuid so a repeat
# hello UPSERTs the same row instead of piling up. TWO columns are DELIBERATELY NOT
# updated on conflict:
#
# * ``first_seen_at`` (issue §1) — a request that keeps re-arriving must still age out
#   against its ORIGINAL first_seen_at, otherwise the TTL is never reached and a stale
#   request lives forever.
# * ``secret_hash`` — the CREDENTIAL the operator approves. Refreshing it on conflict made
#   the pending list a TOCTOU surface: anyone who knows a victim's ``install_uuid`` and the
#   current window code could re-submit the same request with THEIR secret, leaving every
#   operator-visible field (origin, suggested_title, protocol_version, the first-8 uuid)
#   untouched — so the operator would click Approve on the row they inspected and enroll the
#   ATTACKER's credential under the victim's identity. The credential is now frozen at the
#   value the row was CREATED with; a client that genuinely needs to enroll a different
#   secret must first have the pending request rejected (or let it age out under TTL), which
#   is a deliberate operator act. A repeat from the honest client carries the SAME secret it
#   persisted, so nothing changes for it.
#
# Everything else (last_seen_at, the client-proposed title, protocol_version, origin) is
# refreshed to the latest request.
_UPSERT_ENROLL_REQUEST = """
INSERT INTO enroll_requests
    (install_uuid, origin, suggested_title, protocol_version, secret_hash,
     first_seen_at, last_seen_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(install_uuid) DO UPDATE SET
    last_seen_at = excluded.last_seen_at,
    suggested_title = excluded.suggested_title,
    protocol_version = excluded.protocol_version,
    origin = excluded.origin
"""

# Outcomes of :func:`upsert_enroll_request_capped`. Strings rather than a bool because the
# caller maps each onto a DIFFERENT client-facing verdict, and a bool cannot carry three.
ENROLL_ACCEPTED = "accepted"
ENROLL_AT_CAPACITY = "capacity"
ENROLL_SECRET_MISMATCH = "secret_conflict"


def upsert_enroll_request(
    conn: sqlite3.Connection,
    install_uuid: str,
    origin: str | None,
    suggested_title: str | None,
    protocol_version: int,
    secret_hash: str,
    now: int,
) -> None:
    """Insert-or-refresh the pending enroll request for ``install_uuid`` (see SQL).

    The uncapped form, kept for direct callers/tests. Like the capped one it never
    overwrites a stored ``secret_hash`` (see the SQL comment) — the INSERT branch is the
    only way a credential enters the row.
    """
    conn.execute(
        _UPSERT_ENROLL_REQUEST,
        (install_uuid, origin, suggested_title, protocol_version, secret_hash, now, now),
    )


def upsert_enroll_request_capped(
    conn: sqlite3.Connection,
    install_uuid: str,
    origin: str | None,
    suggested_title: str | None,
    protocol_version: int,
    secret_hash: str,
    now: int,
    max_pending: int,
    ttl_ms: int,
) -> str:
    """Capacity-check + upsert atomically in ONE write transaction; return the outcome.

    Returns one of :data:`ENROLL_ACCEPTED`, :data:`ENROLL_AT_CAPACITY`,
    :data:`ENROLL_SECRET_MISMATCH`.

    The capacity count and the write run under the SAME ``Database.write`` BEGIN, so the
    ceiling is authoritative even when N enroll_requests race (each in its own connection):
    this is the ONLY capacity gate — the channel deliberately does no advisory pre-count,
    which would be a second unauthenticated DB read per socket.

    An install_uuid that ALREADY has a LIVE row is an UPDATE (refresh), never a new row, so
    it is always accepted regardless of capacity — else a full list could not even refresh
    ``last_seen_at`` and a pending request would age out under TTL. A genuinely NEW
    install_uuid is written only when ``count < max_pending``.

    A repeat whose ``secret_hash`` DIFFERS from the stored one writes NOTHING and returns
    :data:`ENROLL_SECRET_MISMATCH`: the credential is frozen at creation (see the SQL
    comment above), and silently answering ``enroll_pending`` would leave the client waiting
    on the approval of a secret it does not hold. Refusing instead keeps ``last_seen_at``
    frozen too, so the stale row ages out on schedule and the client's next retry is
    accepted — the state self-heals within the TTL without an operator, and immediately if
    the operator rejects the stale request.

    **The frozen-credential rule applies to LIVE rows only, hence ``ttl_ms``.** A row is
    pending for the reader exactly while ``first_seen_at >= now - ttl_ms`` — that is the
    filter BOTH read surfaces apply (:func:`list_pending_enroll_requests`,
    :func:`get_enroll_request`) — and the physical sweep only catches up within a tick. In
    the gap between the two, an expired row is invisible AND unapprovable, so freezing the
    credential against it refused a legitimate re-registration with ``secret_conflict``
    while the operator's list had nothing to reject: an un-fixable state, for a row that
    was already logically gone. An expired row is therefore treated as ABSENT — deleted and
    re-INSERTed with the new credential and a fresh ``first_seen_at``, which the plain
    UPSERT could not do (its DO UPDATE deliberately leaves both columns alone).

    The capacity ``COUNT(*)`` is deliberately NOT TTL-filtered, and the expired row is
    counted before it is deleted. The ceiling is an anti-flood bound on ROWS, and rows
    expired-but-not-yet-swept are real rows; counting them is the stricter reading and the
    one §2 documents. The refusal it can produce is self-clearing — the sweeper runs every
    ``TICK_MS`` — whereas the secret freeze above was not.
    """
    row = conn.execute(
        "SELECT secret_hash, first_seen_at FROM enroll_requests WHERE install_uuid = ?",
        (install_uuid,),
    ).fetchone()
    expired = row is not None and row[1] < now - ttl_ms
    if row is None or expired:
        # No LIVE row for this install_uuid: this write adds one to the operator's list, so
        # it must pass the ceiling. Counted BEFORE the delete below — every row still in the
        # table counts, TTL or not (docstring).
        n = conn.execute("SELECT COUNT(*) FROM enroll_requests").fetchone()[0]
        if n >= max_pending:
            return ENROLL_AT_CAPACITY
        if expired:
            # Clear the tombstone so the UPSERT takes its INSERT branch and the row comes
            # back with THIS request's secret_hash and a fresh first_seen_at.
            conn.execute(
                "DELETE FROM enroll_requests WHERE install_uuid = ?", (install_uuid,)
            )
    elif row[0] != secret_hash:
        return ENROLL_SECRET_MISMATCH
    conn.execute(
        _UPSERT_ENROLL_REQUEST,
        (install_uuid, origin, suggested_title, protocol_version, secret_hash, now, now),
    )
    return ENROLL_ACCEPTED


# NOTE: there is deliberately no `count_enroll_requests` helper any more. It existed to
# feed an ADVISORY pre-count in the /ext enroll handler — a second sqlite connection taken
# from the shared pool on a fully UNAUTHENTICATED socket, before the window code had been
# checked, whose answer ``upsert_enroll_request_capped`` then recomputed inside the write
# anyway. Keeping a helper whose docstring calls it "the capacity gate" is an invitation to
# wire that read back in; the gate is the transaction.


def count_active_instances(conn: sqlite3.Connection) -> int:
    """Number of approved (active) instances. Exposed for completeness / later use."""
    row = conn.execute(
        "SELECT COUNT(*) FROM instances WHERE status = 'active'"
    ).fetchone()
    return int(row[0])


# --- /admin enrollment API helpers (Task E) ---------------------------------
# The read/approve/reject/list SQL the /admin JSON endpoints run. Kept HERE next to
# the other enroll_requests helpers (upsert / count) so all the enrollment SQL lives in
# one mutation-testable place — the endpoints never inline SQL of their own.


class ApproveConflict(Exception):
    """An approve that must answer HTTP 409 (issue #35 acceptance 11).

    Raised when the target id is ALREADY active — a re-approve of a live id, detected by
    the ``WHERE instances.status != 'active'`` guard matching zero rows. The OTHER 409
    path — a second active instance carrying the SAME ``secret_hash`` — surfaces as a raw
    ``sqlite3.IntegrityError`` from the ``UNIQUE(secret_hash)`` index (§1); the handler
    maps BOTH to 409. Together they make two racing approves resolve to exactly one 200
    and one 409, with exactly one active row / one secret left in the DB.
    """


# Read-time listing of PENDING enroll requests, already TTL-filtered (acceptance 12). A
# row whose FROZEN ``first_seen_at`` is older than the cutoff is never returned — the
# physical DELETE (delete_expired_enroll_requests) then removes it within TTL+tick. The
# EXISTS sub-select is the ``id_exists`` hint: whether an ``instances`` row already
# carries this request's ``install_uuid`` — i.e. this install was enrolled before (a
# revoked/pending re-enrol), so the operator can reuse the same id (the re-approve /
# MAIN-restore path). It is a UI hint only; approval never depends on it.
_LIST_PENDING_ENROLL_REQUESTS = """
SELECT
    e.install_uuid,
    e.origin,
    e.suggested_title,
    e.protocol_version,
    e.first_seen_at,
    e.last_seen_at,
    EXISTS(SELECT 1 FROM instances i WHERE i.install_uuid = e.install_uuid) AS id_exists
FROM enroll_requests e
WHERE e.first_seen_at >= ?
ORDER BY e.first_seen_at ASC
"""


def list_pending_enroll_requests(
    conn: sqlite3.Connection, *, now: int, ttl_ms: int
) -> list[dict]:
    """Return the pending enroll requests not past TTL, newest-first-frozen order.

    ``cutoff = now - ttl_ms``: a request whose ``first_seen_at`` is strictly OLDER than
    the cutoff is filtered out at read time (never returned after TTL, acceptance 12).
    ``install_uuid_short`` is the first 8 chars for a compact display; ``origin`` /
    ``suggested_title`` are UNTRUSTED (length already clamped server-side in slice B) and
    returned VERBATIM — the JSON API never HTML-encodes; the #36 page uses ``textContent``.
    """
    cutoff = now - ttl_ms
    rows = conn.execute(_LIST_PENDING_ENROLL_REQUESTS, (cutoff,)).fetchall()
    out: list[dict] = []
    for (install_uuid, origin, suggested_title, proto, first_seen_at,
         last_seen_at, id_exists) in rows:
        out.append({
            "install_uuid": install_uuid,
            "install_uuid_short": (install_uuid or "")[:8],
            "origin": origin,
            "suggested_title": suggested_title,
            "protocol_version": proto,
            "first_seen_at": first_seen_at,
            "last_seen_at": last_seen_at,
            "id_exists": bool(id_exists),
        })
    return out


def get_enroll_request(
    conn: sqlite3.Connection, install_uuid: str, *, now: int, ttl_ms: int
) -> dict | None:
    """Return one pending, NOT-expired enroll request by ``install_uuid`` (or ``None``).

    The approve handler reads this FIRST (own read txn) to 404 an absent/expired request
    and to capture the ``secret_hash`` it will enroll. Reading it out of band is what
    lets two racing approves BOTH hold the secret and so collide on the write (the
    ``UNIQUE(secret_hash)`` / already-active guards) rather than one silently 404-ing.
    Applies the SAME ``first_seen_at >= now - ttl_ms`` filter as the list (an expired
    request is not approvable, matching the read-time TTL of acceptance 12).
    """
    cutoff = now - ttl_ms
    row = conn.execute(
        "SELECT install_uuid, secret_hash, suggested_title, first_seen_at "
        "FROM enroll_requests WHERE install_uuid = ? AND first_seen_at >= ?",
        (install_uuid, cutoff),
    ).fetchone()
    if row is None:
        return None
    return {
        "install_uuid": row[0],
        "secret_hash": row[1],
        "suggested_title": row[2],
        "first_seen_at": row[3],
    }


# Create-or-REACTIVATE the operator-assigned instance row in ONE statement (§1, acc 11).
# A brand-new id INSERTs; an EXISTING revoked/pending id is UPDATEd back to 'active' —
# this is the ONLY path that restores a revoked MAIN (Task D leaves MAIN revoked). The
# ``WHERE instances.status != 'active'`` guard makes re-approving an ALREADY-active id a
# no-op (rowcount 0 → ApproveConflict → 409), so an approve never silently overwrites a
# live instance. ``conn_epoch`` / ``connected`` are LEFT untouched on the update path so
# a reactivation does not disturb a socket that somehow still holds the id.
_APPROVE_UPSERT = """
INSERT INTO instances (id, status, secret_hash, install_uuid, enrolled_at, title)
VALUES (?, 'active', ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    status = 'active',
    secret_hash = excluded.secret_hash,
    install_uuid = excluded.install_uuid,
    enrolled_at = excluded.enrolled_at,
    title = excluded.title
WHERE instances.status != 'active'
"""


def approve_enroll_request(
    conn: sqlite3.Connection,
    *,
    instance_id: str,
    secret_hash: str,
    install_uuid: str,
    title: str | None,
    now: int,
) -> None:
    """Enroll ``instance_id`` from a captured ``secret_hash`` in ONE write txn (acc 11).

    Runs :data:`_APPROVE_UPSERT` then, on success, DELETEs the consumed enroll_request
    (idempotent — the loser of a race deletes an already-gone row). Two 409 guards:

    * ``rowcount == 0`` ⇒ the id was already active (the ``WHERE status != 'active'``
      guard matched nothing) ⇒ :class:`ApproveConflict`;
    * a ``UNIQUE(secret_hash)`` collision (a DIFFERENT active id already carries this
      secret) raises ``sqlite3.IntegrityError`` straight out of ``conn.execute`` — the
      write txn rolls back, so no partial row and the request stays for a retry.

    The whole body is one synchronous ``fn(conn)`` (Фаза 2 contract): the caller runs it
    under ``Database.write`` so the upsert and the delete commit together or not at all.
    """
    cur = conn.execute(
        _APPROVE_UPSERT, (instance_id, secret_hash, install_uuid, now, title)
    )
    if cur.rowcount == 0:
        # The id exists and is already 'active' — a re-approve of a live instance. Refuse
        # rather than clobber it; the request is left in place (not consumed).
        raise ApproveConflict(
            f"instance {instance_id!r} is already active; refusing to overwrite"
        )
    conn.execute(
        "DELETE FROM enroll_requests WHERE install_uuid = ?", (install_uuid,)
    )


def reject_enroll_request(conn: sqlite3.Connection, install_uuid: str) -> bool:
    """Delete the pending enroll request for ``install_uuid``; return whether a row went.

    Idempotent: rejecting an already-gone request removes nothing and returns ``False``
    (the handler still answers 200 — the desired end state, request absent, holds).
    """
    cur = conn.execute(
        "DELETE FROM enroll_requests WHERE install_uuid = ?", (install_uuid,)
    )
    return cur.rowcount > 0


_LIST_INSTANCES = """
SELECT id, title, status, connected, last_seen_at, enrolled_at, revoked_at
FROM instances
ORDER BY id
"""


def list_instances(conn: sqlite3.Connection) -> list[dict]:
    """Return EVERY instance (all statuses) for the operator console.

    Unlike the curator's ``status='active'`` reads, this lists revoked/pending rows too
    so the operator can see a revoked MAIN awaiting re-approval or a stuck pending id.
    """
    rows = conn.execute(_LIST_INSTANCES).fetchall()
    return [
        {
            "id": r[0],
            "title": r[1],
            "status": r[2],
            # revoke clears status/session but leaves the `connected` column to the
            # channel's async socket teardown (not guaranteed if there is no live socket),
            # so report `connected` only for an ACTIVE row — a revoked/pending instance is
            # never "connected" for the operator console, whatever the stale flag says.
            "connected": bool(r[3]) and r[2] == "active",
            "last_seen_at": r[4],
            "enrolled_at": r[5],
            "revoked_at": r[6],
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
    retention, so it is never swept. ``initiator`` names who acted ('admin' for an
    ADMIN_TOKEN caller). Returns the new row id.
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
