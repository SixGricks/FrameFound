--[[
  Library → Import FrameFound listing…

  A listing — the GELCO calendar picks, a property's gallery — into
  Lightroom to edit, in one step:

  - by reference, where the photographs already are. Nothing is copied, and
    FrameFound's originals are never moved or written by this plugin;
  - the RAW original rather than the JPEG when the camera wrote both. A
    drone's DNG keeps the sky highlights a printed page needs;
  - into a collection named after the listing, under a "FrameFound"
    collection set, so the photographs are together to edit and export.

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
local LrTasks = import "LrTasks"
local LrView = import "LrView"

local Client = require "FrameFoundClient"

local SET_NAME = "FrameFound"

local function plural(n, word)
  return tostring(n) .. " " .. word .. (n == 1 and "" or "s")
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

local function importListing(listing, preferRaw)
  local catalog = LrApplication.activeCatalog()
  local plan, unmapped, unreachable = {}, {}, {}
  for _, item in ipairs(listing.items or {}) do
    local path, isRaw = chooseFile(item, preferRaw)
    if path then
      table.insert(plan, { path = path, raw = isRaw, name = item.filename })
    elseif item.path == nil then
      -- No profile covers this photograph's library on this machine.
      table.insert(unmapped, item.filename or "?")
    else
      -- A path, but nothing there: usually a share that is not connected.
      table.insert(unreachable, item.path)
    end
  end

  if #plan == 0 then
    local why = "None of this listing's photographs can be reached from this machine."
    if #unreachable > 0 then
      why = why .. "\n\nLooked for:\n  " .. unreachable[1] .. "\n\n"
        .. unreachableAdvice(unreachable[1])
    elseif #unmapped > 0 then
      why = why .. "\n\nNo path profile on this machine covers their library. In FrameFound, "
        .. "open Libraries and add a path profile that says where this machine sees it "
        .. "(for example GELCO on Y:\\)."
    end
    LrDialogs.message("FrameFound", why, "warning")
    return
  end

  local photos, added, reused, raws, failed = {}, 0, 0, 0, {}
  catalog:withWriteAccessDo("Import FrameFound listing", function()
    for _, entry in ipairs(plan) do
      local photo = catalog:findPhotoByPath(entry.path, false)
      if photo then
        reused = reused + 1
      else
        -- One unreadable file must not abort the other thirteen.
        local ok, result = LrTasks.pcall(function()
          return catalog:addPhoto(entry.path)
        end)
        if ok and result then
          photo = result
          added = added + 1
        else
          table.insert(failed, entry.name or entry.path)
        end
      end
      if photo then
        table.insert(photos, photo)
        if entry.raw then
          raws = raws + 1
        end
      end
    end
  end, { timeout = 120 })

  local collection
  if #photos > 0 then
    catalog:withWriteAccessDo("FrameFound collection", function()
      local set = catalog:createCollectionSet(SET_NAME, nil, true)
      collection = catalog:createCollection(listing.name, set, true)
      collection:addPhotos(photos)
    end, { timeout = 30 })
    -- Show them. Not every Lightroom version allows it; that is no failure.
    LrTasks.pcall(function()
      catalog:setActiveSources({ collection })
    end)
  end

  local lines = {
    plural(#photos, "photograph") .. " in Collections › " .. SET_NAME .. " › " .. listing.name
      .. (raws > 0 and (" — " .. raws .. " as the RAW original.") or "."),
  }
  if reused > 0 then
    table.insert(lines, "Already in this catalogue, so reused: " .. reused .. ".")
  end
  if #failed > 0 then
    table.insert(lines, "Lightroom could not add: " .. table.concat(failed, ", "))
  end
  if #unreachable > 0 then
    table.insert(lines, "Not found on this machine: " .. #unreachable .. ". First: "
      .. unreachable[1] .. "\n" .. unreachableAdvice(unreachable[1]))
  end
  if #unmapped > 0 then
    table.insert(lines, "In a library with no path profile for this machine: " .. #unmapped .. ".")
  end
  table.insert(lines, "Nothing was copied or moved.")
  LrDialogs.message("FrameFound", table.concat(lines, "\n\n"), "info")
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

    local listingItems = {}
    local known = {}
    for _, listing in ipairs(listings) do
      known[listing.listing_id] = true
      table.insert(listingItems, {
        title = listing.name .. "  (" .. plural(listing.photos or 0, "photo") .. ")",
        value = listing.listing_id,
      })
    end

    local prefs = Client.prefs()
    local props = LrBinding.makePropertyTable(context)
    props.listing = known[prefs.lastListing] and prefs.lastListing or listings[1].listing_id
    props.profile = prefixes[prefs.profile] and prefs.profile or order[1]
    props.preferRaw = prefs.preferRaw ~= false

    local factory = LrView.osFactory()
    local contents = factory:column({
      spacing = factory:control_spacing(),
      bind_to_object = props,
      factory:static_text({ title = "Listing:" }),
      factory:popup_menu({ value = LrView.bind("listing"), items = listingItems }),
      factory:static_text({ title = "This machine's paths:" }),
      factory:popup_menu({ value = LrView.bind("profile"), items = profileItems }),
      factory:checkbox({
        title = "Use the RAW original (DNG, CR3, …) when the camera wrote one",
        value = LrView.bind("preferRaw"),
      }),
      factory:static_text({
        title = "Added where they are, into a collection named after the listing.\n"
          .. "Nothing is copied or moved.",
        height_in_lines = 2,
        text_color = LrColor(0.5, 0.5, 0.5),
      }),
    })

    local choice = LrDialogs.presentModalDialog({
      title = "Import FrameFound listing",
      contents = contents,
      actionVerb = "Import",
    })
    if choice ~= "ok" then
      return
    end
    prefs.lastListing = props.listing
    prefs.profile = props.profile
    prefs.preferRaw = props.preferRaw

    local fetched, listing = LrTasks.pcall(function()
      return Client.listing(props.listing, props.profile)
    end)
    if not fetched then
      LrDialogs.message("FrameFound", tostring(listing), "critical")
      return
    end
    importListing(listing, props.preferRaw)
  end)
end)
