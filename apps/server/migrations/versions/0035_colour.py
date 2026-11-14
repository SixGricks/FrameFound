"""Colour: a look per clip, and a local model's proposals for a listing

`clip_looks` holds at most two looks per clip: the one a local model
suggested (written only by the `local_ai` lane's job) and the one a person
chose (written only by the API), so neither can overwrite the other. A look
is the sliders a LUT can carry plus the white balance and levels frozen on
one still; the `.cube` file is built from them when asked for and never
stored. Keyed on the asset, never on a shot window, which a re-analysis
deletes.

`listing_edit_suggestions` holds what a local model proposes for each
photograph of a listing (straightening, whether the sky is worth replacing,
a caption and a file name) until somebody applies or dismisses it. Asking
writes only these rows; applying writes edit versions as auto-edit always
has, and these rows record what it was applied from.

Which build answered is kept as text on both, so removing a model changes
nothing it once said.

Additive only: no existing table changes.

Revision ID: 0035
Revises: 0034
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | None = None
depends_on: str | None = None


def _text(name: str, length: int) -> sa.Column[object]:
    return sa.Column(name, sa.String(length), nullable=False, server_default="")


def _provenance() -> list[sa.Column[object]]:
    """The names every table of answers records a build by, as text.

    `variant` is part of the build (as on `local_models`): two quantisations
    of one GGUF repository at one commit would otherwise be told apart by
    nothing. It is "" rather than NULL, as on `local_ai_comparison_models`.
    """
    return [
        sa.Column("model", sa.String(200), nullable=True),
        sa.Column("model_revision", sa.String(64), nullable=True),
        sa.Column("variant", sa.String(100), nullable=False, server_default=""),
        sa.Column("runtime", sa.String(30), nullable=True),
        sa.Column("runtime_version", sa.String(40), nullable=True),
        sa.Column("prompt_version", sa.String(40), nullable=True),
    ]


def _demo() -> sa.Column[object]:
    return sa.Column("demo", sa.Boolean(), nullable=False, server_default=sa.false())


def _decided_by() -> sa.Column[object]:
    return sa.Column(
        "decided_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


def _now(name: str) -> sa.Column[object]:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "clip_looks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "asset_id",
            sa.Uuid(),
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # suggested | chosen: always written, by the one writer of each.
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("source", sa.String(10), nullable=False, server_default="model"),
        sa.Column("recipe", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("measured", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("left_out", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("still_ts_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("features", sa.JSON(), nullable=False, server_default="[]"),
        _text("reason", 300),
        *_provenance(),
        _demo(),
        _decided_by(),
        _now("created_at"),
        _now("updated_at"),
        sa.UniqueConstraint("asset_id", "kind", name="uq_clip_looks_asset_kind"),
    )
    op.create_index("ix_clip_looks_asset_id", "clip_looks", ["asset_id"])
    # The video look learns from every chosen row.
    op.create_index("ix_clip_looks_kind", "clip_looks", ["kind"])

    op.create_table(
        "listing_edit_suggestions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "listing_id",
            sa.Uuid(),
            sa.ForeignKey("listings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "item_id",
            sa.Uuid(),
            sa.ForeignKey("listing_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "asset_id",
            sa.Uuid(),
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rotate", sa.Float(), nullable=False, server_default="0"),
        sa.Column("keystone", sa.Float(), nullable=False, server_default="0"),
        sa.Column("sky_worth_replacing", sa.Boolean(), nullable=False, server_default=sa.false()),
        _text("caption", 300),
        _text("slug", 80),
        _text("note", 300),
        sa.Column("state", sa.String(12), nullable=False, server_default="proposed"),
        sa.Column("applied_version", sa.Integer(), nullable=True),
        *_provenance(),
        _demo(),
        _now("created_at"),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        _decided_by(),
        # One per photograph: asking again replaces it.
        sa.UniqueConstraint("item_id", name="uq_listing_edit_suggestions_item_id"),
    )
    op.create_index(
        "ix_listing_edit_suggestions_listing_id", "listing_edit_suggestions", ["listing_id"]
    )
    # Deleting an asset has to find its rows here; without this, a scan of
    # the table per asset on PostgreSQL.
    op.create_index(
        "ix_listing_edit_suggestions_asset_id", "listing_edit_suggestions", ["asset_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_listing_edit_suggestions_asset_id", table_name="listing_edit_suggestions")
    op.drop_index("ix_listing_edit_suggestions_listing_id", table_name="listing_edit_suggestions")
    op.drop_table("listing_edit_suggestions")

    op.drop_index("ix_clip_looks_kind", table_name="clip_looks")
    op.drop_index("ix_clip_looks_asset_id", table_name="clip_looks")
    op.drop_table("clip_looks")
