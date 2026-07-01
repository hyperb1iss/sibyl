-- =============================================================================
-- Sibyl Comprehensive Permission Data Model
-- =============================================================================
--
-- HISTORICAL REFERENCE SCHEMA (as of 2026-07-01).
-- PostgreSQL is no longer part of Sibyl's runtime; SurrealDB is the live data plane.
-- Keep this file as a reference for the RBAC model it describes -- effective-role
-- resolution and Row-Level Security (RLS) policies -- not as a schema you can apply.
-- The live implementation is the SurrealDB auth schema:
--   packages/python/sibyl-core/src/sibyl_core/backends/surreal/auth_schema.py
--
-- This schema provides multi-tier authorization:
--   1. Global Admin Roles (super-admin, system-admin)
--   2. Org-level RBAC (owner, admin, member, viewer)
--   3. Project-level RBAC with visibility controls
--   4. Team-based project access grants
--   5. API key scoped permissions
--   6. Row-Level Security (RLS) policies
--
-- Architecture (historical PostgreSQL reference model):
--   - PostgreSQL 14+ with RLS (historical; the live runtime is SurrealDB)
--   - Defense-in-depth: app-level + database-level enforcement
--   - Audit logging for all permission changes
--
-- =============================================================================

-- -----------------------------------------------------------------------------
-- EXTENSIONS
-- -----------------------------------------------------------------------------

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- -----------------------------------------------------------------------------
-- ENUM TYPES
-- -----------------------------------------------------------------------------

-- Global admin roles (platform-wide, not org-scoped)
CREATE TYPE global_role AS ENUM (
    'super_admin',    -- Full platform access, can impersonate, manage all orgs
    'system_admin'    -- System operations (maintenance, migrations), no user data
);

-- Organization roles (already exists, shown for reference)
-- CREATE TYPE organizationrole AS ENUM ('owner', 'admin', 'member', 'viewer');

-- Project roles (new)
CREATE TYPE project_role AS ENUM (
    'project_owner',        -- Full project control: settings, membership, delete
    'project_maintainer',   -- Manage tasks/epics, can't change project settings
    'project_contributor',  -- Create/update own tasks, can't manage others' work
    'project_viewer'        -- Read-only access
);

-- Project visibility
CREATE TYPE project_visibility AS ENUM (
    'private',      -- Only explicit members/teams can access
    'project',      -- All project members (default for new projects)
    'org'           -- All org members can access with default role
);

-- API key scope categories
CREATE TYPE api_scope AS ENUM (
    'mcp',              -- MCP protocol access
    'api:read',         -- REST API read operations
    'api:write',        -- REST API write operations
    'api:admin',        -- REST API admin operations
    'graph:read',       -- Knowledge graph read
    'graph:write',      -- Knowledge graph write
    'crawler:manage',   -- Crawler operations
    'project:*',        -- All projects (when scoped to org)
    'billing:read',     -- Read billing info
    'billing:manage'    -- Manage billing
);

-- -----------------------------------------------------------------------------
-- GLOBAL ADMINS TABLE
-- -----------------------------------------------------------------------------
-- Platform-level administrators, separate from org hierarchy

CREATE TABLE global_admins (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role global_role NOT NULL,

    -- Audit
    granted_by UUID REFERENCES users(id) ON DELETE SET NULL,
    granted_at TIMESTAMP NOT NULL DEFAULT now(),
    revoked_at TIMESTAMP,
    revoked_by UUID REFERENCES users(id) ON DELETE SET NULL,
    reason TEXT,

    -- Constraints
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now(),

    CONSTRAINT uq_global_admins_user_active
        UNIQUE NULLS NOT DISTINCT (user_id, revoked_at)
);

CREATE INDEX ix_global_admins_user_id ON global_admins(user_id);
CREATE INDEX ix_global_admins_role ON global_admins(role) WHERE revoked_at IS NULL;

COMMENT ON TABLE global_admins IS 'Platform-level admin roles, separate from org hierarchy';
COMMENT ON COLUMN global_admins.role IS 'super_admin: full access; system_admin: ops only';

-- -----------------------------------------------------------------------------
-- PROJECTS TABLE
-- -----------------------------------------------------------------------------
-- Canonical project records (graph entities reference these via graph_project_id)

CREATE TABLE projects (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,

    -- Identity
    name VARCHAR(255) NOT NULL,
    slug VARCHAR(64) NOT NULL,
    description TEXT,

    -- Graph linkage
    graph_project_id VARCHAR(64) NOT NULL,  -- e.g., 'project_abc123'

    -- Visibility & access control
    visibility project_visibility NOT NULL DEFAULT 'org',
    default_role project_role DEFAULT 'project_viewer',  -- Role for org members when visibility='org'

    -- Ownership (exactly one owner required)
    owner_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,

    -- Settings
    settings JSONB NOT NULL DEFAULT '{}',
    archived_at TIMESTAMP,

    -- Timestamps
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now(),

    -- Constraints
    CONSTRAINT uq_projects_org_slug UNIQUE (organization_id, slug),
    CONSTRAINT uq_projects_org_graph_id UNIQUE (organization_id, graph_project_id)
);

CREATE INDEX ix_projects_organization_id ON projects(organization_id);
CREATE INDEX ix_projects_owner_user_id ON projects(owner_user_id);
CREATE INDEX ix_projects_graph_project_id ON projects(graph_project_id);
CREATE INDEX ix_projects_visibility ON projects(visibility);
CREATE INDEX ix_projects_active ON projects(organization_id) WHERE archived_at IS NULL;

COMMENT ON TABLE projects IS 'Canonical project records linked to graph entities';
COMMENT ON COLUMN projects.visibility IS 'private: explicit access only; org: all org members';
COMMENT ON COLUMN projects.default_role IS 'Role granted to org members when visibility=org';

-- -----------------------------------------------------------------------------
-- PROJECT MEMBERS TABLE
-- -----------------------------------------------------------------------------
-- Direct user-to-project membership

CREATE TABLE project_members (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- Denormalized org_id for efficient RLS queries
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    -- Role
    role project_role NOT NULL DEFAULT 'project_contributor',

    -- Audit
    granted_by UUID REFERENCES users(id) ON DELETE SET NULL,
    granted_at TIMESTAMP NOT NULL DEFAULT now(),

    -- Timestamps
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now(),

    CONSTRAINT uq_project_members_project_user UNIQUE (project_id, user_id)
);

CREATE INDEX ix_project_members_organization_id ON project_members(organization_id);
CREATE INDEX ix_project_members_project_id ON project_members(project_id);
CREATE INDEX ix_project_members_user_id ON project_members(user_id);
CREATE INDEX ix_project_members_role ON project_members(project_id, role);

COMMENT ON TABLE project_members IS 'Direct user membership in projects';

-- -----------------------------------------------------------------------------
-- TEAM PROJECTS TABLE
-- -----------------------------------------------------------------------------
-- Team-to-project access grants

CREATE TABLE team_projects (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- Denormalized org_id for RLS
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    team_id UUID NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,

    -- Role granted to all team members
    role project_role NOT NULL DEFAULT 'project_contributor',

    -- Audit
    granted_by UUID REFERENCES users(id) ON DELETE SET NULL,
    granted_at TIMESTAMP NOT NULL DEFAULT now(),

    -- Timestamps
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now(),

    CONSTRAINT uq_team_projects_team_project UNIQUE (team_id, project_id)
);

CREATE INDEX ix_team_projects_organization_id ON team_projects(organization_id);
CREATE INDEX ix_team_projects_team_id ON team_projects(team_id);
CREATE INDEX ix_team_projects_project_id ON team_projects(project_id);

COMMENT ON TABLE team_projects IS 'Team-based project access grants';

-- -----------------------------------------------------------------------------
-- API KEY PROJECT SCOPES TABLE
-- -----------------------------------------------------------------------------
-- Scope API keys to specific projects (optional restriction)

CREATE TABLE api_key_project_scopes (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    api_key_id UUID NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,

    -- Timestamps
    created_at TIMESTAMP NOT NULL DEFAULT now(),

    CONSTRAINT uq_api_key_project_scopes UNIQUE (api_key_id, project_id)
);

CREATE INDEX ix_api_key_project_scopes_api_key_id ON api_key_project_scopes(api_key_id);
CREATE INDEX ix_api_key_project_scopes_project_id ON api_key_project_scopes(project_id);

COMMENT ON TABLE api_key_project_scopes IS 'Optional project-level restrictions for API keys';

-- -----------------------------------------------------------------------------
-- PERMISSION AUDIT LOG TABLE
-- -----------------------------------------------------------------------------
-- Immutable audit trail for permission changes

CREATE TABLE permission_audit_log (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- Context
    organization_id UUID REFERENCES organizations(id) ON DELETE SET NULL,
    project_id UUID REFERENCES projects(id) ON DELETE SET NULL,

    -- Action
    action VARCHAR(128) NOT NULL,  -- e.g., 'project.member.add', 'org.role.change'
    target_type VARCHAR(64) NOT NULL,  -- 'user', 'team', 'api_key', 'project'
    target_id UUID NOT NULL,

    -- Actor
    actor_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    actor_type VARCHAR(32) NOT NULL DEFAULT 'user',  -- 'user', 'api_key', 'system'

    -- Change details
    old_value JSONB,
    new_value JSONB,
    metadata JSONB DEFAULT '{}',

    -- Request context
    ip_address VARCHAR(64),
    user_agent VARCHAR(512),
    request_id VARCHAR(64),

    -- Timestamp (immutable)
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

-- Partition by month for efficient retention
-- CREATE TABLE permission_audit_log ... PARTITION BY RANGE (created_at);

CREATE INDEX ix_permission_audit_log_org_id ON permission_audit_log(organization_id);
CREATE INDEX ix_permission_audit_log_project_id ON permission_audit_log(project_id);
CREATE INDEX ix_permission_audit_log_action ON permission_audit_log(action);
CREATE INDEX ix_permission_audit_log_target ON permission_audit_log(target_type, target_id);
CREATE INDEX ix_permission_audit_log_actor ON permission_audit_log(actor_user_id);
CREATE INDEX ix_permission_audit_log_created_at ON permission_audit_log(created_at DESC);

COMMENT ON TABLE permission_audit_log IS 'Immutable audit trail for all permission changes';

-- -----------------------------------------------------------------------------
-- OWNERSHIP TRANSFER REQUESTS TABLE
-- -----------------------------------------------------------------------------
-- Track org/project ownership transfers (requires acceptance)

CREATE TABLE ownership_transfer_requests (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- What's being transferred
    transfer_type VARCHAR(32) NOT NULL,  -- 'organization', 'project'
    organization_id UUID REFERENCES organizations(id) ON DELETE CASCADE,
    project_id UUID REFERENCES projects(id) ON DELETE CASCADE,

    -- Parties
    from_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    to_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    -- Token for accepting
    token_hash VARCHAR(64) NOT NULL,

    -- Status
    status VARCHAR(32) NOT NULL DEFAULT 'pending',  -- pending, accepted, rejected, expired, cancelled
    expires_at TIMESTAMP NOT NULL,

    -- Resolution
    resolved_at TIMESTAMP,
    resolution_note TEXT,

    -- Timestamps
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now(),

    CONSTRAINT chk_transfer_target CHECK (
        (transfer_type = 'organization' AND organization_id IS NOT NULL AND project_id IS NULL) OR
        (transfer_type = 'project' AND project_id IS NOT NULL)
    )
);

CREATE INDEX ix_ownership_transfer_token ON ownership_transfer_requests(token_hash)
    WHERE status = 'pending';
CREATE INDEX ix_ownership_transfer_to_user ON ownership_transfer_requests(to_user_id)
    WHERE status = 'pending';

COMMENT ON TABLE ownership_transfer_requests IS 'Pending ownership transfer requests requiring acceptance';

-- =============================================================================
-- ROW-LEVEL SECURITY (RLS) POLICIES
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Session Variables for RLS
-- -----------------------------------------------------------------------------
-- Set these per-request: SET LOCAL app.user_id = '...'; SET LOCAL app.org_id = '...';

-- Helper function to get current user ID
CREATE OR REPLACE FUNCTION current_user_id()
RETURNS UUID AS $$
BEGIN
    RETURN NULLIF(current_setting('app.user_id', true), '')::UUID;
EXCEPTION WHEN OTHERS THEN
    RETURN NULL;
END;
$$ LANGUAGE plpgsql STABLE;

-- Helper function to get current org ID
CREATE OR REPLACE FUNCTION current_org_id()
RETURNS UUID AS $$
BEGIN
    RETURN NULLIF(current_setting('app.org_id', true), '')::UUID;
EXCEPTION WHEN OTHERS THEN
    RETURN NULL;
END;
$$ LANGUAGE plpgsql STABLE;

-- Helper function to check if current user is a global admin
CREATE OR REPLACE FUNCTION is_global_admin()
RETURNS BOOLEAN AS $$
BEGIN
    RETURN EXISTS (
        SELECT 1 FROM global_admins
        WHERE user_id = current_user_id()
        AND revoked_at IS NULL
    );
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER;

-- Helper function to check if current user is org owner/admin
CREATE OR REPLACE FUNCTION is_org_admin(org_id UUID)
RETURNS BOOLEAN AS $$
BEGIN
    RETURN EXISTS (
        SELECT 1 FROM organization_members
        WHERE organization_id = org_id
        AND user_id = current_user_id()
        AND role IN ('owner', 'admin')
    );
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER;

-- Helper function to get user's project role (considering all grant sources)
CREATE OR REPLACE FUNCTION get_project_role(p_project_id UUID)
RETURNS project_role AS $$
DECLARE
    v_user_id UUID := current_user_id();
    v_project projects%ROWTYPE;
    v_org_role organizationrole;
    v_direct_role project_role;
    v_team_role project_role;
    v_effective_role project_role;
BEGIN
    -- Get project info
    SELECT * INTO v_project FROM projects WHERE id = p_project_id;
    IF v_project.id IS NULL THEN
        RETURN NULL;
    END IF;

    -- Check if user is the project owner
    IF v_project.owner_user_id = v_user_id THEN
        RETURN 'project_owner';
    END IF;

    -- Check org role (owner/admin always get project_owner)
    SELECT role INTO v_org_role
    FROM organization_members
    WHERE organization_id = v_project.organization_id
    AND user_id = v_user_id;

    IF v_org_role IN ('owner', 'admin') THEN
        RETURN 'project_owner';
    END IF;

    -- Check direct project membership
    SELECT role INTO v_direct_role
    FROM project_members
    WHERE project_id = p_project_id
    AND user_id = v_user_id;

    -- Check team-based grants (get highest role)
    SELECT tp.role INTO v_team_role
    FROM team_projects tp
    JOIN team_members tm ON tm.team_id = tp.team_id
    WHERE tp.project_id = p_project_id
    AND tm.user_id = v_user_id
    ORDER BY
        CASE tp.role
            WHEN 'project_owner' THEN 1
            WHEN 'project_maintainer' THEN 2
            WHEN 'project_contributor' THEN 3
            WHEN 'project_viewer' THEN 4
        END
    LIMIT 1;

    -- Get highest role between direct and team grants
    v_effective_role := CASE
        WHEN v_direct_role = 'project_owner' OR v_team_role = 'project_owner' THEN 'project_owner'
        WHEN v_direct_role = 'project_maintainer' OR v_team_role = 'project_maintainer' THEN 'project_maintainer'
        WHEN v_direct_role = 'project_contributor' OR v_team_role = 'project_contributor' THEN 'project_contributor'
        WHEN v_direct_role = 'project_viewer' OR v_team_role = 'project_viewer' THEN 'project_viewer'
        ELSE NULL
    END;

    -- If no explicit grant but project is org-visible, use default role
    IF v_effective_role IS NULL AND v_project.visibility = 'org' AND v_org_role IS NOT NULL THEN
        v_effective_role := v_project.default_role;
    END IF;

    RETURN v_effective_role;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER;

-- Helper function to check if user can access a project
CREATE OR REPLACE FUNCTION can_access_project(p_project_id UUID)
RETURNS BOOLEAN AS $$
BEGIN
    RETURN get_project_role(p_project_id) IS NOT NULL OR is_global_admin();
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER;

-- -----------------------------------------------------------------------------
-- Enable RLS on Tables
-- -----------------------------------------------------------------------------

ALTER TABLE projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE project_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE team_projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_key_project_scopes ENABLE ROW LEVEL SECURITY;
ALTER TABLE permission_audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE global_admins ENABLE ROW LEVEL SECURITY;

-- Also enable on existing tables (if not already)
ALTER TABLE organization_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE teams ENABLE ROW LEVEL SECURITY;
ALTER TABLE team_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE crawl_sources ENABLE ROW LEVEL SECURITY;

-- -----------------------------------------------------------------------------
-- RLS Policies: Global Admins
-- -----------------------------------------------------------------------------

CREATE POLICY global_admins_select ON global_admins
    FOR SELECT
    USING (is_global_admin() OR user_id = current_user_id());

CREATE POLICY global_admins_insert ON global_admins
    FOR INSERT
    WITH CHECK (is_global_admin());

CREATE POLICY global_admins_update ON global_admins
    FOR UPDATE
    USING (is_global_admin());

-- No delete - only soft revoke via revoked_at

-- -----------------------------------------------------------------------------
-- RLS Policies: Organizations (supplement existing)
-- -----------------------------------------------------------------------------

-- Users can see orgs they're members of (or global admins see all)
CREATE POLICY org_members_select ON organization_members
    FOR SELECT
    USING (
        organization_id = current_org_id()
        OR user_id = current_user_id()
        OR is_global_admin()
    );

CREATE POLICY org_members_insert ON organization_members
    FOR INSERT
    WITH CHECK (
        is_org_admin(organization_id)
        OR is_global_admin()
    );

CREATE POLICY org_members_update ON organization_members
    FOR UPDATE
    USING (
        is_org_admin(organization_id)
        OR is_global_admin()
    );

CREATE POLICY org_members_delete ON organization_members
    FOR DELETE
    USING (
        is_org_admin(organization_id)
        OR is_global_admin()
    );

-- -----------------------------------------------------------------------------
-- RLS Policies: Projects
-- -----------------------------------------------------------------------------

CREATE POLICY projects_select ON projects
    FOR SELECT
    USING (
        organization_id = current_org_id()
        AND (
            can_access_project(id)
            OR is_global_admin()
        )
    );

CREATE POLICY projects_insert ON projects
    FOR INSERT
    WITH CHECK (
        organization_id = current_org_id()
        AND (
            is_org_admin(organization_id)
            OR EXISTS (
                SELECT 1 FROM organization_members
                WHERE organization_id = projects.organization_id
                AND user_id = current_user_id()
                AND role IN ('owner', 'admin', 'member')
            )
            OR is_global_admin()
        )
    );

CREATE POLICY projects_update ON projects
    FOR UPDATE
    USING (
        organization_id = current_org_id()
        AND (
            get_project_role(id) = 'project_owner'
            OR is_global_admin()
        )
    );

CREATE POLICY projects_delete ON projects
    FOR DELETE
    USING (
        organization_id = current_org_id()
        AND (
            -- Only project owner or org owner can delete
            owner_user_id = current_user_id()
            OR EXISTS (
                SELECT 1 FROM organization_members
                WHERE organization_id = projects.organization_id
                AND user_id = current_user_id()
                AND role = 'owner'
            )
            OR is_global_admin()
        )
    );

-- -----------------------------------------------------------------------------
-- RLS Policies: Project Members
-- -----------------------------------------------------------------------------

CREATE POLICY project_members_select ON project_members
    FOR SELECT
    USING (
        organization_id = current_org_id()
        AND (
            can_access_project(project_id)
            OR is_global_admin()
        )
    );

CREATE POLICY project_members_insert ON project_members
    FOR INSERT
    WITH CHECK (
        organization_id = current_org_id()
        AND (
            get_project_role(project_id) IN ('project_owner', 'project_maintainer')
            OR is_global_admin()
        )
    );

CREATE POLICY project_members_update ON project_members
    FOR UPDATE
    USING (
        organization_id = current_org_id()
        AND (
            get_project_role(project_id) IN ('project_owner', 'project_maintainer')
            OR is_global_admin()
        )
    );

CREATE POLICY project_members_delete ON project_members
    FOR DELETE
    USING (
        organization_id = current_org_id()
        AND (
            get_project_role(project_id) IN ('project_owner', 'project_maintainer')
            OR user_id = current_user_id()  -- Users can remove themselves
            OR is_global_admin()
        )
    );

-- -----------------------------------------------------------------------------
-- RLS Policies: Team Projects
-- -----------------------------------------------------------------------------

CREATE POLICY team_projects_select ON team_projects
    FOR SELECT
    USING (
        organization_id = current_org_id()
        AND (
            can_access_project(project_id)
            OR is_global_admin()
        )
    );

CREATE POLICY team_projects_insert ON team_projects
    FOR INSERT
    WITH CHECK (
        organization_id = current_org_id()
        AND (
            get_project_role(project_id) IN ('project_owner', 'project_maintainer')
            OR is_global_admin()
        )
    );

CREATE POLICY team_projects_update ON team_projects
    FOR UPDATE
    USING (
        organization_id = current_org_id()
        AND (
            get_project_role(project_id) IN ('project_owner', 'project_maintainer')
            OR is_global_admin()
        )
    );

CREATE POLICY team_projects_delete ON team_projects
    FOR DELETE
    USING (
        organization_id = current_org_id()
        AND (
            get_project_role(project_id) IN ('project_owner', 'project_maintainer')
            OR is_global_admin()
        )
    );

-- -----------------------------------------------------------------------------
-- RLS Policies: Teams (supplement existing)
-- -----------------------------------------------------------------------------

CREATE POLICY teams_select ON teams
    FOR SELECT
    USING (
        organization_id = current_org_id()
        OR is_global_admin()
    );

CREATE POLICY teams_insert ON teams
    FOR INSERT
    WITH CHECK (
        organization_id = current_org_id()
        AND (
            is_org_admin(organization_id)
            OR is_global_admin()
        )
    );

CREATE POLICY teams_update ON teams
    FOR UPDATE
    USING (
        organization_id = current_org_id()
        AND (
            is_org_admin(organization_id)
            OR EXISTS (
                SELECT 1 FROM team_members
                WHERE team_id = teams.id
                AND user_id = current_user_id()
                AND role = 'lead'
            )
            OR is_global_admin()
        )
    );

CREATE POLICY teams_delete ON teams
    FOR DELETE
    USING (
        organization_id = current_org_id()
        AND (
            is_org_admin(organization_id)
            OR is_global_admin()
        )
    );

-- -----------------------------------------------------------------------------
-- RLS Policies: Team Members
-- -----------------------------------------------------------------------------

CREATE POLICY team_members_select ON team_members
    FOR SELECT
    USING (
        EXISTS (
            SELECT 1 FROM teams
            WHERE teams.id = team_members.team_id
            AND teams.organization_id = current_org_id()
        )
        OR is_global_admin()
    );

CREATE POLICY team_members_insert ON team_members
    FOR INSERT
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM teams
            WHERE teams.id = team_members.team_id
            AND teams.organization_id = current_org_id()
            AND (
                is_org_admin(teams.organization_id)
                OR EXISTS (
                    SELECT 1 FROM team_members tm
                    WHERE tm.team_id = team_members.team_id
                    AND tm.user_id = current_user_id()
                    AND tm.role = 'lead'
                )
            )
        )
        OR is_global_admin()
    );

-- -----------------------------------------------------------------------------
-- RLS Policies: API Keys
-- -----------------------------------------------------------------------------

CREATE POLICY api_keys_select ON api_keys
    FOR SELECT
    USING (
        organization_id = current_org_id()
        AND (
            user_id = current_user_id()
            OR is_org_admin(organization_id)
            OR is_global_admin()
        )
    );

CREATE POLICY api_keys_insert ON api_keys
    FOR INSERT
    WITH CHECK (
        organization_id = current_org_id()
        AND user_id = current_user_id()
    );

CREATE POLICY api_keys_update ON api_keys
    FOR UPDATE
    USING (
        organization_id = current_org_id()
        AND (
            user_id = current_user_id()
            OR is_org_admin(organization_id)
            OR is_global_admin()
        )
    );

CREATE POLICY api_keys_delete ON api_keys
    FOR DELETE
    USING (
        organization_id = current_org_id()
        AND (
            user_id = current_user_id()
            OR is_org_admin(organization_id)
            OR is_global_admin()
        )
    );

-- -----------------------------------------------------------------------------
-- RLS Policies: API Key Project Scopes
-- -----------------------------------------------------------------------------

CREATE POLICY api_key_project_scopes_select ON api_key_project_scopes
    FOR SELECT
    USING (
        EXISTS (
            SELECT 1 FROM api_keys
            WHERE api_keys.id = api_key_project_scopes.api_key_id
            AND api_keys.organization_id = current_org_id()
            AND (
                api_keys.user_id = current_user_id()
                OR is_org_admin(api_keys.organization_id)
                OR is_global_admin()
            )
        )
    );

CREATE POLICY api_key_project_scopes_insert ON api_key_project_scopes
    FOR INSERT
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM api_keys
            WHERE api_keys.id = api_key_project_scopes.api_key_id
            AND api_keys.organization_id = current_org_id()
            AND api_keys.user_id = current_user_id()
        )
        AND can_access_project(project_id)
    );

-- -----------------------------------------------------------------------------
-- RLS Policies: Crawl Sources (org-scoped)
-- -----------------------------------------------------------------------------

CREATE POLICY crawl_sources_select ON crawl_sources
    FOR SELECT
    USING (
        organization_id = current_org_id()
        OR is_global_admin()
    );

CREATE POLICY crawl_sources_insert ON crawl_sources
    FOR INSERT
    WITH CHECK (
        organization_id = current_org_id()
        AND (
            is_org_admin(organization_id)
            OR EXISTS (
                SELECT 1 FROM organization_members
                WHERE organization_id = crawl_sources.organization_id
                AND user_id = current_user_id()
                AND role IN ('owner', 'admin', 'member')
            )
            OR is_global_admin()
        )
    );

CREATE POLICY crawl_sources_update ON crawl_sources
    FOR UPDATE
    USING (
        organization_id = current_org_id()
        AND (
            is_org_admin(organization_id)
            OR is_global_admin()
        )
    );

CREATE POLICY crawl_sources_delete ON crawl_sources
    FOR DELETE
    USING (
        organization_id = current_org_id()
        AND (
            is_org_admin(organization_id)
            OR is_global_admin()
        )
    );

-- -----------------------------------------------------------------------------
-- RLS Policies: Permission Audit Log
-- -----------------------------------------------------------------------------

-- Audit log is append-only, viewable by org admins
CREATE POLICY permission_audit_log_select ON permission_audit_log
    FOR SELECT
    USING (
        (organization_id = current_org_id() AND is_org_admin(organization_id))
        OR is_global_admin()
    );

CREATE POLICY permission_audit_log_insert ON permission_audit_log
    FOR INSERT
    WITH CHECK (true);  -- System can always insert

-- No update or delete policies - audit log is immutable

-- =============================================================================
-- TRIGGERS FOR AUDIT LOGGING
-- =============================================================================

-- Function to log permission changes
CREATE OR REPLACE FUNCTION log_permission_change()
RETURNS TRIGGER AS $$
DECLARE
    v_action TEXT;
    v_target_type TEXT;
    v_target_id UUID;
    v_old_value JSONB;
    v_new_value JSONB;
    v_org_id UUID;
    v_project_id UUID;
BEGIN
    -- Determine action
    v_action := TG_TABLE_NAME || '.' || lower(TG_OP);

    -- Set target based on table
    CASE TG_TABLE_NAME
        WHEN 'organization_members' THEN
            v_target_type := 'user';
            v_target_id := COALESCE(NEW.user_id, OLD.user_id);
            v_org_id := COALESCE(NEW.organization_id, OLD.organization_id);
        WHEN 'project_members' THEN
            v_target_type := 'user';
            v_target_id := COALESCE(NEW.user_id, OLD.user_id);
            v_org_id := COALESCE(NEW.organization_id, OLD.organization_id);
            v_project_id := COALESCE(NEW.project_id, OLD.project_id);
        WHEN 'team_members' THEN
            v_target_type := 'user';
            v_target_id := COALESCE(NEW.user_id, OLD.user_id);
        WHEN 'team_projects' THEN
            v_target_type := 'team';
            v_target_id := COALESCE(NEW.team_id, OLD.team_id);
            v_org_id := COALESCE(NEW.organization_id, OLD.organization_id);
            v_project_id := COALESCE(NEW.project_id, OLD.project_id);
        WHEN 'global_admins' THEN
            v_target_type := 'user';
            v_target_id := COALESCE(NEW.user_id, OLD.user_id);
        WHEN 'projects' THEN
            v_target_type := 'project';
            v_target_id := COALESCE(NEW.id, OLD.id);
            v_org_id := COALESCE(NEW.organization_id, OLD.organization_id);
            v_project_id := COALESCE(NEW.id, OLD.id);
        ELSE
            v_target_type := TG_TABLE_NAME;
            v_target_id := COALESCE(NEW.id, OLD.id);
    END CASE;

    -- Build old/new values
    IF TG_OP = 'DELETE' THEN
        v_old_value := to_jsonb(OLD);
    ELSIF TG_OP = 'UPDATE' THEN
        v_old_value := to_jsonb(OLD);
        v_new_value := to_jsonb(NEW);
    ELSE
        v_new_value := to_jsonb(NEW);
    END IF;

    -- Insert audit record
    INSERT INTO permission_audit_log (
        organization_id,
        project_id,
        action,
        target_type,
        target_id,
        actor_user_id,
        actor_type,
        old_value,
        new_value,
        ip_address,
        request_id
    ) VALUES (
        v_org_id,
        v_project_id,
        v_action,
        v_target_type,
        v_target_id,
        current_user_id(),
        CASE WHEN current_user_id() IS NOT NULL THEN 'user' ELSE 'system' END,
        v_old_value,
        v_new_value,
        current_setting('app.ip_address', true),
        current_setting('app.request_id', true)
    );

    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Create triggers for audit logging
CREATE TRIGGER audit_organization_members
    AFTER INSERT OR UPDATE OR DELETE ON organization_members
    FOR EACH ROW EXECUTE FUNCTION log_permission_change();

CREATE TRIGGER audit_project_members
    AFTER INSERT OR UPDATE OR DELETE ON project_members
    FOR EACH ROW EXECUTE FUNCTION log_permission_change();

CREATE TRIGGER audit_team_members
    AFTER INSERT OR UPDATE OR DELETE ON team_members
    FOR EACH ROW EXECUTE FUNCTION log_permission_change();

CREATE TRIGGER audit_team_projects
    AFTER INSERT OR UPDATE OR DELETE ON team_projects
    FOR EACH ROW EXECUTE FUNCTION log_permission_change();

CREATE TRIGGER audit_global_admins
    AFTER INSERT OR UPDATE OR DELETE ON global_admins
    FOR EACH ROW EXECUTE FUNCTION log_permission_change();

CREATE TRIGGER audit_projects_ownership
    AFTER UPDATE OF owner_user_id, visibility ON projects
    FOR EACH ROW EXECUTE FUNCTION log_permission_change();

-- =============================================================================
-- CONSTRAINT FUNCTIONS
-- =============================================================================

-- Ensure exactly one owner per organization
CREATE OR REPLACE FUNCTION check_org_owner_count()
RETURNS TRIGGER AS $$
DECLARE
    v_owner_count INT;
BEGIN
    -- After insert/update, check we have exactly one owner
    IF TG_OP = 'DELETE' AND OLD.role = 'owner' THEN
        SELECT COUNT(*) INTO v_owner_count
        FROM organization_members
        WHERE organization_id = OLD.organization_id AND role = 'owner';

        IF v_owner_count = 0 THEN
            RAISE EXCEPTION 'Organization must have at least one owner';
        END IF;
    END IF;

    -- After update from owner to something else
    IF TG_OP = 'UPDATE' AND OLD.role = 'owner' AND NEW.role != 'owner' THEN
        SELECT COUNT(*) INTO v_owner_count
        FROM organization_members
        WHERE organization_id = NEW.organization_id AND role = 'owner';

        IF v_owner_count = 0 THEN
            RAISE EXCEPTION 'Organization must have at least one owner';
        END IF;
    END IF;

    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER enforce_org_owner
    AFTER UPDATE OR DELETE ON organization_members
    FOR EACH ROW EXECUTE FUNCTION check_org_owner_count();

-- Ensure project owner is an org member
CREATE OR REPLACE FUNCTION check_project_owner_org_membership()
RETURNS TRIGGER AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM organization_members
        WHERE organization_id = NEW.organization_id
        AND user_id = NEW.owner_user_id
    ) THEN
        RAISE EXCEPTION 'Project owner must be a member of the organization';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER enforce_project_owner_membership
    BEFORE INSERT OR UPDATE OF owner_user_id ON projects
    FOR EACH ROW EXECUTE FUNCTION check_project_owner_org_membership();

-- =============================================================================
-- HELPER VIEWS FOR EFFICIENT PERMISSION QUERIES
-- =============================================================================

-- Materialized view for user's effective project access
-- Refresh periodically or via trigger
CREATE MATERIALIZED VIEW mv_user_project_access AS
SELECT DISTINCT
    pm.user_id,
    p.id AS project_id,
    p.organization_id,
    CASE
        WHEN p.owner_user_id = pm.user_id THEN 'project_owner'::project_role
        WHEN om.role IN ('owner', 'admin') THEN 'project_owner'::project_role
        ELSE COALESCE(
            -- Direct membership
            (SELECT role FROM project_members WHERE project_id = p.id AND user_id = pm.user_id),
            -- Team membership (highest role)
            (SELECT tp.role FROM team_projects tp
             JOIN team_members tm ON tm.team_id = tp.team_id
             WHERE tp.project_id = p.id AND tm.user_id = pm.user_id
             ORDER BY CASE tp.role
                 WHEN 'project_owner' THEN 1
                 WHEN 'project_maintainer' THEN 2
                 WHEN 'project_contributor' THEN 3
                 WHEN 'project_viewer' THEN 4
             END LIMIT 1),
            -- Default role if org-visible
            CASE WHEN p.visibility = 'org' THEN p.default_role END
        )
    END AS effective_role
FROM projects p
CROSS JOIN (SELECT DISTINCT user_id FROM organization_members) pm
JOIN organization_members om ON om.organization_id = p.organization_id AND om.user_id = pm.user_id
WHERE p.archived_at IS NULL;

CREATE UNIQUE INDEX ix_mv_user_project_access
    ON mv_user_project_access(user_id, project_id);

CREATE INDEX ix_mv_user_project_access_org
    ON mv_user_project_access(organization_id, user_id);

-- Refresh function
CREATE OR REPLACE FUNCTION refresh_user_project_access()
RETURNS void AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY mv_user_project_access;
END;
$$ LANGUAGE plpgsql;

-- =============================================================================
-- SAMPLE QUERIES FOR PERMISSION CHECKING
-- =============================================================================

-- Check if user can access a project (efficient version using materialized view)
-- SELECT EXISTS (
--     SELECT 1 FROM mv_user_project_access
--     WHERE user_id = $1 AND project_id = $2 AND effective_role IS NOT NULL
-- );

-- Get all projects user can access in an org
-- SELECT p.*, upa.effective_role
-- FROM projects p
-- JOIN mv_user_project_access upa ON upa.project_id = p.id
-- WHERE upa.user_id = $1 AND upa.organization_id = $2
-- AND upa.effective_role IS NOT NULL;

-- Check if user can write to a project
-- SELECT EXISTS (
--     SELECT 1 FROM mv_user_project_access
--     WHERE user_id = $1 AND project_id = $2
--     AND effective_role IN ('project_owner', 'project_maintainer', 'project_contributor')
-- );

-- =============================================================================
-- MIGRATION HELPERS
-- =============================================================================

-- Backfill projects from graph entities (run after migration)
-- This would be called from Python with actual graph data:
--
-- INSERT INTO projects (organization_id, name, slug, graph_project_id, owner_user_id, visibility)
-- SELECT
--     org.id,
--     $name,
--     $slug,
--     $graph_id,
--     (SELECT user_id FROM organization_members WHERE organization_id = org.id AND role = 'owner' LIMIT 1),
--     'org'
-- FROM organizations org
-- WHERE org.id = $org_id
-- ON CONFLICT (organization_id, graph_project_id) DO NOTHING;

-- Grant default access to all existing org members for backfilled projects
-- INSERT INTO project_members (organization_id, project_id, user_id, role)
-- SELECT p.organization_id, p.id, om.user_id, 'project_contributor'
-- FROM projects p
-- JOIN organization_members om ON om.organization_id = p.organization_id
-- WHERE om.role IN ('member')
-- ON CONFLICT DO NOTHING;

-- =============================================================================
-- INDEX SUMMARY FOR PERMISSION LOOKUPS
-- =============================================================================

-- Key permission lookup patterns and their indexes:
--
-- 1. "What orgs is user X a member of?"
--    ix_organization_members_user_id
--
-- 2. "What's user X's role in org Y?"
--    ix_organization_members_org_user_unique
--
-- 3. "What projects can user X access in org Y?"
--    mv_user_project_access (materialized view)
--    OR: ix_project_members_user_id + ix_team_members_user_id + ix_team_projects_team_id
--
-- 4. "Who are the members of project P?"
--    ix_project_members_project_id
--
-- 5. "What teams have access to project P?"
--    ix_team_projects_project_id
--
-- 6. "Is user X a global admin?"
--    ix_global_admins_user_id WHERE revoked_at IS NULL
--
-- 7. "What projects does API key K have access to?"
--    ix_api_key_project_scopes_api_key_id

COMMENT ON SCHEMA public IS 'Sibyl permission schema v1.0 - Multi-tier RBAC with RLS';
