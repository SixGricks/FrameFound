"""Media serving: derivative bytes, authorized per request.

Auth model (threat model: unauthorized media access):
- A valid session cookie authorizes any derivative.
- OR a valid signed URL (exp + sig query params) authorizes exactly one
  (asset, kind) pair until expiry — used by <img>/<video> tags in other
  origins, share links, and the future Premiere panel.
Original files are NEVER served — only app-generated derivatives under the
data volume, resolved through the DB (no client-supplied paths).
"""

import uuid
from email.utils import formatdate
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select

from framefound.auth import service as auth_service
from framefound.auth.deps import SESSION_COOKIE, DbDep, SettingsDep
from framefound.db.models import Derivative
from framefound.media.signing import SigningError, verify_media_signature
from framefound.media.streaming import range_file_response

router = APIRouter(prefix="/media", tags=["media"])

_CONTENT_TYPES = {
    "webp": "image/webp",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "mp4": "video/mp4",
    "vtt": "text/vtt",
}


async def _authorize(
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    asset_id: uuid.UUID,
    kind: str,
) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token and await auth_service.resolve_session(db, token, settings) is not None:
        if db.info.pop(auth_service.SESSION_TOUCHED, False):
            await db.commit()
        return
    exp = request.query_params.get("exp")
    sig = request.query_params.get("sig")
    if exp and sig:
        try:
            verify_media_signature(settings.secret_key, asset_id, kind, int(exp), sig)
            return
        except (SigningError, ValueError) as err:
            raise HTTPException(403, "This media link is invalid or has expired") from err
    raise HTTPException(401, "Sign in to view media")


@router.get("/{asset_id}/frame")
async def serve_frame(
    asset_id: uuid.UUID,
    ts: int,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
) -> Response:
    """One sampled frame image, addressed by its timestamp."""
    from framefound.db.models import Frame

    await _authorize(request, db, settings, asset_id, "frame")
    # Only the path: the full row carries the frame's 512-float embedding,
    # parsed from text for every strip image and never looked at.
    relative_path = (
        await db.execute(
            select(Frame.relative_path).where(Frame.asset_id == asset_id, Frame.ts_ms == ts)
        )
    ).scalar_one_or_none()
    if relative_path is None:
        raise HTTPException(404, "No frame at that timestamp")
    path = settings.data_dir / relative_path
    if not path.is_file():
        raise HTTPException(404, "Frame image is missing; it can be regenerated")
    return _cacheable(request, path, "image/jpeg")


@router.get("/{asset_id}/{kind}")
async def serve_derivative(
    asset_id: uuid.UUID,
    kind: str,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
) -> Response:
    await _authorize(request, db, settings, asset_id, kind)
    derivative = (
        await db.execute(
            select(Derivative).where(
                Derivative.asset_id == asset_id,
                Derivative.kind == kind,
                Derivative.status == "ready",
            )
        )
    ).scalar_one_or_none()
    if derivative is None:
        raise HTTPException(404, "No such media is available for this item")

    path = settings.data_dir / derivative.relative_path
    if not path.is_file():
        raise HTTPException(404, "Media file is missing; it can be regenerated")
    content_type = _CONTENT_TYPES.get(derivative.media_format, "application/octet-stream")
    return _cacheable(request, path, content_type)


# A day: thumbnails change only when regenerated, and a regenerated file has
# a new size or mtime, so the validator below changes with it.
CACHE_CONTROL = "private, max-age=86400"


def _cacheable(request: Request, path: Path, content_type: str) -> Response:
    """Serve a derivative with validators, answering 304 when the browser
    already has it. Without them every thumbnail was re-downloaded in full
    once the hour's max-age ran out — a Browse page of fifty tiles, again."""
    st = path.stat()
    etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": CACHE_CONTROL})
    response = range_file_response(request, path, content_type)
    response.headers["ETag"] = etag
    response.headers["Last-Modified"] = formatdate(st.st_mtime, usegmt=True)
    response.headers["Cache-Control"] = CACHE_CONTROL
    return response
