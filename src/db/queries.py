"""Synchronous ``fn(conn)`` bodies for ``Database.write`` used by the /ext channel.

Every function here is a plain synchronous callable that runs INSIDE one
``Database.write`` transaction (Фаза 2 contract: no ``await``, one BEGIN/COMMIT).
They are kept out of ``channel.py`` so the exact SQL of the two load-bearing
invariants — the epoch-guarded disconnect UPDATE and the hello upsert that reads
its own new ``conn_epoch`` back in the same transaction — is in one place and
mutation-testable.
"""

from __future__ import annotations

import sqlite3

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
    conn: sqlite3.Connection, secret_hash: str
) -> tuple[str, str] | None:
    """Resolve a hello's ``secretHash`` to ``(instance_id, status)`` or ``None``.

    Uses the UNIQUE ``instances_secret_hash`` index (§1). ``None`` means no instance
    carries this secret at all (the client is unknown / not yet approved). The status is
    returned raw ('active' / 'revoked' / 'pending') so the channel can map it to the
    right client-facing verdict (unknown vs revoked, §7).
    """
    row = conn.execute(
        "SELECT id, status FROM instances WHERE secret_hash = ?", (secret_hash,)
    ).fetchone()
    if row is None:
        return None
    return (row[0], row[1])


# Record (or refresh) a pending enrollment request. Keyed by install_uuid so a repeat
# hello UPSERTs the same row instead of piling up. ``first_seen_at`` is DELIBERATELY NOT
# updated on conflict (issue §1): a request that keeps re-arriving must still age out
# against its ORIGINAL first_seen_at, otherwise the TTL is never reached and a stale
# request lives forever. Everything else (last_seen_at, the client-proposed title,
# secret_hash, protocol_version, origin) is refreshed to the latest hello.
_UPSERT_ENROLL_REQUEST = """
INSERT INTO enroll_requests
    (install_uuid, origin, suggested_title, protocol_version, secret_hash,
     first_seen_at, last_seen_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(install_uuid) DO UPDATE SET
    last_seen_at = excluded.last_seen_at,
    suggested_title = excluded.suggested_title,
    secret_hash = excluded.secret_hash,
    protocol_version = excluded.protocol_version,
    origin = excluded.origin
"""


def upsert_enroll_request(
    conn: sqlite3.Connection,
    install_uuid: str,
    origin: str | None,
    suggested_title: str | None,
    protocol_version: int,
    secret_hash: str,
    now: int,
) -> None:
    """Insert-or-refresh the pending enroll request for ``install_uuid`` (see SQL)."""
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
) -> bool:
    """Capacity-check + upsert atomically in ONE write transaction; return acceptance.

    The channel's pre-read capacity gate (:func:`count_enroll_requests`) is only
    advisory: it runs in a separate read transaction, so N racing enroll_requests could
    each observe ``pending < max`` and all write, overshooting ``max_pending``. This does
    the count and the write under the SAME ``Database.write`` BEGIN, so the ceiling is
    authoritative. An install_uuid that ALREADY has a row is an UPDATE (refresh), never a
    new row, so it is always accepted regardless of capacity — else a full list could not
    even refresh ``last_seen_at`` and a pending request would age out under TTL. A genuinely
    NEW install_uuid is written only when ``count < max_pending``; otherwise nothing is
    written and ``False`` is returned so the caller replies ``enroll_rejected{capacity}``.
    """
    exists = conn.execute(
        "SELECT 1 FROM enroll_requests WHERE install_uuid = ?", (install_uuid,)
    ).fetchone()
    if exists is None:
        n = conn.execute("SELECT COUNT(*) FROM enroll_requests").fetchone()[0]
        if n >= max_pending:
            return False
    conn.execute(
        _UPSERT_ENROLL_REQUEST,
        (install_uuid, origin, suggested_title, protocol_version, secret_hash, now, now),
    )
    return True


def count_enroll_requests(conn: sqlite3.Connection) -> int:
    """Number of pending enroll requests — the capacity gate's numerator (§2)."""
    row = conn.execute("SELECT COUNT(*) FROM enroll_requests").fetchone()
    return int(row[0])


def count_active_instances(conn: sqlite3.Connection) -> int:
    """Number of approved (active) instances. Exposed for completeness / later use."""
    row = conn.execute(
        "SELECT COUNT(*) FROM instances WHERE status = 'active'"
    ).fetchone()
    return int(row[0])


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
