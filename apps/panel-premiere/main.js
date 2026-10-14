/*
 * FrameFound panel for Premiere Pro (UXP). panel build 0.4.0
 *
 * The panel's whole job: search the catalogue, and put the clips the editor
 * picked into the open project at paths their machine can actually open. That
 * last part is the reason the panel exists — the catalogue stores
 * `/media/gelco/...` and a Windows edit bay sees `Z:\`, so an import that does
 * not translate produces a project full of offline media.
 *
 * Written against UXP's restricted capability set on purpose (ADR-0019). No
 * Node, no filesystem beyond what the manifest asks for, and only the network
 * hosts the manifest declares. Nothing here can reach the operator's disk on
 * its own.
 *
 * Two API surfaces are used and they are handled differently:
 *   - `premierepro`  — the UXP DOM API. Availability varies by version, so
 *                      every call is guarded and falls back to an export.
 *   - FrameFound     — a stable contract under /api/v1/panel, versioned with
 *                      the rest of the API.
 */

const BUILD = "0.4.0";
// A hung server used to leave "Searching…" on screen for good.
const API_TIMEOUT_MS = 20000;
const THUMBNAIL_TIMEOUT_MS = 10000;
// The server's cap; the default of 24 left a third of the results unseen.
const MAX_RESULTS = 60;

const state = {
  server: "",
  token: "",
  profile: "",
  results: [],
  selected: new Set(),
  identity: null, // /panel/me: who the token is and what it may do
  connected: false,
};

// Thumbnails for the current results, by asset id. Every re-render used to
// fetch all of them again — selecting one of 24 results was 24 downloads —
// and never revoked the object URLs it made.
const thumbnails = new Map();

const $ = (id) => document.getElementById(id);

// --- persistence ----------------------------------------------------------
// localStorage is per-panel in UXP. The token lives here because there is
// nowhere better inside a panel; it is why the token is scoped, revocable, and
// read-only by default rather than being the operator's password.

function load() {
  try {
    state.server = localStorage.getItem("ff.server") || "";
    state.token = localStorage.getItem("ff.token") || "";
    state.profile = localStorage.getItem("ff.profile") || "";
  } catch (e) {
    /* first run */
  }
  $("server").value = state.server;
  $("token").value = state.token;
}

function normaliseServer(value) {
  // The address of the server, not of its API: a pasted ".../api/v1" would
  // otherwise become ".../api/v1/api/v1/...".
  return value
    .trim()
    .replace(/\/api\/v1\/?$/i, "")
    .replace(/\/+$/, "");
}

function save() {
  state.server = normaliseServer($("server").value);
  state.token = $("token").value.trim();
  $("server").value = state.server;
  try {
    localStorage.setItem("ff.server", state.server);
    localStorage.setItem("ff.token", state.token);
  } catch (e) {
    /* storage unavailable: the settings last until the panel is unloaded */
  }
}

function remember(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch (e) {
    /* see save() */
  }
}

// --- api ------------------------------------------------------------------

async function fetchWithTimeout(url, options, ms) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), ms);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } catch (err) {
    if (err && err.name === "AbortError") {
      throw new Error(`No answer from ${state.server} within ${Math.round(ms / 1000)} s.`);
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

async function api(path, options = {}) {
  if (!state.server) throw new Error("No server address set");
  if (!state.token) throw new Error("No panel token set");
  const response = await fetchWithTimeout(
    state.server + "/api/v1" + path,
    {
      ...options,
      headers: {
        Authorization: "Bearer " + state.token,
        "Content-Type": "application/json",
        ...(options.headers || {}),
      },
    },
    API_TIMEOUT_MS
  );
  if (!response.ok) {
    // Read the body so the server's detail string reaches the status line.
    // The server returns JSON like {"detail": "..."} on errors, but falls back
    // to plain text for anything that escapes FastAPI's exception handler.
    let detail = "";
    try {
      const raw = await response.text();
      if (raw) {
        try {
          const parsed = JSON.parse(raw);
          detail = parsed.detail || (parsed.error && parsed.error.message) || parsed.message || raw;
        } catch {
          detail = raw;
        }
      }
    } catch {
      /* network read failed — detail stays empty */
    }
    const suffix = detail ? ` — ${detail}` : "";
    const error = new Error(`HTTP ${response.status}${suffix}`);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

/*
 * UXP refuses any host the manifest's network permission does not cover, and
 * the message it throws ("Permission denied to the url ... Manifest entry not
 * found") is the same whether the host is absent from the list or the whole
 * permission block is not in effect. The shipped manifest allows every host;
 * see docs/panels.md for the investigation and how to narrow it.
 */
function explainNetworkError(err) {
  const text = String(err && err.message ? err.message : err);
  if (!/Manifest entry not found|Permission denied/i.test(text)) return text;
  return (
    text +
    " — UXP blocked this host. The panel's manifest.json must allow this server " +
    "(scheme, host and port exactly, or \"domains\": \"all\"), and after any " +
    "manifest change the panel must be Unloaded and Loaded again in the UXP " +
    "Developer Tool — Reload does not re-read it. If the panel shows an older " +
    `build than ${BUILD} below, it is stale.`
  );
}

function status(message, isError = false) {
  const el = $("status");
  el.textContent = message;
  el.className = isError ? "err" : "muted";
}

// --- connecting -----------------------------------------------------------

function canExport() {
  return !state.identity || state.identity.scopes.includes("export");
}

async function connect() {
  await loadProfiles();
  // Who we are, and what the token may do. A server from before 2026-10 has
  // no /panel/me: that is a connection, not a failure.
  state.identity = null;
  try {
    state.identity = await api("/panel/me");
  } catch (err) {
    if (!(err && err.status === 404)) throw err;
  }
  state.connected = true;
  if (state.identity) {
    let line = `Connected as ${state.identity.name} (${state.identity.scopes.join(", ")}), FrameFound ${state.identity.server_version}.`;
    if (!canExport()) {
      line += " This token cannot write an XML bin: it has no export scope.";
    }
    status(line);
  } else {
    status("Connected.");
  }
}

async function loadProfiles() {
  const select = $("profile");
  select.innerHTML = "";
  const none = document.createElement("option");
  none.value = "";
  none.textContent = "No path profile (search only)";
  select.appendChild(none);

  // One entry per profile name. The server lists one row per library the
  // profile covers, so a workstation with W:, X: and Y: appeared three times.
  const profiles = await api("/panel/profiles");
  const prefixes = new Map();
  for (const profile of profiles) {
    if (!prefixes.has(profile.profile_name)) prefixes.set(profile.profile_name, []);
    prefixes.get(profile.profile_name).push(profile.mapped_prefix);
  }
  for (const [name, mapped] of prefixes) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = `${name} — ${mapped.join("  ")}`;
    select.appendChild(option);
  }
  select.value = state.profile;
  if (select.value !== state.profile) {
    // The saved profile was renamed or removed. Showing "no profile" while
    // still sending the old name produced paths of null for every result.
    state.profile = "";
    select.value = "";
    remember("ff.profile", "");
  }
}

// --- searching ------------------------------------------------------------

function forgetThumbnails() {
  for (const url of thumbnails.values()) {
    try {
      URL.revokeObjectURL(url);
    } catch (e) {
      /* already gone */
    }
  }
  thumbnails.clear();
}

async function search() {
  if (!state.connected) {
    status("Not connected. Open Settings below.", true);
    return;
  }
  const q = $("q").value.trim();
  status("Searching…");
  try {
    const params = new URLSearchParams({
      q,
      profile: state.profile,
      media_type: $("type").value,
      limit: String(MAX_RESULTS),
    });
    const data = await api("/panel/search?" + params.toString());
    forgetThumbnails();
    state.results = data.results;
    state.selected.clear();
    render();
    status(`${data.results.length} found. ${data.note}`);
  } catch (err) {
    status(explainNetworkError(err), true);
  }
}

function clockTime(ms) {
  const total = Math.round(ms / 1000);
  const m = Math.floor(total / 60);
  const s = String(total % 60).padStart(2, "0");
  return `${m}:${s}`;
}

function render() {
  const list = $("results");
  list.innerHTML = "";

  if (state.results.length === 0) {
    $("import").disabled = true;
    $("count").textContent = "";
    return;
  }

  // --- select-all / deselect-all bar ----------------------------------------
  // "All" means every result in the current page, not the whole catalogue.
  const allSelected = state.results.every((r) => state.selected.has(r.asset_id));
  const bar = document.createElement("div");
  bar.className = "select-bar";

  const selectAllBtn = document.createElement("button");
  selectAllBtn.textContent = allSelected ? "Deselect all" : "Select all";
  selectAllBtn.addEventListener("click", () => {
    if (allSelected) {
      state.selected.clear();
    } else {
      for (const r of state.results) state.selected.add(r.asset_id);
    }
    render();
  });
  bar.appendChild(selectAllBtn);
  list.appendChild(bar);

  // --- result rows ----------------------------------------------------------
  for (const hit of state.results) {
    const row = document.createElement("div");
    row.className = "hit";
    const isSelected = state.selected.has(hit.asset_id);
    row.dataset.selected = isSelected;

    // Checkbox — click is handled separately from the row click so the
    // stopPropagation keeps both handlers from toggling the same item twice.
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.className = "hit-cb";
    cb.checked = isSelected;
    cb.addEventListener("click", (e) => {
      e.stopPropagation();
      if (state.selected.has(hit.asset_id)) state.selected.delete(hit.asset_id);
      else state.selected.add(hit.asset_id);
      render();
    });

    const img = document.createElement("img");
    // Thumbnails ride the same bearer token, so they are fetched rather than
    // set as a src the panel cannot authenticate — once per result.
    thumbnail(hit).then((url) => {
      if (url) img.src = url;
    });

    const body = document.createElement("div");
    body.className = "grow";

    const name = document.createElement("div");
    name.className = "name";
    name.textContent = hit.filename;

    const sub = document.createElement("div");
    sub.className = "sub";
    const bits = [hit.media_type];
    if (hit.duration_s) bits.push(Math.round(hit.duration_s) + "s");
    if (hit.width && hit.height) bits.push(hit.width + "×" + hit.height);
    if (hit.captured_at) bits.push(hit.captured_at.slice(0, 10));
    sub.textContent = bits.join(" · ");
    if (hit.matched === "visual") {
      // Found by what it looks like, not by its name — and for a clip, where.
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = hit.hit_ms != null ? `looks like it @ ${clockTime(hit.hit_ms)}` : "looks like it";
      sub.appendChild(badge);
    }
    if (!hit.path) {
      const warn = document.createElement("span");
      warn.className = "offline";
      warn.textContent = " · no local path";
      sub.appendChild(warn);
    }

    body.appendChild(name);
    body.appendChild(sub);

    // Show the workstation-mapped path when available.
    if (hit.path) {
      const pathEl = document.createElement("div");
      pathEl.className = "path";
      pathEl.textContent = hit.path;
      pathEl.title = hit.path; // full path on hover when truncated
      body.appendChild(pathEl);
    }

    row.appendChild(cb);
    row.appendChild(img);
    row.appendChild(body);

    // Clicking anywhere on the row (except the checkbox itself) toggles it.
    row.addEventListener("click", () => {
      if (state.selected.has(hit.asset_id)) state.selected.delete(hit.asset_id);
      else state.selected.add(hit.asset_id);
      render();
    });

    list.appendChild(row);
  }

  $("import").disabled = state.selected.size === 0;
  $("count").textContent = state.selected.size ? state.selected.size + " selected" : "";
}

async function thumbnail(hit) {
  if (thumbnails.has(hit.asset_id)) return thumbnails.get(hit.asset_id);
  try {
    const response = await fetchWithTimeout(
      state.server + hit.thumbnail_url,
      { headers: { Authorization: "Bearer " + state.token } },
      THUMBNAIL_TIMEOUT_MS
    );
    if (!response.ok) return null;
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    thumbnails.set(hit.asset_id, url);
    return url;
  } catch (e) {
    return null;
  }
}

// --- importing ------------------------------------------------------------

/*
 * Premiere's UXP API for project manipulation is still narrower than
 * ExtendScript's and has moved between versions, so this tries the direct
 * import and falls back to writing an FCP7 XML the editor imports by hand.
 *
 * The fallback is not a consolation prize: it is the path ADR-0019 chose to
 * ship first, it works on every NLE rather than only current Premiere, and it
 * is what makes the panel useful on a version whose API does not cooperate.
 *
 * Results carry `proxy_url`, a server-streamed URL. Premiere's import and
 * proxy-attach calls take local paths, not URLs, so clips without a local
 * path cannot be imported — the panel says so instead of promising to stream.
 */
async function importSelected() {
  const chosen = state.results.filter((r) => state.selected.has(r.asset_id));

  // Only clips with a workstation-mapped local path can be imported directly.
  // The `path` field is null when no path profile covers the asset's library.
  const withPath = chosen.filter((r) => r.path);
  const withoutPath = chosen.filter((r) => !r.path);

  if (withPath.length === 0) {
    status(
      "None of those have a local path on this machine. Pick this machine's path " +
        "profile above (or add one in FrameFound under Libraries) and search again.",
      true
    );
    return;
  }

  status(`Importing ${withPath.length}…`);
  let reason = "";
  try {
    const ppro = require("premierepro");
    const project = await ppro.Project.getActiveProject();
    if (!project) {
      status("No project is open in Premiere.", true);
      return;
    }
    const root = await project.getRootItem();
    const paths = withPath.map((r) => r.path);
    // importFiles(paths, suppressWarnings, targetBin, asNumberedStills)
    const accepted = await project.importFiles(paths, true, root, false);
    if (accepted === false) {
      throw new Error("Premiere declined the import (importFiles returned false)");
    }

    let msg = `Imported ${paths.length} clip${paths.length === 1 ? "" : "s"}.`;
    if (withoutPath.length > 0) {
      msg += ` ${withoutPath.length} skipped (no local path — check the path profile).`;
    }
    status(msg);
    return;
  } catch (err) {
    // Any version whose UXP API does not cooperate lands here, and so does a
    // missing file and a panel running outside Premiere entirely. The reason
    // used to be swallowed; it is the one thing worth reading.
    reason = String(err && err.message ? err.message : err);
    console.error("FrameFound: direct import failed", err);
  }
  if (!canExport()) {
    status(
      `Direct import did not work (${reason}), and this token cannot write an XML ` +
        "bin instead: it has no export scope. Create a token with export ticked.",
      true
    );
    return;
  }
  status(`Direct import did not work (${reason}). Writing an XML to import by hand…`);
  await exportXml(chosen);
}

function exportName() {
  // Named after the search, so two exports in a row do not offer to
  // overwrite each other. Characters a Windows filename cannot hold go.
  const q = $("q").value.trim().replace(/[<>:"/\\|?*\u0000-\u001f]/g, "-");
  return (q ? `FrameFound - ${q}` : "FrameFound selection").slice(0, 100);
}

async function exportXml(chosen) {
  try {
    const data = await api("/panel/export/fcp7", {
      method: "POST",
      body: JSON.stringify({
        asset_ids: chosen.map((c) => c.asset_id),
        profile: state.profile,
        sequence_name: exportName(),
      }),
    });
    const fs = require("uxp").storage.localFileSystem;
    const file = await fs.getFileForSaving(data.filename, { types: ["xml"] });
    if (!file) {
      status("Cancelled.");
      return;
    }
    await file.write(data.xml);
    status(
      `Wrote ${data.filename} (${data.clips} clip${data.clips === 1 ? "" : "s"}). ` +
        "In Premiere: File → Import, choose it, and the clips arrive in a bin."
    );
  } catch (err) {
    status("Could not export: " + explainNetworkError(err), true);
  }
}

// --- wiring ---------------------------------------------------------------

function showSettings(show) {
  $("settings").style.display = show ? "flex" : "none";
}

$("go").addEventListener("click", search);
$("q").addEventListener("keydown", (e) => {
  if (e.key === "Enter") search();
});
$("type").addEventListener("change", () => {
  if (state.connected) search();
});
$("profile").addEventListener("change", () => {
  state.profile = $("profile").value;
  remember("ff.profile", state.profile);
  if (state.results.length) search();
});
$("import").addEventListener("click", importSelected);
$("toggle-settings").addEventListener("click", () => {
  showSettings($("settings").style.display === "none");
});
$("save").addEventListener("click", async () => {
  save();
  try {
    await connect();
    showSettings(false);
    await search();
  } catch (err) {
    state.connected = false;
    status(explainNetworkError(err), true);
  }
});

$("build").textContent = `panel build ${BUILD}`;
load();
if (state.server && state.token) {
  showSettings(false);
  connect()
    .then(() => search())
    .catch((err) => {
      state.connected = false;
      status(explainNetworkError(err), true);
    });
} else {
  showSettings(true);
}
