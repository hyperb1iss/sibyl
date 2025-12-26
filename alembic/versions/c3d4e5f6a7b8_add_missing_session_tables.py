"""Add missing session and password reset tables

Revision ID: c3d4e5f6a7b8
Revises: 6bca5ea1bf2a
Create Date: 2025-12-26 12:00:00.000000

Creates tables that exist in models but were never migrated:
- user_sessions: For JWT session tracking and revocation
- password_reset_tokens: For password reset flow
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: str | Sequence[str] | None = "6bca5ea1bf2a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(conn, table_name: str) -> bool:
    """Check if a table exists in the database."""
    result = conn.execute(
        text("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = :name)"),
        {"name": table_name},
    )
    return bool(result.scalar())


def upgrade() -> None:
    """Create user_sessions and password_reset_tokens tables if they don't exist."""
    conn = op.get_bind()

    # Create user_sessions table
    if not _table_exists(conn, "user_sessions"):
        op.create_table(
            "user_sessions",
            sa.Column("id", sa.UUID(), nullable=False),
            sa.Column("user_id", sa.UUID(), nullable=False),
            sa.Column("organization_id", sa.UUID(), nullable=True),
            sa.Column("token_hash", sa.String(length=128), nullable=False),
            sa.Column("refresh_token_hash", sa.String(length=128), nullable=True),
            sa.Column("refresh_token_expires_at", sa.DateTime(), nullable=True),
            sa.Column("device_name", sa.String(length=255), nullable=True),
            sa.Column("device_type", sa.String(length=64), nullable=True),
            sa.Column("browser", sa.String(length=128), nullable=True),
            sa.Column("os", sa.String(length=128), nullable=True),
            sa.Column("ip_address", sa.String(length=64), nullable=True),
            sa.Column("user_agent", sa.String(length=512), nullable=True),
            sa.Column("location", sa.String(length=255), nullable=True),
            sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("last_active_at", sa.DateTime(), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_user_sessions_user_id", ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], name="fk_user_sessions_organization_id", ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("token_hash", name="user_sessions_token_hash_key"),
        )
        op.create_index(op.f("ix_user_sessions_user_id"), "user_sessions", ["user_id"], unique=False)
        op.create_index(op.f("ix_user_sessions_organization_id"), "user_sessions", ["organization_id"], unique=False)
        op.create_index(op.f("ix_user_sessions_token_hash"), "user_sessions", ["token_hash"], unique=False)
        op.create_index(op.f("ix_user_sessions_refresh_token_hash"), "user_sessions", ["refresh_token_hash"], unique=False)
        op.create_index(op.f("ix_user_sessions_expires_at"), "user_sessions", ["expires_at"], unique=False)

    # Create password_reset_tokens table
    if not _table_exists(conn, "password_reset_tokens"):
        op.create_table(
            "password_reset_tokens",
            sa.Column("id", sa.UUID(), nullable=False),
            sa.Column("user_id", sa.UUID(), nullable=False),
            sa.Column("token_hash", sa.String(length=128), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("used_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_password_reset_tokens_user_id", ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("token_hash", name="password_reset_tokens_token_hash_key"),
        )
        op.create_index(op.f("ix_password_reset_tokens_user_id"), "password_reset_tokens", ["user_id"], unique=False)
        op.create_index(op.f("ix_password_reset_tokens_token_hash"), "password_reset_tokens", ["token_hash"], unique=False)
        op.create_index(op.f("ix_password_reset_tokens_expires_at"), "password_reset_tokens", ["expires_at"], unique=False)


def downgrade() -> None:
    """Drop user_sessions and password_reset_tokens tables."""
    conn = op.get_bind()

    if _table_exists(conn, "password_reset_tokens"):
        op.drop_index(op.f("ix_password_reset_tokens_expires_at"), table_name="password_reset_tokens")
        op.drop_index(op.f("ix_password_reset_tokens_token_hash"), table_name="password_reset_tokens")
        op.drop_index(op.f("ix_password_reset_tokens_user_id"), table_name="password_reset_tokens")
        op.drop_table("password_reset_tokens")

    if _table_exists(conn, "user_sessions"):
        op.drop_index(op.f("ix_user_sessions_expires_at"), table_name="user_sessions")
        op.drop_index(op.f("ix_user_sessions_refresh_token_hash"), table_name="user_sessions")
        op.drop_index(op.f("ix_user_sessions_token_hash"), table_name="user_sessions")
        op.drop_index(op.f("ix_user_sessions_organization_id"), table_name="user_sessions")
        op.drop_index(op.f("ix_user_sessions_user_id"), table_name="user_sessions")
        op.drop_table("user_sessions")
