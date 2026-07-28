"""Subtitle generation and parsing (WebVTT / SubRip).

Editors often ship hand-corrected .srt sidecars next to their footage. Those
are better than anything ASR produces, so the pipeline imports them instead
of transcribing when one is present.
"""

import re
from pathlib import Path

from framefound.ai.transcription import SpeechSegment, TranscriptionResult

SIDECAR_SUFFIXES = (".srt", ".vtt")

# 00:01:02,500 (SubRip) or 00:01:02.500 (WebVTT); hours optional in VTT.
_TS = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")
_ARROW = re.compile(r"-->")


def _timestamp(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    hours, rest = divmod(total_ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, ms = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def build_vtt(segments: list[SpeechSegment]) -> str:
    lines = ["WEBVTT", ""]
    for seg in segments:
        lines.append(f"{_timestamp(seg.start_s)} --> {_timestamp(seg.end_s)}")
        lines.append(seg.text)
        lines.append("")
    return "\n".join(lines)


def _parse_timestamp(text: str) -> float | None:
    match = _TS.search(text)
    if match is None:
        return None
    hours, minutes, secs, frac = match.groups()
    ms = int(frac.ljust(3, "0"))
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(secs) + ms / 1000


def parse_subtitles(text: str) -> list[SpeechSegment]:
    """Parse SubRip or WebVTT cues into segments.

    Tolerant by design: subtitle files in the wild carry BOMs, CRLF endings,
    cue identifiers, styling blocks, and stray numbering. Anything that isn't
    a well-formed timed cue is skipped rather than failing the import.
    """
    segments: list[SpeechSegment] = []
    body = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    for block in body.split("\n\n"):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        timing_index = next((i for i, ln in enumerate(lines) if _ARROW.search(ln)), None)
        if timing_index is None:
            continue  # WEBVTT header, NOTE/STYLE blocks, bare cue ids
        timing = lines[timing_index]
        start_text, _, end_text = timing.partition("-->")
        start = _parse_timestamp(start_text)
        end = _parse_timestamp(end_text)
        if start is None or end is None or end < start:
            continue
        # Strip inline tags (<i>, {\an8}) that belong to presentation only.
        content = " ".join(lines[timing_index + 1 :])
        content = re.sub(r"<[^>]+>|\{[^}]*\}", "", content).strip()
        if content:
            segments.append(SpeechSegment(start_s=start, end_s=end, text=content))
    return segments


def find_sidecar(source: Path) -> Path | None:
    """Locate a subtitle file alongside the media (same stem)."""
    for suffix in SIDECAR_SUFFIXES:
        for candidate in (
            source.with_suffix(suffix),
            source.with_suffix(suffix.upper()),
            source.parent / f"{source.name}{suffix}",  # clip.mp4.srt
        ):
            if candidate.is_file():
                return candidate
    return None


def import_sidecar(path: Path) -> TranscriptionResult | None:
    """Build a TranscriptionResult from an existing subtitle file."""
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    segments = parse_subtitles(text)
    if not segments:
        return None
    return TranscriptionResult(
        language="en",  # TODO(m5): detect from content or filename tags
        language_probability=1.0,
        duration_s=segments[-1].end_s,
        model_name=f"sidecar/{path.suffix.lstrip('.').lower()}",
        segments=segments,
    )
