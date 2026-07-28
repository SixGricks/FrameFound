"""Login rate limiting with escalating lockouts.

In-process implementation: correct for the single api container the compose
stack runs. TODO(m7): back with Redis if the api service is ever scaled
horizontally, and revisit as part of the remote-access hardening checklist.
"""

import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    failures: int = 0
    window_start: float = 0.0
    locked_until: float = 0.0


@dataclass
class LoginRateLimiter:
    """Escalating lockout: free attempts, then exponentially growing locks.

    After `max_attempts` failures inside `window_seconds`, the key locks for
    `base_lock_seconds`, doubling on each further failure up to
    `max_lock_seconds`. Success clears the bucket.
    """

    max_attempts: int = 5
    window_seconds: float = 900.0
    base_lock_seconds: float = 30.0
    max_lock_seconds: float = 3600.0
    _buckets: dict[str, _Bucket] = field(default_factory=dict)

    def retry_after(self, key: str) -> float:
        """Seconds until the key may try again; 0 means allowed now."""
        bucket = self._buckets.get(key)
        if bucket is None:
            return 0.0
        return max(0.0, bucket.locked_until - time.monotonic())

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        bucket = self._buckets.setdefault(key, _Bucket(window_start=now))
        if now - bucket.window_start > self.window_seconds:
            bucket.failures = 0
            bucket.window_start = now
        bucket.failures += 1
        overflow = bucket.failures - self.max_attempts
        if overflow >= 0:
            lock = min(self.base_lock_seconds * (2**overflow), self.max_lock_seconds)
            bucket.locked_until = now + lock

    def record_success(self, key: str) -> None:
        self._buckets.pop(key, None)
