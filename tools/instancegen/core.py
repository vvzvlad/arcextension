"""Pure core of the instance generator (§13) — filesystem + text only.

Everything here runs on Linux/CI without a browser or a mac. The macOS-only real
``.icns``/``.app`` build lives in `macos`; the signing key in `keys`. Nothing in
this module touches the repo's own ``extension/`` — it only ever writes under the
caller-supplied output root.
"""

from __future__ import annotations

import json
import re
import shutil
import struct
import zlib
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
        raise ValueError("token is required (pass --token or set EXT_TOKEN)")
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
        shutil.rmtree(paths.root)

    # 1. Copy the bundle (its own copy per instance).
    copy_bundle(source_extension_dir, paths.extension_dir)

    # 2. Write instance.json — the four fields — into the copied bundle.
    instance = build_instance_json(
        instance_id, title, service_url, token, allow_execute_js
    )
    paths.instance_json.write_text(
        json.dumps(instance, indent=2) + "\n", encoding="utf-8"
    )

    # 3. Stamp <host> + key into the copied manifest.
    host = host_from_service_url(service_url)
    manifest_path = paths.extension_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_path.write_text(
        json.dumps(stamp_manifest(manifest, host, key_b64), indent=2) + "\n",
        encoding="utf-8",
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


def restamp_all(
    out_root: str | Path,
    *,
    token: str,
    service_url: str | None = None,
) -> list[RestampChange]:
    """Rotate the token (and optionally serviceUrl) in EVERY instance (§13).

    The §13 rotation path: rewrite ``instance.json`` in place for all instances,
    PRESERVING each ``instanceId`` and the profile (so ``install_uuid`` survives
    and a browser restart reconnects rather than being rejected as a duplicate).
    ``instanceId`` is IMMUTABLE — this function has no parameter to change it.
    When *service_url* is given, the copied manifest's ``<host>`` is re-stamped to
    match so ``host_permissions`` stay consistent; the pinned ``key`` is left
    as-is (rotating the token must not change the extension id).
    """
    if not token:
        raise ValueError("restamp needs a non-empty token")

    changes: list[RestampChange] = []
    paths = iter_instance_json_paths(out_root)
    if not paths:
        raise FileNotFoundError(f"no instances found under {out_root}")

    for ij in paths:
        data = json.loads(ij.read_text(encoding="utf-8"))
        instance_id = data.get("instanceId")
        if not instance_id:
            # Never fabricate an id; a bad instance.json must be fixed, not
            # silently rewritten (an empty id is exactly the §6 reject case).
            raise ValueError(f"{ij} has no instanceId — refusing to restamp")

        old_token = str(data.get("token", ""))
        data["token"] = token  # instanceId is left untouched — immutable.
        if service_url is not None:
            data["serviceUrl"] = service_url
            _restamp_manifest_host(ij.parent / "manifest.json", service_url)

        ij.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        changes.append(
            RestampChange(
                instance_json=ij,
                instance_id=instance_id,
                old_token_masked=_mask(old_token),
                new_service_url=service_url,
            )
        )
    return changes


def _restamp_manifest_host(manifest_path: Path, service_url: str) -> None:
    """Re-derive `host_permissions` for a changed serviceUrl; keep the pinned key."""
    if not manifest_path.is_file():
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
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
