"""Tests for `entrypoint.sh` — the root-side startup script (deploy/DEPLOY.md §5).

The script fixes state-dir ownership as root and drops privileges to `app` (uid 1000)
via gosu; the non-root branch (a compose `user:` override) fails fast on an unusable
volume instead of letting sqlite fail later.

Running the real container is not free in CI, so the script is exercised directly with a
stub PATH: `id` reports the uid the test wants, `chown` and `gosu` are no-ops/exec
shims. Everything the assertions look at is produced by the real script, not by a
re-implementation of it.

Each test reddens if its guard is removed (noted inline).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[1] / "entrypoint.sh"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _stub_bin(tmp_path: Path, uid: str) -> Path:
    """A PATH directory with the three commands the root branch needs.

    `id -u` decides which branch runs; `chown` cannot work outside a container (we are
    not root and there is no `app` user), so it is a no-op; `gosu app CMD` becomes CMD.
    """
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir(exist_ok=True)

    (bin_dir / "id").write_text(f"#!/bin/sh\necho {uid}\n")
    (bin_dir / "chown").write_text("#!/bin/sh\nexit 0\n")
    # `gosu app "$@"` — drop the user argument and run the rest.
    (bin_dir / "gosu").write_text('#!/bin/sh\nshift\nexec "$@"\n')
    for name in ("id", "chown", "gosu"):
        (bin_dir / name).chmod(0o755)
    return bin_dir


def _run(tmp_path: Path, *, uid: str = "0", env: dict | None = None):
    """Run the real entrypoint with a stub PATH; the payload command is `true`."""
    bin_dir = _stub_bin(tmp_path, uid)
    full_env = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(tmp_path),
    }
    full_env.update(env or {})
    return subprocess.run(
        ["sh", str(ENTRYPOINT), "true"],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )


# --------------------------------------------------------------------------- #
# The script itself has to parse
# --------------------------------------------------------------------------- #
def test_entrypoint_is_valid_sh():
    # Reddens on any syntax error: a broken entrypoint bricks every container start,
    # and nothing else in CI executes this file.
    assert subprocess.run(["sh", "-n", str(ENTRYPOINT)]).returncode == 0


# --------------------------------------------------------------------------- #
# The payload command still runs, and BACKUP_DIR is still prepared
# --------------------------------------------------------------------------- #
def test_execs_the_payload_and_creates_backup_dir(tmp_path):
    backups = tmp_path / "data" / "backups"
    bin_dir = _stub_bin(tmp_path, "0")
    res = subprocess.run(
        ["sh", str(ENTRYPOINT), "sh", "-c", "echo PAYLOAD-RAN"],
        env={
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(tmp_path),
            "BACKUP_DIR": str(backups),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, res.stderr
    # Reddens if the setup exits instead of falling through to the exec: the container
    # would start without ever running the app.
    assert "PAYLOAD-RAN" in res.stdout
    assert backups.is_dir()


# --------------------------------------------------------------------------- #
# Non-root branch (compose `user:` override) still fails fast on an unusable volume
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    os.access("/app/data", os.W_OK), reason="/app/data exists and is writable here"
)
def test_non_root_fails_fast_when_data_is_not_writable(tmp_path):
    res = _run(tmp_path, uid="1000")
    # The override branch must refuse to start rather than let sqlite fail later with
    # "attempt to write a readonly database".
    assert res.returncode == 1
    assert "FATAL" in res.stderr
