"""A second opinion on what a picture shows.

The CLIP learner (ai/tagging.py) spreads a tag the operator already chose.
It cannot propose a tag nobody has typed, and it cannot read the badge on
a machine. Claude can look at a thumbnail and say "power broom", "horse
barn", "aerial" — in the words this catalogue already uses where one fits,
and in new words where none does.

Two routes lead here and both end in the same place:

- **The API reviewer** (processing/tag_review_tasks.py). FrameFound sends
  a small preview to the Claude API itself, for media nobody has tagged,
  under the monthly budget on the Security page. Nobody is watching, so an
  admission policy decides which answers are worth a human's time.
- **The MCP connector** (framefound/mcp). Claude, in conversation with the
  operator, looks at a preview and proposes tags through a tool. The
  operator is already in the loop; the proposals go straight to the queue.

A model on this Mac (framefound/looking) reaches the same queue through
`proposals_from_answer` and `write_proposals`, under origin `local` and a
stricter admission policy of its own (looking/prompt.LOCAL_POLICY).

Whichever route, a proposal lands in the review queue as a `suggested` row —
never as a fact. The operator's yes or no is what trains the learner, and
a second opinion that could confirm itself would not be a second opinion.
"""

import base64
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.ai import recipe_picker
from framefound.ai.recipe_picker import RecipePickUnavailable, post_messages, thinks
from framefound.ai.tagging import slugify
from framefound.db.models import Asset, AssetTag, Derivative, Tag, catalogued

log = structlog.get_logger()

# More tags than this and the vocabulary stops being a vocabulary: the
# most-used ones go, the long tail is left to the "new tags" answer.
MAX_VOCABULARY = 400
# Proposals per picture the model is told to stop at. A thumbnail rarely
# deserves more, and a long list is a sign of guessing.
MAX_PROPOSALS = 6
MAX_TAG_CHARS = 60

# Origins recorded on the suggestion rows (AssetTag.origin).
ORIGIN_CLIP = "clip"
ORIGIN_CLAUDE = "claude"
ORIGIN_MCP = "mcp"
# A model on this Mac (framefound/looking). Its rows also say which look
# proposed them (`look_id`) and, on a clip, the moment (`at_ms`).
ORIGIN_LOCAL = "local"

REVIEW_TOOL: dict[str, Any] = {
    "name": "review_tags",
    "description": "Record which tags describe this picture.",
    "input_schema": {
        "type": "object",
        "properties": {
            "matches": {
                "type": "array",
                "description": "Tags from the vocabulary that clearly apply, with how sure "
                "you are (0-1) that an experienced cataloguer would agree.",
                "items": {
                    "type": "object",
                    "properties": {
                        "tag": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["tag", "confidence"],
                    "additionalProperties": False,
                },
            },
            "new_tags": {
                "type": "array",
                "description": "Tags NOT in the vocabulary that a cataloguer would want for "
                "this picture: the main subject, the kind of place, the shot type. One to "
                "three lowercase words each. Empty when the vocabulary already covers it.",
                "items": {
                    "type": "object",
                    "properties": {
                        "tag": {"type": "string"},
                        "confidence": {"type": "number"},
                        "why": {"type": "string"},
                    },
                    "required": ["tag", "confidence", "why"],
                    "additionalProperties": False,
                },
            },
            "description": {
                "type": "string",
                "description": "One plain line saying what the picture shows.",
            },
        },
        "required": ["matches", "new_tags", "description"],
        "additionalProperties": False,
    },
}

SYSTEM = (
    "You catalogue photographs and video stills for a small media business: "
    "farm and estate auctions, real-estate listings, drone work, church "
    "services. Say what is in the picture, in the words this catalogue "
    "already uses where one fits.\n\n"
    "Rules:\n"
    "- Use a vocabulary tag only when the thing is clearly visible, not implied.\n"
    "- Propose a new tag only for a prominent, nameable subject someone would "
    "search for later: a kind of equipment, building, animal, room, vehicle, "
    'landscape feature or shot type ("aerial", "interior"). Lowercase, '
    "singular, one to three words. Never a person's name, never a judgement "
    '("nice", "good take"), never a colour alone.\n'
    "- Confidence is your honest probability that an experienced cataloguer "
    "would agree.\n"
    f"- At most {MAX_PROPOSALS} tags in total. Answer by calling review_tags once."
)

# Models that reject output_config.effort: the 4.5 generation.
_NO_EFFORT = ("claude-haiku-4", "claude-sonnet-4-5", "claude-opus-4-5")


@dataclass(frozen=True)
class TagProposal:
    """One tag the model put forward for one picture."""

    name: str
    # The model's probability that a cataloguer would agree; None when the
    # proposal came from a conversation, where no number was ever claimed.
    confidence: float | None
    # Already in the vocabulary (matched by slug), or brand new.
    existing: bool
    why: str = ""


@dataclass(frozen=True)
class ReviewAnswer:
    proposals: list[TagProposal]
    description: str
    # The token counts the API reported — the only honest cost figure.
    usage: dict[str, int]


def vocabulary_text(rows: list[tuple[str, int]]) -> str:
    """The tag list as the model sees it: most-used first, capped."""
    ordered = sorted(rows, key=lambda row: (-row[1], row[0].lower()))[:MAX_VOCABULARY]
    if not ordered:
        return "Vocabulary: (empty — nothing has been tagged yet, so every tag is new)"
    lines = "\n".join(f"- {name} ({count})" for name, count in ordered)
    return f"Vocabulary (tag — items carrying it):\n{lines}"


def clean_tag(text: object) -> str:
    return " ".join(str(text).split()).strip().lower()[:MAX_TAG_CHARS]


def preview_from_file(path: Path) -> bytes:
    """The thumbnail FrameFound already made, re-encoded as the small JPEG
    the API gets. Nothing is read from the original."""
    from PIL import Image

    with Image.open(path) as image:
        image.load()
        return recipe_picker.preview_bytes(image)


def _body(preview_jpeg: bytes, model: str, vocabulary: str) -> dict[str, Any]:
    """One request. The tool is *offered*, not forced: current models return
    400 on a forced tool_choice, and with a strict schema plus the
    instruction, every model answers the same way."""
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": 4096 if thinks(model) else 2048,
        # The rules and the vocabulary are identical for every picture in a
        # run; marking them cacheable bills the repeats at the cache-read
        # rate. (Below the model's minimum cacheable length this is a no-op.)
        "system": [
            {"type": "text", "text": SYSTEM},
            {"type": "text", "text": vocabulary, "cache_control": {"type": "ephemeral"}},
        ],
        "tools": [{**REVIEW_TOOL, "strict": True}],
        "tool_choice": {"type": "auto"},
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": base64.b64encode(preview_jpeg).decode(),
                        },
                    },
                    {
                        "type": "text",
                        "text": "Review this picture against the vocabulary and call review_tags.",
                    },
                ],
            }
        ],
    }
    if not model.startswith(_NO_EFFORT):
        # Naming what is in a thumbnail is not long-horizon reasoning, and
        # thinking bills as output tokens.
        body["output_config"] = {"effort": "low"}
    return body


def parse_answer(
    payload: dict[str, Any], vocabulary_slugs: set[str]
) -> tuple[list[TagProposal], str]:
    """The model's tool call as proposals, cleaned and de-duplicated.

    This half knows Anthropic's envelope: the refusal, the tool call among
    the content blocks. What the answer inside it becomes is
    `proposals_from_answer`, which a model on this Mac goes through too.
    """
    if payload.get("stop_reason") == "refusal":
        raise RecipePickUnavailable("The model declined to look at this picture")
    try:
        tool_use = next(
            block
            for block in payload["content"]
            if block.get("type") == "tool_use" and block.get("name") == REVIEW_TOOL["name"]
        )
        answer = dict(tool_use["input"])
    except (KeyError, StopIteration, TypeError) as err:
        raise RecipePickUnavailable("The model returned no answer") from err
    return proposals_from_answer(answer, vocabulary_slugs)


def proposals_from_answer(
    answer: Mapping[str, Any], vocabulary_slugs: set[str]
) -> tuple[list[TagProposal], str]:
    """An answer's `matches` and `new_tags` as proposals, and its one-line
    `description` — whoever gave the answer, with no provider's envelope.

    The answer is a model's text and is read as that: anything that is not
    the shape asked for is skipped rather than trusted, names are cut and
    lowercased, a confidence is held to 0-1, and the same tag said twice is
    kept once at its stronger confidence. Nothing here is written anywhere.

    A "match" the vocabulary does not actually contain is treated as new —
    the model occasionally rewords a tag — so a misspelling can never
    silently attach to the wrong existing tag. Which list the model put a
    tag in decides nothing: `existing` is the slug being in
    `vocabulary_slugs`.
    """
    if not isinstance(answer, Mapping):
        return [], ""
    best: dict[str, TagProposal] = {}
    for key, existing_claim in (("matches", True), ("new_tags", False)):
        rows = answer.get(key) or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            # A tag that is not text is skipped, not turned into text:
            # clean_tag's str() would make a null "none" and a nested object
            # "{'x': 'silo'}", each a new word in the review queue.
            if not isinstance(row, dict) or not isinstance(row.get("tag"), str):
                continue
            name = clean_tag(row["tag"])
            slug = slugify(name)
            if not slug:
                continue
            try:
                confidence = min(1.0, max(0.0, float(row.get("confidence", 0.0))))
            except (TypeError, ValueError, OverflowError):
                # OverflowError: JSON may carry a whole number of any length,
                # and one too long to be a float used to end the run here.
                confidence = 0.0
            proposal = TagProposal(
                name=name,
                confidence=round(confidence, 3),
                existing=slug in vocabulary_slugs,
                why=_one_line(row.get("why")) if not existing_claim else "",
            )
            if slug not in best or (best[slug].confidence or 0.0) < confidence:
                best[slug] = proposal
    proposals = sorted(best.values(), key=lambda p: -(p.confidence or 0.0))
    return proposals, _one_line(answer.get("description"))


def _one_line(text: object) -> str:
    """A reason or a description: one line, cut to 200 characters, and empty
    when the model sent something other than text (a null is not "None")."""
    return " ".join(text.split())[:200] if isinstance(text, str) else ""


def ask_for_tags(
    preview_jpeg: bytes, api_key: str, model: str, vocabulary: list[tuple[str, int]]
) -> ReviewAnswer:
    """One picture in, its proposals out. Synchronous — run it in a worker
    or a thread. Raises RecipePickUnavailable when the API refuses or the
    answer is unusable; `fatal` on it means every further call will too."""
    payload, usage = post_messages(_body(preview_jpeg, model, vocabulary_text(vocabulary)), api_key)
    proposals, description = parse_answer(payload, {slugify(name) for name, _count in vocabulary})
    log.info(
        "tag_review.answered",
        model=model,
        proposals=len(proposals),
        new=sum(1 for p in proposals if not p.existing),
    )
    return ReviewAnswer(proposals=proposals, description=description, usage=usage)


# --- admission: which unsupervised answers deserve a human's time ----------


@dataclass(frozen=True)
class AdmissionPolicy:
    """The knobs `admit` reads. Defaults are deliberately conservative: a
    queue full of weak guesses is the fastest way to make the operator
    stop answering it, and a learner nobody answers learns nothing."""

    # The operator chose this vocabulary, so moderate confidence is enough
    # for a match: a wrong "yes" costs one click and teaches the learner.
    existing_floor: float = 0.5
    # A new tag creates a row on the Tags page that stays until judged;
    # it should be something the model is sure of.
    new_floor: float = 0.75
    max_words: int = 3
    max_per_asset: int = MAX_PROPOSALS


def admit(proposals: list[TagProposal], policy: AdmissionPolicy | None = None) -> list[TagProposal]:
    """Which of the model's proposals go into the review queue.

    YOUR CALL. This is the one judgement in the reviewer that is about
    your library rather than about the API, and it shapes what the Tags
    page asks you every morning. The default below keeps vocabulary
    matches at 50% confidence and new tags at 75%, three words at most,
    strongest first, six per picture. Things worth weighing:

    - Lower `new_floor` and the vocabulary grows faster — at the cost of
      more "is this really a thing?" rows to reject.
    - A library with a settled vocabulary might refuse new tags entirely
      (return only `existing` proposals) and let the operator add words.
    - Video stills are noisier than photographs; the confidence the model
      reports is already lower there, so the same floors bite harder.
    """
    policy = policy or AdmissionPolicy()
    kept: list[TagProposal] = []
    for proposal in sorted(proposals, key=lambda p: -(p.confidence or 0.0)):
        floor = policy.existing_floor if proposal.existing else policy.new_floor
        if (proposal.confidence or 0.0) < floor:
            continue
        if not 1 <= len(proposal.name.split()) <= policy.max_words:
            continue
        kept.append(proposal)
        if len(kept) >= policy.max_per_asset:
            break
    return kept


# --- the catalogue side: what both routes write and read --------------------


async def vocabulary(db: AsyncSession) -> list[tuple[str, int]]:
    """Every tag with how many items the operator has confirmed it on."""
    confirmed = (
        select(AssetTag.tag_id, func.count().label("n"))
        .where(AssetTag.source.in_(("manual", "confirmed")))
        .group_by(AssetTag.tag_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(Tag.name, func.coalesce(confirmed.c.n, 0))
            .outerjoin(confirmed, confirmed.c.tag_id == Tag.id)
            .order_by(Tag.name)
        )
    ).all()
    return [(str(name), int(count)) for name, count in rows]


def _untagged_filter() -> Any:
    """Online pictures with a thumbnail that no person and no Claude has
    given an opinion on yet.

    The CLIP learner's own pending guesses do not count. Since new media is
    scored against every tag on arrival, a learner guess lands a few seconds
    after a file is embedded, hours before the nightly review — and when any
    row counted, most of a new shoot left this pool before Claude saw it
    (2026-11 review). The learner can only spread words already taught; a
    picture it has guessed at is still one Claude has not looked at.
    Legacy suggestions from before `origin` existed are the learner's too.

    A local model's pending guess does not count either. Claude is the
    better second opinion and costs money only when the operator runs it;
    an archive a model on the Mac has been through should still be one
    Claude can review. Once the operator answers a local guess it is their
    opinion like any other, and the picture leaves the pool.

    A picture the API reviewer has already answered for is out, whatever it
    wrote: when Claude only agreed with the learner, or the admission policy
    kept nothing, no row is written, and without this the nightly sweep
    would pay to review the same newest pictures every night.
    """
    from sqlalchemy import and_, or_

    from framefound.ai import spend
    from framefound.db.models import AiUsage

    learner_guess = and_(
        AssetTag.source == "suggested",
        or_(AssetTag.origin.is_(None), AssetTag.origin.in_((ORIGIN_CLIP, ORIGIN_LOCAL))),
    )
    has_opinion = exists().where(AssetTag.asset_id == Asset.id, ~learner_guess)
    reviewed = exists().where(
        AiUsage.asset_id == Asset.id,
        AiUsage.purpose == spend.TAG_REVIEW,
        AiUsage.ok.is_(True),
    )
    has_thumbnail = exists().where(
        Derivative.asset_id == Asset.id,
        Derivative.kind == "thumbnail",
        Derivative.status == "ready",
    )
    return (
        Asset.availability == "online",
        catalogued(),
        Asset.media_type.in_(("image", "video")),
        has_thumbnail,
        ~has_opinion,
        ~reviewed,
    )


async def pick_untagged(
    db: AsyncSession, limit: int, library_id: uuid.UUID | None = None
) -> list[uuid.UUID]:
    """Newest first: the shoot that came in this week is the one the
    operator is about to go looking through."""
    stmt = select(Asset.id).where(*_untagged_filter())
    if library_id is not None:
        stmt = stmt.where(Asset.library_id == library_id)
    stmt = stmt.order_by(Asset.first_indexed_at.desc(), Asset.id).limit(limit)
    return [uuid.UUID(str(value)) for value in (await db.execute(stmt)).scalars().all()]


async def count_untagged(db: AsyncSession, library_id: uuid.UUID | None = None) -> int:
    stmt = select(func.count()).select_from(Asset).where(*_untagged_filter())
    if library_id is not None:
        stmt = stmt.where(Asset.library_id == library_id)
    return int((await db.execute(stmt)).scalar_one())


OUTCOME_SUGGESTED = "suggested"
OUTCOME_NEW_TAG = "suggested (new tag)"
OUTCOME_ALREADY = "already tagged"
OUTCOME_REJECTED = "previously rejected — kept"
OUTCOME_PENDING = "already suggested"
# Only with `strengthen`: the pending suggestion now carries this, stronger, guess.
OUTCOME_STRENGTHENED = "already suggested — now with a stronger guess"
OUTCOME_UNUSABLE = "unusable name"


async def write_proposals(
    db: AsyncSession,
    asset_id: uuid.UUID,
    proposals: list[TagProposal],
    *,
    origin: str,
    created_by: uuid.UUID | None = None,
    look_id: uuid.UUID | None = None,
    at_ms: int | None = None,
    strengthen: bool = False,
) -> dict[str, str]:
    """Proposals become `suggested` rows for the operator to judge.

    An asset the operator has already ruled on — either way — is left
    alone: a rejection stays rejected, which is the whole reason removals
    are stored rather than deleted. Returns what happened to each name, so
    the caller (a task log or Claude in a conversation) can say so.

    `look_id` and `at_ms` are stored on a row this call adds: the local
    model's look that proposed it, and the moment in a clip it is about.

    `strengthen` is for a model that looks at a clip shot by shot and may
    suggest one tag for several shots. A suggestion still pending from the
    same origin, at a lower confidence, takes the stronger guess — its
    confidence, look and moment — so the Tags page opens on the shot the
    model was surest of. It stays a pending suggestion; a row of another
    origin, or one the operator has answered, is never changed.

    Added to the session, not committed; the caller commits.
    """
    outcomes: dict[str, str] = {}
    for proposal in proposals:
        slug = slugify(proposal.name)
        if not slug:
            outcomes[proposal.name] = OUTCOME_UNUSABLE
            continue
        tag = (await db.execute(select(Tag).where(Tag.slug == slug))).scalar_one_or_none()
        created = tag is None
        if tag is None:
            tag = Tag(name=proposal.name, slug=slug)
            db.add(tag)
            await db.flush()
        link = (
            await db.execute(
                select(AssetTag).where(AssetTag.asset_id == asset_id, AssetTag.tag_id == tag.id)
            )
        ).scalar_one_or_none()
        if link is not None:
            if strengthen and _weaker_pending(link, origin, proposal.confidence):
                link.confidence = proposal.confidence
                link.look_id = look_id
                link.at_ms = at_ms
                outcomes[tag.name] = OUTCOME_STRENGTHENED
                continue
            outcomes[tag.name] = {
                "manual": OUTCOME_ALREADY,
                "confirmed": OUTCOME_ALREADY,
                "rejected": OUTCOME_REJECTED,
            }.get(link.source, OUTCOME_PENDING)
            continue
        db.add(
            AssetTag(
                asset_id=asset_id,
                tag_id=tag.id,
                source="suggested",
                confidence=proposal.confidence,
                origin=origin,
                created_by=created_by,
                look_id=look_id,
                at_ms=at_ms,
            )
        )
        outcomes[tag.name] = OUTCOME_NEW_TAG if created else OUTCOME_SUGGESTED
    await db.flush()
    return outcomes


def _weaker_pending(link: AssetTag, origin: str, confidence: float | None) -> bool:
    """A row `strengthen` may move: still a suggestion, from this same origin
    (a local look never rewrites the learner's or Claude's number), with a
    confidence below the new one. A guess with no number strengthens nothing."""
    if link.source != "suggested" or link.origin != origin or confidence is None:
        return False
    return link.confidence is None or link.confidence < confidence
