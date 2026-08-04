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
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from framefound.db.base import Base
from framefound.db.vector_type import Embedding

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
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

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
    first_indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
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

    library: Mapped[Library] = relationship(back_populates="assets")


DERIVATIVE_KINDS = ("thumbnail", "preview", "poster", "proxy", "waveform")


class Job(Base):
    """Execution history for processing tasks (the dashboard's data source).

    Rows are written by the worker task shell — one per execution attempt.
    Live queue depths come from the broker, not this table."""

    __tablename__ = "jobs"

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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

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
