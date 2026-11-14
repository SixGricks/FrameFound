"""The shot search outside the connector finds what `find_shots` prints.

A page or a job that shows a local model shot lines calls
`nle.shotlist.select_shots` and `shot_line`; Claude calls `find_shots`. The
two must agree line for line, or the draft and its review would be about
different shots. Kept apart from test_shotlist.py because this one needs
the connector's package, and that one must run without it.
"""

import pytest
from test_mcp import connect, env, serving  # noqa: F401 - pytest fixture
from test_mcp_shots import _course, _looked_at, _text

from framefound.db.models import Cut, CutPiece
from framefound.nle import shotlist

# Each is (the tool's arguments, the same question as a ShotFilter).
ASKED = [
    ({}, shotlist.ShotFilter()),
    (
        {"folder": "brookside", "at_speed": 50, "order": "time"},
        shotlist.ShotFilter(folder="brookside", at_speed=50, order="time"),
    ),
    (
        {"moves": ["orbits"], "light": ["golden"], "order": "length"},
        shotlist.ShotFilter(moves=["orbit"], light=["golden hour"], order="length"),
    ),
    (
        {"subjects": ["machine"], "energy": ["high"], "at_speed": 200, "min_smooth": 0.8},
        shotlist.ShotFilter(subjects=["machine"], energy=["high"], at_speed=200, min_smooth=0.8),
    ),
    ({"order": "score"}, shotlist.ShotFilter(order="score")),
    ({"min_score": 60}, shotlist.ShotFilter(min_score=60)),
    ({"exclude_used": True}, shotlist.ShotFilter(exclude_used=True)),
]


@pytest.mark.parametrize(("arguments", "asked"), ASKED)
async def test_select_shots_finds_what_find_shots_prints(
    env: dict,  # noqa: F811
    arguments: dict,
    asked: shotlist.ShotFilter,
) -> None:
    ids = await _course(env)
    await _looked_at(env, ids["DJI_20261103035806_0495_D.MP4"], 21000, 27000, 80)
    async with env["factory"]() as db:
        cut = Cut(title="Quick 30", client="Intel", source="mcp", duration_s=4.0, piece_count=1)
        db.add(cut)
        await db.flush()
        db.add(
            CutPiece(
                cut_id=cut.id,
                position=0,
                asset_id=ids["DJI_20261103051509_0639_D.MP4"],
                filename="DJI_20261103051509_0639_D.MP4",
                in_ms=3000,
                out_ms=7000,
                speed=1.0,
                shot={},
            )
        )
        await db.commit()

    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        result = await session.call_tool("find_shots", {**arguments, "limit": 100})
    async with env["factory"]() as db:
        scan = await shotlist.scan_shots(db, asked)

    printed = _text(result).splitlines()
    assert not result.is_error, printed
    expected = [
        shotlist.shot_line(
            asset, window, asked.at_speed, sequence_fps=asked.sequence_fps, used=bool(used)
        )
        + shotlist.score_word(scan.look_of(window, asset))
        for window, asset, used in scan.shots
    ]
    assert expected, "every question here has an answer"
    assert printed[0].startswith(f"{len(expected)} of {len(expected)} shots")
    assert printed[1 : 1 + len(expected)] == expected
