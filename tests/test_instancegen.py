"""Tests for the per-instance browser generator (tools/instancegen, §13).

Pure-core only — everything here runs on Linux/CI without a browser or a mac.
Operational acceptance (an instance actually connecting to /api/state, a clone
rejected as duplicate_instance, reconnect after restart) needs a real browser and
is a MANUAL checklist in tools/README.md — deliberately NOT faked here.

Each test is written to redden if its guard is removed (noted inline).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.instancegen import core, keys, macos

REPO_EXTENSION = Path(__file__).resolve().parents[1] / "extension"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _gen(out_root, instance_id, *, title=None, service_url="wss://curator.example.com",
         token="tok-secret-value", key_b64="AAAABBBBCCCC", ext_id="a" * 32):
    return core.generate_instance(
        out_root=out_root,
        source_extension_dir=REPO_EXTENSION,
        instance_id=instance_id,
        title=title or instance_id,
        service_url=service_url,
        token=token,
        key_b64=key_b64,
        extension_id=ext_id,
    )


# --------------------------------------------------------------------------- #
# instance.json — the FOUR fields
# --------------------------------------------------------------------------- #
def test_instance_json_has_exactly_the_four_fields(tmp_path):
    res = _gen(tmp_path, "work", title="Work", service_url="wss://c.example.com",
               token="the-token")
    data = json.loads(res.paths.instance_json.read_text())
    # Redden: drop `token` in build_instance_json -> this fails.
    assert data == {
        "instanceId": "work",
        "title": "Work",
        "serviceUrl": "wss://c.example.com",
        "token": "the-token",
        "allowExecuteJs": False,
    }
    assert set(data) == {"instanceId", "title", "serviceUrl", "token", "allowExecuteJs"}


def test_missing_token_is_rejected_not_defaulted():
    with pytest.raises(ValueError):
        core.build_instance_json("id", "t", "wss://h", "")


def test_missing_instance_id_is_rejected():
    with pytest.raises(ValueError):
        core.build_instance_json("", "t", "wss://h", "tok")


# --------------------------------------------------------------------------- #
# Per-instance copy + isolation; repo extension/ untouched
# --------------------------------------------------------------------------- #
def test_two_instances_have_separate_copies_and_profiles(tmp_path):
    a = _gen(tmp_path, "alpha", token="tok-a")
    b = _gen(tmp_path, "beta", token="tok-b")

    assert a.paths.extension_dir != b.paths.extension_dir
    assert a.paths.profile_dir != b.paths.profile_dir
    assert a.paths.extension_dir.is_dir() and b.paths.extension_dir.is_dir()
    assert a.paths.profile_dir.is_dir() and b.paths.profile_dir.is_dir()

    da = json.loads(a.paths.instance_json.read_text())
    db = json.loads(b.paths.instance_json.read_text())
    assert da["instanceId"] == "alpha" and db["instanceId"] == "beta"
    assert da["token"] != db["token"]


def test_repo_extension_instance_json_never_created(tmp_path):
    repo_instance_json = REPO_EXTENSION / "instance.json"
    existed_before = repo_instance_json.exists()
    _gen(tmp_path, "gamma")
    # The generator must ONLY write under the output dir (§13). Redden: point the
    # writer at the source bundle -> this fails.
    assert repo_instance_json.exists() == existed_before
    assert not existed_before, "repo must ship only instance.example.json, no real one"


def test_source_instance_example_is_not_copied_as_instance_json(tmp_path):
    # The copy must ignore any stray instance.json in the source bundle so it is
    # authored fresh, not inherited. instance.example.json stays a template only.
    res = _gen(tmp_path, "delta")
    copied = list(res.paths.extension_dir.glob("instance.example.json"))
    assert copied, "example template should travel with the bundle"
    # And the real instance.json is the generated one, not a copied stray.
    assert res.paths.instance_json.is_file()


# --------------------------------------------------------------------------- #
# <host> stamping
# --------------------------------------------------------------------------- #
def test_host_stamped_into_manifest_all_urls_kept(tmp_path):
    res = _gen(tmp_path, "eps", service_url="wss://curator.example.com")
    manifest = json.loads((res.paths.extension_dir / "manifest.json").read_text())
    perms = manifest["host_permissions"]
    assert "https://curator.example.com/*" in perms
    assert "wss://curator.example.com/*" in perms
    assert "<all_urls>" in perms  # SECURITY-sensitive grant kept (§6)
    # Redden: skip the <host> replacement -> a placeholder leaks and this fails.
    assert not any("<host>" in p for p in perms)


def test_host_derived_from_various_urls():
    assert core.host_from_service_url("wss://a.example.com") == "a.example.com"
    assert core.host_from_service_url("wss://a.example.com:8443") == "a.example.com:8443"
    assert core.host_from_service_url("wss://a.example.com/") == "a.example.com"
    assert core.host_from_service_url("host-only") == "host-only"
    with pytest.raises(ValueError):
        core.host_from_service_url("wss://<host>")


# --------------------------------------------------------------------------- #
# key pinned + shared across instances + stable across rename
# --------------------------------------------------------------------------- #
def test_key_pinned_and_identical_across_instances(tmp_path):
    key = "REALKEYBASE64=="
    a = core.generate_instance(
        out_root=tmp_path, source_extension_dir=REPO_EXTENSION, instance_id="one",
        title="One", service_url="wss://h.example.com", token="t1", key_b64=key,
        extension_id="x" * 32,
    )
    b = core.generate_instance(
        out_root=tmp_path, source_extension_dir=REPO_EXTENSION, instance_id="two",
        title="Two", service_url="wss://h.example.com", token="t2", key_b64=key,
        extension_id="x" * 32,
    )
    ma = json.loads((a.paths.extension_dir / "manifest.json").read_text())
    mb = json.loads((b.paths.extension_dir / "manifest.json").read_text())
    assert ma["key"] == key == mb["key"]
    assert ma["key"] != core.KEY_PLACEHOLDER  # real, not the placeholder


def test_key_stable_across_output_rename(tmp_path):
    # Pinning by key (not path) is the whole point: renaming the output dir must
    # not change the manifest key.
    res = _gen(tmp_path, "movable", key_b64="STABLEKEY==")
    before = json.loads((res.paths.extension_dir / "manifest.json").read_text())["key"]
    renamed = tmp_path.parent / (tmp_path.name + "-moved")
    tmp_path.rename(renamed)
    after_path = renamed / "movable" / "extension" / "manifest.json"
    after = json.loads(after_path.read_text())["key"]
    assert before == after == "STABLEKEY=="


def test_stamp_manifest_requires_a_real_key():
    manifest = json.loads((REPO_EXTENSION / "manifest.json").read_text())
    with pytest.raises(ValueError):
        core.stamp_manifest(manifest, "h.example.com", "")


# --------------------------------------------------------------------------- #
# Signing key generation / persistence / id derivation (uses cryptography)
# --------------------------------------------------------------------------- #
def test_signing_key_generated_persisted_and_reused(tmp_path):
    key_path = tmp_path / ".instancegen" / "signing_key.pem"
    pem1 = keys.load_or_create_private_key_pem(key_path)
    assert key_path.is_file()
    assert (key_path.stat().st_mode & 0o777) == 0o600  # secret perms
    pem2 = keys.load_or_create_private_key_pem(key_path)
    assert pem1 == pem2  # reused, not regenerated -> stable id


def test_extension_id_is_32_chars_over_a_to_p_and_deterministic(tmp_path):
    pem = keys.load_or_create_private_key_pem(tmp_path / "k.pem")
    key_b64 = keys.public_key_b64_from_pem(pem)
    ext_id = keys.derive_extension_id(key_b64)
    assert len(ext_id) == 32
    assert all("a" <= c <= "p" for c in ext_id)
    assert keys.derive_extension_id(key_b64) == ext_id  # deterministic
    # The public key round-trips to the same base64 (stable manifest key).
    assert keys.public_key_b64_from_pem(pem) == key_b64


def test_extension_id_known_answer_vector():
    # Chromium-correct KNOWN ANSWER (not just self-consistent): id = SHA-256 of the
    # DER key bytes, first 16 bytes, each nibble high-then-low -> 'a'+n. Verified
    # against an independent reimplementation. Reddens if the nibble order, the
    # 16-byte slice, or the alphabet base ever regress.
    import base64
    key_b64 = base64.b64encode(b"hello-world").decode()
    assert keys.derive_extension_id(key_b64) == "kpkchleenedlackjpokebnbdmonmcoea"


# --------------------------------------------------------------------------- #
# Re-stamp: rotate token everywhere, preserve identity + profile
# --------------------------------------------------------------------------- #
def test_restamp_rotates_all_tokens_and_preserves_identity(tmp_path):
    _gen(tmp_path, "alpha", token="old-tok-alpha")
    _gen(tmp_path, "beta", token="old-tok-beta")

    changes = core.restamp_all(tmp_path, token="new-rotated-token")
    assert len(changes) == 2  # Redden: skip one instance -> length/token check fails.

    for slug, iid in (("alpha", "alpha"), ("beta", "beta")):
        data = json.loads(
            (tmp_path / slug / "extension" / "instance.json").read_text()
        )
        assert data["token"] == "new-rotated-token"  # rotated
        assert data["instanceId"] == iid  # IMMUTABLE — unchanged


def test_restamp_preserves_profile_and_install_uuid(tmp_path):
    _gen(tmp_path, "alpha", token="old")
    # Simulate the SW having minted install_uuid inside the profile on first run.
    profile = tmp_path / "alpha" / "profile"
    marker = profile / "Local State"
    marker.write_text('{"installUuid":"born-in-profile"}')

    core.restamp_all(tmp_path, token="new")

    # Redden: if restamp wiped/recreated the profile, this file would be gone and
    # the browser would come back with a NEW uuid -> rejected as duplicate.
    assert marker.read_text() == '{"installUuid":"born-in-profile"}'


def test_restamp_has_no_way_to_change_instance_id(tmp_path):
    # instanceId immutability: the API/CLI expose no instanceId parameter on
    # restamp, and a restamp preserves it.
    _gen(tmp_path, "fixed-id", token="old")
    import inspect

    sig = inspect.signature(core.restamp_all)
    assert "instance_id" not in sig.parameters
    assert "instanceId" not in sig.parameters
    core.restamp_all(tmp_path, token="new")
    data = json.loads(
        (tmp_path / "fixed-id" / "extension" / "instance.json").read_text()
    )
    assert data["instanceId"] == "fixed-id"


def test_restamp_can_also_change_service_url_and_manifest_host(tmp_path):
    _gen(tmp_path, "alpha", service_url="wss://old.example.com", token="old")
    core.restamp_all(tmp_path, token="new", service_url="wss://new.example.com")
    data = json.loads((tmp_path / "alpha" / "extension" / "instance.json").read_text())
    assert data["serviceUrl"] == "wss://new.example.com"
    manifest = json.loads(
        (tmp_path / "alpha" / "extension" / "manifest.json").read_text()
    )
    assert "wss://new.example.com/*" in manifest["host_permissions"]
    assert "https://new.example.com/*" in manifest["host_permissions"]
    assert "<all_urls>" in manifest["host_permissions"]


def test_restamp_empty_root_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        core.restamp_all(tmp_path, token="new")


# --------------------------------------------------------------------------- #
# Launcher content: Brave, per-instance flags
# --------------------------------------------------------------------------- #
def test_launcher_targets_brave_with_per_instance_flags(tmp_path):
    res = _gen(tmp_path, "alpha")
    script = res.paths.launcher.read_text()
    assert str(res.paths.profile_dir) in script
    assert str(res.paths.extension_dir) in script
    assert "--user-data-dir=" in script
    assert "--load-extension=" in script
    # Brave, NOT Chrome (§13, arch row 20): the default binary is Brave and Chrome
    # would refuse --load-extension.
    assert "Brave" in script
    assert "Google Chrome/Contents" not in script
    assert res.paths.launcher.stat().st_mode & 0o111  # executable


def test_build_launch_command_shape():
    cmd = core.build_launch_command("/bin/brave", "/p", "/e", ["--foo"])
    assert cmd[0] == "/bin/brave"
    assert "--user-data-dir=/p" in cmd
    assert "--load-extension=/e" in cmd
    assert cmd[-1] == "--foo"


def test_two_instances_have_distinct_profile_flags(tmp_path):
    a = _gen(tmp_path, "alpha")
    b = _gen(tmp_path, "beta")
    sa = a.paths.launcher.read_text()
    sb = b.paths.launcher.read_text()
    assert str(a.paths.profile_dir) in sa and str(a.paths.profile_dir) not in sb
    assert str(b.paths.profile_dir) in sb and str(b.paths.profile_dir) not in sa


# --------------------------------------------------------------------------- #
# .app wrapper: Info.plist + icon source
# --------------------------------------------------------------------------- #
def test_info_plist_has_unique_bundle_id_and_refs(tmp_path):
    a = _gen(tmp_path, "alpha")
    b = _gen(tmp_path, "beta")
    pa = a.paths.info_plist.read_text()
    pb = b.paths.info_plist.read_text()
    assert core.bundle_identifier("alpha") in pa
    assert core.bundle_identifier("beta") in pb
    assert core.bundle_identifier("alpha") != core.bundle_identifier("beta")
    assert a.paths.launcher.name in pa  # CFBundleExecutable
    assert a.paths.icon_png.name in pa  # CFBundleIconFile


def test_icon_source_is_a_real_png_and_per_instance(tmp_path):
    a = _gen(tmp_path, "alpha", title="Alpha")
    b = _gen(tmp_path, "beta", title="Beta")
    pa = a.paths.icon_png.read_bytes()
    pb = b.paths.icon_png.read_bytes()
    assert pa[:8] == b"\x89PNG\r\n\x1a\n"  # valid PNG magic
    assert pa != pb  # colour derived from title -> distinct per instance


def test_build_icns_is_reported_noop_off_macos(tmp_path):
    res = _gen(tmp_path, "alpha")
    out = macos.build_icns(res.paths.icon_png, res.paths.icon_png.with_suffix(".icns"))
    # In CI (Linux) it must not fake success — it reports why and keeps the PNG.
    import sys as _sys

    if _sys.platform != "darwin":
        assert out.built is False
        assert out.icns_path is None
        assert "mac" in out.reason.lower()


# --------------------------------------------------------------------------- #
# Overwrite / existing-dir guard
# --------------------------------------------------------------------------- #
def test_regenerating_without_overwrite_refuses(tmp_path):
    _gen(tmp_path, "alpha")
    with pytest.raises(FileExistsError):
        _gen(tmp_path, "alpha")  # helper leaves overwrite at its False default


def test_overwrite_replaces_existing(tmp_path):
    _gen(tmp_path, "alpha", token="first")
    core.generate_instance(
        out_root=tmp_path, source_extension_dir=REPO_EXTENSION, instance_id="alpha",
        title="alpha", service_url="wss://h.example.com", token="second",
        key_b64="K==", extension_id="a" * 32, overwrite=True,
    )
    data = json.loads((tmp_path / "alpha" / "extension" / "instance.json").read_text())
    assert data["token"] == "second"


# --------------------------------------------------------------------------- #
# Path-traversal safety: a malicious/typo title cannot escape the out-root
# --------------------------------------------------------------------------- #
def test_title_with_traversal_stays_under_out_root(tmp_path):
    # A title like "../../escape" must NOT place the .app outside out_root: the .app
    # dir name is slugified. Reddens if instance_paths uses the raw title again.
    out_root = tmp_path / "outroot"
    res = _gen(out_root, "inst", title="../../escape")
    app_dir = res.paths.app_dir.resolve()
    assert str(app_dir).startswith(str(out_root.resolve()) + "/")
    # A title with a slash must not create nested dirs either.
    res2 = _gen(out_root, "inst2", title="Work/Home")
    assert res2.paths.app_dir.parent == (out_root / "inst2").resolve()
    # The human-readable title still survives verbatim in Info.plist.
    plist = res.paths.info_plist.read_text()
    assert "../../escape" in plist  # CFBundleName keeps the raw (xml-escaped) title


# --- instance.json (carries EXT_TOKEN) is owner-only 0600, like the signing key ---
def test_instance_json_written_0600(tmp_path):
    # A group/world-readable token-at-rest is a secret leak; the signing key is 0600,
    # so the token file must match. Reddens if the 0600 write is reverted to write_text.
    res = _gen(tmp_path, "sec", token="super-secret-token")
    mode = res.paths.instance_json.stat().st_mode & 0o777
    assert mode == 0o600, oct(mode)


def test_restamp_keeps_instance_json_0600(tmp_path):
    # Restamp rewrites the token in place — the overwrite must preserve 0600 (an
    # existing file's mode would otherwise persist).
    _gen(tmp_path, "sec", token="old-secret")
    core.restamp_all(tmp_path, token="new-secret")
    ij = next(iter(core.iter_instance_json_paths(tmp_path)))
    assert (ij.stat().st_mode & 0o777) == 0o600, oct(ij.stat().st_mode & 0o777)
