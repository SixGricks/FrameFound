"""Claude's second opinion on tags: the request, the answer, the admission
policy, and the run under the budget.

The reviewer never talks to Anthropic here. What needs proving is ours —
that the tool is offered rather than forced, that a reworded "match"
cannot attach to the wrong tag, that proposals land as *suggestions* the
operator still judges, and that the cap stops a run before the call that
would cross it.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.ai import spend, tag_review
from framefound.ai.tag_review import AdmissionPolicy, TagProposal, admit
from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import AiUsage, Asset, AssetTag, Derivative, Library, Tag

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}
BASE = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)


# --- the request ----------------------------------------------------------


def test_the_tool_is_offered_with_a_strict_schema_not_forced() -> None:
    """Current models return 400 on a forced tool_choice; auto plus strict
    plus the instruction works on every model, so there is one code path."""
    body = tag_review._body(b"\xff\xd8fake", "claude-haiku-5-5", "Vocabulary: ...")
    assert body["tool_choice"] == {"type": "auto"}
    assert body["tools"][0]["strict"] is True
    assert body["tools"][0]["name"] == "review_tags"
    assert body["output_config"] == {"effort": "low"}
    image, text = body["messages"][0]["content"]
    assert image["type"] == "image" and image["source"]["media_type"] == "image/jpeg"
    assert "review_tags" in text["text"]


def test_the_vocabulary_is_cached_and_the_rules_come_first() -> None:
    body = tag_review._body(b"x", "claude-sonnet-5-5", "Vocabulary:\n- barn (3)")
    rules, vocabulary = body["system"]
    assert "cataloguer" in rules["text"]
    assert vocabulary["cache_control"] == {"type": "ephemeral"}
    assert "barn (3)" in vocabulary["text"]


def test_models_that_reject_effort_are_not_sent_it() -> None:
    assert "output_config" not in tag_review._body(b"x", "claude-haiku-4-5", "v")


def test_the_vocabulary_lists_the_most_used_tags_first_and_is_capped() -> None:
    rows = [(f"tag {i}", i) for i in range(tag_review.MAX_VOCABULARY + 50)]
    text = tag_review.vocabulary_text(rows)
    lines = text.splitlines()[1:]
    assert len(lines) == tag_review.MAX_VOCABULARY
    assert lines[0].startswith(f"- tag {tag_review.MAX_VOCABULARY + 49}")
    assert "empty" in tag_review.vocabulary_text([])


# --- the answer -----------------------------------------------------------


def _payload(matches: list[dict], new_tags: list[dict], description: str = "A barn") -> dict:
    return {
        "content": [
            {"type": "thinking", "thinking": "", "signature": "x"},
            {
                "type": "tool_use",
                "name": "review_tags",
                "input": {"matches": matches, "new_tags": new_tags, "description": description},
            },
        ],
        "usage": {"input_tokens": 1500, "output_tokens": 120},
    }


def test_the_answer_is_cleaned_and_deduplicated() -> None:
    proposals, description = tag_review.parse_answer(
        _payload(
            [{"tag": "  Power  Broom ", "confidence": 0.9}, {"tag": "barn", "confidence": 1.7}],
            [
                {"tag": "power-broom", "confidence": 0.4, "why": "dup"},
                {"tag": "Horse Barn", "confidence": 0.81, "why": "a red barn with stalls"},
                {"tag": "!!!", "confidence": 0.9, "why": "nothing usable"},
            ],
        ),
        vocabulary_slugs={"power-broom", "barn"},
    )
    assert description == "A barn"
    by_name = {p.name: p for p in proposals}
    assert set(by_name) == {"power broom", "barn", "horse barn"}
    assert by_name["power broom"].confidence == 0.9, "the stronger duplicate is kept"
    assert by_name["barn"].confidence == 1.0, "clamped"
    assert by_name["horse barn"].existing is False and "stalls" in by_name["horse barn"].why
    assert [p.confidence for p in proposals] == sorted(
        (p.confidence for p in proposals), reverse=True
    )


def test_a_match_the_vocabulary_does_not_contain_is_treated_as_new() -> None:
    """The model occasionally rewords a tag. A misspelt "match" must never
    attach to an existing tag it does not actually name."""
    proposals, _ = tag_review.parse_answer(
        _payload([{"tag": "power brooms", "confidence": 0.9}], []), vocabulary_slugs={"power-broom"}
    )
    assert proposals[0].existing is False


def test_a_refusal_or_an_empty_answer_is_an_error_not_an_empty_list() -> None:
    from framefound.ai.recipe_picker import RecipePickUnavailable

    with pytest.raises(RecipePickUnavailable, match="declined"):
        tag_review.parse_answer({"stop_reason": "refusal", "content": []}, set())
    with pytest.raises(RecipePickUnavailable, match="no answer"):
        tag_review.parse_answer({"content": [{"type": "text", "text": "hi"}]}, set())


def test_ask_for_tags_posts_once_and_reports_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    captured: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: ANN001
        captured["body"] = json
        captured["headers"] = headers
        return httpx.Response(200, json=_payload([{"tag": "barn", "confidence": 0.8}], []))

    monkeypatch.setattr(httpx, "post", fake_post)
    answer = tag_review.ask_for_tags(
        b"\xff\xd8fake", "sk-ant-test", "claude-haiku-5-5", [("barn", 3)]
    )
    assert captured["headers"]["x-api-key"] == "sk-ant-test"
    assert captured["body"]["model"] == "claude-haiku-5-5"
    assert answer.usage == {
        "input_tokens": 1500,
        "output_tokens": 120,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    assert answer.proposals[0].name == "barn" and answer.proposals[0].existing


# --- admission ------------------------------------------------------------


def _p(name: str, confidence: float, existing: bool) -> TagProposal:
    return TagProposal(name=name, confidence=confidence, existing=existing)


def test_a_vocabulary_match_needs_moderate_confidence_a_new_tag_needs_more() -> None:
    kept = admit([_p("barn", 0.55, True), _p("silo", 0.55, False), _p("pond", 0.8, False)])
    assert [p.name for p in kept] == ["pond", "barn"]


def test_long_names_are_refused_and_the_list_is_capped() -> None:
    policy = AdmissionPolicy(max_per_asset=2)
    kept = admit(
        [
            _p("a very long tag name", 0.99, False),
            _p("one", 0.9, True),
            _p("two", 0.8, True),
            _p("three", 0.7, True),
        ],
        policy,
    )
    assert [p.name for p in kept] == ["one", "two"]


def test_the_policy_can_refuse_new_tags_entirely() -> None:
    policy = AdmissionPolicy(new_floor=1.01)
    assert admit([_p("new thing", 0.99, False), _p("barn", 0.6, True)], policy) == [
        _p("barn", 0.6, True)
    ]


# --- the catalogue side ---------------------------------------------------


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict]:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'review.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "review-test-secret")
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

    from framefound.main import create_app

    app = create_app()
    app.dependency_overrides[get_session] = override

    ids: list[uuid.UUID] = []
    async with factory() as db:
        library = Library(name="Intel", root_path=str(tmp_path))
        db.add(library)
        await db.flush()
        for index, name in enumerate(("barn.jpg", "field.jpg", "tagged.jpg", "nothumb.jpg")):
            asset = Asset(
                library_id=library.id,
                relative_path=name,
                filename=name,
                extension="jpg",
                media_type="image",
                size_bytes=1000,
                mtime=BASE,
                availability="online",
                first_indexed_at=datetime(2026, 6, 1 + index, tzinfo=UTC),
            )
            db.add(asset)
            await db.flush()
            ids.append(asset.id)
            if name != "nothumb.jpg":
                relpath = f"derivatives/{str(asset.id)[:2]}/{asset.id}/thumbnail.webp"
                target = tmp_path / "data" / relpath
                target.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (96, 64), (180, 40, 40)).save(target, "WEBP")
                db.add(
                    Derivative(
                        asset_id=asset.id,
                        kind="thumbnail",
                        relative_path=relpath,
                        media_format="webp",
                        status="ready",
                    )
                )
        barn = Tag(name="Barn", slug="barn")
        db.add(barn)
        await db.flush()
        # tagged.jpg already carries a tag: it is not "untagged".
        db.add(AssetTag(asset_id=ids[2], tag_id=barn.id, source="manual"))
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        assert (
            await client.post("/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN})
        ).status_code == 201
        yield {"client": client, "ids": ids, "factory": factory, "app": app}
    await engine.dispose()
    get_settings.cache_clear()


async def test_untagged_means_a_thumbnail_and_no_opinion_of_any_kind(env: dict) -> None:
    async with env["factory"]() as db:
        picked = await tag_review.pick_untagged(db, 10)
        assert set(picked) == {env["ids"][0], env["ids"][1]}, (
            "not the tagged one, not the one with no thumbnail"
        )
        assert picked[0] == env["ids"][1], "newest first"
        assert await tag_review.count_untagged(db) == 2


async def test_a_learner_guess_does_not_take_a_picture_away_from_claude(env: dict) -> None:
    """New media is scored against every tag the moment it is embedded, hours
    before the nightly review. When any row counted as an opinion, the
    learner's guesses took most of a new shoot out of Claude's pool, and
    Claude reviewed only what the learner had nothing to say about."""
    async with env["factory"]() as db:
        barn = (await db.execute(select(Tag).where(Tag.slug == "barn"))).scalar_one()
        db.add(AssetTag(asset_id=env["ids"][0], tag_id=barn.id, source="suggested", origin="clip"))
        # A suggestion written before `origin` existed is the learner's too.
        db.add(AssetTag(asset_id=env["ids"][1], tag_id=barn.id, source="suggested"))
        await db.commit()
        assert set(await tag_review.pick_untagged(db, 10)) == {env["ids"][0], env["ids"][1]}
        assert await tag_review.count_untagged(db) == 2


async def test_claude_having_looked_takes_a_picture_out_of_the_pool(env: dict) -> None:
    """Claude's own suggestion is an opinion. So is an API review that wrote
    nothing — Claude agreeing with the learner, or the admission policy
    keeping nothing — or the nightly sweep would pay to review the same
    newest pictures every night. A failed call is not a review."""
    async with env["factory"]() as db:
        barn = (await db.execute(select(Tag).where(Tag.slug == "barn"))).scalar_one()
        db.add(AssetTag(asset_id=env["ids"][0], tag_id=barn.id, source="suggested", origin="mcp"))
        db.add(AssetTag(asset_id=env["ids"][1], tag_id=barn.id, source="suggested", origin="clip"))
        db.add(
            AiUsage(
                purpose=spend.TAG_REVIEW, model="claude-haiku-5-5", asset_id=env["ids"][1], ok=False
            )
        )
        await db.commit()
        assert await tag_review.pick_untagged(db, 10) == [env["ids"][1]], (
            "Claude's suggestion counts; a failed call does not"
        )
        db.add(AiUsage(purpose=spend.TAG_REVIEW, model="claude-haiku-5-5", asset_id=env["ids"][1]))
        await db.commit()
        assert await tag_review.pick_untagged(db, 10) == []
        assert await tag_review.count_untagged(db) == 0


async def test_proposals_become_suggestions_and_leave_decisions_alone(env: dict) -> None:
    async with env["factory"]() as db:
        barn = (await db.execute(select(Tag).where(Tag.slug == "barn"))).scalar_one()
        db.add(AssetTag(asset_id=env["ids"][0], tag_id=barn.id, source="rejected"))
        await db.commit()
        outcomes = await tag_review.write_proposals(
            db,
            env["ids"][0],
            [_p("barn", 0.9, True), _p("silo", 0.8, False), _p("!!", 0.9, False)],
            origin="claude",
        )
        await db.commit()
    assert outcomes["Barn"] == tag_review.OUTCOME_REJECTED, "a rejection stays rejected"
    assert outcomes["silo"] == tag_review.OUTCOME_NEW_TAG
    assert outcomes["!!"] == tag_review.OUTCOME_UNUSABLE
    async with env["factory"]() as db:
        silo = (await db.execute(select(Tag).where(Tag.slug == "silo"))).scalar_one()
        link = (
            await db.execute(
                select(AssetTag).where(
                    AssetTag.asset_id == env["ids"][0], AssetTag.tag_id == silo.id
                )
            )
        ).scalar_one()
        assert link.source == "suggested" and link.origin == "claude" and link.confidence == 0.8
        again = await tag_review.write_proposals(
            db, env["ids"][0], [_p("silo", 0.5, True)], origin="mcp"
        )
    assert again["silo"] == tag_review.OUTCOME_PENDING


# --- the run --------------------------------------------------------------


async def test_a_run_reviews_the_pictures_records_the_spend_and_queues_suggestions(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from framefound.media.maps_store import load_review_state
    from framefound.processing import tag_review_tasks

    client = env["client"]
    assert (
        await client.put(
            "/api/v1/develop/settings/ai",
            json={
                "api_key": "sk-ant-test",
                "monthly_budget_usd": 2.0,
                "review_model": "claude-haiku-5-5",
            },
        )
    ).status_code == 200

    seen: list[tuple[str, list[str]]] = []

    def fake_ask(preview: bytes, key: str, model: str, vocabulary: list) -> tag_review.ReviewAnswer:
        assert preview[:2] == b"\xff\xd8", "a JPEG preview, made from the thumbnail"
        assert key == "sk-ant-test"
        seen.append((model, [name for name, _ in vocabulary]))
        return tag_review.ReviewAnswer(
            proposals=[
                _p("barn", 0.9, True),
                _p("silo", 0.85, False),
                _p("weak guess", 0.2, False),
            ],
            description="A red barn",
            usage={"input_tokens": 1_000_000, "output_tokens": 0},  # $0.10 on Haiku 5.5
        )

    monkeypatch.setattr(tag_review, "ask_for_tags", fake_ask)
    await asyncio.to_thread(
        tag_review_tasks.review_tags_with_claude, [str(env["ids"][0]), str(env["ids"][1])]
    )

    assert [model for model, _ in seen] == ["claude-haiku-5-5"] * 2
    assert "silo" in seen[1][1], "a tag created by the first picture is vocabulary for the second"
    async with env["factory"]() as db:
        rows = (await db.execute(select(AiUsage))).scalars().all()
        assert len(rows) == 2 and all(
            r.purpose == "tag_review" and r.cost_usd == pytest.approx(0.1) for r in rows
        )
        links = (
            (await db.execute(select(AssetTag).where(AssetTag.source == "suggested")))
            .scalars()
            .all()
        )
        assert len(links) == 4, "barn + silo on each picture; the weak guess was not admitted"
        assert {link.origin for link in links} == {"claude"}
        state = await load_review_state(db)
    assert state.state == "done" and state.reviewed == 2
    assert state.suggested == 4 and state.new_tags == 1
    assert state.spent_usd == pytest.approx(0.2)

    status = (await client.get("/api/v1/tags/review/claude")).json()
    assert status["spent_this_month_usd"] == pytest.approx(0.2)
    assert status["pending_from_claude"] == 4
    assert status["untagged"] == 0
    assert status["last"]["state"] == "done"


async def test_the_budget_stops_a_run_before_the_call_that_would_cross_it(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from framefound.media.maps_store import load_review_state
    from framefound.processing import tag_review_tasks

    client = env["client"]
    # Room for exactly one Haiku call under the estimate, not two.
    one_call = spend.estimate_call("claude-haiku-5-5") * 1.5
    await client.put(
        "/api/v1/develop/settings/ai",
        json={
            "api_key": "sk-ant-test",
            "monthly_budget_usd": one_call,
            "review_model": "claude-haiku-5-5",
        },
    )
    calls = {"n": 0}

    def fake_ask(preview: bytes, key: str, model: str, vocabulary: list) -> tag_review.ReviewAnswer:
        calls["n"] += 1
        return tag_review.ReviewAnswer(
            proposals=[], description="", usage={"input_tokens": 12_000, "output_tokens": 100}
        )

    monkeypatch.setattr(tag_review, "ask_for_tags", fake_ask)
    await asyncio.to_thread(
        tag_review_tasks.review_tags_with_claude, [str(env["ids"][0]), str(env["ids"][1])]
    )
    assert calls["n"] == 1, "the second call would have crossed the cap, so it was never made"
    async with env["factory"]() as db:
        state = await load_review_state(db)
    assert state.state == "budget" and "budget reached" in state.message


async def test_an_account_error_stops_the_run(env: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from framefound.ai.recipe_picker import RecipePickUnavailable
    from framefound.media.maps_store import load_review_state
    from framefound.processing import tag_review_tasks

    await env["client"].put("/api/v1/develop/settings/ai", json={"api_key": "sk-ant-test"})
    calls = {"n": 0}

    def broke(preview: bytes, key: str, model: str, vocabulary: list) -> tag_review.ReviewAnswer:
        calls["n"] += 1
        raise RecipePickUnavailable("Anthropic API returned 401 (authentication_error)", fatal=True)

    monkeypatch.setattr(tag_review, "ask_for_tags", broke)
    await asyncio.to_thread(
        tag_review_tasks.review_tags_with_claude, [str(env["ids"][0]), str(env["ids"][1])]
    )
    assert calls["n"] == 1
    async with env["factory"]() as db:
        state = await load_review_state(db)
        failed = (await db.execute(select(AiUsage).where(AiUsage.ok.is_(False)))).scalars().all()
    assert state.state == "failed" and "401" in state.message
    assert len(failed) == 1 and failed[0].cost_usd is None


# --- the endpoint ---------------------------------------------------------


async def test_starting_a_review_needs_a_key(env: dict) -> None:
    resp = await env["client"].post("/api/v1/tags/review/claude", json={})
    assert resp.status_code == 409
    assert "Anthropic key" in resp.json()["error"]["message"]


async def test_starting_a_review_queues_the_untagged_pictures(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from framefound.processing import tag_review_tasks

    queued: list[list[str]] = []
    monkeypatch.setattr(
        tag_review_tasks.review_tags_with_claude, "delay", lambda ids: queued.append(list(ids))
    )
    client = env["client"]
    await client.put("/api/v1/develop/settings/ai", json={"api_key": "sk-ant-test"})
    resp = await client.post("/api/v1/tags/review/claude", json={"limit": 1})
    assert resp.status_code == 202
    assert resp.json()["queued"] == 1
    assert queued == [[str(env["ids"][1])]], "the newest untagged picture"


async def test_a_used_up_budget_refuses_to_start(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = env["client"]
    await client.put(
        "/api/v1/develop/settings/ai", json={"api_key": "sk-ant-test", "monthly_budget_usd": 0.05}
    )
    async with env["factory"]() as db:
        await spend.record(
            db, purpose=spend.RECIPE, model="claude-haiku-5-5", usage={"input_tokens": 1_000_000}
        )
        await db.commit()
    resp = await client.post("/api/v1/tags/review/claude", json={})
    assert resp.status_code == 409
    assert "used up" in resp.json()["error"]["message"]


async def test_the_settings_page_sees_the_budget_and_the_spend(env: dict) -> None:
    client = env["client"]
    body = (
        await client.put(
            "/api/v1/develop/settings/ai",
            json={"monthly_budget_usd": 12.5, "review_auto": True, "review_batch": 10},
        )
    ).json()
    assert body["monthly_budget_usd"] == 12.5 and body["review_auto"] is True
    assert body["review_batch"] == 10 and body["spent_this_month_usd"] == 0.0
    assert body["review_model"] == "claude-haiku-5-5"
