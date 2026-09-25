"""The operator's look, learned from photographs that already shipped.

The Fotello bake-off (Sep 2026, docs/ai-editing-models.md) fitted the
develop sliders to every final that went to MLS — Fotello's and the
operator's own — and found that "edit this photograph the way the most
similar shipped photographs were edited" came closer to what shipped than
any Claude model's judgment: mean ΔE 8.4 against Opus 5.5's 10.1 and the
preset's 11.4, scored on shoots the look had never seen. It is local, free
and deterministic.

So auto-edit takes its *tone* from the learned look when one is installed,
and keeps from the model only what the look cannot know: straightening
(rotate, keystone), the caption and file name, and whether the sky is
worth replacing. Blending the model's tone back in measured worse (8.6).

The look is installed by `python -m framefound.ops.bakeoff --learn …`,
which writes data/looks/learned.json; re-running it after more shoots
ship makes it better.
"""

from pathlib import Path
from typing import Any

import structlog

from framefound.media import compare
from framefound.media import develop as develop_lib

log = structlog.get_logger()

LOOK_PATH = Path("looks") / "learned.json"
# What the model still decides when the look sets the tone.
GEOMETRY_KEYS = ("rotate", "keystone")
TONE_KEYS = tuple(k for k in develop_lib.RECIPE_FIELDS if k not in GEOMETRY_KEYS)

_cache: dict[Path, tuple[float, compare.LearnedLook]] = {}


def load(data_dir: Path) -> compare.LearnedLook | None:
    """The installed look, or None. Cached by file modification time, so a
    re-learn takes effect on the next run without a restart."""
    import json

    path = data_dir / LOOK_PATH
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    cached = _cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        look = compare.LearnedLook.from_json(json.loads(path.read_text()))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("looks.unreadable", path=str(path), error=str(exc)[:200])
        return None
    _cache[path] = (mtime, look)
    return look


def examples(data_dir: Path) -> int:
    look = load(data_dir)
    return len(look.recipes) if look is not None else 0


def predict(look: compare.LearnedLook, image: Any) -> dict[str, float]:
    """Tone sliders for `image` (any size; it is measured small)."""
    predicted = look.predict(compare.features(image))
    return {k: v for k, v in predicted.items() if k in TONE_KEYS and v}


def combine(learned: dict[str, float], judged: dict[str, Any] | None) -> dict[str, Any]:
    """The learned tone, with the model's straightening (and a chosen sky,
    if the recipe already carries one) kept."""
    recipe: dict[str, Any] = dict(learned)
    for key in (*GEOMETRY_KEYS, "sky"):
        if judged and key in judged:
            recipe[key] = judged[key]
    return recipe
