"""Surreal-backed request-time auth adapters."""

from __future__ import annotations

import hashlib
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self
from uuid import UUID, uuid4

from fastapi import HTTPException

from sibyl import config as config_module
from sibyl.auth.api_key_common import (
    ApiKeyAuth,
    api_key_prefix,
    generate_api_key,
    hash_api_key,
    verify_api_key,
)
from sibyl.auth.http import select_access_token
from sibyl.auth.jwt import JwtError, create_access_token, create_refresh_token, verify_access_token
from sibyl.auth.passwords import hash_password, verify_password
from sibyl.auth.primitives import (
    DeviceTokenError,
    generate_device_code,
    generate_user_code,
    hash_device_code,
)
from sibyl.db.models import ProjectRole, ProjectVisibility
from sibyl.email import PasswordResetEmail, get_email_client
from sibyl.persistence.surreal.auth import (
    SurrealAuthContextResolver,
    SurrealOrganizationMembershipRepository,
    SurrealOrganizationRepository,
    SurrealUserRepository,
    build_surreal_auth_client,
)
from sibyl_core.auth import AuthSession, OrganizationRole, PasswordChange

_ORG_ADMIN_ROLE_VALUES = {"owner", "admin"}
_PROJECT_ROLE_LEVELS: dict[ProjectRole, int] = {
    ProjectRole.VIEWER: 10,
    ProjectRole.CONTRIBUTOR: 20,
    ProjectRole.MAINTAINER: 30,
    ProjectRole.OWNER: 40,
}
_USER_UUID_FIELDS = {"id", "github_id", "created_by_user_id", "accepted_by_user_id"}
_USER_DATETIME_FIELDS = {"created_at", "updated_at", "email_verified_at", "last_login_at"}
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
_DELETE_QUERY_BY_TABLE = {
    "api_keys": "DELETE FROM api_keys WHERE uuid = $uuid;",
    "device_authorization_requests": "DELETE FROM device_authorization_requests WHERE uuid = $uuid;",
    "oauth_connections": "DELETE FROM oauth_connections WHERE uuid = $uuid;",
    "password_reset_tokens": "DELETE FROM password_reset_tokens WHERE uuid = $uuid;",
    "projects": "DELETE FROM projects WHERE uuid = $uuid;",
    "user_sessions": "DELETE FROM user_sessions WHERE uuid = $uuid;",
    "users": "DELETE FROM users WHERE uuid = $uuid;",
}
_CREATE_QUERY_BY_TABLE = {
    "api_keys": "CREATE api_keys CONTENT $record;",
    "device_authorization_requests": "CREATE device_authorization_requests CONTENT $record;",
    "login_history": "CREATE login_history CONTENT $record;",
    "oauth_connections": "CREATE oauth_connections CONTENT $record;",
    "password_reset_tokens": "CREATE password_reset_tokens CONTENT $record;",
    "projects": "CREATE projects CONTENT $record;",
    "user_sessions": "CREATE user_sessions CONTENT $record;",
    "users": "CREATE users CONTENT $record;",
}


@dataclass(frozen=True, slots=True)
class LegacyIssuedAuthSession:
    user: SimpleNamespace
    organization: SimpleNamespace
    access_token: str
    refresh_token: str
    refresh_expires: datetime


@dataclass(frozen=True, slots=True)
class LegacyDeviceBrowserLogin:
    user: SimpleNamespace
    organization: SimpleNamespace
    access_token: str


@dataclass(frozen=True, slots=True)
class LegacyRefreshRotation:
    session_id: UUID
    access_token: str
    refresh_token: str
    refresh_expires: datetime
    user_id: UUID
    organization_id: UUID | None


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _normalize_record(record: Any) -> dict[str, Any] | None:
    if record is None or not isinstance(record, dict):
        return None
    out = dict(record)
    out.pop("id", None)
    return out


def _normalize_records(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    if isinstance(result, dict):
        record = _normalize_record(result)
        return [record] if record is not None else []
    if not isinstance(result, list):
        return []

    records: list[dict[str, Any]] = []
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


def _ns(
    record: dict[str, Any] | None,
    *,
    uuid_fields: set[str],
    datetime_fields: set[str],
    id_field: str = "uuid",
) -> SimpleNamespace | None:
    if record is None:
        return None
    values: dict[str, Any] = dict.fromkeys(uuid_fields | datetime_fields, None)
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


def _auth_user_namespace(record: dict[str, Any] | None) -> SimpleNamespace | None:
    return _ns(
        record,
        uuid_fields={"uuid"},
        datetime_fields=_USER_DATETIME_FIELDS,
    )


def _auth_org_namespace(record: dict[str, Any] | None) -> SimpleNamespace | None:
    return _ns(
        record,
        uuid_fields={"uuid"},
        datetime_fields=_ORG_DATETIME_FIELDS,
    )


def _session_namespace(record: dict[str, Any] | None) -> SimpleNamespace | None:
    return _ns(
        record,
        uuid_fields={"uuid", "user_id", "organization_id"},
        datetime_fields=_SESSION_DATETIME_FIELDS,
    )


def _api_key_namespace(record: dict[str, Any] | None) -> SimpleNamespace | None:
    return _ns(
        record,
        uuid_fields={"uuid", "organization_id", "user_id"},
        datetime_fields=_API_KEY_DATETIME_FIELDS,
    )


def _device_request_namespace(record: dict[str, Any] | None) -> SimpleNamespace | None:
    return _ns(
        record,
        uuid_fields={"uuid", "user_id", "organization_id"},
        datetime_fields=_DEVICE_DATETIME_FIELDS,
    )


def _oauth_connection_namespace(record: dict[str, Any] | None) -> SimpleNamespace | None:
    return _ns(
        record,
        uuid_fields={"uuid", "user_id"},
        datetime_fields=_OAUTH_DATETIME_FIELDS,
    )


def _password_reset_namespace(record: dict[str, Any] | None) -> SimpleNamespace | None:
    return _ns(
        record,
        uuid_fields={"uuid", "user_id"},
        datetime_fields=_PASSWORD_RESET_DATETIME_FIELDS,
    )


@asynccontextmanager
async def _auth_client_scope():
    client = build_surreal_auth_client()
    try:
        yield client
    finally:
        await client.close()


class _SurrealRepository:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def select_one(self, query: str, **params: Any) -> dict[str, Any] | None:
        records = _normalize_records(await self._client.execute_query(query, **params))
        return records[0] if records else None

    async def select_many(self, query: str, **params: Any) -> list[dict[str, Any]]:
        return _normalize_records(await self._client.execute_query(query, **params))

    async def replace_record(
        self, table: str, *, uuid: UUID, record: dict[str, Any]
    ) -> dict[str, Any]:
        delete_query = _DELETE_QUERY_BY_TABLE.get(table)
        create_query = _CREATE_QUERY_BY_TABLE.get(table)
        if delete_query is None or create_query is None:
            msg = f"Unsupported replace table: {table}"
            raise ValueError(msg)
        await self._client.execute_query(delete_query, uuid=str(uuid))
        created = _normalize_records(
            await self._client.execute_query(create_query, record=record)
        )
        if not created:
            msg = f"Failed to write {table} record {uuid}"
            raise RuntimeError(msg)
        return created[0]


class SurrealSessionRepository(_SurrealRepository):
    @classmethod
    def from_client(cls, client: Any) -> Self:
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
            "uuid": str(uuid4()),
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
        return self._auth_session_from_record(created[0])

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
        record = await self.select_one(
            "SELECT * FROM user_sessions WHERE uuid = $uuid LIMIT 1;",
            uuid=str(session.id),
        )
        if record is None:
            msg = f"Session not found: {session.id}"
            raise LookupError(msg)
        updated = {
            **record,
            "token_hash": self.hash_token(new_access_token),
            "expires_at": _coerce_datetime(new_access_expires_at) or new_access_expires_at,
            "refresh_token_hash": self.hash_token(new_refresh_token),
            "refresh_token_expires_at": _coerce_datetime(new_refresh_expires_at)
            or new_refresh_expires_at,
            "last_active_at": _utcnow(),
            "updated_at": _utcnow(),
        }
        written = await self.replace_record("user_sessions", uuid=session.id, record=updated)
        return self._auth_session_from_record(written)

    async def list_user_sessions(
        self, user_id: UUID, *, include_expired: bool = False
    ) -> list[AuthSession]:
        records = await self.select_many(
            "SELECT * FROM user_sessions WHERE user_id = $user_id ORDER BY last_active_at DESC;",
            user_id=str(user_id),
        )
        return [
            self._auth_session_from_record(record)
            for record in records
            if self._is_session_active(record, include_expired=include_expired)
        ]

    async def update_activity(self, token: str) -> bool:
        record = await self.select_one(
            "SELECT * FROM user_sessions WHERE token_hash = $token_hash LIMIT 1;",
            token_hash=self.hash_token(token),
        )
        if not self._is_session_active(record):
            return False
        updated = {**record, "last_active_at": _utcnow(), "updated_at": _utcnow()}
        await self.replace_record(
            "user_sessions",
            uuid=_coerce_uuid(updated.get("uuid"), field_name="session.uuid"),
            record=updated,
        )
        return True

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
        sessions = await self.select_many(
            "SELECT * FROM user_sessions WHERE user_id = $user_id ORDER BY created_at ASC;",
            user_id=user_id,
        )
        for session_record in sessions:
            updated = {
                **session_record,
                "is_current": session_record.get("uuid") == record.get("uuid"),
                "updated_at": _utcnow(),
            }
            await self.replace_record(
                "user_sessions",
                uuid=_coerce_uuid(updated.get("uuid"), field_name="session.uuid"),
                record=updated,
            )
        return True

    async def revoke_session(self, session_id: UUID, user_id: UUID) -> bool:
        record = await self.select_one(
            "SELECT * FROM user_sessions WHERE uuid = $uuid LIMIT 1;",
            uuid=str(session_id),
        )
        if record is None or _coerce_optional_uuid(record.get("user_id")) != user_id:
            return False
        if not self._is_session_active(record, include_expired=True):
            return False
        updated = {**record, "revoked_at": _utcnow(), "updated_at": _utcnow()}
        await self.replace_record("user_sessions", uuid=session_id, record=updated)
        return True

    async def revoke_all_sessions(
        self, user_id: UUID, *, exclude_token_hash: str | None = None
    ) -> int:
        records = await self.select_many(
            "SELECT * FROM user_sessions WHERE user_id = $user_id ORDER BY created_at ASC;",
            user_id=str(user_id),
        )
        now = _utcnow()
        count = 0
        for record in records:
            if record.get("revoked_at") is not None:
                continue
            if exclude_token_hash and record.get("token_hash") == exclude_token_hash:
                continue
            updated = {**record, "revoked_at": now, "updated_at": now}
            await self.replace_record(
                "user_sessions",
                uuid=_coerce_uuid(updated.get("uuid"), field_name="session.uuid"),
                record=updated,
            )
            count += 1
        return count

    async def cleanup_expired(self, *, older_than_days: int = 30) -> int:
        cutoff = _utcnow() - timedelta(days=older_than_days)
        records = await self.select_many("SELECT * FROM user_sessions ORDER BY created_at ASC;")
        count = 0
        for record in records:
            expires_at = _coerce_datetime(record.get("expires_at"))
            if expires_at is None or expires_at >= cutoff:
                continue
            await self._client.execute_query(
                "DELETE FROM user_sessions WHERE uuid = $uuid;",
                uuid=str(record["uuid"]),
            )
            count += 1
        return count

    def _is_session_active(
        self,
        record: dict[str, Any] | None,
        *,
        include_expired: bool = False,
    ) -> bool:
        if record is None or record.get("revoked_at") is not None:
            return False
        if include_expired:
            return True
        expires_at = _coerce_datetime(record.get("expires_at"))
        return expires_at is not None and expires_at > _utcnow()

    def _has_refresh_session(self, record: dict[str, Any] | None) -> bool:
        if not self._is_session_active(record, include_expired=True):
            return False
        if record is None:
            return False
        refresh_expires_at = _coerce_datetime(record.get("refresh_token_expires_at"))
        return refresh_expires_at is not None and refresh_expires_at > _utcnow()

    def _auth_session_from_record(self, record: dict[str, Any]) -> AuthSession:
        return AuthSession(
            id=_coerce_uuid(record.get("uuid"), field_name="session.uuid"),
            user_id=_coerce_uuid(record.get("user_id"), field_name="session.user_id"),
            organization_id=_coerce_optional_uuid(record.get("organization_id")),
            expires_at=_coerce_datetime(record.get("expires_at")) or _utcnow(),
            refresh_token_expires_at=_coerce_datetime(record.get("refresh_token_expires_at")),
            revoked_at=_coerce_datetime(record.get("revoked_at")),
            last_active_at=_coerce_datetime(record.get("last_active_at")),
            is_current=bool(record.get("is_current", False)),
            device_name=record.get("device_name"),
            device_type=record.get("device_type"),
            browser=record.get("browser"),
            os=record.get("os"),
            ip_address=record.get("ip_address"),
            user_agent=record.get("user_agent"),
            location=record.get("location"),
        )


def _scopes_list(value: object | None) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


async def resolve_surreal_auth_context(claims: dict[str, Any]) -> Any:
    async with _auth_client_scope() as client:
        resolver = SurrealAuthContextResolver.from_client(client)
        return await resolver.resolve(claims)


async def resolve_legacy_auth_context(
    *,
    claims: dict[str, Any],
    session: Any | None = None,
) -> Any:
    del session
    return await resolve_surreal_auth_context(claims)


async def _log_audit_event(
    client: Any,
    *,
    action: str,
    user_id: UUID | None,
    organization_id: UUID | None,
    request: Any,
    details: dict[str, Any],
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
    await client.execute_query("CREATE audit_logs CONTENT $record;", record=record)


async def _list_user_org_records(client: Any, *, user_id: UUID) -> list[dict[str, Any]]:
    repo = _SurrealRepository(client)
    memberships = await repo.select_many(
        "SELECT * FROM organization_members WHERE user_id = $user_id ORDER BY created_at ASC;",
        user_id=str(user_id),
    )
    org_ids = [str(record["organization_id"]) for record in memberships if record.get("organization_id")]
    organizations: list[dict[str, Any]] = []
    for org_id in org_ids:
        record = await repo.select_one(
            "SELECT * FROM organizations WHERE uuid = $uuid LIMIT 1;",
            uuid=org_id,
        )
        if record is not None:
            organizations.append(record)
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
    client: Any,
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
    client: Any,
    *,
    user: SimpleNamespace,
    organization: SimpleNamespace,
    request: Any,
    action: str,
    details: dict[str, Any],
) -> LegacyIssuedAuthSession:
    access_token = create_access_token(user_id=user.id, organization_id=organization.id)
    refresh_token, refresh_expires = create_refresh_token(
        user_id=user.id,
        organization_id=organization.id,
    )
    access_expires = _utcnow() + timedelta(minutes=config_module.settings.access_token_expire_minutes)
    sessions = SurrealSessionRepository.from_client(client)
    await sessions.create_session(
        user_id=user.id,
        organization_id=organization.id,
        token=access_token,
        expires_at=access_expires,
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
    return LegacyIssuedAuthSession(
        user=user,
        organization=organization,
        access_token=access_token,
        refresh_token=refresh_token,
        refresh_expires=refresh_expires,
    )


async def authenticate_legacy_api_key(raw_key: str):
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
            updated = {**candidate, "last_used_at": now, "updated_at": now}
            await repo.replace_record(
                "api_keys",
                uuid=_coerce_uuid(updated.get("uuid"), field_name="api_key.uuid"),
                record=updated,
            )
            project_scope_records = await repo.select_many(
                "SELECT * FROM api_key_project_scopes WHERE api_key_id = $api_key_id ORDER BY created_at ASC;",
                api_key_id=str(updated["uuid"]),
            )
            project_ids = [
                _coerce_uuid(record.get("project_id"), field_name="api_key_project_scope.project_id")
                for record in project_scope_records
                if record.get("project_id") is not None
            ]
            return ApiKeyAuth(
                api_key_id=_coerce_uuid(updated.get("uuid"), field_name="api_key.uuid"),
                user_id=_coerce_uuid(updated.get("user_id"), field_name="api_key.user_id"),
                organization_id=_coerce_uuid(
                    updated.get("organization_id"), field_name="api_key.organization_id"
                ),
                scopes=_scopes_list(updated.get("scopes")),
                project_ids=project_ids if project_ids else None,
            )
    return None


async def authenticate_legacy_local_user(*, email: str, password: str):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        record = await repo.select_one(
            "SELECT * FROM users WHERE email = $email LIMIT 1;",
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
            iterations=int(record.get("password_iterations") or config_module.settings.password_iterations),
        )
        if not ok:
            return None
        return _auth_user_namespace(record)


async def get_legacy_user_by_id(user_id: UUID):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        record = await repo.select_one(
            "SELECT * FROM users WHERE uuid = $uuid LIMIT 1;",
            uuid=str(user_id),
        )
        return _auth_user_namespace(record)


async def list_legacy_user_organizations(*, user_id: UUID) -> list[SimpleNamespace]:
    async with _auth_client_scope() as client:
        records = await _list_user_org_records(client, user_id=user_id)
        return [org for record in records if (org := _auth_org_namespace(record)) is not None]


async def ensure_legacy_personal_organization(*, user_id: UUID):
    async with _auth_client_scope() as client:
        users = SurrealUserRepository.from_client(client)
        orgs = SurrealOrganizationRepository.from_client(client)
        memberships = SurrealOrganizationMembershipRepository.from_client(client)
        user = await users.get_by_id(user_id)
        if user is None:
            return None
        organization = await orgs.create_personal_for_user(user)
        await memberships.add_member(
            organization_id=organization.id,
            user_id=user.id,
            role=OrganizationRole.OWNER,
        )
        return _auth_org_namespace(
            {
                "uuid": str(organization.id),
                "name": organization.name,
                "slug": organization.slug,
                "is_personal": organization.is_personal,
                "settings": dict(organization.settings),
            }
        )


async def create_legacy_session_record(
    *,
    user_id: UUID,
    token: str,
    expires_at,
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
        record = await _SurrealRepository(client).select_one(
            "SELECT * FROM user_sessions WHERE uuid = $uuid LIMIT 1;",
            uuid=str(session.id),
        )
        return _session_namespace(record)


async def load_legacy_refresh_session_record(refresh_token: str):
    async with _auth_client_scope() as client:
        sessions = SurrealSessionRepository.from_client(client)
        session = await sessions.get_session_by_refresh_token(refresh_token)
        if session is None:
            return None
        record = await _SurrealRepository(client).select_one(
            "SELECT * FROM user_sessions WHERE uuid = $uuid LIMIT 1;",
            uuid=str(session.id),
        )
        return _session_namespace(record)


async def rotate_legacy_refresh_session_record(
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
        record = await _SurrealRepository(client).select_one(
            "SELECT * FROM user_sessions WHERE uuid = $uuid LIMIT 1;",
            uuid=str(rotated.id),
        )
        return _session_namespace(record)


async def revoke_legacy_refresh_session_record(refresh_token: str) -> None:
    async with _auth_client_scope() as client:
        sessions = SurrealSessionRepository.from_client(client)
        existing = await sessions.get_session_by_refresh_token(refresh_token)
        if existing is None:
            return
        repo = _SurrealRepository(client)
        record = await repo.select_one(
            "SELECT * FROM user_sessions WHERE uuid = $uuid LIMIT 1;",
            uuid=str(existing.id),
        )
        if record is None:
            return
        updated = {**record, "revoked_at": _utcnow(), "updated_at": _utcnow()}
        await repo.replace_record("user_sessions", uuid=existing.id, record=updated)


async def login_legacy_github_identity(*, identity, request) -> LegacyIssuedAuthSession:
    async with _auth_client_scope() as client:
        users = SurrealUserRepository.from_client(client)
        orgs = SurrealOrganizationRepository.from_client(client)
        memberships = SurrealOrganizationMembershipRepository.from_client(client)
        is_first_user = not await users.has_any_users()
        user = await users.upsert_from_github(identity, is_admin=is_first_user)
        organization = await orgs.create_personal_for_user(user)
        await memberships.add_member(
            organization_id=organization.id,
            user_id=user.id,
            role=OrganizationRole.OWNER,
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
            organization=_require_namespace(
                _auth_org_namespace(
                {
                    "uuid": str(organization.id),
                    "name": organization.name,
                    "slug": organization.slug,
                    "is_personal": organization.is_personal,
                    "settings": dict(organization.settings),
                }
                ),
                label="organization",
            ),
            request=request,
            action="auth.github.login",
            details={"github_id": user.github_id, "email": user.email},
        )


async def signup_legacy_local_user(*, email: str, password: str, name: str, request):
    async with _auth_client_scope() as client:
        users = SurrealUserRepository.from_client(client)
        orgs = SurrealOrganizationRepository.from_client(client)
        memberships = SurrealOrganizationMembershipRepository.from_client(client)
        is_first_user = not await users.has_any_users()
        user = await users.create_local_user(
            email=email,
            password=password,
            name=name,
            is_admin=is_first_user,
        )
        organization = await orgs.create_personal_for_user(user)
        await memberships.add_member(
            organization_id=organization.id,
            user_id=user.id,
            role=OrganizationRole.OWNER,
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
            organization=_require_namespace(
                _auth_org_namespace(
                {
                    "uuid": str(organization.id),
                    "name": organization.name,
                    "slug": organization.slug,
                    "is_personal": organization.is_personal,
                    "settings": dict(organization.settings),
                }
                ),
                label="organization",
            ),
            request=request,
            action="auth.local.signup",
            details={"email": user.email},
        )


async def login_legacy_local_user(*, email: str, password: str, request):
    async with _auth_client_scope() as client:
        users = SurrealUserRepository.from_client(client)
        orgs = SurrealOrganizationRepository.from_client(client)
        memberships = SurrealOrganizationMembershipRepository.from_client(client)
        user = await users.authenticate_local(email=email, password=password)
        if user is None:
            return None
        organization = await orgs.create_personal_for_user(user)
        await memberships.add_member(
            organization_id=organization.id,
            user_id=user.id,
            role=OrganizationRole.OWNER,
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
            organization=_require_namespace(
                _auth_org_namespace(
                {
                    "uuid": str(organization.id),
                    "name": organization.name,
                    "slug": organization.slug,
                    "is_personal": organization.is_personal,
                    "settings": dict(organization.settings),
                }
                ),
                label="organization",
            ),
            request=request,
            action="auth.local.login",
            details={"email": user.email},
        )


async def start_legacy_device_authorization(
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


async def exchange_legacy_device_code(*, device_code: str) -> dict[str, object]:
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
            updated = {
                **record,
                "last_polled_at": now,
                "updated_at": now,
            }
            await repo.replace_record(
                "device_authorization_requests",
                uuid=request_row.id,
                record=updated,
            )
            raise DeviceTokenError("authorization_pending", "Authorization pending")

        if request_row.user_id is None:
            raise DeviceTokenError("server_error", "Approved request missing user_id")
        access_token = create_access_token(
            user_id=request_row.user_id,
            organization_id=request_row.organization_id,
            extra_claims={"scope": (request_row.scope or "mcp").strip() or "mcp"},
        )
        refresh_token, refresh_expires = create_refresh_token(
            user_id=request_row.user_id,
            organization_id=request_row.organization_id,
        )
        access_expires = now + timedelta(minutes=config_module.settings.access_token_expire_minutes)
        await sessions.create_session(
            user_id=request_row.user_id,
            organization_id=request_row.organization_id,
            token=access_token,
            expires_at=access_expires,
            refresh_token=refresh_token,
            refresh_token_expires_at=refresh_expires,
            device_name=request_row.client_name,
            device_type="device",
        )
        updated = {
            **record,
            "status": "consumed",
            "consumed_at": now,
            "updated_at": now,
        }
        await repo.replace_record(
            "device_authorization_requests",
            uuid=request_row.id,
            record=updated,
        )
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "expires_in": int(
                timedelta(minutes=config_module.settings.access_token_expire_minutes).total_seconds()
            ),
            "scope": (request_row.scope or "mcp").strip() or "mcp",
        }


async def get_legacy_device_request_by_user_code(user_code: str):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        record = await repo.select_one(
            "SELECT * FROM device_authorization_requests WHERE user_code = $user_code LIMIT 1;",
            user_code=user_code,
        )
        return _device_request_namespace(record)


async def resolve_legacy_request_claims(request) -> dict[str, Any] | None:
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
        auth = await authenticate_legacy_api_key(token)
        if auth is None:
            return None
        return {
            "sub": str(auth.user_id),
            "org": str(auth.organization_id),
            "typ": "api_key",
            "scopes": list(auth.scopes or []),
        }
    return None


async def resolve_legacy_request_user(request):
    claims = await resolve_legacy_request_claims(request)
    if not claims:
        return None
    try:
        user_id = UUID(str(claims.get("sub", "")))
    except ValueError:
        return None
    return await get_legacy_user_by_id(user_id)


async def login_legacy_device_browser_user(*, email: str, password: str, request):
    issued = await login_legacy_local_user(email=email, password=password, request=request)
    if issued is None:
        return None
    return LegacyDeviceBrowserLogin(
        user=issued.user,
        organization=issued.organization,
        access_token=issued.access_token,
    )


async def deny_legacy_device_authorization(*, user_id: UUID, user_code: str, request):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        user = await get_legacy_user_by_id(user_id)
        if user is None:
            return None
        record = await repo.select_one(
            "SELECT * FROM device_authorization_requests WHERE user_code = $user_code LIMIT 1;",
            user_code=user_code,
        )
        request_row = _device_request_namespace(record)
        now = _utcnow()
        if request_row is None or request_row.expires_at <= now or request_row.status != "pending":
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
            details={"device_request_id": str(request_row.id), "client_name": request_row.client_name},
        )
        return _device_request_namespace(written)


async def approve_legacy_device_authorization(*, user_id: UUID, user_code: str, request):
    async with _auth_client_scope() as client:
        users = SurrealUserRepository.from_client(client)
        orgs = SurrealOrganizationRepository.from_client(client)
        memberships = SurrealOrganizationMembershipRepository.from_client(client)
        repo = _SurrealRepository(client)
        user = await users.get_by_id(user_id)
        if user is None:
            return None
        record = await repo.select_one(
            "SELECT * FROM device_authorization_requests WHERE user_code = $user_code LIMIT 1;",
            user_code=user_code,
        )
        request_row = _device_request_namespace(record)
        now = _utcnow()
        if request_row is None or request_row.expires_at <= now or request_row.status != "pending":
            return None
        organization = await orgs.create_personal_for_user(user)
        await memberships.add_member(
            organization_id=organization.id,
            user_id=user.id,
            role=OrganizationRole.OWNER,
        )
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
            details={"device_request_id": str(request_row.id), "client_name": request_row.client_name},
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


async def rotate_legacy_refresh_exchange(
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
        access_token = create_access_token(user_id=user_id, organization_id=organization_id)
        new_refresh_token, refresh_expires = create_refresh_token(
            user_id=user_id,
            organization_id=organization_id,
            session_id=existing.id,
        )
        access_expires = _utcnow() + timedelta(minutes=config_module.settings.access_token_expire_minutes)
        await sessions.rotate_tokens(
            existing,
            new_access_token=access_token,
            new_access_expires_at=access_expires,
            new_refresh_token=new_refresh_token,
            new_refresh_expires_at=refresh_expires,
        )
        await _log_audit_event(
            client,
            action="auth.token.refresh",
            user_id=user_id,
            organization_id=organization_id,
            request=request,
            details={"session_id": str(existing.id)},
        )
        return LegacyRefreshRotation(
            session_id=existing.id,
            access_token=access_token,
            refresh_token=new_refresh_token,
            refresh_expires=refresh_expires,
            user_id=user_id,
            organization_id=organization_id,
        )


async def revoke_legacy_access_session(token: str) -> None:
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        sessions = SurrealSessionRepository.from_client(client)
        existing = await sessions.get_session_by_token(token)
        if existing is None:
            return
        record = await repo.select_one(
            "SELECT * FROM user_sessions WHERE uuid = $uuid LIMIT 1;",
            uuid=str(existing.id),
        )
        if record is None:
            return
        updated = {**record, "revoked_at": _utcnow(), "updated_at": _utcnow()}
        await repo.replace_record("user_sessions", uuid=existing.id, record=updated)


async def log_legacy_audit_event(
    *,
    action: str,
    user_id: UUID | None,
    organization_id: UUID | None,
    request,
    details: dict[str, Any],
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


def _project_record_namespace(record: dict[str, Any]) -> SimpleNamespace:
    owner_user_id = _coerce_optional_uuid(record.get("owner_user_id"))
    return SimpleNamespace(
        id=_coerce_uuid(record.get("uuid"), field_name="projects.uuid"),
        organization_id=_coerce_uuid(record.get("organization_id"), field_name="projects.organization_id"),
        graph_project_id=str(record.get("graph_project_id") or ""),
        name=record.get("name"),
        description=record.get("description"),
        visibility=ProjectVisibility(str(record.get("visibility") or ProjectVisibility.ORG.value)),
        default_role=ProjectRole(str(record.get("default_role") or ProjectRole.VIEWER.value)),
        owner_user_id=owner_user_id,
    )


async def create_legacy_project_record(
    *,
    organization_id: UUID,
    owner_user_id: UUID,
    graph_project_id: str,
    name: str,
    description: str | None = None,
) -> dict[str, Any]:
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


async def update_legacy_project_record(
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


async def delete_legacy_project_record(
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
        await client.execute_query(
            "DELETE FROM api_key_project_scopes WHERE project_id = $project_id;",
            project_id=project_uuid,
        )
        await client.execute_query(
            "DELETE FROM team_projects WHERE project_id = $project_id;",
            project_id=project_uuid,
        )
        await client.execute_query(
            "DELETE FROM project_members WHERE project_id = $project_id;",
            project_id=project_uuid,
        )
        await client.execute_query(
            "DELETE FROM projects WHERE uuid = $uuid AND organization_id = $organization_id;",
            uuid=project_uuid,
            organization_id=str(organization_id),
        )
        return True


async def get_legacy_project_record_by_graph_id(
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
            raise HTTPException(status_code=404, detail=f"Project not found: {graph_project_id}")
        return _project_record_namespace(record)


async def get_legacy_project_record_by_id(
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
            raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")
        return _project_record_namespace(record)


async def list_legacy_api_keys_for_user(*, organization_id: UUID, user_id: UUID):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        records = await repo.select_many(
            "SELECT * FROM api_keys "
            "WHERE organization_id = $organization_id AND user_id = $user_id "
            "ORDER BY created_at DESC;",
            organization_id=str(organization_id),
            user_id=str(user_id),
        )
        return [key for record in records if (key := _api_key_namespace(record)) is not None]


async def create_legacy_api_key_for_user(
    *,
    organization_id: UUID,
    user_id: UUID,
    name: str,
    live: bool,
    scopes: list[str],
    expires_at,
    request,
):
    async with _auth_client_scope() as client:
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
        await _log_audit_event(
            client,
            action="auth.api_key.create",
            user_id=user_id,
            organization_id=organization_id,
            request=request,
            details={"api_key_id": str(key.id), "name": key.name, "prefix": key.key_prefix},
        )
        return key, raw


async def revoke_legacy_api_key_for_user(
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
        if record is None or _coerce_optional_uuid(record.get("organization_id")) != organization_id:
            raise HTTPException(status_code=404, detail="API key not found")
        if _coerce_optional_uuid(record.get("user_id")) != actor_user_id and _role_value(
            actor_org_role
        ) not in _ORG_ADMIN_ROLE_VALUES:
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


async def update_legacy_auth_user(
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
        users = SurrealUserRepository.from_client(client)
        user = await repo.select_one("SELECT * FROM users WHERE uuid = $uuid LIMIT 1;", uuid=str(user_id))
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        changes: list[str] = []
        updated = dict(user)
        if email is not None:
            normalized_email = email.strip().lower()
            existing = await users.get_by_email(normalized_email)
            if existing is not None and existing.id != user_id:
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
            auth_user = await users.get_by_id(user_id)
            if auth_user is None:
                raise HTTPException(status_code=404, detail="User not found")
            changed_user = await users.change_password(
                auth_user,
                PasswordChange(current_password=current_password, new_password=new_password),
            )
            updated["password_salt"] = user.get("password_salt")
            updated["password_hash"] = user.get("password_hash")
            updated["password_iterations"] = user.get("password_iterations")
            refreshed = await repo.select_one(
                "SELECT * FROM users WHERE uuid = $uuid LIMIT 1;",
                uuid=str(changed_user.id),
            )
            if refreshed is not None:
                updated = refreshed
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


async def patch_legacy_auth_user(
    *,
    user_id: UUID,
    updates: dict[str, Any],
    organization_id: UUID | None,
    request,
):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        users = SurrealUserRepository.from_client(client)
        user = await repo.select_one("SELECT * FROM users WHERE uuid = $uuid LIMIT 1;", uuid=str(user_id))
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        updated = dict(user)
        changes: list[str] = []

        if "email" in updates:
            email = updates["email"]
            normalized_email = str(email).strip().lower() if email is not None else ""
            if not normalized_email:
                raise HTTPException(status_code=400, detail="Email is required")
            existing = await users.get_by_email(normalized_email)
            if existing is not None and existing.id != user_id:
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
            updated["avatar_url"] = str(avatar_url).strip() or None if avatar_url is not None else None
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


async def list_legacy_user_sessions(
    *,
    user_id: UUID,
    include_expired: bool = False,
) -> list[SimpleNamespace]:
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        rows = await repo.select_many(
            "SELECT * FROM user_sessions WHERE user_id = $user_id ORDER BY last_active_at DESC;",
            user_id=str(user_id),
        )
        sessions: list[SimpleNamespace] = []
        for row in rows:
            if row.get("revoked_at") is not None:
                continue
            if not include_expired:
                expires_at = _coerce_datetime(row.get("expires_at"))
                if expires_at is None or expires_at <= _utcnow():
                    continue
            session = _session_namespace(row)
            if session is not None:
                sessions.append(session)
        return sessions


async def revoke_all_legacy_user_sessions(
    *,
    user_id: UUID,
    exclude_token_hash: str | None = None,
) -> int:
    async with _auth_client_scope() as client:
        sessions = SurrealSessionRepository.from_client(client)
        return await sessions.revoke_all_sessions(user_id, exclude_token_hash=exclude_token_hash)


async def revoke_legacy_user_session(
    *,
    user_id: UUID,
    session_id: UUID,
) -> bool:
    async with _auth_client_scope() as client:
        sessions = SurrealSessionRepository.from_client(client)
        return await sessions.revoke_session(session_id, user_id)


async def request_legacy_password_reset(email: str) -> None:
    normalized_email = email.strip().lower()
    if not normalized_email:
        return

    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        user = await repo.select_one(
            "SELECT * FROM users WHERE email = $email LIMIT 1;",
            email=normalized_email,
        )
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
        existing_tokens = await repo.select_many(
            "SELECT * FROM password_reset_tokens WHERE user_id = $user_id ORDER BY created_at DESC;",
            user_id=str(user["uuid"]),
        )
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
        for token_record in existing_tokens:
            token_row = _password_reset_namespace(token_record)
            if token_row is None or token_row.used_at is not None or token_row.revoked_at is not None:
                continue
            updated_token = {**token_record, "revoked_at": now}
            await repo.replace_record(
                "password_reset_tokens",
                uuid=token_row.id,
                record=updated_token,
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
        await client.execute_query("CREATE password_reset_tokens CONTENT $record;", record=token_record)

        from sibyl.config import settings as app_settings

        reset_url = (
            f"{app_settings.frontend_url.rstrip('/')}/reset-password?token={raw_token}"
        )
        template = PasswordResetEmail(
            reset_url=reset_url,
            user_name=str(user.get("name") or "") or None,
            expires_in_minutes=60,
        )
        await get_email_client().send_template(template, to=str(user.get("email") or normalized_email))
        await _log_login_history(
            client,
            user_id=_coerce_uuid(user.get("uuid"), field_name="user.uuid"),
            event_type="password_reset_request",
            success=True,
            email_attempted=normalized_email,
        )


async def confirm_legacy_password_reset(token: str, new_password: str) -> None:
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        token_record = await repo.select_one(
            "SELECT * FROM password_reset_tokens WHERE token_hash = $token_hash LIMIT 1;",
            token_hash=_hash_reset_token(token),
        )
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

        user = await repo.select_one(
            "SELECT * FROM users WHERE uuid = $uuid LIMIT 1;",
            uuid=str(reset_token.user_id),
        )
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


async def list_legacy_oauth_connections(*, user_id: UUID) -> list[SimpleNamespace]:
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        rows = await repo.select_many(
            "SELECT * FROM oauth_connections WHERE user_id = $user_id ORDER BY created_at ASC;",
            user_id=str(user_id),
        )
        return [row for record in rows if (row := _oauth_connection_namespace(record)) is not None]


async def remove_legacy_oauth_connection(
    *,
    user_id: UUID,
    connection_id: UUID,
):
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        record = await repo.select_one(
            "SELECT * FROM oauth_connections WHERE uuid = $uuid AND user_id = $user_id LIMIT 1;",
            uuid=str(connection_id),
            user_id=str(user_id),
        )
        connection = _oauth_connection_namespace(record)
        if connection is None:
            raise HTTPException(status_code=404, detail="Connection not found")

        user = await repo.select_one("SELECT * FROM users WHERE uuid = $uuid LIMIT 1;", uuid=str(user_id))
        remaining_connections = await repo.select_many(
            "SELECT * FROM oauth_connections WHERE user_id = $user_id ORDER BY created_at ASC;",
            user_id=str(user_id),
        )
        has_other_connections = any(str(row.get("uuid")) != str(connection_id) for row in remaining_connections)
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


async def has_legacy_owner_membership(*, org_id: str, user_id: str | None) -> bool:
    if user_id is None:
        return False
    async with _auth_client_scope() as client:
        memberships = SurrealOrganizationMembershipRepository.from_client(client)
        membership = await memberships.get_for_user(UUID(org_id), UUID(user_id))
        return _role_value(membership.role if membership is not None else None) == "owner"


async def list_legacy_accessible_project_graph_ids(ctx) -> set[str]:
    if ctx.organization is None:
        return set()
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        org_id = str(ctx.organization.id)
        org_role = _role_value(ctx.org_role)
        user_id = str(ctx.user.id)
        project_records = await repo.select_many(
            "SELECT * FROM projects WHERE organization_id = $organization_id ORDER BY created_at ASC;",
            organization_id=org_id,
        )
        if not project_records:
            if ctx.org_role is None:
                return set()
            from sibyl.db.sync import get_graph_projects

            graph_projects = await get_graph_projects(org_id)
            return {
                graph_id
                for project in graph_projects
                if (graph_id := project.get("id") or project.get("uuid"))
            }
        if org_role in _ORG_ADMIN_ROLE_VALUES:
            return {
                str(record["graph_project_id"])
                for record in project_records
                if str(record.get("graph_project_id") or "").strip()
            }
        accessible: set[str] = set()
        org_visible = {
            str(record["uuid"]): str(record["graph_project_id"])
            for record in project_records
            if record.get("visibility") == ProjectVisibility.ORG.value
            and str(record.get("graph_project_id") or "").strip()
        }
        accessible.update(org_visible.values())
        direct_memberships = await repo.select_many(
            "SELECT * FROM project_members "
            "WHERE organization_id = $organization_id AND user_id = $user_id "
            "ORDER BY created_at ASC;",
            organization_id=org_id,
            user_id=user_id,
        )
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
        team_members = await repo.select_many(
            "SELECT * FROM team_members WHERE user_id = $user_id ORDER BY created_at ASC;",
            user_id=user_id,
        )
        team_ids = [str(record["team_id"]) for record in team_members if record.get("team_id")]
        for team_id in team_ids:
            team_projects = await repo.select_many(
                "SELECT * FROM team_projects WHERE team_id = $team_id ORDER BY created_at ASC;",
                team_id=team_id,
            )
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
        return accessible


async def resolve_legacy_accessible_project_graph_ids(
    *,
    user_id: str,
    org_id: str,
    scopes=None,
    api_key_project_ids=None,
) -> set[str] | None:
    try:
        auth_ctx = await resolve_surreal_auth_context(
            {"sub": user_id, "org": org_id, "scopes": list(scopes or [])}
        )
    except Exception:
        return set()
    if auth_ctx.organization is None:
        return set()
    user_accessible = await list_legacy_accessible_project_graph_ids(auth_ctx)
    if api_key_project_ids is not None:
        api_key_allowed = {str(project_id) for project_id in api_key_project_ids}
        if user_accessible is None:
            return api_key_allowed
        return user_accessible & api_key_allowed
    return user_accessible


async def verify_legacy_entity_project_access(
    *,
    ctx,
    entity_project_id: str | None,
    required_role: ProjectRole,
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
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        record = await repo.select_one(
            "SELECT * FROM projects "
            "WHERE organization_id = $organization_id AND graph_project_id = $graph_project_id "
            "LIMIT 1;",
            organization_id=str(ctx.organization.id),
            graph_project_id=entity_project_id,
        )
        if record is None:
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
        effective_role = await _effective_project_role(repo, ctx, record)
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


async def _effective_project_role(
    repo: _SurrealRepository,
    ctx,
    project: dict[str, Any],
) -> ProjectRole | None:
    if _role_value(ctx.org_role) in _ORG_ADMIN_ROLE_VALUES:
        return ProjectRole.OWNER
    if _coerce_optional_uuid(project.get("owner_user_id")) == ctx.user.id:
        return ProjectRole.OWNER
    direct_record = await repo.select_one(
        "SELECT * FROM project_members WHERE project_id = $project_id AND user_id = $user_id LIMIT 1;",
        project_id=str(project["uuid"]),
        user_id=str(ctx.user.id),
    )
    roles: list[ProjectRole] = []
    direct_role = _coerce_project_role(direct_record.get("role")) if direct_record else None
    if direct_role is not None:
        roles.append(direct_role)
    team_members = await repo.select_many(
        "SELECT * FROM team_members WHERE user_id = $user_id ORDER BY created_at ASC;",
        user_id=str(ctx.user.id),
    )
    for team_member in team_members:
        team_projects = await repo.select_many(
            "SELECT * FROM team_projects WHERE team_id = $team_id AND project_id = $project_id LIMIT 10;",
            team_id=str(team_member["team_id"]),
            project_id=str(project["uuid"]),
        )
        for team_project in team_projects:
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
    "LegacyDeviceBrowserLogin",
    "LegacyIssuedAuthSession",
    "LegacyRefreshRotation",
    "SurrealAuthContextResolver",
    "SurrealOrganizationMembershipRepository",
    "SurrealOrganizationRepository",
    "SurrealSessionRepository",
    "SurrealUserRepository",
    "approve_legacy_device_authorization",
    "authenticate_legacy_api_key",
    "authenticate_legacy_local_user",
    "build_surreal_auth_client",
    "create_legacy_api_key_for_user",
    "create_legacy_project_record",
    "create_legacy_session_record",
    "delete_legacy_project_record",
    "deny_legacy_device_authorization",
    "ensure_legacy_personal_organization",
    "exchange_legacy_device_code",
    "get_legacy_device_request_by_user_code",
    "get_legacy_project_record_by_graph_id",
    "get_legacy_project_record_by_id",
    "get_legacy_user_by_id",
    "has_legacy_owner_membership",
    "list_legacy_accessible_project_graph_ids",
    "list_legacy_api_keys_for_user",
    "list_legacy_oauth_connections",
    "list_legacy_user_sessions",
    "list_legacy_user_organizations",
    "load_legacy_refresh_session_record",
    "log_legacy_audit_event",
    "login_legacy_device_browser_user",
    "login_legacy_github_identity",
    "login_legacy_local_user",
    "resolve_legacy_auth_context",
    "resolve_legacy_accessible_project_graph_ids",
    "resolve_legacy_request_claims",
    "resolve_legacy_request_user",
    "resolve_surreal_auth_context",
    "revoke_legacy_access_session",
    "revoke_legacy_api_key_for_user",
    "revoke_legacy_user_session",
    "revoke_all_legacy_user_sessions",
    "revoke_legacy_refresh_session_record",
    "rotate_legacy_refresh_exchange",
    "rotate_legacy_refresh_session_record",
    "signup_legacy_local_user",
    "start_legacy_device_authorization",
    "patch_legacy_auth_user",
    "request_legacy_password_reset",
    "confirm_legacy_password_reset",
    "remove_legacy_oauth_connection",
    "update_legacy_auth_user",
    "update_legacy_project_record",
    "verify_legacy_entity_project_access",
]


IssuedAuthSession = LegacyIssuedAuthSession
DeviceBrowserLogin = LegacyDeviceBrowserLogin
RefreshRotation = LegacyRefreshRotation
AuthContextResolver = SurrealAuthContextResolver
OrganizationMembershipRepository = SurrealOrganizationMembershipRepository
OrganizationRepository = SurrealOrganizationRepository
SessionRepository = SurrealSessionRepository
UserRepository = SurrealUserRepository
approve_device_authorization = approve_legacy_device_authorization
authenticate_api_key = authenticate_legacy_api_key
authenticate_local_user = authenticate_legacy_local_user
create_api_key_for_user = create_legacy_api_key_for_user
create_project_record = create_legacy_project_record
create_session_record = create_legacy_session_record
delete_project_record = delete_legacy_project_record
deny_device_authorization = deny_legacy_device_authorization
ensure_personal_organization = ensure_legacy_personal_organization
exchange_device_code = exchange_legacy_device_code
get_device_request_by_user_code = get_legacy_device_request_by_user_code
get_project_record_by_graph_id = get_legacy_project_record_by_graph_id
get_project_record_by_id = get_legacy_project_record_by_id
get_user_by_id = get_legacy_user_by_id
has_owner_membership = has_legacy_owner_membership
list_accessible_project_graph_ids = list_legacy_accessible_project_graph_ids
list_api_keys_for_user = list_legacy_api_keys_for_user
list_oauth_connections = list_legacy_oauth_connections
list_user_organizations = list_legacy_user_organizations
list_user_sessions = list_legacy_user_sessions
load_refresh_session_record = load_legacy_refresh_session_record
log_audit_event = log_legacy_audit_event
login_device_browser_user = login_legacy_device_browser_user
login_github_identity = login_legacy_github_identity
login_local_user = login_legacy_local_user
patch_auth_user = patch_legacy_auth_user
confirm_password_reset = confirm_legacy_password_reset
remove_oauth_connection = remove_legacy_oauth_connection
request_password_reset = request_legacy_password_reset
resolve_accessible_project_graph_ids = resolve_legacy_accessible_project_graph_ids
resolve_auth_context = resolve_legacy_auth_context
resolve_request_claims = resolve_legacy_request_claims
resolve_request_user = resolve_legacy_request_user
revoke_access_session = revoke_legacy_access_session
revoke_api_key_for_user = revoke_legacy_api_key_for_user
revoke_all_user_sessions = revoke_all_legacy_user_sessions
revoke_refresh_session_record = revoke_legacy_refresh_session_record
revoke_user_session = revoke_legacy_user_session
rotate_refresh_exchange = rotate_legacy_refresh_exchange
rotate_refresh_session_record = rotate_legacy_refresh_session_record
signup_local_user = signup_legacy_local_user
start_device_authorization = start_legacy_device_authorization
update_auth_user = update_legacy_auth_user
update_project_record = update_legacy_project_record
verify_entity_project_access = verify_legacy_entity_project_access
