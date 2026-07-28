# ADR-0002: FastAPI + SQLAlchemy backend

- Status: Accepted
- Date: 2026-07-28

## Context
The backend must orchestrate FFmpeg, Whisper-family ASR, CLIP embeddings, and
OCR — all Python-native ecosystems — while serving a typed, documented HTTP API.

## Decision
Python 3.12 + FastAPI, Pydantic v2 models for request/response, SQLAlchemy 2.0
(async) + Alembic migrations, structlog for structured logging, pydantic-settings
for env-driven config.

## Alternatives considered
- **NestJS (Node)**: strong API ergonomics, but every AI/media integration
  would cross a process/language boundary; two runtimes to maintain. Rejected.
- **Go**: excellent for the scanner's file throughput, but the AI ecosystem
  gap is decisive. May reappear later for a hot-path scanner if profiling
  demands it.

## Consequences
- One language across API, workers, and scanner; shared models and config.
- OpenAPI schema generated for free → typed client for the web app and the
  future Premiere panel.
- Python performance ceilings are mitigated by pushing heavy work to Celery
  and I/O to asyncpg; revisit only with profiling evidence.
