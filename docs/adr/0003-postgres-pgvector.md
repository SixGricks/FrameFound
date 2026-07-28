# ADR-0003: PostgreSQL + pgvector as the only datastore for MVP

- Status: Accepted
- Date: 2026-07-28

## Context
Search needs relational filters, full-text relevance, and vector similarity.
Dedicated engines (OpenSearch, Qdrant, Weaviate) each add an operational
component to a product whose core promise is "installable by a small business."

## Decision
PostgreSQL 16 (`pgvector/pgvector` image) serves all three modes: relational
metadata, `tsvector` FTS with trigram support, and pgvector HNSW indexes for
embeddings. A `SearchBackend` interface isolates query construction so a
dedicated engine can be added post-1.0 behind the same contract.

## Alternatives considered
- **OpenSearch from day one**: better lexical ranking (BM25) and faceting, but
  a second JVM-based stateful service, doubled backup surface, and cluster
  tuning knowledge nobody in the target audience has. Deferred.
- **Qdrant sidecar for vectors**: fast, but splits the transactional boundary —
  vector rows and asset rows can drift. pgvector HNSW is adequate for the
  design target (hundreds of thousands of assets ≈ low millions of vectors).

## Consequences
- One database to run, back up, and restore. `manage.sh backup` is `pg_dump`
  plus config.
- Hybrid ranking implemented app-side (RRF) rather than engine-side; weights
  live in code where they're testable.
- Scale risk documented in roadmap: benchmark HNSW at 1M+ vectors during beta
  hardening (M8) before promising library-size numbers.
