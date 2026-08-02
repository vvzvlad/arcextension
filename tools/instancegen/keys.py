"""Signing key for the extension `key` manifest field (§13, arch row 21).

The unpacked extension id is, by default, a hash of the LOAD PATH — so it moves
when the bundle is renamed/relocated and every instance would get a DIFFERENT id
(and a different `chrome-extension://` origin), breaking `EXT_ALLOWED_ORIGINS`
and CORS. Stamping a `key` (a base64 SPKI-DER RSA public key) into the manifest
pins the id to the KEY instead of the path, so it is stable across rename AND
identical across every instance/re-stamp (they share one id/origin; instances are
distinguished by `instanceId` + `installUuid`, not by id — one origin entry covers
all).

The key is generated ONCE and reused. We persist the PRIVATE key (PEM) in the
output tree so re-stamps and later instances reuse it (the manifest only needs the
public half; the private half is kept for future .crx packing/signing). NEVER
commit it and never hardcode a key in the repo — it is a per-deployment secret
that lives in the operator's output dir.

`cryptography` is imported lazily so the pure `core` (which merely stamps a
provided key string) does not drag in the dependency.
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from .core import write_private_bytes

# A placeholder id -> chars mapping: the Chromium id is 32 chars over 'a'..'p'.
_ID_ALPHABET_BASE = ord("a")

# RSA-2048 matches the historical Chrome extension key size; the id is derived
# from the public key regardless of size, but 2048 is what real .crx keys use.
_RSA_BITS = 2048


def load_or_create_private_key_pem(path: str | os.PathLike[str]) -> bytes:
    """Return the PEM bytes of the signing private key at *path*.

    Generated (RSA-2048) and persisted with 0600 perms on first use, reused
    thereafter — this is what makes the extension id stable across instances and
    re-stamps. The parent dir is created 0700.
    """
    p = Path(path)
    if p.exists():
        return p.read_bytes()

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=_RSA_BITS)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Write private-key material 0600 (owner-only): it is a per-deployment secret.
    #
    # Atomic + durable + owner-only, via the shared writer. A PARTIAL key file is worse
    # here than anywhere else: the `p.exists()` check above would reuse a truncated PEM
    # forever, and the pinned extension id — hence every instance's chrome-extension://
    # origin — would be lost permanently. Because the destination only ever appears via
    # a rename of a complete, fsync'd file, a crash or power loss simply means "no key
    # yet" and the next run generates one cleanly.
    write_private_bytes(p, pem)
    return pem


def public_key_b64_from_pem(private_pem: bytes) -> str:
    """Return the base64 SPKI-DER of the public half — the manifest `key` value."""
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(private_pem, password=None)
    der = key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return base64.b64encode(der).decode("ascii")


def derive_extension_id(key_b64: str) -> str:
    """Derive the 32-char Chromium extension id from a base64 SPKI-DER `key`.

    Chromium takes SHA-256 of the DER public key, keeps the first 16 bytes and
    maps each nibble 0..15 to 'a'..'p' (`components/crx_file/id_util.cc`).
    """
    der = base64.b64decode(key_b64)
    digest = hashlib.sha256(der).digest()[:16]
    out = []
    for byte in digest:
        out.append(chr(_ID_ALPHABET_BASE + (byte >> 4)))
        out.append(chr(_ID_ALPHABET_BASE + (byte & 0x0F)))
    return "".join(out)
