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

from . import core, macos

# The repo's extension bundle, resolved relative to this file (…/tools/instancegen).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_EXTENSION_DIR = _REPO_ROOT / "extension"

# Seconds any single `git` call gets before the stamp is given up on. A build must never
# hang on a wedged git (a stale index.lock, a network-backed worktree).
_GIT_TIMEOUT = 10
# Separator between the three facts in `version_name`. A middle dot reads well on the
# extension card and cannot be confused with the dots inside the version itself.
_STAMP_SEP = " · "


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


def build_stamp(extension_dir: str | Path) -> tuple[str, str] | None:
    """The ``(version, version_name)`` build stamp for a source ``extension/``, or ``None``.

    This is where the ENVIRONMENT is read — git and the clock — deliberately here and not
    in :mod:`core`, which stays pure text + filesystem.

      * ``version``      = ``<major>.<minor>.<commit-count>``, with ``<major>.<minor>``
        taken from the tracked manifest's own ``version`` literal (that literal is the
        BASE and is never rewritten in the repo) and the count from
        ``git rev-list --count HEAD``. Machine-ordered: it advances with every commit,
        which is what ties a loaded bundle to a point in history.
      * ``version_name`` = that version, the short sha with a ``-dirty`` suffix when the
        working tree has uncommitted changes, and the build time. This is the field the
        extensions page displays, and the ``dirty`` marker is the point of it: without one,
        a build from a modified tree is indistinguishable from the committed code.

    A build must NEVER fail over this. No git on PATH, not a git repo, an empty or broken
    repo, a git that hangs — every one of them returns ``None`` (the caller then copies the
    manifest verbatim, exactly as before the stamp existed) with a note on stderr so the
    degrade is visible rather than silent.

    git runs against the repo the SOURCE ``extension/`` lives in (``git -C``), not the
    process CWD: ``make dev-bundle`` may be invoked from anywhere.
    """
    extension_dir = Path(extension_dir).resolve()
    try:
        manifest = json.loads(
            (extension_dir / "manifest.json").read_text(encoding="utf-8")
        )
        base = str(manifest["version"])
        # First two components of the base literal; a shorter base is padded with 0.
        head = (base.split(".") + ["0", "0"])[:2]

        count = int(_git(extension_dir, "rev-list", "--count", "HEAD"))
        sha = _git(extension_dir, "rev-parse", "--short", "HEAD")
        # --porcelain covers staged, unstaged and untracked-but-not-ignored files; dist/
        # is gitignored, so a build never marks itself dirty.
        dirty = bool(_git(extension_dir, "status", "--porcelain"))

        # Clamped, not wrapped: the spec caps a component at 65535 and an over-long
        # history must degrade to a pinned ceiling, never to an unloadable manifest.
        count = max(0, min(count, core.MANIFEST_VERSION_MAX_COMPONENT))
        version = core.validate_manifest_version(f"{head[0]}.{head[1]}.{count}")
        marker = f"{sha}-dirty" if dirty else sha
        stamp = _STAMP_SEP.join(
            [version, marker, datetime.now().strftime("%Y-%m-%d %H:%M")]
        )
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(
            f"note: build stamp unavailable ({type(exc).__name__}: {exc}) — "
            "manifest.json copied verbatim, version/version_name not stamped",
            file=sys.stderr,
        )
        return None
    return version, stamp


def cmd_generate(args: argparse.Namespace) -> int:
    out_root = Path(args.out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    bundle_dir = Path(args.bundle_dir).resolve()
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
    # No extension id is printed: it is Chromium's hash of the shared bundle's load path
    # and nothing consumes it anymore (no origin allow-list, no CORS list to update).
    print(f"  icon (.icns)   : {icns.reason}")
    if args.service_url:
        # The address is NOT stamped anymore — the extension gets it via the enrollment
        # settings UI (§13). Accepted for compatibility with older invocations; noted so
        # the operator does not expect it to be baked in.
        print(f"  note           : --service-url {args.service_url!r} is informational "
              "only; enter the address in the extension settings during enrollment")
    print("  launch: " + shlex.join(result.launch_command))
    return 0


def cmd_bundle(args: argparse.Namespace) -> int:
    """Build the UNIVERSAL extension bundle (§9) — a copy plus the build stamp.

    Like ``generate`` this needs NO token, NO service URL and NO instanceId: with
    enrollment (§7, issue #35) the build is universal — serviceUrl and the per-install
    secret are entered per profile, not baked in. It also needs no signing key: the
    manifest carries no ``key`` and no ``<host>``. NO ``instance.json`` is written.

    The one thing the copy rewrites is the manifest's build IDENTITY — ``version`` and the
    displayed ``version_name`` (:func:`build_stamp`) — so the extension card in
    brave://extensions says which build is loaded and the operator can tell whether
    pressing "Обновить" after ``make dev-bundle`` actually took. That is not
    configuration: two bundles differing only in the stamp behave identically, so any two
    runs are still byte-identical everywhere else and off the same commit differ only in
    ``version_name``'s build time (acc 16). If the stamp cannot be computed the manifest is
    copied verbatim and the build still succeeds.

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
    version, version_name = stamp if stamp else (None, None)

    # Copy the repo bundle into --out (dev cruft + any stray instance.json skipped).
    # NOTE: --out IS the extension bundle root — manifest.json + all code land here and
    # this whole tree ships to Chrome fleet-wide.
    if rebuilt_in_place:
        # Staged build + swap: the path never changes and the dir is never a half-copy
        # (see core.replace_bundle). Files from the previous build that this one does not
        # emit — a renamed hashed chunk, say — are gone, because it is a replace and not
        # a merge.
        core.replace_bundle(
            args.extension_dir, out_dir, version=version, version_name=version_name
        )
    else:
        core.copy_bundle(
            args.extension_dir, out_dir, version=version, version_name=version_name
        )

    if rebuilt_in_place:
        print(f"Rebuilt universal bundle IN PLACE -> {out_dir}")
        print(
            "  Reload the extension on brave://extensions (chrome://extensions) to pick "
            "it up; if the manifest's permissions changed, confirm the new permissions "
            "there too or those capabilities stay silently dead."
        )
    else:
        print(f"Built universal bundle -> {out_dir}")
    if version_name is not None:
        # Printed so the operator can compare it against the extension card AFTER the
        # reload — that comparison is the whole point of the stamp.
        print(f"  version        : {version}  (version_name: {version_name})")
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

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
