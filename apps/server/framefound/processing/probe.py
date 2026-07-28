"""Technical/capture metadata extraction via ffprobe, ExifTool, and Pillow.

Subprocess safety rules (threat model / brief §23): argv arrays only — a
filename is data, never shell text; hard timeouts; stderr discarded from
results; absence of a tool degrades gracefully to whatever the others give.
"""

import contextlib
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

SUBPROCESS_TIMEOUT_S = 60


def _run_json_tool(argv: list[str]) -> Any | None:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed binaries, argv form, no shell
            argv, capture_output=True, timeout=SUBPROCESS_TIMEOUT_S, check=False
        )
        if completed.returncode != 0:
            return None
        return json.loads(completed.stdout.decode("utf-8", errors="replace"))
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        log.warning("probe.tool_failed", tool=argv[0])
        return None


def _parse_capture_datetime(value: str) -> datetime | None:
    """ExifTool emits 'YYYY:MM:DD HH:MM:SS' with optional subseconds/offset."""
    text = value.strip()
    for fmt in ("%Y:%m:%d %H:%M:%S%z", "%Y:%m:%d %H:%M:%S"):
        with contextlib.suppress(ValueError):
            parsed = datetime.strptime(text.split(".")[0][:25], fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _fps_from_ratio(ratio: str) -> float | None:
    with contextlib.suppress(ValueError, ZeroDivisionError):
        num, _, den = ratio.partition("/")
        return round(float(num) / float(den or 1), 3)
    return None


def probe_ffprobe(path: Path) -> dict[str, Any]:
    if shutil.which("ffprobe") is None:
        return {}
    data = _run_json_tool(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
    )
    if not isinstance(data, dict):
        return {}
    fields: dict[str, Any] = {}
    fmt = data.get("format", {})
    with contextlib.suppress(TypeError, ValueError):
        fields["duration_s"] = round(float(fmt["duration"]), 3)
    with contextlib.suppress(TypeError, ValueError):
        fields["bitrate"] = int(fmt["bit_rate"])
    created = fmt.get("tags", {}).get("creation_time")
    if isinstance(created, str):
        with contextlib.suppress(ValueError):
            fields["captured_at"] = datetime.fromisoformat(created.replace("Z", "+00:00"))

    for stream in data.get("streams", []):
        codec_type = stream.get("codec_type")
        if codec_type == "video" and "video_codec" not in fields:
            fields["video_codec"] = stream.get("codec_name")
            fields["width"] = stream.get("width")
            fields["height"] = stream.get("height")
            rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or ""
            if (fps := _fps_from_ratio(rate)) and fps > 0:
                fields["fps"] = fps
        elif codec_type == "audio" and "audio_codec" not in fields:
            fields["audio_codec"] = stream.get("codec_name")
            with contextlib.suppress(TypeError, ValueError):
                fields["sample_rate"] = int(stream["sample_rate"])
            fields["channels"] = stream.get("channels")
    return {k: v for k, v in fields.items() if v is not None}


_EXIF_FIELD_MAP = {
    "Make": "camera_make",
    "Model": "camera_model",
    "LensModel": "lens",
    "LensID": "lens",
    "FocalLength": "focal_length_mm",
    "FNumber": "aperture_f",
    "ISO": "iso",
    "Orientation": "orientation",
    "GPSLatitude": "gps_lat",
    "GPSLongitude": "gps_lon",
    "ImageWidth": "width",
    "ImageHeight": "height",
}


def probe_exiftool(path: Path) -> dict[str, Any]:
    if shutil.which("exiftool") is None:
        return {}
    data = _run_json_tool(["exiftool", "-json", "-n", str(path)])
    if not isinstance(data, list) or not data:
        return {}
    raw = data[0]
    fields: dict[str, Any] = {}
    for exif_key, field in _EXIF_FIELD_MAP.items():
        value = raw.get(exif_key)
        if value is not None and field not in fields:
            fields[field] = value
    exposure = raw.get("ExposureTime")
    if isinstance(exposure, int | float) and exposure > 0:
        fields["shutter_speed"] = f"1/{round(1 / exposure)}" if exposure < 1 else f"{exposure:g}s"
    for date_key in ("SubSecDateTimeOriginal", "DateTimeOriginal", "CreateDate"):
        value = raw.get(date_key)
        if isinstance(value, str) and (parsed := _parse_capture_datetime(value)):
            fields["captured_at"] = parsed
            break
    return fields


def probe_pillow(path: Path) -> dict[str, Any]:
    try:
        from PIL import Image
    except ImportError:
        return {}
    try:
        with Image.open(path) as img:
            return {"width": img.width, "height": img.height}
    except Exception:
        # Never trust file contents: a corrupt/hostile image just yields nothing.
        return {}


def probe_media(path: Path, media_type: str) -> dict[str, Any]:
    """Merged metadata for one file. Later sources win only for keys the
    earlier ones didn't fill; EXIF beats container tags for capture fields."""
    fields: dict[str, Any] = {}
    if path.suffix.lower() == ".braw":
        from framefound.processing.braw import probe_braw

        fields.update(probe_braw(path))  # {} when the decoder isn't installed
    elif media_type in ("video", "audio"):
        fields.update(probe_ffprobe(path))
    else:
        fields.update(probe_pillow(path))
    for key, value in probe_exiftool(path).items():
        if key in ("camera_make", "camera_model", "lens", "captured_at") or key not in fields:
            fields[key] = value
    return fields
