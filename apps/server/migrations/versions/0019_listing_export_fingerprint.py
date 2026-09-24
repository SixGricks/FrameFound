"""Listing export fingerprint: know when a zip no longer matches its listing

The export records a digest of its inputs (gallery order, room labels,
recipes, object-removal rounds); the listing is re-digested on read, and a
mismatch marks the zip stale instead of serving it. Nullable: exports made
before this revision have no fingerprint and are treated as stale, which is
the safe reading of "we cannot tell".

Revision ID: 0019
Revises: 0018
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("listings", sa.Column("export_fingerprint", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("listings", "export_fingerprint")
