# Security Policy

MediaHub is designed to be exposed to the internet by small organizations without
dedicated security staff. We take vulnerability reports seriously.

## Reporting a vulnerability

**Do not open a public issue for security problems.**

Use GitHub's private vulnerability reporting ("Report a vulnerability" under the
Security tab), or email the maintainers (address published at first release).

Please include: affected version, reproduction steps, and impact assessment.
We aim to acknowledge within 72 hours.

## Supported versions

Pre-1.0: only the latest release receives security fixes.

## Scope of particular concern

- Authentication/session bypass on the web UI or API
- Path traversal or arbitrary file read (especially escaping library roots)
- Unauthorized access to proxies, thumbnails, transcripts, or original paths
- Injection via media metadata, filenames, or transcripts (stored XSS, SQLi)
- SSRF via DDNS/webhook configuration
- Container escape or privilege escalation in the compose stack

See [docs/threat-model.md](docs/threat-model.md) for the full threat model.
