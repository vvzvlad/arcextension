#!/bin/sh
# Postgres-style hybrid entrypoint: start as root, fix state-dir ownership, create
# the continuity marker on the first ever start, then drop privileges to the
# unprivileged `app` user (uid 1000).
set -e

# A fresh unique value for a NEW continuity marker. /proc is the container's own
# source and costs no package; uuidgen and /dev/urandom are fallbacks so the script
# stays runnable — and testable — outside a Linux container.
new_uuid() {
    if [ -r /proc/sys/kernel/random/uuid ]; then
        cat /proc/sys/kernel/random/uuid
    elif command -v uuidgen >/dev/null 2>&1; then
        uuidgen
    else
        od -An -tx1 -N16 /dev/urandom | tr -d ' \n'
        echo
    fi
}

# Create the continuity marker (§7) if — and ONLY if — it is not there yet.
#
# At INSTALL all that matters is that the file exists with some value, so creating it
# is a pure chore and is automated here. What makes a restore DETECTABLE is the
# operator writing a DIFFERENT value into it during the restore (deploy/DEPLOY.md §8,
# step 4), and that half cannot be automated: the service cannot know it was rolled
# back — being outside the service is the whole point of an external marker. So this
# function must never grow an "update" path: rewriting the marker on start would let
# the service erase the only evidence of its own rollback.
#
# "The file is not there" stays a supported state on the reading side
# (clock.MARKER_MISSING) and is no longer practically reachable at pass time — but a
# marker can still be deleted while the service runs, so the sentinel is not dead
# code. A delete observed by a pass records "missing"; the value written on the next
# start is then a change like any other and costs one dry_run + confirmation.
ensure_restore_marker() {
    marker="${RESTORE_MARKER_PATH:-}"
    if [ -z "$marker" ]; then
        return 0    # detection deliberately off (empty default, src/settings.py)
    fi
    if [ -e "$marker" ]; then
        return 0    # an existing value is NEVER touched
    fi
    if ! mkdir -p "$(dirname "$marker")" 2>/dev/null; then
        echo "WARN: cannot create $(dirname "$marker") — continuity marker not created." >&2
        return 0
    fi
    if ! new_uuid > "$marker" 2>/dev/null; then
        echo "WARN: cannot write $marker — continuity marker not created." >&2
        return 0
    fi
    # 644 explicitly, not whatever the umask gives: the file is written as root here
    # but READ as uid 1000, and a marker uid 1000 cannot read disables restore
    # detection silently (curator_restore_marker_unreadable=1).
    chmod 644 "$marker" 2>/dev/null || true
    echo "entrypoint: continuity marker created at $marker"
}

if [ "$(id -u)" = "0" ]; then
    # Heals volumes left by older root-based images (self-healing migration).
    # For huge data dirs this chown can be guarded by a marker file as an optimisation.
    chown -R app:app /app/data
    # Prod keeps everything on the single `curator` volume (BACKUP_DIR=/app/data/backups),
    # so the chown above already covers it. An ABSOLUTE BACKUP_DIR is still handled
    # explicitly, because it may point at a path OUTSIDE /app/data (a separate mount an
    # operator added): that path needs its own mkdir+chown before the drop, or the
    # unprivileged `app` user cannot write the nightly VACUUM INTO copy. The dev default
    # (data/backups, relative → under /app/data) is owned by the chown above and skipped.
    case "${BACKUP_DIR:-}" in
        /*) mkdir -p "$BACKUP_DIR" && chown -R app:app "$BACKUP_DIR" ;;
    esac
    ensure_restore_marker
    # The marker and its directory stay root-owned: the service must be able to READ
    # the marker and must NOT be able to rewrite it — a service that can "heal" the
    # value silently disables the detection the marker exists for. That used to be the
    # job of the `:ro` mount; with one volume (deploy/DEPLOY.md §5) ownership is what
    # carries it. The DIRECTORY matters as much as the file: write access to a directory
    # is enough to unlink and recreate what is inside it. The operator writes the marker
    # from a root shell (`docker compose run --entrypoint sh curator`), so the restore
    # procedure is unaffected.
    if [ -n "${RESTORE_MARKER_PATH:-}" ] && [ -e "$RESTORE_MARKER_PATH" ]; then
        chown root:root "$RESTORE_MARKER_PATH"
        chmod 644 "$RESTORE_MARKER_PATH"
        # …but NEVER take /app/data itself: the app writes the DB there, and a
        # root-owned data dir would break every start. A marker configured directly
        # under /app/data therefore gets file-level protection only — which is why the
        # shipped compose puts it in /app/data/restore/ instead.
        marker_dir=$(dirname "$RESTORE_MARKER_PATH")
        case "$marker_dir" in
            /app/data|/app|/|.) ;;
            *) chown root:root "$marker_dir" && chmod 755 "$marker_dir" ;;
        esac
    fi
    exec gosu app "$@"
else
    # A compose `user:` override is in effect — respect it, but fail fast if the
    # volume is not writable by that uid instead of failing later at runtime
    # (e.g. sqlite "attempt to write a readonly database").
    if [ ! -w /app/data ]; then
        echo "FATAL: /app/data is not writable by uid $(id -u)." >&2
        echo "Fix ownership on the host: chown -R $(id -u) <volume>/_data" >&2
        exit 1
    fi
    # Same fail-fast for an absolute BACKUP_DIR that points outside /app/data: a
    # read-only backup path must surface here, not later as a failed nightly backup.
    case "${BACKUP_DIR:-}" in
        /*)
            if [ ! -w "$BACKUP_DIR" ]; then
                echo "FATAL: $BACKUP_DIR is not writable by uid $(id -u)." >&2
                echo "Fix ownership on the host: chown -R $(id -u) <backup-volume>/_data" >&2
                exit 1
            fi
            ;;
    esac
    # Best effort under an override: no chown is possible, so the marker ends up owned
    # by the overriding uid (which already owns the whole volume anyway). Still worth
    # creating — an install whose marker never appears sits on a permanently "missing"
    # state, and "missing" before a restore and "missing" after it is no change at all,
    # i.e. restore detection would be off without saying so.
    ensure_restore_marker
    exec "$@"
fi
