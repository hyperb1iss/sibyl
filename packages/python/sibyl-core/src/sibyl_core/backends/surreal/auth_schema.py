"""SurrealDB schema bootstrap for Sibyl auth storage."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sibyl_core.backends.surreal.auth_client import SurrealAuthClient


RUNTIME_AUTH_TABLES = ("users", "organizations", "organization_members")
ARCHIVE_ONLY_AUTH_TABLES = (
    "user_sessions",
    "password_reset_tokens",
    "login_history",
    "organization_invitations",
    "api_keys",
    "api_key_project_scopes",
    "oauth_connections",
    "device_authorization_requests",
    "audit_logs",
    "teams",
    "team_members",
    "projects",
    "project_members",
    "team_projects",
)
AUTH_TABLES = (*RUNTIME_AUTH_TABLES, *ARCHIVE_ONLY_AUTH_TABLES)

AUTH_SCHEMA_DEFINITIONS = """
DEFINE TABLE IF NOT EXISTS users SCHEMAFULL;
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
"""

ARCHIVE_ONLY_TABLE_DEFINITIONS = "\n".join(
    f"DEFINE TABLE IF NOT EXISTS {table} SCHEMALESS;" for table in ARCHIVE_ONLY_AUTH_TABLES
)


def _split_statements(sql: str) -> list[str]:
    statements: list[str] = []
    buffer: list[str] = []
    for raw_line in sql.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("--"):
            continue
        buffer.append(raw_line)
        if line.endswith(";"):
            statement = "\n".join(buffer).strip()
            if statement.rstrip(";").strip():
                statements.append(statement)
            buffer = []
    trailing = "\n".join(buffer).strip()
    if trailing:
        statements.append(trailing)
    return statements


async def bootstrap_auth_schema(client: SurrealAuthClient, *, reset: bool = False) -> None:
    if reset:
        for table in AUTH_TABLES:
            await client.execute_query(f"REMOVE TABLE IF EXISTS {table};")

    for statement in _split_statements(AUTH_SCHEMA_DEFINITIONS):
        await client.execute_query(statement)

    for statement in _split_statements(ARCHIVE_ONLY_TABLE_DEFINITIONS):
        await client.execute_query(statement)


__all__ = [
    "ARCHIVE_ONLY_AUTH_TABLES",
    "ARCHIVE_ONLY_TABLE_DEFINITIONS",
    "AUTH_SCHEMA_DEFINITIONS",
    "AUTH_TABLES",
    "RUNTIME_AUTH_TABLES",
    "bootstrap_auth_schema",
]
