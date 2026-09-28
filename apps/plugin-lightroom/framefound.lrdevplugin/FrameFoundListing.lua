--[[
  Library → Import FrameFound listings…

  FrameFound listings — the GELCO calendar picks, a month of property
  galleries — into Lightroom to edit, as many as are ticked, in one run:

  - by reference, where the photographs already are. Nothing is copied, and
    FrameFound's originals are never moved or written by this plugin;
  - the RAW original rather than the JPEG when the camera wrote both. A
    drone's DNG keeps the sky highlights a printed page needs;
  - each listing into a collection of its own name, under a "FrameFound"
    collection set, and every collection imported shown together — thirty
    listings of eight photographs are 240 photographs in one grid, ready to
    select all and edit.

  A photograph already in this catalogue is reused, not added twice, so
  importing a listing again after it changes just brings in what is new.

  LrHttp blocks, so everything runs inside startAsyncTask. Calling it on the
  main task freezes Lightroom with no error at all.
--]]

local LrApplication = import "LrApplication"
local LrBinding = import "LrBinding"
local LrColor = import "LrColor"
local LrDialogs = import "LrDialogs"
local LrFileUtils = import "LrFileUtils"
local LrFunctionContext = import "LrFunctionContext"
local LrPathUtils = import "LrPathUtils"
local LrProgressScope = import "LrProgressScope"
local LrTasks = import "LrTasks"
local LrView = import "LrView"

local Client = require "FrameFoundClient"

local SET_NAME = "FrameFound"
-- File names a summary lists before it says "and N more".
local MAX_NAMED = 5

local function plural(n, word)
  return tostring(n) .. " " .. word .. (n == 1 and "" or "s")
end

local function namedList(names)
  local shown = {}
  for i = 1, math.min(#names, MAX_NAMED) do
    shown[i] = names[i]
  end
  local text = table.concat(shown, ", ")
  if #names > MAX_NAMED then
    text = text .. " and " .. (#names - MAX_NAMED) .. " more"
  end
  return text
end

--- The file to import for one listing item, and whether it is the RAW: the
--  RAW when wanted and this machine can see it, else the listed photograph.
--  nil when this machine can see neither.
local function chooseFile(item, preferRaw)
  if preferRaw and item.raw_path and LrFileUtils.exists(item.raw_path) then
    return item.raw_path, true
  end
  if item.path and LrFileUtils.exists(item.path) then
    return item.path, false
  end
  return nil, false
end

--- Why a path this machine cannot open cannot be opened. A folder whose
--  name ends in a space or a dot (made on a Mac: GELCO's "LedgeRock ") is
--  on the share, but Windows cannot open it by any path — no reconnecting
--  helps, and saying "is the drive connected?" would send someone looking
--  in the wrong place.
local function unreachableAdvice(path)
  local folder = path:match("([^\\/]*[ %.])[\\/]")
  if folder then
    return "Windows cannot open a folder whose name ends in a space or a dot, and “"
      .. folder .. "” does. Rename it on the NAS — FrameFound follows the move on its "
      .. "next scan, listings and all — then import again."
  end
  return "Is that drive connected? Open it in Explorer or Finder once, then try again."
end

--- A folder name Windows and macOS can both hold: no reserved characters,
--  and no trailing space or dot — the name that made GELCO's "LedgeRock "
--  unopenable from Windows.
local function safeFolderName(name)
  local clean = tostring(name or "FrameFound"):gsub('[<>:"/\\|%?%*%c]', "-")
  clean = clean:gsub("[%s%.]+$", ""):gsub("^%s+", "")
  return clean ~= "" and clean or "FrameFound"
end

--- One listing's photographs sorted into what can be added, with what
--  cannot recorded in the run's report. With copyTo, each entry also
--  carries where its named copy goes: <copyTo>/<listing>/<copy_name>.<ext>.
local function plan(listing, preferRaw, report, copyTo, grabTo)
  local entries = {}
  local folder = copyTo and LrPathUtils.child(copyTo, safeFolderName(listing.name))
  for n, item in ipairs(listing.items or {}) do
    local path, isRaw = chooseFile(item, preferRaw)
    if item.grab_url then
      -- A frame of a video: there is no file on the NAS to add where it
      -- is, so it is always a named copy — downloaded, full size — even
      -- when copies were not asked for.
      local grabFolder = folder or LrPathUtils.child(grabTo, safeFolderName(listing.name))
      local stem = item.copy_name
      if stem == nil or stem == "" then
        stem = string.format("%02d-frame-%d", n, item.frame_ms or 0)
      end
      table.insert(entries, {
        grab = item.grab_url,
        name = item.filename,
        folder = grabFolder,
        copy = LrPathUtils.child(grabFolder, stem .. "." .. (item.grab_ext or "jpg")),
      })
    elseif path then
      local entry = { path = path, raw = isRaw, name = item.filename }
      if folder then
        local ext = string.lower(LrPathUtils.extension(path) or "jpg")
        local stem = item.copy_name
        if stem == nil or stem == "" then
          stem = string.format("%02d-", n) .. LrPathUtils.removeExtension(item.filename or "photo")
        end
        entry.folder = folder
        entry.copy = LrPathUtils.child(folder, stem .. "." .. ext)
      end
      table.insert(entries, entry)
    elseif item.path == nil then
      -- No profile covers this photograph's library on this machine.
      report.unmapped = report.unmapped + 1
    else
      -- A path, but nothing there: a share not connected, or a name
      -- Windows cannot open.
      table.insert(report.unreachable, item.path)
    end
  end
  return entries
end

--- Collection names: the listing's own, with its date added when two of
--  the chosen listings share a name ("Shoot 2" twice would merge).
local function collectionNames(chosen)
  local count = {}
  for _, listing in ipairs(chosen) do
    count[listing.name] = (count[listing.name] or 0) + 1
  end
  local names = {}
  for _, listing in ipairs(chosen) do
    local name = listing.name
    if count[name] > 1 then
      name = name .. " (" .. string.sub(listing.created_at or "", 1, 10) .. ")"
    end
    names[listing.listing_id] = name
  end
  return names
end

--- The catalogue's photo at this path, or nil. The SDK reference lists a
--  second argument, caseSensitivity, without saying what it takes; a wrong
--  guess must cost the reuse, not the import.
local function findPhoto(catalog, path)
  local ok, photo = LrTasks.pcall(function()
    return catalog:findPhotoByPath(path, false)
  end)
  if ok then
    return photo
  end
  ok, photo = LrTasks.pcall(function()
    return catalog:findPhotoByPath(path)
  end)
  return ok and photo or nil
end

--- Adds one listing's photographs and files them in its collection. The
--  catalogue is written once per listing, so the progress bar moves and a
--  cancel stops between listings rather than losing a finished one.
local function importOne(catalog, set, name, entries, report, progress, before, total)
  -- Named copies first, outside the catalogue's write lock: copying a
  -- listing off the NAS takes minutes, and Lightroom stays usable. A copy
  -- already there from an earlier run is kept — it may have been edited.
  local ready = {}
  local copying = false
  for _, entry in ipairs(entries) do
    copying = copying or entry.copy ~= nil
  end
  for n, entry in ipairs(entries) do
    if entry.copy then
      if LrFileUtils.exists(entry.copy) then
        report.kept = report.kept + 1
      else
        LrFileUtils.createAllDirectories(entry.folder)
        local ok, copied = LrTasks.pcall(function()
          if entry.grab then
            return Client.download(entry.grab, entry.copy)
          end
          return LrFileUtils.copy(entry.path, entry.copy)
        end)
        if ok and copied ~= false and LrFileUtils.exists(entry.copy) then
          report.copied = report.copied + 1
          report.copyFolder = entry.folder
          if entry.grab then
            report.grabbed = report.grabbed + 1
          end
        else
          table.insert(report.failed, entry.name or entry.path)
          entry = nil
        end
      end
      if entry then
        entry.path = entry.copy
      end
    end
    if copying then
      -- Copying and downloading are the slow part, so they are what the
      -- bar measures.
      progress:setPortionComplete(before + n, total)
    end
    if entry then
      table.insert(ready, entry)
    end
  end
  entries = ready

  local photos = {}
  catalog:withWriteAccessDo("Import from FrameFound", function()
    for n, entry in ipairs(entries) do
      local photo = findPhoto(catalog, entry.path)
      if photo then
        report.reused = report.reused + 1
      else
        -- One unreadable file must not abort the rest.
        local ok, result = LrTasks.pcall(function()
          return catalog:addPhoto(entry.path)
        end)
        if ok and result then
          photo = result
          report.added = report.added + 1
        else
          table.insert(report.failed, entry.name or entry.path)
        end
      end
      if photo then
        table.insert(photos, photo)
        if entry.raw then
          report.raws = report.raws + 1
        end
      end
      if not copying then
        progress:setPortionComplete(before + n, total)
      end
    end
  end, { timeout = 120 })
  if #photos == 0 then
    return nil
  end
  local collection
  catalog:withWriteAccessDo("FrameFound collection", function()
    collection = catalog:createCollection(name, set, true)
    collection:addPhotos(photos)
  end, { timeout = 30 })
  report.photos = report.photos + #photos
  return collection
end

local function summary(report, collections, chosen, stopped)
  local lines = {}
  if #collections == 1 then
    table.insert(lines, plural(report.photos, "photograph") .. " in Collections › "
      .. SET_NAME .. " › " .. collections[1].name .. ".")
  else
    table.insert(lines, plural(report.photos, "photograph") .. " in "
      .. plural(#collections, "collection") .. " under Collections › " .. SET_NAME
      .. ", all showing now.")
  end
  if report.raws > 0 then
    table.insert(lines, report.raws .. " are the RAW original.")
  end
  if report.copied > 0 or report.kept > 0 then
    local copyLine = "Named copies"
    if report.copyFolder then
      copyLine = copyLine .. " in " .. report.copyFolder
    end
    copyLine = copyLine .. ": " .. report.copied .. " copied"
    if report.grabbed > 0 then
      copyLine = copyLine .. " (" .. report.grabbed .. " of them full-size frames from video)"
    end
    if report.kept > 0 then
      copyLine = copyLine .. ", " .. report.kept .. " already there and kept as they were"
    end
    table.insert(lines, copyLine .. ". The originals on the NAS are untouched.")
  end
  if #collections > 1 then
    table.insert(lines, "To edit them together: Ctrl+A (⌘A on a Mac) in the grid selects "
      .. "every one. In Develop, Sync Settings copies one photograph's edits to the rest.")
  end
  if stopped then
    table.insert(lines, "Stopped after " .. #collections .. " of " .. #chosen
      .. " listings — import again to finish; what is done is kept.")
  end
  if report.reused > 0 then
    table.insert(lines, "Already in this catalogue, so reused: " .. report.reused .. ".")
  end
  if #report.failed > 0 then
    table.insert(lines, "Lightroom could not add: " .. namedList(report.failed))
  end
  if #report.unreachable > 0 then
    table.insert(lines, "Not found on this machine: " .. #report.unreachable .. ". First: "
      .. report.unreachable[1] .. "\n" .. unreachableAdvice(report.unreachable[1]))
  end
  if report.unmapped > 0 then
    table.insert(lines, "In a library with no path profile for this machine: "
      .. report.unmapped .. ".")
  end
  if #report.unreadable > 0 then
    table.insert(lines, "Could not read from FrameFound: " .. namedList(report.unreadable))
  end
  if report.copied == 0 and report.kept == 0 then
    table.insert(lines, "Nothing was copied or moved.")
  end
  return table.concat(lines, "\n\n")
end

LrTasks.startAsyncTask(function()
  LrFunctionContext.callWithContext("FrameFoundListing", function(context)
    if not Client.configured() then
      LrDialogs.message(
        "FrameFound",
        "Open File → Plug-in Manager, select FrameFound, and enter your server "
          .. "address and panel token first.",
        "info"
      )
      return
    end

    -- LrTasks.pcall throughout: plain pcall is a C-call boundary and Lua 5.1
    -- cannot yield across one, so it turns a legal LrHttp call into
    -- "Yielding is not allowed within a C or metamethod call".
    local ok, listings = LrTasks.pcall(Client.listings)
    if not ok then
      LrDialogs.message("FrameFound", tostring(listings), "critical")
      return
    end
    if #listings == 0 then
      LrDialogs.message("FrameFound", "FrameFound has no listings with photographs yet.", "info")
      return
    end

    -- One entry per profile name: a workstation's profile covers several
    -- libraries (W:, X:, Y:), and the server lists one row for each.
    local profileItems, prefixes, order = {}, {}, {}
    local okProfiles, profiles = LrTasks.pcall(Client.profiles)
    if okProfiles then
      for _, profile in ipairs(profiles) do
        local name = profile.profile_name
        if prefixes[name] == nil then
          prefixes[name] = {}
          table.insert(order, name)
        end
        table.insert(prefixes[name], profile.mapped_prefix or "?")
      end
    end
    for _, name in ipairs(order) do
      table.insert(profileItems, {
        title = name .. " — " .. table.concat(prefixes[name], "  "),
        value = name,
      })
    end
    if #profileItems == 0 then
      LrDialogs.message(
        "FrameFound",
        "FrameFound has no path profiles, so it cannot say where this machine sees "
          .. "the photographs. In FrameFound, open Libraries and add one for each "
          .. "share (for example GELCO on Y:\\).",
        "warning"
      )
      return
    end

    local prefs = Client.prefs()
    local props = LrBinding.makePropertyTable(context)
    props.profile = prefixes[prefs.profile] and prefs.profile or order[1]
    props.preferRaw = prefs.preferRaw ~= false
    -- Named copies: Lightroom cannot rename a file, and FrameFound never
    -- renames an original, so a copy is how the files in Lightroom get the
    -- listing's names ("ledgerock-03-fall-drone.dng").
    props.copy = prefs.copy ~= false
    props.copyFolder = prefs.copyFolder
      or LrPathUtils.child(LrPathUtils.getStandardFilePath("pictures"), SET_NAME)

    -- Last run's ticks come back, so a batch can be topped up; with none,
    -- the newest listing is ticked.
    local remembered = {}
    for id in string.gmatch(prefs.lastListings or prefs.lastListing or "", "[^,]+") do
      remembered[id] = true
    end
    local anyTicked = false
    for i, listing in ipairs(listings) do
      props["sel_" .. i] = remembered[listing.listing_id] == true
      anyTicked = anyTicked or props["sel_" .. i]
    end
    if not anyTicked then
      props.sel_1 = true
    end
    -- SDK binding note: every property is set before the dialog is built,
    -- because LrView.bind looks them up at layout time.
    props.selectAll = false
    props:addObserver("selectAll", function(tbl, _key, value)
      for i = 1, #listings do
        tbl["sel_" .. i] = value
      end
    end)

    local factory = LrView.osFactory()
    local rows = { spacing = 2 }
    for i, listing in ipairs(listings) do
      table.insert(rows, factory:checkbox({
        title = listing.name .. "   ·   " .. plural(listing.photos or 0, "photo")
          .. "   ·   " .. string.sub(listing.created_at or "", 1, 10),
        value = LrView.bind("sel_" .. i),
      }))
    end

    local contents = factory:column({
      spacing = factory:control_spacing(),
      bind_to_object = props,
      factory:static_text({ title = "Listings to import, newest first:" }),
      factory:checkbox({ title = "Select all", value = LrView.bind("selectAll") }),
      factory:separator({ fill_horizontal = 1 }),
      factory:scrolled_view({
        width = 560,
        height = math.min(#listings * 24 + 12, 340),
        factory:column(rows),
      }),
      factory:separator({ fill_horizontal = 1 }),
      factory:static_text({ title = "This machine's paths:" }),
      factory:popup_menu({ value = LrView.bind("profile"), items = profileItems }),
      factory:checkbox({
        title = "Use the RAW original (DNG, CR3, …) when the camera wrote one",
        value = LrView.bind("preferRaw"),
      }),
      factory:checkbox({
        title = "Copy them into a folder, named as FrameFound names them "
          .. "(ledgerock-03-fall-drone.dng)",
        value = LrView.bind("copy"),
      }),
      factory:row({
        factory:static_text({
          title = LrView.bind("copyFolder"),
          width_in_chars = 50,
          enabled = LrView.bind("copy"),
        }),
        factory:push_button({
          title = "Choose…",
          enabled = LrView.bind("copy"),
          action = function()
            local chosen = LrDialogs.runOpenPanel({
              title = "Where should the named copies go?",
              canChooseFiles = false,
              canChooseDirectories = true,
              canCreateDirectories = true,
              allowsMultipleSelection = false,
              initialDirectory = props.copyFolder,
            })
            if chosen and chosen[1] then
              props.copyFolder = chosen[1]
            end
          end,
        }),
      }),
      factory:static_text({
        title = "Each listing becomes a collection under Collections › " .. SET_NAME
          .. ", and all of them\nare shown together to edit. Copies go in a folder per "
          .. "listing; unticked,\nthe originals are added where they are. The NAS is "
          .. "never written to.",
        height_in_lines = 3,
        text_color = LrColor(0.5, 0.5, 0.5),
      }),
    })

    local choice = LrDialogs.presentModalDialog({
      title = "Import FrameFound listings",
      contents = contents,
      actionVerb = "Import",
    })
    if choice ~= "ok" then
      return
    end

    local chosen, ids = {}, {}
    for i, listing in ipairs(listings) do
      if props["sel_" .. i] then
        table.insert(chosen, listing)
        table.insert(ids, listing.listing_id)
      end
    end
    if #chosen == 0 then
      LrDialogs.message("FrameFound", "No listings were ticked.", "info")
      return
    end
    prefs.lastListings = table.concat(ids, ",")
    prefs.profile = props.profile
    prefs.preferRaw = props.preferRaw
    prefs.copy = props.copy
    prefs.copyFolder = props.copyFolder
    local copyTo = props.copy and props.copyFolder or nil

    local progress = LrProgressScope({
      title = "Importing from FrameFound",
      functionContext = context,
    })
    local report = {
      added = 0, reused = 0, raws = 0, photos = 0, unmapped = 0, copied = 0, kept = 0, grabbed = 0,
      failed = {}, unreachable = {}, unreadable = {},
    }

    -- Every listing is read before anything is added: the photograph count
    -- is the progress bar's length, and a share that is not connected shows
    -- up before the catalogue is touched.
    local plans, total = {}, 0
    for n, listing in ipairs(chosen) do
      if progress:isCanceled() then
        return
      end
      progress:setCaption("Reading listing " .. n .. " of " .. #chosen .. ": " .. listing.name)
      local fetched, detail = LrTasks.pcall(function()
        return Client.listing(listing.listing_id, props.profile)
      end)
      if fetched then
        local entries = plan(detail, props.preferRaw, report, copyTo, props.copyFolder)
        table.insert(plans, { listing = listing, entries = entries })
        total = total + #entries
      else
        table.insert(report.unreadable, listing.name)
      end
    end

    if total == 0 then
      progress:done()
      local why = "None of the photographs in "
        .. (#chosen == 1 and "this listing" or ("these " .. #chosen .. " listings"))
        .. " can be reached from this machine."
      if #report.unreachable > 0 then
        why = why .. "\n\nLooked for:\n  " .. report.unreachable[1] .. "\n\n"
          .. unreachableAdvice(report.unreachable[1])
      elseif report.unmapped > 0 then
        why = why .. "\n\nNo path profile on this machine covers their library. In "
          .. "FrameFound, open Libraries and add a path profile that says where this "
          .. "machine sees it (for example GELCO on Y:\\)."
      elseif #report.unreadable > 0 then
        why = why .. "\n\nCould not read from FrameFound: " .. namedList(report.unreadable)
      end
      LrDialogs.message("FrameFound", why, "warning")
      return
    end

    local catalog = LrApplication.activeCatalog()
    local set
    catalog:withWriteAccessDo("FrameFound collections", function()
      set = catalog:createCollectionSet(SET_NAME, nil, true)
    end, { timeout = 30 })

    local names = collectionNames(chosen)
    local collections, done, stopped = {}, 0, false
    for _, entry in ipairs(plans) do
      if progress:isCanceled() then
        stopped = true
        break
      end
      if #entry.entries > 0 then
        progress:setCaption(entry.listing.name)
        local name = names[entry.listing.listing_id]
        local collection = importOne(
          catalog, set, name, entry.entries, report, progress, done, total
        )
        done = done + #entry.entries
        if collection then
          table.insert(collections, { collection = collection, name = name })
        end
      end
    end
    progress:done()

    if #collections > 0 then
      -- Every imported collection at once: the whole batch in one grid.
      -- Not every Lightroom version allows it; that is no failure.
      local sources = {}
      for i, c in ipairs(collections) do
        sources[i] = c.collection
      end
      LrTasks.pcall(function()
        catalog:setActiveSources(sources)
      end)
    end

    LrDialogs.message("FrameFound", summary(report, collections, chosen, stopped), "info")
  end)
end)
