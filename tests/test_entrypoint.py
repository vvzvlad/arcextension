"""Tests for `entrypoint.sh` — the root-side startup script (deploy/DEPLOY.md §5, §8).

The script is what arms the restore detector on a fresh install: it creates the
continuity marker with a fresh uuid when the file is absent, and must NEVER touch an
existing one (a service able to rewrite the marker could erase the only evidence of its
own rollback — see `src/curator/clock.read_restore_marker`).

Running the real container is not free in CI, so the script is exercised directly with a
stub PATH: `id` reports the uid the test wants, `chown` and `gosu` are no-ops/exec
shims. Everything the assertions look at — the marker file, its mode, its content — is
produced by the real script, not by a re-implementation of it.

Each test reddens if its guard is removed (noted inline).
"""

from __future__ import annotations

import os
import stat
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
    `chmod` is deliberately NOT stubbed — the mode the script sets is asserted below.
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


def _marker(tmp_path: Path) -> Path:
    return tmp_path / "data" / "restore" / "continuity-marker"


# --------------------------------------------------------------------------- #
# The script itself has to parse
# --------------------------------------------------------------------------- #
def test_entrypoint_is_valid_sh():
    # Reddens on any syntax error: a broken entrypoint bricks every container start,
    # and nothing else in CI executes this file.
    assert subprocess.run(["sh", "-n", str(ENTRYPOINT)]).returncode == 0


# --------------------------------------------------------------------------- #
# (a) marker ABSENT -> created, with a value, readable by the service uid
# --------------------------------------------------------------------------- #
def test_creates_marker_when_absent(tmp_path):
    marker = _marker(tmp_path)
    res = _run(tmp_path, env={"RESTORE_MARKER_PATH": str(marker)})

    assert res.returncode == 0, res.stderr
    # Reddens if the creation branch is dropped: without it the file never appears and
    # restore detection sits on MARKER_MISSING forever.
    assert marker.exists()
    value = marker.read_text().strip()
    assert value, "marker must not be empty — an empty file is a value nobody can change from"
    # 644: the file is written as root and READ as uid 1000. Reddens if the chmod goes,
    # under a umask that would otherwise produce 600 (see DEPLOY.md §8).
    assert stat.S_IMODE(marker.stat().st_mode) == 0o644


def test_created_marker_value_is_unique_per_install(tmp_path):
    # Two independent "installs" must not end up with the same marker value: a shared
    # constant would make every restore look like continuity.
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _run(a, env={"RESTORE_MARKER_PATH": str(_marker(a))})
    _run(b, env={"RESTORE_MARKER_PATH": str(_marker(b))})
    assert _marker(a).read_text().strip() != _marker(b).read_text().strip()


# --------------------------------------------------------------------------- #
# (b) marker PRESENT -> never rewritten
# --------------------------------------------------------------------------- #
def test_never_overwrites_an_existing_marker(tmp_path):
    marker = _marker(tmp_path)
    marker.parent.mkdir(parents=True)
    marker.write_text("operator-written-value\n")

    # Several starts in a row — a restart loop must not disturb the value either.
    for _ in range(3):
        res = _run(tmp_path, env={"RESTORE_MARKER_PATH": str(marker)})
        assert res.returncode == 0, res.stderr

    # THE guard of this feature: the value an operator wrote during a restore is the
    # evidence of that restore. Reddens the moment an "update the marker" path appears.
    assert marker.read_text() == "operator-written-value\n"


def test_does_not_recreate_an_empty_existing_marker(tmp_path):
    # `-e`, not `-s`: an existing but empty file is still a state the operator (or a
    # failed write) put there, and rewriting it would be an unannounced change of a
    # fingerprint component.
    marker = _marker(tmp_path)
    marker.parent.mkdir(parents=True)
    marker.write_text("")
    _run(tmp_path, env={"RESTORE_MARKER_PATH": str(marker)})
    assert marker.read_text() == ""


# --------------------------------------------------------------------------- #
# (c) detection switched OFF -> nothing is created
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("env", [{}, {"RESTORE_MARKER_PATH": ""}])
def test_no_marker_when_path_is_not_configured(tmp_path, env):
    res = _run(tmp_path, env=env)
    assert res.returncode == 0, res.stderr
    # An empty RESTORE_MARKER_PATH means "detection off" (src/settings.py). Inventing a
    # path here would turn a documented off-state into a silent on-state.
    assert not (tmp_path / "data").exists()


# --------------------------------------------------------------------------- #
# The payload command still runs, and BACKUP_DIR is still prepared
# --------------------------------------------------------------------------- #
def test_execs_the_payload_and_creates_backup_dir(tmp_path):
    marker = _marker(tmp_path)
    backups = tmp_path / "data" / "backups"
    bin_dir = _stub_bin(tmp_path, "0")
    res = subprocess.run(
        ["sh", str(ENTRYPOINT), "sh", "-c", "echo PAYLOAD-RAN"],
        env={
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(tmp_path),
            "BACKUP_DIR": str(backups),
            "RESTORE_MARKER_PATH": str(marker),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, res.stderr
    # Reddens if the marker work is put AFTER the exec, or if it exits instead of
    # falling through: the container would start without ever running the app.
    assert "PAYLOAD-RAN" in res.stdout
    assert backups.is_dir()
    assert marker.exists()


# --------------------------------------------------------------------------- #
# Non-root branch (compose `user:` override) still fails fast on an unusable volume
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    os.access("/app/data", os.W_OK), reason="/app/data exists and is writable here"
)
def test_non_root_fails_fast_when_data_is_not_writable(tmp_path):
    res = _run(tmp_path, uid="1000", env={"RESTORE_MARKER_PATH": str(_marker(tmp_path))})
    # The override branch must refuse to start rather than let sqlite fail later with
    # "attempt to write a readonly database".
    assert res.returncode == 1
    assert "FATAL" in res.stderr
