"""What the camera does in a clip — measured once, at ingest.

The Brookside Quick 30 (2026-11-09) was cut by Claude through the MCP
connector, and most of its time went on work that belongs here: optical
flow scripts on the Mac to score every drone clip for shake, hesitation and
direction changes, done twice when the first pass was not strict enough;
telling an orbit from a push-in by eye. This module does that analysis once
per clip, so the next video starts from a lookup.

**Describe, never prescribe.** Nothing here knows what a good shot is for a
given edit. It reports what each stretch of a clip *is* (the move, how fast,
how steady, how busy the subject) and what it *can do* (how steady it stays
slowed down or sped up). Fast cuts, sped-up footage, floaty slow motion and
ramps are the editor's choices; the numbers must serve all of them.

How it works, in four steps:

1. **Camera motion per frame pair.** A grid of 24 overlapping blocks is
   phase-correlated between consecutive frames (numpy FFTs, no OpenCV), and
   an affine model is fitted to the 24 shifts by weighted least squares:
   translation, plus how the shift varies across the frame. That variation
   is what tells moves apart — a pan shifts everything alike, a lateral glide
   moves the near ground faster than the far, an orbit moves the foreground
   and background opposite ways, a push-in spreads outward.
2. **Subject motion**, separately: what is left once the camera is
   accounted for. Block shifts the model does not explain, and the share of
   pixels that still change after the previous frame is warped onto the
   current one (machines digging, a crew walking, water).
3. **Windows**: contiguous stretches where the camera keeps doing the same
   thing. A new window starts when the direction of travel turns by more
   than 60 degrees, or the camera starts or stops, and the change holds for
   a second.
4. **Smoothness per speed band.** Jitter that ruins a shot at 50% can vanish
   at 400%, and a hesitation nobody sees at 100% becomes a stutter at 400%.
   So steadiness is measured for each playback speed separately: the band
   of source frequencies that land where the eye sees shake on screen moves
   with the speed.

Everything in this module is pure (arrays in, numbers out); decoding and the
database live in framefound/processing/shots.py.
"""

import math
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

import numpy as np

# Bump when the analysis changes enough that stored windows should be
# rebuilt; the backfill re-runs anything older.
VERSION = 1

ANALYSIS_WIDTH = 320
MAX_ANALYSIS_FPS = 60.0
BLOCK = 48
GRID_COLS = 6
GRID_ROWS = 4
TRACK_HZ = 10
# Playback speeds an editor is likely to use, as fractions of real time.
SPEEDS: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0)
# Subject activity (the warped difference) on every other pair; it moves
# slowly and is the costliest step.
ACTIVITY_EVERY = 2
# Look statistics (brightness, colour) about twice a second.
LOOK_EVERY_S = 0.5

# Below this camera speed (frame widths per second) a stretch is "static":
# 1.2% of the frame per second is a drift no viewer reads as a move.
STATIC_SPEED = 0.012
# A change of direction or of moving/still must hold this long to start a
# new window; anything briefer is a wobble inside the move.
CHANGE_HOLD_S = 1.0
MIN_WINDOW_S = 1.0
TURN_COS = 0.5  # 60 degrees
# Half a 16:9 frame's height, in frame widths: where "top" and "bottom" are.
EDGE = 0.28

# Smoothness scales (see `smoothness`). Displacements are in frame widths.
# Calibrated on GELCO's Brookside drone clips (2026-11-09): the windows Rev 2
# kept wobble 0.00004-0.00006 frame widths in the shake band at 50%, the two
# Rev 1 used and DJ found not smooth enough 0.00010-0.00011 — a fifth of a
# pixel at 1080p, which slowed down reads as shimmer. At this scale those
# score about 0.85 and 0.6. Synthetic jitter of several pixels scores ~0.
SHAKE_SCALE = 0.00012
HESITATION_FREE = 0.08
HESITATION_SCALE = 0.25
REVERSAL_PENALTY = 0.8
# On-screen frequency bands (Hz) the eye reads as shake and as hesitation.
SHAKE_BAND = (3.0, 12.0)
HESITATION_BAND = (0.8, 4.0)
REVERSAL_CUTOFF = 1.5

MOVES = (
    "static",
    "pan",
    "tilt",
    "push-in",
    "pull-out",
    "orbit",
    "rise",
    "descend",
    "lateral glide",
    "top-down drift",
)


# --- per frame pair ------------------------------------------------------------


@dataclass(frozen=True)
class PairMotion:
    """Camera motion between two frames, in frame widths per frame.

    `tx`, `ty` is the shift of the picture's centre (positive right, down).
    The a-terms are how the shift changes across the frame, per frame width
    of position: `a11` horizontal stretch, `a22` vertical stretch, `a12` how
    the horizontal shift changes from top to bottom (parallax in a glide or
    an orbit), `a21` how the vertical shift changes from left to right (a
    roll). `res` is the 90th-percentile block shift the camera model does
    not explain: subject motion.
    """

    tx: float
    ty: float
    a11: float
    a12: float
    a21: float
    a22: float
    conf: float
    res: float
    cut: bool = False


def _hann2d(size: int) -> Any:
    w = np.hanning(size).astype(np.float32)
    return np.outer(w, w)


def to_gray(rgb: Any) -> Any:
    """Luma as float32 in [0, 255]."""
    f = rgb.astype(np.float32)
    return 0.299 * f[..., 0] + 0.587 * f[..., 1] + 0.114 * f[..., 2]


class MotionEstimator:
    """Block phase correlation and an affine fit, for one frame size."""

    def __init__(self, width: int, height: int) -> None:
        self.w, self.h = width, height
        self.bs = min(BLOCK, (height // 2) // 2 * 2 or 2, (width // 2) // 2 * 2 or 2)
        bs = self.bs
        self.xs = np.linspace(0, width - bs, GRID_COLS).round().astype(int)
        self.ys = np.linspace(0, height - bs, GRID_ROWS).round().astype(int)
        self.origins = [(int(y), int(x)) for y in self.ys for x in self.xs]
        self.window = _hann2d(bs)
        # Block centres relative to the frame centre, in frame widths.
        self.u = np.array(
            [
                ((x + bs / 2 - width / 2) / width, (y + bs / 2 - height / 2) / width)
                for y, x in self.origins
            ],
            dtype=np.float64,
        )
        n = len(self.origins)
        design = np.zeros((2 * n, 6))
        design[0::2, 0] = 1.0
        design[0::2, 2] = self.u[:, 0]
        design[0::2, 3] = self.u[:, 1]
        design[1::2, 1] = 1.0
        design[1::2, 4] = self.u[:, 0]
        design[1::2, 5] = self.u[:, 1]
        self.design = design
        # Sample grid for the warped difference: every other pixel, inset.
        gy, gx = np.mgrid[4 : height - 4 : 2, 4 : width - 4 : 2]
        self.grid_x = gx.astype(np.float32)
        self.grid_y = gy.astype(np.float32)
        self.grid_u = (self.grid_x - width / 2) / width
        self.grid_v = (self.grid_y - height / 2) / width
        self._cache: tuple[Any, Any, Any] | None = None

    def _blocks(self, frame: Any, offset: tuple[int, int]) -> tuple[Any, Any, Any]:
        bs = self.bs
        oy, ox = offset
        out = np.empty((len(self.origins), bs, bs), dtype=np.float32)
        shift = np.zeros((len(self.origins), 2))
        for i, (y, x) in enumerate(self.origins):
            yy = min(max(y + oy, 0), self.h - bs)
            xx = min(max(x + ox, 0), self.w - bs)
            out[i] = frame[yy : yy + bs, xx : xx + bs]
            shift[i] = (xx - x, yy - y)
        variance = out.reshape(len(self.origins), -1).var(axis=1)
        out -= out.reshape(len(self.origins), -1).mean(axis=1)[:, None, None]
        out *= self.window
        return out, shift, variance

    def estimate(
        self, prev: Any, cur: Any, predict: tuple[float, float] = (0.0, 0.0)
    ) -> PairMotion:
        """Motion from `prev` to `cur` (both gray, same size). `predict` is
        the expected shift in pixels — the previous pair's — so a fast move
        stays inside the blocks' range."""
        bs = self.bs
        shift = (int(round(predict[1])), int(round(predict[0])))
        cached = self._cache
        if cached is not None and cached[0] is prev:
            fa, variance = cached[1], cached[2]
        else:
            a, _, variance = self._blocks(prev, (0, 0))
            fa = np.fft.rfft2(a)
        b, offset, cur_variance = self._blocks(cur, shift)
        fb = np.fft.rfft2(b)
        # With no predicted shift the current frame's blocks sit on the
        # grid, so their spectrum is the next pair's `prev` for free.
        self._cache = (cur, fb, cur_variance) if shift == (0, 0) else None
        cross = fb * np.conj(fa)
        cross /= np.abs(cross) + 1e-9
        surface = np.fft.irfft2(cross, s=(bs, bs))
        flat = surface.reshape(len(self.origins), -1)
        peak = flat.argmax(axis=1)
        conf = flat[np.arange(len(peak)), peak]
        py, px = np.divmod(peak, bs)

        def sub(r: Any, yy: Any, xx: Any, axis: str) -> Any:
            # Foroosh, Zerubia and Berthod's estimate for a phase
            # correlation peak: the neighbour on the far side of the true
            # position carries the fraction. A parabola fitted to the peak
            # instead halves small shifts, which made a push-in look half
            # as fast vertically as horizontally.
            n = np.arange(len(yy))
            if axis == "x":
                left = r[n, yy, (xx - 1) % bs]
                right = r[n, yy, (xx + 1) % bs]
            else:
                left = r[n, (yy - 1) % bs, xx]
                right = r[n, (yy + 1) % bs, xx]
            centre = r[n, yy, xx]
            left = np.maximum(left, 0.0)
            right = np.maximum(right, 0.0)
            with np.errstate(divide="ignore", invalid="ignore"):
                step = np.where(right > left, right / (right + centre), -left / (left + centre))
            return np.clip(np.nan_to_num(step), -0.5, 0.5)

        sx = sub(surface, py, px, "x")
        sy = sub(surface, py, px, "y")
        dx = np.where(px > bs // 2, px - bs, px) + sx + offset[:, 0]
        dy = np.where(py > bs // 2, py - bs, py) + sy + offset[:, 1]

        weights = np.where((conf > 0.06) & (variance > 4.0), conf, 0.0)
        mean_abs = float(np.abs(cur - prev).mean())
        params, used, residual = self._fit(dx, dy, weights)
        if params is None:
            # Too little texture or a hard cut: no reliable camera motion.
            is_cut = mean_abs > 40.0
            return PairMotion(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, cut=is_cut)
        is_cut = mean_abs > 40.0 and float(conf.mean()) < 0.08
        res = float(np.percentile(residual, 90)) if residual.size else 0.0
        w = self.w
        tx, ty, a11, a12, a21, a22 = (float(v) for v in params)
        return PairMotion(
            tx / w,
            ty / w,
            a11 / w,
            a12 / w,
            a21 / w,
            a22 / w,
            float(conf[used].mean()) if used.any() else 0.0,
            res / w,
            cut=is_cut,
        )

    def _fit(self, dx: Any, dy: Any, weights: Any) -> tuple[Any, Any, Any]:
        used = weights > 0
        if used.sum() < 4:
            return None, used, np.zeros(0)
        params = None
        for _ in range(2):
            if used.sum() < 4:
                break
            rows = np.repeat(used, 2)
            wt = np.sqrt(np.repeat(np.where(used, weights, 0.0), 2))
            target = np.empty(2 * len(dx))
            target[0::2], target[1::2] = dx, dy
            if used.sum() < 8:
                # Too few blocks for six parameters: translation only.
                params = np.zeros(6)
                params[0] = float(np.average(dx[used], weights=weights[used]))
                params[1] = float(np.average(dy[used], weights=weights[used]))
            else:
                solved, *_ = np.linalg.lstsq(
                    self.design[rows] * wt[rows, None], target[rows] * wt[rows], rcond=None
                )
                params = solved
            model = self.design @ params
            err = np.hypot(dx - model[0::2], dy - model[1::2])
            limit = max(3.0 * float(np.median(err[used])), 0.6)
            keep = used & (err <= limit)
            if keep.sum() == used.sum():
                break
            used = keep
        if params is None:
            return None, used, np.zeros(0)
        model = self.design @ params
        err = np.hypot(dx - model[0::2], dy - model[1::2])
        return params, used, err[weights > 0]

    def activity(self, prev: Any, cur: Any, motion: PairMotion) -> float:
        """Share of the picture that still changes once the camera's motion
        is undone: the previous frame is warped by the fitted model and
        compared with the current one, exposure-matched."""
        w = self.w
        dxp = (motion.tx + motion.a11 * self.grid_u + motion.a12 * self.grid_v) * w
        dyp = (motion.ty + motion.a21 * self.grid_u + motion.a22 * self.grid_v) * w
        sx = self.grid_x - dxp
        sy = self.grid_y - dyp
        inside = (sx >= 0) & (sx < self.w - 1) & (sy >= 0) & (sy < self.h - 1)
        if inside.mean() < 0.3:
            return 0.0
        sx = np.clip(sx, 0, self.w - 1.001)
        sy = np.clip(sy, 0, self.h - 1.001)
        x0 = sx.astype(np.int32)
        y0 = sy.astype(np.int32)
        fx = sx - x0
        fy = sy - y0
        top = prev[y0, x0] * (1 - fx) + prev[y0, x0 + 1] * fx
        bottom = prev[y0 + 1, x0] * (1 - fx) + prev[y0 + 1, x0 + 1] * fx
        warped = top * (1 - fy) + bottom * fy
        current = cur[self.grid_y.astype(np.int32), self.grid_x.astype(np.int32)]
        gain = (float(current[inside].mean()) + 1.0) / (float(warped[inside].mean()) + 1.0)
        diff = np.abs(current - warped * gain)
        return float((diff[inside] > 18.0).mean())


def look_of(rgb: Any) -> tuple[float, float, float, float]:
    """(brightness, saturation, warmth, contrast) of one frame, each about
    0-1 (warmth is red minus blue, -1..1)."""
    f = rgb.astype(np.float32) / 255.0
    r, g, b = f[..., 0], f[..., 1], f[..., 2]
    luma = 0.299 * r + 0.587 * g + 0.114 * b
    mx = f.max(axis=-1)
    mn = f.min(axis=-1)
    lit = mx > 0.08
    sat = float(((mx - mn) / (mx + 1e-3))[lit].mean()) if lit.any() else 0.0
    return float(luma.mean()), sat, float((r - b).mean()), float(luma.std())


# --- a whole clip --------------------------------------------------------------


@dataclass
class Analysis:
    """Per-pair motion and periodic look samples for one clip."""

    fps: float
    width: int
    height: int
    frames: int = 0
    t: list[float] = field(default_factory=list)
    pairs: list[PairMotion] = field(default_factory=list)
    act_t: list[float] = field(default_factory=list)
    act: list[float] = field(default_factory=list)
    look_t: list[float] = field(default_factory=list)
    look: list[tuple[float, float, float, float]] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return self.frames / self.fps if self.fps else 0.0

    def arrays(self) -> dict[str, Any]:
        names = ("tx", "ty", "a11", "a12", "a21", "a22", "conf", "res")
        out = {name: np.array([getattr(p, name) for p in self.pairs]) for name in names}
        out["cut"] = np.array([p.cut for p in self.pairs], dtype=bool)
        out["t"] = np.array(self.t)
        return out


@dataclass(frozen=True)
class YuvFrame:
    """A decoded frame as full-resolution planes (yuv444p, studio range):
    the motion step reads luma straight off `y`, and only the few frames
    sampled for their look are turned back into RGB."""

    y: Any
    u: Any
    v: Any

    def rgb(self) -> Any:
        luma = (self.y.astype(np.float32) - 16.0) * (255.0 / 219.0)
        cb = (self.u.astype(np.float32) - 128.0) * (255.0 / 224.0)
        cr = (self.v.astype(np.float32) - 128.0) * (255.0 / 224.0)
        r = luma + 1.5748 * cr
        g = luma - 0.1873 * cb - 0.4681 * cr
        b = luma + 1.8556 * cb
        return np.clip(np.stack([r, g, b], axis=-1), 0, 255).astype(np.uint8)


def _luma(frame: Any) -> Any:
    if isinstance(frame, YuvFrame):
        return frame.y.astype(np.float32)
    return to_gray(frame)


def _rgb(frame: Any) -> Any:
    return frame.rgb() if isinstance(frame, YuvFrame) else frame


def analyse(frames: Iterable[Any], fps: float) -> Analysis:
    """Run the estimator over a stream of frames: RGB arrays (H, W, 3,
    uint8) or YuvFrame planes, as processing/shots.decode_yuv gives them."""
    iterator: Iterator[Any] = iter(frames)
    first = next(iterator, None)
    if first is None:
        return Analysis(fps=fps, width=0, height=0)
    prev = _luma(first)
    height, width = prev.shape[:2]
    result = Analysis(fps=fps, width=width, height=height, frames=1)
    estimator = MotionEstimator(width, height)
    look_step = max(1, int(round(fps * LOOK_EVERY_S)))
    result.look_t.append(0.0)
    result.look.append(look_of(_rgb(first)))
    predict = (0.0, 0.0)
    index = 0
    for frame in iterator:
        index += 1
        cur = _luma(frame)
        motion = estimator.estimate(prev, cur, predict)
        t = index / fps
        result.t.append(t)
        result.pairs.append(motion)
        if motion.cut or motion.conf == 0.0:
            predict = (0.0, 0.0)
        else:
            predict = (motion.tx * width, motion.ty * width)
            if index % ACTIVITY_EVERY == 0:
                result.act_t.append(t)
                result.act.append(estimator.activity(prev, cur, motion))
        if index % look_step == 0:
            result.look_t.append(t)
            result.look.append(look_of(_rgb(frame)))
        prev = cur
    result.frames = index + 1
    return result


# --- the stored track ----------------------------------------------------------


def _bins(t: Any, duration: float) -> Any:
    count = max(1, int(math.ceil(duration * TRACK_HZ)))
    return np.minimum((t * TRACK_HZ).astype(int), count - 1), count


def _binned_sum(index: Any, values: Any, count: int) -> Any:
    return np.bincount(index, weights=values, minlength=count)[:count]


def _binned_mean(index: Any, values: Any, count: int) -> Any:
    total = np.bincount(index, weights=values, minlength=count)[:count]
    n = np.bincount(index, minlength=count)[:count]
    out = np.full(count, np.nan)
    np.divide(total, n, out=out, where=n > 0)
    return out


def track(analysis: Analysis) -> dict[str, Any]:
    """The compact motion track stored as a derived file: 10 samples a
    second of camera velocity (frame widths, or per second for scale and
    roll), confidence, subject activity and look. Columnar so it stays
    small; rebuildable from the proxy like every other derivative."""
    a = analysis.arrays()
    duration = max(analysis.duration_s, 1.0 / TRACK_HZ)
    fps = analysis.fps or 1.0
    out: dict[str, Any] = {
        "version": VERSION,
        "hz": TRACK_HZ,
        "fps": round(analysis.fps, 3),
        "duration_s": round(duration, 3),
        "analysed_width": analysis.width,
        "analysed_height": analysis.height,
    }
    if a["t"].size == 0:
        return out
    index, count = _bins(a["t"], duration)
    scale = (a["a11"] + a["a22"]) / 2
    for name, values in (
        ("vx", a["tx"] * fps),
        ("vy", a["ty"] * fps),
        ("vs", scale * fps),
        ("vr", a["a21"] * fps),
    ):
        out[name] = [round(float(v), 5) for v in _binned_mean(index, values, count)]
    out["conf"] = [round(float(v), 3) for v in _binned_mean(index, a["conf"], count)]
    out["res"] = [round(float(v), 5) for v in _binned_mean(index, a["res"] * fps, count)]
    out["cut"] = [int(i) for i in sorted(set(index[a["cut"]].tolist()))]
    if analysis.act:
        act_index, _ = _bins(np.array(analysis.act_t), duration)
        out["act"] = [
            round(float(v), 4) for v in _binned_mean(act_index, np.array(analysis.act), count)
        ]
    if analysis.look:
        look = np.array(analysis.look)
        out["look_t"] = [round(t, 2) for t in analysis.look_t]
        for column, name in enumerate(("brightness", "saturation", "warmth", "contrast")):
            out[name] = [round(float(v), 4) for v in look[:, column]]
    cleaned: dict[str, Any] = _no_nan(out)
    return cleaned


def _no_nan(value: Any) -> Any:
    if isinstance(value, float):
        return None if math.isnan(value) else value
    if isinstance(value, list):
        return [_no_nan(v) for v in value]
    if isinstance(value, dict):
        return {k: _no_nan(v) for k, v in value.items()}
    return value


# --- windows -------------------------------------------------------------------


@dataclass
class Window:
    start_s: float
    end_s: float
    move: str
    direction: str | None
    speed: float  # camera speed, frame widths per second
    smooth: float  # at 100%
    smooth_by_speed: dict[str, float]
    energy: str
    energy_score: float
    features: dict[str, float]
    look: dict[str, Any]

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


def speed_key(speed: float) -> str:
    """0.5 -> "50": how per-speed numbers are keyed when stored."""
    return str(int(round(speed * 100)))


def _moving_average(values: Any, n: int) -> Any:
    if values.size == 0 or n <= 1:
        return values.copy()
    n = min(n, values.size)
    kernel = np.ones(n)
    total = np.convolve(np.nan_to_num(values), kernel, mode="same")
    weight = np.convolve((~np.isnan(values)).astype(float), kernel, mode="same")
    return np.divide(total, weight, out=np.zeros_like(total), where=weight > 0)


def segment(
    vx: Any,
    vy: Any,
    vs: Any,
    cuts: set[int] | None = None,
    hz: int = TRACK_HZ,
    vp: Any = None,
) -> list[tuple[int, int]]:
    """Split a track into stretches of consistent camera behaviour.

    Works on the 10 Hz velocities smoothed over a second. A window ends when
    the camera starts, stops, or turns by more than 60 degrees from the
    window's own average direction — and the change holds for a second, so
    a gust or a correction does not cut a move in two. Hard cuts always end
    a window. `vp` is the horizontal parallax (how the shift changes top to
    bottom): an orbit around the middle of the frame barely moves the centre
    and is all parallax. Returns [start, end) bin ranges.
    """
    n = len(vx)
    if n == 0:
        return []
    smooth_n = hz  # one second
    parallax = np.zeros(n) if vp is None else np.asarray(vp, dtype=float)
    sx, sy, sz, sp = (
        _moving_average(np.asarray(v, dtype=float), smooth_n) for v in (vx, vy, vs, parallax)
    )
    vec = np.stack([sx, sy, 0.5 * sz, EDGE * sp], axis=1)
    mag = np.linalg.norm(vec, axis=1)
    moving = mag >= STATIC_SPEED
    hold = max(1, int(round(CHANGE_HOLD_S * hz)))
    cuts = cuts or set()

    windows: list[tuple[int, int]] = []
    start = 0
    total = np.zeros(4)
    state_moving = bool(moving[0])

    def differs(j: int, mean_dir: Any, as_moving: bool) -> bool:
        if bool(moving[j]) != as_moving:
            return True
        if not as_moving:
            return False
        norm = float(np.linalg.norm(mean_dir))
        if norm == 0 or mag[j] == 0:
            return False
        return float(vec[j] @ mean_dir) / (norm * float(mag[j])) < TURN_COS

    for i in range(n):
        if i > start:
            # A hard cut always ends the window; any other change has to
            # hold for a second (or to the end of the clip) to count.
            changed = i in cuts or all(
                differs(j, total, state_moving) for j in range(i, min(n, i + hold))
            )
            if changed:
                windows.append((start, i))
                start, total, state_moving = i, np.zeros(4), bool(moving[i])
        total = total + vec[i]
    windows.append((start, n))

    # Fold anything shorter than the minimum into its longer neighbour.
    min_bins = max(1, int(round(MIN_WINDOW_S * hz)))
    merged = True
    while merged and len(windows) > 1:
        merged = False
        for k, (a, b) in enumerate(windows):
            if b - a >= min_bins:
                continue
            if k == 0:
                windows[0:2] = [(a, windows[1][1])]
            elif k == len(windows) - 1:
                windows[k - 1 : k + 1] = [(windows[k - 1][0], b)]
            else:
                left = windows[k - 1][1] - windows[k - 1][0]
                right = windows[k + 1][1] - windows[k + 1][0]
                if left >= right:
                    windows[k - 1 : k + 1] = [(windows[k - 1][0], b)]
                else:
                    windows[k : k + 2] = [(a, windows[k + 1][1])]
            merged = True
            break
    return windows


def camera_speed(features: dict[str, float]) -> float:
    """How fast the picture moves, frame widths per second: the shift at
    the frame's centre or, if more, the average at its top and bottom edges
    (an orbit pivots on the middle), with vertical shift and zoom."""
    tx = features.get("tx", 0.0)
    px = features.get("px", 0.0)
    edges = (abs(tx - px * EDGE) + abs(tx + px * EDGE)) / 2
    horizontal = max(abs(tx), edges)
    zoom = 0.5 * abs(features.get("scale", 0.0))
    return math.sqrt(horizontal**2 + features.get("ty", 0.0) ** 2 + zoom**2)


def classify(features: dict[str, float], top_down: bool = False) -> tuple[str, str | None]:
    """The move and the camera's direction, from a window's mean motion.

    `features` are per-second means: tx, ty (picture shift, frame widths),
    scale (expansion rate), px (how the horizontal shift changes top to
    bottom) and py (vertical stretch beyond the horizontal). Image y grows
    downward, and the picture moves opposite to the camera.

    `top_down` is the framing, which motion alone cannot see: straight down,
    a forward flight is a uniform shift exactly like a pan.
    """
    tx = features.get("tx", 0.0)
    ty = features.get("ty", 0.0)
    scale = features.get("scale", 0.0)
    px = features.get("px", 0.0)
    py = features.get("py", 0.0)
    zoom = 0.5 * abs(scale)
    if camera_speed(features) < STATIC_SPEED:
        return "static", None
    vertical = abs(ty)
    horizontal = max(abs(tx), (abs(tx - px * EDGE) + abs(tx + px * EDGE)) / 2)
    if zoom >= 0.6 * max(horizontal, vertical):
        if top_down:
            return ("descend", "down") if scale > 0 else ("rise", "up")
        return ("push-in", "in") if scale > 0 else ("pull-out", "out")
    if top_down:
        if horizontal >= vertical:
            return "top-down drift", "right" if tx < 0 else "left"
        return "top-down drift", "forward" if ty > 0 else "back"
    if vertical > horizontal:
        # Uniform vertical shift is the camera tilting. Depth makes it
        # uneven — the near ground (bottom) moves faster — and with the
        # scale term says whether the drone is climbing, sinking, or
        # flying toward or away from what it looks at.
        travelling = abs(py) > 0.25 * vertical or abs(scale) > 0.2 * vertical
        if not travelling:
            return "tilt", "up" if ty > 0 else "down"
        if ty > 0:
            return ("push-in", "in") if scale >= 0 else ("rise", "up")
        return ("pull-out", "out") if scale <= 0 else ("descend", "down")
    # Horizontal: compare the shift at the top and the bottom of the frame.
    top = tx - px * EDGE
    bottom = tx + px * EDGE
    camera = "right" if tx < 0 else "left"
    big = max(abs(top), abs(bottom))
    ratio = min(abs(top), abs(bottom)) / big if big else 1.0
    if top * bottom < 0 or ratio < 0.25:
        # Foreground and background part ways around a pivot: an orbit.
        # The camera travels opposite to the foreground.
        lead = bottom if abs(bottom) >= abs(top) else -top
        return "orbit", "right" if lead < 0 else "left"
    if ratio < 0.75:
        return "lateral glide", camera
    return "pan", camera


def _band_rms(signal: Any, fps: float, low: float, high: float) -> float:
    """RMS of the part of `signal` between `low` and `high` Hz."""
    n = signal.size
    if n < 8:
        return 0.0
    taper = np.hanning(n)
    spectrum = np.fft.rfft((signal - signal.mean()) * taper)
    freqs = np.fft.rfftfreq(n, d=1.0 / fps)
    band = (freqs >= low) & (freqs <= high) & (freqs > 0)
    if not band.any():
        return 0.0
    power = (np.abs(spectrum[band]) ** 2).sum() * 2.0 / (n * n)
    return float(math.sqrt(power / float((taper**2).mean())))


def _band_displacement(velocity: Any, fps: float, low: float, high: float) -> float:
    """RMS displacement (frame widths) of the band of a per-second
    velocity signal — how far the picture wobbles, not how fast."""
    n = velocity.size
    if n < 8:
        return 0.0
    taper = np.hanning(n)
    spectrum = np.fft.rfft((velocity - velocity.mean()) * taper)
    freqs = np.fft.rfftfreq(n, d=1.0 / fps)
    band = (freqs >= low) & (freqs <= high) & (freqs > 0)
    if not band.any():
        return 0.0
    displacement = spectrum[band] / (2 * math.pi * freqs[band])
    power = (np.abs(displacement) ** 2).sum() * 2.0 / (n * n)
    return float(math.sqrt(power / float((taper**2).mean())))


def _lowpass(signal: Any, fps: float, cutoff: float) -> Any:
    spectrum = np.fft.rfft(signal)
    freqs = np.fft.rfftfreq(signal.size, d=1.0 / fps)
    spectrum[freqs > cutoff] = 0
    return np.fft.irfft(spectrum, n=signal.size)


def smoothness(vel: dict[str, Any], fps: float, speed: float = 1.0) -> float:
    """How steady a stretch looks when played at `speed` (0.5 = 50%): the
    product of smoothness_parts' scores, 0-1."""
    return smoothness_parts(vel, fps, speed)["smooth"]


def smoothness_parts(vel: dict[str, Any], fps: float, speed: float = 1.0) -> dict[str, float]:
    """How steady a stretch looks when played at `speed` (0.5 = 50%), part
    by part, so a low score can say why.

    `vel` holds per-second velocities sampled at `fps`: x, y (picture
    shift), z (expansion, as the shift it causes at the frame's edge) and r
    (roll, likewise). Three things are scored, each 0-1, and multiplied:

    - **shake**: how far the picture wobbles in the on-screen band the eye
      reads as shake (3-12 Hz), the horizon's rocking included. At 50% that
      band is 6-24 Hz of the source, where wind buffeting lives; at 400% it
      is 0.75-3 Hz, and the same buffeting plays too fast to see.
    - **hesitation**: the camera's speed stuttering by more than a few
      percent of itself, 0.8-4 Hz on screen. Slower changes are speed
      ramps — the pilot easing in, or an editor's ramp — not hesitation.
    - **reversals**: the move backing up on itself.

    Roll counts only as shake. On Brookside's forward flights a slow "roll"
    term measured perspective and yaw, not the horizon — a DJI gimbal holds
    the horizon — and marked every flight unwatchable sped up.
    """
    nyquist = fps / 2.0
    x, y, z, r = (np.asarray(vel[k], dtype=float) for k in ("x", "y", "z", "r"))
    if x.size < 8:
        return {"smooth": 1.0, "shake": 1.0, "hesitation": 1.0, "reversals": 0}
    lo, hi = SHAKE_BAND[0] / speed, min(SHAKE_BAND[1] / speed, nyquist * 0.95)
    shake = math.sqrt(sum(_band_displacement(v, fps, lo, hi) ** 2 for v in (x, y, z, r)))
    s_shake = 1.0 / (1.0 + (shake / SHAKE_SCALE) ** 2)

    lo, hi = HESITATION_BAND[0] / speed, min(HESITATION_BAND[1] / speed, nyquist * 0.95)
    mean = np.array([x.mean(), y.mean(), z.mean()])
    pace = max(float(np.linalg.norm(mean)), 0.03)
    wobble = math.sqrt(sum(_band_rms(v, fps, lo, hi) ** 2 for v in (x, y, z)))
    hesitation = max(0.0, wobble / pace - HESITATION_FREE)
    s_hes = 1.0 / (1.0 + (hesitation / HESITATION_SCALE) ** 2)

    reversals = 0
    norm = float(np.linalg.norm(mean))
    if norm > 0.02:
        heading = mean / norm
        along = _lowpass(
            x * heading[0] + y * heading[1] + z * heading[2], fps, REVERSAL_CUTOFF / speed
        )
        strong = np.abs(along) > 0.3 * norm
        signs = np.sign(along[strong])
        reversals = int((np.diff(signs) != 0).sum()) if signs.size > 1 else 0
    total = max(0.0, min(1.0, s_shake * s_hes * REVERSAL_PENALTY**reversals))
    return {
        "smooth": float(total),
        "shake": round(s_shake, 3),
        "hesitation": round(s_hes, 3),
        "reversals": reversals,
    }


def energy_of(activity: float, residual: float, camera_speed: float) -> tuple[str, float]:
    """Subject energy from what the camera does not explain: the share of
    the picture still changing once the camera's motion is undone
    (`activity`), and the block shift the camera model leaves over
    (`residual`, frame widths per second).

    The residual counts only while the camera is nearly still. In a forward
    drone flight perspective alone leaves 0.05-0.15 frame widths a second
    unexplained (measured on Brookside's 0495, an empty course), which read
    as a busy subject; the warped difference stayed under 1%, as it should."""
    still = min(max(1.0 - camera_speed / 0.05, 0.0), 1.0)
    score = max(activity / 0.03, still * residual / 0.03)
    label = "low" if score < 0.4 else "medium" if score < 1.0 else "high"
    return label, round(min(score / 1.5, 1.0), 3)


def windows(
    analysis: Analysis, extra_cuts_s: Iterable[float] = (), top_down: bool = False
) -> list[Window]:
    """The clip's windows, each described."""
    a = analysis.arrays()
    fps = analysis.fps or 1.0
    if a["t"].size < 2:
        return []
    duration = analysis.duration_s
    index, count = _bins(a["t"], duration)
    scale = (a["a11"] + a["a22"]) / 2
    vx = _binned_mean(index, a["tx"] * fps, count)
    vy = _binned_mean(index, a["ty"] * fps, count)
    vs = _binned_mean(index, scale * fps, count)
    vp = _binned_mean(index, a["a12"] * fps, count)
    cuts = set(index[a["cut"]].tolist())
    for at in extra_cuts_s:
        if 0 < at < duration:
            cuts.add(int(at * TRACK_HZ))
    act_t = np.array(analysis.act_t)
    act = np.array(analysis.act)
    look_t = np.array(analysis.look_t)
    look = np.array(analysis.look) if analysis.look else np.zeros((0, 4))

    out: list[Window] = []
    for first, last in segment(vx, vy, vs, cuts, vp=vp):
        start_s = first / TRACK_HZ
        end_s = min(duration, last / TRACK_HZ)
        inside = (a["t"] >= start_s) & (a["t"] < end_s) & ~a["cut"]
        if inside.sum() < 2:
            continue
        sel = {k: v[inside] for k, v in a.items() if k != "cut"}
        features = {
            "tx": float(sel["tx"].mean() * fps),
            "ty": float(sel["ty"].mean() * fps),
            "scale": float(((sel["a11"] + sel["a22"]) / 2).mean() * fps),
            "px": float(sel["a12"].mean() * fps),
            "py": float((sel["a22"] - sel["a11"]).mean() * fps),
            "roll": float(sel["a21"].mean() * fps),
            "conf": float(sel["conf"].mean()),
        }
        move, direction = classify(features, top_down=top_down)
        speed = camera_speed(features)
        vel = {
            "x": sel["tx"] * fps,
            "y": sel["ty"] * fps,
            "z": 0.5 * (sel["a11"] + sel["a22"]) / 2 * fps,
            "r": 0.5 * sel["a21"] * fps,
        }
        by_speed = {speed_key(s): round(smoothness(vel, fps, s), 3) for s in SPEEDS}
        in_act = (act_t >= start_s) & (act_t < end_s)
        activity = float(np.median(act[in_act])) if in_act.any() else 0.0
        residual = float(np.median(sel["res"]) * fps)
        energy, energy_score = energy_of(activity, residual, speed)
        features["activity"] = activity
        features["residual"] = residual
        in_look = (look_t >= start_s) & (look_t <= end_s)
        if not in_look.any() and look_t.size:
            in_look = (
                np.abs(look_t - (start_s + end_s) / 2)
                == np.abs(look_t - (start_s + end_s) / 2).min()
            )
        window_look: dict[str, Any] = {}
        if in_look.any():
            mean_look = look[in_look].mean(axis=0)
            window_look = describe_look(*(float(v) for v in mean_look))
        out.append(
            Window(
                start_s=round(start_s, 3),
                end_s=round(end_s, 3),
                move=move,
                direction=direction,
                speed=round(speed, 4),
                smooth=by_speed["100"],
                smooth_by_speed=by_speed,
                energy=energy,
                energy_score=energy_score,
                features={k: round(v, 5) for k, v in features.items()},
                look=window_look,
            )
        )
    return out


def describe_look(
    brightness: float, saturation: float, warmth: float, contrast: float
) -> dict[str, Any]:
    """Measured light, from the pictures themselves. Sun position says when
    the sun was low; this says whether it was out. Direct sun gives hard
    contrast and saturated colour; overcast, haze and rain flatten both."""
    sun = 0.6 * min(max((contrast - 0.12) / 0.12, 0.0), 1.0) + 0.4 * min(
        max((saturation - 0.15) / 0.25, 0.0), 1.0
    )
    sky = "direct" if sun >= 0.6 else "overcast" if sun < 0.35 else "mixed"
    return {
        "brightness": round(brightness, 3),
        "saturation": round(saturation, 3),
        "warmth": round(warmth, 3),
        "contrast": round(contrast, 3),
        "sun": round(sun, 3),
        "sky": sky,
    }


def speed_fit(
    duration_s: float, source_fps: float | None, sequence_fps: float
) -> dict[str, dict[str, Any]]:
    """What a window can do at each speed: how long it runs on the timeline,
    and whether slowing it down shows real frames. A 29.97 source at 50% in
    a 29.97 sequence has none to spare — every frame is shown twice, or
    invented by interpolation; a 59.94 source at 50% fills the sequence."""
    out: dict[str, dict[str, Any]] = {}
    for speed in SPEEDS:
        entry: dict[str, Any] = {"length_s": round(duration_s / speed, 2)}
        if speed < 1.0 and source_fps:
            entry["real_frames"] = source_fps * speed >= sequence_fps * 0.97
        out[speed_key(speed)] = entry
    return out
