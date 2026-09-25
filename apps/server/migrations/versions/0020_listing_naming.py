"""Listing naming: captions, file-name slugs and the delivery package

Each photograph in a listing gains a caption ("what it shows") and a slug for
its file name, suggested by the AI and confirmed by the operator — the same
contract as room labels. The listing gains a file-name suffix (the address and
sale type, "130-davis-rd-auction") and free-text notes that head the photo
index. Together they let the export produce the package the operator used to
assemble by hand before each paid editing batch:
`01-front-exterior-brick-colonial-130-davis-rd-auction.jpg`, a Photo Index and
contact sheets.

Server defaults of "" so existing rows read as "not named yet" rather than
NULL, which every reader would otherwise have to special-case.

Revision ID: 0020
Revises: 0019
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "listing_items",
        sa.Column("caption", sa.String(length=300), nullable=False, server_default=""),
    )
    op.add_column(
        "listing_items",
        sa.Column("slug", sa.String(length=80), nullable=False, server_default=""),
    )
    op.add_column(
        "listing_items",
        sa.Column("naming_source", sa.String(length=16), nullable=False, server_default=""),
    )
    # When the AI last named it: how the UI tells a naming run's progress
    # from names that were already there (edited_at does the same for edits).
    op.add_column(
        "listing_items",
        sa.Column("named_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "listings",
        sa.Column("file_suffix", sa.String(length=120), nullable=False, server_default=""),
    )
    op.add_column(
        "listings",
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("listings", "notes")
    op.drop_column("listings", "file_suffix")
    op.drop_column("listing_items", "named_at")
    op.drop_column("listing_items", "naming_source")
    op.drop_column("listing_items", "slug")
    op.drop_column("listing_items", "caption")
