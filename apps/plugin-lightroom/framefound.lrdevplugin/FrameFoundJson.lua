--[[
  A small JSON decoder, because the Lightroom SDK has none.

  The first version of this plugin pulled fields out with string patterns.
  That stopped being safe with Windows paths: JSON writes Y:\GELCO as
  "Y:\\GELCO", a pattern hands back the escaped form, and Lightroom is asked
  for a file at a path with doubled backslashes. A caption with a quote or a
  bracket in it would have cut a listing short. Decoding properly is a
  hundred lines and ends that class of bug.

  Objects, arrays, strings (every escape, \u and surrogate pairs included),
  numbers, true and false. null becomes nil — an absent field, which every
  caller already checks for.
--]]

local Json = {}

local ESCAPES = {
  ['"'] = '"',
  ["\\"] = "\\",
  ["/"] = "/",
  b = "\b",
  f = "\f",
  n = "\n",
  r = "\r",
  t = "\t",
}

local function fail(pos, what)
  error("FrameFound sent something this plugin cannot read (" .. what .. " at character "
    .. pos .. ")", 0)
end

local function utf8(code)
  if code < 0x80 then
    return string.char(code)
  elseif code < 0x800 then
    return string.char(0xC0 + math.floor(code / 0x40), 0x80 + code % 0x40)
  elseif code < 0x10000 then
    return string.char(
      0xE0 + math.floor(code / 0x1000),
      0x80 + math.floor(code / 0x40) % 0x40,
      0x80 + code % 0x40
    )
  end
  return string.char(
    0xF0 + math.floor(code / 0x40000),
    0x80 + math.floor(code / 0x1000) % 0x40,
    0x80 + math.floor(code / 0x40) % 0x40,
    0x80 + code % 0x40
  )
end

local function skip(text, pos)
  return text:find("[^ \t\r\n]", pos) or #text + 1
end

local decodeValue

local function decodeString(text, pos)
  local parts = {}
  local i = pos + 1
  while true do
    local j = text:find('["\\]', i)
    if not j then
      fail(pos, "a string that never ends")
    end
    table.insert(parts, text:sub(i, j - 1))
    if text:sub(j, j) == '"' then
      return table.concat(parts), j + 1
    end
    local kind = text:sub(j + 1, j + 1)
    if kind == "u" then
      local code = tonumber(text:sub(j + 2, j + 5), 16)
      if code == nil or #text:sub(j + 2, j + 5) < 4 then
        fail(j, "a bad \\u escape")
      end
      local after = j + 6
      -- A character beyond the first 65,536 arrives as a surrogate pair.
      if code >= 0xD800 and code <= 0xDBFF and text:sub(after, after + 1) == "\\u" then
        local low = tonumber(text:sub(after + 2, after + 5), 16)
        if low and low >= 0xDC00 and low <= 0xDFFF then
          code = 0x10000 + (code - 0xD800) * 0x400 + (low - 0xDC00)
          after = after + 6
        end
      end
      table.insert(parts, utf8(code))
      i = after
    else
      local char = ESCAPES[kind]
      if char == nil then
        fail(j, "a bad escape")
      end
      table.insert(parts, char)
      i = j + 2
    end
  end
end

local function decodeNumber(text, pos)
  local literal = text:match("^-?%d+%.?%d*[eE]?[-+]?%d*", pos)
  local value = literal and tonumber(literal)
  if value == nil then
    fail(pos, "a bad number")
  end
  return value, pos + #literal
end

local function decodeArray(text, pos)
  local out, n = {}, 0
  pos = skip(text, pos + 1)
  if text:sub(pos, pos) == "]" then
    return out, pos + 1
  end
  while true do
    local value
    value, pos = decodeValue(text, pos)
    n = n + 1
    out[n] = value
    pos = skip(text, pos)
    local c = text:sub(pos, pos)
    if c == "]" then
      return out, pos + 1
    elseif c ~= "," then
      fail(pos, "a missing comma in a list")
    end
    pos = skip(text, pos + 1)
  end
end

local function decodeObject(text, pos)
  local out = {}
  pos = skip(text, pos + 1)
  if text:sub(pos, pos) == "}" then
    return out, pos + 1
  end
  while true do
    if text:sub(pos, pos) ~= '"' then
      fail(pos, "a missing field name")
    end
    local key, value
    key, pos = decodeString(text, pos)
    pos = skip(text, pos)
    if text:sub(pos, pos) ~= ":" then
      fail(pos, "a missing colon")
    end
    value, pos = decodeValue(text, pos + 1)
    out[key] = value
    pos = skip(text, pos)
    local c = text:sub(pos, pos)
    if c == "}" then
      return out, pos + 1
    elseif c ~= "," then
      fail(pos, "a missing comma")
    end
    pos = skip(text, pos + 1)
  end
end

decodeValue = function(text, pos)
  pos = skip(text, pos)
  local c = text:sub(pos, pos)
  if c == "{" then
    return decodeObject(text, pos)
  elseif c == "[" then
    return decodeArray(text, pos)
  elseif c == '"' then
    return decodeString(text, pos)
  elseif c == "-" or c:match("%d") then
    return decodeNumber(text, pos)
  elseif text:sub(pos, pos + 3) == "true" then
    return true, pos + 4
  elseif text:sub(pos, pos + 4) == "false" then
    return false, pos + 5
  elseif text:sub(pos, pos + 3) == "null" then
    return nil, pos + 4
  end
  fail(pos, "an unexpected character")
end

function Json.decode(text)
  if type(text) ~= "string" then
    fail(0, "no response")
  end
  local value, pos = decodeValue(text, 1)
  if skip(text, pos) <= #text then
    fail(pos, "more after the end")
  end
  return value
end

return Json
