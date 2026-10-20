"""
App-level symmetric encryption for sensitive columns (OAuth tokens, etc.).

Usage:
    from api.crypto import Encrypted   # SQLAlchemy TypeDecorator — transparent
    token_blob = Column(Encrypted)

Key management:
    Generate a key once:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    Store in .env:        CREDENTIALS_KEY=<key>
    Rotating the key invalidates all stored tokens — just re-auth.

If CREDENTIALS_KEY is not set, values are stored as plaintext with a warning.
This keeps dev/test environments working without secrets management.

A stored value is never silently misread: with a key set, a value that is not valid
ciphertext for it raises ValueError (rotated key, tampering, or a plaintext row saved
before the key existed); with NO key set, a value that is structurally a Fernet token
also raises (the key was removed) instead of being handed to a publisher as if it were
the credential.
"""
import base64
import binascii
import logging
import re

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from api.config import settings

_log = logging.getLogger(__name__)

_B64URL_RE = re.compile(r"[A-Za-z0-9_-]+={0,2}")
# version(1) + timestamp(8) + IV(16) + >=1 AES block(16) + HMAC(32)
_FERNET_MIN_BYTES = 73


def _looks_like_fernet_token(value: str) -> bool:
    """True if `value` is structurally a Fernet token: strict urlsafe-base64, version byte
    0x80, long enough, and a whole number of 16-byte AES blocks of ciphertext. Deliberately
    strict — it only decides which error to raise, and a real OAuth access token (ya29...,
    EAA...) must never be mistaken for one."""
    if not _B64URL_RE.fullmatch(value):
        return False
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        return False
    return len(raw) >= _FERNET_MIN_BYTES and raw[0] == 0x80 and (len(raw) - 57) % 16 == 0


def _fernet():
    key = settings.credentials_key
    if not key:
        return None
    from cryptography.fernet import Fernet
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except ValueError as exc:
        raise ValueError(
            "CREDENTIALS_KEY is not a valid Fernet key — it must be 32 url-safe "
            "base64-encoded bytes. Generate one with: "
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        ) from exc


def seal(plaintext: str) -> str:
    f = _fernet()
    if f is None:
        _log.warning("CREDENTIALS_KEY not set — storing credential as plaintext")
        return plaintext
    return f.encrypt(plaintext.encode()).decode()


def open_(ciphertext: str) -> str:
    f = _fernet()
    if f is None:
        if _looks_like_fernet_token(ciphertext):
            raise ValueError(
                "A stored credential is encrypted but CREDENTIALS_KEY is not set — "
                "restore the key, or re-authenticate to replace the stored token."
            )
        return ciphertext
    try:
        return f.decrypt(ciphertext.encode()).decode()
    except Exception as exc:
        if not _looks_like_fernet_token(ciphertext):
            raise ValueError(
                "A stored credential is not encrypted — it was saved while CREDENTIALS_KEY "
                "was unset. Re-authenticate to store it encrypted."
            ) from exc
        raise ValueError(
            "Failed to decrypt credential — CREDENTIALS_KEY may have been rotated. "
            "Re-authenticate to refresh the stored token."
        ) from exc


class Encrypted(TypeDecorator):
    """Transparently encrypts/decrypts column values using Fernet symmetric encryption."""
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return seal(value) if value is not None else value

    def process_result_value(self, value, dialect):
        return open_(value) if value is not None else value
