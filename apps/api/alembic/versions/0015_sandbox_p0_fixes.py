"""Sandbox P0 fixes: SUSPENDING status, idempotency index, runner heartbeat field.

Revision ID: 0015_sandbox_p0_fixes
Revises: 0014_sandbox_model
Create Date: 2026-02-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015_sandbox_p0_fixes"
down_revision: str | None = "0014_sandbox_model"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add unique partial index on idempotency_key and sandbox_heartbeat_at to runners."""
    # Unique partial index for idempotency dedup (R24)
    # Drop the existing non-unique index first
    op.drop_index("ix_sandbox_tasks_idempotency_key", table_name="sandbox_tasks")
    op.create_index(
        "ix_sandbox_tasks_idempotency",
        "sandbox_tasks",
        ["organization_id", "sandbox_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    # Runner sandbox-specific heartbeat field (R1)
    op.add_column(
        "runners",
        sa.Column("sandbox_heartbeat_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    """Revert idempotency index and remove sandbox_heartbeat_at."""
    op.drop_column("runners", "sandbox_heartbeat_at")

    op.drop_index(
        "ix_sandbox_tasks_idempotency",
        table_name="sandbox_tasks",
    )
    # Restore the original non-unique index
    op.create_index(
        "ix_sandbox_tasks_idempotency_key",
        "sandbox_tasks",
        ["idempotency_key"],
    )
