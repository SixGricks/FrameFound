"""File-stability detection.

A file that is still being copied to the NAS must never enter the processing
pipeline: FFmpeg would read a truncated MP4, the hash would be wrong, and the
asset would need reprocessing. Being conservative here costs indexing latency,
not correctness — False always means "check again next cycle".

Two entry points:
- `is_file_stable`: two observations taken some seconds apart (watcher path).
- `looks_at_rest`: single observation for initial/reconciliation scans, where
  almost every file has been at rest for a long time.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class FileObservation:
    """A point-in-time snapshot of a candidate file."""

    size_bytes: int
    mtime_epoch: float
    observed_at_epoch: float
    readable: bool  # a non-blocking open+read of the first block succeeded


def is_file_stable(
    earlier: FileObservation,
    later: FileObservation,
    min_quiet_seconds: float = 10.0,
) -> bool:
    """Two-observation policy for watcher-detected files.

    Stable only when: nothing changed between observations, the file is
    readable and non-empty, and the mtime has been quiet for
    `min_quiet_seconds` (SMB copies can pause mid-transfer; a very fresh
    mtime means the writer may still come back).
    """
    if later.size_bytes == 0:
        return False  # placeholder some tools create before writing content
    if not later.readable:
        return False  # locked or mid-transfer on some SMB configurations
    if later.size_bytes != earlier.size_bytes:
        return False
    if later.mtime_epoch != earlier.mtime_epoch:
        return False
    return later.observed_at_epoch - later.mtime_epoch >= min_quiet_seconds


def looks_at_rest(
    size_bytes: int,
    mtime_epoch: float,
    now_epoch: float,
    min_quiet_seconds: float = 60.0,
) -> bool:
    """Single-observation heuristic for bulk scans: non-empty and untouched
    for `min_quiet_seconds`. Files newer than that are deferred to the next
    pass (or to the watcher's two-observation check)."""
    return size_bytes > 0 and (now_epoch - mtime_epoch) >= min_quiet_seconds
