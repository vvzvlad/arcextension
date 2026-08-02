"""The pass lease with a fencing epoch (§7 "Аренда с fencing").

``settings.pass_lease = {owner, epoch, until}`` in the architecture; here it is
stored as THREE ``settings`` rows — ``pass_lease_owner``, ``pass_lease_epoch``,
``pass_lease_until`` — so the fencing guard can be a single, unambiguous
``UPDATE … WHERE value = ?`` whose ``rowcount`` is 1 when the epoch still matches
and 0 when it has moved (verified on the container's SQLite 3.46.1: a matched
no-op UPDATE counts the row, a mismatched one does not). Semantically identical to
the single-key JSON shape; the decomposition only buys clean SQL fencing.

Every function here is a synchronous ``fn(conn)`` run INSIDE one ``Database.write``
transaction (Фаза 2 contract: no ``await`` inside; one ``BEGIN IMMEDIATE`` … COMMIT).
Because the single writer serializes all writes AND ``BEGIN IMMEDIATE`` takes the
DB write lock across processes, ``acquire`` is a correct mutual-exclusion primitive
even under a rolling redeploy (two live containers) — the very scenario the fencing
epoch exists for.

The load-bearing rules (§7):

* **Every pass write is conditional on the epoch.** A lease-guarded write that
  changes zero rows ⇒ the pass stops immediately (another worker took the lease,
  or a pause bumped the epoch). :func:`guard` is that check; :func:`guarded` wraps
  a mutation ``fn`` so the whole transaction rolls back on a lost lease.
* **Renewal is a SEPARATE task** (see runner), cancelled by the SAME ``finally``
  that releases the lease — an inline renewal in the command loop would blow the
  TTL after a dozen consecutive command timeouts and hand the lease to the next
  pass while the first keeps sending commands.
* **Acquiring bumps the epoch** (monotonic) — a zombie pass that outlived its TTL
  is fenced out the moment the next pass acquires: its next guarded write matches
  the old epoch and changes zero rows.
"""

from __future__ import annotations

import sqlite3

_OWNER_KEY = "pass_lease_owner"
_EPOCH_KEY = "pass_lease_epoch"
_UNTIL_KEY = "pass_lease_until"


class LeaseLost(Exception):
    """Raised inside a guarded write when the pass no longer holds the lease.

    It aborts the enclosing transaction (rolling back any partial mutation) and is
    caught by the runner, which then stops the pass at once (§7).
    """


def _get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row is not None else None


def _set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _epoch(conn: sqlite3.Connection) -> int:
    raw = _get(conn, _EPOCH_KEY)
    return int(raw) if raw is not None else 0


def acquire(conn: sqlite3.Connection, owner: str, now: int, ttl_ms: int):
    """Try to take the lease. Returns ``(acquired: bool, epoch: int)``.

    Acquire succeeds iff there is no live lease (``until`` absent or ``<= now``).
    On success the epoch is bumped (the fencing token) and ``until`` is set to
    ``now + ttl_ms``. On failure the CURRENT epoch is returned unchanged.

    ``owner`` must be unique per attempt (a uuid) so "same owner" never lets two
    concurrent attempts both win — a still-live lease blocks regardless of owner.
    """
    until_raw = _get(conn, _UNTIL_KEY)
    until = int(until_raw) if until_raw is not None else None
    if until is not None and until > now:
        # Someone holds a live lease. Do NOT touch anything (no epoch bump).
        return False, _epoch(conn)
    new_epoch = _epoch(conn) + 1
    _set(conn, _OWNER_KEY, owner)
    _set(conn, _EPOCH_KEY, str(new_epoch))
    _set(conn, _UNTIL_KEY, str(now + ttl_ms))
    return True, new_epoch


def renew(conn: sqlite3.Connection, owner: str, epoch: int, now: int, ttl_ms: int) -> bool:
    """Extend ``until`` iff this owner still holds this epoch. Returns success.

    A ``False`` return means the lease was lost (a pause bumped the epoch, or the
    lease expired and another pass took it) — the renewal task stops on it.
    """
    if _epoch(conn) != epoch or _get(conn, _OWNER_KEY) != owner:
        return False
    _set(conn, _UNTIL_KEY, str(now + ttl_ms))
    return True


def release(conn: sqlite3.Connection, owner: str, epoch: int) -> None:
    """Release the lease iff this owner still holds this epoch.

    Expires ``until`` (sets it to 0) so the next pass can acquire immediately; the
    epoch is left as-is (it only ever moves forward). Releasing someone else's
    lease is a no-op — an evicted pass must never clear the live one.
    """
    if _epoch(conn) != epoch or _get(conn, _OWNER_KEY) != owner:
        return
    _set(conn, _UNTIL_KEY, "0")
    _set(conn, _OWNER_KEY, "")


def bump_epoch(conn: sqlite3.Connection) -> int:
    """Increment the epoch WITHOUT taking the lease (pause uses this to stop an
    in-flight pass: its next guarded write then changes zero rows). Returns the new
    epoch. Provided for Фаза 16; kept next to the fencing logic it belongs to."""
    new_epoch = _epoch(conn) + 1
    _set(conn, _EPOCH_KEY, str(new_epoch))
    return new_epoch


def guard(conn: sqlite3.Connection, epoch: int) -> None:
    """Fencing guard: raise :class:`LeaseLost` unless the stored epoch still equals
    ``epoch``. A matched no-op UPDATE returns ``rowcount == 1``; a mismatch (or a
    pause/other pass having bumped the epoch) returns 0 → the pass must stop."""
    cur = conn.execute(
        "UPDATE settings SET value = value WHERE key = ? AND value = ?",
        (_EPOCH_KEY, str(epoch)),
    )
    if cur.rowcount == 0:
        raise LeaseLost(f"lease epoch {epoch} no longer held")


def guarded(epoch: int, fn):
    """Wrap a mutation ``fn(conn)`` so the transaction first fences on ``epoch``.

    Use as ``db.write(lease.guarded(epoch, real_fn))`` — a lost lease raises
    :class:`LeaseLost` BEFORE ``real_fn`` writes anything, and the enclosing
    ``BEGIN IMMEDIATE`` rolls back, so no partial pass write ever lands.
    """

    def _run(conn: sqlite3.Connection):
        guard(conn, epoch)
        return fn(conn)

    return _run


def read_lease(conn: sqlite3.Connection) -> dict:
    """Return ``{owner, epoch, until}`` for diagnostics/metrics/tests."""
    until_raw = _get(conn, _UNTIL_KEY)
    return {
        "owner": _get(conn, _OWNER_KEY),
        "epoch": _epoch(conn),
        "until": int(until_raw) if until_raw is not None else None,
    }
