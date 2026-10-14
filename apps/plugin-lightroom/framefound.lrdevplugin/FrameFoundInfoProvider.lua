--[[
  Plug-in Manager settings: server address and panel token.

  The token is entered once here and stored in Lightroom's plugin preferences.
  It is deliberately a panel token rather than the operator's password: it is
  scoped to reading, it is listed on FrameFound's Security page with when it
  was last used, and it can be revoked from there if this machine is lost.
--]]

local LrDialogs = import "LrDialogs"
local LrHttp = import "LrHttp"
local LrTasks = import "LrTasks"
local LrView = import "LrView"

local Client = require "FrameFoundClient"

local provider = {}

function provider.sectionsForTopOfDialog(factory, _properties)
  local prefs = Client.prefs()

  return {
    {
      title = "FrameFound connection",

      factory:row({
        factory:static_text({ title = "Server address:", width = 110 }),
        factory:edit_field({
          value = LrView.bind({ key = "server", object = prefs }),
          width_in_chars = 34,
          placeholder_string = "http://framefound.local:8080",
        }),
      }),

      factory:row({
        factory:static_text({ title = "Panel token:", width = 110 }),
        factory:password_field({
          value = LrView.bind({ key = "token", object = prefs }),
          width_in_chars = 34,
        }),
      }),

      factory:row({
        factory:static_text({ title = "", width = 110 }),
        factory:static_text({
          title = "Create one in FrameFound under Security → Panel tokens.\n"
            .. "It is shown once, and can be revoked there at any time.",
          height_in_lines = 2,
        }),
      }),

      factory:row({
        factory:static_text({ title = "", width = 110 }),
        factory:push_button({
          title = "Test connection",
          action = function()
            -- LrHttp blocks; running it on the dialog's task would freeze
            -- Lightroom rather than report a failure.
            LrTasks.startAsyncTask(function()
              -- LrTasks.pcall, not pcall. Lightroom runs Lua 5.1, which cannot
              -- yield across a C-call boundary, and plain `pcall` is a C
              -- function. LrHttp.get yields, so wrapping it in pcall raises
              -- "Yielding is not allowed within a C or metamethod call" — from
              -- inside an async task, where the call is otherwise perfectly
              -- legal. The SDK ships this yield-safe pcall for the purpose.
              local ok, result = LrTasks.pcall(function()
                return Client.profiles()
              end)
              if not ok then
                LrDialogs.message("FrameFound", tostring(result), "critical")
                return
              end
              -- Who the token is and what it may do. An older server has no
              -- /panel/me; that is not a failed connection.
              local known, identity = LrTasks.pcall(function()
                return Client.identity()
              end)
              local lines = {}
              if known and identity and identity.name then
                table.insert(lines, "Connected as \"" .. identity.name .. "\" ("
                  .. table.concat(identity.scopes or {}, ", ") .. ") — FrameFound "
                  .. tostring(identity.server_version or "?") .. ".")
                if not Client.hasScope(identity, "export") then
                  table.insert(lines, "This token has no \"export\" scope, so a listing's video "
                    .. "frames cannot be grabbed. Create a token with export ticked for that.")
                end
              else
                table.insert(lines, "Connected.")
              end
              if #result == 0 then
                table.insert(lines, "No path profiles yet. Searching works, but nothing can be "
                  .. "added to Lightroom until FrameFound knows where this machine sees each "
                  .. "share: open Libraries in FrameFound and add a path profile per library.")
              else
                table.insert(lines, #result .. " path profile(s) available.")
              end
              LrDialogs.message("FrameFound", table.concat(lines, "\n\n"), "info")
            end)
          end,
        }),
      }),
    },
  }
end

return provider
