from framefound.scanner.stability import FileObservation, is_file_stable, looks_at_rest

NOW = 1_000_000.0


def obs(size: int = 100, mtime: float = NOW - 60, at: float = NOW, readable: bool = True):
    return FileObservation(
        size_bytes=size, mtime_epoch=mtime, observed_at_epoch=at, readable=readable
    )


def test_settled_file_is_stable() -> None:
    assert is_file_stable(obs(at=NOW - 5), obs())


def test_growing_file_is_unstable() -> None:
    assert not is_file_stable(obs(size=100, at=NOW - 5), obs(size=200))


def test_touched_file_is_unstable() -> None:
    assert not is_file_stable(obs(mtime=NOW - 60, at=NOW - 5), obs(mtime=NOW - 1))


def test_zero_byte_placeholder_is_unstable() -> None:
    assert not is_file_stable(obs(size=0, at=NOW - 5), obs(size=0))


def test_unreadable_file_is_unstable() -> None:
    assert not is_file_stable(obs(at=NOW - 5), obs(readable=False))


def test_fresh_mtime_needs_quiet_period() -> None:
    recent = NOW - 2  # modified 2s ago; default quiet period is 10s
    assert not is_file_stable(obs(mtime=recent, at=NOW - 1), obs(mtime=recent, at=NOW))


def test_looks_at_rest() -> None:
    assert looks_at_rest(100, NOW - 3600, NOW)
    assert not looks_at_rest(100, NOW - 5, NOW)  # too fresh
    assert not looks_at_rest(0, NOW - 3600, NOW)  # empty placeholder
