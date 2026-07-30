"""Orchestration around the render: ordering, guards, and moving the result.

FFmpeg itself is stubbed. What is under test is the part that decides *what* to
run and *when to refuse* — the failures here are silent ones (a piece missing
from the playlist, a half-written file left looking finished) rather than
anything FFmpeg would report.
"""

from pathlib import Path

import pytest

from framefound.media import pipeline
from framefound.media.render import RenderSpec, Slide


def _spec(tmp_path: Path, count: int = 3, *, make_files: bool = True) -> RenderSpec:
    slides = []
    for i in range(count):
        path = tmp_path / f"photo{i}.jpg"
        if make_files:
            path.write_bytes(b"not really a jpeg, never decoded in these tests")
        slides.append(Slide(path=str(path), seconds=3.0, direction="in"))
    return RenderSpec(slides=slides)


# --- refusing early -------------------------------------------------------


def test_a_slideshow_with_no_photographs_is_refused(tmp_path: Path) -> None:
    with pytest.raises(pipeline.RenderError, match="at least one"):
        pipeline.check_sources(RenderSpec(slides=[]))


def test_missing_previews_are_reported_before_any_encoding(tmp_path: Path) -> None:
    """Discovered up front rather than on piece 30 of 40. A missing preview is
    an incomplete derivative store, which is a different problem — and a
    different fix — from a render that failed."""
    spec = _spec(tmp_path, 3)
    Path(spec.slides[1].path).unlink()
    with pytest.raises(pipeline.RenderError, match="1 of 3"):
        pipeline.check_sources(spec)


def test_a_complete_selection_passes(tmp_path: Path) -> None:
    pipeline.check_sources(_spec(tmp_path, 3))


# --- planning -------------------------------------------------------------


def test_every_slide_and_every_join_gets_a_piece(tmp_path: Path) -> None:
    pieces = pipeline.plan_pieces(_spec(tmp_path, 4), tmp_path / "work")
    assert [p.kind for p in pieces] == ["body", "fade", "body", "fade", "body", "fade", "body"]


def test_piece_filenames_sort_into_playback_order(tmp_path: Path) -> None:
    """The concat playlist is written in this order, and a directory listing
    that sorts the same way is much easier to read after a failure."""
    pieces = pipeline.plan_pieces(_spec(tmp_path, 3), tmp_path / "work")
    names = [p.path.name for p in pieces]
    assert names == sorted(names)


def test_pieces_are_named_in_a_way_an_operator_can_read(tmp_path: Path) -> None:
    """These strings end up in the error the operator sees, so "photo 3" beats
    "piece index 4"."""
    pieces = pipeline.plan_pieces(_spec(tmp_path, 3), tmp_path / "work")
    assert pieces[0].label == "photo 1"
    assert pieces[1].label == "the join after photo 1"


def test_a_single_photograph_needs_no_join(tmp_path: Path) -> None:
    pieces = pipeline.plan_pieces(_spec(tmp_path, 1), tmp_path / "work")
    assert [p.kind for p in pieces] == ["body"]


# --- running --------------------------------------------------------------


def test_an_ffmpeg_that_writes_nothing_is_caught(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """FFmpeg exiting 0 having produced no file is what an unrenderable filter
    graph looks like. Caught here the message names the piece; left to the
    concat it is reported as a corrupt file instead."""
    monkeypatch.setattr(pipeline, "_run", lambda argv, timeout: None)
    piece = pipeline.plan_pieces(_spec(tmp_path, 2), tmp_path / "work")[0]
    piece.path.parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(pipeline.RenderError, match="empty clip"):
        pipeline.run_piece(piece)


def test_a_failing_piece_names_which_photograph(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from framefound.processing.ffmpeg import FfmpegError

    def boom(argv: list[str], timeout: int) -> None:
        raise FfmpegError("the file could not be processed")

    monkeypatch.setattr(pipeline, "_run", boom)
    pieces = pipeline.plan_pieces(_spec(tmp_path, 3), tmp_path / "work")
    with pytest.raises(pipeline.RenderError, match="photo 2"):
        pipeline.run_piece(pieces[2])


# --- stitching ------------------------------------------------------------


def _fake_ffmpeg_writing(target: Path):  # type: ignore[no-untyped-def]
    def run(argv: list[str], timeout: int) -> None:
        Path(argv[-1]).parent.mkdir(parents=True, exist_ok=True)
        Path(argv[-1]).write_bytes(b"pretend mp4")

    return run


def test_the_playlist_lists_every_piece_in_order(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    workdir = tmp_path / "work"
    spec = _spec(tmp_path, 3)
    pieces = pipeline.plan_pieces(spec, workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pipeline, "_run", _fake_ffmpeg_writing(workdir))
    monkeypatch.setattr(pipeline, "probe_seconds", lambda path: 7.8)

    pipeline.stitch(spec, pieces, workdir, tmp_path / "out" / "final.mp4")
    listing = (workdir / "pieces.txt").read_text(encoding="utf-8").splitlines()
    assert len(listing) == len(pieces)
    assert [line.split("/")[-1].rstrip("'") for line in listing] == [p.path.name for p in pieces]


def test_the_result_is_moved_into_place_only_when_complete(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Staged under a temporary name, so an interrupted render never leaves
    something that looks like a finished video."""
    workdir = tmp_path / "work"
    output = tmp_path / "out" / "final.mp4"
    spec = _spec(tmp_path, 2)
    pieces = pipeline.plan_pieces(spec, workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pipeline, "_run", _fake_ffmpeg_writing(workdir))
    monkeypatch.setattr(pipeline, "probe_seconds", lambda path: 5.4)

    result = pipeline.stitch(spec, pieces, workdir, output)
    assert output.is_file()
    assert not (workdir / "final.mp4").exists(), "the staged file must be moved, not copied"
    assert result.seconds == 5.4
    assert result.size_bytes == output.stat().st_size


def test_the_measured_duration_wins_over_the_estimate(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """They should agree. When they do not, it means the graph did something
    other than intended — which is worth surfacing rather than covering with a
    computed number that always looks right."""
    workdir = tmp_path / "work"
    spec = _spec(tmp_path, 3)  # estimate: 3*3.0 - 2*0.6 = 7.8
    pieces = pipeline.plan_pieces(spec, workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pipeline, "_run", _fake_ffmpeg_writing(workdir))
    monkeypatch.setattr(pipeline, "probe_seconds", lambda path: 6.1)

    assert spec.total_seconds == pytest.approx(7.8)
    assert pipeline.stitch(spec, pieces, workdir, tmp_path / "o.mp4").seconds == 6.1


def test_the_estimate_is_used_when_probing_is_unavailable(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    workdir = tmp_path / "work"
    spec = _spec(tmp_path, 3)
    pieces = pipeline.plan_pieces(spec, workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pipeline, "_run", _fake_ffmpeg_writing(workdir))
    monkeypatch.setattr(pipeline, "probe_seconds", lambda path: None)

    assert pipeline.stitch(spec, pieces, workdir, tmp_path / "o.mp4").seconds == pytest.approx(7.8)


# --- end to end (still no FFmpeg) -----------------------------------------


def test_progress_is_reported_once_per_photograph(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A forty-photograph render is minutes of work, so "is it doing
    anything?" has to be answerable. Transitions do not count — the operator
    thinks in photographs."""
    monkeypatch.setattr(pipeline, "_run", _fake_ffmpeg_writing(tmp_path))
    monkeypatch.setattr(pipeline, "probe_seconds", lambda path: 1.0)
    monkeypatch.setattr(pipeline, "choose_encoder", lambda spec: None)
    seen: list[int] = []
    pipeline.render(
        _spec(tmp_path, 4),
        tmp_path / "out.mp4",
        tmp_path / "work",
        on_progress=seen.append,
    )
    assert seen == [1, 2, 3, 4]


def test_the_gpu_is_never_required(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`choose_encoder` probes functionally and leaves the spec alone when
    there is no usable GPU."""
    monkeypatch.setattr(pipeline, "nvenc_available", lambda: False)
    spec = _spec(tmp_path, 2)
    pipeline.choose_encoder(spec)
    assert spec.video_codec == "libx264"


def test_the_gpu_is_used_when_it_is_genuinely_there(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Decided once, before any piece: the concat demuxer copies packets, so
    pieces from different encoders would be stitched into a file whose
    parameters change partway through."""
    monkeypatch.setattr(pipeline, "nvenc_available", lambda: True)
    spec = _spec(tmp_path, 2)
    pipeline.choose_encoder(spec)
    assert spec.video_codec == "h264_nvenc"
