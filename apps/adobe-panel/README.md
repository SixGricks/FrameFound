# Adobe Premiere Pro Panel (Milestone 9 — not started)

Deliberately empty until the core API and path-mapping system are stable.

First task (ADR-0015): research Adobe's **current** supported extensibility
path for Premiere panels — UXP vs legacy CEP vs current plugin APIs — before
writing any code. Do not assume CEP.

The panel will consume the versioned HTTP API only (`/api/v1/...`), including
`GET /assets/{id}/paths` for workstation path-mapping. No Python imports, no
private endpoints.
