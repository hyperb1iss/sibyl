"""Auth archive export/import helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import UUID

from sibyl.persistence.surreal.auth import build_surreal_auth_client
from sibyl_core.backends.surreal import bootstrap_auth_schema
from sibyl_core.backends.surreal.records import (
    normalize_records as _normalize_records,
    query_error as _query_error,
    raise_on_error as _raise_on_error,
)

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
    "identity_provider": {
        "select": "SELECT * FROM identity_provider ORDER BY id ASC;",
        "delete_all": "DELETE FROM identity_provider;",
        "delete_by_uuid": "DELETE FROM identity_provider WHERE uuid = $uuid;",
        "create": "CREATE identity_provider CONTENT $record;",
    },
    "user_identity": {
        "select": "SELECT * FROM user_identity ORDER BY id ASC;",
        "delete_all": "DELETE FROM user_identity;",
        "delete_by_uuid": "DELETE FROM user_identity WHERE uuid = $uuid;",
        "create": "CREATE user_identity CONTENT $record;",
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
    "api_key_memory_space_scopes": {
        "select": "SELECT * FROM api_key_memory_space_scopes ORDER BY id ASC;",
        "delete_all": "DELETE FROM api_key_memory_space_scopes;",
        "delete_by_uuid": "DELETE FROM api_key_memory_space_scopes WHERE uuid = $uuid;",
        "create": "CREATE api_key_memory_space_scopes CONTENT $record;",
    },
    "oauth_client_registrations": {
        "select": "SELECT * FROM oauth_client_registrations ORDER BY id ASC;",
        "delete_all": "DELETE FROM oauth_client_registrations;",
        "delete_by_uuid": "DELETE FROM oauth_client_registrations WHERE uuid = $uuid;",
        "create": "CREATE oauth_client_registrations CONTENT $record;",
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
    "memory_spaces": {
        "select": "SELECT * FROM memory_spaces ORDER BY id ASC;",
        "delete_all": "DELETE FROM memory_spaces;",
        "delete_by_uuid": "DELETE FROM memory_spaces WHERE uuid = $uuid;",
        "create": "CREATE memory_spaces CONTENT $record;",
    },
    "memory_space_members": {
        "select": "SELECT * FROM memory_space_members ORDER BY id ASC;",
        "delete_all": "DELETE FROM memory_space_members;",
        "delete_by_uuid": "DELETE FROM memory_space_members WHERE uuid = $uuid;",
        "create": "CREATE memory_space_members CONTENT $record;",
    },
    "llm_usage_buckets": {
        "select": "SELECT * FROM llm_usage_buckets ORDER BY id ASC;",
        "delete_all": "DELETE FROM llm_usage_buckets;",
        "delete_by_uuid": "DELETE FROM llm_usage_buckets WHERE uuid = $uuid;",
        "create": "CREATE llm_usage_buckets CONTENT $record;",
    },
}
AUTH_ARCHIVE_TABLES = tuple(_AUTH_ARCHIVE_SQL)
_SELECT_TABLE_ROWS = {table: queries["select"] for table, queries in _AUTH_ARCHIVE_SQL.items()}
_DELETE_TABLE_ROWS = {table: queries["delete_all"] for table, queries in _AUTH_ARCHIVE_SQL.items()}
_CREATE_RECORD = {table: queries["create"] for table, queries in _AUTH_ARCHIVE_SQL.items()}
_SELECT_BY_UUID = {
    table: f"SELECT * FROM {table} WHERE uuid = $uuid LIMIT 1;"  # noqa: S608 - fixed constants
    for table in _AUTH_ARCHIVE_SQL
}
_SELECT_SURREAL_TABLE_ROWS = {
    table: f"SELECT * FROM {table};"  # noqa: S608 - table names are fixed constants
    for table in _AUTH_ARCHIVE_SQL
}
_AUTH_ORG_SCOPED_TABLES = frozenset(
    {
        "organization_members",
        "user_sessions",
        "organization_invitations",
        "api_keys",
        "device_authorization_requests",
        "audit_logs",
        "teams",
        "projects",
        "project_members",
        "team_projects",
        "memory_spaces",
        "memory_space_members",
        "llm_usage_buckets",
    }
)

_ORG_USER_REDACTED_FIELDS = frozenset(
    {
        "password_salt",
        "password_hash",
        "password_iterations",
    }
)
_ORG_EXCLUDED_GLOBAL_USER_TABLES = frozenset(
    {
        "user_identity",
        "password_reset_tokens",
        "login_history",
    }
)
_ORG_SCOPED_REDACTED_FIELDS = {
    "api_keys": frozenset({"key_salt", "key_hash"}),
    "device_authorization_requests": frozenset({"device_code_hash", "user_code"}),
    "organization_invitations": frozenset({"token", "token_hash"}),
    "user_sessions": frozenset({"token_hash", "refresh_token_hash"}),
}

_AUTH_ORG_CLEAN_TABLES = (
    "organization_members",
    "user_sessions",
    "organization_invitations",
    "api_keys",
    "device_authorization_requests",
    "audit_logs",
    "teams",
    "projects",
    "project_members",
    "team_projects",
    "memory_spaces",
    "memory_space_members",
    "llm_usage_buckets",
)


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


def _sort_auth_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    def _key(row: dict[str, object]) -> str:
        value = row.get("uuid") or row.get("user_code") or row.get("key") or row.get("backup_id")
        if value is not None:
            return str(value)
        return json.dumps(row, sort_keys=True, default=str)

    return sorted(rows, key=_key)


def _row_ids(rows: list[dict[str, object]]) -> list[str]:
    ids: list[str] = []
    for row in rows:
        value = str(row.get("uuid") or "").strip()
        if value:
            ids.append(value)
    return ids


def _row_values(rows: list[dict[str, object]], field: str) -> list[str]:
    values = {str(row.get(field) or "").strip() for row in rows}
    return sorted(value for value in values if value)


async def _select_auth_rows(
    client: Any,
    query: str,
    **params: object,
) -> list[dict[str, object]]:
    result = await client.execute_query(query, **params)
    error = _query_error(result)
    if error is not None:
        raise RuntimeError(error)
    return _sort_auth_rows(
        [
            {str(key): _serialize_value(value) for key, value in row.items()}
            for row in _normalize_records(result)
        ]
    )


def _redact_auth_rows(
    rows: list[dict[str, object]],
    redacted_fields: frozenset[str],
) -> list[dict[str, object]]:
    return [
        {key: value for key, value in row.items() if key not in redacted_fields} for row in rows
    ]


async def _select_auth_rows_by_ids(
    client: Any,
    table: str,
    field: str,
    values: list[str],
) -> list[dict[str, object]]:
    if not values:
        return []
    return await _select_auth_rows(
        client,
        f"SELECT * FROM {table} WHERE {field} IN $values ORDER BY id ASC;",  # noqa: S608
        values=values,
    )


async def _export_all_auth_tables(client: Any) -> dict[str, list[dict[str, object]]]:
    tables: dict[str, list[dict[str, object]]] = {}
    for table in AUTH_ARCHIVE_TABLES:
        tables[table] = await _select_auth_rows(
            client,
            _SELECT_SURREAL_TABLE_ROWS[table],
        )
    return tables


async def _select_org_auth_ids(client: Any, table: str, organization_id: str) -> list[str]:
    rows = await _select_auth_rows(
        client,
        f"SELECT uuid FROM {table} WHERE organization_id = $organization_id;",  # noqa: S608
        organization_id=organization_id,
    )
    return _row_ids(rows)


async def _clean_auth_archive_rows(client: Any, organization_id: str | None) -> None:
    if organization_id is None:
        wipe_sql = "\n".join(_DELETE_TABLE_ROWS[table] for table in AUTH_ARCHIVE_TABLES)
        wipe_result = await client.execute_query_raw(
            f"BEGIN TRANSACTION;\n{wipe_sql}\nCOMMIT TRANSACTION;",
        )
        _raise_on_error(wipe_result, query="restore_auth_archive_payload:clean")
        return

    api_key_ids = await _select_org_auth_ids(client, "api_keys", organization_id)
    team_ids = await _select_org_auth_ids(client, "teams", organization_id)
    org_wipe_sql = "\n".join(
        (
            "DELETE FROM api_key_project_scopes WHERE api_key_id IN $api_key_ids;",
            "DELETE FROM api_key_memory_space_scopes WHERE api_key_id IN $api_key_ids;",
            "DELETE FROM team_members WHERE team_id IN $team_ids;",
            *(
                f"DELETE FROM {table} WHERE organization_id = $organization_id;"  # noqa: S608
                for table in _AUTH_ORG_CLEAN_TABLES
            ),
            "DELETE FROM organizations WHERE uuid = $organization_id;",
        )
    )
    wipe_result = await client.execute_query_raw(
        f"BEGIN TRANSACTION;\n{org_wipe_sql}\nCOMMIT TRANSACTION;",
        organization_id=organization_id,
        api_key_ids=api_key_ids,
        team_ids=team_ids,
    )
    _raise_on_error(wipe_result, query="restore_auth_archive_payload:clean_org")


async def _export_org_auth_tables(
    client: Any,
    organization_id: str,
) -> dict[str, list[dict[str, object]]]:
    tables: dict[str, list[dict[str, object]]] = {table: [] for table in AUTH_ARCHIVE_TABLES}

    tables["organizations"] = await _select_auth_rows(
        client,
        "SELECT * FROM organizations WHERE uuid = $organization_id ORDER BY id ASC;",
        organization_id=organization_id,
    )

    for table in _AUTH_ORG_SCOPED_TABLES:
        if table == "team_members":
            continue
        tables[table] = await _select_auth_rows(
            client,
            f"SELECT * FROM {table} WHERE organization_id = $organization_id ORDER BY id ASC;",  # noqa: S608
            organization_id=organization_id,
        )
        if redacted_fields := _ORG_SCOPED_REDACTED_FIELDS.get(table):
            tables[table] = _redact_auth_rows(tables[table], redacted_fields)

    team_ids = _row_ids(tables["teams"])
    tables["team_members"] = await _select_auth_rows_by_ids(
        client,
        "team_members",
        "team_id",
        team_ids,
    )

    user_ids = sorted(
        {
            *_row_values(tables["organization_members"], "user_id"),
            *_row_values(tables["user_sessions"], "user_id"),
            *_row_values(tables["organization_invitations"], "created_by_user_id"),
            *_row_values(tables["organization_invitations"], "accepted_by_user_id"),
            *_row_values(tables["api_keys"], "user_id"),
            *_row_values(tables["projects"], "owner_user_id"),
            *_row_values(tables["project_members"], "user_id"),
            *_row_values(tables["team_members"], "user_id"),
            *_row_values(tables["device_authorization_requests"], "user_id"),
            *_row_values(tables["audit_logs"], "user_id"),
            *_row_values(tables["memory_spaces"], "created_by_user_id"),
            *_row_values(tables["memory_space_members"], "created_by_user_id"),
        }
    )
    # Organization archives are downloadable by organization admins, while user
    # credentials and identity records are account-wide. Keep the member profile
    # rows needed to preserve org membership references, but never include global
    # credential or identity material in a scoped organization archive.
    tables["users"] = _redact_auth_rows(
        await _select_auth_rows_by_ids(client, "users", "uuid", user_ids),
        _ORG_USER_REDACTED_FIELDS,
    )
    for table in _ORG_EXCLUDED_GLOBAL_USER_TABLES:
        tables[table] = []

    api_key_ids = _row_ids(tables["api_keys"])
    tables["api_key_project_scopes"] = await _select_auth_rows_by_ids(
        client,
        "api_key_project_scopes",
        "api_key_id",
        api_key_ids,
    )
    tables["api_key_memory_space_scopes"] = await _select_auth_rows_by_ids(
        client,
        "api_key_memory_space_scopes",
        "api_key_id",
        api_key_ids,
    )

    return tables


async def _export_surreal_auth_archive_payload(
    organization_id: str | UUID | None = None,
) -> dict[str, object]:
    client = build_surreal_auth_client()

    try:
        if organization_id is None:
            tables = await _export_all_auth_tables(client)
        else:
            tables = await _export_org_auth_tables(client, str(organization_id))
    finally:
        await client.close()

    row_counts = {table: len(tables[table]) for table in AUTH_ARCHIVE_TABLES}
    return {
        "version": AUTH_ARCHIVE_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "organization_id": str(organization_id) if organization_id is not None else None,
        "tables": tables,
        "row_counts": row_counts,
        "total_rows": sum(row_counts.values()),
    }


async def export_auth_archive_payload(
    organization_id: str | UUID | None = None,
) -> dict[str, object]:
    """Export Surreal auth/RBAC tables into a JSON-safe payload."""

    return await _export_surreal_auth_archive_payload(organization_id)


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
    payload_org_id = payload.get("organization_id")
    organization_id = str(payload_org_id).strip() if payload_org_id is not None else None
    if organization_id == "":
        organization_id = None

    client = build_surreal_auth_client()
    tables_restored = 0
    rows_restored = 0
    errors: list[str] = []

    try:
        await bootstrap_auth_schema(client, reset=False)
        if clean:
            await _clean_auth_archive_rows(client, organization_id)

        for table in AUTH_ARCHIVE_TABLES:
            rows = tables.get(table)
            if rows is None:
                rows = []
            if not isinstance(rows, list):
                errors.append(f"{table} payload must be a list")
                continue
            if not rows:
                continue

            restored_table = False
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
                    existing_result = await client.execute_query(_SELECT_BY_UUID[table], uuid=uuid)
                    existing_error = _query_error(existing_result)
                    if existing_error is not None:
                        raise RuntimeError(existing_error)
                    if _normalize_records(existing_result):
                        continue
                    create_result = await client.execute_query(_CREATE_RECORD[table], record=record)
                    create_error = _query_error(create_result)
                    if create_error is not None:
                        raise RuntimeError(create_error)
                    rows_restored += 1
                    restored_table = True
                except Exception as exc:
                    errors.append(f"{table}:{uuid}: {exc}")
            if restored_table:
                tables_restored += 1
        return AuthArchiveRestoreResult(
            success=not errors,
            tables_restored=tables_restored,
            rows_restored=rows_restored,
            errors=errors[:50],
        )
    finally:
        await client.close()
