"""SurrealDB schema bootstrap for Sibyl auth storage."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sibyl_core.backends.surreal.schema_helpers import execute_schema_statement, split_statements

if TYPE_CHECKING:
    from sibyl_core.backends.surreal.auth_client import SurrealAuthClient


CORE_AUTH_TABLES = ("users", "organizations", "organization_members")
EXTENDED_AUTH_TABLES = (
    "user_sessions",
    "password_reset_tokens",
    "login_history",
    "organization_invitations",
    "api_keys",
    "api_key_project_scopes",
    "api_key_memory_space_scopes",
    "oauth_connections",
    "oauth_client_registrations",
    "device_authorization_requests",
    "audit_logs",
    "teams",
    "team_members",
    "projects",
    "project_members",
    "team_projects",
    "memory_spaces",
    "memory_space_members",
)
AUTH_TABLES = (*CORE_AUTH_TABLES, *EXTENDED_AUTH_TABLES)

AUTH_SCHEMA_DEFINITIONS = """
DEFINE TABLE IF NOT EXISTS users SCHEMAFULL;
ALTER TABLE IF EXISTS users SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON users TYPE string;
DEFINE FIELD IF NOT EXISTS github_id ON users TYPE option<int>;
DEFINE FIELD IF NOT EXISTS email ON users TYPE option<string>;
DEFINE FIELD IF NOT EXISTS name ON users TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS bio ON users TYPE option<string>;
DEFINE FIELD IF NOT EXISTS timezone ON users TYPE string DEFAULT 'UTC';
DEFINE FIELD IF NOT EXISTS avatar_url ON users TYPE option<string>;
DEFINE FIELD IF NOT EXISTS email_verified_at ON users TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS last_login_at ON users TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS preferences ON users TYPE object FLEXIBLE DEFAULT {};
DEFINE FIELD IF NOT EXISTS password_salt ON users TYPE option<string>;
DEFINE FIELD IF NOT EXISTS password_hash ON users TYPE option<string>;
DEFINE FIELD IF NOT EXISTS password_iterations ON users TYPE option<int>;
DEFINE FIELD IF NOT EXISTS is_admin ON users TYPE bool DEFAULT false;
DEFINE FIELD IF NOT EXISTS created_at ON users TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at ON users TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_users_uuid ON users FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_users_email ON users FIELDS email UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_users_github_id ON users FIELDS github_id UNIQUE;

DEFINE TABLE IF NOT EXISTS organizations SCHEMAFULL;
ALTER TABLE IF EXISTS organizations SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON organizations TYPE string;
DEFINE FIELD IF NOT EXISTS name ON organizations TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS slug ON organizations TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS is_personal ON organizations TYPE bool DEFAULT false;
DEFINE FIELD IF NOT EXISTS settings ON organizations TYPE object FLEXIBLE DEFAULT {};
DEFINE FIELD IF NOT EXISTS created_at ON organizations TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at ON organizations TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_organizations_uuid ON organizations FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_organizations_slug ON organizations FIELDS slug UNIQUE;

DEFINE TABLE IF NOT EXISTS organization_members SCHEMAFULL;
ALTER TABLE IF EXISTS organization_members SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON organization_members TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON organization_members TYPE string;
DEFINE FIELD IF NOT EXISTS user_id ON organization_members TYPE string;
DEFINE FIELD IF NOT EXISTS role ON organization_members TYPE string DEFAULT 'member';
DEFINE FIELD IF NOT EXISTS created_at ON organization_members TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at ON organization_members TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_organization_members_uuid ON organization_members FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_organization_members_org ON organization_members FIELDS organization_id;
DEFINE INDEX IF NOT EXISTS idx_organization_members_user ON organization_members FIELDS user_id;
DEFINE INDEX IF NOT EXISTS idx_organization_members_org_user
    ON organization_members FIELDS organization_id, user_id UNIQUE;

DEFINE TABLE IF NOT EXISTS user_sessions SCHEMAFULL;
ALTER TABLE IF EXISTS user_sessions SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON user_sessions TYPE string;
DEFINE FIELD IF NOT EXISTS user_id ON user_sessions TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON user_sessions TYPE option<string>;
DEFINE FIELD IF NOT EXISTS token_hash ON user_sessions TYPE string;
DEFINE FIELD IF NOT EXISTS refresh_token_hash ON user_sessions TYPE option<string>;
DEFINE FIELD IF NOT EXISTS refresh_token_expires_at ON user_sessions TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS device_name ON user_sessions TYPE option<string>;
DEFINE FIELD IF NOT EXISTS device_type ON user_sessions TYPE option<string>;
DEFINE FIELD IF NOT EXISTS browser ON user_sessions TYPE option<string>;
DEFINE FIELD IF NOT EXISTS os ON user_sessions TYPE option<string>;
DEFINE FIELD IF NOT EXISTS ip_address ON user_sessions TYPE option<string>;
DEFINE FIELD IF NOT EXISTS user_agent ON user_sessions TYPE option<string>;
DEFINE FIELD IF NOT EXISTS location ON user_sessions TYPE option<string>;
DEFINE FIELD IF NOT EXISTS is_current ON user_sessions TYPE bool DEFAULT false;
DEFINE FIELD IF NOT EXISTS version ON user_sessions TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS last_active_at ON user_sessions TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS expires_at ON user_sessions TYPE datetime;
DEFINE FIELD IF NOT EXISTS revoked_at ON user_sessions TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS created_at ON user_sessions TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at ON user_sessions TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_user_sessions_uuid ON user_sessions FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_user_sessions_user ON user_sessions FIELDS user_id;
DEFINE INDEX IF NOT EXISTS idx_user_sessions_org ON user_sessions FIELDS organization_id;
DEFINE INDEX IF NOT EXISTS idx_user_sessions_token_hash ON user_sessions FIELDS token_hash;
DEFINE INDEX IF NOT EXISTS idx_user_sessions_refresh_hash
    ON user_sessions FIELDS refresh_token_hash;
DEFINE INDEX IF NOT EXISTS idx_user_sessions_last_active
    ON user_sessions FIELDS last_active_at;

DEFINE TABLE IF NOT EXISTS password_reset_tokens SCHEMAFULL;
ALTER TABLE IF EXISTS password_reset_tokens SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON password_reset_tokens TYPE string;
DEFINE FIELD IF NOT EXISTS user_id ON password_reset_tokens TYPE string;
DEFINE FIELD IF NOT EXISTS token_hash ON password_reset_tokens TYPE string;
DEFINE FIELD IF NOT EXISTS expires_at ON password_reset_tokens TYPE datetime;
DEFINE FIELD IF NOT EXISTS used_at ON password_reset_tokens TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS revoked_at ON password_reset_tokens TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS ip_address ON password_reset_tokens TYPE option<string>;
DEFINE FIELD IF NOT EXISTS user_agent ON password_reset_tokens TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_at ON password_reset_tokens TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_password_reset_tokens_uuid
    ON password_reset_tokens FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_password_reset_tokens_user
    ON password_reset_tokens FIELDS user_id;
DEFINE INDEX IF NOT EXISTS idx_password_reset_tokens_hash
    ON password_reset_tokens FIELDS token_hash UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_password_reset_tokens_expires
    ON password_reset_tokens FIELDS expires_at;

DEFINE TABLE IF NOT EXISTS login_history SCHEMAFULL;
ALTER TABLE IF EXISTS login_history SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON login_history TYPE string;
DEFINE FIELD IF NOT EXISTS user_id ON login_history TYPE option<string>;
DEFINE FIELD IF NOT EXISTS event_type ON login_history TYPE string;
DEFINE FIELD IF NOT EXISTS auth_method ON login_history TYPE option<string>;
DEFINE FIELD IF NOT EXISTS success ON login_history TYPE bool DEFAULT false;
DEFINE FIELD IF NOT EXISTS failure_reason ON login_history TYPE option<string>;
DEFINE FIELD IF NOT EXISTS ip_address ON login_history TYPE option<string>;
DEFINE FIELD IF NOT EXISTS user_agent ON login_history TYPE option<string>;
DEFINE FIELD IF NOT EXISTS device_info ON login_history TYPE option<object> FLEXIBLE;
DEFINE FIELD IF NOT EXISTS email_attempted ON login_history TYPE option<string>;
DEFINE FIELD IF NOT EXISTS session_id ON login_history TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_at ON login_history TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_login_history_uuid ON login_history FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_login_history_user ON login_history FIELDS user_id;
DEFINE INDEX IF NOT EXISTS idx_login_history_event ON login_history FIELDS event_type;
DEFINE INDEX IF NOT EXISTS idx_login_history_created ON login_history FIELDS created_at;

DEFINE TABLE IF NOT EXISTS organization_invitations SCHEMAFULL;
ALTER TABLE IF EXISTS organization_invitations SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON organization_invitations TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON organization_invitations TYPE string;
DEFINE FIELD IF NOT EXISTS invited_email ON organization_invitations TYPE string;
DEFINE FIELD IF NOT EXISTS invited_role ON organization_invitations TYPE string DEFAULT 'member';
DEFINE FIELD IF NOT EXISTS token ON organization_invitations TYPE option<string>;
DEFINE FIELD OVERWRITE token ON organization_invitations TYPE option<string>;
DEFINE FIELD IF NOT EXISTS token_hash ON organization_invitations TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_by_user_id ON organization_invitations TYPE string;
DEFINE FIELD IF NOT EXISTS expires_at ON organization_invitations TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS accepted_at ON organization_invitations TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS accepted_by_user_id ON organization_invitations TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_at ON organization_invitations TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at ON organization_invitations TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_organization_invitations_uuid
    ON organization_invitations FIELDS uuid UNIQUE;
REMOVE INDEX IF EXISTS idx_organization_invitations_token ON TABLE organization_invitations;
DEFINE INDEX IF NOT EXISTS idx_organization_invitations_token_hash
    ON organization_invitations FIELDS token_hash;
DEFINE INDEX IF NOT EXISTS idx_organization_invitations_org
    ON organization_invitations FIELDS organization_id;
DEFINE INDEX IF NOT EXISTS idx_organization_invitations_email
    ON organization_invitations FIELDS invited_email;

DEFINE TABLE IF NOT EXISTS api_keys SCHEMAFULL;
ALTER TABLE IF EXISTS api_keys SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON api_keys TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON api_keys TYPE string;
DEFINE FIELD IF NOT EXISTS user_id ON api_keys TYPE string;
DEFINE FIELD IF NOT EXISTS name ON api_keys TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS key_prefix ON api_keys TYPE string;
DEFINE FIELD IF NOT EXISTS key_salt ON api_keys TYPE string;
DEFINE FIELD IF NOT EXISTS key_hash ON api_keys TYPE string;
DEFINE FIELD IF NOT EXISTS scopes ON api_keys TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS expires_at ON api_keys TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS revoked_at ON api_keys TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS last_used_at ON api_keys TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS created_at ON api_keys TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at ON api_keys TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_api_keys_uuid ON api_keys FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_api_keys_org ON api_keys FIELDS organization_id;
DEFINE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys FIELDS user_id;
DEFINE INDEX IF NOT EXISTS idx_api_keys_prefix ON api_keys FIELDS key_prefix;

DEFINE TABLE IF NOT EXISTS api_key_project_scopes SCHEMAFULL;
ALTER TABLE IF EXISTS api_key_project_scopes SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON api_key_project_scopes TYPE string;
DEFINE FIELD IF NOT EXISTS api_key_id ON api_key_project_scopes TYPE string;
DEFINE FIELD IF NOT EXISTS project_id ON api_key_project_scopes TYPE string;
DEFINE FIELD IF NOT EXISTS allowed_operations
    ON api_key_project_scopes TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS created_at
    ON api_key_project_scopes TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at
    ON api_key_project_scopes TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_api_key_project_scopes_uuid
    ON api_key_project_scopes FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_api_key_project_scopes_key
    ON api_key_project_scopes FIELDS api_key_id;
DEFINE INDEX IF NOT EXISTS idx_api_key_project_scopes_project
    ON api_key_project_scopes FIELDS project_id;
DEFINE INDEX IF NOT EXISTS idx_api_key_project_scopes_key_project
    ON api_key_project_scopes FIELDS api_key_id, project_id UNIQUE;

DEFINE TABLE IF NOT EXISTS api_key_memory_space_scopes SCHEMAFULL;
ALTER TABLE IF EXISTS api_key_memory_space_scopes SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON api_key_memory_space_scopes TYPE string;
DEFINE FIELD IF NOT EXISTS api_key_id ON api_key_memory_space_scopes TYPE string;
DEFINE FIELD IF NOT EXISTS memory_space_id ON api_key_memory_space_scopes TYPE string;
DEFINE FIELD IF NOT EXISTS allowed_operations
    ON api_key_memory_space_scopes TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS created_at
    ON api_key_memory_space_scopes TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at
    ON api_key_memory_space_scopes TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_api_key_memory_space_scopes_uuid
    ON api_key_memory_space_scopes FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_api_key_memory_space_scopes_key
    ON api_key_memory_space_scopes FIELDS api_key_id;
DEFINE INDEX IF NOT EXISTS idx_api_key_memory_space_scopes_space
    ON api_key_memory_space_scopes FIELDS memory_space_id;
DEFINE INDEX IF NOT EXISTS idx_api_key_memory_space_scopes_key_space
    ON api_key_memory_space_scopes FIELDS api_key_id, memory_space_id UNIQUE;

DEFINE TABLE IF NOT EXISTS oauth_connections SCHEMAFULL;
ALTER TABLE IF EXISTS oauth_connections SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON oauth_connections TYPE string;
DEFINE FIELD IF NOT EXISTS user_id ON oauth_connections TYPE string;
DEFINE FIELD IF NOT EXISTS provider ON oauth_connections TYPE string;
DEFINE FIELD IF NOT EXISTS provider_user_id ON oauth_connections TYPE string;
DEFINE FIELD IF NOT EXISTS provider_username ON oauth_connections TYPE option<string>;
DEFINE FIELD IF NOT EXISTS provider_email ON oauth_connections TYPE option<string>;
DEFINE FIELD IF NOT EXISTS access_token_encrypted ON oauth_connections TYPE option<string>;
DEFINE FIELD IF NOT EXISTS refresh_token_encrypted ON oauth_connections TYPE option<string>;
DEFINE FIELD IF NOT EXISTS token_expires_at ON oauth_connections TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS scopes ON oauth_connections TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS connected_at ON oauth_connections TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS disconnected_at ON oauth_connections TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS last_used_at ON oauth_connections TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS created_at ON oauth_connections TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at ON oauth_connections TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_oauth_connections_uuid ON oauth_connections FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_oauth_connections_user ON oauth_connections FIELDS user_id;
DEFINE INDEX IF NOT EXISTS idx_oauth_connections_provider_user
    ON oauth_connections FIELDS provider, provider_user_id UNIQUE;

DEFINE TABLE IF NOT EXISTS oauth_client_registrations SCHEMAFULL;
ALTER TABLE IF EXISTS oauth_client_registrations SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON oauth_client_registrations TYPE string;
DEFINE FIELD IF NOT EXISTS client_id ON oauth_client_registrations TYPE string;
DEFINE FIELD IF NOT EXISTS client_info ON oauth_client_registrations TYPE object FLEXIBLE;
DEFINE FIELD IF NOT EXISTS created_at
    ON oauth_client_registrations TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at
    ON oauth_client_registrations TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_oauth_client_registrations_uuid
    ON oauth_client_registrations FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_oauth_client_registrations_client
    ON oauth_client_registrations FIELDS client_id UNIQUE;

DEFINE TABLE IF NOT EXISTS device_authorization_requests SCHEMAFULL;
ALTER TABLE IF EXISTS device_authorization_requests SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON device_authorization_requests TYPE string;
DEFINE FIELD IF NOT EXISTS device_code_hash ON device_authorization_requests TYPE string;
DEFINE FIELD IF NOT EXISTS user_code ON device_authorization_requests TYPE string;
DEFINE FIELD IF NOT EXISTS client_name ON device_authorization_requests TYPE option<string>;
DEFINE FIELD IF NOT EXISTS scope ON device_authorization_requests TYPE string DEFAULT 'mcp';
DEFINE FIELD IF NOT EXISTS status ON device_authorization_requests TYPE string DEFAULT 'pending';
DEFINE FIELD IF NOT EXISTS poll_interval_seconds
    ON device_authorization_requests TYPE int DEFAULT 5;
DEFINE FIELD IF NOT EXISTS last_polled_at
    ON device_authorization_requests TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS expires_at ON device_authorization_requests TYPE datetime;
DEFINE FIELD IF NOT EXISTS approved_at ON device_authorization_requests TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS denied_at ON device_authorization_requests TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS consumed_at ON device_authorization_requests TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS user_id ON device_authorization_requests TYPE option<string>;
DEFINE FIELD IF NOT EXISTS organization_id ON device_authorization_requests TYPE option<string>;
DEFINE FIELD IF NOT EXISTS created_at
    ON device_authorization_requests TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at
    ON device_authorization_requests TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_device_authorization_requests_uuid
    ON device_authorization_requests FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_device_authorization_requests_device_code
    ON device_authorization_requests FIELDS device_code_hash UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_device_authorization_requests_user_code
    ON device_authorization_requests FIELDS user_code UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_device_authorization_requests_status
    ON device_authorization_requests FIELDS status;
DEFINE INDEX IF NOT EXISTS idx_device_authorization_requests_user
    ON device_authorization_requests FIELDS user_id;
DEFINE INDEX IF NOT EXISTS idx_device_authorization_requests_org
    ON device_authorization_requests FIELDS organization_id;

DEFINE TABLE IF NOT EXISTS audit_logs SCHEMAFULL;
ALTER TABLE IF EXISTS audit_logs SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON audit_logs TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON audit_logs TYPE option<string>;
DEFINE FIELD IF NOT EXISTS user_id ON audit_logs TYPE option<string>;
DEFINE FIELD IF NOT EXISTS action ON audit_logs TYPE string;
DEFINE FIELD IF NOT EXISTS ip_address ON audit_logs TYPE option<string>;
DEFINE FIELD IF NOT EXISTS user_agent ON audit_logs TYPE option<string>;
DEFINE FIELD IF NOT EXISTS details ON audit_logs TYPE object FLEXIBLE DEFAULT {};
DEFINE FIELD IF NOT EXISTS created_at ON audit_logs TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at ON audit_logs TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_audit_logs_uuid ON audit_logs FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_audit_logs_org ON audit_logs FIELDS organization_id;
DEFINE INDEX IF NOT EXISTS idx_audit_logs_user ON audit_logs FIELDS user_id;
DEFINE INDEX IF NOT EXISTS idx_audit_logs_action ON audit_logs FIELDS action;

DEFINE TABLE IF NOT EXISTS teams SCHEMAFULL;
ALTER TABLE IF EXISTS teams SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON teams TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON teams TYPE string;
DEFINE FIELD IF NOT EXISTS name ON teams TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS slug ON teams TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS description ON teams TYPE option<string>;
DEFINE FIELD IF NOT EXISTS avatar_url ON teams TYPE option<string>;
DEFINE FIELD IF NOT EXISTS settings ON teams TYPE object FLEXIBLE DEFAULT {};
DEFINE FIELD IF NOT EXISTS is_default ON teams TYPE bool DEFAULT false;
DEFINE FIELD IF NOT EXISTS graph_entity_id ON teams TYPE option<string>;
DEFINE FIELD IF NOT EXISTS last_synced_at ON teams TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS created_at ON teams TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at ON teams TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_teams_uuid ON teams FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_teams_org ON teams FIELDS organization_id;
DEFINE INDEX IF NOT EXISTS idx_teams_org_slug ON teams FIELDS organization_id, slug UNIQUE;

DEFINE TABLE IF NOT EXISTS team_members SCHEMAFULL;
ALTER TABLE IF EXISTS team_members SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON team_members TYPE string;
DEFINE FIELD IF NOT EXISTS team_id ON team_members TYPE string;
DEFINE FIELD IF NOT EXISTS user_id ON team_members TYPE string;
DEFINE FIELD IF NOT EXISTS role ON team_members TYPE string DEFAULT 'member';
DEFINE FIELD IF NOT EXISTS joined_at ON team_members TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS created_at ON team_members TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at ON team_members TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_team_members_uuid ON team_members FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_team_members_team ON team_members FIELDS team_id;
DEFINE INDEX IF NOT EXISTS idx_team_members_user ON team_members FIELDS user_id;
DEFINE INDEX IF NOT EXISTS idx_team_members_team_user
    ON team_members FIELDS team_id, user_id UNIQUE;

DEFINE TABLE IF NOT EXISTS projects SCHEMAFULL;
ALTER TABLE IF EXISTS projects SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON projects TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON projects TYPE string;
DEFINE FIELD IF NOT EXISTS name ON projects TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS slug ON projects TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS description ON projects TYPE option<string>;
DEFINE FIELD IF NOT EXISTS graph_project_id ON projects TYPE string;
DEFINE FIELD IF NOT EXISTS visibility ON projects TYPE string DEFAULT 'org';
DEFINE FIELD IF NOT EXISTS default_role ON projects TYPE string DEFAULT 'project_viewer';
DEFINE FIELD IF NOT EXISTS owner_user_id ON projects TYPE option<string>;
DEFINE FIELD IF NOT EXISTS settings ON projects TYPE object FLEXIBLE DEFAULT {};
DEFINE FIELD IF NOT EXISTS is_shared ON projects TYPE bool DEFAULT false;
DEFINE FIELD IF NOT EXISTS created_at ON projects TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at ON projects TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_projects_uuid ON projects FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_projects_org ON projects FIELDS organization_id;
DEFINE INDEX IF NOT EXISTS idx_projects_owner ON projects FIELDS owner_user_id;
REMOVE INDEX IF EXISTS idx_projects_org_slug ON TABLE projects;
DEFINE INDEX IF NOT EXISTS idx_projects_org_slug_lookup
    ON projects FIELDS organization_id, slug;
DEFINE INDEX IF NOT EXISTS idx_projects_org_graph_id
    ON projects FIELDS organization_id, graph_project_id UNIQUE;

DEFINE TABLE IF NOT EXISTS project_members SCHEMAFULL;
ALTER TABLE IF EXISTS project_members SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON project_members TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON project_members TYPE string;
DEFINE FIELD IF NOT EXISTS project_id ON project_members TYPE string;
DEFINE FIELD IF NOT EXISTS user_id ON project_members TYPE string;
DEFINE FIELD IF NOT EXISTS role ON project_members TYPE string DEFAULT 'project_contributor';
DEFINE FIELD IF NOT EXISTS joined_at ON project_members TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS created_at ON project_members TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at ON project_members TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_project_members_uuid
    ON project_members FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_project_members_project_user
    ON project_members FIELDS project_id, user_id UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_project_members_org
    ON project_members FIELDS organization_id;
DEFINE INDEX IF NOT EXISTS idx_project_members_project
    ON project_members FIELDS project_id;
DEFINE INDEX IF NOT EXISTS idx_project_members_user
    ON project_members FIELDS user_id;

DEFINE TABLE IF NOT EXISTS team_projects SCHEMAFULL;
ALTER TABLE IF EXISTS team_projects SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON team_projects TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON team_projects TYPE string;
DEFINE FIELD IF NOT EXISTS team_id ON team_projects TYPE string;
DEFINE FIELD IF NOT EXISTS project_id ON team_projects TYPE string;
DEFINE FIELD IF NOT EXISTS role ON team_projects TYPE string DEFAULT 'project_contributor';
DEFINE FIELD IF NOT EXISTS created_at ON team_projects TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at ON team_projects TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_team_projects_uuid
    ON team_projects FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_team_projects_team_project
    ON team_projects FIELDS team_id, project_id UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_team_projects_org
    ON team_projects FIELDS organization_id;
DEFINE INDEX IF NOT EXISTS idx_team_projects_team
    ON team_projects FIELDS team_id;
DEFINE INDEX IF NOT EXISTS idx_team_projects_project
    ON team_projects FIELDS project_id;

DEFINE TABLE IF NOT EXISTS memory_spaces SCHEMAFULL;
ALTER TABLE IF EXISTS memory_spaces SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON memory_spaces TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON memory_spaces TYPE string;
DEFINE FIELD IF NOT EXISTS memory_scope ON memory_spaces TYPE string;
DEFINE FIELD IF NOT EXISTS scope_key ON memory_spaces TYPE option<string>;
DEFINE FIELD IF NOT EXISTS name ON memory_spaces TYPE string DEFAULT '';
DEFINE FIELD IF NOT EXISTS description ON memory_spaces TYPE option<string>;
DEFINE FIELD IF NOT EXISTS state ON memory_spaces TYPE string DEFAULT 'active';
DEFINE FIELD IF NOT EXISTS disabled_reason ON memory_spaces TYPE option<string>;
DEFINE FIELD IF NOT EXISTS metadata ON memory_spaces TYPE object FLEXIBLE DEFAULT {};
DEFINE FIELD IF NOT EXISTS created_by_user_id ON memory_spaces TYPE string;
DEFINE FIELD IF NOT EXISTS created_at ON memory_spaces TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at ON memory_spaces TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_memory_spaces_uuid
    ON memory_spaces FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_memory_spaces_org
    ON memory_spaces FIELDS organization_id;
DEFINE INDEX IF NOT EXISTS idx_memory_spaces_scope
    ON memory_spaces FIELDS organization_id, memory_scope, scope_key UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_memory_spaces_creator
    ON memory_spaces FIELDS created_by_user_id;

DEFINE TABLE IF NOT EXISTS memory_space_members SCHEMAFULL;
ALTER TABLE IF EXISTS memory_space_members SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS uuid ON memory_space_members TYPE string;
DEFINE FIELD IF NOT EXISTS organization_id ON memory_space_members TYPE string;
DEFINE FIELD IF NOT EXISTS space_id ON memory_space_members TYPE string;
DEFINE FIELD IF NOT EXISTS principal_type ON memory_space_members TYPE string;
DEFINE FIELD IF NOT EXISTS principal_id ON memory_space_members TYPE string;
DEFINE FIELD IF NOT EXISTS role ON memory_space_members TYPE string DEFAULT 'reader';
DEFINE FIELD IF NOT EXISTS permissions ON memory_space_members TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS expires_at ON memory_space_members TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS created_by_user_id ON memory_space_members TYPE string;
DEFINE FIELD IF NOT EXISTS created_at ON memory_space_members TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at ON memory_space_members TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_memory_space_members_uuid
    ON memory_space_members FIELDS uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_memory_space_members_org
    ON memory_space_members FIELDS organization_id;
DEFINE INDEX IF NOT EXISTS idx_memory_space_members_space_principal
    ON memory_space_members FIELDS space_id, principal_type, principal_id UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_memory_space_members_principal
    ON memory_space_members FIELDS principal_type, principal_id;
"""


async def bootstrap_auth_schema(client: SurrealAuthClient, *, reset: bool = False) -> None:
    if reset:
        for table in AUTH_TABLES:
            await client.execute_query(f"REMOVE TABLE IF EXISTS {table};")

    for statement in split_statements(AUTH_SCHEMA_DEFINITIONS):
        await execute_schema_statement(client.execute_query, statement, scope="auth")


__all__ = [
    "AUTH_SCHEMA_DEFINITIONS",
    "AUTH_TABLES",
    "CORE_AUTH_TABLES",
    "EXTENDED_AUTH_TABLES",
    "bootstrap_auth_schema",
]
