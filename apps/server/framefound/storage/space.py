"""How big a disk or share is and how much of it is free, right on a Mac.

    space.disk_usage(Path("/Volumes/Intel")).free

`shutil.disk_usage` asks `statvfs`, and on macOS that call keeps the
32-bit block counts of its oldest form: a volume of more than 2^32 blocks
wraps around. The NAS shares on the mini (20 TiB, 10 TiB free by `df`)
came out as 3,915 GB with 2,035 GB free, on the Storage page and in
`framefound doctor` alike. `statfs`, which `df` uses, has 64-bit counts.
So on macOS this asks `statfs`; everywhere else `statvfs` is right and
`shutil.disk_usage` is used as it is.
"""

import ctypes
import ctypes.util
import functools
import os
import shutil
import sys
from pathlib import Path
from typing import Any, NamedTuple

# The Unix fields in Darwin's <sys/mount.h>, 64-bit inode layout (the only
# layout on arm64, and `statfs$INODE64` on Intel).
_MFSTYPENAMELEN = 16
_MAXPATHLEN = 1024


class _StatFs(ctypes.Structure):
    _fields_ = [
        ("f_bsize", ctypes.c_uint32),
        ("f_iosize", ctypes.c_int32),
        ("f_blocks", ctypes.c_uint64),
        ("f_bfree", ctypes.c_uint64),
        ("f_bavail", ctypes.c_uint64),
        ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64),
        ("f_fsid", ctypes.c_int32 * 2),
        ("f_owner", ctypes.c_uint32),
        ("f_type", ctypes.c_uint32),
        ("f_flags", ctypes.c_uint32),
        ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * _MFSTYPENAMELEN),
        ("f_mntonname", ctypes.c_char * _MAXPATHLEN),
        ("f_mntfromname", ctypes.c_char * _MAXPATHLEN),
        ("f_flags_ext", ctypes.c_uint32),
        ("f_reserved", ctypes.c_uint32 * 7),
    ]


class Usage(NamedTuple):
    """The same three figures, in bytes, as `shutil.disk_usage`."""

    total: int
    used: int
    free: int


def from_counts(block_size: int, blocks: int, blocks_free: int, blocks_available: int) -> Usage:
    """`shutil.disk_usage`'s arithmetic: free is what this user may write
    (`f_bavail`, as `df` shows), used is everything not free to anyone."""
    return Usage(
        total=blocks * block_size,
        used=(blocks - blocks_free) * block_size,
        free=blocks_available * block_size,
    )


@functools.cache
def _darwin_statfs() -> Any | None:
    """libc's 64-bit `statfs`, or None where it cannot be found."""
    name = ctypes.util.find_library("c") or "libSystem.B.dylib"
    try:
        libc = ctypes.CDLL(name, use_errno=True)
    except OSError:
        return None
    for symbol in ("statfs$INODE64", "statfs"):
        try:
            function = getattr(libc, symbol)
        except AttributeError:
            continue
        function.argtypes = [ctypes.c_char_p, ctypes.POINTER(_StatFs)]
        function.restype = ctypes.c_int
        return function
    return None


def _statfs(path: Path) -> Usage | None:
    function = _darwin_statfs()
    if function is None:
        return None
    found = _StatFs()
    if function(os.fsencode(path), ctypes.byref(found)) != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), str(path))
    return from_counts(found.f_bsize, found.f_blocks, found.f_bfree, found.f_bavail)


def disk_usage(path: Path | str) -> Usage:
    """Total, used and free bytes of the disk or share `path` is on.
    Raises OSError like `shutil.disk_usage`."""
    if sys.platform == "darwin":
        usage = _statfs(Path(path))
        if usage is not None:
            return usage
    total, used, free = shutil.disk_usage(path)
    return Usage(total, used, free)
