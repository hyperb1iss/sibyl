"""add current_job_id to crawl_sources

Revision ID: da686c1f231b
Revises: 79011f63dfa8
Create Date: 2025-12-21 12:57:41.428650

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'da686c1f231b'
down_revision: Union[str, Sequence[str], None] = '79011f63dfa8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add current_job_id column to track active crawl jobs."""
    op.add_column(
        'crawl_sources',
        sa.Column('current_job_id', sa.String(length=64), nullable=True)
    )


def downgrade() -> None:
    """Remove current_job_id column."""
    op.drop_column('crawl_sources', 'current_job_id')
