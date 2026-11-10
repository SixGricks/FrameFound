"""Shot intelligence, the pure parts: motion, windows, smoothness per
speed, light and the descriptor vocabulary (framefound/media/motion.py,
sun.py, shot_vocabulary.py).

Frames are synthesised in numpy so these run anywhere; the same moves as
real FFmpeg-encoded clips are in test_shots_ffmpeg.py. A pan is an exact
sub-pixel shift of a fixed picture (a Fourier shift), the way a camera sees
one. A bilinear crop is not: interpolating a low-resolution texture at a
fractional position blurs it differently every frame, and at the steadiness
real gimbal footage reaches (a twentieth of a pixel) that alone reads as
shake.
"""

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from framefound.media import motion, shot_vocabulary, sun

W, H, FPS = 320, 180, 30.0


@pytest.fixture(scope="module")
def texture() -> np.ndarray:
    rng = np.random.default_rng(11)
    coarse = Image.fromarray((rng.random((80, 200, 3)) * 255).astype(np.uint8))
    base = np.asarray(coarse.resize((1600, 640), Image.Resampling.BICUBIC)).astype(float)
    fine = rng.random((640, 1600, 3)) * 40
    return np.clip(base * 0.8 + fine, 0, 255).astype(np.uint8)


@pytest.fixture(scope="module")
def world() -> np.ndarray:
    """The Fourier transform of a gray picture to pan across."""
    rng = np.random.default_rng(5)
    coarse = Image.fromarray((rng.random((60, 160)) * 255).astype(np.uint8))
    picture = np.asarray(coarse.resize((1024, 360), Image.Resampling.BICUBIC)).astype(float)
    return np.fft.fft2(picture + rng.random(picture.shape) * 20)


def _shifted(world: np.ndarray, dx: float, dy: float) -> np.ndarray:
    ky = np.fft.fftfreq(world.shape[0])[:, None]
    kx = np.fft.fftfreq(world.shape[1])[None, :]
    image = np.fft.ifft2(world * np.exp(2j * np.pi * (kx * dx + ky * dy))).real
    gray = np.clip(image[90 : 90 + H, 300 : 300 + W], 0, 255).astype(np.uint8)
    return np.stack([gray, gray, gray], axis=-1)


def pan(world: np.ndarray, seconds: float, fw_per_s: float, jitter_px: float = 0.0):  # type: ignore[no-untyped-def]
    """The camera travelling right at `fw_per_s`, so the picture moves left;
    `jitter_px` adds a 7 Hz / 9 Hz wobble."""
    for k in range(int(seconds * FPS)):
        t = k / FPS
        x = fw_per_s * W * t + jitter_px * math.sin(2 * math.pi * 7 * t)
        y = jitter_px * math.sin(2 * math.pi * 9 * t + 1)
        yield _shifted(world, x, y)


def test_a_pan_is_measured_at_its_true_speed_and_direction(world: np.ndarray) -> None:
    analysis = motion.analyse(pan(world, 4, 0.15), FPS)
    [window] = motion.windows(analysis)
    assert (window.move, window.direction) == ("pan", "right")
    assert window.speed == pytest.approx(0.15, rel=0.03)
    by = window.smooth_by_speed
    assert all(by[k] > 0.9 for k in ("25", "50", "100", "200")), by
    assert window.energy == "low"


def test_jitter_ruins_slow_motion_and_vanishes_sped_up(world: np.ndarray) -> None:
    """The same pan with a 7-9 Hz wobble of a pixel and a half. Slowed to 50%
    it plays at 3.5-4.5 Hz, squarely where the eye sees shake; at 400% it is
    28-36 Hz and invisible. Smoothness has to say both."""
    analysis = motion.analyse(pan(world, 4, 0.15, jitter_px=1.5), FPS)
    [window] = motion.windows(analysis)
    by = window.smooth_by_speed
    assert by["50"] < 0.3 and by["100"] < 0.3
    assert by["400"] > 0.8
    assert window.move == "pan", "jitter does not change what the move is"


def test_a_push_in_spreads_outward(texture: np.ndarray) -> None:
    source = Image.fromarray(texture[100:460, 300:940])

    def frames():  # type: ignore[no-untyped-def]
        for k in range(int(4 * FPS)):
            z = 1 + 0.15 * k / FPS
            cw, ch = 640 / z, 360 / z
            box = (320 - cw / 2, 180 - ch / 2, 320 + cw / 2, 180 + ch / 2)
            yield np.asarray(source.resize((W, H), Image.Resampling.BILINEAR, box=box))

    [window] = motion.windows(motion.analyse(frames(), FPS))
    assert (window.move, window.direction) == ("push-in", "in")
    assert window.features["scale"] > 0.08


def test_a_static_shot_with_moving_content_is_static_and_busy(texture: np.ndarray) -> None:
    """A locked-off camera on a site with a machine moving through it:
    no camera motion, high subject energy. Calm and busy are separate."""
    base = texture[200:380, 200:520]

    def frames():  # type: ignore[no-untyped-def]
        for k in range(int(4 * FPS)):
            frame = base.copy()
            x = 20 + int(1.5 * k)
            frame[50:130, x : x + 70] = texture[0:80, 600 + 3 * k : 670 + 3 * k]
            yield frame

    [window] = motion.windows(motion.analyse(frames(), FPS))
    assert window.move == "static"
    assert window.energy in ("medium", "high")

    calm = motion.windows(motion.analyse((base.copy() for _ in range(90)), FPS))
    assert calm[0].move == "static" and calm[0].energy == "low"


def test_a_change_of_direction_starts_a_new_window(world: np.ndarray) -> None:
    def frames():  # type: ignore[no-untyped-def]
        x = 0.0
        for k in range(int(6 * FPS)):
            x += (0.15 if k < 3 * FPS else -0.15) * W / FPS
            yield _shifted(world, x, 0.0)

    found = motion.windows(motion.analyse(frames(), FPS))
    assert [(w.move, w.direction) for w in found] == [("pan", "right"), ("pan", "left")]
    assert found[0].end_s == pytest.approx(3.0, abs=0.6)
    assert found[1].start_s == found[0].end_s


def test_a_wobble_shorter_than_a_second_does_not_split_a_move() -> None:
    vx = [-0.15] * 40 + [0.15] * 5 + [-0.15] * 40
    assert motion.segment(vx, [0.0] * 85, [0.0] * 85) == [(0, 85)]


def test_a_hard_cut_always_ends_a_window() -> None:
    vx = [-0.15] * 60
    assert motion.segment(vx, [0.0] * 60, [0.0] * 60, cuts={30}) == [(0, 30), (30, 60)]


@pytest.mark.parametrize(
    ("features", "top_down", "expected"),
    [
        ({"tx": -0.1}, False, ("pan", "right")),
        ({"tx": 0.1}, False, ("pan", "left")),
        # The near ground (bottom) moves faster: a lateral glide.
        ({"tx": -0.1, "px": -0.15}, False, ("lateral glide", "right")),
        # Foreground and background part ways around the subject: an orbit.
        ({"tx": -0.01, "px": -0.5}, False, ("orbit", "right")),
        ({"ty": 0.1}, False, ("tilt", "up")),
        ({"ty": 0.1, "py": 0.08, "scale": -0.05}, False, ("rise", "up")),
        ({"ty": 0.1, "py": 0.08, "scale": 0.05}, False, ("push-in", "in")),
        ({"ty": -0.1, "py": -0.08, "scale": 0.05}, False, ("descend", "down")),
        ({"scale": 0.2}, False, ("push-in", "in")),
        ({"scale": -0.2}, False, ("pull-out", "out")),
        ({"tx": 0.002}, False, ("static", None)),
        # Straight down, the same shift is the drone travelling over ground.
        ({"tx": -0.1}, True, ("top-down drift", "right")),
        ({"scale": 0.2}, True, ("descend", "down")),
    ],
)
def test_moves_are_told_apart_by_how_the_picture_moves(
    features: dict[str, float], top_down: bool, expected: tuple[str, str | None]
) -> None:
    assert motion.classify(features, top_down=top_down) == expected


def test_slow_motion_needs_frames_the_source_has() -> None:
    """A 29.97 source at 50% in a 29.97 sequence shows every frame twice;
    a 59.94 one fills it with real frames."""
    thirty = motion.speed_fit(4.0, 29.97, 29.97)
    sixty = motion.speed_fit(4.0, 59.94, 29.97)
    assert thirty["50"] == {"length_s": 8.0, "real_frames": False}
    assert sixty["50"] == {"length_s": 8.0, "real_frames": True}
    assert sixty["25"]["real_frames"] is False
    assert thirty["400"] == {"length_s": 1.0}


def test_the_track_is_compact_and_rebuildable_data(world: np.ndarray) -> None:
    analysis = motion.analyse(pan(world, 3, 0.15), FPS)
    track = motion.track(analysis)
    assert track["hz"] == motion.TRACK_HZ and len(track["vx"]) == 30
    assert track["vx"][10] == pytest.approx(-0.15, rel=0.05)
    text = json.dumps(track)
    assert "NaN" not in text and len(text) < 8000


def test_look_tells_hard_sun_from_flat_light() -> None:
    assert motion.describe_look(0.5, 0.45, 0.08, 0.26)["sky"] == "direct"
    assert motion.describe_look(0.6, 0.08, 0.0, 0.07)["sky"] == "overcast"


# --- the sun -------------------------------------------------------------------


def test_the_sun_at_greenwich_on_the_june_solstice() -> None:
    """At solar noon the sun stands 90 - 51.48 + 23.44 = 61.96 degrees up."""
    start = datetime(2026, 6, 21, 11, 0, tzinfo=UTC)
    best = max(sun.sun_position(start + timedelta(minutes=m), 51.4769, 0.0) for m in range(120))
    assert best[0] == pytest.approx(61.96, abs=0.1)
    assert best[1] == pytest.approx(180, abs=1.0)


def test_sunset_over_philadelphia_on_the_brookside_shoot() -> None:
    """2026-11-02, the day Brookside was flown: sunset about 4:58 pm EST."""
    when = datetime(2026, 11, 2, 21, 0, tzinfo=UTC)
    while sun.sun_position(when, 39.95, -75.17)[0] > -0.833:
        when += timedelta(minutes=1)
    assert (
        datetime(2026, 11, 2, 21, 53, tzinfo=UTC)
        <= when
        <= datetime(2026, 11, 2, 22, 3, tzinfo=UTC)
    )


@pytest.mark.parametrize(
    ("elevation", "expected"),
    [
        (-10, "night"),
        (-5, "blue hour"),
        (3, "golden hour"),
        (12, "low sun"),
        (30, "mid sun"),
        (60, "high sun"),
    ],
)
def test_light_classes(elevation: float, expected: str) -> None:
    assert sun.light_class(elevation) == expected


def test_light_comes_from_the_sun_not_the_colour() -> None:
    """Brookside's golden-hour clips measured cooler than its 11-degree
    ones (autumn leaves set the colour), so warmth must not move a window
    into golden hour."""
    from framefound.processing.shots import light_for

    assert light_for(7.5, {"warmth": 0.12, "sky": "direct"}) == "low sun"
    assert light_for(3.5, {"warmth": 0.01, "sky": "direct"}) == "golden hour"


def test_the_brookside_clips_shot_late_were_golden() -> None:
    """Brookside Country Club (41.73 N, 70.59 W, from 219 located photos),
    2026-11-02: 0633 at 21:13:54 UTC was golden hour; 0494 at 19:56 was not."""
    elevation = sun.sun_position(datetime(2026, 11, 2, 21, 14, 2, tzinfo=UTC), 41.7282, -70.5894)
    assert sun.light_class(elevation[0]) == "golden hour"
    earlier = sun.sun_position(datetime(2026, 11, 2, 19, 56, 20, tzinfo=UTC), 41.7282, -70.5894)
    assert sun.light_class(earlier[0]) == "low sun"


# --- vocabulary ----------------------------------------------------------------


def test_another_clients_vocabulary_is_a_file_not_a_deploy(tmp_path: Path) -> None:
    folder = tmp_path / shot_vocabulary.VOCABULARY_DIR
    folder.mkdir()
    (folder / "barns.json").write_text(
        json.dumps({"label": "Barns", "subjects": {"Timber Frame": ["exposed beams"]}}),
        encoding="utf-8",
    )
    (folder / "broken.json").write_text("{not json", encoding="utf-8")
    found = shot_vocabulary.load_vocabularies(tmp_path)
    assert {"golf-construction", "barns"} <= set(found)
    subjects = shot_vocabulary.all_subjects(found)
    assert subjects["timber frame"] == ["exposed beams"]
    assert "bunker" in subjects and "pump station" in subjects


def _unit(*values: float) -> list[float]:
    norm = math.sqrt(sum(v * v for v in values))
    return [v / norm for v in values]


def test_subjects_are_the_ones_a_window_stands_out_for() -> None:
    vector = _unit(1, 0.2, 0, 0)
    subjects = {
        "bunker": [_unit(1, 0.1, 0, 0)],
        "green": [_unit(0.7, 0.7, 0, 0)],
        "trench": [_unit(0, 0, 1, 0)],
        "sod": [_unit(0, 0, 0, 1)],
        "pond": [_unit(0, 0.1, 0.9, 0.1)],
    }
    found = shot_vocabulary.describe_subjects(vector, subjects)
    assert list(found)[0] == "bunker"
    assert "trench" not in found and "sod" not in found


def test_a_drone_is_aerial_whatever_clip_says() -> None:
    prompts = {"view": {"aerial": [_unit(0, 1)], "ground": [_unit(1, 0)]}}
    looks_grounded = _unit(1, 0.1)
    assert shot_vocabulary.describe_framing(looks_grounded, prompts)["view"] == "ground"
    assert (
        shot_vocabulary.describe_framing(looks_grounded, prompts, is_drone=True)["view"] == "aerial"
    )
