"""Add inter-agent message bus table.

Revision ID: 0013
Revises: 0012
Create Date: 2025-01-14

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create inter_agent_messages table for agent-to-agent communication."""
    op.create_table(
        "inter_agent_messages",
        # Primary key
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        # Sender/receiver
        sa.Column("from_agent_id", sa.String(64), nullable=False),
        sa.Column("to_agent_id", sa.String(64), nullable=True),
        # Message content
        sa.Column("message_type", sa.String(32), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        # Response tracking
        sa.Column("response_to_id", sa.UUID(), nullable=True),
        sa.Column("requires_response", sa.Boolean(), nullable=False, server_default="false"),
        # Priority and context
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "context",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="'{}'::jsonb",
        ),
        # Delivery tracking
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.Column("responded_at", sa.DateTime(), nullable=True),
        # Timestamps
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        # Constraints
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["response_to_id"], ["inter_agent_messages.id"]),
    )

    # Indexes for common query patterns
    op.create_index("ix_inter_agent_messages_org", "inter_agent_messages", ["organization_id"])
    op.create_index("ix_inter_agent_messages_from", "inter_agent_messages", ["from_agent_id"])
    op.create_index("ix_inter_agent_messages_type", "inter_agent_messages", ["message_type"])
    op.create_index(
        "ix_inter_agent_to_agent", "inter_agent_messages", ["to_agent_id", "read_at"]
    )
    op.create_index("ix_inter_agent_response", "inter_agent_messages", ["response_to_id"])
    op.create_index(
        "ix_inter_agent_org_created", "inter_agent_messages", ["organization_id", "created_at"]
    )


def downgrade() -> None:
    """Drop inter_agent_messages table."""
    op.drop_index("ix_inter_agent_org_created", table_name="inter_agent_messages")
    op.drop_index("ix_inter_agent_response", table_name="inter_agent_messages")
    op.drop_index("ix_inter_agent_to_agent", table_name="inter_agent_messages")
    op.drop_index("ix_inter_agent_messages_type", table_name="inter_agent_messages")
    op.drop_index("ix_inter_agent_messages_from", table_name="inter_agent_messages")
    op.drop_index("ix_inter_agent_messages_org", table_name="inter_agent_messages")
    op.drop_table("inter_agent_messages")
