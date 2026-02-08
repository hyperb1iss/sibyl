"""SQLModel schemas for Sibyl PostgreSQL storage.

This module defines the PostgreSQL tables for:
- Auth/account primitives (users)
- Crawled documents, chunks, and embeddings (pgvector)

Architecture:
- User: Auth identity (GitHub-backed for now)
- CrawlSource: Track documentation sources (websites, repos)
- CrawledDocument: Store raw crawled documents
- DocumentChunk: Store chunked content with embeddings for hybrid search
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from pydantic import field_validator
from sqlalchemy import ARRAY, Column, DateTime, Enum, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship, SQLModel


def utcnow_naive() -> datetime:
    """Get current UTC time as naive datetime (for TIMESTAMP WITHOUT TIME ZONE)."""
    return datetime.now(UTC).replace(tzinfo=None)


# =============================================================================
# Enums
# =============================================================================


class SourceType(StrEnum):
    """Types of documentation sources."""

    WEBSITE = "website"
    GITHUB = "github"
    LOCAL = "local"
    API_DOCS = "api_docs"


class CrawlStatus(StrEnum):
    """Status of a crawl operation."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"


class ChunkType(StrEnum):
    """Type of content chunk."""

    TEXT = "text"
    CODE = "code"
    HEADING = "heading"
    LIST = "list"
    TABLE = "table"


class AgentMessageRole(StrEnum):
    """Role of an agent message sender."""

    # Names must be lowercase to match Postgres enum values
    agent = "agent"
    user = "user"
    system = "system"


class AgentMessageType(StrEnum):
    """Type of agent message content."""

    # Names must be lowercase to match Postgres enum values
    text = "text"
    tool_call = "tool_call"
    tool_result = "tool_result"
    error = "error"


class InterAgentMessageType(StrEnum):
    """Types of inter-agent messages for the message bus.

    These enable agent-to-agent communication during distributed execution.
    """

    # Progress updates (non-blocking, broadcast)
    progress = "progress"

    # Query/response pair (can be blocking)
    query = "query"
    response = "response"

    # Code review workflow
    review_request = "review_request"
    review_result = "review_result"

    # Status signals
    blocker = "blocker"
    delegation = "delegation"

    # System messages
    system = "system"


# =============================================================================
# Base Model
# =============================================================================


class TimestampMixin(SQLModel):
    """Mixin for created/updated timestamps."""

    created_at: datetime = Field(
        default_factory=utcnow_naive,
        description="When this record was created",
    )
    updated_at: datetime = Field(
        default_factory=utcnow_naive,
        description="When this record was last updated",
        sa_column_kwargs={"onupdate": utcnow_naive},
    )


# =============================================================================
# User - Authentication identity
# =============================================================================


class User(TimestampMixin, table=True):
    """A user identity record (GitHub OAuth or local email/password)."""

    __tablename__ = "users"

    id: UUID = Field(default_factory=uuid4, primary_key=True)

    # GitHub identity
    github_id: int | None = Field(
        default=None,
        index=True,
        unique=True,
        description="GitHub numeric user id (null for local users)",
    )

    # Profile info
    email: str | None = Field(
        default=None,
        max_length=255,
        unique=True,
        index=True,
        description="Primary email (may be null if unavailable)",
    )
    name: str = Field(default="", max_length=255, description="Display name")
    bio: str | None = Field(default=None, description="User bio")  # TEXT in DB
    timezone: str = Field(default="UTC", max_length=64, description="User timezone")
    avatar_url: str | None = Field(
        default=None,
        sa_type=Text,
        description="Profile avatar URL (supports data URLs)",
    )
    email_verified_at: datetime | None = Field(
        default=None,
        description="When email was verified",
    )
    last_login_at: datetime | None = Field(
        default=None,
        description="Last login timestamp",
    )
    preferences: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default="'{}'"),
        description="User preferences JSON",
    )

    # Local auth (PBKDF2 hash)
    password_salt: str | None = Field(
        default=None,
        max_length=128,
        description="Hex salt for local password hash (null if no local password)",
    )
    password_hash: str | None = Field(
        default=None,
        max_length=128,
        description="Hex PBKDF2 hash for local password (null if no local password)",
    )
    password_iterations: int | None = Field(
        default=None,
        description="PBKDF2 iteration count used for password hash",
    )

    # System admin flag (first user becomes admin)
    is_admin: bool = Field(
        default=False,
        description="System administrator - can manage server settings and all orgs",
    )

    def __repr__(self) -> str:
        return f"<User github_id={self.github_id!r} email={self.email!r}>"


# =============================================================================
# Login History - Track login events
# =============================================================================


class LoginHistory(SQLModel, table=True):
    """Login event history for security auditing."""

    __tablename__ = "login_history"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID | None = Field(default=None, foreign_key="users.id", index=True)
    event_type: str = Field(max_length=50, index=True)
    auth_method: str | None = Field(default=None, max_length=50)
    success: bool = Field(default=False)
    failure_reason: str | None = Field(default=None, max_length=255)
    ip_address: str | None = Field(default=None, max_length=64)
    user_agent: str | None = Field(default=None, max_length=512)
    device_info: dict[str, Any] | None = Field(default=None, sa_column=Column(JSONB))
    email_attempted: str | None = Field(default=None, max_length=255)
    session_id: UUID | None = Field(default=None)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC).replace(tzinfo=None),
        sa_column=Column(DateTime, nullable=False, server_default=text("now()"), index=True),
    )


# =============================================================================
# Password Reset Tokens
# =============================================================================


class PasswordResetToken(SQLModel, table=True):
    """Password reset token for email-based password recovery."""

    __tablename__ = "password_reset_tokens"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="users.id", index=True)
    token_hash: str = Field(max_length=64, unique=True, index=True)
    expires_at: datetime = Field(index=True)
    used_at: datetime | None = Field(default=None)
    revoked_at: datetime | None = Field(default=None)
    ip_address: str | None = Field(default=None, max_length=64)
    user_agent: str | None = Field(default=None, max_length=512)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC).replace(tzinfo=None),
        sa_column=Column(DateTime, nullable=False, server_default=text("now()")),
    )


# =============================================================================
# Organization - Tenant boundary for auth
# =============================================================================


class Organization(TimestampMixin, table=True):
    """An organization/tenant."""

    __tablename__ = "organizations"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(max_length=255, description="Organization display name")
    slug: str = Field(max_length=64, unique=True, index=True, description="URL-safe unique slug")
    is_personal: bool = Field(default=False, description="Personal org owned by one user")

    settings: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        description="Arbitrary org settings",
    )

    def __repr__(self) -> str:
        return f"<Organization slug={self.slug!r} personal={self.is_personal}>"


class OrganizationRole(StrEnum):
    """Role of a user within an organization."""

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class OrganizationMember(TimestampMixin, table=True):
    """Membership record linking a user to an organization."""

    __tablename__ = "organization_members"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    organization_id: UUID = Field(foreign_key="organizations.id", index=True)
    user_id: UUID = Field(foreign_key="users.id", index=True)
    role: OrganizationRole = Field(
        default=OrganizationRole.MEMBER,
        sa_column=Column(
            Enum(
                OrganizationRole,
                name="organizationrole",
                values_callable=lambda enum: [e.value for e in enum],
            ),
            nullable=False,
            server_default=text("'member'"),
        ),
        description="Membership role",
    )

    organization: Organization = Relationship()
    user: User = Relationship()

    __table_args__ = (
        Index(
            "ix_organization_members_org_user_unique",
            "organization_id",
            "user_id",
            unique=True,
        ),
    )


# =============================================================================
# API Keys - Long-lived credentials for CLI + automation
# =============================================================================


class ApiKey(TimestampMixin, table=True):
    """Long-lived API key (store only hash + salt, never raw key)."""

    __tablename__ = "api_keys"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    organization_id: UUID = Field(foreign_key="organizations.id", index=True)
    user_id: UUID = Field(foreign_key="users.id", index=True)

    name: str = Field(default="", max_length=255, description="Display name for this key")
    key_prefix: str = Field(
        max_length=32,
        index=True,
        description="Non-secret prefix for lookup (e.g. sk_live_abc123...)",
    )
    key_salt: str = Field(max_length=64, description="Hex-encoded salt")
    key_hash: str = Field(max_length=128, description="Hex-encoded derived key hash")

    scopes: list[str] = Field(
        default_factory=list,
        sa_type=ARRAY(String),
        description="Granted scopes for this API key (e.g. mcp, api:read, api:write)",
    )
    expires_at: datetime | None = Field(default=None, description="Optional expiry timestamp")

    revoked_at: datetime | None = Field(default=None, description="Revocation timestamp")
    last_used_at: datetime | None = Field(default=None, description="Last usage timestamp")


# =============================================================================
# User Sessions - Track active login sessions
# =============================================================================


class UserSession(TimestampMixin, table=True):
    """User login session for session tracking and revocation."""

    __tablename__ = "user_sessions"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="users.id", index=True)
    organization_id: UUID | None = Field(
        default=None,
        foreign_key="organizations.id",
        index=True,
        description="Org context for this session",
    )

    token_hash: str = Field(
        max_length=128,
        index=True,
        description="SHA256 hash of the access token",
    )
    refresh_token_hash: str | None = Field(
        default=None,
        max_length=128,
        index=True,
        description="SHA256 hash of the refresh token",
    )
    refresh_token_expires_at: datetime | None = Field(
        default=None,
        description="Refresh token expiry (longer than access token)",
    )

    # Device info
    device_name: str | None = Field(default=None, max_length=255)
    device_type: str | None = Field(default=None, max_length=64)
    browser: str | None = Field(default=None, max_length=128)
    os: str | None = Field(default=None, max_length=128)
    ip_address: str | None = Field(default=None, max_length=64)
    user_agent: str | None = Field(default=None, max_length=512)
    location: str | None = Field(default=None, max_length=255)

    # Session state
    is_current: bool = Field(default=False, description="Mark as current active session")
    last_active_at: datetime | None = Field(
        default=None,
        description="Last activity timestamp",
    )
    expires_at: datetime = Field(description="Session expiry time")
    revoked_at: datetime | None = Field(
        default=None,
        description="Revocation timestamp (null if active)",
    )


# =============================================================================
# Audit Logs - Immutable trail for sensitive actions
# =============================================================================


class AuditLog(TimestampMixin, table=True):
    """Append-only audit event record."""

    __tablename__ = "audit_logs"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    organization_id: UUID | None = Field(
        default=None,
        foreign_key="organizations.id",
        index=True,
        description="Org context for the event (null for pre-auth events)",
    )
    user_id: UUID | None = Field(
        default=None,
        foreign_key="users.id",
        index=True,
        description="User who performed the action (null for unauthenticated)",
    )

    action: str = Field(
        max_length=128,
        index=True,
        description="Machine-readable action name (e.g. auth.login, org.member.add)",
    )
    ip_address: str | None = Field(
        default=None,
        max_length=64,
        description="Client IP address (best-effort)",
    )
    user_agent: str | None = Field(
        default=None,
        max_length=512,
        description="User-Agent header (best-effort)",
    )

    details: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB),
        description="Arbitrary structured details (never secrets)",
    )


# =============================================================================
# Org Invitations - Invite a user (by email) to an org
# =============================================================================


class OrganizationInvitation(TimestampMixin, table=True):
    """Invitation to join an organization."""

    __tablename__ = "organization_invitations"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    organization_id: UUID = Field(foreign_key="organizations.id", index=True)
    invited_email: str = Field(max_length=255, index=True, description="Invitee email")
    invited_role: OrganizationRole = Field(
        default=OrganizationRole.MEMBER,
        sa_column=Column(
            Enum(
                OrganizationRole,
                name="organizationrole",
                values_callable=lambda enum: [e.value for e in enum],
            ),
            nullable=False,
            server_default=text("'member'"),
        ),
        description="Role to grant when accepted",
    )
    token: str = Field(max_length=96, unique=True, index=True, description="Opaque invite token")
    created_by_user_id: UUID = Field(foreign_key="users.id", index=True)

    expires_at: datetime | None = Field(default=None, description="Expiry timestamp")
    accepted_at: datetime | None = Field(default=None, description="Acceptance timestamp")
    accepted_by_user_id: UUID | None = Field(default=None, foreign_key="users.id", index=True)


# =============================================================================
# Device Authorization - CLI login without local callback server
# =============================================================================


class DeviceAuthorizationRequest(TimestampMixin, table=True):
    """Short-lived device authorization request (RFC 8628-style)."""

    __tablename__ = "device_authorization_requests"

    id: UUID = Field(default_factory=uuid4, primary_key=True)

    # Store only a hash of the device code (never the raw device_code)
    device_code_hash: str = Field(
        max_length=64,
        unique=True,
        index=True,
        description="SHA256 hex digest of device_code",
    )
    # Short code shown to the user; stored normalized (e.g. ABCD-EFGH)
    user_code: str = Field(
        max_length=16,
        unique=True,
        index=True,
        description="Human-friendly user code",
    )

    client_name: str | None = Field(
        default=None,
        max_length=255,
        description="Optional client display name",
    )
    scope: str = Field(default="mcp", max_length=255, description="Requested scope(s)")

    status: str = Field(
        default="pending",
        max_length=16,
        index=True,
        description="pending|approved|denied|consumed",
    )

    poll_interval_seconds: int = Field(default=5, description="Recommended polling interval")
    last_polled_at: datetime | None = Field(default=None, description="Last polling timestamp")

    expires_at: datetime = Field(description="Expiry timestamp")
    approved_at: datetime | None = Field(default=None, description="Approval timestamp")
    denied_at: datetime | None = Field(default=None, description="Denial timestamp")
    consumed_at: datetime | None = Field(default=None, description="Token issuance timestamp")

    user_id: UUID | None = Field(default=None, foreign_key="users.id", index=True)
    organization_id: UUID | None = Field(default=None, foreign_key="organizations.id", index=True)


# =============================================================================
# OAuth Connections - Link/unlink OAuth providers
# =============================================================================


class OAuthConnection(TimestampMixin, table=True):
    """OAuth provider connection for a user."""

    __tablename__ = "oauth_connections"
    __table_args__ = (
        Index("ix_oauth_connections_provider_user", "provider", "provider_user_id", unique=True),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="users.id", index=True)

    # Provider info
    provider: str = Field(max_length=50, description="OAuth provider (github, google, etc.)")
    provider_user_id: str = Field(max_length=255, description="User ID from provider")
    provider_username: str | None = Field(
        default=None, max_length=255, description="Username from provider"
    )
    provider_email: str | None = Field(
        default=None, max_length=255, description="Email from provider"
    )

    # Tokens (encrypted at rest)
    access_token_encrypted: str | None = Field(
        default=None, sa_type=Text, description="Encrypted access token"
    )
    refresh_token_encrypted: str | None = Field(
        default=None, sa_type=Text, description="Encrypted refresh token"
    )
    token_expires_at: datetime | None = Field(default=None, description="Token expiry")

    # Scopes
    scopes: list[str] = Field(
        default_factory=list,
        sa_type=ARRAY(String),
        description="Granted OAuth scopes",
    )

    # Status
    connected_at: datetime = Field(
        default_factory=utcnow_naive, description="When connection was established"
    )
    disconnected_at: datetime | None = Field(default=None, description="When disconnected")
    last_used_at: datetime | None = Field(default=None, description="Last used for auth")

    user: User = Relationship()


# =============================================================================
# Teams - Teams within organizations
# =============================================================================


class TeamRole(StrEnum):
    """Role of a user within a team."""

    LEAD = "lead"
    MEMBER = "member"
    VIEWER = "viewer"


class Team(TimestampMixin, table=True):
    """A team within an organization."""

    __tablename__ = "teams"
    __table_args__ = (Index("ix_teams_org_slug_unique", "organization_id", "slug", unique=True),)

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    organization_id: UUID = Field(foreign_key="organizations.id", index=True)

    # Team info
    name: str = Field(max_length=255, description="Team display name")
    slug: str = Field(max_length=64, description="URL-safe slug (unique per org)")
    description: str | None = Field(default=None, sa_type=Text, description="Team description")
    avatar_url: str | None = Field(default=None, max_length=2048, description="Team avatar URL")

    # Settings
    settings: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        description="Team settings",
    )
    is_default: bool = Field(default=False, description="Default team for org members")

    # Graph sync
    graph_entity_id: str | None = Field(
        default=None, max_length=64, description="Entity ID in knowledge graph"
    )
    last_synced_at: datetime | None = Field(default=None, description="Last graph sync")

    organization: Organization = Relationship()
    members: list["TeamMember"] = Relationship(
        back_populates="team",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class TeamMember(TimestampMixin, table=True):
    """Membership record linking a user to a team."""

    __tablename__ = "team_members"
    __table_args__ = (Index("ix_team_members_team_user_unique", "team_id", "user_id", unique=True),)

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    team_id: UUID = Field(foreign_key="teams.id", index=True)
    user_id: UUID = Field(foreign_key="users.id", index=True)

    # Role
    role: TeamRole = Field(
        default=TeamRole.MEMBER,
        sa_column=Column(
            Enum(TeamRole, name="teamrole", values_callable=lambda enum: [e.value for e in enum]),
            nullable=False,
            server_default=text("'member'"),
        ),
        description="Team membership role",
    )

    # Timestamps
    joined_at: datetime = Field(default_factory=utcnow_naive, description="When user joined team")

    team: Team = Relationship(back_populates="members")
    user: User = Relationship()


# =============================================================================
# Projects - Project-level RBAC
# =============================================================================


class ProjectRole(StrEnum):
    """Role of a user within a project."""

    OWNER = "project_owner"
    MAINTAINER = "project_maintainer"
    CONTRIBUTOR = "project_contributor"
    VIEWER = "project_viewer"


class ProjectVisibility(StrEnum):
    """Visibility level for a project."""

    PRIVATE = "private"  # Only explicit grants (plus org owner/admin override)
    PROJECT = "project"  # Explicit grants via direct membership or teams
    ORG = "org"  # All org members get default role


class Project(TimestampMixin, table=True):
    """A project within an organization.

    Links Postgres (auth source of truth) to graph entities (graph_project_id).
    Project RBAC is enforced here; graph queries filter by allowed projects.
    """

    __tablename__ = "projects"
    __table_args__ = (
        Index("ix_projects_org_slug_unique", "organization_id", "slug", unique=True),
        Index(
            "ix_projects_org_graph_id_unique", "organization_id", "graph_project_id", unique=True
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    organization_id: UUID = Field(foreign_key="organizations.id", index=True)

    # Identity
    name: str = Field(max_length=255, description="Project display name")
    slug: str = Field(max_length=64, description="URL-safe slug (unique per org)")
    description: str | None = Field(default=None, sa_type=Text, description="Project description")

    # Graph linkage
    graph_project_id: str = Field(
        max_length=64,
        description="Entity ID in knowledge graph (e.g. project_abc123)",
    )

    # Access control
    visibility: ProjectVisibility = Field(
        default=ProjectVisibility.ORG,
        sa_column=Column(
            Enum(
                ProjectVisibility,
                name="projectvisibility",
                values_callable=lambda enum: [e.value for e in enum],
            ),
            nullable=False,
            server_default=text("'org'"),
        ),
        description="Project visibility level",
    )
    default_role: ProjectRole = Field(
        default=ProjectRole.VIEWER,
        sa_column=Column(
            Enum(
                ProjectRole,
                name="projectrole",
                values_callable=lambda enum: [e.value for e in enum],
            ),
            nullable=False,
            server_default=text("'project_viewer'"),
        ),
        description="Default role for org members when visibility=org",
    )
    owner_user_id: UUID = Field(
        foreign_key="users.id",
        index=True,
        description="Project owner (can transfer ownership)",
    )

    # Settings
    settings: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        description="Project settings",
    )

    # Shared project marker
    is_shared: bool = Field(
        default=False,
        description="True if this is the org's shared project for org-wide knowledge",
    )

    # Relationships
    organization: Organization = Relationship()
    owner: User = Relationship()
    members: list["ProjectMember"] = Relationship(
        back_populates="project",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    team_grants: list["TeamProject"] = Relationship(
        back_populates="project",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )

    def __repr__(self) -> str:
        return f"<Project {self.slug} org={self.organization_id}>"


class ProjectMember(TimestampMixin, table=True):
    """Direct membership linking a user to a project."""

    __tablename__ = "project_members"
    __table_args__ = (
        Index("ix_project_members_project_user_unique", "project_id", "user_id", unique=True),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    organization_id: UUID = Field(
        foreign_key="organizations.id",
        index=True,
        description="Denormalized for RLS policies",
    )
    project_id: UUID = Field(foreign_key="projects.id", index=True)
    user_id: UUID = Field(foreign_key="users.id", index=True)

    role: ProjectRole = Field(
        default=ProjectRole.CONTRIBUTOR,
        sa_column=Column(
            Enum(
                ProjectRole,
                name="projectrole",
                values_callable=lambda enum: [e.value for e in enum],
                create_type=False,  # Already created by Project
            ),
            nullable=False,
            server_default=text("'project_contributor'"),
        ),
        description="Project membership role",
    )

    # Timestamps
    joined_at: datetime = Field(
        default_factory=utcnow_naive, description="When user joined project"
    )

    project: Project = Relationship(back_populates="members")
    user: User = Relationship()

    def __repr__(self) -> str:
        return f"<ProjectMember project={self.project_id} user={self.user_id} role={self.role}>"


class TeamProject(TimestampMixin, table=True):
    """Team-level grant to a project (all team members inherit this role)."""

    __tablename__ = "team_projects"
    __table_args__ = (
        Index("ix_team_projects_team_project_unique", "team_id", "project_id", unique=True),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    organization_id: UUID = Field(
        foreign_key="organizations.id",
        index=True,
        description="Denormalized for RLS policies",
    )
    team_id: UUID = Field(foreign_key="teams.id", index=True)
    project_id: UUID = Field(foreign_key="projects.id", index=True)

    role: ProjectRole = Field(
        default=ProjectRole.CONTRIBUTOR,
        sa_column=Column(
            Enum(
                ProjectRole,
                name="projectrole",
                values_callable=lambda enum: [e.value for e in enum],
                create_type=False,
            ),
            nullable=False,
            server_default=text("'project_contributor'"),
        ),
        description="Role granted to all team members for this project",
    )

    project: Project = Relationship(back_populates="team_grants")
    team: Team = Relationship()

    def __repr__(self) -> str:
        return f"<TeamProject team={self.team_id} project={self.project_id} role={self.role}>"


class ApiKeyProjectScope(TimestampMixin, table=True):
    """Restrict an API key to specific projects (optional least-privilege)."""

    __tablename__ = "api_key_project_scopes"
    __table_args__ = (
        Index(
            "ix_api_key_project_scopes_key_project_unique", "api_key_id", "project_id", unique=True
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    api_key_id: UUID = Field(foreign_key="api_keys.id", index=True)
    project_id: UUID = Field(foreign_key="projects.id", index=True)

    # Optional: further restrict operations on this project
    allowed_operations: list[str] = Field(
        default_factory=list,
        sa_type=ARRAY(String),
        description="Allowed operations (empty = all allowed by key scopes)",
    )

    def __repr__(self) -> str:
        return f"<ApiKeyProjectScope key={self.api_key_id} project={self.project_id}>"


# =============================================================================
# CrawlSource - Documentation sources to crawl
# =============================================================================


class CrawlSource(TimestampMixin, table=True):
    """A documentation source to be crawled.

    Tracks configuration and status for each documentation source.
    One source can have many documents.
    """

    __tablename__ = "crawl_sources"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    organization_id: UUID = Field(
        foreign_key="organizations.id",
        index=True,
        description="Organization that owns this source",
    )
    name: str = Field(max_length=255, index=True, description="Human-readable source name")
    url: str = Field(max_length=2048, unique=True, description="Base URL or path")
    source_type: SourceType = Field(default=SourceType.WEBSITE, description="Type of source")
    description: str | None = Field(default=None, sa_type=Text, description="Source description")

    # Crawl configuration
    crawl_depth: int = Field(default=2, ge=0, le=10, description="Max link follow depth")
    include_patterns: list[str] = Field(
        default_factory=list,
        sa_type=ARRAY(String),
        description="URL patterns to include (regex)",
    )
    exclude_patterns: list[str] = Field(
        default_factory=list,
        sa_type=ARRAY(String),
        description="URL patterns to exclude (regex)",
    )
    respect_robots: bool = Field(default=True, description="Respect robots.txt")

    # Crawl status
    crawl_status: CrawlStatus = Field(default=CrawlStatus.PENDING, description="Current status")
    current_job_id: str | None = Field(
        default=None, max_length=64, description="Active crawl job ID"
    )
    last_crawled_at: datetime | None = Field(default=None, description="Last successful crawl")
    last_error: str | None = Field(default=None, sa_type=Text, description="Last error message")

    # Statistics
    document_count: int = Field(default=0, ge=0, description="Number of documents crawled")
    chunk_count: int = Field(default=0, ge=0, description="Total chunks across documents")
    total_tokens: int = Field(default=0, ge=0, description="Total tokens processed")

    # Auto-detected metadata
    tags: list[str] = Field(
        default_factory=list,
        sa_type=ARRAY(String),
        description="Auto-detected tags from content",
    )
    categories: list[str] = Field(
        default_factory=list,
        sa_type=ARRAY(String),
        description="Content categories (tutorial, reference, etc.)",
    )
    favicon_url: str | None = Field(
        default=None,
        max_length=2048,
        description="URL to site favicon (auto-detected during crawl)",
    )

    # Relationships
    documents: list["CrawledDocument"] = Relationship(
        back_populates="source",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )

    def __repr__(self) -> str:
        return f"<CrawlSource {self.name} ({self.url})>"

    @field_validator("last_crawled_at", "created_at", "updated_at", mode="before")
    @classmethod
    def strip_timezone(cls, v: datetime | None) -> datetime | None:
        """Ensure datetimes are naive (PostgreSQL TIMESTAMP WITHOUT TIME ZONE)."""
        if v is not None and v.tzinfo is not None:
            return v.replace(tzinfo=None)
        return v


# =============================================================================
# CrawledDocument - Raw crawled pages
# =============================================================================


class CrawledDocument(TimestampMixin, table=True):
    """A crawled document/page from a source.

    Stores the raw content and metadata for each crawled page.
    One document has many chunks.
    """

    __tablename__ = "crawled_documents"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    source_id: UUID = Field(foreign_key="crawl_sources.id", index=True)
    url: str = Field(max_length=2048, unique=True, description="Full page URL")
    title: str = Field(max_length=512, default="", description="Page title")

    # Content
    raw_content: str = Field(default="", sa_type=Text, description="Raw HTML/markdown")
    content: str = Field(default="", sa_type=Text, description="Extracted clean text")
    content_hash: str = Field(max_length=64, default="", description="SHA256 of content")

    # Hierarchy
    parent_url: str | None = Field(default=None, max_length=2048, description="Parent page URL")
    section_path: list[str] = Field(
        default_factory=list,
        sa_type=ARRAY(String),
        description="Breadcrumb path",
    )
    depth: int = Field(default=0, ge=0, description="Depth from source root")

    # Metadata
    language: str | None = Field(default=None, max_length=10, description="Primary language code")
    word_count: int = Field(default=0, ge=0, description="Word count")
    token_count: int = Field(default=0, ge=0, description="Estimated token count")
    has_code: bool = Field(default=False, description="Contains code blocks")
    is_index: bool = Field(default=False, description="Is an index/listing page")

    # Extracted data
    headings: list[str] = Field(
        default_factory=list, sa_type=ARRAY(String), description="Page headings"
    )
    links: list[str] = Field(
        default_factory=list, sa_type=ARRAY(String), description="Outgoing links"
    )
    code_languages: list[str] = Field(
        default_factory=list, sa_type=ARRAY(String), description="Languages in code blocks"
    )

    # Crawl metadata
    crawled_at: datetime = Field(
        default_factory=utcnow_naive,
        description="When this page was crawled",
    )
    http_status: int | None = Field(default=None, description="HTTP response status")

    # Relationships
    source: CrawlSource = Relationship(back_populates="documents")
    chunks: list["DocumentChunk"] = Relationship(
        back_populates="document",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )

    def __repr__(self) -> str:
        return f"<CrawledDocument {self.title or self.url[:50]}>"


# =============================================================================
# DocumentChunk - Chunked content with embeddings
# =============================================================================


class DocumentChunk(TimestampMixin, table=True):
    """A chunk of document content with embedding.

    Stores chunked content for hybrid retrieval:
    - Dense vector for semantic search (pgvector)
    - Full text for BM25 search (tsvector)
    - Sparse vector for learned sparse retrieval
    """

    __tablename__ = "document_chunks"
    __table_args__ = (
        # Full-text search index
        Index(
            "ix_chunks_content_fts",
            text("to_tsvector('english', content)"),
            postgresql_using="gin",
        ),
        # Vector similarity index (IVFFlat for speed, HNSW for accuracy)
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    document_id: UUID = Field(foreign_key="crawled_documents.id", index=True)

    # Chunk identification
    chunk_index: int = Field(ge=0, description="Position in document")
    chunk_type: ChunkType = Field(default=ChunkType.TEXT, description="Type of chunk")

    # Content
    content: str = Field(sa_type=Text, description="Chunk text content")
    context: str | None = Field(
        default=None,
        sa_type=Text,
        description="Contextual prefix (Anthropic technique)",
    )
    token_count: int = Field(default=0, ge=0, description="Token count for this chunk")

    # Location in document
    start_char: int = Field(default=0, ge=0, description="Start character offset")
    end_char: int = Field(default=0, ge=0, description="End character offset")
    heading_path: list[str] = Field(
        default_factory=list, sa_type=ARRAY(String), description="Heading hierarchy to this chunk"
    )

    # Embeddings - using 1536 dims for OpenAI ada-002
    # Will add support for other models via config
    embedding: Any = Field(
        default=None,
        sa_column=Column(Vector(1536), nullable=True),
        description="Dense embedding vector",
    )

    # Code-specific metadata
    language: str | None = Field(default=None, max_length=50, description="Code language if code")
    is_complete: bool = Field(default=True, description="Is this a complete code block")

    # Quality signals
    has_entities: bool = Field(default=False, description="Contains named entities")
    entity_ids: list[str] = Field(
        default_factory=list, sa_type=ARRAY(String), description="Extracted entity UUIDs"
    )

    # Relationships
    document: CrawledDocument = Relationship(back_populates="chunks")

    def __repr__(self) -> str:
        return f"<DocumentChunk {self.id} [{self.chunk_type}]>"


# =============================================================================
# SystemSettings - System-wide configuration stored in database
# =============================================================================


class SystemSetting(TimestampMixin, table=True):
    """System-wide configuration settings stored in the database.

    Used for storing API keys and other configuration that should persist
    in the database rather than environment variables. Supports encryption
    for sensitive values.

    Priority order for settings lookup:
    1. Database (this table)
    2. Environment variable
    3. Default value
    """

    __tablename__ = "system_settings"

    key: str = Field(
        primary_key=True,
        max_length=128,
        description="Setting key (e.g., 'openai_api_key')",
    )
    value: str = Field(
        sa_type=Text,
        description="Setting value (encrypted if is_secret=True)",
    )
    is_secret: bool = Field(
        default=False,
        description="Whether this value is encrypted and should be masked in responses",
    )
    description: str | None = Field(
        default=None,
        max_length=512,
        description="Human-readable description of the setting",
    )

    def __repr__(self) -> str:
        masked = "***" if self.is_secret else self.value[:20]
        return f"<SystemSetting key={self.key!r} value={masked!r}>"


# =============================================================================
# BackupSettings - Per-organization backup configuration
# =============================================================================


class BackupSettings(TimestampMixin, table=True):
    """Per-organization backup configuration.

    Stores backup schedule, retention, and preferences for each organization.
    Only one BackupSettings row per organization.
    """

    __tablename__ = "backup_settings"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    organization_id: UUID = Field(
        foreign_key="organizations.id",
        unique=True,
        index=True,
        description="Organization this setting belongs to",
    )

    # Enable/disable scheduled backups
    enabled: bool = Field(default=True, description="Enable scheduled automatic backups")

    # Schedule (cron expression)
    schedule: str = Field(
        default="0 2 * * *",
        max_length=64,
        description="Cron schedule for automatic backups (default: 2 AM daily)",
    )

    # Retention policy
    retention_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="Number of days to retain backups before auto-cleanup",
    )

    # Backup options
    include_postgres: bool = Field(default=True, description="Include PostgreSQL in backups")
    include_graph: bool = Field(default=True, description="Include knowledge graph in backups")

    # Last backup info (denormalized for quick access)
    last_backup_at: datetime | None = Field(default=None, description="When last backup completed")
    last_backup_id: str | None = Field(
        default=None, max_length=64, description="ID of last completed backup"
    )

    def __repr__(self) -> str:
        return f"<BackupSettings org={self.organization_id} enabled={self.enabled}>"


class BackupStatus(StrEnum):
    """Status of a backup operation."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class Backup(TimestampMixin, table=True):
    """Record of a backup archive.

    Tracks individual backup archives with metadata for UI display
    and lifecycle management.
    """

    __tablename__ = "backups"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    organization_id: UUID = Field(
        foreign_key="organizations.id",
        index=True,
        description="Organization this backup belongs to",
    )

    # Backup identification
    backup_id: str = Field(
        max_length=64,
        unique=True,
        index=True,
        description="Unique backup identifier (e.g., backup_20260110_153045)",
    )

    # Status (stored as VARCHAR, use BackupStatus.X.value when setting)
    status: str = Field(
        default=BackupStatus.PENDING.value,
        sa_column=Column(String(32), nullable=False, server_default=text("'pending'")),
        description="Current backup status (pending, in_progress, completed, failed)",
    )
    job_id: str | None = Field(default=None, max_length=128, description="arq job ID for tracking")

    # Archive details
    filename: str | None = Field(default=None, max_length=255, description="Archive filename")
    file_path: str | None = Field(default=None, max_length=1024, description="Full path to archive")
    size_bytes: int = Field(default=0, ge=0, description="Archive size in bytes")

    # Backup contents
    include_postgres: bool = Field(default=True, description="Includes PostgreSQL dump")
    include_graph: bool = Field(default=True, description="Includes knowledge graph")
    entity_count: int = Field(default=0, ge=0, description="Number of graph entities")
    relationship_count: int = Field(default=0, ge=0, description="Number of graph relationships")

    # Timing
    started_at: datetime | None = Field(default=None, description="When backup started")
    completed_at: datetime | None = Field(default=None, description="When backup completed")
    duration_seconds: float = Field(default=0.0, ge=0, description="Backup duration")

    # Metadata
    error: str | None = Field(default=None, sa_type=Text, description="Error message if failed")
    triggered_by: str | None = Field(
        default=None, max_length=64, description="How backup was triggered (scheduled, manual)"
    )
    created_by_user_id: UUID | None = Field(
        default=None, foreign_key="users.id", description="User who triggered manual backup"
    )

    def __repr__(self) -> str:
        return f"<Backup {self.backup_id} status={self.status}>"


# =============================================================================
# AgentMessage - Chat history for agent sessions
# =============================================================================


class AgentMessage(SQLModel, table=True):
    """A message in an agent chat session.

    Stores agent execution messages for UI reconstruction after page reload.
    Full tool outputs are NOT stored - only summaries/previews to keep size reasonable.
    Real-time streaming uses WebSocket; this is for persistence.
    """

    __tablename__ = "agent_messages"
    __table_args__ = (
        Index("ix_agent_messages_agent_num", "agent_id", "message_num"),
        Index("ix_agent_messages_parent", "agent_id", "parent_tool_use_id"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    agent_id: str = Field(
        max_length=64,
        index=True,
        description="Agent entity ID from knowledge graph",
    )
    organization_id: UUID = Field(
        foreign_key="organizations.id",
        index=True,
        description="Organization that owns this agent",
    )

    message_num: int = Field(ge=0, description="Sequence number within agent session")
    role: AgentMessageRole = Field(description="Who sent the message")
    type: AgentMessageType = Field(
        default=AgentMessageType.text, description="Type of message content"
    )
    content: str = Field(sa_type=Text, description="Message content (summary for tool results)")

    # Tool call tracking for message pairing and subagent grouping
    tool_id: str | None = Field(
        default=None,
        max_length=64,
        index=True,
        description="Tool use ID for this message (for pairing calls with results)",
    )
    parent_tool_use_id: str | None = Field(
        default=None,
        max_length=64,
        index=True,
        description="Parent Task tool ID when this message is from a subagent",
    )

    extra: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        description="Icon, tool_name, is_error, etc.",
    )

    created_at: datetime = Field(
        default_factory=utcnow_naive,
        sa_column=Column(DateTime, nullable=False, server_default=text("now()"), index=True),
        description="When message was created",
    )


# =============================================================================
# Inter-Agent Message Bus
# =============================================================================


class InterAgentMessage(TimestampMixin, table=True):
    """A message in the inter-agent message bus.

    Enables agent-to-agent communication for distributed execution:
    - Progress updates (broadcast to orchestrator/UI)
    - Query/response (blocking asks between agents)
    - Review requests (code review workflow)
    - Blockers and delegations

    Messages are persisted for audit trail and async pickup.
    Real-time delivery uses Redis pub/sub.
    """

    __tablename__ = "inter_agent_messages"
    __table_args__ = (
        Index("ix_inter_agent_to_agent", "to_agent_id", "read_at"),
        Index("ix_inter_agent_response", "response_to_id"),
        Index("ix_inter_agent_org_created", "organization_id", "created_at"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    organization_id: UUID = Field(
        foreign_key="organizations.id",
        index=True,
        description="Organization context",
    )

    # Sender/receiver
    from_agent_id: str = Field(
        max_length=64,
        index=True,
        description="Agent that sent this message",
    )
    to_agent_id: str | None = Field(
        default=None,
        max_length=64,
        index=True,
        description="Target agent (None = broadcast to orchestrator)",
    )

    # Message type and content
    message_type: str = Field(
        max_length=32,
        index=True,
        description="Type of message (see InterAgentMessageType)",
    )
    subject: str = Field(
        max_length=255,
        description="Short subject line for UI display",
    )
    content: str = Field(
        sa_type=Text,
        description="Full message content",
    )

    # Response tracking for query/response pattern
    response_to_id: UUID | None = Field(
        default=None,
        foreign_key="inter_agent_messages.id",
        description="If this is a response, the original message ID",
    )
    requires_response: bool = Field(
        default=False,
        description="True if sender is waiting for response (blocking query)",
    )

    # Priority and context
    priority: int = Field(
        default=0,
        ge=0,
        le=10,
        description="0=normal, 10=critical (for ordering)",
    )
    context: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        description="Additional context (file paths, line numbers, etc.)",
    )

    # Delivery tracking
    read_at: datetime | None = Field(
        default=None,
        description="When the target agent read this message",
    )
    responded_at: datetime | None = Field(
        default=None,
        description="When a response was received (for blocking queries)",
    )


# =============================================================================
# Sandboxes - Ephemeral execution environments
# =============================================================================


class SandboxStatus(StrEnum):
    """Status of a sandbox environment."""

    PENDING = "pending"
    STARTING = "starting"
    RUNNING = "running"
    SUSPENDING = "suspending"
    SUSPENDED = "suspended"
    TERMINATING = "terminating"
    FAILED = "failed"
    DELETED = "deleted"


class Sandbox(TimestampMixin, table=True):
    """A sandbox execution environment for isolated task execution."""

    __tablename__ = "sandboxes"
    __table_args__ = (
        Index("ix_sandboxes_org_status", "organization_id", "status"),
        UniqueConstraint("organization_id", "user_id", name="ix_sandboxes_org_user_unique"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    organization_id: UUID = Field(
        foreign_key="organizations.id",
        index=True,
        description="Organization that owns this sandbox",
    )
    user_id: UUID | None = Field(
        default=None,
        foreign_key="users.id",
        index=True,
        description="User who requested this sandbox",
    )

    name: str = Field(
        default="sandbox",
        max_length=255,
        index=True,
        description="Sandbox display name",
    )
    status: str = Field(
        default=SandboxStatus.PENDING.value,
        sa_column=Column(
            String(32),
            nullable=False,
            server_default=text("'pending'"),
        ),
        description="Current sandbox lifecycle status",
    )

    # Runner association
    runner_id: UUID | None = Field(
        default=None,
        foreign_key="runners.id",
        index=True,
        description="Runner currently connected to this sandbox",
    )

    # Runtime identity
    image: str = Field(
        default="ghcr.io/hyperb1iss/sibyl-sandbox:latest",
        max_length=512,
        description="Container image used by this sandbox",
    )
    namespace: str | None = Field(
        default=None,
        max_length=255,
        index=True,
        description="Kubernetes namespace for this sandbox",
    )
    pod_name: str | None = Field(
        default=None,
        max_length=255,
        index=True,
        description="Kubernetes pod name for this sandbox",
    )

    # Resource profile
    cpu_request: str = Field(default="250m", max_length=32, description="Requested CPU")
    cpu_limit: str = Field(default="1000m", max_length=32, description="CPU limit")
    memory_request: str = Field(default="512Mi", max_length=32, description="Requested memory")
    memory_limit: str = Field(default="2Gi", max_length=32, description="Memory limit")
    ephemeral_storage_request: str = Field(
        default="1Gi",
        max_length=32,
        description="Requested ephemeral storage",
    )
    ephemeral_storage_limit: str = Field(
        default="4Gi",
        max_length=32,
        description="Ephemeral storage limit",
    )

    # Time limits
    idle_ttl_seconds: int = Field(
        default=1800,
        ge=60,
        le=86400,
        description="Auto-stop sandbox after this many idle seconds",
    )
    max_lifetime_seconds: int = Field(
        default=14400,
        ge=300,
        le=604800,
        description="Maximum total lifetime for sandbox in seconds",
    )

    # Runtime timestamps
    last_heartbeat: datetime | None = Field(
        default=None,
        index=True,
        description="Last runtime heartbeat from sandbox controller",
    )
    started_at: datetime | None = Field(default=None, description="When sandbox became running")
    stopped_at: datetime | None = Field(default=None, description="When sandbox stopped")
    expires_at: datetime | None = Field(default=None, description="Hard expiry timestamp")
    error_message: str | None = Field(
        default=None,
        sa_type=Text,
        description="Latest runtime/provisioning error for this sandbox",
    )

    # Opaque metadata for runtime/reconciler
    context: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        description="Runtime metadata (labels, node info, annotations)",
    )

    def __repr__(self) -> str:
        return f"<Sandbox {self.name} status={self.status}>"


class SandboxTaskStatus(StrEnum):
    """Status for work executed inside a sandbox."""

    QUEUED = "queued"
    DISPATCHED = "dispatched"
    ACKED = "acked"
    RUNNING = "running"
    RETRY = "retry"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class SandboxTask(TimestampMixin, table=True):
    """A task execution record tied to a sandbox."""

    __tablename__ = "sandbox_tasks"
    __table_args__ = (
        Index("ix_sandbox_tasks_sandbox_created", "sandbox_id", "created_at"),
        Index(
            "ix_sandbox_tasks_idempotency",
            "organization_id",
            "sandbox_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    sandbox_id: UUID = Field(
        foreign_key="sandboxes.id",
        index=True,
        description="Sandbox this task ran in",
    )
    organization_id: UUID = Field(
        foreign_key="organizations.id",
        index=True,
        description="Organization context for this task",
    )
    runner_id: UUID | None = Field(
        default=None,
        foreign_key="runners.id",
        index=True,
        description="Runner that dispatched this task",
    )
    task_id: str | None = Field(
        default=None,
        max_length=64,
        index=True,
        description="Associated graph task entity ID",
    )
    task_type: str = Field(
        default="agent_execution",
        max_length=64,
        index=True,
        description="Task execution type",
    )
    idempotency_key: str | None = Field(
        default=None,
        max_length=255,
        index=True,
        description="Optional idempotency key for dedupe",
    )

    status: str = Field(
        default=SandboxTaskStatus.QUEUED.value,
        sa_column=Column(
            String(32),
            nullable=False,
            server_default=text("'queued'"),
        ),
        description="Sandbox task execution status",
    )
    command: str | None = Field(
        default=None,
        sa_type=Text,
        description="Optional command executed in sandbox",
    )
    working_directory: str | None = Field(
        default=None,
        max_length=1024,
        description="Working directory inside sandbox",
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        description="Dispatcher payload (task assignment envelope)",
    )
    result: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        description="Dispatcher/task result payload",
    )
    attempt_count: int = Field(default=0, ge=0, description="Number of dispatch attempts")
    max_attempts: int = Field(default=3, ge=1, description="Maximum retry attempts")

    # Execution output
    exit_code: int | None = Field(default=None, description="Process exit code")
    stdout_preview: str | None = Field(
        default=None,
        sa_type=Text,
        description="Truncated stdout preview",
    )
    stderr_preview: str | None = Field(
        default=None,
        sa_type=Text,
        description="Truncated stderr preview",
    )
    error_message: str | None = Field(
        default=None,
        sa_type=Text,
        description="Error details when execution fails",
    )

    requested_at: datetime = Field(default_factory=utcnow_naive, description="When task was queued")
    last_dispatch_at: datetime | None = Field(default=None, description="Last dispatch attempt time")
    acked_at: datetime | None = Field(default=None, description="When runner acknowledged task")
    started_at: datetime | None = Field(default=None, description="When execution started")
    completed_at: datetime | None = Field(default=None, description="When execution completed")
    failed_at: datetime | None = Field(default=None, description="When task entered failed state")

    context: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        description="Task context (env, invocation metadata, policy flags)",
    )

    def __repr__(self) -> str:
        return f"<SandboxTask {self.id} status={self.status}>"


class UserSSHKey(TimestampMixin, table=True):
    """User-managed SSH public key used for sandbox access."""

    __tablename__ = "user_ssh_keys"
    __table_args__ = (
        Index("ix_user_ssh_keys_user_fingerprint_unique", "user_id", "fingerprint", unique=True),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(
        foreign_key="users.id",
        index=True,
        description="Owner of this SSH key",
    )

    name: str = Field(max_length=255, description="Display name for this key")
    public_key: str = Field(sa_type=Text, description="OpenSSH public key")
    key_type: str = Field(
        default="ssh-ed25519",
        max_length=32,
        description="Key algorithm type",
    )
    fingerprint: str = Field(
        max_length=128,
        index=True,
        description="Canonical fingerprint for dedupe and lookup",
    )
    comment: str | None = Field(
        default=None,
        max_length=255,
        description="Optional user-supplied key comment",
    )
    is_active: bool = Field(default=True, description="Whether this key may be used")
    last_used_at: datetime | None = Field(default=None, description="Last successful key usage")

    extra: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        description="Additional metadata for audit/integration",
    )

    def __repr__(self) -> str:
        return f"<UserSSHKey user={self.user_id} fingerprint={self.fingerprint}>"


# =============================================================================
# Runner - Distributed agent execution hosts
# =============================================================================


class RunnerStatus(StrEnum):
    """Status of a runner."""

    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"
    DRAINING = "draining"  # Finishing current work, accepting no new


class Runner(TimestampMixin, table=True):
    """A registered runner that executes agents.

    Runners connect to Sibyl Core via WebSocket, receive task assignments,
    and spawn Claude Code agents with filesystem access. Each runner
    tracks its capabilities, current load, and project affinity.

    Architecture note: Runner IDENTITY (who it is) stays in the graph.
    This table is for OPERATIONAL STATE only (heartbeat, status, load).
    """

    __tablename__ = "runners"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    organization_id: UUID = Field(
        foreign_key="organizations.id",
        index=True,
        description="Organization this runner belongs to",
    )
    user_id: UUID = Field(
        foreign_key="users.id",
        index=True,
        description="User who registered this runner (OAuth identity)",
    )

    # Identity (links to graph entity)
    graph_runner_id: str = Field(
        max_length=64,
        unique=True,
        index=True,
        description="Entity ID in knowledge graph (e.g., runner_abc123)",
    )
    name: str = Field(max_length=255, description="Runner display name")
    hostname: str = Field(max_length=255, description="Host machine name")
    sandbox_id: UUID | None = Field(
        default=None,
        foreign_key="sandboxes.id",
        index=True,
        description="Attached sandbox for this runner, if isolated",
    )
    is_sandbox_runner: bool = Field(
        default=False,
        index=True,
        description="Whether this runner executes only within sandbox constraints",
    )

    # Capabilities
    capabilities: list[str] = Field(
        default_factory=list,
        sa_type=ARRAY(String),
        description="Runner capabilities (e.g., docker, gpu, large_context)",
    )
    max_concurrent_agents: int = Field(
        default=3,
        ge=1,
        le=20,
        description="Max agents this runner can execute simultaneously",
    )
    current_agent_count: int = Field(
        default=0,
        ge=0,
        description="Current number of active agents on this runner",
    )

    # Status - stored as string to avoid async enum issues
    status: str = Field(
        default=RunnerStatus.OFFLINE.value,
        sa_column=Column(
            String(32),
            nullable=False,
            server_default=text("'offline'"),
        ),
        description="Current runner status",
    )
    last_heartbeat: datetime | None = Field(
        default=None,
        index=True,
        description="Last heartbeat timestamp",
    )
    sandbox_heartbeat_at: datetime | None = Field(
        default=None,
        description="Last sandbox-specific heartbeat (for idle timeout tracking)",
    )

    # Connection info
    websocket_session_id: str | None = Field(
        default=None,
        max_length=64,
        description="Current WebSocket session ID",
    )
    client_version: str | None = Field(
        default=None,
        max_length=32,
        description="Runner client version (e.g., 0.1.0)",
    )

    def __repr__(self) -> str:
        return f"<Runner {self.name} status={self.status}>"


class RunnerProject(TimestampMixin, table=True):
    """Links a runner to projects it has warm worktrees for.

    When a runner has a warm worktree for a project, it can be
    preferentially assigned tasks for that project (project affinity).
    """

    __tablename__ = "runner_projects"
    __table_args__ = (
        Index("ix_runner_projects_runner_project_unique", "runner_id", "project_id", unique=True),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    runner_id: UUID = Field(foreign_key="runners.id", index=True)
    project_id: UUID = Field(foreign_key="projects.id", index=True)

    # Worktree info
    worktree_path: str = Field(
        max_length=1024,
        description="Local path to worktree on runner",
    )
    worktree_branch: str | None = Field(
        default=None,
        max_length=255,
        description="Current branch in worktree",
    )
    last_used_at: datetime | None = Field(
        default=None,
        description="When this worktree was last used",
    )

    def __repr__(self) -> str:
        return f"<RunnerProject runner={self.runner_id} project={self.project_id}>"


# =============================================================================
# AgentState - Operational state for agents (identity in graph)
# =============================================================================


class AgentOperationalStatus(StrEnum):
    """Operational status of an agent.

    Aligned with AgentStatus from sibyl_core.models for consistency.
    """

    INITIALIZING = "initializing"  # Setting up worktree and environment
    WORKING = "working"  # Actively executing tasks
    PAUSED = "paused"  # User-initiated pause
    WAITING_APPROVAL = "waiting_approval"  # Blocked on human approval
    WAITING_DEPENDENCY = "waiting_dependency"  # Blocked on another task
    RESUMING = "resuming"  # Recovering after restart
    COMPLETED = "completed"  # Finished successfully
    FAILED = "failed"  # Error state
    TERMINATED = "terminated"  # User stopped the agent


class AgentState(TimestampMixin, table=True):
    """Operational state for an agent.

    Agent IDENTITY (name, type, task assignment, work history) lives in
    the knowledge graph as an Entity. This table stores ephemeral
    operational state that changes frequently during execution.
    """

    __tablename__ = "agent_states"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    organization_id: UUID = Field(
        foreign_key="organizations.id",
        index=True,
        description="Organization that owns this agent",
    )

    # Link to graph entity
    graph_agent_id: str = Field(
        max_length=64,
        unique=True,
        index=True,
        description="Agent entity ID in knowledge graph",
    )

    # Execution context
    runner_id: UUID | None = Field(
        default=None,
        foreign_key="runners.id",
        index=True,
        description="Runner executing this agent (null if not assigned)",
    )
    task_id: str | None = Field(
        default=None,
        max_length=64,
        index=True,
        description="Task entity ID being worked on",
    )
    orchestrator_id: str | None = Field(
        default=None,
        max_length=64,
        index=True,
        description="Parent orchestrator ID if worker agent",
    )

    # Status - using String to match migration and avoid enum issues
    status: str = Field(
        default="initializing",
        max_length=32,
        index=True,
        description="Current operational status (see AgentOperationalStatus)",
    )
    last_heartbeat: datetime | None = Field(
        default=None,
        index=True,
        description="Last heartbeat from agent process",
    )
    progress_percent: int = Field(
        default=0,
        ge=0,
        le=100,
        description="Estimated progress (0-100)",
    )
    current_activity: str | None = Field(
        default=None,
        max_length=512,
        description="Current activity description for UI",
    )

    # Metrics
    tokens_used: int = Field(default=0, ge=0, description="Total tokens consumed")
    cost_usd: float = Field(default=0.0, ge=0, description="Estimated cost in USD")
    started_at: datetime | None = Field(default=None, description="When execution started")
    completed_at: datetime | None = Field(default=None, description="When execution completed")

    # Error tracking
    error_message: str | None = Field(
        default=None,
        sa_type=Text,
        description="Error message if failed",
    )
    error_count: int = Field(default=0, ge=0, description="Number of errors encountered")

    def __repr__(self) -> str:
        return f"<AgentState {self.graph_agent_id} status={self.status}>"


# =============================================================================
# OrchestratorState - Operational state for task orchestrators
# =============================================================================


class OrchestratorPhase(StrEnum):
    """Phase of the orchestrator build loop."""

    IMPLEMENT = "implement"
    REVIEW = "review"
    REWORK = "rework"
    HUMAN_REVIEW = "human_review"
    MERGE = "merge"


class OrchestratorOperationalStatus(StrEnum):
    """Operational status of an orchestrator."""

    PENDING = "pending"
    IMPLEMENTING = "implementing"
    REVIEWING = "reviewing"
    REWORKING = "reworking"
    HUMAN_REVIEW = "human_review"
    PAUSED = "paused"
    COMPLETE = "complete"
    FAILED = "failed"


class OrchestratorState(TimestampMixin, table=True):
    """Operational state for a task orchestrator.

    Orchestrator IDENTITY (config, task assignment) lives in the graph.
    This table stores the evolving state of the build loop.
    """

    __tablename__ = "orchestrator_states"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    organization_id: UUID = Field(
        foreign_key="organizations.id",
        index=True,
        description="Organization that owns this orchestrator",
    )

    # Link to graph entity
    graph_orchestrator_id: str = Field(
        max_length=64,
        unique=True,
        index=True,
        description="Orchestrator entity ID in knowledge graph",
    )
    task_id: str = Field(
        max_length=64,
        index=True,
        description="Task entity ID being orchestrated",
    )
    project_id: UUID = Field(
        foreign_key="projects.id",
        index=True,
        description="Project this orchestrator belongs to",
    )

    # Build loop state
    current_phase: OrchestratorPhase = Field(
        default=OrchestratorPhase.IMPLEMENT,
        sa_column=Column(
            Enum(
                OrchestratorPhase,
                name="orchestratorphase",
                values_callable=lambda enum: [e.value for e in enum],
            ),
            nullable=False,
            server_default=text("'implement'"),
        ),
        description="Current phase in build loop",
    )
    status: OrchestratorOperationalStatus = Field(
        default=OrchestratorOperationalStatus.PENDING,
        sa_column=Column(
            Enum(
                OrchestratorOperationalStatus,
                name="orchestratoroperationalstatus",
                values_callable=lambda enum: [e.value for e in enum],
            ),
            nullable=False,
            server_default=text("'pending'"),
        ),
        description="Current operational status",
    )

    # Quality gate config (from graph, cached here for quick access)
    gate_config: list[str] = Field(
        default_factory=list,
        sa_type=ARRAY(String),
        description="Quality gates to run (lint, typecheck, test, ai_review, security_scan, human_review)",
    )

    # Iteration tracking
    rework_count: int = Field(default=0, ge=0, description="Number of rework iterations")
    max_rework_attempts: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Max rework attempts before escalation",
    )

    # Current worker
    current_worker_id: str | None = Field(
        default=None,
        max_length=64,
        index=True,
        description="Current worker agent entity ID",
    )

    # Gate results
    gate_results: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        description="Results from each quality gate run",
    )

    # Review state
    review_feedback: str | None = Field(
        default=None,
        sa_type=Text,
        description="Feedback from AI or human review",
    )
    human_reviewer_id: UUID | None = Field(
        default=None,
        foreign_key="users.id",
        description="User assigned for human review",
    )

    # Metrics
    total_tokens: int = Field(default=0, ge=0, description="Total tokens across all iterations")
    total_cost_usd: float = Field(default=0.0, ge=0, description="Total cost in USD")
    started_at: datetime | None = Field(default=None, description="When orchestration started")
    completed_at: datetime | None = Field(default=None, description="When orchestration completed")

    # Error tracking
    error_message: str | None = Field(
        default=None,
        sa_type=Text,
        description="Error message if failed",
    )

    def __repr__(self) -> str:
        return f"<OrchestratorState {self.graph_orchestrator_id} phase={self.current_phase}>"


# =============================================================================
# Utility functions
# =============================================================================


def create_tables_sql() -> str:
    """Generate SQL for creating tables and extensions.

    Returns raw SQL for manual execution or Alembic migrations.
    """
    return """
    -- Enable pgvector extension
    CREATE EXTENSION IF NOT EXISTS vector;

    -- Create enum types
    CREATE TYPE source_type AS ENUM ('website', 'github', 'local', 'api_docs');
    CREATE TYPE crawl_status AS ENUM ('pending', 'in_progress', 'completed', 'failed', 'partial');
    CREATE TYPE chunk_type AS ENUM ('text', 'code', 'heading', 'list', 'table');
    """
