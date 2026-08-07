"""Pure core of the instance generator (§13) — filesystem + text only.

Everything here runs on Linux/CI without a browser or a mac. The macOS-only real
``.icns``/``.app`` build lives in `macos`. Nothing in this module touches the repo's own
``extension/`` — it only ever writes under the caller-supplied output root.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path

# Default path of the SYSTEM Brave binary the launcher execs. Brave, not Chrome:
# `--load-extension` is gated behind `BUILDFLAG(GOOGLE_CHROME_BRANDING)` and
# Chrome refuses it ("--load-extension is not allowed in Google Chrome") (§13,
# arch row 20). This is a public third-party app path, not a secret — a default
# is fine (AGENTS.md); override with --brave-binary.
DEFAULT_BRAVE_BINARY = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"

# Fallback CFBundleShortVersionString/CFBundleVersion for the .app when the bundle it wraps
# carries no readable `version` (an unstamped or hand-made bundle dir).
DEFAULT_APP_VERSION = "0.1.0"

# No manifest CONFIGURATION placeholders remain: `<host>` went with issue #35 (the manifest
# is hostless, only `<all_urls>`), and the `key` field went with the extension-id pinning
# (there is no origin allow-list left to pin an id for — see src/api/cors.py). That is what
# made bundles non-interchangeable: a bundle configured for one host/id was NOT the same
# artefact as another, so the universal build had to stamp nothing.
#
# `bundle` does stamp two fields, and they are a different kind of thing: `version` and
# `version_name` are build IDENTITY, not configuration (`stamp_manifest` below). Two
# bundles that differ only in them behave identically — the stamp changes nothing the
# extension reads or does; it only lets a human look at the card in brave://extensions and
# tell WHICH build is loaded. Everything else is still copied verbatim.

# Directory name of an instance's empty --user-data-dir under the output root.
_PROFILE_DIRNAME = "profile"

# Bundle entries never copied into the universal `bundle` output: dev/test cruft and —
# critically — any stray instance.json / node_modules from the source tree.
_COPY_IGNORE = shutil.ignore_patterns(
    "node_modules", "test", "instance.json", ".git", "*.log", "__pycache__"
)


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
def slugify(text: str) -> str:
    """A filesystem-safe slug for a dir name / bundle id segment."""
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", text.strip()).strip("-._")
    return slug.lower() or "instance"


@dataclass(frozen=True)
class InstancePaths:
    """Absolute paths of one instance's on-disk layout under the output root.

    Under enrollment (§13) an instance is a THIN wrapper: an empty ``--user-data-dir``
    plus a ``.app`` whose launcher loads the SHARED universal bundle (built once by
    ``instancegen bundle``) — there is NO per-instance extension copy and NO
    ``instance.json``. The profile lives in the STABLE instance dir (not inside the
    movable ``.app``): the ``.app`` launcher references it by absolute path, so
    relocating the ``.app`` does not break ``--user-data-dir``, and regenerating the
    ``.app`` leaves the profile (hence ``install_uuid``) untouched (§6/§13).
    """

    root: Path  # <out>/<slug>
    profile_dir: Path  # <out>/<slug>/profile     (--user-data-dir)
    app_dir: Path  # <out>/<slug>/<Title>.app
    launcher: Path  # .app/Contents/MacOS/run
    info_plist: Path  # .app/Contents/Info.plist
    icon_png: Path  # .app/Contents/Resources/AppIcon.png (source)


def instance_paths(out_root: str | Path, instance_id: str, title: str) -> InstancePaths:
    root = Path(out_root).resolve() / slugify(instance_id)
    # Slugify the .app DIR name too: a raw title like "../x" or "Work/Home" would
    # otherwise escape the out-root / create nested dirs. The human-readable title
    # survives untouched in Info.plist's CFBundleName/CFBundleDisplayName.
    app = root / f"{slugify(title)}.app"
    contents = app / "Contents"
    return InstancePaths(
        root=root,
        profile_dir=root / _PROFILE_DIRNAME,
        app_dir=app,
        launcher=contents / "MacOS" / "run",
        info_plist=contents / "Info.plist",
        icon_png=contents / "Resources" / "AppIcon.png",
    )


# --------------------------------------------------------------------------- #
# Launcher + Info.plist
# --------------------------------------------------------------------------- #
def build_launch_command(
    brave_binary: str,
    profile_dir: str | Path,
    extension_dir: str | Path,
    extra_flags: list[str] | None = None,
) -> list[str]:
    """The argv the launcher execs: Brave + the two per-instance flags (§13)."""
    return [
        brave_binary,
        f"--user-data-dir={profile_dir}",
        f"--load-extension={extension_dir}",
        *(extra_flags or []),
    ]


def render_launcher_script(
    brave_binary: str,
    profile_dir: str | Path,
    extension_dir: str | Path,
    extra_flags: list[str] | None = None,
) -> str:
    """A POSIX-sh launcher that execs the system Brave with the instance flags.

    Paths are absolute and quoted; ``"$@"`` forwards any extra args. The profile
    is a stable absolute dir OUTSIDE the ``.app`` so relocating the ``.app`` does
    not orphan it (and regenerating the ``.app`` never touches it -> ``install_uuid``
    survives). ``--load-extension`` points at the SHARED universal bundle.
    """
    flags = " ".join(_sh_quote(f) for f in (extra_flags or []))
    flags_line = f"  {flags} \\\n" if flags else ""
    return (
        "#!/bin/sh\n"
        "# Auto-generated by tools/instancegen (§13). Do not edit by hand;\n"
        "# regenerate instead. Launches the SYSTEM Brave — NOT Google Chrome,\n"
        "# which refuses --load-extension (§13, arch row 20).\n"
        "set -eu\n"
        f"exec {_sh_quote(brave_binary)} \\\n"
        f"  --user-data-dir={_sh_quote(str(profile_dir))} \\\n"
        f"  --load-extension={_sh_quote(str(extension_dir))} \\\n"
        f"{flags_line}"
        '  "$@"\n'
    )


def _sh_quote(value: str) -> str:
    """Single-quote a value for POSIX sh."""
    return "'" + value.replace("'", "'\\''") + "'"


def bundle_identifier(instance_id: str) -> str:
    """A per-instance CFBundleIdentifier (§13: unique per bundle)."""
    return f"xyz.arcextension.instance.{slugify(instance_id)}"


def build_info_plist(
    title: str,
    bundle_id: str,
    executable_name: str,
    icon_name: str,
    version: str = DEFAULT_APP_VERSION,
) -> str:
    """A minimal but valid ``Info.plist`` for the ``.app`` wrapper.

    *version* is the bundle's stamped ``version`` (see :func:`stamp_manifest`), passed in
    by :func:`generate_instance` so the ``.app`` reports the same build as the extension
    it launches. The default keeps the function usable on its own, without a stamp.
    """
    entries = {
        "CFBundleName": title,
        "CFBundleDisplayName": title,
        "CFBundleIdentifier": bundle_id,
        "CFBundleExecutable": executable_name,
        "CFBundleIconFile": icon_name,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "CFBundleInfoDictionaryVersion": "6.0",
        "LSMinimumSystemVersion": "11.0",
    }
    body = "".join(
        f"\t<key>{k}</key>\n\t<string>{_xml_escape(v)}</string>\n"
        for k, v in entries.items()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        f"{body}"
        "</dict>\n</plist>\n"
    )


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# --------------------------------------------------------------------------- #
# Icon (pure: a solid-colour PNG source; the real .icns is built on mac)
# --------------------------------------------------------------------------- #
def instance_icon_png(title: str, size: int = 1024) -> bytes:
    """A per-instance PNG icon source: a solid square coloured from *title*.

    Pure stdlib (zlib) — no PIL/mac tools — so CI can assert a real, per-instance
    icon SOURCE is placed. The mac layer downsizes it into a proper ``.icns``.
    """
    color = _color_from_text(title)
    return _solid_rgba_png(size, color)


def _color_from_text(text: str) -> tuple[int, int, int, int]:
    """A deterministic, reasonably distinct opaque colour from a string."""
    h = zlib.crc32(text.encode("utf-8"))
    r = 64 + (h & 0x7F)
    g = 64 + ((h >> 8) & 0x7F)
    b = 64 + ((h >> 16) & 0x7F)
    return (r, g, b, 255)


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def _solid_rgba_png(size: int, rgba: tuple[int, int, int, int]) -> bytes:
    row = bytes([0]) + bytes(rgba) * size  # filter byte 0 + `size` RGBA pixels
    raw = row * size
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(raw, 9))
        + _png_chunk(b"IEND", b"")
    )


# --------------------------------------------------------------------------- #
# Build stamp (pure text; the environment is read in `cli`, never here)
# --------------------------------------------------------------------------- #
# Chrome's rules for manifest `version`
# (https://developer.chrome.com/docs/extensions/reference/manifest/version): one to four
# dot-separated integers, each 0..65535 inclusive, a non-zero component must not carry a
# leading zero (`032` is rejected), and they must not be all zero (`0.0.0.0` is rejected,
# `0.1.0.0` is fine). An invalid version does not degrade — the browser refuses to load
# the extension at all — so it is validated here rather than written out and discovered
# on the extensions page.
MANIFEST_VERSION_MAX_COMPONENT = 65535
_VERSION_COMPONENT_RE = re.compile(r"\A(?:0|[1-9][0-9]*)\Z")


def validate_manifest_version(version: str) -> str:
    """Return *version* unchanged, or raise ``ValueError`` describing what is illegal."""
    parts = version.split(".")
    if not 1 <= len(parts) <= 4:
        raise ValueError(
            f"invalid manifest version {version!r}: expected 1 to 4 dot-separated "
            f"integers, got {len(parts)}"
        )
    for part in parts:
        if not _VERSION_COMPONENT_RE.match(part):
            raise ValueError(
                f"invalid manifest version {version!r}: component {part!r} must be a "
                "non-negative integer with no leading zero"
            )
        if int(part) > MANIFEST_VERSION_MAX_COMPONENT:
            raise ValueError(
                f"invalid manifest version {version!r}: component {part!r} exceeds the "
                f"maximum {MANIFEST_VERSION_MAX_COMPONENT}"
            )
    if all(int(part) == 0 for part in parts):
        raise ValueError(f"invalid manifest version {version!r}: must not be all zero")
    return version


def stamp_manifest(manifest_text: str, *, version: str, version_name: str) -> str:
    """Return *manifest_text* with ``version``/``version_name`` set to the build stamp.

    ``version_name`` is the field brave://extensions RENDERS when present ("will be used
    for display purposes if present"), so it carries the human-readable build identity;
    ``version`` stays machine-ordered so the scheme still advances per commit for anything
    that reads only it.

    Pure text in, pure text out — the caller supplies the two values, this never reads git
    or the clock. Re-serialised with the manifest's own formatting: 2-space indent and
    ``ensure_ascii=False``, because the ``//``-prefixed comment keys hold Russian prose and
    escaping it would rewrite most of the file for no reason.
    """
    validate_manifest_version(version)
    if not version_name.strip():
        raise ValueError("version_name must not be empty")

    data = json.loads(manifest_text)
    if not isinstance(data, dict):
        raise ValueError("manifest.json must contain a JSON object")

    # Rebuild the mapping so `version_name` lands next to `version` instead of at the end:
    # json.dumps preserves insertion order, and the two belong together in a diff.
    stamped: dict = {}
    for key, value in data.items():
        if key == "version":
            stamped["version"] = version
            stamped["version_name"] = version_name
        elif key != "version_name":
            stamped[key] = value
    if "version" not in stamped:  # a manifest without one: append rather than fail
        stamped["version"] = version
        stamped["version_name"] = version_name

    return json.dumps(stamped, indent=2, ensure_ascii=False) + "\n"


def manifest_version(bundle_dir: str | Path) -> str | None:
    """The ``version`` of a BUILT bundle's manifest, or ``None`` if it has none/unreadable.

    Read-only. Used to make the ``.app`` wrapper report the same build as the bundle its
    launcher loads.
    """
    try:
        data = json.loads((Path(bundle_dir) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) and version else None


# --------------------------------------------------------------------------- #
# Bundle copy
# --------------------------------------------------------------------------- #
def copy_bundle(
    src_extension_dir: str | Path,
    dst_extension_dir: str | Path,
    *,
    version: str | None = None,
    version_name: str | None = None,
) -> None:
    """Copy the extension bundle, skipping dev cruft and any source instance.json.

    Used by the universal ``bundle`` build (§9) to materialise the ONE fleet-wide
    bundle every instance loads. The repo's own ``extension/`` is only READ here.

    With *version*/*version_name* given, the COPY's ``manifest.json`` is stamped with the
    build identity (:func:`stamp_manifest`) — the source manifest is never written. With
    both left ``None`` this is a verbatim copy and nothing is rewritten at all.
    """
    src = Path(src_extension_dir)
    if not (src / "manifest.json").is_file():
        raise ValueError(f"{src} is not an extension bundle (no manifest.json)")
    shutil.copytree(src, dst_extension_dir, ignore=_COPY_IGNORE)
    if version is None and version_name is None:
        return
    if version is None or version_name is None:
        raise ValueError("version and version_name must be given together")
    dst_manifest = Path(dst_extension_dir) / "manifest.json"
    dst_manifest.write_text(
        stamp_manifest(
            dst_manifest.read_text(encoding="utf-8"),
            version=version,
            version_name=version_name,
        ),
        encoding="utf-8",
    )


# Prefix of the staging dir `replace_bundle` builds into. Dotted so it is inconspicuous
# next to the bundle, and distinctive so a leftover from a crashed rebuild is obvious.
_REBUILD_STAGING_PREFIX = ".rebuild-"


def replace_bundle(
    src_extension_dir: str | Path,
    dst_extension_dir: str | Path,
    *,
    version: str | None = None,
    version_name: str | None = None,
) -> None:
    """Rebuild an EXISTING bundle dir IN PLACE, keeping its path and never half-writing it.

    The destination path MUST survive the rebuild unchanged, and that is the whole point:
    Chromium derives the ``chrome-extension://`` id from the absolute path of the loaded
    directory, so the id — and with it the extension's origin and that profile's
    ``chrome.storage.local``, where the enrollment secret lives — is a function of THIS
    path. Rebuilding into a differently-named dir hands the browser a *different*
    extension and silently drops the instance's enrolment.

    A naive ``rmtree(dst)`` + ``copytree`` would keep the path but is still wrong: an
    interrupted copy leaves the operator with a directory that is no longer a loadable
    extension. So the fresh tree is built into a staging dir NEXT TO the target — same
    parent, hence the same filesystem, hence ``os.replace`` is a rename and not a copy —
    and only a COMPLETE tree is ever swapped in:

        1. copy src -> <parent>/.rebuild-XXXX/new   (the slow part; dst still intact)
        2. rename dst -> <staging>/old              (dst is free for an instant)
        3. rename <staging>/new -> dst              (dst is now the NEW tree, same path)
        4. rmtree the staging dir                   (drops the previous tree)

    The only lossy window is between (2) and (3) — two renames in one directory — and
    even there the previous tree still exists under the staging dir until step (4).
    Anything that fails earlier leaves the existing bundle exactly as it was.

    *version*/*version_name* are forwarded to :func:`copy_bundle`, so the staged tree is
    already stamped before the swap — the live bundle is never a stamped-in-place tree.
    """
    src = Path(src_extension_dir).resolve()
    dst = Path(dst_extension_dir).resolve()
    # Step (4) DELETES the previous tree at dst, so a source that is dst — or lives inside
    # it — would destroy itself. `--out extension` is a plausible typo; refuse it here
    # rather than eat the repo's own bundle.
    if src == dst or src.is_relative_to(dst):
        raise ValueError(
            f"refusing to rebuild {dst} from a source inside it ({src}) — the rebuild "
            "replaces that whole directory"
        )
    parent = dst.parent
    parent.mkdir(parents=True, exist_ok=True)
    # Staging goes in the TARGET's parent on purpose: os.replace cannot rename across
    # filesystems, and a system temp dir is very often a different one.
    staging = Path(tempfile.mkdtemp(prefix=_REBUILD_STAGING_PREFIX, dir=parent))
    new_tree = staging / "new"
    old_tree = staging / "old"

    try:
        # Validates the source and does all the copying while dst is still untouched.
        copy_bundle(
            src_extension_dir, new_tree, version=version, version_name=version_name
        )
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    moved_away = False
    if dst.exists() or dst.is_symlink():
        os.replace(dst, old_tree)
        moved_away = True
    try:
        os.replace(new_tree, dst)
    except BaseException:
        # Put the previous bundle back at its path rather than leaving nothing loadable.
        if moved_away:
            os.replace(old_tree, dst)
        shutil.rmtree(staging, ignore_errors=True)
        raise

    shutil.rmtree(staging, ignore_errors=True)


def _clear_instance_dir_keeping_profile(root: Path) -> None:
    """Empty a regenerated instance's dir but KEEP ``profile/`` (§6/§13 invariant).

    Everything the generator writes (the ``.app``) it can write again; the profile it
    CANNOT. The profile holds the ``install_uuid`` minted by the SW on first run —
    plus the session, cookies and saved passwords. An
    ``--overwrite`` that rmtree'd the whole root silently destroyed all of it, and
    the regenerated instance came back with a NEW install_uuid: a reconnect the
    service is entitled to treat as a different install. Regeneration is therefore a
    bundle-level operation only, exactly as ``InstancePaths`` documents.
    """
    for child in root.iterdir():
        if child.name == _PROFILE_DIRNAME:
            continue
        # Symlinked dirs are unlinked, not walked into: rmtree would follow a
        # planted link out of the instance dir.
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


# --------------------------------------------------------------------------- #
# Generate (§13) — the thin per-instance .app wrapper over the shared bundle
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GenerateResult:
    paths: InstancePaths
    launch_command: list[str]


def generate_instance(
    *,
    out_root: str | Path,
    bundle_dir: str | Path,
    instance_id: str,
    title: str,
    brave_binary: str = DEFAULT_BRAVE_BINARY,
    icon_source_png: bytes | None = None,
    extra_flags: list[str] | None = None,
    overwrite: bool = False,
) -> GenerateResult:
    """Materialise one instance's ``.app`` + empty profile under *out_root*.

    Under enrollment (§13) an instance NO LONGER carries its own extension copy or an
    ``instance.json``: the whole fleet loads the SINGLE universal bundle built once by
    ``instancegen bundle`` (§9). This therefore writes only three things — an empty
    ``--user-data-dir``, the ``.app`` (launcher + Info.plist + icon), and nothing else —
    with the launcher's ``--load-extension`` pointing at the SHARED *bundle_dir*. Two
    instances built against the same *bundle_dir* load the exact same extension dir, and
    therefore the same ``chrome-extension://`` id — which is now simply the hash of that
    shared load path, with nothing pinning it. Nothing depends on the id being stable: no
    origin is checked anywhere anymore (see :mod:`src.api.cors`).

    The service address and the per-install secret are NOT baked in — the extension gets
    them through the enrollment settings UI, so ``generate`` needs no serviceUrl and no
    token. ``icon_source_png`` defaults to a generated per-instance icon.
    """
    paths = instance_paths(out_root, instance_id, title)

    # Validate --bundle-dir BEFORE touching the filesystem. This must precede the
    # --overwrite clear below: otherwise a bad bundle-dir would wipe the existing .app via
    # _clear_instance_dir_keeping_profile and only THEN raise, leaving the operator worse
    # off than when they started. The launcher must point at a REAL bundle — a missing
    # manifest means `instancegen bundle` was never run (or --bundle-dir is wrong), and
    # loading a non-bundle dir would fail silently inside Brave later.
    bundle_dir = Path(bundle_dir).resolve()
    if not (bundle_dir / "manifest.json").is_file():
        raise ValueError(
            f"{bundle_dir} is not an extension bundle (no manifest.json) — build the "
            "shared universal bundle first with `instancegen bundle`"
        )

    if paths.root.exists():
        if not overwrite:
            raise FileExistsError(
                f"{paths.root} already exists (use --overwrite to rebuild the .app; "
                "the profile is kept)"
            )
        _clear_instance_dir_keeping_profile(paths.root)

    # 1. Empty profile dir (the --user-data-dir). Deliberately empty: the SW mints
    #    install_uuid here on first run, which is what distinguishes a reconnect from a
    #    cloned .app (§6). The generator NEVER writes install_uuid.
    paths.profile_dir.mkdir(parents=True, exist_ok=True)

    # 2. The .app wrapper: launcher (loads the SHARED bundle), Info.plist, icon source.
    launch_command = build_launch_command(
        brave_binary, paths.profile_dir, bundle_dir, extra_flags
    )
    paths.launcher.parent.mkdir(parents=True, exist_ok=True)
    paths.launcher.write_text(
        render_launcher_script(
            brave_binary, paths.profile_dir, bundle_dir, extra_flags
        ),
        encoding="utf-8",
    )
    paths.launcher.chmod(0o755)
    # The .app reports the version of the bundle it actually launches, read from that
    # bundle's stamped manifest — not a literal that would go stale the moment the bundle
    # is rebuilt. An unstamped/hand-made bundle falls back to DEFAULT_APP_VERSION.
    paths.info_plist.write_text(
        build_info_plist(
            title,
            bundle_identifier(instance_id),
            paths.launcher.name,
            paths.icon_png.name,
            version=manifest_version(bundle_dir) or DEFAULT_APP_VERSION,
        ),
        encoding="utf-8",
    )
    paths.icon_png.parent.mkdir(parents=True, exist_ok=True)
    paths.icon_png.write_bytes(
        icon_source_png if icon_source_png is not None else instance_icon_png(title)
    )

    return GenerateResult(paths=paths, launch_command=launch_command)
