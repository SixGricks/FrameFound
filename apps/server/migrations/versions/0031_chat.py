"""The chat panel: conversations with Claude, their turns and tool calls

`chat_conversations` is one conversation in the web panel, private to the
user who started it, with the model it was started on, what it has cost,
and whether a turn is running or waiting for a yes or no.

`chat_messages` holds each turn exactly as the Claude API has it (the
content-block array, as JSON, not JSONB: the API rejects a conversation
whose earlier turns come back changed, and JSONB reorders keys). Pictures
are not in the rows; they are files beside the conversation.

`chat_tool_calls` records every tool Claude asked for, and for the ones
that change the catalogue or spend money, who said yes or no and when.

Additive only: no existing table changes.

Revision ID: 0031
Revises: 0030
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "chat_conversations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("title", sa.String(200), nullable=False, server_default=""),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("pictures", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("prefix_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("state", sa.String(24), nullable=False, server_default="idle"),
        sa.Column("state_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("turn_reason", sa.String(24), nullable=True),
        sa.Column("turn_message", sa.String(500), nullable=True),
        sa.Column("spent_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_chat_conversations_user_id", "chat_conversations", ["user_id"])
    op.create_index("ix_chat_conversations_updated_at", "chat_conversations", ["updated_at"])

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.Uuid(),
            sa.ForeignKey("chat_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(10), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("context", sa.JSON(), nullable=True),
        sa.Column("usage", sa.JSON(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("stop_reason", sa.String(30), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("conversation_id", "seq", name="uq_chat_messages_conversation_seq"),
    )
    op.create_index("ix_chat_messages_conversation_id", "chat_messages", ["conversation_id"])

    op.create_table(
        "chat_tool_calls",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.Uuid(),
            sa.ForeignKey("chat_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "message_id",
            sa.Uuid(),
            sa.ForeignKey("chat_messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tool_use_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("input", sa.JSON(), nullable=False),
        sa.Column("needs_confirm", sa.Boolean(), nullable=False, server_default=sa.false()),
        # No default: `pending` means "waiting for the operator's yes", and
        # a row that did not say its state must not read as that.
        sa.Column("state", sa.String(12), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("artifact_name", sa.String(200), nullable=True),
        sa.Column("artifact_path", sa.String(1024), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "decided_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
    )
    op.create_index("ix_chat_tool_calls_conversation_id", "chat_tool_calls", ["conversation_id"])
    op.create_index("ix_chat_tool_calls_message_id", "chat_tool_calls", ["message_id"])


def downgrade() -> None:
    op.drop_index("ix_chat_tool_calls_message_id", table_name="chat_tool_calls")
    op.drop_index("ix_chat_tool_calls_conversation_id", table_name="chat_tool_calls")
    op.drop_table("chat_tool_calls")
    op.drop_index("ix_chat_messages_conversation_id", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index("ix_chat_conversations_updated_at", table_name="chat_conversations")
    op.drop_index("ix_chat_conversations_user_id", table_name="chat_conversations")
    op.drop_table("chat_conversations")
