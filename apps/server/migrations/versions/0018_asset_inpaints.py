"""Asset inpaints: object removal results, versioned like edits

Unlike a develop recipe, an inpaint cannot be re-derived cheaply — LaMa is
tens of seconds per region on these CPUs — so the *result* is stored as a
file in the data directory and the row records which mask produced it.
Versions chain: each removal runs on the previous result, and undo deletes
the newest row and its file. The original stays untouched on its read-only
mount, as ever.

Revision ID: 0018
Revises: 0017
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "asset_inpaints",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "asset_id",
            sa.Uuid(),
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        # The brush mask, normalised 0-1 bbox plus the PNG the operator drew,
        # kept for provenance: which pixels were invented, and when.
        sa.Column("mask_meta", sa.JSON(), nullable=False),
        # queued | running | ready | failed
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("relative_path", sa.String(length=1024), nullable=True),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.UniqueConstraint("asset_id", "version", name="uq_asset_inpaints_asset_version"),
    )
    op.create_index("ix_asset_inpaints_asset_id", "asset_inpaints", ["asset_id"])


def downgrade() -> None:
    op.drop_index("ix_asset_inpaints_asset_id", table_name="asset_inpaints")
    op.drop_table("asset_inpaints")
