"""Add organization_id to crawl_sources

Revision ID: 82eb1c16ee12
Revises: ae2c1a74e94a
Create Date: 2025-12-23 18:13:20.315159

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "82eb1c16ee12"
down_revision: str | Sequence[str] | None = "ae2c1a74e94a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Step 1: Add column as nullable first
    op.add_column("crawl_sources", sa.Column("organization_id", sa.Uuid(), nullable=True))

    # Step 2: Assign existing sources to the first organization (if any exist)
    # This handles data migration for existing crawl sources
    connection = op.get_bind()
    connection.execute(
        sa.text("""
            UPDATE crawl_sources
            SET organization_id = (
                SELECT id FROM organizations ORDER BY created_at ASC LIMIT 1
            )
            WHERE organization_id IS NULL
        """)
    )

    # Step 3: Make column NOT NULL after data migration
    op.alter_column("crawl_sources", "organization_id", nullable=False)

    # Step 4: Create index and foreign key
    op.create_index(
        op.f("ix_crawl_sources_organization_id"), "crawl_sources", ["organization_id"], unique=False
    )
    op.create_foreign_key(
        "fk_crawl_sources_organization",
        "crawl_sources",
        "organizations",
        ["organization_id"],
        ["id"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("fk_crawl_sources_organization", "crawl_sources", type_="foreignkey")
    op.drop_index(op.f("ix_crawl_sources_organization_id"), table_name="crawl_sources")
    op.drop_column("crawl_sources", "organization_id")
