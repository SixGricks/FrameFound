"""What a local model is asked about a listing's photograph, and what its
answer becomes (design L4 3.4).

The local mode of auto-edit splits the work the Claude mode does in one
call. The model looks at each photograph and proposes what Claude decides
today and nothing more: straightening (rotate, verticals), whether the sky
is worth replacing, a caption and a file name. It is never asked about
brightness or colour: the learned look sets the tone, as it does in the
other modes, because it beat every model's judgment when measured
(`media/looks.py`).

A proposal is stored and shown; nothing changes until the operator ticks
it and presses Apply (`ai_edit_listing` mode `local`). This module is the
pure part: the question, the form of the answer, how an answer is cleaned
(`clean_judgement`), and the recipe Apply would write for it
(`proposed_recipe`). Nothing here asks a model or touches a database.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from framefound.ai import recipe_picker
from framefound.media import develop, looks

# Goes into the record of every proposal. Bump it on ANY change to
# INSTRUCTIONS, PROMPT or PhotoJudgement: a stored proposal says which
# question it answered.
PROMPT_VERSION = "listing-look-1"
# Smaller than these is not worth the edge pixels a correction costs.
ROTATE_DEAD, KEYSTONE_DEAD = 0.2, 0.05
# At or above these a correction is "large — look first": the row starts
# unticked, so a model that misread the photograph cannot tilt it by itself.
ROTATE_LARGE, KEYSTONE_LARGE = 3.0, 0.6
# What is kept of the model's note on its proposal.
NOTE_LENGTH = 200


class PhotoJudgement(BaseModel):
    """What a model is asked to answer with. The bounds are the engine's own
    for the two corrections. The strings' limits are generous, well past
    what the cleaners keep, and they are sent with the question (a runtime
    that follows the schema never writes past them). An answer that does go
    past one is not the form asked for: it is refused like any other, asked
    once more, and if it is still too long that photograph gets no proposal
    and is counted as failed."""

    model_config = ConfigDict(extra="forbid")

    rotate: float = Field(ge=-5.0, le=5.0)
    keystone: float = Field(ge=-1.0, le=1.0)
    sky_worth_replacing: bool
    caption: str = Field(max_length=300)
    file_name: str = Field(max_length=120)
    note: str = Field(max_length=300)


# The words Claude is given today for the same fields (recipe_picker's
# RECIPE_TOOL and NAMING_PROPERTIES), as prose. Every minus is the plain
# keyboard one: the answer is JSON, and a model shown a typographic minus
# may write it back.
INSTRUCTIONS = (
    "You prepare real-estate and farm-auction photographs for a listing. Do "
    "not judge brightness or colour; that is done elsewhere. rotate: degrees "
    "from -5 to 5 to level a visibly tilted horizon or counter; 0 unless it "
    "is clearly tilted. keystone: -1 to 1; positive corrects verticals that "
    "lean together toward the top; be conservative, 0 unless walls clearly "
    "lean. sky_worth_replacing: true only for an exterior whose sky is blown "
    "white or drab grey. caption: one plain line saying what the photograph "
    "shows, no marketing words, no address. file_name: two to six lowercase "
    "words joined by hyphens naming what it shows, no address, no numbers. "
    "note: one short sentence on what you would change. Answer with one JSON "
    "object and nothing else."
)
PROMPT = "Look at this photograph and answer."


@dataclass(frozen=True)
class Judged:
    """A model's answer about one photograph, cleaned: what is stored."""

    rotate: float
    keystone: float
    sky: bool
    caption: str
    slug: str
    note: str

    @property
    def large(self) -> bool:
        """A correction big enough that a person should look before it lands."""
        return is_large(self.rotate, self.keystone)


def is_large(rotate: float, keystone: float) -> bool:
    """Whether a straightening is "large — look first" (for a stored proposal)."""
    return abs(rotate) >= ROTATE_LARGE or abs(keystone) >= KEYSTONE_LARGE


def _held(value: float, key: str, dead: float, places: int) -> float:
    """One correction through the engine's own bounds, then the dead band."""
    if not math.isfinite(value):
        return 0.0
    cleaned = develop.clean_recipe({key: value}).get(key, 0.0)
    number = round(float(cleaned), places)
    # "+ 0.0" so that a tiny negative is never stored as -0.0.
    return 0.0 if abs(number) < dead else number + 0.0


def clean_judgement(answer: PhotoJudgement) -> Judged:
    """A model's answer as what is stored: the corrections held to the
    engine's bounds and dropped inside their dead bands, the caption and the
    name cleaned the way Claude's are (`recipe_picker`), the note cut short.
    A file name that cleans to nothing leaves the slug empty: the photograph
    is then simply not renamed."""
    return Judged(
        rotate=_held(answer.rotate, "rotate", ROTATE_DEAD, 2),
        keystone=_held(answer.keystone, "keystone", KEYSTONE_DEAD, 3),
        sky=bool(answer.sky_worth_replacing),
        caption=recipe_picker.clean_caption(answer.caption),
        slug=recipe_picker.clean_slug(answer.file_name),
        note=" ".join(str(answer.note).split())[:NOTE_LENGTH],
    )


def proposed_recipe(
    tone: Mapping[str, float] | None,
    rotate: float,
    keystone: float,
    *,
    straighten: bool = True,
) -> dict[str, Any]:
    """The recipe Apply writes for a proposal, less the sky.

    `tone` is the learned look's prediction for the photograph
    (`looks.predict`), or None when no look is installed: the listing preset
    then sets the tone, exactly as the preset mode does. The model's
    straightening is kept unless the operator turned it off. NOT cleaned,
    and no sky: the caller adds the sky (only where one was picked) and
    cleans, as `ai_edit_listing` does for the other modes."""
    geometry: dict[str, Any] = {}
    if straighten:
        if rotate:
            geometry["rotate"] = rotate
        if keystone:
            geometry["keystone"] = keystone
    if tone is None:
        return {**develop.LISTING_PRESET, **geometry}
    return looks.combine(dict(tone), geometry)
