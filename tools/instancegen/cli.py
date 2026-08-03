"""CLI for the instance generator (§13): ``generate`` and ``bundle``.

Under enrollment (§7/§13, issue #35) the extension build is UNIVERSAL: ``bundle``
produces the ONE key-pinned bundle the whole fleet loads, and ``generate`` only wraps
that shared bundle in a per-instance ``.app`` + empty profile. Neither bakes in a
service address or a secret — both are entered per profile through the enrollment
settings UI — so ``generate`` needs NO token and NO ``instance.json`` (both are gone).
The signing key that pins the fleet-wide id is generated/persisted under
``.instancegen/`` (or supplied via ``bundle --key-file``) — never hardcoded.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from . import core, keys, macos


def _warn_if_inside_git_repo(out_root: Path) -> None:
    """Warn (stderr) if *out_root* sits inside a git working tree: the 0600 signing
    key under ``<out>/.instancegen/`` is a per-deployment secret and must not be
    committed. ``.gitignore`` also covers ``.instancegen/``, but a loud warning is
    the belt to that suspenders."""
    for parent in [out_root, *out_root.parents]:
        if (parent / ".git").exists():
            print(
                f"WARNING: --out {out_root} is inside a git repo ({parent}); the "
                f"signing key under {out_root / _KEY_SUBDIR}/ is a secret — do NOT "
                "commit it (see .gitignore for .instancegen/).",
                file=sys.stderr,
            )
            return

# Where the persistent signing key lives, relative to the output root. Keeping it
# in the output tree is what makes the extension id stable across instances and
# re-stamps. It is a SECRET — never commit it (the .example manifest ships a
# placeholder; real keys live only in the operator's output dir).
_KEY_SUBDIR = ".instancegen"
_KEY_FILENAME = "signing_key.pem"

# The repo's extension bundle, resolved relative to this file (…/tools/instancegen).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_EXTENSION_DIR = _REPO_ROOT / "extension"


def _resolve_key(out_root: Path, key_file: str | None) -> tuple[str, str]:
    """Return (base64 manifest key, derived extension id).

    Uses ``--key-file`` if given (a PEM private key or a raw base64 public key),
    else generates/reuses ``<out>/.instancegen/signing_key.pem``.
    """
    if key_file:
        raw = Path(key_file).read_bytes()
        if b"-----BEGIN" in raw:
            key_b64 = keys.public_key_b64_from_pem(raw)
        else:
            key_b64 = raw.decode("ascii").strip()
    else:
        key_path = out_root / _KEY_SUBDIR / _KEY_FILENAME
        pem = keys.load_or_create_private_key_pem(key_path)
        key_b64 = keys.public_key_b64_from_pem(pem)
    return key_b64, keys.derive_extension_id(key_b64)


def _bundle_extension_id(bundle_dir: Path) -> str | None:
    """The pinned ``chrome-extension://`` id of the SHARED bundle, for the printout.

    Derived from the ``key`` already pinned in the shared bundle's manifest (by
    ``instancegen bundle``); ``None`` if the bundle still carries the placeholder key
    (not yet pinned) OR the manifest is unreadable/malformed. This is a best-effort
    convenience printout run AFTER the instance is already created, so a broken manifest
    must NOT crash it with a bare traceback — the instance itself is valid, and the id
    can be recovered by pinning the shared bundle's key. ``generate`` then prints a hint
    instead of the id.
    """
    try:
        manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
        key_b64 = str(manifest.get("key") or "")
    except (OSError, ValueError):
        return None
    if not key_b64 or key_b64 == core.KEY_PLACEHOLDER:
        return None
    return keys.derive_extension_id(key_b64)


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
    ext_id = _bundle_extension_id(bundle_dir)
    print(f"Generated instance {args.instance_id!r} -> {p.root}")
    print(f"  shared bundle  : {bundle_dir}  (--load-extension target; NOT copied)")
    print(f"  profile        : {p.profile_dir}  (empty; install_uuid born here)")
    print(f"  app bundle     : {p.app_dir}")
    print(f"  launcher       : {p.launcher}")
    if ext_id is not None:
        print(f"  extension id   : {ext_id}")
        print(f"  origin (for EXT_ALLOWED_ORIGINS): chrome-extension://{ext_id}")
    else:
        print("  extension id   : (bundle key is the placeholder — pin it via "
              "`instancegen bundle --key-file`)")
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
    """Build a UNIVERSAL, key-pinned extension bundle (§9).

    Like ``generate`` this needs NO token, NO service URL and NO instanceId: with
    enrollment (§7, issue #35) the build is universal — serviceUrl and the per-install
    secret are entered per profile, not baked in. It only copies the
    repo ``extension/`` into ``--out`` and pins the manifest ``key`` (the one
    ``chrome-extension://`` id for the whole fleet — predpos. 19). NO ``instance.json``
    is written. Two runs with the same ``--key-file`` are byte-identical (acc 16).
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
    # 1. Copy the repo bundle into --out (dev cruft + any stray instance.json skipped).
    #    NOTE: --out IS the extension bundle root — manifest.json + all code land here and
    #    this whole tree ships to Chrome fleet-wide.
    core.copy_bundle(args.extension_dir, out_dir)
    # 2. Resolve the signing key. SECURITY: a GENERATED private key must NEVER land inside
    #    out_dir. The key pins the single fleet-wide chrome-extension:// id (predpos. 19);
    #    if it shipped inside the distributed bundle, an attacker could rebuild a spoofed
    #    extension under the SAME id and defeat EXT_ALLOWED_ORIGINS. So the key store lives
    #    in a `.instancegen` SIBLING of the bundle (out_dir.parent), symmetric with
    #    `generate` — whose key store is likewise a sibling of the per-instance bundles,
    #    never inside one. With --key-file the caller supplies the key; nothing is written.
    key_root = out_dir.parent
    if not args.key_file:
        _warn_if_inside_git_repo(key_root)
    key_b64, ext_id = _resolve_key(key_root, args.key_file)
    # 3. Pin the key with a deterministic, hostless stamp (no <host>, <all_urls> kept).
    core.stamp_bundle_manifest(out_dir / "manifest.json", key_b64)

    print(f"Built universal bundle -> {out_dir}")
    print(f"  manifest key pinned : extension id {ext_id}")
    print(f"  origin (for EXT_ALLOWED_ORIGINS): chrome-extension://{ext_id}")
    print("  NO instance.json written (universal build — serviceUrl/token are per-profile)")
    if not args.key_file:
        key_path = key_root / _KEY_SUBDIR / _KEY_FILENAME
        print(f"  signing key (SECRET) written BESIDE the bundle: {key_path}")
        print(
            "  For a REPRODUCIBLE fleet-wide id (same chrome-extension:// id across "
            "rebuilds) pass --key-file. Without it the id is derived from the key stored "
            "beside the bundle — kept OUTSIDE the distributed tree, never shipped in it."
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
        help="build a UNIVERSAL, key-pinned extension bundle (no token/url/instanceId)",
        allow_abbrev=False,
    )
    b.add_argument("--out", required=True, help="output dir for the universal bundle")
    # Deliberately NO --token/--token-file, --service-url or --instance-id: the universal
    # build bakes in none of them (§9) — they are per-profile settings under enrollment.
    b.add_argument(
        "--extension-dir",
        default=str(_DEFAULT_EXTENSION_DIR),
        help="source extension bundle (default: repo extension/)",
    )
    b.add_argument(
        "--key-file",
        default=None,
        help="PEM private key or base64 pubkey. REQUIRED for a REPRODUCIBLE fleet-wide "
        "id (same chrome-extension:// id across rebuilds). Without it a key is generated "
        "in a .instancegen sibling BESIDE --out (never inside the distributed bundle — "
        "it is a secret that pins the fleet id).",
    )
    b.set_defaults(func=cmd_bundle)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
