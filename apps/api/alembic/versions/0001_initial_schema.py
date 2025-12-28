"""Initial schema for Sibyl.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2025-12-26

Consolidated from 18 migrations into single initial schema.
All tables, constraints, indexes, and foreign keys in one migration.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:  # noqa: PLR0915
    """Create all tables and constraints."""
    # =========================================================================
    # Extensions (created by postInitSQL in CNPG cluster, skip if exists)
    # =========================================================================
    # Note: pgvector extension must be created by superuser in postInitSQL
    # The migration user doesn't have CREATE EXTENSION privileges

    # =========================================================================
    # Enum Types
    # =========================================================================
    chunktype = postgresql.ENUM(
        "TEXT", "CODE", "HEADING", "LIST", "TABLE", name="chunktype", create_type=False
    )
    crawlstatus = postgresql.ENUM(
        "PENDING",
        "IN_PROGRESS",
        "COMPLETED",
        "FAILED",
        "PARTIAL",
        name="crawlstatus",
        create_type=False,
    )
    organizationrole = postgresql.ENUM(
        "owner", "admin", "member", "viewer", name="organizationrole", create_type=False
    )
    sourcetype = postgresql.ENUM(
        "WEBSITE", "GITHUB", "LOCAL", "API_DOCS", name="sourcetype", create_type=False
    )
    teamrole = postgresql.ENUM("lead", "member", "viewer", name="teamrole", create_type=False)

    # Create enums (idempotent pattern for safety)
    connection = op.get_bind()
    chunktype.create(connection, checkfirst=True)
    crawlstatus.create(connection, checkfirst=True)
    organizationrole.create(connection, checkfirst=True)
    sourcetype.create(connection, checkfirst=True)
    teamrole.create(connection, checkfirst=True)

    # =========================================================================
    # Core Tables (no foreign key dependencies)
    # =========================================================================

    # users - base authentication identity
    op.create_table(
        "users",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("github_id", sa.Integer(), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column("password_salt", sa.String(length=128), nullable=True),
        sa.Column("password_hash", sa.String(length=128), nullable=True),
        sa.Column("password_iterations", sa.Integer(), nullable=True),
        sa.Column(
            "preferences", postgresql.JSONB(), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column("bio", sa.String(), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.Column("email_verified_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_github_id", "users", ["github_id"], unique=True)

    # organizations - tenant boundary
    op.create_table(
        "organizations",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("is_personal", sa.Boolean(), nullable=False),
        sa.Column("settings", postgresql.JSONB(), server_default=sa.text("'{}'"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_organizations_slug", "organizations", ["slug"], unique=True)

    # =========================================================================
    # User-dependent Tables
    # =========================================================================

    # login_history - login event tracking
    op.create_table(
        "login_history",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("auth_method", sa.String(length=50), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("failure_reason", sa.String(length=255), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("device_info", postgresql.JSONB(), nullable=True),
        sa.Column("email_attempted", sa.String(length=255), nullable=True),
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_login_history_created_at", "login_history", ["created_at"])
    op.create_index("ix_login_history_event_type", "login_history", ["event_type"])
    op.create_index("ix_login_history_user_id", "login_history", ["user_id"])

    # password_reset_tokens
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_password_reset_tokens_expires_at", "password_reset_tokens", ["expires_at"])
    op.create_index(
        "ix_password_reset_tokens_token_hash", "password_reset_tokens", ["token_hash"], unique=True
    )
    op.create_index("ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"])

    # oauth_connections - linked OAuth providers
    op.create_table(
        "oauth_connections",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("provider_user_id", sa.String(length=255), nullable=False),
        sa.Column("provider_username", sa.String(length=255), nullable=True),
        sa.Column("provider_email", sa.String(length=255), nullable=True),
        sa.Column("access_token_encrypted", sa.Text(), nullable=True),
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(), nullable=True),
        sa.Column("scopes", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("connected_at", sa.DateTime(), nullable=False),
        sa.Column("disconnected_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_oauth_connections_provider_user",
        "oauth_connections",
        ["provider", "provider_user_id"],
        unique=True,
    )
    op.create_index("ix_oauth_connections_user_id", "oauth_connections", ["user_id"])

    # =========================================================================
    # Organization-dependent Tables
    # =========================================================================

    # organization_members - user <-> org membership
    op.create_table(
        "organization_members",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("role", organizationrole, server_default=sa.text("'member'"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_organization_members_org_user_unique",
        "organization_members",
        ["organization_id", "user_id"],
        unique=True,
    )
    op.create_index(
        "ix_organization_members_organization_id", "organization_members", ["organization_id"]
    )
    op.create_index("ix_organization_members_user_id", "organization_members", ["user_id"])

    # organization_invitations
    op.create_table(
        "organization_invitations",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("invited_email", sa.String(length=255), nullable=False),
        sa.Column(
            "invited_role", organizationrole, server_default=sa.text("'member'"), nullable=False
        ),
        sa.Column("token", sa.String(length=96), nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("accepted_at", sa.DateTime(), nullable=True),
        sa.Column("accepted_by_user_id", sa.UUID(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_organization_invitations_accepted_by_user_id",
        "organization_invitations",
        ["accepted_by_user_id"],
    )
    op.create_index(
        "ix_organization_invitations_created_by_user_id",
        "organization_invitations",
        ["created_by_user_id"],
    )
    op.create_index(
        "ix_organization_invitations_invited_email", "organization_invitations", ["invited_email"]
    )
    op.create_index(
        "ix_organization_invitations_organization_id",
        "organization_invitations",
        ["organization_id"],
    )
    op.create_index(
        "ix_organization_invitations_token", "organization_invitations", ["token"], unique=True
    )

    # api_keys - long-lived API credentials
    op.create_table(
        "api_keys",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("key_prefix", sa.String(length=32), nullable=False),
        sa.Column("key_salt", sa.String(length=64), nullable=False),
        sa.Column("key_hash", sa.String(length=128), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_api_keys_key_prefix", "api_keys", ["key_prefix"])
    op.create_index("ix_api_keys_organization_id", "api_keys", ["organization_id"])
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])

    # audit_logs - immutable audit trail
    op.create_table(
        "audit_logs",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=True),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_organization_id", "audit_logs", ["organization_id"])
    op.create_index("ix_audit_logs_user_id", "audit_logs", ["user_id"])

    # user_sessions - JWT session tracking
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=True),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("device_name", sa.String(length=255), nullable=True),
        sa.Column("device_type", sa.String(length=64), nullable=True),
        sa.Column("browser", sa.String(length=128), nullable=True),
        sa.Column("os", sa.String(length=128), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("location", sa.String(length=255), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("last_active_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("refresh_token_hash", sa.String(length=128), nullable=True),
        sa.Column("refresh_token_expires_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_sessions_organization_id", "user_sessions", ["organization_id"])
    op.create_index("ix_user_sessions_refresh_token_hash", "user_sessions", ["refresh_token_hash"])
    op.create_index("ix_user_sessions_token_hash", "user_sessions", ["token_hash"])
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])

    # device_authorization_requests - CLI device auth flow
    op.create_table(
        "device_authorization_requests",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("device_code_hash", sa.String(length=64), nullable=False),
        sa.Column("user_code", sa.String(length=16), nullable=False),
        sa.Column("client_name", sa.String(length=255), nullable=True),
        sa.Column("scope", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("poll_interval_seconds", sa.Integer(), nullable=False),
        sa.Column("last_polled_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("denied_at", sa.DateTime(), nullable=True),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("organization_id", sa.UUID(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_device_authorization_requests_device_code_hash",
        "device_authorization_requests",
        ["device_code_hash"],
        unique=True,
    )
    op.create_index(
        "ix_device_authorization_requests_organization_id",
        "device_authorization_requests",
        ["organization_id"],
    )
    op.create_index(
        "ix_device_authorization_requests_status", "device_authorization_requests", ["status"]
    )
    op.create_index(
        "ix_device_authorization_requests_user_code",
        "device_authorization_requests",
        ["user_code"],
        unique=True,
    )
    op.create_index(
        "ix_device_authorization_requests_user_id", "device_authorization_requests", ["user_id"]
    )

    # teams - teams within organizations
    op.create_table(
        "teams",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("avatar_url", sa.String(length=2048), nullable=True),
        sa.Column("settings", postgresql.JSONB(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("graph_entity_id", sa.String(length=64), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_teams_org_slug_unique", "teams", ["organization_id", "slug"], unique=True)
    op.create_index("ix_teams_organization_id", "teams", ["organization_id"])

    # team_members
    op.create_table(
        "team_members",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("team_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("role", teamrole, server_default=sa.text("'member'"), nullable=False),
        sa.Column("joined_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_team_members_team_id", "team_members", ["team_id"])
    op.create_index(
        "ix_team_members_team_user_unique", "team_members", ["team_id", "user_id"], unique=True
    )
    op.create_index("ix_team_members_user_id", "team_members", ["user_id"])

    # =========================================================================
    # Document/Crawl Tables
    # =========================================================================

    # crawl_sources - documentation sources to crawl
    op.create_table(
        "crawl_sources",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("source_type", sourcetype, nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("crawl_depth", sa.Integer(), nullable=False),
        sa.Column("include_patterns", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("exclude_patterns", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("respect_robots", sa.Boolean(), nullable=False),
        sa.Column("crawl_status", crawlstatus, nullable=False),
        sa.Column("last_crawled_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("document_count", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("current_job_id", sa.String(length=64), nullable=True),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("tags", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("categories", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("favicon_url", sa.String(length=2048), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url"),
    )
    op.create_index("ix_crawl_sources_name", "crawl_sources", ["name"])
    op.create_index("ix_crawl_sources_organization_id", "crawl_sources", ["organization_id"])

    # crawled_documents
    op.create_table(
        "crawled_documents",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("raw_content", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("parent_url", sa.String(length=2048), nullable=True),
        sa.Column("section_path", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("language", sa.String(length=10), nullable=True),
        sa.Column("word_count", sa.Integer(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("has_code", sa.Boolean(), nullable=False),
        sa.Column("is_index", sa.Boolean(), nullable=False),
        sa.Column("headings", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("links", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("code_languages", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("crawled_at", sa.DateTime(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url"),
    )
    op.create_index("ix_crawled_documents_source_id", "crawled_documents", ["source_id"])

    # document_chunks - with vector embeddings
    op.create_table(
        "document_chunks",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("chunk_type", chunktype, nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("context", sa.Text(), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("start_char", sa.Integer(), nullable=False),
        sa.Column("end_char", sa.Integer(), nullable=False),
        sa.Column("heading_path", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("embedding", postgresql.ARRAY(sa.Float()), nullable=True),  # vector(1536)
        sa.Column("language", sa.String(length=50), nullable=True),
        sa.Column("is_complete", sa.Boolean(), nullable=False),
        sa.Column("has_entities", sa.Boolean(), nullable=False),
        sa.Column("entity_ids", postgresql.ARRAY(sa.String()), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_document_chunks_document_id", "document_chunks", ["document_id"])

    # Create vector column with proper type (can't use ARRAY in migration)
    op.execute("""
        ALTER TABLE document_chunks
        ALTER COLUMN embedding TYPE vector(1536)
        USING embedding::vector(1536)
    """)

    # Full-text search index on chunks
    op.execute("""
        CREATE INDEX ix_chunks_content_fts
        ON document_chunks USING gin(to_tsvector('english', content))
    """)

    # HNSW vector similarity index
    op.execute("""
        CREATE INDEX ix_chunks_embedding_hnsw
        ON document_chunks USING hnsw(embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)

    # =========================================================================
    # Foreign Key Constraints (with proper cascade behavior)
    # =========================================================================

    # Users (SET NULL for audit preservation)
    op.create_foreign_key(
        "fk_login_history_user_id",
        "login_history",
        "users",
        ["user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_password_reset_tokens_user_id",
        "password_reset_tokens",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_oauth_connections_user_id",
        "oauth_connections",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # Organization members
    op.create_foreign_key(
        "fk_organization_members_organization_id",
        "organization_members",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_organization_members_user_id",
        "organization_members",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # Organization invitations
    op.create_foreign_key(
        "fk_organization_invitations_organization_id",
        "organization_invitations",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_organization_invitations_created_by_user_id",
        "organization_invitations",
        "users",
        ["created_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_organization_invitations_accepted_by_user_id",
        "organization_invitations",
        "users",
        ["accepted_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # API keys
    op.create_foreign_key(
        "fk_api_keys_organization_id",
        "api_keys",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_api_keys_user_id", "api_keys", "users", ["user_id"], ["id"], ondelete="CASCADE"
    )

    # Audit logs (SET NULL for preservation)
    op.create_foreign_key(
        "fk_audit_logs_organization_id",
        "audit_logs",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_audit_logs_user_id", "audit_logs", "users", ["user_id"], ["id"], ondelete="SET NULL"
    )

    # User sessions
    op.create_foreign_key(
        "fk_user_sessions_user_id",
        "user_sessions",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_user_sessions_organization_id",
        "user_sessions",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Device auth requests (SET NULL for audit)
    op.create_foreign_key(
        "fk_device_authorization_requests_user_id",
        "device_authorization_requests",
        "users",
        ["user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_device_authorization_requests_organization_id",
        "device_authorization_requests",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Teams
    op.create_foreign_key(
        "fk_teams_organization_id",
        "teams",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_team_members_team_id", "team_members", "teams", ["team_id"], ["id"], ondelete="CASCADE"
    )
    op.create_foreign_key(
        "fk_team_members_user_id", "team_members", "users", ["user_id"], ["id"], ondelete="CASCADE"
    )

    # Crawl sources
    op.create_foreign_key(
        "fk_crawl_sources_organization_id",
        "crawl_sources",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # Documents
    op.create_foreign_key(
        "fk_crawled_documents_source_id",
        "crawled_documents",
        "crawl_sources",
        ["source_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_document_chunks_document_id",
        "document_chunks",
        "crawled_documents",
        ["document_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    """Drop all tables in reverse order."""
    # Drop foreign keys first (in reverse order of creation)
    op.drop_constraint("fk_document_chunks_document_id", "document_chunks", type_="foreignkey")
    op.drop_constraint("fk_crawled_documents_source_id", "crawled_documents", type_="foreignkey")
    op.drop_constraint("fk_crawl_sources_organization_id", "crawl_sources", type_="foreignkey")
    op.drop_constraint("fk_team_members_user_id", "team_members", type_="foreignkey")
    op.drop_constraint("fk_team_members_team_id", "team_members", type_="foreignkey")
    op.drop_constraint("fk_teams_organization_id", "teams", type_="foreignkey")
    op.drop_constraint(
        "fk_device_authorization_requests_organization_id",
        "device_authorization_requests",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_device_authorization_requests_user_id",
        "device_authorization_requests",
        type_="foreignkey",
    )
    op.drop_constraint("fk_user_sessions_organization_id", "user_sessions", type_="foreignkey")
    op.drop_constraint("fk_user_sessions_user_id", "user_sessions", type_="foreignkey")
    op.drop_constraint("fk_audit_logs_user_id", "audit_logs", type_="foreignkey")
    op.drop_constraint("fk_audit_logs_organization_id", "audit_logs", type_="foreignkey")
    op.drop_constraint("fk_api_keys_user_id", "api_keys", type_="foreignkey")
    op.drop_constraint("fk_api_keys_organization_id", "api_keys", type_="foreignkey")
    op.drop_constraint(
        "fk_organization_invitations_accepted_by_user_id",
        "organization_invitations",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_organization_invitations_created_by_user_id",
        "organization_invitations",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_organization_invitations_organization_id",
        "organization_invitations",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_organization_members_user_id", "organization_members", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_organization_members_organization_id", "organization_members", type_="foreignkey"
    )
    op.drop_constraint("fk_oauth_connections_user_id", "oauth_connections", type_="foreignkey")
    op.drop_constraint(
        "fk_password_reset_tokens_user_id", "password_reset_tokens", type_="foreignkey"
    )
    op.drop_constraint("fk_login_history_user_id", "login_history", type_="foreignkey")

    # Drop indexes
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
    op.execute("DROP INDEX IF EXISTS ix_chunks_content_fts")

    # Drop tables (reverse order of creation)
    op.drop_table("document_chunks")
    op.drop_table("crawled_documents")
    op.drop_table("crawl_sources")
    op.drop_table("team_members")
    op.drop_table("teams")
    op.drop_table("device_authorization_requests")
    op.drop_table("user_sessions")
    op.drop_table("audit_logs")
    op.drop_table("api_keys")
    op.drop_table("organization_invitations")
    op.drop_table("organization_members")
    op.drop_table("oauth_connections")
    op.drop_table("password_reset_tokens")
    op.drop_table("login_history")
    op.drop_table("organizations")
    op.drop_table("users")

    # Drop enums
    op.execute("DROP TYPE IF EXISTS teamrole")
    op.execute("DROP TYPE IF EXISTS sourcetype")
    op.execute("DROP TYPE IF EXISTS organizationrole")
    op.execute("DROP TYPE IF EXISTS crawlstatus")
    op.execute("DROP TYPE IF EXISTS chunktype")

    # Drop extension (optional, might be used by other databases)
    # op.execute("DROP EXTENSION IF EXISTS vector")
