"""One-time copy of per-extension STATE from the main Brave profile into an instance.

``generate``'s ``--sync-extensions`` gives an instance the main profile's extension
CODE (``--load-extension``, re-resolved at every launch) but none of its STATE, so
Bitwarden and friends come up logged-out. This module copies that state ONCE.

**It is a copy and it CANNOT be a live sync.** Chromium keeps per-extension storage in
LevelDB, and a LevelDB is single-writer: the process that opens it takes a ``LOCK`` file
and holds it for the browser's lifetime. Two browsers pointed at one directory therefore
cannot share it — a symlink from an instance's profile into the main profile does not
produce a shared vault, it produces a second browser that sees broken/locked storage.
So after this runs the profiles DIVERGE **locally**: a vault entry added in one does not
appear in the others, and re-running this command is the only way to re-align them (by
overwriting, not merging). What is NOT local — and is therefore NOT divergent — is
whatever the SERVER keys off the copied storage: Bitwarden's storage carries an ``appId``,
the device identifier a refresh token is bound to, so both profiles present themselves to
the server as ONE device. See :func:`copy_extension_state` for what that is and is not
known to do.

**What the copy does and does not buy.** It carries the account and the ENCRYPTED VAULT,
so the full login — email + master password + 2FA — is not needed again in the instance.
Whether the vault comes up UNLOCKED depends on the Bitwarden vault-timeout setting: with
timeout "Never" and action "Lock" the derived key is persisted in that same storage and
the instance should come up unlocked; with any other timeout/action the instance asks for
the master password once. Nothing here can promise more than that.

**The encrypted vault then exists in one more profile on this disk.** Main profile plus
every instance this is run for. Bitwarden is not the only such extension: MetaMask
(``nkbihfbeogaeaoehlefnkodbefgpgknn``) is store-installed and therefore eligible too, and
its ``chrome.storage.local`` holds the ENCRYPTED SEED VAULT of the wallet. That is the
actual price of not re-entering credentials, and it is the operator's call to make
knowingly rather than to discover later.

Three hard safety properties, all enforced below:

* **Nothing is copied while any Brave process lives** (:func:`running_brave_processes`).
  These are live databases; snapshotting one under its own writer can copy a half-flushed
  log and produce a corrupt destination. There is deliberately no ``--force``, and the
  check FAILS CLOSED on every outcome it cannot positively read as "nothing matched".
* **Only ids the browser really installed are eligible** (:func:`eligible_extension_ids`),
  and the extension THIS instance loads unpacked is excluded by its derived id on top of
  that (:func:`chromium_unpacked_extension_id`). Those two layers are what keep the
  curator's per-install identity out of the copy.
* **The SOURCE profile is only ever READ**, the same invariant the generated launcher
  holds (``core.render_launcher_script``).
"""

from __future__ import annotations

import hashlib
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
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
# encrypted vault and session live here — 7.2 MB on the owner's profile; MetaMask's
# encrypted seed vault lives here too); `Sync Extension Settings` is `chrome.storage.sync`
# (16 KB). Measured on the real profile: there is nothing else per-extension to take —
# Bitwarden has no per-origin IndexedDB dir.
LOCAL_SETTINGS_DIRNAME = "Local Extension Settings"
SYNC_SETTINGS_DIRNAME = "Sync Extension Settings"

# The unpacked-extension directories the main browser owns. A `<version>/manifest.json`
# under `<id>` here is what makes an id eligible — see `eligible_extension_ids`.
EXTENSIONS_DIRNAME = "Extensions"

# The profile directory INSIDE a Chromium `--user-data-dir`. An instance's user-data-dir is
# `<instance>/profile` (core._PROFILE_DIRNAME) and the state dirs sit under
# `<instance>/profile/Default/`.
_CHROMIUM_PROFILE_DIRNAME = "Default"

# Seconds `pgrep` gets before the check is given up on (and the copy refused).
_PGREP_TIMEOUT = 10

# Where an instance's launcher lives under its root, as `generate` writes it
# (`<instance>/<Title>.app/Contents/MacOS/run`, see `core.instance_paths`).
_LAUNCHER_GLOB = "*.app/Contents/MacOS/run"

# Extensions whose per-extension storage IS a secret store, id -> what it holds.
#
# THIS LIST NEVER DECIDES WHAT IS COPIED. Eligibility is `eligible_extension_ids` alone and
# no id is hard-coded by either of its layers; this map only decides how LOUD the plan is
# about what the run DESTROYS at the destination. "19.4 MB replaced" and "this instance's
# own MetaMask seed vault deleted" are the same number and not the same event, and the
# second one is the decision the operator is actually taking. An id missing from this map
# is reported plainly, never silently — the map can only add emphasis, never remove a row.
KNOWN_SECRET_STORES = {
    "nkbihfbeogaeaoehlefnkodbefgpgknn": "MetaMask — the wallet's ENCRYPTED SEED VAULT",
    "nngceckbapebfimnlniiiahkandclblb": "Bitwarden — the ENCRYPTED PASSWORD VAULT",
}


def secret_store_label(extension_id: str) -> str | None:
    """What *extension_id*'s storage holds, if it is a known vault/wallet; else ``None``."""
    return KNOWN_SECRET_STORES.get(extension_id)


class StateCopyRefused(RuntimeError):
    """The copy was refused, or died part-way through, with a message a human can act on.

    Almost every one of these is raised BEFORE anything is written — a live browser, an
    unverifiable process check, a source that is not a profile, an instance with no
    profile, an ``--only`` id that must not be cloned, a destination with no room. The one
    exception is an ``OSError`` from the copy loop itself, which is converted to this type
    so the operator gets the id it died on (and the list of ids already committed, printed
    on stderr) instead of a raw traceback. Either way the CLI turns it into a clean
    non-zero exit.
    """


@dataclass(frozen=True)
class CopiedState:
    """One extension's copied state: its id, the bytes copied and which dirs came along.

    *bytes_replaced* is what the instance held for this id BEFORE the copy and no longer
    holds: the copy replaces each ``<id>`` directory whole, so that state is gone.
    """

    extension_id: str
    bytes_copied: int
    parts: tuple[str, ...]
    bytes_replaced: int = 0


@dataclass(frozen=True)
class PlannedCopy:
    """One extension the plan WOULD copy: its id, total bytes and the dirs involved.

    *bytes_replaced* is the other half of the transaction and the half the plan used to
    hide: the size of what the DESTINATION already holds for this id, across exactly the
    dirs this run replaces. It is not a merge — ``core.replace_tree`` swaps the whole
    directory — so every one of those bytes is deleted, irrecoverably and with no backup.
    The plan sizes the source and the destination with the same
    :func:`_tree_bytes`, so the two numbers are comparable.
    """

    extension_id: str
    bytes_to_copy: int
    parts: tuple[str, ...]
    bytes_replaced: int = 0


@dataclass(frozen=True)
class StateCopyPlan:
    """Everything decided before a byte moves — what `--dry-run` prints and the copy obeys.

    Separating the decision from the act is what makes ``--dry-run`` honest: the same
    function produces both, so the list a dry run shows is the list the real run copies,
    and the refusals a dry run reports are the refusals the real run raises.
    """

    source: Path
    destination: Path
    selected: tuple[PlannedCopy, ...]
    # (id, reason) for every id with stored state that is NOT copied — the dry run prints
    # these, because "why is Bitwarden not in the list" is the question that gets asked.
    excluded: tuple[tuple[str, str], ...]
    # Leftover `.rebuild-*` staging dirs found next to the destinations (see `core`).
    stale_staging: tuple[Path, ...]
    # Every Brave binary the running-browser check must look for (default + this
    # instance's own launcher, which `generate --brave-binary` may have pointed elsewhere).
    brave_binaries: tuple[str, ...]
    total_bytes: int
    # What the run DESTROYS: the state the instance holds RIGHT NOW for the selected ids,
    # summed over exactly the directories that get replaced. Carried next to `total_bytes`
    # because a plan that shows only what arrives shows half the transaction.
    total_replaced_bytes: int
    # Peak disk the run needs at the destination: everything landed, PLUS the single
    # largest tree existing twice while it is staged next to its target (core.replace_tree
    # builds the new tree before it drops the old one).
    peak_bytes: int
    free_bytes: int
    # Set when identity layer (b) — the unpacked-id exclusion read out of THIS instance's
    # launcher — could not be computed, with the reason. `None` means it was computed.
    # A silently degraded guard is the thing this field exists to make un-silent.
    unpacked_layer_note: str | None = None

    @property
    def fits(self) -> bool:
        return self.free_bytes >= self.peak_bytes


# --------------------------------------------------------------------------- #
# The running-browser guard (the module's only impure seam)
# --------------------------------------------------------------------------- #
def _pgrep(pattern: str) -> list[str]:
    """Raw ``pgrep -fl`` lines for *pattern* — ``"<pid> <command line>"``, pid KEPT.

    FAILS CLOSED, and the decision is taken from the EXIT CODE, never from the shape of
    stdout. ``pgrep`` answers 0 for "matched", 1 for "no match" — and 2 for a bad pattern
    and 3 for a fatal error, both of which print NOTHING on stdout. Reading stdout first
    turns those two into "Brave is not running" and lets the copy proceed onto live
    LevelDBs; reproduced with ``pgrep -fl "["`` (exit 2, empty stdout). So: anything that
    is not 0 or 1 is a refusal, exit 0 with no output is a refusal (pgrep claimed a match
    and printed none), and a line that cannot be parsed is NOT dropped — the caller counts
    it as a running process. A false positive costs a needless refusal; a false negative
    costs a corrupt vault copy.
    """
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

    if out.returncode not in (0, 1):
        raise StateCopyRefused(
            f"cannot check whether Brave is running: `pgrep -fl {pattern}` exited "
            f"{out.returncode} ({out.stderr.strip() or 'no stderr'}) — that is a bad "
            "pattern (2) or a fatal pgrep error (3), NOT an answer. Refusing to copy "
            "live LevelDB databases unverified."
        )
    if out.returncode == 1:  # the good case: pgrep positively matched nothing
        return []
    lines = [line for line in out.stdout.splitlines() if line.strip()]
    if not lines:
        raise StateCopyRefused(
            f"cannot check whether Brave is running: `pgrep -fl {pattern}` reported a "
            "match (exit 0) but printed nothing. Refusing to copy live LevelDB databases "
            "on a process check that contradicts itself."
        )
    return lines


def running_brave_processes(
    brave_binaries: Sequence[str] = (core.DEFAULT_BRAVE_BINARY,),
) -> list[str]:
    """``pgrep -fl`` lines of every live process whose argv mentions a Brave binary.

    THE impure seam of this module: tests monkeypatch this name rather than shelling out.

    One ``pgrep -f`` per binary NAME (not full path, so a Brave installed elsewhere is
    caught too) covers both browsers that matter: the main browser and every instance
    ``.app``, which differ only in ``--user-data-dir``. *brave_binaries* carries more than
    the default because ``generate --brave-binary`` lets an instance's launcher exec ANY
    binary — grepping only for ``Brave Browser`` would leave such an instance invisible and
    its live databases would be copied from under it (see
    :func:`instance_brave_binaries`). Matches are deduplicated by pid, so a process caught
    by two patterns is listed once.

    Fails CLOSED — see :func:`_pgrep` for every branch that refuses, and note that an EMPTY
    pattern set refuses HERE. With no patterns the loop below never runs, so ``pgrep`` is
    never called and every refusal in :func:`_pgrep` is skipped: the function would return
    ``[]``, i.e. "no browser is running", having checked nothing. No caller reaches that
    today (``plan.brave_binaries`` always contains :data:`core.DEFAULT_BRAVE_BINARY`), but
    "fails closed" is this function's own promise and must not rest on a caller keeping it.
    """
    patterns = sorted({Path(binary).name for binary in brave_binaries if str(binary)})
    if not patterns:
        raise StateCopyRefused(
            "cannot check whether Brave is running: no binary name to search for "
            f"(brave_binaries={list(brave_binaries)!r}) — `pgrep` was never run, so this "
            "is not an answer. Refusing to copy live LevelDB databases unverified."
        )
    matches: dict[str, str] = {}
    for pattern in patterns:
        for line in _pgrep(pattern):
            # The pid is the dedup key. A line with no space is unparseable, and its whole
            # text becomes the key — unparseable lines are kept, never dropped.
            matches.setdefault(line.split(" ", 1)[0], line)
    return [matches[key] for key in sorted(matches)]


# `--user-data-dir` may be given as `--user-data-dir=<path>` (what the generated launcher
# emits). Stopping at whitespace mis-cuts a path containing a space, which is fine: this
# value is only ever a LABEL in the refusal message, never a path anything acts on.
_USER_DATA_DIR_RE = re.compile(r"--user-data-dir=(\S+)")

# `pgrep -fl` prints "<pid> <command line>". Anything else is unparseable and is reported
# as such rather than discarded.
_PGREP_LINE_RE = re.compile(r"\A(\d+)\s+(.*)\Z", re.DOTALL)


def browsers_to_quit(
    matches: Sequence[str], binary_names: Sequence[str] | None = None
) -> list[str]:
    """Human labels for what the operator must actually quit, PIDS INCLUDED.

    Chromium runs dozens of helper processes (renderers, GPU, utility), all carrying the
    binary's name and all identified by a ``--type=`` flag. Quitting a browser takes its
    helpers with it, so only the type-less processes are worth naming.

    The type-less set is NOT all browsers, though. On the owner's machine it also holds
    ``chrome_crashpad_handler`` and the ``app_mode_loader`` PWA shims — processes with no
    window and no ``--user-data-dir``, which the old code labelled "the MAIN Brave
    profile". A shim that outlives the browser then produced a refusal telling the operator
    to quit something that was not running, with nothing to identify it and deliberately no
    ``--force`` to get past it. So each label carries the PID and the executable's name, and
    anything whose executable is not one of the Brave binaries being searched for is called
    what it is: a helper to kill, not a browser to quit.

    *binary_names* are the file names :func:`running_brave_processes` searched for; the
    default is the standard Brave binary alone.
    """
    known = set(binary_names or [Path(core.DEFAULT_BRAVE_BINARY).name])
    labels = []
    for raw in matches:
        parsed = _PGREP_LINE_RE.match(raw.strip())
        if parsed is None:
            # NEVER dropped: an unreadable line is proof a process matched, and the whole
            # point of this guard is that anything it cannot read counts against copying.
            labels.append(
                f"an UNPARSEABLE `pgrep` line — counted as a live process: {raw.strip()!r}"
            )
            continue
        pid, cmdline = parsed.group(1), parsed.group(2)
        if "--type=" in cmdline:  # a helper of some browser, not a browser
            continue
        # The executable is everything before the first option. Splitting on whitespace
        # would cut "…/MacOS/Brave Browser" in half.
        executable = Path(cmdline.split(" --", 1)[0].strip()).name
        if executable not in known:
            labels.append(
                f"pid {pid}  {executable} — a Brave helper/PWA shim, not a browser "
                f"window: quit it in Activity Monitor (or `kill {pid}`)"
            )
            continue
        found = _USER_DATA_DIR_RE.search(cmdline)
        where = found.group(1) if found else "the MAIN Brave profile (no --user-data-dir)"
        labels.append(f"pid {pid}  {executable} — {where}")
    return sorted(set(labels))


# --------------------------------------------------------------------------- #
# Reading the TARGET instance's own launcher
# --------------------------------------------------------------------------- #
# The generated launcher's last statement: `exec '<binary>' \`.
_EXEC_LINE_RE = re.compile(r"^exec\s+(.+?)\s*\\?$", re.MULTILINE)
# `--load-extension=<value>`, where <value> is either an sh-quoted path (sync off) or
# `"$EXTS"` (sync on), the variable being assigned a few lines above. `.+?` and not `\S+?`:
# the quoted path may contain SPACES, and cutting it at the first one would derive an id
# from half a path — i.e. exclude nothing while looking like it excluded something.
_LOAD_EXTENSION_RE = re.compile(r"--load-extension=(.+?)\s*\\?$", re.MULTILINE)


def instance_launchers(instance_dir: str | Path) -> list[Path]:
    """Every launcher script inside *instance_dir* — normally exactly one."""
    return sorted(p for p in Path(instance_dir).glob(_LAUNCHER_GLOB) if p.is_file())


def _first_sh_word(value: str) -> str | None:
    """The first shell word of *value*, unquoted; ``None`` if it does not parse."""
    try:
        words = shlex.split(value)
    except ValueError:
        return None
    return words[0] if words else None


def instance_brave_binaries(instance_dir: str | Path) -> list[str]:
    """The Brave binaries THIS instance's launcher(s) actually exec.

    ``generate --brave-binary`` writes whatever path it is given into the launcher, so an
    instance may be running a Brave that is not :data:`core.DEFAULT_BRAVE_BINARY` — a
    second channel (Beta/Nightly), a Brave in ``~/Applications``, a wrapper script. The
    running-browser guard greps for the default AND for these, so such an instance is
    visible to it instead of silently passing the check while its databases are live.

    Returns ``[]`` when the launcher is missing or unreadable; the caller always keeps the
    default in the set, so a missing launcher degrades to exactly the old behaviour rather
    than to no check at all.
    """
    binaries = []
    for launcher in instance_launchers(instance_dir):
        try:
            text = launcher.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found = _EXEC_LINE_RE.search(text)
        if found is None:
            continue
        binary = _first_sh_word(found.group(1))
        if binary:
            binaries.append(binary)
    return sorted(set(binaries))


def instance_unpacked_load_paths(instance_dir: str | Path) -> list[str]:
    """The ``--load-extension`` paths baked into THIS instance's launcher(s).

    With extension sync ON the launcher reads ``--load-extension="$EXTS"`` and assigns
    ``EXTS='<bundle>'`` above (the main profile's dirs are appended at RUN time, so the
    script itself only carries the shared curator bundle). With sync off the path is
    written straight into the flag. Both forms are handled; anything unexpected yields
    nothing, and the caller falls back on the ``Extensions/`` filter alone.
    """
    paths = []
    for launcher in instance_launchers(instance_dir):
        try:
            text = launcher.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found = _LOAD_EXTENSION_RE.search(text)
        if found is None:
            continue
        value = _first_sh_word(found.group(1))
        if value is None:
            continue
        if value.startswith("$"):  # `"$EXTS"` -> read the assignment it refers to
            name = value.lstrip("$").strip("{}")
            if not name.isidentifier():
                continue
            assigned = re.search(rf"^{re.escape(name)}=(.+)$", text, re.MULTILINE)
            if assigned is None:
                continue
            value = _first_sh_word(assigned.group(1))
            if value is None:
                continue
        # Chromium splits --load-extension on commas; every entry is a load path.
        paths.extend(part for part in value.split(",") if part)
    return sorted(set(paths))


def unpacked_layer_unavailable(instance_dir: str | Path) -> str | None:
    """Why identity layer (b) could NOT be computed for *instance_dir*; ``None`` when it was.

    Layer (b) is the POSITIVE half of the identity guard: it states what this instance's
    own unpacked extension IS, by deriving its id from the ``--load-extension`` path baked
    into the instance's launcher. Every input to that comes from the launcher, so a missing
    launcher, an unreadable one, or one carrying no ``--load-extension`` makes the layer
    yield an EMPTY id set — and an empty exclusion set excludes nothing. The run then goes
    ahead on layer (a) alone (the source's ``Extensions/<id>/<version>/manifest.json``
    filter), which is a real guard but a weaker one, and nothing in the output said so:
    the operator had to infer the degradation from the phrasing of an exclusion reason.

    Degrading is deliberate — refusing the whole copy because a launcher is missing would
    be worse — but degrading SILENTLY is not. This returns the sentence the caller prints.
    """
    if instance_unpacked_load_paths(instance_dir):
        return None
    launchers = instance_launchers(instance_dir)
    if not launchers:
        return (
            f"no launcher script under {Path(instance_dir)} ({_LAUNCHER_GLOB}) — there is "
            "nothing to read a --load-extension path out of"
        )
    problems = []
    for launcher in launchers:
        try:
            text = launcher.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            problems.append(f"{launcher} is unreadable ({type(exc).__name__}: {exc})")
            continue
        if _LOAD_EXTENSION_RE.search(text) is None:
            problems.append(f"{launcher} carries no --load-extension flag")
        else:
            problems.append(f"{launcher}'s --load-extension value did not parse")
    return "; ".join(problems)


def chromium_unpacked_extension_id(load_path: str | Path) -> str:
    """The ``chrome-extension://`` id Chromium derives from an unpacked dir's ABSOLUTE path.

    Chromium hashes the load path with SHA-256, takes the first 16 bytes and maps each
    nibble 0..15 onto ``'a'..'p'`` (``crx_file::id_util::GenerateId``). An extension
    loaded unpacked from a shared directory therefore has the SAME id in every profile and
    no ``Extensions/<id>`` dir anywhere — which is exactly the curator extension, and
    exactly what must never be copied.

    Verified against the real deployment: ``/Users/vvzvlad/Data/Projects/arcextension/dist``
    hashes to ``enhmndaehfanaeinicoffekhbepjhkmf``, the id the owner's browsers show.

    (Store-installed extensions are unaffected: their manifests carry a ``key`` and
    Chromium hashes that instead, which is why they keep their real id in an instance.)
    """
    digest = hashlib.sha256(str(load_path).encode("utf-8")).hexdigest()[:32]
    return "".join(chr(ord("a") + int(nibble, 16)) for nibble in digest)


def instance_unpacked_extension_ids(instance_dir: str | Path) -> list[str]:
    """The ids of every extension THIS instance loads unpacked, derived from its launcher."""
    return sorted(
        {
            chromium_unpacked_extension_id(path)
            for path in instance_unpacked_load_paths(instance_dir)
        }
    )


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #
def is_installed_unpacked(extensions_dir: Path, extension_id: str) -> bool:
    """Does the SOURCE profile really hold an installed copy of *extension_id*?

    THE LAUNCHER'S OWN DEFINITION, deliberately: ``_render_extension_sync`` only loads a
    version dir once it finds ``<version>/manifest.json`` in it, so "installed" means the
    same thing on both sides of this tool. A bare ``Extensions/<id>`` — empty, or half
    removed by a garbage collection that was interrupted — is NOT an installed extension,
    and accepting it would let the identity guard be defeated by a directory anyone can
    create with ``mkdir``.

    SYMLINKS ARE NOT FOLLOWED at any of the three levels. ``Path.is_dir()`` follows them,
    so a symlinked ``Extensions/<id>`` pointing anywhere at all would otherwise satisfy the
    only check standing between the curator's ``chrome.storage.local`` — that instance's
    ``install_uuid`` and per-install secret — and the copy.
    """
    root = extensions_dir / extension_id
    if root.is_symlink() or not root.is_dir():
        return False
    try:
        versions = list(root.iterdir())
    except OSError:
        return False
    for version in versions:
        if version.is_symlink() or not version.is_dir():
            continue
        manifest = version / "manifest.json"
        if not manifest.is_symlink() and manifest.is_file():
            return True
    return False


def stored_state_ids(source_default_dir: str | Path) -> list[str]:
    """Every id with a ``Local Extension Settings/<id>`` directory in the source profile."""
    local = Path(source_default_dir) / LOCAL_SETTINGS_DIRNAME
    if not local.is_dir():
        return []
    return sorted(entry.name for entry in local.iterdir() if entry.is_dir())


def eligible_extension_ids(
    source_default_dir: str | Path, exclude_ids: Sequence[str] = ()
) -> list[str]:
    """Ids whose state may be copied: stored state PLUS a real installed unpacked copy.

    THIS IS THE WHOLE SAFETY FILTER, and it is not about tidiness. Store-installed
    extensions are unpacked by the browser into ``Extensions/<id>/<version>_0/``; the
    CURATOR extension never is — it is loaded unpacked from a shared directory outside any
    profile, so it has no ``Extensions/`` dir in any profile while having the SAME id in
    every one of them (Chromium hashes the shared load path). Its ``chrome.storage.local``
    is where that instance's ``install_uuid`` and its per-install enrollment secret live
    (§6). A blanket copy of every state dir would therefore overwrite the instance's own
    identity with the MAIN browser's, and the service would see the instance as a different
    install — an enrolment silently taken over.

    Two layers, because one thin one is not enough for a guard with this consequence:

    1. **Negative, and the launcher's own test**: :func:`is_installed_unpacked` requires a
       ``<version>/manifest.json`` and follows no symlinks, so an empty or half-removed
       ``Extensions/<id>`` no longer counts as installed.
    2. **Positive**: *exclude_ids* names the ids the TARGET instance loads unpacked,
       derived from its launcher's ``--load-extension`` path
       (:func:`instance_unpacked_extension_ids`). That does not ask what is missing from
       the source — it states what this instance's identity actually IS.

    No id is hard-coded by either layer: any extension loaded unpacked (the curator today,
    a dev build tomorrow) is excluded, and every store-installed one passes.
    """
    source = Path(source_default_dir)
    extensions = source / EXTENSIONS_DIRNAME
    excluded = set(exclude_ids)
    return [
        ext_id
        for ext_id in stored_state_ids(source)
        if ext_id not in excluded and is_installed_unpacked(extensions, ext_id)
    ]


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


def _free_bytes(path: Path) -> int:
    """Free bytes on the filesystem *path* will live on (its nearest existing ancestor)."""
    probe = path
    while not probe.exists():
        if probe.parent == probe:
            return 0
        probe = probe.parent
    return shutil.disk_usage(probe).free


# --------------------------------------------------------------------------- #
# Planning (what `--dry-run` prints and what the copy obeys)
# --------------------------------------------------------------------------- #
def plan_extension_state_copy(
    *,
    source_default_dir: str | Path,
    instance_dir: str | Path,
    only: list[str] | None = None,
) -> StateCopyPlan:
    """Decide everything — paths, eligibility, sizes, disk — without writing a byte.

    Raises :class:`StateCopyRefused` for a bad source, a bad instance dir or an ``--only``
    id that is not eligible. It does NOT check for a running browser and does NOT check
    that the plan fits on disk: those are the copy's refusals, so a dry run can report both
    conditions instead of dying on them.
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

    # The POSITIVE identity layer: what THIS instance loads unpacked, by derived id.
    unpacked_here = instance_unpacked_extension_ids(instance)
    unpacked_paths = instance_unpacked_load_paths(instance)
    eligible = eligible_extension_ids(source, exclude_ids=unpacked_here)
    stored = stored_state_ids(source)
    extensions = source / EXTENSIONS_DIRNAME

    if only is not None:
        _refuse_ineligible_only(
            only=only,
            eligible=eligible,
            stored=stored,
            extensions=extensions,
            unpacked_here=unpacked_here,
            unpacked_paths=unpacked_paths,
            source=source,
        )
        selected_ids = [ext_id for ext_id in eligible if ext_id in only]
    else:
        selected_ids = eligible

    selected = []
    peak_single = 0
    for ext_id in selected_ids:
        parts = _state_dirs(source, ext_id)
        sizes = [_tree_bytes(source / part / ext_id) for part in parts]
        peak_single = max([peak_single, *sizes])
        selected.append(
            PlannedCopy(
                extension_id=ext_id,
                bytes_to_copy=sum(sizes),
                parts=tuple(parts),
                # The other half of the transaction: what the destination holds for this id
                # TODAY, over exactly the dirs this run replaces. Same `_tree_bytes` as the
                # source side, so the two are one comparison and not two measurements.
                bytes_replaced=sum(
                    _tree_bytes(destination / part / ext_id)
                    for part in parts
                    if (destination / part / ext_id).is_dir()
                ),
            )
        )

    layer_note = unpacked_layer_unavailable(instance)
    excluded = tuple(
        (
            ext_id,
            _exclusion_reason(
                ext_id,
                extensions,
                unpacked_here,
                unpacked_paths,
                layer_b_available=layer_note is None,
            ),
        )
        for ext_id in stored
        if ext_id not in selected_ids
    )
    total = sum(item.bytes_to_copy for item in selected)
    return StateCopyPlan(
        source=source,
        destination=destination,
        selected=tuple(selected),
        excluded=excluded,
        stale_staging=tuple(_stale_staging_dirs(destination)),
        brave_binaries=tuple(
            sorted({core.DEFAULT_BRAVE_BINARY, *instance_brave_binaries(instance)})
        ),
        total_bytes=total,
        total_replaced_bytes=sum(item.bytes_replaced for item in selected),
        peak_bytes=total + peak_single,
        free_bytes=_free_bytes(destination),
        unpacked_layer_note=layer_note,
    )


def _exclusion_reason(
    ext_id: str,
    extensions: Path,
    unpacked_here: Sequence[str],
    unpacked_paths: Sequence[str],
    *,
    layer_b_available: bool = True,
) -> str:
    """Why an id with stored state is not being copied — one sentence, no jargon.

    THE IDENTITY WORDING IS RESERVED FOR THE ID THAT IS AN IDENTITY. Every id that lacks
    an ``Extensions/<id>/<version>/manifest.json`` used to be told it was "loaded from
    outside the profile, and its storage is per-install IDENTITY" — true of the curator,
    false of the six Chrome COMPONENT extensions (Web Store, Docs Offline, …) that share
    the shape on the real profile. They were excluded correctly and described wrongly, the
    same conflation :func:`_refuse_ineligible_only` was already split to avoid.

    The two are told apart by layer (b): the ids THIS instance loads unpacked are known by
    derivation, so anything else is a component/foreign extension, not this install's
    identity. When layer (b) is unavailable (*layer_b_available* false — see
    :func:`unpacked_layer_unavailable`) that distinction genuinely cannot be drawn, and the
    reason says so instead of picking one of the two and sounding certain.
    """
    if ext_id in unpacked_here:
        where = ", ".join(unpacked_paths) or "this instance's --load-extension path"
        return (
            "this instance LOADS IT UNPACKED itself "
            f"({where}) — its storage is this install's identity (install_uuid + "
            "enrollment secret), never something to clone"
        )
    if not is_installed_unpacked(extensions, ext_id):
        missing = (
            f"no {EXTENSIONS_DIRNAME}/{ext_id}/<version>/manifest.json in the source "
            "profile, so the browser never installed it there: it is loaded from outside "
            "the profile — a Chrome COMPONENT extension (Web Store, Docs Offline and the "
            "like), a policy-installed one, or an unpacked build"
        )
        if layer_b_available:
            return (
                f"{missing}. Not this instance's identity (layer (b) checked: this "
                "instance does not load it unpacked) — just not something this copy can "
                "verify, so its state stays where it is"
            )
        return (
            f"{missing}. Whether it is a component extension or an unpacked build that IS "
            "an install identity could NOT be determined here — identity layer (b) is "
            "unavailable (see the note above). Excluded either way"
        )
    return "not selected by --only"


def _refuse_ineligible_only(
    *,
    only: Sequence[str],
    eligible: Sequence[str],
    stored: Sequence[str],
    extensions: Path,
    unpacked_here: Sequence[str],
    unpacked_paths: Sequence[str],
    source: Path,
) -> None:
    """Refuse an ``--only`` list naming ids that must not (or cannot) be copied.

    THREE DISTINCT CASES, because collapsing them slanders the operator. A one-character
    typo in Bitwarden's id used to be reported as "its storage holds THIS install's
    identity", i.e. the owner was told he had nearly overwritten his ``install_uuid`` when
    all he did was mistype. The identity wording is reserved for the case that really is
    one, and it is kept verbatim there.
    """
    unknown = [ext_id for ext_id in only if ext_id not in eligible]
    if not unknown:
        return

    lines = []
    for ext_id in unknown:
        has_state = ext_id in stored
        installed = is_installed_unpacked(extensions, ext_id)
        if ext_id in unpacked_here:
            where = ", ".join(unpacked_paths) or "its --load-extension path"
            lines.append(
                f"  {ext_id} — is the extension THIS INSTANCE loads unpacked ({where}). "
                "Its storage holds THIS install's identity: copying it would overwrite "
                "the instance's install_uuid and enrollment secret with the main "
                "browser's."
            )
        elif has_state and not installed:
            lines.append(
                f"  {ext_id} — no {EXTENSIONS_DIRNAME}/<id>/<version>/manifest.json in "
                f"{source}. An extension with stored state but no unpacked directory is "
                "loaded from outside the profile (the curator extension is), and its "
                "storage holds THIS install's identity — copying it would overwrite the "
                "instance's install_uuid and enrollment secret with the main browser's."
            )
        elif installed and not has_state:
            lines.append(
                f"  {ext_id} — is installed in {source} but has no "
                f"{LOCAL_SETTINGS_DIRNAME}/<id> directory: there is NOTHING to copy for "
                "it. Launch the main browser's copy of it once, or drop it from --only."
            )
        else:
            lines.append(
                f"  {ext_id} — is unknown to {source}: no stored state and no "
                f"{EXTENSIONS_DIRNAME}/<id>. That is a TYPO or an id from another "
                "profile, not an identity problem. Run with --dry-run to list the "
                "eligible ids."
            )
    raise StateCopyRefused("refusing --only:\n" + "\n".join(lines))


def _stale_staging_dirs(destination: Path) -> list[Path]:
    """Leftover `.rebuild-*` staging dirs next to either of the two destinations."""
    found = []
    for part in (LOCAL_SETTINGS_DIRNAME, SYNC_SETTINGS_DIRNAME):
        found.extend(core.stale_staging_dirs(destination / part))
    return sorted(found)


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
    (:func:`eligible_extension_ids`) or the whole run is refused, with the reason spelled
    out per id (:func:`_refuse_ineligible_only`). The default is every eligible id.

    Each ``<id>`` directory is REPLACED, not merged: a LevelDB is a set of files that only
    make sense together, and dropping fresh ``.ldb`` files next to a stale ``MANIFEST``
    yields a database that is neither. Replacing means DELETING: whatever this instance had
    stored for that id — its own MetaMask wallet, its own logged-in Bitwarden — is gone,
    with no backup and no undo. :attr:`PlannedCopy.bytes_replaced` sizes that loss per id
    and :attr:`StateCopyPlan.total_replaced_bytes` for the run, so ``--dry-run`` shows both
    halves of the trade instead of only what arrives. The replacement goes through
    :func:`core.replace_tree`, so the copy is staged beside the destination and swapped in
    whole — an interrupted run leaves that id's previous state exactly as it was. Any
    ``.rebuild-*`` staging dir a KILLED earlier run left behind (holding a partial copy of
    the vault) is swept before this one starts.

    Refuses (:class:`StateCopyRefused`) while ANY Brave process is alive, and before that
    if the plan will not fit on the destination's filesystem. The process check runs LAST,
    after every path has been validated, so a typo is reported without demanding the
    browser be quit first — but nothing has been written by then either.

    THE COMMIT IS PER ID, not per run. If the loop dies half-way, the ids it already
    finished are already replaced; that list is printed on stderr before the exception
    propagates, because the previous state of exactly those ids is gone and the operator
    otherwise has no way to know which. An ``OSError``/``shutil.Error`` from the copy is
    converted into :class:`StateCopyRefused` naming the id it died on, so the CLI reports
    it instead of a traceback.

    Returns one :class:`CopiedState` per id, in id order. Read the module docstring for
    what this copy is worth and what it costs: it is a one-time copy, it cannot be a live
    sync, and it puts the encrypted vault (and MetaMask's encrypted seed vault) in one more
    profile on this disk.
    """
    plan = plan_extension_state_copy(
        source_default_dir=source_default_dir, instance_dir=instance_dir, only=only
    )

    if not plan.fits:
        raise StateCopyRefused(
            f"refusing: {_bytes(plan.peak_bytes)} needed at {plan.destination} but only "
            f"{_bytes(plan.free_bytes)} free. The copy lands {_bytes(plan.total_bytes)} "
            "and each directory exists TWICE while it is staged beside its target (the "
            "stage-and-swap that keeps an interrupted copy from destroying what is "
            "there). Free some space, or restrict the run with --only."
        )

    # LAST guard before the first byte is written: these are live LevelDB databases and a
    # snapshot taken under their own writer can be a corrupt one. No --force exists.
    running = running_brave_processes(plan.brave_binaries)
    if running:
        quit_these = browsers_to_quit(
            running, [Path(b).name for b in plan.brave_binaries]
        ) or ["every Brave process"]
        raise StateCopyRefused(
            "refusing: Brave is running ("
            f"{len(running)} process(es)). These are live LevelDB databases — copying one "
            "while its own writer runs can produce a corrupt snapshot. Quit (⌘Q, not just "
            "close the windows):\n  " + "\n  ".join(quit_these) + "\nthen run this again. "
            "There is deliberately no --force."
        )

    # A SIGKILL between staging and swap leaves `.rebuild-XXXX/new/` holding a partial copy
    # of the vault, next to the real one, forever. Nothing else ever removes it.
    for stale in plan.stale_staging:
        shutil.rmtree(stale, ignore_errors=True)

    source = plan.source
    copied: list[CopiedState] = []
    current = ""
    try:
        for item in plan.selected:
            current = item.extension_id
            for part in item.parts:
                src_dir = source / part / current
                core.replace_tree(
                    plan.destination / part / current,
                    lambda staged, src_dir=src_dir: shutil.copytree(src_dir, staged),
                )
            copied.append(
                CopiedState(
                    extension_id=current,
                    bytes_copied=item.bytes_to_copy,
                    parts=item.parts,
                    # Measured BEFORE the swap, by the plan — after it there is nothing
                    # left to measure, which is the point of reporting it.
                    bytes_replaced=item.bytes_replaced,
                )
            )
    except BaseException as exc:
        # Each id is COMMITTED as the loop walks, and the previous state of those ids is
        # already gone. Saying which ones is the difference between a recoverable
        # situation and a half-migrated profile nobody can reason about.
        done = ", ".join(item.extension_id for item in copied) or "(none)"
        print(
            f"copy-state FAILED while copying {current}.\n"
            f"  already copied and NOT revertible: {done}\n"
            f"  untouched: everything after {current} in the plan",
            file=sys.stderr,
        )
        if isinstance(exc, OSError):  # shutil.Error is an OSError subclass
            raise StateCopyRefused(
                f"copy failed on {current} ({type(exc).__name__}: {exc}). Already copied "
                f"(their previous state is gone): {done}. Fix the cause and re-run — the "
                "copy overwrites, so re-running is safe."
            ) from exc
        raise
    return copied


def _bytes(count: int) -> str:
    """A short human size for the refusal messages this module raises."""
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")  # pragma: no cover
