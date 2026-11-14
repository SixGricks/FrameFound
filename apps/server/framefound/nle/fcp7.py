"""FCP7 XML export: get search results into an editor (M9, ADR-0019).

The catalogue can find the shot in three milliseconds and then the editor
alt-tabs to Premiere and hunts for the file by hand. This closes that gap
without an Adobe SDK, code signing, or a panel — Premiere Pro and DaVinci
Resolve both import FCP7 XML, so one exporter serves the NLEs the target users
actually run. Final Cut Pro (10.x/11) is *not* one of them: it reads only
FCPXML, and an FCP7 file needs a converter such as 7toX first. Only the long
discontinued Final Cut Pro 7 opened this format natively.

Two things about this format are worth stating because they are where
implementations go wrong:

**Timing is in frames, not seconds.** Every duration and marker position is an
integer frame count against a declared timebase. A marker placed from a
floating-point second lands on the wrong frame roughly half the time, and on
long-form auction footage a half-second error is a different word.

**NTSC rates are not what they say.** 29.97 fps is 30 frames in 1001/1000
seconds, and 23.976 is 24 in the same ratio. The format expresses this as an
integer `timebase` plus an `ntsc` flag, so 29.97 is `timebase 30, ntsc TRUE`.
Writing `timebase 29` produces a file that imports and then drifts.

File paths become `file://` URLs. They are the paths *this server* sees, which
are not the paths the editor's workstation sees — a path mapping belongs here
eventually, and until then the export is honest about what it wrote.
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from urllib.parse import quote

# NTSC-family rates, mapped to the (timebase, ntsc) pair the format wants.
# The key is what a probe reports; the value is what the XML must say.
NTSC_RATES = {
    23.976: (24, True),
    23.98: (24, True),
    29.97: (30, True),
    59.94: (60, True),
    119.88: (120, True),
}
DEFAULT_TIMEBASE = 25


@dataclass
class Marker:
    """A point of interest — a transcript hit, or a detected scene change."""

    name: str
    seconds: float
    comment: str = ""


@dataclass
class Clip:
    name: str
    path: str
    duration_s: float | None = None
    fps: float | None = None
    width: int | None = None
    height: int | None = None
    has_audio: bool = True
    markers: list[Marker] = field(default_factory=list)


def rate_for(fps: float | None) -> tuple[int, bool]:
    """(timebase, ntsc) for a probed frame rate.

    Rounding is deliberate: probes report 29.969999... and a dictionary lookup
    on the raw float would miss.
    """
    if not fps or fps <= 0:
        return DEFAULT_TIMEBASE, False
    rounded = round(fps, 3)
    for rate, mapping in NTSC_RATES.items():
        if abs(rounded - rate) < 0.01:
            return mapping
    return max(1, round(fps)), False


def frames(seconds: float | None, fps: float | None) -> int:
    """The frame index containing an instant, at the real rate.

    Two decisions worth being explicit about.

    **The real rate, not the declared timebase.** 29.97 material declares
    `timebase 30`, but a wall-clock second contains 29.97 frames. Converting
    against 30 is the classic NTSC drift bug — a frame gained every ten
    seconds, which on a half-hour auction is nearly two seconds out.

    **Floor, not round.** A marker at t belongs on the frame that is on screen
    at t, which is the one below it. `round()` also half-rounds to even in
    Python, so it turns 299.7 into 300 but 12.5 into 12 — inconsistent in a way
    that is invisible until someone checks two values by hand. Truncating is
    unambiguous and is what timecode conventionally means. A duration may end up
    at most one frame short, which for a bin's clip length is inconsequential.
    """
    if not seconds or seconds <= 0:
        return 0
    return max(0, int(seconds * effective_rate(fps)))


def effective_rate(fps: float | None) -> float:
    """Frames per wall-clock second for a probed rate: 29.97 is 30*1000/1001."""
    timebase, ntsc = rate_for(fps)
    return timebase * 1000.0 / 1001.0 if ntsc else float(timebase)


def file_url(path: str) -> str:
    """A `file://` URL. Spaces and non-ASCII are everywhere in real media
    libraries, and an unquoted path silently fails to resolve on import.

    Windows paths arrive from the path-mapping profiles (`Z:\\Intel\\...`) and
    need their own shape: read as POSIX they were one opaque segment, quoted
    into `file://localhostZ%3A%5CIntel...`, and every clip imported offline.
    """
    if len(path) > 2 and path[0].isalpha() and path[1] == ":" and path[2] in "\\/":
        # The form Premiere itself writes: file://localhost/Z%3A/Intel/clip.mp4
        return "file://localhost/" + quote(path.replace("\\", "/"))
    if path.startswith("\\\\"):
        # UNC: \\nas\share\clip.mp4 -> file://nas/share/clip.mp4
        host, _, rest = path[2:].replace("\\", "/").partition("/")
        return f"file://{quote(host)}/{quote(rest)}"
    posix = PurePosixPath(path).as_posix()
    return "file://localhost" + quote(posix)


def _rate_element(parent: ET.Element, fps: float | None) -> None:
    timebase, ntsc = rate_for(fps)
    rate = ET.SubElement(parent, "rate")
    ET.SubElement(rate, "timebase").text = str(timebase)
    ET.SubElement(rate, "ntsc").text = "TRUE" if ntsc else "FALSE"


def _clip_element(clip: Clip, index: int) -> ET.Element:
    duration = frames(clip.duration_s, clip.fps)
    item = ET.Element("clip", {"id": f"clip-{index}"})
    ET.SubElement(item, "name").text = clip.name
    ET.SubElement(item, "duration").text = str(duration)
    _rate_element(item, clip.fps)
    # in/out of -1 means "the whole clip", which is what a bin wants; a
    # subclip would set real values here.
    ET.SubElement(item, "in").text = "-1"
    ET.SubElement(item, "out").text = "-1"

    file_el = ET.SubElement(item, "file", {"id": f"file-{index}"})
    ET.SubElement(file_el, "name").text = clip.name
    ET.SubElement(file_el, "pathurl").text = file_url(clip.path)
    _rate_element(file_el, clip.fps)
    ET.SubElement(file_el, "duration").text = str(duration)

    media = ET.SubElement(file_el, "media")
    video = ET.SubElement(media, "video")
    ET.SubElement(video, "duration").text = str(duration)
    if clip.width and clip.height:
        characteristics = ET.SubElement(video, "samplecharacteristics")
        ET.SubElement(characteristics, "width").text = str(clip.width)
        ET.SubElement(characteristics, "height").text = str(clip.height)
    if clip.has_audio:
        audio = ET.SubElement(media, "audio")
        ET.SubElement(audio, "channelcount").text = "2"

    for marker in clip.markers:
        position = frames(marker.seconds, clip.fps)
        marker_el = ET.SubElement(item, "marker")
        ET.SubElement(marker_el, "name").text = marker.name
        ET.SubElement(marker_el, "comment").text = marker.comment
        ET.SubElement(marker_el, "in").text = str(position)
        # A zero-length marker is a point rather than a range; -1 says so.
        ET.SubElement(marker_el, "out").text = "-1"
    return item


def build_bin(name: str, clips: list[Clip]) -> str:
    """An FCP7 XML bin containing the given clips, as a string.

    A bin rather than a sequence on purpose: a search result set is a group of
    clips to look through, not an edit. Dropping an edit order on the operator
    would be presuming to know what they intend to do with it.
    """
    root = ET.Element("xmeml", {"version": "5"})
    bin_el, _files = _bin_element(name, clips)
    root.append(bin_el)
    return _document(root)


def _bin_element(name: str, clips: list[Clip]) -> tuple[ET.Element, dict[str, str]]:
    """A <bin> of clips, and the file id each clip's path was given — so a
    sequence in the same document can refer to those files instead of
    defining them a second time under ids that collide."""
    bin_el = ET.Element("bin")
    ET.SubElement(bin_el, "name").text = name
    children = ET.SubElement(bin_el, "children")
    files: dict[str, str] = {}
    for index, clip in enumerate(clips, start=1):
        children.append(_clip_element(clip, index))
        files.setdefault(clip.path, f"file-{index}")
    return bin_el, files


def build_project(
    name: str,
    bin_name: str,
    clips: list[Clip],
    sequence_name: str,
    cuts: list["Cut"],
) -> str:
    """One document holding a bin and a sequence cut from it: the selects
    hand-off (nle/selects.py). Premiere imports a <project> as a bin named
    after it, with the clips bin and the sequence inside.

    The sequence's clipitems refer to the bin's <file> elements by id. File
    ids are global to the document, and writing each file twice — once in
    the bin, once in the sequence, both as `file-1` — is the kind of thing an
    importer resolves by keeping whichever it read first."""
    root = ET.Element("xmeml", {"version": "5"})
    project = ET.SubElement(root, "project")
    ET.SubElement(project, "name").text = name
    children = ET.SubElement(project, "children")
    bin_el, files = _bin_element(bin_name, clips)
    children.append(bin_el)
    children.append(_sequence_element(sequence_name, cuts, files_written=files))
    return _document(root)


_UNWRITABLE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def xml_text(text: str) -> str:
    """`text` without the characters no XML 1.0 file can hold: control
    characters other than tab and the line ends, lone surrogates, and the
    two non-characters U+FFFE and U+FFFF.

    ElementTree writes them as they are, and one NUL in a marker's note
    makes the whole file one Premiere and Resolve refuse to import. A
    model's reason can carry one as valid JSON (an escaped NUL); so can a
    title or a note Claude sends. Text without them is returned unchanged,
    so a file that was valid is written byte for byte as before."""
    return _UNWRITABLE.sub("", text)


def _document(root: ET.Element) -> str:
    ET.indent(root, space="  ")
    # Every writer's last step, so a name, title or note from anywhere is
    # covered: what cannot be in the file is left out of it.
    body = xml_text(ET.tostring(root, encoding="unicode"))
    # The DOCTYPE is not optional: Premiere refuses files without it.
    return '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + body + "\n"


@dataclass
class Cut:
    """One piece of a rough cut: a clip from `in_s` to `out_s` of its source.

    A still photograph is a Cut too (`is_still`), held on screen for
    `out_s - in_s` seconds — real-estate pieces mix drone video with the
    listing's photographs.
    """

    clip: Clip
    in_s: float
    out_s: float
    is_still: bool = False
    note: str = ""
    # Where the note's marker goes, in seconds of the source. None puts it on
    # the cut's first frame; a select with handles puts it on the moment
    # itself, a second in, where the words start.
    marker_s: float | None = None
    marker_name: str | None = None


def build_sequence(
    name: str,
    cuts: list[Cut],
    *,
    fps: float | None = None,
    width: int | None = None,
    height: int | None = None,
) -> str:
    """An FCP7 XML *sequence*: the cuts laid end to end on V1 (and A1/A2).

    Where `build_bin` hands over a pile of clips, this is an edit someone
    (or Claude, through the MCP connector) has already decided on — a
    rough cut to open in Premiere or Resolve and finish by hand. Every cut
    references its original through a workstation path, so nothing is
    copied and the editor relinks nothing.

    Timing is in frames at each *source's* rate for in/out, and at the
    sequence's rate for where the piece sits on the timeline — the two
    differ when 23.976 drone footage meets a 29.97 interview.
    """
    root = ET.Element("xmeml", {"version": "5"})
    root.append(_sequence_element(name, cuts, fps=fps, width=width, height=height))
    return _document(root)


def _sequence_element(
    name: str,
    cuts: list[Cut],
    *,
    fps: float | None = None,
    width: int | None = None,
    height: int | None = None,
    files_written: dict[str, str] | None = None,
) -> ET.Element:
    seq_fps = fps or next((c.clip.fps for c in cuts if c.clip.fps and not c.is_still), None)
    sequence = ET.Element("sequence", {"id": "sequence-1"})
    ET.SubElement(sequence, "name").text = name
    duration_el = ET.SubElement(sequence, "duration")
    _rate_element(sequence, seq_fps)

    media = ET.SubElement(sequence, "media")
    video = ET.SubElement(media, "video")
    fmt = ET.SubElement(video, "format")
    characteristics = ET.SubElement(fmt, "samplecharacteristics")
    first = next((c.clip for c in cuts if not c.is_still and c.clip.width), None)
    ET.SubElement(characteristics, "width").text = str(width or (first.width if first else 1920))
    ET.SubElement(characteristics, "height").text = str(height or (first.height if first else 1080))
    _rate_element(characteristics, seq_fps)
    video_track = ET.SubElement(video, "track")
    audio = ET.SubElement(media, "audio")
    audio_tracks = [ET.SubElement(audio, "track"), ET.SubElement(audio, "track")]

    files_written = {} if files_written is None else dict(files_written)
    # Placed from running *seconds*, not by adding frame counts: each cut's
    # length truncates to whole frames, and summing those lost up to a frame
    # per cut — on a two-hundred-cut piece, several seconds of drift from
    # where the transcript says the words are.
    elapsed_s = 0.0
    position = 0
    seq_rate = effective_rate(seq_fps)
    for index, cut in enumerate(cuts, start=1):
        length_s = max(0.0, cut.out_s - cut.in_s)
        end = frames(elapsed_s + length_s, seq_fps)
        span = end - position
        if span <= 0:
            continue
        # A still has no rate of its own, and a clip with no probed rate is
        # taken at the sequence's — so in/out are always counted in the rate
        # the clipitem declares, not a 25 fps fallback it never mentions.
        source_rate = seq_fps if cut.is_still else (cut.clip.fps or seq_fps)
        same_rate = rate_for(source_rate) == rate_for(seq_fps)
        source_in = 0 if cut.is_still else frames(cut.in_s, source_rate)
        # The source span comes *from* the timeline span. Flooring the cut's
        # length on its own as well gave two numbers that disagreed by a
        # frame on about half the cuts (a 29.97 cut 180 frames long on the
        # timeline over 179 of source; seven of ten 1.03 s cuts at 25 fps),
        # and an NLE reads a clipitem whose out-in differs from end-start at
        # one rate as a speed change or a shifted out point. Across rates
        # (23.976 drone into a 29.97 interview) it converts by the real,
        # NTSC-aware rates.
        if same_rate:
            source_span = span
        else:
            source_span = max(1, round(span * effective_rate(source_rate) / seq_rate))
        clock_end = end
        if not cut.is_still and cut.clip.duration_s:
            # Derived from the timeline, a cut that ends on the clip's last
            # frame can point one frame past the media, which imports as a
            # frame of offline. Pull both ends back so they still agree.
            last = frames(cut.clip.duration_s, source_rate)
            if source_in + source_span > last:
                source_span = last - source_in
                span = (
                    source_span
                    if same_rate
                    else round(source_span * seq_rate / effective_rate(source_rate))
                )
                if source_span <= 0 or span <= 0:
                    continue
                end = position + span
        # The running clock gives up whatever the clamp trimmed. Advancing it
        # by the full requested length handed those frames to the next cut,
        # whose source span is derived from its timeline span — so a quote
        # after three b-roll cuts run to their clip's end played two frames
        # past where it was cut (out 6100 for a requested 6098). Taking the
        # trimmed frames off keeps the clock's sub-frame remainder intact.
        elapsed_s += length_s - (clock_end - end) / seq_rate
        place = _Placement(
            start=position,
            end=end,
            source_in=source_in,
            source_out=source_in + source_span,
            source_rate=source_rate,
        )
        position = place.end
        video_track.append(_clipitem(f"clipitem-v{index}", cut, place, files_written))
        if cut.clip.has_audio and not cut.is_still:
            for channel, track_el in enumerate(audio_tracks, start=1):
                item = _clipitem(f"clipitem-a{channel}-{index}", cut, place, files_written)
                source_track = ET.SubElement(item, "sourcetrack")
                ET.SubElement(source_track, "mediatype").text = "audio"
                ET.SubElement(source_track, "trackindex").text = str(channel)
                track_el.append(item)

    duration_el.text = str(position)
    return sequence


@dataclass(frozen=True)
class _Placement:
    start: int  # timeline frames, at the sequence rate
    end: int
    source_in: int  # source frames, at the source's own rate
    source_out: int
    source_rate: float | None


def _clipitem(
    item_id: str, cut: Cut, place: _Placement, files_written: dict[str, str]
) -> ET.Element:
    """One <clipitem> on a sequence track. The first use of an original
    carries its full <file>; later uses refer back to it by id, as
    Premiere itself writes it."""
    item = ET.Element("clipitem", {"id": item_id})
    ET.SubElement(item, "name").text = cut.clip.name
    total = place.source_out if cut.is_still else frames(cut.clip.duration_s, place.source_rate)
    ET.SubElement(item, "duration").text = str(max(total, place.source_out))
    _rate_element(item, place.source_rate)
    ET.SubElement(item, "start").text = str(place.start)
    ET.SubElement(item, "end").text = str(place.end)
    ET.SubElement(item, "in").text = str(place.source_in)
    ET.SubElement(item, "out").text = str(place.source_out)
    key = cut.clip.path
    if key in files_written:
        ET.SubElement(item, "file", {"id": files_written[key]})
    else:
        file_id = f"file-{len(files_written) + 1}"
        files_written[key] = file_id
        file_el = ET.SubElement(item, "file", {"id": file_id})
        ET.SubElement(file_el, "name").text = cut.clip.name
        ET.SubElement(file_el, "pathurl").text = file_url(cut.clip.path)
        _rate_element(file_el, place.source_rate)
        if not cut.is_still:
            ET.SubElement(file_el, "duration").text = str(total)
        file_media = ET.SubElement(file_el, "media")
        file_video = ET.SubElement(file_media, "video")
        if cut.clip.width and cut.clip.height:
            chars = ET.SubElement(file_video, "samplecharacteristics")
            ET.SubElement(chars, "width").text = str(cut.clip.width)
            ET.SubElement(chars, "height").text = str(cut.clip.height)
        if cut.clip.has_audio and not cut.is_still:
            file_audio = ET.SubElement(file_media, "audio")
            ET.SubElement(file_audio, "channelcount").text = "2"
    if cut.note:
        at = place.source_in
        if cut.marker_s is not None and not cut.is_still:
            # Inside the cut whatever rounding did to its ends.
            at = min(
                max(frames(cut.marker_s, place.source_rate), place.source_in), place.source_out - 1
            )
        marker = ET.SubElement(item, "marker")
        ET.SubElement(marker, "name").text = (cut.marker_name or cut.note)[:60]
        ET.SubElement(marker, "comment").text = cut.note
        ET.SubElement(marker, "in").text = str(at)
        ET.SubElement(marker, "out").text = "-1"
    return item
