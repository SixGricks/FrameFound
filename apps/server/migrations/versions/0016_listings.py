"""Listings: a property shoot ordered and named for upload

A listing is a small, operator-curated set with per-item state (room label,
position), which is why it gets a join table rather than the JSON id list
slideshows use: labels and positions are edited one at a time, and an UPDATE
to one item must not rewrite the whole selection.

Revision ID: 0016
Revises: 0015
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "listings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("export_status", sa.String(length=20), nullable=False, server_default="none"),
        sa.Column("export_relpath", sa.String(length=1024), nullable=True),
        sa.Column("export_error", sa.String(length=500), nullable=True),
        sa.Column("exported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.create_table(
        "listing_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "listing_id",
            sa.Uuid(),
            sa.ForeignKey("listings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "asset_id",
            sa.Uuid(),
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("room", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("room_source", sa.String(length=16), nullable=False, server_default="suggested"),
        sa.Column("room_score", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.UniqueConstraint("listing_id", "asset_id", name="uq_listing_items_listing_asset"),
    )
    op.create_index("ix_listing_items_listing_id", "listing_items", ["listing_id"])
    op.create_index("ix_listing_items_asset_id", "listing_items", ["asset_id"])


def downgrade() -> None:
    op.drop_index("ix_listing_items_asset_id", table_name="listing_items")
    op.drop_index("ix_listing_items_listing_id", table_name="listing_items")
    op.drop_table("listing_items")
    op.drop_table("listings")
