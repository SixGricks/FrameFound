"""Listing items that are a frame of a video

Five of GELCO's thirteen calendar courses have no finished-work stills —
their finished work was filmed, in 4K, not photographed. A listing item can
now be a frame of a video: the video and a time. Lightroom's import gets a
full-size grab of that frame, made from the original by ffmpeg on the
server (the original is only read).

Revision ID: 0023
Revises: 0022
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("listing_items", sa.Column("frame_ms", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("listing_items", "frame_ms")
