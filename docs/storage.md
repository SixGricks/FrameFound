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

## The low-space guard

Generation stops while headroom remains, rather than filling the disk:

```bash
FRAMEFOUND_MIN_FREE_GB=5
```

Below this threshold, thumbnail and proxy jobs pause and log a clear reason
instead of writing. The database, and therefore your catalog, is never put at
risk by preview generation. Free space is shown on the **System** page; work
resumes automatically once space is available and the assets are reprocessed.
