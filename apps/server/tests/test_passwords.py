from framefound.auth.passwords import hash_password, needs_rehash, verify_password


def test_hash_and_verify_roundtrip() -> None:
    hashed = hash_password("correct horse battery staple")
    assert hashed != "correct horse battery staple"
    assert hashed.startswith("$argon2id$")
    assert verify_password(hashed, "correct horse battery staple")


def test_wrong_password_rejected() -> None:
    hashed = hash_password("right-password")
    assert not verify_password(hashed, "wrong-password")
    assert not verify_password(hashed, "")


def test_fresh_hash_needs_no_rehash() -> None:
    assert not needs_rehash(hash_password("some-password"))
