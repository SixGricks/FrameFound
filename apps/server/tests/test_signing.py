import time
import uuid

import pytest

from framefound.media.signing import SigningError, sign_media_url, verify_media_signature

SECRET = "unit-test-secret"
ASSET = uuid.uuid4()


def test_roundtrip() -> None:
    expires, sig = sign_media_url(SECRET, ASSET, "proxy", ttl_seconds=60)
    verify_media_signature(SECRET, ASSET, "proxy", expires, sig)  # no raise


def test_expired_rejected() -> None:
    expires, sig = sign_media_url(SECRET, ASSET, "proxy", ttl_seconds=60)
    with pytest.raises(SigningError, match="expired"):
        verify_media_signature(SECRET, ASSET, "proxy", int(time.time()) - 5, sig)


def test_tampered_signature_rejected() -> None:
    expires, sig = sign_media_url(SECRET, ASSET, "proxy", ttl_seconds=60)
    with pytest.raises(SigningError, match="Invalid"):
        verify_media_signature(SECRET, ASSET, "proxy", expires, sig[:-4] + "beef")


def test_kind_is_scoped() -> None:
    expires, sig = sign_media_url(SECRET, ASSET, "thumbnail", ttl_seconds=60)
    with pytest.raises(SigningError):
        verify_media_signature(SECRET, ASSET, "proxy", expires, sig)


def test_asset_is_scoped() -> None:
    expires, sig = sign_media_url(SECRET, ASSET, "proxy", ttl_seconds=60)
    with pytest.raises(SigningError):
        verify_media_signature(SECRET, uuid.uuid4(), "proxy", expires, sig)


def test_missing_secret_refuses_to_sign() -> None:
    with pytest.raises(SigningError, match="secret"):
        sign_media_url("", ASSET, "proxy")
