"""Shot intelligence: per-window motion, light and framing; cut history

`shot_windows` holds what the motion analysis found in each video — the
stretches where the camera keeps doing one thing, with the move, speed,
smoothness at each playback speed, subject energy, and (from the
descriptor pass) light, framing and subjects. Rebuilt whole when a clip is
re-analysed; the motion track itself is a derived file, kind "motion".

`cuts` and `cut_pieces` record every rough cut built, with a snapshot of
each piece's shot, so the next video can be different on purpose.

Additive only: no existing table changes.

Revision ID: 0028
Revises: 0027
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "shot_windows",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "asset_id",
            sa.Uuid(),
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idx", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("move", sa.String(20), nullable=False),
        sa.Column("direction", sa.String(10), nullable=True),
        sa.Column("speed", sa.Float(), nullable=False, server_default="0"),
        sa.Column("smooth", sa.Float(), nullable=False, server_default="0"),
        sa.Column("smooth_by_speed", sa.JSON(), nullable=False),
        sa.Column("energy", sa.String(8), nullable=False, server_default="low"),
        sa.Column("energy_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("source_fps", sa.Float(), nullable=True),
        sa.Column("features", sa.JSON(), nullable=False),
        sa.Column("sun_elevation", sa.Float(), nullable=True),
        sa.Column("light", sa.String(12), nullable=True),
        sa.Column("look", sa.JSON(), nullable=False),
        sa.Column("framing", sa.JSON(), nullable=False),
        sa.Column("subjects", sa.JSON(), nullable=False),
        sa.Column("analysis_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("described_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("asset_id", "idx", name="uq_shot_windows_asset_idx"),
    )
    op.create_index("ix_shot_windows_asset_id", "shot_windows", ["asset_id"])
    op.create_index("ix_shot_windows_move", "shot_windows", ["move"])
    op.create_index("ix_shot_windows_light", "shot_windows", ["light"])

    op.create_table(
        "cuts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("title", sa.String(300), nullable=False, server_default=""),
        sa.Column("client", sa.String(200), nullable=False, server_default=""),
        sa.Column(
            "library_id",
            sa.Uuid(),
            sa.ForeignKey("libraries.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source", sa.String(20), nullable=False, server_default="mcp"),
        sa.Column("profile", sa.String(100), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=False, server_default="0"),
        sa.Column("piece_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_cuts_client", "cuts", ["client"])
    op.create_index("ix_cuts_library_id", "cuts", ["library_id"])
    op.create_index("ix_cuts_created_at", "cuts", ["created_at"])

    op.create_table(
        "cut_pieces",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "cut_id", sa.Uuid(), sa.ForeignKey("cuts.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "asset_id", sa.Uuid(), sa.ForeignKey("assets.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("filename", sa.String(512), nullable=False, server_default=""),
        sa.Column("in_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("out_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("speed", sa.Float(), nullable=False, server_default="1"),
        sa.Column("shot", sa.JSON(), nullable=False),
    )
    op.create_index("ix_cut_pieces_cut_id", "cut_pieces", ["cut_id"])
    op.create_index("ix_cut_pieces_asset_id", "cut_pieces", ["asset_id"])


def downgrade() -> None:
    op.drop_index("ix_cut_pieces_asset_id", table_name="cut_pieces")
    op.drop_index("ix_cut_pieces_cut_id", table_name="cut_pieces")
    op.drop_table("cut_pieces")
    op.drop_index("ix_cuts_created_at", table_name="cuts")
    op.drop_index("ix_cuts_library_id", table_name="cuts")
    op.drop_index("ix_cuts_client", table_name="cuts")
    op.drop_table("cuts")
    op.drop_index("ix_shot_windows_light", table_name="shot_windows")
    op.drop_index("ix_shot_windows_move", table_name="shot_windows")
    op.drop_index("ix_shot_windows_asset_id", table_name="shot_windows")
    op.drop_table("shot_windows")
