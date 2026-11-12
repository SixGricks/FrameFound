# FrameFound on a Mac

FrameFound installed as a program on one Mac — no Docker, no second machine.
Everything the containers do runs as background services in your login
session, started when you log in and restarted if they stop, and one
command drives it: `framefound`.

Why not Docker on the Mac: containers there run inside a Linux virtual
machine that cannot reach the GPU, the Neural Engine or the hardware video
encoder, which are the reason to run on Apple silicon. Decision record:
[ADR-0022](adr/0022-native-mac-install.md).

> **Status (2026-11-11):** built and unit-tested, but not yet run on a Mac.
> Expect the first install to turn something up; `framefound doctor` and the
> logs in `~/FrameFound/logs` are where it will show.

## Install

You need [Homebrew](https://brew.sh), and the NAS connected in Finder.

```bash
git clone https://github.com/SixGricks/FrameFound ~/FrameFound/src
```

```bash
bash ~/FrameFound/src/infrastructure/mac/install.sh --proxies "/Volumes/intel/FrameFound Proxies"
```

That is the whole install. It takes a few minutes: Homebrew installs
Postgres (with pgvector), Valkey, Caddy, Node, ffmpeg and exiftool; the web
app is built; the services start. It ends by printing the address
(`http://localhost:8080`) and is safe to run again at any time.

`--proxies` is where video proxies go — here a folder on the NAS. Leave it
off to keep them on the Mac; change it later with one command
([Proxies](#proxies)). Keep the checkout out of Desktop, Documents and
Downloads: macOS keeps background services out of those.

## Bring the old server over

From the Mac, check you can reach the old server without a password prompt
getting in the way (`ssh-copy-id framefound@192.168.1.193` once, if not):

```bash
framefound migrate --from framefound@192.168.1.193
```

It shows the plan — each library's old path and where it is on this Mac —
and asks before doing anything. Then it:

1. copies the thumbnails, frames, transcripts and proxies **while the old
   server keeps running**;
2. stops the old server's app, copies the catalogue, and copies whatever
   changed during step 1;
3. rewrites each library's path (`/media/intel` → `/Volumes/intel`), carries
   the settings that must not change, and starts FrameFound here.

You sign in with the same account. Nothing on the old server is deleted;
`docker compose up -d` there brings it back exactly as it was. If a share is
mounted under a different name on the Mac, say so:
`--map "/media/promo=/Volumes/Promo Video"`.

Afterwards:

- `framefound doctor`
- Point the Premiere panel, the Lightroom plugin and Claude's connector at
  the Mac: `http://<the Mac's name>:8080` (the tokens still work — they came
  over in the catalogue).
- The catalogue arrives at whatever schema the old server was on and is
  brought up to date on first start, so maintenance steps that were waiting
  on a deploy now run here:
  `framefound ops pair_proxies --apply`, then `framefound ops shots backfill`.
- When you are sure, on the old server: `cd framefound && docker compose down`.

## Day to day

| | |
|---|---|
| `framefound status` | what is running |
| `framefound doctor` | is everything healthy, one line per thing |
| `framefound open` | the web app |
| `framefound logs worker-media` | follow one service's log |
| `framefound restart` | after editing the settings file |
| `framefound update` | pull the latest release, rebuild, restart |
| `framefound backup` | a catalogue backup now (one is taken nightly) |
| `framefound restore <archive>` | replace the catalogue with a backup's |
| `framefound ops <tool> …` | the maintenance tools the docs give as `docker compose exec` commands |

Everything is in `~/FrameFound`:

| | |
|---|---|
| `framefound.env` | every setting, with comments |
| `pgdata/` | the catalogue (Postgres) |
| `data/` | thumbnails, frames, transcripts, `backups/` |
| `models/` | downloaded model weights |
| `logs/` | one log per service |
| `src/` | the code |

## Proxies

Proxies are most of the derived data by size, so they are the one thing
worth keeping somewhere else. Wherever they are, moving them is one command:

```bash
framefound proxies move "/Volumes/Fast SSD/FrameFound Proxies"
```

It copies the proxies (never deletes), checks the copy, switches FrameFound
over and restarts the workers. The old copies stay where they were until
you remove them; the command prints how. `framefound proxies` shows where
they are now.

A proxy store is a drive or share that can be away. While it is, playback
of proxies says so and new proxies wait; everything else carries on, and it
all resumes when the store is back. FrameFound puts a small marker file in
the folder and writes only where it finds it, so an unplugged drive can
never be mistaken for an empty one. The marker also keeps the scanner out:
a proxy folder on the same share as your footage is never catalogued.

If a proxy drive is lost rather than moved: `framefound proxies move <new
folder> --empty`, then `framefound proxies rebuild`.

## Backups

A catalogue backup is taken nightly into `~/FrameFound/data/backups` (the
newest 14 are kept). That is the same disk as the catalogue, so set a second
place in `framefound.env`:

```
FRAMEFOUND_BACKUP_COPY_DIR=/Volumes/intel/FrameFound Backups
```

`framefound doctor` warns until you do. Each archive includes the settings
file, and with it the key that unlocks stored secrets — treat the folder
accordingly. Originals are never in a backup; derived files are rebuildable.

## Mac settings

- **Never sleep.** System Settings → Energy: *Prevent automatic sleeping*
  and *Start up automatically after a power failure*. `framefound doctor`
  checks the first.
- **Stay logged in.** The services run in your login session, because that
  is where the NAS is mounted. After a restart they start when you log in
  (turn on automatic login if the Mac should come back unattended; FileVault
  has to be off for that).
- **Reconnect the NAS at login.** Add the share to System Settings →
  General → Login Items.
- **Firewall.** If macOS asks whether `caddy` may accept incoming
  connections, allow it: that is other machines reaching the web app.
- **Remote access.** Install the Tailscale app; FrameFound is then at
  `http://<the Mac's tailnet name>:8080` from your other devices.

## Sharing the Mac with Adobe

The heavy workers run at a lower priority than everything else, with
low-priority disk access, so Premiere and Photoshop win any contention. If
editing still stutters during a big backfill, lower these in
`framefound.env` and `framefound restart`:

```
FRAMEFOUND_MEDIA_WORKERS=1
FRAMEFOUND_VISION_WORKERS=1
```

Two accelerations are available and off until you choose them:

- **Hardware encoding for proxies.** Detected, but proxies stay on the CPU
  encoder until the quality setting is chosen in `videotoolbox_proxy_args()`
  (`apps/server/framefound/processing/ffmpeg.py`) — how proxies should look
  is a choice, not a default to guess. `framefound doctor` says which
  encoder is in use.
- **CoreML for the vision models.** Run `framefound doctor --parity`; if it
  passes, set `FRAMEFOUND_ONNX_PROVIDER=coreml` and restart. The check is
  there because every vector already in the catalogue came from a CPU, and
  search compares new ones against them.

## Known limits

- **Not yet run on a Mac** (see the status note at the top).
- **Blackmagic RAW.** The BRAW decoder is a Linux build
  (`infrastructure/braw`). BRAW clips already catalogued keep their posters;
  new ones get none here until the decoder is built for macOS.
- **The Security page's Tailscale button and the drive-management page**
  talk to Linux-only pieces and show as unavailable. Use the Tailscale app
  and Finder.
- **HTTP on the local network and tailnet only.** The containers' public
  HTTPS and Cloudflare Tunnel modes are not set up by this install.
- **Libraries live under `/Volumes`** — network shares and external drives.
- **Workers on a second machine** are possible but not packaged:
  [remote-workers.md](remote-workers.md).

## When something is wrong

```bash
framefound doctor
```

Each line names one thing and what to do about it. A service that will not
stay up shows its log path; `framefound logs <service>` follows it.

If a library shows as unreachable although Finder can open it, macOS is
withholding network volumes from background services: System Settings →
Privacy & Security → Full Disk Access, add
`~/FrameFound/venv/bin/python` (press ⌘⇧G in the file picker to type the
path), then `framefound restart`.

## Removing it

```bash
framefound uninstall
```

removes the background services and nothing else. Delete `~/FrameFound` to
remove the catalogue, settings and derived files too. Homebrew's packages
are left for you (`brew uninstall …`).
