"""File identity (ADR-0010): fast partial hash, lazy full hash.

The partial hash (BLAKE3 over the first and last 1 MiB plus the size) changes
whenever real media content changes — headers and trailers hold the volatile
bytes in every container format we index — while costing two reads regardless
of file size. The full hash is computed lazily when dedupe or integrity
verification actually needs it.
"""

from pathlib import Path

from blake3 import blake3

PARTIAL_CHUNK_BYTES = 1024 * 1024


def partial_hash(path: Path) -> str:
    size = path.stat().st_size
    hasher = blake3()
    hasher.update(size.to_bytes(8, "big"))
    with path.open("rb") as fh:
        hasher.update(fh.read(PARTIAL_CHUNK_BYTES))
        if size > 2 * PARTIAL_CHUNK_BYTES:
            fh.seek(-PARTIAL_CHUNK_BYTES, 2)
            hasher.update(fh.read(PARTIAL_CHUNK_BYTES))
        elif size > PARTIAL_CHUNK_BYTES:
            fh.seek(PARTIAL_CHUNK_BYTES)
            hasher.update(fh.read())
    return hasher.hexdigest()


def full_hash(path: Path, chunk_bytes: int = 4 * 1024 * 1024) -> str:
    hasher = blake3()
    with path.open("rb") as fh:
        while chunk := fh.read(chunk_bytes):
            hasher.update(chunk)
    return hasher.hexdigest()
