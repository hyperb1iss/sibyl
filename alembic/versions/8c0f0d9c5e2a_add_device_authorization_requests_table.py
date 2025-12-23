"""add device authorization requests table

Revision ID: 8c0f0d9c5e2a
Revises: 57e3f7d2f8c1
Create Date: 2025-12-22 23:40:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8c0f0d9c5e2a"
down_revision: str | Sequence[str] | None = "57e3f7d2f8c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create device_authorization_requests table."""
    op.create_table(
        "device_authorization_requests",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("device_code_hash", sa.String(length=64), nullable=False),
        sa.Column("user_code", sa.String(length=16), nullable=False),
        sa.Column("client_name", sa.String(length=255), nullable=True),
        sa.Column("scope", sa.String(length=255), nullable=False, server_default=sa.text("'mcp'")),
        sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("poll_interval_seconds", sa.Integer(), nullable=False, server_default=sa.text("5")),
        sa.Column("last_polled_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("denied_at", sa.DateTime(), nullable=True),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("device_code_hash"),
        sa.UniqueConstraint("user_code"),
    )

    op.create_index(
        op.f("ix_device_authorization_requests_device_code_hash"),
        "device_authorization_requests",
        ["device_code_hash"],
        unique=False,
    )
    op.create_index(
        op.f("ix_device_authorization_requests_user_code"),
        "device_authorization_requests",
        ["user_code"],
        unique=False,
    )
    op.create_index(
        op.f("ix_device_authorization_requests_status"),
        "device_authorization_requests",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_device_authorization_requests_expires_at"),
        "device_authorization_requests",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop device_authorization_requests table."""
    op.drop_index(op.f("ix_device_authorization_requests_expires_at"), table_name="device_authorization_requests")
    op.drop_index(op.f("ix_device_authorization_requests_status"), table_name="device_authorization_requests")
    op.drop_index(op.f("ix_device_authorization_requests_user_code"), table_name="device_authorization_requests")
    op.drop_index(
        op.f("ix_device_authorization_requests_device_code_hash"),
        table_name="device_authorization_requests",
    )
    op.drop_table("device_authorization_requests")

