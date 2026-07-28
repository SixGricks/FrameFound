"""Media catalog: libraries, path_mappings, assets, scans.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "libraries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("root_path", sa.String(length=1024), nullable=False),
        sa.Column("read_only", sa.Boolean(), nullable=False),
        sa.Column("include_extensions", sa.JSON(), nullable=True),
        sa.Column("exclude_globs", sa.JSON(), nullable=False),
        sa.Column("scan_interval_minutes", sa.Integer(), nullable=True),
        sa.Column("watcher_enabled", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("last_scan_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_libraries"),
        sa.UniqueConstraint("name", name="uq_libraries_name"),
    )

    op.create_table(
        "path_mappings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("library_id", sa.Uuid(), nullable=False),
        sa.Column("profile_name", sa.String(length=100), nullable=False),
        sa.Column("platform", sa.String(length=20), nullable=False),
        sa.Column("mapped_prefix", sa.String(length=1024), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_path_mappings"),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["libraries.id"],
            name="fk_path_mappings_library_id_libraries",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("library_id", "profile_name", name="uq_path_mappings_library_id"),
    )

    op.create_table(
        "assets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("library_id", sa.Uuid(), nullable=False),
        sa.Column("relative_path", sa.String(length=2048), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("extension", sa.String(length=20), nullable=False),
        sa.Column("mime_type", sa.String(length=100), nullable=True),
        sa.Column("media_type", sa.String(length=10), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("mtime", sa.DateTime(timezone=True), nullable=False),
        sa.Column("partial_hash", sa.String(length=64), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("availability", sa.String(length=20), nullable=False),
        sa.Column("processing_status", sa.String(length=30), nullable=False),
        sa.Column(
            "first_indexed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("fps", sa.Float(), nullable=True),
        sa.Column("video_codec", sa.String(length=50), nullable=True),
        sa.Column("audio_codec", sa.String(length=50), nullable=True),
        sa.Column("sample_rate", sa.Integer(), nullable=True),
        sa.Column("channels", sa.Integer(), nullable=True),
        sa.Column("bitrate", sa.BigInteger(), nullable=True),
        sa.Column("orientation", sa.Integer(), nullable=True),
        sa.Column("camera_make", sa.String(length=100), nullable=True),
        sa.Column("camera_model", sa.String(length=100), nullable=True),
        sa.Column("lens", sa.String(length=200), nullable=True),
        sa.Column("focal_length_mm", sa.Float(), nullable=True),
        sa.Column("aperture_f", sa.Float(), nullable=True),
        sa.Column("shutter_speed", sa.String(length=20), nullable=True),
        sa.Column("iso", sa.Integer(), nullable=True),
        sa.Column("gps_lat", sa.Float(), nullable=True),
        sa.Column("gps_lon", sa.Float(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("favorite", sa.Boolean(), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("custom_fields", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_assets"),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["libraries.id"],
            name="fk_assets_library_id_libraries",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("library_id", "relative_path", name="uq_assets_library_id"),
    )
    op.create_index("ix_assets_filename", "assets", ["filename"])
    op.create_index("ix_assets_media_type", "assets", ["media_type"])
    op.create_index("ix_assets_partial_hash", "assets", ["partial_hash"])
    op.create_index("ix_assets_content_hash", "assets", ["content_hash"])
    op.create_index("ix_assets_captured_at", "assets", ["captured_at"])
    op.create_index("ix_assets_camera_model", "assets", ["camera_model"])
    op.create_index("ix_assets_last_verified_at", "assets", ["last_verified_at"])

    op.create_table(
        "scans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("library_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("files_seen", sa.Integer(), nullable=False),
        sa.Column("files_new", sa.Integer(), nullable=False),
        sa.Column("files_changed", sa.Integer(), nullable=False),
        sa.Column("files_moved", sa.Integer(), nullable=False),
        sa.Column("files_missing", sa.Integer(), nullable=False),
        sa.Column("files_deferred", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_scans"),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["libraries.id"],
            name="fk_scans_library_id_libraries",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_scans_status", "scans", ["status"])


def downgrade() -> None:
    op.drop_table("scans")
    op.drop_table("assets")
    op.drop_table("path_mappings")
    op.drop_table("libraries")
