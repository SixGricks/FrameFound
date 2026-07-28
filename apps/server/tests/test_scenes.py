"""Sample planning and perceptual hashing (pure logic, no FFmpeg needed)."""

from pathlib import Path

import pytest

from framefound.media.phash import dhash, hamming_distance
from framefound.processing.scenes import interval_for, plan_samples


def test_interval_scales_with_duration() -> None:
    assert interval_for(30) == 5.0
    assert interval_for(300) == 10.0
    assert interval_for(1800) == 30.0
    assert interval_for(7200) == 60.0


def test_plan_covers_long_take_without_cuts() -> None:
    plan = plan_samples(300.0, [])
    assert len(plan) == 30  # 300s / 10s cadence
    assert all(not is_scene for _, is_scene in plan)
    assert plan[0][0] == 0.0


def test_scene_changes_are_marked_and_ordered() -> None:
    plan = plan_samples(120.0, [12.5, 47.0, 96.25])
    scenes = [ts for ts, is_scene in plan if is_scene]
    assert scenes == [12.5, 47.0, 96.25]
    assert plan == sorted(plan)


def test_ticks_near_a_cut_are_suppressed() -> None:
    # 60s clip -> 5s cadence, guard 2.5s. A cut at 20.4 should absorb the
    # 20.0 tick rather than sampling the same shot twice.
    plan = plan_samples(60.0, [20.4])
    timestamps = [ts for ts, _ in plan]
    assert 20.4 in timestamps
    assert 20.0 not in timestamps


def test_frame_count_is_capped_but_spread() -> None:
    plan = plan_samples(36000.0, [float(i) for i in range(0, 36000, 7)], max_frames=50)
    assert len(plan) == 50
    assert plan[-1][0] > 30000  # thinned evenly, not truncated early


def test_zero_duration_still_samples_first_frame() -> None:
    assert plan_samples(0.0, []) == [(0.0, False)]


def test_dhash_stable_and_sensitive(tmp_path: Path) -> None:
    pil = pytest.importorskip("PIL.Image")
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    # dHash compares each pixel with its RIGHT neighbour, so the fixtures must
    # vary horizontally — a vertical gradient hashes to all zeros by design.
    horizontal = pil.linear_gradient("L").rotate(90)
    horizontal.save(a)
    horizontal.transpose(pil.Transpose.FLIP_LEFT_RIGHT).save(b)

    hash_a, hash_b = dhash(a), dhash(b)
    assert hash_a is not None and hash_b is not None
    assert len(hash_a) == 16
    assert dhash(a) == hash_a  # deterministic
    assert hamming_distance(hash_a, hash_b) > 6  # mirrored gradient differs


def test_dhash_survives_rescale(tmp_path: Path) -> None:
    pil = pytest.importorskip("PIL.Image")
    original = tmp_path / "o.png"
    shrunk = tmp_path / "s.png"
    img = pil.linear_gradient("L").rotate(90).resize((512, 512))
    img.save(original)
    img.resize((200, 200)).save(shrunk)
    # The point of a perceptual hash: re-encoding at another size still matches.
    assert hamming_distance(dhash(original) or "", dhash(shrunk) or "") <= 6


def test_dhash_on_garbage_returns_none(tmp_path: Path) -> None:
    pytest.importorskip("PIL.Image")
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    assert dhash(bad) is None
