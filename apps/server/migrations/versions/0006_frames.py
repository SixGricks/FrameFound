"""Sampled frames (scene changes + interval ticks).

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "frames",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("ts_ms", sa.Integer(), nullable=False),
        sa.Column("scene_number", sa.Integer(), nullable=True),
        sa.Column("is_scene_change", sa.Boolean(), nullable=False),
        sa.Column("relative_path", sa.String(length=1024), nullable=False),
        sa.Column("phash", sa.String(length=32), nullable=True),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("ocr_text", sa.Text(), nullable=True),
        sa.Column("embedding_model", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_frames"),
        sa.ForeignKeyConstraint(
            ["asset_id"], ["assets.id"], name="fk_frames_asset_id_assets", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("asset_id", "ts_ms", name="uq_frames_asset_id"),
    )
    op.create_index("ix_frames_asset_id", "frames", ["asset_id"])
    op.create_index("ix_frames_ts_ms", "frames", ["ts_ms"])
    op.create_index("ix_frames_phash", "frames", ["phash"])


def downgrade() -> None:
    op.drop_table("frames")
