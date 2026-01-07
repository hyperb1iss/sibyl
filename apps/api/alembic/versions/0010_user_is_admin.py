"""Add is_admin field to users table.

Revision ID: 0010_user_is_admin
Revises: 0009_planning_studio
Create Date: 2026-01-07

Adds `is_admin` boolean column to users table for system-level admin privileges.
The first user to register becomes an admin automatically (handled in app code).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010_user_is_admin"
down_revision: str | None = "0009_planning_studio"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add is_admin column with default false
    op.add_column(
        "users",
        sa.Column(
            "is_admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # Make the first user (by created_at) an admin if any users exist
    connection = op.get_bind()
    connection.execute(
        sa.text("""
            UPDATE users
            SET is_admin = true
            WHERE id = (
                SELECT id FROM users
                ORDER BY created_at ASC
                LIMIT 1
            )
        """)
    )


def downgrade() -> None:
    op.drop_column("users", "is_admin")
