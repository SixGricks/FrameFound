from pathlib import Path

from framefound.scanner.identity import PARTIAL_CHUNK_BYTES, full_hash, partial_hash


def test_same_content_same_hashes(tmp_path: Path) -> None:
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    a.write_bytes(b"x" * 1000)
    b.write_bytes(b"x" * 1000)
    assert partial_hash(a) == partial_hash(b)
    assert full_hash(a) == full_hash(b)


def test_content_change_changes_hashes(tmp_path: Path) -> None:
    f = tmp_path / "f.bin"
    f.write_bytes(b"x" * 1000)
    before = partial_hash(f)
    f.write_bytes(b"y" * 1000)
    assert partial_hash(f) != before


def test_size_alone_distinguishes(tmp_path: Path) -> None:
    """Two files with identical first/last megabyte but different sizes must
    differ — the size is baked into the partial hash."""
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    chunk = b"z" * PARTIAL_CHUNK_BYTES
    a.write_bytes(chunk + b"\0" * 100 + chunk)
    b.write_bytes(chunk + b"\0" * 200 + chunk)
    assert partial_hash(a) != partial_hash(b)


def test_partial_reads_only_edges(tmp_path: Path) -> None:
    """Middle-of-file changes beyond the sampled edges are NOT caught by the
    partial hash (by design — mtime+size cover that); full hash catches them."""
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    edge = b"e" * PARTIAL_CHUNK_BYTES
    a.write_bytes(edge + b"\1" * 100 + edge)
    b.write_bytes(edge + b"\2" * 100 + edge)
    assert partial_hash(a) == partial_hash(b)
    assert full_hash(a) != full_hash(b)


def test_small_file_hashing(tmp_path: Path) -> None:
    f = tmp_path / "tiny.bin"
    f.write_bytes(b"tiny")
    assert partial_hash(f)  # no seek errors on files smaller than the chunk
