"""The Google Drive organizer: propose, approve, apply, undo.

Nothing here writes without being shown first. Preview computes the full
rename plan and returns it; apply executes exactly the renames the client
sends back (which are the previewed ones, edited or not); a manifest written
into the folder records old→new so undo is one call. Files the classifier
cannot place are left untouched and reported, never guessed at.

Drive is the only external party, the service account only sees folders the
operator shared with it, and the catalogue-first classifier means a shoot
that lives on the NAS is organized without a single pixel leaving the
machine — the Drive traffic is metadata: list, rename, one small JSON.
"""

import asyncio
import datetime as dt
import tempfile
from pathlib import Path
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from framefound.ai import rooms as rooms_lib
from framefound.ai.embeddings import (
    EmbeddingProvider,
    EmbeddingUnavailable,
    get_embedding_provider,
)
from framefound.auth.deps import CurrentUser, DbDep, require_admin
from framefound.db.models import AuditLog
from framefound.integrations import gdrive as gdrive_lib
from framefound.integrations import organize as organize_lib
from framefound.media.maps_store import GdriveConfig, load_gdrive_config, save_gdrive_config

log = structlog.get_logger()

router = APIRouter(prefix="/gdrive", tags=["gdrive"])

MAX_FILES = 500
MAX_NAME_LEN = 200

# Swapped by tests for an httpx.MockTransport; None means real HTTP.
_transport: httpx.BaseTransport | None = None


def _client(config: GdriveConfig) -> gdrive_lib.GdriveClient:
    return gdrive_lib.GdriveClient(config.service_account(), transport=_transport)


async def _ready_config(db: DbDep) -> GdriveConfig:
    config = await load_gdrive_config(db)
    if not config.ready:
        raise HTTPException(
            503,
            "Google Drive is not configured — add a service account key on the "
            "Security page, then share the folder with its email address",
        )
    return config


class GdriveSettingsOut(BaseModel):
    configured: bool
    enabled: bool
    # The address the operator shares folders with. An identifier, not a secret.
    client_email: str


class GdriveSettingsIn(BaseModel):
    # None = leave stored key alone; "" = clear it; text = replace it.
    service_account_json: str | None = Field(default=None, max_length=10_000)
    enabled: bool | None = None


@router.get("/settings", response_model=GdriveSettingsOut)
async def gdrive_settings(_user: CurrentUser, db: DbDep) -> GdriveSettingsOut:
    config = await load_gdrive_config(db)
    return GdriveSettingsOut(
        configured=bool(config.service_account_sealed),
        enabled=config.enabled,
        client_email=config.client_email,
    )


@router.put("/settings", response_model=GdriveSettingsOut, dependencies=[require_admin])
async def update_gdrive_settings(
    body: GdriveSettingsIn, _user: CurrentUser, db: DbDep
) -> GdriveSettingsOut:
    config = await load_gdrive_config(db)
    if body.service_account_json is not None:
        try:
            config.with_service_account(body.service_account_json)
        except ValueError as err:
            raise HTTPException(400, str(err)) from err
    if body.enabled is not None:
        config.enabled = body.enabled
    await save_gdrive_config(db, config)
    log.info("gdrive.settings_updated", configured=bool(config.service_account_sealed))
    return GdriveSettingsOut(
        configured=bool(config.service_account_sealed),
        enabled=config.enabled,
        client_email=config.client_email,
    )


class FolderIn(BaseModel):
    folder: str = Field(min_length=1, max_length=500, description="Drive folder URL or id")


class RenameOut(BaseModel):
    file_id: str
    old_name: str
    new_name: str
    room: str
    room_label: str
    score: float
    source: str  # catalogue | thumbnail


class SkippedOut(BaseModel):
    file_id: str
    name: str
    reason: str


class PreviewOut(BaseModel):
    folder_id: str
    folder_name: str
    total: int
    renames: list[RenameOut]
    skipped: list[SkippedOut]
    from_catalogue: int
    from_thumbnail: int


def _classify_folder(
    client: gdrive_lib.GdriveClient,
    files: list[dict[str, Any]],
    matched: dict[str, list[float] | None],
    vectors: list[list[float]],
) -> tuple[list[dict[str, Any]], list[SkippedOut]]:
    """The synchronous half of preview: thumbnail fallbacks and scoring.

    Runs in a worker thread — Drive fetches and ONNX inference both block.
    """
    provider: EmbeddingProvider | None = None
    classified: list[dict[str, Any]] = []
    skipped: list[SkippedOut] = []
    for file in files:
        stem, ext = organize_lib.original_stem(str(file["name"]))
        embedding = matched.get(file["id"])
        source = "catalogue"
        if embedding is None:
            source = "thumbnail"
            link = file.get("thumbnailLink")
            if not link:
                skipped.append(
                    SkippedOut(file_id=file["id"], name=file["name"], reason="no thumbnail")
                )
                continue
            try:
                if provider is None:
                    provider = get_embedding_provider()
                data = client.thumbnail(str(link))
                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as handle:
                    handle.write(data)
                    temp_path = Path(handle.name)
                try:
                    embedding = provider.embed_image(temp_path).vector
                finally:
                    temp_path.unlink(missing_ok=True)
            except (gdrive_lib.GdriveError, EmbeddingUnavailable) as err:
                skipped.append(SkippedOut(file_id=file["id"], name=file["name"], reason=str(err)))
                continue
        room, score = rooms_lib.classify(embedding, vectors)
        if not room:
            skipped.append(
                SkippedOut(file_id=file["id"], name=file["name"], reason="no confident room")
            )
            continue
        classified.append(
            {
                "file_id": file["id"],
                "old_name": file["name"],
                "stem": stem,
                "ext": ext,
                "room": room,
                "score": score,
                "source": source,
            }
        )
    return classified, skipped


@router.post("/organize/preview", response_model=PreviewOut)
async def organize_preview(body: FolderIn, _user: CurrentUser, db: DbDep) -> PreviewOut:
    config = await _ready_config(db)
    try:
        folder_id = gdrive_lib.parse_folder_id(body.folder)
    except gdrive_lib.GdriveError as err:
        raise HTTPException(400, str(err)) from err
    try:
        vectors = await asyncio.to_thread(rooms_lib.room_vectors)
    except EmbeddingUnavailable as err:
        raise HTTPException(503, "The AI runtime is not available on this host") from err

    client = _client(config)
    try:
        folder_name = await asyncio.to_thread(client.folder_name, folder_id)
        files = await asyncio.to_thread(client.list_images, folder_id)
        if len(files) > MAX_FILES:
            raise HTTPException(400, f"That folder has more than {MAX_FILES} images")

        # Catalogue matches happen here on the event loop — they are DB reads.
        tokens = organize_lib.folder_tokens(folder_name)
        matched: dict[str, list[float] | None] = {}
        for file in files:
            stem, _ = organize_lib.original_stem(str(file["name"]))
            matched[file["id"]] = await organize_lib.catalogue_embedding(db, stem, tokens)

        classified, skipped = await asyncio.to_thread(
            _classify_folder, client, files, matched, vectors
        )
    except gdrive_lib.GdriveError as err:
        raise HTTPException(502, str(err)) from err
    finally:
        client.close()

    classified.sort(key=lambda c: rooms_lib.canonical_sort_key(str(c["room"]), float(c["score"])))
    renames = [
        RenameOut(
            file_id=entry["file_id"],
            old_name=entry["old_name"],
            new_name=organize_lib.proposed_name(
                index, rooms_lib.ROOM_LABELS[entry["room"]], entry["stem"], entry["ext"]
            ),
            room=entry["room"],
            room_label=rooms_lib.ROOM_LABELS[entry["room"]],
            score=round(float(entry["score"]), 3),
            source=entry["source"],
        )
        for index, entry in enumerate(classified, 1)
    ]
    log.info(
        "gdrive.preview",
        folder=folder_id,
        total=len(files),
        renamed=len(renames),
        skipped=len(skipped),
    )
    return PreviewOut(
        folder_id=folder_id,
        folder_name=folder_name,
        total=len(files),
        renames=renames,
        skipped=skipped,
        from_catalogue=sum(1 for r in renames if r.source == "catalogue"),
        from_thumbnail=sum(1 for r in renames if r.source == "thumbnail"),
    )


class ApplyRenameIn(BaseModel):
    file_id: str = Field(pattern=r"^[A-Za-z0-9_-]{5,100}$")
    old_name: str = Field(min_length=1, max_length=MAX_NAME_LEN)
    new_name: str = Field(min_length=1, max_length=MAX_NAME_LEN)


class ApplyIn(BaseModel):
    folder: str = Field(min_length=1, max_length=500)
    renames: list[ApplyRenameIn] = Field(min_length=1, max_length=MAX_FILES)


class ApplyOut(BaseModel):
    renamed: int
    failed: list[SkippedOut]
    manifest_file_id: str | None


def _apply_renames(
    client: gdrive_lib.GdriveClient,
    folder_id: str,
    renames: list[ApplyRenameIn],
) -> tuple[list[ApplyRenameIn], list[SkippedOut], str | None]:
    done: list[ApplyRenameIn] = []
    failed: list[SkippedOut] = []
    for entry in renames:
        try:
            client.rename(entry.file_id, entry.new_name)
            done.append(entry)
        except gdrive_lib.GdriveError as err:
            failed.append(SkippedOut(file_id=entry.file_id, name=entry.old_name, reason=str(err)))
    manifest_id: str | None = None
    if done:
        # One manifest per folder: the newest organize owns undo. Written
        # before the old one is removed — the other order had a window in
        # which a failed upload left renames applied and no undo at all.
        try:
            existing = client.find_file(folder_id, gdrive_lib.MANIFEST_NAME)
            manifest_id = client.upload_json(
                folder_id,
                gdrive_lib.MANIFEST_NAME,
                {
                    "tool": "framefound",
                    "created_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                    "folder_id": folder_id,
                    "renames": [
                        {"file_id": e.file_id, "old_name": e.old_name, "new_name": e.new_name}
                        for e in done
                    ],
                },
            )
        except gdrive_lib.GdriveError as err:
            log.warning("gdrive.manifest_failed", folder=folder_id, error=str(err))
        else:
            if existing:
                try:
                    client.delete(str(existing["id"]))
                except gdrive_lib.GdriveError as err:
                    # Harmless: undo reads the newest manifest by creation time.
                    log.warning("gdrive.old_manifest_kept", folder=folder_id, error=str(err))
    return done, failed, manifest_id


@router.post("/organize/apply", response_model=ApplyOut, dependencies=[require_admin])
async def organize_apply(body: ApplyIn, user: CurrentUser, db: DbDep) -> ApplyOut:
    config = await _ready_config(db)
    try:
        folder_id = gdrive_lib.parse_folder_id(body.folder)
    except gdrive_lib.GdriveError as err:
        raise HTTPException(400, str(err)) from err
    for entry in body.renames:
        if any(ch in entry.new_name for ch in "/\\") or entry.new_name.startswith("."):
            raise HTTPException(400, f"Refusing rename to {entry.new_name!r}")

    client = _client(config)
    try:
        done, failed, manifest_id = await asyncio.to_thread(
            _apply_renames, client, folder_id, body.renames
        )
    finally:
        client.close()

    db.add(
        AuditLog(
            event="gdrive.organized",
            actor_user_id=user.id,
            detail={"folder_id": folder_id, "renamed": len(done), "failed": len(failed)},
        )
    )
    await db.commit()
    log.info("gdrive.applied", folder=folder_id, renamed=len(done), failed=len(failed))
    return ApplyOut(renamed=len(done), failed=failed, manifest_file_id=manifest_id)


class UndoOut(BaseModel):
    restored: int
    failed: list[SkippedOut]


def _undo_from_manifest(
    client: gdrive_lib.GdriveClient, folder_id: str
) -> tuple[int, list[SkippedOut]]:
    existing = client.find_file(folder_id, gdrive_lib.MANIFEST_NAME)
    if not existing:
        raise gdrive_lib.GdriveError("No FrameFound manifest in that folder — nothing to undo")
    manifest = client.download_json(str(existing["id"]))
    restored = 0
    failed: list[SkippedOut] = []
    for entry in manifest.get("renames", []):
        try:
            client.rename(str(entry["file_id"]), str(entry["old_name"]))
            restored += 1
        except (gdrive_lib.GdriveError, KeyError) as err:
            failed.append(
                SkippedOut(
                    file_id=str(entry.get("file_id", "")),
                    name=str(entry.get("new_name", "")),
                    reason=str(err),
                )
            )
    if not failed:
        client.delete(str(existing["id"]))
    return restored, failed


@router.post("/organize/undo", response_model=UndoOut, dependencies=[require_admin])
async def organize_undo(body: FolderIn, user: CurrentUser, db: DbDep) -> UndoOut:
    config = await _ready_config(db)
    try:
        folder_id = gdrive_lib.parse_folder_id(body.folder)
    except gdrive_lib.GdriveError as err:
        raise HTTPException(400, str(err)) from err

    client = _client(config)
    try:
        restored, failed = await asyncio.to_thread(_undo_from_manifest, client, folder_id)
    except gdrive_lib.GdriveError as err:
        raise HTTPException(404 if "manifest" in str(err) else 502, str(err)) from err
    finally:
        client.close()

    db.add(
        AuditLog(
            event="gdrive.undone",
            actor_user_id=user.id,
            detail={"folder_id": folder_id, "restored": restored, "failed": len(failed)},
        )
    )
    await db.commit()
    log.info("gdrive.undone", folder=folder_id, restored=restored, failed=len(failed))
    return UndoOut(restored=restored, failed=failed)
