# FrameFound on a Mac

FrameFound installed as a program on one Mac — no Docker, no second machine.
Everything the containers do runs as background services in your login
session, started when you log in and restarted if they stop, and one
command drives it: `framefound`.

Why not Docker on the Mac: containers there run inside a Linux virtual
machine that cannot reach the GPU, the Neural Engine or the hardware video
encoder, which are the reason to run on Apple silicon. Decision record:
[ADR-0022](adr/0022-native-mac-install.md).

> **Status (2026-11-13): it has run on a Mac.** On 2026-11-13 FrameFound was
> installed on one Apple-silicon Mac mini, the catalogue was brought over
> from the old server with `framefound migrate`, and that Mac is now the
> server in daily use. The old server's app is stopped, not removed.
>
> That first install found three bugs, all fixed in this release:
> Homebrew's rsync was never installed (macOS has an rsync of its own, so the
> installer thought it was there); the `framefound` command was put in place
> only at the very end, so an install that stopped early told you to run a
> command that did not exist yet; and PostgreSQL would not start as a
> background service, because macOS starts those with no language setting
> (a locale). The fixes are unit-tested and have **not yet run on that Mac**:
> they arrive with its first `framefound update`, which needs one step by
> hand ([Updating](#updating)).
>
> So, exactly: **run on a Mac** — the install, the migration (three
> libraries on NAS shares), and serving the catalogue since. **Not yet run on
> a Mac** — `framefound update`, the two new `framefound doctor` lines
> (`locale`, `rsync`), and everything under
> [Local AI models](#local-ai-models). Nothing has been reported from that
> Mac, either way, about the other commands on this page (moving the store
> or the proxies, restoring a backup); they are unit-tested.
> `framefound doctor` and the logs in `~/FrameFound/logs` are where a problem
> shows.

## Install

You need [Homebrew](https://brew.sh), and the NAS connected in Finder.

```bash
git clone https://github.com/SixGricks/FrameFound ~/FrameFound/src
```

```bash
bash ~/FrameFound/src/infrastructure/mac/install.sh --store "/Volumes/Photos/Framefound"
```

That is the whole install. It takes a few minutes: Homebrew installs
Postgres (with pgvector), Valkey, Caddy, Node, ffmpeg, exiftool and rsync;
the `framefound` command is put in place; the web app is built; the
services start. It ends by printing the address (`http://localhost:8080`)
and is safe to run again at any time.

Three things about it are worth knowing, each learned from the first real
install:

- **A tool counts as installed only where the background services will
  look for it**: Homebrew's folders (`/opt/homebrew/bin`, `/usr/local/bin`)
  and the system's own. Your Terminal's PATH is not what is asked. A Node
  from nvm or an ffmpeg from conda is found by the Terminal and not by a
  service, so Homebrew's is installed beside it. **rsync is always
  Homebrew's**, even though macOS has one: the system's is an older kind
  that lacks options `framefound migrate` and `framefound proxies move` use,
  so it does not count and Homebrew's is installed next to it.
- **The `framefound` command is there early**: straight after the tools and
  the settings file, before the database, the web build and the services.
  If a later step fails, the command is still there, and `framefound doctor`
  can say why. If the install says the command is somewhere "this Terminal
  does not look", Homebrew is not on that Terminal's PATH yet: type the full
  path it gives (`/opt/homebrew/bin/framefound …`), or open a new Terminal
  window after doing Homebrew's own "Next steps".
- **Every service is started with a UTF-8 locale** (`LC_ALL` and `LANG`,
  both `en_US.UTF-8`, the one the database is created with). macOS starts
  background services with no locale at all, and PostgreSQL will not start
  without one: its log repeats "postmaster became multithreaded during
  startup" while the same command works from a Terminal, which sets a locale
  itself. Nothing needs setting; `framefound doctor` has a `locale` line
  that says what the services are given.

`--store` is the folder for everything bulky — video proxies, slideshow
videos, listing exports, basemaps and the second copy of each backup — here
a `Framefound` folder on the NAS's Photos drive. The drive has to be
connected; FrameFound makes the folder, and a folder inside it for each
kind. (If it is not connected the install still finishes, and ends by saying
the store was not set. If the folder turns out to be on the Mac's own disk
— which is what a share that is not connected can look like — the install
asks before using it; `--yes` answers in advance.) Leave `--store` off to
keep everything on the Mac, and add or change it later
([Bulk storage](#bulk-storage)). Keep the checkout out of Desktop, Documents
and Downloads: macOS keeps background services out of those.

## Bring the old server over

Three things first, on the Mac:

1. **Let the Mac reach the old server without a password**, once:
   `ssh-copy-id framefound@192.168.1.193`.
2. **Connect every library's share in Finder**, and note each one's name
   exactly as Finder shows it.
3. **Do not create an account on the new install.** Leave the first-time
   setup alone: the accounts come over with the catalogue, and the
   migration refuses to start if this install already has one ("This
   install already has a catalogue with users in it"). If you did make one,
   `--replace` overwrites what is here with the old server's.

Then:

```bash
framefound migrate --from framefound@192.168.1.193
```

**Share names are taken exactly as written, capitals and spaces included.**
The old server saw every share under `/media` (`/media/intel`); the Mac
mounts each under `/Volumes` with the name Finder shows. Left to itself the
migration looks for `/media/intel` at `/Volumes/intel`, which is not where
a share Finder shows as `Intel` is: treat the names as case-sensitive. So
give one `--map` for every library whose share is named differently here
than on the old server, a different capital letter included:

```bash
framefound migrate --from framefound@192.168.1.193 \
  --map "/media/intel=/Volumes/Intel" \
  --map "/media/gelco=/Volumes/GELCO" \
  --map "/media/promo=/Volumes/Promo Video"
```

A name with a space in it needs nothing more than the quotes. The path on
the right is what is recorded as the library's folder, as typed.

It shows the plan — each library's old path and where it is on this Mac —
and asks before doing anything. **Read the plan before saying yes**: a
library whose folder was not found is marked `<- absent on this Mac`. Say
no, add or correct its `--map`, and run it again; nothing has been changed.
(A library with no place at all is refused outright, with the `--map` to
add.) Then it:

1. copies the thumbnails, frames, transcripts and proxies **while the old
   server keeps running** — and, when the install has a store, the old
   server's slideshow videos, listing exports and basemaps straight into
   it rather than through the Mac's disk;
2. stops the old server's app, copies the catalogue, and copies whatever
   changed during step 1;
3. rewrites each library's path (`/media/intel` → `/Volumes/Intel`), carries
   the settings that must not change, and starts FrameFound here.

You sign in with the same account. Nothing on the old server is deleted;
`docker compose up -d` there brings it back exactly as it was.

Afterwards:

- `framefound doctor`
- **Add each NAS share to Login Items** (System Settings → General → Login
  Items). Without that, a restart of the Mac leaves the shares unconnected
  and every library reads as offline until each is opened in Finder again.
- **Point the other machines at the Mac**: browsers, the Premiere panel and
  the Lightroom plugin use `http://<the Mac's local hostname>.local:8080`
  (the name is under System Settings → General → Sharing). The tokens still
  work — they came over in the catalogue.
- **Claude on the Mac itself** keeps its connector settings in
  `~/Library/Application Support/Claude/claude_desktop_config.json`. Claude
  writes that file again when it quits, so an edit made while it is open
  can be lost: quit Claude, edit the file, open Claude again. What goes in
  it is in [mcp.md](mcp.md#connecting).
- The catalogue arrives at whatever schema the old server was on and is
  brought up to date on first start, so maintenance steps that were waiting
  on a deploy now run here:
  `framefound ops pair_proxies --apply`, then `framefound ops shots backfill`.
  With no `--library`, the backfill does every library in turn, by name
  (one that fails does not stop the next); `--library NAME` does one.
- When you are sure, on the old server: `cd framefound && docker compose down`.

## Day to day

| | |
|---|---|
| `framefound status` | what is running |
| `framefound doctor` | is everything healthy, one line per thing |
| `framefound open` | the web app |
| `framefound logs worker-media` | follow one service's log |
| `framefound restart` | after editing the settings file |
| `framefound update` | back up, pull the latest release, rebuild, restart ([Updating](#updating)) |
| `framefound storage` | where the bulky files live, and whether that drive is connected |
| `framefound backup` | a catalogue backup now (one is taken nightly) |
| `framefound restore <archive>` | replace the catalogue with a backup's |
| `framefound ops <tool> …` | the maintenance tools the docs give as `docker compose exec` commands |

Everything is in `~/FrameFound`:

| | |
|---|---|
| `framefound.env` | every setting, with comments |
| `pgdata/` | the catalogue (Postgres) |
| `data/` | thumbnails, frames, transcripts, `backups/` — and the bulky files too, until they have a store |
| `models/` | the vision and transcription models, and the AI models you add (`library/`) — or, once the library is in the store, a copy of each one that has a role (`local-ai/`) |
| `runtimes/` | the software that runs AI models, one folder per version ([Local AI models](#local-ai-models)) |
| `logs/` | one log per service |
| `src/` | the code |

## Updating

```bash
framefound update
```

`framefound update` backs up the catalogue, then pulls the latest release
into `~/FrameFound/src`, rebuilds and restarts. The backup comes first
because a release that changes the shape of the catalogue changes it as
FrameFound starts, on your real data, and the nightly backup may be most
of a day old. It is the same backup `framefound backup` takes, kept and
copied off the Mac the same way ([Backups](#backups)), and the update says
where it was written (`Backup written: …/framefound-….tar.gz`).

**If the backup fails**, the update stops before anything is pulled or
changed, and says why. Put that right (most often PostgreSQL is not
running: `framefound doctor` says) and update again. Only if a backup
cannot be taken and the update cannot wait, `framefound update
--skip-backup` updates without one. Only the backup on the Mac counts
here: if the copy off the Mac fails (the NAS is full or drops part way),
the update says so, names the backup it wrote on the Mac, and goes on.

**If a file was edited on this Mac** and the new release changes the same
file, the update stops before anything is changed. It names the file, says
that an update never overwrites an edit, and gives the lines to type: put
the edit to one side (`git -C ~/FrameFound/src stash`), update, then drop
it (`git -C ~/FrameFound/src stash drop`) if the release already does what
the edit did, or make the change again by hand. It never discards an edit
for you. Do not put a set-aside edit back with `git stash pop`: where the
edit and the release changed the same lines, git writes both versions into
the file and FrameFound cannot start from it
(`git -C ~/FrameFound/src checkout HEAD -- <file>` puts the release's file
back; the edit stays in the stash). An edit to a file the release does not
touch is simply kept. A checkout with changes of its own committed on this
Mac, which the release does not have, is told so, with the line that puts
them on top of the release. If git stops for any other reason (no network,
say), the update says FrameFound is running as it was, with git's own
message above. Should that happen just after an edit was put to one side,
the edit is out of the code and the release never arrived: that is the one
time `git -C ~/FrameFound/src stash pop` is right, because nothing came in
for it to collide with. It puts the edit back until the update can be
tried again.

**Once only: an install made before this fix** — the first Mac install,
2026-11-13. That release's `update` cannot explain a refused pull: it ends
on git's own message and `framefound: git failed (exit 1)`, with nothing
changed. The Mac it ran on has the locale fix as an edit by hand to
`apps/server/framefound/native/services.py`, which this release changes
too, so its first update is refused. The release contains that fix, so
nothing of the edit is needed. If `framefound update` was tried before
any of this and failed, nothing was changed: start here all the same. On
that Mac, once, one command at a time, reading what each says before
typing the next.

That first update runs the release already on the Mac, whose `update`
does not back up before it pulls (backing up first arrives with this
release), and this release's catalogue changes run on your real data as
it starts. So step 1 is not optional.

1. Take a backup:

```bash
framefound backup
```

2. Put the edit to one side:

```bash
git -C ~/FrameFound/src stash
```

3. Update:

```bash
framefound update
```

4. **Only if the update succeeded** — it ends with `FrameFound is
   running:` and an address, and no line beginning `framefound:` and
   ending `failed` — throw the edit away. The release has the fix in it
   now.

```bash
git -C ~/FrameFound/src stash drop
```

**If the update failed**, do not type that line, and do not restart
FrameFound or the Mac. Put the edit back instead:

```bash
git -C ~/FrameFound/src stash pop
```

That puts the locale fix back, so PostgreSQL still starts. It matters
because every service reads the file each time it starts: while the edit
is set to one side, a restart, a reboot, or launchd restarting a service
that stopped would start PostgreSQL without the fix, which is the original
fault (it does not come up, and nor does the catalogue). Then try the four
steps again another day, from the backup.

That is for the usual failure, which ends `framefound: git failed (exit
1)`: git could not fetch the release (no network, say), so nothing came
in and the edit has nothing to collide with. **If it got further** (it
ends `framefound: uv failed`, or with a message from the new release's
install), the release is in with its own fix: do not `stash pop`, which
would write the edit and the release into the same lines; run
`framefound update` again, and type step 4 when it succeeds. To tell the
two apart, ask whether the release's fix is in the code:

```bash
git -C ~/FrameFound/src grep -l LC_ALL -- apps/server/framefound/native/services.py
```

It prints nothing when the release never arrived (so `stash pop`), and
the file's name when it did (so update again).

After step 4, `framefound doctor`'s `locale` line reads `en_US.UTF-8 for
every background service`.

From this release on, an install explains itself.

## Bulk storage

The Mac's own disk is small and shared with Adobe, so the large, rarely
read files go to the store: video proxies, slideshow videos, listing
exports, basemaps, and a second copy of each backup, each in a folder of
its own inside it. What every page reads — thumbnails, frames, transcripts
— and the catalogue itself stay on the Mac, where they are fast.

`framefound storage` shows where each kind is, how much there is, and
whether its drive is connected. The **Storage** page in the web app shows
the same, and is where things are changed without a terminal:

- **No store yet?** Set the bulk folder on the Storage page, or run
  `framefound install --store "/Volumes/Photos/Framefound"` (the whole
  install again, so a few minutes). Nothing is moved: a kind with no files
  yet starts in the store, and one that has files stays on the Mac until
  you move it.
- **Move one kind** — on the page, or
  `framefound storage move renders "/Volumes/Photos/Framefound/renders"`.
  It copies (never deletes), checks the copy and switches over, with
  nothing restarted. The page shows how far it has got and can be closed
  meanwhile. The old copies stay until you choose Remove old copies there.
  One move runs at a time, from the page or the terminal, and a move is
  refused up front if the files will not fit — moving something back onto
  the Mac's own disk included.
- **One kind somewhere else** works the same way: proxies on an external
  SSD for faster scrubbing while the rest stays on the NAS, say.
- **Change the store itself** the same two ways. Editing
  `FRAMEFOUND_BULK_DIR` in `framefound.env` does not: the folder is
  recorded in the data folder when it is set, and that record is what is
  followed.

The store is a share that can be away. While it is, only what lives there
stops, and says so — a slideshow's video "is not connected" rather than
gone — and everything else carries on. What was asked for meanwhile is
asked for again when the share is back; nothing restarts by itself:

- a slideshow or an export started while it was away stopped with that
  reason: press Render or Export again;
- clips that arrived meanwhile have no proxy: `framefound proxies rebuild`
  makes them;
- a basemap that finished downloading meanwhile is kept on the Mac: choose
  Download again and it is put in place, not downloaded twice.

FrameFound puts a small marker file in each of its folders and writes only
where it finds one, so a share that is not connected can never be mistaken
for an empty folder and filled on the Mac's own disk. The marker also keeps
the scanner out: the store can sit on the same drive as your photographs
and is never catalogued. The details are in
[storage.md](storage.md#bulk-storage-a-folder-per-kind).

## Proxies

Proxies are most of the bulk by size. They live in the store's `proxies`
folder unless you say otherwise:

```bash
framefound proxies move "/Volumes/Fast SSD/FrameFound Proxies"
```

copies the proxies (never deletes), checks the copy, switches FrameFound
over and restarts the workers; the command prints how to remove the old
copies. `framefound proxies` shows where they are now. It is the older of
two ways to do the same thing — the Storage page and `framefound storage
move proxies <folder>` restart nothing — and whichever was used last is the
one that stands. `install.sh --store … --proxies …` sets both at once.

An install made with `--proxies "/Volumes/Photos/Framefound/proxies"`
before there was a store needs no move: set the bulk folder to
`/Volumes/Photos/Framefound` and the proxies are already in their place.

If a proxy drive is lost rather than moved: `framefound proxies move <new
folder> --empty`, then `framefound proxies rebuild`. The rebuild queues
every proxy that should exist and does not: the ones on record whose file
is gone, and the videos that never got one because the store was away when
their turn came.

## Backups

A catalogue backup is taken nightly into `~/FrameFound/data/backups` (the
newest 14 are kept). That is the same disk as the catalogue, so each one is
also copied off the Mac: into the store's `backups` folder, with nothing to
set. Without a store — or to keep the copies somewhere else — name a folder
in `framefound.env`:

```
FRAMEFOUND_BACKUP_COPY_DIR=/Volumes/Intel/FrameFound Backups
```

`framefound doctor` warns until one or the other is in place, and when the
folder is not connected (that night's copy is skipped and said so; the
backup on the Mac is still taken). Each archive includes the settings file,
and with it the key that unlocks stored secrets — treat the folder
accordingly. Originals are never in a backup; derived files are rebuildable.

Each archive also holds the record of where the bulky files live. On a new
Mac, `framefound restore <archive>` puts that record back with the
catalogue, so the proxies and slideshows on the NAS are found again rather
than looked for on the new Mac's disk; `framefound storage` shows the
result. An install that already has a record keeps its own.

## Mac settings

- **Never sleep.** System Settings → Energy: *Prevent automatic sleeping*
  and *Start up automatically after a power failure*. `framefound doctor`
  checks the first.
- **Stay logged in.** The services run in your login session, because that
  is where the NAS is mounted. After a restart they start when you log in
  (turn on automatic login if the Mac should come back unattended; FileVault
  has to be off for that).
- **Reconnect the NAS at login.** Add every share that holds a library, or
  the store, to System Settings → General → Login Items. Without that the
  libraries read as offline after a restart until each share is opened in
  Finder again.
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

## Local AI models

AI models that run on this Mac itself, at no cost per use and with nothing
leaving the building. The plan and its reasons are in
[local-models.md](local-models.md); this is how to use it.

> **Status (2026-11-13):** the manager, Compare and the second program that
> runs models (llama.cpp, for GGUF builds) are built and tested, but have not
> yet run on a Mac or downloaded a real model. Compare only measures:
> nothing a model answers in a comparison reaches the catalogue. A model can
> also suggest a colour look for a clip (**Suggest a look** in the **Look**
> card on a clip's page), which is used only if you choose it, and
> straightening, captions and file names for a listing's photographs (**Ask
> the local model** on the listing's page), which are applied only to the
> photographs you tick
> ([ai-editing-models.md](ai-editing-models.md#auto-edits-modes)). Both are
> built and tested and, like the rest, not yet run on a Mac. So is a rough
> cut drafted by the model that holds the Editor role (**Cuts** in the top
> menu, then **Draft a cut**; or **Draft a cut…** on clips ticked in
> Search, and **Draft a cut from this folder** on a clip's page or a folder
> in Browse;
> [local-models.md](local-models.md#rough-cuts-from-the-local-editor)).
> Tagging and scoring by the Looker is built and tested too, and like the
> rest has not yet run on a Mac: a review of a folder when an administrator
> asks for one (**Review with AI**, on a folder in Browse's **Folders** view
> or on a library on the Libraries page) and looking at new footage in a
> library whose box is ticked there. What it said about an item is in the
> card **What the local model saw** on the item's page, what it said about
> each shot is listed under **Shots** on a clip's page, and its score shows
> in Browse, Search and a listing's suggested removals
> ([local-models.md](local-models.md#reviewing-a-folder-on-request)).
>
> **Nothing on this page's local AI has run with a real model**, on a Mac or
> anywhere: every answer in the tests is a stand-in's.

**Everything is done on the AI models page** (Manage › AI models). No
terminal, no settings file and no release is needed to add a model, give it
a job, update the software that runs it or take it away. The page reads, top
to bottom:

| Section | What it is for |
|---|---|
| **Getting started** | Shown until the first model answers a Test: the four steps below, each ticked off as it is done |
| **Right now** | What the models are doing: the model in memory (and **Unload now**), the work waiting and running, and **Pause**, which unloads the model and lets none run until **Resume** |
| **Who does what** | The two jobs and which model has each: the **Looker** (looks at pictures) and the **Editor** (works from text). Giving a job to a model copies it onto this Mac first; the model that had the job keeps it until the copy is done |
| **Models** | Every model added: who made it, its licence, its size, the memory it needs and whether it fits by day or only overnight. **Test** asks it one question — about the newest photograph in the catalogue, for a model that reads pictures — and shows the answer, how long it took and the memory it used. **Remove** frees its space |
| **Compare on your footage** | **Compare models** opens Compare: installed models asked the same question about the same fixed sample of your footage, their answers side by side, and how far each agrees with the tags you decided and the shots your finished cuts used — as counts, never a score. You record which model should look, or that none is good enough yet. How to read it: [local-models.md](local-models.md#comparing-models) |
| **Add a model** | Paste a model's name from Hugging Face and press **Check**. Before anything downloads it says who published it and who made the model it was built from, whether that is allowed, the licence, the size, whether it fits the memory budgets and what will run it. **Download** shows a bar you can leave and come back to; **Cancel** and **Resume** keep what has arrived |
| **Software that runs models** | One row per program: MLX-VLM, which runs MLX models, and llama.cpp, which runs GGUF builds and is needed only for a model that has no MLX build. Each shows the version in use, the newest one, **Install**, **Update**, **Roll back** to the version before and **Install a particular version**. An update is installed beside the old one and checked before it is switched to, so a failed one changes nothing |
| **Sharing this Mac** | How much memory a model may use by day (8 GB) and overnight (20 GB, 21:30–05:30), and how long an idle model stays loaded (5 minutes). A model too big for the daytime budget waits for 21:30, and its Test says so before you press it |
| **Approved publishers** | The three lists that decide which models may be added: **makers** (who trained a model: `google`, `allenai`, `nvidia`, `apple` to start with), **converters** (who packaged it for the Mac: `mlx-community`) and **blocked**, which wins over both. Also the Hugging Face token, for a model whose maker asks you to accept their terms first; it is stored sealed and never shown again |

**The first time,** on the page:

1. **Software that runs models › Install MLX-VLM.** It needs the internet
   and takes a few minutes.
2. **Add a model:** `mlx-community/gemma-4-e4b-it-4bit` (Gemma 4 E4B, about
   5 GB, which fits by day). **Check**, then **Download**.
3. **Who does what:** give it the **Looker** job.
4. **Test** it.

The Editor's first model is `mlx-community/gemma-4-12B-it-OptiQ-4bit`. It
needs about 10 GB, so under the 8 GB daytime budget it runs only overnight;
raise the daytime budget to 11 GB on the page if it should run by day.

**Where things are kept.** The model library — every model added — is in
the store, `models/library/` (on the NAS), and appears on the Storage page as
*Model library*, where it can be moved like any other kind. A model that has
a job is also copied into `~/FrameFound/models/local-ai/`, so it loads
quickly and keeps working when the NAS is away; taking the job away frees
that copy. Without a store, the library is `~/FrameFound/models/library/`
and nothing is copied. Downloads arrive in `~/FrameFound/data/tmp/` first
and are checked against what Hugging Face says each file should be before
they are put in the library. The software that runs models is in
`~/FrameFound/runtimes/`, one folder per program and inside it one per
version (`runtimes/mlx-vlm/0.7.12/`, `runtimes/llama-cpp/0.9.0/`), so
updating it can never break FrameFound itself.

**The second program, llama.cpp.** Most community conversions of a model
come as GGUF builds, which MLX-VLM cannot run; llama.cpp can. Nothing asks
for it until a GGUF model is added, and its row says "Nothing you have added
needs it" until then. **Install** fetches the program FrameFound needs from
llama.cpp's own releases on GitHub — the archive built for Apple-silicon
Macs, checked against the checksum GitHub lists for it (an archive without
one is refused) — into `runtimes/llama-cpp/<version>/`; Homebrew plays no
part, so `brew upgrade` can never change it. llama.cpp brings out a
numbered version every few weeks (0.9.0) and a daily build most days
(b12318): the page offers the numbered ones as updates and shows which
daily build each was cut from ("0.9.0 (build b12110)"); **Install a
particular version** also takes a daily build by its number, for a model
that needs something newer, and while a daily build is in use no update is
offered. GitHub is asked only when **Check now** or **Install** is pressed;
it limits how often a machine may ask, and the page says so when it has.

Checking a GGUF repository lists every **build** it holds (Q4_K_M, Q8_0, …),
smallest first, each with its size, the memory it needs and whether it fits
by day, overnight or not at all; one is suggested, and choosing another
checks it again with that build's figures. Only the chosen build's model
file, its part that reads pictures (if it has one) and its licence text are
downloaded. Two builds of one model are two models, each with its own row.
A GGUF publisher is never approved by default: add one under **Approved
publishers** (as a converter) only once you know who they are. A model on
llama.cpp that has not been through **Compare** is asked about once when it
is given a job; one click gives it the job anyway, and its row says "not
compared yet" until a comparison finishes.

**What is allowed onto the Mac.** Only the files a model is made of
(weights and their settings), each checked as it arrives; never program
code from a model's page, and a model that needs some is refused. A model
whose publisher is blocked, or that was built from a blocked publisher's
model, is refused however it is packaged.

**Two more services** run alongside the rest:

| Service | What it does |
|---|---|
| `model-server` | Loads one model when there is a question for it and unloads it after the idle minutes. The program that runs the model is its child, on this Mac only (port 54806); the server itself answers only this Mac (port 54805) |
| `worker-local-ai` | The work: downloads, copies onto this Mac, installs of the software, and everything a model is asked — Tests, comparisons, folder reviews, looks for clips and listings, rough-cut drafts. One job at a time, and the lowest priority of anything FrameFound runs, so Adobe always comes first |

**After `framefound update`** an existing install has: three new lines in
`framefound.env` (`FRAMEFOUND_RUNTIMES_DIR`, the line that turns the page on,
and the two ports), each with a comment saying what it is; the `runtimes/`
folder; and the two services, started. Nothing else changes, and the page
then begins at step 1.

**`framefound doctor`** gives local AI a few lines of its own, all warnings
at most (nothing else waits for a model): the runtimes folder and this
Mac's memory; `uv`, which installs the software; the version of MLX-VLM in
use and the one kept to go back to; llama.cpp's, once it is installed or a
model needs it (with the daily build it comes from); whether the model server answers; what
holds the model's port, if anything does; whether each job's model is on
this Mac; a pause somebody forgot; and whether a memory budget leaves under
4 GB for everything else. If it says the version in use is recorded but its
program is missing (its folder was emptied by hand), **Software that runs
models › Install a particular version** with that same version number
installs it afresh; nothing else needs doing.

**When it is first run on the mini,** two things are worth looking at once
by hand. After the first Test, compare the memory it reports with Activity
Monitor's figure for the `python` process (done 2026-11-14: Test said
9.2 GB for Gemma 4 12B and 5.7 GB for E4B, Activity Monitor 9.08 and
5.58 GB, so the figure is right and a little high). And while a model is loaded,
`lsof -nP -iTCP:54806 -sTCP:LISTEN` should show `127.0.0.1`, never `*`: the
model must not be reachable from the network.

## Known limits

- **Run on one Mac so far, and only in part** (the status note at the top
  says which parts). Updating and local AI models have not run on a Mac.
- **Blackmagic RAW.** The BRAW decoder is a Linux build
  (`infrastructure/braw`). BRAW clips already catalogued keep their posters;
  new ones get none here until the decoder is built for macOS.
- **Signing in to Tailscale is done in the Tailscale app.** The Security
  page's Tailscale card reads the app on a Mac: it says whether this Mac is
  on your tailnet, under what name, and the addresses to open from your
  other devices. The app cannot hand the page a sign-in link, so the card's
  sign-in button only passes on one the app already holds; sign in from the
  app's menu bar icon (Log in) instead. When the app is signed in but
  turned off, turn it on from the same icon (Connect); the card's "Turn
  Tailscale on" says so rather than switching it. (A Homebrew `tailscaled` is read
  through its socket instead, and its sign-in link comes from the page as
  on Linux.)
- **The Storage page's network-drive mounting** talks to Linux-only pieces
  and is unavailable. Use Finder. (Where the bulky files live, and moving
  them, is the part of the Storage page that does work here.)
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

Two of its lines came out of the first real install:

- **`locale`**, after the lines for the services: the language setting
  every background service is started with. `ok` reads `en_US.UTF-8 for
  every background service`. It **fails** when the services would be started
  with none — the state in which PostgreSQL will not start and
  `~/FrameFound/logs/postgres.log` repeats "postmaster became multithreaded
  during startup" — and `framefound update` brings the release that sets
  one. It is a warning when this Mac does not have the locale it is given.
  It reads what the services are given, never your Terminal's own setting,
  which is why the Terminal working proves nothing here.
- **`rsync`**, after the lines for ffmpeg and the other tools: which rsync
  `framefound proxies move` and `framefound migrate` would run. `ok` shows
  Homebrew's by its path. A warning says the one found "is the one that
  came with macOS, not Homebrew's", or that none is installed: run
  `brew install rsync` (or `framefound install`, which now does). Nothing
  else uses rsync, so it is never more than a warning.

If a library reads as offline after the Mac restarted, its share is not
connected: open it in Finder, and add it to Login Items
([Mac settings](#mac-settings)) so it reconnects by itself.

If a library shows as unreachable although Finder can open it, macOS is
withholding network volumes from background services: System Settings →
Privacy & Security → Full Disk Access, add
`~/FrameFound/venv/bin/python` (press ⌘⇧G in the file picker to type the
path), then `framefound restart`. The same goes for the store: if the
Storage page says a kind is not connected while `framefound storage`, run
from Terminal, says it is, the services are the ones being kept out.

## Removing it

```bash
framefound uninstall
```

removes the background services and nothing else. Delete `~/FrameFound` to
remove the catalogue, settings and derived files too. Homebrew's packages
are left for you (`brew uninstall …`).
