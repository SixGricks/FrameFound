"""Slideshows: proposing a selection, then rendering it.

Two steps on purpose, and the split is the whole design.

**Propose** runs the selection over the library and hands back a list for the
operator to look at. Nothing is written. **Create** takes the list they
approved — possibly after removing a photograph or two — stores it, and queues
the render.

The alternative, a single "make me a slideshow" button, would produce a video
in which somebody's ex-husband features prominently and there would be no
moment at which that could have been caught. Selection is good enough to be
worth trusting and not good enough to be worth trusting blindly, which is the
same judgement the face review flow makes.

The stored row keeps the *resolved* asset list rather than the query, so
re-rendering next month reproduces the same video rather than quietly picking
up whatever has been added since.
"""

import asyncio
import uuid
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.sql.elements import ColumnElement

from framefound.auth.deps import CurrentUser, DbDep, SettingsDep, require_admin
from framefound.db.models import Asset, Face, Frame, Slideshow
from framefound.media import slideshow as selector
from framefound.media.streaming import range_file_response
from framefound.media.theming import THEMES, get_theme, score_against_theme

log = structlog.get_logger()

router = APIRouter(prefix="/slideshows", tags=["slideshows"])

# A slideshow of more than this is not a slideshow, and the render is minutes
# of CPU per hundred photographs. The cap is a kindness, not a limitation.
MAX_SLIDES = 300
# How many frames to consider. Selection is O(n^2) in the near-duplicate pass,
# and beyond this the extra candidates are not improving the result.
CANDIDATE_LIMIT = 4000


class ThemeOut(BaseModel):
    slug: str
    label: str
    hold_seconds: float
    transition_seconds: float
    accent: str


class ProposeRequest(BaseModel):
    theme: str = Field(default="plain", max_length=40)
    library_id: uuid.UUID | None = None
    captured_from: datetime | None = None
    captured_to: datetime | None = None
    target_count: int = Field(default=40, ge=1, le=MAX_SLIDES)
    required_people: list[uuid.UUID] = Field(default_factory=list)


class ProposedSlide(BaseModel):
    asset_id: uuid.UUID
    filename: str
    media_type: str
    captured_at: datetime | None
    theme_score: float
    people: list[uuid.UUID]
    has_preview: bool


class ProposeResponse(BaseModel):
    theme: str
    slides: list[ProposedSlide]
    considered: int
    dropped_duplicates: int
    people_covered: list[uuid.UUID]
    people_missing: list[uuid.UUID]
    note: str


class CreateRequest(BaseModel):
    title: str = Field(default="", max_length=200)
    theme: str = Field(default="plain", max_length=40)
    asset_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_SLIDES)
    hold_seconds: float | None = Field(default=None, gt=0.2, le=30)
    transition_seconds: float | None = Field(default=None, ge=0, le=5)
    width: int = Field(default=1920, ge=320, le=3840)
    height: int = Field(default=1080, ge=240, le=2160)
    fps: int = Field(default=30, ge=24, le=60)
    # Relative to the data directory. The operator supplies their own licensed
    # tracks; FrameFound ships no music and will not fetch any.
    audio_relpath: str = Field(default="", max_length=512)


class SlideshowOut(BaseModel):
    id: uuid.UUID
    title: str
    theme: str
    status: str
    slide_count: int
    segments_done: int
    duration_seconds: float | None
    size_mb: float | None
    error: str | None
    created_at: datetime
    video_url: str | None


def _as_out(show: Slideshow) -> SlideshowOut:
    return SlideshowOut(
        id=show.id,
        title=show.title,
        theme=show.theme,
        status=show.status,
        slide_count=len(show.asset_ids or []),
        segments_done=show.segments_done,
        duration_seconds=show.duration_seconds,
        size_mb=round(show.size_bytes / 1024**2, 1) if show.size_bytes else None,
        error=show.error,
        created_at=show.created_at,
        video_url=f"/api/v1/slideshows/{show.id}/video" if show.status == "ready" else None,
    )


@router.get("/themes", response_model=list[ThemeOut])
async def list_themes(_user: CurrentUser) -> list[ThemeOut]:
    """The available looks. Data rather than code, so a church running a
    different VBS next summer can have one without a deploy."""
    return [
        ThemeOut(
            slug=theme.slug,
            label=theme.label,
            hold_seconds=theme.hold_seconds,
            transition_seconds=theme.transition_seconds,
            accent=theme.accent,
        )
        for theme in THEMES.values()
    ]


@router.post("/propose", response_model=ProposeResponse)
async def propose(body: ProposeRequest, _user: CurrentUser, db: DbDep) -> ProposeResponse:
    """Choose photographs for a slideshow, without committing to anything.

    Scoring against the theme needs CLIP text vectors for the theme's prompts.
    When embeddings are unavailable the selection still works — it falls back
    to chronological with near-duplicates collapsed, which is a perfectly good
    slideshow and much better than an error.
    """
    theme = get_theme(body.theme)

    conditions: list[ColumnElement[bool]] = [Frame.embedding.is_not(None)]
    if body.library_id is not None:
        conditions.append(Asset.library_id == body.library_id)
    if body.captured_from is not None:
        conditions.append(Asset.captured_at >= body.captured_from)
    if body.captured_to is not None:
        conditions.append(Asset.captured_at <= body.captured_to)

    # One frame per asset: a slideshow shows photographs, and a video
    # contributing forty sampled frames would swamp it.
    rows = (
        await db.execute(
            select(Frame, Asset)
            .join(Asset, Asset.id == Frame.asset_id)
            .where(*conditions, Frame.ts_ms == 0)
            .order_by(Asset.captured_at.asc().nullslast())
            .limit(CANDIDATE_LIMIT)
        )
    ).all()
    if not rows:
        raise HTTPException(
            status_code=400,
            detail="No photographs with visual indexing yet match that selection",
        )

    asset_ids = [row[1].id for row in rows]
    people_by_asset = await _people_for(db, asset_ids)
    previews = await _preview_assets(db, asset_ids)

    positive, negative = await _theme_vectors(theme)
    candidates = [
        selector.Candidate(
            asset_id=str(asset.id),
            captured_at=asset.captured_at,
            embedding=frame.embedding,
            theme_score=score_against_theme(frame.embedding, positive, negative),
            # Without a sharpness measure every frame ties, and selection falls
            # through to theme score then capture order. A real sharpness pass
            # would improve this; ranking on a number we do not have would not.
            sharpness=1.0,
            person_ids=[str(p) for p in people_by_asset.get(asset.id, [])],
        )
        for frame, asset in rows
    ]

    selection = selector.select(
        candidates,
        target_count=body.target_count,
        required_people=[str(p) for p in body.required_people],
        themed=bool(positive),
    )

    by_id = {str(row[1].id): row[1] for row in rows}
    slides = [
        ProposedSlide(
            asset_id=uuid.UUID(chosen.asset_id),
            filename=by_id[chosen.asset_id].filename,
            media_type=by_id[chosen.asset_id].media_type,
            captured_at=chosen.captured_at,
            theme_score=chosen.theme_score,
            people=[uuid.UUID(p) for p in chosen.person_ids],
            has_preview=uuid.UUID(chosen.asset_id) in previews,
        )
        for chosen in selection.chosen
    ]

    note = (
        f"{len(slides)} chosen from {len(candidates)} photographs."
        if positive
        else f"{len(slides)} chosen from {len(candidates)}, in date order — "
        "no theme scoring was applied."
    )
    missing_previews = sum(1 for s in slides if not s.has_preview)
    if missing_previews:
        note += f" {missing_previews} have no preview image yet and cannot be rendered."

    return ProposeResponse(
        theme=theme.slug,
        slides=slides,
        considered=len(candidates),
        dropped_duplicates=selection.dropped_duplicates,
        people_covered=[uuid.UUID(p) for p in selection.people_covered],
        people_missing=[uuid.UUID(p) for p in selection.people_missing],
        note=note,
    )


async def _theme_vectors(theme: Any) -> tuple[list[list[float]], list[list[float]]]:
    """CLIP text vectors for a theme's prompts, or nothing if unavailable.

    An absent embedding provider means an unthemed slideshow, not a failure.
    """
    if not theme.prompts:
        return [], []
    try:
        from framefound.ai.embeddings import get_embedding_provider

        provider = get_embedding_provider()
        positive = [(await asyncio.to_thread(provider.embed_text, p)).vector for p in theme.prompts]
        negative = [
            (await asyncio.to_thread(provider.embed_text, p)).vector for p in theme.negative_prompts
        ]
        return positive, negative
    except Exception as exc:  # provider missing, model not downloaded, ...
        log.warning("slideshow.theme_scoring_unavailable", error=str(exc)[:200])
        return [], []


async def _people_for(db: DbDep, asset_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[uuid.UUID]]:
    """Who appears in each asset, counting only judgements that stand.

    A rejected face keeps its person_id — the pair is the judgement — so
    filtering on source matters here or a slideshow would "cover" somebody the
    operator explicitly said was not them.
    """
    if not asset_ids:
        return {}
    rows = (
        await db.execute(
            select(Face.asset_id, Face.person_id).where(
                Face.asset_id.in_(asset_ids),
                Face.person_id.is_not(None),
                Face.source != "rejected",
            )
        )
    ).all()
    out: dict[uuid.UUID, list[uuid.UUID]] = {}
    for asset_id, person_id in rows:
        out.setdefault(asset_id, []).append(person_id)
    return out


async def _preview_assets(db: DbDep, asset_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    from framefound.db.models import Derivative

    if not asset_ids:
        return set()
    rows = (
        await db.execute(
            select(Derivative.asset_id).where(
                Derivative.asset_id.in_(asset_ids),
                Derivative.kind == "preview",
                Derivative.status == "ready",
            )
        )
    ).all()
    return {row[0] for row in rows}


@router.post("", response_model=SlideshowOut, status_code=202)
async def create(body: CreateRequest, _user: CurrentUser, db: DbDep) -> SlideshowOut:
    """Store an approved selection and queue the render."""
    theme = get_theme(body.theme)
    hold = body.hold_seconds if body.hold_seconds is not None else theme.hold_seconds
    transition = (
        body.transition_seconds if body.transition_seconds is not None else theme.transition_seconds
    )
    # Caught here rather than in the worker, where it would be a failed render
    # several minutes later. A middle slide gives up a transition at each end.
    if len(body.asset_ids) > 1 and hold <= transition * 2:
        raise HTTPException(
            status_code=400,
            detail=(
                f"A {hold:g}s hold cannot fit a {transition:g}s transition at each end. "
                f"Hold each photo longer than {transition * 2:g}s, or shorten the transition."
            ),
        )

    count_query = select(func.count()).select_from(Asset).where(Asset.id.in_(body.asset_ids))
    present = (await db.execute(count_query)).scalar_one()
    if present != len(set(body.asset_ids)):
        raise HTTPException(status_code=400, detail="Some of those photographs no longer exist")

    show = Slideshow(
        title=body.title.strip() or f"{theme.label} slideshow",
        theme=theme.slug,
        status="pending",
        asset_ids=[str(a) for a in body.asset_ids],
        settings={
            "hold_seconds": hold,
            "transition_seconds": transition,
            "width": body.width,
            "height": body.height,
            "fps": body.fps,
            "audio_relpath": body.audio_relpath.strip(),
        },
    )
    db.add(show)
    await db.commit()
    await _queue(show)
    return _as_out(show)


async def _queue(show: Slideshow) -> None:
    try:
        from framefound.processing.tasks import render_slideshow

        render_slideshow.delay(str(show.id))
    except Exception:
        raise HTTPException(status_code=503, detail="The processing queue is unavailable") from None
    log.info("slideshow.queued", slideshow_id=str(show.id), slides=len(show.asset_ids))


@router.get("", response_model=list[SlideshowOut])
async def list_slideshows(_user: CurrentUser, db: DbDep) -> list[SlideshowOut]:
    rows = (
        (await db.execute(select(Slideshow).order_by(Slideshow.created_at.desc()).limit(200)))
        .scalars()
        .all()
    )
    return [_as_out(show) for show in rows]


@router.get("/{slideshow_id}", response_model=SlideshowOut)
async def get_slideshow(slideshow_id: uuid.UUID, _user: CurrentUser, db: DbDep) -> SlideshowOut:
    show = await db.get(Slideshow, slideshow_id)
    if show is None:
        raise HTTPException(status_code=404, detail="No such slideshow")
    return _as_out(show)


@router.post("/{slideshow_id}/render", response_model=SlideshowOut, status_code=202)
async def rerender(slideshow_id: uuid.UUID, _user: CurrentUser, db: DbDep) -> SlideshowOut:
    """Render again — after a failure, or after changing the pacing.

    Reproduces the same video: the selection is stored, so a library that has
    grown since does not change what is in it.
    """
    show = await db.get(Slideshow, slideshow_id)
    if show is None:
        raise HTTPException(status_code=404, detail="No such slideshow")
    if show.status == "rendering":
        raise HTTPException(status_code=409, detail="That slideshow is already rendering")
    show.status = "pending"
    show.error = None
    show.segments_done = 0
    await db.commit()
    await _queue(show)
    return _as_out(show)


@router.get("/{slideshow_id}/video")
async def stream_video(  # type: ignore[no-untyped-def]
    slideshow_id: uuid.UUID, request: Request, _user: CurrentUser, db: DbDep, settings: SettingsDep
):
    """Serve the rendered file with range support, so it scrubs in a browser."""
    show = await db.get(Slideshow, slideshow_id)
    if show is None or not show.relative_path:
        raise HTTPException(status_code=404, detail="No such slideshow")
    path = settings.data_dir / show.relative_path
    if not path.is_file():
        raise HTTPException(status_code=404, detail="The rendered file is no longer on disk")
    return range_file_response(request, path, "video/mp4")


@router.delete("/{slideshow_id}", status_code=204, dependencies=[require_admin])
async def delete_slideshow(
    slideshow_id: uuid.UUID, _user: CurrentUser, db: DbDep, settings: SettingsDep
) -> None:
    """Delete the row and the rendered file.

    A genuine delete: the video is reproducible from the selection, and keeping
    tombstones of things the operator threw away helps nobody. The originals
    are untouched, as always.
    """
    show = await db.get(Slideshow, slideshow_id)
    if show is None:
        return
    if show.relative_path:
        (settings.data_dir / show.relative_path).unlink(missing_ok=True)
    import shutil

    shutil.rmtree(settings.data_dir / "renders" / "work" / str(show.id), ignore_errors=True)
    await db.execute(delete(Slideshow).where(Slideshow.id == slideshow_id))
    await db.commit()
    log.info("slideshow.deleted", slideshow_id=str(slideshow_id))
