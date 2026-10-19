"""Ed25519 keys for the link between the main computer and its machines: sign, verify, show a fingerprint.

Each side holds its own private key and the other's public key. The main computer's key lives in the vault; a
machine's in its own secrets/ folder. Neither is ever sent: only public keys and signatures cross the link.
"""

import base64
import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


def new() -> str:
    """A new private key, as unencrypted PKCS8 PEM text."""
    return Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


def _private(pem: str) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("not an Ed25519 key")
    return key


def public(pem: str) -> str:
    """The public half of private key `pem`, as base64 of its 32 raw bytes."""
    raw = _private(pem).public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def valid_public(text: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(text, validate=True))
    except ValueError:
        return False
    return True


def sign(pem: str, data: bytes) -> str:
    return base64.b64encode(_private(pem).sign(data)).decode()


def verify(public_key: str, data: bytes, signature: str) -> bool:
    """Whether `signature` is `public_key`'s over `data`; False for anything malformed."""
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True)).verify(
            base64.b64decode(signature, validate=True), data)
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True


def fingerprint(public_key: str) -> str:
    """Short, readable fingerprint to compare by eye on both screens: 'A1B2 C3D4 E5F6 0718'."""
    digest = hashlib.sha256(base64.b64decode(public_key)).hexdigest()[:16].upper()
    return " ".join(digest[i:i + 4] for i in range(0, 16, 4))
