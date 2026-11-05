"""Search relevance: ranking, natural-language queries, filters and fusion.

The operator searches the way a client asks — "drone shot of white
farmhouse", "someone saying starting bid" — not the way a database matches.
These tests pin the behaviour that makes that work on the SQLite test
dialect, and check the Postgres-only statements by compiling them.
"""

import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Select, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.ai import embeddings as embeddings_module
from framefound.ai.embeddings import EmbeddingResult, EmbeddingUnavailable
from framefound.api.v1 import search as search_module
from framefound.api.v1.search import (
    TagHit,
    TranscriptHit,
    VisualHit,
    _pg_any_word,
    _pg_every_word,
    _query_words,
    clear_embedding_cache,
    fuse_results,
)
from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import (
    Asset,
    AssetTag,
    Frame,
    Library,
    Tag,
    Transcript,
    TranscriptSegment,
)
from framefound.search.ranking import fuse

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}

POND = [0.0, 1.0, 0.0, 0.0]
CROWD = [0.0, 0.0, 1.0, 0.0]


def farm(strength: float) -> list[float]:
    return [0.0, 0.0, 0.0, strength]


class FakeProvider:
    """Maps known words to fixed vectors, and counts what it was asked."""

    def __init__(self) -> None:
        self.calls = 0

    def embed_text(self, text: str) -> EmbeddingResult:
        self.calls += 1
        lowered = text.lower()
        if "farm" in lowered:
            return EmbeddingResult(farm(1.0), "fake")
        if "pond" in lowered:
            return EmbeddingResult(POND, "fake")
        return EmbeddingResult(CROWD, "fake")

    def embed_image(self, path: Path) -> EmbeddingResult:  # pragma: no cover
        return EmbeddingResult(CROWD, "fake")


class BrokenProvider(FakeProvider):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    def embed_text(self, text: str) -> EmbeddingResult:
        raise self.error


AUCTION_SEGMENTS = [
    (0, "Welcome to the auction preview."),
    (4200, "The starting bid will be announced on site."),
    (9800, "Settlement will be on or before thirty days."),
    (15000, "Bid early: the starting price is low and the bid moves fast."),
]


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict]:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "relevance-test-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", db_url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    clear_embedding_cache()
    provider = FakeProvider()
    monkeypatch.setattr(embeddings_module, "_provider", provider)

    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    from framefound.main import create_app

    app = create_app()
    app.dependency_overrides[get_session] = override

    ids: dict[str, uuid.UUID] = {}
    lib_root = tmp_path / "lib"
    lib_root.mkdir()
    async with factory() as db:
        library = Library(name="L", root_path=str(lib_root))
        db.add(library)
        await db.flush()

        def add_asset(name: str, media_type: str, availability: str = "online") -> Asset:
            asset = Asset(
                library_id=library.id,
                relative_path=f"shoots/{name}",
                filename=name,
                extension=name.rsplit(".", 1)[-1],
                media_type=media_type,
                size_bytes=10,
                mtime=datetime.now(UTC),
                availability=availability,
            )
            db.add(asset)
            return asset

        def add_frame(asset: Asset, ts_ms: int, vector: list[float]) -> None:
            db.add(
                Frame(
                    asset_id=asset.id,
                    ts_ms=ts_ms,
                    relative_path=f"frames/{asset.filename}-{ts_ms}.jpeg",
                    embedding=vector,
                    embedding_model="fake",
                )
            )

        def add_transcript(asset: Asset, segments: list[tuple[int, str]]) -> Transcript:
            transcript = Transcript(
                asset_id=asset.id,
                language="en",
                model_name="fake",
                full_text=" ".join(t for _, t in segments),
                segment_count=len(segments),
            )
            db.add(transcript)
            return transcript

        drone = add_asset("aerial_0042.mp4", "video")
        front = add_asset("farmhouse_front.jpg", "image")
        pond = add_asset("pond.jpg", "image")
        auction = add_asset("auction_walkthrough.mp4", "video")
        interview = add_asset("owner_interview.mp3", "audio")
        offline = add_asset("farmhouse_offline.jpg", "image", availability="offline")
        await db.flush()
        for asset in (drone, front, pond, auction, interview, offline):
            ids[asset.filename] = asset.id

        # One long drone clip: every frame looks more like a farmhouse than
        # anything else does, the best at 6 s.
        for second in range(10):
            add_frame(drone, second * 1000, farm(1.0 if second == 6 else 0.95 + second / 1000))
        add_frame(front, 0, farm(0.9))
        add_frame(pond, 0, POND)
        add_frame(auction, 0, CROWD)
        add_frame(offline, 0, farm(1.0))

        for asset, segments in (
            (auction, AUCTION_SEGMENTS),
            (interview, [(2500, "We bought the farmhouse in 1990.")]),
            (offline, [(0, "farmhouse starting bid")]),
        ):
            transcript = add_transcript(asset, segments)
            await db.flush()
            for start, said in segments:
                db.add(
                    TranscriptSegment(
                        transcript_id=transcript.id, start_ms=start, end_ms=start + 3000, text=said
                    )
                )

        farmhouse = Tag(name="Farmhouse", slug="farmhouse")
        art = Tag(name="Art", slug="art")
        db.add_all([farmhouse, art])
        await db.flush()
        db.add_all(
            [
                AssetTag(asset_id=front.id, tag_id=farmhouse.id, source="manual"),
                AssetTag(asset_id=drone.id, tag_id=farmhouse.id, source="suggested"),
                AssetTag(asset_id=offline.id, tag_id=farmhouse.id, source="manual"),
                AssetTag(asset_id=pond.id, tag_id=art.id, source="manual"),
            ]
        )
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        assert (
            await client.post("/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN})
        ).status_code == 201

        async def search(q: str, **params: object) -> dict:
            resp = await client.get("/api/v1/search", params={"q": q, **params})
            assert resp.status_code == 200, resp.text
            return resp.json()

        yield {
            "client": client,
            "ids": ids,
            "search": search,
            "provider": provider,
            "factory": factory,
        }
    await engine.dispose()
    get_settings.cache_clear()
    clear_embedding_cache()


def _all_asset_ids(body: dict) -> set[str]:
    return {
        hit["asset_id"]
        for key in ("transcript_hits", "filename_hits", "visual_hits", "tag_hits", "results")
        for hit in body[key]
    }


# ------------------------------------------------------------ transcripts


BIND = r"%\(\w+\)s(?:::\w+)?"  # a bound parameter, perhaps with a cast


def _pg(stmt: object) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))  # type: ignore[attr-defined]


def test_postgres_transcripts_rank_by_relevance_not_time() -> None:
    sql = _pg(_pg_every_word("starting bid", None, None, 25))
    # The predicate is the indexed expression from migration 0005, unchanged.
    assert re.search(
        rf"WHERE .*to_tsvector\({BIND}, transcript_segments\.text\) "
        rf"@@ websearch_to_tsquery\({BIND}, {BIND}\)",
        sql,
        re.S,
    )
    order = sql.split("ORDER BY", 1)[1]
    assert re.match(
        r"\s*ts_rank_cd\(to_tsvector\(.*?transcript_segments\.text\), "
        r"websearch_to_tsquery\(.*?\)\) DESC",
        order,
    ), order
    assert "assets.availability" in sql


def test_postgres_any_word_fallback_is_an_or_query() -> None:
    excluded = uuid.uuid4()
    stmt = _pg_any_word(["drone", "farmhouse"], [excluded], None, "video", 10)
    compiled = stmt.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "to_tsquery(" in sql and "websearch_to_tsquery" not in sql
    assert "drone | farmhouse" in compiled.params.values()
    assert "NOT IN" in sql
    assert re.search(r"ORDER BY ts_rank_cd\(.*\) DESC", sql)
    assert "assets.media_type" in sql


def test_query_words_cannot_carry_tsquery_operators() -> None:
    words = _query_words("It's a 'barn' & (pond):* !x|y -- barn")
    assert words == ["it", "s", "a", "barn", "pond", "x", "y"]
    assert all(re.fullmatch(r"\w+", w) for w in words)


async def test_natural_language_query_still_finds_what_was_said(env: dict) -> None:
    """Every word of the question is never said in one breath."""
    body = await env["search"]("someone saying starting bid")
    starts = [hit["start_ms"] for hit in body["transcript_hits"]]
    assert starts, "the any-word fallback found nothing"
    assert set(starts[:2]) == {4200, 15000}


async def test_phrase_hits_rank_above_any_word_hits(env: dict) -> None:
    body = await env["search"]("starting bid")
    auction = str(env["ids"]["auction_walkthrough.mp4"])
    hits = [(h["asset_id"], h["start_ms"]) for h in body["transcript_hits"]]
    assert hits[0] == (auction, 4200)
    assert (auction, 15000) in hits


async def test_any_word_hits_rank_by_overlap(env: dict) -> None:
    body = await env["search"]("bid settlement thirty")
    assert body["transcript_hits"][0]["start_ms"] == 9800


async def test_a_quoted_phrase_is_not_loosened(env: dict) -> None:
    body = await env["search"]('"starting price"')
    assert [h["start_ms"] for h in body["transcript_hits"]] == [15000]


# ------------------------------------------------------------ tags


async def test_tag_inside_a_natural_query_is_found(env: dict) -> None:
    body = await env["search"]("drone shot of white farmhouse")
    hits = body["tag_hits"]
    assert {h["tag_name"] for h in hits} == {"Farmhouse"}
    assert [h["asset_id"] for h in hits] == [
        str(env["ids"]["farmhouse_front.jpg"]),
        str(env["ids"]["aerial_0042.mp4"]),
    ]
    # Confirmed first, then the suggestion.
    assert [h["confirmed"] for h in hits] == [True, False]


async def test_tag_inside_query_matches_whole_words_only(env: dict) -> None:
    body = await env["search"]("the starting bid")
    assert body["tag_hits"] == []  # "art" is inside "starting", not a word of it


# ------------------------------------------------------------ visual


async def test_one_clip_cannot_fill_the_visual_results(env: dict) -> None:
    body = await env["search"]("farmhouse from the air", limit=3)
    ids = [h["asset_id"] for h in body["visual_hits"]]
    assert len(ids) == len(set(ids)), "frames of one clip repeated"
    assert ids[:2] == [str(env["ids"]["aerial_0042.mp4"]), str(env["ids"]["farmhouse_front.jpg"])]
    # The clip's row is its best frame, with that frame's time.
    assert body["visual_hits"][0]["ts_ms"] == 6000
    assert body["visual_hits"][0]["similarity"] == pytest.approx(1.0)


# The Postgres path cannot run here (no pgvector in the test dialect), so
# AnnDb plays the HNSW index over an in-memory catalogue, faithfully to how
# pgvector answers: without an iterative scan it returns hnsw.ef_search
# frames and only then are the filters applied; with one, it keeps scanning
# (up to hnsw.max_scan_tuples) until the LIMIT has rows that pass.


class _Scalar:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value


class _Rows:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows

    def all(self) -> list[tuple[Any, ...]]:
        return self.rows


class AnnDb:
    def __init__(self, frames: list[tuple[str, str, int, float]], version: str) -> None:
        self.frames = sorted(frames, key=lambda f: f[3])  # (asset, media_type, ts_ms, distance)
        self.version = version
        self.statements: list[str] = []
        self.settings: dict[str, str] = {}
        self.windows: list[int] = []

    async def execute(self, statement: object) -> object:
        from sqlalchemy.sql.elements import TextClause

        if isinstance(statement, TextClause):
            sql = str(statement)
            self.statements.append(sql)
            if "extversion" in sql:
                return _Scalar(self.version)
            match = re.fullmatch(r"SET LOCAL (\S+) = (\S+)", sql)
            assert match, sql
            self.settings[match[1]] = match[2]
            return None
        params = cast(Any, statement).compile(dialect=postgresql.dialect()).params
        window, limit = params["param_1"], params["param_2"]
        self.windows.append(window)
        media_type = params.get("media_type_1")
        if self.settings.get("hnsw.iterative_scan") == "relaxed_order":
            scanned = self.frames[: int(self.settings["hnsw.max_scan_tuples"])]
        else:
            scanned = self.frames[: int(self.settings["hnsw.ef_search"])]
        passing = [f for f in scanned if media_type is None or f[1] == media_type][:window]
        best: dict[str, tuple[int, float]] = {}
        for asset, _, ts_ms, distance in passing:
            if asset not in best or distance < best[asset][1]:
                best[asset] = (ts_ms, distance)
        ordered = sorted(best.items(), key=lambda item: item[1][1])[:limit]
        return _Rows([(ts, asset, d, len(passing)) for asset, (ts, d) in ordered])


def _nearest_frames(media_type: str | None) -> Select[Any]:
    distance = Frame.embedding.cosine_distance(farm(1.0)).label("distance")
    return search_module._filtered(
        select(Frame.asset_id, Frame.ts_ms, distance).join(Asset, Asset.id == Frame.asset_id),
        None,
        media_type,
    )


def _photo_heavy_catalogue() -> list[tuple[str, str, int, float]]:
    """Four hundred drone stills nearest the query, then thirty short clips."""
    photos = [(f"photo-{i}", "image", 0, 0.10 + i / 10_000) for i in range(400)]
    clips = [
        (f"clip-{c}", "video", s * 5000, 0.30 + c / 1000 + s / 100_000)
        for c in range(30)
        for s in range(4)
    ]
    return photos + clips


@pytest.fixture()
def fresh_pgvector_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search_module, "_pgvector_version", None)


@pytest.mark.usefixtures("fresh_pgvector_version")
async def test_a_media_type_filter_is_not_starved_by_the_index_window() -> None:
    """search_media(media_type="video") on a photo-heavy catalogue: the 160
    nearest frames were all stills, so the filter left no clips at all."""
    db: Any = AnnDb(_photo_heavy_catalogue(), version="0.8.0")
    scored = await search_module._nearest_assets_pg(db, _nearest_frames("video"), 20)
    assert len(scored) == 20
    assert {asset for _, asset, _ in scored} <= {f"clip-{c}" for c in range(30)}
    assert "SET LOCAL hnsw.iterative_scan = relaxed_order" in db.statements
    assert "SET LOCAL hnsw.max_scan_tuples = 20000" in db.statements
    # Nearest first (the SQL that guarantees it is checked below).
    similarities = [sim for _, _, sim in scored]
    assert similarities == sorted(similarities, reverse=True)


@pytest.mark.usefixtures("fresh_pgvector_version")
async def test_long_clips_cannot_fill_the_frame_window() -> None:
    """Three clips with a hundred near-identical frames each held the whole
    160-frame window, and DISTINCT ON left two hits for a limit of 20."""
    frames = [
        (f"clip-{c}", "video", s * 1000, 0.05 + c / 100 + s / 100_000)
        for c in range(3)
        for s in range(100)
    ]
    frames += [(f"photo-{i}", "image", 0, 0.20 + i / 1000) for i in range(40)]
    db: Any = AnnDb(frames, version="0.8.0")
    scored = await search_module._nearest_assets_pg(db, _nearest_frames(None), 20)
    assert len(scored) == 20
    assert len({asset for _, asset, _ in scored}) == 20
    assert db.windows == [160, 640]  # widened once, then the window came back short


@pytest.mark.usefixtures("fresh_pgvector_version")
async def test_iterative_scan_is_not_set_on_an_older_pgvector() -> None:
    """pgvector before 0.8 rejects hnsw.iterative_scan, which would abort
    the search's transaction; the version is looked up once."""
    db: Any = AnnDb(_photo_heavy_catalogue(), version="0.7.4")
    await search_module._nearest_assets_pg(db, _nearest_frames("video"), 20)
    await search_module._nearest_assets_pg(db, _nearest_frames("video"), 20)
    assert not [s for s in db.statements if "iterative_scan" in s or "max_scan" in s]
    assert sum("extversion" in s for s in db.statements) == 1


def test_version_parsing_treats_the_unreadable_as_too_old() -> None:
    parse = search_module._parse_version
    assert parse("0.8.0") == (0, 8, 0) >= search_module.ITERATIVE_SCAN_SINCE
    assert parse("0.7.4") < search_module.ITERATIVE_SCAN_SINCE
    assert parse(None) < search_module.ITERATIVE_SCAN_SINCE
    assert parse("dev") < search_module.ITERATIVE_SCAN_SINCE


async def test_widening_stops_at_the_frame_window_ceiling() -> None:
    windows: list[int] = []

    async def always_full(window: int) -> tuple[list[str], int]:
        windows.append(window)
        return ["one clip"], window

    assert await search_module._widen_until_filled(always_full, 20) == ["one clip"]
    assert windows == [160, 640, 2560, search_module.MAX_FRAME_WINDOW]


def test_postgres_nearest_assets_are_reordered_above_the_scan() -> None:
    sql = _pg(search_module._best_frame_per_asset(_nearest_frames("video"), 160, 20))
    assert "DISTINCT ON" in sql
    assert "count(*) OVER ()" in sql  # frames in the window, counted before DISTINCT
    assert "assets.media_type" in sql
    # The outermost ORDER BY is by distance: under relaxed_order it is what
    # puts the nearest asset first.
    assert re.search(r"ORDER BY anon_1\.distance, anon_1\.asset_id\s+LIMIT", sql), sql


# ------------------------------------------------------------ filters


async def test_offline_assets_are_never_returned(env: dict) -> None:
    body = await env["search"]("farmhouse starting bid")
    assert str(env["ids"]["farmhouse_offline.jpg"]) not in _all_asset_ids(body)
    body = await env["search"]("farmhouse")
    assert str(env["ids"]["farmhouse_offline.jpg"]) not in _all_asset_ids(body)


@pytest.mark.parametrize("media_type", ["image", "video", "audio"])
async def test_media_type_filters_every_list(env: dict, media_type: str) -> None:
    body = await env["search"]("farmhouse", media_type=media_type)
    kinds = {
        hit["media_type"]
        for key in ("transcript_hits", "filename_hits", "visual_hits", "tag_hits", "results")
        for hit in body[key]
    }
    assert kinds == {media_type}


async def test_unknown_media_type_is_rejected(env: dict) -> None:
    resp = await env["client"].get("/api/v1/search", params={"q": "farm", "media_type": "pdf"})
    assert resp.status_code == 422


# ------------------------------------------------------------ fusion


async def test_results_fuse_every_strategy_one_row_per_asset(env: dict) -> None:
    body = await env["search"]("farmhouse")
    results = body["results"]
    ids = [r["asset_id"] for r in results]
    assert len(ids) == len(set(ids))

    top = results[0]
    # Tagged, looks like one, and named one: the operator's own word leads.
    assert top["asset_id"] == str(env["ids"]["farmhouse_front.jpg"])
    assert top["reasons"] == ["tag", "visual", "name"]
    assert top["tag"] == "Farmhouse"

    by_id = {r["asset_id"]: r for r in results}
    drone = by_id[str(env["ids"]["aerial_0042.mp4"])]
    assert drone["reasons"] == ["suggested_tag", "visual"]
    assert drone["ts_ms"] == 6000 and drone["frame_ms"] == 6000

    spoken = by_id[str(env["ids"]["owner_interview.mp3"])]
    assert spoken["reasons"] == ["transcript"]
    assert spoken["ts_ms"] == 2500
    assert spoken["frame_ms"] is None
    assert "farmhouse" in spoken["snippet"]
    assert all(r["score"] > 0 for r in results)
    assert [r["score"] for r in results] == sorted((r["score"] for r in results), reverse=True)


def _hit(cls: type, asset_id: uuid.UUID, **fields: object) -> object:
    base = {
        "asset_id": asset_id,
        "filename": f"{asset_id}.mp4",
        "library_id": uuid.uuid4(),
        "media_type": "video",
    }
    return cls(**base, **fields)


def test_speech_sets_the_moment_and_a_frame_sets_the_thumbnail() -> None:
    clip = uuid.uuid4()
    results = fuse_results(
        tag_hits=[],
        visual_hits=[_hit(VisualHit, clip, ts_ms=8000, similarity=0.3)],
        transcript_hits=[
            _hit(TranscriptHit, clip, start_ms=42000, end_ms=45000, text="sold"),
            _hit(TranscriptHit, clip, start_ms=1000, end_ms=2000, text="later hit"),
        ],
        filename_hits=[],
        limit=10,
    )
    assert len(results) == 1
    only = results[0]
    assert (only.ts_ms, only.frame_ms, only.snippet) == (42000, 8000, "sold")
    assert only.reasons == ["visual", "transcript"]


def test_confirmed_tag_outranks_suggestion_and_name() -> None:
    confirmed, suggested, named = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    def tag(asset_id: uuid.UUID, ok: bool) -> object:
        return _hit(TagHit, asset_id, tag_name="Barn", tag_slug="barn", confirmed=ok)

    from framefound.api.v1.search import FilenameHit

    results = fuse_results(
        tag_hits=[tag(suggested, False), tag(confirmed, True)],
        visual_hits=[],
        transcript_hits=[],
        filename_hits=[_hit(FilenameHit, named, captured_at=None)],
        limit=10,
    )
    assert results[0].asset_id == confirmed
    assert {r.asset_id for r in results[1:]} == {suggested, named}
    assert {tuple(r.reasons) for r in results[1:]} == {("suggested_tag",), ("name",)}


def test_fuse_counts_a_repeated_key_once_per_strategy() -> None:
    hits = {h.key: h for h in fuse({"transcript": ["a", "a", "a"], "visual": ["b"]})}
    assert hits["a"].score == pytest.approx(hits["b"].score)


# ------------------------------------------------------------ embedding


class RecordingLog:
    """Stands in for the module's structlog logger (which caches its
    processors on first use, so structlog's capture_logs cannot see it)."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, str, dict]] = []

    def __getattr__(self, level: str):  # noqa: ANN204
        def record(event: str, **kw: object) -> None:
            self.entries.append((level, event, kw))

        return record


async def test_an_embedding_crash_is_logged_not_called_indexing(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(embeddings_module, "_provider", BrokenProvider(ValueError("boom")))
    log = RecordingLog()
    monkeypatch.setattr(search_module, "log", log)
    body = await env["search"]("farmhouse")
    assert body["visual_hits"] == [] and body["visual_available"] is False
    # The page shows "still indexing" only for "indexing"; a crash used to
    # come back as the very same false and was shown as a wait.
    assert body["visual_status"] == "unavailable"
    assert body["tag_hits"], "the other retrievers must keep answering"
    assert log.entries == [("warning", "search.embed_failed", {"exc_info": True})]


async def test_a_missing_model_is_not_logged_as_a_failure(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        embeddings_module, "_provider", BrokenProvider(EmbeddingUnavailable("no model"))
    )
    log = RecordingLog()
    monkeypatch.setattr(search_module, "log", log)
    body = await env["search"]("farmhouse")
    assert body["visual_available"] is False
    # Frames are indexed; no model will not resolve itself by waiting.
    assert body["visual_status"] == "unavailable"
    assert log.entries == []


async def test_visual_status_tells_indexing_from_ready(env: dict) -> None:
    body = await env["search"]("farmhouse")
    assert (body["visual_status"], body["visual_available"]) == ("ready", True)

    async with env["factory"]() as db:
        await db.execute(update(Frame).values(embedding=None))
        await db.commit()
    body = await env["search"]("farmhouse")
    assert (body["visual_status"], body["visual_available"]) == ("indexing", False)


async def test_query_vectors_are_cached(env: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    provider: FakeProvider = env["provider"]
    await env["search"]("farmhouse")
    await env["search"]("farmhouse")
    await env["search"]("  farmhouse ")  # the same query, differently spaced
    assert provider.calls == 1
    cached = search_module._cached_text_vector("farmhouse")
    assert isinstance(cached, tuple) and all(isinstance(x, float) for x in cached)

    clear_embedding_cache()
    await env["search"]("farmhouse")
    assert provider.calls == 2

    # A different model must not be answered from the old one's vectors.
    replacement = FakeProvider()
    monkeypatch.setattr(embeddings_module, "_provider", replacement)
    await env["search"]("farmhouse")
    assert replacement.calls == 1
