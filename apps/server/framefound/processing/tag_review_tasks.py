"""Claude reviews untagged media, under the budget.

The worker side of ai/tag_review.py: for each asset it is handed, send the
thumbnail FrameFound already made to the Claude API, pass the answer
through the admission policy, and write what survives as suggestions for
the operator to judge. Before *every* call the monthly budget is checked
from the ledger, so a run that would cross the cap stops and says so,
rather than finding out from the invoice.

Progress is kept in one settings row (AiReviewState) that the Tags page
and the MCP connector read: how many were reviewed, how many suggestions
came of it, what it cost, and why it stopped if it stopped early.
"""

import asyncio
import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from framefound.ai import spend, tag_review
from framefound.ai.recipe_picker import RecipePickUnavailable
from framefound.celery_app import celery_app
from framefound.config import get_settings
from framefound.db.models import Asset, Derivative
from framefound.media.maps_store import (
    AiReviewState,
    load_ai_edit_config,
    load_review_state,
    save_review_state,
)

log = structlog.get_logger()

# One run at a time is plenty: the API is the bottleneck, and two runs
# would race each other for the same budget.
MAX_BATCH = 500
# How often the state row is refreshed mid-run, so the page can show
# progress without a write per picture.
PROGRESS_EVERY = 5


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _review(asset_ids: list[str]) -> None:
    settings = get_settings()
    engine = create_async_engine(settings.db_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            config = await load_ai_edit_config(db)
            state = AiReviewState(state="running", started_at=_now(), requested=len(asset_ids))
            await save_review_state(db, state)

            if not config.ready:
                state.state = "failed"
                state.message = "No Anthropic key is configured (Security page)"
                state.finished_at = _now()
                await save_review_state(db, state)
                return
            api_key = config.api_key()
            model = config.review_model or config.model
            vocab = await tag_review.vocabulary(db)
            policy = tag_review.AdmissionPolicy()

            for raw_id in asset_ids[:MAX_BATCH]:
                try:
                    asset_id = uuid.UUID(str(raw_id))
                except ValueError:
                    continue
                asset = await db.get(Asset, asset_id)
                if asset is None or asset.availability != "online":
                    state.skipped += 1
                    continue
                relative_path = (
                    await db.execute(
                        select(Derivative.relative_path).where(
                            Derivative.asset_id == asset_id,
                            Derivative.kind == "thumbnail",
                            Derivative.status == "ready",
                        )
                    )
                ).scalar_one_or_none()
                path = settings.data_dir / relative_path if relative_path else None
                if path is None or not path.is_file():
                    state.skipped += 1
                    continue

                # The cap is checked before the call that would cross it.
                try:
                    await spend.check_budget(
                        db, config.monthly_budget_usd, spend.estimate_call(model)
                    )
                except spend.BudgetExceeded as exc:
                    state.state = "budget"
                    state.message = str(exc)[:300]
                    log.warning("tag_review.budget_reached", spent=exc.budget.spent_usd)
                    break

                try:
                    preview = await asyncio.to_thread(tag_review.preview_from_file, path)
                except Exception as exc:  # noqa: BLE001 - a corrupt thumbnail is skipped, not fatal
                    state.skipped += 1
                    log.warning(
                        "tag_review.preview_failed", asset_id=str(asset_id), error=str(exc)[:200]
                    )
                    continue
                try:
                    answer = await asyncio.to_thread(
                        tag_review.ask_for_tags, preview, api_key, model, vocab
                    )
                except RecipePickUnavailable as exc:
                    await spend.record(
                        db,
                        purpose=spend.TAG_REVIEW,
                        model=model,
                        usage=None,
                        asset_id=asset_id,
                        ok=False,
                        note=str(exc),
                    )
                    await db.commit()
                    state.failed += 1
                    state.message = state.message or str(exc)[:300]
                    if exc.fatal:
                        # No credit, a bad key, a model this key cannot use:
                        # every remaining call fails the same way.
                        state.state = "failed"
                        break
                    continue

                ledger = await spend.record(
                    db, purpose=spend.TAG_REVIEW, model=model, usage=answer.usage, asset_id=asset_id
                )
                state.spent_usd = round(state.spent_usd + (ledger.cost_usd or 0.0), 6)
                admitted = tag_review.admit(answer.proposals, policy)
                outcomes = await tag_review.write_proposals(
                    db, asset_id, admitted, origin=tag_review.ORIGIN_CLAUDE
                )
                await db.commit()
                state.reviewed += 1
                for name, outcome in outcomes.items():
                    if outcome == tag_review.OUTCOME_NEW_TAG:
                        state.new_tags += 1
                        state.suggested += 1
                        # Later pictures in the run can match it by name.
                        vocab.append((name, 0))
                    elif outcome == tag_review.OUTCOME_SUGGESTED:
                        state.suggested += 1
                if state.reviewed % PROGRESS_EVERY == 0:
                    await save_review_state(db, state)

            if state.state == "running":
                state.state = "done"
            state.finished_at = _now()
            await save_review_state(db, state)
            log.info(
                "tag_review.finished",
                state=state.state,
                reviewed=state.reviewed,
                suggested=state.suggested,
                new_tags=state.new_tags,
                skipped=state.skipped,
                failed=state.failed,
                spent_usd=state.spent_usd,
            )
    finally:
        await engine.dispose()


@celery_app.task(name="framefound.review_tags_with_claude", queue="default")
def review_tags_with_claude(asset_ids: list[str]) -> None:
    """Review these assets with the Claude API. One run at a time; the
    endpoint that queues it refuses a second while one is live."""
    asyncio.run(_review(list(asset_ids)))


async def _sweep() -> None:
    engine = create_async_engine(get_settings().db_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            config = await load_ai_edit_config(db)
            if not (config.review_auto and config.ready):
                return
            current = await load_review_state(db)
            if current.state == "running":
                return  # last night's run is still going
            ids = await tag_review.pick_untagged(db, config.review_batch)
    finally:
        await engine.dispose()
    if ids:
        log.info("tag_review.sweep_queued", assets=len(ids))
        review_tags_with_claude.delay([str(asset_id) for asset_id in ids])


@celery_app.task(name="framefound.review_sweep", queue="default")
def review_sweep() -> None:
    """Nightly: hand the newest untagged media to the reviewer, when the
    operator has turned automatic review on. The budget still applies."""
    asyncio.run(_sweep())
