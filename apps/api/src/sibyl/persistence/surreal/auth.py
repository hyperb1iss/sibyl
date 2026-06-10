"""Surreal-backed auth repositories."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Self, cast
from uuid import UUID, uuid4

from sibyl import config as config_module
from sibyl.auth.passwords import PasswordError, hash_password, verify_password
from sibyl.auth.primitives import slugify
from sibyl.persistence.auth_common import (
    InvalidAuthClaimsError,
    RepositoryAuthContextResolver,
    UserNotFoundError,
)
from sibyl_core.auth import (
    AuthContext,
    AuthMembership,
    AuthOrganization,
    AuthUser,
    GitHubUserIdentity,
    OrganizationRole,
    PasswordChange,
)
from sibyl_core.backends.surreal import SurrealAuthClient
from sibyl_core.backends.surreal.records import (
    coerce_datetime as _coerce_datetime,
    coerce_uuid as _coerce_uuid,
    normalize_record as _normalize_record,
    normalize_records as _normalize_records,
    utcnow as _utcnow,
)

AUTH_NAMESPACE = "sibyl_auth"
AUTH_DATABASE = "auth"


@dataclass(slots=True)
class _SharedAuthClientState:
    client: SurrealAuthClient | None = None


_shared_auth_client_state = _SharedAuthClientState()
_shared_auth_client_lock = asyncio.Lock()
_UPSERT_RECORD = {
    "organization_members": "UPSERT organization_members CONTENT $record WHERE uuid = $uuid;",
    "organizations": "UPSERT organizations CONTENT $record WHERE uuid = $uuid;",
    "users": "UPSERT users CONTENT $record WHERE uuid = $uuid;",
}
type SurrealRecord = dict[str, object]


def _object_mapping(value: object) -> Mapping[object, object]:
    if not isinstance(value, Mapping):
        return {}
    return cast("Mapping[object, object]", value)


def build_surreal_auth_client() -> SurrealAuthClient:
    """Build a Surreal auth client from application settings."""

    return SurrealAuthClient(
        url=config_module.settings.resolved_surreal_url,
        username=config_module.settings.surreal_username,
        password=config_module.settings.surreal_password.get_secret_value(),
        token=config_module.settings.surreal_token.get_secret_value(),
        namespace=AUTH_NAMESPACE,
        database=AUTH_DATABASE,
        pool_size=config_module.settings.surreal_client_pool_size("auth"),
    )


async def get_shared_surreal_auth_client() -> SurrealAuthClient:
    if _shared_auth_client_state.client is not None:
        return _shared_auth_client_state.client

    async with _shared_auth_client_lock:
        if _shared_auth_client_state.client is None:
            _shared_auth_client_state.client = build_surreal_auth_client()
        return _shared_auth_client_state.client


async def close_shared_surreal_auth_client() -> None:
    async with _shared_auth_client_lock:
        client = _shared_auth_client_state.client
        _shared_auth_client_state.client = None
        if client is not None:
            await client.close()


@asynccontextmanager
async def surreal_auth_client_scope() -> AsyncGenerator[SurrealAuthClient]:
    yield await get_shared_surreal_auth_client()


def _coerce_optional_str(value: object | None) -> str | None:
    return value if isinstance(value, str) else None


def _claim_optional_str(value: object | None) -> str | None:
    return str(value) if value is not None else None


def _claim_string_set(value: object | None) -> frozenset[str] | None:
    if value is None:
        return None
    if isinstance(value, list | tuple | set | frozenset):
        return frozenset(str(item) for item in value)
    return frozenset({str(value)})


def _coerce_optional_int(value: object | None) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _coerce_int(value: object | None, *, field_name: str) -> int:
    if isinstance(value, bool):
        msg = f"{field_name} must be an integer"
        raise TypeError(msg)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value:
        return int(value)
    msg = f"{field_name} is required"
    raise TypeError(msg)


def _coerce_dict(value: object | None) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _user_from_record(record: Mapping[str, object]) -> AuthUser:
    return AuthUser(
        id=_coerce_uuid(record.get("uuid"), field_name="user.uuid"),
        email=_coerce_optional_str(record.get("email")),
        name=str(record.get("name") or ""),
        avatar_url=_coerce_optional_str(record.get("avatar_url")),
        github_id=_coerce_optional_int(record.get("github_id")),
        is_admin=bool(record.get("is_admin", False)),
        bio=_coerce_optional_str(record.get("bio")),
        timezone=str(record.get("timezone") or "UTC"),
        preferences=_coerce_dict(record.get("preferences")),
        email_verified_at=_coerce_datetime(record.get("email_verified_at")),
        last_login_at=_coerce_datetime(record.get("last_login_at")),
        created_at=_coerce_datetime(record.get("created_at")) or _utcnow(),
        updated_at=_coerce_datetime(record.get("updated_at")),
    )


def _organization_from_record(record: Mapping[str, object]) -> AuthOrganization:
    return AuthOrganization(
        id=_coerce_uuid(record.get("uuid"), field_name="organization.uuid"),
        name=str(record.get("name") or ""),
        slug=str(record.get("slug") or ""),
        is_personal=bool(record.get("is_personal", False)),
        settings=_coerce_dict(record.get("settings")),
    )


def _membership_from_record(record: Mapping[str, object]) -> AuthMembership:
    return AuthMembership(
        id=_coerce_uuid(record.get("uuid"), field_name="membership.uuid"),
        organization_id=_coerce_uuid(
            record.get("organization_id"), field_name="membership.organization_id"
        ),
        user_id=_coerce_uuid(record.get("user_id"), field_name="membership.user_id"),
        role=OrganizationRole(str(record.get("role") or OrganizationRole.MEMBER.value)),
        created_at=_coerce_datetime(record.get("created_at")),
        updated_at=_coerce_datetime(record.get("updated_at")),
    )


class _SurrealAuthRepository:
    def __init__(self, client: SurrealAuthClient) -> None:
        self._client = client

    async def _select_one(self, query: str, **params: object) -> SurrealRecord | None:
        records = _normalize_records(await self._client.execute_query(query, **params))
        return records[0] if records else None

    async def _select_many(self, query: str, **params: object) -> list[SurrealRecord]:
        return _normalize_records(await self._client.execute_query(query, **params))

    async def _replace(self, table: str, *, uuid: UUID, record: SurrealRecord) -> SurrealRecord:
        created = _normalize_records(
            await self._client.execute_query(
                _UPSERT_RECORD[table],
                uuid=str(uuid),
                record=record,
            )
        )
        if not created:
            msg = f"Failed to write {table} record {uuid}"
            raise RuntimeError(msg)
        return created[0]


class SurrealUserRepository(_SurrealAuthRepository):
    """UserRepository backed by the shared Surreal auth namespace."""

    @classmethod
    def from_client(cls, client: SurrealAuthClient) -> Self:
        return cls(client)

    async def get_by_id(self, user_id: UUID) -> AuthUser | None:
        record = await self._select_one(
            "SELECT * FROM users WHERE uuid = $uuid AND deleted_at = NONE LIMIT 1;",
            uuid=str(user_id),
        )
        return _user_from_record(record) if record is not None else None

    async def has_any_users(self) -> bool:
        record = await self._select_one("SELECT * FROM users WHERE deleted_at = NONE LIMIT 1;")
        return record is not None

    async def get_by_github_id(self, github_id: int) -> AuthUser | None:
        record = await self._select_one(
            "SELECT * FROM users WHERE github_id = $github_id AND deleted_at = NONE LIMIT 1;",
            github_id=github_id,
        )
        return _user_from_record(record) if record is not None else None

    async def get_by_email(self, email: str) -> AuthUser | None:
        normalized = email.strip().lower()
        if not normalized:
            return None
        record = await self._select_one(
            "SELECT * FROM users WHERE email = $email AND deleted_at = NONE LIMIT 1;",
            email=normalized,
        )
        return _user_from_record(record) if record is not None else None

    async def upsert_from_github(
        self, identity: GitHubUserIdentity, *, is_admin: bool = False
    ) -> AuthUser:
        existing = await self._select_one(
            "SELECT * FROM users WHERE github_id = $github_id LIMIT 1;",
            github_id=identity.github_id,
        )
        if existing is not None and existing.get("deleted_at") is not None:
            raise ValueError("User is scheduled for deletion")
        now = _utcnow()
        record: SurrealRecord
        if existing is None:
            record = {
                "uuid": str(uuid4()),
                "github_id": identity.github_id,
                "email": identity.email.lower() if identity.email else None,
                "name": identity.name or identity.login,
                "avatar_url": identity.avatar_url,
                "bio": None,
                "timezone": "UTC",
                "preferences": {},
                "password_salt": None,
                "password_hash": None,
                "password_iterations": None,
                "is_admin": is_admin,
                "created_at": now,
                "updated_at": now,
            }
        else:
            record = {
                **existing,
                "email": identity.email.lower() if identity.email else existing.get("email"),
                "name": identity.name or existing.get("name") or identity.login,
                "avatar_url": identity.avatar_url or existing.get("avatar_url"),
                "updated_at": now,
            }
        written = await self._replace(
            "users",
            uuid=_coerce_uuid(record.get("uuid"), field_name="user.uuid"),
            record=record,
        )
        return _user_from_record(written)

    async def create_local_user(
        self, *, email: str, password: str, name: str, is_admin: bool = False
    ) -> AuthUser:
        normalized = email.strip().lower()
        if not normalized:
            raise ValueError("Email is required")
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("Name is required")
        if await self.get_by_email(normalized) is not None:
            raise ValueError("Email is already in use")

        now = _utcnow()
        password_state = hash_password(password)
        record = {
            "uuid": str(uuid4()),
            "github_id": None,
            "email": normalized,
            "name": normalized_name,
            "avatar_url": None,
            "bio": None,
            "timezone": "UTC",
            "preferences": {},
            "password_salt": password_state.salt_hex,
            "password_hash": password_state.hash_hex,
            "password_iterations": password_state.iterations,
            "is_admin": is_admin,
            "created_at": now,
            "updated_at": now,
        }
        created = _normalize_records(
            await self._client.execute_query("CREATE users CONTENT $record;", record=record)
        )
        if not created:
            msg = "Failed to create local user"
            raise RuntimeError(msg)
        return _user_from_record(created[0])

    async def authenticate_local(self, *, email: str, password: str) -> AuthUser | None:
        record = await self._select_one(
            "SELECT * FROM users WHERE email = $email AND deleted_at = NONE LIMIT 1;",
            email=email.strip().lower(),
        )
        if record is None:
            return None
        if (
            not record.get("password_salt")
            or not record.get("password_hash")
            or not record.get("password_iterations")
        ):
            return None
        try:
            ok = verify_password(
                password,
                salt_hex=str(record["password_salt"]),
                hash_hex=str(record["password_hash"]),
                iterations=_coerce_int(
                    record.get("password_iterations"),
                    field_name="user.password_iterations",
                ),
            )
        except PasswordError:
            return None
        return _user_from_record(record) if ok else None

    async def update_profile(
        self,
        user: AuthUser,
        *,
        email: str | None = None,
        name: str | None = None,
        avatar_url: str | None = None,
    ) -> AuthUser:
        record = await self._select_one(
            "SELECT * FROM users WHERE uuid = $uuid LIMIT 1;", uuid=str(user.id)
        )
        if record is None:
            msg = f"User not found: {user.id}"
            raise LookupError(msg)

        updated = dict(record)
        if email is not None:
            normalized_email = email.strip().lower()
            if not normalized_email:
                raise ValueError("Email is required")
            existing = await self.get_by_email(normalized_email)
            if existing is not None and existing.id != user.id:
                raise ValueError("Email is already in use")
            updated["email"] = normalized_email
        if name is not None:
            normalized_name = name.strip()
            if not normalized_name:
                raise ValueError("Name is required")
            updated["name"] = normalized_name
        if avatar_url is not None:
            updated["avatar_url"] = avatar_url.strip() or None
        updated["updated_at"] = _utcnow()

        written = await self._replace("users", uuid=user.id, record=updated)
        return _user_from_record(written)

    async def change_password(self, user: AuthUser, change: PasswordChange) -> AuthUser:
        record = await self._select_one(
            "SELECT * FROM users WHERE uuid = $uuid LIMIT 1;", uuid=str(user.id)
        )
        if record is None:
            msg = f"User not found: {user.id}"
            raise LookupError(msg)

        if (
            record.get("password_salt")
            and record.get("password_hash")
            and record.get("password_iterations")
        ):
            if not change.current_password:
                raise ValueError("Current password is required")
            try:
                ok = verify_password(
                    change.current_password,
                    salt_hex=str(record["password_salt"]),
                    hash_hex=str(record["password_hash"]),
                    iterations=_coerce_int(
                        record.get("password_iterations"),
                        field_name="user.password_iterations",
                    ),
                )
            except PasswordError as e:
                raise ValueError("Invalid current password") from e
            if not ok:
                raise ValueError("Invalid current password")

        password_state = hash_password(change.new_password)
        updated = {
            **record,
            "password_salt": password_state.salt_hex,
            "password_hash": password_state.hash_hex,
            "password_iterations": password_state.iterations,
            "updated_at": _utcnow(),
        }
        written = await self._replace("users", uuid=user.id, record=updated)
        return _user_from_record(written)


class SurrealOrganizationRepository(_SurrealAuthRepository):
    """OrganizationRepository backed by the shared Surreal auth namespace."""

    @classmethod
    def from_client(cls, client: SurrealAuthClient) -> Self:
        return cls(client)

    async def get_by_id(self, org_id: UUID) -> AuthOrganization | None:
        record = await self._select_one(
            "SELECT * FROM organizations WHERE uuid = $uuid LIMIT 1;",
            uuid=str(org_id),
        )
        return _organization_from_record(record) if record is not None else None

    async def get_by_slug(self, slug: str) -> AuthOrganization | None:
        record = await self._select_one(
            "SELECT * FROM organizations WHERE slug = $slug LIMIT 1;",
            slug=slug,
        )
        return _organization_from_record(record) if record is not None else None

    async def list_all(self, limit: int = 100) -> list[AuthOrganization]:
        records = await self._select_many(
            "SELECT * FROM organizations ORDER BY created_at ASC LIMIT $limit;",
            limit=int(limit),
        )
        return [_organization_from_record(record) for record in records]

    async def create(
        self,
        *,
        name: str,
        slug: str | None = None,
        is_personal: bool = False,
        settings: dict[str, object] | None = None,
    ) -> AuthOrganization:
        now = _utcnow()
        record = {
            "uuid": str(uuid4()),
            "name": name,
            "slug": slugify(slug or name),
            "is_personal": is_personal,
            "settings": dict(settings or {}),
            "created_at": now,
            "updated_at": now,
        }
        created = _normalize_records(
            await self._client.execute_query("CREATE organizations CONTENT $record;", record=record)
        )
        if not created:
            msg = "Failed to create organization"
            raise RuntimeError(msg)
        return _organization_from_record(created[0])

    async def update(
        self,
        organization: AuthOrganization,
        *,
        name: str | None = None,
        slug: str | None = None,
        settings: dict[str, object] | None = None,
    ) -> AuthOrganization:
        record = await self._select_one(
            "SELECT * FROM organizations WHERE uuid = $uuid LIMIT 1;",
            uuid=str(organization.id),
        )
        if record is None:
            msg = f"Organization not found: {organization.id}"
            raise LookupError(msg)

        updated = dict(record)
        if name is not None:
            updated["name"] = name
        if slug is not None:
            updated["slug"] = slugify(slug)
        if settings is not None:
            updated["settings"] = dict(settings)
        updated["updated_at"] = _utcnow()

        written = await self._replace("organizations", uuid=organization.id, record=updated)
        return _organization_from_record(written)

    async def delete(self, organization: AuthOrganization) -> None:
        await self._client.execute_query(
            "DELETE FROM organizations WHERE uuid = $uuid;",
            uuid=str(organization.id),
        )

    async def create_personal_for_user(self, user: AuthUser) -> AuthOrganization:
        suffix = str(user.github_id) if user.github_id is not None else str(user.id)
        slug = f"u-{suffix}"
        existing = await self.get_by_slug(slug)
        if existing is not None:
            return existing
        return await self.create(
            name=user.name or f"User {suffix}",
            slug=slug,
            is_personal=True,
            settings={},
        )


class SurrealOrganizationMembershipRepository(_SurrealAuthRepository):
    """OrganizationMembershipRepository backed by the shared Surreal auth namespace."""

    @classmethod
    def from_client(cls, client: SurrealAuthClient) -> Self:
        return cls(client)

    async def get(self, membership_id: UUID) -> AuthMembership | None:
        record = await self._select_one(
            "SELECT * FROM organization_members WHERE uuid = $uuid LIMIT 1;",
            uuid=str(membership_id),
        )
        return _membership_from_record(record) if record is not None else None

    async def get_for_user(self, organization_id: UUID, user_id: UUID) -> AuthMembership | None:
        record = await self._select_one(
            "SELECT * FROM organization_members "
            "WHERE organization_id = $organization_id AND user_id = $user_id "
            "LIMIT 1;",
            organization_id=str(organization_id),
            user_id=str(user_id),
        )
        return _membership_from_record(record) if record is not None else None

    async def list_for_org(self, organization_id: UUID) -> list[AuthMembership]:
        records = await self._select_many(
            "SELECT * FROM organization_members WHERE organization_id = $organization_id "
            "ORDER BY created_at ASC;",
            organization_id=str(organization_id),
        )
        return [_membership_from_record(record) for record in records]

    async def add_member(
        self,
        *,
        organization_id: UUID,
        user_id: UUID,
        role: OrganizationRole = OrganizationRole.MEMBER,
    ) -> AuthMembership:
        existing = await self._select_one(
            "SELECT * FROM organization_members "
            "WHERE organization_id = $organization_id AND user_id = $user_id "
            "LIMIT 1;",
            organization_id=str(organization_id),
            user_id=str(user_id),
        )
        if existing is not None:
            updated = {**existing, "role": role.value, "updated_at": _utcnow()}
            written = await self._replace(
                "organization_members",
                uuid=_coerce_uuid(updated.get("uuid"), field_name="membership.uuid"),
                record=updated,
            )
            return _membership_from_record(written)

        now = _utcnow()
        record = {
            "uuid": str(uuid4()),
            "organization_id": str(organization_id),
            "user_id": str(user_id),
            "role": role.value,
            "created_at": now,
            "updated_at": now,
        }
        created = _normalize_records(
            await self._client.execute_query(
                "CREATE organization_members CONTENT $record;", record=record
            )
        )
        if not created:
            msg = "Failed to create organization membership"
            raise RuntimeError(msg)
        return _membership_from_record(created[0])

    async def remove_member(self, *, organization_id: UUID, user_id: UUID) -> None:
        membership = await self.get_for_user(organization_id, user_id)
        if membership is None:
            return
        if membership.role is OrganizationRole.OWNER:
            owners = await self._count_owners(organization_id)
            if owners <= 1:
                raise ValueError("Cannot remove the last organization owner")
        await self._client.execute_query(
            "DELETE FROM organization_members "
            "WHERE organization_id = $organization_id AND user_id = $user_id;",
            organization_id=str(organization_id),
            user_id=str(user_id),
        )

    async def set_role(
        self,
        *,
        organization_id: UUID,
        user_id: UUID,
        role: OrganizationRole,
    ) -> AuthMembership:
        existing = await self.get_for_user(organization_id, user_id)
        if existing is None:
            raise ValueError("User is not a member of this organization")
        if existing.role is OrganizationRole.OWNER and role is not OrganizationRole.OWNER:
            owners = await self._count_owners(organization_id)
            if owners <= 1:
                raise ValueError("Cannot demote the last organization owner")

        record = await self._select_one(
            "SELECT * FROM organization_members "
            "WHERE organization_id = $organization_id AND user_id = $user_id "
            "LIMIT 1;",
            organization_id=str(organization_id),
            user_id=str(user_id),
        )
        if record is None:
            raise ValueError("User is not a member of this organization")

        updated = {**record, "role": role.value, "updated_at": _utcnow()}
        written = await self._replace(
            "organization_members",
            uuid=_coerce_uuid(updated.get("uuid"), field_name="membership.uuid"),
            record=updated,
        )
        return _membership_from_record(written)

    async def _count_owners(self, organization_id: UUID) -> int:
        memberships = await self.list_for_org(organization_id)
        return sum(1 for membership in memberships if membership.role is OrganizationRole.OWNER)


class SurrealAuthContextResolver(RepositoryAuthContextResolver):
    """Build AuthContext using the Surreal-backed repositories."""

    def __init__(
        self,
        *,
        client: SurrealAuthClient,
        users: SurrealUserRepository,
        organizations: SurrealOrganizationRepository,
        memberships: SurrealOrganizationMembershipRepository,
    ) -> None:
        super().__init__(
            users=users,
            organizations=organizations,
            memberships=memberships,
        )
        self._client = client

    @classmethod
    def from_client(cls, client: SurrealAuthClient) -> Self:
        return cls(
            client=client,
            users=SurrealUserRepository.from_client(client),
            organizations=SurrealOrganizationRepository.from_client(client),
            memberships=SurrealOrganizationMembershipRepository.from_client(client),
        )

    async def resolve(self, claims: Mapping[str, object]) -> AuthContext:
        try:
            user_id = UUID(str(claims.get("sub", "")))
        except ValueError as e:
            raise InvalidAuthClaimsError("Invalid token") from e

        organization_id = self._parse_optional_uuid(claims.get("org"))
        params: dict[str, str] = {"user_id": str(user_id)}

        if organization_id is None:
            query = """
                RETURN {
                    user: (
                        SELECT * FROM users
                        WHERE uuid = $user_id AND deleted_at = NONE
                        LIMIT 1
                    )[0],
                    organization: NONE,
                    membership: NONE,
                };
            """
        else:
            params["organization_id"] = str(organization_id)
            query = """
                RETURN {
                    user: (
                        SELECT * FROM users
                        WHERE uuid = $user_id AND deleted_at = NONE
                        LIMIT 1
                    )[0],
                    organization: (
                        SELECT * FROM organizations WHERE uuid = $organization_id LIMIT 1
                    )[0],
                    membership: (
                        SELECT * FROM organization_members
                        WHERE organization_id = $organization_id AND user_id = $user_id
                        LIMIT 1
                    )[0],
                };
            """

        payload = _object_mapping(await self._client.execute_query(query, **params))

        user_record = _normalize_record(payload.get("user"))
        if user_record is None:
            msg = f"User not found: {user_id}"
            raise UserNotFoundError(msg)

        organization_record = _normalize_record(payload.get("organization"))
        membership_record = _normalize_record(payload.get("membership"))
        scope_values = claims.get("scopes", [])
        scopes = (
            frozenset(str(scope) for scope in scope_values)
            if isinstance(scope_values, list | tuple | set | frozenset)
            else frozenset()
        )
        api_key_project_ids = _claim_string_set(claims.get("api_key_project_ids"))
        api_key_memory_space_ids = _claim_string_set(claims.get("api_key_memory_space_ids"))
        api_key_memory_scope_keys = _claim_string_set(claims.get("api_key_memory_scope_keys"))

        return AuthContext(
            user=_user_from_record(user_record),
            organization=_organization_from_record(organization_record)
            if organization_record is not None
            else None,
            org_role=_membership_from_record(membership_record).role
            if membership_record is not None
            else None,
            scopes=scopes,
            api_key_id=_claim_optional_str(claims.get("api_key_id")),
            api_key_project_ids=api_key_project_ids,
            api_key_memory_space_ids=api_key_memory_space_ids,
            api_key_memory_scope_keys=api_key_memory_scope_keys,
        )
