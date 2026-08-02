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
"""
import logging

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from api.config import settings

_log = logging.getLogger(__name__)


def _fernet():
    key = settings.credentials_key
    if not key:
        return None
    from cryptography.fernet import Fernet
    return Fernet(key.encode() if isinstance(key, str) else key)


def seal(plaintext: str) -> str:
    f = _fernet()
    if f is None:
        _log.warning("CREDENTIALS_KEY not set — storing credential as plaintext")
        return plaintext
    return f.encrypt(plaintext.encode()).decode()


def open_(ciphertext: str) -> str:
    f = _fernet()
    if f is None:
        return ciphertext
    try:
        return f.decrypt(ciphertext.encode()).decode()
    except Exception as exc:
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
