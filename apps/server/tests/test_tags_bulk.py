"""Bulk tag review: many answers about one tag in one request.

The Tags page used to answer one tile at a time, each answer queueing a full
re-learn of the tag — and a re-learn now scores the whole library. Bulk is
not only faster for the operator; it is one library pass instead of sixty.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from test_tags_api import env  # noqa: F401 - pytest fixture

from framefound.db.models import Asset, AssetTag, Tag

BASE = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)


async def _queue(env: dict, n: int = 6) -> tuple[str, list[uuid.UUID]]:  # noqa: F811
    """A tag taught on one asset, with `n` more assets suggested for it."""
    first = env["assets"][0]
    created = (
        await env["client"].post(f"/api/v1/tags/assets/{first}", json={"name": "Power Broom"})
    ).json()
    tag_id = created[0]["tag_id"]
    async with env["factory"]() as db:
        library_id = (await db.get(Asset, first)).library_id
        ids = []
        for i in range(n):
            asset = Asset(
                library_id=library_id,
                relative_path=f"q{i}.mp4",
                filename=f"q{i}.mp4",
                extension="mp4",
                media_type="video",
                size_bytes=1000,
                mtime=BASE,
                availability="online",
            )
            db.add(asset)
            await db.flush()
            db.add(
                AssetTag(
                    asset_id=asset.id,
                    tag_id=uuid.UUID(tag_id),
                    source="suggested",
                    confidence=0.9 - i * 0.05,
                )
            )
            ids.append(asset.id)
        await db.commit()
    env["learned"].clear()
    return tag_id, ids


async def _sources(env: dict, tag_id: str) -> dict[uuid.UUID, str]:  # noqa: F811
    async with env["factory"]() as db:
        rows = (
            await db.execute(
                select(AssetTag.asset_id, AssetTag.source).where(
                    AssetTag.tag_id == uuid.UUID(tag_id)
                )
            )
        ).all()
    return {asset_id: source for asset_id, source in rows}


async def test_many_answers_in_one_request_relearn_once(env: dict) -> None:  # noqa: F811
    tag_id, ids = await _queue(env)
    resp = await env["client"].post(
        f"/api/v1/tags/{tag_id}/decide",
        json={"accept": [str(i) for i in ids[:4]], "reject": [str(i) for i in ids[4:]]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"accepted": 4, "rejected": 2, "skipped": 0}
    sources = await _sources(env, tag_id)
    assert [sources[i] for i in ids] == ["confirmed"] * 4 + ["rejected"] * 2
    # The point of bulk: one re-learn for six answers, not six.
    assert env["learned"] == [tag_id]


async def test_a_rejection_forgets_its_confidence(env: dict) -> None:  # noqa: F811
    tag_id, ids = await _queue(env, n=1)
    await env["client"].post(f"/api/v1/tags/{tag_id}/decide", json={"reject": [str(ids[0])]})
    async with env["factory"]() as db:
        link = (await db.execute(select(AssetTag).where(AssetTag.asset_id == ids[0]))).scalar_one()
    assert link.source == "rejected" and link.confidence is None


async def test_settled_answers_are_never_overturned_by_a_stale_page(env: dict) -> None:  # noqa: F811
    """Another tab, the asset page or Claude answered in the meantime; a bulk
    click from an older page must not overturn it — nor touch a hand tag."""
    tag_id, ids = await _queue(env, n=3)
    first = env["assets"][0]  # the hand-tagged example
    await env["client"].post(
        f"/api/v1/tags/assets/{ids[0]}/{tag_id}/decide", json={"accept": False}
    )
    env["learned"].clear()
    resp = await env["client"].post(
        f"/api/v1/tags/{tag_id}/decide",
        json={"accept": [str(ids[0]), str(ids[1])], "reject": [str(first), str(ids[2])]},
    )
    assert resp.json() == {"accepted": 1, "rejected": 1, "skipped": 2}
    sources = await _sources(env, tag_id)
    assert sources[ids[0]] == "rejected", "an earlier no stays a no"
    assert sources[first] == "manual", "a hand tag is never rejected in bulk"
    assert sources[ids[1]] == "confirmed" and sources[ids[2]] == "rejected"


async def test_unknown_or_foreign_assets_are_skipped(env: dict) -> None:  # noqa: F811
    tag_id, ids = await _queue(env, n=1)
    stranger = uuid.uuid4()
    resp = await env["client"].post(
        f"/api/v1/tags/{tag_id}/decide", json={"accept": [str(ids[0]), str(stranger)]}
    )
    assert resp.json() == {"accepted": 1, "rejected": 0, "skipped": 1}


async def test_nothing_changed_means_nothing_relearned(env: dict) -> None:  # noqa: F811
    tag_id, _ = await _queue(env, n=1)
    resp = await env["client"].post(
        f"/api/v1/tags/{tag_id}/decide", json={"accept": [str(uuid.uuid4())]}
    )
    assert resp.json()["skipped"] == 1
    assert env["learned"] == []


async def test_the_same_asset_in_both_lists_is_refused(env: dict) -> None:  # noqa: F811
    tag_id, ids = await _queue(env, n=1)
    resp = await env["client"].post(
        f"/api/v1/tags/{tag_id}/decide",
        json={"accept": [str(ids[0])], "reject": [str(ids[0])]},
    )
    assert resp.status_code == 422
    assert (await _sources(env, tag_id))[ids[0]] == "suggested"


async def test_an_empty_answer_and_an_unknown_tag_are_refused(env: dict) -> None:  # noqa: F811
    tag_id, _ = await _queue(env, n=1)
    assert (await env["client"].post(f"/api/v1/tags/{tag_id}/decide", json={})).status_code == 422
    assert (
        await env["client"].post(
            f"/api/v1/tags/{uuid.uuid4()}/decide", json={"accept": [str(uuid.uuid4())]}
        )
    ).status_code == 404


async def test_a_runaway_list_is_refused(env: dict) -> None:  # noqa: F811
    tag_id, _ = await _queue(env, n=1)
    many = [str(uuid.uuid4()) for _ in range(501)]
    resp = await env["client"].post(f"/api/v1/tags/{tag_id}/decide", json={"accept": many})
    assert resp.status_code == 422


async def test_the_whole_queue_is_listed_for_review(env: dict) -> None:  # noqa: F811
    """The page asks for up to 200 so a bulk "Select all" covers the queue,
    not just the first sixty."""
    tag_id, ids = await _queue(env, n=70)
    rows = (await env["client"].get(f"/api/v1/tags/{tag_id}/pending?limit=200")).json()
    assert len(rows) == 70
    assert [r["asset_id"] for r in rows[:2]] == [str(ids[0]), str(ids[1])]


async def test_bulk_needs_a_session(env: dict) -> None:  # noqa: F811
    tag_id, ids = await _queue(env, n=1)
    await env["client"].post("/api/v1/auth/logout")
    resp = await env["client"].post(f"/api/v1/tags/{tag_id}/decide", json={"accept": [str(ids[0])]})
    assert resp.status_code == 401
    async with env["factory"]() as db:
        assert (await db.get(Tag, uuid.UUID(tag_id))) is not None


async def test_the_still_waiting_check_is_part_of_the_write(env: dict) -> None:  # noqa: F811
    """Reading the rows and then writing them by id let an answer committed
    in between (the asset page, another tab, Claude) be overwritten. Every
    UPDATE the bulk call makes must carry the "still suggested" condition
    itself, so the database settles the race."""
    from sqlalchemy import event

    tag_id, ids = await _queue(env, n=2)
    engine = env["factory"].kw["bind"].sync_engine
    updates: list[str] = []

    def capture(_conn, _cursor, statement, _params, _context, _many) -> None:  # type: ignore[no-untyped-def]
        if statement.lstrip().upper().startswith("UPDATE ASSET_TAGS"):
            updates.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        await env["client"].post(
            f"/api/v1/tags/{tag_id}/decide",
            json={"accept": [str(ids[0])], "reject": [str(ids[1])]},
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(updates) == 2
    assert all("asset_tags.source = " in u.split(" WHERE ", 1)[1] for u in updates)
