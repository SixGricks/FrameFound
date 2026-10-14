--[[
  Library → Show FrameFound paths for selected photo.

  The diagnostic. When a path profile is wrong, every other symptom is
  confusing — photographs that "cannot be found" on a drive that is plainly
  mounted. Seeing all four mappings side by side is how somebody notices the
  Windows profile points at the wrong drive letter.

  Matches on filename, which is what Lightroom has to hand. Ambiguous when two
  cards produced the same DSC_0001.NEF, and the dialog says so rather than
  pretending the first hit is the answer.
--]]

local LrApplication = import "LrApplication"
local LrDialogs = import "LrDialogs"
local LrPathUtils = import "LrPathUtils"
local LrTasks = import "LrTasks"

local Client = require "FrameFoundClient"

LrTasks.startAsyncTask(function()
  local catalog = LrApplication.activeCatalog()
  local photo = catalog:getTargetPhoto()

  if photo == nil then
    LrDialogs.message("FrameFound", "Select a photograph first.", "info")
    return
  end

  local localPath = photo:getRawMetadata("path")
  local filename = LrPathUtils.leafName(localPath or "")

  -- LrTasks.pcall: plain pcall is a C-call boundary, and Lua 5.1 cannot yield
  -- across one, which is what turns a legal LrHttp call into "Yielding is not
  -- allowed within a C or metamethod call".
  -- Any kind of file: a selected video has paths too.
  local ok, results = LrTasks.pcall(function()
    return Client.search(filename, "", "", 60)
  end)
  if not ok then
    LrDialogs.message("FrameFound", tostring(results), "critical")
    return
  end

  -- Only an entry with this very name. A search also returns what *looks*
  -- like the query — on a catalogue with embeddings, the nearest frames to
  -- the string "DJI_0323.JPG" — and taking the first result showed a
  -- different photograph's paths under this one's name.
  local matches = {}
  for _, hit in ipairs(results) do
    if hit.filename and string.lower(hit.filename) == string.lower(filename) then
      table.insert(matches, hit)
    end
  end

  if #matches == 0 then
    LrDialogs.message(
      "FrameFound",
      filename .. " is not in the FrameFound catalogue, or has not been scanned yet."
        .. (#results > 0 and ("\n\n(" .. #results .. " similar-looking results were found, none with this name.)") or ""),
      "info"
    )
    return
  end

  local lines = { "Lightroom sees this photograph at:", "  " .. (localPath or "(unknown)"), "" }

  if #matches > 1 then
    table.insert(
      lines,
      #matches .. " catalogue entries share that filename — showing the first."
    )
    table.insert(lines, "")
  end

  local hit = matches[1]
  local ok2, detail = LrTasks.pcall(function()
    return Client.getJson("/panel/assets/" .. hit.asset_id .. "/paths")
  end)
  if ok2 then
    table.insert(lines, "FrameFound stores it at:")
    table.insert(lines, "  " .. (detail.server_path or "(unknown)"))
    table.insert(lines, "")
    table.insert(lines, "Workstation profiles:")
    for _, entry in ipairs(detail.paths or {}) do
      table.insert(
        lines,
        "  " .. (entry.profile_name or "?") .. " (" .. (entry.platform or "?") .. "): "
          .. (entry.path or "?")
      )
    end
  end

  LrDialogs.message("FrameFound paths", table.concat(lines, "\n"), "info")
end)
