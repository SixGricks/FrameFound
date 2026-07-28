"""File-stability detection.

A file that is still being copied to the NAS must never enter the processing
pipeline: FFmpeg would read a truncated MP4, the hash would be wrong, and the
asset would need reprocessing. Before any file is enqueued, the scanner calls
`is_file_stable` with two observations of the file taken `interval_seconds`
apart.

This policy is deliberately a small, pure, unit-testable function: NAS
environments differ (SMB copy semantics, camera-offload apps, rsync temp
files), and this is the one place that judgment lives.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class FileObservation:
    """A point-in-time snapshot of a candidate file."""

    size_bytes: int
    mtime_epoch: float
    readable: bool  # a non-blocking open+read of the first block succeeded


def is_file_stable(
    earlier: FileObservation,
    later: FileObservation,
    interval_seconds: float,
    min_quiet_seconds: float = 10.0,
) -> bool:
    """Decide whether a file has finished being written.

    Returns True only when it is safe to hash and process the file.

    Considerations for the implementation:
    - size or mtime changed between observations -> definitely still copying
    - `later.readable` False -> locked or mid-transfer on some SMB setups
    - a file whose mtime is *very* recent may still be in a copy pause;
      requiring `min_quiet_seconds` since `later.mtime_epoch` trades indexing
      latency for safety
    - zero-byte files are placeholders some tools create first; never stable
    """
    # TODO(user): implement the stability policy for your NAS environment.
    # The scanner treats False as "check again next cycle" — being
    # conservative here costs latency, not correctness.
    raise NotImplementedError
