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

Role = String(20)  # admin | user | readonly — validated in the service layer
ROLES = ("admin", "user", "readonly")


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(Role, default="user")
    totp_secret: Mapped[str | None] = mapped_column(String(255), default=None)  # TODO(m7)
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
    gps_lat: Mapped[float | None] = mapped_column(Float, default=None)
    gps_lon: Mapped[float | None] = mapped_column(Float, default=None)
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
