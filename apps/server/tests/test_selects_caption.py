"""Selects from a search that found a clip by a local model's caption.

The search names the middle of the captioned shot as the clip's moment; a
selects reel built from that search must cut that shot and say why, not
open the clip at 0 s as if its file name had matched.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from test_scan import db  # noqa: F401 - fixture
from test_selects import _shoot

from framefound.api.v1.selects import MomentIn, SelectsRequest, assemble, moments_from_search
from framefound.db.models import Look, ShotWindow

CAPTION = "A slow orbit of the clubhouse at sunset"


async def _captioned_shot(db: AsyncSession, asset_id: uuid.UUID) -> None:  # noqa: F811
    db.add_all(
        [
            ShotWindow(asset_id=asset_id, idx=0, start_ms=0, end_ms=21_000, move="pan"),
            ShotWindow(asset_id=asset_id, idx=1, start_ms=21_000, end_ms=30_000, move="orbit"),
            Look(
                asset_id=asset_id,
                start_ms=21_000,
                end_ms=30_000,
                caption=CAPTION,
                score=80,
                key="google/gemma-4-E4B-it@abc#look-1",
                model="google/gemma-4-E4B-it",
            ),
        ]
    )
    await db.commit()


async def test_a_clip_found_by_its_caption_is_selected_at_that_shot(
    db: AsyncSession,  # noqa: F811
) -> None:
    shoot = await _shoot(db)
    await _captioned_shot(db, shoot.drone)

    moments = await moments_from_search(db, "clubhouse orbit", None, 25)
    assert moments == [
        MomentIn(
            id=str(shoot.drone), at_s=25.5, kind="visual", reason="AI caption: clubhouse orbit"
        )
    ]

    result = await assemble(db, SelectsRequest(title="Clubhouse", query="clubhouse orbit"))
    [chosen] = result.selects
    assert chosen.filename == "DJI_0042.MP4"
    assert chosen.reason == "AI caption: clubhouse orbit"
    assert 0 < chosen.in_s <= 25.5 < chosen.out_s, "the captioned shot, not the clip's opening"


async def test_a_caption_does_not_take_the_moment_from_what_was_said(
    db: AsyncSession,  # noqa: F811
) -> None:
    """Words said on camera still give the sentence: the caption only
    decides the moment when nothing better did."""
    shoot = await _shoot(db)
    db.add(
        Look(
            asset_id=shoot.interview,
            caption="A superintendent talks about the new mainline",
            score=60,
            key="google/gemma-4-E4B-it@abc#look-1",
            model="google/gemma-4-E4B-it",
        )
    )
    await db.commit()

    moments = await moments_from_search(db, "mainline", None, 25)
    assert [(m.id, m.at_s, m.kind) for m in moments] == [(str(shoot.interview), 10.0, "speech")]
