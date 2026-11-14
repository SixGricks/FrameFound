"""The demo's looks: made-up answers to the Looker's question, shaped like real ones.

Only ever used on an answer that L1's demo model server gave (`Answer.demo`)
while demo mode is on (`local_models.availability`), which a real install
can never be in. That server answers every question with JSON that fits the
schema and means nothing: a caption of random words, any score, tags that
are not the catalogue's. A demo comparison read off that would show nonsense
in every cell, so the turn puts one of these in its place instead:

- a stock sentence for the caption, and a reason that says it was made up;
- a score from a hash of the seed;
- one to three tags drawn only from the vocabulary the model was shown, each
  with a confidence spread across the floors Compare counts at;
- no new tags: a demo model has seen nothing it could name.

The seed is the model and the item (`"<repo id>:<item id>"`), so two demo
models answer the same item differently and reading the page again changes
nothing. Nothing here has looked at a picture, so a demo model's agreement
with the catalogue hovers around chance — which is honest, and the page's
banner says the answers are made up.
"""

import hashlib
from collections.abc import Sequence

from framefound.looking.prompt import LookAnswer, TagGuess

# The stock sentences, chosen by the seed: plain descriptions of the kind of
# thing the business films, none naming a place or a person.
SHOT_CAPTIONS = (
    "A drone moves slowly over open ground with trees along the edge.",
    "A wide view from above of grass, paths and a few buildings.",
    "The camera passes along the front of a building and its grounds.",
    "A high view of a landscape under an open sky.",
    "A low pass over a lawn towards a line of trees.",
)
PHOTO_CAPTIONS = (
    "A room photographed from its doorway in even light.",
    "The outside of a building on a clear day.",
    "A view across a field towards farm buildings.",
    "A close view of a detail inside a room.",
)
# Says, on every demo answer, that nobody looked.
REASON = "Made up for the demonstration: no model looked at this picture."

# A demo score lies between these, so its figures have a spread to count.
SCORE_LOW = 35
SCORE_HIGH = 95
# A demo confidence lies between these: under 0.6, between 0.6 and 0.85,
# and above, so all three floors Compare counts at have something to drop.
CONFIDENCE_LOW = 0.45
CONFIDENCE_HIGH = 0.97
MOST_TAGS = 3
# Invented figures for a demo answer: seconds it "took", memory it "used".
SECONDS = (2.0, 9.0)
MEMORY_GB = (2.5, 12.0)
# A vocabulary word longer than a model may write (TagGuess) is never drawn:
# cut short, it would not be the catalogue's word any more.
_TAG_CHARS = 60


def _unit(seed: str, what: str) -> float:
    """A number in [0, 1) that the seed and the question always give alike."""
    digest = hashlib.sha256(f"{what}\0{seed}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def _between(seed: str, what: str, low: float, high: float) -> float:
    return low + (high - low) * _unit(seed, what)


def _words(vocabulary: Sequence[str]) -> list[str]:
    """The vocabulary's words as a model would be shown them, once each."""
    seen: dict[str, str] = {}
    for word in vocabulary:
        if not isinstance(word, str):
            continue
        said = " ".join(word.split())
        if said and len(said) <= _TAG_CHARS:
            seen.setdefault(said.casefold(), said)
    return list(seen.values())


def answer_for(seed: str, vocabulary: Sequence[str], *, shot: bool) -> LookAnswer:
    """A made-up look, the same for the same seed and vocabulary.

    `vocabulary` is the words the model was shown (the names of the
    comparison's frozen vocabulary). Every tag is one of them, as written
    there; with no vocabulary there are none.
    """
    words = _words(vocabulary)
    tags: list[TagGuess] = []
    if words:
        many = 1 + int(_unit(seed, "how many") * min(MOST_TAGS, len(words)))
        chosen = sorted(words, key=lambda word: _unit(seed, f"tag {word}"))[:many]
        tags = [
            TagGuess(
                tag=word,
                confidence=round(
                    _between(seed, f"sure {word}", CONFIDENCE_LOW, CONFIDENCE_HIGH), 2
                ),
            )
            for word in chosen
        ]
        tags.sort(key=lambda guess: -guess.confidence)
    captions = SHOT_CAPTIONS if shot else PHOTO_CAPTIONS
    caption = captions[int(_unit(seed, "caption") * len(captions))]
    score = SCORE_LOW + int(_unit(seed, "score") * (SCORE_HIGH - SCORE_LOW + 1))
    return LookAnswer(caption=caption, score=score, reason=REASON, matches=tags, new_tags=[])


def figures_for(seed: str) -> tuple[float, float]:
    """(seconds, memory in GB) for a made-up answer: invented, the same for the
    same seed. The fake server reports one answer time for every model, so a
    demo comparison's pace would otherwise read alike in every column.

    Compare takes only the seconds. It never stores the memory as a model's:
    the page labels a stored figure as what the software reported, so a
    caller that shows this one must say it was invented."""
    return (
        round(_between(seed, "seconds", *SECONDS), 1),
        round(_between(seed, "memory", *MEMORY_GB), 1),
    )
