"""Thin macOS platform layer: the real ``.icns`` build (§13).

This is the ONE step that cannot run in CI: turning the pure-core PNG icon source
into a proper ``AppIcon.icns`` needs Apple's ``sips`` + ``iconutil``, present only
on macOS. Everything else about an instance (empty profile dir, launcher,
Info.plist, PNG icon source — there is no per-instance extension copy and no
``instance.json`` under enrollment) is produced by `core` and is
identical on Linux and mac — the ``.app`` is already a runnable bundle without the
``.icns`` (macOS falls back to the generic app icon), so CI emits every file and
asserts its content; only the icon polish is deferred to a mac.

`build_icns` is a no-op that reports "not built" off macOS (or when the tools are
missing), so callers can always invoke it and log the outcome.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# The iconset sizes macOS expects (name, pixel size).
_ICONSET_SIZES = [
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
]


@dataclass(frozen=True)
class IcnsResult:
    built: bool
    icns_path: Path | None
    reason: str  # why it was/wasn't built — logged by the CLI


def _tools_available() -> bool:
    return bool(shutil.which("sips") and shutil.which("iconutil"))


def build_icns(source_png: str | Path, dest_icns: str | Path) -> IcnsResult:
    """Build a real ``.icns`` from *source_png* — only on macOS with the tools.

    Off macOS (or if ``sips``/``iconutil`` are missing) this is a reported no-op:
    the ``.app`` still works with its PNG icon source in place; the ``.icns`` is a
    cosmetic polish an operator regenerates on a mac. Returns whether it built and
    a human reason.
    """
    source = Path(source_png)
    dest = Path(dest_icns)
    if sys.platform != "darwin":
        return IcnsResult(False, None, "not macOS — PNG source kept; build .icns on a mac")
    if not _tools_available():
        return IcnsResult(False, None, "sips/iconutil not found — PNG source kept")
    if not source.is_file():
        return IcnsResult(False, None, f"icon source missing: {source}")

    iconset = dest.with_suffix(".iconset")
    iconset.mkdir(parents=True, exist_ok=True)
    for name, px in _ICONSET_SIZES:
        subprocess.run(
            ["sips", "-z", str(px), str(px), str(source), "--out", str(iconset / name)],
            check=True,
            capture_output=True,
        )
    subprocess.run(
        ["iconutil", "-c", "icns", str(iconset), "-o", str(dest)],
        check=True,
        capture_output=True,
    )
    return IcnsResult(True, dest, "built via sips + iconutil")
