--[[
  FrameFound plugin for Lightroom Classic.

  Lightroom Classic has no UXP. Its extension model is the Lightroom SDK: Lua,
  loaded from a .lrdevplugin (unpacked) or .lrplugin (packaged) directory. That
  is why this is a wholly separate client from the Premiere panel rather than
  shared code — the two applications have nothing in common but HTTP.

  What is shared is the part that matters: both authenticate with the same
  scoped panel token, both call /api/v1/panel, and both translate catalogue
  paths through the same workstation profiles.

  Scope note: this plugin reads. It finds material in FrameFound and tells
  Lightroom where it is. It does not write to the FrameFound catalogue and it
  never touches an original — the token it uses cannot do either.
--]]

return {
  LrSdkVersion = 13.0,
  -- 6.0 is the floor for LrHttp with headers, which the token auth needs.
  LrSdkMinimumVersion = 6.0,

  LrToolkitIdentifier = "com.sixgricks.framefound",
  LrPluginName = "FrameFound",

  LrPluginInfoUrl = "https://github.com/SixGricks/FrameFound",

  -- Settings live in Plug-in Manager: server address and token, entered once.
  LrPluginInfoProvider = "FrameFoundInfoProvider.lua",

  LrLibraryMenuItems = {
    {
      title = "Search FrameFound…",
      file = "FrameFoundSearch.lua",
    },
    {
      title = "Show FrameFound paths for selected photo",
      file = "FrameFoundPaths.lua",
    },
  },

  VERSION = { major = 0, minor = 1, revision = 0, build = 0 },
}
