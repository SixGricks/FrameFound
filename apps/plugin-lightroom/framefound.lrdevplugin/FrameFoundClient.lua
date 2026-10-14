--[[
  Talking to FrameFound from Lightroom.

  Kept in one file so the three menu items share exactly one idea of what the
  API is and how it authenticates. LrHttp is synchronous and must not be called on
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
-- A search can be the first use of the text model after the server restarted,
-- and loading it on a CPU without AVX takes longer than a request should.
local SEARCH_TIMEOUT = 60

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

--- What an HTTP failure means, in words the person at the keyboard can act on.
--  Shared by every call, downloads included: a grab refused for a token
--  without the export scope used to be reported as "Lightroom could not add".
local function explainStatus(status, token, what)
  if status == 401 then
    -- Show what was actually sent. A token is 47 characters pasted by hand,
    -- and "rejected" reads as "revoked" when the real cause is a dropped
    -- character or a trailing newline. Both ends and the length: a matching
    -- prefix alone proves nothing — the first failure of this kind had the
    -- right prefix and the right length and was still wrong, transcribed by
    -- eye from a screen, where 0/O, l/I/1 and -/_ do not survive.
    local head = token:sub(1, 10)
    local tail = token:sub(-4)
    return "FrameFound rejected this token.\n\n"
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
  elseif status == 403 then
    return "This panel token does not have permission for "
      .. (what or "that")
      .. ". Video frame grabs and XML bins need the \"export\" scope: tick it when "
      .. "creating the token under Security → Panel tokens."
  elseif status == 404 then
    return "FrameFound has no " .. (what or "such thing") .. " (404)."
  end
  return "FrameFound returned an error (" .. tostring(status) .. ") for " .. (what or "that") .. "."
end

--- GET a panel endpoint. Must be called inside LrTasks.startAsyncTask.
--  `timeout` is seconds; the default suits anything but a search.
function FrameFoundClient.get(path, timeout)
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

  local body, responseHeaders = LrHttp.get(server .. "/api/v1" .. path, headers, timeout or TIMEOUT)

  if body == nil then
    local reason = responseHeaders and responseHeaders.error and responseHeaders.error.name
    local hint = ""
    if reason and tostring(reason):lower():find("timeout", 1, true) then
      hint = " The server may be loading its search model after a restart; try again in a minute."
    end
    LrErrors.throwUserError(
      "Could not reach FrameFound at " .. server .. (reason and (" (" .. reason .. ")") or "") .. hint
    )
  end

  local status = responseHeaders and responseHeaders.status
  if status ~= nil and status >= 400 then
    LrErrors.throwUserError(explainStatus(status, token, path))
  end

  return body
end

--- GET a panel endpoint and decode it. Inside LrTasks.startAsyncTask only.
function FrameFoundClient.getJson(path)
  return Json.decode(FrameFoundClient.get(path))
end

--- Search. `mediaType` "" means any kind; nil keeps the photographs-only
--  default the dialogs want.
function FrameFoundClient.search(query, profile, mediaType, limit)
  local path = "/panel/search?q="
    .. FrameFoundClient.encode(query)
    .. "&profile="
    .. FrameFoundClient.encode(profile)
    .. "&media_type="
    .. FrameFoundClient.encode(mediaType == nil and "image" or mediaType)
    .. "&limit="
    .. tostring(limit or 24)
  local data = Json.decode(FrameFoundClient.get(path, SEARCH_TIMEOUT))
  return data.results or {}, data
end

function FrameFoundClient.profiles()
  return FrameFoundClient.getJson("/panel/profiles")
end

--- Who this token is, and what it may do: { kind, name, scopes, user_email,
--  server_version }. A server from before 2026-10 has no such endpoint and
--  answers 404, which callers treat as "unknown", not as a failure.
function FrameFoundClient.identity()
  return FrameFoundClient.getJson("/panel/me")
end

function FrameFoundClient.hasScope(identity, scope)
  for _, s in ipairs((identity and identity.scopes) or {}) do
    if s == scope then
      return true
    end
  end
  return false
end

--- Download a panel endpoint's file (a full-size frame grab) to a path.
--  Grabbing a 4K frame from a video on the NAS takes the server a few
--  seconds the first time, hence the long timeout. Returns true, or throws
--  a user error that says why. Inside LrTasks.startAsyncTask only.
function FrameFoundClient.download(path, destination)
  local token = FrameFoundClient.token()
  local headers = {
    { field = "Authorization", value = "Bearer " .. token },
  }
  local body, response = LrHttp.get(FrameFoundClient.server() .. "/api/v1" .. path, headers, 180)
  local status = response and response.status
  if body == nil or status == nil then
    local reason = response and response.error and response.error.name
    LrErrors.throwUserError(
      "Could not download " .. path .. " from FrameFound" .. (reason and (" (" .. reason .. ")") or "")
    )
  elseif status >= 400 then
    LrErrors.throwUserError(explainStatus(status, token, "the frame grab " .. path))
  end
  local file = io.open(destination, "wb")
  if file == nil then
    LrErrors.throwUserError("Could not write " .. destination)
  end
  file:write(body)
  file:close()
  return true
end

--- Recent listings, newest first: { listing_id, name, photos, created_at }.
--  200 is the server's limit: a month of shoots is a few dozen.
function FrameFoundClient.listings()
  return FrameFoundClient.getJson("/panel/listings?limit=200")
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
