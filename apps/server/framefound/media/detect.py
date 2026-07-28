"""Extension-based media type detection (brief §5.4).

Extensions are the *dispatch* signal only — actual decoding always sniffs and
validates content (never trust filenames). RAW coverage grows with library
support in M3+.
"""

import mimetypes
from pathlib import PurePath

IMAGE_EXTENSIONS = {
    "jpg",
    "jpeg",
    "png",
    "tif",
    "tiff",
    "webp",
    "heic",
    "heif",
    "bmp",
    "gif",
    # Camera RAW (metadata + preview support; full decode arrives with rawpy in M3)
    "cr2",
    "cr3",
    "nef",
    "arw",
    "dng",
    "orf",
    "rw2",
    "raf",
}
VIDEO_EXTENSIONS = {
    "mp4",
    "mov",
    "m4v",
    "mkv",
    "avi",
    "mxf",
    "mts",
    "m2ts",
    "mpg",
    "mpeg",
    "webm",
    "wmv",
    # Blackmagic RAW: indexed + ExifTool metadata; FFmpeg cannot decode BRAW,
    # so posters/proxies record a clean failure until a BRAW decoder lands.
    "braw",
}
AUDIO_EXTENSIONS = {"wav", "mp3", "m4a", "aac", "flac", "ogg", "aif", "aiff"}

SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | AUDIO_EXTENSIONS


def extension_of(name: str) -> str:
    return PurePath(name).suffix.lstrip(".").lower()


def media_type_for(name: str) -> str | None:
    """'image' | 'video' | 'audio' for supported files, else None."""
    ext = extension_of(name)
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    return None


def guess_mime(name: str) -> str | None:
    mime, _ = mimetypes.guess_type(name)
    return mime
