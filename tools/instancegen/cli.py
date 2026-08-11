"""CLI for the instance generator (§13): ``generate`` and ``bundle``.

Under enrollment (§7/§13, issue #35) the extension build is UNIVERSAL: ``bundle``
produces the ONE bundle the whole fleet loads, and ``generate`` only wraps that shared
bundle in a per-instance ``.app`` + empty profile. Neither bakes in a service address or
a secret — both are entered per profile through the enrollment settings UI — so
``generate`` needs NO token and NO ``instance.json`` (both are gone).

There is no signing key and no ``--key-file`` either. The key existed only to PIN the
``chrome-extension://`` id so one origin could be listed in ``EXT_ALLOWED_ORIGINS``; that
allow-list is gone (``src/api/cors.py`` carries the argument), so nothing reads the id and
letting Chromium derive it from the load path is fine.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import core, macos, state

# The repo's extension bundle, resolved relative to this file (…/tools/instancegen).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_EXTENSION_DIR = _REPO_ROOT / "extension"

# Seconds any single `git` call gets before the stamp is given up on. A build must never
# hang on a wedged git (a stale index.lock, a network-backed worktree).
_GIT_TIMEOUT = 10


def _git(repo_dir: Path, *argv: str) -> str:
    """Run one git command in *repo_dir* and return its stripped stdout.

    Raises on anything that is not a clean success — the single caller turns every failure
    into "no stamp".
    """
    out = subprocess.run(
        ["git", "-C", str(repo_dir), *argv],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
        check=True,
    )
    return out.stdout.strip()


def build_stamp(extension_dir: str | Path) -> tuple[str, str, str] | None:
    """The ``(version, marker, built_at)`` build stamp for a source ``extension/``, or ``None``.

    This is where the ENVIRONMENT is read — git and the clock — deliberately here and not
    in :mod:`core`, which stays pure text + filesystem.

      * ``version``  = ``<major>.<minor>.<commit-count>.<HHMM>``, with ``<major>.<minor>``
        taken from the tracked manifest's own ``version`` literal (that literal is the
        BASE and is never rewritten in the repo), the count from
        ``git rev-list --count HEAD`` and ``<HHMM>`` the build's wall-clock hour and minute
        as ONE integer (20:58 -> ``2058``, 09:30 -> ``930``, 00:05 -> ``5``). The count
        advances with every commit, which ties a loaded bundle to a point in history; the
        minute is what makes two builds of the SAME commit distinguishable, which is the
        entire point of the stamp — the operator compares the extension card against what
        ``make dev-bundle`` just printed. Four integer components, each within the spec's
        0..65535, twelve characters or so: it fits the narrow slot the extensions page
        gives a version.
      * ``marker``   = the short sha, with ``-dirty`` glued to it when the working tree has
        uncommitted changes. This is TERMINAL-ONLY output.
      * ``built_at`` = the same wall clock as ``<HHMM>``, spelled out for a human
        (``%Y-%m-%d %H:%M``). Also TERMINAL-ONLY.

    Only ``version`` reaches the manifest. The sha, the ``-dirty`` marker and the full date
    deliberately do NOT: the extensions page renders the version beside the extension NAME,
    in a slot with room for a version and nothing else, and a longer string wrapped and
    truncated the name itself (see :func:`core.stamp_build_identity`). They are still part
    of the build identity, so :func:`cmd_bundle` prints them — the terminal output of
    ``make dev-bundle`` is where the full identity lives, and the ``-dirty`` marker in
    particular is the only thing that tells a build from a modified tree apart from the
    committed code.

    A build must NEVER fail over this. No git on PATH, not a git repo, an empty or broken
    repo, a shallow clone, a manifest that is not a JSON object, a git that hangs — every
    one of them returns ``None`` (the caller then copies the manifest verbatim, exactly as
    before the stamp existed) with a note on stderr so the degrade is visible rather than
    silent.

    That fallback is not free, and the cost lands where the operator is told to look: an
    unstamped bundle shows the base literal on the extension card ("arcextension 0.1.0"),
    which is indistinguishable from an older build that did not reload — the exact question
    the stamp exists to answer. The stderr note is the ONLY signal that this happened. The
    verbatim copy is kept anyway, deliberately: a build must not fail over its own identity.

    git runs against the repo the SOURCE ``extension/`` lives in (``git -C``), not the
    process CWD: ``make dev-bundle`` may be invoked from anywhere.
    """
    extension_dir = Path(extension_dir).resolve()
    try:
        manifest = json.loads(
            (extension_dir / "manifest.json").read_text(encoding="utf-8")
        )
        # Explicit, because `manifest["version"]` on a list/str/None raises TypeError,
        # which is NOT in the except tuple below: a manifest.json that is valid JSON but
        # not an object would kill the whole build instead of degrading to a verbatim copy.
        if not isinstance(manifest, dict):
            raise ValueError("manifest.json must contain a JSON object")
        base = str(manifest["version"])
        # First two components of the base literal; a shorter base is padded with 0.
        head = (base.split(".") + ["0", "0"])[:2]

        # Confirm THIS repo actually tracks THIS manifest before believing anything it
        # says. `git -C` pins the starting directory but git still walks UP from there, so
        # a source tree unpacked inside an unrelated working tree ($HOME under a dotfiles
        # repo, a TMPDIR inside one) would otherwise get a successful answer from that
        # foreign repo — a confidently wrong stamp with no failure to degrade on. A
        # non-zero exit here is caught below like any other git trouble.
        _git(extension_dir, "ls-files", "--error-unmatch", "--", "manifest.json")
        if _git(extension_dir, "rev-parse", "--is-shallow-repository") == "true":
            # `rev-list --count HEAD` answers 1 on a `--depth 1` clone and exits 0, so
            # nothing fails and the version comes out quietly wrong (0.1.1 for a
            # 131-commit history). actions/checkout defaults to fetch-depth: 1, so a CI
            # bundle build would ship exactly that. Degrade instead.
            raise ValueError(
                "shallow clone: `rev-list --count` is not the real commit count"
            )

        count = int(_git(extension_dir, "rev-list", "--count", "HEAD"))
        sha = _git(extension_dir, "rev-parse", "--short", "HEAD")
        # --porcelain covers staged, unstaged and untracked-but-not-ignored files, but the
        # pathspec limits it to what actually SHIPS in the bundle. With no pathspec any
        # scratch file anywhere in the repo (`??` entries count) would glue `-dirty` onto
        # every build from then on, and the marker would stop telling a modified tree from
        # committed code — which is its entire job.
        dirty = bool(
            _git(extension_dir, "status", "--porcelain", "--", str(extension_dir))
        )

        # Clamped, not wrapped: the spec caps a component at 65535 and an over-long
        # history must degrade to a pinned ceiling, never to an unloadable manifest.
        count = max(0, min(count, core.MANIFEST_VERSION_MAX_COMPONENT))
        # ONE clock read for both the version's `<HHMM>` and the printed date, so the two
        # can never name different minutes across a tick.
        now = datetime.now()
        # str(int), so no component ever carries a leading zero (09:30 -> `930`, which the
        # spec accepts while `0930` it would reject), and 0..2359 is far under 65535.
        minute_of_day = now.hour * 100 + now.minute
        version = core.validate_manifest_version(
            f"{head[0]}.{head[1]}.{count}.{minute_of_day}"
        )
        marker = f"{sha}-dirty" if dirty else sha
        built_at = now.strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(
            f"note: build stamp unavailable ({type(exc).__name__}: {exc}) — "
            "manifest.json copied verbatim, version not stamped",
            file=sys.stderr,
        )
        return None
    return version, marker, built_at


def cmd_generate(args: argparse.Namespace) -> int:
    # Argument checks run BEFORE a single directory is created, same reasoning as
    # `copy_bundle`'s: a refusal after `out_root.mkdir` leaves a half-made output tree
    # behind for the operator to clean up.
    #
    # `~` is expanded and the path made absolute HERE: the launcher is a script run from
    # an arbitrary CWD by launchd, and `~` inside the sh-quoted literal would never expand.
    sync_extensions = args.sync_extensions
    if sync_extensions is not None:
        # An EMPTY value is MISSING configuration, not "the current directory".
        # `Path("").resolve()` is the CWD, so `--sync-extensions "$BRAVE_PROFILE"` with the
        # variable unset would quietly bake the generator's working directory (the repo
        # root, typically) into the launcher. Fail on missing configuration rather than
        # substitute something (AGENTS.md); --no-sync-extensions is how you turn it OFF.
        if not str(sync_extensions):
            raise SystemExit(
                "--sync-extensions got an EMPTY path (an unset shell variable?) — an "
                "empty path resolves to the current directory, which is never the Brave "
                "profile. Pass a real Extensions dir, or --no-sync-extensions to load "
                "only the curator bundle."
            )
        sync_extensions = Path(sync_extensions).expanduser().resolve()

    bundle_dir = Path(args.bundle_dir).resolve()
    # Hoisted above the mkdir for the same reason as the empty-path check: `core` keeps
    # this guard too (it is the library-level one), but there it runs AFTER
    # `out_root.mkdir`, so a comma in either path left an empty output tree behind for the
    # operator to clean up. Refuse before creating anything.
    core.reject_comma_in_load_extension_paths(bundle_dir, sync_extensions)

    out_root = Path(args.out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    title = args.title or args.instance_id

    icon_png = None
    if args.icon:
        icon_png = Path(args.icon).read_bytes()

    result = core.generate_instance(
        out_root=out_root,
        bundle_dir=bundle_dir,
        instance_id=args.instance_id,
        title=title,
        brave_binary=args.brave_binary,
        icon_source_png=icon_png,
        overwrite=args.overwrite,
        sync_extensions_from=sync_extensions,
    )

    icns = macos.build_icns(
        result.paths.icon_png, result.paths.icon_png.with_suffix(".icns")
    )

    p = result.paths
    print(f"Generated instance {args.instance_id!r} -> {p.root}")
    print(f"  shared bundle  : {bundle_dir}  (--load-extension target; NOT copied)")
    print(f"  profile        : {p.profile_dir}  (empty; install_uuid born here)")
    print(f"  app bundle     : {p.app_dir}")
    print(f"  launcher       : {p.launcher}")
    if sync_extensions is not None:
        # STDOUT must not claim a working sync for a path that is not there: the note
        # below goes to stderr, and under `make instance > build.log` this line is the
        # only one kept — it would advertise ~26 extensions an instance will not have.
        # Same shape as cmd_bundle's "NOT stamped (see the note above)".
        if sync_extensions.is_dir():
            print(f"  sync extensions: {sync_extensions}  (re-read at EVERY launch, so "
                  "they keep updating with the main browser)")
        else:
            print(f"  sync extensions: {sync_extensions}  (NOT FOUND — see the note "
                  "below; the curator bundle ALONE until that path appears)")
        print("                   state is NOT copied — they start logged-out "
              "(--no-sync-extensions to skip)")
        if not sync_extensions.is_dir():
            # NOT an error: a machine with no such profile is a legitimate state, and the
            # launcher re-checks the path at every launch, so it starts working the moment
            # that directory appears. But the degrade is otherwise TOTALLY silent — the
            # launcher's `[ -d "$MAIN" ]` guard skips the whole sync and neither it nor the
            # browser says a word, so every synced extension is simply absent. Sync is on
            # by DEFAULT and the default path comes from the generating user's `~`, so a
            # .app generated under `sudo`, or copied to another Mac, lands here. Same
            # degrade-with-a-note pattern as `build_stamp` above.
            print(
                f"note: --sync-extensions {sync_extensions} does not exist or is not a "
                "directory — this instance will launch with the curator bundle ALONE, "
                "with none of the main profile's extensions. Not fatal: the launcher "
                "re-checks that path at every launch.",
                file=sys.stderr,
            )
    # No extension id is printed: it is Chromium's hash of the shared bundle's load path
    # and nothing consumes it anymore (no origin allow-list, no CORS list to update).
    print(f"  icon (.icns)   : {icns.reason}")
    if args.service_url:
        # The address is NOT stamped anymore — the extension gets it via the enrollment
        # settings UI (§13). Accepted for compatibility with older invocations; noted so
        # the operator does not expect it to be baked in.
        print(f"  note           : --service-url {args.service_url!r} is informational "
              "only; enter the address in the extension settings during enrollment")
    # The LAUNCHER is printed, never a reconstructed argv. With extension sync on, the
    # `--load-extension` value is resolved AT LAUNCH into the bundle plus one dir per
    # main-profile extension, so a reconstructed argv would print a single path that the
    # instance does not actually run with — and an operator debugging "why is Bitwarden
    # missing here" would copy that line, get a browser without Bitwarden and conclude the
    # opposite of the truth. One source of truth for the argv: the script itself.
    print(f"  launch: {shlex.quote(str(p.launcher))}   (this script IS the argv; "
          "with sync on, --load-extension is resolved inside it at launch)")
    return 0


def cmd_bundle(args: argparse.Namespace) -> int:
    """Build the UNIVERSAL extension bundle (§9) — a copy plus the build stamp.

    Like ``generate`` this needs NO token, NO service URL and NO instanceId: with
    enrollment (§7, issue #35) the build is universal — serviceUrl and the per-install
    secret are entered per profile, not baked in. It also needs no signing key: the
    manifest carries no ``key`` and no ``<host>``. NO ``instance.json`` is written.

    The one thing the copy rewrites is the manifest's build IDENTITY — ``version``, and
    nothing else (:func:`build_stamp`) — so the extension card in brave://extensions says
    which build is loaded and the operator can tell whether pressing "Обновить" after
    ``make dev-bundle`` actually took. That is not configuration: two bundles differing
    only in the stamp behave identically, so any two runs are still byte-identical
    everywhere else and off the same commit differ only in ``version``'s trailing build
    minute (acc 16). If the stamp cannot be computed the manifest is copied verbatim and
    the build still succeeds.

    An existing ``--out`` is refused unless ``--force``, which rebuilds THAT SAME PATH via
    :func:`core.replace_bundle` (staged copy + swap). In place is the only correct way to
    refresh a bundle a browser already loads unpacked: the ``chrome-extension://`` id is
    the hash of the load path, so a different path is a different extension.
    """
    out_dir = Path(args.out).resolve()
    rebuilt_in_place = out_dir.exists()
    if rebuilt_in_place and not args.force:
        # copy_bundle (shutil.copytree) requires a fresh destination; refusing an
        # existing dir also keeps the byte-identical guarantee honest — each build lands
        # in a clean tree, never merged on top of a previous one.
        raise SystemExit(
            f"{out_dir} already exists — `bundle` writes a fresh dir; remove it or "
            "choose another --out, or pass --force to rebuild THIS dir in place. "
            "In place is what you want for a dir a browser already loads: the "
            "chrome-extension:// id is the hash of this path, so a new path means a new "
            "id, a new origin and an empty chrome.storage.local (enrolment lost)."
        )
    # None on any git/environment trouble -> a verbatim copy, never a failed build.
    stamp = build_stamp(args.extension_dir)
    # Only `version` is stamped into the manifest; `marker`/`built_at` are printed below.
    version = stamp[0] if stamp else None

    # Copy the repo bundle into --out (dev cruft + any stray instance.json skipped).
    # NOTE: --out IS the extension bundle root — manifest.json + all code land here and
    # this whole tree ships to Chrome fleet-wide.
    if rebuilt_in_place:
        # Staged build + swap: the path never changes and the dir is never a half-copy
        # (see core.replace_bundle). Files from the previous build that this one does not
        # emit — a renamed hashed chunk, say — are gone, because it is a replace and not
        # a merge.
        core.replace_bundle(args.extension_dir, out_dir, version=version)
    else:
        core.copy_bundle(args.extension_dir, out_dir, version=version)

    if rebuilt_in_place:
        print(f"Rebuilt universal bundle IN PLACE -> {out_dir}")
        print(
            "  Reload the extension on brave://extensions (chrome://extensions) to pick "
            "it up; if the manifest's permissions changed, confirm the new permissions "
            "there too or those capabilities stay silently dead."
        )
    else:
        print(f"Built universal bundle -> {out_dir}")
    if stamp is not None:
        version, marker, built_at = stamp
        # The version is printed so the operator can compare it against the extension card
        # AFTER the reload — that comparison is the whole point of the stamp, and the card
        # shows exactly this string (the manifest carries no version_name to display
        # instead). The sha, the `-dirty` marker and the full date do NOT fit that card,
        # so the terminal is the only place they are reported: `-dirty` is what tells a
        # build from a modified tree apart from the committed code.
        print(f"  version        : {version}   <- this is what the extension card shows")
        print(f"  build          : commit {marker}, built {built_at}")
    else:
        print("  version        : NOT stamped (see the note above) — manifest copied verbatim")
    print("  NO instance.json written (universal build — serviceUrl/token are per-profile)")
    print(
        "  The chrome-extension:// id is Chromium's hash of this dir's absolute path, so "
        "it changes if you move or rename it. The SERVICE does not care: no origin is "
        "checked on /ext and /api/* CORS accepts any origin. The BROWSER does — a new id "
        "is a new origin with an empty chrome.storage.local, i.e. a re-enrolment. Rebuild "
        "in place (--force) instead of moving the dir."
    )
    return 0


def _human_bytes(count: int) -> str:
    """*count* as a short human size — the operator is comparing 7 MB against 16 KB."""
    if count < 1024:
        return f"{count} B"
    for unit in ("KB", "MB", "GB"):
        count /= 1024
        if count < 1024 or unit == "GB":
            return f"{count:.1f} {unit}"
    raise AssertionError("unreachable")  # pragma: no cover


# The paragraph every run ends with — the things that are not reversible by an undo and
# must therefore be stated before the operator discovers them.
#
# The DIVERGENCE claim is deliberately split in two, because the earlier text asserted
# both halves as one fact ("logging out here does not log the others out"). The LOCAL half
# is certain: a LevelDB has one writer, the two profiles hold two independent copies, and a
# vault entry added in one is invisible to the other. The SERVER half is not the same
# claim. The Bitwarden storage carries an `appId` — the device identifier the server binds
# a device and its refresh token to — and it was CONFIRMED present in the owner's real
# `Local Extension Settings/nngceckbapebfimnlniiiahkandclblb` before this text was written,
# so after the copy both profiles present the SAME device identity. What that does to a
# "log out" / "deauthorize sessions" / device-approval action was NOT tested (it would take
# a live account), so the consequence is named as unverified rather than asserted either
# way. Do not re-collapse these two into one sentence.
_COPY_TERMS = (
    "Every id listed above is REPLACED WHOLE. What this instance had stored for that "
    "extension — its\nown wallet, its own logged-in vault, its own settings — is DELETED, "
    "not merged with what arrives.\nThe last column above is how much, per id and in "
    "total; there is no backup and no undo.\n"
    "This is a ONE-TIME COPY, not a sync. Two browsers cannot share one LevelDB (single "
    "writer,\nlock-protected — a symlink would only make the instance see broken "
    "storage), so the two profiles\nhold two independent copies from now on: a vault "
    "entry added in one does not appear in the other.\nRe-run this to re-align them (it "
    "overwrites, it does not merge).\n"
    "The copied Bitwarden storage carries its `appId` (checked: it is there), the device "
    "identifier the\nserver ties a device and its refresh token to — so server-side the "
    "two profiles are now ONE\ndevice. What that does to a logout, a session revoke or a "
    "device-approval prompt in one is\nNOT VERIFIED: it was not tested. Assume they are "
    "one device until you have checked.\n"
    "The account and the ENCRYPTED VAULT came along, so the full login (email + master "
    "password +\n2FA) is not needed again. Whether the vault comes up UNLOCKED is your "
    "Bitwarden vault-timeout\nsetting's business: with «Never» + «Lock» the derived key is "
    "persisted and it should; otherwise\nyou are asked for the master password once.\n"
    "Bitwarden is NOT the only extension this moves. MetaMask "
    "(nkbihfbeogaeaoehlefnkodbefgpgknn) is\nstore-installed and therefore eligible too, "
    "and its chrome.storage.local holds the wallet's\nENCRYPTED SEED VAULT — use --only if "
    "you want the password manager without the wallet.\n"
    "So the encrypted vault(s) now exist in this instance's profile TOO — one more copy on "
    "this disk,\nalongside the main profile and every other instance you run this for."
)


def _print_plan_rows(rows, *, done: bool = False) -> None:
    """The `<id>  <size>  Local+Sync  replaces …` table, shared by dry run and real run.

    THE LAST COLUMN IS THE ONE THAT WAS MISSING. The table used to size only what ARRIVES,
    while the destination is not empty: on the owner's real `infra` instance those same 21
    ids already hold 95.9 MB of that instance's OWN state (measured), including a 19.4 MB
    MetaMask seed vault and a 7.3 MB Bitwarden vault, and every byte of it is deleted by
    the run. "Overwrites" in a prose paragraph is not that number. So each row says what it
    destroys — the figure grows every time the instance is used — and a row
    that destroys a wallet or a vault says it in a line of its own — the difference between
    "the instance gets my main wallet" and "the instance's own wallet is deleted" is
    exactly the decision being taken here.

    Written in the present tense for a plan and the past tense for a finished run (*done*),
    because "will be deleted" and "has been deleted" are not the same message to read.
    """
    for ext_id, size, parts, replaced in rows:
        # Which of the two dirs is involved, spelled short: `Local` is chrome.storage.local,
        # `Sync` is chrome.storage.sync (often absent, and then simply not listed).
        short = "+".join(part.split()[0] for part in parts)
        verb = "DELETED" if done else "DELETES"
        verdict = f"{verb} {_human_bytes(replaced)}" if replaced else "replaced nothing"
        print(f"  {ext_id}  {_human_bytes(size):>9}  {short:<10}  {verdict}")
        secret = state.secret_store_label(ext_id)
        if replaced and secret:
            was = "WAS DELETED" if done else "IS DELETED"
            hint = (
                "" if done else
                " Drop this id from --only if you meant to keep it."
            )
            print(f"      ^^^ THIS INSTANCE'S OWN {secret} ({_human_bytes(replaced)}) "
                  f"{was} and replaced by the main profile's. Not merged, not backed up, "
                  f"no undo.{hint}")


def _print_destroy_total(replaced_total: int, rows, *, done: bool = False) -> None:
    """The run's destruction total, named as deletion — the counterpart of `total:`."""
    destroying = [(ext_id, replaced) for ext_id, _s, _p, replaced in rows if replaced]
    label = "DELETED" if done else "DELETES"
    if not destroying:
        print(f"  {label}: nothing — every destination directory was empty or absent")
        return
    vaults = [ext_id for ext_id, _ in destroying if state.secret_store_label(ext_id)]
    tail = f", {len(vaults)} of them a wallet/vault" if vaults else ""
    held = "held" if done else "holds RIGHT NOW"
    print(f"  {label}: {_human_bytes(replaced_total)} of state this instance {held} "
          f"across {len(destroying)} of those id(s){tail} — irrecoverably replaced, "
          "not merged")


def _print_layer_b_note(note: str | None) -> None:
    """Say it out loud when the positive identity layer went no-op, and why.

    The guard has two layers: (a) the source must really have the extension installed
    unpacked, (b) the id THIS instance loads unpacked is excluded by derivation from its
    launcher. Layer (b) needs the launcher; without one it yields no ids, excludes nothing
    and the run proceeds on layer (a) alone. That degradation was previously invisible —
    the output looked identical to a healthy run. Printed now, so a weaker guard is READ
    rather than inferred from the phrasing of an exclusion reason.
    """
    if note is None:
        return
    print("\nNOTE: identity guard layer (b) UNAVAILABLE — the unpacked-id exclusion could "
          f"not be computed.\n  cause: {note}\n"
          "  Only layer (a) is in force (the source's Extensions/<id>/<version>/"
          "manifest.json filter).\n"
          "  An extension this instance loads unpacked would NOT be excluded by name. If "
          "this instance\n  is a real generated one, its launcher is missing or damaged — "
          "regenerate it before copying.")


def _cmd_copy_state_dry_run(args: argparse.Namespace, only: list[str] | None) -> int:
    """List what a real run WOULD copy and what it would skip, touching nothing.

    This exists because the warnings below used to print only AFTER ~130 MB of encrypted
    vault had already been written into a second profile — i.e. the operator learned what
    the command does at the one moment he could no longer decide not to. A dry run answers
    "which ids, how big, what is excluded and why" first.

    It deliberately does NOT refuse on a running browser or on a full disk: both are the
    real run's refusals, and a dry run that dies on them cannot do its job (Brave is
    running precisely when the operator is deciding whether to quit it). Both are REPORTED
    instead.
    """
    plan = state.plan_extension_state_copy(
        source_default_dir=args.source,
        instance_dir=args.instance_dir,
        only=only,
    )
    print("DRY RUN — nothing was read into, written to or deleted from any profile.")
    print(f"Would copy extension state: {plan.source}")
    print(f"                         -> {plan.destination}")
    rows = [
        (item.extension_id, item.bytes_to_copy, item.parts, item.bytes_replaced)
        for item in plan.selected
    ]
    _print_plan_rows(rows)
    if not plan.selected:
        print("  (nothing eligible — see the exclusions below)")
    print(f"  total: {_human_bytes(plan.total_bytes)} across "
          f"{len(plan.selected)} extension(s)")
    _print_destroy_total(plan.total_replaced_bytes, rows)
    print(f"  disk : {_human_bytes(plan.peak_bytes)} needed at peak (the copy plus the "
          f"one tree being staged), {_human_bytes(plan.free_bytes)} free"
          f"{'' if plan.fits else '  <-- DOES NOT FIT; the real run would refuse'}")
    _print_layer_b_note(plan.unpacked_layer_note)
    if plan.excluded:
        print(f"\nExcluded ({len(plan.excluded)} id(s) that have stored state):")
        for ext_id, reason in plan.excluded:
            print(f"  {ext_id}\n      {reason}")
    if plan.stale_staging:
        print(f"\nStale staging dirs a real run would sweep ({len(plan.stale_staging)}):")
        for path in plan.stale_staging:
            print(f"  {path}")
    try:
        running = state.running_brave_processes(plan.brave_binaries)
    except state.StateCopyRefused as exc:
        print(f"\nNOTE: the real run would REFUSE — {exc}")
    else:
        if running:
            quit_these = state.browsers_to_quit(
                running, [Path(b).name for b in plan.brave_binaries]
            )
            print(f"\nNOTE: the real run would REFUSE — Brave is running "
                  f"({len(running)} process(es)). Quit:")
            for label in quit_these or ["every Brave process"]:
                print(f"  {label}")
    print("\n" + _COPY_TERMS)
    return 0


def cmd_copy_state(args: argparse.Namespace) -> int:
    """Copy the store-installed extensions' STATE from the main profile into an instance.

    This is the answer to "can the state come across too" — and the answer is "once, by
    copy". :mod:`.state` carries the full argument; the things the operator must be told
    are printed below every run (``_COPY_TERMS``), because they are not reversible by an
    undo. ``--dry-run`` prints the same terms plus the plan, and writes nothing.
    """
    only = None
    if args.only is not None:
        only = [part.strip() for part in args.only.split(",") if part.strip()]
        if not only:
            # An EMPTY value is missing configuration, not "all of them" — same reasoning
            # as `generate`'s empty --sync-extensions check (AGENTS.md).
            raise SystemExit(
                "--only got an empty id list (an unset shell variable?) — pass real "
                "extension ids, or drop --only to copy every eligible extension."
            )

    try:
        if args.dry_run:
            return _cmd_copy_state_dry_run(args, only)
        copied = state.copy_extension_state(
            source_default_dir=args.source,
            instance_dir=args.instance_dir,
            only=only,
        )
    except state.StateCopyRefused as exc:
        # A refusal is an expected outcome, not a crash: exit non-zero with the message
        # (SystemExit prints it on stderr and exits 1) instead of a traceback.
        raise SystemExit(str(exc)) from exc

    source = Path(args.source).expanduser().resolve()
    profile = Path(args.instance_dir).expanduser().resolve() / "profile"
    print(f"Copied extension state: {source}")
    print(f"                     -> {profile}")
    rows = [
        (item.extension_id, item.bytes_copied, item.parts, item.bytes_replaced)
        for item in copied
    ]
    _print_plan_rows(rows, done=True)
    if not copied:
        print("  (nothing eligible — no extension has both stored state and an installed "
              f"{state.EXTENSIONS_DIRNAME}/<id>/<version>/manifest.json in that profile; "
              "run again with --dry-run to see why each id was skipped)")
    print(f"  total: {_human_bytes(sum(i.bytes_copied for i in copied))} across "
          f"{len(copied)} extension(s)")
    # Past tense here: these directories are already gone. Reported all the same — the
    # operator has to know what this instance no longer has, not only what it gained.
    _print_destroy_total(sum(i.bytes_replaced for i in copied), rows, done=True)
    # Re-read rather than threaded through `copy_extension_state`: the function is pure and
    # cheap (it parses the same launcher the plan did), and the copy's return type stays
    # the list of what was copied.
    _print_layer_b_note(state.unpacked_layer_unavailable(
        Path(args.instance_dir).expanduser()
    ))
    print("\n" + _COPY_TERMS)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="generate_instance",
        description="Build the universal bundle and per-instance Brave .apps (§13).",
        # allow_abbrev=False everywhere, and not as a style choice: with the default
        # prefix matching an operator's typo could silently resolve to a longer option;
        # `bundle` in particular must keep rejecting --token/--token-file exactly (a
        # secret must never sit in argv).
        allow_abbrev=False,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser(
        "generate",
        help="wrap the shared bundle in a per-instance .app + empty profile",
        allow_abbrev=False,
    )
    g.add_argument("--instance-id", required=True, help="names the .app/profile (§13)")
    g.add_argument("--title", default=None, help="display title (default: instanceId)")
    # OPTIONAL and NOT stamped: the address is entered per profile via the enrollment
    # settings UI (§13). Accepted only for compatibility with older invocations.
    g.add_argument(
        "--service-url",
        default=None,
        help="informational only (the address is entered during enrollment, not baked in)",
    )
    g.add_argument(
        "--bundle-dir",
        required=True,
        help="the SHARED universal bundle built by `instancegen bundle` "
        "(--load-extension target; every instance loads this same dir)",
    )
    g.add_argument("--out", required=True, help="output root for instances")
    g.add_argument("--icon", default=None, help="PNG icon source (default: generated)")
    g.add_argument(
        "--brave-binary", default=core.DEFAULT_BRAVE_BINARY, help="system Brave path"
    )
    g.add_argument(
        "--overwrite",
        action="store_true",
        help="rebuild an existing instance's .app (the profile is KEPT)",
    )
    # ON by default: an instance with the curator bundle and NONE of the owner's 26 other
    # extensions is not a usable browser. Both options write the same dest, so the last
    # one on the command line wins; argparse takes the default from the FIRST action that
    # declares one, hence SUPPRESS on the opt-out.
    g.add_argument(
        "--sync-extensions",
        dest="sync_extensions",
        default=core.DEFAULT_MAIN_EXTENSIONS_DIR,
        metavar="PATH",
        help="the MAIN Brave profile's Extensions dir, also loaded unpacked (default: "
        f"{core.DEFAULT_MAIN_EXTENSIONS_DIR}). Re-read at every launch, so the instance "
        "follows the main browser's updates; extension STATE is not copied",
    )
    g.add_argument(
        "--no-sync-extensions",
        dest="sync_extensions",
        action="store_const",
        const=None,
        default=argparse.SUPPRESS,
        help="load ONLY the curator bundle (no main-profile extensions)",
    )
    g.set_defaults(func=cmd_generate)

    b = sub.add_parser(
        "bundle",
        help="build the UNIVERSAL extension bundle (no token/url/instanceId/key)",
        allow_abbrev=False,
    )
    b.add_argument("--out", required=True, help="output dir for the universal bundle")
    # Deliberately NO --token/--token-file, --service-url or --instance-id: the universal
    # build bakes in none of them (§9) — they are per-profile settings under enrollment.
    # And deliberately NO --key-file: there is no signing key anymore (see the module
    # docstring), so the id is the load-path hash and nothing reads it.
    b.add_argument(
        "--extension-dir",
        default=str(_DEFAULT_EXTENSION_DIR),
        help="source extension bundle (default: repo extension/)",
    )
    # --force does NOT mean "clobber": it means "rebuild THIS path", which is the only
    # way to refresh a dir a browser already loads unpacked (the chrome-extension:// id
    # is that path's hash). The swap is staged, so an interrupted rebuild cannot leave a
    # half-copied, unloadable bundle behind (core.replace_bundle).
    b.add_argument(
        "--force",
        action="store_true",
        help="rebuild an existing --out IN PLACE (same path, so the same "
        "chrome-extension:// id); the swap is staged, never a half-written dir",
    )
    b.set_defaults(func=cmd_bundle)

    s = sub.add_parser(
        "copy-state",
        help="copy the main profile's extension STATE into an instance (one-time copy)",
        allow_abbrev=False,
    )
    s.add_argument(
        "--instance-dir",
        required=True,
        help="an instance root generated by `generate` (its --user-data-dir is the "
        "`profile` dir inside it)",
    )
    s.add_argument(
        "--from",
        dest="source",
        default=state.DEFAULT_MAIN_PROFILE_DIR,
        metavar="PATH",
        help="the MAIN Brave profile's `Default` dir, read-only (default: "
        f"{state.DEFAULT_MAIN_PROFILE_DIR})",
    )
    s.add_argument(
        "--only",
        default=None,
        metavar="ID,ID",
        help="restrict the copy to these extension ids (default: every eligible one)",
    )
    # NOT the opposite of --force. It writes nothing at all, which is why it is safe to
    # run while Brave is up — and running it first is the point: the terms of this copy
    # are otherwise only printed once ~130 MB of encrypted vault has already moved.
    s.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="list what WOULD be copied (ids, sizes, total) and what is excluded and "
        "why, then exit without touching anything",
    )
    # Deliberately NO --force: the running-browser check guards live LevelDB databases,
    # and a snapshot taken under their own writer can be corrupt. Quitting Brave is the
    # only way past it.
    s.set_defaults(func=cmd_copy_state)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
