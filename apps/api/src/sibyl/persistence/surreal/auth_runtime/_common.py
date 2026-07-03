"""Shared primitives for the Surreal auth runtime submodules.

Holds the constants, dataclasses, record-namespace coercers, the repository
base, the session repository, and the cross-cutting orchestration helpers
(audit logging, session issuance, personal-org provisioning) that the domain
submodules build on. Domain submodules import from here; this module never
imports from its siblings, which keeps the package import graph acyclic.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Protocol, Self, cast
from uuid import UUID, uuid4

from fastapi import HTTPException
from starlette import status
from starlette.requests import Request

from sibyl import config as config_module
from sibyl.auth.api_key_common import (
    ApiKeyAuth,
    ApiKeyMemorySpaceAuth,
)
from sibyl.auth.jwt import (
    create_access_token,
    create_refresh_token,
    decode_token_unverified,
)
from sibyl.auth.session_cache import access_session_cache
from sibyl.persistence.surreal.auth import (
    SurrealAuthContextResolver,
    surreal_auth_client_scope,
)
from sibyl_core.auth import (
    AuthContext,
    AuthSession,
    AuthUser,
    OrganizationRole,
    ProjectRole,
)
from sibyl_core.backends.surreal import SurrealAuthClient
from sibyl_core.backends.surreal.connection import _is_transient_connection_error
from sibyl_core.backends.surreal.records import (
    coerce_datetime as _coerce_datetime,
    coerce_uuid as _coerce_uuid,
    normalize_record as _normalize_record,
    normalize_records as _normalize_records,
    query_error as _query_error,
    utcnow as _utcnow,
)

logger = logging.getLogger(__name__)

_ORG_ADMIN_ROLE_VALUES = {"owner", "admin"}
_PROJECT_ROLE_LEVELS: dict[ProjectRole, int] = {
    ProjectRole.VIEWER: 10,
    ProjectRole.CONTRIBUTOR: 20,
    ProjectRole.MAINTAINER: 30,
    ProjectRole.OWNER: 40,
}
_USER_UUID_FIELDS = {"id", "github_id", "created_by_user_id", "accepted_by_user_id"}
_USER_DATETIME_FIELDS = {
    "created_at",
    "updated_at",
    "email_verified_at",
    "last_login_at",
    "deleted_at",
    "purge_after",
}
_SAFE_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_REST_READ_SCOPES = frozenset({"api:read", "api:write"})
_REST_WRITE_SCOPE = "api:write"


def _is_rest_request(request: Request) -> bool:
    return request.url.path.startswith("/api/")


def _api_key_allows_rest(*, scopes: list[str], method: str) -> bool:
    normalized = {s.strip() for s in scopes if str(s).strip()}
    if method.upper() in _SAFE_HTTP_METHODS:
        return bool(normalized & _REST_READ_SCOPES)
    return _REST_WRITE_SCOPE in normalized


def _insufficient_api_scope(*, scopes: list[str], method: str) -> HTTPException:
    expected = "api:read or api:write" if method.upper() in _SAFE_HTTP_METHODS else "api:write"
    actual = ", ".join(scope for scope in scopes if scope.strip()) or "none"
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "error": "insufficient_api_scope",
            "message": "Request is missing required REST scope.",
            "remediation": "Use a REST scope that matches this request.",
            "details": {
                "expected": expected,
                "actual": actual,
            },
        },
    )


def _project_not_found_detail(project_id: object) -> str:
    return (
        f"Project not found: {project_id}. Run 'sibyl project relink' or use "
        "--all-projects for an unscoped write."
    )


_ORG_DATETIME_FIELDS = {"created_at", "updated_at"}
_SESSION_DATETIME_FIELDS = {
    "created_at",
    "updated_at",
    "expires_at",
    "refresh_token_expires_at",
    "revoked_at",
    "last_active_at",
}
_API_KEY_DATETIME_FIELDS = {
    "created_at",
    "updated_at",
    "expires_at",
    "revoked_at",
    "last_used_at",
}
_DEVICE_DATETIME_FIELDS = {
    "created_at",
    "updated_at",
    "expires_at",
    "approved_at",
    "denied_at",
    "consumed_at",
    "last_polled_at",
}
_PASSWORD_RESET_DATETIME_FIELDS = {"created_at", "expires_at", "used_at", "revoked_at"}
_LOGIN_HISTORY_DATETIME_FIELDS = {"created_at"}
_UPSERT_QUERY_BY_TABLE = {
    "api_keys": "UPSERT api_keys CONTENT $record WHERE uuid = $uuid;",
    "device_authorization_requests": (
        "UPSERT device_authorization_requests CONTENT $record WHERE uuid = $uuid;"
    ),
    "identity_provider": "UPSERT identity_provider CONTENT $record WHERE uuid = $uuid;",
    "oauth_client_registrations": (
        "UPSERT oauth_client_registrations CONTENT $record WHERE uuid = $uuid;"
    ),
    "password_reset_tokens": "UPSERT password_reset_tokens CONTENT $record WHERE uuid = $uuid;",
    "memory_spaces": "UPSERT memory_spaces CONTENT $record WHERE uuid = $uuid;",
    "memory_space_members": ("UPSERT memory_space_members CONTENT $record WHERE uuid = $uuid;"),
    "projects": "UPSERT projects CONTENT $record WHERE uuid = $uuid;",
    "teams": "UPSERT teams CONTENT $record WHERE uuid = $uuid;",
    "team_members": "UPSERT team_members CONTENT $record WHERE uuid = $uuid;",
    "team_projects": "UPSERT team_projects CONTENT $record WHERE uuid = $uuid;",
    "user_identity": "UPSERT user_identity CONTENT $record WHERE uuid = $uuid;",
    "user_sessions": "UPSERT user_sessions CONTENT $record WHERE uuid = $uuid;",
    "users": "UPSERT users CONTENT $record WHERE uuid = $uuid;",
}
_ENABLED_MEMORY_SPACE_SCOPES = {"private", "delegated", "project", "team"}
_MEMORY_SPACE_SCOPES = {
    "private",
    "delegated",
    "project",
    "team",
    "organization",
    "shared",
    "public",
}
type SurrealRecord = dict[str, object]


class QueryClient(Protocol):
    async def execute_query(self, query: str, **params: object) -> object: ...


class RawQueryFunc(Protocol):
    def __call__(self, query: str, **params: object) -> Awaitable[object]: ...


@dataclass(frozen=True, slots=True)
class IssuedAuthSession:
    user: SimpleNamespace
    organization: SimpleNamespace
    session_id: UUID
    access_token: str
    refresh_token: str
    refresh_expires: datetime


@dataclass(frozen=True, slots=True)
class IssuedOidcSession:
    user: SimpleNamespace
    organization: SimpleNamespace
    session_id: UUID
    access_token: str
    access_expires: datetime


@dataclass(frozen=True, slots=True)
class DeviceBrowserLogin:
    user: SimpleNamespace
    organization: SimpleNamespace
    access_token: str
    refresh_token: str
    refresh_expires: datetime


@dataclass(frozen=True, slots=True)
class RefreshRotation:
    session_id: UUID
    access_token: str
    refresh_token: str
    refresh_expires: datetime
    user_id: UUID
    organization_id: UUID | None


@dataclass(frozen=True, slots=True)
class UserDeletionRequestResult:
    user_id: UUID
    purge_after: datetime
    private_memories_scheduled: int
    api_keys_revoked: int
    sessions_revoked: int


def _normalize_raw_statement_records(
    result: object, *, statement_index: int
) -> list[SurrealRecord]:
    if isinstance(result, dict):
        payload = {str(key): value for key, value in result.items()}
        statements = payload.get("result")
        if (
            "status" not in payload
            and isinstance(statements, list)
            and statements
            and all(isinstance(statement, dict) for statement in statements)
        ):
            raw_statement = cast("Mapping[object, object]", statements[statement_index])
            statement = {str(key): value for key, value in raw_statement.items()}
            if "result" in statement:
                return _normalize_records(statement.get("result"))
            return _normalize_records(statement)
    return _normalize_records(result)


def _record_payload(value: object) -> SurrealRecord:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _coerce_optional_uuid(value: object | None) -> UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    return None


def _uuid_str(value: UUID | None) -> str | None:
    return str(value) if value is not None else None


def _role_value(role: object | None) -> str | None:
    if role is None:
        return None
    value = getattr(role, "value", None)
    if isinstance(value, str):
        return value
    if isinstance(role, str):
        return role
    return None


def _optional_str(value: object | None) -> str | None:
    return value if isinstance(value, str) else None


def _coerce_int(value: object | None, *, field_name: str) -> int:
    if isinstance(value, bool):
        msg = f"{field_name} must be an integer"
        raise TypeError(msg)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    msg = f"{field_name} is required"
    raise TypeError(msg)


def _optional_int(value: object | None) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _ns(
    record: SurrealRecord | None,
    *,
    uuid_fields: set[str],
    datetime_fields: set[str],
    id_field: str = "uuid",
) -> SimpleNamespace | None:
    if record is None:
        return None
    values: SurrealRecord = dict.fromkeys(uuid_fields | datetime_fields, None)
    for key, value in record.items():
        if key in uuid_fields:
            values[key] = _coerce_optional_uuid(value)
        elif key in datetime_fields:
            values[key] = _coerce_datetime(value)
        else:
            values[key] = value
    if id_field in values and values.get(id_field) is not None:
        values["id"] = values[id_field]
    return SimpleNamespace(**values)


def _require_namespace(value: SimpleNamespace | None, *, label: str) -> SimpleNamespace:
    if value is None:
        msg = f"{label} namespace could not be materialized"
        raise RuntimeError(msg)
    return value


def _auth_user_namespace(record: SurrealRecord | None) -> SimpleNamespace | None:
    user = _ns(
        record,
        uuid_fields={"uuid"},
        datetime_fields=_USER_DATETIME_FIELDS,
    )
    if user is None:
        return None

    user.email = getattr(user, "email", None)
    user.name = str(getattr(user, "name", None) or "")
    user.avatar_url = getattr(user, "avatar_url", None)
    user.github_id = getattr(user, "github_id", None)
    user.is_admin = bool(getattr(user, "is_admin", False))
    user.bio = getattr(user, "bio", None)
    user.timezone = str(getattr(user, "timezone", None) or "UTC")
    user.preferences = dict(getattr(user, "preferences", None) or {})
    user.email_verified_at = getattr(user, "email_verified_at", None)
    user.last_login_at = getattr(user, "last_login_at", None)
    user.created_at = getattr(user, "created_at", None) or _utcnow()
    user.updated_at = getattr(user, "updated_at", None)
    return user


def _auth_user_model(record: SurrealRecord | None) -> AuthUser | None:
    user = _auth_user_namespace(record)
    if user is None:
        return None
    return AuthUser(
        id=user.id,
        email=getattr(user, "email", None),
        name=str(getattr(user, "name", None) or ""),
        avatar_url=getattr(user, "avatar_url", None),
        github_id=getattr(user, "github_id", None),
        is_admin=bool(getattr(user, "is_admin", False)),
        bio=getattr(user, "bio", None),
        timezone=getattr(user, "timezone", None),
        preferences=dict(getattr(user, "preferences", None) or {}),
        email_verified_at=getattr(user, "email_verified_at", None),
        last_login_at=getattr(user, "last_login_at", None),
        created_at=getattr(user, "created_at", None) or _utcnow(),
        updated_at=getattr(user, "updated_at", None),
    )


def _auth_org_namespace(record: SurrealRecord | None) -> SimpleNamespace | None:
    return _ns(
        record,
        uuid_fields={"uuid"},
        datetime_fields=_ORG_DATETIME_FIELDS,
    )


def _session_namespace(record: SurrealRecord | None) -> SimpleNamespace | None:
    session = _ns(
        record,
        uuid_fields={"uuid", "user_id", "organization_id"},
        datetime_fields=_SESSION_DATETIME_FIELDS,
    )
    if session is None:
        return None

    # SurrealDB omits unset/NONE columns from stored records, and sessions
    # created before device metadata was captured have no such keys at all.
    # Seed the full shape so consumers can read these attributes unconditionally.
    session.token_hash = getattr(session, "token_hash", None)
    session.refresh_token_hash = getattr(session, "refresh_token_hash", None)
    session.device_name = getattr(session, "device_name", None)
    session.device_type = getattr(session, "device_type", None)
    session.browser = getattr(session, "browser", None)
    session.os = getattr(session, "os", None)
    session.ip_address = getattr(session, "ip_address", None)
    session.user_agent = getattr(session, "user_agent", None)
    session.location = getattr(session, "location", None)
    session.is_current = bool(getattr(session, "is_current", False))
    session.version = getattr(session, "version", 0) or 0
    return session


def _auth_session_namespace(session: AuthSession | None) -> SimpleNamespace | None:
    if session is None:
        return None
    return SimpleNamespace(
        id=session.id,
        uuid=session.id,
        user_id=session.user_id,
        organization_id=session.organization_id,
        expires_at=session.expires_at,
        refresh_token_expires_at=session.refresh_token_expires_at,
        revoked_at=session.revoked_at,
        last_active_at=session.last_active_at,
        is_current=session.is_current,
        device_name=session.device_name,
        device_type=session.device_type,
        browser=session.browser,
        os=session.os,
        ip_address=session.ip_address,
        user_agent=session.user_agent,
        location=session.location,
    )


def _api_key_namespace(record: SurrealRecord | None) -> SimpleNamespace | None:
    return _ns(
        record,
        uuid_fields={"uuid", "organization_id", "user_id"},
        datetime_fields=_API_KEY_DATETIME_FIELDS,
    )


def _unique_strings(values: list[str] | tuple[str, ...] | None) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values or []:
        normalized = str(value).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def _unique_uuids(values: list[UUID] | tuple[UUID, ...] | None) -> list[UUID]:
    seen: set[UUID] = set()
    out: list[UUID] = []
    for value in values or []:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _api_key_memory_space_scope(record: SurrealRecord) -> ApiKeyMemorySpaceAuth:
    return ApiKeyMemorySpaceAuth(
        memory_space_id=_coerce_uuid(record.get("uuid"), field_name="memory_spaces.uuid"),
        memory_scope=str(record.get("memory_scope") or "private"),
        scope_key=_optional_str(record.get("scope_key")),
    )


def _device_request_namespace(record: SurrealRecord | None) -> SimpleNamespace | None:
    request_row = _ns(
        record,
        uuid_fields={"uuid", "user_id", "organization_id"},
        datetime_fields=_DEVICE_DATETIME_FIELDS,
    )
    if request_row is None:
        return None

    # client_name is option<string>; scope/status carry schema defaults that
    # rows predating those fields never received. SurrealDB omits unset columns,
    # so seed the consumer surface to keep approval and token exchange safe.
    request_row.client_name = getattr(request_row, "client_name", None)
    request_row.scope = getattr(request_row, "scope", None)
    request_row.status = getattr(request_row, "status", None)
    return request_row


def _password_reset_namespace(record: SurrealRecord | None) -> SimpleNamespace | None:
    return _ns(
        record,
        uuid_fields={"uuid", "user_id"},
        datetime_fields=_PASSWORD_RESET_DATETIME_FIELDS,
    )


@asynccontextmanager
async def _auth_client_scope() -> AsyncIterator[SurrealAuthClient]:
    async with surreal_auth_client_scope() as client:
        yield client


class _SurrealRepository:
    def __init__(self, client: QueryClient) -> None:
        self._client = client

    async def select_one(self, query: str, **params: object) -> SurrealRecord | None:
        records = _normalize_records(await self._client.execute_query(query, **params))
        return records[0] if records else None

    async def select_many(self, query: str, **params: object) -> list[SurrealRecord]:
        return _normalize_records(await self._client.execute_query(query, **params))

    async def replace_record(
        self, table: str, *, uuid: UUID, record: SurrealRecord
    ) -> SurrealRecord:
        query = _UPSERT_QUERY_BY_TABLE.get(table)
        if query is None:
            msg = f"Unsupported replace table: {table}"
            raise ValueError(msg)
        created = _normalize_records(
            await self._client.execute_query(query, uuid=str(uuid), record=record)
        )
        if not created:
            msg = f"Failed to write {table} record {uuid}"
            raise RuntimeError(msg)
        return {**record, **created[0]}


async def _execute_raw_statement_records(
    client: QueryClient,
    query: str,
    *,
    statement_index: int = -1,
    **params: object,
) -> list[SurrealRecord]:
    raw_candidate = getattr(client, "execute_query_raw", None)
    if callable(raw_candidate):
        execute_query_raw = cast("RawQueryFunc", raw_candidate)
        result = await execute_query_raw(query, **params)
    else:
        result = await client.execute_query(query, **params)
    error = _query_error(result)
    if error is not None:
        raise RuntimeError(error)
    return _normalize_raw_statement_records(result, statement_index=statement_index)


class SurrealSessionRepository(_SurrealRepository):
    @classmethod
    def from_client(cls, client: QueryClient) -> Self:
        return cls(client)

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    async def create_session(
        self,
        *,
        user_id: UUID,
        token: str,
        expires_at: datetime,
        session_id: UUID | None = None,
        organization_id: UUID | None = None,
        refresh_token: str | None = None,
        refresh_token_expires_at: datetime | None = None,
        device_name: str | None = None,
        device_type: str | None = None,
        browser: str | None = None,
        os: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        location: str | None = None,
    ) -> AuthSession:
        now = _utcnow()
        record: SurrealRecord = {
            "uuid": str(session_id or uuid4()),
            "user_id": str(user_id),
            "organization_id": _uuid_str(organization_id),
            "token_hash": self.hash_token(token),
            "refresh_token_hash": self.hash_token(refresh_token) if refresh_token else None,
            "refresh_token_expires_at": _coerce_datetime(refresh_token_expires_at),
            "device_name": device_name,
            "device_type": device_type,
            "browser": browser,
            "os": os,
            "ip_address": ip_address,
            "user_agent": user_agent,
            "location": location,
            "is_current": False,
            "version": 0,
            "last_active_at": now,
            "expires_at": _coerce_datetime(expires_at) or expires_at,
            "revoked_at": None,
            "created_at": now,
            "updated_at": now,
        }
        created = _normalize_records(
            await self._client.execute_query("CREATE user_sessions CONTENT $record;", record=record)
        )
        if not created:
            msg = "Failed to create session"
            raise RuntimeError(msg)
        session = self._auth_session_from_record(created[0])
        access_session_cache.store_session(session)
        return session

    async def get_session_by_token(self, token: str) -> AuthSession | None:
        record = await self.select_one(
            "SELECT * FROM user_sessions WHERE token_hash = $token_hash LIMIT 1;",
            token_hash=self.hash_token(token),
        )
        if not self._is_session_active(record):
            return None
        if record is None:
            return None
        return self._auth_session_from_record(record)

    async def get_session_by_id(self, session_id: UUID) -> AuthSession | None:
        record = await self.select_one(
            "SELECT * FROM user_sessions WHERE uuid = $uuid LIMIT 1;",
            uuid=str(session_id),
        )
        if record is None:
            return None
        if not self._is_session_active(record):
            return None
        return self._auth_session_from_record(record)

    async def get_session_by_refresh_token(self, refresh_token: str) -> AuthSession | None:
        record = await self.select_one(
            "SELECT * FROM user_sessions WHERE refresh_token_hash = $refresh_token_hash LIMIT 1;",
            refresh_token_hash=self.hash_token(refresh_token),
        )
        if not self._has_refresh_session(record):
            return None
        if record is None:
            return None
        return self._auth_session_from_record(record)

    async def rotate_tokens(
        self,
        session: AuthSession,
        *,
        new_access_token: str,
        new_access_expires_at: datetime,
        new_refresh_token: str,
        new_refresh_expires_at: datetime,
    ) -> AuthSession:
        now = _utcnow()
        result = await self._client.execute_query(
            """
                UPDATE user_sessions
                SET token_hash = $token_hash,
                    expires_at = $expires_at,
                    refresh_token_hash = $refresh_token_hash,
                    refresh_token_expires_at = $refresh_token_expires_at,
                    last_active_at = $last_active_at,
                    version = $next_version,
                    updated_at = $updated_at
                WHERE uuid = $uuid
                    AND (
                        version = $expected_version
                        OR (version = NONE AND $expected_version = 0)
                    );
            """,
            uuid=str(session.id),
            expected_version=session.version,
            next_version=session.version + 1,
            token_hash=self.hash_token(new_access_token),
            expires_at=_coerce_datetime(new_access_expires_at) or new_access_expires_at,
            refresh_token_hash=self.hash_token(new_refresh_token),
            refresh_token_expires_at=_coerce_datetime(new_refresh_expires_at)
            or new_refresh_expires_at,
            last_active_at=now,
            updated_at=now,
        )
        error = _query_error(result)
        if error is not None:
            raise RuntimeError(error)
        updated_records = _normalize_records(result)
        if not updated_records:
            msg = f"Session not found: {session.id}"
            raise LookupError(msg)
        updated = self._auth_session_from_record(updated_records[0])
        access_session_cache.store_session(updated)
        return updated

    async def list_user_sessions(
        self, user_id: UUID, *, include_expired: bool = False
    ) -> list[AuthSession]:
        params: SurrealRecord = {"user_id": str(user_id)}
        query = "SELECT * FROM user_sessions WHERE user_id = $user_id AND revoked_at = NONE"
        if not include_expired:
            params["now"] = _utcnow()
            query += " AND expires_at > $now"
        query += " ORDER BY last_active_at DESC;"
        records = await self.select_many(query, **params)
        return [self._auth_session_from_record(record) for record in records]

    async def update_activity(self, token: str) -> bool:
        now = _utcnow()
        result = await self._client.execute_query(
            "UPDATE user_sessions SET last_active_at = $last_active_at, updated_at = $updated_at "
            "WHERE token_hash = $token_hash AND revoked_at = NONE AND expires_at > $now;",
            token_hash=self.hash_token(token),
            last_active_at=now,
            updated_at=now,
            now=now,
        )
        error = _query_error(result)
        if error is not None:
            raise RuntimeError(error)
        return bool(_normalize_records(result))

    async def mark_current(self, token: str) -> bool:
        record = await self.select_one(
            "SELECT * FROM user_sessions WHERE token_hash = $token_hash LIMIT 1;",
            token_hash=self.hash_token(token),
        )
        if not self._is_session_active(record):
            return False
        if record is None:
            return False
        user_id = str(record["user_id"])
        session_id = str(record["uuid"])
        now = _utcnow()
        await _execute_raw_statement_records(
            self._client,
            """
                UPDATE user_sessions
                SET is_current = false, updated_at = $updated_at
                WHERE user_id = $user_id AND is_current = true AND uuid != $uuid;
                UPDATE user_sessions
                SET is_current = true, updated_at = $updated_at
                WHERE uuid = $uuid AND user_id = $user_id;
            """,
            user_id=user_id,
            uuid=session_id,
            updated_at=now,
        )
        return True

    async def revoke_session(self, session_id: UUID, user_id: UUID) -> bool:
        now = _utcnow()
        result = await self._client.execute_query(
            "UPDATE user_sessions SET revoked_at = $revoked_at, updated_at = $updated_at "
            "WHERE uuid = $uuid AND user_id = $user_id AND revoked_at = NONE;",
            uuid=str(session_id),
            user_id=str(user_id),
            revoked_at=now,
            updated_at=now,
        )
        error = _query_error(result)
        if error is not None:
            raise RuntimeError(error)
        revoked = bool(_normalize_records(result))
        if revoked:
            access_session_cache.mark_revoked(session_id, user_id=user_id)
        return revoked

    async def revoke_loaded_session(self, session: AuthSession) -> bool:
        if session.revoked_at is not None:
            return False
        now = _utcnow()
        result = await self._client.execute_query(
            "UPDATE user_sessions SET revoked_at = $revoked_at, updated_at = $updated_at "
            "WHERE uuid = $uuid;",
            uuid=str(session.id),
            revoked_at=now,
            updated_at=now,
        )
        error = _query_error(result)
        if error is not None:
            raise RuntimeError(error)
        access_session_cache.mark_revoked(
            session.id,
            user_id=session.user_id,
            organization_id=session.organization_id,
            expires_at=session.refresh_token_expires_at or session.expires_at,
        )
        return True

    async def revoke_all_sessions(
        self, user_id: UUID, *, exclude_token_hash: str | None = None
    ) -> int:
        now = _utcnow()
        params: SurrealRecord = {
            "user_id": str(user_id),
            "revoked_at": now,
            "updated_at": now,
        }
        query = (
            "UPDATE user_sessions SET revoked_at = $revoked_at, updated_at = $updated_at "
            "WHERE user_id = $user_id AND revoked_at = NONE"
        )
        if exclude_token_hash:
            query += " AND token_hash != $exclude_token_hash"
            params["exclude_token_hash"] = exclude_token_hash
        result = await self._client.execute_query(query + ";", **params)
        error = _query_error(result)
        if error is not None:
            raise RuntimeError(error)
        records = _normalize_records(result)
        access_session_cache.invalidate_user(user_id)
        return len(records)

    async def cleanup_expired(self, *, older_than_days: int = 30) -> int:
        cutoff = _utcnow() - timedelta(days=older_than_days)
        result = await self._client.execute_query(
            "DELETE FROM user_sessions WHERE expires_at < $cutoff;",
            cutoff=cutoff,
        )
        error = _query_error(result)
        if error is not None:
            raise RuntimeError(error)
        return len(_normalize_records(result))

    def _is_session_active(
        self,
        record: SurrealRecord | None,
        *,
        include_expired: bool = False,
    ) -> bool:
        if record is None or record.get("revoked_at") is not None:
            return False
        if include_expired:
            return True
        expires_at = _coerce_datetime(record.get("expires_at"))
        return expires_at is not None and expires_at > _utcnow()

    def _has_refresh_session(self, record: SurrealRecord | None) -> bool:
        if not self._is_session_active(record, include_expired=True):
            return False
        if record is None:
            return False
        refresh_expires_at = _coerce_datetime(record.get("refresh_token_expires_at"))
        return refresh_expires_at is not None and refresh_expires_at > _utcnow()

    def _auth_session_from_record(self, record: SurrealRecord) -> AuthSession:
        return AuthSession(
            id=_coerce_uuid(record.get("uuid"), field_name="session.uuid"),
            user_id=_coerce_uuid(record.get("user_id"), field_name="session.user_id"),
            organization_id=_coerce_optional_uuid(record.get("organization_id")),
            expires_at=_coerce_datetime(record.get("expires_at")) or _utcnow(),
            refresh_token_expires_at=_coerce_datetime(record.get("refresh_token_expires_at")),
            revoked_at=_coerce_datetime(record.get("revoked_at")),
            last_active_at=_coerce_datetime(record.get("last_active_at")),
            is_current=bool(record.get("is_current", False)),
            version=_optional_int(record.get("version")) or 0,
            device_name=_optional_str(record.get("device_name")),
            device_type=_optional_str(record.get("device_type")),
            browser=_optional_str(record.get("browser")),
            os=_optional_str(record.get("os")),
            ip_address=_optional_str(record.get("ip_address")),
            user_agent=_optional_str(record.get("user_agent")),
            location=_optional_str(record.get("location")),
        )


def _scopes_list(value: object | None) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _api_key_claim_payload(auth: ApiKeyAuth) -> SurrealRecord:
    payload: SurrealRecord = {
        "sub": str(auth.user_id),
        "org": str(auth.organization_id),
        "typ": "api_key",
        "api_key_id": str(auth.api_key_id),
        "scopes": list(auth.scopes or []),
    }
    if auth.project_ids is not None:
        payload["api_key_project_ids"] = [str(project_id) for project_id in auth.project_ids]
    if auth.memory_space_ids is not None:
        payload["api_key_memory_space_ids"] = [
            str(memory_space_id) for memory_space_id in auth.memory_space_ids
        ]
    if auth.memory_spaces is not None:
        payload["api_key_memory_scope_keys"] = [
            memory_space.policy_key for memory_space in auth.memory_spaces
        ]
    return payload


async def _resolve_auth_context_from_claims(claims: Mapping[str, object]) -> AuthContext:
    async with _auth_client_scope() as client:
        resolver = SurrealAuthContextResolver.from_client(client)
        return await resolver.resolve(claims)


async def _log_audit_event(
    client: QueryClient,
    *,
    action: str,
    user_id: UUID | None,
    organization_id: UUID | None,
    request: Request | None,
    details: SurrealRecord,
) -> str | None:
    now = _utcnow()
    audit_id = str(uuid4())
    record = {
        "uuid": audit_id,
        "user_id": _uuid_str(user_id),
        "organization_id": _uuid_str(organization_id),
        "action": action,
        "ip_address": request.client.host if request and request.client else None,
        "user_agent": request.headers.get("user-agent") if request else None,
        "details": details,
        "created_at": now,
        "updated_at": now,
    }
    try:
        await client.execute_query("CREATE audit_logs CONTENT $record;", record=record)
    except Exception as exc:
        if _is_transient_connection_error(exc):
            logger.warning(
                "Skipped transient auth audit log write action=%s error=%s",
                action,
                exc,
            )
            return None
        raise
    return audit_id


async def _list_user_org_records(client: QueryClient, *, user_id: UUID) -> list[SurrealRecord]:
    repo = _SurrealRepository(client)
    organizations = await repo.select_many(
        """
            SELECT * FROM organizations
            WHERE uuid IN (
                SELECT VALUE organization_id FROM organization_members
                WHERE user_id = $user_id
            );
        """,
        user_id=str(user_id),
    )
    organizations.sort(
        key=lambda record: (
            not bool(record.get("is_personal", False)),
            str(record.get("name") or "").lower(),
        )
    )
    return organizations


def _hash_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _generate_reset_token() -> str:
    return secrets.token_urlsafe(32)


async def _log_login_history(
    client: QueryClient,
    *,
    user_id: UUID | None,
    event_type: str,
    success: bool,
    failure_reason: str | None = None,
    email_attempted: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    record = {
        "uuid": str(uuid4()),
        "user_id": _uuid_str(user_id),
        "event_type": event_type,
        "auth_method": "password_reset",
        "success": success,
        "failure_reason": failure_reason,
        "ip_address": ip_address,
        "user_agent": user_agent,
        "device_info": None,
        "email_attempted": email_attempted,
        "session_id": None,
        "created_at": _utcnow(),
    }
    await client.execute_query("CREATE login_history CONTENT $record;", record=record)


async def _issue_auth_session(
    client: QueryClient,
    *,
    user: SimpleNamespace,
    organization: SimpleNamespace,
    request: Request | None,
    action: str,
    details: SurrealRecord,
) -> IssuedAuthSession:
    session_id = uuid4()
    access_token = create_access_token(
        user_id=user.id,
        organization_id=organization.id,
        session_id=session_id,
    )
    refresh_token, refresh_expires = create_refresh_token(
        user_id=user.id,
        organization_id=organization.id,
        session_id=session_id,
    )
    access_expires = _utcnow() + timedelta(
        minutes=config_module.settings.access_token_expire_minutes
    )
    sessions = SurrealSessionRepository.from_client(client)
    await sessions.create_session(
        user_id=user.id,
        organization_id=organization.id,
        token=access_token,
        expires_at=access_expires,
        session_id=session_id,
        refresh_token=refresh_token,
        refresh_token_expires_at=refresh_expires,
        ip_address=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent") if request else None,
    )
    await _log_audit_event(
        client,
        action=action,
        user_id=user.id,
        organization_id=organization.id,
        request=request,
        details=details,
    )
    return IssuedAuthSession(
        user=user,
        organization=organization,
        session_id=session_id,
        access_token=access_token,
        refresh_token=refresh_token,
        refresh_expires=refresh_expires,
    )


async def _issue_oidc_session(
    client: QueryClient,
    *,
    user: SimpleNamespace,
    organization: SimpleNamespace,
    request: Request | None,
    action: str,
    details: SurrealRecord,
) -> IssuedOidcSession:
    session_id = uuid4()
    expires_in = timedelta(minutes=config_module.settings.oidc.session_minutes)
    access_token = create_access_token(
        user_id=user.id,
        organization_id=organization.id,
        session_id=session_id,
        expires_in=expires_in,
        extra_claims={"amr": ["oidc"], "idp": details.get("provider_name")},
    )
    access_expires = _utcnow() + expires_in
    sessions = SurrealSessionRepository.from_client(client)
    await sessions.create_session(
        user_id=user.id,
        organization_id=organization.id,
        token=access_token,
        expires_at=access_expires,
        session_id=session_id,
        refresh_token=None,
        refresh_token_expires_at=None,
        ip_address=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent") if request else None,
    )
    await _log_audit_event(
        client,
        action=action,
        user_id=user.id,
        organization_id=organization.id,
        request=request,
        details=details,
    )
    return IssuedOidcSession(
        user=user,
        organization=organization,
        session_id=session_id,
        access_token=access_token,
        access_expires=access_expires,
    )


async def _ensure_personal_org_membership_record(
    client: QueryClient, user: AuthUser
) -> SurrealRecord:
    suffix = str(user.github_id) if user.github_id is not None else str(user.id)
    slug = f"u-{suffix}"
    now = _utcnow()
    payload = await client.execute_query(
        """
            RETURN {
                organization: (SELECT * FROM organizations WHERE slug = $slug LIMIT 1)[0],
                membership: (
                    SELECT * FROM organization_members
                    WHERE organization_id IN (
                        SELECT VALUE uuid FROM organizations WHERE slug = $slug LIMIT 1
                    )
                        AND user_id = $user_id
                    LIMIT 1
                )[0],
            };
        """,
        slug=slug,
        user_id=str(user.id),
    )
    payload = _record_payload(payload)
    organization = _normalize_record(payload.get("organization"))
    membership = _normalize_record(payload.get("membership"))

    if organization is None:
        create_result = await client.execute_query(
            "CREATE organizations CONTENT $record;",
            record={
                "uuid": str(uuid4()),
                "name": user.name or f"User {suffix}",
                "slug": slug,
                "is_personal": True,
                "settings": {},
                "created_at": now,
                "updated_at": now,
            },
        )
        error = _query_error(create_result)
        if error is not None:
            raise RuntimeError(error)
        records = _normalize_records(create_result)
        if not records:
            msg = "Failed to create personal organization"
            raise RuntimeError(msg)
        organization = records[0]

    organization_id = _coerce_uuid(organization.get("uuid"), field_name="organization.uuid")
    if membership is None:
        membership_result = await client.execute_query(
            "CREATE organization_members CONTENT $record;",
            record={
                "uuid": str(uuid4()),
                "organization_id": str(organization_id),
                "user_id": str(user.id),
                "role": OrganizationRole.OWNER.value,
                "created_at": now,
                "updated_at": now,
            },
        )
    elif str(membership.get("role") or "") != OrganizationRole.OWNER.value:
        membership_result = await client.execute_query(
            """
                UPDATE organization_members
                SET role = $role,
                    updated_at = $updated_at
                WHERE uuid = $uuid;
            """,
            uuid=str(_coerce_uuid(membership.get("uuid"), field_name="membership.uuid")),
            role=OrganizationRole.OWNER.value,
            updated_at=now,
        )
    else:
        membership_result = None
    if membership_result is not None:
        error = _query_error(membership_result)
        if error is not None:
            raise RuntimeError(error)
        if not _normalize_records(membership_result):
            msg = "Failed to write personal organization membership"
            raise RuntimeError(msg)

    return organization


def _session_id_from_access_token(token: str) -> UUID | None:
    sid = decode_token_unverified(token).get("sid")
    if not isinstance(sid, str) or not sid:
        return None
    try:
        return UUID(sid)
    except ValueError:
        return None
