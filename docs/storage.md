# Storage layout — where everything lives

FrameFound writes three classes of data. They have very different needs, so
they are configured separately.

| Data | Size | Rebuildable? | Where it should live |
|---|---|---|---|
| **Originals** | your whole archive | — (irreplaceable) | NAS, mounted **read-only** |
| **Catalog database** | ~1 MB per 5k assets | No (back it up) | **Local disk**, always |
| **Derivatives** (thumbnails, posters, proxies, frames, transcript sidecars) | large — proxies dominate | Yes, from originals | local disk *or* a NAS share |

> **Two classes of derived data.** What every page reads stays on the
> machine's own disk; the bulky kinds can each live somewhere else and be
> moved one at a time — see
> [Bulk storage](#bulk-storage-a-folder-per-kind). "Moving derivatives to a
> NAS" further down is the containers' older, all-or-nothing form of the
> same idea.

## Bulk storage: a folder per kind

| Stays on the machine's own disk (fast) | Bulk: a NAS share or an external drive |
|---|---|
| The catalogue database — never on a network share | Video proxies |
| Thumbnails, previews, posters, frames, shot stills (read on every page) | Slideshow videos |
| Transcripts and the queue | Listing exports |
| Work in progress: a render's pieces, a zip being built | Basemaps |
| Frame grabs made for editing | The second copy of each catalogue backup |
| A copy of each AI model that has a job (Looker, Editor) | The model library: every AI model added on the AI models page |

Most of what FrameFound generates is small and read on every page. That
stays beside the database. A few kinds are large and read rarely, and each
of those can live somewhere else:

| Kind | What | Unless moved | In a bulk folder |
|---|---|---|---|
| `proxies` | video proxies | `<data>/derivatives/…/proxy.mp4` | `<bulk>/proxies/derivatives/…/proxy.mp4` |
| `renders` | slideshow videos | `<data>/renders/<id>.mp4` | `<bulk>/renders/<id>.mp4` |
| `exports` | listing zips | `<data>/exports/listings/<id>.zip` | `<bulk>/exports/listings/<id>.zip` |
| `basemaps` | map extracts | `<data>/basemaps/<name>.pmtiles` | `<bulk>/basemaps/<name>.pmtiles` |
| `backups` | the copy of each catalogue backup kept off the machine | none | `<bulk>/backups/` |
| `models` | the AI model library | `<models>/library/<publisher>/<name>/<revision>/` | `<bulk>/models/library/…` |

The model library is the one kind with a copy on the machine as well. While
the library is somewhere that can be unplugged, each model that has a job is
also kept in `<models>/local-ai/…` and runs from there, so a NAS that is
away stops downloads, removals and changes of job, never a model already at
work. With the library on the machine's own disk nothing is copied. The
models folder also holds the vision and transcription models' caches; only
`library/` is the library, and only it is counted, moved or removed.

Nothing in the database changes when a kind moves: a slideshow's row still
says `renders/<id>.mp4`, and only the folder that path hangs off is
different. Work in progress — a render's pieces, a zip being built, a
basemap being extracted — is always written on the machine's own disk, and
the finished file is copied to its store under a `.partial` name and renamed.
A share that drops mid-write leaves nothing that looks finished.

### One folder for all of it

Give FrameFound one folder on the NAS and every bulky kind gets a subfolder
of its own inside it. On a Mac, at install or any time after:

```bash
framefound install --store "/Volumes/Photos/Framefound"
```

or, on any install, from the Storage page. Either way the folder is checked
(below), marked, given a marked subfolder per kind, and recorded. **Nothing
is moved.** A kind that has made nothing yet starts in the bulk folder
straight away; a kind that already has files stays where it is, still
found, until you move it. So on a new install everything lands on the NAS
from the first file, and on an old one you bring the kinds over one at a
time.

A folder that looks like a share that is not connected is asked about
before anything is made in it, on the page and at the command line alike
(`--yes` answers in advance): one on the same disk as FrameFound's own
data, or one that is the bulk folder already but has lost its marker.
Marked on the quiet, every kind in it would read as connected and fill the
machine's own disk while the files on the real share were reported missing.
The second case has one honest use — the drive was replaced and the folder
is a new, empty one — and saying yes starts it empty.

The folder can sit on the same share as a library — the Photos drive, say.
Its marker keeps the scanner out, so it is never catalogued as footage. It
cannot be a folder the catalogue already lists footage in.

### Moving one kind

On the **Storage** page each kind has a line: where it lives, how much there
is, whether that place is connected. An administrator gives a kind a new
folder and starts the move; the page shows how far it has got, can be closed
and come back to, and offers Cancel. From a terminal on a Mac the same move
is:

```bash
framefound storage                                  # where everything is
framefound storage move renders "/Volumes/Fast SSD/FrameFound renders"
```

One move runs at a time, whichever way it was started: the turn is a file
in the data folder (`.framefound-move.lock`), so the page refuses a move
while one started from a terminal is running, and the other way about. A
move whose process was killed stops holding the turn after two minutes.

A move is copy, check, switch (`storage/moves.py`):

1. the folder is checked — on a drive that is really there, empty or
   already FrameFound's, not another kind's folder, not one the catalogue
   lists footage in, and with room for what is coming. Only the last folder
   of the path is ever created: FrameFound will not make `/Volumes/Photos`
   itself, because if that drive is not connected the "store" would be on
   the machine's own disk. A folder on the same disk as FrameFound's own
   data is asked about before it is used, for the same reason, and nothing
   is made in it until the answer is yes;
2. the files are copied while everything keeps running. This is the long
   part; it can be interrupted or cancelled, and asking again carries on
   from what was copied;
3. the kind's writers are paused for the last look, what changed during the
   copy is brought over, and the move waits until nothing is still being
   written (a writer that reaches its last step during the pause waits for
   the move rather than losing its work);
4. every file is checked to have arrived, and the copy of anything deleted
   while the move ran — a basemap, a slideshow — is taken back out of the
   new folder, so the switch does not bring it back;
5. the folder is marked and recorded in `<data>/.framefound-stores.json`.
   Every running process follows at its next lookup — no restart.

**Room.** A move never fills a disk. Before each pass it compares what is
still to copy with the free space where it is going, less
`FRAMEFOUND_MIN_FREE_GB` — the same floor every other writer keeps, because
the machine's own disk also holds the catalogue — and refuses with both
figures if it will not fit. That includes moving a kind *back* onto the
machine's own disk. The page says so at the click (for proxies, from what
the catalogue says they take), and a copy stops short of the floor if
something else fills the disk meanwhile.

**The same folder under another name.** On a Mac's volumes and on SMB
shares `Framefound` and `FrameFound` are one folder; a symlink is another
name for its target; and macOS mounts a share a second time as
`/Volumes/Photos-1`. A kind is never "moved" to the folder it is already in
under another name, and Remove old copies refuses when the old folder and
the one in use turn out to be one: every file there would "have a copy".

Nothing of a kind's is deleted where it lives. The old copies stay where
they were until you choose **Remove old copies** for that kind on the
Storage page, which removes only that kind's files and only those that have
a copy in the folder now in use; any that do not are counted and left, to
be removed in a second, explicit step. Deleting a basemap, a slideshow or a
listing after a move also deletes its old copy, so that moving back onto
the old folder later does not bring it back.

A store that is gone for good — a drive that died — cannot be copied from.
Start its replacement empty instead (the "start empty" choice on the page;
`--empty` on the command line) and have the files made again:
`framefound proxies rebuild` for proxies, Render or Export again for the
rest. That works at the same path too: a replacement drive given the old
one's name, or a share rebuilt under its old name.

A move that stops — the drive is unplugged, the worker is restarted —
changes nothing: the kind still lives where it did, its writers are no
longer paused, and what was copied is kept. The Storage page says why;
start it again when the cause is fixed.

### Where a kind lives

First match wins:

1. its own folder, recorded by a move;
2. its own setting — `FRAMEFOUND_PROXY_DIR` for proxies (written by
   `framefound proxies move`), `FRAMEFOUND_BACKUP_COPY_DIR` for backup
   copies;
3. `<bulk>/<kind>`, where `<bulk>` is the one folder for everything bulky
   (the folder recorded when it was set, else `FRAMEFOUND_BULK_DIR`);
4. where it always was. With nothing set, nothing changes.

The newest instruction wins: `framefound proxies move` clears a folder an
earlier move recorded for proxies, and a later move records one that
outranks the setting.

The record is what is followed, for the bulk folder as well: once it has
been set, editing `FRAMEFOUND_BULK_DIR` in the settings file changes
nothing, restart or not (`framefound storage` points out when the two
differ). Change the bulk folder with `framefound install --store <folder>`
or on the Storage page.

That record is the one thing in the data folder that cannot be rebuilt, so
each catalogue backup on a Mac carries it (`stores.json` in the archive),
and `framefound restore` puts it back when the data folder has none — a new
Mac, a data folder made again. An install that already has a record keeps
its own: that one says where the files are now.

### When a store is away

**A store carries a marker file**, and nothing is written to it without one:
`.framefound-bulk-store` in the bulk folder and in each kind's folder
(`.framefound-proxy-store` for proxies, as before). A share that fails to
mount leaves its mountpoint behind as an empty folder; without the marker
test FrameFound would start filling the machine's own disk under the
share's name. While a store is away, only what lives there stops, and says
so: the page asking for a slideshow's video or a listing's zip is told the
store is not connected (not that the file is gone), and a render or export
started meanwhile stops with that reason. Search, thumbnails and everything
on the machine's own disk carry on. The Storage page shows which kinds are
affected; the System page and the banner show the bulk folder as one
unreachable volume. The marker also keeps the scanner out, so a bulk folder
on the same share as a library is never catalogued as footage.

Nothing that stopped starts again by itself when the store is back:

- **Slideshows and exports** — press Render or Export again.
- **Proxies** for clips that arrived meanwhile were skipped, not failed,
  and nothing queues a proxy twice. `framefound proxies rebuild` (in the
  containers, `python -m framefound.ops.proxy_store --requeue`) finds the
  videos that never got the proxy their library asks for, as well as
  proxies on record whose file is gone, and queues them.
- **A basemap** whose download finished while the store could not be
  written to is kept on the machine's own disk rather than thrown away. The
  Basemaps page says it is waiting; choose Download for it again and it is
  put in place at once, with nothing downloaded twice.
- **An AI model's download** keeps what had arrived: **Resume** on the AI
  models page carries on from there. A model that was being copied onto the
  machine to take over a job did not take it — the model that had the job
  still has it — and is given the job again on the same page.

### In the containers

The Storage page and its moves are the same code there, with limits. A
kind can only be moved to a folder the containers can already see and write
— in practice a share under the cache root, which they see as
`/cache/<name>/…` (`FRAMEFOUND_CACHE_ROOT` on the host). Backup copies are
made by the Mac install's backup service only; the containers' backup
service keeps its archives in `/data/backups` and copies them nowhere. And
none of this has been tried on the container install yet: it has automated
tests, and has never moved a real file there.

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

On Ubuntu's cloud and virtual kernels, `iocharset=utf8` needs a module that
kernel updates do not install by themselves; without it the next reboot fails
the mount with `mount error(79)`. Install `linux-image-extra-virtual` once
(`--no-install-recommends`) — see
[deployment/proxmox.md](deployment/proxmox.md) — and run
`manage.sh doctor` to check.

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
