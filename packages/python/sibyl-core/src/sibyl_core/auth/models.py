"""Backend-agnostic auth domain models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class OrganizationRole(StrEnum):
    """Role of a user within an organization."""

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


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
    preferences: dict[str, Any] = Field(default_factory=dict)


class AuthOrganization(BaseModel):
    """Normalized organization record independent of any persistence model."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str = ""
    slug: str = ""
    is_personal: bool = False
    settings: dict[str, Any] = Field(default_factory=dict)


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
    device_name: str | None = None
    device_type: str | None = None
    browser: str | None = None
    os: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    location: str | None = None


def _required_uuid(value: Any, field_name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    msg = f"{field_name} is required for auth coercion"
    raise TypeError(msg)


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _str_or_default(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _bool_or_default(value: Any, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _dict_or_default(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def coerce_auth_user(value: AuthUser | Any) -> AuthUser:
    """Coerce an ORM row or stub object into the auth user contract."""

    if isinstance(value, AuthUser):
        return value
    return AuthUser(
        id=_required_uuid(getattr(value, "id", None), "user.id"),
        email=_optional_str(getattr(value, "email", None)),
        name=_str_or_default(getattr(value, "name", None)),
        avatar_url=_optional_str(getattr(value, "avatar_url", None)),
        github_id=_optional_int(getattr(value, "github_id", None)),
        is_admin=_bool_or_default(getattr(value, "is_admin", None)),
        bio=_optional_str(getattr(value, "bio", None)),
        timezone=_str_or_default(getattr(value, "timezone", None), "UTC"),
        preferences=_dict_or_default(getattr(value, "preferences", None)),
    )


def coerce_auth_organization(value: AuthOrganization | Any | None) -> AuthOrganization | None:
    """Coerce an ORM row or stub object into the auth organization contract."""

    if value is None:
        return None
    if isinstance(value, AuthOrganization):
        return value
    return AuthOrganization(
        id=_required_uuid(getattr(value, "id", None), "organization.id"),
        name=_str_or_default(getattr(value, "name", None)),
        slug=_str_or_default(getattr(value, "slug", None)),
        is_personal=_bool_or_default(getattr(value, "is_personal", None)),
        settings=_dict_or_default(getattr(value, "settings", None)),
    )


def coerce_organization_role(value: OrganizationRole | str | Any | None) -> OrganizationRole | None:
    """Normalize enum, string, or ORM-like role objects into the shared enum."""

    if value is None:
        return None
    if isinstance(value, OrganizationRole):
        return value
    if isinstance(value, str):
        return OrganizationRole(value)

    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, str):
        return OrganizationRole(enum_value)

    role_value = getattr(value, "role", None)
    if isinstance(role_value, OrganizationRole):
        return role_value
    if isinstance(role_value, str):
        return OrganizationRole(role_value)

    msg = f"Unsupported organization role value: {value!r}"
    raise TypeError(msg)
