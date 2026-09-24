"""Stage ordering and the frame fallbacks.

Two production findings (2026-09) pinned here:

- Stills were sampled in a race with their own thumbnail. Enqueued together
  on different queues, sampling often ran first, found nothing to read,
  returned quietly under a "succeeded" job — 152 photographs never reached
  visual search, faces, or listing room labels.
- 407 Blackmagic RAW clips had no frames: ffmpeg cannot decode BRAW, every
  planned timestamp failed, and the job still reported success. Their poster
  comes from the SDK decoder, so the thumbnail is the frame they can offer.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.models import Asset, Frame, Library
from framefound.processing import derivatives as deriv
from framefound.processing import scenes
from framefound.processing import tasks as task_module


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict]:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'chain.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "chain-test-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", db_url)
    get_settings.cache_clear()
    (tmp_path / "data").mkdir()

    sent: dict[str, list[str]] = {}
    for name in (
        "generate_derivatives",
        "generate_proxy",
        "transcribe_asset",
        "sample_frames",
        "embed_frames",
        "detect_faces",
    ):
        bucket = sent.setdefault(name, [])
        monkeypatch.setattr(getattr(task_module, name), "delay", bucket.append)

    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    root = tmp_path / "lib"
    root.mkdir()
    async with factory() as db:
        library = Library(name="L", root_path=str(root), generate_proxies=False)
        db.add(library)
        await db.commit()
        yield {"db": db, "library": library, "sent": sent, "data": tmp_path / "data"}
    await engine.dispose()
    get_settings.cache_clear()


async def _asset(env: dict, name: str, media_type: str, **kwargs: Any) -> Asset:
    asset = Asset(
        library_id=env["library"].id,
        relative_path=name,
        filename=name,
        extension=name.rsplit(".", 1)[-1],
        media_type=media_type,
        size_bytes=10,
        mtime=datetime.now(UTC),
        **kwargs,
    )
    env["db"].add(asset)
    await env["db"].commit()
    return asset


def _write_thumbnail(env: dict, asset: Asset) -> Path:
    path = env["data"] / deriv.derivative_relpath(asset.id, "thumbnail", "webp")
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 48), (120, 90, 60)).save(path, "WEBP")
    return path


async def test_a_still_is_not_sampled_alongside_its_thumbnail(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(task_module, "probe_media", lambda _path, _kind: {})
    asset = await _asset(env, "kitchen.jpg", "image")

    await task_module._extract(env["db"], asset, env["library"], Path("unused"))
    assert env["sent"]["generate_derivatives"] == [str(asset.id)]
    assert env["sent"]["sample_frames"] == [], "sampling must wait for the thumbnail"


async def test_a_still_is_sampled_once_its_thumbnail_exists(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def made(*_args: Any) -> None:
        return None

    monkeypatch.setattr(task_module.deriv, "generate_visuals", made)
    asset = await _asset(env, "kitchen.jpg", "image")

    await task_module._visuals(env["db"], asset, env["library"], Path("unused"))
    assert env["sent"]["sample_frames"] == [str(asset.id)]


async def test_video_is_still_sampled_straight_away(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(task_module, "probe_media", lambda _path, _kind: {})
    asset = await _asset(env, "walk.mp4", "video")

    await task_module._extract(env["db"], asset, env["library"], Path("unused"))
    assert env["sent"]["sample_frames"] == [str(asset.id)]


async def test_a_lost_enqueue_does_not_fail_a_good_extraction(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(task_module, "probe_media", lambda _path, _kind: {})

    def broker_down(_asset_id: str) -> None:
        raise ConnectionError("broker blip")

    monkeypatch.setattr(task_module.generate_derivatives, "delay", broker_down)
    asset = await _asset(env, "kitchen.jpg", "image")

    await task_module._extract(env["db"], asset, env["library"], Path("unused"))
    assert asset.processing_status == "ready"


async def test_braw_takes_its_frame_from_the_thumbnail(env: dict) -> None:
    asset = await _asset(env, "A001.braw", "video", duration_s=42.0)
    thumb = _write_thumbnail(env, asset)

    await task_module._sample_frames(env["db"], asset, env["library"], Path("unused.braw"))
    frames = (await env["db"].execute(select(Frame))).scalars().all()
    assert [f.relative_path for f in frames] == [
        str(thumb.relative_to(env["data"])).replace("\\", "/")
    ]
    assert env["sent"]["embed_frames"] == [str(asset.id)]
    assert env["sent"]["detect_faces"] == [str(asset.id)]


async def test_undecodable_video_falls_back_to_its_thumbnail(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    def cannot_decode(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("ffmpeg: unsupported codec")

    monkeypatch.setattr(scenes, "extract_frame", cannot_decode)
    monkeypatch.setattr(scenes, "should_scene_detect", lambda *_a, **_k: False)
    asset = await _asset(env, "damaged.mp4", "video", duration_s=30.0)
    _write_thumbnail(env, asset)

    await task_module._sample_frames(env["db"], asset, env["library"], Path("damaged.mp4"))
    frames = (await env["db"].execute(select(Frame))).scalars().all()
    assert len(frames) == 1 and frames[0].ts_ms == 0
    assert env["sent"]["embed_frames"] == [str(asset.id)]


async def test_a_still_without_a_thumbnail_writes_nothing(env: dict) -> None:
    asset = await _asset(env, "orphan.jpg", "image")

    await task_module._sample_frames(env["db"], asset, env["library"], Path("orphan.jpg"))
    assert (await env["db"].execute(select(Frame))).scalars().all() == []
    assert env["sent"]["embed_frames"] == []
