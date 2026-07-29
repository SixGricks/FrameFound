"""The departure lane: deletes and moves out of a watched tree.

The whole point of the lane is that a watchdog event is a prompt to look, not
a fact. These tests pin the cases where believing the event would be wrong.
"""

import uuid
from pathlib import Path

from framefound.scanner.watcher import WatchQueue


def _queue_with_file(tmp_path: Path, name: str = "clip.mp4") -> tuple[WatchQueue, uuid.UUID, Path]:
    root = tmp_path / "lib"
    root.mkdir(exist_ok=True)
    (root / name).write_bytes(b"x" * 32)
    return WatchQueue(), uuid.uuid4(), root


def test_departure_is_held_until_the_grace_period_elapses(tmp_path: Path) -> None:
    queue, lib, root = _queue_with_file(tmp_path)
    queue.depart(lib, root, str(root / "clip.mp4"), is_directory=False)
    assert queue.departures_due(min_age_seconds=60) == []
    assert len(queue.departures_due(min_age_seconds=0)) == 1


def test_a_file_reappearing_cancels_its_departure(tmp_path: Path) -> None:
    # Save-by-replace: an editor deletes then rewrites within a second. Acting
    # on the delete would flap the asset to `missing` and straight back.
    queue, lib, root = _queue_with_file(tmp_path)
    queue.depart(lib, root, str(root / "clip.mp4"), is_directory=False)
    queue.offer(lib, root, str(root / "clip.mp4"))
    assert queue.departures_due(min_age_seconds=0) == []


def test_departure_supersedes_a_pending_arrival(tmp_path: Path) -> None:
    queue, lib, root = _queue_with_file(tmp_path)
    queue.offer(lib, root, str(root / "clip.mp4"))
    queue.depart(lib, root, str(root / "clip.mp4"), is_directory=False)
    assert queue.due(min_age_seconds=0) == []
    assert len(queue.departures_due(min_age_seconds=0)) == 1


def test_non_media_deletions_are_ignored(tmp_path: Path) -> None:
    queue, lib, root = _queue_with_file(tmp_path)
    queue.depart(lib, root, str(root / "project.prproj"), is_directory=False)
    queue.depart(lib, root, str(root / ".DS_Store"), is_directory=False)
    assert queue.departures_due(min_age_seconds=0) == []


def test_directory_deletions_are_kept_regardless_of_name(tmp_path: Path) -> None:
    queue, lib, root = _queue_with_file(tmp_path)
    queue.depart(lib, root, str(root / "2026 Shoot"), is_directory=True)
    departures = queue.departures_due(min_age_seconds=0)
    assert len(departures) == 1
    assert departures[0].is_directory
    assert departures[0].relative_path == "2026 Shoot"


def test_paths_outside_the_library_root_are_dropped(tmp_path: Path) -> None:
    queue, lib, root = _queue_with_file(tmp_path)
    queue.depart(lib, root, str(tmp_path / "elsewhere" / "clip.mp4"), is_directory=False)
    assert queue.departures_due(min_age_seconds=0) == []


def test_repeated_events_collapse_to_one_departure(tmp_path: Path) -> None:
    queue, lib, root = _queue_with_file(tmp_path)
    for _ in range(5):
        queue.depart(lib, root, str(root / "clip.mp4"), is_directory=False)
    assert len(queue.departures_due(min_age_seconds=0)) == 1


def test_moved_directory_queues_every_media_file_inside_it(tmp_path: Path) -> None:
    # Watchdog reports the folder move and says nothing about its contents, so
    # without the walk a whole shoot would sit unindexed until the next scan.
    queue, lib, root = _queue_with_file(tmp_path)
    moved = root / "2026 Shoot"
    (moved / "a-roll").mkdir(parents=True)
    (moved / "a-roll" / "take1.mp4").write_bytes(b"y" * 32)
    (moved / "a-roll" / "take2.mov").write_bytes(b"y" * 32)
    (moved / "notes.txt").write_text("not media")
    (moved / ".hidden").mkdir()
    (moved / ".hidden" / "sidecar.mp4").write_bytes(b"z" * 32)

    queue.offer_tree(lib, root, str(moved))
    queued = {c.relative_path for c in queue.due(min_age_seconds=0)}
    assert queued == {"2026 Shoot/a-roll/take1.mp4", "2026 Shoot/a-roll/take2.mov"}
