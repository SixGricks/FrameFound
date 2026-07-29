"""Geocode cache

Reverse-geocoding results survive restarts because every lookup is billable
and place clusters are recomputed on demand.

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "geocode_cache",
        sa.Column("cache_key", sa.String(length=40), primary_key=True),
        sa.Column("address", sa.String(length=300), nullable=False, server_default=""),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("geocode_cache")
