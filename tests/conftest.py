import os

# Provide the required tokens BEFORE any test module imports src.settings
# (Settings() is instantiated at import time and would otherwise fail because
# EXT_TOKEN / METRICS_TOKEN have no default). In CI the same variables are
# injected via the workflow's `env:` block.
os.environ.setdefault("EXT_TOKEN", "test-ext-token")
os.environ.setdefault("METRICS_TOKEN", "test-metrics-token")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

import threading  # noqa: E402
from types import SimpleNamespace  # noqa: E402 - must follow the env defaults above

import pytest  # noqa: E402

EXT_TOKEN = "test-ext-token"
METRICS_TOKEN = "test-metrics-token"
ADMIN_TOKEN = "test-admin-token"

# --- THE settings surface for tests ------------------------------------------
# Every test that builds an app used to hand-roll its own ``SimpleNamespace``. Nine of
# them had drifted apart, and the gaps were not cosmetic: a fixture missing
# ``lease_ttl_ms`` turned a background curator pass — which the TestClient's real
# lifespan starts — into a bare ``AttributeError`` inside an unrelated test, five
# minutes after it began. One place to add a field is the point.
#
# Mirrors ``src.settings.Settings`` field for field (the app reads attributes off this
# object, so a missing one is an AttributeError at the worst possible moment). When
# ``Settings`` gains a field, add it HERE and every test file inherits it.
_DEFAULTS: dict = {
    # tokens / transport
    "ext_token": EXT_TOKEN,
    "metrics_token": METRICS_TOKEN,
    "admin_token": ADMIN_TOKEN,
    "protocol_version": 1,
    "ext_allowed_origins": "",
    "host": "0.0.0.0",
    "port": 8000,
    # timings — deliberately test-shaped, not production-shaped
    "idle_minutes": 60,
    "tick_ms": 60_000,
    # A ping in the middle of a synchronous websocket exchange steals the frame the
    # test is waiting for, so the heartbeat is parked far away.
    "heartbeat_ms": 600_000,
    "cmd_timeout_ms": 2_000,
    "snapshot_timeout_ms": 2_000,
    "lease_ttl_ms": 600_000,
    "state_fresh_ms": 3_000_000,
    # horizons / windows
    "restore_exemption_min": 120,
    "pause_default_min": 60,
    "incomplete_after_min": 15,
    "self_nav_limit": 10,
    "quarantine_ttl_min": 1440,
    "actions_retention_days": 90,
    "js_audit_retention_days": 730,
    # identity / misc
    "main_instance_id": "main",
    "restore_marker_path": "",
    "log_level": "INFO",
    "enroll_window_min": 10,
}

# Parked far beyond any test's lifetime. ``src.app._curator_driver`` sleeps this long
# before running a REAL pass; a test that blocks past it (a socket wait that never
# arrives, a slow machine) otherwise gets a live curator pass executing underneath it,
# against its own database, mid-assertion. A test that genuinely exercises the interval
# passes its own value.
PARKED_PASS_INTERVAL_MIN = 100_000


def make_settings(tmp_path=None, **over) -> SimpleNamespace:
    """The full settings surface, rooted at ``tmp_path``; ``**over`` wins.

    Usable two ways, so a module can adopt it without rewriting every call site:

    * as a fixture — ``def test_x(settings_factory, tmp_path): s = settings_factory(tmp_path)``
    * as a plain import — ``from conftest import make_settings`` (``tests/`` is on
      ``sys.path`` during collection, and pytest loads this same file as ``conftest``,
      so there is no second copy of the module).

    ``tmp_path=None`` is for the tests that never build an app (they call functions
    directly and open their own ``Database``): ``db_path`` / ``backup_dir`` are then left
    OFF the object entirely, so touching one raises a named ``AttributeError`` instead of
    quietly pointing at ``"None/curator.db"``.
    """
    values = dict(_DEFAULTS)
    if tmp_path is not None:
        values["db_path"] = str(tmp_path / "curator.db")
        values["backup_dir"] = str(tmp_path / "backups")
    values["pass_interval_min"] = PARKED_PASS_INTERVAL_MIN
    values.update(over)
    return SimpleNamespace(**values)


@pytest.fixture
def settings_factory():
    """``settings_factory(tmp_path, **over)`` -> the shared settings object."""
    return make_settings


# --- bounded socket waits ----------------------------------------------------
# TestClient's ``receive_json`` blocks FOREVER. A frame that never arrives therefore
# turned a test into a multi-minute hang that read as "the suite is slow" instead of a
# red test — and, once the hang outlived ``PASS_INTERVAL_MIN``, the app's real periodic
# curator driver woke up INSIDE the test and ran a live pass against it. Every wait for a
# frame goes through this helper so a missing frame fails fast and says so.
#
# Lives HERE, not copied per file, for the same reason ``make_settings`` does: seven
# verbatim copies is the drift this module exists to stop.
_RECV_TIMEOUT_S = 5.0


def _recv(ws, timeout=_RECV_TIMEOUT_S):
    """``ws.receive_json()`` with a hard deadline; AssertionError if nothing arrives.

    Runs the blocking read on a daemon thread and re-raises whatever it produced on the
    caller's thread, so tests that EXPECT ``WebSocketDisconnect`` keep working.
    """
    box = {}

    def _pull():
        try:
            box["frame"] = ws.receive_json()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            box["error"] = exc

    thread = threading.Thread(target=_pull, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise AssertionError(
            f"no websocket frame arrived within {timeout}s "
            "(the expected command/snapshot_request was never sent)"
        )
    if "error" in box:
        raise box["error"]
    return box["frame"]
