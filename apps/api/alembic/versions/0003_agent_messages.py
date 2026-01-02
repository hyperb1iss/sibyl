"""Add agent_messages table for chat history persistence.

Revision ID: 0003_agent_messages
Revises: 0002_api_key_scopes_and_expiry
Create Date: 2026-01-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003_agent_messages"
down_revision: str | None = "0002_api_key_scopes_and_expiry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create enum types
    op.execute("CREATE TYPE agentmessagerole AS ENUM ('agent', 'user', 'system')")
    op.execute("CREATE TYPE agentmessagetype AS ENUM ('text', 'tool_call', 'tool_result', 'error')")

    op.create_table(
        "agent_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", sa.String(64), nullable=False, index=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("message_num", sa.Integer(), nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM("agent", "user", "system", name="agentmessagerole", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "type",
            postgresql.ENUM(
                "text",
                "tool_call",
                "tool_result",
                "error",
                name="agentmessagetype",
                create_type=False,
            ),
            nullable=False,
            server_default="text",
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "extra",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("now()"),
            index=True,
        ),
    )

    # Composite index for efficient retrieval by agent + ordering
    op.create_index(
        "ix_agent_messages_agent_num",
        "agent_messages",
        ["agent_id", "message_num"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_messages_agent_num", table_name="agent_messages")
    op.drop_table("agent_messages")
    op.execute("DROP TYPE agentmessagetype")
    op.execute("DROP TYPE agentmessagerole")
