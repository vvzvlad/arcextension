"""Task G (§8/§9, issue #35): the enrollment observability metrics and the
`instancegen bundle` universal-build tool.

Covers:
  * ``curator_enroll_window_seconds_remaining`` — the clamped gauge semantics
    (no window = 0, open = positive, armed-but-past = 0, at-deadline = 0)
    at both the pure-helper level and through a real ``/metrics`` scrape, including the
    "survives a restart" acceptance (the gauge is DB-derived, not process memory).
  * ``curator_auth_rejections_total{reason}`` — the newly LABELED counter family, one
    series per coarse reason (so the §37 alert can key on ``reason="enroll_bad_code"``),
    and the empty-breakdown case emitting a still-valid exposition.
  * ``instancegen bundle`` — a hostless, key-free, instance.json-free universal bundle
    that any two runs produce byte-for-byte identically apart from the build minute inside
    the manifest's ``version``, which the build stamps on purpose (acc 16). That one
    value is normalised, not skipped: every file, including ``manifest.json``, is compared.

Each assertion is written so that removing the guard it names reddens the test.
"""

from __future__ import annotations

import hashlib
import json
import re
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
from tools.instancegen import cli, core

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
def test_enroll_window_seconds_remaining_clamped_at_zero():
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
    # Armed but PAST the deadline -> CLAMPED to 0: a naturally-expired window reads closed,
    # not overdue (symmetric with the no-window case; the overdue alert was dropped in #37).
    # Redden: return the negative magnitude and a benign expired window reads as an anomaly.
    assert _enroll_window_seconds_remaining(Snapshot(enroll_window_until=now - 5_000), now) == 0
    assert _enroll_window_seconds_remaining(Snapshot(enroll_window_until=now - 400), now) == 0


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


def test_enroll_window_gauge_armed_past_deadline_is_zero(tmp_path):
    import time

    s = _settings(tmp_path)
    app = create_app(s)
    with TestClient(app) as client:
        # An armed deadline already 5 s in the PAST, not yet closed (the row lingers) ->
        # the gauge CLAMPS to 0: a naturally-expired window reads closed, not overdue.
        _set_setting(s.db_path, ENROLL_WINDOW_UNTIL_KEY, int(time.time() * 1000) - 5_000)
        v = _scalar(_scrape(client), "curator_enroll_window_seconds_remaining")
        assert v == 0.0, v


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
# instancegen bundle — universal, hostless, key-free, no instance.json
# --------------------------------------------------------------------------- #
# The build minute the stamp puts in `version`'s FOURTH component (`<HH*100+MM>`) — the ONE
# thing in a built tree that is allowed to differ between two runs. Anchored to the
# `"version"` line so the `"//version"` prose above it is never touched, and requiring all
# four components so an unstamped (verbatim-copied) manifest simply does not match.
_BUILD_MINUTE_RE = re.compile(
    r'^(\s*"version": "\d+\.\d+\.\d+)\.\d+"', re.MULTILINE
)


def _without_build_minute(text: str) -> str:
    """*text* with the build stamp's trailing build minute replaced by a constant.

    Normalising that one component — rather than dropping the field or the whole file — is
    what keeps everything else under comparison: the rest of `version`, key order,
    indentation and `ensure_ascii`.

    Be precise about what that buys. These are TWO BUILDS ON THE SAME MACHINE, so what
    they can see is exactly what DIFFERS BETWEEN RUNS: a uuid4, a pid, a random salt, a
    counter, an unnormalised clock — any of those smuggled anywhere into the built tree
    reddens them. A value that is CONSTANT on one machine — a user name, the absolute
    build directory, the hostname — is invisible to a two-run comparison by construction
    and always will be; it would be stamped identically into both builds. That half of the
    guarantee is carried instead by the FORMAT assertions in
    `tests/test_instancegen.py::test_cli_bundle_stamps_the_version` and
    `::test_cli_bundle_writes_no_version_name_and_a_short_version`, which pin the whole
    shape of the stamped version — four integer components and nothing else — and so reject
    an extra field regardless of whether it varies.
    """
    return _BUILD_MINUTE_RE.sub(r'\1.<minute>"', text)


def _manifest_text(root: Path) -> str:
    """The built manifest's TEXT, build-minute-normalised. Nothing else is dropped.

    Deliberately NOT a parsed dict: the digest this replaces held key order, indentation and
    `ensure_ascii`, and comparing dicts would let a re-serialisation with `sort_keys=True`
    or `ensure_ascii=True` stay green. Also deliberately does NOT require a stamp to be
    present: built from a tarball with no `.git`, the build degrades to a verbatim copy and
    these tests must still be testing reproducibility, not the availability of git.
    """
    return _without_build_minute((root / "manifest.json").read_text(encoding="utf-8"))


def _content_digest(root: Path) -> list[tuple[str, str]]:
    """Sorted (relpath, sha256-of-contents) for EVERY file under *root*.

    Compares file CONTENTS only (not mtimes/metadata), which is what "byte-identical"
    means for a reproducible build. No file is excluded — a skip list would leave the
    guarantee with a file-sized hole. `manifest.json`, the one file the build rewrites, is
    hashed with only its build minute normalised, so it is held to the same bar as the
    rest of the tree.
    """
    digests = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root))
        if rel == "manifest.json":
            payload = _manifest_text(root).encode("utf-8")
        else:
            payload = path.read_bytes()
        digests.append((rel, hashlib.sha256(payload).hexdigest()))
    return digests


def test_bundle_produces_a_hostless_key_free_manifest_no_instance_json(tmp_path):
    out = tmp_path / "dist"
    rc = cli.main(
        ["bundle", "--out", str(out), "--extension-dir", str(REPO_EXTENSION)]
    )
    assert rc == 0
    manifest = json.loads((out / "manifest.json").read_text())
    # NO `key` field. An extension id is not pinned anymore (nothing checks an origin), and
    # a leftover placeholder would be an INVALID key that stops Brave loading the unpacked
    # extension at all. Redden: put a `key` back in extension/manifest.json.
    assert "key" not in manifest
    # Hostless: ONLY <all_urls>, no per-host patterns, no <host> placeholder.
    assert manifest["host_permissions"] == ["<all_urls>"]
    assert not any("<host>" in p for p in manifest["host_permissions"])
    # NO instance.json (universal build — serviceUrl/token are per-profile). Redden: have
    # bundle write instance.json and this fails.
    assert not (out / "instance.json").exists()


def test_bundle_two_runs_byte_identical(tmp_path):
    d1 = tmp_path / "b1"
    d2 = tmp_path / "b2"
    for d in (d1, d2):
        cli.main(["bundle", "--out", str(d), "--extension-dir", str(REPO_EXTENSION)])
    # A copy with no CONFIGURATION stamped into it -> identical trees, WHOLE tree included
    # (acc 16). The single licensed variation is the build stamp's trailing minute,
    # normalised inside the digest; the rest of `version` and every byte of every other
    # file must match. Redden: reintroduce any stamping step whose input can vary between
    # runs — of anything else, or of `version`'s first three components.
    assert _content_digest(d1) == _content_digest(d2)
    # The same guarantee for the one rewritten file, compared as TEXT so a failure reads as
    # a diff instead of a hash mismatch.
    assert _manifest_text(d1) == _manifest_text(d2)


def test_bundle_writes_no_secret_material_beside_or_inside_the_output(tmp_path):
    # The generator used to persist an RSA private key in a `.instancegen` sibling of the
    # bundle. Nothing generates or stores key material anymore, so neither the distributed
    # tree nor its parent gains a secret. Redden: bring key generation back.
    root = tmp_path / "fleet"
    out = root / "dist"
    assert cli.main(["bundle", "--out", str(out), "--extension-dir", str(REPO_EXTENSION)]) == 0
    assert not (root / ".instancegen").exists()
    assert not (out / ".instancegen").exists()
    assert list(root.rglob("*.pem")) == []


def test_bundle_refuses_an_existing_out_dir(tmp_path):
    out = tmp_path / "dist"
    out.mkdir()
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["bundle", "--out", str(out), "--extension-dir", str(REPO_EXTENSION)])
    # The refusal must name --force and say WHY rebuilding in place is the right answer:
    # without that pointer the only documented way out is a new dir, which changes the
    # chrome-extension:// id. Redden: drop the hint from cmd_bundle's message.
    message = str(excinfo.value)
    assert "--force" in message
    assert "chrome-extension" in message
    # …and it must not have touched the dir it refused.
    assert list(out.iterdir()) == []


# --------------------------------------------------------------------------- #
# `bundle --force`: rebuild IN PLACE, because the id is the load path's hash
# --------------------------------------------------------------------------- #
def _fake_extension(root: Path, chunk_name: str) -> Path:
    """A minimal source bundle whose hashed chunk name we control.

    Stands in for `startpage`'s Vite output: every build emits `assets/index-<hash>.js`
    under a DIFFERENT name, which is exactly the file a merge-instead-of-replace rebuild
    would leave behind forever.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(json.dumps({"name": "x", "version": "1"}))
    assets = root / "startpage" / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / chunk_name).write_text(f"// built chunk {chunk_name}\n")
    return root


def test_bundle_force_rebuilds_the_same_path_byte_for_byte(tmp_path):
    # --force must leave a tree identical to a FRESH build at the SAME path: same dir, no
    # merge residue, no staging siblings left over. The path is the whole point — Chromium
    # hashes it into the chrome-extension:// id. Redden: make --force write elsewhere (or
    # leave its staging dir behind) and the path/sibling assertions fail.
    live = tmp_path / "live" / "dist"
    reference = tmp_path / "ref" / "dist"
    for d in (live, reference):
        assert cli.main(
            ["bundle", "--out", str(d), "--extension-dir", str(REPO_EXTENSION)]
        ) == 0
    before_inode = live.stat().st_ino

    assert cli.main(
        ["bundle", "--out", str(live), "--extension-dir", str(REPO_EXTENSION), "--force"]
    ) == 0

    assert live.is_dir()
    # Whole-tree comparison, manifest.json included: only the stamp's build minute is
    # normalised (same rule as the two-runs test above).
    assert _content_digest(live) == _content_digest(reference)
    assert _manifest_text(live) == _manifest_text(reference)
    # The dir is a NEW inode (it was swapped, not merged into) at the SAME path…
    assert live.stat().st_ino != before_inode
    # …and nothing was left beside it — a leftover staging dir would sit in this parent.
    assert [p.name for p in live.parent.iterdir()] == ["dist"]


def test_bundle_force_removes_a_renamed_chunk_from_the_previous_build(tmp_path):
    # THE bug this flag exists for: every startpage build emits a differently-hashed
    # `assets/index-<hash>.js`. A rebuild that merged on top of the old tree would keep
    # BOTH chunks forever (and the stale one is the one a cached index.html may load).
    # Redden: implement --force as copytree(dirs_exist_ok=True) and the old chunk stays.
    src = _fake_extension(tmp_path / "src", "index-OLDHASH.js")
    out = tmp_path / "dist"
    assert cli.main(["bundle", "--out", str(out), "--extension-dir", str(src)]) == 0
    assert (out / "startpage" / "assets" / "index-OLDHASH.js").is_file()

    # Next build of the same source emits a chunk under a new name.
    (src / "startpage" / "assets" / "index-OLDHASH.js").unlink()
    (src / "startpage" / "assets" / "index-NEWHASH.js").write_text("// built chunk new\n")

    assert cli.main(
        ["bundle", "--out", str(out), "--extension-dir", str(src), "--force"]
    ) == 0

    assert (out / "startpage" / "assets" / "index-NEWHASH.js").is_file()
    assert not (out / "startpage" / "assets" / "index-OLDHASH.js").exists()
    assert sorted(p.name for p in (out / "startpage" / "assets").iterdir()) == [
        "index-NEWHASH.js"
    ]


def test_bundle_force_on_a_missing_out_dir_just_builds_it(tmp_path):
    # --force is "rebuild THIS path", not "there must already be something here": passing
    # it on a first build must succeed, so the make target does not need two code paths.
    out = tmp_path / "dist"
    assert cli.main(
        ["bundle", "--out", str(out), "--extension-dir", str(REPO_EXTENSION), "--force"]
    ) == 0
    assert (out / "manifest.json").is_file()
    assert [p.name for p in out.parent.iterdir()] == ["dist"]


def test_a_failed_force_rebuild_leaves_the_previous_bundle_loadable(tmp_path, monkeypatch):
    # The reason --force stages the copy instead of rmtree'ing the target: if the build
    # blows up halfway, the operator must still have a LOADABLE extension at that path —
    # the browser has it loaded unpacked right now. Redden: implement --force as
    # `shutil.rmtree(out); copy_bundle(...)` and the old manifest is gone after the raise.
    src = _fake_extension(tmp_path / "src", "index-OLDHASH.js")
    out = tmp_path / "dist"
    assert cli.main(["bundle", "--out", str(out), "--extension-dir", str(src)]) == 0
    previous = _content_digest(out)

    def boom(*_args, **_kwargs):
        raise RuntimeError("copy died halfway")

    monkeypatch.setattr(core, "copy_bundle", boom)
    with pytest.raises(RuntimeError, match="copy died halfway"):
        cli.main(["bundle", "--out", str(out), "--extension-dir", str(src), "--force"])

    # Same path, same intact tree, and no staging dir left rotting beside it.
    assert (out / "manifest.json").is_file()
    assert _content_digest(out) == previous
    assert sorted(p.name for p in out.parent.iterdir()) == ["dist", "src"]


def test_replace_bundle_validates_the_source_before_touching_the_target(tmp_path):
    # A wrong --extension-dir must fail BEFORE the existing bundle is disturbed (same
    # invariant `generate` has for a bad --bundle-dir). Redden: move the manifest check
    # after the swap and the live bundle is destroyed by a typo.
    src = _fake_extension(tmp_path / "src", "index-OLDHASH.js")
    out = tmp_path / "dist"
    assert cli.main(["bundle", "--out", str(out), "--extension-dir", str(src)]) == 0
    previous = _content_digest(out)

    not_a_bundle = tmp_path / "empty"
    not_a_bundle.mkdir()
    with pytest.raises(ValueError, match="manifest.json"):
        core.replace_bundle(not_a_bundle, out)

    assert _content_digest(out) == previous


def test_force_refuses_to_rebuild_a_dir_that_contains_the_source(tmp_path):
    # `--out extension` (or any parent of it) is a plausible typo, and the rebuild
    # REPLACES that whole directory — i.e. it would delete the very source it copies from.
    # Redden: drop the src-inside-dst guard in replace_bundle and the repo bundle can be
    # eaten by one wrong --out.
    src = _fake_extension(tmp_path / "workspace" / "extension", "index-OLDHASH.js")
    workspace = tmp_path / "workspace"
    with pytest.raises(ValueError, match="refusing"):
        core.replace_bundle(src, workspace)
    with pytest.raises(ValueError, match="refusing"):
        core.replace_bundle(src, src)
    assert (src / "manifest.json").is_file()


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
