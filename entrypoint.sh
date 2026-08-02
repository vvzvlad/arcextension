#!/bin/sh
# Postgres-style hybrid entrypoint: start as root, fix state-dir ownership,
# then drop privileges to the unprivileged `app` user (uid 1000).
set -e

if [ "$(id -u)" = "0" ]; then
    # Heals volumes left by older root-based images (self-healing migration).
    # For huge data dirs this chown can be guarded by a marker file as an optimisation.
    chown -R app:app /app/data
    # Backups mount on a SEPARATE volume in prod (§12: BACKUP_DIR=/app/backups). Only
    # an ABSOLUTE BACKUP_DIR is a distinct mount that needs its own mkdir+chown before
    # the drop, or the unprivileged `app` user cannot write the nightly VACUUM INTO
    # copy. The dev default (data/backups, relative → under /app/data) is already
    # owned by the chown above, so it is skipped here.
    case "${BACKUP_DIR:-}" in
        /*) mkdir -p "$BACKUP_DIR" && chown -R app:app "$BACKUP_DIR" ;;
    esac
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
    # Same fail-fast for a separate absolute BACKUP_DIR mount (§12): a read-only copy
    # volume must surface here, not later as a failed nightly backup.
    case "${BACKUP_DIR:-}" in
        /*)
            if [ ! -w "$BACKUP_DIR" ]; then
                echo "FATAL: $BACKUP_DIR is not writable by uid $(id -u)." >&2
                echo "Fix ownership on the host: chown -R $(id -u) <backup-volume>/_data" >&2
                exit 1
            fi
            ;;
    esac
    exec "$@"
fi
