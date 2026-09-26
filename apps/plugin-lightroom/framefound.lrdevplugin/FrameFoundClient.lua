--[[
  Talking to FrameFound from Lightroom.

  Kept in one file so the two menu items share exactly one idea of what the API
  is and how it authenticates. LrHttp is synchronous and must not be called on
  the main task, so every caller wraps this in LrTasks.startAsyncTask — a
  mistake that manifests as Lightroom freezing rather than as an error, which
  is why it is said here as well as at each call site.

  There is no JSON library in the Lightroom SDK, so FrameFoundJson.lua is a
  small decoder. (Patterns were used until Windows paths arrived escaped —
  "Y:\\GELCO" — and a caption could contain a bracket.)
--]]

local LrHttp = import "LrHttp"
local LrPrefs = import "LrPrefs"
local LrErrors = import "LrErrors"

local Json = require "FrameFoundJson"

local FrameFoundClient = {}

local TIMEOUT = 20

function FrameFoundClient.prefs()
  return LrPrefs.prefsForPlugin()
end

--- Trimmed. A token is pasted, and a paste routinely carries a trailing space
--  or newline that turns a valid credential into a 401 with nothing to see.
function FrameFoundClient.trim(value)
  if value == nil then
    return ""
  end
  return (tostring(value):gsub("^%s+", ""):gsub("%s+$", ""))
end

function FrameFoundClient.token()
  return FrameFoundClient.trim(FrameFoundClient.prefs().token)
end

function FrameFoundClient.server()
  return (FrameFoundClient.trim(FrameFoundClient.prefs().server):gsub("/+$", ""))
end

function FrameFoundClient.configured()
  return FrameFoundClient.server() ~= "" and FrameFoundClient.token() ~= ""
end

-- Percent-encode a query value. Lightroom has no url-encode helper.
function FrameFoundClient.encode(value)
  if value == nil then
    return ""
  end
  return (
    tostring(value)
      :gsub("[^%w%-%._~ ]", function(c)
        return string.format("%%%02X", string.byte(c))
      end)
      :gsub(" ", "+")
  )
end

--- GET a panel endpoint. Must be called inside LrTasks.startAsyncTask.
function FrameFoundClient.get(path)
  local prefs = FrameFoundClient.prefs()
  if not FrameFoundClient.configured() then
    LrErrors.throwUserError(
      "FrameFound is not set up yet. Open File → Plug-in Manager, "
        .. "select FrameFound, and enter your server address and panel token."
    )
  end

  local server = FrameFoundClient.server()
  local token = FrameFoundClient.token()
  local headers = {
    { field = "Authorization", value = "Bearer " .. token },
    { field = "Accept", value = "application/json" },
  }

  local body, responseHeaders = LrHttp.get(server .. "/api/v1" .. path, headers, TIMEOUT)

  if body == nil then
    local reason = responseHeaders and responseHeaders.error and responseHeaders.error.name
    LrErrors.throwUserError(
      "Could not reach FrameFound at " .. server .. (reason and (" (" .. reason .. ")") or "")
    )
  end

  local status = responseHeaders and responseHeaders.status
  if status == 401 then
    -- Show what was actually sent. A token is 43 characters pasted by hand,
    -- and "rejected" reads as "revoked" when the real cause is a dropped
    -- character or a trailing newline. The prefix shown here is the same one
    -- listed on the Security page, so the two can simply be compared — which
    -- turns a guess into a two-second check.
    -- Show both ends and the length. A matching prefix alone proves nothing:
    -- the first failure of this kind had the right prefix and the right
    -- length, and was still wrong — the token had been transcribed by eye
    -- from a screen, and 0/O, l/I/1 and -/_ do not survive that. Showing the
    -- tail as well is what makes a mid-string error visible.
    local head = token:sub(1, 10)
    local tail = token:sub(-4)
    LrErrors.throwUserError(
      "FrameFound rejected this token.\n\n"
        .. "Sent: "
        .. head
        .. "…"
        .. tail
        .. "  ("
        .. tostring(#token)
        .. " characters; a valid one is 47)\n\n"
        .. "Most likely it was typed or read by eye rather than pasted — "
        .. "0/O, l/I/1 and -/_ all look alike in a token. Use the Copy button "
        .. "on Security → Panel tokens, or create a fresh one and paste it. "
        .. "A matching prefix is not proof: check the length and the last "
        .. "four characters too."
    )
  elseif status == 403 then
    LrErrors.throwUserError("This panel token does not have permission for that.")
  elseif status ~= nil and status >= 400 then
    LrErrors.throwUserError("FrameFound returned an error (" .. tostring(status) .. ").")
  end

  return body
end

--- GET a panel endpoint and decode it. Inside LrTasks.startAsyncTask only.
function FrameFoundClient.getJson(path)
  return Json.decode(FrameFoundClient.get(path))
end

function FrameFoundClient.search(query, profile, mediaType, limit)
  local path = "/panel/search?q="
    .. FrameFoundClient.encode(query)
    .. "&profile="
    .. FrameFoundClient.encode(profile)
    .. "&media_type="
    .. FrameFoundClient.encode(mediaType or "image")
    .. "&limit="
    .. tostring(limit or 24)
  local data = FrameFoundClient.getJson(path)
  return data.results or {}, data
end

function FrameFoundClient.profiles()
  return FrameFoundClient.getJson("/panel/profiles")
end

--- Recent listings, newest first: { listing_id, name, photos, created_at }.
function FrameFoundClient.listings()
  return FrameFoundClient.getJson("/panel/listings?limit=60")
end

--- One listing's photographs in order, at this profile's paths, each with
--  its RAW original (raw_path) when the catalogue has one.
function FrameFoundClient.listing(listingId, profile)
  return FrameFoundClient.getJson(
    "/panel/listings/"
      .. FrameFoundClient.encode(listingId)
      .. "?profile="
      .. FrameFoundClient.encode(profile)
  )
end

return FrameFoundClient
