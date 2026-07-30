"""Face recognition: people and faces

Person grouping with operator-supplied names. Off switch lives in app_settings,
not here, so disabling recognition never means losing the names already given.

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa
from alembic import op

from framefound.db.vector_type import Embedding

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "people",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("slug", sa.String(length=140), nullable=False, server_default=""),
        sa.Column("prototype", Embedding(), nullable=True),
        sa.Column("threshold", sa.Float(), nullable=False, server_default="0.42"),
        sa.Column("face_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cover_face_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_people_slug", "people", ["slug"])

    op.create_table(
        "faces",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "frame_id",
            sa.Uuid(),
            sa.ForeignKey("frames.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "asset_id",
            sa.Uuid(),
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "person_id",
            sa.Uuid(),
            sa.ForeignKey("people.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("box_x", sa.Float(), nullable=False),
        sa.Column("box_y", sa.Float(), nullable=False),
        sa.Column("box_w", sa.Float(), nullable=False),
        sa.Column("box_h", sa.Float(), nullable=False),
        sa.Column("detection_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("embedding", Embedding(), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="detected"),
        sa.Column("similarity", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_faces_frame_id", "faces", ["frame_id"])
    op.create_index("ix_faces_asset_id", "faces", ["asset_id"])
    op.create_index("ix_faces_person_id", "faces", ["person_id"])
    op.create_index("ix_faces_source", "faces", ["source"])


def downgrade() -> None:
    op.drop_table("faces")
    op.drop_index("ix_people_slug", table_name="people")
    op.drop_table("people")
