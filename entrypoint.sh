#!/bin/sh
# Postgres-style hybrid entrypoint: start as root, fix state-dir ownership, then drop
# privileges to the unprivileged `app` user (uid 1000).
set -e

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
    exec "$@"
fi
