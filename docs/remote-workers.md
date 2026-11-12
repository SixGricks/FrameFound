# Workers on another machine (optional, not packaged)

A **remote worker** is the same Python package running on a second machine,
taking jobs off the server's queues and writing into the server's data
store. It adds capacity and replaces nothing: stop it and the server's own
workers carry on.

This was built first, for the Mac mini, and then set aside when installing
FrameFound on the mini itself turned out far simpler ([mac.md](mac.md),
[ADR-0022](adr/0022-native-mac-install.md)). The capability is kept and
tested — it is how a second machine (a laptop with a GPU, say) could help
later — but there is **no installer for it**: this page is the reference
for setting one up by hand.

## What a remote worker needs

| Need | How | Why |
|---|---|---|
| The code | A checkout at the server's release, `pip install -e "apps/server[db,queue,scanner,media,ai]"` | Same tasks, same schema. |
| Database + queue | Reach the server's Postgres and Valkey. For the containers: add `docker-compose.remote-workers.yml` to `COMPOSE_FILE` (publishes both on the server's **loopback only**) and forward them over SSH | Valkey has no password in the containers, so it must never be published on the network. |
| Data store | The server's data folder mounted read-write (NFS or SMB), at `FRAMEFOUND_DATA_DIR` | Derivatives must land where the server serves them from. |
| Originals | The same shares the server's libraries live on, mounted read-only | |
| Path translation | `FRAMEFOUND_ROOT_MAP=/media/intel=/Volumes/intel;…` | Library roots are stored as the server's paths. |
| A name | `FRAMEFOUND_WORKER_NAME=laptop` | Shown against its jobs on the processing page. |

Then check, and start one Celery worker per lane (never two latency classes
in one worker — see the top of `processing/tasks.py`):

```bash
python -m framefound.ops.worker_check
```

```bash
celery -A framefound.worker worker -Q transcribe --concurrency=1 -n transcribe@%h
```

```bash
celery -A framefound.worker worker -Q vision --concurrency=2 -n vision@%h
```

```bash
celery -A framefound.worker worker -Q media,frames --concurrency=2 -n media@%h
```

`worker_check` prints one line per requirement — database, schema version,
queue, data store, every library through the map, tools, encoder — and
exits non-zero while any fails. A worker whose checkout expects a different
schema than the database must not be started.

## What setting a root map changes

`FRAMEFOUND_ROOT_MAP` is what makes a process a remote worker. Leave it
unset on the server. With it set:

- **It never marks a file missing.** On the server a file absent from a
  reachable library is flagged missing; on another machine the same
  evidence could as easily be its own mount or mapping. Only the server's
  scanner decides "missing".
- **It writes nothing unless the real data store is mounted.** The API
  writes `.framefound-data-store` into the store at start-up; a remote
  worker checks for it before every task. A failed mount leaves an empty
  folder behind, and without this check every derivative would land on the
  worker's own disk while the catalogue recorded it as made.
- **What it cannot do, it hands back.** No data store, no map entry for a
  library, or a share not mounted there: the job goes back on its queue
  (after a 30-second pause, so it cannot spin) for the server's workers,
  rather than being skipped or failed. A broken worker slows nothing down;
  it just stops helping. Each hand-back is a job row reading
  "Handed back by <name>: …".

**What a remote worker takes:** bulk ingest — `transcribe`, `vision`,
`media` (proxies) and `frames`. The operator's own work (listing exports,
slideshow renders, object removal, AI edits, basemaps) is on the `exports`
queue, which a remote worker should not consume, so nothing somebody is
waiting on depends on it.

The processing page shows a **Machines** section once a second machine has
done work, and each recent failure names its machine (`jobs.worker`).
