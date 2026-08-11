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
import os
import re
import shutil
import subprocess
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


# Short-sha length the fixture repo below pins via `core.abbrev`. Git's default is `auto`
# — a length derived from the repo's object count — and a developer or a runner may also
# set `core.abbrev` globally, in which case `git rev-parse --short HEAD` obeys THAT
# (`core.abbrev=4` really does answer a 4-character sha). Pinning it on the fixture repo
# makes the sha a known quantity, so the format assertion tests the stamp instead of the
# ambient config. 12 is far above git's minimum of 4 and unambiguous in a tiny repo.
_FIXTURE_ABBREV = 12


def _tiny_repo_extension(root: Path, *, commits: int = 1) -> Path:
    """A real, NON-shallow git repo with an `extension/` bundle inside it.

    Every test that asserts on a real build stamp owns its repository instead of reading
    the project's own `extension/`, because THE SHAPE OF THIS PROJECT'S CLONE MUST NOT
    DECIDE WHETHER THE ASSERTION RUNS. `cli.build_stamp` degrades to "no stamp" on a
    shallow clone on purpose (there `rev-list --count HEAD` answers 1 and the version
    would come out confidently wrong), and `actions/checkout` clones with `fetch-depth: 1`
    — so against `REPO_EXTENSION` there is legitimately no stamp in CI, and the assertions
    would either die on a missing stamp or have to be skipped in exactly the
    environment that matters most. A repo built here is non-shallow by construction and
    its commit count is the test's own choice.

    It is a real repo rather than a stub because the `-dirty` marker is about what git
    reports for a real working tree.

    Identity, signing and hooks are configured LOCALLY on this repo, so it commits in a
    sandbox with no global git config and never inherits the developer's or the runner's.
    """
    repo = root / "repo"
    ext = repo / "extension"
    ext.mkdir(parents=True)
    (ext / "manifest.json").write_text(
        json.dumps({"name": "x", "version": "0.2.0", "manifest_version": 3}),
        encoding="utf-8",
    )

    def git(*argv):
        subprocess.run(
            ["git", "-C", str(repo), *argv], check=True, capture_output=True, text=True
        )

    # --template= (empty) neutralises a global `init.templateDir`, whose hooks and
    # `info/exclude` would otherwise be copied into this repo — the same class of ambient
    # config as the identity/signing/abbrev settings below.
    git("init", "-q", "--template=")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    git("config", "commit.gpgsign", "false")
    git("config", "core.abbrev", str(_FIXTURE_ABBREV))
    # A global ignore file matching anything the fixture writes (e.g. `background.js`)
    # would hide it from `git status --porcelain` and silently kill the `-dirty` assertion.
    git("config", "core.excludesFile", "/dev/null")
    git("add", "-A")
    # --no-verify so a globally configured `core.hooksPath` cannot run someone's
    # pre-commit hook here and fail the fixture.
    git("commit", "-qm", "init", "--no-verify")
    for n in range(1, commits):
        git("commit", "-qm", f"c{n}", "--allow-empty", "--no-verify")
    return ext


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
def test_bundle_takes_no_key_file_and_nothing_stamps_a_manifest(tmp_path):
    """The key existed only to PIN the extension id for `EXT_ALLOWED_ORIGINS`.

    That allow-list is gone (src/api/cors.py), so the whole key layer went with it: no
    `--key-file` flag, no `keys` module, no stamping helpers, and no `key` field in the
    repo manifest. Redden: reintroduce any of them and one of these assertions fails.
    """
    parser = cli.build_parser()
    with pytest.raises(SystemExit):  # argparse exits 2 on an unknown option
        parser.parse_args(["bundle", "--out", str(tmp_path / "d"), "--key-file", "/k.pem"])
    args = parser.parse_args(["bundle", "--out", str(tmp_path / "d")])
    assert not hasattr(args, "key_file")

    import tools.instancegen as instancegen

    assert not hasattr(instancegen, "keys")
    for gone in ("KEY_PLACEHOLDER", "HOST_PLACEHOLDER", "stamp_manifest",
                 "stamp_bundle_manifest", "write_private_bytes"):
        assert not hasattr(core, gone), gone

    manifest = json.loads((REPO_EXTENSION / "manifest.json").read_text())
    assert "key" not in manifest
    assert "//key" not in manifest


# --------------------------------------------------------------------------- #
# Build stamp: `version` in the BUILT bundle (never in extension/, never version_name)
# --------------------------------------------------------------------------- #
def _repo_manifest_text() -> str:
    return (REPO_EXTENSION / "manifest.json").read_text(encoding="utf-8")


def test_stamp_build_identity_sets_the_version_and_changes_nothing_else():
    # Exactly ONE value differs; every other key — including the `//`-comment keys that
    # carry the Russian prose — comes through with its value and its position intact. Note
    # this is a key-level guarantee, not a byte-level one: the file is re-serialised whole,
    # so the blank lines between the manifest's sections do not survive (documented in
    # stamp_build_identity). Redden: serialise with ensure_ascii=True, or drop/reorder any
    # other key.
    before_text = _repo_manifest_text()
    after_text = core.stamp_build_identity(before_text, version="0.1.130.1952")
    before = json.loads(before_text)
    after = json.loads(after_text)

    assert after["version"] == "0.1.130.1952"
    # Everything else is identical, key for key and value for value…
    assert {k: v for k, v in after.items() if k != "version"} == \
           {k: v for k, v in before.items() if k != "version"}
    # …and in the same order, with `version` still in its original slot (no field is
    # inserted next to it anymore).
    assert list(after) == list(before)
    # The Russian comment text survives UNESCAPED (ensure_ascii=False), not as \uXXXX.
    assert "«Читать и изменять закладки»" in after_text
    assert "\\u" not in after_text
    # manifest_version 3 is untouched — the output is still a loadable MV3 manifest.
    assert after["manifest_version"] == 3


def test_stamp_build_identity_strips_an_inherited_version_name():
    # A source manifest that somehow carries a `version_name` must NOT have it copied
    # through: brave://extensions renders that field beside the extension NAME and a long
    # value there wraps and truncates the name («arcextens... 0.1.135 · edd7787 · …»),
    # which is the defect this scheme fixes. Stripping it makes the page fall back to
    # `version`. Redden: pass unknown keys through untouched.
    src = json.dumps({"name": "x", "version": "0.1.0", "version_name": "long display"})
    after = json.loads(core.stamp_build_identity(src, version="0.1.130.1952"))
    assert after["version"] == "0.1.130.1952"
    assert "version_name" not in after


@pytest.mark.parametrize(
    "bad",
    [
        "0.1.65536",       # component over the spec maximum
        "0.1.130.65536",   # …and over it in the new FOURTH component
        "0.1.032",         # leading zero on a non-zero component
        "0.1.130.0932",    # leading zero in the build-minute component
        "0.0.0.0",         # all zero
        "0",               # all zero (single component)
        "0.1.2.3.4",       # more than four components
        "0.1.x",           # not an integer
        "0.1.-1",          # negative
        "",                # empty
    ],
)
def test_stamp_build_identity_refuses_an_invalid_version(bad):
    # An invalid `version` does not degrade — Chrome refuses to load the extension at all —
    # so it must raise here instead of producing an unloadable manifest. Redden: drop the
    # validate_manifest_version call from stamp_build_identity.
    with pytest.raises(ValueError):
        core.stamp_build_identity(_repo_manifest_text(), version=bad)


def test_stamp_build_identity_accepts_the_spec_edges():
    # The mirror of the case above: legal versions must NOT be rejected. The four-component
    # form the stamp now emits is first in the list — `<major>.<minor>.<count>.<HHMM>` —
    # together with the two boundary minutes it can produce (00:00 -> 0, 23:59 -> 2359).
    for good in ("0.1.135.2058", "0.1.135.0", "0.1.135.2359",
                 "0.1.0.0", "65535.65535.65535.65535", "1", "0.0.1"):
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
    core.copy_bundle(REPO_EXTENSION, out, version="0.1.130.1952")

    built = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert built["version"] == "0.1.130.1952"
    assert "version_name" not in built
    assert (REPO_EXTENSION / "manifest.json").read_bytes() == repo_before


def test_copy_bundle_validates_the_version_before_copying_anything(tmp_path):
    # EVERY argument check must run BEFORE shutil.copytree. `version` is validated inside
    # stamp_build_identity, which runs AFTER the copy — so copy_bundle validates it up
    # front too, or an illegal version would leave a complete but UNSTAMPED bundle at
    # --out and the retry would have to fight the leftovers with --force. (This replaces
    # the old "version and version_name must be given together" pairing check: with
    # version_name gone there is no pair left, but the copy-nothing-before-validating
    # property it protected is the same one asserted here.) Redden: move the
    # validate_manifest_version call back below copytree.
    out = tmp_path / "dist"
    with pytest.raises(ValueError, match="exceeds the maximum"):
        core.copy_bundle(REPO_EXTENSION, out, version="0.1.130.65536")
    assert not out.exists()
    with pytest.raises(ValueError, match="1 to 4 dot-separated"):
        core.copy_bundle(REPO_EXTENSION, out, version="0.1.130.1952.7")
    assert not out.exists()


def test_replace_bundle_forwards_the_stamp(tmp_path):
    # `bundle --force` goes through replace_bundle, so the in-place rebuild — the one the
    # operator actually runs (make dev-bundle) — must stamp too. Redden: drop the
    # version forwarding in replace_bundle.
    out = tmp_path / "dist"
    core.copy_bundle(REPO_EXTENSION, out)
    core.replace_bundle(REPO_EXTENSION, out, version="0.1.131.2058")
    built = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert built["version"] == "0.1.131.2058"
    assert "version_name" not in built


def test_cli_bundle_stamps_the_version(tmp_path):
    # End to end through the CLI: the built manifest carries a version whose first two
    # components come from the tracked literal, a third component that is the commit count
    # and a fourth that is the build minute.
    #
    # This builds a repo the TEST owns rather than REPO_EXTENSION, because the shape of
    # THIS project's clone must not decide whether the assertion below runs. build_stamp
    # degrades to no stamp on a shallow clone by design, and CI checks out with
    # fetch-depth: 1 — so read against REPO_EXTENSION this test dies on a missing stamp in
    # CI, and skipping instead would retire the format assertion in the one environment
    # that publishes images. See _tiny_repo_extension.
    ext = _tiny_repo_extension(tmp_path, commits=3)
    out = tmp_path / "dist"
    assert cli.main(["bundle", "--out", str(out), "--extension-dir", str(ext)]) == 0

    base = json.loads((ext / "manifest.json").read_text())["version"]
    built = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    major, minor = base.split(".")[:2]
    # The full SHAPE of `version` is pinned, and it is pinned here on purpose: this is now
    # the string a human reads off the extension card in brave://extensions (the manifest
    # carries no version_name for the page to display instead), so changing the component
    # count is a user-visible change and must be a deliberate one. Four integer components,
    # nothing else — no sha, no date, no `-dirty`; those live in the CLI's printed summary.
    assert re.fullmatch(r"\d+\.\d+\.\d+\.\d+", built["version"]), built["version"]
    core.validate_manifest_version(built["version"])  # raises if the CLI emitted junk
    count, minute = built["version"].split(".")[2:]
    # The EXACT count, not merely the prefix: the history is this test's own, so three
    # commits on a `0.2.0` base is `0.2.3.<minute>` and nothing else. Redden: derive the
    # third component from anything but `git rev-list --count HEAD`.
    assert f"{major}.{minor}.{count}" == f"{major}.{minor}.3"
    # The build minute is a wall clock, so it is pinned by RANGE rather than by value:
    # `HH*100 + MM` lives in 0..2359. Redden: emit `%H%M` as a zero-padded string (`0930`)
    # and the leading zero makes validate_manifest_version reject the whole version above.
    assert 0 <= int(minute) <= 2359, minute
    assert built["manifest_version"] == 3  # still a loadable MV3 bundle
    # The tracked literal is NOT bumped by a build.
    assert json.loads((ext / "manifest.json").read_text())["version"] == base


def test_cli_bundle_writes_no_version_name_and_a_short_version(tmp_path):
    # THE defect this scheme fixes. brave://extensions renders `version_name` when present,
    # and it renders it in the slot BESIDE THE EXTENSION NAME — a slot with roughly ten
    # characters of room. The 35-character human-readable stamp that used to live there
    # («0.1.135 · edd7787 · 2026-08-07 20:58») wrapped to two lines and truncated the name
    # itself, so the card read «arcextens... 0.1.135 · edd7787 · 2026-08-07 20:58» while
    # every neighbouring extension showed its full name and a compact version. With no
    # `version_name` the page falls back to `version` ("if no version_name is present, the
    # version field will be used for display purposes as well"). Redden: re-add
    # `version_name` to the stamp — which would silently truncate the name again, with no
    # other test noticing.
    ext = _tiny_repo_extension(tmp_path, commits=3)
    out = tmp_path / "dist"
    assert cli.main(["bundle", "--out", str(out), "--extension-dir", str(ext)]) == 0

    built = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert "version_name" not in built
    # …and what the card DOES render stays inside that budget: `0.1.135.2058` is twelve
    # characters, and the ceiling here is what stops the identity creeping back into the
    # rendered field one component at a time.
    assert len(built["version"]) <= 16, built["version"]


def test_cli_bundle_prints_the_sha_and_the_dirty_flag(tmp_path, capsys):
    # The sha, the `-dirty` marker and the full build date left the manifest because they
    # do not fit the card — they must NOT have left the operator's screen with it. The
    # terminal output of `make dev-bundle` is now the only place the full build identity
    # is reported, alongside the version to compare against the card. Redden: drop the
    # `build          : ...` line from cmd_bundle.
    ext = _tiny_repo_extension(tmp_path, commits=3)
    (ext / "background.js").write_text("// uncommitted\n", encoding="utf-8")
    out = tmp_path / "dist"
    assert cli.main(["bundle", "--out", str(out), "--extension-dir", str(ext)]) == 0

    printed = capsys.readouterr().out
    built = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    # The version the card will show is printed verbatim — that comparison is the point.
    assert built["version"] in printed
    # The marker keeps its SHAPE, not merely its presence: `-dirty` QUALIFIES THE SHA and
    # must stay glued to it (see test_build_stamp_dirty_marker_only_looks_at_the_bundle).
    assert re.search(
        r"commit [0-9a-f]{" + str(_FIXTURE_ABBREV) + r"}-dirty, built "
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}",
        printed,
    ), printed


def test_cli_bundle_still_builds_when_git_is_unavailable(tmp_path, monkeypatch, capsys):
    # A build must NEVER fail over the stamp: no git, not a repo, a broken repo — all
    # degrade to the verbatim copy this tool did before the stamp existed, with a note on
    # stderr. Redden: let build_stamp propagate instead of returning None.
    #
    # The seam patched is the MODULE's own `_git`, not stdlib `subprocess.run`: replacing
    # the latter would swap it out for every other consumer for the duration of the test
    # and tie this test to subprocess internals it does not care about.
    def no_git(*_args, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(cli, "_git", no_git)
    out = tmp_path / "dist"
    assert cli.main(["bundle", "--out", str(out), "--extension-dir", str(REPO_EXTENSION)]) == 0

    assert (out / "manifest.json").read_bytes() == (REPO_EXTENSION / "manifest.json").read_bytes()
    built = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    # Untouched: the BASE literal comes through unstamped, and no display field is invented
    # to stand in for the missing stamp.
    assert built["version"] == json.loads(
        (REPO_EXTENSION / "manifest.json").read_text(encoding="utf-8")
    )["version"]
    assert "version_name" not in built
    assert "build stamp unavailable" in capsys.readouterr().err


def test_build_stamp_returns_none_outside_a_git_repo(tmp_path, capsys):
    # A source extension/ that is not in a git repo at all (an unpacked tarball) — the
    # other half of the fallback, exercised without monkeypatching git away. `ls-files
    # --error-unmatch` makes this hold even when tmp_path DOES sit inside some unrelated
    # working tree: no repo tracks this manifest, so there is nothing to trust.
    src = tmp_path / "extension"
    src.mkdir()
    (src / "manifest.json").write_text(json.dumps({"name": "x", "version": "0.1.0"}))
    assert cli.build_stamp(src) is None
    assert "build stamp unavailable" in capsys.readouterr().err


def test_build_stamp_degrades_when_the_manifest_is_not_a_json_object(tmp_path, capsys):
    # A manifest.json that is valid JSON but NOT an object (an array, a string, null) must
    # reach the same fallback as a missing git. `manifest["version"]` on a list raises
    # TypeError, which is not in build_stamp's except tuple — so before the explicit check
    # this killed the build with a traceback, and it used to build fine (copy_bundle only
    # requires manifest.json to BE a file). Redden: index the parsed JSON before the
    # isinstance check.
    src = tmp_path / "extension"
    src.mkdir()
    (src / "manifest.json").write_text('["not", "an", "object"]', encoding="utf-8")

    assert cli.build_stamp(src) is None
    err = capsys.readouterr().err
    assert "build stamp unavailable" in err
    # Named specifically, so this pins the isinstance check and not some git failure.
    assert "must contain a JSON object" in err


def test_cli_bundle_survives_a_manifest_that_is_not_a_json_object(tmp_path, capsys):
    # …and the whole build still succeeds, producing the verbatim copy it produced before
    # the stamp existed. This is the REGRESSION the type check prevents.
    src = tmp_path / "extension"
    src.mkdir()
    (src / "manifest.json").write_text('["not", "an", "object"]', encoding="utf-8")
    out = tmp_path / "dist"

    assert cli.main(["bundle", "--out", str(out), "--extension-dir", str(src)]) == 0
    assert (out / "manifest.json").read_bytes() == (src / "manifest.json").read_bytes()
    assert "build stamp unavailable" in capsys.readouterr().err


def test_build_stamp_degrades_on_a_shallow_clone(tmp_path, monkeypatch, capsys):
    # `git rev-list --count HEAD` returns 1 on a `--depth 1` clone and exits 0, so nothing
    # fails and the stamp comes out confidently WRONG (0.1.1 instead of 0.1.131). Nothing
    # would reach stderr either. actions/checkout defaults to fetch-depth: 1, so any CI
    # bundle build would ship that. Redden: drop the is-shallow-repository check.
    answers = {
        ("ls-files", "--error-unmatch", "--", "manifest.json"): "manifest.json",
        ("rev-parse", "--is-shallow-repository"): "true",
        ("rev-list", "--count", "HEAD"): "1",  # the lie a shallow clone tells
        ("rev-parse", "--short", "HEAD"): "abc1234",
    }
    monkeypatch.setattr(cli, "_git", lambda _repo_dir, *argv: answers.get(argv, ""))

    assert cli.build_stamp(REPO_EXTENSION) is None
    err = capsys.readouterr().err
    assert "build stamp unavailable" in err
    # Named, so the test cannot pass because some other git call happened to fail.
    assert "shallow clone" in err


def test_build_stamp_dirty_marker_only_looks_at_the_bundle(tmp_path):
    # `git status --porcelain` with no pathspec counts `??` untracked entries anywhere in
    # the repo, so one scratch file would glue `-dirty` onto every build from then on and
    # the marker would stop distinguishing a modified tree from committed code — its whole
    # job. Redden: drop the `-- <extension_dir>` pathspec.
    ext = _tiny_repo_extension(tmp_path)
    (ext.parent / "scratch.txt").write_text("not part of the bundle", encoding="utf-8")

    # The marker moved out of the manifest with the rest of the human-readable identity,
    # so it is asserted where it now lives: `build_stamp`'s second return value, which
    # cmd_bundle prints (and which nothing stamps into the version anymore — a numeric
    # manifest version cannot carry a `-dirty` suffix).
    stamp = cli.build_stamp(ext)
    assert stamp is not None
    assert "-dirty" not in stamp[1]
    # A clean tree's marker is a BARE sha, pinned by shape so a silently empty marker or
    # an always-appended suffix cannot pass.
    assert re.fullmatch(r"[0-9a-f]{" + str(_FIXTURE_ABBREV) + r"}", stamp[1]), stamp[1]

    # A change INSIDE the bundle is exactly what the marker is for.
    (ext / "background.js").write_text("// uncommitted\n", encoding="utf-8")
    stamp = cli.build_stamp(ext)
    assert stamp is not None
    # The whole SHAPE is pinned, not just the marker's presence: `-dirty` QUALIFIES THE
    # SHA and must stay glued to it. `abc1234-dirty` says "this build is that commit plus
    # uncommitted edits", while a separate ` · -dirty` token reads as a statement about
    # the build in general. Presence alone leaves that free to move.
    assert re.fullmatch(
        r"[0-9a-f]{" + str(_FIXTURE_ABBREV) + r"}-dirty", stamp[1]
    ), stamp[1]
    # …and the marker stays OUT of the version, which is the field the card renders.
    assert "dirty" not in stamp[0]


def test_build_stamp_ignores_a_repo_that_does_not_track_the_manifest(tmp_path):
    # git walks UP from `-C`, so a source tree unpacked inside an unrelated working tree
    # would get a successful rev-list from THAT repo — a confidently wrong stamp with
    # nothing to degrade on. `ls-files --error-unmatch` is what refuses it. Redden: drop
    # that call and this untracked-but-inside-a-repo bundle gets stamped from the host repo.
    ext = _tiny_repo_extension(tmp_path)
    stray = ext.parent / "unpacked" / "extension"
    stray.mkdir(parents=True)
    (stray / "manifest.json").write_text(
        json.dumps({"name": "x", "version": "0.3.0"}), encoding="utf-8"
    )
    assert cli.build_stamp(stray) is None


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


def test_generated_app_truncates_a_four_component_version_for_the_plist(tmp_path):
    # A Chrome manifest `version` may carry FOUR components; Apple allows at most three in
    # CFBundleShortVersionString/CFBundleVersion, so writing all four out makes a formally
    # invalid plist. Same "validate what you write" reasoning as validate_manifest_version.
    # Redden: pass `version` through to build_info_plist untruncated.
    bundle = tmp_path / "dist"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(json.dumps({"name": "x", "version": "1.2.3.4"}))
    res = core.generate_instance(
        out_root=tmp_path / "inst", bundle_dir=bundle, instance_id="main", title="Main"
    )
    plist = res.paths.info_plist.read_text()
    assert "<string>1.2.3</string>" in plist
    assert "1.2.3.4" not in plist


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
    # No extension-sync prologue unless it is asked for (core defaults to off; the CLI
    # is what turns it on). Redden: make sync_extensions_from default to a path in core.
    assert "$EXTS" not in script


def test_launcher_without_extension_sync_is_byte_identical(tmp_path):
    """The no-sync launcher is EXACTLY the script this generator has always emitted.

    A no-regression pin, deliberately byte-for-byte: `--no-sync-extensions` (and every
    caller that passes no sync dir at all) must keep producing the old launcher, so the
    extension-sync feature cannot leak a single character into it. Redden: change any
    byte of `render_launcher_script`'s non-sync output — including its comment header,
    which is why the sync explanation lives inside the sync-only prologue instead.
    """
    assert core.render_launcher_script("/bin/brave", "/p", "/e", ["--foo"]) == (
        "#!/bin/sh\n"
        "# Auto-generated by tools/instancegen (§13). Do not edit by hand;\n"
        "# regenerate instead. Launches the SYSTEM Brave — NOT Google Chrome,\n"
        "# which refuses --load-extension (§13, arch row 20).\n"
        "set -eu\n"
        "exec '/bin/brave' \\\n"
        "  --user-data-dir='/p' \\\n"
        "  --load-extension='/e' \\\n"
        "  '--foo' \\\n"
        '  "$@"\n'
    )
    # Same for the full generate path, and explicitly for sync_extensions_from=None.
    assert core.render_launcher_script(
        "/bin/brave", "/p", "/e", ["--foo"], sync_extensions_from=None
    ) == core.render_launcher_script("/bin/brave", "/p", "/e", ["--foo"])


# --------------------------------------------------------------------------- #
# Launcher content: main-profile extensions, resolved at LAUNCH
#
# These run the generated script with `sh` (not bash) against a stub "brave" that prints
# its argv, so the POSIX-sh claim in the script's shebang is actually exercised — and so
# is `set -eu`, which an empty/absent main profile must not trip.
# --------------------------------------------------------------------------- #
def _stub_brave(tmp_path) -> Path:
    """A fake Brave that prints each argv entry on its own line."""
    stub = tmp_path / "brave-stub"
    stub.write_text('#!/bin/sh\nfor a in "$@"; do echo "$a"; done\n')
    stub.chmod(0o755)
    return stub


def _run_launcher(launcher: Path, *, shell: str = "sh", locale: str | None = None) -> list[str]:
    """Run *launcher* under *shell* and return the argv the stub Brave saw.

    *locale* sets `LC_ALL` for the run: glob expansion is sorted by the current collating
    sequence, so the ORDER in which the launcher walks the main profile's ids — and hence
    which id happens to be the loop's last iteration — is locale-dependent (`C` puts
    `Temp` first, `en_US.UTF-8` puts it between `bbb` and `zzz-…`). The degradation tests
    must not depend on which one the developer's machine happens to use.
    """
    env = dict(os.environ)
    if locale is not None:
        env["LC_ALL"] = locale
    out = subprocess.run(
        [shell, str(launcher)], capture_output=True, text=True, check=True, env=env
    )
    return out.stdout.splitlines()


def _loaded_extensions(launcher: Path, **kw) -> list[str]:
    """The `--load-extension` value the launcher actually passed, split into paths."""
    argv = _run_launcher(launcher, **kw)
    flags = [a for a in argv if a.startswith("--load-extension=")]
    assert len(flags) == 1, argv
    return flags[0].split("=", 1)[1].split(",")


def _sync_instance(tmp_path, ext: Path, bundle: Path | None = None, name="inst"):
    """Generate an instance whose launcher syncs *ext* and execs the argv-printing stub."""
    return _gen(tmp_path / name, "alpha",
                bundle_dir=bundle if bundle is not None else _make_bundle(tmp_path),
                brave_binary=str(_stub_brave(tmp_path)), sync_extensions_from=ext)


def _fake_main_profile(tmp_path) -> Path:
    """A Default/Extensions tree shaped like the real one, warts included.

    Mirrors what the owner's profile actually contains: several ids, one of them keeping
    THREE version dirs side by side (old versions are not collected immediately), plus
    Chromium's `Temp` staging dir. `bbb`'s newest dir by MTIME is `1.10.0_0` while the
    lexicographically LAST one is `1.9.0_0` — sorting by name picks the wrong (older) one,
    which is the entire reason the launcher sorts by mtime.

    Two entries are deliberately degenerate:

    * `.hidden/` is a hidden DIRECTORY carrying a perfectly good manifest, and it must
      still not be loaded — POSIX `*` never expands to a leading dot, so `"$MAIN"/*/`
      never yields it. That is why the launcher's `case` needs no `.*` branch: a branch
      for it would be dead code (the `.DS_Store` this fixture used to plant could not
      have reached it either, being a FILE that `*/` cannot match).
    * `zzz-nomanifest/` has a version dir with NO manifest.json, and is named to sort
      LAST under every collation (`z` follows both `T`/`Temp` and every other id in C and
      in en_US.UTF-8). So the id whose manifest check fails is always the loop's FINAL
      iteration, deterministically — the point at which a non-zero status from the loop
      body would reach `set -eu` and kill the launcher before it ever execs Brave.
    """
    ext = tmp_path / "main" / "Extensions"
    for rel in ("aaa/1.0.0_0", "bbb/1.2.0_0", "bbb/1.9.0_0", "bbb/1.10.0_0",
                "Temp/9.9.9_0", ".hidden/1.0.0_0", "zzz-nomanifest/1.0.0_0"):
        (ext / rel).mkdir(parents=True)
        if not rel.startswith("zzz-nomanifest/"):
            (ext / rel / "manifest.json").write_text('{"name": "x", "key": "k"}')
    # mtimes: oldest 1.2.0_0, then 1.9.0_0, newest 1.10.0_0 (name order says otherwise).
    for name, stamp in (("1.2.0_0", 1_700_000_000), ("1.9.0_0", 1_700_001_000),
                        ("1.10.0_0", 1_700_002_000)):
        os.utime(ext / "bbb" / name, (stamp, stamp))
    return ext


# Both collations the fixture's ordering claim covers: `C` walks `Temp` FIRST, a UTF-8
# collation walks it in the middle. Either way `zzz-nomanifest` is last (see
# `_fake_main_profile`). A machine without en_US.UTF-8 silently falls back to C, which
# only makes the run a duplicate of the first — never a false pass.
_COLLATIONS = ["C", "en_US.UTF-8"]


@pytest.mark.parametrize("locale", _COLLATIONS)
def test_launcher_loads_main_profile_extensions_newest_version_by_mtime(tmp_path, locale):
    """The curator bundle FIRST, then one dir per real extension id, newest by mtime.

    Redden: sort by name instead of mtime (picks bbb/1.9.0_0), drop the `Temp` case, or
    drop the manifest.json check (adds `zzz-nomanifest`). The hidden `.hidden/` dir is
    absent because of the glob itself; redden that by globbing dotfiles too.

    Run under both collations because the walk order — and therefore which id lands in
    the loop's last, `set -e`-exposed iteration — is the locale's choice, not ours.
    """
    ext = _fake_main_profile(tmp_path)
    bundle = _make_bundle(tmp_path)
    res = _sync_instance(tmp_path, ext, bundle)
    assert _loaded_extensions(res.paths.launcher, locale=locale) == [
        str(bundle.resolve()),          # the curator bundle is always first
        str(ext / "aaa" / "1.0.0_0"),
        str(ext / "bbb" / "1.10.0_0"),  # NOT 1.9.0_0, which sorts last by name
        # no Temp/, no .hidden/, no zzz-nomanifest/
    ]


@pytest.mark.parametrize("shell", ["sh", "dash"])
def test_launcher_falls_back_when_the_newest_version_dir_has_no_manifest(tmp_path, shell):
    """One unloadable version dir must not drop the extension — the sibling next to it wins.

    This is a REAL state, not a hypothetical: the main browser garbage-collecting an old
    version deletes the files inside that dir, which RAISES its mtime above the live one,
    so for a moment the newest dir on disk is a manifest-less husk (a browser killed
    mid-cleanup leaves it that way permanently). Taking only the first candidate meant the
    extension — as likely Bitwarden as anything else — was absent from the instance
    entirely, with nothing said anywhere.

    Redden: take `ls -dt … | head -1` and test that single candidate.
    """
    if shutil.which(shell) is None:  # pragma: no cover - dash is not on every box
        pytest.skip(f"{shell} is not installed here")
    ext = tmp_path / "main" / "Extensions"
    good = ext / "bitwarden" / "1.0.0_0"
    husk = ext / "bitwarden" / "1.1.0_0" / "_metadata"  # newer, no manifest.json
    good.mkdir(parents=True)
    (good / "manifest.json").write_text('{"name": "x", "key": "k"}')
    husk.mkdir(parents=True)
    os.utime(good.parent / "1.0.0_0", (1_700_000_000, 1_700_000_000))
    os.utime(good.parent / "1.1.0_0", (1_700_009_000, 1_700_009_000))
    bundle = _make_bundle(tmp_path)
    res = _sync_instance(tmp_path, ext, bundle)
    assert _loaded_extensions(res.paths.launcher, shell=shell) == [
        str(bundle.resolve()), str(good)
    ]


def test_launcher_re_resolves_extensions_at_every_launch(tmp_path):
    """An extension UPDATE in the main profile is picked up with no regeneration.

    This is why the paths are not baked in at generation time: the main browser owns
    those dirs and writes a new `<version>_0` on every update. Redden: bake the resolved
    paths into the script — the launcher then keeps loading the vanished 1.0.0_0.
    """
    ext = _fake_main_profile(tmp_path)
    bundle = _make_bundle(tmp_path)
    res = _sync_instance(tmp_path, ext, bundle)
    assert str(ext / "aaa" / "1.0.0_0") in _loaded_extensions(res.paths.launcher)

    # The main browser updates `aaa` and collects the old version — no regeneration here.
    (ext / "aaa" / "2.0.0_0").mkdir()
    (ext / "aaa" / "2.0.0_0" / "manifest.json").write_text('{"name": "x", "key": "k"}')
    shutil.rmtree(ext / "aaa" / "1.0.0_0")
    loaded = _loaded_extensions(res.paths.launcher)
    assert str(ext / "aaa" / "2.0.0_0") in loaded
    assert str(ext / "aaa" / "1.0.0_0") not in loaded


def _snapshot(tree: Path) -> dict[str, tuple[float, int]]:
    """(relative path) -> (mtime, size) for every entry under *tree*, dirs included."""
    return {
        str(p.relative_to(tree)): (p.stat().st_mtime, p.stat().st_size)
        for p in sorted(tree.rglob("*"))
    }


@pytest.mark.parametrize("locale", _COLLATIONS)
def test_launcher_never_writes_into_the_main_profile(tmp_path, locale):
    """THE safety property of this feature: the main profile is READ, never touched.

    That profile is the owner's real browser — its extension dirs, and the browser that
    owns them, are live while an instance runs. The launcher must only ever glob and
    `[ -f ]` in there: no copy, no temp file, no reordered dir, not even a `mkdir -p` on a
    missing path. Asserted as a full before/after snapshot of (relpath, mtime, size) —
    every entry, files and directories both.

    Redden: make the launcher (or the generator) write anything under `$MAIN` — a
    `mkdir -p "$MAIN"`, a staging copy of an extension, a cached list of resolved dirs.
    """
    ext = _fake_main_profile(tmp_path)
    before = _snapshot(ext)
    res = _sync_instance(tmp_path, ext)
    _run_launcher(res.paths.launcher, locale=locale)
    assert _snapshot(ext) == before


@pytest.mark.parametrize("state", ["missing", "empty"])
def test_launcher_still_loads_the_bundle_without_a_usable_main_profile(tmp_path, state):
    """No main profile (or an empty one) must not abort the launcher under `set -eu`.

    An instance on a machine without that profile has to come up exactly as it did
    before this feature. Redden: drop the `[ -d "$MAIN" ]` guard, or let the failing
    `ls` / a false test at the end of the loop body kill the shell.
    """
    ext = tmp_path / "main" / "Extensions"
    if state == "empty":
        ext.mkdir(parents=True)
    bundle = _make_bundle(tmp_path)
    res = _sync_instance(tmp_path, ext, bundle)
    assert _loaded_extensions(res.paths.launcher) == [str(bundle.resolve())]


@pytest.mark.parametrize("broken", ["no-version-dirs", "unreadable"])
@pytest.mark.parametrize("locale", _COLLATIONS)
def test_launcher_skips_a_degenerate_id_dir_and_keeps_the_rest(tmp_path, broken, locale):
    """A degenerate id dir costs THAT id — never the launch, never the other extensions.

    Both states happen for real: an id dir with no `<version>_0` inside it is what an
    uninstall leaves behind mid-collection, and an unreadable one is what a profile
    generated under another user (or a `sudo`-built instance) looks like. In both the
    glob inside the id dir cannot be expanded at all, `ls` fails, and under `set -eu`
    that must degrade to "skip this id" rather than kill the shell before `exec`.

    The broken id sorts LAST in every collation, so it is the loop's final iteration —
    the one whose exit status the `for`, the `if` and then `set -e` actually see.

    Redden: drop the `2>/dev/null`-guarded, status-0 shape of the candidate loop (e.g.
    end the loop body on a bare `[ -n "$v" ] && …` false chain).
    """
    if broken == "unreadable" and os.geteuid() == 0:  # pragma: no cover - CI runs as root
        pytest.skip("root ignores directory permissions, so nothing would be unreadable")
    ext = tmp_path / "main" / "Extensions"
    good = ext / "aaa" / "1.0.0_0"
    good.mkdir(parents=True)
    (good / "manifest.json").write_text('{"name": "x", "key": "k"}')
    bad = ext / "zzz-broken"
    bad.mkdir()
    if broken == "unreadable":
        (bad / "1.0.0_0").mkdir()
        (bad / "1.0.0_0" / "manifest.json").write_text('{"name": "x", "key": "k"}')
        os.chmod(bad, 0o000)
    try:
        bundle = _make_bundle(tmp_path)
        res = _sync_instance(tmp_path, ext, bundle)
        assert _loaded_extensions(res.paths.launcher, locale=locale) == [
            str(bundle.resolve()), str(good)
        ]
    finally:
        os.chmod(bad, 0o755)  # else tmp_path cleanup cannot descend into it


def test_launcher_survives_spaces_in_the_synced_profile_path(tmp_path):
    # The real path is "~/Library/Application Support/BraveSoftware/…" — unquoted it
    # would split. Redden: drop the _sh_quote around MAIN, or split the `ls -dt` output on
    # $IFS (`for v in $(ls …)`) instead of reading it a line at a time.
    ext = tmp_path / "Application Support" / "Extensions"
    (ext / "aaa" / "1.0.0_0").mkdir(parents=True)
    (ext / "aaa" / "1.0.0_0" / "manifest.json").write_text("{}")
    bundle = _make_bundle(tmp_path)
    res = _sync_instance(tmp_path, ext, bundle)
    assert _loaded_extensions(res.paths.launcher) == [
        str(bundle.resolve()), str(ext / "aaa" / "1.0.0_0")
    ]


def test_cli_syncs_main_profile_extensions_by_default_and_can_opt_out(tmp_path):
    """ON by default (an instance with no Bitwarden is not a usable browser), opt-out flag.

    Redden: flip the default to off, or drop --no-sync-extensions.
    """
    parser = cli.build_parser()
    base = ["generate", "--instance-id", "a", "--bundle-dir", str(tmp_path),
            "--out", str(tmp_path)]
    assert parser.parse_args(base).sync_extensions == core.DEFAULT_MAIN_EXTENSIONS_DIR
    assert core.DEFAULT_MAIN_EXTENSIONS_DIR.startswith("~/Library/Application Support/")
    assert parser.parse_args(base + ["--no-sync-extensions"]).sync_extensions is None
    assert parser.parse_args(base + ["--sync-extensions", "/p"]).sync_extensions == "/p"


def test_cli_generate_bakes_an_absolute_expanded_sync_path(tmp_path):
    # `~` inside the sh-quoted literal would never expand, and the launcher runs from an
    # arbitrary CWD. Redden: pass args.sync_extensions through without expanduser/resolve.
    bundle = _make_bundle(tmp_path)
    out = tmp_path / "inst"
    rc = cli.main(["generate", "--instance-id", "main", "--bundle-dir", str(bundle),
                   "--out", str(out), "--sync-extensions", "~/Some Where/Extensions"])
    assert rc == 0
    script = (out / "main" / "main.app" / "Contents" / "MacOS" / "run").read_text()
    assert f"MAIN='{Path.home()}/Some Where/Extensions'" in script
    assert "~" not in script


def test_cli_generate_refuses_an_empty_sync_path(tmp_path):
    """`--sync-extensions ""` is MISSING configuration, not the current directory.

    `Path("").expanduser().resolve()` is the CWD, so an unset `--sync-extensions
    "$BRAVE_PROFILE"` would bake the generator's working directory (the repo root) into
    the launcher and quietly load whatever `*/*/manifest.json` happens to live there.
    Redden: go back to `if sync_extensions is not None` alone — the empty string sails
    through it and the launcher gets `MAIN='<cwd>'`.
    """
    bundle = _make_bundle(tmp_path)
    out = tmp_path / "inst"
    with pytest.raises(SystemExit, match="EMPTY path"):
        cli.main(["generate", "--instance-id", "main", "--bundle-dir", str(bundle),
                  "--out", str(out), "--sync-extensions", ""])
    assert not out.exists()  # nothing generated at all


def test_cli_generate_notes_a_sync_path_that_does_not_exist(tmp_path, capsys):
    """A missing main profile degrades — loudly on stderr, exactly like `build_stamp`.

    Sync is ON by default and the default path comes from the GENERATING user's `~`, so a
    `.app` copied to another Mac or generated under `sudo` points at a directory that is
    not there. The launcher's `[ -d "$MAIN" ]` guard then skips all ~26 extensions and
    says nothing, the browser says nothing either — this note is the only signal. It must
    NOT be fatal: a missing main profile is a legitimate state, and the launcher re-checks
    the path at every launch. Redden: drop the note (generation goes silent) or raise
    instead of noting (a legitimate state becomes a failed build).
    """
    bundle = _make_bundle(tmp_path)
    out = tmp_path / "inst"
    missing = tmp_path / "no-such-profile" / "Extensions"
    rc = cli.main(["generate", "--instance-id", "main", "--bundle-dir", str(bundle),
                   "--out", str(out), "--sync-extensions", str(missing)])
    assert rc == 0  # generation SUCCEEDS
    err = capsys.readouterr().err
    assert "does not exist" in err and str(missing) in err
    # …and the same path is still baked in, because it may exist by the next launch.
    script = (out / "main" / "main.app" / "Contents" / "MacOS" / "run").read_text()
    assert f"MAIN='{missing}'" in script


def test_cli_generate_says_nothing_when_the_sync_path_is_there(tmp_path, capsys):
    # The note must mark a real degrade, not fire on every healthy run. Redden: print it
    # unconditionally.
    ext = _fake_main_profile(tmp_path)
    bundle = _make_bundle(tmp_path)
    rc = cli.main(["generate", "--instance-id", "main", "--bundle-dir", str(bundle),
                   "--out", str(tmp_path / "inst"), "--sync-extensions", str(ext)])
    assert rc == 0
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("which", ["bundle", "sync"])
def test_generate_refuses_a_comma_in_either_baked_path(tmp_path, which):
    """Chromium splits `--load-extension` on commas, so a comma in either path is fatal.

    Extension ids and `<version>_0` dirs cannot contain a comma, but these two paths are
    operator-chosen — and one comma in the MAIN path cuts every one of the ~27 entries
    built under it into halves that name nothing, with the browser reporting nothing.
    Redden: drop the check and the launcher is generated with the comma in it.
    """
    ext = _fake_main_profile(tmp_path)
    bundle = _make_bundle(tmp_path, name="di,st" if which == "bundle" else "dist")
    if which == "sync":
        comma_dir = tmp_path / "ma,in"
        comma_dir.mkdir()
        shutil.copytree(ext, comma_dir / "Extensions")
        ext = comma_dir / "Extensions"
    with pytest.raises(ValueError, match="comma"):
        _gen(tmp_path / "inst", "alpha", bundle_dir=bundle, sync_extensions_from=ext)


def test_generate_result_carries_no_reconstructed_launch_command(tmp_path, capsys):
    """The printed `launch:` line names the LAUNCHER, not an argv that would be a lie.

    With sync on, the launcher resolves `--load-extension` at launch into the bundle plus
    one dir per main-profile extension. A reconstructed argv (the old
    `GenerateResult.launch_command`) still said `--load-extension=<bundle>` — a single
    path — so an operator debugging "why is Bitwarden missing in this instance" copied
    that line, got a browser without Bitwarden and concluded the sync was at fault. This
    repo does not accept silent divergence (cf. the build stamp's `-dirty` marker), so the
    field is gone and the script is the one source of truth for the argv.

    Redden: put `launch_command` back on the dataclass and print `shlex.join` of it.
    """
    ext = _fake_main_profile(tmp_path)
    bundle = _make_bundle(tmp_path)
    res = _sync_instance(tmp_path, ext, bundle)
    assert not hasattr(res, "launch_command")

    out = tmp_path / "cli-out"
    rc = cli.main(["generate", "--instance-id", "main", "--bundle-dir", str(bundle),
                   "--out", str(out), "--sync-extensions", str(ext)])
    assert rc == 0
    launcher = out / "main" / "main.app" / "Contents" / "MacOS" / "run"
    line = next(ln for ln in capsys.readouterr().out.splitlines() if "launch:" in ln)
    assert str(launcher) in line
    # The one thing it must never print is a `--load-extension=` the instance does not use.
    assert "--load-extension=" not in line


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
