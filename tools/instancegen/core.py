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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# Default path of the SYSTEM Brave binary the launcher execs. Brave, not Chrome:
# `--load-extension` is gated behind `BUILDFLAG(GOOGLE_CHROME_BRANDING)` and
# Chrome refuses it ("--load-extension is not allowed in Google Chrome") (§13,
# arch row 20). This is a public third-party app path, not a secret — a default
# is fine (AGENTS.md); override with --brave-binary.
DEFAULT_BRAVE_BINARY = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"

# Default path of the MAIN Brave profile's `Extensions` dir — the store-installed
# extensions an instance loads ALONGSIDE the curator bundle (see
# `render_launcher_script`). Like DEFAULT_BRAVE_BINARY above this is a public
# third-party app path, not a secret nor one of our own services, so a default is fine
# (AGENTS.md); override with --sync-extensions, turn it off with --no-sync-extensions.
DEFAULT_MAIN_EXTENSIONS_DIR = (
    "~/Library/Application Support/BraveSoftware/Brave-Browser/Default/Extensions"
)

# Fallback CFBundleShortVersionString/CFBundleVersion for the .app when the bundle it wraps
# carries no readable `version` (an unstamped or hand-made bundle dir).
DEFAULT_APP_VERSION = "0.1.0"

# No manifest CONFIGURATION placeholders remain: `<host>` went with issue #35 (the manifest
# is hostless, only `<all_urls>`), and the `key` field went with the extension-id pinning
# (there is no origin allow-list left to pin an id for — see src/api/cors.py). That is what
# made bundles non-interchangeable: a bundle configured for one host/id was NOT the same
# artefact as another, so the universal build stamps no configuration at all.
#
# It does stamp build IDENTITY — `version`, and ONLY `version`; `stamp_build_identity`
# below carries the argument for why that is a different kind of thing. Note the price: the
# stamped manifest is RE-SERIALISED as a whole, so the built manifest.json is not a
# byte-for-byte copy of the source one — in particular the blank lines separating the
# manifest's sections do not survive, and `diff dist/manifest.json extension/manifest.json`
# shows that reflow on top of the two stamped values. Every OTHER file is copied verbatim.

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
def render_launcher_script(
    brave_binary: str,
    profile_dir: str | Path,
    extension_dir: str | Path,
    extra_flags: list[str] | None = None,
    sync_extensions_from: str | Path | None = None,
) -> str:
    """A POSIX-sh launcher that execs the system Brave with the instance flags.

    Paths are absolute and quoted; ``"$@"`` forwards any extra args. The profile
    is a stable absolute dir OUTSIDE the ``.app`` so relocating the ``.app`` does
    not orphan it (and regenerating the ``.app`` never touches it -> ``install_uuid``
    survives). ``--load-extension`` points at the SHARED universal bundle.

    With *sync_extensions_from* set to a MAIN Brave profile's ``Extensions`` directory,
    the launcher additionally loads that profile's store-installed extensions
    (Bitwarden, DeepL, …) unpacked, so a fresh instance does not come up with the curator
    bundle alone. Every store-installed manifest carries a ``key``, and Chromium derives
    an unpacked extension's id from that key rather than from the load path, so each one
    keeps its REAL ``chrome-extension://`` id and origin.

    The list is built AT EVERY LAUNCH by the emitted shell, not baked in here — see the
    comment in the generated script for why. What it does NOT carry is extension STATE:
    the vault session, the per-extension settings and the local storage all live in the
    profile's ``Local Extension Settings``, which stays empty in a fresh instance. So the
    extensions arrive installed but logged-out and unconfigured. That is the DEFAULT
    because it is the safe one, not because the state must stay put: ``copy-state`` /
    ``make instance-state`` copies it into ONE chosen instance on request, once, with
    every Brave quit (:mod:`.state`). What is deliberately not on offer is a live shared
    session — a LevelDB has a single writer, so the profiles diverge from the copy on.

    Two costs of this mechanism are STATED here rather than fixed, because fixing either
    one costs more than it buys:

    **(a) The mtime heuristic picks the newest version dir ON DISK, which is not
    necessarily the version the main browser has ACTIVE.** Chromium unpacks an update
    ahead of time and activates it later, recording the pending one under
    ``idle_install_info`` in ``Secure Preferences``. Measured on the owner's real profile:
    uBlock Origin active 1.72.2 / on disk 1.73.0, MetaMask active 13.41.0.0 / on disk
    13.42.0.0 — 2 of 26 diverged that day. Usually this only means "slightly newer". The
    edge that must be named: ``idle_install_info`` is ALSO where Chromium parks an update
    requesting NEW PERMISSIONS the user has not approved yet, and a ``--load-extension``
    extension is granted its manifest's permissions with no prompt at all — so an instance
    can run a permission set the main browser is deliberately holding back.

    **(b) A directory under ``Extensions/`` does not mean the extension is ENABLED.**
    Disabling an extension in ``brave://extensions`` writes ``state``/``disable_reasons``
    into prefs and LEAVES the directory on disk; an uninstalled one also lingers there
    until garbage collection. The glob loads both, and ``--load-extension`` activates what
    it is given unconditionally — so an extension the owner disabled in the main browser
    stays alive in every instance, and one they removed can come back until Chromium
    collects the dir. The owner is NOT the only writer of ``disable_reasons``: the browser
    sets it too — a Web Store blocklisting (an extension pulled for MALWARE), the greylist,
    enterprise policy — and the directory is deliberately kept so the extension can be
    restored, so a killswitch that disabled an extension in the main browser is BYPASSED in
    every instance, which for a profile carrying a password manager and a crypto wallet is a
    different class of consequence from "I turned it off and it still runs".

    **Reading ``Secure Preferences`` was CONSIDERED AND REJECTED — it is not impossible.**
    ``/usr/bin/plutil`` ships in the BASE macOS install (a real Mach-O, unlike the
    ``/usr/bin/python3`` shim, which is only a Command Line Tools trampoline), reads
    Chromium's JSON and answers both questions directly — ``plutil -extract
    "extensions.settings.<id>.path" raw -o - "Secure Preferences"`` names the ACTIVE version
    dir, ``…disable_reasons`` the enablement state, 26 sequential calls in 0.22 s wall; a
    missing or corrupt file answers empty and complains on stderr, so falling back is
    trivial. The mtime glob stays anyway, on these three reasons:

    1. **The shipped branch would leave CI.** The tests run the GENERATED launcher
       end-to-end on whatever machine runs them. ``plutil`` is macOS-only, so the launcher
       would become ``plutil … || <mtime fallback>`` and a Linux runner would only ever
       exercise the fallback — green CI on a branch that never runs on the target platform.
       That is precisely the disease ``_tiny_repo_extension``'s docstring was written
       against ("the shape of the clone must not decide whether the assertion runs"): here
       the PLATFORM would decide, and the untested branch would be the shipped one.
    2. **The fallback survives regardless.** ``Secure Preferences`` is written lazily and
       can be caught mid-flush (verified: ``plutil`` answers empty on a truncated file), so
       the glob remains the fallback either way — both costs above merely become RARER, at
       double the complexity in the one script that must never fail.
    3. **It binds to undocumented Chromium internals.** ``extensions.settings.<id>.path``
       and ``disable_reasons`` are private schema, not an API; a rename would fall back to
       the heuristic SILENTLY, i.e. a new silent divergence replacing the one it removed.
    """
    flags = " ".join(_sh_quote(f) for f in (extra_flags or []))
    flags_line = f"  {flags} \\\n" if flags else ""
    if sync_extensions_from is None:
        prologue = ""
        load_extension = _sh_quote(str(extension_dir))
    else:
        prologue = _render_extension_sync(extension_dir, sync_extensions_from)
        load_extension = '"$EXTS"'
    return (
        "#!/bin/sh\n"
        "# Auto-generated by tools/instancegen (§13). Do not edit by hand;\n"
        "# regenerate instead. Launches the SYSTEM Brave — NOT Google Chrome,\n"
        "# which refuses --load-extension (§13, arch row 20).\n"
        "set -eu\n"
        f"{prologue}"
        f"exec {_sh_quote(brave_binary)} \\\n"
        f"  --user-data-dir={_sh_quote(str(profile_dir))} \\\n"
        f"  --load-extension={load_extension} \\\n"
        f"{flags_line}"
        '  "$@"\n'
    )


def _render_extension_sync(
    extension_dir: str | Path, main_extensions_dir: str | Path
) -> str:
    """The sh prologue that builds ``$EXTS`` — the curator bundle plus the main profile's.

    Resolved at LAUNCH and deliberately not baked in at generation time: the main browser
    OWNS those directories and rewrites them on every extension update (a new
    ``<id>/<version>_0/`` dir, the old one collected later), so a baked absolute path is a
    silent drop — the extension would simply stop being loaded the first time it updated,
    with no error anywhere. Chromium never auto-updates a ``--load-extension`` extension,
    so re-reading the main profile at each launch is the ONLY thing that keeps these
    current.

    The version dir is picked by MTIME, and EVERY candidate is walked newest-first until
    one that actually carries a ``manifest.json`` is found — see the trade-offs (a) and (b)
    in :func:`render_launcher_script` for what that heuristic can and cannot know.
    """
    return (
        "\n"
        "# Load the MAIN profile's store-installed extensions next to the curator bundle.\n"
        "# Re-resolved on EVERY launch, never baked in: the main browser owns these dirs\n"
        "# and rewrites them on every extension update, so a baked path silently stops\n"
        "# existing (and Chromium never auto-updates a --load-extension extension).\n"
        "# Their real chrome-extension:// ids survive: each manifest carries a `key`, and\n"
        "# Chromium hashes that key instead of the load path. Extension STATE does NOT\n"
        "# come along (Local Extension Settings stays empty) — logged-out by design.\n"
        "# The curator bundle is FIRST and is loaded even with no main profile present.\n"
        "# Two things this cannot know: whether the newest dir on disk is the version the\n"
        "# main browser has ACTIVE (an update pending a permission prompt sits on disk\n"
        "# already), and whether an extension is ENABLED there at all — a dir survives both\n"
        "# the owner disabling it and the Web Store blocklisting it. Both are answerable\n"
        "# from Secure Preferences via plutil; considered and rejected, see\n"
        "# render_launcher_script in tools/instancegen/core.py for why.\n"
        f"EXTS={_sh_quote(str(extension_dir))}\n"
        f"MAIN={_sh_quote(str(main_extensions_dir))}\n"
        'if [ -d "$MAIN" ]; then\n'
        '  for d in "$MAIN"/*/; do\n'
        "    # An empty $MAIN leaves the glob unexpanded, as a literal, which the checks\n"
        "    # below would discard anyway — this says so explicitly instead of relying on\n"
        "    # it. It does NOT make the script zsh-proof: zsh's `nomatch` aborts at\n"
        "    # expansion time, before this line runs.\n"
        '    [ -e "$d" ] || continue\n'
        "    id=${d%/}; id=${id##*/}\n"
        "    # Not an extension: Chromium's staging dir. Dotted entries need no case of\n"
        "    # their own — POSIX `*` never expands to a leading dot.\n"
        '    case "$id" in Temp) continue ;; esac\n'
        "    # Newest by MTIME, not by name: several ids keep 2-3 version dirs side by\n"
        "    # side and `<version>_0` sorts wrongly (1.10.0_0 < 1.9.0_0). Every candidate\n"
        "    # is tried, newest first, and the first one that really is an extension wins:\n"
        "    # the newest dir is NOT always loadable — deleting the files inside an old\n"
        "    # version dir raises that dir's mtime above the live one, so a main browser\n"
        "    # collecting an old version (or killed mid-cleanup) puts a manifest-less dir\n"
        "    # on top. Testing only the first candidate dropped the whole extension.\n"
        '    v=$(ls -dt "$d"*/ 2>/dev/null | while IFS= read -r c; do\n'
        '      [ -f "${c}manifest.json" ] || continue\n'
        "      printf '%s\\n' \"$c\"\n"
        "      break\n"
        "    done)\n"
        '    if [ -n "$v" ]; then EXTS="$EXTS,${v%/}"; fi\n'
        "  done\n"
        "fi\n"
    )


def _sh_quote(value: str) -> str:
    """Single-quote a value for POSIX sh."""
    return "'" + value.replace("'", "'\\''") + "'"


def bundle_identifier(instance_id: str) -> str:
    """A per-instance CFBundleIdentifier (§13: unique per bundle)."""
    return f"xyz.tabscurator.instance.{slugify(instance_id)}"


# Apple allows at most THREE integers in CFBundleShortVersionString/CFBundleVersion, while
# a Chrome manifest `version` may carry four. The fourth component is dropped rather than
# written out: same "validate what you write" reasoning as `validate_manifest_version` —
# emitting a formally invalid plist and finding out later is the failure mode to avoid.
_APP_VERSION_MAX_COMPONENTS = 3


def _app_version(version: str) -> str:
    """*version* truncated to the three components Apple's plist version keys allow."""
    return ".".join(version.split(".")[:_APP_VERSION_MAX_COMPONENTS])


def build_info_plist(
    title: str,
    bundle_id: str,
    executable_name: str,
    icon_name: str,
    version: str = DEFAULT_APP_VERSION,
) -> str:
    """A minimal but valid ``Info.plist`` for the ``.app`` wrapper.

    *version* is the version of the bundle this ``.app`` launches (see
    :func:`bundle_version`), passed in by :func:`generate_instance`. It is truncated to at
    most three components before it reaches the plist: a four-part manifest version is
    legal for Chrome but not for Apple, and writing all four produces a formally invalid
    plist. The default keeps the function usable on its own, without a bundle to read.
    """
    plist_version = _app_version(version)
    entries = {
        "CFBundleName": title,
        "CFBundleDisplayName": title,
        "CFBundleIdentifier": bundle_id,
        "CFBundleExecutable": executable_name,
        "CFBundleIconFile": icon_name,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": plist_version,
        "CFBundleVersion": plist_version,
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


def stamp_build_identity(manifest_text: str, *, version: str) -> str:
    """Return *manifest_text* with ``version`` set to the build stamp, and no other field.

    What this stamps is build IDENTITY, and that is deliberately NOT what the deleted
    ``stamp_manifest(manifest, host, key_b64)`` used to stamp. A baked-in ``<host>`` or
    signing key is CONFIGURATION: it made two bundles different artefacts, which is why the
    universal build stamps none of it. Two bundles that differ only in their version behave
    identically — the stamp changes nothing the extension reads or does, it only lets a
    human look at the card in brave://extensions and tell WHICH build is loaded. Do not
    grow a ``host=``/``key=`` parameter here: that would put configuration stamping back
    under an identity name, which is exactly the conflation the rename undid.

    ``version_name`` is NOT written, and any inherited one is REMOVED. That field is the one
    brave://extensions renders when present, and the extensions page renders it in the slot
    BESIDE THE EXTENSION NAME — a slot with roughly ten characters of room. A human-readable
    stamp there (``0.1.135 · edd7787 · 2026-08-07 20:58``, 35 characters) wrapped to two
    lines and truncated the name itself, so the card read «arcextens… 0.1.135 · edd7787 · …».
    With no ``version_name`` the page falls back to ``version`` ("if no version_name is
    present, the version field will be used for display purposes as well"), which is exactly
    how every other extension's card renders. So the build identity has to fit INSIDE the
    version, and the short sha, the ``-dirty`` marker and the full build date deliberately
    do not travel in the manifest at all — the terminal output of ``make dev-bundle`` is
    where the full identity is printed. Do not re-add ``version_name``: it truncates the
    name again, silently.

    Pure text in, pure text out — the caller supplies the value, this never reads git or
    the clock.

    The file is RE-SERIALISED WHOLE, not patched in place: the text is parsed, the value is
    set and ``json.dumps(indent=2, ensure_ascii=False)`` writes it back out. That is the
    robust path — the output is always valid JSON, every key keeps its value and its order,
    and the Russian prose in the ``//``-comment keys stays unescaped. It is NOT
    byte-surgical though: the blank lines separating the manifest's sections do not survive
    the round-trip, so ``diff dist/manifest.json extension/manifest.json`` shows the reflow
    as well as the stamped value — more than one changed line, by design.
    """
    validate_manifest_version(version)

    data = json.loads(manifest_text)
    if not isinstance(data, dict):
        raise ValueError("manifest.json must contain a JSON object")

    # A dict comprehension keeps insertion order, and assigning an EXISTING key keeps its
    # position — so `version` stays where the source manifest put it (and is appended only
    # if the source had none) while any `version_name` is dropped.
    stamped = {key: value for key, value in data.items() if key != "version_name"}
    stamped["version"] = version

    return json.dumps(stamped, indent=2, ensure_ascii=False) + "\n"


def bundle_version(bundle_dir: str | Path) -> str | None:
    """The ``version`` of a BUILT bundle's manifest, or ``None`` if it has none/unreadable.

    Named for the bundle, not the manifest: the manifest has a key literally called
    ``manifest_version`` whose value is the MV3 marker ``3``, and a ``manifest_version()``
    returning ``"0.1.131"`` reads like that at every call site.

    Read-only. Used to make the ``.app`` wrapper report the version of the bundle its
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
) -> None:
    """Copy the extension bundle, skipping dev cruft and any source instance.json.

    Used by the universal ``bundle`` build (§9) to materialise the ONE fleet-wide
    bundle every instance loads. The repo's own ``extension/`` is only READ here.

    With *version* given, the COPY's ``manifest.json`` is stamped with the build identity
    (:func:`stamp_build_identity`) — the source manifest is never written. With it left
    ``None`` this is a verbatim copy and nothing is rewritten at all.
    """
    src = Path(src_extension_dir)
    # EVERY argument check runs before a single byte is copied. A raise after copytree
    # would leave a complete but unstamped bundle sitting at the destination, and the
    # retry would then need --force to get past its own leftovers. `version` is therefore
    # validated HERE and not only inside stamp_build_identity, which runs after the copy.
    if version is not None:
        validate_manifest_version(version)
    if not (src / "manifest.json").is_file():
        raise ValueError(f"{src} is not an extension bundle (no manifest.json)")
    shutil.copytree(src, dst_extension_dir, ignore=_COPY_IGNORE)
    if version is None:  # verbatim copy: nothing is rewritten
        return
    dst_manifest = Path(dst_extension_dir) / "manifest.json"
    dst_manifest.write_text(
        stamp_build_identity(
            dst_manifest.read_text(encoding="utf-8"), version=version
        ),
        encoding="utf-8",
    )


# Prefix of the staging dir `replace_tree` builds into. Dotted so it is inconspicuous
# next to the target, and distinctive so a leftover from a crashed rebuild is obvious.
_REBUILD_STAGING_PREFIX = ".rebuild-"


def stale_staging_dirs(parent: str | Path) -> list[Path]:
    """Leftover ``.rebuild-*`` staging dirs in *parent* — never a live one's.

    :func:`replace_tree` removes its own staging dir on every path it can control, but not
    on the one it cannot: a ``SIGKILL`` (or a power cut) between the build and the swap
    leaves ``.rebuild-XXXX/new/`` sitting next to the target with a PARTIAL copy of
    whatever was being replaced — for :mod:`.state` that is a partial copy of an encrypted
    vault, left in the profile forever, because nothing else ever looks for it.

    Only real directories whose name carries the prefix are reported; a symlink with that
    name is left alone rather than followed, since deleting through one would reach outside
    the profile. This never runs concurrently with a live ``replace_tree``: the caller
    sweeps BEFORE it starts copying, and the copy refuses to run at all while a browser is
    alive.
    """
    parent = Path(parent)
    if not parent.is_dir():
        return []
    try:
        children = list(parent.iterdir())
    except OSError:
        return []
    return sorted(
        child
        for child in children
        if child.name.startswith(_REBUILD_STAGING_PREFIX)
        and not child.is_symlink()
        and child.is_dir()
    )


def replace_tree(dst: str | Path, build: Callable[[Path], None]) -> None:
    """Put a freshly built tree at *dst* without ever leaving a half-written one there.

    *build* is handed a path that does not exist yet and must materialise the complete new
    tree at it. Only a COMPLETE tree is ever swapped in:

        1. build -> <parent>/.rebuild-XXXX/new     (the slow part; dst still intact)
        2. rename dst -> <staging>/old             (dst is free for an instant)
        3. rename <staging>/new -> dst             (dst is now the NEW tree, same path)
        4. rmtree the staging dir                  (drops the previous tree)

    The staging dir is a sibling of *dst* on purpose: ``os.replace`` cannot rename across
    filesystems, and a system temp dir very often is one. The only lossy window is between
    (2) and (3) — two renames in one directory — and even there the previous tree still
    exists under the staging dir until step (4). Anything that fails earlier leaves *dst*
    exactly as it was.

    Shared by the two callers that must not corrupt what they replace: an unpacked bundle a
    browser already loads (:func:`replace_bundle`) and an extension's LevelDB state
    directory (:mod:`.state`).
    """
    dst = Path(dst).resolve()
    parent = dst.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=_REBUILD_STAGING_PREFIX, dir=parent))
    new_tree = staging / "new"
    old_tree = staging / "old"

    try:
        build(new_tree)
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
        # Put the previous tree back at its path rather than leaving nothing there.
        if moved_away:
            os.replace(old_tree, dst)
        shutil.rmtree(staging, ignore_errors=True)
        raise

    shutil.rmtree(staging, ignore_errors=True)


def replace_bundle(
    src_extension_dir: str | Path,
    dst_extension_dir: str | Path,
    *,
    version: str | None = None,
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
    extension. So the fresh tree is staged next to the target and swapped in whole — see
    :func:`replace_tree`, which owns that dance.

    *version* is forwarded to :func:`copy_bundle`, so the staged tree is already stamped
    before the swap — the live bundle is never a stamped-in-place tree. ``copy_bundle``
    also validates the source, and it runs inside the staged build, i.e. while dst is
    still untouched.
    """
    src = Path(src_extension_dir).resolve()
    dst = Path(dst_extension_dir).resolve()
    # The swap DELETES the previous tree at dst, so a source that is dst — or lives inside
    # it — would destroy itself. `--out extension` is a plausible typo; refuse it here
    # rather than eat the repo's own bundle.
    if src == dst or src.is_relative_to(dst):
        raise ValueError(
            f"refusing to rebuild {dst} from a source inside it ({src}) — the rebuild "
            "replaces that whole directory"
        )
    replace_tree(dst, lambda staged: copy_bundle(src, staged, version=version))


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
    """What ``generate`` produced: the instance's paths, and nothing that duplicates them.

    There is deliberately NO ``launch_command`` here anymore. It used to carry the argv
    ``build_launch_command`` reconstructed — Brave plus the two per-instance flags — and
    with extension sync on that reconstruction became a LIE: the launcher resolves
    ``--load-extension`` at launch time into the bundle plus ~26 main-profile dirs, while
    the field still named the bundle alone. An operator copying it got a browser without
    Bitwarden and concluded the sync was broken. The launcher script is the single source
    of truth for the argv, so ``paths.launcher`` is what callers are given.
    """

    paths: InstancePaths


def reject_comma_in_load_extension_paths(
    bundle_dir: str | Path | None, sync_extensions_from: str | Path | None
) -> None:
    """Refuse either operator-chosen ``--load-extension`` path if it contains a COMMA.

    Chromium splits ``--load-extension`` on commas. Extension ids and ``<version>_0`` dir
    names cannot contain one, but these two paths ARE operator-chosen — and a single comma
    anywhere in the main-profile path poisons EVERY entry the launcher builds under it (~27
    at once), each half naming a directory that does not exist, with the browser reporting
    nothing. Refuse the path instead of generating that launcher.

    Lives on its own so both the library entry point (:func:`generate_instance`) and the
    CLI can raise the SAME refusal from ONE text — the CLI runs it before it creates
    ``--out``, so a refused invocation leaves no half-made output tree behind.
    """
    for option, path in (
        ("--bundle-dir", bundle_dir),
        ("--sync-extensions", sync_extensions_from),
    ):
        if path is not None and "," in str(path):
            raise ValueError(
                f"{option} path contains a comma ({path}) — Chromium splits "
                "--load-extension on commas, so every path baked from it would be cut "
                "into pieces that do not exist. Move or rename the directory."
            )


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
    sync_extensions_from: str | Path | None = None,
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

    *sync_extensions_from* (a MAIN Brave profile's ``Extensions`` dir) additionally makes
    the launcher load that profile's store-installed extensions, re-resolved at every
    launch — see :func:`render_launcher_script`. Their STATE is NOT carried over: a fresh
    instance's ``Local Extension Settings`` is empty, so they arrive logged-out and
    unconfigured, deliberately (one shared vault session across every space is not wanted).

    Neither *bundle_dir* nor *sync_extensions_from* may contain a COMMA: Chromium splits
    ``--load-extension`` on commas, so one in either operator-chosen path would cut every
    entry built from it into nonexistent halves — with sync on that is all ~27 extensions
    at once, silently.
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

    # The library-level comma guard (see the function's own docstring). The CLI runs the
    # same check BEFORE it creates `--out`; this one stays because `generate_instance` is
    # the public entry point and has other callers.
    reject_comma_in_load_extension_paths(bundle_dir, sync_extensions_from)

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
    paths.launcher.parent.mkdir(parents=True, exist_ok=True)
    paths.launcher.write_text(
        render_launcher_script(
            brave_binary, paths.profile_dir, bundle_dir, extra_flags,
            sync_extensions_from=sync_extensions_from,
        ),
        encoding="utf-8",
    )
    paths.launcher.chmod(0o755)
    # The .app reports the version the shared bundle carried AT INSTANCE-GENERATION TIME,
    # read from that bundle's manifest. It is a SNAPSHOT, not a live reading: `make
    # dev-bundle` rebuilds the shared bundle and touches no instance, so from the next
    # rebuild on this plist names an older build. That is acceptable because the plist is
    # not where anyone checks which build is loaded — the extension card in
    # brave://extensions is (it renders the manifest's `version`) and it stays the
    # source of truth. What this does buy is that a freshly generated .app does not
    # advertise a version the bundle never had. An unstamped/hand-made bundle (no readable
    # `version`) falls back to DEFAULT_APP_VERSION.
    paths.info_plist.write_text(
        build_info_plist(
            title,
            bundle_identifier(instance_id),
            paths.launcher.name,
            paths.icon_png.name,
            version=bundle_version(bundle_dir) or DEFAULT_APP_VERSION,
        ),
        encoding="utf-8",
    )
    paths.icon_png.parent.mkdir(parents=True, exist_ok=True)
    paths.icon_png.write_bytes(
        icon_source_png if icon_source_png is not None else instance_icon_png(title)
    )

    return GenerateResult(paths=paths)
