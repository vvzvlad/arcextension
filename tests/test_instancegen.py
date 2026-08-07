"""Tests for the per-instance browser generator (tools/instancegen, §13).

Pure-core only — everything here runs on Linux/CI without a browser or a mac.

Under enrollment (§7/§13, issue #35/#37) an instance is a THIN wrapper: `generate` no
longer copies the extension or writes an `instance.json`. It only builds an empty
`--user-data-dir` and a `.app` whose launcher `--load-extension`s the SHARED universal
bundle built once by `instancegen bundle`. The service address and the per-install secret
are entered per profile during enrollment, so `generate` takes no token and no serviceUrl.

The `bundle` build (a universal, key-free, instance.json-free copy) is covered in
`tests/test_enroll_metrics_and_bundle.py`. Operational acceptance (an instance actually
enrolling, a clone re-enrolling) needs a real browser and is a MANUAL checklist in
`tools/README.md` — deliberately NOT faked here.

Each test is written to redden if its guard is removed (noted inline).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.instancegen import cli, core, macos

REPO_EXTENSION = Path(__file__).resolve().parents[1] / "extension"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _make_bundle(tmp_path, name="dist") -> Path:
    """A minimal SHARED universal bundle dir the generated .app points at.

    `generate` only requires a `manifest.json` to be present (it loads, never copies,
    this tree). A real fleet builds it with `instancegen bundle`; a stub is enough here.
    """
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    (d / "manifest.json").write_text(
        json.dumps({"name": "x", "host_permissions": ["<all_urls>"]})
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
    # The whole CLI path (macos.build_icns no-op off-mac) returns 0 and lays down a
    # launcher pointing at the shared bundle.
    bundle = _make_bundle(tmp_path)
    out = tmp_path / "inst"
    rc = cli.main(["generate", "--instance-id", "main", "--bundle-dir", str(bundle),
                   "--out", str(out), "--title", "Main"])
    assert rc == 0
    launcher = out / "main" / "main.app" / "Contents" / "MacOS" / "run"
    assert launcher.is_file()
    assert str(bundle.resolve()) in launcher.read_text()


# --------------------------------------------------------------------------- #
# No signing key anywhere: not in the CLI, not in the module, not in the manifest
# --------------------------------------------------------------------------- #
def test_bundle_takes_no_key_file_and_stamps_no_configuration(tmp_path):
    """The key existed only to PIN the extension id for `EXT_ALLOWED_ORIGINS`.

    That allow-list is gone (src/api/cors.py), so the whole key layer went with it: no
    `--key-file` flag, no `keys` module, no key/host stamping helpers, and no `key` field
    in the repo manifest. Redden: reintroduce any of them and one of these assertions
    fails.

    `core.stamp_manifest` is NOT in the gone list and must not be added back to it: it
    stamps build IDENTITY (version/version_name), not CONFIGURATION. What made a bundle
    non-interchangeable was a baked-in host or id; two bundles differing only in their
    version stamp behave identically.
    """
    parser = cli.build_parser()
    with pytest.raises(SystemExit):  # argparse exits 2 on an unknown option
        parser.parse_args(["bundle", "--out", str(tmp_path / "d"), "--key-file", "/k.pem"])
    args = parser.parse_args(["bundle", "--out", str(tmp_path / "d")])
    assert not hasattr(args, "key_file")

    import tools.instancegen as instancegen

    assert not hasattr(instancegen, "keys")
    for gone in ("KEY_PLACEHOLDER", "HOST_PLACEHOLDER",
                 "stamp_bundle_manifest", "write_private_bytes"):
        assert not hasattr(core, gone), gone

    manifest = json.loads((REPO_EXTENSION / "manifest.json").read_text())
    assert "key" not in manifest
    assert "//key" not in manifest


# --------------------------------------------------------------------------- #
# Build stamp: `version` / `version_name` in the BUILT bundle (never in extension/)
# --------------------------------------------------------------------------- #
def _repo_manifest_text() -> str:
    return (REPO_EXTENSION / "manifest.json").read_text(encoding="utf-8")


def test_stamp_manifest_sets_both_fields_and_changes_nothing_else():
    # The stamp must be surgical: exactly two values differ, every other key — including
    # the `//`-comment keys that carry the Russian prose — comes through untouched.
    # Redden: serialise with ensure_ascii=True, or drop/reorder any other key.
    before_text = _repo_manifest_text()
    after_text = core.stamp_manifest(
        before_text, version="0.1.130", version_name="0.1.130 · abc1234 · 2026-08-07 19:52"
    )
    before = json.loads(before_text)
    after = json.loads(after_text)

    assert after["version"] == "0.1.130"
    assert after["version_name"] == "0.1.130 · abc1234 · 2026-08-07 19:52"
    # Everything else is identical, key for key and value for value.
    assert {k: v for k, v in after.items() if k not in ("version", "version_name")} == \
           {k: v for k, v in before.items() if k != "version"}
    # …and in the same order, with version_name inserted right after version.
    expected_order = []
    for key in before:
        expected_order.append(key)
        if key == "version":
            expected_order.append("version_name")
    assert list(after) == expected_order
    # The Russian comment text survives UNESCAPED (ensure_ascii=False), not as \uXXXX.
    assert "«Читать и изменять закладки»" in after_text
    assert "\\u" not in after_text
    # manifest_version 3 is untouched — the output is still a loadable MV3 manifest.
    assert after["manifest_version"] == 3


@pytest.mark.parametrize(
    "bad",
    [
        "0.1.65536",   # component over the spec maximum
        "0.1.032",     # leading zero on a non-zero component
        "0.0.0.0",     # all zero
        "0",           # all zero (single component)
        "0.1.2.3.4",   # more than four components
        "0.1.x",       # not an integer
        "0.1.-1",      # negative
        "",            # empty
    ],
)
def test_stamp_manifest_refuses_an_invalid_version(bad):
    # An invalid `version` does not degrade — Chrome refuses to load the extension at all —
    # so it must raise here instead of producing an unloadable manifest. Redden: drop the
    # validate_manifest_version call from stamp_manifest.
    with pytest.raises(ValueError):
        core.stamp_manifest(_repo_manifest_text(), version=bad, version_name="x")


def test_stamp_manifest_accepts_the_spec_edges():
    # The mirror of the case above: legal versions must NOT be rejected.
    for good in ("0.1.0.0", "65535.65535.65535.65535", "1", "0.0.1"):
        assert core.validate_manifest_version(good) == good


def test_copy_bundle_without_a_stamp_is_still_verbatim(tmp_path):
    # The no-regression pin: every existing caller passes no stamp and must keep getting a
    # byte-identical manifest. Redden: stamp unconditionally in copy_bundle.
    out = tmp_path / "dist"
    core.copy_bundle(REPO_EXTENSION, out)
    assert (out / "manifest.json").read_bytes() == (REPO_EXTENSION / "manifest.json").read_bytes()


def test_copy_bundle_stamps_the_output_and_never_the_repo(tmp_path):
    # The stamp lands in the OUTPUT manifest only. The repo's own extension/ is READ-ONLY
    # to this module (module docstring), and a build that bumped the tracked manifest would
    # make every build dirty the working tree. Redden: stamp src instead of dst.
    repo_before = (REPO_EXTENSION / "manifest.json").read_bytes()
    out = tmp_path / "dist"
    core.copy_bundle(
        REPO_EXTENSION, out, version="0.1.130", version_name="0.1.130 · abc1234 · now"
    )

    built = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert built["version"] == "0.1.130"
    assert built["version_name"] == "0.1.130 · abc1234 · now"
    assert (REPO_EXTENSION / "manifest.json").read_bytes() == repo_before


def test_replace_bundle_forwards_the_stamp(tmp_path):
    # `bundle --force` goes through replace_bundle, so the in-place rebuild — the one the
    # operator actually runs (make dev-bundle) — must stamp too. Redden: drop the
    # version/version_name forwarding in replace_bundle.
    out = tmp_path / "dist"
    core.copy_bundle(REPO_EXTENSION, out)
    core.replace_bundle(
        REPO_EXTENSION, out, version="0.1.131", version_name="0.1.131 · def5678-dirty · now"
    )
    built = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert built["version"] == "0.1.131"
    assert built["version_name"] == "0.1.131 · def5678-dirty · now"


def test_cli_bundle_stamps_version_and_version_name(tmp_path):
    # End to end through the CLI against the real repo: the built manifest carries a
    # version whose first two components come from the tracked literal, a third component
    # that is the commit count, and a version_name naming that version plus a sha.
    out = tmp_path / "dist"
    assert cli.main(["bundle", "--out", str(out), "--extension-dir", str(REPO_EXTENSION)]) == 0

    base = json.loads((REPO_EXTENSION / "manifest.json").read_text())["version"]
    built = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    major, minor = base.split(".")[:2]
    assert built["version"].startswith(f"{major}.{minor}.")
    core.validate_manifest_version(built["version"])  # raises if the CLI emitted junk
    assert built["version_name"].startswith(built["version"])
    assert built["manifest_version"] == 3  # still a loadable MV3 bundle
    # The tracked literal is NOT bumped by a build.
    assert json.loads((REPO_EXTENSION / "manifest.json").read_text())["version"] == base


def test_cli_bundle_still_builds_when_git_is_unavailable(tmp_path, monkeypatch, capsys):
    # A build must NEVER fail over the stamp: no git, not a repo, a broken repo — all
    # degrade to the verbatim copy this tool did before the stamp existed, with a note on
    # stderr. Redden: let build_stamp propagate instead of returning None.
    def no_git(*_args, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(cli.subprocess, "run", no_git)
    out = tmp_path / "dist"
    assert cli.main(["bundle", "--out", str(out), "--extension-dir", str(REPO_EXTENSION)]) == 0

    assert (out / "manifest.json").read_bytes() == (REPO_EXTENSION / "manifest.json").read_bytes()
    built = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert "version_name" not in built
    assert "build stamp unavailable" in capsys.readouterr().err


def test_build_stamp_returns_none_outside_a_git_repo(tmp_path, capsys):
    # A source extension/ that is not in a git repo at all (an unpacked tarball) — the
    # other half of the fallback, exercised without monkeypatching git away.
    src = tmp_path / "extension"
    src.mkdir()
    (src / "manifest.json").write_text(json.dumps({"name": "x", "version": "0.1.0"}))
    # tmp_path is outside any working tree, so `git -C` walks up and finds no repo.
    assert cli.build_stamp(src) is None
    assert "build stamp unavailable" in capsys.readouterr().err


def test_generated_app_reports_the_bundles_version(tmp_path):
    # The .app's CFBundleShortVersionString/CFBundleVersion must agree with the bundle its
    # launcher loads, instead of a frozen literal. Redden: hard-code the version in
    # build_info_plist's caller again.
    bundle = tmp_path / "dist"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(json.dumps({"name": "x", "version": "0.1.130"}))
    res = core.generate_instance(
        out_root=tmp_path / "inst", bundle_dir=bundle, instance_id="main", title="Main"
    )
    plist = res.paths.info_plist.read_text()
    assert "<string>0.1.130</string>" in plist
    assert "<string>0.1.0</string>" not in plist


def test_generated_app_falls_back_when_the_bundle_has_no_version(tmp_path):
    # An unstamped/hand-made bundle dir still produces a valid Info.plist.
    bundle = _make_bundle(tmp_path)  # no `version` key at all
    res = core.generate_instance(
        out_root=tmp_path / "inst", bundle_dir=bundle, instance_id="main", title="Main"
    )
    assert f"<string>{core.DEFAULT_APP_VERSION}</string>" in res.paths.info_plist.read_text()


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
