"""add org invitations table

Revision ID: 57e3f7d2f8c1
Revises: 2b8f6b4c6d3a
Create Date: 2025-12-22 05:30:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM

# revision identifiers, used by Alembic.
revision: str = "57e3f7d2f8c1"
down_revision: str | Sequence[str] | None = "2b8f6b4c6d3a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create organization_invitations table."""
    op.create_table(
        "organization_invitations",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("invited_email", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column(
            "invited_role",
            PG_ENUM(
                "owner",
                "admin",
                "member",
                "viewer",
                name="organizationrole",
                create_type=False,
            ),
            nullable=False,
            server_default=sa.text("'member'"),
        ),
        sa.Column("token", sqlmodel.sql.sqltypes.AutoString(length=96), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("accepted_at", sa.DateTime(), nullable=True),
        sa.Column("accepted_by_user_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["accepted_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token"),
    )
    op.create_index(
        op.f("ix_organization_invitations_organization_id"),
        "organization_invitations",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_organization_invitations_invited_email"),
        "organization_invitations",
        ["invited_email"],
        unique=False,
    )
    op.create_index(
        op.f("ix_organization_invitations_token"),
        "organization_invitations",
        ["token"],
        unique=False,
    )
    op.create_index(
        op.f("ix_organization_invitations_created_by_user_id"),
        "organization_invitations",
        ["created_by_user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_organization_invitations_accepted_by_user_id"),
        "organization_invitations",
        ["accepted_by_user_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop organization_invitations table."""
    op.drop_index(
        op.f("ix_organization_invitations_accepted_by_user_id"),
        table_name="organization_invitations",
    )
    op.drop_index(
        op.f("ix_organization_invitations_created_by_user_id"),
        table_name="organization_invitations",
    )
    op.drop_index(op.f("ix_organization_invitations_token"), table_name="organization_invitations")
    op.drop_index(
        op.f("ix_organization_invitations_invited_email"),
        table_name="organization_invitations",
    )
    op.drop_index(
        op.f("ix_organization_invitations_organization_id"),
        table_name="organization_invitations",
    )
    op.drop_table("organization_invitations")
