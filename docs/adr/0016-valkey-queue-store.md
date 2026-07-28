# ADR-0016: Valkey as the queue store image

- Status: Accepted
- Date: 2026-07-28

## Context
ADR-0004 chose Celery with a Redis-protocol broker. Redis changed licenses at
7.4 (RSALv2/SSPL — not OSI-approved), which complicates redistribution
guidance for an Apache-2.0 project. Options: pin the last BSD Redis (7.2,
which will age out of security support) or adopt Valkey, the Linux
Foundation's BSD-3 fork that is protocol- and client-compatible.

## Decision
Use `valkey/valkey:8-alpine` in the compose stack. The service keeps the name
`redis`, and clients keep using `redis-py`/Celery's redis transport — the wire
protocol is unchanged, so application code is untouched.

## Consequences
- License inventory stays uniformly permissive (docs/licensing.md updated).
- Actively maintained upstream instead of a frozen 7.2 pin.
- Risk accepted: Valkey and Redis may diverge in future major versions;
  our usage (lists, pub/sub for Celery) is core protocol with effectively no
  divergence risk in the foreseeable window.
