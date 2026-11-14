"""About how long a review should take, from what was measured.

An estimate, and every sentence here says so. It is the seconds this model
took to answer, as recorded on its own looks (or on its one Test), times
what is left to look at. It leaves out loading the model, making playable
copies and finding shots, and the review dialog says that it does.

It says nothing about how good the answers are. Seconds are the only thing
measured here; a fast model is not a better one.
"""

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.db.models import LocalAiJob, LocalModel, Look
from framefound.looking.scope import Counts

# The newest this many successful looks of each kind are what "measured" means:
# enough that one slow load does not move the figure, few enough that a
# runtime update shows within an evening.
SAMPLE = 50
# Three pictures and a longer answer: what a shot costs next to a photograph,
# for a kind with no history of its own yet. A guess, used only until the
# first shots are timed.
SHOT_COST = 1.5
# L1's Test job (local_models/handlers.py registers it under this name). Its
# answer is timed like any other, on one picture.
TEST_KIND = "test"
# Tests looked through for one that was shown a picture.
TESTS_LOOKED_AT = 5

Basis = Literal["measured", "test", "none"]

NO_ESTIMATE = (
    "No estimate yet: nothing has been looked at with this model. The first few will tell."
)
LEFT_OUT = "Loading the model and making playable copies are extra."
NOTHING_WAITING = "Nothing here is waiting to be looked at."
# Clips with no playable copy yet (or no shots found, or their drive away)
# are something a review will do, but how many shots they hold is not known
# until they are made ready, so they cannot be timed.
READY_FIRST = (
    "The clips here have to be made ready first; how long looking at them takes "
    "is known once their shots are found."
)
NOT_COUNTED = "Looking at the clips still to be made ready is not counted."


@dataclass(frozen=True)
class Speed:
    """Seconds per item, as this model has been seen to take them."""

    per_photo_s: float | None
    per_shot_s: float | None
    # How many timed answers the figures come from (1 for a Test).
    sample: int
    basis: Basis


NONE = Speed(None, None, 0, "none")


async def speed(db: AsyncSession, model: LocalModel) -> Speed:
    """The median seconds of this model's newest successful looks, photographs
    (and anything else judged from one picture) and shots apart.

    A kind with no history borrows the other's (`SHOT_COST`). With no looks
    at all, the model's newest finished Test that was shown a picture
    (`test`, one sample). Otherwise `none`.

    Looks are matched on the repository and the build's variant, as stored on
    the look itself (no foreign key): a 4-bit and an 8-bit build of one
    repository are not the same speed, and two revisions of one build are
    close enough to share a figure.
    """
    photos = await _timed(db, model, whole=True)
    shots = await _timed(db, model, whole=False)
    if photos or shots:
        per_photo = statistics.median(photos) if photos else None
        per_shot = statistics.median(shots) if shots else None
        if per_photo is None and per_shot is not None:
            per_photo = per_shot / SHOT_COST
        if per_shot is None and per_photo is not None:
            per_shot = per_photo * SHOT_COST
        return Speed(per_photo, per_shot, len(photos) + len(shots), "measured")
    tested = await _tested(db, model)
    if tested is not None:
        return Speed(tested, tested * SHOT_COST, 1, "test")
    return NONE


def seconds_for(counts: Counts, speed: Speed) -> int | None:
    """What is left to look at in `counts`, at `speed`; None without a speed.

    Photographs and clips judged from one still at the photograph's rate,
    shots at the shot's. Nothing is added for loading the model or for
    making clips ready: the clips that still need a copy are not counted at
    all, since how many shots they hold is not known until they are made.

    0 only when nothing is counted: a sub-second total (one photograph at a
    demonstration model's 0.4 s) is 1, so its words do not say there is
    nothing to do."""
    if speed.per_photo_s is None or speed.per_shot_s is None:
        return None
    total = (counts.photos + counts.stills_only) * speed.per_photo_s
    total += counts.shots * speed.per_shot_s
    return max(1, round(total)) if total > 0 else 0


def to_make_ready(counts: Counts) -> int:
    """The clips a review would make ready (or judge from one still when it
    cannot) before looking at them: done by a review, not timed here."""
    return counts.need_copy + counts.need_shots + counts.copy_away


def seconds_left(items: int, speed: Speed) -> int | None:
    """A running review's time to go: `items` (photographs and shots, not told
    apart once a review is under way) at the mean of the two rates. Rougher
    than `seconds_for`, and None without a speed."""
    if speed.per_photo_s is None or speed.per_shot_s is None:
        return None
    return round(max(items, 0) * (speed.per_photo_s + speed.per_shot_s) / 2)


def duration_text(seconds: float) -> str:
    """A length of time as the pages write it: `under a minute`, `25 min`, `1 h 40 min`, `3 h`."""
    if seconds < 60:
        return "under a minute"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h" if rest == 0 else f"{hours} h {rest} min"


def rate_text(seconds: float) -> str:
    """`9 s`, `2.5 s`: whole seconds from ten up, one decimal below."""
    return f"{seconds:.0f} s" if seconds >= 10 else f"{seconds:.1f}".removesuffix(".0") + " s"


def words(seconds: int | None, speed: Speed, *, unready: int = 0) -> str:
    """The dialog's sentence for an estimate, saying what it rests on and what
    it leaves out. Never a promise: "about" for what was measured, "roughly"
    for the one test.

    `unready` is `to_make_ready(counts)`: clips the review will make ready
    and then look at, which `seconds` does not count. With nothing counted
    and such clips waiting, the words say they come first rather than that
    nothing is to do."""
    if seconds is None or speed.per_photo_s is None or speed.per_shot_s is None:
        return NO_ESTIMATE
    if seconds == 0:
        return READY_FIRST if unready else NOTHING_WAITING
    left_out = f"{LEFT_OUT} {NOT_COUNTED}" if unready else LEFT_OUT
    if speed.basis == "test":
        lead = "Under a minute" if seconds < 60 else f"Roughly {duration_text(seconds)}"
        return f"{lead}, from the one test this model has run. {left_out}"
    lead = "Under a minute" if seconds < 60 else f"About {duration_text(seconds)}"
    sample = (
        "from the one answer timed so far"
        if speed.sample == 1
        else f"from its last {speed.sample} answers"
    )
    return (
        f"{lead} at this model's measured speed ({rate_text(speed.per_photo_s)} a photograph, "
        f"{rate_text(speed.per_shot_s)} a shot, {sample}). {left_out}"
    )


def as_dict(seconds: int | None, speed: Speed, *, unready: int = 0) -> dict[str, Any]:
    """The `estimate` object of the review dialog (section 6.6), with its
    words; `unready` as for `words`."""
    return {
        "seconds": seconds,
        "basis": speed.basis,
        "per_photo_s": _rounded(speed.per_photo_s),
        "per_shot_s": _rounded(speed.per_shot_s),
        "sample": speed.sample,
        "words": words(seconds, speed, unready=unready),
    }


async def _timed(db: AsyncSession, model: LocalModel, *, whole: bool) -> list[float]:
    span = Look.start_ms.is_(None) if whole else Look.start_ms.is_not(None)
    found: Sequence[Any] = (
        await db.scalars(
            select(Look.seconds)
            .where(
                Look.model == model.repo_id,
                Look.variant == model.variant,
                Look.ok.is_(True),
                Look.seconds.is_not(None),
                span,
            )
            .order_by(Look.created_at.desc(), Look.id.desc())
            .limit(SAMPLE)
        )
    ).all()
    return [float(value) for value in found if _usable(value)]


async def _tested(db: AsyncSession, model: LocalModel) -> float | None:
    tests: Sequence[LocalAiJob] = (
        await db.scalars(
            select(LocalAiJob)
            .where(
                LocalAiJob.kind == TEST_KIND,
                LocalAiJob.model_id == model.id,
                LocalAiJob.state == "done",
                LocalAiJob.seconds.is_not(None),
            )
            .order_by(LocalAiJob.finished_at.desc(), LocalAiJob.created_at.desc())
            .limit(TESTS_LOOKED_AT)
        )
    ).all()
    for job in tests:
        # A text test of a model that reads pictures is far quicker than a
        # look and would promise too much.
        kind = job.result.get("kind") if isinstance(job.result, dict) else None
        if kind != "text" and _usable(job.seconds):
            return float(job.seconds or 0.0)
    return None


def _usable(value: object) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 1)
