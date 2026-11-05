"""Search: tags, pictures, spoken words and filenames, fused into one list.

Four retrievers answer every query (docs/architecture.md §search):

- tags — a human judgement, matched either way round: the tag inside the
  query ("drone shot of white farmhouse" finds the tag "farmhouse") or the
  query inside the tag name;
- visual — CLIP similarity of the query to sampled frames, one best frame
  per asset;
- transcripts — Postgres full-text search against the GIN index from
  migration 0005, ranked by `ts_rank_cd`, with an any-word fallback when
  every-word finds too little; the SQLite test dialect falls back to LIKE;
- filenames and folder paths — substring match (trigram index, 0026).

Each list is kept as-is for the per-strategy sections, and Reciprocal Rank
Fusion (search/ranking.py) merges them into `results`: one row per asset,
with the reasons it matched and the moment to jump to.
"""

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from functools import lru_cache
from typing import Any, Literal

import structlog
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import Select, func, literal, or_, select, text

from framefound.auth.deps import CurrentUser, DbDep
from framefound.db.models import Asset, AssetTag, Tag, Transcript, TranscriptSegment
from framefound.search.ranking import fuse

log = structlog.get_logger()

router = APIRouter(prefix="/search", tags=["search"])

MediaTypeFilter = Literal["image", "video", "audio"]


def _dot(a: Any, b: Any) -> float:
    """Cosine similarity for L2-normalised vectors (SQLite fallback)."""
    if a is None or b is None or len(a) == 0 or len(b) == 0:
        return 0.0
    return float(sum(x * y for x, y in zip(a, b, strict=False)))


class TranscriptHit(BaseModel):
    asset_id: uuid.UUID
    filename: str
    library_id: uuid.UUID
    media_type: str
    start_ms: int
    end_ms: int
    text: str


class FilenameHit(BaseModel):
    asset_id: uuid.UUID
    filename: str
    library_id: uuid.UUID
    media_type: str
    captured_at: datetime | None


class TagHit(BaseModel):
    """An asset carrying a tag whose name matches the query.

    Ranked above everything else, because a tag is a human judgement about what
    the thing is. A filename or a CLIP score is a guess; someone typed this.
    """

    asset_id: uuid.UUID
    filename: str
    library_id: uuid.UUID
    media_type: str
    tag_name: str
    tag_slug: str
    confirmed: bool  # false for a suggestion still awaiting judgement


class VisualHit(BaseModel):
    asset_id: uuid.UUID
    filename: str
    library_id: uuid.UUID
    media_type: str
    ts_ms: int
    similarity: float


class FusedResult(BaseModel):
    """One asset in the fused ranking, with why it matched.

    `ts_ms` is the moment to open it at — where the words were said if speech
    matched, else the best-matching frame. `frame_ms` is that frame alone:
    a transcript time is not a sampled frame, so a thumbnail is fetched by
    `frame_ms`, never by `ts_ms`.
    """

    asset_id: uuid.UUID
    filename: str
    library_id: uuid.UUID
    media_type: str
    score: float
    reasons: list[str]
    ts_ms: int | None = None
    frame_ms: int | None = None
    snippet: str | None = None
    tag: str | None = None


# Why visual search did or did not answer. "indexing": no frame has an
# embedding yet, which resolves itself as the workers catch up. "unavailable":
# frames are indexed but this server cannot embed the query (no model, no
# onnxruntime, or the encoder crashed) — waiting will not fix it. One boolean
# used to carry both, and the page told the operator a broken model was
# "still indexing".
VisualStatus = Literal["ready", "indexing", "unavailable"]


class SearchResponse(BaseModel):
    query: str
    transcript_hits: list[TranscriptHit]
    filename_hits: list[FilenameHit]
    visual_hits: list[VisualHit] = []
    visual_status: VisualStatus = "ready"
    # visual_status == "ready". Kept for the MCP connector and older panels,
    # which read only this.
    visual_available: bool = True
    tag_hits: list[TagHit] = []
    # The four lists above fused into one, best first (search/ranking.py).
    results: list[FusedResult] = []


# How many nearest frames to ask the index for per result wanted. Several
# frames of one clip sit next to each other in CLIP space, and a library
# filter is applied after the index has answered.
NEIGHBOUR_FRAMES = 8

# The widest the nearest-frames window may grow when it is full but still
# holds fewer than `limit` distinct assets (see `_widen_until_filled`).
MAX_FRAME_WINDOW = 4000

# pgvector 0.8 added iterative index scans; older versions reject the
# hnsw.iterative_scan parameter outright, which aborts the transaction.
ITERATIVE_SCAN_SINCE = (0, 8)
# Bound on the tuples one iterative scan may visit: a few dozen per frame
# wanted, so a filter keeping one frame in twenty-five is still met, and
# never so many that one search walks the whole graph.
SCAN_TUPLES_PER_FRAME = 25
MIN_SCAN_TUPLES = 20_000  # pgvector's own default
MAX_SCAN_TUPLES = 100_000

# How much each retriever counts in the fused ranking. A confirmed tag is the
# operator's own word; a suggestion and a filename are weaker evidence.
FUSION_WEIGHTS: dict[str, float] = {
    "tag": 1.5,
    "suggested_tag": 0.8,
    "transcript": 1.2,
    "visual": 1.0,
    "name": 0.8,
}

# Words too common to be worth an any-word transcript match on SQLite.
# (Postgres drops its own English stopwords inside to_tsquery.)
# fmt: off
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were", "has", "have",
    "had", "not", "but", "you", "your", "our", "its", "his", "her", "they", "them", "their",
    "there", "here", "what", "when", "where", "who", "how", "all", "any", "can", "will",
    "just", "into", "onto", "over", "under", "about",
})
# fmt: on
_WORD = re.compile(r"[^\W_]+")


def _query_words(query: str) -> list[str]:
    """Lower-cased alphanumeric words of the query, de-duplicated in order.

    Only letters and digits survive, so the words are safe to join into a
    to_tsquery expression — its operators (& | ! : * ( ) ') never appear.
    """
    seen: dict[str, None] = {}
    for word in _WORD.findall(query.lower()):
        seen.setdefault(word, None)
    return list(seen)


def _wants_exact(query: str) -> bool:
    """A quoted phrase or a -negation is an instruction, not a hint: the
    any-word fallback would ignore it, so it is not run."""
    return '"' in query or any(token.startswith("-") for token in query.split())


def _filtered(
    stmt: Any, library_id: uuid.UUID | None, media_type: str | None
) -> Any:  # a Select; typed loosely: SQLAlchemy 2.0 and 2.1 spell its generics differently
    """The filters every retriever shares. Offline assets never appear: a
    result the operator cannot open is worse than no result."""
    stmt = stmt.where(Asset.availability == "online")
    if library_id is not None:
        stmt = stmt.where(Asset.library_id == library_id)
    if media_type is not None:
        stmt = stmt.where(Asset.media_type == media_type)
    return stmt


# ---------------------------------------------------------------- embedding

# The provider the cache was filled by. Held strongly so the identity check
# below cannot be fooled by a new provider reusing a freed object's id.
_embed_cache_owner: object | None = None


@lru_cache(maxsize=256)
def _cached_text_vector(query: str) -> tuple[float, ...]:
    """The query's text embedding — the vector itself, never the provider.

    The panels repeat the same few searches; CLIP's text encoder is 35 ms on
    the deployment host, so a repeat costs nothing. Failures raise and are
    therefore not cached.
    """
    from framefound.ai.embeddings import get_embedding_provider

    return tuple(get_embedding_provider().embed_text(query).vector)


def clear_embedding_cache() -> None:
    """Forget cached query vectors (tests; a model change)."""
    global _embed_cache_owner
    _cached_text_vector.cache_clear()
    _embed_cache_owner = None


async def _embed_query(query: str) -> list[float]:
    from framefound.ai.embeddings import get_embedding_provider

    global _embed_cache_owner
    provider = get_embedding_provider()
    if provider is not _embed_cache_owner:
        # A different model answers differently: vectors from the last one
        # would be silently wrong.
        _cached_text_vector.cache_clear()
        _embed_cache_owner = provider
    key = " ".join(query.split())
    return list(await asyncio.to_thread(_cached_text_vector, key))


# ---------------------------------------------------------------- retrievers


async def _widen_ann_search(db: DbDep, candidates: int) -> None:
    """Let the HNSW index return enough rows for the filters that follow.

    pgvector's index yields at most `hnsw.ef_search` rows — 40 by default —
    *before* any WHERE is applied. A search limited to a library holding a
    fifth of the frames therefore came back with eight hits for a limit of
    25, and no limit above 40 could ever be met. SET LOCAL lasts for this
    transaction only. Postgres-only; the SQLite path ranks in Python.
    """
    ef_search = max(100, min(1000, int(candidates)))
    await db.execute(text(f"SET LOCAL hnsw.ef_search = {ef_search}"))  # noqa: S608


# The installed pgvector's version, looked up once per process: it changes
# only with an image upgrade, which restarts the API.
_pgvector_version: tuple[int, ...] | None = None


def _parse_version(raw: str | None) -> tuple[int, ...]:
    """ "0.8.0" → (0, 8, 0); anything unreadable → (0,), i.e. too old."""
    parts: list[int] = []
    for piece in (raw or "").split("."):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    return tuple(parts) or (0,)


async def _allow_iterative_scan(db: DbDep, candidates: int) -> None:
    """Let the HNSW scan keep going until the filters have been satisfied.

    `ef_search` alone bounds the rows the index returns *before* the
    library, media-type and availability filters. On a catalogue of tens of
    thousands of photos, `search_media(media_type="video")` found the 160
    nearest frames, nearly all of them drone stills, filtered them out and
    returned two clips. pgvector 0.8's iterative scan continues until the
    LIMIT has that many rows that pass. `relaxed_order` may hand rows back
    slightly out of distance order; every query here re-sorts by distance
    above the scan, so the order the operator sees is still exact.

    Skipped on pgvector before 0.8 — the parameter does not exist there and
    setting it would abort the search's transaction. `extversion` is the
    installed SQL version: it can only be 0.8 if the 0.8 library is loaded.
    """
    global _pgvector_version
    if _pgvector_version is None:
        raw = (
            await db.execute(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'"))
        ).scalar_one_or_none()
        _pgvector_version = _parse_version(raw)
    if _pgvector_version < ITERATIVE_SCAN_SINCE:
        return
    tuples = max(MIN_SCAN_TUPLES, min(MAX_SCAN_TUPLES, int(candidates) * SCAN_TUPLES_PER_FRAME))
    await db.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    await db.execute(text(f"SET LOCAL hnsw.max_scan_tuples = {tuples}"))  # noqa: S608


async def _widen_until_filled[T](
    fetch: Callable[[int], Awaitable[tuple[list[T], int]]], limit: int
) -> list[T]:
    """Ask for ever more nearest frames until they hold `limit` assets.

    `fetch(window)` returns the best frame of each distinct asset among the
    `window` nearest frames, and how many frames that window really held.
    One window of `limit * NEIGHBOUR_FRAMES` frames is not enough: a 4K
    drone clip carries up to 240 sampled frames that all sit together in
    CLIP space, so a few clips filled the whole window and DISTINCT ON left
    a handful of hits for a limit of twenty. Stop when enough assets were
    found, when the window came back short (nothing more passes the
    filters), or at MAX_FRAME_WINDOW.
    """
    window = limit * NEIGHBOUR_FRAMES
    while True:
        found, frames_seen = await fetch(window)
        if len(found) >= limit or frames_seen < window or window >= MAX_FRAME_WINDOW:
            return found
        window = min(window * 4, MAX_FRAME_WINDOW)


def _best_frame_per_asset(nearest_frames: Select[Any], window: int, limit: int) -> Select[Any]:
    """The nearest `limit` assets, each at its best frame, among the `window`
    nearest frames — plus how many frames that window held.

    `nearest_frames` selects (asset_id, ts_ms, distance). The count is a
    window function over the DISTINCT ON level, which Postgres evaluates
    before DISTINCT, so it counts frames, not assets. The outermost ORDER BY
    is the one that makes the result exact under `relaxed_order`.
    """
    nearest = nearest_frames.order_by("distance").limit(window).subquery()
    best = (
        select(
            nearest.c.asset_id,
            nearest.c.ts_ms,
            nearest.c.distance,
            func.count().over().label("window_frames"),
        )
        .distinct(nearest.c.asset_id)
        .order_by(nearest.c.asset_id, nearest.c.distance)
        .subquery()
    )
    return (
        select(best.c.ts_ms, Asset, best.c.distance, best.c.window_frames)
        .join(Asset, Asset.id == best.c.asset_id)
        .order_by(best.c.distance, best.c.asset_id)
        .limit(limit)
    )


async def _nearest_assets_pg(
    db: DbDep, nearest_frames: Select[Any], limit: int
) -> list[tuple[int, Asset, float]]:
    """Postgres: the HNSW index, widened until `limit` assets are found."""

    async def fetch(window: int) -> tuple[list[tuple[int, Asset, float]], int]:
        await _widen_ann_search(db, window)
        await _allow_iterative_scan(db, window)
        rows = (await db.execute(_best_frame_per_asset(nearest_frames, window, limit))).all()
        frames_seen = int(rows[0][3]) if rows else 0
        return [(int(ts_ms), asset, 1.0 - float(d)) for ts_ms, asset, d, _ in rows], frames_seen

    return await _widen_until_filled(fetch, limit)


async def _visual_search(
    db: DbDep,
    query: str,
    library_id: uuid.UUID | None,
    limit: int,
    media_type: str | None = None,
) -> tuple[list[VisualHit], VisualStatus]:
    """Nearest assets to the query in CLIP space — one row per asset, at its
    best frame.

    Asking for `limit` frames let one long drone clip fill every slot with
    its own neighbouring frames; the de-duplication happens *before* the
    limit, as in `similar_assets`. Postgres uses the HNSW index via
    pgvector's cosine operator. SQLite (the test dialect) has no vector type,
    so similarity is computed in Python over the small fixture set.
    """
    from framefound.ai.embeddings import EmbeddingUnavailable
    from framefound.db.models import Frame

    # "Is anything indexed" — one row, not a count over every frame.
    indexed = (
        await db.execute(select(Frame.id).where(Frame.embedding.is_not(None)).limit(1))
    ).first()
    if indexed is None:
        return [], "indexing"

    try:
        vector = await _embed_query(query)
    except EmbeddingUnavailable:
        # Frames are indexed, so this is not "still indexing" either: this
        # server has no model or runtime to embed the query with.
        return [], "unavailable"
    except Exception:
        # Not "still indexing": something broke. Say so in the log, and keep
        # the other three retrievers answering.
        log.warning("search.embed_failed", exc_info=True)
        return [], "unavailable"

    scored: list[tuple[int, Asset, float]] = []
    if db.get_bind().dialect.name == "postgresql":
        distance = Frame.embedding.cosine_distance(vector).label("distance")
        nearest_frames = _filtered(
            select(Frame.asset_id, Frame.ts_ms, distance)
            .join(Asset, Asset.id == Frame.asset_id)
            .where(Frame.embedding.is_not(None)),
            library_id,
            media_type,
        )
        scored = await _nearest_assets_pg(db, nearest_frames, limit)
    else:
        candidates = (
            await db.execute(
                _filtered(
                    select(Frame.asset_id, Frame.ts_ms, Frame.embedding).join(
                        Asset, Asset.id == Frame.asset_id
                    ),
                    library_id,
                    media_type,
                ).where(Frame.embedding.is_not(None))
            )
        ).all()
        best_frame: dict[uuid.UUID, tuple[int, float]] = {}
        for asset_id, ts_ms, embedding in candidates:
            similarity = _dot(vector, embedding)
            held = best_frame.get(asset_id)
            if held is None or similarity > held[1]:
                best_frame[asset_id] = (ts_ms, similarity)
        top = sorted(best_frame.items(), key=lambda item: -item[1][1])[:limit]
        assets = {
            asset.id: asset
            for asset in (
                await db.execute(select(Asset).where(Asset.id.in_([a for a, _ in top])))
            ).scalars()
        }
        scored = [(ts_ms, assets[asset_id], sim) for asset_id, (ts_ms, sim) in top]

    return [
        VisualHit(
            asset_id=asset.id,
            filename=asset.filename,
            library_id=asset.library_id,
            media_type=asset.media_type,
            ts_ms=ts_ms,
            similarity=round(similarity, 4),
        )
        for ts_ms, asset, similarity in scored
    ], "ready"


def _fts_vector() -> Any:
    # Must stay textually identical to the expression index from 0005, or
    # the planner cannot use it.
    return func.to_tsvector("english", TranscriptSegment.text)


def _segments(library_id: uuid.UUID | None, media_type: str | None) -> Any:
    return _filtered(
        select(TranscriptSegment, Asset)
        .join(Transcript, Transcript.id == TranscriptSegment.transcript_id)
        .join(Asset, Asset.id == Transcript.asset_id),
        library_id,
        media_type,
    )


def _pg_every_word(q: str, library_id: uuid.UUID | None, media_type: str | None, limit: int) -> Any:
    """Every word must be said (websearch syntax), best-ranked first."""
    tsquery = func.websearch_to_tsquery("english", q)
    return (
        _segments(library_id, media_type)
        .where(_fts_vector().op("@@")(tsquery))
        .order_by(func.ts_rank_cd(_fts_vector(), tsquery).desc(), TranscriptSegment.start_ms)
        .limit(limit)
    )


def _pg_any_word(
    words: list[str],
    exclude: list[uuid.UUID],
    library_id: uuid.UUID | None,
    media_type: str | None,
    limit: int,
) -> Any:
    """Any word said, ranked by how many and how close together.

    The words are bare alphanumerics (see `_query_words`) and travel as a
    bound parameter; to_tsquery stems them and drops English stopwords.
    """
    tsquery = func.to_tsquery("english", " | ".join(words))
    stmt = _segments(library_id, media_type).where(_fts_vector().op("@@")(tsquery))
    if exclude:
        stmt = stmt.where(TranscriptSegment.id.not_in(exclude))
    return stmt.order_by(
        func.ts_rank_cd(_fts_vector(), tsquery).desc(), TranscriptSegment.start_ms
    ).limit(limit)


async def _transcript_search(
    db: DbDep, q: str, library_id: uuid.UUID | None, limit: int, media_type: str | None
) -> list[Any]:
    """Segments that say the query, most relevant first.

    Every-word matching first: it is the precise answer. A natural-language
    query ("someone saying the starting bid is low") rarely has every word in
    one segment, so when that finds fewer than `limit` an any-word match
    tops the list up, ranked below every precise hit.
    """
    words = _query_words(q)
    if db.get_bind().dialect.name == "postgresql":
        rows = list((await db.execute(_pg_every_word(q, library_id, media_type, limit))).all())
        if len(rows) < limit and len(words) > 1 and not _wants_exact(q):
            found = [seg.id for seg, _ in rows]
            more = _pg_any_word(words, found, library_id, media_type, limit - len(rows))
            rows.extend((await db.execute(more)).all())
        return rows

    # tests / SQLite: LIKE on the phrase, then on its words.
    phrase = q.replace('"', "").strip()
    rows = list(
        (
            await db.execute(
                _segments(library_id, media_type)
                .where(TranscriptSegment.text.ilike(f"%{phrase}%"))
                .order_by(TranscriptSegment.start_ms)
                .limit(limit)
            )
        ).all()
    )
    useful = [w for w in words if len(w) >= 3 and w not in _STOPWORDS]
    if len(rows) < limit and useful and not _wants_exact(q):
        stmt = _segments(library_id, media_type).where(
            or_(*(TranscriptSegment.text.ilike(f"%{w}%") for w in useful))
        )
        if rows:
            stmt = stmt.where(TranscriptSegment.id.not_in([seg.id for seg, _ in rows]))
        wanted = limit - len(rows)
        candidates = (await db.execute(stmt.limit(wanted * 4))).all()

        def overlap(row: Any) -> tuple[int, int]:
            said = row[0].text.lower()
            return (-sum(1 for w in useful if w in said), row[0].start_ms)

        rows.extend(sorted(candidates, key=overlap)[:wanted])
    return rows


async def _name_search(
    db: DbDep, q: str, library_id: uuid.UUID | None, limit: int, media_type: str | None
) -> list[Asset]:
    """Filename or folder path containing the query; a filename match first."""
    pattern = f"%{q}%"
    stmt = _filtered(
        select(Asset).where(or_(Asset.filename.ilike(pattern), Asset.relative_path.ilike(pattern))),
        library_id,
        media_type,
    ).order_by(Asset.filename.ilike(pattern).desc(), Asset.mtime.desc())
    return list((await db.execute(stmt.limit(limit))).scalars().all())


def _tag_phrase(query: str) -> str:
    """The query as space-separated lower-case words, padded with a space at
    each end, so a tag can be found inside it on word boundaries only."""
    return " " + " ".join(re.sub(r"[^\w'\-]+", " ", query.lower()).split()) + " "


async def _tag_search(
    db: DbDep,
    query: str,
    library_id: uuid.UUID | None,
    limit: int,
    media_type: str | None = None,
) -> list["TagHit"]:
    """Assets carrying a tag whose name matches — either way round.

    "broom" finds the tag "Power Broom" (the query inside the name), and
    "drone shot of white farmhouse" finds the tag "Farmhouse" (the name
    inside the query, on word boundaries, so "art" is not found inside
    "starting").

    Confirmed tags come first: a suggestion the operator has not judged yet is
    a claim, and mixing the two would present a guess with the same authority
    as a decision.

    Rejected links are excluded — the operator has already said no, and
    resurrecting it in search results would be the same nagging the tagging
    model was designed to avoid.
    """
    name_in_query = literal(_tag_phrase(query)).ilike(
        literal("% ") + func.lower(Tag.name) + literal(" %")
    )
    stmt = (
        _filtered(
            select(Asset, Tag, AssetTag.source)
            .join(AssetTag, AssetTag.asset_id == Asset.id)
            .join(Tag, Tag.id == AssetTag.tag_id),
            library_id,
            media_type,
        )
        .where(
            or_(Tag.name.ilike(f"%{query}%"), name_in_query),
            AssetTag.source != "rejected",
        )
        .order_by(AssetTag.source == "suggested", Asset.mtime.desc())
        .limit(limit)
    )
    return [
        TagHit(
            asset_id=asset.id,
            filename=asset.filename,
            library_id=asset.library_id,
            media_type=asset.media_type,
            tag_name=tag.name,
            tag_slug=tag.slug,
            confirmed=source != "suggested",
        )
        for asset, tag, source in (await db.execute(stmt)).all()
    ]


# ---------------------------------------------------------------- fusion


def fuse_results(
    *,
    tag_hits: list[TagHit],
    visual_hits: list[VisualHit],
    transcript_hits: list[TranscriptHit],
    filename_hits: list[FilenameHit],
    limit: int,
) -> list[FusedResult]:
    """One row per asset from the four lists, by weighted RRF.

    No queries: everything needed is already in the hits. An asset's moment
    is its best transcript segment if speech matched, else its best frame.
    """
    info: dict[str, Any] = {}
    confirmed: list[str] = []
    suggested: list[str] = []
    tag_name: dict[str, str] = {}
    for hit in tag_hits:
        key = str(hit.asset_id)
        info.setdefault(key, hit)
        tag_name.setdefault(key, hit.tag_name)
        (confirmed if hit.confirmed else suggested).append(key)
    # A confirmed tag already says it; the suggestion would be a weaker echo.
    said = set(confirmed)
    suggested = [key for key in suggested if key not in said]

    spoken: dict[str, TranscriptHit] = {}
    for segment in transcript_hits:  # best first, so the first is the moment
        key = str(segment.asset_id)
        info.setdefault(key, segment)
        spoken.setdefault(key, segment)
    frames: dict[str, VisualHit] = {}
    for visual in visual_hits:
        key = str(visual.asset_id)
        info.setdefault(key, visual)
        frames.setdefault(key, visual)
    for named in filename_hits:
        info.setdefault(str(named.asset_id), named)

    fused = fuse(
        {
            "tag": confirmed,
            "suggested_tag": suggested,
            "visual": list(frames),
            "transcript": list(spoken),
            "name": list(dict.fromkeys(str(n.asset_id) for n in filename_hits)),
        },
        weights=FUSION_WEIGHTS,
    )

    results: list[FusedResult] = []
    for ranked in fused[:limit]:
        first = info[ranked.key]
        moment = spoken.get(ranked.key)
        frame = frames.get(ranked.key)
        results.append(
            FusedResult(
                asset_id=first.asset_id,
                filename=first.filename,
                library_id=first.library_id,
                media_type=first.media_type,
                score=round(ranked.score, 6),
                reasons=ranked.reasons,
                ts_ms=moment.start_ms if moment else (frame.ts_ms if frame else None),
                frame_ms=frame.ts_ms if frame else None,
                snippet=moment.text if moment else None,
                tag=tag_name.get(ranked.key),
            )
        )
    return results


# ---------------------------------------------------------------- endpoint


@router.get("", response_model=SearchResponse)
async def search(
    _user: CurrentUser,
    db: DbDep,
    q: str = Query(min_length=2, max_length=200),
    library_id: uuid.UUID | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    media_type: MediaTypeFilter | None = None,
) -> SearchResponse:
    return await run_search(db, q, library_id, limit, media_type=media_type)


async def run_search(
    db: DbDep,
    q: str,
    library_id: uuid.UUID | None,
    limit: int,
    *,
    media_type: str | None = None,
) -> SearchResponse:
    """The search itself, apart from the HTTP: the MCP connector runs the
    same one, so Claude and the web UI never disagree about a result."""
    seg_rows = await _transcript_search(db, q, library_id, limit, media_type)
    name_rows = await _name_search(db, q, library_id, limit, media_type)
    visual_hits, visual_status = await _visual_search(db, q, library_id, limit, media_type)
    tag_hits = await _tag_search(db, q, library_id, limit, media_type)

    transcript_hits = [
        TranscriptHit(
            asset_id=asset.id,
            filename=asset.filename,
            library_id=asset.library_id,
            media_type=asset.media_type,
            start_ms=seg.start_ms,
            end_ms=seg.end_ms,
            text=seg.text,
        )
        for seg, asset in seg_rows
    ]
    filename_hits = [
        FilenameHit(
            asset_id=a.id,
            filename=a.filename,
            library_id=a.library_id,
            media_type=a.media_type,
            captured_at=a.captured_at,
        )
        for a in name_rows
    ]
    return SearchResponse(
        query=q,
        visual_hits=visual_hits,
        visual_status=visual_status,
        visual_available=visual_status == "ready",
        tag_hits=tag_hits,
        transcript_hits=transcript_hits,
        filename_hits=filename_hits,
        results=fuse_results(
            tag_hits=tag_hits,
            visual_hits=visual_hits,
            transcript_hits=transcript_hits,
            filename_hits=filename_hits,
            limit=limit,
        ),
    )


@router.get("/similar/{asset_id}", response_model=list[VisualHit])
async def similar_assets(
    asset_id: uuid.UUID, _user: CurrentUser, db: DbDep, limit: int = Query(default=12, ge=1, le=50)
) -> list[VisualHit]:
    """Visually closest frames from *other* assets — 'more like this'."""
    from framefound.db.models import Frame

    reference = (
        await db.execute(
            select(Frame)
            .where(Frame.asset_id == asset_id, Frame.embedding.is_not(None))
            .order_by(Frame.ts_ms)
            .limit(1)
        )
    ).scalar_one_or_none()
    if reference is None:
        raise HTTPException(404, "This item has not been visually indexed yet")

    # One hit per asset: twenty frames of the same clip is not "similar
    # media". The de-duplication happens *before* the limit — done after it,
    # a clip whose frames crowded the top left "more like this" with four
    # results for a limit of twelve.
    scored: list[tuple[int, Asset, float]] = []
    if db.get_bind().dialect.name == "postgresql":
        distance = Frame.embedding.cosine_distance(reference.embedding).label("distance")
        nearest_frames = select(Frame.asset_id, Frame.ts_ms, distance).where(
            Frame.embedding.is_not(None), Frame.asset_id != asset_id
        )
        scored = await _nearest_assets_pg(db, nearest_frames, limit)
    else:
        candidates = (
            await db.execute(
                select(Frame, Asset)
                .join(Asset, Asset.id == Frame.asset_id)
                .where(Frame.embedding.is_not(None), Frame.asset_id != asset_id)
            )
        ).all()
        ranked = sorted(
            (
                (frame, asset, _dot(reference.embedding, frame.embedding))
                for frame, asset in candidates
            ),
            key=lambda row: -row[2],
        )
        seen: set[uuid.UUID] = set()
        for frame, asset, similarity in ranked:
            if asset.id in seen:
                continue
            seen.add(asset.id)
            scored.append((frame.ts_ms, asset, similarity))
            if len(scored) == limit:
                break

    return [
        VisualHit(
            asset_id=asset.id,
            filename=asset.filename,
            library_id=asset.library_id,
            media_type=asset.media_type,
            ts_ms=ts_ms,
            similarity=round(similarity, 4),
        )
        for ts_ms, asset, similarity in scored
    ]
