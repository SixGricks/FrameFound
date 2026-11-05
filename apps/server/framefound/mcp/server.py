"""The tools Claude gets when it connects to FrameFound.

Designed around what the operator actually asks for: "find me the drone
shots of the Ledgerock barn", "what have we got tagged power broom?",
"look at these and tell me what they are". So the surface is small —
find by tag, search by words, look at a picture, propose and decide tags,
and ask the server-side reviewer to work through a batch under the
budget — and every result carries a link the caller can open.

Two rules the tools keep whatever Claude asks:

- Nothing writes to an original. The connector reads the catalogue and
  the derived previews; the library is read-only to it, as to everything.
- A tag Claude proposes is a *suggestion*. It enters the same review
  queue as the learner's guesses, marked with where it came from, and it
  becomes an example only when the operator says yes — here, through
  `decide_tag`, or on the Tags page.

Authentication and the per-request database session come from
framefound/mcp/auth.py; this module never sees a token.
"""

import asyncio
import io
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from framefound import __version__
from framefound.ai import spend, tag_review
from framefound.ai.tagging import slugify
from framefound.config import get_settings
from framefound.db.models import (
    Asset,
    AssetTag,
    Derivative,
    Frame,
    Library,
    Tag,
    Transcript,
    TranscriptSegment,
)
from framefound.mcp.auth import Principal, open_session, principal
from framefound.media.signing import SigningError, sign_media_url

log = structlog.get_logger()

# Long enough to outlast a conversation; the same horizon the panels get.
MEDIA_TTL_S = 12 * 3600
MAX_RESULTS = 100
PREVIEW_EDGE = 768

INSTRUCTIONS = """FrameFound is a self-hosted catalogue of this business's photographs, video and
audio: auction and real-estate shoots, drone work, church services. Originals never
change; the catalogue holds metadata, previews and tags.

Tags come in two strengths. A *confirmed* tag is one the operator applied or agreed
to. A *suggested* tag is a guess awaiting their yes or no — from the CLIP learner,
from the server-side Claude reviewer, or from you. Treat suggestions as claims.

To look at a picture, call view_media; thumbnails are small but enough to name a
subject. When you propose tags, prefer the existing vocabulary (list_tags) and add
new words only for a prominent, nameable subject someone would search for later.
Proposals are queued for the operator; nothing you say becomes a fact until they
decide, and decide_tag records their answer when they give it to you in chat.

Making a video from the footage: search_media (media_type "video") finds clips by
what they show, what was said and their tags; get_transcript gives every timed cue
so a quote is cut on its words; get_scenes gives each shot's start and end. To see
a moment of a video — a scene's frame_ms, or a search hit's at_ms — call
view_media(id, ts_ms=...); it returns the nearest sampled frame as a picture. The
signed links in results are for the person to open; you cannot fetch them.
get_media_files gives the original's path on the person's computer and a 1080p
proxy to download. build_rough_cut lays chosen pieces end to end as an FCP7 XML
timeline that Premiere Pro and DaVinci Resolve import (File > Import, or Import >
Timeline in Resolve). Final Cut Pro 10/11 cannot open it without a converter to
FCPXML such as 7toX — say so if the person edits in Final Cut. Paths depend on the
computer: call path_profiles and pass the person's profile (their Windows PC or
their Mac) so the paths and the timeline open there. Originals are never changed or
downloaded."""


# --- shapes ------------------------------------------------------------------


class LibraryOut(BaseModel):
    id: str
    name: str
    assets: int


class TagOut(BaseModel):
    name: str
    slug: str
    tagged: int = Field(description="Items the operator confirmed it on")
    pending: int = Field(description="Suggestions awaiting their judgement")
    learned: bool = Field(description="Whether the CLIP learner has a prototype for it yet")


class MediaItem(BaseModel):
    id: str
    filename: str
    media_type: str
    library: str
    captured_at: str | None
    duration_s: float | None
    width: int | None
    height: int | None
    tags: list[str] = Field(description="Confirmed tags")
    suggested: list[str] = Field(description="Tags suggested but not yet judged")
    preview_url: str | None = Field(description="Signed link to the thumbnail, valid 12 hours")
    path: str = Field(description="Where the original lives, as the server sees it")
    local_path: str | None = Field(
        default=None,
        description="The original's path on the computer named by `profile`, when given",
    )


class MediaPage(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[MediaItem]


class SearchHit(MediaItem):
    matched: list[str] = Field(description="How it matched: tag, visual, transcript, name")
    transcript: str | None = None
    at_ms: int | None = Field(
        default=None,
        description=(
            "Where in a video or recording the match is (never set for a photo); "
            "for a video, view_media(id, ts_ms=at_ms) shows it"
        ),
    )
    similarity: float | None = None


class SearchResult(BaseModel):
    query: str
    hits: list[SearchHit]
    visual_search_available: bool


class MediaDetail(MediaItem):
    camera: str | None
    lens: str | None
    gps: list[float] | None = Field(description="[lat, lon] when known")
    title: str | None
    description: str | None
    rating: int | None
    transcript_excerpt: str | None
    urls: dict[str, str] = Field(description="Signed links per ready derivative")


class QueueItem(BaseModel):
    asset_id: str
    filename: str
    media_type: str
    tag: str
    confidence: float | None
    origin: str = Field(description="clip, claude or mcp")
    preview_url: str | None


class ProposalOutcome(BaseModel):
    tag: str
    outcome: str


class ProposeResult(BaseModel):
    asset_id: str
    outcomes: list[ProposalOutcome]
    note: str


class DecideResult(BaseModel):
    asset_id: str
    tag: str
    source: str
    note: str


class ReviewStatus(BaseModel):
    configured: bool
    model: str
    budget_usd: float
    spent_this_month_usd: float
    remaining_usd: float
    untagged: int = Field(
        description=(
            "Online pictures with a thumbnail that nobody has judged yet: no tag or answer of "
            "yours, no suggestion from Claude, not already reviewed by the API. The learner's "
            "guesses do not count."
        )
    )
    last_run: dict[str, Any]


class ReviewStarted(BaseModel):
    queued: int
    message: str


class Overview(BaseModel):
    server_version: str
    connected_as: str
    scopes: list[str]
    can_tag: bool
    libraries: list[LibraryOut]
    tags: int
    pending_suggestions: int
    untagged: int


# --- helpers -----------------------------------------------------------------


def _media_url(base: str, asset_id: uuid.UUID, kind: str) -> str | None:
    try:
        expires, sig = sign_media_url(get_settings().secret_key, asset_id, kind, MEDIA_TTL_S)
    except SigningError:
        return None
    return f"{base}/api/v1/media/{asset_id}/{kind}?exp={expires}&sig={sig}"


async def _libraries(db: AsyncSession) -> dict[uuid.UUID, Library]:
    rows = (await db.execute(select(Library))).scalars().all()
    return {library.id: library for library in rows}


async def _library_id(db: AsyncSession, library: str | None) -> uuid.UUID | None:
    """A library by id or by name; None means all of them."""
    if not library or not library.strip():
        return None
    wanted = library.strip()
    try:
        candidate = uuid.UUID(wanted)
    except ValueError:
        candidate = None
    stmt = select(Library.id).where(
        (Library.id == candidate) if candidate else func.lower(Library.name) == wanted.lower()
    )
    found = (await db.execute(stmt)).scalar_one_or_none()
    if found is None:
        names = ", ".join(sorted(lib.name for lib in (await _libraries(db)).values()))
        raise ToolError(f"No library called {wanted!r}. Libraries: {names or 'none'}")
    return uuid.UUID(str(found))


async def _tags_on(
    db: AsyncSession, asset_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[list[str], list[str]]]:
    """(confirmed, suggested) tag names per asset, in one query."""
    out: dict[uuid.UUID, tuple[list[str], list[str]]] = {aid: ([], []) for aid in asset_ids}
    if not asset_ids:
        return out
    rows = (
        await db.execute(
            select(AssetTag.asset_id, AssetTag.source, Tag.name)
            .join(Tag, Tag.id == AssetTag.tag_id)
            .where(AssetTag.asset_id.in_(asset_ids), AssetTag.source != "rejected")
            .order_by(Tag.name)
        )
    ).all()
    for asset_id, source, name in rows:
        confirmed, suggested = out.setdefault(uuid.UUID(str(asset_id)), ([], []))
        (suggested if source == "suggested" else confirmed).append(str(name))
    return out


def _item(
    asset: Asset,
    libraries: dict[uuid.UUID, Library],
    tags: dict[uuid.UUID, tuple[list[str], list[str]]],
    base: str,
    profiles: dict[uuid.UUID, tuple[Any, Any]] | None = None,
) -> MediaItem:
    from framefound.api.v1.panel import _translate_for

    library = libraries.get(asset.library_id)
    confirmed, suggested = tags.get(asset.id, ([], []))
    root = library.root_path.rstrip("/") if library else ""
    return MediaItem(
        id=str(asset.id),
        filename=asset.filename,
        media_type=asset.media_type,
        library=library.name if library else "",
        captured_at=asset.captured_at.isoformat() if asset.captured_at else None,
        duration_s=asset.duration_s,
        width=asset.width,
        height=asset.height,
        tags=confirmed,
        suggested=suggested,
        preview_url=_media_url(base, asset.id, "thumbnail"),
        path=f"{root}/{asset.relative_path}",
        local_path=_translate_for(asset, profiles) if profiles else None,
    )


async def _asset(db: AsyncSession, asset_id: str) -> Asset:
    try:
        key = uuid.UUID(asset_id)
    except ValueError:
        raise ToolError(f"{asset_id!r} is not an asset id") from None
    asset = await db.get(Asset, key)
    if asset is None:
        raise ToolError(f"No asset {asset_id}")
    return asset


async def _tag_by_name(db: AsyncSession, name: str) -> Tag | None:
    slug = slugify(name)
    if not slug:
        return None
    return (await db.execute(select(Tag).where(Tag.slug == slug))).scalar_one_or_none()


async def _profile_map(db: AsyncSession, profile: str | None) -> dict[uuid.UUID, tuple[Any, Any]]:
    from framefound.mcp.video import _profile_map as resolve

    return await resolve(db, profile)


def _require_tag_scope(who: Principal) -> None:
    if not who.can_tag:
        raise ToolError(
            "This token cannot write tags. Create one on the Security page with the "
            "'tag' scope (and not for a read-only account) to propose or decide tags."
        )


def _relearn(tag_id: uuid.UUID) -> None:
    from framefound.api.v1.tags import _relearn as queue_relearn

    queue_relearn(tag_id)


# --- the server --------------------------------------------------------------


def build_server() -> MCPServer:
    server = MCPServer(
        name="FrameFound",
        instructions=INSTRUCTIONS,
        version=__version__,
    )

    @server.tool()
    async def overview() -> Overview:
        """What this FrameFound holds, and what this connection may do. Call
        it first: it names the libraries and says whether tagging is allowed."""
        who = principal()
        async with open_session() as db:
            libraries = await _libraries(db)
            counts: dict[uuid.UUID, int] = {
                uuid.UUID(str(library_id)): int(n)
                for library_id, n in (
                    await db.execute(
                        select(Asset.library_id, func.count()).group_by(Asset.library_id)
                    )
                ).all()
            }
            tags = int((await db.execute(select(func.count()).select_from(Tag))).scalar_one())
            pending = int(
                (
                    await db.execute(
                        select(func.count())
                        .select_from(AssetTag)
                        .where(AssetTag.source == "suggested")
                    )
                ).scalar_one()
            )
            untagged = await tag_review.count_untagged(db)
        return Overview(
            server_version=__version__,
            connected_as=f"{who.token_name} ({who.email})",
            scopes=sorted(who.scopes),
            can_tag=who.can_tag,
            libraries=[
                LibraryOut(id=str(lib.id), name=lib.name, assets=int(counts.get(lib.id, 0)))
                for lib in sorted(libraries.values(), key=lambda lib: lib.name.lower())
            ],
            tags=tags,
            pending_suggestions=pending,
            untagged=untagged,
        )

    @server.tool()
    async def list_tags(query: str = "", limit: int = 200) -> list[TagOut]:
        """The tag vocabulary with counts. `query` narrows by name. Use these
        names (or their slugs) with find_media and propose_tags."""
        principal()
        limit = max(1, min(limit, 1000))
        async with open_session() as db:
            stmt = select(Tag).order_by(Tag.name)
            term = query.strip().lower()
            if term:
                stmt = stmt.where(
                    func.lower(Tag.name).contains(term) | Tag.slug.contains(slugify(term))
                )
            tags = (await db.execute(stmt.limit(limit))).scalars().all()
            confirmed: dict[uuid.UUID, int] = {
                uuid.UUID(str(tag_id)): int(n)
                for tag_id, n in (
                    await db.execute(
                        select(AssetTag.tag_id, func.count())
                        .where(AssetTag.source.in_(("manual", "confirmed")))
                        .group_by(AssetTag.tag_id)
                    )
                ).all()
            }
            pending: dict[uuid.UUID, int] = {
                uuid.UUID(str(tag_id)): int(n)
                for tag_id, n in (
                    await db.execute(
                        select(AssetTag.tag_id, func.count())
                        .where(AssetTag.source == "suggested")
                        .group_by(AssetTag.tag_id)
                    )
                ).all()
            }
        return [
            TagOut(
                name=tag.name,
                slug=tag.slug,
                tagged=int(confirmed.get(tag.id, 0)),
                pending=int(pending.get(tag.id, 0)),
                learned=tag.prototype is not None,
            )
            for tag in tags
        ]

    @server.tool()
    async def find_media(
        tags: list[str] | None = None,
        match: Literal["all", "any"] = "all",
        media_type: Literal["image", "video", "audio"] | None = None,
        library: str | None = None,
        include_suggested: bool = False,
        captured_after: str | None = None,
        captured_before: str | None = None,
        limit: int = 25,
        page: int = 1,
        profile: str | None = None,
    ) -> MediaPage:
        """Media carrying tags. `match` "all" needs every tag, "any" needs one.
        Confirmed tags only unless `include_suggested`. No tags: the newest
        media, filtered by type, library and capture dates (ISO, e.g.
        2026-09-01). `library` is a name or id. `profile` (see path_profiles)
        adds each original's path on that computer."""
        who = principal()
        limit = max(1, min(limit, MAX_RESULTS))
        page = max(1, page)
        wanted_sources = (
            ("manual", "confirmed", "suggested") if include_suggested else ("manual", "confirmed")
        )
        async with open_session() as db:
            library_id = await _library_id(db, library)
            query = select(Asset).where(Asset.availability == "online")
            if library_id is not None:
                query = query.where(Asset.library_id == library_id)
            if media_type:
                query = query.where(Asset.media_type == media_type)
            for bound, is_after in ((captured_after, True), (captured_before, False)):
                if bound:
                    try:
                        when = datetime.fromisoformat(bound)
                    except ValueError:
                        raise ToolError(f"{bound!r} is not an ISO date") from None
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=UTC)
                    query = query.where(
                        Asset.captured_at >= when if is_after else Asset.captured_at <= when
                    )

            wanted_tags = [name for name in (tags or []) if name.strip()]
            if wanted_tags:
                resolved: list[Tag] = []
                unknown: list[str] = []
                for name in wanted_tags:
                    tag = await _tag_by_name(db, name)
                    if tag is None:
                        unknown.append(name)
                    else:
                        resolved.append(tag)
                if unknown:
                    raise ToolError(
                        f"No tag called {', '.join(repr(n) for n in unknown)}. "
                        "list_tags shows the vocabulary."
                    )
                if match == "all":
                    for tag in resolved:
                        query = query.where(
                            Asset.id.in_(
                                select(AssetTag.asset_id).where(
                                    AssetTag.tag_id == tag.id, AssetTag.source.in_(wanted_sources)
                                )
                            )
                        )
                else:
                    query = query.where(
                        Asset.id.in_(
                            select(AssetTag.asset_id).where(
                                AssetTag.tag_id.in_([tag.id for tag in resolved]),
                                AssetTag.source.in_(wanted_sources),
                            )
                        )
                    )

            total = int(
                (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
            )
            rows = (
                (
                    await db.execute(
                        query.order_by(
                            Asset.captured_at.desc().nullslast(),
                            Asset.first_indexed_at.desc(),
                            Asset.id,
                        )
                        .offset((page - 1) * limit)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            libraries = await _libraries(db)
            on = await _tags_on(db, [asset.id for asset in rows])
            profiles = await _profile_map(db, profile)
        return MediaPage(
            total=total,
            page=page,
            page_size=limit,
            items=[_item(asset, libraries, on, who.base_url, profiles) for asset in rows],
        )

    @server.tool()
    async def search_media(
        query: str,
        library: str | None = None,
        limit: int = 20,
        media_type: Literal["image", "video", "audio"] | None = None,
        profile: str | None = None,
    ) -> SearchResult:
        """Search by words: tags, what the pictures look like (visual search),
        what was said (transcripts) and file names, ranked into one list.
        A `matched` of "visual" is CLIP's opinion; "tag" is the operator's.
        `at_ms` is the best moment in a video or recording; for a video, look
        at it with view_media(id, ts_ms=at_ms). `media_type` narrows to photos, video
        or audio; `profile` (see path_profiles) adds local paths."""
        from framefound.api.v1.search import run_search

        who = principal()
        limit = max(1, min(limit, MAX_RESULTS))
        term = query.strip()
        if len(term) < 2:
            raise ToolError("Give at least two characters to search for")
        async with open_session() as db:
            library_id = await _library_id(db, library)
            found = await run_search(db, term[:200], library_id, limit, media_type=media_type)
            ordered: list[uuid.UUID] = []
            matched: dict[uuid.UUID, list[str]] = {}
            extra: dict[uuid.UUID, dict[str, Any]] = {}

            def note(asset_id: uuid.UUID, how: str, **fields: Any) -> None:
                if asset_id not in matched:
                    ordered.append(asset_id)
                    matched[asset_id] = []
                if how not in matched[asset_id]:
                    matched[asset_id].append(how)
                for key, value in fields.items():
                    extra.setdefault(asset_id, {}).setdefault(key, value)

            # The fused ranking first: an asset matched by tag, picture and
            # speech outranks one that tops a single list. The per-kind
            # lists below only fill in details fusion did not carry.
            for fused in found.results:
                for reason in fused.reasons:
                    how = "suggested tag" if reason == "suggested_tag" else reason
                    note(fused.asset_id, how, at_ms=fused.ts_ms, transcript=fused.snippet)
            for hit in found.tag_hits:
                note(hit.asset_id, "tag" if hit.confirmed else "suggested tag")
            for visual in found.visual_hits:
                note(visual.asset_id, "visual", at_ms=visual.ts_ms, similarity=visual.similarity)
            for segment in found.transcript_hits:
                note(
                    segment.asset_id, "transcript", transcript=segment.text, at_ms=segment.start_ms
                )
            for named in found.filename_hits:
                note(named.asset_id, "name")
            ordered = ordered[:limit]
            assets = {
                asset.id: asset
                for asset in (
                    await db.execute(select(Asset).where(Asset.id.in_(ordered)))
                ).scalars()
            }
            libraries = await _libraries(db)
            on = await _tags_on(db, ordered)
            profiles = await _profile_map(db, profile)
        hits = []
        for asset_id in ordered:
            asset = assets.get(asset_id)
            if asset is None:
                continue
            base = _item(asset, libraries, on, who.base_url, profiles).model_dump()
            fields = extra.get(asset_id, {})
            # A photo's one Frame row sits at ts_ms 0, so a visual hit on it
            # carried at_ms 0 — and Claude, told to pass at_ms to view_media,
            # got "ts_ms is for a moment in a video" and had to retry. A photo
            # has no moment to point at.
            at_ms = fields.get("at_ms") if asset.media_type in ("video", "audio") else None
            hits.append(
                SearchHit(
                    **base,
                    matched=matched[asset_id],
                    transcript=fields.get("transcript"),
                    at_ms=at_ms,
                    similarity=fields.get("similarity"),
                )
            )
        return SearchResult(query=term, hits=hits, visual_search_available=found.visual_available)

    @server.tool()
    async def get_media(id: str) -> MediaDetail:
        """Everything the catalogue knows about one item: capture details,
        tags with their standing, a transcript excerpt, signed links."""
        who = principal()
        async with open_session() as db:
            asset = await _asset(db, id)
            libraries = await _libraries(db)
            on = await _tags_on(db, [asset.id])
            kinds = (
                (
                    await db.execute(
                        select(Derivative.kind).where(
                            Derivative.asset_id == asset.id, Derivative.status == "ready"
                        )
                    )
                )
                .scalars()
                .all()
            )
            segments = (
                (
                    await db.execute(
                        select(TranscriptSegment.text)
                        .join(Transcript, Transcript.id == TranscriptSegment.transcript_id)
                        .where(Transcript.asset_id == asset.id)
                        .order_by(TranscriptSegment.start_ms)
                        .limit(12)
                    )
                )
                .scalars()
                .all()
            )
        base = _item(asset, libraries, on, who.base_url).model_dump()
        excerpt = " ".join(str(text) for text in segments).strip() or None
        urls = {
            kind: url
            for kind in kinds
            if (url := _media_url(who.base_url, asset.id, str(kind))) is not None
        }
        return MediaDetail(
            **base,
            camera=" ".join(part for part in (asset.camera_make, asset.camera_model) if part)
            or None,
            lens=asset.lens,
            gps=[asset.gps_lat, asset.gps_lon]
            if asset.gps_lat is not None and asset.gps_lon is not None
            else None,
            title=asset.title,
            description=asset.description,
            rating=asset.rating,
            transcript_excerpt=excerpt[:1500] if excerpt else None,
            urls=urls,
        )

    @server.tool(structured_output=False)
    async def view_media(
        id: str,
        kind: Literal["thumbnail", "preview", "poster"] = "thumbnail",
        ts_ms: int | None = None,
    ) -> list[Any]:
        """Look at the picture (or a video's poster frame). Returns the image
        itself, small, plus what the catalogue already says about it. Audio
        has nothing to look at.

        For a moment inside a video, pass `ts_ms` (milliseconds): a scene's
        frame_ms from get_scenes, or a search hit's at_ms. You get the
        nearest sampled frame, and the caption says which moment it is."""
        who = principal()
        shown: str = kind
        frame_at: int | None = None
        relative_path: str | None
        async with open_session() as db:
            asset = await _asset(db, id)
            if asset.media_type == "audio":
                raise ToolError("Audio has no picture to look at; get_media has its transcript")
            if ts_ms is not None:
                # The only way Claude can *see* a scene: get_scenes and
                # search_media once offered only signed LAN URLs, which
                # Claude Desktop's off-machine fetching cannot reach, so b-roll
                # from a ten-minute drone clip was chosen from its poster.
                if asset.media_type != "video":
                    raise ToolError(
                        f"{asset.filename} is a {asset.media_type}; ts_ms is for a moment "
                        "in a video. Leave it out to see the picture."
                    )
                # Nearest, not exact as the frame URL is: at_ms is often a
                # transcript cue's start, which falls between sampled frames.
                # Only two columns: the row's 512-float embedding is no use here.
                nearest = (
                    await db.execute(
                        select(Frame.ts_ms, Frame.relative_path)
                        .where(Frame.asset_id == asset.id)
                        .order_by(func.abs(Frame.ts_ms - max(0, ts_ms)), Frame.ts_ms)
                        .limit(1)
                    )
                ).first()
                if nearest is None:
                    raise ToolError(
                        f"No frames have been sampled from {asset.filename} yet; "
                        "leave out ts_ms to see its poster"
                    )
                frame_at, relative_path = int(nearest[0]), str(nearest[1])
                shown = "frame"
            else:
                relative_path = (
                    await db.execute(
                        select(Derivative.relative_path).where(
                            Derivative.asset_id == asset.id,
                            Derivative.kind == kind,
                            Derivative.status == "ready",
                        )
                    )
                ).scalar_one_or_none()
            on = await _tags_on(db, [asset.id])
        if relative_path is None:
            raise ToolError(f"No {kind} has been generated for {asset.filename} yet")
        path = get_settings().data_dir / str(relative_path)
        if not path.is_file():
            raise ToolError(
                f"The {shown} file for {asset.filename} is missing; it can be regenerated"
            )

        def encode() -> bytes:
            from PIL import Image as PilImage

            with PilImage.open(path) as image:
                copy = image.convert("RGB")
                copy.thumbnail((PREVIEW_EDGE, PREVIEW_EDGE), PilImage.Resampling.LANCZOS)
                out = io.BytesIO()
                copy.save(out, "JPEG", quality=80)
                return out.getvalue()

        jpeg = await asyncio.to_thread(encode)
        confirmed, suggested = on.get(asset.id, ([], []))
        caption = (
            f"{asset.filename} ({asset.media_type}"
            + (f", {asset.duration_s:.0f}s" if asset.duration_s else "")
            + ")"
            + (
                f". This is the sampled frame at {frame_at / 1000:.2f}s (ts_ms {frame_at})"
                if frame_at is not None
                else ""
            )
            + f". Confirmed tags: {', '.join(confirmed) or 'none'}. "
            f"Suggested, unjudged: {', '.join(suggested) or 'none'}."
        )
        log.info("mcp.viewed", asset_id=str(asset.id), kind=shown, by=who.token_name)
        return [Image(data=jpeg, format="jpeg"), caption]

    @server.tool()
    async def review_queue(
        tag: str | None = None,
        origin: Literal["clip", "claude", "mcp"] | None = None,
        limit: int = 40,
    ) -> list[QueueItem]:
        """Suggestions awaiting the operator's judgement, strongest first.
        Narrow by tag or by who suggested it. Pair with view_media to help
        them work through it, and decide_tag to record their answers."""
        who = principal()
        limit = max(1, min(limit, 200))
        async with open_session() as db:
            stmt = (
                select(AssetTag, Asset, Tag)
                .join(Asset, Asset.id == AssetTag.asset_id)
                .join(Tag, Tag.id == AssetTag.tag_id)
                .where(AssetTag.source == "suggested", Asset.availability == "online")
                .order_by(AssetTag.confidence.desc().nullslast(), AssetTag.created_at.desc())
                .limit(limit)
            )
            if tag:
                wanted = await _tag_by_name(db, tag)
                if wanted is None:
                    raise ToolError(f"No tag called {tag!r}")
                stmt = stmt.where(AssetTag.tag_id == wanted.id)
            if origin:
                stmt = stmt.where(
                    AssetTag.origin == origin
                    if origin != "clip"
                    else (AssetTag.origin == origin) | AssetTag.origin.is_(None)
                )
            rows = (await db.execute(stmt)).all()
        return [
            QueueItem(
                asset_id=str(asset.id),
                filename=asset.filename,
                media_type=asset.media_type,
                tag=found.name,
                confidence=link.confidence,
                origin=link.origin or "clip",
                preview_url=_media_url(who.base_url, asset.id, "thumbnail"),
            )
            for link, asset, found in rows
        ]

    @server.tool()
    async def propose_tags(id: str, tags: list[str], note: str = "") -> ProposeResult:
        """Suggest tags for one item after looking at it. They go into the
        review queue for the operator, marked as yours; they are not applied.
        An item the operator already ruled on (tagged or rejected) is left
        as it is. Needs the 'tag' scope."""
        who = principal()
        _require_tag_scope(who)
        names = [tag_review.clean_tag(name) for name in tags if str(name).strip()]
        if not names:
            raise ToolError("Give at least one tag")
        proposals = [
            tag_review.TagProposal(name=name, confidence=None, existing=True, why=note[:200])
            for name in names[: tag_review.MAX_PROPOSALS * 2]
        ]
        async with open_session() as db:
            asset = await _asset(db, id)
            outcomes = await tag_review.write_proposals(
                db, asset.id, proposals, origin=tag_review.ORIGIN_MCP, created_by=who.user_id
            )
            await db.commit()
        log.info("mcp.proposed", asset_id=str(asset.id), by=who.token_name, tags=list(outcomes))
        queued = sum(1 for v in outcomes.values() if v.startswith("suggested"))
        return ProposeResult(
            asset_id=str(asset.id),
            outcomes=[ProposalOutcome(tag=k, outcome=v) for k, v in outcomes.items()],
            note=f"{queued} suggestion{'' if queued == 1 else 's'} queued for the operator "
            "to judge.",
        )

    @server.tool()
    async def decide_tag(id: str, tag: str, accept: bool) -> DecideResult:
        """Record the operator's answer on one tag for one item: accept makes
        it a confirmed example, reject makes it negative evidence the learner
        will never offer again. Only do this when the operator has told you
        their decision. Needs the 'tag' scope."""
        who = principal()
        _require_tag_scope(who)
        async with open_session() as db:
            asset = await _asset(db, id)
            found = await _tag_by_name(db, tag)
            if found is None:
                if not accept:
                    raise ToolError(f"No tag called {tag!r} to reject")
                slug = slugify(tag_review.clean_tag(tag))
                if not slug:
                    raise ToolError("That tag name has no usable characters")
                found = Tag(name=tag_review.clean_tag(tag), slug=slug)
                db.add(found)
                await db.flush()
            link = (
                await db.execute(
                    select(AssetTag).where(
                        AssetTag.asset_id == asset.id, AssetTag.tag_id == found.id
                    )
                )
            ).scalar_one_or_none()
            if link is None:
                # No suggestion to judge: the operator is applying (or
                # refusing) the tag outright, which is the manual path.
                link = AssetTag(asset_id=asset.id, tag_id=found.id, origin=tag_review.ORIGIN_MCP)
                db.add(link)
                link.source = "manual" if accept else "rejected"
            elif accept:
                link.source = "confirmed" if link.source == "suggested" else "manual"
            else:
                link.source = "rejected"
            link.confidence = None if not accept else link.confidence
            link.created_by = who.user_id
            await db.commit()
            tag_id, tag_name, source = found.id, found.name, link.source
        _relearn(tag_id)
        log.info(
            "mcp.decided", asset_id=str(asset.id), tag=tag_name, accept=accept, by=who.token_name
        )
        return DecideResult(
            asset_id=str(asset.id),
            tag=tag_name,
            source=source,
            note="Recorded; the learner re-runs with this as "
            + ("a positive example." if accept else "negative evidence."),
        )

    @server.tool()
    async def api_review_status() -> ReviewStatus:
        """The server-side Claude reviewer: whether a key is configured, the
        monthly budget and what is left of it, how many pictures nobody has
        tagged, and what the last run did."""
        principal()
        from framefound.media.maps_store import load_ai_edit_config, load_review_state

        async with open_session() as db:
            config = await load_ai_edit_config(db)
            spent = await spend.spent_this_month(db)
            untagged = await tag_review.count_untagged(db)
            state = await load_review_state(db)
        return ReviewStatus(
            configured=config.ready,
            model=config.review_model or config.model,
            budget_usd=config.monthly_budget_usd,
            spent_this_month_usd=round(spent, 4),
            remaining_usd=round(max(0.0, config.monthly_budget_usd - spent), 4),
            untagged=untagged,
            last_run=state.__dict__,
        )

    @server.tool()
    async def start_api_review(asset_ids: list[str] | None = None, limit: int = 0) -> ReviewStarted:
        """Have the server send pictures to the Claude API for tag proposals,
        under the monthly budget: the given asset ids, or the newest untagged
        ones (`limit`, default the configured batch). Suggestions appear in
        review_queue as origin "claude". Needs the 'tag' scope."""
        who = principal()
        _require_tag_scope(who)
        if who.role != "admin":
            # The same rule as "Review with Claude" on the Tags page: this
            # spends the month's API budget, which is the admin's to spend.
            raise ToolError("Only an admin account can start a paid review")
        from framefound.api.v1.tags import queue_claude_review

        async with open_session() as db:
            try:
                queued, message = await queue_claude_review(
                    db, [uuid.UUID(a) for a in (asset_ids or [])], limit
                )
            except ValueError as err:
                raise ToolError(str(err)) from None
        log.info("mcp.review_started", queued=queued, by=who.token_name)
        return ReviewStarted(queued=queued, message=message)

    from framefound.mcp import video

    video.register(server)
    return server
