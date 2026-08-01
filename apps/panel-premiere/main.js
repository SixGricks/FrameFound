/*
 * FrameFound panel for Premiere Pro (UXP). panel build 0.3.0
 *
 * The panel's whole job: search the catalogue, and put the clips the editor
 * picked into the open project at paths their machine can actually open. That
 * last part is the reason the panel exists — the catalogue stores
 * `/media/gelco/...` and a Windows edit bay sees `Z:\`, so an import that does
 * not translate produces a project full of offline media.
 *
 * Written against UXP's restricted capability set on purpose (ADR-0019). No
 * Node, no filesystem beyond what the manifest asks for, and one declared
 * network host. Nothing here can reach the operator's disk on its own.
 *
 * Two API surfaces are used and they are handled differently:
 *   - `premierepro`  — the UXP DOM API. Availability varies by version, so
 *                      every call is guarded and falls back to an export.
 *   - FrameFound     — a stable contract under /api/v1/panel, versioned with
 *                      the rest of the API.
 */

const state = {
  server: "",
  token: "",
  profile: "",
  results: [],
  selected: new Set(),
};

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

function save() {
  state.server = $("server").value.trim().replace(/\/$/, "");
  state.token = $("token").value.trim();
  localStorage.setItem("ff.server", state.server);
  localStorage.setItem("ff.token", state.token);
}

// --- api ------------------------------------------------------------------

async function api(path, options = {}) {
  if (!state.server) throw new Error("No server address set");
  if (!state.token) throw new Error("No panel token set");
  const response = await fetch(state.server + "/api/v1" + path, {
    ...options,
    headers: {
      Authorization: "Bearer " + state.token,
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
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
          detail = parsed.detail || parsed.message || raw;
        } catch {
          detail = raw;
        }
      }
    } catch {
      /* network read failed — detail stays empty */
    }
    const suffix = detail ? ` — ${detail}` : "";
    if (response.status === 401) {
      throw new Error(`401 Unauthorized${suffix}`);
    }
    if (response.status === 403) {
      throw new Error(`403 Forbidden${suffix}`);
    }
    throw new Error(`HTTP ${response.status}${suffix}`);
  }
  return response.json();
}

/*
 * UXP refuses any host not declared in the manifest, and the message it throws
 * ("Permission denied to the url ... Manifest entry not found") is the same
 * whether the host is genuinely absent or was declared and then discarded.
 *
 * Premiere 26.3.0 rejected `http://192.168.1.193:8080` while that exact string
 * was in the manifest, which leaves two candidates: UXP matches origins
 * without the port, or it drops plain-HTTP entries at parse time because it
 * requires TLS. The manifest now declares both port and port-less forms of
 * each, so if this still fails the answer is the second one — and switching
 * the server field to https:// will change the error to a TLS complaint
 * rather than a permissions one. That difference is the diagnosis.
 */
function explainNetworkError(err, url) {
  const text = String(err && err.message ? err.message : err);
  if (!/Manifest entry not found|Permission denied/i.test(text)) return text;
  return (
    text +
    " — UXP blocked this host. Try the same address over https:// : if the " +
    "error changes to a certificate or TLS failure, UXP is refusing plain " +
    "HTTP and the server needs a certificate this machine trusts. If it stays " +
    "a permissions error, the host is not matching a manifest entry."
  );
}

function status(message, isError = false) {
  const el = $("status");
  el.textContent = message;
  el.className = isError ? "err" : "muted";
}

// --- searching ------------------------------------------------------------

async function loadProfiles() {
  const select = $("profile");
  select.innerHTML = "";
  const none = document.createElement("option");
  none.value = "";
  none.textContent = "No path profile (stream from server)";
  select.appendChild(none);

  const profiles = await api("/panel/profiles");
  for (const profile of profiles) {
    const option = document.createElement("option");
    option.value = profile.profile_name;
    option.textContent = `${profile.profile_name} — ${profile.mapped_prefix}`;
    select.appendChild(option);
  }
  select.value = state.profile;
}

async function search() {
  const q = $("q").value.trim();
  status("Searching…");
  try {
    const params = new URLSearchParams({
      q,
      profile: state.profile,
      media_type: $("type").value,
    });
    const data = await api("/panel/search?" + params.toString());
    state.results = data.results;
    state.selected.clear();
    render();
    status(`${data.results.length} found. ${data.note}`);
  } catch (err) {
    status(err.message, true);
  }
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
    // set as a src the panel cannot authenticate.
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
    sub.textContent = bits.join(" · ");
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
  try {
    const response = await fetch(state.server + hit.thumbnail_url, {
      headers: { Authorization: "Bearer " + state.token },
    });
    if (!response.ok) return null;
    const blob = await response.blob();
    return URL.createObjectURL(blob);
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
 */
async function importSelected() {
  const chosen = state.results.filter((r) => state.selected.has(r.asset_id));

  // Only clips with a workstation-mapped local path can be imported directly.
  // The `path` field is null when no path profile covers the asset's library.
  const withPath = chosen.filter((r) => r.path);
  const withoutPath = chosen.filter((r) => !r.path);

  if (withPath.length === 0) {
    status("None of those have a local path. Pick a path profile first.", true);
    return;
  }

  status(`Importing ${withPath.length}…`);
  try {
    const ppro = require("premierepro");
    const project = await ppro.Project.getActiveProject();
    if (!project) {
      status("No project is open in Premiere.", true);
      return;
    }
    const root = await project.getRootItem();

    // --- Proxy-first import strategy ----------------------------------------
    //
    // FrameFound's search results carry `proxy_url`, which is a *server-streamed*
    // URL (e.g. /api/v1/media/{id}/proxy). Premiere's attachProxy() and
    // setProxyEnabled() require a local filesystem path, not a URL, so the
    // server-side proxy cannot be attached this way.
    //
    // The current schema has no `proxy_path` field (a workstation-mapped path
    // to a locally accessible proxy file). Until the server exposes one —
    // either by mounting the data volume on the edit bay and adding the field
    // to PanelAsset, or by allowing the panel to download the proxy to a temp
    // location — we import the originals directly.
    //
    // When `proxy_path` becomes available, the proxy-first flow would be:
    //
    //   const proxyPaths = withPath.filter(r => r.proxy_path).map(r => r.proxy_path);
    //   const directPaths = withPath.filter(r => !r.proxy_path).map(r => r.path);
    //
    //   if (proxyPaths.length > 0) {
    //     await project.importFiles(proxyPaths, true, root, false);
    //     // Walk the project tree and attach originals as offline sources
    //     const items = await root.getItems();
    //     for (const hit of withPath.filter(r => r.proxy_path)) {
    //       const item = items.find(i => {
    //         try { return i.getMediaPath() === hit.proxy_path; } catch { return false; }
    //       });
    //       if (item) {
    //         // 1 = OverrideMediaType.Video; use 2 for audio-only clips
    //         await item.attachProxy(hit.proxy_path, 1);
    //         await item.setProxyEnabled(true);
    //       }
    //     }
    //   }
    //   if (directPaths.length > 0) {
    //     await project.importFiles(directPaths, true, root, false);
    //   }

    // For now: import originals directly for all clips that have a local path.
    const paths = withPath.map((r) => r.path);
    // importFiles(paths, suppressWarnings, targetBin, asNumberedStills)
    await project.importFiles(paths, true, root, false);

    let msg = `Imported ${paths.length} clip${paths.length === 1 ? "" : "s"}.`;
    if (withoutPath.length > 0) {
      msg += ` ${withoutPath.length} skipped (no local path — check path profile).`;
    }
    status(msg);
  } catch (err) {
    // Any version whose UXP API does not cooperate lands here, and so does a
    // panel running outside Premiere entirely.
    status("Direct import unavailable — writing an XML instead…");
    await exportXml(chosen);
  }
}

async function exportXml(chosen) {
  try {
    const data = await api("/panel/export/fcp7", {
      method: "POST",
      body: JSON.stringify({
        asset_ids: chosen.map((c) => c.asset_id),
        profile: state.profile,
        sequence_name: "FrameFound selection",
      }),
    });
    const fs = require("uxp").storage.localFileSystem;
    const file = await fs.getFileForSaving(data.filename, { types: ["xml"] });
    if (!file) {
      status("Cancelled.");
      return;
    }
    await file.write(data.xml);
    status(`Wrote ${data.filename}. Import it with File → Import.`);
  } catch (err) {
    status("Could not export: " + err.message, true);
  }
}

// --- wiring ---------------------------------------------------------------

$("go").addEventListener("click", search);
$("q").addEventListener("keydown", (e) => {
  if (e.key === "Enter") search();
});
$("type").addEventListener("change", search);
$("profile").addEventListener("change", () => {
  state.profile = $("profile").value;
  localStorage.setItem("ff.profile", state.profile);
  if (state.results.length) search();
});
$("import").addEventListener("click", importSelected);
$("save").addEventListener("click", async () => {
  save();
  try {
    await loadProfiles();
    $("settings").open = false;
    status("Connected.");
    await search();
  } catch (err) {
    status(explainNetworkError(err, state.server), true);
  }
});

load();
if (state.server && state.token) {
  loadProfiles()
    .then(() => {
      status("Connected.");
      return search();
    })
    .catch((err) => status(err.message, true));
} else {
  $("settings").open = true;
}
