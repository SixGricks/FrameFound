"""Turning a selection of stills into a video.

Deterministic throughout. Nothing is generated and nothing is invented — this
pans and crossfades photographs the operator already took, which is why it is
reliable enough to put a client's name on and why it needs no GPU.

**Why the video is built out of small pieces rather than one filter graph.**

The obvious implementation feeds every still into one `filter_complex` and lets
FFmpeg do the rest. Measured on the reference deployment, against the worker's
1000 MB limit:

    one slide, scale + x264, no zoompan          126 MB
    one slide, zoompan @1080p                    795 MB
    one slide, zoompan @4K supersample           787 MB
    one slide, zoompan + encoder threads capped  470 MB
    one slide, ... + a short x264 lookahead      259 MB
    xfade chain, 4 / 8 / 16 / 24 segments        killed, all of them

Two separate findings there. First, resolution is irrelevant — 4K costs the
same as 1080p — because the memory is x264's frame-threading lookahead, and
zoompan emits frames faster than the encoder drains them. Second, and fatally
for the simple design: an xfade chain is killed at *any* length, including
four. Chained xfades make every later input's decoder run ahead and buffer
frames until its transition point, so the graph holds most of the slideshow in
memory no matter how the work is batched.

So the video is assembled from pieces that are each cheap to produce:

    slide 0 body | fade 0->1 | slide 1 body | fade 1->2 | ... | slide n body

A *body* is the part of a slide not shared with a neighbour's crossfade: one
input, one encoder. A *fade* is the tail of one slide dissolving into the head
of the next: exactly two inputs, and only `transition_seconds` long. The pieces
are then stitched by the concat **demuxer** with `-c copy`, which rewrites
timestamps and copies packets without decoding anything.

Measured, twelve slides: worst piece 270 MB, concat 56 MB, and neither figure
grows with the length of the slideshow. Every piece is encoded exactly once,
straight from its still, so the stitch costs no generation of quality either.

The trims are expressed in **frames rather than seconds**, so a body and the
fade that abuts it can never disagree about where the boundary is by a frame.

The builders here are pure functions returning argv, so the whole thing can be
tested without encoding anything. A wrong graph is the expensive failure: it
either dies after minutes of work, or produces something subtly wrong —
transitions in the wrong place, a file that plays nowhere — that survives
review and is noticed only after the video has been handed over.
"""

from dataclasses import dataclass, field

# 30 fps: what social platforms expect, and a Ken Burns move at 24 shows
# visible stepping on a slow pan.
FPS = 30

# zoompan pans by whole pixels, so a slow move across a 1080p still quantises
# and judders. Rendering the move on an upscaled copy hides that, and costs
# nothing measurable — 787 MB at 4K against 795 MB at 1080p, because the
# encoder queue dominates.
ZOOM_SUPERSAMPLE = 2

# 1.0 -> 1.12 is about as far as a still travels before the upscale shows as
# softness on a 1080p delivery.
MAX_ZOOM = 1.12


class RenderPlanError(ValueError):
    """The requested pacing cannot be rendered."""


@dataclass(frozen=True)
class Slide:
    """One still, and how long it is on screen."""

    path: str
    seconds: float
    # Alternating per slide; see `alternate_directions`.
    direction: str = "in"


@dataclass
class RenderSpec:
    width: int = 1920
    height: int = 1080
    fps: int = FPS
    transition_seconds: float = 0.6
    # eq parameters from the theme. 1.0/1.0/0.0 is a no-op, which is the right
    # default for photographs that are already graded.
    saturation: float = 1.0
    contrast: float = 1.0
    brightness: float = 0.0
    audio_path: str = ""
    # Fade the bed out rather than cutting it; music that simply stops sounds
    # like a fault even when it is deliberate.
    audio_fade_seconds: float = 2.5
    # libx264 | h264_nvenc. Set by the pipeline from a functional GPU probe,
    # never guessed here — this module stays free of side effects so the
    # graphs can be tested without a machine to run them on.
    video_codec: str = "libx264"
    slides: list[Slide] = field(default_factory=list)

    @property
    def fade_frames(self) -> int:
        return max(1, round(self.transition_seconds * self.fps))

    @property
    def total_frames(self) -> int:
        if not self.slides:
            return 0
        bounds = (body_bounds(i, self) for i in range(len(self.slides)))
        bodies = sum(end - start for start, end in bounds)
        return bodies + (len(self.slides) - 1) * self.fade_frames

    @property
    def total_seconds(self) -> float:
        return round(self.total_frames / self.fps, 3)


def slide_frames(slide: Slide, spec: RenderSpec) -> int:
    return max(1, round(slide.seconds * spec.fps))


def body_bounds(index: int, spec: RenderSpec) -> tuple[int, int]:
    """The frame range of a slide that is not shared with a crossfade.

    A middle slide gives up `fade_frames` at each end; the first and last give
    up only the end they actually share.
    """
    slide = spec.slides[index]
    total = slide_frames(slide, spec)
    start = spec.fade_frames if index > 0 else 0
    end = total - (spec.fade_frames if index < len(spec.slides) - 1 else 0)
    if end <= start:
        raise RenderPlanError(
            f"A {slide.seconds:g}s slide cannot hold a {spec.transition_seconds:g}s "
            "transition at each end. Hold each photo longer, or shorten the transition."
        )
    return start, end


def alternate_directions(count: int) -> list[str]:
    """Zoom in, then out, then in.

    Every slide drifting the same way reads as a template; randomising reads as
    a glitch. Alternating reads as a choice somebody made.
    """
    return ["in" if i % 2 == 0 else "out" for i in range(count)]


def ken_burns_filter(slide: Slide, spec: RenderSpec) -> str:
    """A slow push or pull across one still, for the slide's full length.

    Always the whole move, even when only a fraction of it is kept: the trim
    that follows selects a window *of this motion*, so the tail rendered into a
    crossfade continues exactly where the body left off.

    zoompan's `d` counts *output* frames, and is applied per *input* frame — so
    the caller must not loop the still. `-loop 1 -t 3` would feed 90 input
    frames and ask for 90 output frames from each.
    """
    frames = slide_frames(slide, spec)
    big_w, big_h = spec.width * ZOOM_SUPERSAMPLE, spec.height * ZOOM_SUPERSAMPLE
    step = (MAX_ZOOM - 1.0) / frames
    if slide.direction == "in":
        zoom = f"min(zoom+{step:.8f},{MAX_ZOOM})"
    else:
        zoom = f"max({MAX_ZOOM}-on*{step:.8f},1.0)"

    return (
        # Fit inside the canvas and pad to fill it, so a vertical phone photo
        # in a 16:9 slideshow gets bars rather than being cropped to a sliver.
        f"scale={big_w}:{big_h}:force_original_aspect_ratio=decrease,"
        f"pad={big_w}:{big_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"zoompan=z='{zoom}':d={frames}"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":s={spec.width}x{spec.height}:fps={spec.fps},"
        # Both xfade and the concat of pieces need a known, matching SAR.
        f"setsar=1"
    )


def colour_filter(spec: RenderSpec) -> str:
    """Theme grade. Omitted entirely when it would be a no-op."""
    if (spec.saturation, spec.contrast, spec.brightness) == (1.0, 1.0, 0.0):
        return ""
    return f"eq=saturation={spec.saturation}:contrast={spec.contrast}:brightness={spec.brightness}"


def cut_chain(slide: Slide, spec: RenderSpec, start_frame: int, end_frame: int) -> str:
    """The Ken Burns move, graded, windowed to a frame range, starting at zero.

    The trailing `fps` is not redundant. `setpts=PTS-STARTPTS` rebases the
    timestamps and in doing so discards the stream's frame-rate metadata; xfade
    then refuses the input outright — *"The inputs needs to be a constant frame
    rate; current rate of 1/0 is invalid"*. The symptom is a transition clip
    with no frames in it, and an encoder that reports only that it could not
    start.
    """
    chain = ken_burns_filter(slide, spec)
    grade = colour_filter(spec)
    if grade:
        chain = f"{chain},{grade}"
    return (
        f"{chain},trim=start_frame={start_frame}:end_frame={end_frame},"
        f"setpts=PTS-STARTPTS,fps={spec.fps}"
    )


def _video_codec_args(spec: RenderSpec) -> list[str]:
    """Encoder settings shared by every piece.

    Every piece must agree on these, or the concat demuxer is stitching streams
    with different parameters and players disagree about what to do with that.

    **`veryfast` rather than `medium`.** Measured on one slide: 6.8 s against
    19.0 s, producing a *smaller* file (984 KB against 1107 KB). There is no
    tradeoff to weigh here — slow pans give a slower preset almost nothing to
    work with, and the difference is three minutes against nine on a
    forty-photograph show. `ultrafast` is where it does turn into a tradeoff:
    3.1 s, but a 9.6 MB file.

    **`sync-lookahead=0:rc-lookahead=10`** is a memory decision rather than a
    quality one: it took a measured slide from 470 MB to 259 MB.

    NVENC is used when the GPU is genuinely usable, on the same
    accelerator-never-a-requirement footing as the proxy transcoder. Its
    parameters are chosen together with the codec because `-x264-params` is not
    merely ignored by NVENC — it is rejected.
    """
    if spec.video_codec == "h264_nvenc":
        codec = ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "22"]
    else:
        codec = [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-x264-params",
            "sync-lookahead=0:rc-lookahead=10",
        ]
    return [
        *codec,
        # yuv420p or the file is valid and plays in nothing the operator's
        # client will actually open it on.
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(spec.fps),
    ]


def body_argv(index: int, spec: RenderSpec, output_path: str) -> list[str]:
    """One slide's unshared middle. A single input and a single encoder."""
    slide = spec.slides[index]
    start, end = body_bounds(index, spec)
    return [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        # Deliberately no `-loop 1 -t`; see `ken_burns_filter`.
        "-i",
        slide.path,
        "-vf",
        cut_chain(slide, spec, start, end),
        *_video_codec_args(spec),
        output_path,
    ]


def transition_argv(index: int, spec: RenderSpec, output_path: str) -> list[str]:
    """The crossfade from slide `index` into slide `index + 1`.

    Exactly two inputs, and only `transition_seconds` of output — which is what
    keeps this bounded where a chained xfade was not.
    """
    outgoing, incoming = spec.slides[index], spec.slides[index + 1]
    tail_start = slide_frames(outgoing, spec) - spec.fade_frames
    tail = cut_chain(outgoing, spec, tail_start, slide_frames(outgoing, spec))
    head = cut_chain(incoming, spec, 0, spec.fade_frames)
    graph = (
        f"[0:v]{tail}[a];[1:v]{head}[b];"
        f"[a][b]xfade=transition=fade:duration={spec.transition_seconds}:offset=0[outv]"
    )
    return [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        outgoing.path,
        "-i",
        incoming.path,
        "-filter_complex",
        graph,
        "-map",
        "[outv]",
        *_video_codec_args(spec),
        output_path,
    ]


def piece_plan(count: int) -> list[tuple[str, int]]:
    """The pieces to render, in the order they are played.

    body 0, fade 0, body 1, fade 1, ... body n-1.
    """
    if count <= 0:
        return []
    pieces: list[tuple[str, int]] = []
    for index in range(count):
        pieces.append(("body", index))
        if index < count - 1:
            pieces.append(("fade", index))
    return pieces


def concat_list(paths: list[str]) -> str:
    """The concat demuxer's playlist.

    Single quotes are the demuxer's own escape convention; a path containing
    one would end the filename early. Every path here is generated by us inside
    a private working directory, so this asserts rather than escapes — a
    quote appearing would mean something upstream is wrong.
    """
    for path in paths:
        if "'" in path or "\n" in path:
            raise RenderPlanError(f"Unusable working path: {path!r}")
    return "".join(f"file '{path}'\n" for path in paths)


def concat_argv(
    list_path: str, spec: RenderSpec, output_path: str, *, with_audio: bool = True
) -> list[str]:
    """Stitch the pieces. Copies packets — no decode, no re-encode.

    `-safe 0` because the playlist holds absolute paths.
    """
    argv = ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", list_path]

    audio = with_audio and bool(spec.audio_path)
    if audio:
        argv += ["-i", spec.audio_path]

    argv += ["-map", "0:v", "-c:v", "copy"]

    if audio:
        fade_start = max(0.0, spec.total_seconds - spec.audio_fade_seconds)
        argv += [
            "-map",
            "1:a",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-af",
            f"afade=t=out:st={fade_start:.3f}:d={spec.audio_fade_seconds}",
            # Trim the bed to the picture rather than the other way around.
            "-shortest",
        ]

    argv += ["-movflags", "+faststart", output_path]
    return argv
