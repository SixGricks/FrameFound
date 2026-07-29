"""Time-based one-time passwords (RFC 6238).

Enrolment is two-step on purpose: a secret is issued, and 2FA only becomes
active once the user proves their authenticator produces a valid code. That
prevents locking someone out of their own account with a mis-scanned QR.
"""

import secrets

import pyotp

# One step of drift each way absorbs clock skew without meaningfully widening
# the window an intercepted code stays usable.
VALID_WINDOW = 1


def new_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, email: str, issuer: str) -> str:
    """otpauth:// URI for authenticator apps (rendered as a QR by the UI)."""
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)


def verify(secret: str, code: str) -> bool:
    cleaned = code.strip().replace(" ", "")
    if not cleaned.isdigit() or len(cleaned) != 6:
        return False
    return bool(pyotp.TOTP(secret).verify(cleaned, valid_window=VALID_WINDOW))


def new_recovery_codes(count: int = 8) -> list[str]:
    """Single-use codes for a lost authenticator."""
    return [f"{secrets.token_hex(2)}-{secrets.token_hex(2)}" for _ in range(count)]
