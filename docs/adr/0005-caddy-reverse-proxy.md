# ADR-0005: Caddy as default reverse proxy

- Status: Accepted
- Date: 2026-07-28

## Context
The public HTTPS mode must be achievable by a non-expert: certificates,
renewal, redirects, and headers with near-zero configuration.

## Decision
Caddy 2. One Caddyfile serves both local-HTTP and public-HTTPS modes, switched
by `FRAMEFOUND_DOMAIN`. Automatic Let's Encrypt issuance/renewal, HTTP→HTTPS
redirect, modern TLS defaults out of the box.

## Alternatives considered
- **Traefik**: excellent Docker-label automation, but its mental model
  (providers, routers, middlewares) leaks into user-facing docs. Our routing is
  static — two upstreams — so labels buy nothing.
- **Nginx Proxy Manager**: GUI is friendly but adds a second admin UI + its own
  database; documented as an alternative only.
- **Raw nginx + certbot**: two moving parts and a renewal cron to explain.

## Consequences
- Remote-access wizard (M7) templates the Caddyfile rather than hand-editing;
  the file stays the single source of routing truth.
- Caddy's on-demand TLS and DNS-challenge plugins remain available for
  wildcard/CGNAT scenarios later.
