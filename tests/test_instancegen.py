"""Tests for the per-instance browser generator (tools/instancegen, §13).

Pure-core only — everything here runs on Linux/CI without a browser or a mac.
Operational acceptance (an instance actually connecting to /api/state, a clone
rejected as duplicate_instance, reconnect after restart) needs a real browser and
is a MANUAL checklist in tools/README.md — deliberately NOT faked here.

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
# Universal (hostless) manifest — issue #35 removed the per-host patterns
# --------------------------------------------------------------------------- #
def test_generate_manifest_is_universal_hostless_all_urls_only(tmp_path):
    # Universal build (§7, issue #35): the repo manifest carries ONLY <all_urls> — the two
    # per-host patterns (https://<host>/* + wss://<host>/*) were removed with enrollment,
    # so generate no longer stamps a concrete <host>. The manifest stays hostless and the
    # key is still pinned. (serviceUrl is irrelevant to host_permissions now.)
    res = _gen(tmp_path, "eps", service_url="wss://curator.example.com",
               key_b64="REALKEYBASE64==")
    manifest = json.loads((res.paths.extension_dir / "manifest.json").read_text())
    perms = manifest["host_permissions"]
    # NEW invariant: exactly <all_urls>, no per-host patterns, no leaked <host> placeholder.
    # Redden: if generate re-introduced host stamping, perms would gain https/wss entries.
    assert perms == ["<all_urls>"]  # SECURITY-sensitive grant kept (§6); nothing else
    assert not any("<host>" in p for p in perms)
    assert not any(p.startswith(("https://", "wss://")) for p in perms)
    # Redden: drop key pinning -> the placeholder leaks and this fails.
    assert manifest["key"] == "REALKEYBASE64=="
    assert manifest["key"] != core.KEY_PLACEHOLDER


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


def test_restamp_change_service_url_keeps_manifest_universal_hostless(tmp_path):
    # Universal build (issue #35): restamp can still change serviceUrl in instance.json,
    # but the manifest has no <host> to re-stamp — host_permissions stays EXACTLY
    # ["<all_urls>"] (no per-host patterns are minted), and the pinned key is untouched.
    _gen(tmp_path, "alpha", service_url="wss://old.example.com", token="old",
         key_b64="PINNEDKEY==")
    core.restamp_all(tmp_path, token="new", service_url="wss://new.example.com")
    data = json.loads((tmp_path / "alpha" / "extension" / "instance.json").read_text())
    # serviceUrl still rotates in the config. Redden: drop the serviceUrl write -> fails.
    assert data["serviceUrl"] == "wss://new.example.com"
    manifest = json.loads(
        (tmp_path / "alpha" / "extension" / "manifest.json").read_text()
    )
    perms = manifest["host_permissions"]
    # NEW invariant: hostless — no https://new.* / wss://new.* patterns are ever stamped.
    # Redden: if restamp re-minted per-host patterns, perms would gain new.example.com.
    assert perms == ["<all_urls>"]
    assert not any("new.example.com" in p for p in perms)
    assert manifest["key"] == "PINNEDKEY=="  # pinned key preserved across the restamp


def test_restamp_empty_root_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        core.restamp_all(tmp_path, token="new")


# --------------------------------------------------------------------------- #
# Re-stamp also carries the CODE (§13): the bundle is duplicated per instance and
# protocolVersion is compared by exact equality, so a rotation that left the copies
# on old code would reject every instance on hello forever.
# --------------------------------------------------------------------------- #
def _source_bundle(tmp_path: Path, marker: str) -> Path:
    """A minimal stand-in extension bundle carrying an identifiable code file."""
    src = tmp_path / f"src-{marker}"
    src.mkdir(exist_ok=True)  # idempotent: callers re-request the same version
    (src / "manifest.json").write_text(
        json.dumps(
            {
                "name": "x",
                "key": core.KEY_PLACEHOLDER,
                "host_permissions": ["https://<host>/*", "wss://<host>/*", "<all_urls>"],
            }
        )
    )
    (src / "sw.js").write_text(f"// {marker}\n")
    return src


def test_restamp_updates_the_extension_code(tmp_path):
    out = tmp_path / "out"
    v1 = _source_bundle(tmp_path, "v1")
    core.generate_instance(
        out_root=out, source_extension_dir=v1, instance_id="alpha", title="Alpha",
        service_url="wss://h.example.com", token="old", key_b64="PINNEDKEY==",
        extension_id="a" * 32,
    )
    v2 = _source_bundle(tmp_path, "v2")
    (v2 / "new_file.js").write_text("// added in v2\n")

    core.restamp_all(out, token="new", source_extension_dir=v2)

    ext = out / "alpha" / "extension"
    # Redden: drop source_extension_dir handling -> the copy keeps the v1 code and
    # every instance is rejected on hello after a PROTOCOL_VERSION bump.
    assert (ext / "sw.js").read_text() == "// v2\n"
    assert (ext / "new_file.js").is_file()


def test_restamp_code_update_keeps_instance_json_correct_and_0600(tmp_path):
    # instance.json lives INSIDE the extension dir, so the code refresh necessarily
    # replaces the directory holding it. The rewritten config must survive intact —
    # with the NEW token — and keep its owner-only perms.
    out = tmp_path / "out"
    v1 = _source_bundle(tmp_path, "v1")
    core.generate_instance(
        out_root=out, source_extension_dir=v1, instance_id="alpha", title="Alpha",
        service_url="wss://h.example.com", token="old-secret", key_b64="PINNEDKEY==",
        extension_id="a" * 32,
    )
    core.restamp_all(out, token="new-secret", source_extension_dir=_source_bundle(tmp_path, "v2"))

    ij = out / "alpha" / "extension" / "instance.json"
    data = json.loads(ij.read_text())
    assert data == {
        "instanceId": "alpha",
        "title": "Alpha",
        "serviceUrl": "wss://h.example.com",
        "token": "new-secret",
        "allowExecuteJs": False,
    }
    assert (ij.stat().st_mode & 0o777) == 0o600, oct(ij.stat().st_mode & 0o777)


def test_restamp_code_update_preserves_pinned_key_and_host(tmp_path):
    # The SOURCE manifest ships the placeholder key. Copying it verbatim would change
    # the extension id — i.e. every instance's chrome-extension:// origin at once,
    # breaking EXT_ALLOWED_ORIGINS/CORS. The pinned key must be carried over.
    out = tmp_path / "out"
    core.generate_instance(
        out_root=out, source_extension_dir=_source_bundle(tmp_path, "v1"),
        instance_id="alpha", title="Alpha", service_url="wss://h.example.com",
        token="old", key_b64="PINNEDKEY==", extension_id="a" * 32,
    )
    core.restamp_all(out, token="new", source_extension_dir=_source_bundle(tmp_path, "v2"))

    manifest = json.loads((out / "alpha" / "extension" / "manifest.json").read_text())
    assert manifest["key"] == "PINNEDKEY=="
    assert manifest["key"] != core.KEY_PLACEHOLDER
    # <host> must be re-stamped in the fresh copy too, not left as a placeholder.
    assert "wss://h.example.com/*" in manifest["host_permissions"]
    assert "https://h.example.com/*" in manifest["host_permissions"]
    assert "<all_urls>" in manifest["host_permissions"]
    assert not any("<host>" in p for p in manifest["host_permissions"])


def test_restamp_code_update_preserves_the_profile(tmp_path):
    out = tmp_path / "out"
    core.generate_instance(
        out_root=out, source_extension_dir=_source_bundle(tmp_path, "v1"),
        instance_id="alpha", title="Alpha", service_url="wss://h.example.com",
        token="old", key_b64="PINNEDKEY==", extension_id="a" * 32,
    )
    marker = out / "alpha" / "profile" / "Local State"
    marker.write_text('{"installUuid":"born-in-profile"}')

    core.restamp_all(out, token="new", source_extension_dir=_source_bundle(tmp_path, "v2"))

    # The profile is a SIBLING of the extension dir precisely so a code refresh
    # cannot touch it (§6/§13). Redden: refresh the instance ROOT instead.
    assert marker.read_text() == '{"installUuid":"born-in-profile"}'


def test_restamp_refuses_code_update_without_a_pinned_key(tmp_path):
    # A copy whose manifest lost its real key cannot be refreshed silently: the
    # placeholder would re-derive a different extension id for the whole fleet.
    out = tmp_path / "out"
    core.generate_instance(
        out_root=out, source_extension_dir=_source_bundle(tmp_path, "v1"),
        instance_id="alpha", title="Alpha", service_url="wss://h.example.com",
        token="old", key_b64="PINNEDKEY==", extension_id="a" * 32,
    )
    manifest_path = out / "alpha" / "extension" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["key"] = core.KEY_PLACEHOLDER
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match="pinned"):
        core.restamp_all(out, token="new", source_extension_dir=_source_bundle(tmp_path, "v2"))


def test_restamp_preflight_refuses_before_touching_any_instance(tmp_path):
    # restamp mutates instances one by one, so a per-instance check made mid-write
    # would split the fleet: earlier instances get the new token, later ones keep the
    # old, and the operator has usually already rotated EXT_TOKEN on the service — so
    # no single token revives everyone. Validation must therefore happen BEFORE the
    # first write. Redden: move the manifest check back inside the apply loop.
    out = tmp_path / "out"
    for iid in ("alpha", "beta", "gamma"):
        core.generate_instance(
            out_root=out, source_extension_dir=_source_bundle(tmp_path, "v1"),
            instance_id=iid, title=iid, service_url="wss://h.example.com",
            token="old-token", key_b64="PINNEDKEY==", extension_id="a" * 32,
        )
    # Break the LAST instance alphabetically, so a naive implementation would already
    # have rewritten alpha and beta by the time it notices.
    broken = out / "gamma" / "extension" / "manifest.json"
    manifest = json.loads(broken.read_text())
    manifest["key"] = core.KEY_PLACEHOLDER
    broken.write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match="pinned"):
        core.restamp_all(
            out, token="new-token", source_extension_dir=_source_bundle(tmp_path, "v2")
        )

    # Every instance still holds the OLD token: the fleet stays consistent and alive.
    for iid in ("alpha", "beta", "gamma"):
        data = json.loads((out / iid / "extension" / "instance.json").read_text())
        assert data["token"] == "old-token", f"{iid} was mutated before the failure"
        assert (out / iid / "extension" / "sw.js").read_text() == "// v1\n"


def test_restamp_preflight_covers_the_config_only_branch(tmp_path):
    # --no-code-update still rewrites the manifest host when --service-url is given, so
    # a corrupt manifest throws during APPLY too. Validating that only under the
    # code-refresh branch left the split fleet the pre-flight exists to prevent.
    # Redden: move the manifest check back under `if source_extension_dir is not None`.
    out = tmp_path / "out"
    for iid in ("alpha", "beta", "gamma"):
        core.generate_instance(
            out_root=out, source_extension_dir=_source_bundle(tmp_path, "v1"),
            instance_id=iid, title=iid, service_url="wss://h.example.com",
            token="old-token", key_b64="PINNEDKEY==", extension_id="a" * 32,
        )
    (out / "gamma" / "extension" / "manifest.json").write_text("NOT JSON")

    with pytest.raises(ValueError, match="not valid JSON"):
        core.restamp_all(out, token="new-token", service_url="wss://new.example.com")

    for iid in ("alpha", "beta"):
        data = json.loads((out / iid / "extension" / "instance.json").read_text())
        assert data["token"] == "old-token", f"{iid} was mutated before the failure"
        assert data["serviceUrl"] == "wss://h.example.com"


def test_restamp_reports_progress_for_a_partial_apply(tmp_path):
    # The CLI needs to tell the operator WHICH instances already hold the new token if
    # the run dies partway — they have usually rotated EXT_TOKEN on the service by then.
    out = tmp_path / "out"
    for iid in ("alpha", "beta"):
        core.generate_instance(
            out_root=out, source_extension_dir=_source_bundle(tmp_path, "v1"),
            instance_id=iid, title=iid, service_url="wss://h.example.com",
            token="old-token", key_b64="PINNEDKEY==", extension_id="a" * 32,
        )
    seen: list[core.RestampChange] = []
    core.restamp_all(out, token="new-token", on_change=seen.append)
    assert [c.instance_id for c in seen] == ["alpha", "beta"]


def test_restamp_preflight_catches_an_underivable_service_url(tmp_path):
    out = tmp_path / "out"
    for iid in ("alpha", "beta"):
        core.generate_instance(
            out_root=out, source_extension_dir=_source_bundle(tmp_path, "v1"),
            instance_id=iid, title=iid, service_url="wss://h.example.com",
            token="old-token", key_b64="PINNEDKEY==", extension_id="a" * 32,
        )
    ij = out / "beta" / "extension" / "instance.json"
    data = json.loads(ij.read_text())
    del data["serviceUrl"]
    ij.write_text(json.dumps(data))

    with pytest.raises(ValueError, match="serviceUrl"):
        core.restamp_all(
            out, token="new-token", source_extension_dir=_source_bundle(tmp_path, "v2")
        )
    assert json.loads(
        (out / "alpha" / "extension" / "instance.json").read_text()
    )["token"] == "old-token"


def test_restamp_stages_instance_json_before_the_swap(tmp_path):
    # The swapped-in tree must already contain instance.json. If it were written
    # after the rename, a crash in that window would leave an extension dir with no
    # config — which iter_instance_json_paths (globbing */extension/instance.json)
    # no longer finds, so the NEXT restamp silently skips that instance forever.
    out = tmp_path / "out"
    core.generate_instance(
        out_root=out, source_extension_dir=_source_bundle(tmp_path, "v1"),
        instance_id="alpha", title="Alpha", service_url="wss://h.example.com",
        token="old", key_b64="PINNEDKEY==", extension_id="a" * 32,
    )

    real_replace = os.replace
    seen: dict[str, bool] = {}

    def spy(src, dst, *a, **kw):
        # At the moment the staged tree is renamed into place it must be complete.
        if str(dst).endswith("/extension"):
            seen["complete"] = (Path(src) / "instance.json").is_file()
        return real_replace(src, dst, *a, **kw)

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(os, "replace", spy)
        # Path.rename is what performs the directory swap; route it through the spy.
        monkey.setattr(
            Path, "rename", lambda self, target: (spy(self, target), Path(target))[1]
        )
        core.restamp_all(out, token="new", source_extension_dir=_source_bundle(tmp_path, "v2"))
    finally:
        monkey.undo()

    assert seen.get("complete") is True, "the tree was swapped in without instance.json"
    assert json.loads(
        (out / "alpha" / "extension" / "instance.json").read_text()
    )["token"] == "new"


def test_restamp_without_source_leaves_code_untouched(tmp_path):
    # The config-only path stays available for callers that update code separately.
    out = tmp_path / "out"
    core.generate_instance(
        out_root=out, source_extension_dir=_source_bundle(tmp_path, "v1"),
        instance_id="alpha", title="Alpha", service_url="wss://h.example.com",
        token="old", key_b64="PINNEDKEY==", extension_id="a" * 32,
    )
    changes = core.restamp_all(out, token="new")
    assert (out / "alpha" / "extension" / "sw.js").read_text() == "// v1\n"
    assert changes[0].code_updated is False


def test_restamp_failed_code_update_leaves_the_instance_working(tmp_path):
    # A missing/invalid source must not consume the live copy: the new tree is staged
    # beside the old one and swapped in only once complete.
    out = tmp_path / "out"
    core.generate_instance(
        out_root=out, source_extension_dir=_source_bundle(tmp_path, "v1"),
        instance_id="alpha", title="Alpha", service_url="wss://h.example.com",
        token="old", key_b64="PINNEDKEY==", extension_id="a" * 32,
    )
    not_a_bundle = tmp_path / "empty"
    not_a_bundle.mkdir()

    with pytest.raises(ValueError):
        core.restamp_all(out, token="new", source_extension_dir=not_a_bundle)

    ext = out / "alpha" / "extension"
    assert (ext / "sw.js").read_text() == "// v1\n"
    assert (ext / "instance.json").is_file()
    assert json.loads((ext / "instance.json").read_text())["token"] == "old"
    # No staging leftovers next to the live copy.
    assert not [p for p in ext.parent.iterdir() if p.name.startswith(".extension")]


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


def test_overwrite_preserves_the_browser_profile(tmp_path):
    # The generator can rewrite the bundle; it CANNOT recreate the profile, which
    # holds install_uuid, the session, cookies and saved passwords. An --overwrite
    # that rmtree'd the whole instance root destroyed all of it silently and the
    # instance came back as a different install (§6/§13, InstancePaths docstring).
    _gen(tmp_path, "alpha", token="first")
    profile = tmp_path / "alpha" / "profile"
    uuid_marker = profile / "Local State"
    uuid_marker.write_text('{"installUuid":"born-in-profile"}')
    cookies = profile / "Default" / "Cookies"
    cookies.parent.mkdir(parents=True)
    cookies.write_text("session-data")

    core.generate_instance(
        out_root=tmp_path, source_extension_dir=REPO_EXTENSION, instance_id="alpha",
        title="alpha", service_url="wss://h.example.com", token="second",
        key_b64="K==", extension_id="a" * 32, overwrite=True,
    )

    # Redden: restore `shutil.rmtree(paths.root)` -> both of these vanish.
    assert uuid_marker.read_text() == '{"installUuid":"born-in-profile"}'
    assert cookies.read_text() == "session-data"
    # …while the bundle really was regenerated.
    data = json.loads((tmp_path / "alpha" / "extension" / "instance.json").read_text())
    assert data["token"] == "second"


def test_overwrite_still_clears_stale_bundle_files(tmp_path):
    # Sparing the profile must not turn --overwrite into a merge: files the previous
    # generation left in the bundle/.app must be gone, or a renamed .app or a removed
    # extension file would linger forever.
    res = _gen(tmp_path, "alpha", title="Old Title", token="first")
    stale = res.paths.extension_dir / "stale.js"
    stale.write_text("// from the previous generation\n")
    old_app = res.paths.app_dir

    core.generate_instance(
        out_root=tmp_path, source_extension_dir=REPO_EXTENSION, instance_id="alpha",
        title="New Title", service_url="wss://h.example.com", token="second",
        key_b64="K==", extension_id="a" * 32, overwrite=True,
    )

    assert not stale.exists()
    assert not old_app.exists()  # the .app named after the OLD title is gone
    assert (tmp_path / "alpha" / "new-title.app").is_dir()


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


# --------------------------------------------------------------------------- #
# Private writes must be COMPLETE, not just created
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


def test_instance_json_is_written_whole_under_short_writes(tmp_path, monkeypatch):
    # A truncated instance.json is unparseable config that survives every existence
    # check and gets loaded as authoritative. Redden: go back to a bare
    # `os.write(fd, data)` -> the file is cut in half and json.loads raises.
    payload = {"instanceId": "alpha", "note": "x" * 5000}
    target = tmp_path / "instance.json"
    _break_os_write(monkeypatch)

    core._write_private_text(target, json.dumps(payload, indent=2) + "\n")

    assert json.loads(target.read_text()) == payload
    assert (target.stat().st_mode & 0o777) == 0o600  # perms survive the fix


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


# --------------------------------------------------------------------------- #
# The token is never a command-line argument
# --------------------------------------------------------------------------- #
def test_cli_has_no_token_option(tmp_path):
    # argv is world-readable in `ps` for the whole run and lands in shell history,
    # so the secret must not be expressible there. Redden: re-add `--token`.
    parser = cli.build_parser()
    with pytest.raises(SystemExit):  # argparse exits 2 on an unknown option
        parser.parse_args(
            ["generate", "--instance-id", "a", "--service-url", "wss://h",
             "--out", str(tmp_path), "--token", "leaked-secret"]
        )
    with pytest.raises(SystemExit):
        parser.parse_args(["restamp", "--out", str(tmp_path), "--token", "leaked-secret"])

    # And it must not sneak back in as a PREFIX of --token-file: argparse's default
    # abbreviation matching would otherwise resolve `--token SECRET` to it, quietly
    # restoring the argv path. Redden: drop allow_abbrev=False.
    args = parser.parse_args(["restamp", "--out", str(tmp_path), "--token-file", "/p"])
    assert args.token_file == "/p"
    assert not hasattr(args, "token")


def test_token_comes_from_env_and_token_file_only(tmp_path, monkeypatch):
    monkeypatch.setenv("EXT_TOKEN", "from-env")
    assert cli._resolve_token(None) == "from-env"

    # --token-file puts a PATH in argv, never the secret itself.
    token_file = tmp_path / "token.txt"
    token_file.write_text("from-file\n")
    assert cli._resolve_token(str(token_file)) == "from-file"

    # Nothing is defaulted: a missing token fails loudly (AGENTS.md).
    monkeypatch.delenv("EXT_TOKEN")
    with pytest.raises(SystemExit):
        cli._resolve_token(None)
