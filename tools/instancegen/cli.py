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


def cmd_copy_state(args: argparse.Namespace) -> int:
    """Copy the store-installed extensions' STATE from the main profile into an instance.

    This is the answer to "can the state come across too" — and the answer is "once, by
    copy". :mod:`.state` carries the full argument; the three things the operator must be
    told are printed below every run, because they are not reversible by an undo:

    * it is a ONE-TIME COPY and cannot be a live sync (a LevelDB has a single writer, so
      two browsers cannot share one directory and a symlink only breaks the second one) —
      from here on the profiles diverge;
    * it removes the full login but not necessarily the unlock: whether the vault comes up
      unlocked is the vault-timeout setting's business, not this tool's;
    * the encrypted vault now sits in one more profile on this disk.
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
    for item in copied:
        # Which of the two dirs came along, spelled short: `Local` is chrome.storage.local,
        # `Sync` is chrome.storage.sync (often absent, and then simply not listed).
        parts = "+".join(part.split()[0] for part in item.parts)
        print(f"  {item.extension_id}  {_human_bytes(item.bytes_copied):>9}  {parts}")
    if not copied:
        print("  (nothing eligible — no extension has both stored state and an "
              f"{state.EXTENSIONS_DIRNAME}/<id> dir in that profile)")
    print(f"  total: {_human_bytes(sum(i.bytes_copied for i in copied))} across "
          f"{len(copied)} extension(s)")
    print(
        "\nThis is a ONE-TIME COPY, not a sync. Two browsers cannot share one LevelDB "
        "(single writer,\nlock-protected — a symlink would only make the instance see "
        "broken storage), so the profiles\nDIVERGE from now on: logging out here does not "
        "log the others out, and a vault change in one\ndoes not propagate. Re-run this to "
        "re-align them (it overwrites, it does not merge).\n"
        "The account and the ENCRYPTED VAULT came along, so the full login (email + master "
        "password +\n2FA) is not needed again. Whether the vault comes up UNLOCKED is your "
        "Bitwarden vault-timeout\nsetting's business: with «Never» + «Lock» the derived key "
        "is persisted and it should; otherwise\nyou are asked for the master password once."
        "\nThe encrypted vault now exists in this instance's profile TOO — one more copy on "
        "this disk,\nalongside the main profile and every other instance you run this for."
    )
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
