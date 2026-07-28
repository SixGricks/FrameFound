"""Signed short-lived media URLs (ADR-0012).

HMAC-SHA256 over (asset_id, kind, expiry) with the server secret. Signed URLs
let <img>/<video> tags, share links, and the future Premiere panel fetch
media without a session cookie — while every byte stays authorization-gated.
Tokens are self-contained: no server-side state, revocation = short expiry.
"""

import hashlib
import hmac
import time
import uuid


class SigningError(ValueError):
    """Signature invalid, expired, or malformed. Message safe to display."""


def _digest(secret: str, asset_id: uuid.UUID, kind: str, expires_epoch: int) -> str:
    message = f"{asset_id}:{kind}:{expires_epoch}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def sign_media_url(
    secret: str, asset_id: uuid.UUID, kind: str, ttl_seconds: int = 3600
) -> tuple[int, str]:
    """Return (expires_epoch, signature) for a media URL."""
    if not secret:
        raise SigningError("Server secret key is not configured")
    expires = int(time.time()) + ttl_seconds
    return expires, _digest(secret, asset_id, kind, expires)


def verify_media_signature(
    secret: str, asset_id: uuid.UUID, kind: str, expires_epoch: int, signature: str
) -> None:
    """Raise SigningError unless the signature is valid and unexpired."""
    if not secret:
        raise SigningError("Server secret key is not configured")
    if time.time() > expires_epoch:
        raise SigningError("This media link has expired")
    expected = _digest(secret, asset_id, kind, expires_epoch)
    if not hmac.compare_digest(expected, signature):
        raise SigningError("Invalid media link")
