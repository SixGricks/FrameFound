"""The MCP connector, end to end: a real MCP client over the real transport,
in process, against the same app and database the API tests use.

The properties worth pinning are the ones that make it safe to hand Claude
a token: no token, no answer; a read-only token cannot write; a proposal is
a suggestion and not a fact; and what Claude sees is what the operator
sees — the same search, the same tags, the same thumbnails.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import Asset, AssetTag, Derivative, Library, Tag

pytest.importorskip("mcp.server.mcpserver")
import httpx2  # noqa: E402  (the SDK's own HTTP client)
from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}
BASE = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
MCP_URL = "http://t/api/mcp"


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict]:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'mcp.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "mcp-test-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", db_url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRAMEFOUND_MEDIA_ROOT", str(tmp_path))
    get_settings.cache_clear()

    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    # The learner runs on a worker; capture what would be queued instead.
    learned: list[str] = []
    monkeypatch.setattr(
        "framefound.api.v1.tags._relearn", lambda tag_id: learned.append(str(tag_id))
    )

    from framefound.main import create_app

    app = create_app()
    app.dependency_overrides[get_session] = override

    ids: dict[str, uuid.UUID] = {}
    async with factory() as db:
        library = Library(name="Intel", root_path=str(tmp_path / "intel"))
        db.add(library)
        await db.flush()
        for name in ("barn-wide.jpg", "barn-and-silo.jpg", "kitchen.jpg"):
            asset = Asset(
                library_id=library.id,
                relative_path=f"2026/{name}",
                filename=name,
                extension="jpg",
                media_type="image",
                size_bytes=1000,
                mtime=BASE,
                availability="online",
                captured_at=BASE,
            )
            db.add(asset)
            await db.flush()
            ids[name] = asset.id
            relpath = f"derivatives/{str(asset.id)[:2]}/{asset.id}/thumbnail.webp"
            target = tmp_path / "data" / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (96, 64), (40, 90, 200)).save(target, "WEBP")
            db.add(
                Derivative(
                    asset_id=asset.id,
                    kind="thumbnail",
                    relative_path=relpath,
                    media_format="webp",
                    status="ready",
                )
            )
        barn, silo = Tag(name="Barn", slug="barn"), Tag(name="Silo", slug="silo")
        db.add_all([barn, silo])
        await db.flush()
        db.add_all(
            [
                AssetTag(asset_id=ids["barn-wide.jpg"], tag_id=barn.id, source="manual"),
                AssetTag(asset_id=ids["barn-and-silo.jpg"], tag_id=barn.id, source="confirmed"),
                AssetTag(asset_id=ids["barn-and-silo.jpg"], tag_id=silo.id, source="manual"),
                # A learner guess on the kitchen, unjudged.
                AssetTag(
                    asset_id=ids["kitchen.jpg"],
                    tag_id=silo.id,
                    source="suggested",
                    confidence=0.61,
                    origin="clip",
                ),
            ]
        )
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        assert (
            await client.post("/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN})
        ).status_code == 201
        tagger = (
            await client.post(
                "/api/v1/auth/panel-tokens",
                json={"name": "Claude on the laptop", "host": "mcp", "scopes": ["read", "tag"]},
            )
        ).json()["token"]
        reader = (
            await client.post(
                "/api/v1/auth/panel-tokens", json={"name": "Read only", "host": "mcp"}
            )
        ).json()["token"]
        yield {
            "app": app,
            "client": client,
            "ids": ids,
            "factory": factory,
            "tagger": tagger,
            "reader": reader,
            "learned": learned,
        }
    await engine.dispose()
    get_settings.cache_clear()


@asynccontextmanager
async def serving(app) -> AsyncIterator[None]:  # type: ignore[no-untyped-def]
    """The app's lifespan, which runs the MCP transport's session manager.
    Entered in the test itself rather than the fixture: the manager's task
    group must be left in the task that entered it, and pytest-asyncio tears
    a generator fixture down in a different one."""
    async with app.router.lifespan_context(app):
        yield


@asynccontextmanager
async def connect(app, token: str) -> AsyncIterator[ClientSession]:  # type: ignore[no-untyped-def]
    """A real MCP client session over the streamable HTTP transport, routed
    into the ASGI app in process. Needs `serving` around it."""
    async with (
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://t",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        ) as http,
        streamable_http_client(MCP_URL, http_client=http, terminate_on_close=False) as (
            read,
            write,
        ),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


def _text(result) -> str:  # type: ignore[no-untyped-def]
    return "\n".join(block.text for block in result.content if getattr(block, "type", "") == "text")


# --- the gate -------------------------------------------------------------


async def test_the_connector_refuses_without_a_token(env: dict) -> None:
    resp = await env["client"].post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"].startswith("Bearer")
    assert "panel token" in resp.json()["error"]["message"]


@pytest.mark.parametrize("bogus", ["ffp_made-up", "Bearer", "sk-ant-something"])
async def test_a_token_that_was_never_issued_is_refused(env: dict, bogus: str) -> None:
    resp = await env["client"].post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers={
            "Authorization": f"Bearer {bogus}",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert resp.status_code == 401


async def test_a_revoked_token_stops_working(env: dict) -> None:
    client = env["client"]
    listed = (await client.get("/api/v1/auth/panel-tokens")).json()
    for token in listed:
        await client.delete(f"/api/v1/auth/panel-tokens/{token['id']}")
    resp = await client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers={
            "Authorization": f"Bearer {env['tagger']}",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert resp.status_code == 401


# --- the tools ------------------------------------------------------------


async def test_the_tools_are_what_the_docs_promise(env: dict) -> None:
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        names = {tool.name for tool in (await session.list_tools()).tools}
    assert names == {
        "overview",
        "list_tags",
        "find_media",
        "search_media",
        "get_media",
        "view_media",
        "review_queue",
        "propose_tags",
        "decide_tag",
        "decide_tags",
        "api_review_status",
        "start_api_review",
        # Making video (framefound/mcp/video.py; test_mcp_video.py).
        "path_profiles",
        "get_media_files",
        "get_transcript",
        "get_scenes",
        "build_rough_cut",
        # Choosing shots (framefound/mcp/shots.py; test_mcp_shots.py).
        "find_shots",
        "contact_sheet",
        "recent_cuts",
    }


async def test_the_overview_names_the_library_and_what_the_token_may_do(env: dict) -> None:
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        result = await session.call_tool("overview", {})
    assert not result.is_error
    body = result.structured_content
    assert body["libraries"] == [
        {
            "id": str(next(iter(env["ids"].values()))) and body["libraries"][0]["id"],
            "name": "Intel",
            "assets": 3,
        }
    ]
    assert body["can_tag"] is False and body["scopes"] == ["read"]
    assert body["tags"] == 2 and body["pending_suggestions"] == 1
    assert body["connected_as"].startswith("Read only")


async def test_find_media_by_tags_all_and_any(env: dict) -> None:
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        both = await session.call_tool(
            "find_media", {"tags": ["barn", "Silo"], "match": "all", "verbose": True}
        )
        either = await session.call_tool(
            "find_media", {"tags": ["barn", "silo"], "match": "any", "verbose": True}
        )
        with_guesses = await session.call_tool(
            "find_media",
            {"tags": ["silo"], "match": "any", "include_suggested": True, "verbose": True},
        )
        unknown = await session.call_tool("find_media", {"tags": ["tractor"]})
    assert [i["filename"] for i in both.structured_content["items"]] == ["barn-and-silo.jpg"]
    assert both.structured_content["items"][0]["tags"] == ["Barn", "Silo"]
    assert both.structured_content["items"][0]["preview_url"].startswith("http://t/api/v1/media/")
    assert both.structured_content["items"][0]["path"].endswith("2026/barn-and-silo.jpg")
    assert {i["filename"] for i in either.structured_content["items"]} == {
        "barn-wide.jpg",
        "barn-and-silo.jpg",
    }
    assert {i["filename"] for i in with_guesses.structured_content["items"]} == {
        "barn-and-silo.jpg",
        "kitchen.jpg",
    }, "a suggestion counts only when asked for"
    assert unknown.is_error and "tractor" in _text(unknown)


async def test_the_preview_link_opens_without_a_session(env: dict) -> None:
    """A signed link: the same mechanism the panels use, so Claude can hand
    the operator a link that works in a browser that is not signed in."""
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        page = await session.call_tool("find_media", {"tags": ["silo"], "verbose": True})
    url = page.structured_content["items"][0]["preview_url"]
    fresh = AsyncClient(transport=ASGITransport(app=env["app"]), base_url="http://t")
    async with fresh:
        resp = await fresh.get(url.removeprefix("http://t"))
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/webp"


async def test_view_media_returns_the_picture_and_its_tags(env: dict) -> None:
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        result = await session.call_tool("view_media", {"id": str(env["ids"]["kitchen.jpg"])})
    assert not result.is_error
    kinds = [block.type for block in result.content]
    assert "image" in kinds and "text" in kinds
    image = next(block for block in result.content if block.type == "image")
    assert image.mime_type == "image/jpeg" and len(image.data) > 100
    assert "Suggested, unjudged: Silo" in _text(result)


async def test_search_merges_tag_and_name_hits(env: dict) -> None:
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        result = await session.call_tool("search_media", {"query": "barn", "verbose": True})
    hits = result.structured_content["hits"]
    by_name = {hit["filename"]: hit for hit in hits}
    assert set(by_name) == {"barn-wide.jpg", "barn-and-silo.jpg"}
    assert "tag" in by_name["barn-wide.jpg"]["matched"]
    assert "name" in by_name["barn-wide.jpg"]["matched"]
    assert result.structured_content["visual_search_available"] is False


async def test_a_read_only_token_cannot_propose_or_decide(env: dict) -> None:
    kitchen = str(env["ids"]["kitchen.jpg"])
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        proposed = await session.call_tool(
            "propose_tags", {"id": kitchen, "tags": ["kitchen island"]}
        )
        decided = await session.call_tool(
            "decide_tag", {"id": kitchen, "tag": "silo", "accept": True}
        )
    assert proposed.is_error and "'tag' scope" in _text(proposed)
    assert decided.is_error
    async with env["factory"]() as db:
        assert (
            await db.execute(select(Tag).where(Tag.slug == "kitchen-island"))
        ).scalar_one_or_none() is None
        link = (
            await db.execute(select(AssetTag).where(AssetTag.asset_id == env["ids"]["kitchen.jpg"]))
        ).scalar_one()
        assert link.source == "suggested", "nothing changed"


async def test_proposals_enter_the_review_queue_marked_as_claudes(env: dict) -> None:
    kitchen = str(env["ids"]["kitchen.jpg"])
    async with serving(env["app"]), connect(env["app"], env["tagger"]) as session:
        result = await session.call_tool(
            "propose_tags",
            {
                "id": kitchen,
                "tags": ["Kitchen Island", "silo", "interior"],
                "note": "pendants over an island",
            },
        )
        queue = await session.call_tool("review_queue", {"origin": "mcp"})
    assert not result.is_error
    outcomes = {o["tag"]: o["outcome"] for o in result.structured_content["outcomes"]}
    assert outcomes["kitchen island"] == "suggested (new tag)"
    assert outcomes["interior"] == "suggested (new tag)"
    assert outcomes["Silo"] == "already suggested", "the learner's guess is left as it is"
    queued = {(row["tag"], row["origin"]) for row in queue.structured_content["result"]}
    assert queued == {("kitchen island", "mcp"), ("interior", "mcp")}
    async with env["factory"]() as db:
        links = (
            (
                await db.execute(
                    select(AssetTag).where(
                        AssetTag.asset_id == env["ids"]["kitchen.jpg"], AssetTag.origin == "mcp"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert all(link.source == "suggested" for link in links), "a proposal is never a fact"
    assert env["learned"] == [], "proposing does not train anything; deciding does"


async def test_deciding_records_the_answer_and_retrains(env: dict) -> None:
    kitchen = str(env["ids"]["kitchen.jpg"])
    async with serving(env["app"]), connect(env["app"], env["tagger"]) as session:
        rejected = await session.call_tool(
            "decide_tag", {"id": kitchen, "tag": "silo", "accept": False}
        )
        applied = await session.call_tool(
            "decide_tag", {"id": kitchen, "tag": "Kitchen", "accept": True}
        )
        queue = await session.call_tool("review_queue", {})
    assert rejected.structured_content["source"] == "rejected"
    assert applied.structured_content["source"] == "manual", (
        "no suggestion to confirm: applied outright"
    )
    assert queue.structured_content["result"] == []
    assert len(env["learned"]) == 2, "both answers teach the learner"
    async with env["factory"]() as db:
        silo = (await db.execute(select(Tag).where(Tag.slug == "silo"))).scalar_one()
        link = (
            await db.execute(
                select(AssetTag).where(
                    AssetTag.asset_id == env["ids"]["kitchen.jpg"], AssetTag.tag_id == silo.id
                )
            )
        ).scalar_one()
        assert link.source == "rejected" and link.confidence is None


async def test_the_api_reviewer_reports_its_budget_and_needs_the_scope(env: dict) -> None:
    async with serving(env["app"]):
        async with connect(env["app"], env["reader"]) as session:
            status = await session.call_tool("api_review_status", {})
            start = await session.call_tool("start_api_review", {})
        body = status.structured_content
        assert body["configured"] is False and body["budget_usd"] == 5.0
        # The kitchen carries only the CLIP learner's guess, which no longer
        # takes a picture out of the review pool (tag_review._untagged_filter):
        # the learner can only spread words already taught, so Claude has
        # still not looked at it. The two barns have a person's tags.
        assert body["untagged"] == 1, "only the kitchen awaits a first opinion"
        assert start.is_error and "'tag' scope" in _text(start)
        async with connect(env["app"], env["tagger"]) as session:
            start = await session.call_tool("start_api_review", {})
    assert start.is_error and "Anthropic key" in _text(start)
