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

from tools.instancegen import cli, core, macos, state

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


def test_cli_generate_stdout_does_not_claim_a_sync_that_is_not_there(tmp_path, capsys):
    """STDOUT must say NOT FOUND for a missing sync dir — the stderr note is not enough.

    The two streams are routinely separated (`make instance > build.log`), and the kept one
    is stdout: the cheerful `sync extensions: <path> (re-read at EVERY launch …)` line then
    stands alone, advertising ~26 extensions the instance will not have. `cmd_bundle`
    already handles its own degrade this way ("NOT stamped (see the note above)").

    Redden: print the same stdout line in both states.
    """
    bundle = _make_bundle(tmp_path)
    missing = tmp_path / "no-such-profile" / "Extensions"
    rc = cli.main(["generate", "--instance-id", "main", "--bundle-dir", str(bundle),
                   "--out", str(tmp_path / "gone"), "--sync-extensions", str(missing)])
    assert rc == 0
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if "sync extensions:" in ln)
    assert "NOT FOUND" in line and str(missing) in line
    assert "re-read at EVERY launch" not in out

    # …and the healthy run keeps the plain line, with no scare word on it.
    ext = _fake_main_profile(tmp_path)
    rc = cli.main(["generate", "--instance-id", "main", "--bundle-dir", str(bundle),
                   "--out", str(tmp_path / "here"), "--sync-extensions", str(ext)])
    assert rc == 0
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if "sync extensions:" in ln)
    assert "NOT FOUND" not in out
    assert "re-read at EVERY launch" in line and str(ext) in line


@pytest.mark.parametrize("which", ["bundle", "sync"])
def test_generate_refuses_a_comma_in_either_baked_path(tmp_path, which):
    """Chromium splits `--load-extension` on commas, so a comma in either path is fatal.

    Extension ids and `<version>_0` dirs cannot contain a comma, but these two paths are
    operator-chosen — and one comma in the MAIN path cuts every one of the ~27 entries
    built under it into halves that name nothing, with the browser reporting nothing.
    Redden: drop the check and the launcher is generated with the comma in it.

    The CLI leg pins WHERE the refusal happens: like the empty-path check, it must run
    BEFORE `out_root.mkdir`, so a refused invocation leaves no half-made output tree for
    the operator to clean up. Redden: leave the check only in `generate_instance`, which
    runs after the mkdir — `--out` is then created and left behind.
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

    out = tmp_path / "cli-out"
    with pytest.raises(ValueError, match="comma"):
        cli.main(["generate", "--instance-id", "main", "--bundle-dir", str(bundle),
                  "--out", str(out), "--sync-extensions", str(ext)])
    assert not out.exists()  # nothing created at all


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


# --------------------------------------------------------------------------- #
# copy-state: the ONE-TIME copy of per-extension state into an instance
# --------------------------------------------------------------------------- #
# The id shapes below stand for the two kinds this feature must tell apart:
# a STORE-installed extension (unpacked by the browser into `Extensions/<id>/`) and the
# CURATOR-style one (loaded unpacked from a shared dir, so it has the SAME id in every
# profile and NO `Extensions/<id>` dir anywhere).
_STORE_ID = "nngceckbapebfimnlniiiahkandclblb"  # shaped like Bitwarden's
_STORE_ID_2 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
_CURATOR_ID = "cccccccccccccccccccccccccccccccc"


def _install_unpacked(source: Path, ext_id: str, version="1.0.0_0") -> Path:
    """Make *ext_id* look INSTALLED in a source profile, the way the browser leaves it.

    A `<version>/manifest.json`, because that — not a bare `Extensions/<id>` — is what the
    launcher itself treats as an installed extension (`core._render_extension_sync`), and
    the copy's eligibility filter now uses the same definition.
    """
    d = source / state.EXTENSIONS_DIRNAME / ext_id / version
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"name": ext_id, "version": "1.0.0"}))
    return d


def _fake_source_profile(tmp_path) -> Path:
    """A main profile's `Default` dir with both kinds of extension in it.

    `_STORE_ID` is installed unpacked and has BOTH state dirs; `_STORE_ID_2` is installed
    and has only `Local Extension Settings` (nothing ever used chrome.storage.sync — the
    common case); `_CURATOR_ID` has state but NO `Extensions/` dir at all, exactly like the
    curator extension in the owner's real profile.
    """
    source = tmp_path / "main" / "Default"
    for ext_id in (_STORE_ID, _STORE_ID_2):
        _install_unpacked(source, ext_id)
    for ext_id in (_STORE_ID, _STORE_ID_2, _CURATOR_ID):
        d = source / state.LOCAL_SETTINGS_DIRNAME / ext_id
        d.mkdir(parents=True)
        (d / "000003.ldb").write_text(f"local-{ext_id}")
        (d / "LOCK").write_text("")
    sync = source / state.SYNC_SETTINGS_DIRNAME / _STORE_ID
    sync.mkdir(parents=True)
    (sync / "000003.ldb").write_text("sync-store")
    return source


def _instance_with_state(tmp_path, name="infra") -> Path:
    """An instance root (as `generate` writes it) whose profile ALREADY holds state.

    Both a store extension's dir and the curator's are pre-populated, because both are
    real: the instance has been running, its store extensions minted empty databases and
    the curator minted THIS install's identity.
    """
    inst = tmp_path / name
    default = inst / "profile" / "Default"
    for ext_id, marker in ((_STORE_ID, "instance-own-state"),
                           (_CURATOR_ID, "instance-install-uuid")):
        d = default / state.LOCAL_SETTINGS_DIRNAME / ext_id
        d.mkdir(parents=True)
        (d / "000001.log").write_text(marker)
    return inst


def _copy(tmp_path, source, inst, monkeypatch, *, running=(), only=None):
    """Run the copy with the process check faked — never a real `pgrep` in a test.

    `running_brave_processes` is THE seam (its own docstring says so): patching it keeps
    the test off the machine's actual process table, which would otherwise decide whether
    the suite passes depending on whether the developer has Brave open.
    """
    monkeypatch.setattr(state, "running_brave_processes", lambda *a, **kw: list(running))
    return state.copy_extension_state(
        source_default_dir=source, instance_dir=inst, only=only
    )


def _local(inst: Path, ext_id: str) -> Path:
    return inst / "profile" / "Default" / state.LOCAL_SETTINGS_DIRNAME / ext_id


def _sync(inst: Path, ext_id: str) -> Path:
    return inst / "profile" / "Default" / state.SYNC_SETTINGS_DIRNAME / ext_id


def test_copy_state_never_copies_an_id_without_an_extensions_dir(tmp_path, monkeypatch):
    """THE identity guard: an id with state but no `Extensions/<id>` is left alone.

    The curator extension is loaded unpacked from a shared directory, so it carries the
    SAME chrome-extension:// id in every profile while having no `Extensions/` dir in any
    of them — and its chrome.storage.local holds THIS instance's install_uuid and
    per-install secret. Copying it would hand the instance the main browser's identity and
    the service would see a different install.

    Redden: drop the `Extensions/<id>` filter in `eligible_extension_ids` — the curator's
    marker below is overwritten by the main profile's state.
    """
    source = _fake_source_profile(tmp_path)
    inst = _instance_with_state(tmp_path)
    copied = _copy(tmp_path, source, inst, monkeypatch)

    assert [c.extension_id for c in copied] == sorted([_STORE_ID, _STORE_ID_2])
    assert _CURATOR_ID not in [c.extension_id for c in copied]
    # The instance's OWN identity is still there, untouched.
    assert (_local(inst, _CURATOR_ID) / "000001.log").read_text() == "instance-install-uuid"
    assert not (_local(inst, _CURATOR_ID) / "000003.ldb").exists()


def test_copy_state_replaces_the_destination_instead_of_merging(tmp_path, monkeypatch):
    """An eligible id IS copied, and what the instance had is REPLACED, not merged.

    A LevelDB is a set of files that only make sense together — fresh `.ldb` files next to
    a stale `MANIFEST`/log is a database that is neither. Redden: copy with
    `dirs_exist_ok=True` instead of replacing, and the instance's own `000001.log` survives
    alongside the copied files.
    """
    source = _fake_source_profile(tmp_path)
    inst = _instance_with_state(tmp_path)
    _copy(tmp_path, source, inst, monkeypatch)

    dest = _local(inst, _STORE_ID)
    assert (dest / "000003.ldb").read_text() == f"local-{_STORE_ID}"
    assert not (dest / "000001.log").exists()  # the instance's previous content is gone
    assert sorted(p.name for p in dest.iterdir()) == ["000003.ldb", "LOCK"]


def test_copy_state_takes_sync_settings_when_present_and_skips_them_silently(
    tmp_path, monkeypatch
):
    """`Sync Extension Settings` comes along when the source has it, and is not invented.

    Most extensions never touch chrome.storage.sync, so its absence is normal and must not
    raise or leave an empty dir behind. Redden: copy the Sync dir unconditionally.
    """
    source = _fake_source_profile(tmp_path)
    inst = _instance_with_state(tmp_path)
    copied = {c.extension_id: c for c in _copy(tmp_path, source, inst, monkeypatch)}

    assert (_sync(inst, _STORE_ID) / "000003.ldb").read_text() == "sync-store"
    assert copied[_STORE_ID].parts == (
        state.LOCAL_SETTINGS_DIRNAME, state.SYNC_SETTINGS_DIRNAME
    )
    # The source has no Sync dir for the second extension: nothing is created for it.
    assert not _sync(inst, _STORE_ID_2).exists()
    assert copied[_STORE_ID_2].parts == (state.LOCAL_SETTINGS_DIRNAME,)


def test_copy_state_only_restricts_the_set(tmp_path, monkeypatch):
    """`ONLY=` moves just the named ids — the point being "just Bitwarden, nothing else".

    Redden: ignore `only` and every eligible id is copied, so the second extension's dir
    appears in the instance.
    """
    source = _fake_source_profile(tmp_path)
    inst = _instance_with_state(tmp_path)
    copied = _copy(tmp_path, source, inst, monkeypatch, only=[_STORE_ID])

    assert [c.extension_id for c in copied] == [_STORE_ID]
    assert (_local(inst, _STORE_ID) / "000003.ldb").is_file()
    assert not _local(inst, _STORE_ID_2).exists()


def test_copy_state_only_refuses_an_id_that_has_no_extensions_dir(tmp_path, monkeypatch):
    """`ONLY=<curator id>` is refused rather than quietly obeyed — the filter has no bypass.

    Redden: apply `only` to the raw state dirs instead of intersecting it with the eligible
    set, and an explicit id becomes a way around the identity guard.
    """
    source = _fake_source_profile(tmp_path)
    inst = _instance_with_state(tmp_path)
    with pytest.raises(state.StateCopyRefused, match=_CURATOR_ID):
        _copy(tmp_path, source, inst, monkeypatch, only=[_CURATOR_ID])
    assert (_local(inst, _CURATOR_ID) / "000001.log").read_text() == "instance-install-uuid"


def test_copy_state_only_tells_a_typo_apart_from_an_identity_clone(tmp_path, monkeypatch):
    """The three refusals `--only` can have are three DIFFERENT sentences.

    All three used to be one: "no Extensions/<id> dir … its storage holds THIS install's
    identity". So a one-character typo in Bitwarden's id told the owner he had nearly
    overwritten his own `install_uuid` — a security incident report for a slip of the
    finger, which is how a real refusal stops being read.

    Redden: collapse any two cases back into one message.
    """
    source = _fake_source_profile(tmp_path)
    inst = _instance_with_state(tmp_path)
    # An id installed in the source but with NO stored state: nothing to copy, no drama.
    installed_no_state = "dddddddddddddddddddddddddddddddd"
    _install_unpacked(source, installed_no_state)

    typo = _STORE_ID[:-1] + "x"
    with pytest.raises(state.StateCopyRefused) as typo_exc:
        _copy(tmp_path, source, inst, monkeypatch, only=[typo])
    with pytest.raises(state.StateCopyRefused) as empty_exc:
        _copy(tmp_path, source, inst, monkeypatch, only=[installed_no_state])
    with pytest.raises(state.StateCopyRefused) as identity_exc:
        _copy(tmp_path, source, inst, monkeypatch, only=[_CURATOR_ID])

    # A typo is called a typo, and is NOT accused of cloning an identity.
    assert "TYPO" in str(typo_exc.value)
    assert "install_uuid" not in str(typo_exc.value)
    # An installed extension with no state has nothing to copy — also not an identity case.
    assert "NOTHING to copy" in str(empty_exc.value)
    assert "install_uuid" not in str(empty_exc.value)
    # Only the real case keeps the identity wording.
    assert "install_uuid and enrollment secret" in str(identity_exc.value)


def test_copy_state_refuses_while_brave_is_running_and_copies_nothing(
    tmp_path, monkeypatch
):
    """THE safety requirement: a live browser blocks the copy, and nothing is written.

    These are live LevelDB databases; snapshotting one under its own writer can copy a
    half-flushed log and leave the instance with a corrupt vault. There is no --force, so
    this is the only outcome while Brave runs.

    Redden: skip the check (or add a --force that bypasses it) — the destination below
    changes.
    """
    source = _fake_source_profile(tmp_path)
    inst = _instance_with_state(tmp_path)
    before = _snapshot(inst)
    running = [
        "93062 /Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "32643 /Applications/Brave Browser.app/Contents/MacOS/Brave Browser "
        f"--user-data-dir={inst}/profile --load-extension=/x",
        "1464 /Applications/Brave Browser.app/…/Brave Browser Helper --type=renderer",
    ]
    with pytest.raises(state.StateCopyRefused) as excinfo:
        _copy(tmp_path, source, inst, monkeypatch, running=running)

    assert _snapshot(inst) == before  # not one byte written
    # The message NAMES what to quit: the main browser and that instance's profile, and
    # not the renderer helper, which quitting the browser takes with it anyway.
    message = str(excinfo.value)
    assert "the MAIN Brave profile" in message
    assert f"{inst}/profile" in message
    assert "renderer" not in message
    # …and it names them by PID, so a refusal is something the operator can act on.
    assert "pid 93062" in message
    assert "pid 32643" in message


def test_copy_state_refuses_when_the_process_check_itself_fails(tmp_path, monkeypatch):
    """No `pgrep`, no copy: an unverified guard must not authorise the copy.

    Redden: return `[]` from `running_brave_processes` when pgrep cannot be run — the guard
    then fails OPEN, i.e. it is exactly as good as no guard on the machine where it breaks.
    """
    def no_pgrep(*_args, **_kwargs):
        raise FileNotFoundError("pgrep")

    monkeypatch.setattr(state.subprocess, "run", no_pgrep)
    with pytest.raises(state.StateCopyRefused, match="cannot check"):
        state.running_brave_processes()


def test_copy_state_interrupted_leaves_the_instances_previous_state_intact(
    tmp_path, monkeypatch
):
    """A copy that dies half-way leaves the destination exactly as it was (stage-and-swap).

    The fresh tree is built beside the target and swapped in whole (core.replace_tree), so
    the failure window is two renames rather than the length of a 7 MB copy. Redden:
    rmtree the destination and copytree straight into it — the assertion below then finds
    the half-written tree instead of the instance's own state.
    """
    source = _fake_source_profile(tmp_path)
    inst = _instance_with_state(tmp_path)

    def die_half_way(_src, dst, *_args, **_kwargs):
        # A HALF-written tree, then death — the state a power cut leaves behind.
        Path(dst).mkdir(parents=True)
        (Path(dst) / "000003.ldb").write_text("half a database")
        raise KeyboardInterrupt("power cut")

    monkeypatch.setattr(state.shutil, "copytree", die_half_way)
    # Restricted to the one id whose destination ALREADY holds state: that is the content
    # the swap has to protect, and copying it must be what gets interrupted.
    with pytest.raises(KeyboardInterrupt):
        _copy(tmp_path, source, inst, monkeypatch, only=[_STORE_ID])

    dest = _local(inst, _STORE_ID)
    assert (dest / "000001.log").read_text() == "instance-own-state"
    assert not (dest / "000003.ldb").exists()
    # And no staging leftovers next to it either.
    assert sorted(p.name for p in dest.parent.iterdir()) == sorted([_STORE_ID, _CURATOR_ID])


def test_copy_state_never_writes_into_the_source_profile(tmp_path, monkeypatch):
    """The SOURCE profile is READ-ONLY here — the same invariant the launcher holds.

    That profile is the owner's real browser. Asserted as a full before/after snapshot of
    (relpath, mtime, size), every entry, files and directories both.

    Redden: stage inside the source, write a marker there, or `mkdir` a missing state dir
    in it.
    """
    source = _fake_source_profile(tmp_path)
    inst = _instance_with_state(tmp_path)
    before = _snapshot(source)
    _copy(tmp_path, source, inst, monkeypatch)
    assert _snapshot(source) == before


def test_cli_copy_state_runs_end_to_end_and_states_the_terms(tmp_path, monkeypatch, capsys):
    """The CLI wiring, plus the three things the operator must be told on every run.

    Redden: drop the copy/sync/vault-location paragraph — an operator would then take this
    for a live sync and assume a logout propagates.
    """
    source = _fake_source_profile(tmp_path)
    inst = _instance_with_state(tmp_path)
    monkeypatch.setattr(state, "running_brave_processes", lambda *a, **kw: [])

    assert cli.main([
        "copy-state", "--instance-dir", str(inst), "--from", str(source),
        "--only", f"{_STORE_ID}, {_STORE_ID_2}",
    ]) == 0

    printed = capsys.readouterr().out
    assert _STORE_ID in printed and _STORE_ID_2 in printed
    assert "ONE-TIME COPY, not a sync" in printed
    assert "master password" in printed          # what it does NOT promise
    assert "one more copy on this disk" in printed  # where the vault now lives
    assert (_local(inst, _STORE_ID) / "000003.ldb").is_file()


def test_cli_copy_state_exits_non_zero_while_brave_runs(tmp_path, monkeypatch):
    # A refusal is an expected outcome, not a crash: it must be a non-zero exit with the
    # message, never a traceback. Redden: let StateCopyRefused propagate out of the CLI.
    source = _fake_source_profile(tmp_path)
    inst = _instance_with_state(tmp_path)
    monkeypatch.setattr(
        state, "running_brave_processes",
        lambda *a, **kw: [
            "93062 /Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
        ],
    )
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["copy-state", "--instance-dir", str(inst), "--from", str(source)])
    assert "Brave is running" in str(excinfo.value)


def test_cli_copy_state_has_no_force_option(tmp_path):
    # The guard protects live databases and must have no bypass. Redden: add --force.
    with pytest.raises(SystemExit):
        cli.main(["copy-state", "--instance-dir", str(tmp_path), "--force"])


# --------------------------------------------------------------------------- #
# The running-browser guard: every branch of the pgrep exit code
# --------------------------------------------------------------------------- #
class _FakeCompletedProcess:
    """Just enough of `subprocess.CompletedProcess` for the guard to read."""

    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_pgrep(monkeypatch, returncode, stdout="", stderr=""):
    monkeypatch.setattr(
        state.subprocess,
        "run",
        lambda *a, **kw: _FakeCompletedProcess(returncode, stdout, stderr),
    )


def test_pgrep_exit_1_is_the_only_all_clear(monkeypatch):
    """Exit 1 with no output is pgrep POSITIVELY answering "nothing matched" — the one
    outcome that may authorise the copy.

    Redden: treat any empty stdout as an all-clear (which is what the old code did) and
    the three tests below stop reddening.
    """
    _fake_pgrep(monkeypatch, 1)
    assert state.running_brave_processes() == []


@pytest.mark.parametrize("code", [2, 3])
def test_pgrep_error_exit_refuses_instead_of_reading_empty_stdout(monkeypatch, code):
    """THE fail-open that mattered: `pgrep` exits 2 on a bad pattern and 3 on a fatal
    error, BOTH with empty stdout.

    The old guard never looked at `returncode` at all — it parsed stdout, found nothing and
    reported "Brave is not running", which lets the copy run onto live LevelDBs. Reproduced
    on the real machine: `pgrep -fl "["` exits 2 and prints nothing.

    Redden: drop the `returncode not in (0, 1)` check — the call returns `[]` and this
    test fails.
    """
    _fake_pgrep(monkeypatch, code, stderr="pgrep: bad pattern")
    with pytest.raises(state.StateCopyRefused) as excinfo:
        state.running_brave_processes()
    assert f"exited {code}" in str(excinfo.value)


def test_pgrep_exit_0_with_no_output_refuses(monkeypatch):
    """Exit 0 means "matched". Printing nothing after that contradicts it, and a process
    check that contradicts itself is not an answer.

    Redden: return `[]` when stdout is empty regardless of the exit code.
    """
    _fake_pgrep(monkeypatch, 0, stdout="\n  \n")
    with pytest.raises(state.StateCopyRefused, match="printed nothing"):
        state.running_brave_processes()


def test_pgrep_unparseable_line_counts_as_a_running_process(monkeypatch):
    """A matched line that cannot be parsed is PROOF of a live process, never a nothing.

    The old code silently DISCARDED any line without a space (`if " " in line`), so a
    single unusual line could empty the whole result and open the guard.

    Redden: filter unparseable lines out of `running_brave_processes` or out of
    `browsers_to_quit` — the copy would then proceed with a browser alive.
    """
    _fake_pgrep(monkeypatch, 0, stdout="not-a-pgrep-line\n")
    running = state.running_brave_processes()
    assert running == ["not-a-pgrep-line"]
    labels = state.browsers_to_quit(running)
    assert len(labels) == 1
    assert "UNPARSEABLE" in labels[0]


def test_pgrep_helpers_and_shims_are_labelled_apart_from_browsers():
    """A crashpad handler or a PWA shim is not "the MAIN Brave profile".

    Six of the 84 matches on the owner's machine are type-less non-browsers
    (`chrome_crashpad_handler`, `app_mode_loader`). Calling them "the MAIN Brave profile"
    told the operator to quit a browser that was not running, with no pid and deliberately
    no --force to get past it.

    Redden: label every type-less process "the MAIN Brave profile" again.
    """
    labels = state.browsers_to_quit([
        "93062 /Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "93222 /Applications/Brave Browser.app/…/Helpers/chrome_crashpad_handler "
        "--monitor-self-annotation=ptype=crashpad-handler",
        "93223 /Users/x/Brave Browser Apps.localized/Home Assistant.app/Contents/MacOS/"
        "app_mode_loader --launched-by-chrome-process-id=93062",
    ])
    joined = "\n".join(labels)
    assert "pid 93062" in joined and "the MAIN Brave profile" in joined
    for pid, name in (("93222", "chrome_crashpad_handler"), ("93223", "app_mode_loader")):
        assert f"pid {pid}  {name}" in joined
        # …and the shims are NOT sold as the main browser.
        assert f"pid {pid}  {name} — the MAIN" not in joined
    assert "helper/PWA shim" in joined


def _write_launcher(inst: Path, bundle: Path, *, brave_binary=None, sync_from=None):
    """Write a REAL generated launcher into an instance dir (not a hand-made stand-in).

    `core.render_launcher_script` is what `generate` emits, so parsing it in the tests is
    parsing the thing the parser has to handle.
    """
    launcher = inst / "curator.app" / "Contents" / "MacOS" / "run"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(
        core.render_launcher_script(
            brave_binary or core.DEFAULT_BRAVE_BINARY,
            inst / "profile",
            bundle,
            sync_extensions_from=sync_from,
        )
    )
    return launcher


def test_pgrep_searches_for_the_instances_own_brave_binary(tmp_path, monkeypatch):
    """`generate --brave-binary` means an instance may exec a Brave the guard never greps
    for — and an invisible browser is a live database copied from under it.

    Redden: grep only for `core.DEFAULT_BRAVE_BINARY`'s name and the second pattern below
    is never searched for.
    """
    other = str(tmp_path / "Brave Nightly.app" / "Contents" / "MacOS" / "Brave Nightly")
    inst = _instance_with_state(tmp_path)
    _write_launcher(inst, tmp_path / "dist", brave_binary=other)

    assert state.instance_brave_binaries(inst) == [other]

    patterns = []

    def record(argv, **_kwargs):
        patterns.append(argv[-1])
        return _FakeCompletedProcess(1)

    monkeypatch.setattr(state.subprocess, "run", record)
    source = _fake_source_profile(tmp_path)
    state.copy_extension_state(source_default_dir=source, instance_dir=inst)
    assert patterns == ["Brave Browser", "Brave Nightly"]


# --------------------------------------------------------------------------- #
# The identity guard's two layers
# --------------------------------------------------------------------------- #
def test_chromium_unpacked_id_matches_the_real_deployment():
    """The id derivation must be the REAL one before anything is excluded by it.

    Chromium hashes the absolute load path with SHA-256, takes 16 bytes and maps each
    nibble 0-15 onto 'a'-'p'. Pinned against the id the owner's browsers actually show for
    `/Users/vvzvlad/Data/Projects/arcextension/dist`, because a derivation that is merely
    plausible would exclude the WRONG id — i.e. silently do nothing.

    Redden: change the byte count, the alphabet or the hash.
    """
    assert state.chromium_unpacked_extension_id(
        "/Users/vvzvlad/Data/Projects/arcextension/dist"
    ) == "enhmndaehfanaeinicoffekhbepjhkmf"


def test_copy_state_excludes_the_bundle_this_instance_loads_unpacked(
    tmp_path, monkeypatch
):
    """POSITIVE layer: the curator's id is derived from THIS instance's launcher and
    excluded by name — it does not depend on the source profile lacking a directory.

    The source below is rigged so the negative layer would PASS the id: it has a full
    `Extensions/<id>/<version>/manifest.json`. Only the derived id keeps it out.

    Redden: drop `exclude_ids` from `eligible_extension_ids` and the instance's own
    identity marker below is overwritten by the main browser's storage.
    """
    bundle = tmp_path / "dist"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(json.dumps({"name": "curator"}))
    curator_id = state.chromium_unpacked_extension_id(str(bundle))

    source = _fake_source_profile(tmp_path)
    _install_unpacked(source, curator_id)  # the negative layer alone would allow it
    d = source / state.LOCAL_SETTINGS_DIRNAME / curator_id
    d.mkdir(parents=True)
    (d / "000003.ldb").write_text("MAIN browser identity")

    inst = _instance_with_state(tmp_path)
    own = inst / "profile" / "Default" / state.LOCAL_SETTINGS_DIRNAME / curator_id
    own.mkdir(parents=True)
    (own / "000001.log").write_text("instance-install-uuid")
    _write_launcher(inst, bundle)

    assert state.instance_unpacked_extension_ids(inst) == [curator_id]
    copied = _copy(tmp_path, source, inst, monkeypatch)
    assert curator_id not in [c.extension_id for c in copied]
    assert (own / "000001.log").read_text() == "instance-install-uuid"
    assert not (own / "000003.ldb").exists()

    # Named explicitly it is refused too, and by the layer that actually knows why.
    with pytest.raises(state.StateCopyRefused, match="THIS INSTANCE loads unpacked"):
        _copy(tmp_path, source, inst, monkeypatch, only=[curator_id])


@pytest.mark.parametrize("binaries", [[], [""], ["", ""]])
def test_running_brave_processes_refuses_an_empty_pattern_set(monkeypatch, binaries):
    """"Fails closed" must not depend on the caller passing a binary.

    With no pattern the loop never runs, `pgrep` is never called and EVERY refusal branch
    in `_pgrep` is skipped — the function returned `[]`, which its callers read as "no
    browser is running", having checked nothing. No CLI path reaches it today (the default
    binary is always in the set), but the docstring promises this unconditionally.

    Redden: drop the `if not patterns` guard — `pgrep` is never invoked and the call
    answers "all clear".
    """
    def never(*_a, **_kw):
        raise AssertionError("pgrep must not be reached — there is nothing to search for")

    monkeypatch.setattr(state.subprocess, "run", never)
    with pytest.raises(state.StateCopyRefused, match="no binary name to search for"):
        state.running_brave_processes(binaries)


def test_exclusion_reason_tells_a_component_extension_from_an_install_identity(tmp_path):
    """Six of the seven excluded ids on the real profile are Chrome COMPONENT extensions
    (Web Store, Docs Offline, …) — excluded correctly and described wrongly: every one was
    told its storage "is per-install IDENTITY", which is true only of the id THIS instance
    loads unpacked.

    Layer (b) is what tells them apart, so this instance gets a real launcher: the derived
    id keeps the identity wording, and the component-shaped id (state, no `Extensions/<id>`,
    NOT the unpacked id) is described as what it is.

    Redden: collapse the two branches of `_exclusion_reason` back into one — the component
    id is accused of being this install's identity again.
    """
    bundle = tmp_path / "dist"
    bundle.mkdir()
    unpacked_id = state.chromium_unpacked_extension_id(str(bundle))

    source = _fake_source_profile(tmp_path)  # `_CURATOR_ID` plays the COMPONENT here
    d = source / state.LOCAL_SETTINGS_DIRNAME / unpacked_id
    d.mkdir(parents=True)
    (d / "000003.ldb").write_text("MAIN browser identity")

    inst = _instance_with_state(tmp_path)
    _write_launcher(inst, bundle)

    plan = state.plan_extension_state_copy(source_default_dir=source, instance_dir=inst)
    reasons = dict(plan.excluded)
    assert plan.unpacked_layer_note is None  # layer (b) really did run

    # The id this instance loads unpacked: the identity wording, kept verbatim.
    assert "its storage is this install's identity" in reasons[unpacked_id]
    # The component-shaped id: called a component, and explicitly NOT an identity.
    assert "COMPONENT extension" in reasons[_CURATOR_ID]
    assert "Not this instance's identity" in reasons[_CURATOR_ID]
    assert "storage is per-install IDENTITY" not in reasons[_CURATOR_ID]
    # Both are still EXCLUDED — the wording changed, the guard did not.
    assert {unpacked_id, _CURATOR_ID}.isdisjoint(i.extension_id for i in plan.selected)


@pytest.mark.parametrize("how", ["no launcher", "no --load-extension"])
def test_copy_state_says_when_identity_layer_b_could_not_be_computed(
    tmp_path, monkeypatch, capsys, how
):
    """A launcher that is missing or carries no `--load-extension` makes layer (b) yield no
    ids — it excludes nothing and the run proceeds on layer (a) alone.

    That is the right degradation and the wrong silence: the output was identical to a
    healthy run, so a weaker guard had to be inferred from the phrasing of a reason.

    Redden: drop `unpacked_layer_note` (or the CLI's note) — the degraded run prints
    exactly what a fully guarded one prints.
    """
    source = _fake_source_profile(tmp_path)
    inst = _instance_with_state(tmp_path)
    if how == "no --load-extension":
        launcher = inst / "curator.app" / "Contents" / "MacOS" / "run"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\nexec '/Applications/Brave Browser.app/x' \\\n")

    plan = state.plan_extension_state_copy(source_default_dir=source, instance_dir=inst)
    assert plan.unpacked_layer_note is not None
    monkeypatch.setattr(state, "running_brave_processes", lambda *a, **kw: [])

    assert cli.main([
        "copy-state", "--instance-dir", str(inst), "--from", str(source), "--dry-run",
    ]) == 0
    printed = capsys.readouterr().out
    assert "layer (b) UNAVAILABLE" in printed
    assert "Only layer (a) is in force" in printed
    if how == "no launcher":
        assert "no launcher script under" in printed
    else:
        assert "carries no --load-extension flag" in printed

    # The real run says it too — a degraded guard is not a dry-run-only concern.
    capsys.readouterr()
    assert cli.main([
        "copy-state", "--instance-dir", str(inst), "--from", str(source),
    ]) == 0
    assert "layer (b) UNAVAILABLE" in capsys.readouterr().out


def test_copy_state_unpacked_id_is_read_through_the_sync_launcher_form(tmp_path):
    """With `--sync-extensions` on, the launcher reads `--load-extension="$EXTS"` and the
    path is in the `EXTS=` assignment above it. Both forms must be understood.

    Redden: only handle the literal `--load-extension=<path>` form and the id comes back
    empty for every instance generated with the DEFAULT settings (sync is on by default).
    """
    bundle = tmp_path / "dist"
    bundle.mkdir()
    inst = tmp_path / "inst"
    _write_launcher(inst, bundle, sync_from=tmp_path / "main" / "Extensions")
    assert state.instance_unpacked_load_paths(inst) == [str(bundle)]
    assert state.instance_unpacked_extension_ids(inst) == [
        state.chromium_unpacked_extension_id(str(bundle))
    ]


def test_copy_state_requires_a_real_manifest_not_a_bare_extensions_dir(
    tmp_path, monkeypatch
):
    """NEGATIVE layer, tightened: an EMPTY or half-removed `Extensions/<id>` is not an
    installed extension.

    The launcher only loads a version dir once it finds `<version>/manifest.json`, so that
    is what "installed" means on both sides. `mkdir Extensions/<curator id>` used to be
    enough to defeat the only guard standing between the curator's storage — this
    install's `install_uuid` and enrollment secret — and the copy.

    Redden: go back to `(extensions / entry.name).is_dir()`.
    """
    source = _fake_source_profile(tmp_path)
    inst = _instance_with_state(tmp_path)
    # Half-removed: the id dir and a version dir exist, but no manifest inside.
    (source / state.EXTENSIONS_DIRNAME / _CURATOR_ID / "9.9.9_0").mkdir(parents=True)

    copied = _copy(tmp_path, source, inst, monkeypatch)
    assert _CURATOR_ID not in [c.extension_id for c in copied]
    assert (_local(inst, _CURATOR_ID) / "000001.log").read_text() == "instance-install-uuid"


def test_copy_state_does_not_follow_a_symlinked_extensions_dir(tmp_path, monkeypatch):
    """`Path.is_dir()` follows symlinks, so a symlinked `Extensions/<id>` pointing at any
    real extension satisfied the guard.

    Redden: drop the `is_symlink()` checks in `is_installed_unpacked`.
    """
    source = _fake_source_profile(tmp_path)
    inst = _instance_with_state(tmp_path)
    (source / state.EXTENSIONS_DIRNAME / _CURATOR_ID).symlink_to(
        source / state.EXTENSIONS_DIRNAME / _STORE_ID, target_is_directory=True
    )

    copied = _copy(tmp_path, source, inst, monkeypatch)
    assert _CURATOR_ID not in [c.extension_id for c in copied]
    assert (_local(inst, _CURATOR_ID) / "000001.log").read_text() == "instance-install-uuid"


# --------------------------------------------------------------------------- #
# Failing part-way, disk space, stale staging
# --------------------------------------------------------------------------- #
def test_copy_state_names_the_id_it_died_on_and_the_ids_already_committed(
    tmp_path, monkeypatch, capsys
):
    """The loop commits PER ID, so a failure half-way leaves a half-migrated profile — and
    the previous state of the ids already done is gone.

    An `OSError` from `shutil` is not `StateCopyRefused`, so the CLI used to hand the
    operator a raw traceback and no idea which ids had already moved.

    Redden: let the OSError propagate untouched, or drop the stderr report.
    """
    source = _fake_source_profile(tmp_path)
    inst = _instance_with_state(tmp_path)

    calls = []
    real_copytree = shutil.copytree

    def fail_on_the_second(src, dst, *args, **kwargs):
        calls.append(src)
        if len(calls) > 1:
            raise OSError(28, "No space left on device")
        return real_copytree(src, dst, *args, **kwargs)

    monkeypatch.setattr(state.shutil, "copytree", fail_on_the_second)
    with pytest.raises(state.StateCopyRefused) as excinfo:
        _copy(tmp_path, source, inst, monkeypatch)

    first, second = sorted([_STORE_ID, _STORE_ID_2])
    assert second in str(excinfo.value)          # the id it died on
    assert first in str(excinfo.value)           # …and what is already committed
    assert first in capsys.readouterr().err      # printed before it propagated, too


def test_copy_state_refuses_when_the_destination_has_no_room(tmp_path, monkeypatch):
    """~130 MB moving onto a full disk must be refused UP FRONT, not discovered per id.

    Redden: drop the disk check — the copy then dies part-way through with an ENOSPC and a
    half-migrated profile.
    """
    source = _fake_source_profile(tmp_path)
    inst = _instance_with_state(tmp_path)

    class _FullDisk:
        total, used, free = 100, 100, 0

    monkeypatch.setattr(state.shutil, "disk_usage", lambda _p: _FullDisk())
    with pytest.raises(state.StateCopyRefused, match="free"):
        _copy(tmp_path, source, inst, monkeypatch)
    # Nothing moved: the instance still holds only what it had.
    assert (_local(inst, _STORE_ID) / "000001.log").read_text() == "instance-own-state"


def test_copy_state_sweeps_stale_staging_dirs_left_by_a_killed_run(tmp_path, monkeypatch):
    """A SIGKILL between staging and swap leaves `.rebuild-XXXX/new/` holding a PARTIAL
    copy of the vault next to the real one, and nothing ever removed it.

    Redden: drop the sweep and the leftover below survives the run.
    """
    source = _fake_source_profile(tmp_path)
    inst = _instance_with_state(tmp_path)
    settings = inst / "profile" / "Default" / state.LOCAL_SETTINGS_DIRNAME
    stale = settings / ".rebuild-deadbeef" / "new"
    stale.mkdir(parents=True)
    (stale / "000003.ldb").write_text("half a vault")

    plan = state.plan_extension_state_copy(
        source_default_dir=source, instance_dir=inst
    )
    assert plan.stale_staging == (settings / ".rebuild-deadbeef",)

    _copy(tmp_path, source, inst, monkeypatch)
    assert not (settings / ".rebuild-deadbeef").exists()
    # The real destinations are untouched by the sweep.
    assert (_local(inst, _STORE_ID) / "000003.ldb").is_file()


# --------------------------------------------------------------------------- #
# CLI: path refusals, the empty --only, and --dry-run
# --------------------------------------------------------------------------- #
def test_cli_copy_state_refuses_a_source_that_is_not_a_brave_profile(tmp_path):
    """`FROM=` pointing anywhere else is a clean refusal naming the dir to pass.

    Redden: drop the `Extensions/` check on the source — the run then reports "nothing
    eligible" and exits 0, which reads as "there was nothing to copy".
    """
    inst = _instance_with_state(tmp_path)
    not_a_profile = tmp_path / "somewhere"
    not_a_profile.mkdir()
    with pytest.raises(SystemExit) as excinfo:
        cli.main([
            "copy-state", "--instance-dir", str(inst), "--from", str(not_a_profile),
        ])
    assert "is not a Brave profile" in str(excinfo.value)


def test_cli_copy_state_refuses_an_instance_dir_without_a_profile(tmp_path):
    """`INSTANCE_DIR` must be an instance ROOT, not the `.app` and not the profile itself.

    Redden: drop the `profile/` check and the copy creates `<whatever>/profile/Default/…`,
    i.e. a vault copy in a directory the operator merely mistyped.
    """
    source = _fake_source_profile(tmp_path)
    bare = tmp_path / "not-an-instance"
    bare.mkdir()
    with pytest.raises(SystemExit) as excinfo:
        cli.main([
            "copy-state", "--instance-dir", str(bare), "--from", str(source),
        ])
    assert "INSTANCE_DIR must be an instance root" in str(excinfo.value)
    assert not (bare / "profile").exists()


@pytest.mark.parametrize("value", ["", " ", ",", " , "])
def test_cli_copy_state_refuses_an_empty_only(tmp_path, value):
    """An EMPTY `--only` is MISSING configuration, never "all 21 of them" (AGENTS.md).

    `make instance-state ONLY=` must reach this, which is why the Makefile passes `--only`
    whenever the variable is DEFINED instead of whenever it is non-empty.

    Redden: treat an empty id list as `None` and an unset shell variable silently copies
    every eligible extension — including the crypto wallet.
    """
    with pytest.raises(SystemExit) as excinfo:
        cli.main([
            "copy-state", "--instance-dir", str(tmp_path), "--only", value,
        ])
    assert "empty id list" in str(excinfo.value)


def test_makefile_passes_only_whenever_it_is_defined(tmp_path):
    """`$(if $(ONLY),…)` turned `ONLY=` into "copy all 21", defeating the CLI's own guard.

    Asserted against the Makefile text rather than by running make: the bug is exactly the
    `$(if $(ONLY),…)` shape, and `$(origin ONLY)` is what tells "defined but empty" from
    "not given at all".

    Redden: go back to `$(if $(ONLY),--only "$(ONLY)",)`.
    """
    makefile = (Path(__file__).resolve().parents[1] / "Makefile").read_text()
    assert '$(if $(ONLY),--only' not in makefile
    assert '$(filter-out undefined,$(origin ONLY)),--only "$(ONLY)"' in makefile
    # INSTANCE_DIR is validated in the target, not passed through empty.
    assert 'test -n "$(INSTANCE_DIR)"' in makefile


def test_cli_copy_state_dry_run_writes_nothing_and_explains_the_exclusions(
    tmp_path, monkeypatch, capsys
):
    """`--dry-run` answers "which ids, how big, what is skipped and why" BEFORE 130 MB of
    encrypted vault moves — the warnings used to print only after it already had.

    It also must not refuse on a running browser: that is the moment the operator is
    deciding whether to quit it. Redden: make the dry run take the copy path, or drop the
    exclusion reasons.
    """
    source = _fake_source_profile(tmp_path)
    inst = _instance_with_state(tmp_path)
    before = _snapshot(inst)
    monkeypatch.setattr(
        state, "running_brave_processes",
        lambda *a, **kw: [
            "93062 /Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
        ],
    )

    assert cli.main([
        "copy-state", "--instance-dir", str(inst), "--from", str(source), "--dry-run",
    ]) == 0

    assert _snapshot(inst) == before  # not one byte written, Brave running or not
    printed = capsys.readouterr().out
    assert "DRY RUN" in printed
    assert _STORE_ID in printed and _STORE_ID_2 in printed
    # …and the excluded id is listed WITH its reason. This instance has no launcher, so
    # layer (b) is unavailable and the reason says the id could not be classified rather
    # than calling a component extension an install identity (see the two tests below).
    assert _CURATOR_ID in printed
    assert "could NOT be determined" in printed
    assert "layer (b) UNAVAILABLE" in printed
    # Sizes, what is destroyed and the disk headroom, all before anything is committed to.
    assert "total:" in printed and "disk :" in printed and "DELETES:" in printed
    # A running browser is REPORTED, not raised.
    assert "would REFUSE" in printed and "pid 93062" in printed


def test_plan_sizes_what_is_destroyed_not_only_what_arrives(tmp_path):
    """The plan sized the SOURCE only, so the run read as "21 ids arrive" when it is also
    "42 MB of this instance's own state is deleted" — MetaMask's wallet among them.

    `_instance_with_state` gives the destination a `_STORE_ID` directory and no
    `_STORE_ID_2` one, which is the real shape: some ids have state to lose, some do not.
    Both must be visible per row, and the totals must reflect both.

    Redden: drop `bytes_replaced` from `PlannedCopy` (or size only `source`) — every row
    reports "replaced nothing" while the destination still loses its state.
    """
    source = _fake_source_profile(tmp_path)
    inst = _instance_with_state(tmp_path)
    # Make the loss measurable and asymmetric: the destination's own _STORE_ID state is
    # bigger than what the source will put in its place.
    (_local(inst, _STORE_ID) / "000002.ldb").write_text("x" * 4096)
    doomed = state._tree_bytes(_local(inst, _STORE_ID))

    plan = state.plan_extension_state_copy(source_default_dir=source, instance_dir=inst)
    rows = {item.extension_id: item for item in plan.selected}

    assert rows[_STORE_ID].bytes_replaced == doomed > 0
    assert rows[_STORE_ID_2].bytes_replaced == 0  # nothing there to lose
    assert plan.total_replaced_bytes == doomed
    assert [i.extension_id for i in plan.selected if i.bytes_replaced] == [_STORE_ID]
    # The source side is untouched by the new measurement.
    assert plan.total_bytes == sum(i.bytes_to_copy for i in plan.selected) > 0


def test_cli_copy_state_dry_run_names_the_state_it_destroys(tmp_path, monkeypatch, capsys):
    """The operator must read the destruction off the plan, not infer it from the word
    "overwrites" in a prose paragraph.

    `_STORE_ID` is Bitwarden's real id, so its row is also the "a vault is being deleted"
    case: that one gets a line of its own, because "the instance gets my main vault" and
    "the instance's own vault is deleted" are the same command and different decisions.

    Redden: print the rows without the last column, or drop the DELETES total.
    """
    source = _fake_source_profile(tmp_path)
    inst = _instance_with_state(tmp_path)
    (_local(inst, _STORE_ID) / "000002.ldb").write_text("x" * 4096)
    monkeypatch.setattr(state, "running_brave_processes", lambda *a, **kw: [])

    assert cli.main([
        "copy-state", "--instance-dir", str(inst), "--from", str(source), "--dry-run",
    ]) == 0
    printed = capsys.readouterr().out
    rows = {line.split()[0]: line for line in printed.splitlines() if line.startswith("  ")}

    assert "DELETES" in rows[_STORE_ID]  # a destination that has something to lose
    assert "replaced nothing" in rows[_STORE_ID_2]  # and one that does not
    # The vault case is impossible to miss, and it names what it is.
    assert "THIS INSTANCE'S OWN Bitwarden" in printed
    assert "ENCRYPTED PASSWORD VAULT" in printed and "no undo" in printed
    # The total names the destruction as deletion, and counts the vault among it.
    assert "DELETES:" in printed and "irrecoverably replaced" in printed
    assert "1 of them a wallet/vault" in printed
    # And the terms paragraph states the same thing rather than only "overwrites".
    assert "REPLACED WHOLE" in printed and "is DELETED, not merged" in printed


def test_cli_copy_state_real_run_reports_the_state_it_deleted(tmp_path, monkeypatch, capsys):
    """The same accounting after the fact, in the past tense: what this instance no longer
    has is as much a result of the run as what it gained.

    Redden: report only `bytes_copied` in the real run's summary.
    """
    source = _fake_source_profile(tmp_path)
    inst = _instance_with_state(tmp_path)
    (_local(inst, _STORE_ID) / "000002.ldb").write_text("x" * 4096)
    monkeypatch.setattr(state, "running_brave_processes", lambda *a, **kw: [])

    assert cli.main([
        "copy-state", "--instance-dir", str(inst), "--from", str(source),
    ]) == 0
    printed = capsys.readouterr().out
    assert "DELETED" in printed and "DELETED:" in printed
    assert "WAS DELETED" in printed  # the vault line, past tense now
    assert "DELETES" not in printed  # a finished run does not speak in the future


def test_cli_copy_state_states_the_appid_and_names_metamask(tmp_path, monkeypatch, capsys):
    """Two corrections to what the operator is told, both about what is NOT local.

    The text used to state as FACT that "logging out here does not log the others out".
    That holds for the local LevelDBs; it does not follow for the SERVER, because the
    copied Bitwarden storage carries its `appId` — the device identifier a refresh token is
    bound to — so both profiles become one device server-side. That half was never
    verified, so it is qualified rather than asserted. And only Bitwarden was named while
    MetaMask is eligible too, its storage holding an encrypted SEED vault.

    Redden: re-assert the logout claim, or drop MetaMask from the paragraph.
    """
    source = _fake_source_profile(tmp_path)
    inst = _instance_with_state(tmp_path)
    monkeypatch.setattr(state, "running_brave_processes", lambda *a, **kw: [])

    assert cli.main([
        "copy-state", "--instance-dir", str(inst), "--from", str(source),
    ]) == 0
    printed = capsys.readouterr().out
    assert "NOT VERIFIED" in printed and "appId" in printed
    assert "logging out here does not log the others out" not in printed
    assert "MetaMask" in printed
    assert "nkbihfbeogaeaoehlefnkodbefgpgknn" in printed
    assert "SEED VAULT" in printed
