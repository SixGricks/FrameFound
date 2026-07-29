"""Filesystem watcher: near-real-time pickup between reconciliation scans.

Watchdog events are treated as *hints*, never truth — SMB/NFS mounts drop and
duplicate events routinely. Every hinted path goes through the two-observation
stability gate before the shared upsert path runs.

Departures (deletes, and moves out of a watched tree) get their own lane with
a much longer grace period. A delete event is never acted on directly: after
the grace period the path is stat'd, and only a path that is genuinely gone
flips its asset to `missing`. Nothing is ever removed from the catalogue here
— reconciliation stays the authority, this just shortens the window where the
UI lists files that are no longer there.

Moved directories are the case worth care: watchdog reports the folder move
and says nothing about the files inside it, so the destination subtree is
walked explicitly. Each file then lands in the normal indexing path, where
move detection rebinds the existing row by content instead of indexing a
duplicate.
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


# A moved folder can hold a lot of files. The walk is bounded so a stray move
# of an entire archive root cannot pin a watchdog thread indefinitely; anything
# past the cap is left to the reconciliation scan, and the shortfall is logged
# rather than passed over in silence.
MAX_MOVED_TREE_FILES = 5000


@dataclass
class Candidate:
    library_id: uuid.UUID
    relative_path: str
    first: FileObservation
    queued_at: float


@dataclass
class Departure:
    """A path a watchdog event claims is gone. Believed only after a stat."""

    library_id: uuid.UUID
    relative_path: str
    is_directory: bool
    noticed_at: float


class WatchQueue:
    """Thread-safe pending set fed by watchdog threads, drained by the
    scanner's async loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[tuple[uuid.UUID, str], Candidate] = {}
        self._departures: dict[tuple[uuid.UUID, str], Departure] = {}

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
            # A path that just arrived is no longer a departure: an editor that
            # saves by replacing a file emits delete-then-create, and acting on
            # the delete would flap the asset to `missing` and straight back.
            self._departures.pop(key, None)

    def offer_tree(self, library_id: uuid.UUID, root: Path, abs_dir: str) -> None:
        """Queue every media file under a directory that has just appeared.

        Watchdog reports a directory move as one event and stays silent about
        its contents, so a folder dragged between libraries would otherwise go
        unnoticed until the next reconciliation scan.
        """
        seen = 0
        for dirpath, dirnames, filenames in os.walk(abs_dir):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if seen >= MAX_MOVED_TREE_FILES:
                    log.warning(
                        "watcher.moved_tree_truncated",
                        directory=abs_dir,
                        limit=MAX_MOVED_TREE_FILES,
                        note="remainder picked up by the next reconciliation scan",
                    )
                    return
                self.offer(library_id, root, os.path.join(dirpath, name))
                seen += 1

    def depart(self, library_id: uuid.UUID, root: Path, abs_path: str, is_directory: bool) -> None:
        path = Path(abs_path)
        if not is_directory and (media_type_for(path.name) is None or path.name.startswith(".")):
            return
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            return
        with self._lock:
            key = (library_id, rel)
            self._items.pop(key, None)
            self._departures.setdefault(key, Departure(library_id, rel, is_directory, time.time()))

    def departures_due(self, min_age_seconds: float) -> list[Departure]:
        cutoff = time.time() - min_age_seconds
        with self._lock:
            ready = [d for d in self._departures.values() if d.noticed_at <= cutoff]
            for departure in ready:
                del self._departures[(departure.library_id, departure.relative_path)]
            return ready

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

        def on_deleted(self, event: FileSystemEvent) -> None:
            queue.depart(library_id, root, os.fsdecode(event.src_path), event.is_directory)

        def on_moved(self, event: FileSystemEvent) -> None:
            src = os.fsdecode(event.src_path)
            dest = os.fsdecode(event.dest_path)
            queue.depart(library_id, root, src, event.is_directory)
            if event.is_directory:
                # Off the observer thread: a large tree would otherwise stall
                # every other event behind the walk.
                threading.Thread(
                    target=queue.offer_tree, args=(library_id, root, dest), daemon=True
                ).start()
            else:
                queue.offer(library_id, root, dest)

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
