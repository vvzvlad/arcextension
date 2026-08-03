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
import shlex
import sys
from pathlib import Path

from . import core, macos

# The repo's extension bundle, resolved relative to this file (…/tools/instancegen).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_EXTENSION_DIR = _REPO_ROOT / "extension"


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
    """Build the UNIVERSAL extension bundle (§9) — a plain copy, nothing stamped.

    Like ``generate`` this needs NO token, NO service URL and NO instanceId: with
    enrollment (§7, issue #35) the build is universal — serviceUrl and the per-install
    secret are entered per profile, not baked in. It also needs no signing key: the
    manifest carries no ``key`` and no ``<host>``, so the whole build is
    ``copy_bundle`` and nothing else. NO ``instance.json`` is written, and any two runs
    are byte-identical because a copy has no inputs to vary (acc 16).
    """
    out_dir = Path(args.out).resolve()
    if out_dir.exists():
        # copy_bundle (shutil.copytree) requires a fresh destination; refusing an
        # existing dir also keeps the byte-identical guarantee honest — each build lands
        # in a clean tree, never merged on top of a previous one.
        raise SystemExit(
            f"{out_dir} already exists — `bundle` writes a fresh dir; remove it or "
            "choose another --out"
        )
    # Copy the repo bundle into --out (dev cruft + any stray instance.json skipped).
    # NOTE: --out IS the extension bundle root — manifest.json + all code land here and
    # this whole tree ships to Chrome fleet-wide.
    core.copy_bundle(args.extension_dir, out_dir)

    print(f"Built universal bundle -> {out_dir}")
    print("  NO instance.json written (universal build — serviceUrl/token are per-profile)")
    print(
        "  The chrome-extension:// id is Chromium's hash of this dir's absolute path, so "
        "it changes if you move or rename it. Nothing depends on it: no origin is checked "
        "on /ext and /api/* CORS accepts any origin."
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
    b.set_defaults(func=cmd_bundle)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
