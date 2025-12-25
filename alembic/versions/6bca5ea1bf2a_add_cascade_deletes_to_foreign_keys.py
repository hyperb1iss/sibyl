"""add_cascade_deletes_to_foreign_keys

Revision ID: 6bca5ea1bf2a
Revises: 442de3d5fb43
Create Date: 2025-12-24 15:51:18.412217

Adds ON DELETE CASCADE / SET NULL to foreign key constraints for proper
referential integrity when parent records are deleted.

Strategy:
- CASCADE: Child records deleted with parent (memberships, keys, sessions, docs)
- SET NULL: Preserve record but null FK (audit logs, history, optional refs)
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "6bca5ea1bf2a"
down_revision: str | Sequence[str] | None = "442de3d5fb43"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Define FK changes: (table, column, ref_table, ref_column, ondelete)
FK_CHANGES: list[tuple[str, str, str, str, str]] = [
    # === Critical data relationships (CASCADE) ===
    ("crawled_documents", "source_id", "crawl_sources", "id", "CASCADE"),
    ("document_chunks", "document_id", "crawled_documents", "id", "CASCADE"),
    ("crawl_sources", "organization_id", "organizations", "id", "CASCADE"),
    # === Team relationships (CASCADE) ===
    ("teams", "organization_id", "organizations", "id", "CASCADE"),
    ("team_members", "team_id", "teams", "id", "CASCADE"),
    ("team_members", "user_id", "users", "id", "CASCADE"),
    # === Organization membership (CASCADE) ===
    ("organization_members", "organization_id", "organizations", "id", "CASCADE"),
    ("organization_members", "user_id", "users", "id", "CASCADE"),
    # === API keys (CASCADE - revoke on user/org delete) ===
    ("api_keys", "organization_id", "organizations", "id", "CASCADE"),
    ("api_keys", "user_id", "users", "id", "CASCADE"),
    # === User sessions (CASCADE on user, SET NULL on org) ===
    ("user_sessions", "user_id", "users", "id", "CASCADE"),
    ("user_sessions", "organization_id", "organizations", "id", "SET NULL"),
    # === OAuth connections (CASCADE - remove on user delete) ===
    ("oauth_connections", "user_id", "users", "id", "CASCADE"),
    # === Password reset tokens (CASCADE) ===
    ("password_reset_tokens", "user_id", "users", "id", "CASCADE"),
    # === Organization invitations (CASCADE on org, SET NULL on users) ===
    ("organization_invitations", "organization_id", "organizations", "id", "CASCADE"),
    ("organization_invitations", "created_by_user_id", "users", "id", "SET NULL"),
    ("organization_invitations", "accepted_by_user_id", "users", "id", "SET NULL"),
    # === Device authorization (SET NULL - temporary records) ===
    ("device_authorization_requests", "user_id", "users", "id", "SET NULL"),
    ("device_authorization_requests", "organization_id", "organizations", "id", "SET NULL"),
    # === Audit/history logs (SET NULL - preserve for compliance) ===
    ("login_history", "user_id", "users", "id", "SET NULL"),
    ("audit_logs", "organization_id", "organizations", "id", "SET NULL"),
    ("audit_logs", "user_id", "users", "id", "SET NULL"),
]


def _get_fk_constraint_name(conn, table: str, column: str) -> str | None:
    """Look up the actual FK constraint name from the database."""
    result = conn.execute(
        text("""
            SELECT tc.constraint_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                ON tc.constraint_name = kcu.constraint_name
                AND tc.table_schema = kcu.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
                AND tc.table_name = :table
                AND kcu.column_name = :column
        """),
        {"table": table, "column": column},
    )
    row = result.fetchone()
    return row[0] if row else None


def _new_fk_name(table: str, column: str) -> str:
    """Generate new FK constraint name."""
    return f"fk_{table}_{column}"


def upgrade() -> None:
    """Add ON DELETE behavior to foreign key constraints."""
    conn = op.get_bind()

    for table, column, ref_table, ref_column, ondelete in FK_CHANGES:
        # Find current FK name
        old_name = _get_fk_constraint_name(conn, table, column)
        new_name = _new_fk_name(table, column)

        if old_name:
            # Drop existing FK
            op.drop_constraint(old_name, table, type_="foreignkey")

        # Create new FK with ON DELETE behavior
        op.create_foreign_key(
            new_name,
            table,
            ref_table,
            [column],
            [ref_column],
            ondelete=ondelete,
        )


def downgrade() -> None:
    """Remove ON DELETE behavior (restore default NO ACTION)."""
    for table, column, ref_table, ref_column, _ondelete in FK_CHANGES:
        fk_name = _new_fk_name(table, column)

        # Drop our named FK
        op.drop_constraint(fk_name, table, type_="foreignkey")

        # Recreate without ON DELETE (default is NO ACTION)
        op.create_foreign_key(
            fk_name,
            table,
            ref_table,
            [column],
            [ref_column],
        )
