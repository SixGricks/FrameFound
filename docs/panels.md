# Editing panels — Premiere Pro and Lightroom Classic

Two clients, one contract. Both authenticate with a **panel token** and talk to
`/api/v1/panel`. Neither can write to the catalogue, and neither can touch an
original.

They are separate codebases because the applications share no extension
technology: Premiere is UXP (JavaScript, manifest-declared permissions),
Lightroom Classic is the Lightroom SDK (Lua, loaded from a plugin directory).
There is no version of this where one codebase serves both, and pretending
otherwise would produce something worse than either.

---

## What they do

| | Premiere | Lightroom Classic |
| --- | --- | --- |
| Search the catalogue | yes | yes |
| Import at workstation paths | direct, falling back to FCP7 XML | adds by reference |
| Diagnose a wrong path profile | via the panel's note | dedicated menu item |
| Write to FrameFound | never | never |

The common thread is the **path profile**. The catalogue stores
`/media/gelco/2026/a001.mp4`; a Windows edit bay sees `Z:\2026\a001.mp4`. The
`path_mappings` table has held those per-workstation profiles since Milestone 2
and nothing consumed them until now. Without translation, an import produces a
project full of offline media — which looks like a broken server, a missing
drive, or a corrupt project, and almost never like a path bug.

---

## Panel tokens

Created in **Security → Panel tokens**. One per machine.

- **Shown once.** Only a SHA-256 hash is stored, so a database leak exposes no
  working credential and there is no endpoint that can redisplay it.
- **Read-only by default.** `export` — permission to produce a bin the host
  application will act on — is a separate opt-in.
- **No scope grants a write** to the library or the catalogue. That is the
  promise the short scope vocabulary exists to keep readable.
- **Revocable**, listed with its prefix and when it was last used, next to
  browser sessions. A credential that cannot be revoked from the machine it
  grants access to is a leak with a delay on it.
- **Optional expiry.** Off by default, because an edit bay used twice a year
  should not silently stop working.

Why not the session cookie: a cookie in a desktop extension cannot be told
apart from the operator's own browser in an audit, cannot be revoked
independently, and rides along on every request the host application happens to
make.

---

## Installing the Premiere panel

Requires Premiere Pro 25.0 or newer (UXP panels for Premiere arrived late; on
older versions use the FCP7 XML export from the web UI instead).

1. Install the **UXP Developer Tool** from Adobe's Creative Cloud app.
2. In UDT: **Add Plugin** → select `apps/panel-premiere/manifest.json`.
3. **Load** it. The panel appears under **Window → Extensions → FrameFound**.
4. Open **Settings** in the panel, enter the server address and the token.

Before loading, edit `manifest.json` so `requiredPermissions.network.domains`
lists *your* server. UXP refuses undeclared hosts, and the failure looks like a
network error rather than a permissions one:

```json
"domains": ["http://framefound.local:8080"]
```

For a permanent install rather than a developer load, the panel needs to be
packaged as a `.ccx` and signed — Adobe's process, not covered here.

### The import fallback is not a consolation prize

Premiere's UXP API for project manipulation is narrower than ExtendScript's and
has moved between versions. The panel attempts a direct `importFiles` and, on
any failure, writes an FCP7 XML for the editor to import by hand. That fallback
is the route [ADR-0019](adr/0019-premiere-panel.md) chose to ship first: it
works in every NLE rather than only current Premiere, and it is what makes the
panel useful on a version whose API does not cooperate.

---

## Installing the Lightroom Classic plugin

1. Copy `apps/plugin-lightroom/framefound.lrdevplugin` somewhere permanent.
2. Lightroom → **File → Plug-in Manager → Add**, select that directory.
3. With FrameFound selected in the manager, enter the server address and token,
   then **Test connection**.

Two menu items appear under **Library**:

- **Search FrameFound…** — search, then add the results to the Lightroom
  catalogue. Photographs are added *where they are*: Lightroom references them
  in place. Nothing is copied or moved, which is the only behaviour consistent
  with FrameFound never writing to originals.
- **Show FrameFound paths for selected photo** — the diagnostic. Prints what
  Lightroom sees, what FrameFound stores, and every workstation profile side by
  side. This is how somebody notices the Windows profile points at the wrong
  drive letter.

### Two Lightroom SDK constraints worth knowing

`LrHttp` is **synchronous** and must not run on the main task. Every call here
is wrapped in `LrTasks.startAsyncTask`; getting that wrong freezes Lightroom
with no error at all, which is a hard symptom to trace back to its cause.

There is **no JSON parser** in the SDK. Rather than vendor one, the handful of
fields the plugin needs are extracted with patterns. That is a deliberate
trade: a real parser is more correct in general, but this code only ever reads
responses from an API in the same repository, and a missing field surfaces as a
nil the caller already checks.

---

## What is not built

- **Marker export from transcript hits.** A search hit currently lands on the
  file; landing on the frame needs markers carried into the XML. `fcp7.py`
  already has a `Marker` type waiting for it.
- **Lightroom → FrameFound direction.** Publishing edits back, or writing
  Lightroom keywords into FrameFound tags. Both are writes, and no token scope
  permits a write today — adding one is a deliberate decision, not an
  extension.
- **Signed, packaged distribution** for either. Both install as developer
  plugins, which is appropriate for a self-hosted tool on machines the operator
  controls.

## Verified from the workstation (2026-07-31)

Before the panels were handed over, the whole path was exercised from the
Windows machine rather than from inside the VM — which is where three real
bugs turned up:

- `http://192.168.1.193:8080` reachable, `/panel/profiles` and `/panel/search`
  200 with a bearer token, **401 without one**.
- A Grick Family Storage asset resolved end to end:
  `/media/family/stef's computer files/.../IMG_0334.JPG` →
  `W:\stef's computer files\...\IMG_0334.JPG`, and `Test-Path` on that PC
  returned **True**. That is the whole feature in one line.

The three bugs, all invisible from inside the VM:

1. **`media_type` was filtered after the limit**, so asking for six images
   returned whatever happened to be an image among the six most recent assets
   of any kind — usually none.
2. **A path profile was resolved once per search, not per library.** A
   workstation mounts several shares and `profile_name` is unique per library,
   so every result outside the one library came back with no path — identical
   in appearance to a broken profile.
3. The search note now says how many results fell outside any profile, rather
   than leaving nulls to be discovered at import time.

## What has not been tested

The token layer, the path translation and the panel API are covered by tests
and verified against the live deployment. **The two clients have not been run
inside Premiere or Lightroom** — that needs the applications themselves, and a
first run should be treated as a first run. The most likely places to need
adjustment are the UXP `importFiles` signature, which has changed between
Premiere versions, and Lightroom's `LrHttp` header handling on older releases.
