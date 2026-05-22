"""Surreal-backed request-time auth adapters."""

from __future__ import annotations

import hashlib
import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Protocol, Self, cast
from uuid import UUID, uuid4

from fastapi import HTTPException
from starlette.requests import Request

from sibyl import config as config_module
from sibyl.auth.api_key_common import (
    ApiKeyAuth,
    ApiKeyMemorySpaceAuth,
    api_key_prefix,
    generate_api_key,
    hash_api_key,
    verify_api_key,
)
from sibyl.auth.http import select_access_token
from sibyl.auth.jwt import (
    JwtError,
    create_access_token,
    create_refresh_token,
    decode_token_unverified,
    verify_access_token,
)
from sibyl.auth.locks import first_user_admin_lock, oauth_identity_lock, signup_email_lock
from sibyl.auth.passwords import PasswordError, hash_password, verify_password
from sibyl.auth.primitives import (
    DeviceTokenError,
    generate_device_code,
    generate_user_code,
    hash_device_code,
)
from sibyl.auth.session_cache import access_session_cache
from sibyl.email import PasswordResetEmail, get_email_client
from sibyl.persistence.auth_common import UserNotFoundError
from sibyl.persistence.content_runtime import soft_delete_private_raw_captures_for_user
from sibyl.persistence.surreal.auth import (
    SurrealAuthContextResolver,
    SurrealOrganizationMembershipRepository,
    SurrealOrganizationRepository,
    SurrealUserRepository,
    build_surreal_auth_client,
    surreal_auth_client_scope,
)
from sibyl_core.audit import audit_event_matches_resource
from sibyl_core.auth import (
    AuthContext,
    AuthSession,
    AuthUser,
    OrganizationRole,
    ProjectRole,
    ProjectVisibility,
)
from sibyl_core.backends.surreal import SurrealAuthClient
from sibyl_core.backends.surreal.connection import _is_transient_connection_error

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
_OAUTH_DATETIME_FIELDS = {
    "created_at",
    "updated_at",
    "token_expires_at",
    "connected_at",
    "disconnected_at",
    "last_used_at",
}
_UPSERT_QUERY_BY_TABLE = {
    "api_keys": "UPSERT api_keys CONTENT $record WHERE uuid = $uuid;",
    "device_authorization_requests": (
        "UPSERT device_authorization_requests CONTENT $record WHERE uuid = $uuid;"
    ),
    "identity_provider": "UPSERT identity_provider CONTENT $record WHERE uuid = $uuid;",
    "oauth_connections": "UPSERT oauth_connections CONTENT $record WHERE uuid = $uuid;",
    "oauth_client_registrations": (
        "UPSERT oauth_client_registrations CONTENT $record WHERE uuid = $uuid;"
    ),
    "password_reset_tokens": "UPSERT password_reset_tokens CONTENT $record WHERE uuid = $uuid;",
    "memory_spaces": "UPSERT memory_spaces CONTENT $record WHERE uuid = $uuid;",
    "memory_space_members": ("UPSERT memory_space_members CONTENT $record WHERE uuid = $uuid;"),
    "projects": "UPSERT projects CONTENT $record WHERE uuid = $uuid;",
    "user_identity": "UPSERT user_identity CONTENT $record WHERE uuid = $uuid;",
    "user_sessions": "UPSERT user_sessions CONTENT $record WHERE uuid = $uuid;",
    "users": "UPSERT users CONTENT $record WHERE uuid = $uuid;",
}
_ENABLED_MEMORY_SPACE_SCOPES = {"private", "delegated", "project"}
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


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _normalize_record(record: object) -> SurrealRecord | None:
    if record is None or not isinstance(record, dict):
        return None
    out = {str(key): value for key, value in record.items()}
    out.pop("id", None)
    return out


def _normalize_records(result: object) -> list[SurrealRecord]:
    if result is None:
        return []
    if isinstance(result, dict):
        record = _normalize_record(result)
        return [record] if record is not None else []
    if not isinstance(result, list):
        return []

    records: list[SurrealRecord] = []
    for item in result:
        if isinstance(item, list):
            for nested in item:
                record = _normalize_record(nested)
                if record is not None:
                    records.append(record)
            continue
        record = _normalize_record(item)
        if record is not None:
            records.append(record)
    return records


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
            statement = statements[statement_index]
            if "result" in statement:
                return _normalize_records(statement.get("result"))
            return _normalize_records(statement)
    return _normalize_records(result)


def _record_payload(value: object) -> SurrealRecord:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _query_error(result: object) -> str | None:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        payload = {str(key): value for key, value in result.items()}
        if (
            "result" in payload
            and "status" not in payload
            and isinstance(payload.get("result"), list)
        ):
            return _query_error(payload["result"])
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


def _coerce_uuid(value: object | None, *, field_name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    msg = f"{field_name} is required"
    raise TypeError(msg)


def _coerce_optional_uuid(value: object | None) -> UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    return None


def _coerce_datetime(value: object | None) -> datetime | None:
    if value is None:
        return value
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(UTC).replace(tzinfo=None)
        return value
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC).replace(tzinfo=None)
        return parsed
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


def _apply_password_change(
    record: SurrealRecord,
    *,
    current_password: str | None,
    new_password: str,
) -> SurrealRecord:
    updated = dict(record)
    has_local_password = bool(
        record.get("password_salt")
        and record.get("password_hash")
        and record.get("password_iterations")
    )
    if not has_local_password:
        # OAuth-only accounts have no credential to verify, so a change-password
        # request here would set a new local password unauthenticated, turning a
        # transient OAuth session into persistent takeover. Adding a first local
        # password must go through a dedicated, re-authenticated flow.
        raise HTTPException(
            status_code=400,
            detail="This account has no password to change",
        )
    if not current_password:
        raise HTTPException(status_code=400, detail="Current password is required")
    try:
        password_matches = verify_password(
            current_password,
            salt_hex=str(record["password_salt"]),
            hash_hex=str(record["password_hash"]),
            iterations=_coerce_int(
                record.get("password_iterations"),
                field_name="user.password_iterations",
            ),
        )
    except (TypeError, ValueError):
        password_matches = False
    if not password_matches:
        raise HTTPException(status_code=400, detail="Invalid current password")

    try:
        password_state = hash_password(new_password)
    except PasswordError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    updated["password_salt"] = password_state.salt_hex
    updated["password_hash"] = password_state.hash_hex
    updated["password_iterations"] = password_state.iterations
    return updated


async def _load_user_update_records(
    client: QueryClient, *, user_id: UUID, email: str | None
) -> tuple[SurrealRecord | None, SurrealRecord | None]:
    if email is None:
        payload = await client.execute_query(
            """
                RETURN {
                    user: (SELECT * FROM users WHERE uuid = $user_id LIMIT 1)[0],
                    email_owner: NONE,
                };
            """,
            user_id=str(user_id),
        )
    else:
        payload = await client.execute_query(
            """
                RETURN {
                    user: (SELECT * FROM users WHERE uuid = $user_id LIMIT 1)[0],
                    email_owner: (
                        SELECT * FROM users
                        WHERE email = $email
                        LIMIT 1
                    )[0],
                };
            """,
            user_id=str(user_id),
            email=email,
        )
    payload = _record_payload(payload)
    return _normalize_record(payload.get("user")), _normalize_record(payload.get("email_owner"))


def _session_namespace(record: SurrealRecord | None) -> SimpleNamespace | None:
    return _ns(
        record,
        uuid_fields={"uuid", "user_id", "organization_id"},
        datetime_fields=_SESSION_DATETIME_FIELDS,
    )


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
    return _ns(
        record,
        uuid_fields={"uuid", "user_id", "organization_id"},
        datetime_fields=_DEVICE_DATETIME_FIELDS,
    )


def _oauth_connection_namespace(record: SurrealRecord | None) -> SimpleNamespace | None:
    return _ns(
        record,
        uuid_fields={"uuid", "user_id"},
        datetime_fields=_OAUTH_DATETIME_FIELDS,
    )


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
        record = {
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


async def resolve_auth_context(
    *,
    claims: Mapping[str, object],
    session: object | None = None,
) -> AuthContext:
    del session
    return await _resolve_auth_context_from_claims(claims)


async def _log_audit_event(
    client: QueryClient,
    *,
    action: str,
    user_id: UUID | None,
    organization_id: UUID | None,
    request: Request | None,
    details: SurrealRecord,
) -> None:
    now = _utcnow()
    record = {
        "uuid": str(uuid4()),
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
            return
        raise


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


async def authenticate_api_key(raw_key: str):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        candidates = await repo.select_many(
            "SELECT * FROM api_keys WHERE key_prefix = $key_prefix ORDER BY created_at DESC;",
            key_prefix=api_key_prefix(raw_key),
        )
        now = _utcnow()
        for candidate in candidates:
            if candidate.get("revoked_at") is not None:
                continue
            expires_at = _coerce_datetime(candidate.get("expires_at"))
            if expires_at is not None and expires_at <= now:
                continue
            if not verify_api_key(
                raw_key,
                salt_hex=str(candidate.get("key_salt") or ""),
                hash_hex=str(candidate.get("key_hash") or ""),
            ):
                continue
            api_key_id = _coerce_uuid(candidate.get("uuid"), field_name="api_key.uuid")
            await client.execute_query(
                """
                UPDATE api_keys
                SET last_used_at = $last_used_at, updated_at = $updated_at
                WHERE uuid = $api_key_id AND revoked_at = NONE;
                """,
                api_key_id=str(api_key_id),
                last_used_at=now,
                updated_at=now,
            )
            project_scope_records = await repo.select_many(
                "SELECT * FROM api_key_project_scopes "
                "WHERE api_key_id = $api_key_id ORDER BY created_at ASC;",
                api_key_id=str(api_key_id),
            )
            project_record_ids = [
                str(record["project_id"])
                for record in project_scope_records
                if str(record.get("project_id") or "").strip()
            ]
            project_records = (
                await repo.select_many(
                    "SELECT uuid, graph_project_id FROM projects "
                    "WHERE uuid IN $project_ids ORDER BY created_at ASC;",
                    project_ids=project_record_ids,
                )
                if project_record_ids
                else []
            )
            project_ids = [
                str(record["graph_project_id"])
                for record in project_records
                if str(record.get("graph_project_id") or "").strip()
            ]
            memory_scope_records = await repo.select_many(
                "SELECT * FROM api_key_memory_space_scopes "
                "WHERE api_key_id = $api_key_id ORDER BY created_at ASC;",
                api_key_id=str(api_key_id),
            )
            memory_space_ids = [
                str(record["memory_space_id"])
                for record in memory_scope_records
                if str(record.get("memory_space_id") or "").strip()
            ]
            memory_space_records = (
                await repo.select_many(
                    "SELECT uuid, memory_scope, scope_key FROM memory_spaces "
                    "WHERE uuid IN $memory_space_ids AND organization_id = $organization_id "
                    "ORDER BY created_at ASC;",
                    memory_space_ids=memory_space_ids,
                    organization_id=str(candidate.get("organization_id")),
                )
                if memory_space_ids
                else []
            )
            memory_spaces = [_api_key_memory_space_scope(record) for record in memory_space_records]
            return ApiKeyAuth(
                api_key_id=api_key_id,
                user_id=_coerce_uuid(candidate.get("user_id"), field_name="api_key.user_id"),
                organization_id=_coerce_uuid(
                    candidate.get("organization_id"), field_name="api_key.organization_id"
                ),
                scopes=_scopes_list(candidate.get("scopes")),
                project_ids=project_ids or None,
                memory_space_ids=[space.memory_space_id for space in memory_spaces] or None,
                memory_spaces=memory_spaces or None,
            )
    return None


async def authenticate_local_user(*, email: str, password: str):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        record = await repo.select_one(
            "SELECT * FROM users WHERE email = $email AND deleted_at = NONE LIMIT 1;",
            email=email.strip().lower(),
        )
        if record is None:
            return None
        if not record.get("password_salt") or not record.get("password_hash"):
            return None
        ok = verify_password(
            password,
            salt_hex=str(record["password_salt"]),
            hash_hex=str(record["password_hash"]),
            iterations=_coerce_int(
                record.get("password_iterations") or config_module.settings.password_iterations,
                field_name="user.password_iterations",
            ),
        )
        if not ok:
            return None
        return _auth_user_namespace(record)


async def get_user_by_id(user_id: UUID):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        record = await repo.select_one(
            "SELECT * FROM users WHERE uuid = $uuid AND deleted_at = NONE LIMIT 1;",
            uuid=str(user_id),
        )
        return _auth_user_namespace(record)


async def list_user_organizations(*, user_id: UUID) -> list[SimpleNamespace]:
    async with _auth_client_scope() as client:
        records = await _list_user_org_records(client, user_id=user_id)
        return [org for record in records if (org := _auth_org_namespace(record)) is not None]


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


async def ensure_personal_organization(*, user_id: UUID):
    async with _auth_client_scope() as client:
        users = SurrealUserRepository.from_client(client)
        user = await users.get_by_id(user_id)
        if user is None:
            return None
        return _auth_org_namespace(await _ensure_personal_org_membership_record(client, user))


async def create_session_record(
    *,
    user_id: UUID,
    token: str,
    expires_at,
    session_id: UUID | None = None,
    organization_id: UUID | None = None,
    refresh_token: str | None = None,
    refresh_token_expires_at=None,
    device_name: str | None = None,
    device_type: str | None = None,
    browser: str | None = None,
    os: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    location: str | None = None,
):
    async with _auth_client_scope() as client:
        sessions = SurrealSessionRepository.from_client(client)
        session = await sessions.create_session(
            user_id=user_id,
            token=token,
            expires_at=expires_at,
            session_id=session_id,
            organization_id=organization_id,
            refresh_token=refresh_token,
            refresh_token_expires_at=refresh_token_expires_at,
            device_name=device_name,
            device_type=device_type,
            browser=browser,
            os=os,
            ip_address=ip_address,
            user_agent=user_agent,
            location=location,
        )
        return _auth_session_namespace(session)


async def load_refresh_session_record(refresh_token: str):
    async with _auth_client_scope() as client:
        sessions = SurrealSessionRepository.from_client(client)
        session = await sessions.get_session_by_refresh_token(refresh_token)
        return _auth_session_namespace(session)


async def rotate_refresh_session_record(
    refresh_token: str,
    *,
    new_access_token: str,
    new_access_expires_at,
    new_refresh_token: str,
    new_refresh_expires_at,
):
    async with _auth_client_scope() as client:
        sessions = SurrealSessionRepository.from_client(client)
        existing = await sessions.get_session_by_refresh_token(refresh_token)
        if existing is None:
            return None
        rotated = await sessions.rotate_tokens(
            existing,
            new_access_token=new_access_token,
            new_access_expires_at=new_access_expires_at,
            new_refresh_token=new_refresh_token,
            new_refresh_expires_at=new_refresh_expires_at,
        )
        return _auth_session_namespace(rotated)


async def revoke_refresh_session_record(refresh_token: str) -> None:
    async with _auth_client_scope() as client:
        sessions = SurrealSessionRepository.from_client(client)
        existing = await sessions.get_session_by_refresh_token(refresh_token)
        if existing is None:
            return
        await sessions.revoke_loaded_session(existing)


async def load_oauth_client_registration(client_id: str) -> SurrealRecord | None:
    normalized = client_id.strip()
    if not normalized:
        return None
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        record = await repo.select_one(
            "SELECT * FROM oauth_client_registrations WHERE client_id = $client_id LIMIT 1;",
            client_id=normalized,
        )
        if record is None:
            return None
        client_info = record.get("client_info")
        if not isinstance(client_info, dict):
            return None
        return {str(key): value for key, value in client_info.items()}


async def save_oauth_client_registration(
    *,
    client_id: str,
    client_info: Mapping[str, object],
) -> None:
    normalized = client_id.strip()
    if not normalized:
        return
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        existing = await repo.select_one(
            "SELECT * FROM oauth_client_registrations WHERE client_id = $client_id LIMIT 1;",
            client_id=normalized,
        )
        now = _utcnow()
        registration_id = (
            _coerce_uuid(existing.get("uuid"), field_name="oauth_client_registrations.uuid")
            if existing is not None
            else uuid4()
        )
        record: SurrealRecord = {
            "uuid": str(registration_id),
            "client_id": normalized,
            "client_info": dict(client_info),
            "created_at": existing.get("created_at") if existing is not None else now,
            "updated_at": now,
        }
        await repo.replace_record(
            "oauth_client_registrations",
            uuid=registration_id,
            record=record,
        )


def _normalize_oidc_role(role: object) -> OrganizationRole:
    value = _role_value(role) or str(role)
    try:
        return OrganizationRole(value)
    except ValueError as exc:
        msg = f"Unsupported OIDC organization role: {value}"
        raise ValueError(msg) from exc


async def _safe_oidc_email(
    client: QueryClient,
    *,
    email: str | None,
    user_id: UUID | None,
) -> str | None:
    normalized = (email or "").strip().lower()
    if not normalized:
        return None
    repo = _SurrealRepository(client)
    owner = await repo.select_one(
        "SELECT uuid FROM users WHERE email = $email AND deleted_at = NONE LIMIT 1;",
        email=normalized,
    )
    if owner is None:
        return normalized
    owner_id = _coerce_uuid(owner.get("uuid"), field_name="user.uuid")
    return normalized if user_id is not None and owner_id == user_id else None


async def _upsert_identity_provider(
    client: QueryClient,
    *,
    provider_name: str,
    issuer: str,
    client_id: str | None,
    scopes: list[str],
    role_claim: str | None,
) -> None:
    repo = _SurrealRepository(client)
    existing = await repo.select_one(
        "SELECT * FROM identity_provider WHERE name = $name LIMIT 1;",
        name=provider_name,
    )
    now = _utcnow()
    provider_id = (
        _coerce_uuid(existing.get("uuid"), field_name="identity_provider.uuid")
        if existing is not None
        else uuid4()
    )
    record: SurrealRecord = {
        "uuid": str(provider_id),
        "name": provider_name,
        "issuer": issuer,
        "client_id": client_id,
        "scopes": _unique_strings(scopes),
        "role_claim": role_claim or config_module.settings.oidc.role_claim,
        "enabled": True,
        "created_at": existing.get("created_at") if existing is not None else now,
        "updated_at": now,
    }
    await repo.replace_record("identity_provider", uuid=provider_id, record=record)


async def _ensure_oidc_organization_membership_record(
    client: QueryClient,
    *,
    user_id: UUID,
    user_name: str,
    role: OrganizationRole,
) -> SurrealRecord:
    repo = _SurrealRepository(client)
    now = _utcnow()
    organization = await repo.select_one(
        "SELECT * FROM organizations WHERE is_personal = false ORDER BY created_at ASC LIMIT 1;"
    )
    if organization is None:
        create_result = await client.execute_query(
            "CREATE organizations CONTENT $record;",
            record={
                "uuid": str(uuid4()),
                "name": "Sibyl",
                "slug": "sibyl",
                "is_personal": False,
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
            msg = "Failed to create OIDC organization"
            raise RuntimeError(msg)
        organization = records[0]

    organization_id = _coerce_uuid(organization.get("uuid"), field_name="organization.uuid")
    membership = await repo.select_one(
        """
            SELECT * FROM organization_members
            WHERE organization_id = $organization_id AND user_id = $user_id
            LIMIT 1;
        """,
        organization_id=str(organization_id),
        user_id=str(user_id),
    )
    if membership is None:
        result = await client.execute_query(
            "CREATE organization_members CONTENT $record;",
            record={
                "uuid": str(uuid4()),
                "organization_id": str(organization_id),
                "user_id": str(user_id),
                "role": role.value,
                "created_at": now,
                "updated_at": now,
            },
        )
    else:
        result = await client.execute_query(
            """
                UPDATE organization_members
                SET role = $role,
                    updated_at = $updated_at
                WHERE uuid = $uuid;
            """,
            uuid=str(_coerce_uuid(membership.get("uuid"), field_name="membership.uuid")),
            role=role.value,
            updated_at=now,
        )
    error = _query_error(result)
    if error is not None:
        raise RuntimeError(error)
    if not _normalize_records(result):
        msg = f"Failed to write OIDC membership for {user_name or user_id}"
        raise RuntimeError(msg)
    return organization


async def login_oidc_identity(
    *,
    provider_name: str,
    issuer: str,
    client_id: str | None = None,
    scopes: list[str] | None = None,
    role_claim: str | None = None,
    subject: str,
    subject_key: str,
    email: str | None,
    name: str,
    avatar_url: str | None,
    role: OrganizationRole | str,
    claims: Mapping[str, object],
    request: Request,
    action: str = "auth.oidc.login",
) -> IssuedOidcSession:
    provider = provider_name.strip().lower()
    if not provider or not subject_key.strip():
        msg = "OIDC provider and subject key are required"
        raise ValueError(msg)
    org_role = _normalize_oidc_role(role)
    now = _utcnow()
    async with (
        oauth_identity_lock(provider, subject_key),
        _auth_client_scope() as client,
    ):
        repo = _SurrealRepository(client)
        await _upsert_identity_provider(
            client,
            provider_name=provider,
            issuer=issuer,
            client_id=client_id,
            scopes=scopes or [],
            role_claim=role_claim,
        )
        identity = await repo.select_one(
            """
                SELECT * FROM user_identity
                WHERE provider_name = $provider_name AND subject_key = $subject_key
                LIMIT 1;
            """,
            provider_name=provider,
            subject_key=subject_key,
        )
        user_record: SurrealRecord | None = None
        user_id: UUID | None = None
        if identity is not None:
            user_id = _coerce_uuid(identity.get("user_id"), field_name="user_identity.user_id")
            user_record = await repo.select_one(
                "SELECT * FROM users WHERE uuid = $uuid LIMIT 1;",
                uuid=str(user_id),
            )
            if user_record is not None and user_record.get("deleted_at") is not None:
                msg = "User is scheduled for deletion"
                raise ValueError(msg)

        safe_email = await _safe_oidc_email(client, email=email, user_id=user_id)
        display_name = name.strip() or safe_email or subject
        is_admin = org_role in {OrganizationRole.OWNER, OrganizationRole.ADMIN}
        if user_record is None:
            user_id = uuid4()
            create_result = await client.execute_query(
                "CREATE users CONTENT $record;",
                record={
                    "uuid": str(user_id),
                    "email": safe_email,
                    "name": display_name,
                    "avatar_url": avatar_url,
                    "github_id": None,
                    "is_admin": is_admin,
                    "bio": None,
                    "timezone": "UTC",
                    "preferences": {},
                    "password_salt": None,
                    "password_hash": None,
                    "password_iterations": None,
                    "email_verified_at": now if safe_email else None,
                    "last_login_at": now,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            error = _query_error(create_result)
            if error is not None:
                raise RuntimeError(error)
            records = _normalize_records(create_result)
            if not records:
                msg = "Failed to create OIDC user"
                raise RuntimeError(msg)
            user_record = records[0]
        else:
            user_id = _coerce_uuid(user_record.get("uuid"), field_name="user.uuid")
            update_email = safe_email if safe_email is not None else user_record.get("email")
            update_result = await client.execute_query(
                """
                    UPDATE users
                    SET email = $email,
                        name = $name,
                        avatar_url = $avatar_url,
                        is_admin = $is_admin,
                        last_login_at = $last_login_at,
                        updated_at = $updated_at
                    WHERE uuid = $uuid;
                """,
                uuid=str(user_id),
                email=update_email,
                name=display_name,
                avatar_url=avatar_url,
                is_admin=is_admin,
                last_login_at=now,
                updated_at=now,
            )
            error = _query_error(update_result)
            if error is not None:
                raise RuntimeError(error)
            records = _normalize_records(update_result)
            if not records:
                msg = "Failed to update OIDC user"
                raise RuntimeError(msg)
            user_record = records[0]

        identity_id = (
            _coerce_uuid(identity.get("uuid"), field_name="user_identity.uuid")
            if identity is not None
            else uuid4()
        )
        identity_record: SurrealRecord = {
            "uuid": str(identity_id),
            "provider_name": provider,
            "issuer": issuer,
            "subject": subject,
            "subject_key": subject_key,
            "user_id": str(user_id),
            "email": safe_email,
            "claims": {str(key): value for key, value in claims.items()},
            "created_at": identity.get("created_at") if identity is not None else now,
            "updated_at": now,
            "last_login_at": now,
        }
        await repo.replace_record("user_identity", uuid=identity_id, record=identity_record)

        organization = _require_namespace(
            _auth_org_namespace(
                await _ensure_oidc_organization_membership_record(
                    client,
                    user_id=user_id,
                    user_name=display_name,
                    role=org_role,
                )
            ),
            label="organization",
        )
        return await _issue_oidc_session(
            client,
            user=_require_namespace(_auth_user_namespace(user_record), label="user"),
            organization=organization,
            request=request,
            action=action,
            details={
                "provider_name": provider,
                "issuer": issuer,
                "subject_key": subject_key,
                "role": org_role.value,
                "email": safe_email,
            },
        )


async def login_github_identity(*, identity, request) -> IssuedAuthSession:
    async with (
        oauth_identity_lock("github", identity.github_id),
        first_user_admin_lock(),
        _auth_client_scope() as client,
    ):
        users = SurrealUserRepository.from_client(client)
        is_first_user = not await users.has_any_users()
        user = await users.upsert_from_github(identity, is_admin=is_first_user)
        organization = _require_namespace(
            _auth_org_namespace(await _ensure_personal_org_membership_record(client, user)),
            label="organization",
        )
        return await _issue_auth_session(
            client,
            user=_require_namespace(
                _auth_user_namespace(
                    {
                        "uuid": str(user.id),
                        "email": user.email,
                        "name": user.name,
                        "avatar_url": user.avatar_url,
                        "github_id": user.github_id,
                        "is_admin": user.is_admin,
                        "bio": user.bio,
                        "timezone": user.timezone,
                        "preferences": dict(user.preferences),
                    }
                ),
                label="user",
            ),
            organization=organization,
            request=request,
            action="auth.github.login",
            details={"github_id": user.github_id, "email": user.email},
        )


async def signup_local_user(*, email: str, password: str, name: str, request):
    async with signup_email_lock(email), first_user_admin_lock(), _auth_client_scope() as client:
        users = SurrealUserRepository.from_client(client)
        is_first_user = not await users.has_any_users()
        user = await users.create_local_user(
            email=email,
            password=password,
            name=name,
            is_admin=is_first_user,
        )
        organization = _require_namespace(
            _auth_org_namespace(await _ensure_personal_org_membership_record(client, user)),
            label="organization",
        )
        return await _issue_auth_session(
            client,
            user=_require_namespace(
                _auth_user_namespace(
                    {
                        "uuid": str(user.id),
                        "email": user.email,
                        "name": user.name,
                        "avatar_url": user.avatar_url,
                        "github_id": user.github_id,
                        "is_admin": user.is_admin,
                        "bio": user.bio,
                        "timezone": user.timezone,
                        "preferences": dict(user.preferences),
                    }
                ),
                label="user",
            ),
            organization=organization,
            request=request,
            action="auth.local.signup",
            details={"email": user.email},
        )


async def delete_failed_local_signup_user(*, user_id: UUID, organization_id: UUID | None) -> None:
    async with _auth_client_scope() as client:
        await client.execute_query(
            "DELETE FROM user_sessions WHERE user_id = $user_id;",
            user_id=str(user_id),
        )
        if organization_id is not None:
            await client.execute_query(
                "DELETE FROM organization_members "
                "WHERE user_id = $user_id AND organization_id = $organization_id;",
                user_id=str(user_id),
                organization_id=str(organization_id),
            )
            await client.execute_query(
                "DELETE FROM organizations WHERE uuid = $organization_id AND is_personal = true;",
                organization_id=str(organization_id),
            )
        await client.execute_query(
            "DELETE FROM users WHERE uuid = $user_id;",
            user_id=str(user_id),
        )


async def login_local_user(*, email: str, password: str, request):
    async with _auth_client_scope() as client:
        users = SurrealUserRepository.from_client(client)
        user = await users.authenticate_local(email=email, password=password)
        if user is None:
            return None
        organization = _require_namespace(
            _auth_org_namespace(await _ensure_personal_org_membership_record(client, user)),
            label="organization",
        )
        break_glass = config_module.settings.break_glass_enabled
        return await _issue_auth_session(
            client,
            user=_require_namespace(
                _auth_user_namespace(
                    {
                        "uuid": str(user.id),
                        "email": user.email,
                        "name": user.name,
                        "avatar_url": user.avatar_url,
                        "github_id": user.github_id,
                        "is_admin": user.is_admin,
                        "bio": user.bio,
                        "timezone": user.timezone,
                        "preferences": dict(user.preferences),
                    }
                ),
                label="user",
            ),
            organization=organization,
            request=request,
            action="auth.break_glass.login" if break_glass else "auth.local.login",
            details={"break_glass": break_glass, "email": user.email},
        )


async def start_device_authorization(
    *,
    client_name: str | None,
    scope: str,
    expires_in,
    poll_interval_seconds: int,
):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        now = _utcnow()
        expires_at = now + expires_in
        for _ in range(20):
            device_code = generate_device_code()
            user_code = generate_user_code()
            device_code_hash = hash_device_code(device_code)
            existing = await repo.select_one(
                "SELECT * FROM device_authorization_requests "
                "WHERE device_code_hash = $device_code_hash OR user_code = $user_code LIMIT 1;",
                device_code_hash=device_code_hash,
                user_code=user_code,
            )
            if existing is not None:
                continue
            record = {
                "uuid": str(uuid4()),
                "device_code_hash": device_code_hash,
                "user_code": user_code,
                "client_name": (client_name or "").strip() or None,
                "scope": (scope or "").strip() or "mcp",
                "status": "pending",
                "poll_interval_seconds": max(1, int(poll_interval_seconds)),
                "last_polled_at": None,
                "expires_at": expires_at,
                "approved_at": None,
                "denied_at": None,
                "consumed_at": None,
                "user_id": None,
                "organization_id": None,
                "created_at": now,
                "updated_at": now,
            }
            created = _normalize_records(
                await client.execute_query(
                    "CREATE device_authorization_requests CONTENT $record;",
                    record=record,
                )
            )
            if not created:
                msg = "Failed to create device authorization request"
                raise RuntimeError(msg)
            return _device_request_namespace(created[0]), device_code
    msg = "Failed to allocate unique device/user codes"
    raise RuntimeError(msg)


async def exchange_device_code(*, device_code: str) -> dict[str, object]:
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        sessions = SurrealSessionRepository.from_client(client)
        device_code_hash = hash_device_code(device_code)
        record = await repo.select_one(
            "SELECT * FROM device_authorization_requests "
            "WHERE device_code_hash = $device_code_hash LIMIT 1;",
            device_code_hash=device_code_hash,
        )
        request_row = _device_request_namespace(record)
        if request_row is None:
            raise DeviceTokenError("invalid_grant", "Invalid device_code")
        now = _utcnow()
        if request_row.expires_at <= now:
            raise DeviceTokenError("expired_token", "Device code expired")
        if request_row.status == "denied":
            raise DeviceTokenError("access_denied", "User denied the request")
        if request_row.status == "consumed":
            raise DeviceTokenError("invalid_grant", "Device code already used")
        if request_row.status != "approved":
            interval = int(request_row.poll_interval_seconds or 5)
            if request_row.last_polled_at is not None:
                delta = (now - request_row.last_polled_at).total_seconds()
                if delta < interval:
                    raise DeviceTokenError("slow_down", "Polling too frequently")
            await client.execute_query(
                """
                    UPDATE device_authorization_requests
                    SET last_polled_at = $last_polled_at,
                        updated_at = $updated_at
                    WHERE uuid = $uuid;
                """,
                uuid=str(request_row.id),
                last_polled_at=now,
                updated_at=now,
            )
            raise DeviceTokenError("authorization_pending", "Authorization pending")

        if request_row.user_id is None:
            raise DeviceTokenError("server_error", "Approved request missing user_id")
        session_id = uuid4()
        access_token = create_access_token(
            user_id=request_row.user_id,
            organization_id=request_row.organization_id,
            session_id=session_id,
            extra_claims={"scope": (request_row.scope or "mcp").strip() or "mcp"},
        )
        refresh_token, refresh_expires = create_refresh_token(
            user_id=request_row.user_id,
            organization_id=request_row.organization_id,
            session_id=session_id,
        )
        access_expires = now + timedelta(minutes=config_module.settings.access_token_expire_minutes)
        await sessions.create_session(
            user_id=request_row.user_id,
            organization_id=request_row.organization_id,
            token=access_token,
            expires_at=access_expires,
            session_id=session_id,
            refresh_token=refresh_token,
            refresh_token_expires_at=refresh_expires,
            device_name=request_row.client_name,
            device_type="device",
        )
        await client.execute_query(
            """
                UPDATE device_authorization_requests
                SET status = $status,
                    consumed_at = $consumed_at,
                    updated_at = $updated_at
                WHERE uuid = $uuid;
            """,
            uuid=str(request_row.id),
            status="consumed",
            consumed_at=now,
            updated_at=now,
        )
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "expires_in": int(
                timedelta(
                    minutes=config_module.settings.access_token_expire_minutes
                ).total_seconds()
            ),
            "scope": (request_row.scope or "mcp").strip() or "mcp",
        }


async def get_device_request_by_user_code(user_code: str):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        record = await repo.select_one(
            "SELECT * FROM device_authorization_requests WHERE user_code = $user_code LIMIT 1;",
            user_code=user_code,
        )
        return _device_request_namespace(record)


async def _load_device_authorization_user_and_request(
    client: QueryClient, *, user_id: UUID, user_code: str
) -> tuple[AuthUser | None, SurrealRecord | None, SimpleNamespace | None]:
    payload = await client.execute_query(
        """
            RETURN {
                user: (SELECT * FROM users WHERE uuid = $user_id LIMIT 1)[0],
                device_request: (
                    SELECT * FROM device_authorization_requests
                    WHERE user_code = $user_code
                    LIMIT 1
                )[0],
            };
        """,
        user_id=str(user_id),
        user_code=user_code,
    )
    payload = _record_payload(payload)
    user = _auth_user_model(_normalize_record(payload.get("user")))
    record = _normalize_record(payload.get("device_request"))
    return user, record, _device_request_namespace(record)


async def resolve_request_claims(request) -> SurrealRecord | None:
    claims = getattr(request.state, "jwt_claims", None)
    if claims:
        return claims
    token = select_access_token(
        authorization=request.headers.get("authorization"),
        cookie_token=request.cookies.get("sibyl_access_token"),
    )
    if not token:
        return None
    try:
        return verify_access_token(token)
    except JwtError:
        pass
    if token.startswith("sk_"):
        auth = await authenticate_api_key(token)
        if auth is None:
            return None
        scopes = list(auth.scopes or [])
        if _is_rest_request(request) and not _api_key_allows_rest(
            scopes=scopes, method=request.method
        ):
            raise HTTPException(status_code=403, detail="Insufficient API key scope")
        return _api_key_claim_payload(auth)
    return None


async def resolve_request_user(request):
    claims = await resolve_request_claims(request)
    if not claims:
        return None
    try:
        user_id = UUID(str(claims.get("sub", "")))
    except ValueError:
        return None
    return await get_user_by_id(user_id)


async def validate_access_session(token: str) -> bool:
    session_id = _session_id_from_access_token(token)
    if session_id is not None:
        cached = access_session_cache.get(session_id)
        if cached is not None:
            return cached

    async with _auth_client_scope() as client:
        sessions = SurrealSessionRepository.from_client(client)
        if session_id is not None:
            session = await sessions.get_session_by_id(session_id)
            if session is None:
                access_session_cache.mark_revoked(session_id)
                return False
            access_session_cache.store_session(session)
            return True
        return await sessions.get_session_by_token(token) is not None


def _session_id_from_access_token(token: str) -> UUID | None:
    sid = decode_token_unverified(token).get("sid")
    if not isinstance(sid, str) or not sid:
        return None
    try:
        return UUID(sid)
    except ValueError:
        return None


async def login_device_browser_user(*, email: str, password: str, request):
    issued = await login_local_user(email=email, password=password, request=request)
    if issued is None:
        return None
    return DeviceBrowserLogin(
        user=issued.user,
        organization=issued.organization,
        access_token=issued.access_token,
        refresh_token=issued.refresh_token,
        refresh_expires=issued.refresh_expires,
    )


async def deny_device_authorization(*, user_id: UUID, user_code: str, request):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        user, record, request_row = await _load_device_authorization_user_and_request(
            client, user_id=user_id, user_code=user_code
        )
        if user is None:
            return None
        now = _utcnow()
        if (
            record is None
            or request_row is None
            or request_row.expires_at <= now
            or request_row.status != "pending"
        ):
            return None
        updated = {
            **record,
            "status": "denied",
            "denied_at": now,
            "updated_at": now,
        }
        written = await repo.replace_record(
            "device_authorization_requests",
            uuid=request_row.id,
            record=updated,
        )
        await _log_audit_event(
            client,
            action="auth.device.deny",
            user_id=user.id,
            organization_id=None,
            request=request,
            details={
                "device_request_id": str(request_row.id),
                "client_name": request_row.client_name,
            },
        )
        return _device_request_namespace(written)


async def approve_device_authorization(*, user_id: UUID, user_code: str, request):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        user, record, request_row = await _load_device_authorization_user_and_request(
            client, user_id=user_id, user_code=user_code
        )
        if user is None:
            return None
        now = _utcnow()
        if (
            record is None
            or request_row is None
            or request_row.expires_at <= now
            or request_row.status != "pending"
        ):
            return None
        organization_record = await _ensure_personal_org_membership_record(client, user)
        organization = _require_namespace(_auth_org_namespace(organization_record), label="org")
        updated = {
            **record,
            "status": "approved",
            "approved_at": now,
            "user_id": str(user.id),
            "organization_id": str(organization.id),
            "updated_at": now,
        }
        written = await repo.replace_record(
            "device_authorization_requests",
            uuid=request_row.id,
            record=updated,
        )
        await _log_audit_event(
            client,
            action="auth.device.approve",
            user_id=user.id,
            organization_id=organization.id,
            request=request,
            details={
                "device_request_id": str(request_row.id),
                "client_name": request_row.client_name,
            },
        )
        return (
            _auth_org_namespace(
                {
                    "uuid": str(organization.id),
                    "name": organization.name,
                    "slug": organization.slug,
                    "is_personal": organization.is_personal,
                    "settings": dict(organization.settings),
                }
            ),
            _device_request_namespace(written),
        )


async def rotate_refresh_exchange(
    *,
    refresh_token: str,
    user_id: UUID,
    organization_id: UUID | None,
    request,
):
    async with _auth_client_scope() as client:
        sessions = SurrealSessionRepository.from_client(client)
        existing = await sessions.get_session_by_refresh_token(refresh_token)
        if existing is None:
            return None

        rotation: RefreshRotation | None = None
        for attempt in range(2):
            access_token = create_access_token(
                user_id=user_id,
                organization_id=organization_id,
                session_id=existing.id,
            )
            new_refresh_token, refresh_expires = create_refresh_token(
                user_id=user_id,
                organization_id=organization_id,
                session_id=existing.id,
            )
            access_expires = _utcnow() + timedelta(
                minutes=config_module.settings.access_token_expire_minutes
            )
            try:
                await sessions.rotate_tokens(
                    existing,
                    new_access_token=access_token,
                    new_access_expires_at=access_expires,
                    new_refresh_token=new_refresh_token,
                    new_refresh_expires_at=refresh_expires,
                )
            except LookupError:
                if attempt == 1:
                    return None
                existing = await sessions.get_session_by_refresh_token(refresh_token)
                if existing is None:
                    return None
                continue
            rotation = RefreshRotation(
                session_id=existing.id,
                access_token=access_token,
                refresh_token=new_refresh_token,
                refresh_expires=refresh_expires,
                user_id=user_id,
                organization_id=organization_id,
            )
            break

        if rotation is None:
            return None
        await _log_audit_event(
            client,
            action="auth.token.refresh",
            user_id=user_id,
            organization_id=organization_id,
            request=request,
            details={"session_id": str(existing.id)},
        )
        return rotation


async def revoke_access_session(token: str) -> None:
    async with _auth_client_scope() as client:
        sessions = SurrealSessionRepository.from_client(client)
        session_id = _session_id_from_access_token(token)
        existing = (
            await sessions.get_session_by_id(session_id)
            if session_id is not None
            else await sessions.get_session_by_token(token)
        )
        if existing is None:
            if session_id is not None:
                access_session_cache.mark_revoked(session_id)
            return
        await sessions.revoke_loaded_session(existing)
        access_session_cache.mark_revoked(
            existing.id,
            user_id=existing.user_id,
            organization_id=existing.organization_id,
            expires_at=existing.refresh_token_expires_at or existing.expires_at,
        )


async def log_audit_event(
    *,
    action: str,
    user_id: UUID | None,
    organization_id: UUID | None,
    request,
    details: SurrealRecord,
) -> None:
    async with _auth_client_scope() as client:
        await _log_audit_event(
            client,
            action=action,
            user_id=user_id,
            organization_id=organization_id,
            request=request,
            details=details,
        )


def _audit_where_clause(
    *,
    organization_id: UUID | str,
    user_id: UUID | str | None,
    action: str | None,
    start_time: datetime | None,
    end_time: datetime | None,
) -> tuple[str, SurrealRecord]:
    clauses = ["organization_id = $organization_id"]
    params: SurrealRecord = {"organization_id": str(organization_id)}
    if user_id:
        clauses.append("user_id = $user_id")
        params["user_id"] = str(user_id)
    if action:
        clauses.append("action = $action")
        params["action"] = action
    if start_time:
        clauses.append("created_at >= $start_time")
        params["start_time"] = start_time
    if end_time:
        clauses.append("created_at <= $end_time")
        params["end_time"] = end_time
    return " AND ".join(clauses), params


def _audit_total(row: SurrealRecord | None) -> int:
    if row is None:
        return 0
    value = row.get("total")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if value is None:
        return 0
    try:
        return int(str(value))
    except ValueError:
        return 0


async def list_audit_events(
    *,
    organization_id: UUID | str,
    user_id: UUID | str | None = None,
    action: str | None = None,
    resource: str | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[SurrealRecord], int]:
    bounded_limit = max(1, min(limit, 200))
    bounded_offset = max(0, offset)
    where_clause, params = _audit_where_clause(
        organization_id=organization_id,
        user_id=user_id,
        action=action,
        start_time=start_time,
        end_time=end_time,
    )

    if resource:
        scan_limit = min(max((bounded_limit + bounded_offset + 1) * 5, 200), 5000)
        query = (
            f"SELECT * FROM audit_logs WHERE {where_clause} "  # noqa: S608
            "ORDER BY created_at DESC LIMIT $scan_limit;"
        )
        async with _auth_client_scope() as client:
            repo = _SurrealRepository(client)
            rows = await repo.select_many(query, **params, scan_limit=scan_limit)
        filtered = [row for row in rows if audit_event_matches_resource(row, resource)]
        return filtered[bounded_offset : bounded_offset + bounded_limit], len(filtered)

    scan_limit = bounded_offset + bounded_limit
    query = (
        f"SELECT * FROM audit_logs WHERE {where_clause} "  # noqa: S608
        "ORDER BY created_at DESC LIMIT $scan_limit;"
    )
    count_query = f"SELECT count() AS total FROM audit_logs WHERE {where_clause} GROUP ALL;"  # noqa: S608
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        rows = await repo.select_many(query, **params, scan_limit=scan_limit)
        count_row = await repo.select_one(count_query, **params)
    return rows[bounded_offset:], _audit_total(count_row)


def _bounded_audit_value(value: object, *, depth: int = 0) -> object:
    if depth >= 3:
        return str(value)[:500]
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, Mapping):
        out: SurrealRecord = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 40:
                out["truncated"] = len(value) - 40
                break
            out[str(key)[:80]] = _bounded_audit_value(item, depth=depth + 1)
        return out
    if isinstance(value, list | tuple | set | frozenset):
        items = list(value)
        out = [_bounded_audit_value(item, depth=depth + 1) for item in items[:20]]
        if len(items) > 20:
            out.append({"truncated": len(items) - 20})
        return out
    return str(value)[:500]


def _bounded_audit_string(value: object | None) -> str | None:
    if value is None:
        return None
    return str(value)[:500]


def _bounded_audit_id_list(values: list[str] | None) -> tuple[list[str], int]:
    if not values:
        return [], 0
    out = [str(value)[:200] for value in values[:20]]
    return out, max(len(values) - 20, 0)


_MEMORY_AUDIT_ACTION_PREFIX = "memory."
_MEMORY_AUDIT_ACTION_CEILING = "memory/"


async def log_memory_audit_event(
    *,
    action: str,
    user_id: UUID | str | None,
    organization_id: UUID | str | None,
    request,
    memory_scope: str | None = None,
    scope_key: str | None = None,
    project_id: str | None = None,
    source_surface: str | None = None,
    source_ids: list[str] | None = None,
    derived_ids: list[str] | None = None,
    policy_allowed: bool | None = None,
    policy_reason: str | None = None,
    details: Mapping[str, object] | None = None,
) -> None:
    """Record metadata-only memory audit receipts exposed through inspect APIs."""
    bounded_source_ids, source_ids_truncated = _bounded_audit_id_list(source_ids)
    bounded_derived_ids, derived_ids_truncated = _bounded_audit_id_list(derived_ids)
    payload: SurrealRecord = {
        "memory_scope": _bounded_audit_string(memory_scope),
        "scope_key": _bounded_audit_string(scope_key),
        "project_id": _bounded_audit_string(project_id),
        "source_surface": _bounded_audit_string(source_surface),
        "source_ids": bounded_source_ids,
        "derived_ids": bounded_derived_ids,
        "policy_allowed": policy_allowed,
        "policy_reason": _bounded_audit_string(policy_reason),
    }
    if source_ids_truncated:
        payload["source_ids_truncated"] = source_ids_truncated
    if derived_ids_truncated:
        payload["derived_ids_truncated"] = derived_ids_truncated
    if details:
        payload["details"] = _bounded_audit_value(details)

    async with _auth_client_scope() as client:
        await _log_audit_event(
            client,
            action=action,
            user_id=_coerce_optional_uuid(user_id),
            organization_id=_coerce_optional_uuid(organization_id),
            request=request,
            details=payload,
        )


async def request_user_deletion(
    *,
    user_id: UUID,
    organization_id: UUID | None,
    request: Request | None,
) -> UserDeletionRequestResult:
    now = _utcnow()
    purge_after = now + timedelta(days=30)
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        user_record = await repo.select_one(
            "SELECT * FROM users WHERE uuid = $user_id AND deleted_at = NONE LIMIT 1;",
            user_id=str(user_id),
        )
        if user_record is None:
            msg = f"User not found: {user_id}"
            raise UserNotFoundError(msg)

    private_memories_scheduled = await soft_delete_private_raw_captures_for_user(
        user_id=user_id,
        purge_after=purge_after,
    )

    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        user_record = await repo.select_one(
            "SELECT * FROM users WHERE uuid = $user_id AND deleted_at = NONE LIMIT 1;",
            user_id=str(user_id),
        )
        if user_record is None:
            msg = f"User not found: {user_id}"
            raise UserNotFoundError(msg)

        updated_user = {
            **user_record,
            "deleted_at": now,
            "purge_after": purge_after,
            "updated_at": now,
        }
        await repo.replace_record("users", uuid=user_id, record=updated_user)
        api_key_rows = await repo.select_many(
            """
                UPDATE api_keys
                SET revoked_at = $now,
                    updated_at = $now
                WHERE user_id = $user_id
                    AND revoked_at = NONE;
            """,
            user_id=str(user_id),
            now=now,
        )
        session_rows = await repo.select_many(
            """
                UPDATE user_sessions
                SET revoked_at = $now,
                    updated_at = $now
                WHERE user_id = $user_id
                    AND revoked_at = NONE;
            """,
            user_id=str(user_id),
            now=now,
        )
        access_session_cache.invalidate_user(user_id)
        await _log_audit_event(
            client,
            action="auth.user.delete_requested",
            user_id=user_id,
            organization_id=organization_id,
            request=request,
            details={
                "purge_after": purge_after.isoformat(),
                "private_memories_scheduled": private_memories_scheduled,
                "api_keys_revoked": len(api_key_rows),
                "sessions_revoked": len(session_rows),
            },
        )

    await log_memory_audit_event(
        action="memory.delete.personal_scheduled",
        user_id=user_id,
        organization_id=organization_id,
        request=request,
        memory_scope="private",
        scope_key=str(user_id),
        policy_allowed=True,
        policy_reason="user_deletion_requested",
        details={
            "purge_after": purge_after.isoformat(),
            "private_memories_scheduled": private_memories_scheduled,
        },
    )
    return UserDeletionRequestResult(
        user_id=user_id,
        purge_after=purge_after,
        private_memories_scheduled=private_memories_scheduled,
        api_keys_revoked=len(api_key_rows),
        sessions_revoked=len(session_rows),
    )


def _memory_audit_details(row: Mapping[str, object]) -> Mapping[str, object]:
    details = row.get("details")
    if isinstance(details, Mapping):
        return {str(key): value for key, value in details.items()}
    return {}


def _memory_audit_id_matches(details: Mapping[str, object], key: str, value: str | None) -> bool:
    if not value:
        return True
    ids = details.get(key)
    if not isinstance(ids, list):
        return False
    return value in {str(item) for item in ids}


def _memory_audit_row_matches(
    row: Mapping[str, object],
    *,
    action: str | None,
    source_id: str | None,
    derived_id: str | None,
    memory_scope: str | None,
    project_id: str | None,
    policy_allowed: bool | None,
) -> bool:
    action_value = str(row.get("action") or "")
    if not action_value.startswith("memory."):
        return False
    if action and action_value != action:
        return False
    details = _memory_audit_details(row)
    if memory_scope and details.get("memory_scope") != memory_scope:
        return False
    if project_id and details.get("project_id") != project_id:
        return False
    if policy_allowed is not None and details.get("policy_allowed") != policy_allowed:
        return False
    if not _memory_audit_id_matches(details, "source_ids", source_id):
        return False
    return _memory_audit_id_matches(details, "derived_ids", derived_id)


async def list_memory_audit_events(
    *,
    organization_id: UUID | str,
    user_id: UUID | str | None = None,
    action: str | None = None,
    source_id: str | None = None,
    derived_id: str | None = None,
    memory_scope: str | None = None,
    project_id: str | None = None,
    policy_allowed: bool | None = None,
    limit: int = 50,
) -> list[SurrealRecord]:
    if action and not action.startswith(_MEMORY_AUDIT_ACTION_PREFIX):
        return []

    bounded_limit = max(1, min(limit, 200))
    scan_limit = max(100, min(bounded_limit * 5, 500))
    params: SurrealRecord = {
        "organization_id": str(organization_id),
        "scan_limit": scan_limit,
    }
    if user_id:
        params["user_id"] = str(user_id)
    if action:
        params["action"] = action
    else:
        params["memory_action_prefix"] = _MEMORY_AUDIT_ACTION_PREFIX
        params["memory_action_ceiling"] = _MEMORY_AUDIT_ACTION_CEILING

    if user_id and action:
        query = (
            "SELECT * FROM audit_logs "
            "WHERE organization_id = $organization_id "
            "AND user_id = $user_id "
            "AND action = $action "
            "ORDER BY created_at DESC LIMIT $scan_limit;"
        )
    elif user_id:
        query = (
            "SELECT * FROM audit_logs "
            "WHERE organization_id = $organization_id "
            "AND user_id = $user_id "
            "AND action >= $memory_action_prefix "
            "AND action < $memory_action_ceiling "
            "ORDER BY created_at DESC LIMIT $scan_limit;"
        )
    elif action:
        query = (
            "SELECT * FROM audit_logs "
            "WHERE organization_id = $organization_id "
            "AND action = $action "
            "ORDER BY created_at DESC LIMIT $scan_limit;"
        )
    else:
        query = (
            "SELECT * FROM audit_logs "
            "WHERE organization_id = $organization_id "
            "AND action >= $memory_action_prefix "
            "AND action < $memory_action_ceiling "
            "ORDER BY created_at DESC LIMIT $scan_limit;"
        )
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        rows = await repo.select_many(query, **params)

    events: list[SurrealRecord] = []
    for row in rows:
        if _memory_audit_row_matches(
            row,
            action=action,
            source_id=source_id,
            derived_id=derived_id,
            memory_scope=memory_scope,
            project_id=project_id,
            policy_allowed=policy_allowed,
        ):
            events.append(row)
        if len(events) >= bounded_limit:
            break
    return events


async def _generate_unique_project_slug(
    repo: _SurrealRepository,
    *,
    organization_id: UUID,
    name: str,
    exclude_uuid: UUID | None = None,
) -> str:
    import re

    base_slug = re.sub(r"[^a-z0-9\\s-]", "", name.lower())
    base_slug = re.sub(r"[\s_]+", "-", base_slug)
    base_slug = re.sub(r"-+", "-", base_slug).strip("-")[:64] or "project"
    slug = base_slug
    suffix = 1

    while suffix <= 100:
        existing = await repo.select_one(
            "SELECT * FROM projects WHERE organization_id = $organization_id AND slug = $slug LIMIT 1;",
            organization_id=str(organization_id),
            slug=slug,
        )
        existing_uuid = _coerce_optional_uuid(existing.get("uuid")) if existing else None
        if existing is None or existing_uuid == exclude_uuid:
            return slug
        suffix += 1
        slug = f"{base_slug}-{suffix}"

    return f"{base_slug[:55]}-{secrets.token_hex(4)}"


def _project_record_namespace(record: SurrealRecord) -> SimpleNamespace:
    owner_user_id = _coerce_optional_uuid(record.get("owner_user_id"))
    return SimpleNamespace(
        id=_coerce_uuid(record.get("uuid"), field_name="projects.uuid"),
        organization_id=_coerce_uuid(
            record.get("organization_id"), field_name="projects.organization_id"
        ),
        graph_project_id=str(record.get("graph_project_id") or ""),
        name=record.get("name"),
        description=record.get("description"),
        visibility=ProjectVisibility(str(record.get("visibility") or ProjectVisibility.ORG.value)),
        default_role=ProjectRole(str(record.get("default_role") or ProjectRole.VIEWER.value)),
        owner_user_id=owner_user_id,
    )


def _memory_space_state(memory_scope: str, state: str | None = None) -> tuple[str, str | None]:
    if memory_scope not in _MEMORY_SPACE_SCOPES:
        raise HTTPException(status_code=400, detail="invalid_memory_scope")
    if state is not None and state not in {"active", "disabled"}:
        raise HTTPException(status_code=400, detail="invalid_memory_space_state")
    if memory_scope not in _ENABLED_MEMORY_SPACE_SCOPES:
        return "disabled", "scope_not_enabled"
    if state == "disabled":
        return "disabled", "manually_disabled"
    return "active", None


def _memory_space_scope_key(
    *,
    memory_scope: str,
    scope_key: str | None,
    created_by_user_id: UUID,
) -> str | None:
    if memory_scope == "private":
        actor_scope_key = str(created_by_user_id)
        if scope_key and scope_key != actor_scope_key:
            raise HTTPException(status_code=400, detail="private_scope_key_mismatch")
        return actor_scope_key
    if memory_scope in {"delegated", "project", "team", "shared"} and not scope_key:
        raise HTTPException(status_code=400, detail="missing_scope_key")
    return scope_key


def _memory_space_namespace(record: SurrealRecord) -> SimpleNamespace:
    return SimpleNamespace(
        id=_coerce_uuid(record.get("uuid"), field_name="memory_spaces.uuid"),
        organization_id=_coerce_uuid(
            record.get("organization_id"), field_name="memory_spaces.organization_id"
        ),
        memory_scope=str(record.get("memory_scope") or "private"),
        scope_key=_optional_str(record.get("scope_key")),
        name=str(record.get("name") or ""),
        description=_optional_str(record.get("description")),
        state=str(record.get("state") or "active"),
        disabled_reason=_optional_str(record.get("disabled_reason")),
        metadata=_record_payload(record.get("metadata")),
        created_by_user_id=_coerce_uuid(
            record.get("created_by_user_id"),
            field_name="memory_spaces.created_by_user_id",
        ),
        created_at=_coerce_datetime(record.get("created_at")),
        updated_at=_coerce_datetime(record.get("updated_at")),
    )


def _memory_space_member_namespace(record: SurrealRecord) -> SimpleNamespace:
    permissions_value = record.get("permissions", [])
    permissions = (
        [str(item) for item in permissions_value if str(item)]
        if isinstance(permissions_value, list)
        else []
    )
    return SimpleNamespace(
        id=_coerce_uuid(record.get("uuid"), field_name="memory_space_members.uuid"),
        organization_id=_coerce_uuid(
            record.get("organization_id"),
            field_name="memory_space_members.organization_id",
        ),
        space_id=_coerce_uuid(record.get("space_id"), field_name="memory_space_members.space_id"),
        principal_type=str(record.get("principal_type") or "user"),
        principal_id=str(record.get("principal_id") or ""),
        role=str(record.get("role") or "reader"),
        permissions=permissions,
        expires_at=_coerce_datetime(record.get("expires_at")),
        created_by_user_id=_coerce_uuid(
            record.get("created_by_user_id"),
            field_name="memory_space_members.created_by_user_id",
        ),
        created_at=_coerce_datetime(record.get("created_at")),
        updated_at=_coerce_datetime(record.get("updated_at")),
    )


async def _assert_project_space_target(
    *,
    organization_id: UUID,
    memory_scope: str,
    scope_key: str | None,
) -> None:
    if memory_scope != "project" or not scope_key:
        return
    await get_project_record_by_graph_id(
        organization_id=organization_id,
        graph_project_id=scope_key,
    )


async def create_project_record(
    *,
    organization_id: UUID,
    owner_user_id: UUID,
    graph_project_id: str,
    name: str,
    description: str | None = None,
) -> SurrealRecord:
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        existing = await repo.select_one(
            "SELECT * FROM projects "
            "WHERE organization_id = $organization_id AND graph_project_id = $graph_project_id "
            "LIMIT 1;",
            organization_id=str(organization_id),
            graph_project_id=graph_project_id,
        )
        if existing is not None:
            return existing

        now = _utcnow()
        record = {
            "uuid": str(uuid4()),
            "organization_id": str(organization_id),
            "owner_user_id": str(owner_user_id),
            "name": name,
            "slug": await _generate_unique_project_slug(
                repo,
                organization_id=organization_id,
                name=name,
            ),
            "description": description[:2000] if description else None,
            "graph_project_id": graph_project_id,
            "visibility": ProjectVisibility.ORG.value,
            "default_role": ProjectRole.VIEWER.value,
            "settings": {},
            "created_at": now,
            "updated_at": now,
        }
        return await repo.replace_record(
            "projects",
            uuid=_coerce_uuid(record["uuid"], field_name="projects.uuid"),
            record=record,
        )


async def update_project_record(
    *,
    organization_id: UUID,
    graph_project_id: str,
    name: str | None = None,
    description: str | None = None,
) -> bool:
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        existing = await repo.select_one(
            "SELECT * FROM projects "
            "WHERE organization_id = $organization_id AND graph_project_id = $graph_project_id "
            "LIMIT 1;",
            organization_id=str(organization_id),
            graph_project_id=graph_project_id,
        )
        if existing is None:
            return False

        updated = dict(existing)
        project_uuid = _coerce_uuid(existing.get("uuid"), field_name="projects.uuid")
        if name is not None and name != existing.get("name"):
            updated["name"] = name
            updated["slug"] = await _generate_unique_project_slug(
                repo,
                organization_id=organization_id,
                name=name,
                exclude_uuid=project_uuid,
            )
        if description is not None:
            updated["description"] = description[:2000] if description else None
        updated["updated_at"] = _utcnow()
        await repo.replace_record("projects", uuid=project_uuid, record=updated)
        return True


async def delete_project_record(
    *,
    organization_id: UUID,
    graph_project_id: str,
) -> bool:
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        existing = await repo.select_one(
            "SELECT * FROM projects "
            "WHERE organization_id = $organization_id AND graph_project_id = $graph_project_id "
            "LIMIT 1;",
            organization_id=str(organization_id),
            graph_project_id=graph_project_id,
        )
        if existing is None:
            return False

        project_uuid = str(existing["uuid"])
        await _execute_raw_statement_records(
            client,
            """
                DELETE FROM api_key_project_scopes WHERE project_id = $project_id;
                DELETE FROM team_projects WHERE project_id = $project_id;
                DELETE FROM project_members WHERE project_id = $project_id;
                DELETE FROM projects WHERE uuid = $uuid AND organization_id = $organization_id;
            """,
            project_id=project_uuid,
            uuid=project_uuid,
            organization_id=str(organization_id),
        )
        return True


async def get_project_record_by_graph_id(
    *,
    organization_id: UUID,
    graph_project_id: str,
):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        record = await repo.select_one(
            "SELECT * FROM projects "
            "WHERE organization_id = $organization_id AND graph_project_id = $graph_project_id "
            "LIMIT 1;",
            organization_id=str(organization_id),
            graph_project_id=graph_project_id,
        )
        if record is None:
            raise HTTPException(
                status_code=404,
                detail=_project_not_found_detail(graph_project_id),
            )
        return _project_record_namespace(record)


async def get_project_record_by_id(
    *,
    organization_id: UUID,
    project_id: UUID,
):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        record = await repo.select_one(
            "SELECT * FROM projects "
            "WHERE organization_id = $organization_id AND uuid = $project_id "
            "LIMIT 1;",
            organization_id=str(organization_id),
            project_id=str(project_id),
        )
        if record is None:
            raise HTTPException(
                status_code=404,
                detail=_project_not_found_detail(project_id),
            )
        return _project_record_namespace(record)


async def list_memory_spaces(*, organization_id: UUID):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        records = await repo.select_many(
            "SELECT * FROM memory_spaces "
            "WHERE organization_id = $organization_id "
            "ORDER BY created_at ASC;",
            organization_id=str(organization_id),
        )
        return [_memory_space_namespace(record) for record in records]


async def create_memory_space(
    *,
    organization_id: UUID,
    created_by_user_id: UUID,
    memory_scope: str,
    scope_key: str | None = None,
    name: str,
    description: str | None = None,
    metadata: Mapping[str, object] | None = None,
):
    normalized_scope = str(memory_scope)
    normalized_scope_key = _memory_space_scope_key(
        memory_scope=normalized_scope,
        scope_key=scope_key,
        created_by_user_id=created_by_user_id,
    )
    state, disabled_reason = _memory_space_state(normalized_scope)
    await _assert_project_space_target(
        organization_id=organization_id,
        memory_scope=normalized_scope,
        scope_key=normalized_scope_key,
    )
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        existing = await repo.select_one(
            "SELECT * FROM memory_spaces "
            "WHERE organization_id = $organization_id "
            "AND memory_scope = $memory_scope "
            "AND scope_key = $scope_key "
            "LIMIT 1;",
            organization_id=str(organization_id),
            memory_scope=normalized_scope,
            scope_key=normalized_scope_key,
        )
        if existing is not None:
            return _memory_space_namespace(existing)

        now = _utcnow()
        record: SurrealRecord = {
            "uuid": str(uuid4()),
            "organization_id": str(organization_id),
            "memory_scope": normalized_scope,
            "scope_key": normalized_scope_key,
            "name": name[:200],
            "description": description[:2000] if description else None,
            "state": state,
            "disabled_reason": disabled_reason,
            "metadata": dict(metadata or {}),
            "created_by_user_id": str(created_by_user_id),
            "created_at": now,
            "updated_at": now,
        }
        created = await repo.replace_record(
            "memory_spaces",
            uuid=_coerce_uuid(record["uuid"], field_name="memory_spaces.uuid"),
            record=record,
        )
        return _memory_space_namespace(created)


async def get_memory_space(*, organization_id: UUID, space_id: UUID):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        record = await repo.select_one(
            "SELECT * FROM memory_spaces "
            "WHERE organization_id = $organization_id AND uuid = $space_id "
            "LIMIT 1;",
            organization_id=str(organization_id),
            space_id=str(space_id),
        )
        if record is None:
            raise HTTPException(status_code=404, detail="memory_space_not_found")
        return _memory_space_namespace(record)


async def list_memory_space_members(*, organization_id: UUID, space_id: UUID):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        records = await repo.select_many(
            "SELECT * FROM memory_space_members "
            "WHERE organization_id = $organization_id AND space_id = $space_id "
            "ORDER BY created_at ASC;",
            organization_id=str(organization_id),
            space_id=str(space_id),
        )
        return [_memory_space_member_namespace(record) for record in records]


async def update_memory_space(
    *,
    organization_id: UUID,
    space_id: UUID,
    name: str | None = None,
    description: str | None = None,
    state: str | None = None,
    metadata: Mapping[str, object] | None = None,
):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        existing = await repo.select_one(
            "SELECT * FROM memory_spaces "
            "WHERE organization_id = $organization_id AND uuid = $space_id "
            "LIMIT 1;",
            organization_id=str(organization_id),
            space_id=str(space_id),
        )
        if existing is None:
            raise HTTPException(status_code=404, detail="memory_space_not_found")

        updated: SurrealRecord = dict(existing)
        if name is not None:
            updated["name"] = name[:200]
        if description is not None:
            updated["description"] = description[:2000] if description else None
        if metadata is not None:
            updated["metadata"] = dict(metadata)
        memory_scope = str(updated.get("memory_scope") or "private")
        if state is None:
            if memory_scope not in _ENABLED_MEMORY_SPACE_SCOPES:
                next_state, disabled_reason = "disabled", "scope_not_enabled"
            else:
                next_state = str(updated.get("state") or "active")
                disabled_reason = (
                    _optional_str(updated.get("disabled_reason"))
                    if next_state == "disabled"
                    else None
                )
        else:
            next_state, disabled_reason = _memory_space_state(memory_scope, state)
        updated["state"] = next_state
        updated["disabled_reason"] = disabled_reason
        updated["updated_at"] = _utcnow()
        saved = await repo.replace_record(
            "memory_spaces",
            uuid=_coerce_uuid(existing.get("uuid"), field_name="memory_spaces.uuid"),
            record=updated,
        )
        return _memory_space_namespace(saved)


async def add_memory_space_member(
    *,
    organization_id: UUID,
    space_id: UUID,
    created_by_user_id: UUID,
    principal_type: str,
    principal_id: str,
    role: str = "reader",
    permissions: list[str] | None = None,
    expires_at: datetime | None = None,
):
    space = await get_memory_space(organization_id=organization_id, space_id=space_id)
    if space.state == "disabled":
        raise HTTPException(status_code=409, detail=space.disabled_reason or "scope_not_enabled")
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        existing = await repo.select_one(
            "SELECT * FROM memory_space_members "
            "WHERE organization_id = $organization_id "
            "AND space_id = $space_id "
            "AND principal_type = $principal_type "
            "AND principal_id = $principal_id "
            "LIMIT 1;",
            organization_id=str(organization_id),
            space_id=str(space_id),
            principal_type=principal_type,
            principal_id=principal_id,
        )
        now = _utcnow()
        record: SurrealRecord = dict(existing or {})
        record.update(
            {
                "uuid": str(record.get("uuid") or uuid4()),
                "organization_id": str(organization_id),
                "space_id": str(space_id),
                "principal_type": principal_type,
                "principal_id": principal_id,
                "role": role,
                "permissions": list(permissions or []),
                "expires_at": expires_at,
                "created_by_user_id": str(record.get("created_by_user_id") or created_by_user_id),
                "updated_at": now,
            }
        )
        record.setdefault("created_at", now)
        saved = await repo.replace_record(
            "memory_space_members",
            uuid=_coerce_uuid(record["uuid"], field_name="memory_space_members.uuid"),
            record=record,
        )
        return _memory_space_member_namespace(saved)


async def _resolve_api_key_project_record_ids(
    repo: _SurrealRepository,
    *,
    organization_id: UUID,
    project_ids: list[str] | tuple[str, ...] | None,
) -> list[str]:
    normalized = _unique_strings(project_ids)
    if not normalized:
        return []
    records = await repo.select_many(
        "SELECT uuid, graph_project_id FROM projects "
        "WHERE organization_id = $organization_id AND graph_project_id IN $project_ids "
        "ORDER BY created_at ASC;",
        organization_id=str(organization_id),
        project_ids=normalized,
    )
    by_graph_id = {str(record.get("graph_project_id")): record for record in records}
    missing = [project_id for project_id in normalized if project_id not in by_graph_id]
    if missing:
        raise HTTPException(status_code=400, detail="invalid_api_key_project_scope")
    return [str(by_graph_id[project_id]["uuid"]) for project_id in normalized]


async def _resolve_api_key_memory_space_ids(
    repo: _SurrealRepository,
    *,
    organization_id: UUID,
    memory_space_ids: list[UUID] | tuple[UUID, ...] | None,
) -> list[UUID]:
    normalized = _unique_uuids(memory_space_ids)
    if not normalized:
        return []
    records = await repo.select_many(
        "SELECT uuid FROM memory_spaces "
        "WHERE organization_id = $organization_id AND uuid IN $memory_space_ids "
        "ORDER BY created_at ASC;",
        organization_id=str(organization_id),
        memory_space_ids=[str(memory_space_id) for memory_space_id in normalized],
    )
    found = {
        _coerce_uuid(record.get("uuid"), field_name="memory_spaces.uuid") for record in records
    }
    missing = [memory_space_id for memory_space_id in normalized if memory_space_id not in found]
    if missing:
        raise HTTPException(status_code=400, detail="invalid_api_key_memory_space_scope")
    return normalized


async def _write_api_key_scope_records(
    client: QueryClient,
    *,
    api_key_id: UUID,
    project_record_ids: list[str],
    memory_space_ids: list[UUID],
) -> None:
    now = _utcnow()
    for project_id in project_record_ids:
        await client.execute_query(
            "CREATE api_key_project_scopes CONTENT $record;",
            record={
                "uuid": str(uuid4()),
                "api_key_id": str(api_key_id),
                "project_id": project_id,
                "allowed_operations": [],
                "created_at": now,
                "updated_at": now,
            },
        )
    for memory_space_id in memory_space_ids:
        await client.execute_query(
            "CREATE api_key_memory_space_scopes CONTENT $record;",
            record={
                "uuid": str(uuid4()),
                "api_key_id": str(api_key_id),
                "memory_space_id": str(memory_space_id),
                "allowed_operations": [],
                "created_at": now,
                "updated_at": now,
            },
        )


async def _decorate_api_key_scopes(
    repo: _SurrealRepository,
    keys: list[SimpleNamespace],
) -> list[SimpleNamespace]:
    api_key_ids = [str(key.id) for key in keys if getattr(key, "id", None) is not None]
    if not api_key_ids:
        return keys
    project_scope_records = await repo.select_many(
        "SELECT * FROM api_key_project_scopes "
        "WHERE api_key_id IN $api_key_ids ORDER BY created_at ASC;",
        api_key_ids=api_key_ids,
    )
    project_record_ids = [
        str(record["project_id"])
        for record in project_scope_records
        if str(record.get("project_id") or "").strip()
    ]
    project_records = (
        await repo.select_many(
            "SELECT uuid, graph_project_id FROM projects WHERE uuid IN $project_ids;",
            project_ids=project_record_ids,
        )
        if project_record_ids
        else []
    )
    graph_ids_by_uuid = {
        str(record["uuid"]): str(record["graph_project_id"])
        for record in project_records
        if str(record.get("uuid") or "").strip()
        and str(record.get("graph_project_id") or "").strip()
    }
    memory_scope_records = await repo.select_many(
        "SELECT * FROM api_key_memory_space_scopes "
        "WHERE api_key_id IN $api_key_ids ORDER BY created_at ASC;",
        api_key_ids=api_key_ids,
    )
    projects_by_key: dict[str, list[str]] = {}
    for record in project_scope_records:
        graph_id = graph_ids_by_uuid.get(str(record.get("project_id") or ""))
        if graph_id:
            projects_by_key.setdefault(str(record["api_key_id"]), []).append(graph_id)
    memory_spaces_by_key: dict[str, list[str]] = {}
    for record in memory_scope_records:
        memory_space_id = str(record.get("memory_space_id") or "").strip()
        if memory_space_id:
            memory_spaces_by_key.setdefault(str(record["api_key_id"]), []).append(memory_space_id)
    for key in keys:
        key_id = str(key.id)
        key.project_ids = projects_by_key.get(key_id, [])
        key.memory_space_ids = memory_spaces_by_key.get(key_id, [])
    return keys


async def list_api_keys_for_user(*, organization_id: UUID, user_id: UUID):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        records = await repo.select_many(
            "SELECT * FROM api_keys "
            "WHERE organization_id = $organization_id AND user_id = $user_id "
            "ORDER BY created_at DESC;",
            organization_id=str(organization_id),
            user_id=str(user_id),
        )
        keys = [key for record in records if (key := _api_key_namespace(record)) is not None]
        return await _decorate_api_key_scopes(repo, keys)


async def create_api_key_for_user(
    *,
    organization_id: UUID,
    user_id: UUID,
    name: str,
    live: bool,
    scopes: list[str],
    project_ids: list[str] | None = None,
    memory_space_ids: list[UUID] | None = None,
    expires_at,
    request,
):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        project_record_ids = await _resolve_api_key_project_record_ids(
            repo,
            organization_id=organization_id,
            project_ids=project_ids,
        )
        resolved_memory_space_ids = await _resolve_api_key_memory_space_ids(
            repo,
            organization_id=organization_id,
            memory_space_ids=memory_space_ids,
        )
        raw = generate_api_key(live=live)
        salt_hex, hash_hex = hash_api_key(raw)
        now = _utcnow()
        record = {
            "uuid": str(uuid4()),
            "organization_id": str(organization_id),
            "user_id": str(user_id),
            "name": name,
            "key_prefix": api_key_prefix(raw),
            "key_salt": salt_hex,
            "key_hash": hash_hex,
            "scopes": [scope.strip() for scope in scopes if str(scope).strip()],
            "expires_at": _coerce_datetime(expires_at),
            "revoked_at": None,
            "last_used_at": None,
            "created_at": now,
            "updated_at": now,
        }
        created = _normalize_records(
            await client.execute_query("CREATE api_keys CONTENT $record;", record=record)
        )
        if not created:
            msg = "Failed to create API key"
            raise RuntimeError(msg)
        key = _api_key_namespace(created[0])
        if key is None:
            msg = "Failed to materialize API key record"
            raise RuntimeError(msg)
        await _write_api_key_scope_records(
            client,
            api_key_id=key.id,
            project_record_ids=project_record_ids,
            memory_space_ids=resolved_memory_space_ids,
        )
        key.project_ids = list(project_ids or [])
        key.memory_space_ids = [
            str(memory_space_id) for memory_space_id in resolved_memory_space_ids
        ]
        await _log_audit_event(
            client,
            action="auth.api_key.create",
            user_id=user_id,
            organization_id=organization_id,
            request=request,
            details={
                "api_key_id": str(key.id),
                "memory_space_scope_count": len(resolved_memory_space_ids),
                "name": key.name,
                "prefix": key.key_prefix,
                "project_scope_count": len(project_record_ids),
            },
        )
        return key, raw


async def revoke_api_key_for_user(
    *,
    api_key_id: UUID,
    organization_id: UUID,
    actor_user_id: UUID,
    actor_org_role,
    request,
) -> None:
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        record = await repo.select_one(
            "SELECT * FROM api_keys WHERE uuid = $uuid LIMIT 1;",
            uuid=str(api_key_id),
        )
        if (
            record is None
            or _coerce_optional_uuid(record.get("organization_id")) != organization_id
        ):
            raise HTTPException(status_code=404, detail="API key not found")
        if (
            _coerce_optional_uuid(record.get("user_id")) != actor_user_id
            and _role_value(actor_org_role) not in _ORG_ADMIN_ROLE_VALUES
        ):
            raise HTTPException(status_code=403, detail="Forbidden")
        updated = {**record, "revoked_at": _utcnow(), "updated_at": _utcnow()}
        await repo.replace_record("api_keys", uuid=api_key_id, record=updated)
        await _log_audit_event(
            client,
            action="auth.api_key.revoke",
            user_id=actor_user_id,
            organization_id=organization_id,
            request=request,
            details={"api_key_id": str(api_key_id)},
        )


async def update_auth_user(
    *,
    user_id: UUID,
    email: str | None,
    name: str | None,
    avatar_url: str | None,
    current_password: str | None,
    new_password: str | None,
    organization_id: UUID | None,
    request,
):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        normalized_email = email.strip().lower() if email is not None else None
        user, email_owner = await _load_user_update_records(
            client,
            user_id=user_id,
            email=normalized_email,
        )
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        changes: list[str] = []
        updated = dict(user)
        if email is not None:
            if (
                email_owner is not None
                and _coerce_optional_uuid(email_owner.get("uuid")) != user_id
            ):
                raise HTTPException(status_code=400, detail="Email is already in use")
            updated["email"] = normalized_email
            changes.append("email")
        if name is not None:
            normalized_name = name.strip()
            if not normalized_name:
                raise HTTPException(status_code=400, detail="Name is required")
            updated["name"] = normalized_name
            changes.append("name")
        if avatar_url is not None:
            updated["avatar_url"] = avatar_url.strip() or None
            changes.append("avatar_url")
        if new_password is not None:
            updated = _apply_password_change(
                updated,
                current_password=current_password,
                new_password=new_password,
            )
            changes.append("password")
        if not changes:
            raise HTTPException(status_code=400, detail="No fields to update")
        updated["updated_at"] = _utcnow()
        written = await repo.replace_record("users", uuid=user_id, record=updated)
        if any(change != "password" for change in changes):
            await _log_audit_event(
                client,
                action="user.update_profile",
                user_id=user_id,
                organization_id=organization_id,
                request=request,
                details={"fields": [change for change in changes if change != "password"]},
            )
        if "password" in changes:
            await _log_audit_event(
                client,
                action="user.change_password",
                user_id=user_id,
                organization_id=organization_id,
                request=request,
                details={},
            )
        return _auth_user_namespace(written)


async def patch_auth_user(
    *,
    user_id: UUID,
    updates: SurrealRecord,
    organization_id: UUID | None,
    request,
):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        normalized_email: str | None = None
        if "email" in updates:
            email = updates["email"]
            normalized_email = str(email).strip().lower() if email is not None else ""
            if not normalized_email:
                raise HTTPException(status_code=400, detail="Email is required")

        user, email_owner = await _load_user_update_records(
            client,
            user_id=user_id,
            email=normalized_email,
        )
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        updated = dict(user)
        changes: list[str] = []

        if "email" in updates:
            if (
                email_owner is not None
                and _coerce_optional_uuid(email_owner.get("uuid")) != user_id
            ):
                raise HTTPException(status_code=400, detail="Email is already in use")
            updated["email"] = normalized_email
            changes.append("email")
        if "name" in updates:
            name = str(updates["name"] or "").strip()
            if not name:
                raise HTTPException(status_code=400, detail="Name is required")
            updated["name"] = name
            changes.append("name")
        if "avatar_url" in updates:
            avatar_url = updates["avatar_url"]
            updated["avatar_url"] = (
                str(avatar_url).strip() or None if avatar_url is not None else None
            )
            changes.append("avatar_url")
        if "bio" in updates:
            bio = updates["bio"]
            updated["bio"] = str(bio).strip() or None if bio is not None else None
            changes.append("bio")
        if "timezone" in updates:
            timezone = updates["timezone"]
            updated["timezone"] = str(timezone).strip() or "UTC" if timezone is not None else "UTC"
            changes.append("timezone")
        if "preferences" in updates:
            preferences = updates["preferences"]
            if not isinstance(preferences, dict):
                raise HTTPException(status_code=400, detail="Preferences must be an object")
            updated["preferences"] = dict(preferences)
            changes.append("preferences")
        if not changes:
            raise HTTPException(status_code=400, detail="No fields to update")

        updated["updated_at"] = _utcnow()
        written = await repo.replace_record("users", uuid=user_id, record=updated)
        await _log_audit_event(
            client,
            action="user.update_profile",
            user_id=user_id,
            organization_id=organization_id,
            request=request,
            details={"fields": changes},
        )
        return _auth_user_namespace(written)


async def list_user_sessions(
    *,
    user_id: UUID,
    include_expired: bool = False,
) -> list[SimpleNamespace]:
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        params: SurrealRecord = {"user_id": str(user_id)}
        query = "SELECT * FROM user_sessions WHERE user_id = $user_id AND revoked_at = NONE"
        if not include_expired:
            params["now"] = _utcnow()
            query += " AND expires_at > $now"
        query += " ORDER BY last_active_at DESC;"
        rows = await repo.select_many(query, **params)
        sessions: list[SimpleNamespace] = []
        for row in rows:
            session = _session_namespace(row)
            if session is not None:
                sessions.append(session)
        return sessions


async def revoke_all_user_sessions(
    *,
    user_id: UUID,
    exclude_token_hash: str | None = None,
) -> int:
    async with _auth_client_scope() as client:
        sessions = SurrealSessionRepository.from_client(client)
        return await sessions.revoke_all_sessions(user_id, exclude_token_hash=exclude_token_hash)


async def revoke_user_session(
    *,
    user_id: UUID,
    session_id: UUID,
) -> bool:
    async with _auth_client_scope() as client:
        sessions = SurrealSessionRepository.from_client(client)
        return await sessions.revoke_session(session_id, user_id)


async def request_password_reset(email: str) -> None:
    normalized_email = email.strip().lower()
    if not normalized_email:
        return

    async with _auth_client_scope() as client:
        payload = await client.execute_query(
            """
                RETURN {
                    user: (
                        SELECT * FROM users
                        WHERE email = $email AND deleted_at = NONE
                        LIMIT 1
                    )[0],
                    tokens: (
                        SELECT * FROM password_reset_tokens
                        WHERE user_id IN (
                            SELECT VALUE uuid FROM users
                            WHERE email = $email AND deleted_at = NONE
                            LIMIT 1
                        )
                        ORDER BY created_at DESC
                    ),
                };
            """,
            email=normalized_email,
        )
        payload = _record_payload(payload)
        user = _normalize_record(payload.get("user"))
        if user is None:
            await _log_login_history(
                client,
                user_id=None,
                event_type="password_reset_request",
                success=False,
                failure_reason="user_not_found",
                email_attempted=normalized_email,
            )
            return

        now = _utcnow()
        rate_limit_cutoff = now - timedelta(minutes=2)
        existing_tokens = _normalize_records(payload.get("tokens"))
        for token_record in existing_tokens:
            created_at = _coerce_datetime(token_record.get("created_at"))
            if (
                created_at is not None
                and created_at > rate_limit_cutoff
                and token_record.get("revoked_at") is None
            ):
                await _log_login_history(
                    client,
                    user_id=_coerce_uuid(user.get("uuid"), field_name="user.uuid"),
                    event_type="password_reset_request",
                    success=False,
                    failure_reason="rate_limited",
                    email_attempted=normalized_email,
                )
                return
        await client.execute_query(
            "UPDATE password_reset_tokens SET revoked_at = $revoked_at "
            "WHERE user_id = $user_id AND used_at = NONE AND revoked_at = NONE;",
            user_id=str(user["uuid"]),
            revoked_at=now,
        )

        raw_token = _generate_reset_token()
        expires_at = now + timedelta(minutes=60)
        token_record = {
            "uuid": str(uuid4()),
            "user_id": str(user["uuid"]),
            "token_hash": _hash_reset_token(raw_token),
            "expires_at": expires_at,
            "used_at": None,
            "revoked_at": None,
            "ip_address": None,
            "user_agent": None,
            "created_at": now,
        }
        await client.execute_query(
            "CREATE password_reset_tokens CONTENT $record;", record=token_record
        )

        from sibyl.config import settings as app_settings

        reset_url = f"{app_settings.frontend_url.rstrip('/')}/reset-password?token={raw_token}"
        template = PasswordResetEmail(
            reset_url=reset_url,
            user_name=str(user.get("name") or "") or None,
            expires_in_minutes=60,
        )
        await get_email_client().send_template(
            template, to=str(user.get("email") or normalized_email)
        )
        await _log_login_history(
            client,
            user_id=_coerce_uuid(user.get("uuid"), field_name="user.uuid"),
            event_type="password_reset_request",
            success=True,
            email_attempted=normalized_email,
        )


async def confirm_password_reset(token: str, new_password: str) -> None:
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        payload = await client.execute_query(
            """
                RETURN {
                    token: (
                        SELECT * FROM password_reset_tokens
                        WHERE token_hash = $token_hash
                        LIMIT 1
                    )[0],
                    user: (
                        SELECT * FROM users
                        WHERE uuid IN (
                            SELECT VALUE user_id FROM password_reset_tokens
                            WHERE token_hash = $token_hash
                            LIMIT 1
                        )
                        LIMIT 1
                    )[0],
                };
            """,
            token_hash=_hash_reset_token(token),
        )
        payload = _record_payload(payload)
        token_record = _normalize_record(payload.get("token"))
        reset_token = _password_reset_namespace(token_record)
        if reset_token is None:
            await _log_login_history(
                client,
                user_id=None,
                event_type="password_reset_confirm",
                success=False,
                failure_reason="token_not_found",
            )
            raise HTTPException(status_code=400, detail="Invalid or expired reset link")
        now = _utcnow()
        if reset_token.used_at is not None:
            raise HTTPException(status_code=400, detail="This reset link has already been used")
        if reset_token.revoked_at is not None:
            raise HTTPException(status_code=400, detail="This reset link has been revoked")
        if reset_token.expires_at is None or reset_token.expires_at < now:
            raise HTTPException(status_code=400, detail="This reset link has expired")

        user = _normalize_record(payload.get("user"))
        if user is None:
            raise HTTPException(status_code=400, detail="User not found")

        password_state = hash_password(new_password)
        updated_user = {
            **user,
            "password_salt": password_state.salt_hex,
            "password_hash": password_state.hash_hex,
            "password_iterations": password_state.iterations,
            "updated_at": now,
        }
        await repo.replace_record("users", uuid=reset_token.user_id, record=updated_user)

        updated_token = {**token_record, "used_at": now}
        await repo.replace_record(
            "password_reset_tokens",
            uuid=reset_token.id,
            record=updated_token,
        )
        await _log_login_history(
            client,
            user_id=reset_token.user_id,
            event_type="password_reset_confirm",
            success=True,
        )


async def list_oauth_connections(*, user_id: UUID) -> list[SimpleNamespace]:
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        rows = await repo.select_many(
            "SELECT * FROM oauth_connections WHERE user_id = $user_id ORDER BY created_at ASC;",
            user_id=str(user_id),
        )
        return [row for record in rows if (row := _oauth_connection_namespace(record)) is not None]


async def remove_oauth_connection(
    *,
    user_id: UUID,
    connection_id: UUID,
):
    async with _auth_client_scope() as client:
        payload = await client.execute_query(
            """
                RETURN {
                    connection: (
                        SELECT * FROM oauth_connections
                        WHERE uuid = $connection_id AND user_id = $user_id
                        LIMIT 1
                    )[0],
                    user: (SELECT * FROM users WHERE uuid = $user_id LIMIT 1)[0],
                    connections: (
                        SELECT * FROM oauth_connections
                        WHERE user_id = $user_id
                        ORDER BY created_at ASC
                    ),
                };
            """,
            connection_id=str(connection_id),
            user_id=str(user_id),
        )
        payload = _record_payload(payload)
        connection = _oauth_connection_namespace(_normalize_record(payload.get("connection")))
        if connection is None:
            raise HTTPException(status_code=404, detail="Connection not found")

        user = _normalize_record(payload.get("user"))
        remaining_connections = _normalize_records(payload.get("connections"))
        has_other_connections = any(
            str(row.get("uuid")) != str(connection_id) for row in remaining_connections
        )
        has_password = bool(user and user.get("password_hash"))
        if not has_other_connections and not has_password:
            raise HTTPException(
                status_code=400,
                detail="Cannot remove last login method. Set a password first.",
            )

        await client.execute_query(
            "DELETE FROM oauth_connections WHERE uuid = $uuid;",
            uuid=str(connection_id),
        )
        return connection


async def has_owner_membership(*, org_id: str, user_id: str | None) -> bool:
    if user_id is None:
        return False
    async with _auth_client_scope() as client:
        records = _normalize_records(
            await client.execute_query(
                """
                    SELECT role FROM organization_members
                    WHERE organization_id = $organization_id AND user_id = $user_id
                    LIMIT 1;
                """,
                organization_id=str(UUID(org_id)),
                user_id=str(UUID(user_id)),
            )
        )
        return bool(records) and _role_value(records[0].get("role")) == "owner"


async def list_accessible_project_graph_ids(ctx) -> set[str]:
    if ctx.organization is None:
        return set()
    async with _auth_client_scope() as client:
        org_id = str(ctx.organization.id)
        org_role = _role_value(ctx.org_role)
        user_id = str(ctx.user.id)
        payload: SurrealRecord = {}
        if org_role in _ORG_ADMIN_ROLE_VALUES:
            project_records = _normalize_records(
                await client.execute_query(
                    """
                        SELECT graph_project_id, created_at FROM projects
                        WHERE organization_id = $organization_id
                        ORDER BY created_at ASC;
                    """,
                    organization_id=org_id,
                )
            )
        else:
            raw_payload = await client.execute_query(
                """
                    RETURN {
                        projects: (
                            SELECT * FROM projects
                            WHERE organization_id = $organization_id
                            ORDER BY created_at ASC
                        ),
                        direct_memberships: (
                            SELECT * FROM project_members
                            WHERE organization_id = $organization_id AND user_id = $user_id
                            ORDER BY created_at ASC
                        ),
                        team_members: (
                            SELECT * FROM team_members
                            WHERE user_id = $user_id
                            ORDER BY created_at ASC
                        ),
                        team_projects: (
                            SELECT * FROM team_projects
                            WHERE team_id IN (
                                SELECT VALUE team_id FROM team_members WHERE user_id = $user_id
                            )
                            ORDER BY created_at ASC
                        ),
                    };
                """,
                organization_id=org_id,
                user_id=user_id,
            )
            payload = _record_payload(raw_payload)
            project_records = _normalize_records(payload.get("projects"))
        if not project_records:
            return set()
        if org_role in _ORG_ADMIN_ROLE_VALUES:
            accessible = {
                str(record["graph_project_id"])
                for record in project_records
                if str(record.get("graph_project_id") or "").strip()
            }
            api_key_allowed = getattr(ctx, "api_key_project_ids", None)
            if api_key_allowed is not None:
                return accessible & {str(project_id) for project_id in api_key_allowed}
            return accessible
        accessible: set[str] = set()
        org_visible = {
            str(record["uuid"]): str(record["graph_project_id"])
            for record in project_records
            if record.get("visibility") == ProjectVisibility.ORG.value
            and str(record.get("graph_project_id") or "").strip()
        }
        accessible.update(org_visible.values())
        direct_memberships = _normalize_records(payload.get("direct_memberships"))
        direct_project_ids = {
            str(record["project_id"])
            for record in direct_memberships
            if str(record.get("project_id") or "").strip()
        }
        accessible.update(
            str(record["graph_project_id"])
            for record in project_records
            if str(record.get("uuid")) in direct_project_ids
            and str(record.get("graph_project_id") or "").strip()
        )
        team_projects = _normalize_records(payload.get("team_projects"))
        granted_project_ids = {
            str(record["project_id"])
            for record in team_projects
            if str(record.get("project_id") or "").strip()
        }
        accessible.update(
            str(record["graph_project_id"])
            for record in project_records
            if str(record.get("uuid")) in granted_project_ids
            and str(record.get("graph_project_id") or "").strip()
        )
        api_key_allowed = getattr(ctx, "api_key_project_ids", None)
        if api_key_allowed is not None:
            return accessible & {str(project_id) for project_id in api_key_allowed}
        return accessible


async def resolve_accessible_project_graph_ids(
    *,
    user_id: str,
    org_id: str,
    scopes=None,
    api_key_project_ids=None,
) -> set[str] | None:
    try:
        auth_ctx = await _resolve_auth_context_from_claims(
            {"sub": user_id, "org": org_id, "scopes": list(scopes or [])}
        )
    except Exception:
        return set()
    if auth_ctx.organization is None:
        return set()
    user_accessible = await list_accessible_project_graph_ids(auth_ctx)
    if api_key_project_ids is not None:
        api_key_allowed = {str(project_id) for project_id in api_key_project_ids}
        if user_accessible is None:
            return api_key_allowed
        return user_accessible & api_key_allowed
    return user_accessible


async def verify_entity_project_access(
    *,
    ctx,
    entity_project_id: str | None,
    required_role: ProjectRole,
    require_existing_project: bool = False,
):
    if ctx.organization is None:
        from sibyl.auth.authorization import ProjectAuthorizationError

        raise ProjectAuthorizationError(
            project_id=entity_project_id or "unknown",
            required_role=required_role,
            actual_role=None,
        )
    if entity_project_id is None:
        if _role_value(ctx.org_role) in _ORG_ADMIN_ROLE_VALUES:
            return ProjectRole.OWNER
        if ctx.org_role is not None and required_role == ProjectRole.VIEWER:
            return ProjectRole.VIEWER
        from sibyl.auth.authorization import ProjectAuthorizationError

        raise ProjectAuthorizationError(
            project_id="unassigned",
            required_role=required_role,
            actual_role=ProjectRole.VIEWER if ctx.org_role else None,
        )
    if _role_value(ctx.org_role) in _ORG_ADMIN_ROLE_VALUES and not require_existing_project:
        return ProjectRole.OWNER
    async with _auth_client_scope() as client:
        payload = await client.execute_query(
            """
                RETURN {
                    project: (
                        SELECT * FROM projects
                        WHERE organization_id = $organization_id
                            AND graph_project_id = $graph_project_id
                        LIMIT 1
                    )[0],
                    direct_membership: (
                        SELECT * FROM project_members
                        WHERE user_id = $user_id
                            AND project_id IN (
                                SELECT VALUE uuid FROM projects
                                WHERE organization_id = $organization_id
                                    AND graph_project_id = $graph_project_id
                                LIMIT 1
                            )
                        LIMIT 1
                    )[0],
                    team_projects: (
                        SELECT * FROM team_projects
                        WHERE project_id IN (
                                SELECT VALUE uuid FROM projects
                                WHERE organization_id = $organization_id
                                    AND graph_project_id = $graph_project_id
                                LIMIT 1
                            )
                            AND team_id IN (
                                SELECT VALUE team_id FROM team_members WHERE user_id = $user_id
                            )
                        LIMIT 10
                    ),
                };
            """,
            organization_id=str(ctx.organization.id),
            graph_project_id=entity_project_id,
            user_id=str(ctx.user.id),
        )
        payload = _record_payload(payload)
        record = _normalize_record(payload.get("project"))
        if record is None:
            if require_existing_project:
                raise HTTPException(
                    status_code=404,
                    detail=_project_not_found_detail(entity_project_id),
                )
            if _role_value(ctx.org_role) in _ORG_ADMIN_ROLE_VALUES:
                return ProjectRole.OWNER
            if ctx.org_role is not None and required_role == ProjectRole.VIEWER:
                return ProjectRole.VIEWER
            from sibyl.auth.authorization import ProjectAuthorizationError

            raise ProjectAuthorizationError(
                project_id=entity_project_id,
                required_role=required_role,
                actual_role=ProjectRole.VIEWER if ctx.org_role else None,
            )
        effective_role = _effective_project_role_from_records(
            ctx=ctx,
            project=record,
            direct_record=_normalize_record(payload.get("direct_membership")),
            team_project_records=_normalize_records(payload.get("team_projects")),
        )
        if effective_role is None:
            from sibyl.auth.authorization import ProjectAuthorizationError

            raise ProjectAuthorizationError(
                project_id=entity_project_id,
                required_role=required_role,
                actual_role=None,
            )
        if _PROJECT_ROLE_LEVELS[effective_role] < _PROJECT_ROLE_LEVELS[required_role]:
            from sibyl.auth.authorization import ProjectAuthorizationError

            raise ProjectAuthorizationError(
                project_id=entity_project_id,
                required_role=required_role,
                actual_role=effective_role,
            )
        return effective_role


def _effective_project_role_from_records(
    *,
    ctx,
    project: SurrealRecord,
    direct_record: SurrealRecord | None,
    team_project_records: list[SurrealRecord],
) -> ProjectRole | None:
    if _role_value(ctx.org_role) in _ORG_ADMIN_ROLE_VALUES:
        return ProjectRole.OWNER
    if _coerce_optional_uuid(project.get("owner_user_id")) == ctx.user.id:
        return ProjectRole.OWNER
    roles: list[ProjectRole] = []
    direct_role = _coerce_project_role(direct_record.get("role")) if direct_record else None
    if direct_role is not None:
        roles.append(direct_role)
    for team_project in team_project_records:
        team_role = _coerce_project_role(team_project.get("role"))
        if team_role is not None:
            roles.append(team_role)
    if project.get("visibility") == ProjectVisibility.ORG.value:
        visibility_role = _coerce_project_role(project.get("default_role"))
        if visibility_role is not None:
            roles.append(visibility_role)
    if not roles:
        return None
    return max(roles, key=lambda role: _PROJECT_ROLE_LEVELS[role])


def _coerce_project_role(value: object | None) -> ProjectRole | None:
    if value is None:
        return None
    raw = _role_value(value)
    if raw is None:
        return None
    return ProjectRole(raw)


__all__ = [
    "AuthContextResolver",
    "DeviceBrowserLogin",
    "IssuedAuthSession",
    "IssuedOidcSession",
    "OrganizationMembershipRepository",
    "OrganizationRepository",
    "RefreshRotation",
    "SessionRepository",
    "SurrealAuthContextResolver",
    "SurrealOrganizationMembershipRepository",
    "SurrealOrganizationRepository",
    "SurrealSessionRepository",
    "SurrealUserRepository",
    "UserDeletionRequestResult",
    "UserRepository",
    "approve_device_authorization",
    "authenticate_api_key",
    "authenticate_local_user",
    "build_surreal_auth_client",
    "confirm_password_reset",
    "add_memory_space_member",
    "create_api_key_for_user",
    "create_memory_space",
    "create_project_record",
    "create_session_record",
    "delete_project_record",
    "delete_failed_local_signup_user",
    "deny_device_authorization",
    "ensure_personal_organization",
    "exchange_device_code",
    "get_device_request_by_user_code",
    "get_memory_space",
    "get_project_record_by_graph_id",
    "get_project_record_by_id",
    "get_user_by_id",
    "has_owner_membership",
    "list_accessible_project_graph_ids",
    "list_audit_events",
    "list_api_keys_for_user",
    "list_memory_audit_events",
    "list_memory_space_members",
    "list_memory_spaces",
    "list_oauth_connections",
    "list_user_organizations",
    "list_user_sessions",
    "load_oauth_client_registration",
    "load_refresh_session_record",
    "log_audit_event",
    "log_memory_audit_event",
    "login_device_browser_user",
    "login_github_identity",
    "login_local_user",
    "login_oidc_identity",
    "patch_auth_user",
    "remove_oauth_connection",
    "request_user_deletion",
    "request_password_reset",
    "resolve_accessible_project_graph_ids",
    "resolve_auth_context",
    "resolve_request_claims",
    "resolve_request_user",
    "revoke_access_session",
    "revoke_all_user_sessions",
    "revoke_api_key_for_user",
    "revoke_refresh_session_record",
    "revoke_user_session",
    "rotate_refresh_exchange",
    "rotate_refresh_session_record",
    "signup_local_user",
    "save_oauth_client_registration",
    "start_device_authorization",
    "update_auth_user",
    "update_memory_space",
    "update_project_record",
    "validate_access_session",
    "verify_entity_project_access",
]


AuthContextResolver = SurrealAuthContextResolver
OrganizationMembershipRepository = SurrealOrganizationMembershipRepository
OrganizationRepository = SurrealOrganizationRepository
SessionRepository = SurrealSessionRepository
UserRepository = SurrealUserRepository
