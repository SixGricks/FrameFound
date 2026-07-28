from framefound.auth.ratelimit import LoginRateLimiter


def make_limiter() -> LoginRateLimiter:
    return LoginRateLimiter(max_attempts=3, base_lock_seconds=10, max_lock_seconds=100)


def test_attempts_below_threshold_are_free() -> None:
    limiter = make_limiter()
    limiter.record_failure("k")
    limiter.record_failure("k")
    assert limiter.retry_after("k") == 0.0


def test_lock_engages_at_threshold() -> None:
    limiter = make_limiter()
    for _ in range(3):
        limiter.record_failure("k")
    assert 0 < limiter.retry_after("k") <= 10


def test_lock_escalates_and_caps() -> None:
    limiter = make_limiter()
    for _ in range(3):
        limiter.record_failure("k")
    first = limiter.retry_after("k")
    limiter.record_failure("k")
    second = limiter.retry_after("k")
    assert second > first
    for _ in range(10):
        limiter.record_failure("k")
    assert limiter.retry_after("k") <= 100


def test_success_clears_bucket() -> None:
    limiter = make_limiter()
    for _ in range(5):
        limiter.record_failure("k")
    limiter.record_success("k")
    assert limiter.retry_after("k") == 0.0


def test_keys_are_independent() -> None:
    limiter = make_limiter()
    for _ in range(5):
        limiter.record_failure("attacker")
    assert limiter.retry_after("legit-user") == 0.0
