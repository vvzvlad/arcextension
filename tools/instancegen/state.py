"""One-time copy of per-extension STATE from the main Brave profile into an instance.

``generate``'s ``--sync-extensions`` gives an instance the main profile's extension
CODE (``--load-extension``, re-resolved at every launch) but none of its STATE, so
Bitwarden and friends come up logged-out. This module copies that state ONCE.

**It is a copy and it CANNOT be a live sync.** Chromium keeps per-extension storage in
LevelDB, and a LevelDB is single-writer: the process that opens it takes a ``LOCK`` file
and holds it for the browser's lifetime. Two browsers pointed at one directory therefore
cannot share it — a symlink from an instance's profile into the main profile does not
produce a shared vault, it produces a second browser that sees broken/locked storage.
So after this runs the profiles DIVERGE: logging out in one does not log the others out,
a vault entry added in one does not appear in the others, and re-running this command is
the only way to re-align them (by overwriting, not merging).

**What the copy does and does not buy.** It carries the account and the ENCRYPTED VAULT,
so the full login — email + master password + 2FA — is not needed again in the instance.
Whether the vault comes up UNLOCKED depends on the Bitwarden vault-timeout setting: with
timeout "Never" and action "Lock" the derived key is persisted in that same storage and
the instance should come up unlocked; with any other timeout/action the instance asks for
the master password once. Nothing here can promise more than that.

**The encrypted vault then exists in one more profile on this disk.** Main profile plus
every instance this is run for. That is the actual price of not re-entering credentials,
and it is the operator's call to make knowingly rather than to discover later.

Two hard safety properties, both enforced below:

* **Nothing is copied while any Brave process lives** (:func:`running_brave_processes`).
  These are live databases; snapshotting one under its own writer can copy a half-flushed
  log and produce a corrupt destination. There is deliberately no ``--force``.
* **The SOURCE profile is only ever READ**, the same invariant the generated launcher
  holds (``core.render_launcher_script``).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import core

# Default MAIN profile directory the state is read from — the ``Default`` dir itself, one
# level above the ``Extensions`` dir `generate --sync-extensions` points at. Like
# `core.DEFAULT_BRAVE_BINARY` this is a public third-party app path, not a secret and not
# one of our own services, so a default is fine (AGENTS.md); override with `--from`.
DEFAULT_MAIN_PROFILE_DIR = (
    "~/Library/Application Support/BraveSoftware/Brave-Browser/Default"
)

# The two directories a Chromium profile keeps per-extension state in, both keyed by
# extension id. `Local Extension Settings` is `chrome.storage.local` (Bitwarden's account,
# encrypted vault and session live here — 7.2 MB on the owner's profile);
# `Sync Extension Settings` is `chrome.storage.sync` (16 KB). Measured on the real profile:
# there is nothing else per-extension to take — Bitwarden has no per-origin IndexedDB dir.
LOCAL_SETTINGS_DIRNAME = "Local Extension Settings"
SYNC_SETTINGS_DIRNAME = "Sync Extension Settings"

# The unpacked-extension directories the main browser owns. Presence of `<id>` here is what
# makes an id eligible — see `eligible_extension_ids`.
EXTENSIONS_DIRNAME = "Extensions"

# The profile directory INSIDE a Chromium `--user-data-dir`. An instance's user-data-dir is
# `<instance>/profile` (core._PROFILE_DIRNAME) and the state dirs sit under
# `<instance>/profile/Default/`.
_CHROMIUM_PROFILE_DIRNAME = "Default"

# Seconds `pgrep` gets before the check is given up on (and the copy refused).
_PGREP_TIMEOUT = 10


class StateCopyRefused(RuntimeError):
    """The copy was refused before anything was written.

    Every refusal this module can raise — a live browser, a source that is not a profile,
    an instance with no profile, an ``--only`` id that must not be cloned — is this one
    type, so the CLI can turn all of them into a clean non-zero exit instead of a
    traceback.
    """


@dataclass(frozen=True)
class CopiedState:
    """One extension's copied state: its id, the bytes copied and which dirs came along."""

    extension_id: str
    bytes_copied: int
    parts: tuple[str, ...]


# --------------------------------------------------------------------------- #
# The running-browser guard (the module's only impure seam)
# --------------------------------------------------------------------------- #
def running_brave_processes(brave_binary: str = core.DEFAULT_BRAVE_BINARY) -> list[str]:
    """Command lines of every live process whose argv mentions the Brave binary.

    THE impure seam of this module: tests monkeypatch this name rather than shelling out.

    One ``pgrep -f`` covers both browsers that matter, because they are the same program:
    the main browser and every instance ``.app`` exec the same ``Brave Browser`` binary and
    differ only in ``--user-data-dir``. Matching on the binary's file name (not its full
    path) also catches a Brave installed somewhere else.

    Fails CLOSED. If ``pgrep`` is missing, times out or dies, this raises
    :class:`StateCopyRefused` rather than returning "nothing is running": an unverified
    guard must not authorise copying a live database. A false positive costs a needless
    refusal; a false negative costs a corrupt vault copy.
    """
    pattern = Path(brave_binary).name
    try:
        out = subprocess.run(
            ["pgrep", "-fl", pattern],
            capture_output=True,
            text=True,
            timeout=_PGREP_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise StateCopyRefused(
            f"cannot check whether Brave is running ({type(exc).__name__}: {exc}) — "
            "refusing to copy live LevelDB databases unverified. Quit every Brave "
            "(the main browser and every instance .app) and make `pgrep` available."
        ) from exc
    # `pgrep -fl` prints "<pid> <full command line>"; pgrep exits 1 with no output when
    # nothing matched, which is the good case.
    return [line.split(" ", 1)[1] for line in out.stdout.splitlines() if " " in line]


# `--user-data-dir` may be given as `--user-data-dir=<path>` (what the generated launcher
# emits). Stopping at whitespace mis-cuts a path containing a space, which is fine: this
# value is only ever a LABEL in the refusal message, never a path anything acts on.
_USER_DATA_DIR_RE = re.compile(r"--user-data-dir=(\S+)")


def browsers_to_quit(cmdlines: list[str]) -> list[str]:
    """Human labels for the top-level BROWSERS among *cmdlines* — what the operator quits.

    Chromium runs dozens of helper processes (renderers, GPU, utility), all carrying the
    binary's name and all identified by a ``--type=`` flag. Quitting a browser takes its
    helpers with it, so only the type-less processes are worth naming — with their
    ``--user-data-dir`` when they have one, which is exactly what tells the main browser
    apart from each instance.
    """
    labels = []
    for line in cmdlines:
        if "--type=" in line:  # a helper of some browser, not a browser
            continue
        found = _USER_DATA_DIR_RE.search(line)
        labels.append(found.group(1) if found else "the MAIN Brave profile")
    return sorted(set(labels))


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #
def eligible_extension_ids(source_default_dir: str | Path) -> list[str]:
    """Ids whose state may be copied: those with BOTH stored state AND an ``Extensions/<id>``.

    THE ``Extensions/<id>`` REQUIREMENT IS THE WHOLE SAFETY FILTER, and it is not about
    tidiness. Store-installed extensions are unpacked by the browser into
    ``Extensions/<id>/<version>_0/``; the CURATOR extension never is — it is loaded unpacked
    from a shared directory outside any profile, so it has no ``Extensions/`` dir in any
    profile while having the SAME id in every one of them (Chromium hashes the shared load
    path). Its ``chrome.storage.local`` is where that instance's ``install_uuid`` and its
    per-install enrollment secret live (§6). A blanket copy of every state dir would
    therefore overwrite the instance's own identity with the MAIN browser's, and the service
    would see the instance as a different install — an enrolment silently taken over.

    No id is hard-coded: any extension loaded unpacked (the curator today, a dev build
    tomorrow) is excluded by the same rule, and every store-installed one passes it.
    """
    source = Path(source_default_dir)
    local = source / LOCAL_SETTINGS_DIRNAME
    extensions = source / EXTENSIONS_DIRNAME
    if not local.is_dir():
        return []
    return sorted(
        entry.name
        for entry in local.iterdir()
        if entry.is_dir() and (extensions / entry.name).is_dir()
    )


def _state_dirs(source: Path, extension_id: str) -> list[str]:
    """Which of the two state dirs the SOURCE actually has for *extension_id*.

    ``Sync Extension Settings/<id>`` is often absent (nothing used ``chrome.storage.sync``);
    that is normal and is skipped silently. A ``Sync`` dir the instance already has of its
    own is left alone rather than deleted — this replaces what the source can supply, it
    does not mirror the source's absences.
    """
    return [
        name
        for name in (LOCAL_SETTINGS_DIRNAME, SYNC_SETTINGS_DIRNAME)
        if (source / name / extension_id).is_dir()
    ]


def _tree_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


# --------------------------------------------------------------------------- #
# The copy
# --------------------------------------------------------------------------- #
def copy_extension_state(
    *,
    source_default_dir: str | Path,
    instance_dir: str | Path,
    only: list[str] | None = None,
) -> list[CopiedState]:
    """Copy per-extension state from a main profile's ``Default`` dir into one instance.

    *source_default_dir* is the MAIN profile's ``Default`` directory (the parent of both
    ``Extensions/`` and the two state dirs) and is only ever READ. *instance_dir* is an
    instance root as ``generate`` writes it, so its ``--user-data-dir`` is
    ``<instance_dir>/profile`` and the state lands under
    ``<instance_dir>/profile/Default/<state dir>/<id>``. That destination path is IDENTICAL
    to the source's relative path because the ids are identical: every store-installed
    manifest carries a ``key``, and Chromium hashes that key instead of the load path, so a
    ``--load-extension``ed store extension keeps its real id in the instance.

    *only* restricts the copy to the given ids; every one of them must be eligible
    (:func:`eligible_extension_ids`) or the whole run is refused — an id with state but no
    ``Extensions/`` dir is an unpacked extension whose state is per-install IDENTITY, never
    something to clone. The default is every eligible id.

    Each ``<id>`` directory is REPLACED, not merged: a LevelDB is a set of files that only
    make sense together, and dropping fresh ``.ldb`` files next to a stale ``MANIFEST``
    yields a database that is neither. The replacement goes through
    :func:`core.replace_tree`, so the copy is staged beside the destination and swapped in
    whole — an interrupted run leaves the instance's previous state exactly as it was.

    Refuses (:class:`StateCopyRefused`) while ANY Brave process is alive. The check runs
    LAST, after every path has been validated, so a typo is reported without demanding the
    browser be quit first — but nothing has been written by then either.

    Returns one :class:`CopiedState` per id, in id order. Read the module docstring for
    what this copy is worth and what it costs: it is a one-time copy, it cannot be a live
    sync, and it puts the encrypted vault in one more profile on this disk.
    """
    source = Path(source_default_dir).expanduser().resolve()
    instance = Path(instance_dir).expanduser().resolve()

    if not (source / EXTENSIONS_DIRNAME).is_dir():
        raise StateCopyRefused(
            f"{source} is not a Brave profile directory (no {EXTENSIONS_DIRNAME}/) — pass "
            "the profile's `Default` dir, e.g. FROM='"
            f"{DEFAULT_MAIN_PROFILE_DIR}'"
        )
    profile = instance / core._PROFILE_DIRNAME
    if not profile.is_dir():
        raise StateCopyRefused(
            f"{profile} does not exist — INSTANCE_DIR must be an instance root generated "
            "by `make instance` (its --user-data-dir is the `profile` dir inside it)"
        )
    destination = profile / _CHROMIUM_PROFILE_DIRNAME

    eligible = eligible_extension_ids(source)
    if only is not None:
        unknown = [ext_id for ext_id in only if ext_id not in eligible]
        if unknown:
            raise StateCopyRefused(
                f"refusing: {', '.join(unknown)} — no {EXTENSIONS_DIRNAME}/<id> dir in "
                f"{source}. An extension with stored state but no unpacked directory is "
                "loaded from outside the profile (the curator extension is), and its "
                "storage holds THIS install's identity — copying it would overwrite the "
                "instance's install_uuid and enrollment secret with the main browser's."
            )
        selected = [ext_id for ext_id in eligible if ext_id in only]
    else:
        selected = eligible

    # LAST guard before the first byte is written: these are live LevelDB databases and a
    # snapshot taken under their own writer can be a corrupt one. No --force exists.
    running = running_brave_processes()
    if running:
        quit_these = browsers_to_quit(running) or ["every Brave process"]
        raise StateCopyRefused(
            "refusing: Brave is running ("
            f"{len(running)} process(es)). These are live LevelDB databases — copying one "
            "while its own writer runs can produce a corrupt snapshot. Quit (⌘Q, not just "
            "close the windows):\n  " + "\n  ".join(quit_these) + "\nthen run this again. "
            "There is deliberately no --force."
        )

    copied = []
    for ext_id in selected:
        parts = _state_dirs(source, ext_id)
        total = 0
        for part in parts:
            src_dir = source / part / ext_id
            total += _tree_bytes(src_dir)
            core.replace_tree(
                destination / part / ext_id,
                lambda staged, src_dir=src_dir: shutil.copytree(src_dir, staged),
            )
        copied.append(
            CopiedState(extension_id=ext_id, bytes_copied=total, parts=tuple(parts))
        )
    return copied
