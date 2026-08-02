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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

# Default path of the SYSTEM Brave binary the launcher execs. Brave, not Chrome:
# `--load-extension` is gated behind `BUILDFLAG(GOOGLE_CHROME_BRANDING)` and
# Chrome refuses it ("--load-extension is not allowed in Google Chrome") (§13,
# arch row 20). This is a public third-party app path, not a secret — a default
# is fine (AGENTS.md); override with --brave-binary.
DEFAULT_BRAVE_BINARY = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"

# The manifest placeholder tokens (must match extension/manifest.json).
HOST_PLACEHOLDER = "<host>"
KEY_PLACEHOLDER = "REPLACE_WITH_BASE64_PUBLIC_KEY_TO_PIN_EXTENSION_ID"

# The instance-config file the SW reads via chrome.runtime.getURL on every start.
INSTANCE_JSON = "instance.json"

# Directory names in an instance's output layout.
_EXTENSION_DIRNAME = "extension"
_PROFILE_DIRNAME = "profile"

# Bundle entries never copied into an instance: dev/test cruft and — critically —
# any stray instance.json / node_modules from the source tree.
_COPY_IGNORE = shutil.ignore_patterns(
    "node_modules", "test", INSTANCE_JSON, ".git", "*.log", "__pycache__"
)


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
def _write_private_text(path, text: str) -> None:
    """Write *text* to *path* owner-only (0600).

    ``instance.json`` carries the ``EXT_TOKEN`` in cleartext — a credential as
    sensitive as the signing key (which is already 0600), so it must never be
    group/world-readable at rest. The browser runs as the same user, so owner-only
    loses no functionality. See :func:`write_private_bytes` for the atomicity and
    permission guarantees.
    """
    write_private_bytes(path, text.encode("utf-8"))


def write_private_bytes(path, data: bytes) -> None:
    """Atomically and durably write *data* to *path* owner-only (0600).

    Write-to-temp + fsync + ``os.replace``, not a truncating write in place. Writing
    straight into the destination leaves a TRUNCATED file if the disk fills on
    flush/close or the process is killed mid-write — and a truncated ``instance.json``
    still passes every ``p.exists()`` check while being unparseable config. Here the
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
    and have this function write the ``EXT_TOKEN`` (or the signing key) wherever the
    link points. ``os.fchmod`` re-asserts 0600 on the descriptor before any bytes are
    written, and since ``os.replace`` makes this inode the destination, 0600 lands on
    the final file regardless of the mode the OLD file had — which is what makes a
    re-stamp over a stray 0644 ``instance.json`` safe.
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

    Both the extension copy and the profile live in the STABLE instance dir (not
    inside the movable ``.app``): the ``.app`` launcher references them by
    absolute path, so relocating the ``.app`` does not break ``--load-extension``
    or ``--user-data-dir``, and a re-stamp that regenerates the bundle still
    leaves the profile (hence ``install_uuid``) untouched (§6/§13).
    """

    root: Path  # <out>/<slug>
    extension_dir: Path  # <out>/<slug>/extension   (--load-extension target)
    profile_dir: Path  # <out>/<slug>/profile     (--user-data-dir)
    app_dir: Path  # <out>/<slug>/<Title>.app
    launcher: Path  # .app/Contents/MacOS/run
    info_plist: Path  # .app/Contents/Info.plist
    icon_png: Path  # .app/Contents/Resources/AppIcon.png (source)
    instance_json: Path  # extension/instance.json


def instance_paths(out_root: str | Path, instance_id: str, title: str) -> InstancePaths:
    root = Path(out_root).resolve() / slugify(instance_id)
    ext = root / _EXTENSION_DIRNAME
    # Slugify the .app DIR name too: a raw title like "../x" or "Work/Home" would
    # otherwise escape the out-root / create nested dirs. The human-readable title
    # survives untouched in Info.plist's CFBundleName/CFBundleDisplayName.
    app = root / f"{slugify(title)}.app"
    contents = app / "Contents"
    return InstancePaths(
        root=root,
        extension_dir=ext,
        profile_dir=root / _PROFILE_DIRNAME,
        app_dir=app,
        launcher=contents / "MacOS" / "run",
        info_plist=contents / "Info.plist",
        icon_png=contents / "Resources" / "AppIcon.png",
        instance_json=ext / INSTANCE_JSON,
    )


# --------------------------------------------------------------------------- #
# instance.json + manifest stamping
# --------------------------------------------------------------------------- #
def build_instance_json(
    instance_id: str,
    title: str,
    service_url: str,
    token: str,
    allow_execute_js: bool = False,
) -> dict:
    """The four required fields (+ allowExecuteJs), matching instance.example.json.

    A fresh ``--user-data-dir`` starts with an empty ``chrome.storage.local``, so
    ``serviceUrl`` (§3) and the ``EXT_TOKEN`` (§12) have no other way into the
    profile — all four MUST be present, none defaulted away (§6).
    """
    if not instance_id:
        raise ValueError("instanceId is required")
    if not service_url:
        raise ValueError("serviceUrl is required")
    if not token:
        raise ValueError(
            "token is required (set EXT_TOKEN, or pass --token-file; there is no "
            "--token option — a secret must not go in argv)"
        )
    return {
        "instanceId": instance_id,
        "title": title,
        "serviceUrl": service_url,
        "token": token,
        "allowExecuteJs": bool(allow_execute_js),
    }


def host_from_service_url(service_url: str) -> str:
    """The host[:port] to stamp into `host_permissions`, derived from serviceUrl.

    ``wss://example.com`` -> ``example.com``. The scheme is irrelevant here: the
    manifest carries BOTH an https and a wss pattern for the same host (§6).
    """
    parts = urlsplit(service_url)
    host = parts.netloc or parts.path  # tolerate a bare "host" with no scheme
    # Strip any userinfo; keep host:port.
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    host = host.strip("/")
    if not host or HOST_PLACEHOLDER in host:
        raise ValueError(f"cannot derive a host from serviceUrl={service_url!r}")
    return host


def stamp_manifest(manifest: dict, host: str, key_b64: str) -> dict:
    """Return a copy of *manifest* with `<host>` filled and `key` pinned.

    ``<all_urls>`` (which carries no ``<host>``) is left untouched; every
    ``host_permissions`` entry has its ``<host>`` token replaced. The ``key`` is
    replaced whether it is the placeholder or already a real value (idempotent
    re-stamp).
    """
    out = json.loads(json.dumps(manifest))  # deep copy
    perms = out.get("host_permissions", [])
    out["host_permissions"] = [p.replace(HOST_PLACEHOLDER, host) for p in perms]
    if not key_b64:
        raise ValueError("a real base64 `key` is required to pin the extension id")
    out["key"] = key_b64
    return out


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
    is a stable absolute dir OUTSIDE the ``.app`` so relocating the bundle does
    not orphan it (and a re-stamp never touches it -> ``install_uuid`` survives).
    """
    flags = " ".join(_sh_quote(f) for f in (extra_flags or []))
    flags_line = f"  {flags} \\\n" if flags else ""
    return (
        "#!/bin/sh\n"
        "# Auto-generated by tools/instancegen (§13). Do not edit by hand;\n"
        "# regenerate or re-stamp instead. Launches the SYSTEM Brave — NOT\n"
        "# Google Chrome, which refuses --load-extension (§13, arch row 20).\n"
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

    Each instance gets its OWN copy because ``instance.json`` lives inside it and
    must differ (§13). The repo's own ``extension/`` is only READ here.
    """
    src = Path(src_extension_dir)
    if not (src / "manifest.json").is_file():
        raise ValueError(f"{src} is not an extension bundle (no manifest.json)")
    shutil.copytree(src, dst_extension_dir, ignore=_COPY_IGNORE)


def _clear_instance_dir_keeping_profile(root: Path) -> None:
    """Empty a regenerated instance's dir but KEEP ``profile/`` (§6/§13 invariant).

    Everything the generator writes (the extension copy, the ``.app``) it can write
    again; the profile it CANNOT. The profile holds the ``install_uuid`` minted by
    the SW on first run — plus the session, cookies and saved passwords. An
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


def _load_manifest(manifest_path: Path) -> dict:
    """Parse an instance's copied manifest, with a message naming the file.

    Used by the re-stamp PRE-FLIGHT as well as by the writers: every parse the apply
    phase would do must first be done here, where a failure aborts the whole run
    instead of splitting the fleet across two tokens.
    """
    if not manifest_path.is_file():
        raise ValueError(f"{manifest_path} is missing — refusing to restamp")
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"{manifest_path} is not valid JSON: {exc}") from exc


def _pinned_manifest_key(manifest_path: Path) -> str:
    """The real ``key`` already pinned in an INSTANCE's copied manifest.

    A code refresh must carry this key over from the old copy: the SOURCE bundle
    ships the placeholder, so copying its manifest verbatim would re-derive a
    different extension id — changing the ``chrome-extension://`` origin of every
    instance at once and breaking ``EXT_ALLOWED_ORIGINS``/CORS (§12). Refusing loudly
    is the only safe answer; there is nothing to guess here.
    """
    manifest = _load_manifest(manifest_path)
    key_b64 = str(manifest.get("key") or "")
    if not key_b64 or key_b64 == KEY_PLACEHOLDER:
        raise ValueError(
            f"{manifest_path} carries no pinned `key` — refusing to refresh the code "
            "(it would change the extension id and every instance's origin)"
        )
    return key_b64


def _refresh_instance_code(
    extension_dir: Path,
    source_extension_dir: str | Path,
    service_url: str,
    instance_json_text: str,
) -> None:
    """Replace one instance's extension CODE from *source_extension_dir* (§13).

    The bundle is duplicated per instance (``instance.json`` lives inside it), and
    ``protocolVersion`` is compared by exact equality (§6) — so a copy missed by an
    update is rejected on ``hello`` FOREVER, visible only in the status bar. §13
    therefore requires the re-stamp operation to carry the code as well; rotating a
    token after a ``PROTOCOL_VERSION`` bump would otherwise kill the whole fleet.

    Preserved across the refresh: the pinned manifest ``key`` (same extension id) and
    the profile (a SIBLING of this directory, never touched).

    The new tree — code, stamped manifest AND ``instance.json`` — is staged beside the
    old one and swapped in only once complete. ``instance.json`` is written into the
    staging tree BEFORE the rename, deliberately: writing it after the swap would open
    a window in which the extension dir exists WITHOUT its config, and a crash there
    leaves an instance that ``iter_instance_json_paths`` (globbing
    ``*/extension/instance.json``) no longer finds — so the next re-stamp skips it
    silently, reports one instance fewer, and that browser never connects again.
    """
    key_b64 = _pinned_manifest_key(extension_dir / "manifest.json")
    materialize_extension_bundle(
        extension_dir,
        source_extension_dir,
        service_url=service_url,
        key_b64=key_b64,
        instance_json_text=instance_json_text,
    )


def materialize_extension_bundle(
    extension_dir: Path,
    source_extension_dir: str | Path,
    *,
    service_url: str,
    key_b64: str,
    instance_json_text: str,
) -> None:
    """Build a COMPLETE extension dir (code + stamped manifest + config), then swap.

    Shared by ``generate`` and the re-stamp code refresh, because both have the same
    all-or-nothing requirement: an extension dir that exists WITHOUT ``instance.json``
    is invisible to :func:`iter_instance_json_paths` (which globs
    ``*/extension/instance.json``), so the next re-stamp silently skips that instance,
    reports one fewer, and that browser never reconnects. Everything is therefore
    assembled in a staging dir and renamed into place only once complete.

    The swap itself is guarded: if the second rename fails, the previous tree is put
    back, so a failure can never leave the instance with NO extension dir at all — a
    worse state than the config-less tree this function exists to prevent. Only the
    final cleanup of the old tree is best-effort; a leftover ``.extension.old`` is
    cosmetic and the next run clears it.
    """
    staging = extension_dir.parent / f".{extension_dir.name}.new"
    previous = extension_dir.parent / f".{extension_dir.name}.old"
    for leftover in (staging, previous):
        if leftover.exists():
            shutil.rmtree(leftover)

    try:
        copy_bundle(source_extension_dir, staging)
        manifest_path = staging / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_path.write_text(
            json.dumps(
                stamp_manifest(manifest, host_from_service_url(service_url), key_b64),
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        # The config joins the tree BEFORE the swap (see the docstring): after the
        # rename the instance is complete, never briefly config-less.
        _write_private_text(staging / INSTANCE_JSON, instance_json_text)
    except BaseException:
        # Drop the half-built tree; the live extension_dir was never touched.
        if staging.exists():
            shutil.rmtree(staging)
        raise

    had_previous = extension_dir.exists()
    if had_previous:
        extension_dir.rename(previous)
    try:
        staging.rename(extension_dir)
    except BaseException:
        # Put the old tree back rather than leaving no extension dir at all.
        if had_previous and previous.exists() and not extension_dir.exists():
            previous.rename(extension_dir)
        if staging.exists():
            shutil.rmtree(staging)
        raise
    if had_previous:
        shutil.rmtree(previous, ignore_errors=True)  # cosmetic; next run clears it


# --------------------------------------------------------------------------- #
# Generate + re-stamp
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GenerateResult:
    paths: InstancePaths
    extension_id: str
    launch_command: list[str]


def generate_instance(
    *,
    out_root: str | Path,
    source_extension_dir: str | Path,
    instance_id: str,
    title: str,
    service_url: str,
    token: str,
    key_b64: str,
    extension_id: str,
    brave_binary: str = DEFAULT_BRAVE_BINARY,
    icon_source_png: bytes | None = None,
    allow_execute_js: bool = False,
    extra_flags: list[str] | None = None,
    overwrite: bool = False,
) -> GenerateResult:
    """Materialise one instance under *out_root* (pure fs — no mac tools run).

    The caller supplies the shared ``key_b64``/``extension_id`` (from `keys`) so
    every instance shares one id/origin. ``icon_source_png`` defaults to a
    generated per-instance icon.
    """
    paths = instance_paths(out_root, instance_id, title)
    if paths.root.exists():
        if not overwrite:
            raise FileExistsError(
                f"{paths.root} already exists (use restamp to rotate, or --overwrite)"
            )
        _clear_instance_dir_keeping_profile(paths.root)

    # 1-3. The bundle (its own copy per instance), with <host>+key stamped into the
    #      manifest and instance.json — the four fields — inside it. Assembled in a
    #      staging dir and swapped in complete, so a kill mid-generate can never leave
    #      an extension dir without its config (which the next re-stamp would silently
    #      skip, rotating N-1 instances and reporting success).
    instance = build_instance_json(
        instance_id, title, service_url, token, allow_execute_js
    )
    materialize_extension_bundle(
        paths.extension_dir,
        source_extension_dir,
        service_url=service_url,
        key_b64=key_b64,
        instance_json_text=json.dumps(instance, indent=2) + "\n",
    )

    # 4. Empty profile dir (the --user-data-dir). Deliberately empty: the SW mints
    #    install_uuid here on first run, which is what distinguishes a reconnect
    #    from a cloned .app (§6). The generator NEVER writes install_uuid.
    paths.profile_dir.mkdir(parents=True, exist_ok=True)

    # 5. The .app wrapper: launcher, Info.plist, icon source.
    launch_command = build_launch_command(
        brave_binary, paths.profile_dir, paths.extension_dir, extra_flags
    )
    paths.launcher.parent.mkdir(parents=True, exist_ok=True)
    paths.launcher.write_text(
        render_launcher_script(
            brave_binary, paths.profile_dir, paths.extension_dir, extra_flags
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

    return GenerateResult(
        paths=paths, extension_id=extension_id, launch_command=launch_command
    )


def iter_instance_json_paths(out_root: str | Path) -> list[Path]:
    """Every instance's ``extension/instance.json`` under *out_root*, sorted.

    Discovers instances for re-stamp without a separate registry: one
    ``extension/instance.json`` per instance dir.
    """
    root = Path(out_root)
    if not root.is_dir():
        return []
    found = [
        p
        for p in root.glob(f"*/{_EXTENSION_DIRNAME}/{INSTANCE_JSON}")
        if p.is_file()
    ]
    return sorted(found)


@dataclass(frozen=True)
class RestampChange:
    instance_json: Path
    instance_id: str
    old_token_masked: str
    new_service_url: str | None
    code_updated: bool = False


def restamp_all(
    out_root: str | Path,
    *,
    token: str,
    service_url: str | None = None,
    source_extension_dir: str | Path | None = None,
    on_change: Callable[[RestampChange], None] | None = None,
) -> list[RestampChange]:
    """Re-stamp EVERY instance: rotate the token, and refresh the code (§13).

    The §13 rotation path: rewrite ``instance.json`` in place for all instances,
    PRESERVING each ``instanceId`` and the profile (so ``install_uuid`` survives
    and a browser restart reconnects rather than being rejected as a duplicate).
    ``instanceId`` is IMMUTABLE — this function has no parameter to change it.
    When *service_url* is given, the copied manifest's ``<host>`` is re-stamped to
    match so ``host_permissions`` stay consistent; the pinned ``key`` is left
    as-is (rotating the token must not change the extension id).

    *source_extension_dir* is the §13 «перештамповать все инстансы, обновляя заодно
    код» half: given, each instance's extension CODE is replaced from that bundle.
    That is what the CLI passes by default, and it is not optional in practice — the
    bundle is duplicated per instance while ``protocolVersion`` is compared by exact
    equality (§6), so an update that bumps the protocol and is followed by a routine
    token rotation would otherwise leave every copy on the old code, each rejected on
    ``hello`` forever with the failure visible only in the status bar. Passing None
    rotates configuration ONLY and is for callers that update the code separately.
    """
    if not token:
        raise ValueError("restamp needs a non-empty token")

    paths = iter_instance_json_paths(out_root)
    if not paths:
        raise FileNotFoundError(f"no instances found under {out_root}")

    # --- PRE-FLIGHT: validate EVERY instance before mutating the first ----------
    # This loop writes nothing. A per-instance check made mid-write would split the
    # fleet: with a bad manifest on instance 3 of 6, the first two already hold the
    # new token, the rest hold the old one — and the operator has typically already
    # rotated EXT_TOKEN on the service, so half the fleet is dead with no single
    # token that fixes it. Validating up front makes the whole operation refuse
    # instead, leaving every instance on the old, consistent, WORKING token.
    planned: list[tuple[Path, dict, str]] = []
    for ij in paths:
        data = json.loads(ij.read_text(encoding="utf-8"))
        instance_id = data.get("instanceId")
        if not instance_id:
            # Never fabricate an id; a bad instance.json must be fixed, not
            # silently rewritten (an empty id is exactly the §6 reject case).
            raise ValueError(f"{ij} has no instanceId — refusing to restamp")

        effective_url = service_url if service_url is not None else data.get("serviceUrl")
        # Whatever the apply phase will PARSE or DERIVE must be checked here, in BOTH
        # branches. The config-only branch still rewrites the manifest host when
        # --service-url is given, and its json.loads throws on a corrupt manifest just
        # as readily — checking that only under the code-refresh branch left exactly
        # the split-fleet hole this pre-flight exists to close.
        needs_manifest = source_extension_dir is not None or service_url is not None
        if needs_manifest:
            if not effective_url:
                raise ValueError(
                    f"{ij} has no serviceUrl — refusing to restamp "
                    "(the manifest host cannot be derived)"
                )
            host_from_service_url(effective_url)  # raises on an underivable host
            _load_manifest(ij.parent / "manifest.json")  # raises on corrupt/missing
        if source_extension_dir is not None:
            _pinned_manifest_key(ij.parent / "manifest.json")  # raises if unpinned
        planned.append((ij, data, str(instance_id)))

    # --- APPLY -----------------------------------------------------------------
    changes: list[RestampChange] = []
    for ij, data, instance_id in planned:
        old_token = str(data.get("token", ""))
        data["token"] = token  # instanceId is left untouched — immutable.
        if service_url is not None:
            data["serviceUrl"] = service_url
        new_text = json.dumps(data, indent=2) + "\n"

        if source_extension_dir is not None:
            # Replaces the extension dir wholesale — including instance.json, which
            # the refresh stages itself so the swapped-in tree is complete. It
            # re-stamps the manifest host too, hence the elif.
            _refresh_instance_code(
                ij.parent, source_extension_dir, str(data["serviceUrl"]), new_text
            )
        else:
            if service_url is not None:
                _restamp_manifest_host(ij.parent / "manifest.json", service_url)
            _write_private_text(ij, new_text)

        change = RestampChange(
            instance_json=ij,
            instance_id=instance_id,
            old_token_masked=_mask(old_token),
            new_service_url=service_url,
            code_updated=source_extension_dir is not None,
        )
        changes.append(change)
        # Reported as it happens so a caller can still tell the operator WHICH
        # instances already carry the new token if a later one blows up mid-apply.
        if on_change is not None:
            on_change(change)
    return changes


def _restamp_manifest_host(manifest_path: Path, service_url: str) -> None:
    """Re-derive `host_permissions` for a changed serviceUrl; keep the pinned key."""
    manifest = _load_manifest(manifest_path)
    host = host_from_service_url(service_url)
    # Rebuild the two host-scoped patterns from the schemes, preserving <all_urls>.
    new_perms = []
    for p in manifest.get("host_permissions", []):
        if p.startswith("https://"):
            new_perms.append(f"https://{host}/*")
        elif p.startswith("wss://"):
            new_perms.append(f"wss://{host}/*")
        else:
            new_perms.append(p)
    manifest["host_permissions"] = new_perms
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


def _mask(secret: str) -> str:
    if len(secret) <= 6:
        return "***"
    return f"{secret[:3]}…{secret[-3:]}"
