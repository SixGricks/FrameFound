"""Location provenance: where a position came from and how sure we are.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "assets",
        # exif | inferred | manual — an inferred position must never be
        # mistaken for one the camera actually recorded.
        sa.Column("gps_source", sa.String(length=20), nullable=True),
    )
    op.add_column("assets", sa.Column("gps_confidence", sa.Float(), nullable=True))
    op.add_column("assets", sa.Column("gps_inferred_from", sa.Uuid(), nullable=True))
    op.create_index("ix_assets_gps_lat", "assets", ["gps_lat"])
    op.create_index("ix_assets_gps_lon", "assets", ["gps_lon"])
    # Anything already carrying coordinates got them from EXIF.
    op.execute("UPDATE assets SET gps_source = 'exif' WHERE gps_lat IS NOT NULL")


def downgrade() -> None:
    op.drop_index("ix_assets_gps_lon", table_name="assets")
    op.drop_index("ix_assets_gps_lat", table_name="assets")
    op.drop_column("assets", "gps_inferred_from")
    op.drop_column("assets", "gps_confidence")
    op.drop_column("assets", "gps_source")
