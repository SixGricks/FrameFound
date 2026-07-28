"""Versioned API router.

Endpoint surface is designed up front so the web UI and the future Premiere
panel share one stable contract. Assets are addressed by UUID, never by path.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from framefound import __version__
from framefound.api.v1.system import router as system_router
from framefound.auth.router import router as auth_router

api_v1 = APIRouter()
api_v1.include_router(auth_router)
api_v1.include_router(system_router)


class SystemInfo(BaseModel):
    name: str
    version: str
    status: str


@api_v1.get("/system/info", response_model=SystemInfo, tags=["system"])
async def system_info() -> SystemInfo:
    """Unauthenticated build info (no secrets, no configuration)."""
    return SystemInfo(name="FrameFound", version=__version__, status="milestone-1")


# ---------------------------------------------------------------------------
# Planned surface (implemented milestone by milestone; kept here as the
# contract of record — see docs/architecture.md):
#
# TODO(m2): CRUD /libraries · GET /libraries/{id}/scan-status · POST /libraries/{id}/scan
# TODO(m3): GET /assets/{id} · /assets/{id}/thumbnail · /assets/{id}/proxy (signed URLs)
# TODO(m4): GET /assets/{id}/transcript · /assets/{id}/subtitles
# TODO(m5): GET /search (hybrid: q, filters, quoted-exact) · /assets/{id}/similar
# TODO(m5): GET /assets/{id}/scenes
# TODO(m6): CRUD /collections, /saved-searches · GET /jobs, POST /jobs/{id}/retry
# TODO(m9): GET /assets/{id}/paths (workstation path-mapping profiles)
# ---------------------------------------------------------------------------
