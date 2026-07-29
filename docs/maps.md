# Maps and address lookup

FrameFound works out **where** your media was shot, groups it into places, and
lets you browse a shoot by location. All of that works with no third party
involved.

Google Maps is an **optional** layer on top: a real aerial basemap, and street
addresses for the handful of places your folder names cannot describe. It is
off until you turn it on, and it is the only part of FrameFound that talks to
an outside service during normal use.

---

## What works without Google

| Feature | Needs Google? |
|---|---|
| Reading GPS from camera/drone metadata | No |
| Inferring location for non-GPS cameras | No |
| Grouping assets into places | No |
| Naming places from your folder structure | No |
| Browsing a place, filtering, paging | No |
| Seeing places positioned relative to each other | No — drawn locally |
| Aerial/satellite basemap | **Yes** |
| Street address for an unnamed place | **Yes** |

Without a key, the Places page draws a scatter plot computed from the
coordinates already on your machine. It shows how jobs sit relative to one
another. It is not a map, and it is not pretending to be one.

---

## What turning it on actually sends

Be clear-eyed about this before enabling it.

**Basemap.** Your browser requests map tiles from Google for the area you are
looking at. Google learns roughly where your shoots are and when you look at
them. It does not receive your media, filenames, or catalogue.

**Address lookup.** The server sends an exact latitude and longitude to
Google's Geocoding API and gets back an address. This is a precise disclosure
of one location per lookup — but only for places your folders could not name,
and each coordinate is only ever sent **once** (see [caching](#caching)).

Neither sends media, file paths, or anything about your catalogue.

If you would rather send nothing at all, do not configure the keys. Places
remains fully functional.

---

## Setting it up

### 1. Create a Google Cloud project

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a project (or pick an existing one).
3. Enable **billing**. Google requires a billing account even within the free
   tier; see [cost](#cost) below.

### 2. Enable the two APIs

Under **APIs & Services → Library**, enable:

- **Maps JavaScript API** — draws the basemap.
- **Geocoding API** — turns coordinates into addresses.

Enable only the one you want if you only want one. They are independent.

### 3. Create **two** keys, not one

This is the part people get wrong, so it is worth stating plainly:
**one key cannot do both jobs safely.**

A browser key must be readable by the page in order to load the map, so its
only meaningful protection is an HTTP referrer restriction. A referrer
restriction breaks server-to-server calls, because the server has no referrer.
Use one key for both and you must either drop the restriction (leaving a key
anyone can lift and bill to you) or break geocoding.

So: two keys.

| | Maps browser key | Geocoding key |
|---|---|---|
| Used by | The page in your browser | The FrameFound server only |
| Visible to | Anyone signed in to FrameFound | Nobody — never leaves the machine |
| Restrict by | **HTTP referrer** | **IP address** |
| Restrict to API | Maps JavaScript API | Geocoding API |

**Browser key — Application restrictions → Websites.** Add the origin you use
to reach FrameFound, for example:

```
https://framefound.example.com/*
http://192.168.1.193:8080/*
```

**Geocoding key — Application restrictions → IP addresses.** Add the public IP
your server makes outbound requests from. If you are unsure, leave it
unrestricted *briefly*, confirm lookups work, then check the outbound IP and
lock it down.

On both keys, set **API restrictions** to the single API each one needs. A key
scoped to one API is worth far less to anyone who steals it.

### 4. Add the keys to FrameFound

Go to **Security → Maps & geocoding**:

1. Paste the browser key into **Maps browser key**.
2. Paste the geocoding key into **Geocoding key**.
3. **Save keys.**
4. Tick **Show a Google basemap on Places**.
5. Tick **Look up an address for places your folders don't name** if you want
   addresses too.

The checkboxes stay disabled until the matching key is stored, so you cannot
switch on a basemap that has no key behind it.

### 5. Check it

Open **Places**. You should see an aerial map with a numbered marker per
place. Click a marker to open that place as a library view.

If the map does not appear, the page falls back to the scatter and says so.
See [troubleshooting](#troubleshooting).

---

## How your keys are stored

- Both are **sealed at rest** with the same Fernet key that protects your TOTP
  seeds and DNS token, derived from `FRAMEFOUND_SECRET_KEY`.
- The settings API **never returns either key** — only whether one exists. The
  field shows `•••••• stored`, never the value.
- The browser key is served to the page only through `/places/map-config`, and
  only to an authenticated session, and only once the basemap is switched on.
  It is not baked into the built JavaScript bundle, so an unauthenticated
  visitor cannot lift it.
- The geocoding key is never sent to a browser under any circumstance.

Losing `FRAMEFOUND_SECRET_KEY` means the sealed keys cannot be read; re-enter
them.

---

## How naming works

A place gets its name from the first of these that produces one:

1. **Your folder structure.** If most of a cluster's files live in
   `2026/Feb 4 - 513 Jacobs Rd/`, the place is called `Feb 4 - 513 Jacobs Rd`.
   Where files straddle subfolders (`a-roll/`, `stills/`), the name comes from
   the shared parent, so the place is named for the shoot rather than for
   whichever subfolder happens to be larger.
2. **Google, if enabled.** Only for clusters step 1 could not name.
3. **`Unknown location`** if neither applies.

Places named by Google carry a small **from map** badge, so you can always
tell which names came from your own filing and which were looked up.

**Folder names win on purpose.** Your directories say
`Feb 4 - 513 Jacobs Rd` — a street address you chose. Google, for the same
coordinate, is likely to answer with a road name or a town. The name you
already gave it is more precise and more useful, so it is never overridden.

### Caching

Every successful lookup is stored in the `geocode_cache` table, keyed on the
coordinate rounded to five decimal places (about a metre).

This matters because place clusters are **recomputed on demand** — their
centroids shift slightly as assets are added. An exact key would miss on every
recomputation and re-bill the same lookup forever.

Lookups that legitimately return nothing (a coordinate in the middle of a
field) are cached too, and retried after six hours, so a blank result is not
re-requested on every page load.

To force a fresh lookup:

```sql
DELETE FROM geocode_cache WHERE cache_key = '41.87810,-87.62980';
```

---

## Cost

Google's Maps Platform includes a recurring monthly free allowance. For a
self-hosted catalogue used by one person or a small team, normal use sits well
inside it:

- **Basemap**: one map load per visit to the Places page.
- **Geocoding**: one request per *unnamed* place, *once ever*, thanks to the
  cache. A library where folders are named after addresses may make zero
  requests.

Set a **budget alert** in Google Cloud Billing anyway. It costs nothing and it
is the difference between noticing a runaway and finding out on a statement.

Concurrency is capped at 4 simultaneous geocoding requests, so a library with
many unnamed clusters ramps rather than arriving all at once.

---

## Troubleshooting

**The map area shows the scatter and says Maps could not load.**
Almost always the referrer restriction. The origin in your browser's address
bar must match a pattern on the browser key. `https://example.com/*` does not
match `http://192.168.1.50:8080`. Open the browser console and look for a
`RefererNotAllowedMapError` or `ApiNotActivatedMapError`.

**Map loads, but says "for development purposes only" over grey tiles.**
Billing is not enabled on the Google Cloud project.

**No addresses appear for unnamed places.**
Check, in order:
1. Is a geocoding key saved? (Security → Maps & geocoding)
2. Is **Look up an address…** ticked?
3. Are there any unnamed places at all? If every place is named from folders,
   nothing will be looked up — that is the intended outcome, not a fault.
4. Check the server log for `geocode.failed`; it records Google's own status
   (`REQUEST_DENIED` usually means an IP restriction or a disabled API).

**Everything worked, then stopped.**
Check the Google Cloud quota page. Also check that your outbound IP has not
changed if you restricted the geocoding key by IP.

**Turning it all off.**
Security → Maps & geocoding → **Remove both keys**. This also disables the
basemap. Places keeps working; cached addresses stay until you delete them.

---

## Switching to a different map provider

Google is not baked in, and there is a reasonable chance you will want to move
— to keep coordinates in-house, to avoid a billing account, or because
somebody else's pricing changed.

The seams are already in the right places:

- **`framefound/media/geocoding.py`** is the only file that speaks Google's
  geocoding protocol. `reverse_geocode_many()` takes coordinates and returns
  addresses; the caching, batching, concurrency cap and failure handling
  around it are provider-agnostic. A different provider means a new
  `_lookup_one()` and `_short_address()`, nothing else.
- **`framefound/media/maps_store.py`** holds two keys and two toggles. A
  provider field would slot in beside them.
- **`components/PlaceMap.tsx`** already renders two ways — Google basemap or
  local scatter — so a third rendering path is an additional branch, not a
  rewrite. Nothing else in the UI knows Google exists.
- **`geocode_cache`** is keyed on coordinates, not on a provider. Cached
  addresses survive a provider change; delete rows if you want them re-looked
  up by the new one.

Candidates worth weighing when the time comes:

| Option | Basemap | Geocoding | Notes |
|---|---|---|---|
| **Self-hosted tiles** (OpenMapTiles / Protomaps + MapLibre) | Yes | No | Nothing leaves your network. Real setup cost and disk. The most on-brand option for a self-hosted catalogue. |
| **Nominatim, self-hosted** | No | Yes | Offline reverse geocoding, no per-lookup cost, no data sent out. Wants meaningful RAM and disk for a full planet extract; a regional extract is far lighter. |
| **Nominatim, public** | No | Yes | Free but strictly rate-limited and not intended for bulk use. Acceptable for a handful of lookups; do not point a large library at it. |
| **Mapbox / MapTiler** | Yes | Yes | Commercial like Google, different pricing, same privacy trade. |
| **Esri / ArcGIS** | Yes | Yes | Strong aerial imagery, which is what actually matters for property work. |

For this catalogue specifically, **self-hosted tiles plus a regional Nominatim
extract** would remove the outbound dependency entirely, and the folder-name
naming already does most of the work Nominatim would be asked for. Worth
revisiting once the current setup has proved what is actually used.

Tracked in the roadmap under "Maps provider — revisit".

---

## Reference

| | |
|---|---|
| Configure | Security → Maps & geocoding |
| Settings API | `GET`/`PUT /api/v1/places/maps-settings` (PUT is admin-only) |
| Runtime config | `GET /api/v1/places/map-config` |
| Places | `GET /api/v1/places` |
| Assets near a point | `GET /api/v1/assets/near?lat=&lon=&radius_km=` |
| Cache table | `geocode_cache` |
| Server code | `framefound/media/geocoding.py`, `framefound/media/maps_store.py` |
| UI code | `components/PlaceMap.tsx`, `components/MapsSettingsCard.tsx` |

Related: [Location inference](location.md) — how assets without GPS get a
position in the first place.
