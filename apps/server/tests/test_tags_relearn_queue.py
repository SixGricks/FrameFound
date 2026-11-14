"""Answering a tag never waits for the learner's queue.

With the broker unreachable, Celery's publish retried for about 5.5 s before
it gave up. It ran inside the async handlers, so every Y press on the Tags
page sat on "Saving…" that long, and the whole server stalled with it. The
answer was saved all along; only the wait was wrong.
"""

import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from test_tags_api import env  # noqa: F401 - pytest fixture

from framefound.api.v1 import tags as tags_api
from framefound.db.models import Asset, AssetTag, Tag
from framefound.processing import tag_tasks

# Taken at import, before the `env` fixture swaps it for a recorder: these
# tests are about the real one.
REAL_RELEARN = tags_api._relearn
BASE = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
# Far under the 5.5 s a down broker cost, far over a request on a slow runner.
QUICK = 2.0


class Broker:
    """Stands in for `learn_tag.delay`: hangs until released, or refuses."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.arrived = threading.Event()
        self.released = threading.Event()
        self.refuse = False

    def delay(self, tag_id: str) -> None:
        self.sent.append(tag_id)
        self.arrived.set()
        if self.refuse:
            raise ConnectionRefusedError("broker is down")
        self.released.wait(10)

    def wait_for(self, tag_id: uuid.UUID, times: int = 1) -> bool:
        deadline = time.monotonic() + 5
        while self.sent.count(str(tag_id)) < times and time.monotonic() < deadline:
            time.sleep(0.01)
        return self.sent.count(str(tag_id)) >= times


@pytest.fixture()
def broker(monkeypatch: pytest.MonkeyPatch) -> Iterator[Broker]:
    rig = Broker()
    monkeypatch.setattr(tags_api, "_relearn", REAL_RELEARN)
    monkeypatch.setattr(tag_tasks.learn_tag, "delay", rig.delay)
    yield rig
    # Let a hung publish finish and the queue drain while the stand-in is
    # still installed, so nothing reaches a real broker after this test.
    rig.released.set()
    deadline = time.monotonic() + 5
    while (tags_api._relearn_waiting or not tags_api._relearn_jobs.empty()) and (
        time.monotonic() < deadline
    ):
        time.sleep(0.01)
    time.sleep(0.05)


async def _waiting(env: dict, n: int = 2) -> tuple[uuid.UUID, list[uuid.UUID]]:  # noqa: F811
    """A fresh tag with `n` suggestions waiting for an answer."""
    async with env["factory"]() as db:
        library_id = (await db.get(Asset, env["assets"][0])).library_id
        slug = f"kitchen-{uuid.uuid4().hex[:8]}"
        tag = Tag(name=slug, slug=slug)
        db.add(tag)
        await db.flush()
        ids = []
        for i in range(n):
            asset = Asset(
                library_id=library_id,
                relative_path=f"{slug}-{i}.jpg",
                filename=f"{slug}-{i}.jpg",
                extension="jpg",
                media_type="image",
                size_bytes=1000,
                mtime=BASE,
                availability="online",
            )
            db.add(asset)
            await db.flush()
            db.add(AssetTag(asset_id=asset.id, tag_id=tag.id, source="suggested", confidence=0.9))
            ids.append(asset.id)
        await db.commit()
        return tag.id, ids


async def _source(env: dict, tag_id: uuid.UUID, asset_id: uuid.UUID) -> str:  # noqa: F811
    async with env["factory"]() as db:
        return (
            await db.execute(
                select(AssetTag.source).where(
                    AssetTag.tag_id == tag_id, AssetTag.asset_id == asset_id
                )
            )
        ).scalar_one()


async def test_a_hung_queue_does_not_hold_up_the_answer(env: dict, broker: Broker) -> None:  # noqa: F811
    tag_id, ids = await _waiting(env)

    started = time.monotonic()
    one = await env["client"].post(
        f"/api/v1/tags/assets/{ids[0]}/{tag_id}/decide", json={"accept": True}
    )
    many = await env["client"].post(f"/api/v1/tags/{tag_id}/decide", json={"reject": [str(ids[1])]})
    again = await env["client"].post(f"/api/v1/tags/{tag_id}/relearn")
    elapsed = time.monotonic() - started

    assert (one.status_code, many.status_code, again.status_code) == (200, 200, 202)
    assert elapsed < QUICK, f"three answers took {elapsed:.1f}s behind a hung queue"
    assert await _source(env, tag_id, ids[0]) == "confirmed"
    assert await _source(env, tag_id, ids[1]) == "rejected"
    # The pass is still on its way to the queue, not dropped.
    assert broker.wait_for(tag_id)


async def test_a_queue_that_refuses_is_logged_and_the_next_answer_still_queues(
    env: dict,  # noqa: F811
    broker: Broker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker.refuse = True
    warned: list[dict[str, Any]] = []

    class Log:
        def warning(self, event: str, **kw: Any) -> None:
            warned.append({"event": event, **kw})

        def info(self, *_a: Any, **_kw: Any) -> None:
            pass

    monkeypatch.setattr(tags_api, "log", Log())
    tag_id, ids = await _waiting(env)

    resp = await env["client"].post(
        f"/api/v1/tags/assets/{ids[0]}/{tag_id}/decide", json={"accept": False}
    )
    assert resp.status_code == 200
    assert await _source(env, tag_id, ids[0]) == "rejected"
    assert broker.wait_for(tag_id)
    deadline = time.monotonic() + 5
    while not warned and time.monotonic() < deadline:
        time.sleep(0.01)
    assert {"event": "tagging.enqueue_failed", "tag_id": str(tag_id)}.items() <= warned[0].items()

    # A refusal must not take the enqueue thread down with it.
    assert (await env["client"].post(f"/api/v1/tags/{tag_id}/relearn")).status_code == 202
    assert broker.wait_for(tag_id, times=2)


async def test_answers_made_while_a_pass_waits_share_that_pass(
    env: dict,  # noqa: F811
    broker: Broker,
) -> None:
    """While the queue is slow, a run of answers about one tag sends one more
    pass, not one each — the waiting pass reads the tag when it runs."""
    blocker, _ = await _waiting(env, n=1)
    tag_id, _ = await _waiting(env, n=1)

    tags_api._relearn(blocker)
    assert broker.wait_for(blocker)  # the enqueue thread is now stuck sending it
    for _ in range(5):
        assert (await env["client"].post(f"/api/v1/tags/{tag_id}/relearn")).status_code == 202

    broker.released.set()
    assert broker.wait_for(tag_id)
    time.sleep(0.1)
    assert broker.sent.count(str(tag_id)) == 1


def test_an_edit_during_a_publish_gets_a_pass_of_its_own(broker: Broker) -> None:
    """The pass being sent may be read by a worker before a later edit lands,
    so that edit must not be folded into it."""
    tag_id = uuid.uuid4()
    tags_api._relearn(tag_id)
    assert broker.wait_for(tag_id)  # in flight, hung
    tags_api._relearn(tag_id)
    broker.released.set()
    assert broker.wait_for(tag_id, times=2)
