"""The thread cap has to reach the encoder, not just the decoder.

`-threads` is a per-file option in FFmpeg, not a global one. Placed before
`-i` it configures the decoder; placed before the output path it configures
the encoder. The original implementation set only the first, so x264 kept
running one thread per core with the setting appearing to be in force.

Measured on the reference deployment (12 cores), one 1080p slideshow slide:

    no cap                     810 MB
    -threads 2 before -i       809 MB   <- what the code used to do
    -threads 2 before output   470 MB

Most of the distance to the worker's memory limit, paid on every proxy
transcode, invisibly.
"""

from framefound.processing.ffmpeg import _with_thread_cap


def _positions(argv: list[str]) -> list[int]:
    return [i for i, a in enumerate(argv) if a == "-threads"]


def test_the_decoder_is_capped() -> None:
    """Before -i, so a many-threaded decode of a large source is bounded."""
    argv = _with_thread_cap(["ffmpeg", "-i", "in.mov", "-c:v", "libx264", "out.mp4"])
    first = _positions(argv)[0]
    assert first < argv.index("-i")


def test_the_encoder_is_capped() -> None:
    """The regression this file exists for: an encoder-side -threads must sit
    after the last input and before the output path."""
    argv = _with_thread_cap(["ffmpeg", "-i", "in.mov", "-c:v", "libx264", "out.mp4"])
    last = _positions(argv)[-1]
    assert last > argv.index("-i")
    assert last < argv.index("out.mp4")


def test_the_output_path_stays_last() -> None:
    """FFmpeg reads the trailing argument as the destination. Appending an
    option after it silently changes what gets written where."""
    argv = _with_thread_cap(["ffmpeg", "-i", "in.mov", "out.mp4"])
    assert argv[-1] == "out.mp4"


def test_an_explicit_cap_from_the_caller_is_left_alone() -> None:
    original = ["ffmpeg", "-i", "in.mov", "-threads", "8", "out.mp4"]
    assert _with_thread_cap(original) == original


def test_zero_restores_the_ffmpeg_default(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The documented escape hatch for a machine with cores to spare."""
    import framefound.processing.ffmpeg as mod

    class _Settings:
        ffmpeg_threads = 0

    monkeypatch.setattr(mod, "get_settings", lambda: _Settings())
    original = ["ffmpeg", "-i", "in.mov", "out.mp4"]
    assert _with_thread_cap(original) == original


def test_a_degenerate_argv_is_not_mangled() -> None:
    """`argv[1:-1]` on a one-element list would otherwise duplicate the binary
    and produce `['ffmpeg', '-threads', '2', 'ffmpeg']`."""
    assert _with_thread_cap(["ffmpeg"]) == ["ffmpeg"]
