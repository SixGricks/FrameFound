"""Filesystem watcher: near-real-time pickup between reconciliation scans.

Watchdog events are treated as *hints*, never truth — SMB/NFS mounts drop and
duplicate events routinely. Every hinted path goes through the two-observation
stability gate before the shared upsert path runs. Deletions are left to the
reconciliation scan (an unlink event on a network mount is too unreliable to
act on destructively — and we never delete anyway, only flag).
"""

import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import structlog

from framefound.media.detect import media_type_for
from framefound.scanner.stability import FileObservation

log = structlog.get_logger()


def observe_file(path: Path) -> FileObservation | None:
    try:
        st = path.stat()
        with path.open("rb") as fh:
            fh.read(1)
        readable = True
    except OSError:
        try:
            st = path.stat()
        except OSError:
            return None
        readable = False
    return FileObservation(
        size_bytes=st.st_size,
        mtime_epoch=st.st_mtime,
        observed_at_epoch=time.time(),
        readable=readable,
    )


@dataclass
class Candidate:
    library_id: uuid.UUID
    relative_path: str
    first: FileObservation
    queued_at: float


class WatchQueue:
    """Thread-safe pending set fed by watchdog threads, drained by the
    scanner's async loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[tuple[uuid.UUID, str], Candidate] = {}

    def offer(self, library_id: uuid.UUID, root: Path, abs_path: str) -> None:
        path = Path(abs_path)
        if media_type_for(path.name) is None or path.name.startswith("."):
            return
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            return
        observation = observe_file(path)
        if observation is None:
            return
        with self._lock:
            key = (library_id, rel)
            existing = self._items.get(key)
            if existing is None or existing.first.size_bytes != observation.size_bytes:
                self._items[key] = Candidate(library_id, rel, observation, time.time())

    def due(self, min_age_seconds: float) -> list[Candidate]:
        cutoff = time.time() - min_age_seconds
        with self._lock:
            ready = [c for c in self._items.values() if c.queued_at <= cutoff]
            for candidate in ready:
                del self._items[(candidate.library_id, candidate.relative_path)]
            return ready

    def requeue(self, candidate: Candidate, observation: FileObservation) -> None:
        """Still unstable: keep watching with the fresher observation."""
        with self._lock:
            self._items[(candidate.library_id, candidate.relative_path)] = Candidate(
                candidate.library_id, candidate.relative_path, observation, time.time()
            )


def start_observer(library_id: uuid.UUID, root: Path, queue: WatchQueue) -> object | None:
    """Start a watchdog observer for one library. Returns None when watchdog
    is unavailable or the backend refuses the path (network mounts often do —
    reconciliation scans remain the source of truth)."""
    try:
        from watchdog.events import FileSystemEvent, FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        log.warning("watcher.watchdog_unavailable")
        return None

    class Handler(FileSystemEventHandler):
        def on_created(self, event: FileSystemEvent) -> None:
            if not event.is_directory:
                queue.offer(library_id, root, os.fsdecode(event.src_path))

        def on_modified(self, event: FileSystemEvent) -> None:
            if not event.is_directory:
                queue.offer(library_id, root, os.fsdecode(event.src_path))

        def on_moved(self, event: FileSystemEvent) -> None:
            if not event.is_directory:
                queue.offer(library_id, root, os.fsdecode(event.dest_path))

    try:
        observer = Observer()
        observer.schedule(Handler(), str(root), recursive=True)
        observer.daemon = True
        observer.start()
        log.info("watcher.started", root=str(root))
        return observer
    except OSError:
        log.warning("watcher.start_failed", root=str(root))
        return None
