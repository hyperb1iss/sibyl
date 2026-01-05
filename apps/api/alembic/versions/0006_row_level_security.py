"""Enable Row-Level Security (RLS) for multi-tenant isolation.

Revision ID: 0006_row_level_security
Revises: 0005_project_permissions
Create Date: 2026-01-04

RLS ensures that queries automatically filter to the current user's organization,
providing defense-in-depth even if application code misses an org filter.

Session variables are set by the application:
- SET LOCAL app.org_id = '<uuid>'
- SET LOCAL app.user_id = '<uuid>'

Policies check: current_setting('app.org_id', true) = organization_id::text
The 'true' parameter makes it return NULL if not set, rather than raising an error.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_row_level_security"
down_revision: str | None = "0005_project_permissions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tables with organization_id column that need RLS
ORG_SCOPED_TABLES = [
    "organization_members",
    "teams",
    "projects",
    "project_members",
    "team_projects",
    "crawl_sources",
    "audit_logs",
    "organization_invitations",
]

# Tables that inherit org scope via FK (need special handling)
# - team_members: via team_id -> teams.organization_id
# - crawled_documents: via source_id -> crawl_sources.organization_id
# - document_chunks: via document_id -> crawled_documents (no direct org)
# - agent_messages: via agent_id -> graph (no org in postgres)
# These are skipped for now - app-level filtering is sufficient

# Tables with user_id but no organization_id (user-scoped)
USER_SCOPED_TABLES = [
    "user_sessions",
    "login_history",
    "password_reset_tokens",
    "oauth_connections",
]


def upgrade() -> None:
    # =========================================================================
    # Enable RLS on org-scoped tables
    # =========================================================================
    for table in ORG_SCOPED_TABLES:
        # Enable RLS on the table
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")

        # Force RLS even for table owner (important for superuser connections)
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

        # Create policy: only see rows from current org
        op.execute(f"""
            CREATE POLICY {table}_org_isolation ON {table}
            FOR ALL
            USING (
                -- Allow if org_id matches current session
                organization_id::text = current_setting('app.org_id', true)
                -- Or if no session variable is set (for migrations, admin scripts)
                OR current_setting('app.org_id', true) IS NULL
            )
            WITH CHECK (
                -- Only allow writes to current org
                organization_id::text = current_setting('app.org_id', true)
                OR current_setting('app.org_id', true) IS NULL
            )
        """)

    # =========================================================================
    # Enable RLS on user-scoped tables
    # =========================================================================
    for table in USER_SCOPED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

        op.execute(f"""
            CREATE POLICY {table}_user_isolation ON {table}
            FOR ALL
            USING (
                user_id::text = current_setting('app.user_id', true)
                OR current_setting('app.user_id', true) IS NULL
            )
            WITH CHECK (
                user_id::text = current_setting('app.user_id', true)
                OR current_setting('app.user_id', true) IS NULL
            )
        """)

    # =========================================================================
    # Special handling for api_keys (user-owned but org-scoped)
    # =========================================================================
    op.execute("ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE api_keys FORCE ROW LEVEL SECURITY")

    op.execute("""
        CREATE POLICY api_keys_isolation ON api_keys
        FOR ALL
        USING (
            (
                -- User must match
                user_id::text = current_setting('app.user_id', true)
                -- AND org must match
                AND organization_id::text = current_setting('app.org_id', true)
            )
            -- Or bypass when no session context (migrations, etc.)
            OR (
                current_setting('app.user_id', true) IS NULL
                AND current_setting('app.org_id', true) IS NULL
            )
        )
        WITH CHECK (
            (
                user_id::text = current_setting('app.user_id', true)
                AND organization_id::text = current_setting('app.org_id', true)
            )
            OR (
                current_setting('app.user_id', true) IS NULL
                AND current_setting('app.org_id', true) IS NULL
            )
        )
    """)

    # =========================================================================
    # api_key_project_scopes (inherits from api_key ownership)
    # =========================================================================
    op.execute("ALTER TABLE api_key_project_scopes ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE api_key_project_scopes FORCE ROW LEVEL SECURITY")

    # This table doesn't have org_id directly, so we join through api_keys
    op.execute("""
        CREATE POLICY api_key_project_scopes_isolation ON api_key_project_scopes
        FOR ALL
        USING (
            EXISTS (
                SELECT 1 FROM api_keys
                WHERE api_keys.id = api_key_project_scopes.api_key_id
                AND api_keys.user_id::text = current_setting('app.user_id', true)
                AND api_keys.organization_id::text = current_setting('app.org_id', true)
            )
            OR (
                current_setting('app.user_id', true) IS NULL
                AND current_setting('app.org_id', true) IS NULL
            )
        )
        WITH CHECK (
            EXISTS (
                SELECT 1 FROM api_keys
                WHERE api_keys.id = api_key_project_scopes.api_key_id
                AND api_keys.user_id::text = current_setting('app.user_id', true)
                AND api_keys.organization_id::text = current_setting('app.org_id', true)
            )
            OR (
                current_setting('app.user_id', true) IS NULL
                AND current_setting('app.org_id', true) IS NULL
            )
        )
    """)

    # =========================================================================
    # device_authorization_requests (user-scoped, org in claims)
    # =========================================================================
    op.execute("ALTER TABLE device_authorization_requests ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE device_authorization_requests FORCE ROW LEVEL SECURITY")

    # Device auth has user_id (nullable for pending) and org claim embedded in token
    op.execute("""
        CREATE POLICY device_auth_isolation ON device_authorization_requests
        FOR ALL
        USING (
            -- Allow if user matches (for completed authorizations)
            user_id::text = current_setting('app.user_id', true)
            -- Or if pending (no user yet) - these are public pending requests
            OR user_id IS NULL
            -- Or bypass when no session context
            OR current_setting('app.user_id', true) IS NULL
        )
        WITH CHECK (
            user_id::text = current_setting('app.user_id', true)
            OR user_id IS NULL
            OR current_setting('app.user_id', true) IS NULL
        )
    """)


def downgrade() -> None:
    # Drop policies and disable RLS in reverse order

    # device_authorization_requests
    op.execute("DROP POLICY IF EXISTS device_auth_isolation ON device_authorization_requests")
    op.execute("ALTER TABLE device_authorization_requests DISABLE ROW LEVEL SECURITY")

    # api_key_project_scopes
    op.execute("DROP POLICY IF EXISTS api_key_project_scopes_isolation ON api_key_project_scopes")
    op.execute("ALTER TABLE api_key_project_scopes DISABLE ROW LEVEL SECURITY")

    # api_keys
    op.execute("DROP POLICY IF EXISTS api_keys_isolation ON api_keys")
    op.execute("ALTER TABLE api_keys DISABLE ROW LEVEL SECURITY")

    # User-scoped tables
    for table in USER_SCOPED_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_user_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    # Org-scoped tables
    for table in ORG_SCOPED_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_org_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
