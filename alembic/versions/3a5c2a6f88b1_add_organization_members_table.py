"""add organization members table

Revision ID: 3a5c2a6f88b1
Revises: 4f0a7a5f2f52
Create Date: 2025-12-22 04:54:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM

# revision identifiers, used by Alembic.
revision: str = "3a5c2a6f88b1"
down_revision: str | Sequence[str] | None = "4f0a7a5f2f52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create organization_members table."""
    # Use PostgreSQL ENUM explicitly so create_type=False is honored.
    # This prevents SQLAlchemy from trying to re-create the enum during CREATE TABLE.
    role_enum = PG_ENUM(
        "owner",
        "admin",
        "member",
        "viewer",
        name="organizationrole",
        create_type=False,
    )
    role_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "organization_members",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", role_enum, nullable=False, server_default=sa.text("'member'")),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(
        "ix_organization_members_org_user_unique",
        "organization_members",
        ["organization_id", "user_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_organization_members_organization_id"),
        "organization_members",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_organization_members_user_id"),
        "organization_members",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop organization_members table."""
    op.drop_index(op.f("ix_organization_members_user_id"), table_name="organization_members")
    op.drop_index(
        op.f("ix_organization_members_organization_id"), table_name="organization_members"
    )
    op.drop_index("ix_organization_members_org_user_unique", table_name="organization_members")
    op.drop_table("organization_members")

    role_enum = PG_ENUM(
        "owner",
        "admin",
        "member",
        "viewer",
        name="organizationrole",
        create_type=False,
    )
    role_enum.drop(op.get_bind(), checkfirst=True)
