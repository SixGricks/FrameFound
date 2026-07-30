"""Tag learning: work out what a tag looks like, then find more of it.

Runs whenever the evidence changes — a tag added by hand, a suggestion
accepted, a suggestion rejected. Cheap enough to run every time: one text
encode plus arithmetic over vectors already in the database. No training, no
GPU, no labelled dataset.

The reasoning behind the maths is in `framefound/ai/tagging.py`.
"""

import asyncio
import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.celery_app import celery_app
from framefound.config import get_settings
from framefound.db.models import Asset, AssetTag, Frame, Tag

log = structlog.get_logger()

# A tag learned from one example can match a great deal. Capping keeps the
# review list something a human will actually work through, and the next run
# continues from where this one stopped.
MAX_SUGGESTIONS_PER_RUN = 60


async def _asset_vectors(
    db: AsyncSession, asset_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[float]]:
    """One representative vector per asset: the mean of its embedded frames.

    A video's frames vary, so averaging them describes the clip rather than
    whichever moment happened to be sampled first.
    """
    from framefound.ai import tagging

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

    out: dict[uuid.UUID, list[float]] = {}
    for asset_id, vectors in grouped.items():
        mean = tagging.mean_vector(vectors)
        if mean is not None:
            out[asset_id] = mean
    return out


async def _example_vectors(
    db: AsyncSession, tag_id: uuid.UUID
) -> tuple[list[list[float]], list[list[float]]]:
    """(positives, negatives) for a tag, as one vector per asset."""
    links = (
        (
            await db.execute(
                select(AssetTag).where(
                    AssetTag.tag_id == tag_id,
                    AssetTag.source.in_(("manual", "confirmed", "rejected")),
                )
            )
        )
        .scalars()
        .all()
    )
    positive_ids = [link.asset_id for link in links if link.source in ("manual", "confirmed")]
    negative_ids = [link.asset_id for link in links if link.source == "rejected"]
    vectors = await _asset_vectors(db, positive_ids + negative_ids)
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

                positives, negatives = await _example_vectors(db, tag.id)

                # The words, averaged over caption-style prompts: CLIP responds
                # better to "a photo of a power broom" than to the bare noun.
                provider = get_embedding_provider()
                prompt_vectors: list[list[float]] = []
                for prompt in tagging.prompt_variants(tag.name):
                    try:
                        result = await asyncio.to_thread(provider.embed_text, prompt)
                        prompt_vectors.append(result.vector)
                    except Exception:
                        log.warning("tagging.text_encode_failed", tag=tag.name)
                text_vector = tagging.mean_vector(prompt_vectors)

                prototype = tagging.blend(
                    text_vector, tagging.mean_vector(positives), len(positives)
                )
                if prototype is None:
                    # No words we could encode and no embedded examples yet.
                    log.warning("tagging.no_prototype", tag=tag.name)
                    return

                threshold = tagging.derive_threshold(prototype, positives, negatives)
                tag.prototype = prototype
                tag.threshold = threshold.value
                tag.threshold_reason = threshold.reason
                tag.example_count = len(positives)
                tag.learned_at = datetime.now(UTC)
                await db.commit()
                log.info(
                    "tagging.learned",
                    tag=tag.name,
                    examples=len(positives),
                    rejections=len(negatives),
                    threshold=round(threshold.value, 4),
                    reason=threshold.reason,
                )
        finally:
            await engine.dispose()

    asyncio.run(run())
    if then_suggest:
        suggest_for_tag.delay(tag_id)


@celery_app.task(
    name="framefound.suggest_for_tag",
    queue="vision",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 1},
)
def suggest_for_tag(tag_id: str) -> None:
    """Offer the tag on everything that looks like it.

    Suggestions are written as `suggested` rows for the operator to judge;
    nothing is ever presented as fact. An asset the operator has already ruled
    on — either way — is skipped, so a rejection stays rejected.
    """
    import numpy as np

    from framefound.ai import tagging

    async def run() -> None:
        engine = create_async_engine(get_settings().db_url)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                tag = await db.get(Tag, uuid.UUID(tag_id))
                if tag is None or not tag.prototype or not tag.suggest_enabled:
                    return
                decided = set(
                    (await db.execute(select(AssetTag.asset_id).where(AssetTag.tag_id == tag.id)))
                    .scalars()
                    .all()
                )

                # Scored per frame rather than per asset: a subject that appears
                # in one shot of a long clip should still surface the clip.
                rows = (
                    await db.execute(
                        select(Frame.asset_id, Frame.embedding)
                        .join(Asset, Asset.id == Frame.asset_id)
                        .where(Frame.embedding.is_not(None), Asset.availability == "online")
                    )
                ).all()

                # Score everything first, then decide the bar from what was
                # actually seen. An absolute cutoff cannot work across CLIP's
                # modality gap — see ai/tagging.py for the measurements.
                prototype = np.asarray(tag.prototype, dtype=np.float32)
                best: dict[uuid.UUID, float] = {}
                all_scores: list[float] = []
                for asset_id, embedding in rows:
                    if not embedding:
                        continue
                    # Both are unit vectors, so the dot product is cosine.
                    score = float(prototype @ np.asarray(embedding, dtype=np.float32))
                    all_scores.append(score)
                    if asset_id in decided:
                        continue
                    if score > best.get(asset_id, -1.0):
                        best[asset_id] = score

                positives, negatives = await _example_vectors(db, tag.id)
                threshold_spec = tagging.derive_threshold(
                    tag.prototype, positives, negatives, tagging.percentile(all_scores)
                )
                threshold = threshold_spec.value
                # The bar moves with the library, so record what was actually
                # used rather than leaving a stale value from the learn pass.
                tag.threshold = threshold
                tag.threshold_reason = threshold_spec.reason

                above = {a: s for a, s in best.items() if s >= threshold}
                ranked = sorted(above.items(), key=lambda kv: -kv[1])[:MAX_SUGGESTIONS_PER_RUN]
                for asset_id, score in ranked:
                    db.add(
                        AssetTag(
                            asset_id=asset_id,
                            tag_id=tag.id,
                            source="suggested",
                            confidence=round(score, 4),
                        )
                    )
                await db.commit()
                log.info(
                    "tagging.suggested",
                    tag=tag.name,
                    offered=len(ranked),
                    above_threshold=len(above),
                    frames_considered=len(rows),
                    threshold=round(threshold, 4),
                )
        finally:
            await engine.dispose()

    asyncio.run(run())
