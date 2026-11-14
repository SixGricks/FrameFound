"""Milestone 1 tables: users, auth sessions, app settings, audit log.

Types are kept portable (generic Uuid/JSON, non-native enums as strings) so
the suite runs against SQLite in unit tests while production uses PostgreSQL.
Media tables (libraries, assets, ...) land in Milestone 2 — see docs/data-model.md.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.vector_type import Embedding


def _worker_name() -> str | None:
    """The `jobs.worker` default: this process's machine, NULL on the server."""
    return get_settings().worker_name.strip()[:100] or None


Role = String(20)  # admin | user | readonly — validated in the service layer
ROLES = ("admin", "user", "readonly")


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(Role, default="user")
    # Sealed with the app secret (auth/crypto.py) — a DB dump alone is not
    # enough to mint valid codes. Pending until confirmed by a live code.
    totp_secret: Mapped[str | None] = mapped_column(String(255), default=None)
    totp_pending_secret: Mapped[str | None] = mapped_column(String(255), default=None)
    totp_recovery_hashes: Mapped[list[str]] = mapped_column(JSON, default=list)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    sessions: Mapped[list["AuthSession"]] = relationship(back_populates="user")


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # Only a hash of the opaque token is stored; a DB leak exposes no sessions.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # sliding
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # hard cap
    ip: Mapped[str | None] = mapped_column(String(45), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(255), default=None)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped[User] = relationship(back_populates="sessions")


class PanelToken(Base):
    """A credential for an editing panel — Premiere, Lightroom, a script.

    Separate from `auth_sessions` on purpose, even though both are opaque
    bearer strings. A session belongs to a browser, slides its expiry on use
    and carries the user's full authority. A panel token belongs to a *machine*,
    never expires by inactivity (an editor may not open the panel for a month),
    and is deliberately weaker than the user who made it: read-only unless
    explicitly widened.

    The panel is the reason this exists rather than reusing the session cookie.
    A cookie in a desktop extension cannot be revoked from the machine it grants
    access to, cannot be told apart from the operator's own browser in an audit,
    and rides along on every request the host application happens to make. A
    named, listed, revocable token is the same trust decision made visible.

    Only the hash is stored, as with sessions. `prefix` is the visible part, so
    a list of four tokens can be told apart without revealing any of them.
    """

    __tablename__ = "panel_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # Operator-supplied: "Edit bay iMac", "DJ's laptop".
    name: Mapped[str] = mapped_column(String(120), default="")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # First few characters of the token, for identification in a list.
    prefix: Mapped[str] = mapped_column(String(16), default="")
    # premiere | lightroom | other — informational, never a permission.
    host: Mapped[str] = mapped_column(String(20), default="other")
    # Comma-separated. "read" is search and metadata; "export" additionally
    # allows generating an FCP7 XML. Nothing here grants a write to the library.
    scopes: Mapped[str] = mapped_column(String(200), default="read")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Null means it does not expire. An editing workstation that is used twice a
    # year should not silently stop working, so this is opt-in rather than
    # imposed — but it is offered, and shown in the list.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_used_ip: Mapped[str | None] = mapped_column(String(45), default=None)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped[User] = relationship()


class AppSetting(Base):
    """Typed key-value store for wizard-managed configuration."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    event: Mapped[str] = mapped_column(String(100), index=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    ip: Mapped[str | None] = mapped_column(String(45), default=None)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


# --------------------------------------------------------------------------
# Media catalog (Milestone 2)
# --------------------------------------------------------------------------

AVAILABILITY = ("online", "missing", "unmounted")
SCAN_STATUSES = ("pending", "running", "paused", "completed", "failed", "cancelled")


class Library(Base):
    """A watched media folder. `root_path` is validated against the media-root
    allowlist at creation time and never accepted from non-admins."""

    __tablename__ = "libraries"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    root_path: Mapped[str] = mapped_column(String(1024))
    read_only: Mapped[bool] = mapped_column(Boolean, default=True)
    # None = all supported extensions; otherwise a lowercase allowlist.
    include_extensions: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    exclude_globs: Mapped[list[str]] = mapped_column(JSON, default=list)
    scan_interval_minutes: Mapped[int | None] = mapped_column(default=None)  # None = manual
    watcher_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Processing profile v1 (per-library toggles; full profiles come later).
    generate_proxies: Mapped[bool] = mapped_column(Boolean, default=True)
    proxy_resolution: Mapped[int] = mapped_column(default=1080)
    transcribe_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Where an editing application writes its proxies, relative to the
    # original's folder: Premiere's ingest writes `Proxies/<stem>_Proxy.<ext>`.
    # A file matching it beside an original is that original's proxy, not a
    # second asset (scanner/proxies.py, migration 0029). Empty turns pairing off.
    proxy_pattern: Mapped[str] = mapped_column(
        String(200), default="Proxies/{stem}_Proxy", server_default="Proxies/{stem}_Proxy"
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Have the Looker (framefound/looking) look at what arrives in this
    # library. Off unless an administrator turns it on: folders are reviewed
    # when somebody asks. `look_since` is the moment it went on, and nothing
    # older is swept up by it — switching it on must not quietly start a
    # review of ten years of footage (migration 0034).
    look_at_ingest: Mapped[bool] = mapped_column(Boolean, default=False)
    look_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    assets: Mapped[list["Asset"]] = relationship(back_populates="library")
    path_mappings: Mapped[list["PathMapping"]] = relationship(
        back_populates="library", cascade="all, delete-orphan"
    )


class PathMapping(Base):
    """Workstation path profile: /media/intel -> Z:\\Intel (per-client mapping)."""

    __tablename__ = "path_mappings"
    __table_args__ = (UniqueConstraint("library_id", "profile_name"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("libraries.id", ondelete="CASCADE"))
    profile_name: Mapped[str] = mapped_column(String(100))
    platform: Mapped[str] = mapped_column(String(20))  # windows | macos | linux
    mapped_prefix: Mapped[str] = mapped_column(String(1024))

    library: Mapped[Library] = relationship(back_populates="path_mappings")


class Asset(Base):
    """One original media file, addressed by UUID. The path locates it; the
    hashes identify it (ADR-0010). Originals are never modified."""

    __tablename__ = "assets"
    __table_args__ = (UniqueConstraint("library_id", "relative_path"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("libraries.id", ondelete="CASCADE"))
    relative_path: Mapped[str] = mapped_column(String(2048))
    filename: Mapped[str] = mapped_column(String(512), index=True)
    extension: Mapped[str] = mapped_column(String(20))
    mime_type: Mapped[str | None] = mapped_column(String(100), default=None)
    media_type: Mapped[str] = mapped_column(String(10), index=True)  # image | video | audio

    size_bytes: Mapped[int] = mapped_column(BigInteger)
    mtime: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    partial_hash: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    availability: Mapped[str] = mapped_column(String(20), default="online")
    processing_status: Mapped[str] = mapped_column(String(30), default="pending")
    # Indexed: Browse's default order is newest first, a top-N over the
    # whole table on every page without it (migration 0024).
    first_indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None, index=True
    )

    # Technical metadata (filled by the metadata stage; nullable until then).
    duration_s: Mapped[float | None] = mapped_column(Float, default=None)
    width: Mapped[int | None] = mapped_column(default=None)
    height: Mapped[int | None] = mapped_column(default=None)
    fps: Mapped[float | None] = mapped_column(Float, default=None)
    video_codec: Mapped[str | None] = mapped_column(String(50), default=None)
    audio_codec: Mapped[str | None] = mapped_column(String(50), default=None)
    sample_rate: Mapped[int | None] = mapped_column(default=None)
    channels: Mapped[int | None] = mapped_column(default=None)
    bitrate: Mapped[int | None] = mapped_column(BigInteger, default=None)
    orientation: Mapped[int | None] = mapped_column(default=None)

    # Capture metadata.
    camera_make: Mapped[str | None] = mapped_column(String(100), default=None)
    camera_model: Mapped[str | None] = mapped_column(String(100), default=None, index=True)
    lens: Mapped[str | None] = mapped_column(String(200), default=None)
    focal_length_mm: Mapped[float | None] = mapped_column(Float, default=None)
    aperture_f: Mapped[float | None] = mapped_column(Float, default=None)
    shutter_speed: Mapped[str | None] = mapped_column(String(20), default=None)
    iso: Mapped[int | None] = mapped_column(default=None)
    gps_lat: Mapped[float | None] = mapped_column(Float, default=None, index=True)
    gps_lon: Mapped[float | None] = mapped_column(Float, default=None, index=True)
    # exif | inferred | manual. Inference borrows a position from a located
    # asset shot at the same time and place (framefound/media/geo.py); it is
    # always labelled so it can never masquerade as camera-recorded truth.
    gps_source: Mapped[str | None] = mapped_column(String(20), default=None)
    gps_confidence: Mapped[float | None] = mapped_column(Float, default=None)
    gps_inferred_from: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    captured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None, index=True
    )

    # Curation.
    rating: Mapped[int | None] = mapped_column(default=None)
    favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    title: Mapped[str | None] = mapped_column(String(500), default=None)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    custom_fields: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # Set on an editing proxy (Premiere's `Proxies/<stem>_Proxy.mp4`) whose
    # original is catalogued: the row records the pairing, so the original can
    # be decoded from the smaller file, and is otherwise out of the catalogue —
    # not searched, tagged, transcribed or embedded. Cleared when the original
    # goes, because then the proxy is the only copy (scanner/proxies.py).
    proxy_of_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"), default=None, index=True
    )

    # What a local model made of it (framefound/looking), rolled up so
    # Browse, search and culling can sort by it without a join per row. For
    # a photograph it is its look; for a clip, its best-scoring shot's. All
    # NULL until something has been looked at (migration 0034).
    # `look_score` NOT NULL means "a model has an opinion"; `looked_at` NOT
    # NULL means "nothing in it still needs looking at" — a clip with one
    # shot still to do has a score and no `looked_at`.
    look_score: Mapped[int | None] = mapped_column(SmallInteger, default=None)
    look_caption: Mapped[str | None] = mapped_column(String(300), default=None)
    look_reason: Mapped[str | None] = mapped_column(String(200), default=None)
    looked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None, index=True
    )
    # The key its looks share (`Look.key`: `<repo>@<revision>[/<variant>]#<prompt
    # version>`), or "mixed".
    looked_by: Mapped[str | None] = mapped_column(String(300), default=None)

    library: Mapped[Library] = relationship(back_populates="assets")


# processing_status of a paired proxy. The maintenance sweeps select on
# "pending" and "ready", so a paired proxy is never re-queued by them.
PAIRED_PROXY_STATUS = "paired_proxy"


def catalogued() -> Any:
    """The condition every catalogue listing applies: not a paired proxy."""
    return Asset.proxy_of_id.is_(None)


DERIVATIVE_KINDS = ("thumbnail", "preview", "poster", "proxy", "waveform", "motion")


class Job(Base):
    """Execution history for processing tasks (the dashboard's data source).

    Rows are written by the worker task shell — one per execution attempt.
    Live queue depths come from the broker, not this table."""

    __tablename__ = "jobs"
    # The maintenance sweeps ask "has this task run for this asset, and
    # when" for every candidate; one row per attempt, never pruned, so the
    # single-column indexes alone meant a scan of the task's rows each time.
    __table_args__ = (
        Index("ix_jobs_task_asset", "task_name", "asset_id"),
        Index("ix_jobs_task_started", "task_name", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    task_name: Mapped[str] = mapped_column(String(100), index=True)
    asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), default=None, index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="running", index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    error: Mapped[str | None] = mapped_column(String(500), default=None)
    # Which machine ran it (FRAMEFOUND_WORKER_NAME); NULL is the server's own
    # containers. Stamped by default so every place that writes a Job row
    # records it without having to remember to.
    worker: Mapped[str | None] = mapped_column(String(100), default=_worker_name)


class Derivative(Base):
    """A generated file for an asset (thumbnail/preview/poster/proxy/waveform).
    `relative_path` is relative to the app data volume — relocatable by design.
    Derivatives are disposable: rebuildable from originals at any time."""

    __tablename__ = "derivatives"
    __table_args__ = (UniqueConstraint("asset_id", "kind"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20), index=True)
    relative_path: Mapped[str] = mapped_column(String(1024))
    media_format: Mapped[str] = mapped_column(String(20))  # webp | jpeg | mp4 | png
    width: Mapped[int | None] = mapped_column(default=None)
    height: Mapped[int | None] = mapped_column(default=None)
    codec: Mapped[str | None] = mapped_column(String(50), default=None)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|ready|failed
    error: Mapped[str | None] = mapped_column(String(500), default=None)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, default=None)

    asset: Mapped[Asset] = relationship()


class Transcript(Base):
    """One transcript per asset (versioned on regeneration). Full text lives
    here; timestamped granularity lives in the segments."""

    __tablename__ = "transcripts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), unique=True
    )
    language: Mapped[str] = mapped_column(String(10))
    language_confidence: Mapped[float | None] = mapped_column(Float, default=None)
    model_name: Mapped[str] = mapped_column(String(100))
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    full_text: Mapped[str] = mapped_column(Text, default="")
    segment_count: Mapped[int] = mapped_column(default=0)
    version: Mapped[int] = mapped_column(default=1)

    segments: Mapped[list["TranscriptSegment"]] = relationship(
        back_populates="transcript",
        cascade="all, delete-orphan",
        order_by="TranscriptSegment.start_ms",
    )


class TranscriptSegment(Base):
    """The unit of 'jump to 00:02:17' search results."""

    __tablename__ = "transcript_segments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    transcript_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transcripts.id", ondelete="CASCADE"), index=True
    )
    start_ms: Mapped[int] = mapped_column(index=True)
    end_ms: Mapped[int] = mapped_column()
    text: Mapped[str] = mapped_column(Text)
    speaker: Mapped[str | None] = mapped_column(String(50), default=None)  # diarization later
    confidence: Mapped[float | None] = mapped_column(Float, default=None)

    transcript: Mapped[Transcript] = relationship(back_populates="segments")


class Frame(Base):
    """A sampled video frame (scene change or interval tick).

    Images get exactly one row at ts_ms=0, so visual search later queries a
    single table across stills and motion (docs/data-model.md §frames).
    `embedding` stays NULL until an embedding provider runs — the sampling
    stage is useful on its own for scene thumbnails and near-duplicate
    detection via the perceptual hash.
    """

    __tablename__ = "frames"
    __table_args__ = (UniqueConstraint("asset_id", "ts_ms"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), index=True
    )
    ts_ms: Mapped[int] = mapped_column(index=True)
    scene_number: Mapped[int | None] = mapped_column(default=None)
    is_scene_change: Mapped[bool] = mapped_column(Boolean, default=False)
    relative_path: Mapped[str] = mapped_column(String(1024))  # thumbnail under data dir
    phash: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    caption: Mapped[str | None] = mapped_column(Text, default=None)
    ocr_text: Mapped[str | None] = mapped_column(Text, default=None)
    embedding_model: Mapped[str | None] = mapped_column(String(100), default=None)
    # L2-normalised, so cosine distance and dot product agree.
    embedding: Mapped[list[float] | None] = mapped_column(Embedding(), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Scan(Base):
    """One scan/reconciliation run over a library, with live progress counters.
    The scanner service polls `status` between batches, so pause/resume/cancel
    from the API take effect mid-scan."""

    __tablename__ = "scans"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("libraries.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    files_seen: Mapped[int] = mapped_column(default=0)
    files_new: Mapped[int] = mapped_column(default=0)
    files_changed: Mapped[int] = mapped_column(default=0)
    files_moved: Mapped[int] = mapped_column(default=0)
    files_missing: Mapped[int] = mapped_column(default=0)
    files_deferred: Mapped[int] = mapped_column(default=0)  # unstable; next pass
    error: Mapped[str | None] = mapped_column(String(500), default=None)


class GeocodeCache(Base):
    """Reverse-geocoding results, keyed on a rounded coordinate.

    Persisted rather than held in memory because every lookup costs money and
    clusters are recomputed on every request. An empty `address` records a
    lookup that legitimately returned nothing, so it is not retried forever —
    see geocoding.FAILURE_RETRY_AFTER.
    """

    __tablename__ = "geocode_cache"

    cache_key: Mapped[str] = mapped_column(String(40), primary_key=True)
    address: Mapped[str] = mapped_column(String(300), default="")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


TAG_SOURCES = ("manual", "suggested", "confirmed", "rejected")


class Tag(Base):
    """A label the operator cares about, and what the system has learned it
    looks like.

    `prototype` is a CLIP vector: the blend of the tag's own words and the mean
    of the frames the operator tagged. `threshold` is derived per tag, because
    a fixed cutoff cannot work across subjects — see ai/tagging.py.
    """

    __tablename__ = "tags"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    slug: Mapped[str] = mapped_column(String(140), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Learned state. Null until the first learn pass runs.
    prototype: Mapped[list[float] | None] = mapped_column(Embedding(), default=None)
    threshold: Mapped[float | None] = mapped_column(default=None)
    threshold_reason: Mapped[str] = mapped_column(String(160), default="")
    example_count: Mapped[int] = mapped_column(default=0)
    learned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Suggestions can be turned off per tag without deleting what was learned.
    suggest_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    asset_links: Mapped[list["AssetTag"]] = relationship(
        back_populates="tag", cascade="all, delete-orphan"
    )


class AssetTag(Base):
    """One tag on one asset, and how it got there.

    `source` is the whole point. A manual tag is ground truth and teaches the
    prototype; a suggestion is a claim awaiting judgement; a rejection is
    negative evidence that must never be offered again. Collapsing these into
    a boolean would throw away everything the system learns from.
    """

    __tablename__ = "asset_tags"
    __table_args__ = (UniqueConstraint("asset_id", "tag_id", name="uq_asset_tag"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), index=True
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(16), default="manual", index=True)
    confidence: Mapped[float | None] = mapped_column(default=None)
    # Who proposed a suggestion: "clip" (the learner), "claude" (the API
    # reviewer), "mcp" (Claude through the connector), "local" (a model on
    # this Mac, framefound/looking). Null on rows older than migration 0025,
    # which were all the learner's. Informational — every suggestion is
    # judged by the operator the same way.
    origin: Mapped[str | None] = mapped_column(String(20), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    # The look that proposed it (origin "local"): which shot, which build,
    # what else the model said. That row is text and points at no model, so
    # removing a model never touches a tag. Indexed because deleting an
    # asset deletes its looks, and each must find the tags pointing at it.
    look_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("looks.id", ondelete="SET NULL"), default=None, index=True
    )
    # The moment in a clip the suggestion is about, so the Tags page opens there.
    at_ms: Mapped[int | None] = mapped_column(default=None)

    tag: Mapped["Tag"] = relationship(back_populates="asset_links")


class Person(Base):
    """A face cluster, named or not.

    Created unnamed by clustering, then named by the operator. `prototype` is
    the mean of this person's confirmed face embeddings — the same
    nearest-centroid approach as tags, for the same reason: it needs no
    training and improves with every correction.
    """

    __tablename__ = "people"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Blank until the operator names the cluster. The UI shows "Unnamed person"
    # rather than inventing something, because a wrong name is worse than none.
    name: Mapped[str] = mapped_column(String(120), default="")
    slug: Mapped[str] = mapped_column(String(140), default="", index=True)
    prototype: Mapped[list[float] | None] = mapped_column(Embedding(), default=None)
    # Derived from confirmed and rejected faces, per person — a fixed cutoff
    # cannot work across a bearded man and a clean-shaven one.
    threshold: Mapped[float] = mapped_column(Float, default=0.42)
    face_count: Mapped[int] = mapped_column(default=0)
    cover_face_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Face(Base):
    """One detected face in one frame.

    The crop is not stored: the frame is already on disk and the box is enough
    to render a thumbnail on demand. Keeping a second copy of everyone's face
    would double the most sensitive data in the system for no benefit.

    `source` mirrors the tagging vocabulary:
      detected  - clustering's own guess, awaiting judgement
      confirmed - the operator agreed
      rejected  - the operator said no; never re-offered for this person
    """

    __tablename__ = "faces"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    frame_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("frames.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), index=True
    )
    person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("people.id", ondelete="SET NULL"), default=None, index=True
    )
    # Normalised 0-1 so a box survives the frame being re-rendered at another size.
    box_x: Mapped[float] = mapped_column(Float)
    box_y: Mapped[float] = mapped_column(Float)
    box_w: Mapped[float] = mapped_column(Float)
    box_h: Mapped[float] = mapped_column(Float)
    detection_score: Mapped[float] = mapped_column(Float, default=0.0)
    # ArcFace, 512-d, L2-normalised like the CLIP vectors so cosine is a dot.
    embedding: Mapped[list[float] | None] = mapped_column(Embedding(), default=None)
    source: Mapped[str] = mapped_column(String(20), default="detected", index=True)
    similarity: Mapped[float | None] = mapped_column(Float, default=None)
    # A person this face might also be, found by searching the whole catalogue
    # rather than by clustering. Kept separate from `person_id` on purpose: a
    # suggestion must not disturb the grouping a face already belongs to, or
    # rejecting one would strand it away from whoever it really is.
    suggested_person_id: Mapped[uuid.UUID | None] = mapped_column(
        # SET NULL, never CASCADE: a suggestion is an opinion about a face, and
        # deleting the opinion must not delete the face.
        ForeignKey("people.id", ondelete="SET NULL"),
        default=None,
    )
    suggested_similarity: Mapped[float | None] = mapped_column(Float, default=None)
    # 'pending' | 'rejected'. Rejections are kept so the next sweep does not
    # offer the same face for the same person again.
    suggestion_state: Mapped[str | None] = mapped_column(String(16), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Slideshow(Base):
    """A slideshow the operator has asked for, and the video it produced.

    Stored rather than rendered-and-forgotten because the interesting failure
    is social, not technical: somebody watches it and says a person is missing,
    or that one photograph should not have been included. Answering that
    requires knowing exactly which frames went in and in what order, which is
    why `asset_ids` holds the resolved selection rather than the query that
    produced it. Selection is deterministic, but the *library* is not — a scan
    completing between two renders would otherwise silently change the result.

    Re-rendering an existing row therefore reproduces the same video. Changing
    what is in it is an edit to the selection, which is the operator's decision
    and not a side effect of time passing.
    """

    __tablename__ = "slideshows"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(200), default="")
    theme: Mapped[str] = mapped_column(String(40), default="plain")
    # pending | rendering | ready | failed
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    # The resolved selection, in render order.
    asset_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Pacing, canvas and audio. JSON so a new knob does not need a migration —
    # these are render parameters, never queried across rows.
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Relative to the data volume, like Derivative.relative_path.
    relative_path: Mapped[str | None] = mapped_column(String(1024), default=None)
    duration_seconds: Mapped[float | None] = mapped_column(Float, default=None)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, default=None)
    # Segments finished, out of len(asset_ids). A slideshow render is minutes
    # of work, so "it is doing something" has to be answerable.
    segments_done: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(String(500), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Listing(Base):
    """A property shoot being ordered and named for upload.

    The point of a listing is the *sequence*: MLS galleries display in upload
    order, so the export names files 01_, 02_ ... and the operator's ordering
    here is the product. Room labels are suggestions from CLIP until someone
    confirms or overrides them - same contract as tags and faces.
    """

    __tablename__ = "listings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    # none | queued | exporting | ready | failed
    export_status: Mapped[str] = mapped_column(String(20), default="none")
    export_relpath: Mapped[str | None] = mapped_column(String(1024), default=None)
    export_error: Mapped[str | None] = mapped_column(String(500), default=None)
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Digest of what the zip was made from (media/export_state.py); a listing
    # that no longer digests the same has a stale export.
    export_fingerprint: Mapped[str | None] = mapped_column(String(64), default=None)
    # Ends every exported file name: the address and sale type, e.g.
    # "130-davis-rd-auction". "" falls back to one derived from the name.
    file_suffix: Mapped[str] = mapped_column(String(120), default="", server_default="")
    # Heads the photo index — auction date, terms, whatever the brochure and
    # the editor need to know that the photographs cannot say.
    notes: Mapped[str] = mapped_column(Text, default="", server_default="")
    # Auto-edit run: idle | queued | running | done | failed. One at a time
    # per listing, held here rather than in a browser tab (migration 0021).
    ai_edit_state: Mapped[str] = mapped_column(String(20), default="idle", server_default="idle")
    ai_edit_mode: Mapped[str] = mapped_column(String(12), default="", server_default="")
    ai_edit_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    # What the last run did, in a line the page shows as-is.
    ai_edit_message: Mapped[str] = mapped_column(String(300), default="", server_default="")
    # "gallery": files named in gallery order (01-kitchen-…), for MLS.
    # "place": each item's slug is its whole file name, place first
    # (ledgerock-03-fall-drone), for a showcase browsed by place.
    file_naming: Mapped[str] = mapped_column(
        String(12), default="gallery", server_default="gallery"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ListingItem(Base):
    """One photograph's place in a listing.

    A join table rather than a JSON id list (contrast Slideshow.asset_ids)
    because each item carries editable state - label and position - and
    correcting one photograph must not rewrite the whole selection.
    """

    __tablename__ = "listing_items"
    __table_args__ = (
        UniqueConstraint("listing_id", "asset_id", name="uq_listing_items_listing_asset"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    listing_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(default=0)
    # A key from ai/rooms.py, or "" while unclassified.
    room: Mapped[str] = mapped_column(String(40), default="")
    room_source: Mapped[str] = mapped_column(String(16), default="suggested")
    room_score: Mapped[float | None] = mapped_column(Float, default=None)
    # What the photograph shows, for the photo index, and the words its
    # exported file name carries. Same contract as the room: "suggested" by
    # the AI until the operator edits it, then "confirmed" and never
    # overwritten by a later run. "" = nobody has named it yet.
    caption: Mapped[str] = mapped_column(String(300), default="", server_default="")
    slug: Mapped[str] = mapped_column(String(80), default="", server_default="")
    # A frame of a video, in ms from its start: the item is then that frame,
    # grabbed at full size from the original on import (a showcase of a
    # course that was filmed but not photographed).
    frame_ms: Mapped[int | None] = mapped_column(default=None)
    naming_source: Mapped[str] = mapped_column(String(16), default="", server_default="")
    named_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AssetEdit(Base):
    """One version of a photograph's develop recipe.

    Append-only: saving writes version N+1, undo reads version N-1, and
    reverting to the original deletes the rows. Pixels are never stored here
    and the original is never written - the recipe is applied at render time,
    which is what lets an edit be changed a month later without loss.
    """

    __tablename__ = "asset_edits"
    __table_args__ = (UniqueConstraint("asset_id", "version", name="uq_asset_edits_asset_version"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(default=1)
    # Slider values - see media/develop.py for the schema and the maths.
    recipe: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AssetInpaint(Base):
    """One round of object removal on a photograph.

    The result is a file (LaMa is tens of seconds per region, too costly to
    re-derive at render time) and versions chain: each removal runs on the
    previous result, undo deletes the newest row and its file. mask_meta
    keeps the mask that was painted - a record of exactly which pixels are
    invented, which a tool that edits real-estate photographs owes its
    operator.
    """

    __tablename__ = "asset_inpaints"
    __table_args__ = (
        UniqueConstraint("asset_id", "version", name="uq_asset_inpaints_asset_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(default=1)
    mask_meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # queued | running | ready | failed
    status: Mapped[str] = mapped_column(String(20), default="queued")
    relative_path: Mapped[str | None] = mapped_column(String(1024), default=None)
    error: Mapped[str | None] = mapped_column(String(500), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AiUsage(Base):
    """One call to the Claude API, with the token counts the API reported.

    The ledger behind the monthly budget on the Security page (ai/spend.py).
    Token counts are what the API said, not an estimate; `cost_usd` is those
    counts priced from a dated table and is null for a model the table does
    not know. A failed call has no usage and costs nothing, which is also
    what the API bills.
    """

    __tablename__ = "ai_usage"
    __table_args__ = (Index("ix_ai_usage_purpose_created", "purpose", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    # tag_review | recipe | describe | chat
    purpose: Mapped[str] = mapped_column(String(30))
    model: Mapped[str] = mapped_column(String(100))
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cache_write_tokens: Mapped[int] = mapped_column(default=0)
    cache_read_tokens: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[float | None] = mapped_column(Float, default=None)
    asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"), default=None
    )
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(String(200), default="")


class ShotWindow(Base):
    """A stretch of a video where the camera keeps doing one thing.

    Written by the motion analysis (processing/shots.py) from the proxy:
    the move, the camera's speed and direction, how steady it is at each
    playback speed, and how busy the subject is. The descriptor pass adds
    light, framing and subjects. All of it describes; none of it judges —
    which speed, pace or look an edit wants is the editor's call.

    Rows are rebuilt whole when a clip is re-analysed, so nothing else
    keys on their ids for long: cut history keeps a snapshot instead.
    Descriptors, not tags: nothing here enters the tag review queue.
    A local model's opinion of a shot is not stored here either: it lives
    in `looks`, keyed by the shot's times, so a re-analysis that finds the
    same shot keeps it.
    """

    __tablename__ = "shot_windows"
    __table_args__ = (
        UniqueConstraint("asset_id", "idx", name="uq_shot_windows_asset_idx"),
        Index("ix_shot_windows_move", "move"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), index=True
    )
    idx: Mapped[int] = mapped_column(default=0)
    start_ms: Mapped[int] = mapped_column()
    end_ms: Mapped[int] = mapped_column()
    # static | pan | tilt | push-in | pull-out | orbit | rise | descend |
    # lateral glide | top-down drift (media/motion.MOVES)
    move: Mapped[str] = mapped_column(String(20))
    # Which way the camera travels: left/right/up/down/in/out/forward/back.
    direction: Mapped[str | None] = mapped_column(String(10), default=None)
    # Frame widths per second.
    speed: Mapped[float] = mapped_column(Float, default=0.0)
    # 0-1 at real time, and per playback speed ("25" ... "800").
    smooth: Mapped[float] = mapped_column(Float, default=0.0)
    smooth_by_speed: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    energy: Mapped[str] = mapped_column(String(8), default="low")  # low | medium | high
    energy_score: Mapped[float] = mapped_column(Float, default=0.0)
    source_fps: Mapped[float | None] = mapped_column(Float, default=None)
    # The window's mean motion terms, kept so a move can be re-labelled
    # (e.g. once framing says top-down) without decoding again.
    features: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    # Light: the sun's elevation at the window's moment and place, its
    # class (media/sun.LIGHT_CLASSES), and the measured look.
    sun_elevation: Mapped[float | None] = mapped_column(Float, default=None)
    light: Mapped[str | None] = mapped_column(String(12), default=None, index=True)
    look: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Framing and subjects from CLIP (media/shot_vocabulary.py); empty
    # until the descriptor pass has run.
    framing: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    subjects: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    analysis_version: Mapped[int] = mapped_column(default=1)
    described_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Cut(Base):
    """A cut that was built — the record that lets the next video be
    different on purpose.

    Written when build_rough_cut runs (and by importing an older timeline,
    ops/shots.py import-cut). The pieces are kept with a snapshot of what
    each shot was (move, light, subjects, smoothness), so the summary of a
    past cut does not change when a clip is re-analysed.

    Cuts that revise one another are a family: every version shares
    `coalesce(root_id, id)`, and nothing is ever overwritten. A local
    model's draft (`source` "local") counts as footage that went somewhere
    only once its Premiere file has been downloaded (`sent_at`); until then
    it marks no shot used (`nle.shotlist.counts_as_used`).
    """

    __tablename__ = "cuts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(300), default="")
    # Who the video is for; defaults to the library most of its pieces
    # come from (GELCO).
    client: Mapped[str] = mapped_column(String(200), default="", index=True)
    library_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("libraries.id", ondelete="SET NULL"), default=None, index=True
    )
    source: Mapped[str] = mapped_column(String(20), default="mcp")  # mcp | import | local
    profile: Mapped[str | None] = mapped_column(String(100), default=None)
    duration_s: Mapped[float] = mapped_column(Float, default=0.0)
    piece_count: Mapped[int] = mapped_column(default=0)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    # The family: the first version's id (NULL on the first version itself)
    # and the cut this one revises. Neither is a foreign key: deleting
    # version 1 must not un-group versions 2 and 3, and a deleted parent
    # reads as "an earlier version, since deleted".
    root_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None, index=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    # 1 + the highest in the family when written. Two written at the same
    # instant may share a number, so readers order by (version, created_at).
    version: Mapped[int] = mapped_column(default=1)
    # The first time its Premiere file was downloaded from the Cuts page.
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Which build drafted it, as text (the provenance names every table of
    # answers uses): removing a model must not touch a cut. NULL on cuts no
    # local model wrote.
    model: Mapped[str | None] = mapped_column(String(200), default=None)
    model_revision: Mapped[str | None] = mapped_column(String(64), default=None)
    # Part of what makes a build (as on `local_models`); "" when none.
    variant: Mapped[str] = mapped_column(String(100), default="")
    runtime: Mapped[str | None] = mapped_column(String(30), default=None)
    runtime_version: Mapped[str | None] = mapped_column(String(40), default=None)
    prompt_version: Mapped[str | None] = mapped_column(String(40), default=None)

    pieces: Mapped[list["CutPiece"]] = relationship(
        back_populates="cut", cascade="all, delete-orphan", order_by="CutPiece.position"
    )


class CutPiece(Base):
    __tablename__ = "cut_pieces"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    cut_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cuts.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(default=0)
    # SET NULL: a cut that used a since-deleted file is still history.
    asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"), default=None, index=True
    )
    filename: Mapped[str] = mapped_column(String(512), default="")
    in_ms: Mapped[int] = mapped_column(default=0)
    out_ms: Mapped[int] = mapped_column(default=0)
    speed: Mapped[float] = mapped_column(Float, default=1.0)
    # The window the piece came from, as it was described then: move,
    # direction, light, subjects, smoothness at this speed, idx.
    shot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Why the piece is there (the cut's marker). Kept so a cut can be
    # downloaded again with its markers and shown with a reason per shot.
    # Model text when a local model drafted it: shown only as text.
    note: Mapped[str] = mapped_column(String(500), default="")

    cut: Mapped[Cut] = relationship(back_populates="pieces")


# --------------------------------------------------------------------------
# The chat panel (framefound/chat, docs/chat.md)
# --------------------------------------------------------------------------


class ChatConversation(Base):
    """One conversation with Claude in the web panel, private to its owner.

    `model` and `prefix_hash` are fixed when the conversation starts: the
    instructions and the tool list Claude was first sent are kept (as a file
    named by that hash) and sent again unchanged on every later request, so
    an update to FrameFound cannot change the start of a conversation Claude
    has already reasoned about. `state_at` is a heartbeat while a turn runs;
    a `running` row nobody has touched for a few minutes belonged to a
    process that died and is treated as idle.
    """

    __tablename__ = "chat_conversations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(200), default="")
    model: Mapped[str] = mapped_column(String(100))
    # Whether previews Claude asks to look at are sent to it. Switching it
    # changes what later tool results carry, never what was already sent.
    pictures: Mapped[bool] = mapped_column(Boolean, default=True)
    prefix_hash: Mapped[str] = mapped_column(String(64), default="")
    # idle | running | awaiting_confirmation
    state: Mapped[str] = mapped_column(String(24), default="idle")
    state_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # How the last turn ended (end | awaiting_confirmation | budget | steps |
    # refusal | max_tokens | error) and the sentence that went with it, so a
    # panel that was closed while the turn ran can still say what happened.
    turn_reason: Mapped[str | None] = mapped_column(String(24), default=None)
    turn_message: Mapped[str | None] = mapped_column(String(500), default=None)
    spent_usd: Mapped[float] = mapped_column(Float, default=0.0)
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ChatMessage(Base):
    """One turn as the Claude API has it: `content` is the content-block
    array exactly as it was sent or received, thinking blocks included,
    because the API rejects a conversation whose earlier turns were altered.

    The one substitution is a picture: its bytes live in a file beside the
    conversation and the block here is
    `{"type": "image", "blob": "<sha256>", "media_type": "image/jpeg"}`.
    The column is JSON, not JSONB, on purpose: JSONB reorders keys.
    """

    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "seq", name="uq_chat_messages_conversation_seq"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_conversations.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column()
    role: Mapped[str] = mapped_column(String(10))  # user | assistant
    content: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # What the operator typed and the page they were on, for showing the
    # thread again; what Claude was sent is `content`.
    text: Mapped[str | None] = mapped_column(Text, default=None)
    context: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    usage: Mapped[dict[str, int] | None] = mapped_column(JSON, default=None)
    cost_usd: Mapped[float | None] = mapped_column(Float, default=None)
    stop_reason: Mapped[str | None] = mapped_column(String(30), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChatToolCall(Base):
    """A tool Claude asked for in a conversation, and what became of it.

    `state` is one of six, and every row says which (there is no default:
    a row that forgot would otherwise read as waiting for a yes):

    - `running`: being run now, with no answer yet. An at-once call from
      the moment Claude asks; a call that needed a yes from the moment the
      yes is given. A row still `running` when no turn is, was interrupted.
    - `ran`: run at once, and it answered.
    - `pending`: waiting for the operator's yes or no (the tools that
      change the catalogue or spend money). Never run.
    - `approved`: the operator said yes, it was run, and it answered.
    - `declined`: the operator said no, or wrote a new message instead of
      answering. Never run.
    - `failed`: Claude was told it did not work. That covers a tool that
      was run and reported an error (at once or after a yes), a call that
      was interrupted with its effect unknown, and a call that was never
      run because Claude's answer was cut off before it was complete. So
      `failed` does not mean "was run": `result` says which it was.

    `decided_at` and `decided_by` are set by a yes or a no. `result` is
    what Claude was told, plus the pictures the operator is shown.
    """

    __tablename__ = "chat_tool_calls"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_conversations.id", ondelete="CASCADE"), index=True
    )
    # The assistant turn that asked for it.
    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(default=0)
    tool_use_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(60))
    input: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    needs_confirm: Mapped[bool] = mapped_column(Boolean, default=False)
    # running | ran | pending | approved | declined | failed (see above)
    state: Mapped[str] = mapped_column(String(12))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    artifact_name: Mapped[str | None] = mapped_column(String(200), default=None)
    artifact_path: Mapped[str | None] = mapped_column(String(1024), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )


# --------------------------------------------------------------------------
# Local AI models (framefound/local_models, docs/local-models.md)
# --------------------------------------------------------------------------


class LocalModel(Base):
    """One build of a model from Hugging Face that was added to this install.

    A build is a repository at one commit (and, where a repository holds
    several, one variant of it), so the same model at a newer commit is a
    second row. `repo_key` is the name casefolded: Hugging Face tells
    capitals apart and neither the Mac's disk nor a network share does, so
    two names that differ only in capitals must not become two folders that
    are one.

    `chain` is the model and everything it says it was built from, as read
    when it was added. Approval is judged again from it each time the model
    is used, so blocking a publisher today stops a model added last month.
    `files` is exactly what is downloaded and checked, nothing else.

    No path is stored: where the files are is worked out from the storage
    settings each time, so moving the model library changes no row. Nothing
    in the catalogue points here; what a model said is recorded beside the
    result as text, so removing a model never touches a tag or a cut.
    """

    __tablename__ = "local_models"
    __table_args__ = (
        UniqueConstraint("repo_key", "revision", "variant", name="uq_local_models_build"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # `publisher/name`, as Hugging Face spells it.
    repo_id: Mapped[str] = mapped_column(String(200))
    repo_key: Mapped[str] = mapped_column(String(200))
    # The commit every file is fetched at.
    revision: Mapped[str] = mapped_column(String(64))
    # Which build inside the repository; "" when the repository is one build.
    variant: Mapped[str] = mapped_column(String(100), default="")
    publisher: Mapped[str] = mapped_column(String(100))
    # [{"repo_id", "publisher", "bases", "found"}], this model first.
    chain: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    licence: Mapped[str] = mapped_column(String(100), default="")
    licence_name: Mapped[str] = mapped_column(String(200), default="")
    licence_url: Mapped[str | None] = mapped_column(String(500), default=None)
    # "" when anyone may download it; else how access is granted ("auto", ...).
    gated: Mapped[str] = mapped_column(String(10), default="")
    # The kind of program that runs it (local_models/kinds.py): mlx-vlm | demo
    runtime: Mapped[str] = mapped_column(String(30))
    # What it takes, as its page says: ["text", "image"].
    takes: Mapped[list[str]] = mapped_column(JSON, default=list)
    # [{"path", "size", "sha256", "blob_id"}]
    files: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    # Worked out from the size of the weights until a real answer has been
    # measured; then `measured_memory_gb` holds the highest figure ever
    # seen, never the last one (a short test uses less than a long prompt).
    memory_gb: Mapped[float] = mapped_column(Float, default=0.0)
    measured_memory_gb: Mapped[float | None] = mapped_column(Float, default=None)
    # downloading | installed | incomplete
    state: Mapped[str] = mapped_column(String(12), default="downloading")
    # Why it is incomplete, as a sentence.
    error: Mapped[str | None] = mapped_column(String(500), default=None)
    # "mlx-vlm/0.7.12": the program and version it last loaded under, so a
    # load that fails after an update can say which version to go back to.
    last_loaded_with: Mapped[str | None] = mapped_column(String(60), default=None)
    # When this build was published, for display.
    hub_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    added_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class LocalAiRole(Base):
    """Which model does a job: one row for the Looker, one for the Editor.

    `model_id` has the role. `pending_model_id` gets it once its files have
    been copied to this machine and checked; until then the old model keeps
    the role, so a share that drops half-way through a copy does not leave
    the role with nobody. Two columns rather than one JSON value, so the
    page changing one role and a finished copy completing the other cannot
    overwrite each other.

    A model that holds or is waiting for a role is not removed: that rule is
    kept in code (local_models/registry.py), not left to these foreign keys.
    """

    __tablename__ = "local_ai_roles"

    # looker | editor (local_models.ROLES)
    role: Mapped[str] = mapped_column(String(20), primary_key=True)
    model_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("local_models.id", ondelete="SET NULL"), default=None
    )
    pending_model_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("local_models.id", ondelete="SET NULL"), default=None
    )
    # When and by whom the model in `model_id` was given the role.
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )


class LocalAiJob(Base):
    """One piece of local-AI work: a download, a copy to this machine, a
    runtime install, a test, and whatever later phases ask of a model.

    The row is the job. The queue only carries a wake-up, so progress,
    cancel, "waiting until 21:30" and "the worker died" all survive a tab
    closing and a process restarting, and a message delivered twice finds
    nothing left to do.

    Two parties write a row: the runner (progress, by naming the columns it
    writes) and the API (`cancel_requested`, and nothing else writes that).
    Neither ever writes the whole row back from a copy read earlier, which
    is how a Cancel pressed mid-progress would be lost.
    """

    __tablename__ = "local_ai_jobs"
    __table_args__ = (Index("ix_local_ai_jobs_state", "state", "priority", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # download | place | runtime_install | test; later phases add kinds.
    kind: Mapped[str] = mapped_column(String(30))
    # What it is about, in words: "mlx-community/gemma-4-e4b-it-4bit@475b9088".
    subject: Mapped[str] = mapped_column(String(300), default="")
    model_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("local_models.id", ondelete="SET NULL"), default=None, index=True
    )
    # The role a job asked by name, when it asked a role rather than a model.
    role: Mapped[str | None] = mapped_column(String(20), default=None)
    # Lower runs first: 0 somebody pressed a button and is watching,
    # 5 asked for, 9 background.
    priority: Mapped[int] = mapped_column(SmallInteger, default=5)
    # queued | waiting | running | done | failed | cancelled
    state: Mapped[str] = mapped_column(String(12), default="queued")
    # For `waiting`: not before this moment.
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    phase: Mapped[str] = mapped_column(String(20), default="")
    # Bytes for a transfer; items for a batch.
    done: Mapped[int] = mapped_column(BigInteger, default=0)
    total: Mapped[int] = mapped_column(BigInteger, default=0)
    # The one line the row shows: why it waits, how it failed.
    message: Mapped[str] = mapped_column(String(500), default="")
    # The installer's or the runtime's last lines.
    detail: Mapped[str | None] = mapped_column(Text, default=None)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    # What answered, for a job that asked a model. The same five names, as
    # text, wherever an answer is recorded: a job row is history and may be
    # pruned, and removing a model must not change what it once said.
    model: Mapped[str | None] = mapped_column(String(200), default=None)
    model_revision: Mapped[str | None] = mapped_column(String(64), default=None)
    runtime: Mapped[str | None] = mapped_column(String(30), default=None)
    runtime_version: Mapped[str | None] = mapped_column(String(40), default=None)
    prompt_version: Mapped[str | None] = mapped_column(String(40), default=None)
    seconds: Mapped[float | None] = mapped_column(Float, default=None)
    memory_gb: Mapped[float | None] = mapped_column(Float, default=None)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # The runner's heartbeat: a `running` row nobody has touched for a while
    # belonged to a worker that died.
    beat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


# --------------------------------------------------------------------------
# Comparing local models on a fixed sample (framefound/local_models/
# comparisons.py, docs/local-models.md "Comparing models")
# --------------------------------------------------------------------------


class Comparison(Base):
    """A fixed sample of footage that installed models are asked about.

    Everything a model is shown is frozen when the comparison is made: the
    items (asset plus time span, never a shot-window id, which a re-analysis
    deletes), the sentence each item's facts are told as, the vocabulary,
    the pictures (files under `<data_dir>/compare/<id>/`) and the ground
    truth — the tags a person had decided and the shots the counted cuts
    used, as of `truth_at`. Truth read after a model has looked could be the
    model's own suggestions confirmed, so it is never refreshed; "compare
    again on the same sample" makes a new comparison instead.

    Nothing a model says here reaches the catalogue. The owner's decision
    (`verdict`) is a record; it changes no role by itself.
    """

    __tablename__ = "local_ai_comparisons"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    # folder | listing | cuts | comparison
    source_kind: Mapped[str] = mapped_column(String(12))
    # The request's `source` as asked, ids as text: a record, not a key.
    source: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # The sample in words, as it read that day.
    source_label: Mapped[str] = mapped_column(String(300), default="")
    # shots | photos | both
    what: Mapped[str] = mapped_column(String(8), default="both")
    # Items actually in the sample.
    size: Mapped[int] = mapped_column(default=0)
    # Pictures per shot: 3 (start, middle, end) or 1 (the middle).
    stills: Mapped[int] = mapped_column(SmallInteger, default=3)
    # What drew the sample.
    seed: Mapped[int] = mapped_column(BigInteger, default=0)
    # The question's version (looking/prompt.py); an older one can be read,
    # not added to.
    prompt_version: Mapped[str] = mapped_column(String(40))
    # [[name, count], ...] as every model is told it.
    vocabulary: Mapped[list[Any]] = mapped_column(JSON, default=list)
    # [{"id", "title", "source"}]: the cuts that define "used".
    counted_cuts: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # {"photos", "shots", "clips", "used", "unused", "footage_with_tags",
    #  "yes", "no", "pool": {...}, "left_out": {...}}
    counts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # When the ground truth was read.
    truth_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # "" | chosen | none
    verdict: Mapped[str] = mapped_column(String(8), default="")
    # The chosen column. No foreign key: it would point both ways between
    # this table and the columns' table; the code checks it.
    verdict_entry_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    verdict_note: Mapped[str] = mapped_column(Text, default="")
    verdict_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    verdict_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ComparisonItem(Base):
    """One piece of the sample: a photograph, or a shot of a clip.

    A shot is the clip plus `start_ms`/`end_ms`, because shot windows are
    rebuilt by a re-analysis and their ids do not survive it. The facts,
    the facts sentence and the truth are copies as they were that day.
    `asset_id` is NULL once the file has left the catalogue (and may point
    at nothing on SQLite, which does not enforce foreign keys); the item
    and its frozen pictures stay. `position` also names the picture files
    (`<position:04d>-<n>.jpg`); `picture_ms` says how many there are.
    """

    __tablename__ = "local_ai_comparison_items"
    __table_args__ = (
        UniqueConstraint("comparison_id", "position", name="uq_local_ai_comparison_items_position"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    comparison_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("local_ai_comparisons.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column()
    # photo | shot
    kind: Mapped[str] = mapped_column(String(8))
    asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"), default=None, index=True
    )
    # For display after the asset is gone.
    library_name: Mapped[str] = mapped_column(String(200), default="")
    relative_path: Mapped[str] = mapped_column(String(2048), default="")
    filename: Mapped[str] = mapped_column(String(512), default="")
    media_type: Mapped[str] = mapped_column(String(10), default="")
    start_ms: Mapped[int | None] = mapped_column(default=None)
    end_ms: Mapped[int | None] = mapped_column(default=None)
    # sampling.window_facts as it was: for the page.
    facts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Exactly what every model is told about this item ("" for a photograph).
    facts_line: Mapped[str] = mapped_column(String(400), default="")
    # The moment of each frozen picture.
    picture_ms: Mapped[list[int]] = mapped_column(JSON, default=list)
    # [{"slug", "name"}] decided on this asset as of the comparison's truth_at.
    truth_yes: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)
    truth_no: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)
    # NULL: not a shot, or no cut history was counted.
    used: Mapped[bool | None] = mapped_column(Boolean, default=None)
    # Titles of the counted cuts that used it.
    used_in: Mapped[list[str]] = mapped_column(JSON, default=list)


class ComparisonModel(Base):
    """One model's column in a comparison, and the state of its turn.

    The handler (and Cancel) write `state`; what the page shows is worked
    out from this row and its job together, so a worker that died reads as
    stopped rather than running for ever, and pruning job rows loses
    nothing. Which build answered is kept as text (the provenance names
    every table of answers uses); `model_id` is NULL once the model is
    removed, and the column with its answers stays.

    `variant` is part of what makes a build (as on `local_models`): two
    builds of one repository at one commit — two quantisations of a GGUF
    repository — are two columns, not "the same build twice".
    """

    __tablename__ = "local_ai_comparison_models"
    __table_args__ = (
        UniqueConstraint(
            "comparison_id",
            "model",
            "model_revision",
            "variant",
            name="uq_local_ai_comparison_models_build",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    comparison_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("local_ai_comparisons.id", ondelete="CASCADE"), index=True
    )
    model_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("local_models.id", ondelete="SET NULL"), default=None, index=True
    )
    # The repository and its pinned commit, as text.
    model: Mapped[str] = mapped_column(String(200))
    model_revision: Mapped[str] = mapped_column(String(64))
    variant: Mapped[str] = mapped_column(String(100), default="")
    model_name: Mapped[str] = mapped_column(String(200), default="")
    # Filled by the first answer.
    runtime: Mapped[str | None] = mapped_column(String(30), default=None)
    runtime_version: Mapped[str | None] = mapped_column(String(40), default=None)
    prompt_version: Mapped[str] = mapped_column(String(40))
    # queued | running | waiting | done | stopped | failed
    state: Mapped[str] = mapped_column(String(12), default="queued")
    message: Mapped[str] = mapped_column(String(500), default="")
    # The runtime's last lines, when a load failed.
    detail: Mapped[str | None] = mapped_column(Text, default=None)
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # The job running it, or the last one. No foreign key: job rows may be
    # pruned.
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    # Answers stored: ok, not ok. Progress is these, not the job's counts.
    done: Mapped[int] = mapped_column(default=0)
    failed: Mapped[int] = mapped_column(default=0)
    load_seconds: Mapped[float | None] = mapped_column(Float, default=None)
    # this_machine | library
    ran_from: Mapped[str | None] = mapped_column(String(14), default=None)
    # The most any answer reported, and how it was measured (runtime | process).
    memory_gb: Mapped[float | None] = mapped_column(Float, default=None)
    memory_source: Mapped[str | None] = mapped_column(String(10), default=None)
    added_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    # Also the order of the columns on the page.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class ComparisonAnswer(Base):
    """What one model said about one item, or why it said nothing usable.

    One row per column and item, committed as soon as it is had: "the items
    with no answer row" is where a restart, a second delivery, a step aside
    and Carry on all resume, and the unique key makes a doubled answer
    impossible. Model text is untrusted and is only ever shown as text.
    """

    __tablename__ = "local_ai_comparison_answers"
    __table_args__ = (
        UniqueConstraint("entry_id", "item_id", name="uq_local_ai_comparison_answers_entry_item"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    entry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("local_ai_comparison_models.id", ondelete="CASCADE"), index=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("local_ai_comparison_items.id", ondelete="CASCADE"), index=True
    )
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    # bad_answer | died | timeout | runtime_error | no_picture
    problem: Mapped[str | None] = mapped_column(String(20), default=None)
    message: Mapped[str] = mapped_column(String(300), default="")
    # [{"name", "slug", "confidence", "in_vocabulary"}], strongest first.
    tags: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    caption: Mapped[str] = mapped_column(String(300), default="")
    # 0-100
    score: Mapped[int | None] = mapped_column(SmallInteger, default=None)
    reason: Mapped[str] = mapped_column(String(300), default="")
    # What the model wrote, when worth keeping (a failure, a second try);
    # at most 4,000 characters.
    raw: Mapped[str | None] = mapped_column(Text, default=None)
    seconds: Mapped[float | None] = mapped_column(Float, default=None)
    attempts: Mapped[int] = mapped_column(SmallInteger, default=1)
    memory_gb: Mapped[float | None] = mapped_column(Float, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --------------------------------------------------------------------------
# Looking: a local model looking at the catalogue (framefound/looking,
# docs/local-models.md "Looking")
# --------------------------------------------------------------------------


class LookRequest(Base):
    """A review somebody asked for: a folder (and everything beneath it)
    looked at by a local model, now or overnight.

    The work itself is a `local_ai_jobs` row (plus one that makes clips
    ready); this row is what was asked and how far it got, and it outlives
    those jobs, which L1 may prune — hence no foreign key to them.

    Three parties write a row: the look job (its counters, `state`,
    `message`, the times), the make-ready job (`prep_*`) and the API
    (`state` on cancel). Each writes by naming its columns in an UPDATE,
    never by flushing a copy read earlier, which is how a Cancel pressed
    mid-progress would be lost.
    """

    __tablename__ = "look_requests"
    __table_args__ = (Index("ix_look_requests_state", "state", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("libraries.id", ondelete="CASCADE"), index=True
    )
    # Cleaned (media/folders.py); "" is the whole library. Always means
    # "and everything beneath it".
    folder: Mapped[str] = mapped_column(String(2048), default="")
    # now | tonight. Tonight runs only inside the overnight window, however
    # many nights that takes.
    run_when: Mapped[str] = mapped_column(String(10), default="now")
    # Look again at what another build or question already looked at.
    redo: Mapped[bool] = mapped_column(Boolean, default=False)
    # Make clips ready (a playable copy, their shots) first.
    prepare: Mapped[bool] = mapped_column(Boolean, default=True)
    # The model asked for; NULL = whoever is the Looker when it runs. No
    # foreign key: nothing outside L1's own tables points at `local_models`.
    model_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    model_name: Mapped[str] = mapped_column(String(200), default="")
    # active | done | failed | cancelled
    state: Mapped[str] = mapped_column(String(12), default="active")
    # The closing sentence.
    message: Mapped[str] = mapped_column(String(500), default="")
    look_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    prepare_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    # Items: photographs and shots.
    done: Mapped[int] = mapped_column(default=0)
    total: Mapped[int] = mapped_column(default=0)
    failed: Mapped[int] = mapped_column(default=0)
    tags_suggested: Mapped[int] = mapped_column(default=0)
    new_tags: Mapped[int] = mapped_column(default=0)
    # Suggestions kept back (a tag's queue full, too many new tags).
    held: Mapped[int] = mapped_column(default=0)
    # Clips judged from one still because no playable copy could be made.
    stills_only: Mapped[int] = mapped_column(default=0)
    # Making clips ready: "" | running | done; counted in clips.
    prep_state: Mapped[str] = mapped_column(String(10), default="")
    prep_total: Mapped[int] = mapped_column(default=0)
    prep_ready: Mapped[int] = mapped_column(default=0)
    prep_gave_up: Mapped[int] = mapped_column(default=0)
    prep_queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    prep_progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class Look(Base):
    """One attempt by a local model to look at a photograph or a shot.

    The ledger of "looked at": a shot is keyed by its times on its asset,
    never by a `shot_windows` id, because those rows are rebuilt on every
    re-analysis and a caption kept on one would vanish while the asset still
    said done. A failure is a row too (`ok` false, `note` the reason), which
    is how one review never asks about the same bad file twice.

    Which build answered is text (`model`, `model_revision`, `variant`,
    `runtime`, `runtime_version`, `prompt_version`) with no foreign key to
    `local_models`: removing a model must not change what it once said.
    Nothing is pruned; an asset's looks go with the asset.
    """

    __tablename__ = "looks"
    __table_args__ = (
        Index("ix_looks_asset_span", "asset_id", "start_ms", "end_ms"),
        Index("ix_looks_model_created", "model", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"))
    # Both NULL: the whole item (a photograph). Otherwise the shot's times.
    start_ms: Mapped[int | None] = mapped_column(default=None)
    end_ms: Mapped[int | None] = mapped_column(default=None)
    # False: tried and produced nothing usable.
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    # At most one current row per (asset_id, start_ms, end_ms); kept so by
    # the one writer (looking/store.py).
    current: Mapped[bool] = mapped_column(Boolean, default=True)
    # 0-100: how usable the model thought it.
    score: Mapped[int | None] = mapped_column(SmallInteger, default=None)
    caption: Mapped[str] = mapped_column(String(300), default="")
    reason: Mapped[str] = mapped_column(String(200), default="")
    # The cleaned answer and what became of each tag; model text, shown
    # only as text.
    answer: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # Why it failed.
    note: Mapped[str] = mapped_column(String(200), default="")
    # `<repo>@<revision>#<prompt version>`, or `<repo>@<revision>/<variant>#…`
    # for a build that has a variant: "already looked at by this build and
    # question" is a comparison of this, and two quantisations of one GGUF
    # repository at one commit are two builds.
    key: Mapped[str] = mapped_column(String(300), default="")
    model: Mapped[str] = mapped_column(String(200), default="")
    model_revision: Mapped[str] = mapped_column(String(64), default="")
    # Part of what makes a build (as on `local_models`); "" for a repository
    # with one build.
    variant: Mapped[str] = mapped_column(String(100), default="")
    runtime: Mapped[str] = mapped_column(String(30), default="")
    runtime_version: Mapped[str] = mapped_column(String(40), default="")
    prompt_version: Mapped[str] = mapped_column(String(40), default="")
    seconds: Mapped[float | None] = mapped_column(Float, default=None)
    # Answered by the demo's made-up model; the page says so.
    demo: Mapped[bool] = mapped_column(Boolean, default=False)
    # The review that asked; NULL = looking at what arrived (ingest).
    request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("look_requests.id", ondelete="SET NULL"), default=None, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --------------------------------------------------------------------------
# Colour: a look per clip, exported as a LUT, and a local model's proposals
# for a listing's photographs (media/clip_look.py, media/lut.py,
# docs/video-looks.md)
# --------------------------------------------------------------------------


class ClipLook(Base):
    """A colour look for one clip: what a local model suggested, and what a
    person chose.

    At most two rows per clip, one of each kind, and each has one writer:
    `suggested` is written only by the `local_ai` lane's job and `chosen`
    only by the API, so a suggestion arriving can never overwrite a choice
    (nor the reverse), and nothing is ever chosen for anyone. Keyed on the
    asset, never on a `shot_windows` id, which a re-analysis deletes.

    `recipe` holds only what a LUT can carry (`lut.lut_recipe`); white
    balance and levels are frozen in `measured` on the still at
    `still_ts_ms`, which is why the same recipe can be written as the same
    `.cube` file every time. The LUT itself is never stored. A chosen look
    that came from the model copies the suggestion's provenance, so it stays
    self-describing after the clip is suggested again.
    """

    __tablename__ = "clip_looks"
    __table_args__ = (UniqueConstraint("asset_id", "kind", name="uq_clip_looks_asset_kind"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), index=True
    )
    # suggested | chosen. Indexed: the video look learns from every chosen row.
    kind: Mapped[str] = mapped_column(String(10), index=True)
    # Where the sliders came from: model | learned | none | custom. A
    # suggested row is always `model`.
    source: Mapped[str] = mapped_column(String(10), default="model")
    # Cleaned, LUT-carried sliders only; {} is "as shot".
    recipe: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # {"levels": ..., "wb_gains": ...} frozen on the reference still.
    measured: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Slider keys a LUT could not carry, named rather than dropped in silence.
    left_out: Mapped[list[str]] = mapped_column(JSON, default=list)
    still_ts_ms: Mapped[int] = mapped_column(default=0)
    # compare.features of that still: what the video look learns from.
    features: Mapped[list[float]] = mapped_column(JSON, default=list)
    # The model's one line; model text, shown only as text.
    reason: Mapped[str] = mapped_column(String(300), default="")
    # Which build answered, as text (the provenance names every table of
    # answers uses): removing a model must not change what it once said.
    model: Mapped[str | None] = mapped_column(String(200), default=None)
    model_revision: Mapped[str | None] = mapped_column(String(64), default=None)
    # Part of what makes a build (as on `local_models`): two quantisations of
    # one GGUF repository at one commit. "" when none, or when no model answered.
    variant: Mapped[str] = mapped_column(String(100), default="")
    runtime: Mapped[str | None] = mapped_column(String(30), default=None)
    runtime_version: Mapped[str | None] = mapped_column(String(40), default=None)
    prompt_version: Mapped[str | None] = mapped_column(String(40), default=None)
    # Answered by the demo's made-up model; the page and the file say so.
    demo: Mapped[bool] = mapped_column(Boolean, default=False)
    # Who chose (chosen rows).
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ListingEditSuggestion(Base):
    """What a local model proposes for one photograph of a listing, until a
    person applies or dismisses it.

    Asking writes these rows and touches nothing else; Apply (the `local`
    mode of auto-edit, which never talks to a model) reads the ticked ones
    and writes edit versions exactly as the other modes do. One row per
    listing item: asking again replaces it. The row is the provenance of
    what Apply wrote, so `asset_edits` and `listing_items` are not altered.
    """

    __tablename__ = "listing_edit_suggestions"
    __table_args__ = (UniqueConstraint("item_id", name="uq_listing_edit_suggestions_item_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    listing_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), index=True
    )
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("listing_items.id", ondelete="CASCADE"))
    # Indexed: deleting an asset has to find its rows here, and without an
    # index that is a scan of the table per asset on PostgreSQL.
    asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), index=True
    )
    # Straightening, cleaned (media/photo_judge.py).
    rotate: Mapped[float] = mapped_column(Float, default=0.0)
    keystone: Mapped[float] = mapped_column(Float, default=0.0)
    sky_worth_replacing: Mapped[bool] = mapped_column(Boolean, default=False)
    caption: Mapped[str] = mapped_column(String(300), default="")
    slug: Mapped[str] = mapped_column(String(80), default="")
    note: Mapped[str] = mapped_column(String(300), default="")
    # proposed | applied | dismissed
    state: Mapped[str] = mapped_column(String(12), default="proposed")
    # The asset_edits.version Apply wrote.
    applied_version: Mapped[int | None] = mapped_column(default=None)
    model: Mapped[str | None] = mapped_column(String(200), default=None)
    model_revision: Mapped[str | None] = mapped_column(String(64), default=None)
    # As on `clip_looks`: "" when the build has none.
    variant: Mapped[str] = mapped_column(String(100), default="")
    runtime: Mapped[str | None] = mapped_column(String(30), default=None)
    runtime_version: Mapped[str | None] = mapped_column(String(40), default=None)
    prompt_version: Mapped[str | None] = mapped_column(String(40), default=None)
    demo: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )


# --------------------------------------------------------------------------
# Assembly: a local model drafting a rough cut (framefound/nle/drafts.py,
# docs/cuts.md)
# --------------------------------------------------------------------------


class CutDraft(Base):
    """A request to the Editor (a local model) for a rough cut, and how it went.

    A draft that succeeded is an ordinary cut in `cuts` (`source` "local"),
    written by the same function as Claude's; this row is the request. One
    that waits for the overnight window, failed or was cancelled lives only
    here, so no reader of `cuts` has to learn about states. Only the job
    writes the state once the row is queued (`waiting` is never stored: it is
    read off the job row).

    The shots the Editor was offered are kept by clip and span, never by a
    `shot_windows` id, which a re-analysis deletes. Model text (`answer_text`,
    `problems`) is shown only as text.
    """

    __tablename__ = "cut_drafts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # queued | running | done | failed | cancelled
    state: Mapped[str] = mapped_column(String(12), default="queued")
    # Why it failed, in a sentence.
    message: Mapped[str] = mapped_column(String(500), default="")
    title: Mapped[str] = mapped_column(String(120), default="")
    brief: Mapped[str] = mapped_column(Text, default="")
    # The footage asked about (drafts.Scope) plus its `label` for the page.
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    target_s: Mapped[float] = mapped_column(Float, default=30.0)
    min_shot_s: Mapped[float] = mapped_column(Float, default=1.5)
    max_shot_s: Mapped[float] = mapped_column(Float, default=8.0)
    exclude_used: Mapped[bool] = mapped_column(Boolean, default=False)
    # [{"n", "asset_id", "filename", "start_ms", "end_ms", "view", "line"}]
    candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # For "62 of 480 shots, from 41 clips".
    shots_in_scope: Mapped[int] = mapped_column(default=0)
    clips: Mapped[int] = mapped_column(default=0)
    # What the model wrote, cut to 20,000 characters.
    answer_text: Mapped[str | None] = mapped_column(Text, default=None)
    # The check's sentences: what was repaired or dropped.
    problems: Mapped[list[str]] = mapped_column(JSON, default=list)
    # The cut it became; a deleted cut leaves the request.
    cut_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("cuts.id", ondelete="SET NULL"), default=None, index=True
    )
    # "Draft again": the cut whose family gets the next version. History,
    # so no foreign key.
    revises_cut_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    # The local_ai_jobs row. No foreign key: job rows may be pruned.
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, default=None)
    origin: Mapped[str] = mapped_column(String(10), default="web")  # web | chat
    # Which build was asked, kept here too so a draft that failed still says
    # which model failed.
    model: Mapped[str | None] = mapped_column(String(200), default=None)
    model_revision: Mapped[str | None] = mapped_column(String(64), default=None)
    variant: Mapped[str] = mapped_column(String(100), default="")
    runtime: Mapped[str | None] = mapped_column(String(30), default=None)
    runtime_version: Mapped[str | None] = mapped_column(String(40), default=None)
    prompt_version: Mapped[str | None] = mapped_column(String(40), default=None)
    seconds: Mapped[float | None] = mapped_column(Float, default=None)
    demo: Mapped[bool] = mapped_column(Boolean, default=False)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
