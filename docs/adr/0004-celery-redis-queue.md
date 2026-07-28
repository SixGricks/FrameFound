# ADR-0004: Celery + Redis for background jobs

- Status: Accepted
- Date: 2026-07-28

## Context
Jobs must be idempotent, retryable, prioritized, observable, resumable after
restart, and routable to hardware (GPU vs CPU). Candidates: Celery, Dramatiq,
RQ, arq.

## Decision
Celery 5 with Redis broker. Queue segregation by resource class: `default`
(metadata, thumbnails), `media` (FFmpeg transcodes), `transcribe` and `vision`
(AI, GPU-eligible). Celery beat drives reconciliation scans, retention
cleanup, and DDNS checks. Job state is mirrored into the Postgres `jobs` table
by task signals so the UI dashboard never talks to the broker.

## Alternatives considered
- **Dramatiq**: cleaner core, native priorities — but a smaller ecosystem, no
  built-in beat equivalent (needs APScheduler), fewer battle-tested operational
  patterns. Close call; Celery's maturity and routing won.
- **RQ / arq**: too little routing/retry sophistication for GPU-aware
  scheduling.

## Consequences
- Redis priorities are approximate (per-queue lists); true prioritization comes
  from queue routing + per-queue worker concurrency, which is what
  hardware-aware scheduling needs anyway.
- Celery's chord/canvas complexity is deliberately avoided: the pipeline is
  modeled as independent idempotent tasks chained via explicit state
  transitions in the DB, not Celery workflows — restart-safety lives in our
  schema, not the broker.
