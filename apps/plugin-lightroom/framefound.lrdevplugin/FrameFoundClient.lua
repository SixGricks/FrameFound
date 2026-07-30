--[[
  Talking to FrameFound from Lightroom.

  Kept in one file so the two menu items share exactly one idea of what the API
  is and how it authenticates. LrHttp is synchronous and must not be called on
  the main task, so every caller wraps this in LrTasks.startAsyncTask — a
  mistake that manifests as Lightroom freezing rather than as an error, which
  is why it is said here as well as at each call site.

  There is no JSON library in the Lightroom SDK. Rather than vendor one, the
  handful of fields this plugin needs are pulled out with patterns. That is a
  deliberate trade: a real parser would be more correct in general, but this
  code only ever reads responses produced by an API in the same repository,
  and the failure mode of a missing field is a nil the caller checks.
--]]

local LrHttp = import "LrHttp"
local LrPrefs = import "LrPrefs"
local LrErrors = import "LrErrors"

local FrameFoundClient = {}

local TIMEOUT = 20

function FrameFoundClient.prefs()
  return LrPrefs.prefsForPlugin()
end

function FrameFoundClient.configured()
  local prefs = FrameFoundClient.prefs()
  return prefs.server ~= nil and prefs.server ~= "" and prefs.token ~= nil and prefs.token ~= ""
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

  local server = prefs.server:gsub("/$", "")
  local headers = {
    { field = "Authorization", value = "Bearer " .. prefs.token },
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
    LrErrors.throwUserError(
      "FrameFound rejected the panel token. It may have been revoked — "
        .. "create a new one under Security → Panel tokens."
    )
  elseif status == 403 then
    LrErrors.throwUserError("This panel token does not have permission for that.")
  elseif status ~= nil and status >= 400 then
    LrErrors.throwUserError("FrameFound returned an error (" .. tostring(status) .. ").")
  end

  return body
end

--- Every `"key": "value"` and `"key": number` in one JSON object fragment.
local function fields(fragment)
  local out = {}
  for key, value in fragment:gmatch('"([%w_]+)"%s*:%s*"(.-)"') do
    out[key] = value
  end
  for key, value in fragment:gmatch('"([%w_]+)"%s*:%s*(-?[%d%.]+)') do
    out[key] = tonumber(value)
  end
  return out
end

--- Split a JSON array of flat objects into a list of tables.
--  Only handles objects without nested braces, which is all the panel API
--  returns for these two endpoints.
function FrameFoundClient.objects(json, arrayKey)
  local results = {}
  local body = json
  if arrayKey then
    body = json:match('"' .. arrayKey .. '"%s*:%s*%[(.-)%]')
    if body == nil then
      return results
    end
  end
  for fragment in body:gmatch("{(.-)}") do
    table.insert(results, fields(fragment))
  end
  return results
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
  local body = FrameFoundClient.get(path)
  return FrameFoundClient.objects(body, "results"), body
end

function FrameFoundClient.profiles()
  return FrameFoundClient.objects(FrameFoundClient.get("/panel/profiles"))
end

return FrameFoundClient
