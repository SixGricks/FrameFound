"""Comparing local models: a fixed sample, one column per model, its answers

`local_ai_comparisons` is one comparison: how the sample was chosen, the
vocabulary and question version every model is told, which cuts define
"used", when the ground truth was read, and the owner's decision.

`local_ai_comparison_items` is the sample, frozen: each photograph or shot
(an asset and a time span, never a shot-window id, which a re-analysis
deletes), the facts sentence the models are told, and the truth as it was
when the comparison was made. The pictures are files, not rows.

`local_ai_comparison_models` is one model's column and the state of its
turn; which build answered is kept as text, so removing the model keeps the
column. `local_ai_comparison_answers` is one model on one item, one row
each, which is what makes a restarted run carry on rather than ask twice.

Nothing in the catalogue points at these tables and nothing here writes to
the catalogue.

Additive only: no existing table changes.

Revision ID: 0033
Revises: 0032
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | None = None
depends_on: str | None = None


def _user(name: str) -> sa.Column[object]:
    return sa.Column(name, sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


def _created_at() -> sa.Column[object]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "local_ai_comparisons",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("source_kind", sa.String(12), nullable=False),
        sa.Column("source", sa.JSON(), nullable=False),
        sa.Column("source_label", sa.String(300), nullable=False, server_default=""),
        sa.Column("what", sa.String(8), nullable=False, server_default="both"),
        sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stills", sa.SmallInteger(), nullable=False, server_default="3"),
        sa.Column("seed", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("prompt_version", sa.String(40), nullable=False),
        sa.Column("vocabulary", sa.JSON(), nullable=False),
        sa.Column("counted_cuts", sa.JSON(), nullable=False),
        sa.Column("counts", sa.JSON(), nullable=False),
        sa.Column(
            "truth_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("verdict", sa.String(8), nullable=False, server_default=""),
        sa.Column("verdict_entry_id", sa.Uuid(), nullable=True),
        sa.Column("verdict_note", sa.Text(), nullable=False, server_default=""),
        sa.Column("verdict_at", sa.DateTime(timezone=True), nullable=True),
        _user("verdict_by"),
        _user("created_by"),
        _created_at(),
    )
    op.create_index("ix_local_ai_comparisons_created_at", "local_ai_comparisons", ["created_at"])

    op.create_table(
        "local_ai_comparison_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "comparison_id",
            sa.Uuid(),
            sa.ForeignKey("local_ai_comparisons.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column(
            "asset_id", sa.Uuid(), sa.ForeignKey("assets.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("library_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("relative_path", sa.String(2048), nullable=False, server_default=""),
        sa.Column("filename", sa.String(512), nullable=False, server_default=""),
        sa.Column("media_type", sa.String(10), nullable=False, server_default=""),
        sa.Column("start_ms", sa.Integer(), nullable=True),
        sa.Column("end_ms", sa.Integer(), nullable=True),
        sa.Column("facts", sa.JSON(), nullable=False),
        sa.Column("facts_line", sa.String(400), nullable=False, server_default=""),
        sa.Column("picture_ms", sa.JSON(), nullable=False),
        sa.Column("truth_yes", sa.JSON(), nullable=False),
        sa.Column("truth_no", sa.JSON(), nullable=False),
        sa.Column("used", sa.Boolean(), nullable=True),
        sa.Column("used_in", sa.JSON(), nullable=False),
        sa.UniqueConstraint(
            "comparison_id", "position", name="uq_local_ai_comparison_items_position"
        ),
    )
    op.create_index(
        "ix_local_ai_comparison_items_comparison_id",
        "local_ai_comparison_items",
        ["comparison_id"],
    )
    op.create_index(
        "ix_local_ai_comparison_items_asset_id", "local_ai_comparison_items", ["asset_id"]
    )

    op.create_table(
        "local_ai_comparison_models",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "comparison_id",
            sa.Uuid(),
            sa.ForeignKey("local_ai_comparisons.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "model_id",
            sa.Uuid(),
            sa.ForeignKey("local_models.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("model_revision", sa.String(64), nullable=False),
        sa.Column("variant", sa.String(100), nullable=False, server_default=""),
        sa.Column("model_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("runtime", sa.String(30), nullable=True),
        sa.Column("runtime_version", sa.String(40), nullable=True),
        sa.Column("prompt_version", sa.String(40), nullable=False),
        sa.Column("state", sa.String(12), nullable=False, server_default="queued"),
        sa.Column("message", sa.String(500), nullable=False, server_default=""),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("load_seconds", sa.Float(), nullable=True),
        sa.Column("ran_from", sa.String(14), nullable=True),
        sa.Column("memory_gb", sa.Float(), nullable=True),
        sa.Column("memory_source", sa.String(10), nullable=True),
        _user("added_by"),
        _created_at(),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "comparison_id",
            "model",
            "model_revision",
            "variant",
            name="uq_local_ai_comparison_models_build",
        ),
    )
    op.create_index(
        "ix_local_ai_comparison_models_comparison_id",
        "local_ai_comparison_models",
        ["comparison_id"],
    )
    op.create_index(
        "ix_local_ai_comparison_models_model_id", "local_ai_comparison_models", ["model_id"]
    )

    op.create_table(
        "local_ai_comparison_answers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "entry_id",
            sa.Uuid(),
            sa.ForeignKey("local_ai_comparison_models.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "item_id",
            sa.Uuid(),
            sa.ForeignKey("local_ai_comparison_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("problem", sa.String(20), nullable=True),
        sa.Column("message", sa.String(300), nullable=False, server_default=""),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("caption", sa.String(300), nullable=False, server_default=""),
        sa.Column("score", sa.SmallInteger(), nullable=True),
        sa.Column("reason", sa.String(300), nullable=False, server_default=""),
        sa.Column("raw", sa.Text(), nullable=True),
        sa.Column("seconds", sa.Float(), nullable=True),
        sa.Column("attempts", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("memory_gb", sa.Float(), nullable=True),
        _created_at(),
        sa.UniqueConstraint(
            "entry_id", "item_id", name="uq_local_ai_comparison_answers_entry_item"
        ),
    )
    op.create_index(
        "ix_local_ai_comparison_answers_entry_id", "local_ai_comparison_answers", ["entry_id"]
    )
    op.create_index(
        "ix_local_ai_comparison_answers_item_id", "local_ai_comparison_answers", ["item_id"]
    )


def downgrade() -> None:
    # Answers point at columns and items, which point at the comparison.
    op.drop_index(
        "ix_local_ai_comparison_answers_item_id", table_name="local_ai_comparison_answers"
    )
    op.drop_index(
        "ix_local_ai_comparison_answers_entry_id", table_name="local_ai_comparison_answers"
    )
    op.drop_table("local_ai_comparison_answers")
    op.drop_index("ix_local_ai_comparison_models_model_id", table_name="local_ai_comparison_models")
    op.drop_index(
        "ix_local_ai_comparison_models_comparison_id", table_name="local_ai_comparison_models"
    )
    op.drop_table("local_ai_comparison_models")
    op.drop_index("ix_local_ai_comparison_items_asset_id", table_name="local_ai_comparison_items")
    op.drop_index(
        "ix_local_ai_comparison_items_comparison_id", table_name="local_ai_comparison_items"
    )
    op.drop_table("local_ai_comparison_items")
    op.drop_index("ix_local_ai_comparisons_created_at", table_name="local_ai_comparisons")
    op.drop_table("local_ai_comparisons")
