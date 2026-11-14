"""Free space on a Mac's large NAS shares (storage/space.py)."""

import ctypes
import shutil
from pathlib import Path

import pytest

from framefound.storage import space

TIB = 1024**4


def test_darwins_statfs_layout_is_the_64_bit_one() -> None:
    """The struct is read by offset: one field out and every count is wrong."""
    assert ctypes.sizeof(space._StatFs) == 2168
    assert space._StatFs.f_blocks.offset == 8
    assert space._StatFs.f_bavail.offset == 24
    assert space._StatFs.f_fstypename.offset == 72
    assert space._StatFs.f_mntonname.offset == 88


def test_a_20_tib_share_is_20_tib() -> None:
    """The mini's SMB shares: 20 TiB with 10 TiB free by `df`, read by
    `statvfs` as 3,915 GB with 2,035 GB free (block counts that wrapped at
    2^32). 64-bit counts do not."""
    block = 2048
    usage = space.from_counts(block, 20 * TIB // block, 10 * TIB // block, 10 * TIB // block)
    assert usage == space.Usage(total=20 * TIB, used=10 * TIB, free=10 * TIB)
    assert round(usage.free / 1024**3) == 10240


def test_free_is_what_this_user_may_write() -> None:
    usage = space.from_counts(4096, 1000, 300, 250)  # 50 blocks kept for root
    assert (usage.total, usage.used, usage.free) == (4096_000, 700 * 4096, 250 * 4096)


def test_a_mac_asks_statfs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    asked: list[bytes] = []

    def statfs(path: bytes, found: object) -> int:
        asked.append(path)
        result = ctypes.cast(found, ctypes.POINTER(space._StatFs)).contents
        result.f_bsize, result.f_blocks = 2048, 20 * TIB // 2048
        result.f_bfree = result.f_bavail = 10 * TIB // 2048
        return 0

    monkeypatch.setattr(space.sys, "platform", "darwin")
    monkeypatch.setattr(space, "_darwin_statfs", lambda: statfs)

    assert space.disk_usage(tmp_path).total == 20 * TIB
    assert asked == [str(tmp_path).encode()]


def test_a_failed_statfs_is_an_oserror(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(space.sys, "platform", "darwin")
    monkeypatch.setattr(space, "_darwin_statfs", lambda: lambda _path, _found: -1)
    monkeypatch.setattr(space.ctypes, "get_errno", lambda: 2)
    with pytest.raises(FileNotFoundError):
        space.disk_usage(tmp_path / "gone")


def test_elsewhere_it_is_shutils(tmp_path: Path) -> None:
    assert tuple(space.disk_usage(tmp_path)) == tuple(shutil.disk_usage(tmp_path))
