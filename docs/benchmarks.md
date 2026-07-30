# Benchmarks (M8)

```bash
docker compose exec api python -m framefound.ops.benchmark
```

Measured against whatever the deployment actually holds — this hardware, this
storage, this catalogue. A figure from a laptop with warm caches would not tell
anyone anything useful.

Reported as p50/p95/max rather than averages, because an average hides the case
someone complains about. Search that is usually 40 ms and occasionally four
seconds is a slow search.

---

## Reference deployment

A 2012 Mac Pro 5,1 (Westmere Xeons, **no AVX**), 6 GB VM, media on an SMB share
measured at **5.2 MB/s**. Deliberately modest: if it is comfortable here it
will be comfortable anywhere.

| | |
|---|---|
| Assets | 15,774 (15,714 online) |
| Frames | 10,288 — 9,429 embedded |
| Derivatives ready | 31,204 |
| Transcripts | 88 |
| Located assets | 6,867 |

## Results — 2026-07-30

| Query | p50 | p95 | Notes |
|---|---|---|---|
| Browse page 1 (50, sorted) | 34.2 ms | 93.1 ms | |
| Asset count | 3.0 ms | 3.3 ms | drives pagination |
| Browse page 100 (offset 4950) | 57.6 ms | 68.0 ms | offset paging holds up at this depth |
| Filename search (ILIKE) | 3.6 ms | 11.5 ms | no trigram index; scales linearly |
| Transcript phrase search | 6.6 ms | 7.1 ms | |
| **Visual similarity (top 40)** | **2.8 ms** | **6.6 ms** | HNSW index |
| Load located assets | 32.1 ms | 85.3 ms | places clusters this in Python |
| Duplicate grouping | 18.1 ms | 23.4 ms | |

Budget is a 200 ms p95, which leaves room for the network and the browser
inside a sub-second page. Everything is comfortably inside it.

---

## What the first run found

Vector search was doing a **Sort over 9,235 rows instead of an HNSW index
scan** — 75 ms p50, and growing linearly with the library.

The diagnosis took a wrong turn worth recording. The plans initially suggested
`WHERE embedding IS NOT NULL` was blocking the index, because removing the
filter produced an index scan. That was a coincidence of ordering. The real
cause was **stale statistics**: 9,429 embeddings had arrived in one background
run, autovacuum had not caught up, and the planner mis-costed the index scan.
After `ANALYZE frames` the index is used *with* the filter in place, and the
partial index that appeared to fix it turned out to be unnecessary.

| | p50 | p95 |
|---|---|---|
| Before | 75.0 ms | 84.3 ms |
| After `ANALYZE` | **2.8 ms** | **6.6 ms** |

**27× faster**, from a statistics refresh.

This is the worst class of performance bug: it degrades linearly and nothing in
the application looks wrong. The fix is that the scanner now ANALYZEs the
bulk-growing tables on its existing five-minute maintenance tick, and the
benchmark warns — naming stale statistics as the likely cause — if the plan is
ever not an index scan again.

---

## Reading the results

**A single p95 spike is usually a transient — re-run before chasing it.** Seen
in practice: visual similarity reported a 843 ms p95 while the vision queue was
draining, then 6.6 ms on an immediate re-run with the same queue depth and
Postgres at 0% CPU. Twelve samples is enough to see a real tail and few enough
that one cold page-cache miss moves p95. Two consecutive runs disagreeing means
the number is noise; two agreeing means it is not.

## Known scaling limits

Honest about where this will bend first:

- **Filename search has no trigram index.** Linear in asset count; 3.6 ms at
  15k, so roughly 250 ms at 1M. Add `pg_trgm` when a library gets there.
- **Offset pagination** degrades with depth. Fine to page 100; keyset
  pagination would be needed for deep browsing of a very large library.
- **Places clusters on every request** in Python. 6,867 located assets load in
  32 ms and cluster in well under a second; above ~50k this wants persisting.
- **Tag suggestion scores every embedded frame** in one pass. ~4 seconds at 9k
  frames. Above ~100k this wants the same HNSW index the search uses.
- **SMB at 5.2 MB/s** is the binding constraint on anything that reads whole
  files — thumbnails of large TIFFs, full-content hashing, proxy transcodes.
  No amount of query tuning touches it.

## Re-running after a large import

Statistics are refreshed automatically, but if a scan has just added many
thousands of assets and something feels slow, the direct check is:

```bash
docker compose exec postgres psql -U framefound -d framefound -c "ANALYZE"
docker compose exec api python -m framefound.ops.benchmark
```
