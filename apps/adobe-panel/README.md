# Adobe clients

The Adobe integrations live elsewhere; this folder is kept so old links resolve.

- **Premiere Pro (UXP panel):** [`apps/panel-premiere/`](../panel-premiere/) —
  search the catalogue, import clips at this workstation's paths, or write an
  FCP7 XML bin.
- **Lightroom Classic (Lua plugin):**
  [`apps/plugin-lightroom/framefound.lrdevplugin/`](../plugin-lightroom/framefound.lrdevplugin/)
  — import listings into collections, search, and diagnose path profiles.

Both talk only to `/api/v1/panel` with a scoped, revocable panel token. Design,
installation, what has been verified in the host applications and what has
not: [docs/panels.md](../../docs/panels.md); the decision to build on UXP
rather than CEP, and to ship the XML export first:
[ADR-0019](../../docs/adr/0019-premiere-panel.md).
