"""add organizations table

Revision ID: 4f0a7a5f2f52
Revises: 7c0f7a35f49a
Create Date: 2025-12-22 04:53:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
import sqlmodel.sql.sqltypes
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4f0a7a5f2f52"
down_revision: str | Sequence[str] | None = "7c0f7a35f49a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create organizations table."""
    op.create_table(
        "organizations",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column("slug", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("is_personal", sa.Boolean(), nullable=False),
        sa.Column(
            "settings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("graph_name", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index(op.f("ix_organizations_slug"), "organizations", ["slug"], unique=False)


def downgrade() -> None:
    """Drop organizations table."""
    op.drop_index(op.f("ix_organizations_slug"), table_name="organizations")
    op.drop_table("organizations")

