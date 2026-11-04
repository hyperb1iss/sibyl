"""Backend-agnostic auth domain models."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class PrincipalKind(StrEnum):
    """What kind of principal a user record is.

    A service principal is a bot: a member a human sponsors, that signs in only
    with API keys and whose project access its sponsor caps on every request.
    """

    USER = "user"
    SERVICE = "service"


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
    kind: PrincipalKind = PrincipalKind.USER
    sponsor_user_id: UUID | None = None

    @model_validator(mode="after")
    def _bot_is_never_a_deployment_admin(self) -> AuthUser:
        if self.kind is PrincipalKind.SERVICE:
            self.is_admin = False
        return self

    @property
    def is_service(self) -> bool:
        """True for a bot, which a human sponsors and never signs in."""
        return self.kind is PrincipalKind.SERVICE


class UserIdentity(BaseModel):
    """External identity linked to a Sibyl user."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    provider_name: str
    issuer: str
    subject: str
    subject_key: str
    user_id: UUID
    email: str | None = None
    claims: JSONObject = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime | None = None
    last_login_at: datetime | None = None


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
        kind=coerce_principal_kind(_attr(value, "kind")),
        sponsor_user_id=_optional_uuid_value(_attr(value, "sponsor_user_id")),
    )


def coerce_principal_kind(value: object | None) -> PrincipalKind:
    """Read a stored kind; a record that predates the field is a user."""
    if isinstance(value, PrincipalKind):
        return value
    if isinstance(value, str) and value:
        return PrincipalKind(value)
    return PrincipalKind.USER


def _optional_uuid_value(value: object) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str) and value:
        return UUID(value)
    return None


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
