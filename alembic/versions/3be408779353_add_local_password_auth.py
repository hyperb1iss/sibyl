"""add local password auth

Revision ID: 3be408779353
Revises: 57e3f7d2f8c1
Create Date: 2025-12-21 22:26:18.273834

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3be408779353"
down_revision: str | Sequence[str] | None = "57e3f7d2f8c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column(
        "users",
        "github_id",
        existing_type=sa.Integer(),
        nullable=True,
    )

    op.add_column("users", sa.Column("password_salt", sa.String(length=128), nullable=True))
    op.add_column("users", sa.Column("password_hash", sa.String(length=128), nullable=True))
    op.add_column("users", sa.Column("password_iterations", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("users", "password_iterations")
    op.drop_column("users", "password_hash")
    op.drop_column("users", "password_salt")

    op.alter_column(
        "users",
        "github_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
