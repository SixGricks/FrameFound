"""The surface editing panels talk to.

Deliberately its own module rather than reusing `/search` and `/assets`. A
panel is a client that cannot be redeployed as easily as the web UI — it lives
inside somebody's Premiere install — so the contract it depends on should be
small, explicit, and changed only on purpose. Everything here is designed to
survive the web UI being rewritten.

Two things a panel needs that a browser never does:

**A path it can actually open.** The catalogue stores `/media/gelco/...`,
which is meaningless on a Windows edit bay where the same share is ``Z:\\``.
`PathMapping` has held the per-workstation profiles since Milestone 2 and
nothing consumed them; this is what they were for.

**Its own credential.** Panels authenticate with a scoped, revocable token
rather than the operator's session — see `auth/panel_tokens.py`.
"""

import uuid
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from framefound.auth.deps import DbDep, PanelPrincipal, require_panel_scope
from framefound.db.models import Asset, Library, PathMapping

log = structlog.get_logger()

router = APIRouter(prefix="/panel", tags=["panel"])

# A panel's result list is a scrolling strip, not a research tool. Keeping this
# modest keeps the round trip fast over a home connection.
MAX_RESULTS = 60


class PanelAsset(BaseModel):
    asset_id: uuid.UUID
    filename: str
    media_type: str
    duration_s: float | None
    width: int | None
    height: int | None
    captured_at: str | None
    # Where the panel should look for the file, already translated for the
    # workstation's profile. Null when no profile matches — the panel says so
    # rather than handing Premiere a path that cannot exist.
    path: str | None
    # Always available regardless of mapping: stream it from the server.
    proxy_url: str
    thumbnail_url: str


class PanelSearchResponse(BaseModel):
    query: str
    profile: str | None
    results: list[PanelAsset]
    note: str


class PathProfile(BaseModel):
    profile_name: str
    platform: str
    library_id: uuid.UUID
    library_name: str
    server_prefix: str
    mapped_prefix: str


def _translate(server_path: str, root: str, mapped_prefix: str) -> str:
    """Rewrite a catalogue path for a workstation.

    Windows separators are applied when the mapped prefix looks like a Windows
    path, because a profile pointing at `Z:\\Intel` and a path containing
    forward slashes is a combination Premiere will accept and then fail to
    resolve, which is a much more confusing failure than a wrong drive letter.
    """
    if not server_path.startswith(root):
        # Not under this library's root, so there is nothing to rewrite it
        # against. Joining it to the profile anyway would invent a location —
        # better to hand back something obviously wrong than something
        # plausibly wrong that an editor spends an afternoon chasing.
        return server_path

    relative = server_path[len(root) :].lstrip("/")
    windows = ":" in mapped_prefix[:3] or mapped_prefix.startswith("\\\\")
    separator = "\\" if windows else "/"
    if windows:
        relative = relative.replace("/", "\\")
    return mapped_prefix.rstrip("/\\") + separator + relative


@router.get("/profiles", response_model=list[PathProfile])
async def list_profiles(_user: PanelPrincipal, db: DbDep) -> list[PathProfile]:
    """Path profiles the panel can choose from.

    Shown in the panel's settings so an editor picks their workstation once.
    Guessing from the User-Agent was considered and rejected: two editors on
    the same OS can mount the same share at different letters, and a silently
    wrong guess produces offline media in a sequence.
    """
    rows = (
        await db.execute(
            select(PathMapping, Library)
            .join(Library, Library.id == PathMapping.library_id)
            .order_by(PathMapping.profile_name)
        )
    ).all()
    return [
        PathProfile(
            profile_name=mapping.profile_name,
            platform=mapping.platform,
            library_id=library.id,
            library_name=library.name,
            server_prefix=library.root_path,
            mapped_prefix=mapping.mapped_prefix,
        )
        for mapping, library in rows
    ]


@router.get("/search", response_model=PanelSearchResponse)
async def panel_search(
    _user: PanelPrincipal,
    db: DbDep,
    q: str = Query(default="", max_length=300),
    profile: str = Query(default="", max_length=100),
    media_type: str = Query(default="", max_length=20),
    limit: int = Query(default=24, ge=1, le=MAX_RESULTS),
) -> PanelSearchResponse:
    """Search, with every result already carrying a workstation-usable path.

    Falls back to filename matching when semantic search is unavailable, so the
    panel still works on an install where embeddings have not been generated —
    an editor hunting for `A007_C012` by name is the common case anyway.
    """
    from framefound.api.v1.search import _visual_search

    term = q.strip()
    ordered: list[uuid.UUID] = []

    if term:
        # Visual hits first — "the wide of the barn" is what a panel is for.
        # Any failure in the embedding path is logged and stepped over: an
        # install with no embeddings yet must still be able to find A007_C012.
        try:
            hits, _ = await _visual_search(db, term, None, limit)
            ordered = [hit.asset_id for hit in hits]
        except Exception as exc:  # noqa: BLE001 - embeddings unavailable, model missing, ...
            log.info("panel.visual_search_unavailable", error=str(exc)[:200])

        by_name = (
            (
                await db.execute(
                    select(Asset.id)
                    .where(
                        Asset.filename.ilike(f"%{term}%") | Asset.relative_path.ilike(f"%{term}%")
                    )
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        seen = set(ordered)
        ordered += [aid for aid in by_name if aid not in seen]

    if ordered:
        found = (await db.execute(select(Asset).where(Asset.id.in_(ordered)))).scalars().all()
        rank = {aid: i for i, aid in enumerate(ordered)}
        results = sorted(found, key=lambda a: rank.get(a.id, len(ordered)))
    elif term:
        results = []
    else:
        # No query: the most recent material, which is what an editor opening
        # the panel mid-shoot actually wants.
        results = list(
            (
                await db.execute(
                    select(Asset).order_by(Asset.captured_at.desc().nullslast()).limit(limit)
                )
            )
            .scalars()
            .all()
        )

    if media_type:
        results = [a for a in results if a.media_type == media_type]

    mapping, library = await _profile(db, profile)
    entries = [
        PanelAsset(
            asset_id=asset.id,
            filename=asset.filename,
            media_type=asset.media_type,
            duration_s=asset.duration_s,
            width=asset.width,
            height=asset.height,
            captured_at=asset.captured_at.isoformat() if asset.captured_at else None,
            path=(
                _translate(
                    f"{library.root_path.rstrip('/')}/{asset.relative_path}",
                    library.root_path.rstrip("/"),
                    mapping.mapped_prefix,
                )
                if mapping and library and asset.library_id == library.id
                else None
            ),
            proxy_url=f"/api/v1/media/{asset.id}/proxy",
            thumbnail_url=f"/api/v1/media/{asset.id}/thumbnail",
        )
        for asset in results[:limit]
    ]

    if mapping is None:
        note = (
            "No path profile selected, so clips will be linked from the server "
            "rather than your local mount."
        )
    else:
        note = f"Paths translated for {mapping.profile_name}."

    return PanelSearchResponse(
        query=q, profile=mapping.profile_name if mapping else None, results=entries, note=note
    )


async def _profile(db: DbDep, name: str) -> tuple[Any, Any]:
    if not name.strip():
        return None, None
    row = (
        await db.execute(
            select(PathMapping, Library)
            .join(Library, Library.id == PathMapping.library_id)
            .where(PathMapping.profile_name == name.strip())
        )
    ).first()
    if row is None:
        return None, None
    return row[0], row[1]


@router.get("/assets/{asset_id}/paths")
async def asset_paths(asset_id: uuid.UUID, _user: PanelPrincipal, db: DbDep) -> dict[str, Any]:
    """Every known path for one asset, one per workstation profile.

    The endpoint ADR-0019 named. A panel calls this when the editor asks
    "where is this actually?" and it is also the honest answer when a profile
    is wrong: seeing all four mappings side by side is how somebody notices
    that the Windows profile points at the wrong drive letter.
    """
    asset = await db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="No such asset")
    library = await db.get(Library, asset.library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="That library no longer exists")

    root = library.root_path.rstrip("/")
    server_path = f"{root}/{asset.relative_path}"
    mappings = (
        (await db.execute(select(PathMapping).where(PathMapping.library_id == library.id)))
        .scalars()
        .all()
    )
    return {
        "asset_id": str(asset_id),
        "filename": asset.filename,
        "server_path": server_path,
        "paths": [
            {
                "profile_name": m.profile_name,
                "platform": m.platform,
                "path": _translate(server_path, root, m.mapped_prefix),
            }
            for m in mappings
        ],
        "proxy_url": f"/api/v1/media/{asset_id}/proxy",
    }


class ExportRequest(BaseModel):
    asset_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    profile: str = Field(default="", max_length=100)
    sequence_name: str = Field(default="FrameFound selection", max_length=120)


@router.post("/export/fcp7", dependencies=[require_panel_scope("export")])
async def export_fcp7(body: ExportRequest, _user: PanelPrincipal, db: DbDep) -> dict[str, Any]:
    """An FCP7 XML bin for the selected clips.

    The same export the web UI offers, reachable by the panel, and the reason
    the panel is worth building at all: a search result becomes a bin with the
    clips already in it, at paths the workstation can open.

    Requires the `export` scope. A read-only token can find footage and stream
    a proxy; producing a file the host application will act on is a step
    further and is opted into per token.
    """
    from framefound.nle.fcp7 import Clip, build_bin

    rows = (
        (
            await db.execute(
                select(Asset).where(Asset.id.in_(body.asset_ids), Asset.availability == "online")
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        raise HTTPException(status_code=400, detail="None of those clips are available")

    order = {aid: i for i, aid in enumerate(body.asset_ids)}
    rows = sorted(rows, key=lambda a: order.get(a.id, len(order)))

    libraries = {
        library.id: library for library in (await db.execute(select(Library))).scalars().all()
    }
    mapping, profile_library = await _profile(db, body.profile)

    clips: list[Clip] = []
    for asset in rows:
        library = libraries[asset.library_id]
        root = library.root_path.rstrip("/")
        server_path = f"{root}/{asset.relative_path}"
        # This is what the existing web export left as a TODO: the path written
        # into the XML is the *workstation's*, not the server's, whenever a
        # profile applies. An XML full of /media/gelco paths imports into
        # Premiere as offline media on every machine that is not this server.
        path = server_path
        if (
            mapping is not None
            and profile_library is not None
            and asset.library_id == profile_library.id
        ):
            path = _translate(server_path, root, mapping.mapped_prefix)
        clips.append(
            Clip(
                name=asset.filename,
                path=path,
                duration_s=asset.duration_s,
                fps=asset.fps,
                width=asset.width,
                height=asset.height,
                has_audio=asset.audio_codec is not None or asset.media_type == "audio",
            )
        )

    log.info("panel.exported", clips=len(clips), profile=body.profile or "none")
    return {
        "filename": f"{body.sequence_name}.xml",
        "xml": build_bin(body.sequence_name, clips),
        "clips": len(clips),
    }
