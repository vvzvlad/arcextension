"""CLI for the instance generator (§13): ``generate`` and ``restamp``.

Secrets are never defaulted in code (AGENTS.md): the token comes from ``$EXT_TOKEN``
or a ``--token-file`` PATH, and a missing token fails. There is deliberately NO
``--token`` option — see `_resolve_token`. The signing key is generated/persisted
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


def _resolve_token(token_file: str | None) -> str:
    """The EXT_TOKEN, from ``$EXT_TOKEN`` or the file named by ``--token-file``.

    argv is NOT a token source, by design: a command line is world-readable in `ps`
    output for the whole run and is recorded verbatim in the shell history of every
    operator who ever rotates a token. ``--token-file`` carries a PATH — the secret
    itself stays in a file (or the env), never in argv. Nothing is defaulted: a
    missing token fails loudly (AGENTS.md).
    """
    if token_file:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    else:
        token = os.environ.get("EXT_TOKEN", "")
    if not token:
        raise SystemExit(
            "no token: set EXT_TOKEN in the environment (e.g. `EXT_TOKEN=… make "
            "instance`, with the assignment BEFORE the command so it does not land "
            "in argv) or pass --token-file PATH"
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
    token = _resolve_token(args.token_file)
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
    token = _resolve_token(args.token_file)
    # The code refresh is ON by default (§13): the bundle is duplicated per instance
    # and protocolVersion is compared by exact equality, so a rotation that left the
    # copies on old code would reject every instance on hello — with the failure
    # visible only in the status bar. --no-code-update is the explicit opt-out.
    source = None if args.no_code_update else args.extension_dir
    # Accumulated as each instance lands. A pre-flight makes a mid-apply failure rare,
    # but an I/O error can still stop the run partway — and the operator has usually
    # already rotated EXT_TOKEN on the service by then. Printing nothing would leave
    # them guessing which instances hold which token; that list is the difference
    # between a two-minute fix and a hunt.
    applied: list[core.RestampChange] = []
    try:
        changes = core.restamp_all(
            out_root,
            token=token,
            service_url=args.service_url,
            source_extension_dir=source,
            on_change=applied.append,
        )
    except Exception as exc:
        print(f"FAILED after {len(applied)} instance(s): {exc}", file=sys.stderr)
        if applied:
            print("These instances ALREADY carry the NEW token:", file=sys.stderr)
            for ch in applied:
                print(f"  {ch.instance_id}  [{ch.instance_json}]", file=sys.stderr)
            print(
                "Every other instance still holds the OLD token. Fix the cause and "
                "re-run the same command — re-stamping an already-rotated instance is "
                "idempotent.",
                file=sys.stderr,
            )
        else:
            print("No instance was modified.", file=sys.stderr)
        # SystemExit, not a re-raise: the operator needs the list above as the LAST
        # thing on screen, not buried under a traceback. The message already names the
        # offending file and what to do.
        raise SystemExit(1) from exc

    print(f"Re-stamped {len(changes)} instance(s) under {out_root}:")
    for ch in changes:
        extra = f", serviceUrl -> {ch.new_service_url}" if ch.new_service_url else ""
        code = "code refreshed" if ch.code_updated else "code UNCHANGED"
        print(
            f"  {ch.instance_id}: token {ch.old_token_masked} -> (new){extra}, "
            f"{code} [{ch.instance_json}]"
        )
    if source:
        print(f"Extension code copied from {source} (pinned key/profile preserved).")
    else:
        print(
            "WARNING: --no-code-update — the copies keep their old code. A bumped "
            "PROTOCOL_VERSION will be rejected on hello (§13)."
        )
    print("Restart each browser so the SW re-reads instance.json and reconnects.")
    return 0


def cmd_bundle(args: argparse.Namespace) -> int:
    """Build a UNIVERSAL, key-pinned extension bundle (§9).

    Unlike ``generate``/``restamp`` this needs NO token, NO service URL and NO
    instanceId: with enrollment (§7, issue #35) the build is universal — serviceUrl and
    the per-install secret are entered per profile, not baked in. It only copies the
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
        description="Generate/re-stamp per-instance Brave browsers (§13).",
        # allow_abbrev=False everywhere, and not as a style choice: with the default
        # prefix matching, `--token SECRET` silently resolves to the `--token-file`
        # option — so the removed argv path would quietly come back, putting the
        # secret in argv (and then failing with a confusing "no such file" instead of
        # telling the operator what they just did).
        allow_abbrev=False,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="create a new instance", allow_abbrev=False)
    g.add_argument("--instance-id", required=True, help="immutable instanceId (§13)")
    g.add_argument("--title", default=None, help="display title (default: instanceId)")
    g.add_argument("--service-url", required=True, help="e.g. wss://host")
    # No --token: a secret must never sit in argv (ps output, shell history).
    g.add_argument(
        "--token-file", default=None, help="file holding EXT_TOKEN (or set $EXT_TOKEN)"
    )
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
    g.add_argument(
        "--overwrite",
        action="store_true",
        help="rebuild an existing instance's bundle (the profile is KEPT)",
    )
    g.set_defaults(func=cmd_generate)

    r = sub.add_parser(
        "restamp",
        help="rotate the token AND refresh the code across ALL instances",
        allow_abbrev=False,
    )
    r.add_argument("--out", required=True, help="output root holding the instances")
    # No --token here either — same reason as `generate`.
    r.add_argument(
        "--token-file",
        default=None,
        help="file holding the new EXT_TOKEN (or set $EXT_TOKEN)",
    )
    r.add_argument("--service-url", default=None, help="optionally also change serviceUrl")
    r.add_argument(
        "--extension-dir",
        default=str(_DEFAULT_EXTENSION_DIR),
        help="source bundle whose code is copied into every instance "
        "(default: repo extension/)",
    )
    r.add_argument(
        "--no-code-update",
        action="store_true",
        help="rotate config only, leaving each copy's code as-is (§13: unsafe after "
        "a PROTOCOL_VERSION bump)",
    )
    r.set_defaults(func=cmd_restamp)

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
