"""Ed25519 signing of control commands (docs/CONTROL.md, "Signing").

The signature covers the canonical JSON of the command: keys sorted at every level, no spaces
after separators, UTF-8 with non-ASCII characters written as themselves. Signatures travel as
standard base64 of the 64 raw bytes, and public keys as `ed25519:<base64 of the 32 raw bytes>`.
The private key is a PKCS8 PEM file that only its owner may read on POSIX.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import stat as statmod
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (Ed25519PrivateKey,
                                                                Ed25519PublicKey)

PUBLIC_PREFIX = "ed25519:"


class SigningError(Exception):
    """A key file or key string is unusable. The message never holds key material."""


def canonical_json(command: Mapping[str, Any]) -> bytes:
    """The exact bytes that are signed."""
    return json.dumps(command, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def public_key_string(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    public = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return PUBLIC_PREFIX + base64.b64encode(raw).decode("ascii")


def parse_public_key(text: str) -> Ed25519PublicKey:
    if not isinstance(text, str) or not text.startswith(PUBLIC_PREFIX):
        raise SigningError(f"a public key must look like {PUBLIC_PREFIX}<base64>")
    try:
        raw = base64.b64decode(text[len(PUBLIC_PREFIX):], validate=True)
        return Ed25519PublicKey.from_public_bytes(raw)
    except (binascii.Error, ValueError) as err:
        raise SigningError("the public key is not a valid Ed25519 key") from err


def private_from_seed(seed: bytes) -> Ed25519PrivateKey:
    """Build a key from 32 raw bytes. For test vectors; real keys come from generate_key."""
    return Ed25519PrivateKey.from_private_bytes(seed)


def sign_command(key: Ed25519PrivateKey, command: Mapping[str, Any]) -> str:
    return base64.b64encode(key.sign(canonical_json(command))).decode("ascii")


def verify_command(public: str | Ed25519PublicKey, command: Mapping[str, Any],
                   signature: str) -> bool:
    """True only when the signature is valid for exactly this command. Never raises on bad input."""
    try:
        key = public if isinstance(public, Ed25519PublicKey) else parse_public_key(public)
        sig = base64.b64decode(signature, validate=True)
        key.verify(sig, canonical_json(command))
    except (SigningError, binascii.Error, ValueError, TypeError, InvalidSignature):
        return False
    return True


def generate_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def write_key_file(path: str | Path, key: Ed25519PrivateKey) -> None:
    """Write the private key to a new file, mode 0600. Refuses to overwrite an existing file."""
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as err:
        raise SigningError(f"{path} already exists; it is not overwritten") from err
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
    except BaseException:
        # Do not leave a partial key behind; the file was created by this call.
        try:
            os.unlink(str(path))
        except OSError:
            pass
        raise


def keygen(path: str | Path) -> str:
    """Create a key pair, store the private key at path, and return the public key string."""
    key = generate_key()
    write_key_file(path, key)
    return public_key_string(key)


def load_private_key(path: str | Path, *, posix: bool | None = None,
                     stat: Callable[[str], Any] = os.stat) -> Ed25519PrivateKey:
    """Load the private key. On POSIX a file readable by group or others is refused.

    `posix` and `stat` exist so the permission rule can be tested on any platform.
    """
    check = (os.name == "posix") if posix is None else posix
    p = str(path)
    try:
        st = stat(p)
    except OSError as err:
        raise SigningError(f"cannot read the signing key file {p}: {err.strerror}") from err
    if check and statmod.S_IMODE(st.st_mode) & 0o077:
        raise SigningError(f"the signing key file {p} is readable by group or others "
                           f"(mode {statmod.S_IMODE(st.st_mode):04o}); run chmod 600 on it")
    try:
        loaded = serialization.load_pem_private_key(Path(p).read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as err:
        raise SigningError(f"the signing key file {p} is not an unencrypted PEM key") from err
    if not isinstance(loaded, Ed25519PrivateKey):
        raise SigningError(f"the signing key file {p} is not an Ed25519 key")
    return loaded
