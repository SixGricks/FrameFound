# ADR-0012: Signed short-lived media URLs

- Status: Accepted
- Date: 2026-07-28

## Context
Every served byte must be authorized (threat model), but media is fetched by
`<img>`/`<video>` tags, future share links, and the Premiere panel — contexts
where attaching a session cookie is impossible or undesirable. Originals must
never be exposed; only app-generated derivatives.

## Decision
`GET /api/v1/media/{asset_id}/{kind}` serves derivative bytes when EITHER:
1. a valid session cookie accompanies the request (same-origin app usage), or
2. the query carries `exp` (unix expiry) + `sig` = HMAC-SHA256(secret,
   `asset_id:kind:exp`) — issued by `GET /assets/{id}/urls` to authenticated
   users, default TTL 1 h, max 24 h.

Tokens are stateless and scoped to exactly one (asset, kind). Streaming is
range-aware (single-range; multi-range falls back to full-file). Paths are
resolved exclusively through the derivatives table — never client input.

## Alternatives considered
- **Cookie-only**: breaks the Premiere panel and share links; couples media
  fetching to browser session state.
- **JWT media tokens**: more machinery for the same property; HMAC over three
  fields is auditable at a glance.
- **Serving originals** via the same door: explicitly rejected — downloads of
  originals become their own authorized, audited endpoint later (M6),
  distinct from streaming derivatives.

## Consequences
- Revocation is expiry-based; compromised links die within the TTL. Rotating
  `FRAMEFOUND_SECRET_KEY` invalidates all outstanding links at once.
- The secret key becomes load-bearing at M3: startup warning exists since M1;
  the installer generates it.
