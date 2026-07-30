"""The FFmpeg invocations behind a slideshow render.

Worth testing without encoding anything: a wrong graph either dies after
minutes of work, or produces something subtly wrong — transitions in the wrong
place, a file that plays nowhere — that survives review and is noticed only
after the video has been handed over.
"""

import pytest

from framefound.media.render import (
    FPS,
    MAX_ZOOM,
    RenderPlanError,
    RenderSpec,
    Slide,
    alternate_directions,
    body_argv,
    body_bounds,
    colour_filter,
    concat_argv,
    concat_list,
    cut_chain,
    ken_burns_filter,
    piece_plan,
    transition_argv,
)


def _spec(count: int = 3, seconds: float = 3.0, **kwargs: object) -> RenderSpec:
    directions = alternate_directions(count)
    slides = [
        Slide(path=f"/frames/{i}.jpg", seconds=seconds, direction=directions[i])
        for i in range(count)
    ]
    return RenderSpec(slides=slides, **kwargs)  # type: ignore[arg-type]


# --- pacing ---------------------------------------------------------------


def test_directions_alternate_rather_than_repeating_or_randomising() -> None:
    """All-same reads as a template; random reads as a glitch."""
    assert alternate_directions(4) == ["in", "out", "in", "out"]


def test_duration_accounts_for_transitions_overlapping() -> None:
    """Three 3s slides with 0.6s crossfades run 9 - 1.2 = 7.8s, not 9s.
    Getting this wrong puts the audio fade in the wrong place."""
    assert _spec(3, 3.0, transition_seconds=0.6).total_seconds == pytest.approx(7.8)


def test_a_single_slide_has_no_transition_to_subtract() -> None:
    assert _spec(1, 3.0).total_seconds == pytest.approx(3.0)


def test_an_empty_selection_renders_nothing() -> None:
    empty = RenderSpec(slides=[])
    assert empty.total_frames == 0
    assert piece_plan(0) == []


def test_the_first_and_last_slides_only_give_up_one_end() -> None:
    """A middle slide is trimmed at both ends; the outer two are not, or the
    slideshow would open and close mid-dissolve."""
    spec = _spec(3, 3.0, transition_seconds=0.6)  # 90 frames each, 18-frame fades
    assert body_bounds(0, spec) == (0, 72)
    assert body_bounds(1, spec) == (18, 72)
    assert body_bounds(2, spec) == (18, 90)


def test_pacing_that_cannot_fit_its_transitions_is_refused() -> None:
    """A 1s slide cannot give 0.6s to a transition at each end. Better a clear
    refusal than a zero-length body that FFmpeg reports as a broken encoder."""
    spec = _spec(3, 1.0, transition_seconds=0.6)
    with pytest.raises(RenderPlanError, match="cannot hold"):
        body_bounds(1, spec)


def test_the_frame_count_is_exact() -> None:
    """Bodies plus fades, counted in frames rather than accumulated in floats,
    so a long slideshow cannot drift."""
    spec = _spec(4, 3.0, transition_seconds=0.6)
    # 4 bodies: 72 + 54 + 54 + 72 = 252, plus 3 fades of 18 = 54
    assert spec.total_frames == 306
    assert spec.total_seconds == pytest.approx(10.2)


# --- the move -------------------------------------------------------------


def test_the_move_is_rendered_for_the_slides_full_length() -> None:
    """Even though only part is kept. The trim selects a window of this motion,
    so a fade continues exactly where the body left off."""
    chain = ken_burns_filter(Slide(path="/a.jpg", seconds=4.0), RenderSpec(fps=30))
    assert "d=120" in chain


def test_stills_are_padded_rather_than_cropped() -> None:
    """A vertical phone photo in a 16:9 slideshow should get bars, not be
    cropped down to a sliver of somebody's chin."""
    chain = ken_burns_filter(Slide(path="/a.jpg", seconds=3.0), RenderSpec())
    assert "force_original_aspect_ratio=decrease" in chain
    assert "pad=" in chain


def test_the_move_is_rendered_on_an_upscaled_copy() -> None:
    """zoompan pans by whole pixels, so a slow move across a 1080p still
    quantises and judders."""
    chain = ken_burns_filter(Slide(path="/a.jpg", seconds=3.0), RenderSpec())
    assert "scale=3840:2160" in chain


def test_zooming_in_stops_at_the_bound() -> None:
    """Past MAX_ZOOM the upscale shows as softness on delivery."""
    chain = ken_burns_filter(Slide(path="/a.jpg", seconds=3.0, direction="in"), RenderSpec())
    assert "min(zoom+" in chain and str(MAX_ZOOM) in chain


def test_zooming_out_starts_wide_and_ends_at_native() -> None:
    chain = ken_burns_filter(Slide(path="/a.jpg", seconds=3.0, direction="out"), RenderSpec())
    assert chain.count("max(") == 1
    assert ",1.0)" in chain


def test_the_sample_aspect_ratio_is_pinned() -> None:
    """xfade and concat both reject inputs whose SAR disagrees, and neither
    error says so."""
    assert "setsar=1" in ken_burns_filter(Slide(path="/a.jpg", seconds=3.0), RenderSpec())


def test_a_neutral_grade_is_omitted_entirely() -> None:
    """Photographs arrive already graded. An eq filter that changes nothing is
    still a filter, and it should not be in the chain."""
    assert colour_filter(RenderSpec()) == ""


def test_a_theme_grade_appears_when_it_does_something() -> None:
    graded = colour_filter(RenderSpec(saturation=1.12, contrast=1.04))
    assert "eq=" in graded and "saturation=1.12" in graded


# --- the cut --------------------------------------------------------------


def test_trims_are_expressed_in_frames() -> None:
    """Seconds would let a body and the fade abutting it disagree about the
    boundary by a frame, which shows as a stutter at every transition."""
    chain = cut_chain(Slide(path="/a.jpg", seconds=3.0), _spec(), 18, 72)
    assert "trim=start_frame=18:end_frame=72" in chain


def test_the_frame_rate_is_reasserted_after_rebasing_timestamps() -> None:
    """The regression this guards: `setpts=PTS-STARTPTS` discards the stream's
    frame-rate metadata, and xfade then refuses the input outright — "current
    rate of 1/0 is invalid". The symptom is a transition clip containing no
    frames and an encoder that reports only that it could not start."""
    chain = cut_chain(Slide(path="/a.jpg", seconds=3.0), _spec(), 0, 18)
    assert chain.endswith("setpts=PTS-STARTPTS,fps=30")


def test_the_grade_is_applied_before_the_cut() -> None:
    """So a body and its neighbouring fade are graded identically. Grading
    afterwards would put the seams at a different saturation."""
    chain = cut_chain(Slide(path="/a.jpg", seconds=3.0), _spec(saturation=1.12), 0, 18)
    assert chain.index("eq=saturation") < chain.index("trim=")


# --- pieces ---------------------------------------------------------------


def test_pieces_alternate_body_and_fade_in_playback_order() -> None:
    assert piece_plan(3) == [("body", 0), ("fade", 0), ("body", 1), ("fade", 1), ("body", 2)]


def test_a_single_photograph_is_one_piece_with_no_transition() -> None:
    """One photo is a legitimate slideshow."""
    assert piece_plan(1) == [("body", 0)]


def test_n_slides_need_n_minus_one_transitions() -> None:
    for count in range(1, 40):
        plan = piece_plan(count)
        assert sum(1 for kind, _ in plan if kind == "body") == count
        assert sum(1 for kind, _ in plan if kind == "fade") == count - 1


def test_a_body_takes_exactly_one_input() -> None:
    """The whole point: peak memory is a property of one slide, not of the
    slideshow's length."""
    argv = body_argv(0, _spec(), "/work/b.mp4")
    assert argv.count("-i") == 1


def test_a_transition_takes_exactly_two_inputs() -> None:
    """A chained xfade over every segment was killed at any length, including
    four, because each later input's decoder buffers until its turn."""
    argv = transition_argv(0, _spec(), "/work/f.mp4")
    assert argv.count("-i") == 2


def test_a_transition_dissolves_the_tail_of_one_into_the_head_of_the_next() -> None:
    spec = _spec(3, 3.0, transition_seconds=0.6)  # 90-frame slides, 18-frame fades
    graph = transition_argv(0, spec, "/work/f.mp4")[
        transition_argv(0, spec, "/work/f.mp4").index("-filter_complex") + 1
    ]
    assert "trim=start_frame=72:end_frame=90" in graph, "tail of the outgoing slide"
    assert "trim=start_frame=0:end_frame=18" in graph, "head of the incoming slide"
    assert "offset=0" in graph, "both clips already start at zero"


def test_the_transition_uses_both_slides_own_directions() -> None:
    """The dissolve has to continue each slide's motion, not restart it."""
    spec = _spec(2)  # slide 0 zooms in, slide 1 zooms out
    graph = transition_argv(0, spec, "/work/f.mp4")[
        transition_argv(0, spec, "/work/f.mp4").index("-filter_complex") + 1
    ]
    assert "min(zoom+" in graph and "max(" in graph


def test_stills_are_not_looped_into_the_encoder() -> None:
    """zoompan's `d` is applied per input frame. `-loop 1 -t 3` feeds 90 input
    frames and asks for 90 output frames from each: 8100 frames for a
    three-second slide, which renders for minutes and comes out wrong."""
    assert "-loop" not in body_argv(0, _spec(), "/work/b.mp4")


def test_every_piece_is_encoded_identically() -> None:
    """The concat demuxer copies packets. Pieces encoded with different
    parameters produce a file players disagree about."""
    body = body_argv(0, _spec(), "/b.mp4")
    fade = transition_argv(0, _spec(), "/f.mp4")
    for flag in ("-crf", "-preset", "-pix_fmt", "-r", "-x264-params"):
        assert body[body.index(flag) + 1] == fade[fade.index(flag) + 1]


def test_the_gpu_path_does_not_carry_x264_parameters() -> None:
    """NVENC does not ignore `-x264-params`; it rejects them."""
    argv = body_argv(0, _spec(video_codec="h264_nvenc"), "/b.mp4")
    assert "h264_nvenc" in argv
    assert "-x264-params" not in argv
    assert "-crf" not in argv


def test_the_default_encoder_needs_no_gpu() -> None:
    """The GPU is an accelerator, never a requirement."""
    assert "libx264" in body_argv(0, _spec(), "/b.mp4")


# --- stitching ------------------------------------------------------------


def test_the_stitch_copies_rather_than_re_encodes() -> None:
    """Each piece was encoded once from its still. Re-encoding here would cost
    a generation of quality for nothing."""
    argv = concat_argv("/work/list.txt", _spec(), "/out.mp4")
    assert "copy" in argv
    assert "libx264" not in argv


def test_absolute_paths_are_permitted_in_the_playlist() -> None:
    argv = concat_argv("/work/list.txt", _spec(), "/out.mp4")
    assert argv[argv.index("-safe") + 1] == "0"


def test_the_output_plays_on_a_phone() -> None:
    """faststart, or the file has to be fully downloaded before it will start.
    The pieces already carry yuv420p."""
    assert "+faststart" in concat_argv("/work/list.txt", _spec(), "/out.mp4")


def test_the_playlist_quotes_each_piece() -> None:
    listing = concat_list(["/work/w/0000-body.mp4", "/work/w/0000-fade.mp4"])
    assert listing == "file '/work/w/0000-body.mp4'\nfile '/work/w/0000-fade.mp4'\n"


def test_a_path_that_would_break_the_playlist_is_refused() -> None:
    """A quote would end the filename early and the demuxer would read the
    remainder as another directive."""
    with pytest.raises(RenderPlanError):
        concat_list(["/work/it's.mp4"])


# --- audio ----------------------------------------------------------------


def test_no_audio_flags_when_there_is_no_bed() -> None:
    argv = concat_argv("/l.txt", _spec(), "/out.mp4")
    assert "-c:a" not in argv
    assert "-shortest" not in argv


def test_the_bed_is_the_second_input() -> None:
    argv = concat_argv("/l.txt", _spec(audio_path="/music.m4a"), "/out.mp4")
    assert "1:a" in argv
    assert "-shortest" in argv


def test_the_bed_fades_out_rather_than_stopping() -> None:
    """Music that simply cuts sounds like a fault even when it is deliberate."""
    spec = _spec(3, 3.0, audio_path="/music.m4a", audio_fade_seconds=2.5)
    argv = concat_argv("/l.txt", spec, "/out.mp4")
    filters = argv[argv.index("-af") + 1]
    assert "afade=t=out" in filters
    assert float(filters.split("st=")[1].split(":")[0]) == pytest.approx(7.8 - 2.5)


def test_a_fade_longer_than_the_video_does_not_go_negative() -> None:
    spec = _spec(1, 1.0, audio_path="/m.m4a", audio_fade_seconds=5.0)
    filters = concat_argv("/l.txt", spec, "/o.mp4")[
        concat_argv("/l.txt", spec, "/o.mp4").index("-af") + 1
    ]
    assert float(filters.split("st=")[1].split(":")[0]) >= 0.0


def test_audio_can_be_left_off_explicitly() -> None:
    spec = _spec(3, audio_path="/music.m4a")
    assert "-c:a" not in concat_argv("/l.txt", spec, "/o.mp4", with_audio=False)


def test_the_default_frame_rate_is_smooth_enough_for_a_slow_pan() -> None:
    # 24 shows visible stepping on a Ken Burns move.
    assert FPS >= 30
