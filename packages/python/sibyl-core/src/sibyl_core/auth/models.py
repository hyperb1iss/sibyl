"""Backend-agnostic auth domain models."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

JSONObject = dict[str, object]


class OrganizationRole(StrEnum):
    """Role of a user within an organization."""

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class ProjectRole(StrEnum):
    """Role of a user within a project."""

    OWNER = "project_owner"
    MAINTAINER = "project_maintainer"
    CONTRIBUTOR = "project_contributor"
    VIEWER = "project_viewer"


class ProjectVisibility(StrEnum):
    """Visibility level for a project."""

    PRIVATE = "private"
    PROJECT = "project"
    ORG = "org"


class AuthUser(BaseModel):
    """Normalized user identity independent of any persistence model."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str | None = None
    name: str = ""
    avatar_url: str | None = None
    github_id: int | None = None
    is_admin: bool = False
    bio: str | None = None
    timezone: str | None = None
    preferences: JSONObject = Field(default_factory=dict)
    email_verified_at: datetime | None = None
    last_login_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime | None = None


class AuthOrganization(BaseModel):
    """Normalized organization record independent of any persistence model."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str = ""
    slug: str = ""
    is_personal: bool = False
    settings: JSONObject = Field(default_factory=dict)


class AuthMembership(BaseModel):
    """Normalized organization membership record."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID | None = None
    organization_id: UUID
    user_id: UUID
    role: OrganizationRole = OrganizationRole.MEMBER
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AuthSession(BaseModel):
    """Normalized authenticated session record."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    organization_id: UUID | None = None
    expires_at: datetime
    refresh_token_expires_at: datetime | None = None
    revoked_at: datetime | None = None
    last_active_at: datetime | None = None
    is_current: bool = False
    version: int = 0
    device_name: str | None = None
    device_type: str | None = None
    browser: str | None = None
    os: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    location: str | None = None


def _attr(value: object, name: str) -> object | None:
    return getattr(value, name, None)


def _required_uuid(value: object, field_name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    msg = f"{field_name} is required for auth coercion"
    raise TypeError(msg)


def _optional_uuid(value: object, field_name: str) -> UUID | None:
    return _required_uuid(value, field_name) if value is not None else None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _str_or_default(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _bool_or_default(value: object, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _optional_datetime(value: object) -> datetime | None:
    return value if isinstance(value, datetime) else None


def _required_datetime(value: object, field_name: str) -> datetime:
    if isinstance(value, datetime):
        return value
    msg = f"{field_name} is required for auth coercion"
    raise TypeError(msg)


def _json_value(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {str(key): _json_value(item) for key, item in mapping.items()}
    return str(value)


def _dict_or_default(value: object) -> JSONObject:
    if not isinstance(value, Mapping):
        return {}
    mapping = cast(Mapping[object, object], value)
    return {str(key): _json_value(item) for key, item in mapping.items()}


def coerce_auth_user(value: object) -> AuthUser:
    """Coerce an ORM row or stub object into the auth user contract."""

    if isinstance(value, AuthUser):
        return value
    return AuthUser(
        id=_required_uuid(_attr(value, "id"), "user.id"),
        email=_optional_str(_attr(value, "email")),
        name=_str_or_default(_attr(value, "name")),
        avatar_url=_optional_str(_attr(value, "avatar_url")),
        github_id=_optional_int(_attr(value, "github_id")),
        is_admin=_bool_or_default(_attr(value, "is_admin")),
        bio=_optional_str(_attr(value, "bio")),
        timezone=_str_or_default(_attr(value, "timezone"), "UTC"),
        preferences=_dict_or_default(_attr(value, "preferences")),
        email_verified_at=_optional_datetime(_attr(value, "email_verified_at")),
        last_login_at=_optional_datetime(_attr(value, "last_login_at")),
        created_at=_optional_datetime(_attr(value, "created_at")) or datetime.now(UTC),
        updated_at=_optional_datetime(_attr(value, "updated_at")),
    )


def coerce_auth_organization(value: object | None) -> AuthOrganization | None:
    """Coerce an ORM row or stub object into the auth organization contract."""

    if value is None:
        return None
    if isinstance(value, AuthOrganization):
        return value
    return AuthOrganization(
        id=_required_uuid(_attr(value, "id"), "organization.id"),
        name=_str_or_default(_attr(value, "name")),
        slug=_str_or_default(_attr(value, "slug")),
        is_personal=_bool_or_default(_attr(value, "is_personal")),
        settings=_dict_or_default(_attr(value, "settings")),
    )


def coerce_auth_membership(value: object | None) -> AuthMembership | None:
    """Coerce an ORM row or stub object into the auth membership contract."""

    if value is None:
        return None
    if isinstance(value, AuthMembership):
        return value
    return AuthMembership(
        id=_optional_uuid(_attr(value, "id"), "membership.id"),
        organization_id=_required_uuid(
            _attr(value, "organization_id"), "membership.organization_id"
        ),
        user_id=_required_uuid(_attr(value, "user_id"), "membership.user_id"),
        role=coerce_organization_role(_attr(value, "role")) or OrganizationRole.MEMBER,
        created_at=_optional_datetime(_attr(value, "created_at")),
        updated_at=_optional_datetime(_attr(value, "updated_at")),
    )


def coerce_auth_session(value: object | None) -> AuthSession | None:
    """Coerce an ORM row or stub object into the auth session contract."""

    if value is None:
        return None
    if isinstance(value, AuthSession):
        return value
    return AuthSession(
        id=_required_uuid(_attr(value, "id"), "session.id"),
        user_id=_required_uuid(_attr(value, "user_id"), "session.user_id"),
        organization_id=_optional_uuid(_attr(value, "organization_id"), "session.organization_id"),
        expires_at=_required_datetime(_attr(value, "expires_at"), "session.expires_at"),
        refresh_token_expires_at=_optional_datetime(_attr(value, "refresh_token_expires_at")),
        revoked_at=_optional_datetime(_attr(value, "revoked_at")),
        last_active_at=_optional_datetime(_attr(value, "last_active_at")),
        is_current=_bool_or_default(_attr(value, "is_current")),
        version=_optional_int(_attr(value, "version")) or 0,
        device_name=_optional_str(_attr(value, "device_name")),
        device_type=_optional_str(_attr(value, "device_type")),
        browser=_optional_str(_attr(value, "browser")),
        os=_optional_str(_attr(value, "os")),
        ip_address=_optional_str(_attr(value, "ip_address")),
        user_agent=_optional_str(_attr(value, "user_agent")),
        location=_optional_str(_attr(value, "location")),
    )


def coerce_organization_role(value: object | None) -> OrganizationRole | None:
    """Normalize enum, string, or ORM-like role objects into the shared enum."""

    if value is None:
        return None
    if isinstance(value, OrganizationRole):
        return value
    if isinstance(value, str):
        return OrganizationRole(value)

    enum_value = _attr(value, "value")
    if isinstance(enum_value, str):
        return OrganizationRole(enum_value)

    role_value = _attr(value, "role")
    if isinstance(role_value, OrganizationRole):
        return role_value
    if isinstance(role_value, str):
        return OrganizationRole(role_value)

    msg = f"Unsupported organization role value: {value!r}"
    raise TypeError(msg)
