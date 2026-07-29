"""Encryption for secrets stored in the database.

TOTP seeds and DNS API tokens live in Postgres, so a database leak alone must
not hand an attacker working credentials. Both are sealed with a key derived
from FRAMEFOUND_SECRET_KEY, which lives only in the environment — an attacker
needs the dump *and* the host configuration.

Rotating FRAMEFOUND_SECRET_KEY therefore invalidates stored secrets: users
re-enrol 2FA and DNS tokens are re-entered. That is documented, not accidental.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from framefound.config import get_settings


class SecretUnavailable(RuntimeError):
    """The stored value cannot be decrypted with the current secret key."""


def _cipher() -> Fernet:
    secret = get_settings().secret_key
    if not secret:
        raise SecretUnavailable("Server secret key is not configured")
    # Fernet needs 32 url-safe base64 bytes; the configured secret is arbitrary.
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def seal(plaintext: str) -> str:
    return _cipher().encrypt(plaintext.encode()).decode()


def unseal(ciphertext: str) -> str:
    try:
        return _cipher().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, ValueError) as err:
        raise SecretUnavailable(
            "Stored secret cannot be read; it was encrypted with a different key"
        ) from err
