"""Measuring an edit against the edit that shipped.

The question a bake-off asks is "how close is FrameFound's auto-edit to what
went to MLS?", and three things make it answerable rather than a matter of
taste:

- **Distance** is colour difference (CIE ΔE, in Lab) between the two edits
  on a coarse grid — region by region, not pixel by pixel. A coarse grid is
  what makes it fair: the reference may be straightened or keystoned a few
  pixels differently, and what the eye compares between two edits is the
  brightness and colour of the walls, the windows and the sky, not the
  alignment of the skirting board. The mean L*, a*, b* and chroma
  differences say *which way* an edit is off (darker, cooler, flatter).
- **Matching** pairs originals with finals by name when the names survived,
  and by picture content when the organizer renamed them. The content
  descriptor is made tone-blind (rank-equalised, then normalised), because
  the two sides of a pair differ in exactly the way an edit changes tone.
- **The engine's ceiling**: fitting the sliders directly to the reference
  shows how close the engine *can* get with perfect judgment. That splits
  every shortfall into "the model chose badly" (a better model or prompt
  fixes it) and "the engine cannot do this" (an engine feature, or a
  different kind of tool, fixes it).

Pure functions over PIL images and numpy; the bake-off command and tests
drive them.
"""

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

# Grid the distance is measured on (landscape; transposed for portrait).
GRID = (48, 36)
# Size the ceiling fit renders at: every adjustment is scale-free, so a
# 256px render predicts the full-size one, at a sixteenth of the pixels.
FIT_EDGE = 256
# Below this, a content match is a guess — the pair is left out.
MIN_MATCH = 0.55

_WHITE = (0.95047, 1.0, 1.08883)
_RGB_TO_XYZ = (
    (0.4124, 0.3576, 0.1805),
    (0.2126, 0.7152, 0.0722),
    (0.0193, 0.1192, 0.9505),
)


def to_lab(rgb: Any) -> Any:
    """sRGB (0..1 floats, last axis RGB) to CIE Lab under D65."""
    import numpy as np

    rgb = np.clip(np.asarray(rgb, dtype=np.float64), 0.0, 1.0)
    linear = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    xyz = linear @ np.asarray(_RGB_TO_XYZ).T / np.asarray(_WHITE)
    delta = 6.0 / 29.0
    f = np.where(xyz > delta**3, np.cbrt(xyz), xyz / (3 * delta**2) + 4.0 / 29.0)
    lightness = 116.0 * f[..., 1] - 16.0
    a = 500.0 * (f[..., 0] - f[..., 1])
    b = 200.0 * (f[..., 1] - f[..., 2])
    return np.stack([lightness, a, b], axis=-1)


def grid_lab(image: Any, portrait: bool | None = None) -> Any:
    """The image averaged down to the measuring grid, in Lab."""
    import numpy as np
    from PIL import Image

    if portrait is None:
        portrait = image.height > image.width
    size = (GRID[1], GRID[0]) if portrait else GRID
    small = image.convert("RGB").resize(size, Image.Resampling.BOX)
    return to_lab(np.asarray(small, dtype=np.float64) / 255.0)


@dataclass
class Distance:
    delta_e: float  # mean ΔE76 over the grid: overall "how different"
    d_lightness: float  # + = brighter than the reference (L* units)
    d_a: float  # + = more magenta, - = greener
    d_b: float  # + = warmer/yellower, - = bluer
    d_chroma: float  # + = more saturated
    contrast: float  # spread of L* relative to the reference's; 1 = same

    def as_dict(self) -> dict[str, float]:
        return {key: round(value, 3) for key, value in asdict(self).items()}


def distance_lab(candidate: Any, reference: Any) -> Distance:
    """Distance between two grids already in Lab (same shape)."""
    import numpy as np

    diff = candidate - reference
    chroma_c = np.hypot(candidate[..., 1], candidate[..., 2])
    chroma_r = np.hypot(reference[..., 1], reference[..., 2])
    spread_r = float(np.std(reference[..., 0]))
    return Distance(
        delta_e=float(np.mean(np.sqrt((diff**2).sum(axis=-1)))),
        d_lightness=float(np.mean(diff[..., 0])),
        d_a=float(np.mean(diff[..., 1])),
        d_b=float(np.mean(diff[..., 2])),
        d_chroma=float(np.mean(chroma_c - chroma_r)),
        contrast=float(np.std(candidate[..., 0])) / spread_r if spread_r > 1e-6 else 1.0,
    )


def distance(candidate: Any, reference: Any) -> Distance:
    """How far `candidate` is from `reference`, both PIL images. Measured on
    the candidate's orientation, so a reference of a slightly different
    crop still lines up region for region."""
    portrait = candidate.height > candidate.width
    return distance_lab(grid_lab(candidate, portrait), grid_lab(reference, portrait))


# --------------------------------------------------------------- framing


def _structure(grey: Any) -> Any:
    """Tone-blind structure of a small grey array: ranks plus their gradient,
    normalised, so a dot product is a correlation that an edit's tone curve
    cannot move."""
    import numpy as np

    flat = grey.astype(np.float64).ravel()
    ranks = np.argsort(np.argsort(flat)).astype(np.float64).reshape(grey.shape)
    gy, gx = np.gradient(ranks)
    parts = []
    for part in (ranks, np.hypot(gx, gy)):
        vector = part.ravel() - part.mean()
        norm = float(np.linalg.norm(vector))
        parts.append(vector / norm if norm > 1e-9 else vector)
    return np.concatenate(parts) / np.sqrt(2.0)


@dataclass
class Framing:
    box: tuple[float, float, float, float]  # of the original, as fractions
    scale: float  # 1.0 = same framing; 1.15 = the final is a 15% tighter crop
    score: float  # structural correlation at that framing

    def crop(self, image: Any) -> Any:
        left, top, right, bottom = self.box
        w, h = image.size
        return image.crop((round(left * w), round(top * h), round(right * w), round(bottom * h)))


def align(base: Any, reference: Any) -> Framing:
    """Where the final's frame sits inside the original's.

    Finals are often re-framed as well as re-toned — lens correction,
    straightened verticals, a tighter crop — and a colour distance measured
    across two different framings charges the misregistration to the colour.
    This finds the crop (scale and offset, at the final's aspect) of the
    original whose structure best matches the final, coarse to fine, so the
    comparison is made region for region. A match too weak to trust returns
    the whole frame.
    """
    import numpy as np
    from PIL import Image

    grey = base.convert("L")
    grey.thumbnail((192, 192), Image.Resampling.BOX)
    width, height = grey.size
    aspect = reference.width / reference.height
    target = (40, max(8, round(40 / aspect))) if aspect >= 1 else (max(8, round(40 * aspect)), 40)
    wanted = _structure(
        np.asarray(reference.convert("L").resize(target, Image.Resampling.BOX), dtype=np.float64)
    )
    if width / height > aspect:
        full_w, full_h = height * aspect, float(height)
    else:
        full_w, full_h = float(width), width / aspect

    def score(x: float, y: float, s: float) -> float:
        cw, ch = full_w / s, full_h / s
        x = min(max(x, 0.0), width - cw)
        y = min(max(y, 0.0), height - ch)
        crop = grey.resize(target, Image.Resampling.BOX, box=(x, y, x + cw, y + ch))
        return float(_structure(np.asarray(crop, dtype=np.float64)) @ wanted)

    best = (-2.0, 0.0, 0.0, 1.0)
    for s in np.arange(1.0, 1.37, 0.04):
        cw, ch = full_w / s, full_h / s
        for x in np.linspace(0.0, width - cw, 9):
            for y in np.linspace(0.0, height - ch, 9):
                value = score(float(x), float(y), float(s))
                if value > best[0]:
                    best = (value, float(x), float(y), float(s))
    # Refine around the coarse answer.
    _value, bx, by, bs = best
    step_x, step_y = width * 0.03, height * 0.03
    for s in np.arange(bs - 0.03, bs + 0.031, 0.01):
        if s < 1.0:
            continue
        for x in np.linspace(bx - step_x, bx + step_x, 7):
            for y in np.linspace(by - step_y, by + step_y, 7):
                value = score(float(x), float(y), float(s))
                if value > best[0]:
                    best = (value, float(x), float(y), float(s))

    value, x, y, s = best
    if value < MIN_MATCH:
        return Framing((0.0, 0.0, 1.0, 1.0), 1.0, round(value, 3))
    cw, ch = full_w / s, full_h / s
    x = min(max(x, 0.0), width - cw)
    y = min(max(y, 0.0), height - ch)
    return Framing(
        (x / width, y / height, (x + cw) / width, (y + ch) / height), round(s, 3), round(value, 3)
    )


# ---------------------------------------------------------------- matching

_NUMBERED = re.compile(r"^\d{1,3}_(?=[a-z])")


def name_key(filename: str) -> str:
    """A file name reduced to what survives an edit round-trip: no folder,
    no extension, no case, and no "001_" gallery prefix ("001_IMG_0802.jpg"
    was IMG_0802.JPG). SEO names pass through whole — both sides share them
    when the organizer renamed before the batch went out."""
    stem = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    return _NUMBERED.sub("", stem)


def descriptor(image: Any) -> Any:
    """What a photograph looks like with its tone taken out: rank-equalised
    grey at 32x32, plus its gradient, each normalised. Two edits of one
    frame agree; two frames of one kitchen mostly do not."""
    import numpy as np
    from PIL import Image

    grey = np.asarray(
        image.convert("L").resize((32, 32), Image.Resampling.BOX), dtype=np.float64
    ).ravel()
    ranks = np.argsort(np.argsort(grey)).astype(np.float64).reshape(32, 32)
    gy, gx = np.gradient(ranks)
    parts = []
    for part in (ranks, np.hypot(gx, gy)):
        flat = part.ravel()
        flat = flat - flat.mean()
        norm = float(np.linalg.norm(flat))
        parts.append(flat / norm if norm > 1e-9 else flat)
    return np.concatenate(parts) / np.sqrt(2.0)


@dataclass
class Pair:
    original: str
    reference: str
    method: str  # "name" | "content"
    score: float


def match(
    originals: dict[str, Any],
    references: dict[str, Any],
    orientation: dict[str, bool] | None = None,
) -> list[Pair]:
    """Pair each reference with the original it was made from.

    `originals` and `references` map a file name to its descriptor. Names
    are tried first; what is left is matched by content, best score first,
    each original used once. `orientation` (name -> portrait?) keeps a
    vertical from matching a horizontal of the same wall.
    """
    import numpy as np

    pairs: list[Pair] = []
    by_key = {name_key(name): name for name in originals}
    used: set[str] = set()
    unmatched: list[str] = []
    for ref in sorted(references):
        original = by_key.get(name_key(ref))
        if original is not None and original not in used:
            used.add(original)
            pairs.append(Pair(original, ref, "name", 1.0))
        else:
            unmatched.append(ref)

    left = [name for name in sorted(originals) if name not in used]
    if unmatched and left:
        refs = np.stack([references[name] for name in unmatched])
        origs = np.stack([originals[name] for name in left])
        scores = refs @ origs.T
        if orientation:
            for i, ref in enumerate(unmatched):
                for j, original in enumerate(left):
                    if orientation.get(ref) != orientation.get(original):
                        scores[i, j] -= 1.0
        order = np.dstack(np.unravel_index(np.argsort(-scores, axis=None), scores.shape))[0]
        taken_refs: set[int] = set()
        taken_origs: set[int] = set()
        for i, j in order:
            score = float(scores[i, j])
            if score < MIN_MATCH:
                break
            if i in taken_refs or j in taken_origs:
                continue
            taken_refs.add(int(i))
            taken_origs.add(int(j))
            pairs.append(Pair(left[j], unmatched[i], "content", round(score, 3)))
    pairs.sort(key=lambda pair: pair.reference)
    return pairs


# ------------------------------------------------------- learning a look

FEATURE_NAMES = (
    "l_p2", "l_p10", "l_p25", "l_p50", "l_p75", "l_p90", "l_p98",
    "a_mean", "b_mean", "chroma_mean", "clipped", "crushed",
    "top_l", "bottom_l", "blue_sky", "green", "portrait",
)  # fmt: skip


def features(image: Any) -> Any:
    """A photograph as the numbers an editor's first glance takes in: how
    its brightness is spread, its cast, how much is blown or crushed, sky
    above and grass below. What "photographs like this one" means when
    looking up how similar photographs were edited."""
    import numpy as np
    from PIL import Image

    small = image.convert("RGB")
    small.thumbnail((128, 128), Image.Resampling.BOX)
    lab = to_lab(np.asarray(small, dtype=np.float64) / 255.0)
    lightness, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    third = max(1, lab.shape[0] // 3)
    top = lab[:third]
    return np.asarray(
        [
            *np.percentile(lightness, (2, 10, 25, 50, 75, 90, 98)),
            float(a.mean()),
            float(b.mean()),
            float(np.hypot(a, b).mean()),
            float((lightness > 95).mean()) * 100,
            float((lightness < 12).mean()) * 100,
            float(top[..., 0].mean()),
            float(lab[-third:, ..., 0].mean()),
            float(((top[..., 2] < -12) & (top[..., 0] > 45)).mean()) * 100,
            float(((a < -12) & (lightness > 20)).mean()) * 100,
            100.0 if image.height > image.width else 0.0,
        ]
    )


@dataclass
class LearnedLook:
    """Slider recipes fitted to shipped edits, indexed by what the original
    looked like. Predicting is a weighted average of the nearest examples'
    recipes — k-nearest-neighbours on standardised features, no training
    step, and every prediction explainable by the photographs it came from."""

    feats: Any  # (n, f)
    recipes: list[dict[str, float]]
    mean: Any
    scale: Any
    k: int = 7

    @classmethod
    def fit(cls, feats: list[Any], recipes: list[dict[str, float]], k: int = 7) -> "LearnedLook":
        import numpy as np

        matrix = np.stack(feats)
        mean = matrix.mean(axis=0)
        scale = matrix.std(axis=0)
        scale[scale < 1e-6] = 1.0
        return cls((matrix - mean) / scale, list(recipes), mean, scale, k)

    @classmethod
    def from_json(cls, payload: dict[str, Any], k: int = 7) -> "LearnedLook":
        """The look as `bakeoff --learn` saved it: raw features, the
        standardisation, and each example's fitted recipe."""
        import numpy as np

        if list(payload.get("features", [])) != list(FEATURE_NAMES):
            raise ValueError("saved look was made with a different feature set")
        mean = np.asarray(payload["mean"], dtype=np.float64)
        scale = np.asarray(payload["scale"], dtype=np.float64)
        raw = np.asarray([e["features"] for e in payload["examples"]], dtype=np.float64)
        recipes = [dict(e["recipe"]) for e in payload["examples"]]
        return cls((raw - mean) / scale, recipes, mean, scale, k)

    def predict(self, feat: Any) -> dict[str, float]:
        import numpy as np

        query = (np.asarray(feat) - self.mean) / self.scale
        dist = np.sqrt(((self.feats - query) ** 2).sum(axis=1))
        nearest = np.argsort(dist)[: self.k]
        weights = 1.0 / (dist[nearest] + 0.5)
        weights /= weights.sum()
        keys = sorted({key for i in nearest for key in self.recipes[i]})
        chosen = list(zip(nearest, weights, strict=True))
        return {
            key: round(float(sum(w * self.recipes[i].get(key, 0.0) for i, w in chosen)), 3)
            for key in keys
        }


# ----------------------------------------------------------- the ceiling

# The sliders the fit moves, with its opening step. Geometry is left out:
# it does not change colour, and the grid is coarse enough not to mind.
FIT_STEPS: tuple[tuple[str, float], ...] = (
    ("exposure", 0.4),
    ("auto_wb", 0.25),
    ("temperature", 0.1),
    ("tint", 0.1),
    ("contrast", 0.1),
    ("shadows", 0.2),
    ("highlights", 0.2),
    ("window_pull", 0.2),
    ("local_contrast", 0.2),
    ("vibrance", 0.2),
    ("saturation", 0.1),
)


def fit_recipe(
    base: Any,
    reference_lab: Any,
    start: dict[str, Any],
    render: Callable[[Any, dict[str, Any]], Any],
    bounds: dict[str, tuple[float, float]],
    max_rounds: int = 12,
) -> tuple[dict[str, Any], float]:
    """The recipe that brings `base` closest to the reference, by coordinate
    descent from `start`: try each slider a step up and down, keep what
    helps, halve the steps when nothing does. Deterministic, and a few
    hundred small renders per photograph.

    `reference_lab` is the reference already on the grid, in `base`'s
    orientation. Returns the recipe and its ΔE.
    """
    portrait = base.height > base.width

    def loss(recipe: dict[str, Any]) -> float:
        return distance_lab(grid_lab(render(base, recipe), portrait), reference_lab).delta_e

    current = {key: float(value) for key, value in start.items() if key in bounds}
    best = loss(current)
    scale = 1.0
    for _round in range(max_rounds):
        improved = False
        for key, step in FIT_STEPS:
            low, high = bounds[key]
            for sign in (1.0, -1.0):
                value = min(high, max(low, current.get(key, 0.0) + sign * step * scale))
                if value == current.get(key, 0.0):
                    continue
                trial = {**current, key: value}
                score = loss(trial)
                if score < best - 1e-3:
                    current, best = trial, score
                    improved = True
                    break
        if not improved:
            scale /= 2.0
            if scale < 0.12:
                break
    return {key: round(value, 3) for key, value in current.items() if value}, best
