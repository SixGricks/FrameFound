"""How far a model's answers agree with decisions the catalogue already holds.

Two things can be counted when models are compared on footage, because for
two things a person has already decided:

- **tags** — the tags somebody said yes or no to on that footage, against
  the tags the model proposed for it;
- **scores** — the shots an editor put in a finished cut, against the
  scores the model gave those shots and the ones left out.

Everything here is a count beside what it is a count of: "22 of 31", "the
used shot scored higher in 290 of 451 pairs". **None of it is a grade.**
There is no percentage, no single figure, no ranking and no "best" in this
module, and none is to be added: a number that looks like a mark gets read
as one (this project once read a colour-difference figure that way). The
tags decided so far are few and were mostly prompted by another model;
"used in a cut" depends on what that edit needed that day. Both are
evidence about agreement with past decisions. Which model is good is
judged by eye, on the page, by the person who owns the footage.

Pure: standard library only, nothing read and nothing written. The caller
turns its rows into `ItemTruth` and `ItemAnswer` (tags already as slugs)
and renders the counts it gets back.
"""

import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from statistics import fmean, median

# The confidence floors the tag counts are given at: everything a model
# proposed, then the two floors proposed for admitting a local model's tags
# to the review queue. Counting at all three is what lets those floors be
# set from a comparison instead of from a guess.
FLOORS: tuple[float, ...] = (0.0, 0.6, 0.85)
# Fewer used shots than this and the score counts are a hint, and say so.
FEW_USED = 5


@dataclass(frozen=True)
class ItemTruth:
    """What was already decided about one item of a sample, read once before
    any model looked (truth read afterwards could be the model's own
    suggestion, confirmed)."""

    item: str  # the item's id
    footage: str  # the asset's id (or the item's, if the asset is gone)
    kind: str  # "photo" | "shot"
    yes: frozenset[str]  # slugs a person said yes to on this footage
    no: frozenset[str]
    used: bool | None  # None: no cut history applies (a photograph)


@dataclass(frozen=True)
class ItemAnswer:
    """One model's answer for one item, already cleaned by the caller."""

    item: str
    ok: bool
    tags: tuple[tuple[str, float, bool], ...]  # slug, confidence, in the vocabulary
    score: int | None  # 0-100
    seconds: float | None
    attempts: int
    problem: str | None  # bad_answer | died | timeout | runtime_error | no_picture


@dataclass(frozen=True)
class TagAgreement:
    """Tag counts at one confidence floor. `proposed` is always
    `yes_found + no_repeated + unjudged`."""

    floor: float
    footage: int  # pieces of footage that had any decided tag
    yes_total: int
    yes_found: int
    no_total: int
    no_repeated: int
    proposed: int
    unjudged: int
    unjudged_new: int


@dataclass(frozen=True)
class ScoreAgreement:
    """Score counts over every pair of one used shot and one unused shot.
    `pairs` is always `higher + lower + same`; guessing gets about half."""

    used: int  # scored shots of each kind
    unused: int
    pairs: int
    higher: int
    lower: int
    same: int
    scored: int
    distinct: int  # how many different scores it gave
    few: bool  # fewer than FEW_USED used shots


@dataclass(frozen=True)
class Pace:
    answered: int
    median_s: float | None
    slowest_s: float | None
    per_hour: int | None


@dataclass(frozen=True)
class Form:
    asked: int
    answered: int
    second_tries: int
    failed: dict[str, int]


def common(answers: Mapping[str, Mapping[str, ItemAnswer]]) -> frozenset[str]:
    """The items every given model answered — `answers` is each model's
    answers by item.

    The counts are taken over this set and no other: a model that failed on
    eight items must not be counted on an easier or a harder sample than the
    one beside it. With no model there is nothing in common.
    """
    shared: set[str] | None = None
    for by_item in answers.values():
        answered = {item for item, answer in by_item.items() if answer.ok}
        shared = answered if shared is None else shared & answered
    return frozenset(shared or ())


def tag_agreement(
    truth: Sequence[ItemTruth],
    answers: Mapping[str, ItemAnswer],
    *,
    over: Collection[str],
    floor: float = 0.0,
) -> TagAgreement | None:
    """One model's tags against the tags a person decided, over the items in
    `over`, counting only proposals at or above `floor`.

    Counted per piece of footage, not per item: a tag belongs to a clip, not
    to a shot of it, so a clip's proposals are everything the model proposed
    on any of its shots that are in `over`. Counting per shot would credit a
    clip's "drone" once for every sampled shot and call the model wrong for
    not seeing the barn in a shot that does not show it.

    Footage with nothing decided is left out entirely — there is nothing to
    agree with. A tag nobody ruled on is neither right nor wrong: it is
    counted as `unjudged`, and as `unjudged_new` when the catalogue does not
    have the word. None when no footage in `over` has a decided tag.
    """
    wanted = set(over)
    by_footage: dict[str, list[ItemTruth]] = {}
    for row in truth:
        if row.item in wanted:
            by_footage.setdefault(row.footage, []).append(row)

    footage = yes_total = yes_found = no_total = no_repeated = 0
    proposed = unjudged = unjudged_new = 0
    for rows in by_footage.values():
        yes: set[str] = set().union(*(row.yes for row in rows))
        # One ruling per tag per piece of footage; if two ever disagreed, a
        # yes stands, so the three parts still add up to what was proposed.
        no: set[str] = set().union(*(row.no for row in rows)) - yes
        if not yes and not no:
            continue
        said: dict[str, bool] = {}  # slug -> is it a word the catalogue has
        for row in rows:
            answer = answers.get(row.item)
            if answer is None or not answer.ok:
                continue
            for slug, confidence, known in answer.tags:
                if confidence >= floor:
                    said[slug] = said.get(slug, False) or known
        footage += 1
        yes_total += len(yes)
        no_total += len(no)
        yes_found += len(yes & said.keys())
        no_repeated += len(no & said.keys())
        proposed += len(said)
        open_ = [slug for slug in said if slug not in yes and slug not in no]
        unjudged += len(open_)
        unjudged_new += sum(1 for slug in open_ if not said[slug])
    if not footage:
        return None
    return TagAgreement(
        floor=floor,
        footage=footage,
        yes_total=yes_total,
        yes_found=yes_found,
        no_total=no_total,
        no_repeated=no_repeated,
        proposed=proposed,
        unjudged=unjudged,
        unjudged_new=unjudged_new,
    )


def score_agreement(
    truth: Sequence[ItemTruth],
    answers: Mapping[str, ItemAnswer],
    *,
    over: Collection[str],
) -> ScoreAgreement | None:
    """One model's scores against which shots were used in a finished cut:
    every used shot in `over` set against every unused one — did the model
    score the used one higher, lower, or the same.

    Pairs, not a "top twelve" and not a correlation: a small model may give
    every shot 70, and anything that sorts and cuts would then report
    whatever the sort happened to do. Here those are `same`, and `distinct`
    says how many different scores there were at all.

    Only items whose `used` is known and whose answer carries a score take
    part (a photograph has no "used"). None when that leaves no used shot or
    no unused shot — there is no pair to count.
    """
    wanted = set(over)
    used: list[int] = []
    unused: list[int] = []
    taken: set[str] = set()
    for row in truth:
        if row.item not in wanted or row.used is None or row.item in taken:
            continue
        answer = answers.get(row.item)
        if answer is None or not answer.ok or answer.score is None:
            continue
        taken.add(row.item)
        (used if row.used else unused).append(answer.score)
    if not used or not unused:
        return None
    higher = sum(1 for mine in used for other in unused if mine > other)
    lower = sum(1 for mine in used for other in unused if mine < other)
    pairs = len(used) * len(unused)
    return ScoreAgreement(
        used=len(used),
        unused=len(unused),
        pairs=pairs,
        higher=higher,
        lower=lower,
        same=pairs - higher - lower,
        scored=len(used) + len(unused),
        distinct=len({*used, *unused}),
        few=len(used) < FEW_USED,
    )


def pace(answers: Mapping[str, ItemAnswer], kinds: Mapping[str, str], kind: str) -> Pace:
    """How long one model took over the items of one kind ("shot" or
    "photo" — three pictures against one, so they are never mixed).

    Only items it answered, timed by the answer alone: a failure's time is
    not a pace, and loading the model is reported once, elsewhere.
    `per_hour` is derived from those times with nothing else running; it is
    "about", not a promise.
    """
    answered = 0
    times: list[float] = []
    for item, answer in answers.items():
        if not answer.ok or kinds.get(item) != kind:
            continue
        answered += 1
        seconds = answer.seconds
        if seconds is not None and math.isfinite(seconds) and seconds >= 0:
            times.append(float(seconds))
    if not times:
        return Pace(answered=answered, median_s=None, slowest_s=None, per_hour=None)
    mean = fmean(times)
    return Pace(
        answered=answered,
        median_s=float(median(times)),
        slowest_s=max(times),
        per_hour=round(3600 / mean) if mean > 0 else None,
    )


def form(answers: Mapping[str, ItemAnswer], asked: int) -> Form:
    """How one model kept to the form it was asked for, out of `asked` items:
    how many it answered, how many of those needed the second try, and how
    many failed, by reason. Itself a result — it is counted over everything
    the model was asked, not over the items the models have in common.
    """
    answered = second_tries = 0
    failed: dict[str, int] = {}
    for answer in answers.values():
        if answer.ok:
            answered += 1
            second_tries += 1 if answer.attempts > 1 else 0
        else:
            reason = answer.problem or "unknown"
            failed[reason] = failed.get(reason, 0) + 1
    return Form(asked=asked, answered=answered, second_tries=second_tries, failed=failed)


def spread(scores: Sequence[int | None]) -> int | None:
    """How far apart the models' scores for one item are: highest minus
    lowest. None when fewer than two models scored it — one score differs
    from nothing. It is the page's "where they differ" filter, not a
    measure of any model.
    """
    given = [score for score in scores if score is not None]
    if len(given) < 2:
        return None
    return max(given) - min(given)
