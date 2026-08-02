"""Task G (§8/§9, issue #35): the enrollment observability metrics and the
`instancegen bundle` universal-build tool.

Covers:
  * ``curator_enroll_window_seconds_remaining`` — the SIGNED/zero gauge semantics
    (no window = 0, open = positive, armed-but-past = negative, exactly-at-deadline = 0)
    at both the pure-helper level and through a real ``/metrics`` scrape, including the
    "survives a restart" acceptance (the gauge is DB-derived, not process memory).
  * ``curator_auth_rejections_total{reason}`` — the newly LABELED counter family, one
    series per coarse reason (so the §37 alert can key on ``reason="enroll_bad_code"``),
    and the empty-breakdown case emitting a still-valid exposition.
  * ``instancegen bundle`` — a key-pinned, hostless, instance.json-free universal bundle
    that two runs with the same key produce byte-for-byte identically (acc 16).

Each assertion is written so that removing the guard it names reddens the test.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from conftest import METRICS_TOKEN, make_settings
from starlette.testclient import TestClient

from src.api.auth_metrics import auth_rejections
from src.api.metrics import (
    MetricsRegistry,
    Snapshot,
    _enroll_window_seconds_remaining,
)
from src.app import create_app
from src.curator.enroll import ENROLL_WINDOW_UNTIL_KEY
from tools.instancegen import cli, core, keys

REPO_EXTENSION = Path(__file__).resolve().parents[1] / "extension"
MAUTH = {"Authorization": f"Bearer {METRICS_TOKEN}"}


def _settings(tmp_path, **over):
    return make_settings(tmp_path, **over)


def _set_setting(db_path, key, value):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        conn.commit()
    finally:
        conn.close()


def _scrape(client):
    r = client.get("/metrics", headers=MAUTH)
    assert r.status_code == 200
    return r.text


def _samples(body, name):
    """All sample lines of ``name`` as ``(labels_str, value_float)`` (skips HELP/TYPE)."""
    out = []
    for line in body.splitlines():
        if line.startswith("#"):
            continue
        if line.startswith(name + " "):
            out.append(("", float(line[len(name) + 1:].strip())))
        elif line.startswith(name + "{"):
            labels, _, val = line[len(name) + 1:].partition("} ")
            out.append((labels, float(val.strip())))
    return out


def _scalar(body, name):
    s = _samples(body, name)
    assert len(s) == 1 and s[0][0] == "", f"{name}: expected one unlabelled sample, got {s}"
    return s[0][1]


# --------------------------------------------------------------------------- #
# curator_enroll_window_seconds_remaining — the pure helper (signed / zero edges)
# --------------------------------------------------------------------------- #
def test_enroll_window_seconds_remaining_signed_and_zero():
    now = 1_000_000
    # No window armed -> EXACTLY 0.
    assert _enroll_window_seconds_remaining(Snapshot(), now) == 0
    # Armed and open, whole seconds ahead -> POSITIVE remaining (ceil).
    assert _enroll_window_seconds_remaining(Snapshot(enroll_window_until=now + 30_000), now) == 30
    # Sub-second but still open -> rounds UP to >= 1, never colliding with the no-window 0.
    # Redden: floor here (`// 1000`) and this open window reads 0, indistinguishable from
    # "no window".
    assert _enroll_window_seconds_remaining(Snapshot(enroll_window_until=now + 400), now) == 1
    # EXACTLY at the deadline -> 0, deliberately indistinguishable from "no window".
    assert _enroll_window_seconds_remaining(Snapshot(enroll_window_until=now), now) == 0
    # Armed but PAST the deadline and not closed -> NEGATIVE (the §37 alert keys on < 0).
    # Redden: clamp to 0 like read_enroll_window and the overdue window goes invisible.
    assert _enroll_window_seconds_remaining(Snapshot(enroll_window_until=now - 5_000), now) == -5
    assert _enroll_window_seconds_remaining(Snapshot(enroll_window_until=now - 400), now) == -1


# --------------------------------------------------------------------------- #
# curator_enroll_window_seconds_remaining — through a real /metrics scrape
# --------------------------------------------------------------------------- #
def test_enroll_window_gauge_no_window_is_zero(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        body = _scrape(client)
        # Always emitted, and 0 when nothing is armed. Redden: register the gauge only
        # when a window exists and this becomes a missing series (NoData).
        assert "# HELP curator_enroll_window_seconds_remaining " in body
        assert _scalar(body, "curator_enroll_window_seconds_remaining") == 0.0


def test_enroll_window_gauge_open_window_is_positive(tmp_path):
    import time

    s = _settings(tmp_path)
    app = create_app(s)
    with TestClient(app) as client:
        # Arm a window ~10 min into the future.
        _set_setting(s.db_path, ENROLL_WINDOW_UNTIL_KEY, int(time.time() * 1000) + 600_000)
        v = _scalar(_scrape(client), "curator_enroll_window_seconds_remaining")
        # Positive and close to the 600 s remaining (a few seconds of slack for the read).
        assert 590.0 < v <= 600.0, v


def test_enroll_window_gauge_armed_past_deadline_is_negative(tmp_path):
    import time

    s = _settings(tmp_path)
    app = create_app(s)
    with TestClient(app) as client:
        # An armed deadline already 5 s in the PAST, not yet closed (the row lingers) ->
        # the gauge must go NEGATIVE, which is what the future §37 alert keys on.
        _set_setting(s.db_path, ENROLL_WINDOW_UNTIL_KEY, int(time.time() * 1000) - 5_000)
        v = _scalar(_scrape(client), "curator_enroll_window_seconds_remaining")
        assert v < 0.0, v


def test_enroll_window_gauge_survives_restart(tmp_path):
    import time

    s = _settings(tmp_path)
    app = create_app(s)
    # The DB is created by the app lifespan, so arm the window inside the first client.
    with TestClient(app) as client:
        _set_setting(s.db_path, ENROLL_WINDOW_UNTIL_KEY, int(time.time() * 1000) + 600_000)
        assert _scalar(_scrape(client), "curator_enroll_window_seconds_remaining") > 0.0

    # A brand-new app object (empty process memory) on the SAME DB still reflects the
    # stored deadline — proving the gauge is read from `settings`, not held in memory
    # (acc 14: a window that outlives a restart still reads > 0).
    app2 = create_app(_settings(tmp_path))
    with TestClient(app2) as client2:
        assert _scalar(_scrape(client2), "curator_enroll_window_seconds_remaining") > 0.0


def test_enroll_window_gauge_served_in_degraded_mode(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        # DB pulled out entirely: /metrics must still answer (no 500) and the gauge
        # degrades to 0 (no window), never raising.
        client.app.state.db = None
        body = _scrape(client)
        assert _scalar(body, "curator_enroll_window_seconds_remaining") == 0.0


# --------------------------------------------------------------------------- #
# curator_auth_rejections_total{reason} — the labeled counter family
# --------------------------------------------------------------------------- #
def test_auth_rejections_labeled_by_reason(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        before = {lbls: v for lbls, v in _samples(_scrape(client), "curator_auth_rejections_total")}
        # Seed a few rejections directly on the process singleton, exactly as the guards
        # do (auth_rejections.incr(reason)).
        auth_rejections.incr("enroll_bad_code")
        auth_rejections.incr("enroll_bad_code")
        auth_rejections.incr("api_token")

        body = _scrape(client)
        got = {lbls: v for lbls, v in _samples(body, "curator_auth_rejections_total")}
        # Each series carries a {reason}; the per-reason count moved by exactly what we
        # seeded. Redden: drop the reason label (fold to one unlabelled total) and these
        # by-label keys vanish -> KeyError.
        assert got['reason="enroll_bad_code"'] == before.get('reason="enroll_bad_code"', 0.0) + 2
        assert got['reason="api_token"'] == before.get('reason="api_token"', 0.0) + 1
        # The label §37 alerts on must be emittable as its own line.
        assert 'curator_auth_rejections_total{reason="enroll_bad_code"}' in body
        # No bare (unlabelled) sample leaks — the family is fully labeled.
        assert "\ncurator_auth_rejections_total " not in body


def test_auth_rejections_empty_breakdown_is_valid_exposition():
    # A fresh process whose by_reason() is empty must still render a valid exposition:
    # HELP/TYPE present, NO sample line (matches the other per-label families). Redden:
    # emit a spurious/malformed line for an empty samples list and a scrape breaks.
    reg = MetricsRegistry()
    reg.metric("curator_auth_rejections_total", "help", "counter", [])
    out = reg.render()
    assert "# HELP curator_auth_rejections_total help" in out
    assert "# TYPE curator_auth_rejections_total counter" in out
    assert "\ncurator_auth_rejections_total " not in out
    assert "curator_auth_rejections_total{" not in out


# --------------------------------------------------------------------------- #
# instancegen bundle — universal, key-pinned, hostless, no instance.json
# --------------------------------------------------------------------------- #
def _key_file(tmp_path) -> Path:
    kp = tmp_path / "signing_key.pem"
    keys.load_or_create_private_key_pem(kp)
    return kp


def _content_digest(root: Path) -> list[tuple[str, str]]:
    """Sorted (relpath, sha256-of-contents) for every file under *root*.

    Compares file CONTENTS only (not mtimes/metadata), which is what "byte-identical"
    means for a reproducible build.
    """
    return [
        (str(p.relative_to(root)), hashlib.sha256(p.read_bytes()).hexdigest())
        for p in sorted(root.rglob("*"))
        if p.is_file()
    ]


def test_bundle_produces_key_pinned_hostless_manifest_no_instance_json(tmp_path):
    keyfile = _key_file(tmp_path)
    out = tmp_path / "dist"
    rc = cli.main(
        ["bundle", "--out", str(out), "--extension-dir", str(REPO_EXTENSION),
         "--key-file", str(keyfile)]
    )
    assert rc == 0
    manifest = json.loads((out / "manifest.json").read_text())
    # key pinned to the file's public half, not the placeholder. Redden: skip the stamp
    # and the placeholder leaks.
    expected_key = keys.public_key_b64_from_pem(keyfile.read_bytes())
    assert manifest["key"] == expected_key
    assert manifest["key"] != core.KEY_PLACEHOLDER
    # Hostless: ONLY <all_urls>, no per-host patterns, no <host> placeholder.
    assert manifest["host_permissions"] == ["<all_urls>"]
    assert not any("<host>" in p for p in manifest["host_permissions"])
    # NO instance.json (universal build — serviceUrl/token are per-profile). Redden: have
    # bundle write instance.json and this fails.
    assert not (out / "instance.json").exists()


def test_bundle_two_runs_byte_identical(tmp_path):
    keyfile = _key_file(tmp_path)
    d1 = tmp_path / "b1"
    d2 = tmp_path / "b2"
    for d in (d1, d2):
        cli.main(
            ["bundle", "--out", str(d), "--extension-dir", str(REPO_EXTENSION),
             "--key-file", str(keyfile)]
        )
    # Same key + deterministic stamp + timestamp-free copy -> identical trees (acc 16).
    # Redden: sort_keys/order drift or a nondeterministic stamp and these diverge.
    assert _content_digest(d1) == _content_digest(d2)
    # --key-file was given, so no secret .instancegen and no instance.json were created.
    assert not (d1 / ".instancegen").exists()
    assert not (d1 / "instance.json").exists()


def test_bundle_generated_key_lands_outside_the_distributed_bundle(tmp_path):
    # SECURITY: --out IS the extension bundle that ships fleet-wide, and the private
    # signing key pins the single chrome-extension:// id (predpos. 19). A generated key
    # must therefore live BESIDE the bundle, NEVER inside it — else shipping the tree
    # leaks the key and an attacker can forge an extension under the same id, defeating
    # EXT_ALLOWED_ORIGINS. Redden: point _resolve_key back at out_dir and the key
    # reappears inside the distributed tree.
    root = tmp_path / "fleet"
    out = root / "dist"
    rc = cli.main(["bundle", "--out", str(out), "--extension-dir", str(REPO_EXTENSION)])
    assert rc == 0
    # The generated key lives in a .instancegen SIBLING of the bundle (symmetric with
    # `generate`), OUTSIDE out_dir.
    assert (root / ".instancegen" / "signing_key.pem").is_file()
    # The distributed bundle carries NO private key material at all.
    assert not (out / ".instancegen").exists()
    assert list(out.rglob("*.pem")) == []
    # …and it is still a valid key-pinned, hostless, instance.json-free bundle.
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["key"] != core.KEY_PLACEHOLDER  # a real generated key was pinned
    assert manifest["host_permissions"] == ["<all_urls>"]
    assert not (out / "instance.json").exists()


def test_bundle_refuses_an_existing_out_dir(tmp_path):
    out = tmp_path / "dist"
    out.mkdir()
    with pytest.raises(SystemExit):
        cli.main(["bundle", "--out", str(out), "--extension-dir", str(REPO_EXTENSION),
                  "--key-file", str(_key_file(tmp_path))])


def test_bundle_rejects_token_service_url_and_instance_id_options(tmp_path):
    # The universal build bakes in NO token/url/instanceId (§9): argparse must reject
    # each. Redden: re-add any of these options to the bundle subparser.
    parser = cli.build_parser()
    base = ["bundle", "--out", str(tmp_path / "x")]
    for extra in (
        ["--token", "leaked-secret"],
        ["--token-file", "/p"],
        ["--service-url", "wss://h"],
        ["--instance-id", "i"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(base + extra)
