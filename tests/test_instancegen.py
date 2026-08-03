"""Tests for the per-instance browser generator (tools/instancegen, §13).

Pure-core only — everything here runs on Linux/CI without a browser or a mac.

Under enrollment (§7/§13, issue #35/#37) an instance is a THIN wrapper: `generate` no
longer copies the extension or writes an `instance.json`. It only builds an empty
`--user-data-dir` and a `.app` whose launcher `--load-extension`s the SHARED universal
bundle built once by `instancegen bundle`. The service address and the per-install secret
are entered per profile during enrollment, so `generate` takes no token and no serviceUrl.

The `bundle` build (universal, key-pinned, instance.json-free) is covered in
`tests/test_enroll_metrics_and_bundle.py`. Operational acceptance (an instance actually
enrolling, a clone re-enrolling) needs a real browser and is a MANUAL checklist in
`tools/README.md` — deliberately NOT faked here.

Each test is written to redden if its guard is removed (noted inline).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tools.instancegen import cli, core, keys, macos

REPO_EXTENSION = Path(__file__).resolve().parents[1] / "extension"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _make_bundle(tmp_path, name="dist", key="AAAABBBBCCCC") -> Path:
    """A minimal SHARED universal bundle dir the generated .app points at.

    `generate` only requires a `manifest.json` to be present (it loads, never copies,
    this tree). A real fleet builds it with `instancegen bundle`; a stub is enough here.
    """
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    (d / "manifest.json").write_text(
        json.dumps({"name": "x", "key": key, "host_permissions": ["<all_urls>"]})
    )
    return d


def _gen(out_root, instance_id, *, title=None, bundle_dir=None, **kw):
    return core.generate_instance(
        out_root=out_root,
        bundle_dir=bundle_dir if bundle_dir is not None else _make_bundle(Path(out_root)),
        instance_id=instance_id,
        title=title or instance_id,
        **kw,
    )


# --------------------------------------------------------------------------- #
# generate writes NO extension copy and NO instance.json (issue #37, acc 3)
# --------------------------------------------------------------------------- #
def test_generate_writes_no_extension_copy_and_no_instance_json(tmp_path):
    bundle = _make_bundle(tmp_path)
    res = core.generate_instance(
        out_root=tmp_path / "inst", bundle_dir=bundle, instance_id="main", title="Main"
    )
    root = res.paths.root
    # The .app + profile exist…
    assert res.paths.profile_dir.is_dir()
    assert res.paths.launcher.is_file()
    assert res.paths.app_dir.is_dir()
    # …but there is NO per-instance extension copy and NO instance.json anywhere under the
    # instance tree. Redden: restore the per-instance copy_bundle / build_instance_json.
    assert not (root / "extension").exists()
    assert list(root.rglob("instance.json")) == []
    assert list(root.rglob("manifest.json")) == []  # the manifest lives in the SHARED bundle


def test_generate_launcher_loads_the_shared_bundle(tmp_path):
    bundle = _make_bundle(tmp_path)
    res = core.generate_instance(
        out_root=tmp_path / "inst", bundle_dir=bundle, instance_id="main", title="Main"
    )
    script = res.paths.launcher.read_text()
    # The launcher --load-extensions the SHARED bundle (absolute), not a per-instance copy.
    # (Paths are sh-quoted in the script, so match on the path substring.)
    assert "--load-extension=" in script
    assert str(bundle.resolve()) in script
    assert str(res.paths.profile_dir) in script


def test_generate_rejects_a_bundle_dir_without_a_manifest(tmp_path):
    # Pointing --bundle-dir at a non-bundle must fail loudly, not produce a .app that
    # loads nothing. Redden: drop the manifest.json existence check.
    not_a_bundle = tmp_path / "empty"
    not_a_bundle.mkdir()
    with pytest.raises(ValueError, match="manifest.json"):
        core.generate_instance(
            out_root=tmp_path / "inst", bundle_dir=not_a_bundle,
            instance_id="main", title="Main",
        )


def test_bad_bundle_dir_on_overwrite_does_not_destroy_the_existing_app(tmp_path):
    # The --bundle-dir validation must run BEFORE _clear_instance_dir_keeping_profile:
    # otherwise a typo'd/empty bundle-dir on --overwrite would wipe the live .app and only
    # THEN raise, leaving the operator worse off. Redden: move the manifest check back
    # below the clear-existing branch.
    good = _make_bundle(tmp_path, name="good")
    out = tmp_path / "inst"
    res = core.generate_instance(
        out_root=out, bundle_dir=good, instance_id="main", title="Main"
    )
    app = res.paths.app_dir
    profile = res.paths.profile_dir
    (profile / "Local State").write_text('{"installUuid":"born-in-profile"}')
    assert app.is_dir()

    not_a_bundle = tmp_path / "empty"
    not_a_bundle.mkdir()
    with pytest.raises(ValueError, match="manifest.json"):
        core.generate_instance(
            out_root=out, bundle_dir=not_a_bundle, instance_id="main", title="Main",
            overwrite=True,
        )

    # The .app AND the profile survive untouched — nothing was cleared before the raise.
    assert app.is_dir()
    assert (profile / "Local State").read_text() == '{"installUuid":"born-in-profile"}'


# --------------------------------------------------------------------------- #
# Two instances share ONE bundle dir, own separate profiles (issue #37, acc 4)
# --------------------------------------------------------------------------- #
def test_two_instances_point_at_the_same_bundle_dir(tmp_path):
    bundle = _make_bundle(tmp_path)
    out = tmp_path / "inst"
    a = core.generate_instance(out_root=out, bundle_dir=bundle, instance_id="alpha", title="Alpha")
    b = core.generate_instance(out_root=out, bundle_dir=bundle, instance_id="beta", title="Beta")

    sa = a.paths.launcher.read_text()
    sb = b.paths.launcher.read_text()
    # Both launchers --load-extension the SAME shared dir -> one chrome-extension:// id.
    # Redden: reintroduce a per-instance copy and the two paths diverge.
    assert str(bundle.resolve()) in sa
    assert str(bundle.resolve()) in sb
    # …while the profiles are distinct and per-instance.
    assert a.paths.profile_dir != b.paths.profile_dir
    assert a.paths.profile_dir.is_dir() and b.paths.profile_dir.is_dir()
    assert str(a.paths.profile_dir) in sa and str(a.paths.profile_dir) not in sb


def test_generate_writes_nothing_into_the_repo_extension(tmp_path):
    # The generator must ONLY write under the output dir (§13): it must never mutate the
    # repo's own extension/ (in particular never author an instance.json there).
    repo_instance_json = REPO_EXTENSION / "instance.json"
    existed_before = repo_instance_json.exists()
    _gen(tmp_path, "gamma")
    assert repo_instance_json.exists() == existed_before
    assert not existed_before, "repo must ship no real instance.json"


# --------------------------------------------------------------------------- #
# --service-url is OPTIONAL, and the token can never be an argv argument
# --------------------------------------------------------------------------- #
def test_cli_generate_service_url_optional_and_bundle_dir_required(tmp_path):
    bundle = _make_bundle(tmp_path)
    parser = cli.build_parser()
    base = ["generate", "--instance-id", "a", "--bundle-dir", str(bundle), "--out", str(tmp_path)]
    # --service-url is OPTIONAL now (the address is entered during enrollment, not baked
    # in). Parses with and without it. Redden: mark --service-url required again.
    assert parser.parse_args(base).service_url is None
    assert parser.parse_args(base + ["--service-url", "wss://h"]).service_url == "wss://h"
    # --bundle-dir is REQUIRED — an instance must point at a real shared bundle.
    with pytest.raises(SystemExit):
        parser.parse_args(["generate", "--instance-id", "a", "--out", str(tmp_path)])


def test_cli_has_no_token_options_anywhere(tmp_path):
    # Enrollment removed the shared secret entirely, so there is nothing to pass — and a
    # secret must never be expressible in argv (ps output, shell history). Redden: re-add
    # --token / --token-file to generate.
    bundle = _make_bundle(tmp_path)
    parser = cli.build_parser()
    base = ["generate", "--instance-id", "a", "--bundle-dir", str(bundle), "--out", str(tmp_path)]
    for extra in (["--token", "leaked"], ["--token-file", "/p"]):
        with pytest.raises(SystemExit):  # argparse exits 2 on an unknown option
            parser.parse_args(base + extra)
    args = parser.parse_args(base)
    assert not hasattr(args, "token")
    assert not hasattr(args, "token_file")


def test_cli_generate_runs_end_to_end(tmp_path):
    # The whole CLI path (macos.build_icns no-op off-mac, ext-id derived from the shared
    # bundle key) returns 0 and lays down a launcher pointing at the shared bundle.
    bundle = _make_bundle(tmp_path, key=keys.public_key_b64_from_pem(
        keys.load_or_create_private_key_pem(tmp_path / "k.pem")
    ))
    out = tmp_path / "inst"
    rc = cli.main(["generate", "--instance-id", "main", "--bundle-dir", str(bundle),
                   "--out", str(out), "--title", "Main"])
    assert rc == 0
    launcher = out / "main" / "main.app" / "Contents" / "MacOS" / "run"
    assert launcher.is_file()
    assert str(bundle.resolve()) in launcher.read_text()


def test_cli_generate_survives_a_malformed_bundle_manifest(tmp_path):
    # _bundle_extension_id runs AFTER the instance is created, purely for the id printout.
    # A syntactically-broken manifest.json in the shared bundle must NOT crash the CLI with
    # a bare JSONDecodeError — the instance is valid regardless. Redden: drop the try/except
    # in _bundle_extension_id and this raises instead of returning 0.
    bundle = tmp_path / "dist"
    bundle.mkdir()
    (bundle / "manifest.json").write_text("{ this is not valid json ")
    out = tmp_path / "inst"
    rc = cli.main(["generate", "--instance-id", "main", "--bundle-dir", str(bundle),
                   "--out", str(out)])
    assert rc == 0
    assert (out / "main" / "main.app" / "Contents" / "MacOS" / "run").is_file()
    # The helper itself returns None (a hint is printed instead of a bogus id).
    assert cli._bundle_extension_id(bundle) is None


# --------------------------------------------------------------------------- #
# Manifest stamping (shared bundle build) + signing key still work
# --------------------------------------------------------------------------- #
def test_stamp_manifest_requires_a_real_key():
    manifest = json.loads((REPO_EXTENSION / "manifest.json").read_text())
    with pytest.raises(ValueError):
        core.stamp_manifest(manifest, "h.example.com", "")


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
    assert keys.public_key_b64_from_pem(pem) == key_b64


def test_extension_id_known_answer_vector():
    # Chromium-correct KNOWN ANSWER (not just self-consistent): id = SHA-256 of the
    # DER key bytes, first 16 bytes, each nibble high-then-low -> 'a'+n. Verified
    # against an independent reimplementation.
    import base64
    key_b64 = base64.b64encode(b"hello-world").decode()
    assert keys.derive_extension_id(key_b64) == "kpkchleenedlackjpokebnbdmonmcoea"


# --------------------------------------------------------------------------- #
# Launcher content: Brave, per-instance flags
# --------------------------------------------------------------------------- #
def test_launcher_targets_brave_with_per_instance_flags(tmp_path):
    res = _gen(tmp_path, "alpha")
    script = res.paths.launcher.read_text()
    assert str(res.paths.profile_dir) in script
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
    bundle = _make_bundle(tmp_path)
    a = _gen(tmp_path, "alpha", bundle_dir=bundle)
    b = _gen(tmp_path, "beta", bundle_dir=bundle)
    sa = a.paths.launcher.read_text()
    sb = b.paths.launcher.read_text()
    assert str(a.paths.profile_dir) in sa and str(a.paths.profile_dir) not in sb
    assert str(b.paths.profile_dir) in sb and str(b.paths.profile_dir) not in sa


# --------------------------------------------------------------------------- #
# .app wrapper: Info.plist + icon source
# --------------------------------------------------------------------------- #
def test_info_plist_has_unique_bundle_id_and_refs(tmp_path):
    bundle = _make_bundle(tmp_path)
    a = _gen(tmp_path, "alpha", bundle_dir=bundle)
    b = _gen(tmp_path, "beta", bundle_dir=bundle)
    pa = a.paths.info_plist.read_text()
    pb = b.paths.info_plist.read_text()
    assert core.bundle_identifier("alpha") in pa
    assert core.bundle_identifier("beta") in pb
    assert core.bundle_identifier("alpha") != core.bundle_identifier("beta")
    assert a.paths.launcher.name in pa  # CFBundleExecutable
    assert a.paths.icon_png.name in pa  # CFBundleIconFile


def test_icon_source_is_a_real_png_and_per_instance(tmp_path):
    bundle = _make_bundle(tmp_path)
    a = _gen(tmp_path, "alpha", title="Alpha", bundle_dir=bundle)
    b = _gen(tmp_path, "beta", title="Beta", bundle_dir=bundle)
    pa = a.paths.icon_png.read_bytes()
    pb = b.paths.icon_png.read_bytes()
    assert pa[:8] == b"\x89PNG\r\n\x1a\n"  # valid PNG magic
    assert pa != pb  # colour derived from title -> distinct per instance


def test_build_icns_is_reported_noop_off_macos(tmp_path):
    res = _gen(tmp_path, "alpha")
    out = macos.build_icns(res.paths.icon_png, res.paths.icon_png.with_suffix(".icns"))
    import sys as _sys

    if _sys.platform != "darwin":
        assert out.built is False
        assert out.icns_path is None
        assert "mac" in out.reason.lower()


# --------------------------------------------------------------------------- #
# Overwrite / existing-dir guard
# --------------------------------------------------------------------------- #
def test_regenerating_without_overwrite_refuses(tmp_path):
    bundle = _make_bundle(tmp_path)
    _gen(tmp_path, "alpha", bundle_dir=bundle)
    with pytest.raises(FileExistsError):
        _gen(tmp_path, "alpha", bundle_dir=bundle)  # overwrite left at its False default


def test_overwrite_preserves_the_browser_profile(tmp_path):
    # The generator can rewrite the .app; it CANNOT recreate the profile, which holds
    # install_uuid, the session, cookies and saved passwords. An --overwrite that
    # rmtree'd the whole instance root destroyed all of it silently and the instance came
    # back as a different install (§6/§13, InstancePaths docstring).
    bundle = _make_bundle(tmp_path)
    _gen(tmp_path, "alpha", bundle_dir=bundle)
    profile = tmp_path / "alpha" / "profile"
    uuid_marker = profile / "Local State"
    uuid_marker.write_text('{"installUuid":"born-in-profile"}')
    cookies = profile / "Default" / "Cookies"
    cookies.parent.mkdir(parents=True)
    cookies.write_text("session-data")

    core.generate_instance(
        out_root=tmp_path, bundle_dir=bundle, instance_id="alpha", title="alpha",
        overwrite=True,
    )

    # Redden: restore `shutil.rmtree(paths.root)` -> both of these vanish.
    assert uuid_marker.read_text() == '{"installUuid":"born-in-profile"}'
    assert cookies.read_text() == "session-data"


def test_overwrite_clears_the_stale_app(tmp_path):
    # Sparing the profile must not turn --overwrite into a merge: the .app named after the
    # OLD title must be gone, or a renamed .app would linger forever.
    bundle = _make_bundle(tmp_path)
    res = _gen(tmp_path, "alpha", title="Old Title", bundle_dir=bundle)
    old_app = res.paths.app_dir
    assert old_app.is_dir()

    core.generate_instance(
        out_root=tmp_path, bundle_dir=bundle, instance_id="alpha", title="New Title",
        overwrite=True,
    )

    assert not old_app.exists()  # the .app named after the OLD title is gone
    assert (tmp_path / "alpha" / "new-title.app").is_dir()


# --------------------------------------------------------------------------- #
# Path-traversal safety: a malicious/typo title cannot escape the out-root
# --------------------------------------------------------------------------- #
def test_title_with_traversal_stays_under_out_root(tmp_path):
    # A title like "../../escape" must NOT place the .app outside out_root: the .app
    # dir name is slugified. Reddens if instance_paths uses the raw title again.
    out_root = tmp_path / "outroot"
    bundle = _make_bundle(tmp_path)
    res = _gen(out_root, "inst", title="../../escape", bundle_dir=bundle)
    app_dir = res.paths.app_dir.resolve()
    assert str(app_dir).startswith(str(out_root.resolve()) + "/")
    # A title with a slash must not create nested dirs either.
    res2 = _gen(out_root, "inst2", title="Work/Home", bundle_dir=bundle)
    assert res2.paths.app_dir.parent == (out_root / "inst2").resolve()
    # The human-readable title still survives verbatim in Info.plist.
    plist = res.paths.info_plist.read_text()
    assert "../../escape" in plist  # CFBundleName keeps the raw (xml-escaped) title


# --------------------------------------------------------------------------- #
# Private writes must be COMPLETE, not just created (the signing key)
# --------------------------------------------------------------------------- #
def _break_os_write(monkeypatch):
    """Make a bare os.write() write only the first half of its buffer.

    That is the real short-write failure mode (a filling disk, an interrupted
    syscall): os.write returns the short count and raises nothing, so the caller
    that ignores the return value leaves a TRUNCATED file which still satisfies
    `p.exists()`. io.FileIO writes at the C level and does not route through this
    patch, so a correct implementation is unaffected — an os.write-based one is not.
    """
    real_write = os.write

    def half_write(fd, data):
        return real_write(fd, bytes(data)[: max(1, len(bytes(data)) // 2)])

    monkeypatch.setattr(os, "write", half_write)


def test_signing_key_is_written_whole_under_short_writes(tmp_path, monkeypatch):
    # A truncated PEM is worse than a missing one: `p.exists()` makes it look
    # generated, so it is REUSED forever and the pinned extension id is lost.
    key_path = tmp_path / ".instancegen" / "signing_key.pem"
    _break_os_write(monkeypatch)

    pem = keys.load_or_create_private_key_pem(key_path)

    assert key_path.read_bytes() == pem
    assert (key_path.stat().st_mode & 0o777) == 0o600
    # The persisted key must still be usable — a half PEM would fail to parse.
    assert keys.public_key_b64_from_pem(key_path.read_bytes())
