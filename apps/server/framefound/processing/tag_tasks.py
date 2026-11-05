"""Tag learning: work out what a tag looks like, then find more of it.

Runs whenever the evidence changes — a tag added by hand, a suggestion
accepted, a suggestion rejected — and whenever new media arrives: an asset is
scored against every learned tag as soon as its frames are embedded
(`suggest_tags_for_asset`), and a nightly sweep re-runs every tag as a safety
net. Cheap enough to run every time: one text encode plus arithmetic over
vectors already in the database. No training, no GPU, no labelled dataset.

The reasoning behind the maths is in `framefound/ai/tagging.py`.
"""

import asyncio
import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.celery_app import celery_app
from framefound.config import get_settings
from framefound.db.models import Asset, AssetTag, Frame, Tag

log = structlog.get_logger()

# The review queue is bounded per tag, not per run. A cap of 60 per run was
# enough while only the operator's own edits ran the learner: the queue grew
# as fast as they worked it. The nightly sweep runs every tag unattended, and
# from five examples the bar may sit at the top 10% of the library (about
# 3,000 assets on 30k), so a per-run cap let every tag add 60 unanswered
# suggestions a night, building towards tens of thousands (2026-11 review).
# A run now only tops a tag's waiting suggestions back up to this; each
# answer frees a slot. Sixty is one sitting on the Tags page for one tag.
MAX_PENDING_PER_TAG = 60
# New media gets headroom above that, so a backlog nobody has worked through
# does not stop next week's shoot being suggested, which is what arrival
# scoring is for. Still a bound: at about 750 new photos a month, a tag that
# matches one in ten fills the headroom in roughly a month left unanswered,
# and from there the sweep offers the strongest matches as the queue drains.
ARRIVAL_PENDING_PER_TAG = 2 * MAX_PENDING_PER_TAG

POSITIVE_SOURCES = ("manual", "confirmed")
LABELLED_SOURCES = (*POSITIVE_SOURCES, "rejected")


async def _frames_by_asset(
    db: AsyncSession, asset_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[list[float]]]:
    if not asset_ids:
        return {}
    rows = (
        await db.execute(
            select(Frame.asset_id, Frame.embedding).where(
                Frame.asset_id.in_(asset_ids), Frame.embedding.is_not(None)
            )
        )
    ).all()
    grouped: dict[uuid.UUID, list[list[float]]] = {}
    for asset_id, embedding in rows:
        if embedding:
            grouped.setdefault(asset_id, []).append(embedding)
    return grouped


async def _asset_vectors(
    db: AsyncSession, asset_ids: list[uuid.UUID], reference: list[float] | None
) -> dict[uuid.UUID, list[float]]:
    """One representative vector per asset: the mean of its frames most like
    `reference` — the same aggregation candidates are scored with, so an
    example's score and a candidate's are on one scale."""
    from framefound.ai import tagging

    out: dict[uuid.UUID, list[float]] = {}
    for asset_id, frames in (await _frames_by_asset(db, asset_ids)).items():
        vector = tagging.representative(frames, reference)
        if vector is not None:
            out[asset_id] = vector
    return out


async def _example_vectors(
    db: AsyncSession, tag_id: uuid.UUID, reference: list[float] | None
) -> tuple[list[list[float]], list[list[float]]]:
    """(positives, negatives) for a tag, as one vector per asset."""
    links = (
        (
            await db.execute(
                select(AssetTag).where(
                    AssetTag.tag_id == tag_id, AssetTag.source.in_(LABELLED_SOURCES)
                )
            )
        )
        .scalars()
        .all()
    )
    positive_ids = [link.asset_id for link in links if link.source in POSITIVE_SOURCES]
    negative_ids = [link.asset_id for link in links if link.source == "rejected"]
    vectors = await _asset_vectors(db, positive_ids + negative_ids, reference)
    return (
        [vectors[a] for a in positive_ids if a in vectors],
        [vectors[a] for a in negative_ids if a in vectors],
    )


@celery_app.task(
    name="framefound.learn_tag",
    queue="vision",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 2},
)
def learn_tag(tag_id: str, then_suggest: bool = True) -> None:
    from framefound.ai import tagging
    from framefound.ai.embeddings import get_embedding_provider

    async def run() -> None:
        engine = create_async_engine(get_settings().db_url)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                tag = await db.get(Tag, uuid.UUID(tag_id))
                if tag is None:
                    return

                # The words, averaged over the library's kinds of picture:
                # "a real estate photo of", "an aerial drone photo of", ...
                provider = get_embedding_provider()
                text_vector = await asyncio.to_thread(
                    tagging.text_prototype, tag.name, lambda p: provider.embed_text(p).vector
                )
                if text_vector is None:
                    log.warning("tagging.text_encode_failed", tag=tag.name)

                # A video example is its frames most like what the tag is
                # believed to be so far — the stored prototype, or the words on
                # the first pass — not the whole clip.
                reference = tag.prototype or text_vector
                positives, negatives = await _example_vectors(db, tag.id, reference)

                prototype = tagging.blend(
                    text_vector, tagging.mean_vector(positives), len(positives)
                )
                if prototype is None:
                    # No words we could encode and no embedded examples yet.
                    log.warning("tagging.no_prototype", tag=tag.name)
                    return

                tag.prototype = prototype
                tag.example_count = len(positives)
                tag.learned_at = datetime.now(UTC)
                # The suggestion pass runs here, in this transaction, so the
                # new prototype and the percentile bar measured for it are
                # committed together. learn_tag can only derive the
                # no-baseline fallback bar, on a different scale. Committed
                # alone, arrival scoring judged new media by it while the pass
                # waited in the queue and wrote suggestions the real bar would
                # refuse, which nothing retracted. Committing no bar instead
                # made arrival scoring skip the tag, and the queued pass only
                # tops up to the sweep's 60, so files arriving then lost the
                # 120 arrival headroom whenever 60 or more were waiting
                # (2026-11 review). With one commit there is no gap: arrival
                # scoring sees the old pair or the new one, never a mix.
                if then_suggest and tag.suggest_enabled:
                    await _score_library(db, tag)
                else:
                    # No pass coming: the fallback is the only bar there will
                    # be, so keep it.
                    threshold = tagging.derive_threshold(prototype, positives, negatives)
                    tag.threshold = threshold.value
                    tag.threshold_reason = threshold.reason
                await db.commit()
                log.info(
                    "tagging.learned",
                    tag=tag.name,
                    examples=len(positives),
                    rejections=len(negatives),
                    threshold=None if tag.threshold is None else round(tag.threshold, 4),
                    reason=tag.threshold_reason,
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


@celery_app.task(
    name="framefound.suggest_for_tag",
    queue="vision",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 1},
)
def suggest_for_tag(tag_id: str) -> None:
    """Score the library against a tag's stored prototype on its own.

    learn_tag runs the same pass inline and no longer queues this. The task
    stays registered so messages queued by the previous release (whose
    learn_tag chained into it) still run.
    """

    async def run() -> None:
        engine = create_async_engine(get_settings().db_url)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                tag = await db.get(Tag, uuid.UUID(tag_id))
                if tag is None or not tag.prototype or not tag.suggest_enabled:
                    return
                await _score_library(db, tag)
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(run())


async def _score_library(db: AsyncSession, tag: Tag) -> None:
    """Offer the tag on everything that looks like it, and set its bar.

    Suggestions are written as `suggested` rows for the operator to judge;
    nothing is ever presented as fact. An asset the operator has already ruled
    on — either way — is skipped, so a rejection stays rejected. Leaves the
    commit to the caller, so a re-learned prototype and its bar land together.
    """
    from framefound.ai import tagging

    prototype = tag.prototype
    if not prototype:
        return
    links = (
        await db.execute(
            select(AssetTag.asset_id, AssetTag.source).where(AssetTag.tag_id == tag.id)
        )
    ).all()
    decided = {asset_id for asset_id, _ in links}
    # Whatever is already waiting for an answer — from an earlier run,
    # arrival scoring or Claude — counts against the cap.
    pending = sum(1 for _, source in links if source == "suggested")

    rows = (
        await db.execute(
            select(Frame.asset_id, Frame.embedding)
            .join(Asset, Asset.id == Frame.asset_id)
            .where(Frame.embedding.is_not(None), Asset.availability == "online")
        )
    ).all()
    grouped: dict[uuid.UUID, list[list[float]]] = {}
    for asset_id, embedding in rows:
        if embedding:
            grouped.setdefault(asset_id, []).append(embedding)

    # Every asset is scored on its best few frames, exactly as the examples
    # are represented — a subject in a few shots of a long clip still
    # surfaces the clip, and the bar the examples set is on the same scale
    # as the scores it is compared with. Score everything first, then decide
    # the bar from what was actually seen. An absolute cutoff cannot work
    # across CLIP's modality gap — see ai/tagging.py for the measurements.
    all_scores: list[float] = []
    candidates: dict[uuid.UUID, tuple[float, list[float]]] = {}
    for asset_id, frames in grouped.items():
        vector = tagging.representative(frames, prototype)
        if vector is None:
            continue
        score = tagging.cosine(prototype, vector)
        all_scores.append(score)
        if asset_id not in decided:
            candidates[asset_id] = (score, vector)

    positives, negatives = await _example_vectors(db, tag.id, prototype)
    baseline = tagging.percentile(all_scores, tagging.baseline_percentile(len(positives)))
    threshold_spec = tagging.derive_threshold(prototype, positives, negatives, baseline)
    threshold = threshold_spec.value
    # The bar moves with the library, so record what was actually used.
    tag.threshold = threshold
    tag.threshold_reason = threshold_spec.reason

    above = [(a, s, v) for a, (s, v) in candidates.items() if s >= threshold]
    flags = tagging.veto_flags([v for _, _, v in above], positives, negatives)
    kept = [(a, s) for (a, s, _), veto in zip(above, flags, strict=True) if not veto]
    # The bar above is refreshed even when there is no room, so arrival
    # scoring still has a current one to judge against.
    room = max(0, MAX_PENDING_PER_TAG - pending)
    ranked = sorted(kept, key=lambda kv: -kv[1])[:room]
    for asset_id, score in ranked:
        db.add(
            AssetTag(
                asset_id=asset_id,
                tag_id=tag.id,
                source="suggested",
                confidence=round(score, 4),
                origin="clip",
            )
        )
    log.info(
        "tagging.suggested",
        tag=tag.name,
        offered=len(ranked),
        already_pending=pending,
        above_threshold=len(above),
        vetoed=len(above) - len(kept),
        assets_considered=len(grouped),
        frames_considered=len(rows),
        threshold=round(threshold, 4),
    )


async def _suggest_tags_for_asset(db: AsyncSession, asset_id: uuid.UUID) -> int:
    import numpy as np

    from framefound.ai import tagging

    tags = (
        (
            await db.execute(
                select(Tag).where(
                    Tag.prototype.is_not(None),
                    Tag.threshold.is_not(None),
                    Tag.suggest_enabled.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    if not tags:
        return 0
    frames = [
        embedding
        for embedding in (
            await db.execute(
                select(Frame.embedding).where(
                    Frame.asset_id == asset_id, Frame.embedding.is_not(None)
                )
            )
        )
        .scalars()
        .all()
        if embedding
    ]
    if not frames:
        return 0
    width = len(frames[0])
    frames = [frame for frame in frames if len(frame) == width]
    # Any row at all — manual, confirmed, rejected, or a suggestion already
    # waiting — is left exactly as it is.
    existing = set(
        (await db.execute(select(AssetTag.tag_id).where(AssetTag.asset_id == asset_id)))
        .scalars()
        .all()
    )
    tags = [t for t in tags if t.id not in existing and t.prototype and len(t.prototype) == width]
    if not tags:
        return 0

    # One product scores every frame against every tag; each tag then sees
    # the asset as the mean of the frames most like it.
    prototypes = tagging.unit_rows([t.prototype for t in tags])
    means = tagging.top_frame_means(tagging.unit_rows(frames), prototypes)
    scores = np.einsum("td,td->t", means, prototypes)
    cleared = [
        (tag, float(score), [float(c) for c in mean])
        for tag, score, mean in zip(tags, scores, means, strict=True)
        if tag.threshold is not None and float(score) >= tag.threshold
    ]
    if not cleared:
        return 0

    # A tag whose queue is already full gets nothing more on arrival; one
    # grouped count covers every tag this asset cleared.
    pending = {
        tag_id: int(count)
        for tag_id, count in (
            await db.execute(
                select(AssetTag.tag_id, func.count())
                .where(
                    AssetTag.tag_id.in_([tag.id for tag, _, _ in cleared]),
                    AssetTag.source == "suggested",
                )
                .group_by(AssetTag.tag_id)
            )
        ).all()
    }
    cleared = [c for c in cleared if pending.get(c[0].id, 0) < ARRIVAL_PENDING_PER_TAG]
    if not cleared:
        return 0

    # Rejections veto look-alikes here too. Only tags that cleared the bar and
    # have been rejected somewhere need their examples loaded.
    rejected_tags = set(
        (
            await db.execute(
                select(AssetTag.tag_id).where(
                    AssetTag.tag_id.in_([tag.id for tag, _, _ in cleared]),
                    AssetTag.source == "rejected",
                )
            )
        )
        .scalars()
        .all()
    )
    offered = 0
    for tag, score, vector in cleared:
        if tag.id in rejected_tags:
            positives, negatives = await _example_vectors(db, tag.id, tag.prototype)
            if tagging.vetoed(vector, positives, negatives):
                continue
        db.add(
            AssetTag(
                asset_id=asset_id,
                tag_id=tag.id,
                source="suggested",
                confidence=round(score, 4),
                origin="clip",
            )
        )
        offered += 1
    await db.commit()
    if offered:
        log.info("tagging.suggested_on_arrival", asset_id=str(asset_id), offered=offered)
    return offered


async def suggest_tags_for_asset(db: AsyncSession, asset_id: uuid.UUID) -> int:
    """Score one newly embedded asset against every learned tag.

    Without this, new media was only ever considered when the operator next
    edited or re-learned a tag — so the vocabulary never reached anything
    shot after it was taught. Uses each tag's stored prototype and the bar
    measured for it, which learn_tag commits together; the nightly sweep
    refreshes both. A tag with no bar yet is skipped. A tag already holding
    ARRIVAL_PENDING_PER_TAG unanswered suggestions is skipped too.

    Called from the embedding stage, which must not fail because of it: any
    error is logged and swallowed, and the asset is still searchable.
    Returns how many suggestions were written.
    """
    if not get_settings().tag_autosuggest:
        return 0
    try:
        return await _suggest_tags_for_asset(db, asset_id)
    except Exception:
        log.warning("tagging.on_arrival_failed", asset_id=str(asset_id), exc_info=True)
        try:
            await db.rollback()
        except Exception:
            log.warning("tagging.on_arrival_rollback_failed", asset_id=str(asset_id))
        return 0


async def _learned_tag_ids() -> list[uuid.UUID]:
    engine = create_async_engine(get_settings().db_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            return list(
                (
                    await db.execute(
                        select(Tag.id).where(
                            Tag.prototype.is_not(None), Tag.suggest_enabled.is_(True)
                        )
                    )
                )
                .scalars()
                .all()
            )
    finally:
        await engine.dispose()


@celery_app.task(name="framefound.suggest_all_tags", queue="default")
def suggest_all_tags() -> None:
    """Nightly safety net: re-learn every learned tag, which re-suggests.

    Catches whatever arrival scoring missed — media embedded while a worker
    was down, or before a tag existed — and rebuilds each prototype and bar
    against the library as it now stands. Re-learning rather than only
    re-suggesting matters once: prototypes stored before the 2026-11 review
    were the mean of *every* frame of a video example, and nothing else
    rebuilds them until the operator touches the tag. Only queues the work;
    each learn_tag scores the library itself on the vision queue.
    """
    if not get_settings().tag_autosuggest:
        return
    tag_ids = asyncio.run(_learned_tag_ids())
    for tag_id in tag_ids:
        learn_tag.delay(str(tag_id))
    log.info("tagging.sweep_queued", tags=len(tag_ids))
