"""Password hashing with argon2id (the current OWASP recommendation)."""

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

# Library defaults are argon2id with sane cost parameters; pin explicitly so a
# library upgrade can't silently weaken hashing.
_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)

MIN_PASSWORD_LENGTH = 10


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)
