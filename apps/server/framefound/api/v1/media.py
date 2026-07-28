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
    response = range_file_response(request, path, content_type)
    response.headers["Cache-Control"] = "private, max-age=3600"
    return response
