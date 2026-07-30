"""Theming a slideshow: choosing photos that fit, and styling them to match.

Two halves, both deterministic and both usable today.

**Selection.** CLIP already puts words and images in one space, which is what
made zero-shot tagging work. The same trick answers "which of these 400 VBS
photos feel like Rainforest Falls": encode the theme's words, score every
candidate frame, and prefer the ones that land close. No model invents
anything — it is ranking photographs the operator already took.

**Styling.** A theme also carries a look: a colour grade, a transition, a pace,
a font. Those are FFmpeg filter parameters, chosen from a preset rather than
generated, so a render is repeatable and inspectable.

Themes are deliberately data, not code. A church running a different VBS next
summer should be able to add one without a deploy.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Theme:
    """A named look, and the words that describe what belongs in it."""

    slug: str
    label: str
    # Fed to CLIP. Several short phrases beat one long one: CLIP was trained on
    # caption-length text, and a paragraph blurs toward the average image.
    prompts: list[str]
    # Things to push *down* rather than exclude. A parking lot is a real photo
    # from the day and might still be wanted; it just should not lead.
    negative_prompts: list[str] = field(default_factory=list)
    # FFmpeg eq/curves parameters. Deliberately gentle: a heavy grade on a
    # photographer's own edit is insulting, and these are already graded.
    saturation: float = 1.0
    contrast: float = 1.0
    brightness: float = 0.0
    # Seconds each still is held, before transitions.
    hold_seconds: float = 3.5
    transition: str = "fade"
    transition_seconds: float = 0.6
    accent: str = "#f0a22e"


# A small starting set. The rainforest one exists because it was asked for by
# name; the rest cover the recurring shapes of this operator's work.
THEMES: dict[str, Theme] = {
    "rainforest": Theme(
        slug="rainforest",
        label="Rainforest / jungle",
        prompts=[
            "a lush green rainforest",
            "a waterfall in a jungle",
            "tropical leaves and vines",
            "children playing in a jungle themed room",
            "green and blue decorations",
        ],
        negative_prompts=["an empty parking lot", "a blank wall", "a car park"],
        saturation=1.12,
        contrast=1.04,
        hold_seconds=3.2,
        transition="fade",
        accent="#3fa66a",
    ),
    "celebration": Theme(
        slug="celebration",
        label="Celebration / party",
        prompts=[
            "people celebrating together",
            "a birthday party with balloons",
            "friends laughing at a party",
            "confetti and streamers",
        ],
        negative_prompts=["an empty room", "a blank wall"],
        saturation=1.1,
        hold_seconds=2.8,
        transition="fade",
        accent="#e4572e",
    ),
    "worship": Theme(
        slug="worship",
        label="Worship / service",
        prompts=[
            "a church congregation worshipping",
            "a worship band on stage",
            "people singing together",
            "hands raised in worship",
        ],
        saturation=0.98,
        contrast=1.02,
        hold_seconds=4.0,
        transition="fade",
        transition_seconds=0.9,
        accent="#6aa9c4",
    ),
    "property": Theme(
        slug="property",
        label="Property / listing",
        prompts=[
            "the exterior of a house",
            "a bright modern kitchen",
            "a spacious living room",
            "a landscaped garden",
        ],
        negative_prompts=["a cluttered garage", "a blurry photo"],
        # Estate photography is already graded to taste; leave it alone.
        saturation=1.0,
        contrast=1.0,
        hold_seconds=4.0,
        transition="fade",
        accent="#8bb06a",
    ),
    "plain": Theme(
        slug="plain",
        label="No theme — chronological",
        prompts=[],
        saturation=1.0,
        hold_seconds=3.5,
        transition="fade",
    ),
}


def get_theme(slug: str) -> Theme:
    return THEMES.get(slug, THEMES["plain"])


def score_against_theme(
    frame_vector: list[float] | None,
    positive: list[list[float]],
    negative: list[list[float]],
) -> float:
    """How well one frame fits a theme, in [-1, 1].

    The best positive prompt wins rather than the mean: a photo of a waterfall
    should score highly on "a waterfall in a jungle" even if it looks nothing
    like "children playing in a jungle themed room". Averaging would punish
    every photo for not matching all of them at once.

    Negatives are subtracted at a fraction of their strength. They are a nudge,
    not a filter — a photo of the parking lot on the day is still a photo from
    the day.
    """
    if not frame_vector or not positive:
        return 0.0

    def dot(a: list[float], b: list[float]) -> float:
        return float(sum(x * y for x, y in zip(a, b, strict=False)))

    best = max(dot(frame_vector, p) for p in positive)
    worst = max((dot(frame_vector, n) for n in negative), default=0.0)
    return round(best - 0.35 * worst, 6)
