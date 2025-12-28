"""Add API key scopes and optional expiry.

Revision ID: 0002_api_key_scopes_and_expiry
Revises: 0001_initial_schema
Create Date: 2025-12-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002_api_key_scopes_and_expiry"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column(
            "scopes",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.add_column("api_keys", sa.Column("expires_at", sa.DateTime(), nullable=True))
    # Keep the default for safe inserts; schema can be tightened later if desired.


def downgrade() -> None:
    op.drop_column("api_keys", "expires_at")
    op.drop_column("api_keys", "scopes")

