"""Versioned API router.

Endpoint surface is designed up front so the web UI and the future Premiere
panel share one stable contract. Assets are addressed by UUID, never by path.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from framefound import __version__
from framefound.api.v1.assets import router as assets_router
from framefound.api.v1.basemaps import router as basemaps_router
from framefound.api.v1.chat import router as chat_router
from framefound.api.v1.clip_looks import router as clip_looks_router
from framefound.api.v1.cuts import router as cuts_router
from framefound.api.v1.develop import router as develop_router
from framefound.api.v1.duplicates import router as duplicates_router
from framefound.api.v1.gdrive import router as gdrive_router
from framefound.api.v1.libraries import router as libraries_router
from framefound.api.v1.listing_suggestions import router as listing_suggestions_router
from framefound.api.v1.listings import router as listings_router
from framefound.api.v1.local_models import router as local_models_router
from framefound.api.v1.local_models_compare import router as local_models_compare_router
from framefound.api.v1.looks import router as looks_router
from framefound.api.v1.media import router as media_router
from framefound.api.v1.panel import router as panel_router
from framefound.api.v1.people import router as people_router
from framefound.api.v1.places import router as places_router
from framefound.api.v1.remote_access import router as remote_access_router
from framefound.api.v1.search import router as search_router
from framefound.api.v1.selects import router as selects_router
from framefound.api.v1.showcase import router as showcase_router
from framefound.api.v1.slideshows import router as slideshows_router
from framefound.api.v1.storage import router as storage_router
from framefound.api.v1.system import router as system_router
from framefound.api.v1.tags import router as tags_router
from framefound.auth.router import router as auth_router

api_v1 = APIRouter()
api_v1.include_router(auth_router)
api_v1.include_router(system_router)
api_v1.include_router(libraries_router)
api_v1.include_router(assets_router)
api_v1.include_router(media_router)
api_v1.include_router(search_router)
api_v1.include_router(selects_router)
api_v1.include_router(remote_access_router)
api_v1.include_router(storage_router)
api_v1.include_router(duplicates_router)
api_v1.include_router(places_router)
api_v1.include_router(basemaps_router)
api_v1.include_router(people_router)
api_v1.include_router(tags_router)
api_v1.include_router(slideshows_router)
api_v1.include_router(listings_router)
api_v1.include_router(develop_router)
api_v1.include_router(gdrive_router)
api_v1.include_router(panel_router)
api_v1.include_router(showcase_router)
api_v1.include_router(chat_router)
api_v1.include_router(local_models_router)
api_v1.include_router(local_models_compare_router)
api_v1.include_router(clip_looks_router)
api_v1.include_router(listing_suggestions_router)
api_v1.include_router(cuts_router)
api_v1.include_router(looks_router)


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
# TODO(m3): /assets/{id}/thumbnail · /assets/{id}/proxy (signed URLs)
# TODO(m5): GET /search (hybrid: q, filters, quoted-exact) · /assets/{id}/similar
# TODO(m5): GET /assets/{id}/scenes
# TODO(m6): CRUD /collections, /saved-searches · GET /jobs, POST /jobs/{id}/retry
# TODO(m9): GET /assets/{id}/paths (workstation path-mapping profiles)
# ---------------------------------------------------------------------------
