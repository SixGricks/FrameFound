"""Driving a slideshow render from a stored selection to a finished file.

Split from `render.py` on purpose: that module is pure argv construction and
can be tested without touching a disk, while this one owns the filesystem, the
ordering and the temporary directory. Keeping them apart is what let the filter
graphs be tested properly.

The work is exposed as a **list of pieces** rather than a single call, because
a forty-photograph render takes minutes and the operator is entitled to know it
is progressing. The Celery task runs the pieces one at a time and commits
progress between them; `render()` is the same loop without a database, for
tests and for the command line.

Everything here works on **derivative previews**, never originals. A preview is
a 2048px local file; the original may be a 400 MB TIFF on a share that reads at
5.2 MB/s. For a 1920x1080 delivery the preview already has more pixels than the
output needs, so reading the original would cost minutes per photograph and buy
nothing visible. It also keeps the standing promise that only the scanner ever
touches originals.
"""

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import structlog

from framefound.media.render import (
    RenderSpec,
    Slide,
    body_argv,
    concat_argv,
    concat_list,
    piece_plan,
    transition_argv,
)
from framefound.processing.ffmpeg import FfmpegError, _run, nvenc_available

log = structlog.get_logger()

# Per piece. Generous — a slow piece on a busy host is not a failure — but a
# wedged FFmpeg must not hold the media queue open forever.
PIECE_TIMEOUT_S = 600
# The stitch copies packets rather than decoding, so it is fast even for a long
# slideshow. This is a stall guard, not a work budget.
CONCAT_TIMEOUT_S = 900


class RenderError(RuntimeError):
    """The render could not be completed. The message is shown to the operator."""


@dataclass(frozen=True)
class Piece:
    """One FFmpeg run: either a slide's body or a crossfade between two."""

    kind: str  # body | fade
    index: int
    argv: list[str]
    path: Path

    @property
    def label(self) -> str:
        if self.kind == "body":
            return f"photo {self.index + 1}"
        return f"the join after photo {self.index + 1}"


@dataclass
class RenderResult:
    path: Path
    seconds: float
    size_bytes: int


ProgressHook = Callable[[int], None]


def check_sources(spec: RenderSpec) -> None:
    """Fail before any encoding when a preview is missing.

    Checked up front rather than discovered on piece 30 of 40: a missing
    preview means the derivative store is incomplete, which is a different
    problem — and a different fix — from a render that failed.
    """
    if not spec.slides:
        raise RenderError("A slideshow needs at least one photograph")
    missing = sum(1 for s in spec.slides if not Path(s.path).is_file())
    if missing:
        raise RenderError(f"{missing} of {len(spec.slides)} photographs have no preview image yet")


def choose_encoder(spec: RenderSpec) -> None:
    """Point the whole render at the GPU when there genuinely is one.

    Decided once, before any piece is rendered, because the concat demuxer
    copies packets: pieces encoded by different encoders would be stitched into
    a file whose parameters change partway through. `nvenc_available` probes
    functionally rather than reading `-encoders`, which lists h264_nvenc on
    builds that have no GPU to run it on.
    """
    if nvenc_available():
        spec.video_codec = "h264_nvenc"
        log.info("slideshow.encoder", codec="h264_nvenc")


def plan_pieces(spec: RenderSpec, workdir: Path) -> list[Piece]:
    """Every body and every crossfade, in playback order.

    Each is a separate FFmpeg run with at most two inputs, which is what keeps
    peak memory a property of one slide rather than of the slideshow's length.
    """
    pieces: list[Piece] = []
    for kind, index in piece_plan(len(spec.slides)):
        # Ordered names: the concat playlist is written in this order, and a
        # directory listing that sorts the same way is easier to inspect after
        # a failure.
        path = workdir / f"{len(pieces):04d}-{kind}{index}.mp4"
        argv = (
            body_argv(index, spec, str(path))
            if kind == "body"
            else transition_argv(index, spec, str(path))
        )
        pieces.append(Piece(kind=kind, index=index, argv=argv, path=path))
    return pieces


def run_piece(piece: Piece) -> None:
    """Encode one piece. Blocking; safe to call in a worker thread."""
    try:
        _run(piece.argv, PIECE_TIMEOUT_S)
    except FfmpegError as exc:
        raise RenderError(f"Could not render {piece.label}: {exc}") from exc
    if not piece.path.is_file() or piece.path.stat().st_size == 0:
        # An FFmpeg that exits 0 having written nothing is what an unrenderable
        # filter graph looks like. Caught here, the message names the piece;
        # left to the concat, it is reported as a corrupt file instead.
        raise RenderError(f"Rendering {piece.label} produced an empty clip")


def stitch(spec: RenderSpec, pieces: list[Piece], workdir: Path, output: Path) -> RenderResult:
    """Join the pieces and move the result into place."""
    list_path = workdir / "pieces.txt"
    list_path.write_text(concat_list([str(p.path) for p in pieces]), encoding="utf-8")

    # Written under a temporary name and moved into place, so an interrupted
    # render never leaves behind something that looks like a finished video.
    staged = workdir / "final.mp4"
    try:
        _run(concat_argv(str(list_path), spec, str(staged)), CONCAT_TIMEOUT_S)
    except FfmpegError as exc:
        raise RenderError(f"The slideshow could not be assembled: {exc}") from exc

    if not staged.is_file() or staged.stat().st_size == 0:
        raise RenderError("The render produced no output")

    seconds = probe_seconds(staged) or spec.total_seconds
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staged), str(output))
    return RenderResult(path=output, seconds=round(seconds, 3), size_bytes=output.stat().st_size)


def probe_seconds(path: Path) -> float | None:
    """The finished file's actual duration.

    Reported in preference to the arithmetic estimate. The two should agree,
    and when they do not it means the graph did something other than what was
    intended — exactly the failure worth surfacing rather than covering with a
    computed number that always looks right.
    """
    if shutil.which("ffprobe") is None:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 - fixed binary, argv form, no shell
            [  # noqa: S607 - resolved from PATH in our own image
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ],
            capture_output=True,
            timeout=60,
            check=False,
        )
        return float(completed.stdout.decode().strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def render(
    spec: RenderSpec,
    output: Path,
    workdir: Path,
    *,
    on_progress: ProgressHook | None = None,
) -> RenderResult:
    """Render a slideshow start to finish, without a database.

    The caller owns `workdir` and its cleanup: after a failure the pieces are
    worth keeping long enough to look at.
    """
    check_sources(spec)
    choose_encoder(spec)
    workdir.mkdir(parents=True, exist_ok=True)
    pieces = plan_pieces(spec, workdir)
    for piece in pieces:
        run_piece(piece)
        if piece.kind == "body" and on_progress is not None:
            on_progress(piece.index + 1)
    return stitch(spec, pieces, workdir, output)


def slides_for(paths: list[str], *, hold_seconds: float, directions: list[str]) -> list[Slide]:
    """Pair each preview with its hold time and pan direction."""
    return [
        Slide(path=path, seconds=hold_seconds, direction=directions[i])
        for i, path in enumerate(paths)
    ]
