"""Send selects to Premiere: search hits as a bin with markers plus a string-out.

The arithmetic is in `nle/selects.py`; this module fetches what it needs —
the assets, their transcript segments, their sampled frames, the workstation
path profile — and is shared by three callers that must agree:

- `POST /api/v1/selects`, the web search page's *Send to Premiere*;
- `POST /api/v1/panel/export/selects`, for the Premiere panel (`export` scope);
- the MCP tool `build_selects`, which is how Claude drives it.

Each takes either the moments someone picked or a query to pick them from.
Nothing is rendered and nothing on the share is touched: the XML points at
the originals, at the paths the editing machine sees them by.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.auth.deps import CurrentUser, DbDep
from framefound.db.models import Asset, Frame, Library, Transcript, TranscriptSegment
from framefound.nle import selects as nle
from framefound.nle.fcp7 import Clip

log = structlog.get_logger()
router = APIRouter(prefix="/selects", tags=["selects"])

MAX_MOMENTS = 300
MAX_SEARCH_RESULTS = 60
Kind = Literal["speech", "visual", "tag", "name"]


class SelectsError(ValueError):
    """The request cannot become a selects reel; the message says why."""


class MomentIn(BaseModel):
    """One moment to select. Give `at_s` (where the hit is) and FrameFound
    finds the soundbite or the shot around it; or give `in_s`/`out_s` to
    choose the range yourself."""

    id: str = Field(description="Asset id")
    at_s: float | None = Field(default=None, ge=0, description="The hit, seconds into the source")
    in_s: float | None = Field(default=None, ge=0)
    out_s: float | None = Field(default=None, ge=0)
    kind: Kind | None = Field(
        default=None,
        description="speech: complete the sentence around at_s; visual/tag/name: the shot "
        "around at_s. Omitted: speech if words were said at at_s, else the shot.",
    )
    reason: str = Field(default="", max_length=500, description="Becomes the marker comment")


TITLE_MAX = 120


def short_title(raw: str, limit: int = TITLE_MAX) -> str:
    """A title that fits: cut at the last word before `limit`, with an
    ellipsis. The search page titles a reel with its query, which may be
    longer than a bin name should be."""
    text = " ".join(raw.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    head = cut.rsplit(" ", 1)[0] if " " in cut else cut
    return head.rstrip(" ,;:-–") + "…"


class SelectsRequest(BaseModel):
    title: str = Field(default="FrameFound", max_length=TITLE_MAX)
    profile: str = Field(default="", max_length=100)
    query: str = Field(default="", max_length=200)
    library_id: uuid.UUID | None = None
    moments: list[MomentIn] = Field(default_factory=list, max_length=MAX_MOMENTS)
    handles_s: float = Field(default=nle.DEFAULT_HANDLES_S, ge=0, le=10)
    max_moment_s: float = Field(default=nle.DEFAULT_MAX_PICTURE_S, ge=1, le=120)
    max_speech_s: float = Field(default=nle.DEFAULT_MAX_SPEECH_S, ge=2, le=120)
    still_s: float = Field(default=nle.DEFAULT_STILL_S, ge=0.5, le=60)
    order: nle.Order = "rank"
    limit: int = Field(default=25, ge=1, le=MAX_SEARCH_RESULTS)

    @field_validator("title", mode="before")
    @classmethod
    def _fit_title(cls, value: Any) -> Any:
        return short_title(value) if isinstance(value, str) else value


class SelectOut(BaseModel):
    name: str
    filename: str
    in_s: float
    out_s: float
    reason: str


class SelectsResult(BaseModel):
    title: str
    filename: str
    profile: str | None
    xml: str = Field(description="FCP7 XML: save as .xml and import into Premiere (or Resolve)")
    selects: list[SelectOut]
    clips: int
    duration_s: float
    unmapped: list[str] = Field(description="No path for this profile: they will import offline")
    offline: list[str] = Field(description="Not on the share now: left out")
    note: str


# --- choosing moments from a search -----------------------------------------------


async def moments_from_search(
    db: AsyncSession, query: str, library_id: uuid.UUID | None, limit: int
) -> list[MomentIn]:
    """The fused search's hits as moments, best first.

    A clip found by what was said gives every matching sentence; one found by
    what it shows gives the best frame's shot; a tag or a name with no frame
    gives the clip's opening shot. Audio-only files are not offered: a select
    is a picture on V1.
    """
    from framefound.api.v1.search import run_search

    found = await run_search(db, query, library_id, min(limit, MAX_SEARCH_RESULTS))
    spoken: dict[uuid.UUID, list[Any]] = {}
    for segment in found.transcript_hits:
        spoken.setdefault(segment.asset_id, []).append(segment)
    moments: list[MomentIn] = []
    for result in found.results:
        if result.media_type == "audio":
            continue
        aid = str(result.asset_id)
        if "transcript" in result.reasons and result.media_type == "video":
            for segment in spoken.get(result.asset_id, []):
                moments.append(MomentIn(id=aid, at_s=segment.start_ms / 1000, kind="speech"))
        elif "visual" in result.reasons:
            moments.append(
                MomentIn(
                    id=aid,
                    at_s=(result.frame_ms or 0) / 1000,
                    kind="visual",
                    reason=f"visual: {query}",
                )
            )
        elif result.tag:
            at = (result.frame_ms or 0) / 1000
            moments.append(MomentIn(id=aid, at_s=at, kind="tag", reason=f"tag: {result.tag}"))
        else:
            moments.append(MomentIn(id=aid, at_s=0.0, kind="name", reason=f"name: {query}"))
    return moments


# --- building --------------------------------------------------------------------


@dataclass
class _Loaded:
    asset: Asset
    library: Library


async def _load(db: AsyncSession, ids: list[str]) -> dict[str, _Loaded]:
    parsed: dict[str, uuid.UUID] = {}
    for raw in ids:
        try:
            parsed[raw] = uuid.UUID(raw)
        except ValueError as exc:
            raise SelectsError(f"{raw!r} is not an asset id") from exc
    rows = (
        await db.execute(
            select(Asset, Library)
            .join(Library, Library.id == Asset.library_id)
            .where(Asset.id.in_(set(parsed.values())))
        )
    ).all()
    by_id = {asset.id: _Loaded(asset, library) for asset, library in rows}
    missing = [raw for raw, aid in parsed.items() if aid not in by_id]
    if missing:
        raise SelectsError(f"No such asset: {', '.join(missing[:5])}")
    # A paired editing proxy never appears: its original does, at the same
    # moment — they are the same timeline.
    originals = {
        loaded.asset.proxy_of_id
        for loaded in by_id.values()
        if loaded.asset.proxy_of_id is not None and loaded.asset.proxy_of_id not in by_id
    }
    if originals:
        for asset, library in (
            await db.execute(
                select(Asset, Library)
                .join(Library, Library.id == Asset.library_id)
                .where(Asset.id.in_(originals))
            )
        ).all():
            by_id[asset.id] = _Loaded(asset, library)
    out: dict[str, _Loaded] = {}
    for raw, aid in parsed.items():
        loaded = by_id[aid]
        if loaded.asset.proxy_of_id is not None and loaded.asset.proxy_of_id in by_id:
            loaded = by_id[loaded.asset.proxy_of_id]
        out[raw] = loaded
    return out


async def _segments(
    db: AsyncSession, asset_ids: set[uuid.UUID]
) -> dict[uuid.UUID, list[nle.Segment]]:
    if not asset_ids:
        return {}
    rows = (
        await db.execute(
            select(Transcript.asset_id, TranscriptSegment)
            .join(TranscriptSegment, TranscriptSegment.transcript_id == Transcript.id)
            .where(Transcript.asset_id.in_(asset_ids))
            .order_by(Transcript.asset_id, TranscriptSegment.start_ms)
        )
    ).all()
    out: dict[uuid.UUID, list[nle.Segment]] = {}
    for asset_id, seg in rows:
        out.setdefault(asset_id, []).append(
            nle.Segment(seg.start_ms / 1000, seg.end_ms / 1000, seg.text)
        )
    return out


async def _scenes(
    db: AsyncSession, assets: dict[uuid.UUID, Asset]
) -> dict[uuid.UUID, tuple[list[tuple[float, float]], bool]]:
    if not assets:
        return {}
    rows = (
        await db.execute(
            select(Frame.asset_id, Frame.ts_ms, Frame.scene_number)
            .where(Frame.asset_id.in_(set(assets)))
            .order_by(Frame.asset_id, Frame.ts_ms)
        )
    ).all()
    sampled: dict[uuid.UUID, list[tuple[int, int | None]]] = {aid: [] for aid in assets}
    for asset_id, ts_ms, scene in rows:
        sampled[asset_id].append((int(ts_ms), scene))
    out = {}
    for asset_id, frames in sampled.items():
        groups, detected = nle.scene_groups(frames)
        out[asset_id] = (nle.scene_ranges(groups, assets[asset_id].duration_s), detected)
    return out


def _segment_at(segments: list[nle.Segment], at_s: float) -> int | None:
    """The segment being spoken at `at_s`, or the next to start within a
    second of it — a hit time is a segment's start, give or take rounding."""
    for index, seg in enumerate(segments):
        if seg.start_s <= at_s < seg.end_s:
            return index
    for index, seg in enumerate(segments):
        if 0 <= seg.start_s - at_s <= 1.0:
            return index
    return None


def _when(asset: Asset) -> datetime | None:
    """For ordering by capture: the camera's clock, else the shoot folder's date."""
    if asset.captured_at is not None:
        stamp = asset.captured_at
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
    day = nle.shoot_date(asset.relative_path)
    if day and len(day) == 10:
        return datetime.fromisoformat(day).replace(tzinfo=UTC)
    return None


async def assemble(db: AsyncSession, request: SelectsRequest) -> SelectsResult:
    """The whole hand-off: moments (given, or found by `query`), resolved to
    ranges, merged, ordered, named and written as one FCP7 document."""
    from framefound.api.v1.panel import _profiles, _translate_for

    wanted = list(request.moments)
    if not wanted:
        if not request.query.strip():
            raise SelectsError("Give moments to select, or a query to find them")
        wanted = await moments_from_search(
            db, request.query.strip(), request.library_id, request.limit
        )
        if not wanted:
            raise SelectsError(f"Nothing in the catalogue matched {request.query.strip()!r}")
    loaded = await _load(db, [m.id for m in wanted])

    offline: list[str] = []
    audio: list[str] = []
    usable: list[tuple[int, MomentIn, Asset]] = []
    for rank, moment in enumerate(wanted):
        asset = loaded[moment.id].asset
        if asset.availability != "online":
            if asset.filename not in offline:
                offline.append(asset.filename)
            continue
        if asset.media_type == "audio":
            if asset.filename not in audio:
                audio.append(asset.filename)
            continue
        usable.append((rank, moment, asset))
    if not usable:
        raise SelectsError(
            "None of those can be selected: "
            + (
                "; ".join(
                    x for x in (_listed("offline", offline), _listed("audio only", audio)) if x
                )
            )
        )

    videos = {asset.id: asset for _, _, asset in usable if asset.media_type == "video"}
    segments = await _segments(db, set(videos))
    scenes = await _scenes(db, videos)

    moments: list[nle.Moment] = []
    for rank, wanted_moment, asset in usable:
        moments.append(_resolve(rank, wanted_moment, asset, segments, scenes, request))

    merged = nle.merge(moments, request.handles_s)
    by_id = {str(asset.id): asset for _, _, asset in usable}
    library_of = {item.asset.id: item.library for item in loaded.values()}
    final = nle.ordered(merged, request.order, lambda aid: _when(by_id[aid]))

    profiles = await _profiles(db, request.profile)
    sources: dict[str, nle.Source] = {}
    unmapped: list[str] = []
    for aid in dict.fromkeys(m.asset_id for m in final):
        asset = by_id[aid]
        library = library_of[asset.id]
        path = _translate_for(asset, profiles) if profiles else None
        if path is None:
            path = f"{library.root_path.rstrip('/')}/{asset.relative_path}"
            unmapped.append(asset.filename)
        still = asset.media_type == "image"
        sources[aid] = nle.Source(
            asset_id=aid,
            clip=Clip(
                name=asset.filename,
                path=path,
                duration_s=None if still else asset.duration_s,
                fps=None if still else asset.fps,
                width=asset.width,
                height=asset.height,
                has_audio=not still and asset.audio_codec is not None,
            ),
            relative_path=asset.relative_path,
            captured_at=asset.captured_at,
            is_still=still,
        )

    title = request.title.strip() or "FrameFound"
    xml = nle.build_selects_xml(title, final, sources, handles_s=request.handles_s)
    selects = [
        SelectOut(
            name=nle.select_name(
                by_id[m.asset_id].relative_path, by_id[m.asset_id].captured_at, m.reason
            ),
            filename=by_id[m.asset_id].filename,
            in_s=round(m.in_s, 3),
            out_s=round(m.out_s, 3),
            reason=m.reason,
        )
        for m in final
    ]
    total = sum(m.out_s - m.in_s for m in final)
    note = (
        f"{len(final)} selects from {len(sources)} clips, {total:.1f}s before handles "
        f"({request.handles_s:g}s each side)."
    )
    if unmapped:
        note += (
            f" {len(unmapped)} file(s) have no path for "
            + (
                f"profile {request.profile.strip()!r}"
                if request.profile.strip()
                else "any profile (none given)"
            )
            + ": they carry the server's path and will import offline until relinked — "
            + ", ".join(unmapped[:10])
            + "."
        )
    if offline:
        note += f" Left out, not on the share now: {', '.join(offline[:10])}."
    if audio:
        note += f" Left out, audio only: {', '.join(audio[:10])}."
    log.info(
        "selects.built", selects=len(final), clips=len(sources), profile=request.profile or "none"
    )
    return SelectsResult(
        title=title,
        filename=nle.safe_filename(title),
        profile=request.profile.strip() or None,
        xml=xml,
        selects=selects,
        clips=len(sources),
        duration_s=round(total, 3),
        unmapped=unmapped,
        offline=offline,
        note=note,
    )


def _listed(what: str, names: list[str]) -> str:
    return f"{what}: {', '.join(names[:10])}" if names else ""


def _resolve(
    rank: int,
    wanted: MomentIn,
    asset: Asset,
    segments: dict[uuid.UUID, list[nle.Segment]],
    scenes: dict[uuid.UUID, tuple[list[tuple[float, float]], bool]],
    request: SelectsRequest,
) -> nle.Moment:
    aid = str(asset.id)
    if asset.media_type == "image":
        return nle.Moment(aid, 0.0, request.still_s, wanted.reason or "photo", rank, is_still=True)
    duration = asset.duration_s
    if wanted.in_s is not None and wanted.out_s is not None:
        out_s = min(wanted.out_s, duration) if duration else wanted.out_s
        if out_s <= wanted.in_s:
            raise SelectsError(f"{asset.filename}: out_s must be after in_s")
        at = wanted.at_s if wanted.at_s is not None else wanted.in_s
        return nle.Moment(aid, wanted.in_s, out_s, wanted.reason or "selected", rank, at_s=at)

    at_s = wanted.at_s or 0.0
    spoken = segments.get(asset.id, [])
    hit = _segment_at(spoken, at_s) if wanted.kind in ("speech", None) else None
    if hit is not None:
        start, end, text = nle.speech_span(spoken, hit, request.max_speech_s)
        return nle.Moment(aid, start, end, wanted.reason or text or "speech", rank, at_s=start)

    ranges, detected = scenes.get(asset.id, ([], False))
    start, end = nle.picture_span(at_s, ranges, detected, duration, request.max_moment_s)
    if end <= start:  # a clip of unknown length, hit at its very end
        end = start + min(request.max_moment_s, 2 * nle.NO_SCENES_HALF_S)
    default = {
        "visual": f"visual: {request.query.strip()}" if request.query.strip() else "visual",
        "tag": "tag",
        "name": "name",
        "speech": "speech",
    }.get(wanted.kind or "visual", "visual")
    return nle.Moment(aid, start, end, wanted.reason or default, rank, at_s=at_s)


# --- the web endpoint ----------------------------------------------------------------


@router.post("", response_model=SelectsResult)
async def send_selects(body: SelectsRequest, _user: CurrentUser, db: DbDep) -> SelectsResult:
    """Selects for Premiere from the web search page's chosen rows (or a query)."""
    try:
        return await assemble(db, body)
    except SelectsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
