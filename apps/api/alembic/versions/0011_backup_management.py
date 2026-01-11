"""Add backup_settings and backups tables for backup management.

Enables per-organization backup configuration and tracking of backup archives.

Revision ID: 0011_backup_management
Revises: 0010_user_is_admin
Create Date: 2026-01-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011_backup_management"
down_revision: str | None = "0010_user_is_admin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Using VARCHAR with CHECK constraint instead of ENUM to avoid
    # SQLAlchemy's async enum creation issues in migrations

    # Create backup_settings table (per-org configuration)
    op.create_table(
        "backup_settings",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "organization_id",
            sa.UUID(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        # Configuration
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("schedule", sa.String(64), nullable=False, server_default="0 2 * * *"),
        sa.Column("retention_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("include_postgres", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("include_graph", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        # Last backup info (denormalized)
        sa.Column("last_backup_at", sa.DateTime(), nullable=True),
        sa.Column("last_backup_id", sa.String(64), nullable=True),
        # Timestamps
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_backup_settings_organization_id", "backup_settings", ["organization_id"])

    # Create backups table (individual backup records)
    op.create_table(
        "backups",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "organization_id",
            sa.UUID(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Identification
        sa.Column("backup_id", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("job_id", sa.String(128), nullable=True),
        # Archive details
        sa.Column("filename", sa.String(255), nullable=True),
        sa.Column("file_path", sa.String(1024), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        # Contents
        sa.Column("include_postgres", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("include_graph", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("entity_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("relationship_count", sa.Integer(), nullable=False, server_default="0"),
        # Timing
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=False, server_default="0.0"),
        # Metadata
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("triggered_by", sa.String(64), nullable=True),
        sa.Column(
            "created_by_user_id",
            sa.UUID(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Timestamps
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_backups_organization_id", "backups", ["organization_id"])
    op.create_index("ix_backups_backup_id", "backups", ["backup_id"])
    op.create_index("ix_backups_status", "backups", ["status"])
    op.create_index("ix_backups_created_at", "backups", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_backups_created_at")
    op.drop_index("ix_backups_status")
    op.drop_index("ix_backups_backup_id")
    op.drop_index("ix_backups_organization_id")
    op.drop_table("backups")

    op.drop_index("ix_backup_settings_organization_id")
    op.drop_table("backup_settings")
