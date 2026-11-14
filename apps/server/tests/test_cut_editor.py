"""The Editor's question and what FrameFound does with its answer
(framefound/ai/cut_editor.py).

Pure: shots are built in memory, nothing is stored, no model is asked. The
job that asks the Editor and writes the cut is test_cut_drafts.py.
"""

import ast
import json
import math
import random
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from framefound.ai import cut_editor
from framefound.ai.cut_editor import (
    Candidate,
    Constraints,
    EditorAnswer,
    EditorCut,
    Hints,
    check,
    choose,
    cut_off,
    demo_answer,
    prompt,
    rank,
    read_brief,
)
from framefound.db.models import Asset, Look, ShotWindow
from framefound.nle import shotlist

BASE = datetime(2026, 11, 3, 6, 40, tzinfo=UTC)
SOURCE = Path(cut_editor.__file__)


def _asset(name: str, minutes: float | None = 0.0) -> Asset:
    when = None if minutes is None else BASE + timedelta(minutes=minutes)
    return Asset(id=uuid.uuid4(), filename=name, captured_at=when)


def _window(
    start: float,
    end: float,
    *,
    move: str = "orbit",
    direction: str | None = "right",
    smooth: float = 0.8,
    energy: str = "low",
    light: str | None = "golden hour",
    sky: str | None = None,
    view: str | None = "aerial",
    subjects: dict[str, float] | None = None,
    idx: int = 0,
) -> ShotWindow:
    return ShotWindow(
        idx=idx,
        start_ms=round(start * 1000),
        end_ms=round(end * 1000),
        move=move,
        direction=direction,
        smooth=smooth,
        smooth_by_speed={"100": smooth},
        energy=energy,
        light=light,
        look={"sky": sky} if sky else {},
        framing={"view": view} if view else {},
        subjects=subjects if subjects is not None else {"green": 1.4, "bunker": 1.1},
    )


def _shot(
    asset: Asset, start: float, end: float, *, used: list[str] | None = None, **kw: Any
) -> shotlist.Shot:
    window = _window(start, end, **kw)
    window.asset_id = asset.id
    return (window, asset, used or [])


def _candidates(*spans: tuple[float, float]) -> list[Candidate]:
    """Shots numbered from 1, one clip each, with the given source spans."""
    return [
        Candidate(
            n=n,
            asset_id=uuid.uuid4(),
            filename=f"DJI_20261103051509_06{n:02d}_D.MP4",
            start_s=start,
            end_s=end,
            view="aerial",
            line=f"{start:.1f}-{end:.1f}s orbit right smooth .90 energy low golden green",
        )
        for n, (start, end) in enumerate(spans, start=1)
    ]


def _answer(*cuts: tuple[int, float, float]) -> EditorAnswer:
    return EditorAnswer(
        cuts=[EditorCut(shot=n, in_s=a, out_s=b, reason="because") for n, a, b in cuts]
    )


THIRTY = Constraints(target_s=30, min_shot_s=1.5, max_shot_s=8)


# --- the question is pinned ------------------------------------------------------

PINNED_VERSION = "editor-1"
PINNED_INSTRUCTIONS = (
    "You are drafting a rough cut for a video editor from a numbered list of shots.\n"
    "Each line is: number, clip, view, in-out seconds of the source, camera move, "
    "smoothness 0-1, subject energy, light, subjects. "
    '"used" means an earlier video already used that shot. '
    '"score N" is another model\'s 0-100 opinion of the shot, where one was given.\n'
    "Rules:\n"
    "- Use only shot numbers from the list. Never invent a shot.\n"
    "- in_s and out_s are seconds of that shot's source and must lie inside its "
    "in-out range.\n"
    '- Do not use a shot twice. Prefer shots not marked "used".\n'
    "- The order of the cuts is their order on the timeline. Open and close on "
    "purpose; vary the moves.\n"
    "- reason: one line of at most 15 words saying why this shot is here.\n"
    'The answer has "title" (a short title for the cut), "cuts" (each with "shot", '
    '"in_s", "out_s" and "reason") and "notes" (anything the editor should know).'
)
PINNED_PROMPT = (
    "Brief: Thirty seconds of smooth aerials at golden hour, opening on the clubhouse.\n"
    "Length: about 30 seconds. Use about 6 cuts, each between 1.5 and 8 seconds.\n"
    "\n"
    "Shots:\n"
    "1 DJI_0631 aerial 0.0-9.5s rise up smooth .93 energy low golden clubhouse,green\n"
    "2 DJI_0639 aerial 2.0-14.0s orbit right smooth .95 energy low golden green,bunker used\n"
    "3 Brookside_walk 0.0-6.0s static smooth .99 energy medium mid-sun cart path score 72"
)
PINNED_SCHEMA = {
    "$defs": {
        "EditorCut": {
            "properties": {
                "in_s": {"title": "In S", "type": "number"},
                "out_s": {"title": "Out S", "type": "number"},
                "reason": {"default": "", "title": "Reason", "type": "string"},
                "shot": {"title": "Shot", "type": "integer"},
            },
            "required": ["shot", "in_s", "out_s"],
            "title": "EditorCut",
            "type": "object",
        }
    },
    "properties": {
        "cuts": {"items": {"$ref": "#/$defs/EditorCut"}, "title": "Cuts", "type": "array"},
        "notes": {"default": "", "title": "Notes", "type": "string"},
        "title": {"default": "", "title": "Title", "type": "string"},
    },
    "required": ["cuts"],
    "title": "EditorAnswer",
    "type": "object",
}
CHANGED = (
    "What the Editor is asked has changed. Drafts already made were asked the old question: "
    "move PROMPT_VERSION in framefound/ai/cut_editor.py on from {version!r}, then pin the "
    "new text here under the new version. A change to the connector's shot line "
    "(shotlist.window_line, shotlist.score_word) is a change to the question too."
)


def _pinned_candidates() -> list[Candidate]:
    """The three shots of the pinned prompt, offered by `choose` from real
    windows: most of each line is `find_shots`' own description of the
    shot, and that has to be pinned here, not typed in by hand. Between
    them they use every rule of the line: a move with a direction, a static
    shot whose direction is dropped, the light written short, two subjects
    of three, the used flag, a score, and a shot with no view."""
    first = _asset("DJI_20261103051509_0631_D.MP4", 0)
    second = _asset("DJI_20261103051509_0639_D.MP4", 5)
    walk = _asset("Brookside walk.mov", 10)
    shots = [
        _shot(first, 0, 9.5, move="rise", direction="up", smooth=0.93,
              subjects={"clubhouse": 1.2, "green": 1.0, "bunker": 0.5}),
        _shot(second, 2, 14, smooth=0.95, used=["Quick 30"]),
        _shot(walk, 0, 6, move="static", direction="left", smooth=0.99, energy="medium",
              light="mid sun", view=None, subjects={"cart path": 1.0}),
    ]  # fmt: skip
    scored = Look(score=72, demo=False)
    return choose(
        shots, Hints(), THIRTY, look_of=lambda _w, asset: scored if asset is walk else None
    )


def test_the_question_is_pinned() -> None:
    """A word of the question changed without a new version would leave
    drafts that say they were asked something they were not. The shot lines
    are built by `choose` from real windows, so a change to the connector's
    line (which `find_shots` shares) fails here too, not only in its own
    tests."""
    told = CHANGED.format(version=PINNED_VERSION)
    brief = "  Thirty seconds of smooth aerials at golden hour, opening on the clubhouse.\n"

    assert cut_editor.PROMPT_VERSION == PINNED_VERSION, "pin the text below under the new version"
    assert cut_editor.INSTRUCTIONS == PINNED_INSTRUCTIONS, told
    assert prompt(brief, THIRTY, _pinned_candidates()) == PINNED_PROMPT, told
    assert EditorAnswer.model_json_schema() == PINNED_SCHEMA, told
    assert (cut_editor.MAX_LINES, cut_editor.PER_CLIP, cut_editor.MAX_DRAFT_CUTS) == (80, 3, 60)


def test_the_module_imports_only_what_every_install_has() -> None:
    """The API, the job and the chat tool all import it, with and without the
    connector's `mcp` extra: the standard library, pydantic, the shot list
    and the models' types, and nothing else."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    outside = {
        name
        for name in names
        if name.split(".")[0] not in sys.stdlib_module_names
        and name.split(".")[0] != "pydantic"
        and name not in ("framefound.nle.shotlist", "framefound.db.models")
    }

    assert outside == set()


def test_how_many_cuts_to_ask_for_and_the_room_to_answer_in() -> None:
    assert THIRTY.about == 6  # 30 / 4.75 = 6.3
    assert Constraints(target_s=600, min_shot_s=1, max_shot_s=1).about == 60
    assert Constraints(target_s=5, min_shot_s=8, max_shot_s=8).about == 1
    assert Constraints(target_s=19, min_shot_s=1, max_shot_s=1).about == 19
    assert cut_editor.max_tokens(THIRTY) == 400 + 60 * 12
    assert cut_editor.max_tokens(Constraints(target_s=600, min_shot_s=1, max_shot_s=1)) == 4000
    with pytest.raises(ValueError, match="shortest shot"):
        Constraints(target_s=30, min_shot_s=9, max_shot_s=8)
    with pytest.raises(ValueError, match="5 to 600"):
        Constraints(target_s=900)
    with pytest.raises(ValueError, match="numbers"):
        Constraints(target_s=math.nan)


# --- reading the brief -----------------------------------------------------------


def test_the_brief_names_a_length_a_move_and_a_light() -> None:
    hints = read_brief(
        "Thirty seconds of smooth aerial orbits at golden hour, opening on the clubhouse's "
        "greens; shots under 4 s"
    )

    assert hints.target_s == 30
    assert hints.moves == {"orbit"}
    assert hints.light == {"golden hour"}
    assert hints.views == {"aerial"}
    assert hints.steady is True
    assert {"clubhouse", "green"} <= hints.words
    # Words that said something else are not left over for the subjects.
    assert not hints.words & {"thirty", "smooth", "aerial", "orbits", "golden", "hour", "shots"}


@pytest.mark.parametrize(
    ("brief", "seconds"),
    [
        ("0:45 of the back nine", 45),
        ("a minute on the clubhouse", 60),
        ("a minute and a half", 90),
        ("half a minute", 30),
        ("90s teaser for the members", 90),
        ("cut it to 20 seconds", 20),
        ("a second pass at the 15-second spot", 15),
        ("thirty seconds, nothing over four seconds", 30),
        ("shots of 2 to 4 seconds", None),
        ("each shot about 3 s", None),
        ("golden hour at 7:30 pm", None),
        ("fifteen minutes of everything", 600),
        ("a six second bumper", 6),
        ("2 seconds", 5),
        ("the greens and the pond", None),
    ],
)
def test_a_length_is_read_wherever_the_brief_says_it(brief: str, seconds: float | None) -> None:
    """The length of the cut, not of a shot; the largest when several are
    named; held to the 5-600 s a draft may ask for."""
    assert read_brief(brief).target_s == seconds


def test_a_word_that_means_two_things_is_not_read_as_a_move_or_a_light() -> None:
    hints = read_brief("Low energy under a blue sky, still images welcome; push in on the pond")

    assert hints.light == frozenset()
    assert hints.skies == frozenset()
    assert hints.moves == {"push-in"}
    assert read_brief("low sun and an overcast blue hour").light == {"low sun", "blue hour"}
    assert read_brief("low sun and an overcast blue hour").skies == {"overcast"}
    assert read_brief("handheld at eye level").views == {"ground"}


# --- which shots are offered -----------------------------------------------------


def _course() -> list[shotlist.Shot]:
    """Four clips, several shots each, of different moves and quality."""
    a = _asset("DJI_20261103051509_0631_D.MP4", 0)
    b = _asset("DJI_20261103051509_0639_D.MP4", 5)
    c = _asset("DJI_20261103051509_0645_D.MP4", 10)
    d = _asset("Brookside walk.mov", 15)
    return [
        _shot(a, 0, 6, move="orbit", smooth=0.95),
        _shot(a, 6, 12, move="orbit", smooth=0.9, idx=1),
        _shot(b, 0, 5, move="rise", direction="up", smooth=0.6, subjects={"clubhouse": 1.5}),
        _shot(b, 5, 9, move="pan", smooth=0.85, idx=1),
        _shot(c, 0, 8, move="push-in", direction="in", smooth=0.7, light="high sun"),
        _shot(c, 8, 9, move="pan", smooth=0.99, idx=1),  # shorter than 1.5 s
        _shot(d, 0, 6, move="static", direction=None, smooth=0.99, view="ground", used=["Old"]),
    ]


def _spans(candidates: list[Candidate]) -> set[tuple[uuid.UUID, float]]:
    return {(c.asset_id, c.start_s) for c in candidates}


def test_hints_only_change_the_order_never_what_may_be_offered() -> None:
    shots = _course()
    plain = choose(shots, Hints(), THIRTY)
    hinted = choose(shots, read_brief("a rise over the clubhouse at noon, from the ground"), THIRTY)

    # With room for everything, the brief changes nothing about what is offered.
    assert _spans(hinted) == _spans(plain)
    assert len(plain) == 6  # all but the one shorter than the shortest shot asked for
    # With room for two, the plain ranking keeps the smoothest; the hinted
    # one keeps the rise over the clubhouse it asked for.
    two_plain = {c.line.split()[1] for c in choose(shots, Hints(), THIRTY, limit=2)}
    two_hinted = {
        c.line.split()[1]
        for c in choose(shots, read_brief("a rise over the clubhouse"), THIRTY, limit=2)
    }
    assert "rise" not in two_plain
    assert "rise" in two_hinted
    # A hint the footage has nothing for changes nothing.
    assert choose(shots, read_brief("a tilt at night"), THIRTY, limit=3) == choose(
        shots, Hints(), THIRTY, limit=3
    )


def test_what_a_shot_is_ranked_on() -> None:
    clip = _asset("DJI_20261103051509_0631_D.MP4")
    plain = _shot(clip, 0, 6, move="orbit", smooth=0.8)
    used = _shot(clip, 0, 6, move="orbit", smooth=0.8, used=["Teaser"])
    everything = Hints(
        moves=frozenset({"orbit"}),
        light=frozenset({"golden hour"}),
        views=frozenset({"aerial"}),
        words=frozenset({"green"}),
        steady=True,
    )

    assert rank(plain, Hints()) == pytest.approx(0.8)
    assert rank(used, Hints()) == pytest.approx(0.5)
    assert rank(plain, Hints(), score=60) == pytest.approx(1.1)
    assert rank(plain, everything) == pytest.approx(0.8 + 4 * 0.5 + 0.25 * 0.8)
    overcast = _shot(clip, 0, 6, light=None, sky="overcast")
    assert rank(overcast, Hints(skies=frozenset({"overcast"}))) == pytest.approx(1.3)


def test_no_clip_fills_the_list_by_itself() -> None:
    long_flight = _asset("DJI_20261103051509_0700_D.MP4", 0)
    others = [_asset(f"DJI_20261103051509_07{n:02d}_D.MP4", n) for n in range(1, 4)]
    shots = [_shot(long_flight, s * 5, s * 5 + 5, smooth=0.99, idx=s) for s in range(10)]
    shots += [_shot(o, 0, 5, smooth=0.5) for o in others]

    def from_the_flight(offered: list[Candidate]) -> int:
        return sum(1 for c in offered if c.asset_id == long_flight.id)

    offered = choose(shots, Hints(), THIRTY, limit=5)

    # Its ten shots are the smoothest in the folder; three of them make the list.
    assert len(offered) == 5
    assert from_the_flight(offered) == 3
    assert from_the_flight(choose(shots, Hints(), THIRTY, per_clip=1)) == 1


def test_the_list_is_numbered_in_shooting_order() -> None:
    early = _asset("Z_last_by_name.mov", 0)
    late = _asset("A_first_by_name.mov", 30)
    undated = _asset("B_no_date.mov", None)
    shots = [
        _shot(undated, 0, 4, smooth=0.99),
        _shot(late, 10, 14, smooth=0.95, idx=1),
        _shot(late, 0, 4, smooth=0.5),
        _shot(early, 3, 7, smooth=0.7),
    ]

    offered = choose(shots, Hints(), THIRTY)

    assert [c.n for c in offered] == [1, 2, 3, 4]
    assert [(c.filename, c.start_s) for c in offered] == [
        ("Z_last_by_name.mov", 3.0),
        ("A_first_by_name.mov", 0.0),
        ("A_first_by_name.mov", 10.0),
        ("B_no_date.mov", 0.0),
    ]


def test_a_candidate_reads_as_find_shots_prints_it() -> None:
    """What the Editor reads is what Claude reads: after the number and the
    view, the line is `find_shots`' own, used flag and score included."""
    clip = _asset("DJI_20261103051509_0639_D.MP4")
    window, _, used = _shot(clip, 2, 14, used=["Quick 30"])
    look = Look(score=72, demo=False)

    (offered,) = choose([(window, clip, used)], Hints(), THIRTY, look_of=lambda _w, _a: look)

    printed = shotlist.shot_line(clip, window, used=True) + shotlist.score_word(look)
    assert offered.claude_line() == printed
    assert offered.editor_line() == f"1 DJI_0639 aerial {offered.line}"
    assert offered.line.endswith("green,bunker used score 72")
    assert "->" not in offered.line  # never a speed (P9)
    # Kept on a draft by clip and span, and read back the same.
    kept = json.loads(json.dumps(offered.as_json()))
    assert kept["start_ms"] == 2000 and kept["end_ms"] == 14000
    assert Candidate.from_json(kept) == offered


def test_eighty_lines_stay_under_nine_kilobytes() -> None:
    """A big shoot folder offered whole, every line at its longest (a move
    with a direction, two subjects, a third marked used, every shot scored):
    the eighty lines and the brief still fit in about 8 KB. The cap is about
    what a small model reads well, not the model's room (E4)."""
    shots = []
    for n in range(40):
        clip = _asset(f"DJI_20261103051509_{n:04d}_D.MP4", n)
        for s in range(4):
            shots.append(
                _shot(
                    clip,
                    s * 9.5,
                    s * 9.5 + 9.5,
                    move=("lateral glide", "top-down drift", "orbit", "push-in")[s],
                    direction=("left", None, "right", "in")[s],
                    smooth=0.5 + (n * 7 + s) % 50 / 100,
                    energy="medium",
                    subjects={"clubhouse": 1.6, "cart path": 1.2},
                    used=["Quick 30"] if (n + s) % 3 == 0 else [],
                    idx=s,
                )
            )
    look = Look(score=88, demo=False)
    offered = choose(shots, Hints(), THIRTY, look_of=lambda _w, _a: look)
    asked = prompt("Thirty seconds of the course, opening on the clubhouse.", THIRTY, offered)

    assert len(offered) == 80
    assert all(c.line.endswith("score 88") for c in offered)
    assert len(asked.encode("utf-8")) < 9 * 1024
    assert len(cut_editor.INSTRUCTIONS.encode("utf-8")) < 1024


# --- what is done with the answer ------------------------------------------------


def test_a_shot_number_that_is_not_on_the_list_is_dropped_and_said() -> None:
    shots = _candidates((0, 10), (0, 10))

    pieces, problems = check(_answer((1, 1, 5), (91, 0, 4), (2, 2, 6)), shots, THIRTY)

    assert [p.n for p in pieces] == [1, 2]
    assert problems[0] == "Cut 2 named shot 91, which is not on the list."


def test_a_cut_outside_its_shot_is_pulled_back_inside() -> None:
    shots = _candidates((2.0, 14.0), (20.0, 26.0))

    pieces, problems = check(_answer((1, 9.0, 15.2), (2, 18.0, 22.5)), shots, THIRTY)

    assert [(p.in_s, p.out_s) for p in pieces] == [(9.0, 14.0), (20.0, 22.5)]
    assert problems[:2] == [
        "Cut 1 ran past the end of its shot and was pulled back to 14.0 s.",
        "Cut 2 started before its shot and was moved up to 20.0 s.",
    ]


def test_a_cut_too_long_for_one_shot_is_shortened() -> None:
    pieces, problems = check(_answer((1, 1.0, 13.0)), _candidates((0, 20)), Constraints(8))

    assert [(p.in_s, p.out_s) for p in pieces] == [(1.0, 9.0)]
    assert problems == ["Cut 1 was 12.0 s long and was cut to 8 s."]


def test_a_sliver_is_widened_inside_its_shot() -> None:
    shots = _candidates((0, 10), (4.0, 12.0), (0, 1.0))

    pieces, problems = check(
        _answer((1, 5.0, 5.2), (2, 11.9, 12.0), (3, 0.4, 0.5)), shots, Constraints(5)
    )

    # Widened evenly about its middle; at the end of its shot, slid back
    # inside; a shot shorter than the shortest asked for, to its whole length.
    assert [(p.in_s, p.out_s) for p in pieces] == [(4.35, 5.85), (10.5, 12.0), (0.0, 1.0)]
    assert problems[:3] == [
        "Cut 1 was 0.2 s long and was widened to 1.5 s inside its shot.",
        "Cut 2 was 0.1 s long and was widened to 1.5 s inside its shot.",
        "Cut 3 was 0.1 s long and was widened to 1 s inside its shot.",
    ]


def test_the_same_shot_twice_is_one_cut() -> None:
    shots = _candidates((0, 10), (0, 10))

    pieces, problems = check(
        _answer((1, 1, 4), (2, 0, 3), (1, 3.5, 6), (1, 3.9, 7.0)), shots, THIRTY
    )

    # Shot 1 again at 3.5-6 overlaps the first by half a second: a repeat.
    # At 3.9 it overlaps by a tenth: a different moment of the same shot.
    assert [(p.n, p.in_s) for p in pieces] == [(1, 1.0), (2, 0.0), (1, 3.9)]
    assert problems[0] == "Cut 3 repeated the shot of cut 1."


def test_a_draft_twice_too_long_is_cut_back_to_the_length_asked_for() -> None:
    shots = _candidates(*[(0, 10)] * 12)

    pieces, problems = check(_answer(*[(n, 1, 6) for n in range(1, 13)]), shots, THIRTY)

    assert len(pieces) == 6
    assert sum(p.length for p in pieces) == pytest.approx(30)
    assert problems == ["It ran over the 30 s asked for, so the last 6 cuts were left off."]

    # When leaving cuts off would make it too short, the last one is shortened.
    pieces, problems = check(
        _answer(*[(n, 1, 9) for n in range(1, 6)]), shots, Constraints(target_s=20)
    )
    assert [p.length for p in pieces] == pytest.approx([8, 8, 4])
    assert problems == [
        "It ran over the 20 s asked for, so the last 2 cuts were left off.",
        "The last cut was shortened by 4.0 s to bring the cut nearer the 20 s asked for.",
    ]


def test_a_short_draft_is_left_short_and_says_by_how_much() -> None:
    pieces, problems = check(
        _answer((1, 0, 5), (2, 0, 5), (3, 0, 4.5)), _candidates(*[(0, 10)] * 3), THIRTY
    )

    assert len(pieces) == 3
    assert problems == ["It is 15.5 s short of the 30 s asked for."]
    # Within a tenth of the length asked for is the length asked for.
    _, near = check(
        _answer(*[(n, 0, 5.5) for n in range(1, 6)]), _candidates(*[(0, 10)] * 5), THIRTY
    )
    assert near == []


def test_reversed_times_are_swapped_and_times_that_are_not_numbers_are_dropped() -> None:
    shots = _candidates((0, 10), (0, 10), (0, 10))
    answer = EditorAnswer(
        cuts=[
            EditorCut(shot=1, in_s=6, out_s=1),
            EditorCut(shot=2, in_s=math.nan, out_s=3),
            EditorCut(shot=3, in_s=1, out_s=math.inf),
        ]
    )

    pieces, problems = check(answer, shots, Constraints(5))

    assert [(p.n, p.in_s, p.out_s) for p in pieces] == [(1, 1.0, 6.0)]
    assert problems == [
        "Cut 1 had its in and out points the wrong way round; they were swapped.",
        "Cut 2 gave no usable in and out points and was dropped.",
        "Cut 3 gave no usable in and out points and was dropped.",
    ]


def test_more_than_sixty_cuts_are_cut_back_and_a_reason_is_one_short_line() -> None:
    shots = _candidates(*[(0, 600)] * 74)
    answer = EditorAnswer(
        cuts=[
            EditorCut(shot=n, in_s=n * 8, out_s=n * 8 + 2, reason="  Opens\n wide  " + "x" * 300)
            for n in range(1, 75)
        ]
    )

    pieces, problems = check(answer, shots, Constraints(target_s=120, min_shot_s=1, max_shot_s=2))

    assert problems[0] == "It listed 74 cuts; only the first 60 were kept."
    assert len(pieces) == 60
    assert pieces[0].reason.startswith("Opens wide xxx")
    assert len(pieces[0].reason) == 200


def test_a_reason_loses_what_no_file_or_database_can_hold() -> None:
    """A NUL or an escape is valid JSON, and a small model can write one. In
    a reason it became a marker Premiere refuses the whole file over, and a
    note PostgreSQL refuses to store; `check` is where the answer is made
    safe, so it goes there. Words either side of it stay apart."""
    raw = json.dumps(
        {
            "cuts": [
                {"shot": 1, "in_s": 1.0, "out_s": 4.0, "reason": "wide open\u0000 the green"},
                {"shot": 2, "in_s": 11.0, "out_s": 14.0, "reason": "orbit\u001bright\u007f"},
            ]
        }
    )
    answer = EditorAnswer.model_validate_json(raw)

    pieces, _ = check(answer, _candidates((0, 10), (10, 20)), THIRTY)

    assert [p.reason for p in pieces] == ["wide open the green", "orbit right"]


def test_no_piece_is_ever_shorter_than_half_a_second() -> None:
    """Whatever the Editor writes, what leaves `check` is inside its shot, at
    least half a second long, never a repeat, and timed to the hundredth —
    so the record and the timeline cannot disagree (P7)."""
    rng = random.Random(20261113)
    spans = [(0.0, 0.3), (5.0, 5.45)]
    for _ in range(8):
        start = rng.uniform(0, 30)
        spans.append((start, start + rng.uniform(0.2, 12)))
    shots = _candidates(*spans)
    by_n = {s.n: s for s in shots}

    def wild() -> float:
        return rng.choice([rng.uniform(-5, 50), rng.uniform(0, 1), 0.0, 1e9, -1e9])

    for _ in range(300):
        cuts = [
            EditorCut(shot=rng.randint(0, 12), in_s=wild(), out_s=wild())
            for _ in range(rng.randint(0, 15))
        ]
        c = Constraints(
            target_s=rng.choice([5, 30, 120]), min_shot_s=rng.choice([0.5, 1.5, 3]), max_shot_s=8
        )
        pieces, _ = check(EditorAnswer(cuts=cuts), shots, c)
        for i, p in enumerate(pieces):
            shot = by_n[p.n]
            assert p.length >= cut_editor.MIN_PIECE_S
            assert shot.start_s <= p.in_s < p.out_s <= shot.end_s
            assert p.in_s == round(p.in_s, 2) and p.out_s == round(p.out_s, 2)
            for q in pieces[:i]:
                if q.asset_id == p.asset_id:
                    assert (
                        min(p.out_s, q.out_s) - max(p.in_s, q.in_s) <= cut_editor.REPEAT_OVERLAP_S
                    )

    # The two shots too short to hold half a second are named, not used.
    pieces, problems = check(_answer((1, 0, 0.3), (2, 5, 5.45)), shots, Constraints(5))
    assert pieces == []
    assert problems == [
        "Cut 1 was too short to use, even across its whole shot (0.3 s), and was dropped.",
        "Cut 2 was too short to use, even across its whole shot (0.45 s), and was dropped.",
    ]


# --- the demonstration answer and an answer cut off ------------------------------


def _body(text: str) -> dict[str, Any]:
    """The request `ask()` sends: one user message, its text as a part."""
    return {
        "model": "/models/demo",
        "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "EditorAnswer"}},
    }


def test_the_demo_answer_passes_its_own_check() -> None:
    offered = choose(_course(), Hints(), THIRTY)
    c = Constraints(target_s=20, min_shot_s=1.5, max_shot_s=5)
    question = cut_editor.INSTRUCTIONS + "\n\n" + prompt("A short one.", c, offered)

    written = demo_answer(_body(question + "\n\nAnswer with one JSON object and nothing else."))
    answer = EditorAnswer.model_validate_json(written)
    pieces, problems = check(answer, offered, c)

    assert problems == []
    assert len(pieces) == c.about == 6
    assert sum(p.length for p in pieces) == pytest.approx(19.5)
    assert {p.reason for p in pieces} == {cut_editor.DEMO_REASON}
    # Spread through the list, not the first six.
    assert [p.n for p in pieces] == [1, 2, 3, 4, 5, 6]
    # A message whose content is plain text reads the same.
    plain = {"messages": [{"role": "user", "content": question}]}
    assert demo_answer(plain) == written
    # Something that is not the Editor's question gets a well-formed empty cut.
    assert EditorAnswer.model_validate_json(demo_answer({"messages": []})).cuts == []


def test_the_demo_answer_takes_its_cuts_evenly_from_a_long_list() -> None:
    offered = _candidates(*[(0, 10)] * 20)
    written = demo_answer(
        _body(prompt("Brief.", Constraints(target_s=10, min_shot_s=2, max_shot_s=3), offered))
    )

    assert [cut["shot"] for cut in json.loads(written)["cuts"]] == [3, 8, 13, 18]


def test_an_answer_that_stops_mid_object_reads_as_cut_off() -> None:
    whole = json.dumps({"cuts": [{"shot": 1, "in_s": 2.0, "out_s": 5.0, "reason": "a } here"}]})

    assert cut_off(whole[:-12]) is True
    assert cut_off('Here it is: {"cuts": [{"shot": 1, "reason": "opens on {the') is True
    # A brace inside a string closes nothing.
    assert cut_off('{"cuts": [], "notes": "the last one, }') is True
    assert cut_off('{"notes": "a \\" and a }", "cuts": [') is True
    assert cut_off(whole) is False
    assert cut_off("Sure! " + whole + " Hope that helps.") is False
    assert cut_off("I would open on the clubhouse and close on the eighteenth.") is False
    assert cut_off("") is False
