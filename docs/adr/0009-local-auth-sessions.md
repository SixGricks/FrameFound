# ADR-0009: Local auth with server-side sessions; OIDC deferred

- Status: Accepted
- Date: 2026-07-28

## Context
The first release must be securable by a non-expert on the public internet:
no default credentials, brute-force resistance, revocable sessions. SSO
(OIDC/Authentik/Keycloak/Entra) matters to some deployments but not the MVP
audience.

## Decision
Built-in local accounts:
- **argon2id** password hashing (OWASP-recommended; explicit cost parameters).
- **Opaque server-side sessions**: random 256-bit token in an HttpOnly,
  SameSite=Lax cookie; only the SHA-256 of the token stored. Sliding 12 h idle
  expiry capped by a 7 d absolute limit (both configurable).
- **Roles**: admin / user / readonly enforced by a FastAPI dependency.
- **First admin** created only with the one-time installer-generated setup
  token, consumed on success.
- **Login rate limiting**: escalating lockouts per (ip, email); generic error
  messages; timing-equalized email probing; audit log on every auth event.

## Alternatives considered
- **JWTs**: stateless but non-revocable without a denylist (which reintroduces
  state), and long-lived tokens in cookies widen the theft window. Server-side
  sessions are simpler and strictly more controllable for a single-server appliance.
- **OIDC-first**: forces every small deployment to stand up an IdP. Deferred
  post-MVP behind the same session layer.

## Consequences
- Session validation costs one indexed DB lookup per request — negligible at
  this scale, and it buys instant revocation ("log out all sessions").
- SameSite=Lax is the CSRF baseline; re-audited in the M7 hardening pass
  along with trusted-proxy IP handling and TOTP.
