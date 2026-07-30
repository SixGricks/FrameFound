"""FCP7 XML export: get search results into an editor (M9, ADR-0019).

The catalogue can find the shot in three milliseconds and then the editor
alt-tabs to Premiere and hunts for the file by hand. This closes that gap
without an Adobe SDK, code signing, or a panel — Premiere, DaVinci Resolve and
Final Cut all import FCP7 XML, so one exporter serves every NLE the target
users actually run.

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
    timebase, ntsc = rate_for(fps)
    effective = timebase * 1000.0 / 1001.0 if ntsc else float(timebase)
    return max(0, int(seconds * effective))


def file_url(path: str) -> str:
    """A `file://` URL. Spaces and non-ASCII are everywhere in real media
    libraries, and an unquoted path silently fails to resolve on import."""
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
    bin_el = ET.SubElement(root, "bin")
    ET.SubElement(bin_el, "name").text = name
    children = ET.SubElement(bin_el, "children")
    for index, clip in enumerate(clips, start=1):
        children.append(_clip_element(clip, index))

    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    # The DOCTYPE is not optional: Premiere refuses files without it.
    return '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + body + "\n"
