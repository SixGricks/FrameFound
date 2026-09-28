"""Listing file naming: gallery order, or by place

A property gallery's files are named for the order MLS shows them in
(`01-kitchen-…`). A showcase — GELCO's calendar shortlist, every course's
best eight in one listing — is browsed by place, so its files lead with
the course (`ledgerock-03-fall-drone`) and a folder of them sorts by
course in Lightroom. The listing records which it is.

Revision ID: 0022
Revises: 0021
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "listings",
        sa.Column("file_naming", sa.String(length=12), nullable=False, server_default="gallery"),
    )


def downgrade() -> None:
    op.drop_column("listings", "file_naming")
