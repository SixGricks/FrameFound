# Threat Model (Milestone 0 outline)

Scope: a MediaHub instance on a Proxmox VM, optionally exposed to the internet
via Caddy/DDNS or Cloudflare Tunnel, indexing read-only NAS media. To be
expanded into a full analysis before Milestone 7 (remote access) ships.

## Assets to protect

1. **Original media** (integrity above all — the product promise).
2. Derived data revealing content: proxies, thumbnails, transcripts, captions.
3. Credentials: user passwords, session tokens, DDNS/Cloudflare tokens, NAS
   mount credentials (held by the host, not the app).
4. Server path structure (client names, project names in folder trees).
5. The host itself (pivot target).

## Trust boundaries

```
Internet ──► Caddy (TLS, headers) ──► web/api (authn/authz) ──► internal net (pg, redis, workers) ──► read-only NAS mounts
```

## Threats and mitigations (by category)

### Spoofing / authentication
- Brute force, credential stuffing → argon2id, rate limits, escalating delays,
  lockout, optional TOTP, audit log. **No default passwords; one-time expiring
  setup token** creates the first admin.
- Session theft → server-side opaque sessions, Secure/HttpOnly/SameSite
  cookies, revocation ("log out all"), configurable lifetime.

### Tampering
- Original media modification → mounts `:ro` in every container; app never
  holds write credentials to originals. Even full app compromise cannot alter
  originals.
- Malicious media (crafted MP4/TIFF hitting FFmpeg/ExifTool/Pillow CVEs) →
  pinned patched versions, subprocess isolation with timeouts + rlimits,
  non-root workers, no shell interpolation (argv arrays only), file-type
  sniffing before dispatch. Sandboxing (seccomp/nsjail) evaluated in M8.
- Poisoned metadata / filenames (stored XSS, log injection, SQLi) → all
  metadata treated as untrusted input: parameterized queries only, output
  encoding in UI, strict CSP, filename sanitization on display, no metadata in
  shell commands.

### Repudiation
- Audit log for logins, admin changes, remote-access toggles, deletions of
  derived data, reprocess commands.

### Information disclosure
- Unauthorized media access → **every byte authorized**: proxies/thumbnails
  served through the API with session checks; signed short-lived URLs for
  streaming (ADR-0012); no static file mounts of media in Caddy.
- Path traversal / arbitrary read → all paths resolved and verified under the
  library-root allowlist; asset access is by UUID, never client-supplied
  paths; scanner accepts new roots from admins only.
- SSRF via DDNS config → provider adapters use fixed vendor endpoints; no
  user-supplied URLs in MVP.
- Internal ports → only Caddy publishes ports; pg/redis unreachable from
  outside the compose network.
- Secrets → env/file-based, gitignored, never logged; scoped Cloudflare API
  tokens only (never global keys).

### Denial of service
- Indexing bombs (zip-adjacent: 10M tiny files, 8K 10-hour video) → bounded
  queues, per-job timeouts and memory caps, concurrency limits, quiet hours.
- Auth/API floods → Caddy body-size limits + app-level rate limiting on auth
  and search endpoints.

### Elevation of privilege
- Container escape → non-root users, dropped capabilities, no privileged
  containers, read-only root FS where practical, minimal base images, image
  scanning in CI (Trivy).
- Role bypass → central authorization dependency in FastAPI (admin / user /
  readonly), tested per route; admin UI hidden ≠ admin API protected — both
  enforced server-side.

### Privacy-specific
- Face recognition (post-MVP) is **off by default**, admin-enabled, with
  documented biometric-data deletion that hard-deletes embeddings.
- Local-first: any future cloud AI provider requires explicit opt-in and a
  visible "data leaves this server" disclosure.

## Open items (tracked for M7/M8)

- [ ] Formal review of signed-URL scheme (expiry, audience, revocation)
- [ ] FFmpeg sandbox evaluation (nsjail vs seccomp profiles vs gVisor)
- [ ] Dependency/container scanning gates in CI (fail thresholds)
- [ ] Pen-test checklist before recommending public HTTPS mode
- [ ] Backup encryption at rest
