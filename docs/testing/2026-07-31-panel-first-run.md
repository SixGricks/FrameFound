# FrameFound Plugin Test Report
**Date:** 2026-07-31  
**Tester:** Automated via Claude computer control  
**Server:** http://192.168.1.193:8080  
**Repo:** C:\Users\wesix\OneDrive\Desktop\Media Hub  

---

## Environment

| Component | Version |
|---|---|
| Premiere Pro | 26.3.0 |
| Lightroom Classic | 14.x (Intel Nov 2025 catalog) |
| UXP Developer Tools (UDT) | Installed, connected |
| Windows | 11 |
| Panel manifest ID | com.sixgricks.framefound |
| Panel manifest host minVersion | 25.0.0 |
| LR plugin version | 0.1.0.0 |

---

## Step 1 — Create Panel Token

**Result: PASS**

Token `ffp_geuoEiLraq04Ao-94cOEe6B8ECgwDLtUTfhJVLRRjkI` generated from FrameFound server admin panel. Token is `ffp_`-prefixed bearer token; SHA-256 hash stored server-side; shown only once at creation.

---

## Step 2 — Verify Manifest Network Permissions

**Result: PASS**

`apps/panel-premiere/manifest.json` contains the following in `requiredPermissions.network.domains`:

```json
["http://framefound.local:8080", "https://framefound.local", "http://192.168.1.193:8080"]
```

`http://192.168.1.193:8080` is correctly declared. manifestVersion: 5, id: `com.sixgricks.framefound`, host: premierepro, minVersion: 25.0.0.

---

## Step 3 — Install Premiere Panel

**Result: PASS (after script fix)**

`install-premiere-panel.ps1` had a parse error on first attempt:

> `The string is missing the terminator: "."` at line 27 char 82

Cause: `>` character in a Write-Host string (`"check Window > UXP Plugins"`) was treated as shell redirection in the bash heredoc used to create the file. Fixed by rewriting the file using Python. The string was also corrected to `"Window, UXP Plugins"`.

After fix, script ran successfully. Files copied to:  
`%APPDATA%\Adobe\UXP\Plugins\External\com.sixgricks.framefound\`

Files confirmed present:
- `index.html` — 3,916 bytes
- `main.js` — 9,270 bytes
- `manifest.json` — 1,000 bytes

---

## Step 4 — Load Panel in UDT / Verify Panel Appears

**Result: PASS (after enabling developer mode)**

Initial UDT attempt showed:

> `Plugin Load Failed. No applications connected to service`

Root cause: "Enable developer mode" checkbox in **Edit → Preferences → Plugins → UXP Plugins** was unchecked. This setting is not mentioned in `docs/panels.md`.

Fix: Checked the box, clicked OK, restarted Premiere Pro. After restart, UDT connected immediately and loaded `com.sixgricks.framefound` successfully. Panel appeared in Window → UXP Plugins as "FrameFound".

Note: Passing `--enable-uxp-developer-tools` as a launch flag caused Premiere to show:

> `This file path does not exist on disk at this location. --enable-uxp-developer-tools`

This was harmless; Premiere launched anyway. The actual fix was the Preferences checkbox.

---

## Step 5 — Enter Server URL + Token, Click Save/Connect

**Result: FAIL — BUG**

Entered server URL `http://192.168.1.193:8080` and token `ffp_geuoEiLraq04Ao-94cOEe6B8ECgwDLtUTfhJVLRRjkI` in the panel's settings form. Clicked Save.

**Exact error:**

> `Permission denied to the url http://192.168.1.193:8080/api/v1/panel/profiles. Manifest entry not found.`

The domain `http://192.168.1.193:8080` IS present in `requiredPermissions.network.domains` in manifest.json. UXP in Premiere Pro 26.3.0 is rejecting a correctly declared network domain.

**Not fixed per protocol** — reporting as UXP bug in Premiere 26.3.0.

---

## Step 6 — Search and Add Clip to Premiere Timeline

**Result: BLOCKED**

Step 6 was unreachable because Step 5 failed. Panel cannot communicate with the server.

---

## Step 7 — Install Lightroom Classic Plugin

**Result: PASS**

Plugin installed via **File → Plug-in Manager → Add**, pointing to:  
`C:\Users\wesix\OneDrive\Desktop\Media Hub\apps\plugin-lightroom\framefound.lrdevplugin`

Plugin status in Plug-in Manager:
- Name: FrameFound
- Version: 0.1.0.0
- Status: **"This plug-in is enabled"**

Server URL `http://192.168.1.193:8080` and token entered in the plugin's settings section within Plug-in Manager.

---

## Step 8 — Test Connection + Verify Library Menu Items

**Result: PARTIAL PASS / FAIL**

### Menu items — PASS

Both expected items are present under **Library → Plug-in Extras**:
- FrameFound *(greyed-out section header)*
- **Search FrameFound…**
- **Show FrameFound paths for selected photo**

### Test connection — FAIL — BUG

Clicking **"Test connection"** in Plug-in Manager produced:

> **Error**  
> **FrameFound**  
> Yielding is not allowed within a C or metamethod call

Root cause: The `LrHttp` call in the Test connection handler is not wrapped in `LrTasks.startAsyncTask`. It is being called from a UI callback (C/metamethod context) where yielding is forbidden by the Lightroom SDK.

**Not fixed per protocol** — reporting as bug.

---

## Step 9 — Search FrameFound (Library → Plug-in Extras → Search FrameFound…)

**Result: FAIL — BUG**

Dialog opened correctly: "Search FrameFound" with query field, "This machine's paths:" dropdown showing **"No path profile"**, and note "Photographs are added where they already are. Nothing is copied or moved."

Entered query `church`, clicked **Search**.

**Exact error:**

> **Error**  
> **FrameFound**  
> Yielding is not allowed within a C or metamethod call

Same threading bug as Step 8. The search handler's `LrHttp` call is also not wrapped in `LrTasks.startAsyncTask`.

No results were returned. No photos were imported.

---

## Step 10 — Show FrameFound Paths for Selected Photo

**Result: FAIL — BUG**

Invoked **Library → Plug-in Extras → Show FrameFound paths for selected photo** with a photo selected (DJI_20260630181310_0323_D.JPG).

**Exact error:**

> **Error**  
> **FrameFound**  
> Yielding is not allowed within a C or metamethod call

Same threading bug. All three Lightroom menu actions (Test connection, Search FrameFound, Show paths) fail with the identical error, indicating every `LrHttp` call in the plugin is outside `LrTasks.startAsyncTask`.

---

## Summary

| Step | Description | Result |
|---|---|---|
| 1 | Create panel token | ✅ PASS |
| 2 | Verify manifest network domains | ✅ PASS |
| 3 | Install Premiere panel | ✅ PASS (script fix required) |
| 4 | Load in UDT / panel appears | ✅ PASS (developer mode checkbox required) |
| 5 | Connect panel to server | ❌ FAIL — UXP rejects declared domain |
| 6 | Add clip to timeline | 🚫 BLOCKED by Step 5 |
| 7 | Install Lightroom plugin | ✅ PASS |
| 8 | Menu items present + test connection | ⚠️ PARTIAL — menu items ✅, connection ❌ |
| 9 | Search FrameFound | ❌ FAIL — LrHttp threading error |
| 10 | Show FrameFound paths | ❌ FAIL — LrHttp threading error |

---

## Bugs to Fix

### Bug 1 — Premiere UXP: Declared network domain rejected (BLOCKER)

**Affects:** Steps 5–6  
**Error:** `Permission denied to the url http://192.168.1.193:8080/api/v1/panel/profiles. Manifest entry not found.`  
**Details:** `http://192.168.1.193:8080` is declared in `requiredPermissions.network.domains` but UXP in Premiere 26.3.0 rejects it. Likely a UXP platform bug with bare IP addresses rather than hostnames. Possible workaround: test with `framefound.local` hostname instead of IP, or check if manifestVersion 6 has different behavior.

### Bug 2 — Lightroom plugin: LrHttp called outside LrTasks.startAsyncTask (BLOCKER)

**Affects:** Steps 8–10 (all three menu actions)  
**Error:** `Yielding is not allowed within a C or metamethod call`  
**Details:** Every `LrHttp` call in the plugin is invoked from a UI callback (button handler or menu item handler), which runs in a C/metamethod context where coroutine yielding is forbidden. All network calls must be wrapped in `LrTasks.startAsyncTask(function() ... end)`. This applies to: Test connection handler, Search handler, Show paths handler.

---

## First-Run Discoveries (Not in Docs)

1. **Premiere: "Enable developer mode" checkbox required** — Edit → Preferences → Plugins → UXP Plugins → "Enable developer mode" must be checked before UDT can connect. Not mentioned in `docs/panels.md`. Panel install instructions should include this step.

2. **install-premiere-panel.ps1: `>` in string causes bash heredoc breakage** — When the script is created via a bash heredoc, the `>` character in `"check Window > UXP Plugins"` is interpreted as shell redirection. Script creation must use Python or escape the character.
