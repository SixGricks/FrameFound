"""Filesystem scanner and watcher service.

Entrypoint: python -m mediahub.scanner  (see __main__.py)

Design (docs/architecture.md §pipeline):
- watchdog events where the filesystem supports them (local, some NFS)
- periodic reconciliation scans as the source of truth (SMB/NFS events are
  unreliable) — the watcher is only an optimization
- stability gate before any file is enqueued for processing (stability.py)
"""
