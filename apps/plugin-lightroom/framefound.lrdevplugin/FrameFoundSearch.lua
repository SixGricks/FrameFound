--[[
  Library → Search FrameFound…

  Searches the catalogue and offers to add what was found to this Lightroom
  catalogue, at paths this machine can open.

  Why adding rather than copying: Lightroom references photographs where they
  live. FrameFound's promise is that originals are never moved or written to,
  and importing by reference is the only behaviour consistent with that. The
  files stay exactly where they are; Lightroom learns about them.

  LrHttp blocks, so everything runs inside startAsyncTask. Calling it on the
  main task freezes Lightroom with no error at all.
--]]

local LrApplication = import "LrApplication"
local LrBinding     = import "LrBinding"
local LrColor       = import "LrColor"
local LrDialogs     = import "LrDialogs"
local LrFileUtils   = import "LrFileUtils"
local LrFunctionContext = import "LrFunctionContext"
local LrPathUtils   = import "LrPathUtils"
local LrTasks       = import "LrTasks"
local LrView        = import "LrView"

local Client = require "FrameFoundClient"

local function addToCatalogue(results)
  local catalog = LrApplication.activeCatalog()
  local added, missing, skipped = 0, 0, 0

  catalog:withWriteAccessDo("Add from FrameFound", function()
    for _, hit in ipairs(results) do
      local path = hit.path
      if path == nil or path == "" then
        -- No workstation profile matched, so there is no local file to point
        -- at. Counted and reported rather than guessed at.
        skipped = skipped + 1
      elseif not LrFileUtils.exists(path) then
        -- The profile produced a path this machine cannot see. Usually a drive
        -- that is not mounted, or a profile pointing at the wrong letter.
        missing = missing + 1
      else
        local photo = catalog:addPhoto(path)
        if photo then
          added = added + 1
        end
      end
    end
  end, { timeout = 30 })

  local message = added .. " added to the catalogue."
  if missing > 0 then
    message = message
      .. "\n\n"
      .. missing
      .. " could not be found on this machine. Check that the drive is mounted "
      .. "and that the path profile points at the right place."
  end
  if skipped > 0 then
    message = message
      .. "\n\n"
      .. skipped
      .. " had no local path — choose a path profile and search again."
  end
  LrDialogs.message("FrameFound", message, "info")
end

LrTasks.startAsyncTask(function()
  LrFunctionContext.callWithContext("FrameFoundSearch", function(context)
    if not Client.configured() then
      LrDialogs.message(
        "FrameFound",
        "Open File → Plug-in Manager, select FrameFound, and enter your server "
          .. "address and panel token first.",
        "info"
      )
      return
    end

    local prefs   = Client.prefs()
    local factory = LrView.osFactory()
    local props   = LrBinding.makePropertyTable(context)
    props.query   = ""
    props.profile = prefs.profile or ""

    -- Profiles are fetched up front: an editor picking their workstation from
    -- a list cannot mistype it, and a mistyped profile silently yields paths
    -- that do not resolve.
    local profileItems = { { title = "No path profile", value = "" } }
    -- LrTasks.pcall throughout: plain pcall is a C-call boundary and Lua 5.1
    -- cannot yield across one, so it turns a legal LrHttp call into
    -- "Yielding is not allowed within a C or metamethod call".
    local ok, profiles = LrTasks.pcall(function()
      return Client.profiles()
    end)
    if ok then
      for _, profile in ipairs(profiles) do
        table.insert(profileItems, {
          title = profile.profile_name .. " — " .. (profile.mapped_prefix or ""),
          value = profile.profile_name,
        })
      end
    end

    local contents = factory:column({
      spacing = factory:control_spacing(),
      bind_to_object = props,
      factory:static_text({ title = "What are you looking for?" }),
      factory:edit_field({ value = LrView.bind("query"), width_in_chars = 40 }),
      factory:static_text({ title = "This machine's paths:" }),
      factory:popup_menu({ value = LrView.bind("profile"), items = profileItems }),
      factory:static_text({
        title      = "Photographs are added where they already are. Nothing is copied or moved.",
        text_color = LrColor(0.5, 0.5, 0.5),
      }),
    })

    local choice = LrDialogs.presentModalDialog({
      title      = "Search FrameFound",
      contents   = contents,
      actionVerb = "Search",
    })
    if choice ~= "ok" or props.query == "" then
      return
    end

    prefs.profile = props.profile

    local searched, results = LrTasks.pcall(function()
      return Client.search(props.query, props.profile, "image", 60)
    end)
    if not searched then
      -- The error carries the LrErrors user message; surface it plainly.
      LrDialogs.message("FrameFound", tostring(results), "critical")
      return
    end

    if #results == 0 then
      LrDialogs.message("FrameFound", "Nothing matched that search.", "info")
      return
    end

    -- ── Preview dialog with per-row selection ────────────────────────────────
    --
    -- Each result gets its own boolean property ("sel_1", "sel_2", …) bound to
    -- a checkbox. All default to true (everything selected). A "Select all"
    -- master checkbox at the top toggles every row at once via an observer.
    --
    -- SDK binding note: property names must exist on the propertyTable *before*
    -- the dialog is presented — LrView.bind() looks them up at layout time.
    -- Dynamic keys like "sel_" .. i are fine as long as they are pre-populated.
    -- The observer on "selectAll" cascades its new value to every "sel_N" key.
    --
    -- Colours on the path labels (unchanged from the plain-text version):
    --   dim grey  → file exists and is reachable
    --   red       → path resolved but file is absent (drive not mounted, etc.)
    --   amber     → no path at all (profile did not match this entry)

    local numResults = #results

    -- Initialise selection state before building the view.
    for i = 1, numResults do
      props["sel_" .. i] = true
    end
    props.selectAll = true

    -- Master toggle: cascade to every individual row.
    props:addObserver("selectAll", function(tbl, _key, val)
      for i = 1, numResults do
        tbl["sel_" .. i] = val
      end
    end)

    -- Build one row per result.
    local previewRows = {
      fill_horizontal = 1,
      spacing         = 2,
    }

    for i, hit in ipairs(results) do
      local path = hit.path or ""
      local leaf, pathLabel, pathColor

      if path ~= "" then
        leaf      = LrPathUtils.leafName(path)
        pathLabel = path
        if LrFileUtils.exists(path) then
          -- Reachable — dim path so the filename stands out.
          pathColor = LrColor(0.35, 0.35, 0.35)
        else
          -- Profile resolved a path but the file is not there.
          pathColor = LrColor(0.70, 0.18, 0.18)
          leaf      = leaf .. "  ·  not found on this machine"
        end
      else
        -- No profile match — name the file anyway so the user knows what
        -- FrameFound found, even though it cannot be imported right now.
        leaf      = hit.original_filename or hit.filename or "(unknown filename)"
        pathLabel = "No local path — choose a path profile and search again."
        pathColor = LrColor(0.55, 0.38, 0.00)
      end

      table.insert(previewRows, factory:row({
        fill_horizontal = 1,
        margin_bottom   = 6,
        -- Checkbox bound to this row's individual property.
        -- Title is a thin space so the widget has non-zero width with no label
        -- text of its own; the color-coded static_texts carry the visible info.
        factory:checkbox({
          title = " ",
          value = LrView.bind("sel_" .. i),
        }),
        factory:column({
          fill_horizontal = 1,
          factory:static_text({
            title           = tostring(i) .. ".  " .. leaf,
            fill_horizontal = 1,
          }),
          factory:static_text({
            title          = "      " .. pathLabel,
            text_color     = pathColor,
            width_in_chars = 54,
          }),
        }),
      }))
    end

    -- Cap the scroll area at ~360 px; each row is roughly 50 px with margin.
    local scrollHeight = math.min(numResults * 50 + 12, 360)

    local previewContents = factory:column({
      spacing        = factory:control_spacing(),
      bind_to_object = props,   -- all LrView.bind() calls inside inherit this
      factory:static_text({
        title = numResults
          .. " photograph"
          .. (numResults == 1 and "" or "s")
          .. " found — check the ones you want to add:",
      }),
      -- Master toggle sits above the separator so it is always visible even
      -- when the list is long enough to scroll.
      factory:row({
        factory:checkbox({
          title = "Select all",
          value = LrView.bind("selectAll"),
        }),
      }),
      factory:separator({ fill_horizontal = 1 }),
      factory:scroll_view({
        width  = 600,
        height = scrollHeight,
        factory:column(previewRows),
      }),
      factory:separator({ fill_horizontal = 1 }),
      factory:static_text({
        title      = "Photographs are added by reference — nothing is copied or moved.",
        text_color = LrColor(0.50, 0.50, 0.50),
      }),
    })

    local preview = LrDialogs.presentModalDialog({
      title      = "FrameFound — Review results",
      contents   = previewContents,
      actionVerb = "Add them",
      cancelVerb = "Cancel",
    })

    if preview == "ok" then
      -- Collect only the rows whose checkbox was left ticked.
      local selected = {}
      for i, hit in ipairs(results) do
        if props["sel_" .. i] then
          table.insert(selected, hit)
        end
      end

      if #selected == 0 then
        LrDialogs.message("FrameFound", "Nothing selected.", "info")
      else
        addToCatalogue(selected)
      end
    end
  end)
end)
