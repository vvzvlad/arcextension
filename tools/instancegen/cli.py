"""CLI for the instance generator (§13): ``generate`` and ``restamp``.

Secrets are never defaulted in code (AGENTS.md): the token comes from ``--token``
or ``$EXT_TOKEN`` and a missing token fails. The signing key is generated/persisted
under ``<out>/.instancegen/`` (or supplied via ``--key-file``) — never hardcoded.
"""

from __future__ import annotations

import argparse
import os
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


def _resolve_token(arg_token: str | None) -> str:
    token = arg_token if arg_token is not None else os.environ.get("EXT_TOKEN")
    if not token:
        raise SystemExit(
            "no token: pass --token or set EXT_TOKEN (never defaulted in code)"
        )
    return token


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


def cmd_generate(args: argparse.Namespace) -> int:
    out_root = Path(args.out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    _warn_if_inside_git_repo(out_root)
    token = _resolve_token(args.token)
    key_b64, ext_id = _resolve_key(out_root, args.key_file)
    title = args.title or args.instance_id

    icon_png = None
    if args.icon:
        icon_png = Path(args.icon).read_bytes()

    result = core.generate_instance(
        out_root=out_root,
        source_extension_dir=args.extension_dir,
        instance_id=args.instance_id,
        title=title,
        service_url=args.service_url,
        token=token,
        key_b64=key_b64,
        extension_id=ext_id,
        brave_binary=args.brave_binary,
        icon_source_png=icon_png,
        allow_execute_js=args.allow_execute_js,
        overwrite=args.overwrite,
    )

    icns = macos.build_icns(
        result.paths.icon_png, result.paths.icon_png.with_suffix(".icns")
    )

    p = result.paths
    print(f"Generated instance {args.instance_id!r} -> {p.root}")
    print(f"  extension copy : {p.extension_dir}")
    print(f"  profile        : {p.profile_dir}  (empty; install_uuid born here)")
    print(f"  app bundle     : {p.app_dir}")
    print(f"  launcher       : {p.launcher}")
    print(f"  extension id   : {ext_id}")
    print(f"  origin (for EXT_ALLOWED_ORIGINS): chrome-extension://{ext_id}")
    print(f"  icon (.icns)   : {icns.reason}")
    print("  launch: " + shlex.join(result.launch_command))
    return 0


def cmd_restamp(args: argparse.Namespace) -> int:
    out_root = Path(args.out).resolve()
    token = _resolve_token(args.token)
    changes = core.restamp_all(out_root, token=token, service_url=args.service_url)
    print(f"Re-stamped {len(changes)} instance(s) under {out_root}:")
    for ch in changes:
        extra = f", serviceUrl -> {ch.new_service_url}" if ch.new_service_url else ""
        print(
            f"  {ch.instance_id}: token {ch.old_token_masked} -> (new){extra} "
            f"[{ch.instance_json}]"
        )
    print("Restart each browser so the SW re-reads instance.json and reconnects.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="generate_instance",
        description="Generate/re-stamp per-instance Brave browsers (§13).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="create a new instance")
    g.add_argument("--instance-id", required=True, help="immutable instanceId (§13)")
    g.add_argument("--title", default=None, help="display title (default: instanceId)")
    g.add_argument("--service-url", required=True, help="e.g. wss://host")
    g.add_argument("--token", default=None, help="EXT_TOKEN (or set $EXT_TOKEN)")
    g.add_argument("--out", required=True, help="output root for instances")
    g.add_argument(
        "--extension-dir",
        default=str(_DEFAULT_EXTENSION_DIR),
        help="source extension bundle (default: repo extension/)",
    )
    g.add_argument("--key-file", default=None, help="PEM private key or base64 pubkey")
    g.add_argument("--icon", default=None, help="PNG icon source (default: generated)")
    g.add_argument(
        "--brave-binary", default=core.DEFAULT_BRAVE_BINARY, help="system Brave path"
    )
    g.add_argument("--allow-execute-js", action="store_true", help="set the default OFF")
    g.add_argument("--overwrite", action="store_true", help="replace an existing dir")
    g.set_defaults(func=cmd_generate)

    r = sub.add_parser("restamp", help="rotate the token across ALL instances")
    r.add_argument("--out", required=True, help="output root holding the instances")
    r.add_argument("--token", default=None, help="new EXT_TOKEN (or set $EXT_TOKEN)")
    r.add_argument("--service-url", default=None, help="optionally also change serviceUrl")
    r.set_defaults(func=cmd_restamp)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
