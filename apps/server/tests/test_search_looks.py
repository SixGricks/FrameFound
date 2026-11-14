"""Search with a local model's looks (L3 D14, D15).

A use score nudges a fused score by at most ten per cent and never makes
anything match; a caption is a fifth, weaker reason, labelled as one. With
no looks in the database, search answers exactly what it did before looks
existed — the existing tests in test_search_relevance.py pin that answer,
and the first test here pins that the new code does not change it.
"""

import uuid

import pytest
from sqlalchemy import update
from test_search_relevance import _hit, env  # noqa: F401 - pytest fixture

from framefound.api.v1.search import (
    FUSION_WEIGHTS,
    CaptionHit,
    FilenameHit,
    TagHit,
    TranscriptHit,
    VisualHit,
    fuse_results,
    score_factor,
)
from framefound.db.models import Asset, Look, ShotWindow

# ------------------------------------------------------------ no looks


async def test_search_is_unchanged_when_nothing_has_a_score(env: dict) -> None:  # noqa: F811
    """The endpoint's fused list equals the fusion of its own lists computed
    the way it was before looks: same assets, same order, same scores."""
    body = await env["search"]("farmhouse")
    assert body["caption_hits"] == []
    before = fuse_results(
        tag_hits=[TagHit(**hit) for hit in body["tag_hits"]],
        visual_hits=[VisualHit(**hit) for hit in body["visual_hits"]],
        transcript_hits=[TranscriptHit(**hit) for hit in body["transcript_hits"]],
        filename_hits=[FilenameHit(**hit) for hit in body["filename_hits"]],
        limit=25,
    )
    assert [r.model_dump(mode="json") for r in before] == body["results"]
    assert all(r["look_score"] is None and r["caption"] is None for r in body["results"])
    assert "caption" not in {reason for r in body["results"] for reason in r["reasons"]}


def test_the_nudge_is_ten_per_cent_at_most_either_way() -> None:
    assert score_factor(50) == 1.0
    assert score_factor(100) == pytest.approx(1.10)
    assert score_factor(0) == pytest.approx(0.90)
    # A score outside 0-100 cannot push further.
    assert score_factor(250) == score_factor(100)
    assert score_factor(-9) == score_factor(0)


# ------------------------------------------------------------ scores


def _visual(asset_id: uuid.UUID, similarity: float) -> VisualHit:
    return _hit(VisualHit, asset_id, ts_ms=0, similarity=similarity)  # type: ignore[return-value]


def test_a_better_take_sorts_above_a_worse_one_that_matched_the_same_way() -> None:
    """Two takes of the same subject, each found by its picture alone and
    next to each other: the one the model scored 90 leads the one it
    scored 30, and each says its score."""
    worse, better = uuid.uuid4(), uuid.uuid4()
    hits = {"visual_hits": [_visual(worse, 0.31), _visual(better, 0.30)]}
    plain = fuse_results(tag_hits=[], transcript_hits=[], filename_hits=[], limit=10, **hits)
    assert [r.asset_id for r in plain] == [worse, better]

    scores = {str(worse): 30, str(better): 90}
    nudged = fuse_results(
        tag_hits=[], transcript_hits=[], filename_hits=[], limit=10, look_scores=scores, **hits
    )
    assert [r.asset_id for r in nudged] == [better, worse]
    assert [r.look_score for r in nudged] == [90, 30]
    by_id = {r.asset_id: r for r in plain}
    assert nudged[0].score == pytest.approx(by_id[better].score * score_factor(90), abs=1e-6)
    assert nudged[0].reasons == ["visual"], "a score is not a reason it matched"


def test_a_score_cannot_lift_a_weaker_match_over_a_stronger_one() -> None:
    """Tagged and seen, scored 0, against seen only, scored 100: the
    asset with two reasons still leads."""
    strong, weak = uuid.uuid4(), uuid.uuid4()
    results = fuse_results(
        tag_hits=[_hit(TagHit, strong, tag_name="Barn", tag_slug="barn", confirmed=True)],  # type: ignore[list-item]
        visual_hits=[_visual(weak, 0.4), _visual(strong, 0.3)],
        transcript_hits=[],
        filename_hits=[],
        limit=10,
        look_scores={str(strong): 0, str(weak): 100},
    )
    assert [r.asset_id for r in results] == [strong, weak]


def test_the_cut_to_the_limit_comes_after_the_nudge() -> None:
    """A scored neighbour just below the limit can move into it; the list
    is never cut first and nudged afterwards."""
    first, second = uuid.uuid4(), uuid.uuid4()
    results = fuse_results(
        tag_hits=[],
        visual_hits=[_visual(first, 0.31), _visual(second, 0.30)],
        transcript_hits=[],
        filename_hits=[],
        limit=1,
        look_scores={str(second): 100},
    )
    assert [r.asset_id for r in results] == [second]


async def test_the_endpoint_reads_the_scores_of_what_it_found(env: dict) -> None:  # noqa: F811
    before = {r["asset_id"]: r for r in (await env["search"]("farmhouse"))["results"]}
    drone = env["ids"]["aerial_0042.mp4"]
    async with env["factory"]() as db:
        await db.execute(update(Asset).where(Asset.id == drone).values(look_score=100))
        await db.commit()
    after = {r["asset_id"]: r for r in (await env["search"]("farmhouse"))["results"]}
    scored = after[str(drone)]
    assert scored["look_score"] == 100
    assert scored["score"] == pytest.approx(before[str(drone)]["score"] * 1.1, abs=1e-6)
    assert scored["reasons"] == before[str(drone)]["reasons"]
    others = [key for key in after if key != str(drone)]
    assert all(after[key]["score"] == before[key]["score"] for key in others)
    assert all(after[key]["look_score"] is None for key in others)


# ------------------------------------------------------------ captions


def _caption(asset_id: uuid.UUID, text: str, ts_ms: int | None = None) -> CaptionHit:
    return _hit(CaptionHit, asset_id, caption=text, ts_ms=ts_ms)  # type: ignore[return-value]


def test_a_caption_counts_for_less_than_a_suggested_tag() -> None:
    assert FUSION_WEIGHTS["caption"] < FUSION_WEIGHTS["suggested_tag"]
    captioned, suggested = uuid.uuid4(), uuid.uuid4()
    results = fuse_results(
        tag_hits=[_hit(TagHit, suggested, tag_name="Barn", tag_slug="barn", confirmed=False)],  # type: ignore[list-item]
        visual_hits=[],
        transcript_hits=[],
        filename_hits=[],
        limit=10,
        caption_hits=[_caption(captioned, "a red barn at dusk")],
    )
    assert [r.asset_id for r in results] == [suggested, captioned]


def test_a_caption_match_opens_at_its_shot_when_nothing_else_set_a_moment() -> None:
    clip = uuid.uuid4()
    [only] = fuse_results(
        tag_hits=[],
        visual_hits=[],
        transcript_hits=[],
        filename_hits=[],
        limit=10,
        caption_hits=[_caption(clip, "an orbit of the clubhouse", ts_ms=12000)],
    )
    assert (only.reasons, only.caption, only.ts_ms, only.frame_ms) == (
        ["caption"],
        "an orbit of the clubhouse",
        12000,
        None,
    )


async def _look(env: dict, filename: str, caption: str, **fields: object) -> None:  # noqa: F811
    async with env["factory"]() as db:
        db.add(
            Look(
                asset_id=env["ids"][filename],
                caption=caption,
                score=fields.pop("score", 70),
                key="google/gemma-4-E4B-it@abc#look-1",
                model="google/gemma-4-E4B-it",
                **fields,
            )
        )
        await db.commit()


async def _windows(env: dict, filename: str, spans: list[tuple[int, int]]) -> None:  # noqa: F811
    async with env["factory"]() as db:
        for idx, (start, end) in enumerate(spans):
            db.add(
                ShotWindow(
                    asset_id=env["ids"][filename], idx=idx, start_ms=start, end_ms=end, move="pan"
                )
            )
        await db.commit()


async def test_a_caption_match_is_labelled_as_one(env: dict) -> None:  # noqa: F811
    """The pond photograph says nothing about a farmhouse anywhere but in
    the model's caption: it is found for that, and says so."""
    await _look(env, "pond.jpg", "A white Farmhouse seen across the pond")
    body = await env["search"]("white farmhouse")
    pond = str(env["ids"]["pond.jpg"])
    [hit] = [h for h in body["caption_hits"] if h["asset_id"] == pond]
    assert hit["caption"] == "A white Farmhouse seen across the pond"
    [fused] = [r for r in body["results"] if r["asset_id"] == pond]
    assert "caption" in fused["reasons"]
    assert fused["caption"] == "A white Farmhouse seen across the pond"


async def test_only_a_current_successful_caption_holding_every_word_is_searched(
    env: dict,  # noqa: F811
) -> None:
    await _look(env, "pond.jpg", "A white farmhouse by the pond", current=False)
    await _look(env, "farmhouse_front.jpg", "white farmhouse", ok=False, current=False)
    await _look(env, "owner_interview.mp3", "A white barn")  # one word of two
    await _look(env, "auction_walkthrough.mp4", "white farmhouse, wide", start_ms=0, end_ms=4000)
    await _look(env, "farmhouse_offline.jpg", "white farmhouse")  # offline: never shown
    await _windows(env, "auction_walkthrough.mp4", [(0, 4000)])
    body = await env["search"]("the white farmhouse")
    assert [h["asset_id"] for h in body["caption_hits"]] == [
        str(env["ids"]["auction_walkthrough.mp4"])
    ]
    assert body["caption_hits"][0]["ts_ms"] == 2000
    negated = await env["search"]("white farmhouse -wide")
    assert negated["caption_hits"] == []


async def test_one_clip_with_many_matching_shots_does_not_crowd_out_the_rest(
    env: dict,  # noqa: F811
) -> None:
    """Nine shots of one drone clip all say "aerial view of the golf course"
    and score 90; a photograph says it too and scores 60. Asked for two
    results, search gives the clip once (its best shot) and the photograph —
    one row per asset is chosen before the list is cut, so the many shots of
    one clip cannot use up the list."""
    spans = [(i * 1000, i * 1000 + 1000) for i in range(9)]
    await _windows(env, "aerial_0042.mp4", spans)
    for start, end in spans:
        await _look(
            env,
            "aerial_0042.mp4",
            "an aerial view of the golf course",
            score=90,
            start_ms=start,
            end_ms=end,
        )
    await _look(env, "pond.jpg", "aerial view of the golf course and pond", score=60)
    body = await env["search"]("aerial golf course", limit=2)
    assert [h["asset_id"] for h in body["caption_hits"]] == [
        str(env["ids"]["aerial_0042.mp4"]),
        str(env["ids"]["pond.jpg"]),
    ]
    assert body["caption_hits"][0]["ts_ms"] == 500, "the clip opens at its first best shot"


async def test_a_caption_of_a_shot_that_no_longer_exists_is_not_searched(
    env: dict,  # noqa: F811
) -> None:
    """The clip was analysed again and its shots now fall at other times. A
    look still marked current at the old times (recorded while the clip was
    being analysed, or left by a path that rebuilt the windows without
    retiring it) would send the operator to a shot that is gone; so would a
    look of the whole clip, which stands only while the clip has no shots."""
    await _windows(env, "auction_walkthrough.mp4", [(0, 5000), (5000, 9000)])
    await _look(env, "auction_walkthrough.mp4", "a white farmhouse", start_ms=0, end_ms=4000)
    await _look(env, "auction_walkthrough.mp4", "a white farmhouse")  # the whole clip
    body = await env["search"]("white farmhouse")
    assert body["caption_hits"] == []

    # The same shot at the window's times is found, at its middle.
    await _look(env, "auction_walkthrough.mp4", "a white farmhouse", start_ms=5000, end_ms=9000)
    body = await env["search"]("white farmhouse")
    assert [(h["asset_id"], h["ts_ms"]) for h in body["caption_hits"]] == [
        (str(env["ids"]["auction_walkthrough.mp4"]), 7000)
    ]
