# Storage layout — where everything lives

FrameFound writes three classes of data. They have very different needs, so
they are configured separately.

| Data | Size | Rebuildable? | Where it should live |
|---|---|---|---|
| **Originals** | your whole archive | — (irreplaceable) | NAS, mounted **read-only** |
| **Catalog database** | ~1 MB per 5k assets | No (back it up) | **Local disk**, always |
| **Derivatives** (thumbnails, posters, proxies, frames, transcript sidecars) | large — proxies dominate | Yes, from originals | local disk *or* a NAS share |

## Moving derivatives to a NAS

The app disk is usually the smallest volume in the system, and 1080p proxies
of a large video archive will fill it. Point the derivative store elsewhere
with one variable:

```bash
FRAMEFOUND_DATA_STORE=/mnt/framefound-data
```

Unset, it uses a Docker volume on the app disk. Set to a path, Docker binds
that path into every service at `/data`.

### Preparing the share

Use a **separate, writable** share — not the one holding your originals. That
keeps the read-only guarantee on your media intact: even a fully compromised
container cannot write to the originals.

SMB, in `/etc/fstab`:

```
//nas/framefound-data  /mnt/framefound-data  cifs  credentials=/etc/framefound-smb.cred,rw,uid=1000,gid=1000,iocharset=utf8,vers=3.0,_netdev,nofail  0 0
```

NFS:

```
nas:/export/framefound-data  /mnt/framefound-data  nfs  rw,_netdev,nofail,soft,timeo=100  0 0
```

Both entries matter in detail:

- **`uid=1000,gid=1000`** — containers run as uid 1000. Without this, every
  write fails with a permission error.
- **`_netdev,nofail`** — the VM still boots when the NAS is unreachable.
  FrameFound then reports the storage problem instead of failing to start.
- **`rw`** — deliberately different from the media mount's `ro`.

Then move any existing derivatives and restart:

```bash
docker compose down
sudo rsync -a /var/lib/docker/volumes/framefound_framefound-data/_data/ /mnt/framefound-data/
docker compose up -d
```

Nothing is lost if you skip the rsync — derivatives regenerate from originals.
It just costs processing time.

### What this does *not* move

**The database stays on local disk.** Postgres on SMB/NFS risks corruption and
performs badly; it is excluded by design. It is also small — the catalog for a
100k-asset library is a few hundred megabytes.

### Trade-offs

- Thumbnail loading gains a network round trip. On a LAN this is unnoticeable;
  the API caches aggressively and browsers cache for an hour.
- Proxy streaming is sequential and bandwidth-friendly — well suited to a NAS.
- If the NAS goes offline, previews stop working and generation pauses, but
  the catalog, search, and metadata keep working entirely.

## Done: the second drive and the RAM (2026-07-31)

Both landed. 6 GB → **58 GB RAM**, plus a 2.4 TB `sdb`. What follows is what the
measurements showed, because the plan below was written with a guess in it and
the guess was wrong.

### Which disk is faster: neither, for reads

The plan said "if the new drive is faster, put Postgres on it". Measured with
`fio` on real files (4k random, iodepth 16, O_DIRECT):

| | sda (existing) | sdb (new) |
| --- | --- | --- |
| random 4k read | 326 IOPS, 49 ms | 315 IOPS, 51 ms |
| random 4k write | 325 IOPS, 49 ms | 594 IOPS, 27 ms |

Reads are identical within noise, so **Postgres stayed on `sda`** — moving it
would have been risk for nothing. `sdb` is ~1.8× on writes, which suits
derivative generation, and its real value is the 2.4 TB.

A first pass against the raw devices reported `sdb` at 54,000 IOPS and 7.5 GB/s
sequential, which would have been faster than most NVMe. That was an artifact:
reading never-written blocks on a thin-provisioned virtual disk returns zeros
without touching physical media. Any benchmark of an empty virtual disk
measures nothing. Format it and test with real files.

Both disks sit at ~320 IOPS and ~50 ms latency, which is spinning-rust
territory. Worth knowing before blaming software for a slow query.

### What moved, and what it bought

`/data` (6.6 GB, 36,159 files) moved to `/mnt/ffdata/framefound-data`, verified
byte-identical, and the old Docker volume was reclaimed — root went from 30 GB
to 36 GB free. Postgres, models and Redis stayed on `sda`.

Container limits were rewritten against 58 GB rather than 5.9 GB. The old
numbers were a zero-sum trade — `worker-vision` had to be paid for by trimming
`worker` — and that constraint is gone:

| service | was | now | concurrency |
| --- | --- | --- | --- |
| worker (visuals/metadata) | 1000M | 4G | 1 → 3 |
| worker-media (media/frames) | 1100M | 8G | 1 → 3 |
| worker-vision | 1200M | 6G | 1 → 2 |
| worker-ai (transcribe) | 1100M | 6G | 1 (unchanged) |
| postgres | 800M | 8G | — |
| api | 700M | 1500M | — |

Total 37.4 GB of 58, leaving ~20 GB for the host and page cache. Transcription
concurrency deliberately stays at 1: two Whisper models resident is the profile
that used to get that container killed, and RAM removes the reason to run near
the edge rather than the reason to be careful.

Postgres was also given real settings — `shared_buffers=2GB`,
`effective_cache_size=6GB`, `work_mem=64MB`. The defaults assume a shared box
(128 MB of shared buffers), and on a disk doing 320 IOPS every page served from
memory instead is the largest single win available. Measured afterwards:
pgvector HNSW search at **3.2 ms warm median**, 99.9% buffer cache hit rate.

### Still to do

Health-aware storage — a disconnected-mount alert and per-drive capacity
warnings on the System page. With one disk "the disk" was unambiguous. With
two, a full or unmounted `sdb` now presents as unexplained failures spread
across proxies, renders and basemaps, with no page saying which disk ran out.

---

## The original plan (kept for the reasoning)

Both upgrades are expected together, and they relieve different constraints —
worth being clear about which is which, because it decides what to do first.

**What the second drive fixes.** The host currently has ~34 GB free on one
volume shared by Postgres, derivatives, models and now renders. That is the
binding constraint on three separate things: proxy transcodes pause below
`FRAMEFOUND_MIN_FREE_GB`, basemap extracts need room for a `.part` file the
size of the finished archive, and a long slideshow render needs working space
for every piece before the stitch. None of those are RAM problems.

**What the RAM fixes.** Worker memory limits currently total 7.6 GB against
5.9 GB installed, which only works because they are ceilings rather than
reservations. `worker-vision` was raised to 1200M after being OOM-killed
holding three ONNX sessions, paid for by trimming `worker` from 1300M to
1000M. More RAM removes that zero-sum trade and lets the frames and vision
lanes run wider, which is what would actually drain the 5,700-job frames
backlog faster.

### The migration is already a one-line change

Every service that touches derived data mounts `${FRAMEFOUND_DATA_STORE:-framefound-data}`,
so pointing `/data` at a new disk is an `.env` edit and a restart:

```bash
FRAMEFOUND_DATA_STORE=/mnt/fast/framefound-data
```

Nothing in the database stores an absolute path to derived files —
`Derivative.relative_path`, `Frame.relative_path` and `Slideshow.relative_path`
are all relative to the data directory, which was the point of that convention.
So the move is: stop the stack, `rsync -a` the volume to the new disk, change
the variable, start. No re-processing, no re-scan, no rebuilt thumbnails.

`framefound-models` and `postgres-data` are separate named volumes and can be
moved independently on the same principle.

### What should go where

| | Where | Why |
| --- | --- | --- |
| Postgres | fastest disk, ideally SSD | pgvector HNSW search is random-read bound; this is the one that benefits most from NVMe |
| `/data` derivatives + frames | large disk | ~15,700 thumbnails and previews today, and it grows with the library |
| `/data/renders` | large disk | a slideshow is ~1.5 MB per photograph, and the working directory briefly holds every piece |
| `/data/basemaps` | large disk | one file per region; the continental US is ~12 GB |
| `/models` | either, small | ~350 MB of ONNX weights, read once per worker start |
| Originals | unchanged, read-only | FrameFound never writes here and that does not change |

If the second drive is spinning rust and the existing one is SSD, put Postgres
and `/models` on the SSD and everything in `/data` on the new disk. If the new
one is the faster drive, move Postgres to it and leave the rest.

### What to do before the hardware arrives

- Nothing in the code. The relocation hook already exists and is exercised by
  the `FRAMEFOUND_DATA_STORE` default.
- Worth checking `docs/benchmarks.md` search latency *before* the move, so the
  Postgres relocation can be judged on numbers rather than impression.
- The 101 failed derivatives should be re-run after the move: several are
  likely to be the low-space guard rather than genuine failures, and it would
  be a shame to migrate the failure state along with the data.

### Still to build

Health-aware storage — a disconnected-mount alert and per-drive capacity
warnings on the System page. With one drive, "the disk" is unambiguous. With
two, a full or unmounted second drive presents as unexplained failures spread
across proxies, renders and basemaps, and the operator has no page that says
which disk ran out. That becomes worth building the day the drive goes in.

## The low-space guard

Generation stops while headroom remains, rather than filling the disk:

```bash
FRAMEFOUND_MIN_FREE_GB=5
```

Below this threshold, thumbnail and proxy jobs pause and log a clear reason
instead of writing. The database, and therefore your catalog, is never put at
risk by preview generation. Free space is shown on the **System** page; work
resumes automatically once space is available and the assets are reprocessed.
