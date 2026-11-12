# ADR-0022: A native install on the Mac mini; workers on another machine as an option

- Status: Accepted
- Date: 2026-11-11

## Context

The server is a 2012 Mac Pro VM: Westmere CPUs without AVX, x264 proxies and
Whisper on the CPU, and a roadmap that rates visual search on it as high
risk ([Hardware path](../roadmap.md)). The owner now has an always-on Mac
mini (Apple silicon, 32 GB) that already runs Premiere, Lightroom and the
Claude client — and on 2026-11-09 it was doing FrameFound's heavy work by
hand for the Brookside cut.

Two ways to use it were built on 2026-11-11, in this order.

**First, workers on the mini against the VM** (the mini joins the server's
queues; nothing migrates). It works, and its safety rules are sound, but
setting it up meant an SSH key restricted to two forwards, a tunnel, an NFS
export, an autofs mount and a path map — every step there only because two
machines had to share one database and one folder of files. The owner's
verdict on the setup was "convoluted", and it was.

**Then the question that made it simple:** could FrameFound be installed on
the mini as a program instead? With everything on one machine there is
nothing to share, and all of that plumbing has no reason to exist.

Docker on the mini was never the answer: containers on macOS run in a Linux
VM that cannot reach the GPU, the Neural Engine or the media engine.

## Decision

### FrameFound runs natively on the Mac mini, as launchd agents

`framefound/native` turns each compose service into a launchd agent in the
logged-in user's session, under one folder (`~/FrameFound`) and one command
(`framefound`). Guide: [docs/mac.md](../mac.md).

- **Homebrew provides the programs** (Postgres with pgvector, Valkey, Caddy,
  Node, ffmpeg, exiftool, rsync); FrameFound owns their data and config.
  Postgres is its own cluster on its own loopback port, not
  `brew services`, so it cannot collide with another Postgres on a
  workstation. The Postgres version is whichever Homebrew's pgvector bottle
  was built for, oldest first — found at install time, never hard-coded.
- **Agents, not daemons.** They start at login, because that is where the
  NAS is mounted and where Adobe runs. A logged-in Mac that never sleeps is
  the deployment; running before login was considered and declined as
  complexity for a case the owner does not have.
- **launchd runs a launcher, not the programs**:
  `python -m framefound.native run <service>` reads `framefound.env`, waits
  for the ports the service needs, then `exec`s it. No secret is ever
  written into a plist, and start-up order needs no launchd tricks.
- **The queue has a password** (`REDIS_PASSWORD`). In the containers Valkey
  is only on the internal network; on a workstation it shares loopback with
  every other program.
- **Rejected: a double-click `.app`** bundling Postgres, Python and Node. A
  large packaging project for the same day-to-day result as one install
  command.
- **Most of it is Python, not shell**, with every outside effect an argv
  list — so the decisions (which Postgres, what order to stop in, what a
  plist contains, what a migration refuses) are unit-tested on any platform.
  The bash bootstrap only creates the venv.

### Proxies have their own, movable store

`FRAMEFOUND_PROXY_DIR` puts video proxies — most of the derived bytes —
somewhere other than the data folder, under the same relative paths, so the
database never changes when they move (`framefound proxies move`). The
owner's choice: a NAS share now, an external SSD later if scrubbing wants
it.

A separate store can be away, and the folder it mounts on can still be
there, empty. So the store carries a marker file placed only by the
operator's own command; nothing is written without it, a missing store
pauses proxy generation exactly as a full disk does, and
`framefound proxies rebuild` refuses to treat an unplugged drive as
"everything is missing". The scanner skips any folder holding a store
marker, so a proxy folder on the footage's own share is never catalogued.

### Migration is one command, and the old server stays up for most of it

`framefound migrate --from user@host` copies derived files first (old server
still serving), then stops the old app, dumps and restores the catalogue,
copies the delta, rewrites each library root once (`/media/x` →
`/Volumes/x`) and carries the settings that describe the catalogue — the
secret key above all. Everything that can refuse does so before anything is
changed. Nothing on the old server is deleted; `docker compose up -d` there
is the rollback.

### Workers on another machine remain possible, unpackaged

What was built first is kept as a capability
([remote-workers.md](../remote-workers.md)): `FRAMEFOUND_ROOT_MAP` marks a
process as a remote worker, which never marks a file missing, writes
nothing without the data store's marker, and hands back to the queue
(Celery `Reject`, after a pause) any job it cannot do. The operator's own
jobs moved to an `exports` queue so a remote worker takes only bulk ingest;
`jobs.worker` (migration 0030) records which machine ran each job. The Mac
launchd kit for it was removed in favour of the native install.

### Acceleration is opt-in and checked

VideoToolbox is probed functionally, like NVENC; its quality setting is the
operator's choice (`videotoolbox_proxy_args`), and until it is made proxies
stay on libx264. CoreML for the ONNX models is a setting that
`framefound doctor --parity` refuses unless its vectors match the CPU's
(cosine ≥ 0.995), because search compares new vectors with every CPU-made
one already stored.

## Consequences

- One machine holds everything, so backups must leave it:
  `FRAMEFOUND_BACKUP_COPY_DIR`, and `doctor` warns while it is unset.
- FrameFound now shares a workstation with Adobe. The heavy lanes run at a
  lower priority with low-priority I/O, and their concurrency is a setting.
- Two install paths exist (containers, native Mac). The native Caddyfile
  repeats the containers' security rules, and the lanes repeat compose's
  queue lists; tests hold each pair together.
- Not carried over to the Mac: the BRAW decoder (a Linux build), the
  Security page's Tailscale button and the mount helper (Linux-only), public
  HTTPS and the Cloudflare tunnel.
- As of this decision the native install has been exercised only by unit
  tests; nothing has run on a Mac.
