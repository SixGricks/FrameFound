"""WebVTT generation from transcript segments."""

from framefound.ai.transcription import SpeechSegment


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
