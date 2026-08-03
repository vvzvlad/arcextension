import os

# Provide the required tokens BEFORE any test module imports src.settings
# (Settings() is instantiated at import time and would otherwise fail because
# METRICS_TOKEN / ADMIN_TOKEN have no default). In CI the same variables are
# injected via the workflow's `env:` block.
os.environ.setdefault("METRICS_TOKEN", "test-metrics-token")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

import hashlib  # noqa: E402
import sqlite3  # noqa: E402
import threading  # noqa: E402
from types import SimpleNamespace  # noqa: E402 - must follow the env defaults above

import pytest  # noqa: E402

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
    "metrics_token": METRICS_TOKEN,
    "admin_token": ADMIN_TOKEN,
    "protocol_version": 1,
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
    "enroll_preauth_max": 128,
    "admin_session_ttl_min": 720,
    # Build identity, baked into the image at build time (src/settings.py). "unknown" is
    # what an un-stamped build reports, and it is the right default here: a test that
    # cares about the revision passes its own.
    "build_revision": "unknown",
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


# --- secret-based /ext hello helpers (enrollment, issue #35) -----------------
# Under enrollment a hello authenticates by a per-install SECRET (option A): the client
# sends the RAW secret over TLS, the server hashes it (sha256) and resolves that to an
# ACTIVE instances row, taking the server-assigned id from that row. A test that wants to
# drive the hello path must therefore first have an approved (active) row whose stored
# ``secret_hash`` is sha256(raw) — the thing Task E's operator approval creates — and then
# present the RAW secret. These helpers make that a one-liner so every /ext test converges
# on the same shape instead of hand-rolling INSERTs.


def admin_headers() -> dict:
    """Authorization for an ADMIN_TOKEN caller on ``/api/*`` and ``/mcp`` (issue #35 §4).

    The human/agent credential: opens every ``/api/*`` route of either caller kind, and
    is the DB-free branch of :func:`src.api.guards.require_api_caller`.
    """
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def instance_headers(raw_secret: str) -> dict:
    """Authorization for an INSTANCE caller on ``/api/*`` — its RAW secret (issue #35 §4,
    option A), the SAME credential the client sends on /ext hello; the server hashes it and
    matches the stored sha256. Pair with :func:`approve_instance` to have an active row the
    secret resolves to."""
    return {"Authorization": f"Bearer {raw_secret}"}


def secret_for(instance_id: str) -> str:
    """A deterministic per-instance RAW secret for tests (never a real credential) — the
    value the client presents on the wire / as the /api Bearer."""
    return f"secret-{instance_id}"


def secret_hash_for(instance_id: str) -> str:
    """sha256 hex of :func:`secret_for` — the value STORED in ``instances.secret_hash``
    (what the server computes on receipt of the raw secret). Tests seed a row with this and
    present :func:`secret_for` (the raw) on the wire."""
    return hashlib.sha256(secret_for(instance_id).encode("utf-8")).hexdigest()


def approve_instance(db_path, instance_id, *, status="active", secret_hash=None):
    """Insert (or update) an ``instances`` row so a secret-hello authenticates.

    Mimics what a successful ``enroll_request`` creates (src.db.queries.enroll_instance):
    a row whose ``id`` is the name the browser asked for, plus a ``secret_hash`` and a
    ``status`` (default 'active'). ``conn_epoch`` starts at 0 and the first hello bumps it
    to 1 via the UPDATE-only upsert.
    """
    sh = secret_hash if secret_hash is not None else secret_hash_for(instance_id)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(
            "INSERT INTO instances (id, status, secret_hash, connected, conn_epoch) "
            "VALUES (?, ?, ?, 0, 0) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, "
            "secret_hash=excluded.secret_hash",
            (instance_id, status, sh),
        )
        conn.commit()
    finally:
        conn.close()


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
