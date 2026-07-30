"""Slideshows: rendered videos and the selection that produced them

The resolved asset list is stored on the row rather than the query that chose
it, so re-rendering reproduces the same video even after the library has grown.

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "slideshows",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("title", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("theme", sa.String(length=40), nullable=False, server_default="plain"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("asset_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("settings", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("relative_path", sa.String(length=1024), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("segments_done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_slideshows_status", "slideshows", ["status"])


def downgrade() -> None:
    op.drop_index("ix_slideshows_status", table_name="slideshows")
    op.drop_table("slideshows")
