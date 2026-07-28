# ADR-0010: File identity strategy

- Status: Accepted
- Date: 2026-07-28

## Context
Paths are locations, not identities: editors reorganize folders constantly,
and reprocessing terabytes because a folder was renamed is unacceptable.
Full-content hashing at scan time is also unacceptable — an initial scan of a
multi-TB NAS over SMB must not read every byte.

## Decision
Layered identity, cheapest signal first:

1. **(size, mtime)** — change detection for already-indexed paths. Sub-second
   mtime jitter across filesystems is absorbed with a 1 s tolerance.
2. **Partial hash** — BLAKE3 over the first 1 MiB + last 1 MiB + the size,
   computed for every indexed file. Two reads regardless of file size; catches
   header/trailer changes, which is where every supported container format
   keeps its volatile bytes.
3. **Full BLAKE3** — computed lazily (dedupe verification, integrity checks,
   backup manifests), never during bulk scans. Invalidated on change.

**Move/rename detection**: a new path whose (size, partial hash) matches an
indexed asset whose old path no longer exists on disk re-binds that asset to
the new path — same UUID, no reprocessing, all derived data (thumbnails,
transcripts, embeddings) carries over.

**Missing files** are flagged (`missing` / `unmounted`), never deleted; the
watermark is `last_verified_at` against the scan start time, so the scanner
holds no giant in-memory path set.

## Alternatives considered
- **Full hash always**: correct but reads every byte of the archive on first
  scan; kills the "scan millions of entries" target on network storage.
- **Inode/file-id tracking**: not stable across SMB mounts or NAS migrations.
- **xxHash/MD5**: fine for partial hashing, but BLAKE3 is as fast in practice,
  cryptographic (usable for integrity/backup manifests), and one algorithm
  everywhere beats two.

## Consequences
- A middle-of-file corruption with unchanged size/mtime/edges is invisible
  until a full-hash verification pass runs (planned `verify` command, M8).
- Partial-hash collisions across *different* files are possible in theory;
  move re-binding therefore also requires the old path to be gone, and the
  lazy full hash disambiguates when it matters.
