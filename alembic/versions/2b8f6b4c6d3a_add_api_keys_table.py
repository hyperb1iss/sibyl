"""add api keys table

Revision ID: 2b8f6b4c6d3a
Revises: 3a5c2a6f88b1
Create Date: 2025-12-22 05:16:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2b8f6b4c6d3a"
down_revision: str | Sequence[str] | None = "3a5c2a6f88b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create api_keys table."""
    op.create_table(
        "api_keys",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column("key_prefix", sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False),
        sa.Column("key_salt", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("key_hash", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_api_keys_key_prefix"), "api_keys", ["key_prefix"], unique=False)
    op.create_index(
        op.f("ix_api_keys_organization_id"), "api_keys", ["organization_id"], unique=False
    )
    op.create_index(op.f("ix_api_keys_user_id"), "api_keys", ["user_id"], unique=False)


def downgrade() -> None:
    """Drop api_keys table."""
    op.drop_index(op.f("ix_api_keys_user_id"), table_name="api_keys")
    op.drop_index(op.f("ix_api_keys_organization_id"), table_name="api_keys")
    op.drop_index(op.f("ix_api_keys_key_prefix"), table_name="api_keys")
    op.drop_table("api_keys")

