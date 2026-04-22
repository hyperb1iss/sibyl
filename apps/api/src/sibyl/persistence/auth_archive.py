"""Auth archive export/import helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from uuid import UUID

from sqlalchemy import text

from sibyl.db.connection import get_session
from sibyl.persistence.surreal.auth import build_surreal_auth_client
from sibyl_core.backends.surreal import bootstrap_auth_schema

AUTH_ARCHIVE_VERSION = "1.0"
_AUTH_ARCHIVE_SQL = {
    "users": {
        "select": "SELECT * FROM users ORDER BY id ASC;",
        "delete_all": "DELETE FROM users;",
        "delete_by_uuid": "DELETE FROM users WHERE uuid = $uuid;",
        "create": "CREATE users CONTENT $record;",
    },
    "organizations": {
        "select": "SELECT * FROM organizations ORDER BY id ASC;",
        "delete_all": "DELETE FROM organizations;",
        "delete_by_uuid": "DELETE FROM organizations WHERE uuid = $uuid;",
        "create": "CREATE organizations CONTENT $record;",
    },
    "organization_members": {
        "select": "SELECT * FROM organization_members ORDER BY id ASC;",
        "delete_all": "DELETE FROM organization_members;",
        "delete_by_uuid": "DELETE FROM organization_members WHERE uuid = $uuid;",
        "create": "CREATE organization_members CONTENT $record;",
    },
    "user_sessions": {
        "select": "SELECT * FROM user_sessions ORDER BY id ASC;",
        "delete_all": "DELETE FROM user_sessions;",
        "delete_by_uuid": "DELETE FROM user_sessions WHERE uuid = $uuid;",
        "create": "CREATE user_sessions CONTENT $record;",
    },
    "password_reset_tokens": {
        "select": "SELECT * FROM password_reset_tokens ORDER BY id ASC;",
        "delete_all": "DELETE FROM password_reset_tokens;",
        "delete_by_uuid": "DELETE FROM password_reset_tokens WHERE uuid = $uuid;",
        "create": "CREATE password_reset_tokens CONTENT $record;",
    },
    "login_history": {
        "select": "SELECT * FROM login_history ORDER BY id ASC;",
        "delete_all": "DELETE FROM login_history;",
        "delete_by_uuid": "DELETE FROM login_history WHERE uuid = $uuid;",
        "create": "CREATE login_history CONTENT $record;",
    },
    "organization_invitations": {
        "select": "SELECT * FROM organization_invitations ORDER BY id ASC;",
        "delete_all": "DELETE FROM organization_invitations;",
        "delete_by_uuid": "DELETE FROM organization_invitations WHERE uuid = $uuid;",
        "create": "CREATE organization_invitations CONTENT $record;",
    },
    "api_keys": {
        "select": "SELECT * FROM api_keys ORDER BY id ASC;",
        "delete_all": "DELETE FROM api_keys;",
        "delete_by_uuid": "DELETE FROM api_keys WHERE uuid = $uuid;",
        "create": "CREATE api_keys CONTENT $record;",
    },
    "api_key_project_scopes": {
        "select": "SELECT * FROM api_key_project_scopes ORDER BY id ASC;",
        "delete_all": "DELETE FROM api_key_project_scopes;",
        "delete_by_uuid": "DELETE FROM api_key_project_scopes WHERE uuid = $uuid;",
        "create": "CREATE api_key_project_scopes CONTENT $record;",
    },
    "oauth_connections": {
        "select": "SELECT * FROM oauth_connections ORDER BY id ASC;",
        "delete_all": "DELETE FROM oauth_connections;",
        "delete_by_uuid": "DELETE FROM oauth_connections WHERE uuid = $uuid;",
        "create": "CREATE oauth_connections CONTENT $record;",
    },
    "device_authorization_requests": {
        "select": "SELECT * FROM device_authorization_requests ORDER BY id ASC;",
        "delete_all": "DELETE FROM device_authorization_requests;",
        "delete_by_uuid": "DELETE FROM device_authorization_requests WHERE uuid = $uuid;",
        "create": "CREATE device_authorization_requests CONTENT $record;",
    },
    "audit_logs": {
        "select": "SELECT * FROM audit_logs ORDER BY id ASC;",
        "delete_all": "DELETE FROM audit_logs;",
        "delete_by_uuid": "DELETE FROM audit_logs WHERE uuid = $uuid;",
        "create": "CREATE audit_logs CONTENT $record;",
    },
    "teams": {
        "select": "SELECT * FROM teams ORDER BY id ASC;",
        "delete_all": "DELETE FROM teams;",
        "delete_by_uuid": "DELETE FROM teams WHERE uuid = $uuid;",
        "create": "CREATE teams CONTENT $record;",
    },
    "team_members": {
        "select": "SELECT * FROM team_members ORDER BY id ASC;",
        "delete_all": "DELETE FROM team_members;",
        "delete_by_uuid": "DELETE FROM team_members WHERE uuid = $uuid;",
        "create": "CREATE team_members CONTENT $record;",
    },
    "projects": {
        "select": "SELECT * FROM projects ORDER BY id ASC;",
        "delete_all": "DELETE FROM projects;",
        "delete_by_uuid": "DELETE FROM projects WHERE uuid = $uuid;",
        "create": "CREATE projects CONTENT $record;",
    },
    "project_members": {
        "select": "SELECT * FROM project_members ORDER BY id ASC;",
        "delete_all": "DELETE FROM project_members;",
        "delete_by_uuid": "DELETE FROM project_members WHERE uuid = $uuid;",
        "create": "CREATE project_members CONTENT $record;",
    },
    "team_projects": {
        "select": "SELECT * FROM team_projects ORDER BY id ASC;",
        "delete_all": "DELETE FROM team_projects;",
        "delete_by_uuid": "DELETE FROM team_projects WHERE uuid = $uuid;",
        "create": "CREATE team_projects CONTENT $record;",
    },
}
AUTH_ARCHIVE_TABLES = tuple(_AUTH_ARCHIVE_SQL)
_SELECT_TABLE_ROWS = {
    table: queries["select"] for table, queries in _AUTH_ARCHIVE_SQL.items()
}
_DELETE_TABLE_ROWS = {
    table: queries["delete_all"] for table, queries in _AUTH_ARCHIVE_SQL.items()
}
_DELETE_BY_UUID = {
    table: queries["delete_by_uuid"] for table, queries in _AUTH_ARCHIVE_SQL.items()
}
_CREATE_RECORD = {
    table: queries["create"] for table, queries in _AUTH_ARCHIVE_SQL.items()
}


@dataclass(frozen=True)
class AuthArchiveRestoreResult:
    """Summary of one auth archive restore."""

    success: bool
    tables_restored: int
    rows_restored: int
    errors: list[str] = field(default_factory=list)


def _serialize_value(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return _serialize_value(value.value)
    if isinstance(value, dict):
        return {str(key): _serialize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize_value(item) for item in value]
    return value


def _deserialize_value(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _deserialize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_deserialize_value(item) for item in value]
    if isinstance(value, str):
        if "T" not in value and not value.endswith("Z"):
            return value
        normalized = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return value
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC).replace(tzinfo=None)
        return parsed
    return value


def _query_error(result: object) -> str | None:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        payload = {str(key): value for key, value in result.items()}
        status = payload.get("status")
        if isinstance(status, str) and status.upper() == "ERR":
            detail = payload.get("detail") or payload.get("result") or payload
            return str(detail)
        return None
    if not isinstance(result, list):
        return None
    for item in result:
        error = _query_error(item)
        if error is not None:
            return error
    return None


async def export_auth_archive_payload() -> dict[str, object]:
    """Export auth/RBAC tables from PostgreSQL into a JSON-safe payload."""

    tables: dict[str, list[dict[str, object]]] = {}
    row_counts: dict[str, int] = {}

    async with get_session() as session:
        for table in AUTH_ARCHIVE_TABLES:
            result = await session.execute(text(_SELECT_TABLE_ROWS[table]))
            rows = [
                {str(key): _serialize_value(value) for key, value in dict(row).items()}
                for row in result.mappings().all()
            ]
            tables[table] = rows
            row_counts[table] = len(rows)

    return {
        "version": AUTH_ARCHIVE_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "tables": tables,
        "row_counts": row_counts,
        "total_rows": sum(row_counts.values()),
    }


async def restore_auth_archive_payload(
    payload: dict[str, object],
    *,
    clean: bool = False,
) -> AuthArchiveRestoreResult:
    """Restore an auth/RBAC payload into the Surreal auth namespace."""

    raw_tables = payload.get("tables")
    if not isinstance(raw_tables, dict):
        raise TypeError("auth archive payload is missing a tables object")
    tables = {str(key): value for key, value in raw_tables.items()}

    client = build_surreal_auth_client()
    tables_restored = 0
    rows_restored = 0
    errors: list[str] = []

    try:
        await bootstrap_auth_schema(client, reset=clean)
        if clean:
            for table in AUTH_ARCHIVE_TABLES:
                await client.execute_query(_DELETE_TABLE_ROWS[table])

        for table in AUTH_ARCHIVE_TABLES:
            rows = tables.get(table)
            if rows is None:
                rows = []
            if not isinstance(rows, list):
                errors.append(f"{table} payload must be a list")
                continue
            if not rows:
                continue

            tables_restored += 1
            for row in rows:
                if not isinstance(row, dict):
                    errors.append(f"{table} row payload must be an object")
                    continue
                normalized_row = {str(key): value for key, value in row.items()}

                record = {
                    str(key): _deserialize_value(value)
                    for key, value in normalized_row.items()
                    if key != "id"
                }
                uuid = str(normalized_row.get("id") or normalized_row.get("uuid") or "").strip()
                if not uuid:
                    errors.append(f"{table} row is missing id")
                    continue
                record["uuid"] = uuid

                try:
                    delete_result = await client.execute_query(_DELETE_BY_UUID[table], uuid=uuid)
                    delete_error = _query_error(delete_result)
                    if delete_error is not None:
                        raise RuntimeError(delete_error)
                    create_result = await client.execute_query(_CREATE_RECORD[table], record=record)
                    create_error = _query_error(create_result)
                    if create_error is not None:
                        raise RuntimeError(create_error)
                    rows_restored += 1
                except Exception as exc:
                    errors.append(f"{table}:{uuid}: {exc}")
        return AuthArchiveRestoreResult(
            success=not errors,
            tables_restored=tables_restored,
            rows_restored=rows_restored,
            errors=errors[:50],
        )
    finally:
        await client.close()
