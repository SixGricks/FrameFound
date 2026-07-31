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
local LrBinding = import "LrBinding"
local LrDialogs = import "LrDialogs"
local LrFileUtils = import "LrFileUtils"
local LrFunctionContext = import "LrFunctionContext"
local LrTasks = import "LrTasks"
local LrView = import "LrView"

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

    local prefs = Client.prefs()
    local factory = LrView.osFactory()
    local props = LrBinding.makePropertyTable(context)
    props.query = ""
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
        title = "Photographs are added where they already are. Nothing is copied or moved.",
        text_color = import("LrColor")(0.5, 0.5, 0.5),
      }),
    })

    local choice = LrDialogs.presentModalDialog({
      title = "Search FrameFound",
      contents = contents,
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

    local confirm = LrDialogs.confirm(
      "FrameFound found " .. #results .. " photograph" .. (#results == 1 and "" or "s"),
      "Add them to this Lightroom catalogue? They stay where they are — Lightroom "
        .. "will reference them in place.",
      "Add them",
      "Cancel"
    )
    if confirm == "ok" then
      addToCatalogue(results)
    end
  end)
end)
