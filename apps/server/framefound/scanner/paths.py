"""Path validation against the media-root allowlist.

Threat model: path traversal / arbitrary file read. Every library root must
resolve inside the configured media root; every relative path must resolve
inside its library root. Symlinks are resolved before checking, so a link
pointing outside the allowlist is rejected.
"""

from pathlib import Path


class PathValidationError(ValueError):
    """Raised when a path escapes the allowlist. Message is safe to display."""


def validate_library_root(candidate: str, media_root: Path) -> Path:
    path = Path(candidate)
    if not path.is_absolute():
        raise PathValidationError("Library path must be absolute")
    try:
        resolved = path.resolve(strict=True)
        allowed = media_root.resolve(strict=True)
    except OSError as exc:
        raise PathValidationError("Library path does not exist or is not accessible") from exc
    if not resolved.is_dir():
        raise PathValidationError("Library path must be a folder")
    if resolved != allowed and allowed not in resolved.parents:
        raise PathValidationError("Library path must be inside the configured media root")
    return resolved


def safe_join(root: Path, relative: str) -> Path:
    """Join a stored relative path to its library root, refusing escapes."""
    joined = (root / relative).resolve()
    resolved_root = root.resolve()
    if joined != resolved_root and resolved_root not in joined.parents:
        raise PathValidationError("Path escapes the library root")
    return joined
