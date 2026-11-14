"""Everything a look writes: the ledger, the suggestions, the roll-up.

A look is one row of `looks` for one item — a photograph, a shot (keyed by
its times on the clip, never by a `shot_windows` id), or a clip judged from
one still. This module is the only writer of that table, which is how "at
most one current row per span" stays true.

What the model said reaches the catalogue in two ways, and both are
opinions, labelled as such: tags as `suggested` rows of origin `local` for
the operator to judge (through the function Claude's reviewer uses, so a
rejection stays rejected), and the asset's roll-up — a score, a caption and
a reason, plus "nothing here still needs looking at" — for Browse, search
and culling to sort and filter by without a join per row.

Every function flushes and none commits: the handler commits once per item,
so an item is either wholly recorded or not at all.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.ai import tag_review
from framefound.ai.tag_review import TagProposal
from framefound.ai.tagging import slugify
from framefound.db.models import Asset, AssetTag, LocalModel, Look, ShotWindow, Tag
from framefound.looking import MAX_LOCAL_PENDING_PER_TAG, ORIGIN_LOCAL, prompt
from framefound.looking.prompt import PROMPT_VERSION, Cleaned
from framefound.looking.scope import Item

if TYPE_CHECKING:
    from framefound.local_models.ask import Answer

Span = tuple[int | None, int | None]

# What a clip's roll-up says when its shots were looked at under more than
# one build or question. `prompt.key_of` never produces it.
MIXED = "mixed"
NOTE_CHARS = 200

# What became of a tag the store did not write, in the look's stored answer.
OUTCOME_HELD_FULL = (
    f"held back — {MAX_LOCAL_PENDING_PER_TAG} suggestions of this tag are already waiting"
)
OUTCOME_HELD_NEW = "held back — this review has proposed as many new tags as it may"
# The outcomes that put a suggestion in front of the operator.
WRITTEN = frozenset(
    {tag_review.OUTCOME_SUGGESTED, tag_review.OUTCOME_NEW_TAG, tag_review.OUTCOME_STRENGTHENED}
)


@dataclass(frozen=True)
class Suggested:
    """What `suggest` did with one look's tags."""

    written: int  # suggestion rows added or strengthened
    new_tags: int  # Tag rows created
    # Tag names not written: the tag's queue was full, or the review had used
    # its allowance of new tags. They stay in the look's stored answer.
    held: list[str] = field(default_factory=list)


def key_for(model: LocalModel, prompt_version: str = PROMPT_VERSION) -> str:
    """The redo key of what `model` says to the question: the one string a
    scope compares with (`looking.scope`) and a look is stored under."""
    return prompt.key_of(
        model.repo_id, model.revision, variant=model.variant, prompt_version=prompt_version
    )


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)


def _on_span(asset_id: uuid.UUID, span: Span) -> list[Any]:
    start, end = span
    return [
        Look.asset_id == asset_id,
        Look.start_ms.is_(None) if start is None else Look.start_ms == start,
        Look.end_ms.is_(None) if end is None else Look.end_ms == end,
    ]


async def record(
    db: AsyncSession,
    item: Item,
    answer: Answer,
    cleaned: Cleaned,
    *,
    request_id: uuid.UUID | None,
    model: LocalModel | None = None,
    now: datetime | None = None,
) -> Look:
    """The model's answer about `item`, as the span's one current look.

    The span's earlier current look (if any) stops being current; this one
    is `ok` and current. Which build answered is copied as text from the
    answer — the five provenance names — and the variant from the model's
    row (L1's `Answer` has none): `model` when the caller holds it, else it
    is read by `answer.model_id`. The key is made from the same parts, so
    it is the key a scope asked with; a model row that is gone, or is not
    the one that answered, is refused rather than stored under a key that
    would make "redo" look again for ever.

    The stored answer is what was used — the cleaned caption, score and
    reason, and the admitted tags; `suggest` adds what became of each.

    An answer about a span that is no longer one of the clip's items is
    kept but not current (`look.current` is False): the clip was analysed
    again while the model was answering (the look runs on another lane, and
    an answer takes seconds), so `after_reanalysis` had already retired
    the span's looks and would never see this one. The same rule as
    `after_reanalysis`: a shot's times must be a window's, and a clip is
    judged whole only while it has no windows."""
    if model is None:
        from framefound.local_models import registry

        model = await registry.get_model(db, answer.model_id)
        if model is None:
            raise ValueError("The model that answered is no longer installed.")
    elif model.id != answer.model_id:
        raise ValueError("The answer is not from this model.")
    provenance = answer.provenance()
    key = prompt.key_of(
        answer.repo_id,
        answer.revision,
        variant=model.variant,
        prompt_version=answer.prompt_version,
    )
    still_an_item = await _is_item(db, item)
    await db.execute(
        update(Look)
        .where(*_on_span(item.asset_id, item.span), Look.current.is_(True))
        .values(current=False)
    )
    look = Look(
        asset_id=item.asset_id,
        start_ms=item.start_ms,
        end_ms=item.end_ms,
        ok=True,
        current=still_an_item,
        score=cleaned.score,
        caption=cleaned.caption,
        reason=cleaned.reason,
        answer={
            "caption": cleaned.caption,
            "score": cleaned.score,
            "reason": cleaned.reason,
            "tags": [
                {"name": p.name, "confidence": p.confidence, "existing": p.existing}
                for p in cleaned.proposals
            ],
            "dropped": cleaned.dropped,
        },
        key=key,
        variant=model.variant,
        seconds=answer.seconds,
        demo=answer.demo,
        request_id=request_id,
        # Set here rather than by the database so the roll-up, in the same
        # transaction, can read when a photograph was looked at.
        created_at=_now(now),
        **provenance,
    )
    db.add(look)
    await db.flush()
    return look


async def record_failure(
    db: AsyncSession,
    item: Item,
    *,
    model: LocalModel,
    note: str,
    request_id: uuid.UUID | None,
    now: datetime | None = None,
) -> Look:
    """`item` was tried and nothing usable came of it, and why.

    The row is not current, so the item still needs looking at; the scope
    reads it as "failed here" — in this review (`request_id`), or at ingest
    under this key, which `INGEST_TRIES` of end the tries. There is no
    answer to take a runtime version from, so the build is the model's
    repository, commit and variant, and the question is this one."""
    look = Look(
        asset_id=item.asset_id,
        start_ms=item.start_ms,
        end_ms=item.end_ms,
        ok=False,
        current=False,
        note=prompt._plain(note, NOTE_CHARS),
        key=key_for(model),
        model=model.repo_id,
        model_revision=model.revision,
        variant=model.variant,
        runtime=model.runtime,
        prompt_version=PROMPT_VERSION,
        request_id=request_id,
        created_at=_now(now),
    )
    db.add(look)
    await db.flush()
    return look


async def _pending_local(db: AsyncSession, tag_id: uuid.UUID) -> int:
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(AssetTag)
                .where(
                    AssetTag.tag_id == tag_id,
                    AssetTag.source == "suggested",
                    AssetTag.origin == ORIGIN_LOCAL,
                )
            )
        ).scalar_one()
    )


async def _hold(
    db: AsyncSession, asset_id: uuid.UUID, proposal: TagProposal, *, new_tags_left: int
) -> str | None:
    """Why this proposal is not written, or None to write it.

    Only a proposal that would add a row is held: one that strengthens or
    meets a suggestion already on this asset adds nothing to any queue."""
    slug = slugify(proposal.name)
    if not slug:
        return None  # write_proposals says it is unusable
    tag = (await db.execute(select(Tag.id).where(Tag.slug == slug))).scalar_one_or_none()
    if tag is None:
        return OUTCOME_HELD_NEW if new_tags_left <= 0 else None
    linked = (
        await db.execute(
            select(AssetTag.id).where(AssetTag.asset_id == asset_id, AssetTag.tag_id == tag)
        )
    ).scalar_one_or_none()
    if linked is not None:
        return None
    if await _pending_local(db, tag) >= MAX_LOCAL_PENDING_PER_TAG:
        return OUTCOME_HELD_FULL
    return None


async def suggest(
    db: AsyncSession,
    look: Look,
    proposals: Sequence[TagProposal],
    *,
    new_tags_left: int,
) -> Suggested:
    """A look's admitted tags, as suggestions of origin `local`.

    Each goes through `tag_review.write_proposals`, so a rejection stays
    rejected and an answered tag is left alone; it carries the look and, on
    a shot, the shot's middle, where the Tags page opens the clip. One tag
    said about several shots of a clip is one row that follows the
    strongest guess (`strengthen`).

    Two limits hold a proposal back instead (P11): a tag nobody has used
    yet once the review has proposed `new_tags_left` of them, and a tag
    that already has `MAX_LOCAL_PENDING_PER_TAG` local suggestions waiting —
    what the Tags page loads, and the point past which the CLIP learner
    stops topping it up. What became of every tag is kept on the look."""
    at_ms = (
        (look.start_ms + look.end_ms) // 2
        if look.start_ms is not None and look.end_ms is not None
        else None
    )
    written = 0
    created = 0
    held: list[str] = []
    told: list[dict[str, Any]] = []
    for proposal in proposals:
        reason = await _hold(db, look.asset_id, proposal, new_tags_left=new_tags_left - created)
        if reason is not None:
            held.append(proposal.name)
            outcome = reason
        else:
            outcomes = await tag_review.write_proposals(
                db,
                look.asset_id,
                [proposal],
                origin=ORIGIN_LOCAL,
                look_id=look.id,
                at_ms=at_ms,
                strengthen=True,
            )
            outcome = next(iter(outcomes.values()))
            written += outcome in WRITTEN
            created += outcome == tag_review.OUTCOME_NEW_TAG
        told.append(
            {
                "name": proposal.name,
                "confidence": proposal.confidence,
                "existing": proposal.existing,
                "outcome": outcome,
            }
        )
    # A new dict, not an edit in place: a JSON column notices only that.
    look.answer = {**(look.answer or {}), "tags": told}
    await db.flush()
    return Suggested(written=written, new_tags=created, held=held)


async def current(db: AsyncSession, asset_id: uuid.UUID) -> dict[Span, Look]:
    """The asset's current looks, by span: what the model now says of each
    item of it."""
    rows = (
        (
            await db.execute(
                select(Look).where(
                    Look.asset_id == asset_id, Look.current.is_(True), Look.ok.is_(True)
                )
            )
        )
        .scalars()
        .all()
    )
    return {(row.start_ms, row.end_ms): row for row in rows}


async def _window_spans(db: AsyncSession, asset_id: uuid.UUID) -> set[Span]:
    rows = await db.execute(
        select(ShotWindow.start_ms, ShotWindow.end_ms).where(ShotWindow.asset_id == asset_id)
    )
    return {(int(start), int(end)) for start, end in rows.all()}


async def _is_item(db: AsyncSession, item: Item) -> bool:
    """`item`'s span is still one of its asset's items: a photograph's whole,
    a shot whose times are a window's, or a clip with no windows judged
    whole. Read in the recording transaction, so a re-analysis committed
    while the model was answering is seen."""
    if item.media_type != "video":
        return True
    windows = await _window_spans(db, item.asset_id)
    return item.span in windows if windows else item.span == (None, None)


def _best(looks: Sequence[Look]) -> Look | None:
    """The look with the highest score; of equals, the earliest shot."""
    return min(
        looks,
        key=lambda look: (-(look.score or 0), look.start_ms or 0, look.end_ms or 0),
        default=None,
    )


async def _counted(
    db: AsyncSession, asset_id: uuid.UUID, media_type: str
) -> tuple[list[Look], bool]:
    """The asset's current looks that count for it, and whether nothing in
    it still needs looking at.

    A photograph's is its look of the whole. A clip's are those of its
    shots whose times are still one of its windows — a look left from
    before a re-analysis moved the shots does not count — and it is complete
    when every window has one. A clip with no windows is its look of the
    whole, judged from one still."""
    looks = await current(db, asset_id)
    if media_type == "video":
        windows = await _window_spans(db, asset_id)
        if windows:
            on = [looks[span] for span in windows if span in looks]
            return on, len(on) == len(windows)
    whole = looks.get((None, None))
    return ([] if whole is None else [whole]), whole is not None


async def best_current(db: AsyncSession, asset: Asset) -> Look | None:
    """The one current look that speaks for `asset`, or None while it has
    none: a photograph's look; a clip's best-scoring shot (of equals, the
    earliest), or its look of the whole while it has no shots.

    This is the look `roll_up` writes onto the asset's own row, which
    search, culling and the grid read; a page that shows a look says whose
    it is by asking here, so the two cannot name different looks."""
    on, _complete = await _counted(db, asset.id, asset.media_type)
    return _best(on)


async def roll_up(db: AsyncSession, asset_id: uuid.UUID, *, now: datetime | None = None) -> None:
    """The asset's five look columns, from its current looks.

    A photograph's are its current look's, or all NULL. A clip's score,
    caption and reason are its best-scoring current shot's (only shots
    whose times are still one of its windows count). `looked_at` and
    `looked_by` say "nothing here still needs looking at" (P20): they are
    set only when every window has a current look — or, for a clip with no
    windows, when it has a current look of the whole — and are NULL
    otherwise, so a clip half done has a score and no `looked_at`.
    `looked_at` keeps the moment the clip was first complete; `looked_by` is
    the key its looks share, or "mixed"."""
    found: Any = (
        await db.execute(select(Asset.media_type, Asset.looked_at).where(Asset.id == asset_id))
    ).one_or_none()
    if found is None:
        return
    on, complete = await _counted(db, asset_id, found.media_type)
    best = _best(on)
    looked_at: datetime | None = None
    looked_by: str | None = None
    if complete and best is not None:
        if found.media_type != "video":
            looked_at, looked_by = best.created_at, best.key
        else:
            looked_at = found.looked_at or _now(now)
            keys = {look.key for look in on}
            looked_by = keys.pop() if len(keys) == 1 else MIXED
    await db.execute(
        update(Asset)
        .where(Asset.id == asset_id)
        .values(
            look_score=best.score if best is not None else None,
            look_caption=best.caption if best is not None else None,
            look_reason=best.reason if best is not None else None,
            looked_at=looked_at,
            looked_by=looked_by,
        )
    )
    await db.flush()


async def after_reanalysis(
    db: AsyncSession, asset_id: uuid.UUID, *, now: datetime | None = None
) -> None:
    """A clip's windows were rebuilt: looks whose span is no longer one of
    them stop being current, then the roll-up is made again.

    A shot found again at the same times keeps its look (D2); one whose
    times moved needs looking at again. A look of the whole clip stands only
    while the clip has no windows — it was judged from its poster because
    there were no shots to show, and once there are, the shots are what is
    looked at. Nothing is deleted: the looks, and the suggestions pointing
    at them, stay as the record of what was said."""
    windows = await _window_spans(db, asset_id)
    stale = [
        look.id
        for span, look in (await current(db, asset_id)).items()
        if (span not in windows if windows else span != (None, None))
    ]
    if stale:
        await db.execute(update(Look).where(Look.id.in_(stale)).values(current=False))
    await roll_up(db, asset_id, now=now)
