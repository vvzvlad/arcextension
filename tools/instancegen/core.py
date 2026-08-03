"""Pure core of the instance generator (§13) — filesystem + text only.

Everything here runs on Linux/CI without a browser or a mac. The macOS-only real
``.icns``/``.app`` build lives in `macos`; the signing key in `keys`. Nothing in
this module touches the repo's own ``extension/`` — it only ever writes under the
caller-supplied output root.
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

# The manifest placeholder tokens (must match extension/manifest.json).
HOST_PLACEHOLDER = "<host>"
KEY_PLACEHOLDER = "REPLACE_WITH_BASE64_PUBLIC_KEY_TO_PIN_EXTENSION_ID"

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
def write_private_bytes(path, data: bytes) -> None:
    """Atomically and durably write *data* to *path* owner-only (0600).

    Write-to-temp + fsync + ``os.replace``, not a truncating write in place. Writing
    straight into the destination leaves a TRUNCATED file if the disk fills on
    flush/close or the process is killed mid-write — and a truncated signing key still
    passes every ``p.exists()`` check while being an unparseable PEM. Here the
    destination keeps its previous contents until a COMPLETE file is renamed over it;
    ``os.replace`` is atomic within a directory, so a reader sees old or new, never
    half.

    The ``fsync`` before the rename is what makes that true across a POWER LOSS rather
    than only across a crash: without it the rename can reach disk while the data
    behind it has not, leaving an empty or partial file under the real name. The
    directory is fsync'd too, so the rename itself survives.

    ``tempfile.mkstemp`` supplies the temp file: it creates with ``O_EXCL`` and an
    UNPREDICTABLE name at mode 0600. A fixed name like ``.<name>.tmp`` is guessable, so
    in a shared/world-writable output dir a neighbour could pre-plant it as a symlink
    and have this function write the signing key wherever the link points.
    ``os.fchmod`` re-asserts 0600 on the descriptor before any bytes are written, and
    since ``os.replace`` makes this inode the destination, 0600 lands on the final file
    regardless of the mode the OLD file had.
    """
    path = Path(path)
    # Same directory as the destination: os.replace is atomic only within a filesystem.
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        # os.fdopen takes ownership of fd and closes it. A buffered writer writes
        # everything or raises — a bare os.write() may write only PART of the buffer
        # and return the short count without raising (a filling disk, a signal).
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _fsync_dir(directory: Path) -> None:
    """fsync a directory so a rename into it survives a power loss.

    Best-effort: some platforms/filesystems refuse to open a directory for this, and
    failing the whole write over a durability nicety would be worse than the risk.
    """
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


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
# Manifest stamping (universal `bundle` build — §9)
# --------------------------------------------------------------------------- #
def stamp_manifest(manifest: dict, host: str | None, key_b64: str) -> dict:
    """Return a copy of *manifest* with `<host>` filled (when a host is given) and `key` pinned.

    ``<all_urls>`` (which carries no ``<host>``) is left untouched; when *host* is a
    string, every ``host_permissions`` entry has its ``<host>`` token replaced.

    When *host* is ``None`` this is the HOSTLESS path taken by the universal ``bundle``
    build (§9): the manifest carries only ``<all_urls>`` (issue #35 removed the two
    per-host patterns — there is no ``<host>`` to fill), so ``host_permissions`` is left
    EXACTLY as-is and only the ``key`` is pinned. Note that even with a host string this
    is a no-op on a hostless manifest — there is no ``<host>`` token to replace — so the
    two paths differ only in intent, not in effect on the current manifest.

    The ``key`` is replaced whether it is the placeholder or already a real value
    (idempotent re-stamp).
    """
    out = json.loads(json.dumps(manifest))  # deep copy
    if host is not None:
        perms = out.get("host_permissions", [])
        out["host_permissions"] = [p.replace(HOST_PLACEHOLDER, host) for p in perms]
    if not key_b64:
        raise ValueError("a real base64 `key` is required to pin the extension id")
    out["key"] = key_b64
    return out


def stamp_bundle_manifest(manifest_path: str | Path, key_b64: str) -> None:
    """Pin the manifest ``key`` IN PLACE with a DETERMINISTIC, hostless stamp (§9).

    Used by the universal ``bundle`` build: the manifest carries only ``<all_urls>``, so
    this leaves ``host_permissions`` untouched and only pins ``key`` (via the hostless
    :func:`stamp_manifest` path). The re-serialisation is stable — ``indent=2`` with the
    input key order preserved (no timestamps, no randomness) — so two runs with the SAME
    key produce a byte-for-byte identical manifest (acc 16).
    """
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    stamped = stamp_manifest(manifest, None, key_b64)
    manifest_path.write_text(json.dumps(stamped, indent=2) + "\n", encoding="utf-8")


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
    version: str = "0.1.0",
) -> str:
    """A minimal but valid ``Info.plist`` for the ``.app`` wrapper."""
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
# Bundle copy
# --------------------------------------------------------------------------- #
def copy_bundle(src_extension_dir: str | Path, dst_extension_dir: str | Path) -> None:
    """Copy the extension bundle, skipping dev cruft and any source instance.json.

    Used by the universal ``bundle`` build (§9) to materialise the ONE fleet-wide
    bundle every instance loads. The repo's own ``extension/`` is only READ here.
    """
    src = Path(src_extension_dir)
    if not (src / "manifest.json").is_file():
        raise ValueError(f"{src} is not an extension bundle (no manifest.json)")
    shutil.copytree(src, dst_extension_dir, ignore=_COPY_IGNORE)


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
    instances built against the same *bundle_dir* load the exact same extension dir, so
    they share one ``chrome-extension://`` id/origin (the key is pinned inside the shared
    bundle, not here).

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
    paths.info_plist.write_text(
        build_info_plist(
            title,
            bundle_identifier(instance_id),
            paths.launcher.name,
            paths.icon_png.name,
        ),
        encoding="utf-8",
    )
    paths.icon_png.parent.mkdir(parents=True, exist_ok=True)
    paths.icon_png.write_bytes(
        icon_source_png if icon_source_png is not None else instance_icon_png(title)
    )

    return GenerateResult(paths=paths, launch_command=launch_command)
