"""Local AI models: the models added, who has which role, and the work done for them

`local_models` is one row per build of a model from Hugging Face: the
repository at one commit, what it was built from as read when it was added
(approval is judged again from that each time the model is used), the files
that are downloaded and checked, and how much memory it needs. The name is
also kept casefolded and that is the unique key, because Hugging Face tells
capitals apart and neither the Mac's disk nor a network share does. No path
is stored: where the files are is worked out from the storage settings, so
moving the model library changes no row.

`local_ai_roles` is at most two rows, the Looker and the Editor: the model
that has the role, and the one that gets it once its files have been copied
to this machine.

`local_ai_jobs` is the work itself: downloads, copies, runtime installs,
tests, and whatever later phases ask of a model. The queue only carries a
wake-up; progress, cancel and "waiting until tonight" live in the row. A job
that asked a model records which model, build, runtime and prompt answered,
as text.

Nothing in the catalogue points at these tables, so removing a model never
touches a tag or a cut.

Additive only: no existing table changes.

Revision ID: 0032
Revises: 0031
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "local_models",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("repo_id", sa.String(200), nullable=False),
        sa.Column("repo_key", sa.String(200), nullable=False),
        sa.Column("revision", sa.String(64), nullable=False),
        sa.Column("variant", sa.String(100), nullable=False, server_default=""),
        sa.Column("publisher", sa.String(100), nullable=False),
        sa.Column("chain", sa.JSON(), nullable=False),
        sa.Column("licence", sa.String(100), nullable=False, server_default=""),
        sa.Column("licence_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("licence_url", sa.String(500), nullable=True),
        sa.Column("gated", sa.String(10), nullable=False, server_default=""),
        sa.Column("runtime", sa.String(30), nullable=False),
        sa.Column("takes", sa.JSON(), nullable=False),
        sa.Column("files", sa.JSON(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("memory_gb", sa.Float(), nullable=False, server_default="0"),
        sa.Column("measured_memory_gb", sa.Float(), nullable=True),
        sa.Column("state", sa.String(12), nullable=False, server_default="downloading"),
        sa.Column("error", sa.String(500), nullable=True),
        sa.Column("last_loaded_with", sa.String(60), nullable=True),
        sa.Column("hub_modified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "added_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("repo_key", "revision", "variant", name="uq_local_models_build"),
    )

    op.create_table(
        "local_ai_roles",
        sa.Column("role", sa.String(20), primary_key=True),
        sa.Column(
            "model_id",
            sa.Uuid(),
            sa.ForeignKey("local_models.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "pending_model_id",
            sa.Uuid(),
            sa.ForeignKey("local_models.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "assigned_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
    )

    op.create_table(
        "local_ai_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("subject", sa.String(300), nullable=False, server_default=""),
        sa.Column(
            "model_id",
            sa.Uuid(),
            sa.ForeignKey("local_models.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("role", sa.String(20), nullable=True),
        sa.Column("priority", sa.SmallInteger(), nullable=False, server_default="5"),
        sa.Column("state", sa.String(12), nullable=False, server_default="queued"),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("phase", sa.String(20), nullable=False, server_default=""),
        sa.Column("done", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("total", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("message", sa.String(500), nullable=False, server_default=""),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("model", sa.String(200), nullable=True),
        sa.Column("model_revision", sa.String(64), nullable=True),
        sa.Column("runtime", sa.String(30), nullable=True),
        sa.Column("runtime_version", sa.String(40), nullable=True),
        sa.Column("prompt_version", sa.String(40), nullable=True),
        sa.Column("seconds", sa.Float(), nullable=True),
        sa.Column("memory_gb", sa.Float(), nullable=True),
        sa.Column(
            "requested_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("beat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_local_ai_jobs_state", "local_ai_jobs", ["state", "priority", "created_at"])
    op.create_index("ix_local_ai_jobs_model_id", "local_ai_jobs", ["model_id"])


def downgrade() -> None:
    # Jobs and roles point at models, so they go first.
    op.drop_index("ix_local_ai_jobs_model_id", table_name="local_ai_jobs")
    op.drop_index("ix_local_ai_jobs_state", table_name="local_ai_jobs")
    op.drop_table("local_ai_jobs")
    op.drop_table("local_ai_roles")
    op.drop_table("local_models")
