# ADR-0001: Monorepo with a single Python server image

- Status: Accepted
- Date: 2026-07-28

## Context
The product brief proposed separate service codebases (`services/scanner`,
`transcriber`, `vision`, `proxy-generator`) plus a `packages/` tree. All of
these share the database models, configuration, media probing, and most
dependencies. The heavy AI dependency stack (PyTorch/CUDA) dominates image
size either way.

## Decision
One repository; one Python package (`mediahub`) built into **one container
image** with multiple entrypoints (`api`, `worker`, `worker-ai`, `scanner`,
`scheduler`, `ddns`). Runtime isolation comes from separate containers and
separate Celery queues, not separate codebases. The frontend (`apps/web`) is
its own image.

## Alternatives considered
- **Per-service Python packages/images**: 4-6× CI build time, version skew between
  services sharing one schema, dependency duplication, no isolation benefit
  beyond what containers already give. Rejected as premature.
- **Full monolith (worker inside API process)**: couples API latency to FFmpeg
  and GPU workloads; prevents GPU-only scheduling. Rejected.

## Consequences
- Single migration history, single test suite, one image to version and ship
  (plus web) — matches the "single-install appliance" goal.
- The AI worker image is large even for the API container; acceptable at this
  scale. If it hurts, a slim non-AI image variant can be split later without
  code changes (same package, different extras).
- A future contributor can still extract a service; the queue boundary is
  already the seam.
