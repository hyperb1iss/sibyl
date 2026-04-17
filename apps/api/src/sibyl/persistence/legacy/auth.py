"""Legacy auth adapters that satisfy the backend-agnostic contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Self
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from sibyl.auth.memberships import OrganizationMembershipManager
from sibyl.auth.organizations import OrganizationManager
from sibyl.auth.sessions import SessionManager
from sibyl.auth.users import UserManager
from sibyl.db.models import (
    Organization,
    OrganizationMember,
    OrganizationRole as LegacyOrgRole,
    User,
    UserSession,
)
from sibyl_core.auth import (
    AuthContext,
    AuthMembership,
    AuthOrganization,
    AuthSession,
    AuthUser,
    GitHubUserIdentity,
    OrganizationMembershipRepository,
    OrganizationRepository,
    OrganizationRole,
    PasswordChange,
    SessionRepository,
    UserRepository,
)
from sibyl_core.auth.models import (
    coerce_auth_membership,
    coerce_auth_organization,
    coerce_auth_session,
    coerce_auth_user,
)


class InvalidAuthClaimsError(ValueError):
    """JWT/API-key claims are present but unusable."""


class UserNotFoundError(LookupError):
    """Claims referenced a user that no longer exists."""


async def authenticate_legacy_api_key(raw_key: str):
    """Authenticate an API key via the current relational auth runtime."""
    from sibyl.auth.api_keys import ApiKeyManager
    from sibyl.db.connection import get_session

    async with get_session() as session:
        return await ApiKeyManager.from_session(session).authenticate(raw_key)


async def resolve_legacy_accessible_project_graph_ids(
    *,
    user_id: str,
    org_id: str,
    scopes: Sequence[str] | None = None,
    api_key_project_ids: Sequence[str] | None = None,
) -> set[str] | None:
    """Resolve project graph IDs accessible to the given user in an organization."""
    from sibyl.auth.authorization import list_accessible_project_graph_ids
    from sibyl.db.connection import get_session

    async with get_session() as session:
        resolver = LegacyAuthContextResolver.from_session(session)
        try:
            auth_ctx = await resolver.resolve(
                {
                    "sub": user_id,
                    "org": org_id,
                    "scopes": list(scopes or []),
                }
            )
        except UserNotFoundError:
            return set()
        if auth_ctx.organization is None:
            return set()

        user_accessible = await list_accessible_project_graph_ids(session, auth_ctx)

    if api_key_project_ids is not None:
        api_key_allowed = set(api_key_project_ids)
        if user_accessible is None:
            return api_key_allowed
        return user_accessible & api_key_allowed

    return user_accessible


async def has_legacy_owner_membership(*, org_id: str, user_id: str | None) -> bool:
    """Return whether the user is an owner in the current organization."""
    from sibyl.db.connection import get_session

    if user_id is None:
        return False

    async with get_session() as session:
        membership = await OrganizationMembershipManager.from_session(session).get_for_user(
            UUID(org_id),
            UUID(user_id),
        )
    return bool(membership and getattr(membership.role, "value", membership.role) == "owner")


async def list_legacy_accessible_project_graph_ids(
    ctx: AuthContext,
) -> set[str] | None:
    """Resolve accessible project graph IDs for an authenticated web context."""
    from sibyl.auth.authorization import list_accessible_project_graph_ids
    from sibyl.db.connection import get_session

    async with get_session() as session:
        return await list_accessible_project_graph_ids(session, ctx)


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
) -> UserSession:
    """Create a legacy session record in a fresh relational session."""
    from sibyl.db.connection import get_session

    async with get_session() as session:
        return await SessionManager(session).create_session(
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


async def load_legacy_refresh_session_record(refresh_token: str) -> UserSession | None:
    """Load a legacy session by refresh token."""
    from sibyl.db.connection import get_session

    async with get_session() as session:
        return await SessionManager(session).get_session_by_refresh_token(refresh_token)


async def rotate_legacy_refresh_session_record(
    refresh_token: str,
    *,
    new_access_token: str,
    new_access_expires_at,
    new_refresh_token: str,
    new_refresh_expires_at,
) -> UserSession | None:
    """Rotate a legacy session's access and refresh tokens."""
    from sibyl.db.connection import get_session

    async with get_session() as session:
        manager = SessionManager(session)
        existing = await manager.get_session_by_refresh_token(refresh_token)
        if existing is None:
            return None
        return await manager.rotate_tokens(
            existing,
            new_access_token=new_access_token,
            new_access_expires_at=new_access_expires_at,
            new_refresh_token=new_refresh_token,
            new_refresh_expires_at=new_refresh_expires_at,
        )


async def revoke_legacy_refresh_session_record(refresh_token: str) -> None:
    """Best-effort revoke a legacy session identified by refresh token."""
    from sibyl.db.connection import get_session

    async with get_session() as session:
        existing = await SessionManager(session).get_session_by_refresh_token(refresh_token)
        if existing is not None:
            existing.revoked_at = datetime.now(UTC).replace(tzinfo=None)


async def authenticate_legacy_local_user(*, email: str, password: str) -> User | None:
    """Authenticate a local legacy user by email and password."""
    from sibyl.db.connection import get_session

    async with get_session() as session:
        return await UserManager(session).authenticate_local(email=email, password=password)


async def get_legacy_user_by_id(user_id: UUID) -> User | None:
    """Load a legacy user by identifier."""
    from sibyl.db.connection import get_session

    async with get_session() as session:
        return await UserManager(session).get_by_id(user_id)


async def list_legacy_user_organizations(*, user_id: UUID) -> list[Organization]:
    """List organizations the legacy user belongs to."""
    from sibyl.db.connection import get_session

    async with get_session() as session:
        result = await session.execute(
            select(Organization)
            .join(OrganizationMember, col(OrganizationMember.organization_id) == col(Organization.id))
            .where(col(OrganizationMember.user_id) == user_id)
            .order_by(col(Organization.is_personal).desc(), col(Organization.name).asc())
        )
        return list(result.scalars().all())


async def ensure_legacy_personal_organization(*, user_id: UUID) -> Organization | None:
    """Ensure the user has a personal organization and owner membership."""
    from sibyl.db.connection import get_session

    async with get_session() as session:
        user = await UserManager(session).get_by_id(user_id)
        if user is None:
            return None

        organization = await OrganizationManager(session).create_personal_for_user(user)
        await OrganizationMembershipManager(session).add_member(
            organization_id=organization.id,
            user_id=user.id,
            role=LegacyOrgRole.OWNER,
        )
        return organization


async def list_legacy_api_keys_for_user(
    *,
    organization_id: UUID,
    user_id: UUID,
):
    """List API keys visible to the user in an organization."""
    from sibyl.auth.api_keys import ApiKeyManager
    from sibyl.db.connection import get_session

    async with get_session() as session:
        return await ApiKeyManager(session).list_for_user(
            organization_id=organization_id,
            user_id=user_id,
        )


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
    """Create and audit a legacy API key."""
    from sibyl.auth.api_keys import ApiKeyManager
    from sibyl.auth.audit import AuditLogger
    from sibyl.db.connection import get_session

    async with get_session() as session:
        record, raw = await ApiKeyManager(session).create(
            organization_id=organization_id,
            user_id=user_id,
            name=name,
            live=live,
            scopes=scopes,
            expires_at=expires_at,
        )
        await AuditLogger(session).log(
            action="auth.api_key.create",
            user_id=user_id,
            organization_id=organization_id,
            request=request,
            details={"api_key_id": str(record.id), "name": record.name, "prefix": record.key_prefix},
        )
        return record, raw


async def revoke_legacy_api_key_for_user(
    *,
    api_key_id: UUID,
    organization_id: UUID,
    actor_user_id: UUID,
    actor_org_role: OrganizationRole | None,
    request,
) -> None:
    """Revoke and audit a legacy API key when the actor is authorized."""
    from sibyl.auth.api_keys import ApiKeyManager
    from sibyl.auth.audit import AuditLogger
    from sibyl.db.connection import get_session
    from sibyl.db.models import ApiKey

    async with get_session() as session:
        key = await session.get(ApiKey, api_key_id)
        if key is None or key.organization_id != organization_id:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="API key not found")

        if key.user_id != actor_user_id and actor_org_role not in {
            OrganizationRole.OWNER,
            OrganizationRole.ADMIN,
        }:
            from fastapi import HTTPException

            raise HTTPException(status_code=403, detail="Forbidden")

        await ApiKeyManager(session).revoke(api_key_id)
        await AuditLogger(session).log(
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
) -> User:
    """Update a legacy auth user profile and audit the changes."""
    from fastapi import HTTPException

    from sibyl.auth.audit import AuditLogger
    from sibyl.auth.users import PasswordChange
    from sibyl.db.connection import get_session

    changes: list[str] = []
    if email is not None:
        changes.append("email")
    if name is not None:
        changes.append("name")
    if avatar_url is not None:
        changes.append("avatar_url")
    if new_password is not None:
        changes.append("password")
    if not changes:
        raise HTTPException(status_code=400, detail="No fields to update")

    async with get_session() as session:
        manager = UserManager(session)
        user = await manager.get_by_id(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        try:
            await manager.update_profile(
                user,
                email=email,
                name=name,
                avatar_url=avatar_url,
            )
            if new_password is not None:
                await manager.change_password(
                    user,
                    PasswordChange(
                        current_password=current_password,
                        new_password=new_password,
                    ),
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if any(change != "password" for change in changes):
            await AuditLogger(session).log(
                action="user.update_profile",
                user_id=user.id,
                organization_id=organization_id,
                request=request,
                details={"fields": [change for change in changes if change != "password"]},
            )
        if "password" in changes:
            await AuditLogger(session).log(
                action="user.change_password",
                user_id=user.id,
                organization_id=organization_id,
                request=request,
                details={},
            )

        return user


def _to_auth_user(value: object | None) -> AuthUser | None:
    if value is None:
        return None
    return coerce_auth_user(value)


def _to_auth_organization(value: object | None) -> AuthOrganization | None:
    if value is None:
        return None
    return coerce_auth_organization(value)


def _to_auth_membership(value: object | None) -> AuthMembership | None:
    if value is None:
        return None
    return coerce_auth_membership(value)


def _to_auth_session(value: object | None) -> AuthSession | None:
    if value is None:
        return None
    return coerce_auth_session(value)


def _require_auth_organization(value: object | None) -> AuthOrganization:
    organization = _to_auth_organization(value)
    if organization is None:
        msg = "Organization coercion returned no result"
        raise TypeError(msg)
    return organization


def _require_auth_membership(value: object | None) -> AuthMembership:
    membership = _to_auth_membership(value)
    if membership is None:
        msg = "Membership coercion returned no result"
        raise TypeError(msg)
    return membership


def _require_auth_session(value: object | None) -> AuthSession:
    session = _to_auth_session(value)
    if session is None:
        msg = "Session coercion returned no result"
        raise TypeError(msg)
    return session


class LegacyUserRepository(UserRepository):
    """UserRepository backed by the current SQLModel auth manager."""

    def __init__(self, manager: UserManager) -> None:
        self._manager = manager

    @classmethod
    def from_session(cls, session: AsyncSession) -> Self:
        return cls(UserManager(session))

    async def get_by_id(self, user_id: UUID) -> AuthUser | None:
        return _to_auth_user(await self._manager.get_by_id(user_id))

    async def has_any_users(self) -> bool:
        return await self._manager.has_any_users()

    async def get_by_github_id(self, github_id: int) -> AuthUser | None:
        return _to_auth_user(await self._manager.get_by_github_id(github_id))

    async def get_by_email(self, email: str) -> AuthUser | None:
        return _to_auth_user(await self._manager.get_by_email(email))

    async def upsert_from_github(
        self, identity: GitHubUserIdentity, *, is_admin: bool = False
    ) -> AuthUser:
        return coerce_auth_user(
            await self._manager.upsert_from_github(identity, is_admin=is_admin)
        )

    async def create_local_user(
        self, *, email: str, password: str, name: str, is_admin: bool = False
    ) -> AuthUser:
        return coerce_auth_user(
            await self._manager.create_local_user(
                email=email,
                password=password,
                name=name,
                is_admin=is_admin,
            )
        )

    async def authenticate_local(self, *, email: str, password: str) -> AuthUser | None:
        return _to_auth_user(
            await self._manager.authenticate_local(email=email, password=password)
        )

    async def update_profile(
        self,
        user: AuthUser,
        *,
        email: str | None = None,
        name: str | None = None,
        avatar_url: str | None = None,
    ) -> AuthUser:
        user_row = await self._require_user_row(user.id)
        return coerce_auth_user(
            await self._manager.update_profile(
                user_row,
                email=email,
                name=name,
                avatar_url=avatar_url,
            )
        )

    async def change_password(self, user: AuthUser, change: PasswordChange) -> AuthUser:
        user_row = await self._require_user_row(user.id)
        return coerce_auth_user(await self._manager.change_password(user_row, change))

    async def _require_user_row(self, user_id: UUID) -> User:
        user = await self._manager.get_by_id(user_id)
        if user is None:
            msg = f"User not found: {user_id}"
            raise UserNotFoundError(msg)
        return user


class LegacyOrganizationRepository(OrganizationRepository):
    """OrganizationRepository backed by the current SQLModel auth managers."""

    def __init__(self, manager: OrganizationManager, user_manager: UserManager) -> None:
        self._manager = manager
        self._user_manager = user_manager

    @classmethod
    def from_session(cls, session: AsyncSession) -> Self:
        return cls(OrganizationManager(session), UserManager(session))

    async def get_by_id(self, org_id: UUID) -> AuthOrganization | None:
        return _to_auth_organization(await self._manager.get_by_id(org_id))

    async def get_by_slug(self, slug: str) -> AuthOrganization | None:
        return _to_auth_organization(await self._manager.get_by_slug(slug))

    async def list_all(self, limit: int = 100) -> list[AuthOrganization]:
        return [
            _require_auth_organization(org)
            for org in await self._manager.list_all(limit)
            if org is not None
        ]

    async def create(
        self,
        *,
        name: str,
        slug: str | None = None,
        is_personal: bool = False,
        settings: dict[str, object] | None = None,
    ) -> AuthOrganization:
        return _require_auth_organization(
            await self._manager.create(
                name=name,
                slug=slug,
                is_personal=is_personal,
                settings=settings,
            )
        )

    async def update(
        self,
        organization: AuthOrganization,
        *,
        name: str | None = None,
        slug: str | None = None,
        settings: dict[str, object] | None = None,
    ) -> AuthOrganization:
        org_row = await self._require_org_row(organization.id)
        return _require_auth_organization(
            await self._manager.update(org_row, name=name, slug=slug, settings=settings)
        )

    async def delete(self, organization: AuthOrganization) -> None:
        org_row = await self._require_org_row(organization.id)
        await self._manager.delete(org_row)

    async def create_personal_for_user(self, user: AuthUser) -> AuthOrganization:
        user_row = await self._require_user_row(user.id)
        return _require_auth_organization(await self._manager.create_personal_for_user(user_row))

    async def _require_org_row(self, organization_id: UUID) -> Organization:
        organization = await self._manager.get_by_id(organization_id)
        if organization is None:
            msg = f"Organization not found: {organization_id}"
            raise LookupError(msg)
        return organization

    async def _require_user_row(self, user_id: UUID) -> User:
        user = await self._user_manager.get_by_id(user_id)
        if user is None:
            msg = f"User not found: {user_id}"
            raise UserNotFoundError(msg)
        return user


class LegacyOrganizationMembershipRepository(OrganizationMembershipRepository):
    """OrganizationMembershipRepository backed by the current SQLModel manager."""

    def __init__(self, manager: OrganizationMembershipManager) -> None:
        self._manager = manager

    @classmethod
    def from_session(cls, session: AsyncSession) -> Self:
        return cls(OrganizationMembershipManager(session))

    async def get(self, membership_id: UUID) -> AuthMembership | None:
        return _to_auth_membership(await self._manager.get(membership_id))

    async def get_for_user(self, organization_id: UUID, user_id: UUID) -> AuthMembership | None:
        return _to_auth_membership(await self._manager.get_for_user(organization_id, user_id))

    async def list_for_org(self, organization_id: UUID) -> list[AuthMembership]:
        return [
            _require_auth_membership(membership)
            for membership in await self._manager.list_for_org(organization_id)
            if membership is not None
        ]

    async def add_member(
        self,
        *,
        organization_id: UUID,
        user_id: UUID,
        role: OrganizationRole = OrganizationRole.MEMBER,
    ) -> AuthMembership:
        return _require_auth_membership(
            await self._manager.add_member(
                organization_id=organization_id,
                user_id=user_id,
                role=role,
            )
        )

    async def remove_member(self, *, organization_id: UUID, user_id: UUID) -> None:
        await self._manager.remove_member(organization_id=organization_id, user_id=user_id)

    async def set_role(
        self, *, organization_id: UUID, user_id: UUID, role: OrganizationRole
    ) -> AuthMembership:
        return _require_auth_membership(
            await self._manager.set_role(
                organization_id=organization_id,
                user_id=user_id,
                role=role,
            )
        )


class LegacySessionRepository(SessionRepository):
    """SessionRepository backed by the current SQLModel manager."""

    def __init__(self, manager: SessionManager) -> None:
        self._manager = manager
        self._session = manager.session

    @classmethod
    def from_session(cls, session: AsyncSession) -> Self:
        return cls(SessionManager(session))

    @staticmethod
    def hash_token(token: str) -> str:
        return SessionManager.hash_token(token)

    async def create_session(
        self,
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
    ) -> AuthSession:
        return _require_auth_session(
            await self._manager.create_session(
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
        )

    async def get_session_by_token(self, token: str) -> AuthSession | None:
        return _to_auth_session(await self._manager.get_session_by_token(token))

    async def get_session_by_refresh_token(self, refresh_token: str) -> AuthSession | None:
        return _to_auth_session(await self._manager.get_session_by_refresh_token(refresh_token))

    async def rotate_tokens(
        self,
        session: AuthSession,
        *,
        new_access_token: str,
        new_access_expires_at,
        new_refresh_token: str,
        new_refresh_expires_at,
    ) -> AuthSession:
        session_row = await self._require_session_row(session.id)
        return _require_auth_session(
            await self._manager.rotate_tokens(
                session_row,
                new_access_token=new_access_token,
                new_access_expires_at=new_access_expires_at,
                new_refresh_token=new_refresh_token,
                new_refresh_expires_at=new_refresh_expires_at,
            )
        )

    async def list_user_sessions(
        self, user_id: UUID, *, include_expired: bool = False
    ) -> list[AuthSession]:
        return [
            _require_auth_session(session)
            for session in await self._manager.list_user_sessions(
                user_id,
                include_expired=include_expired,
            )
            if session is not None
        ]

    async def update_activity(self, token: str) -> bool:
        return await self._manager.update_activity(token)

    async def mark_current(self, token: str) -> bool:
        return await self._manager.mark_current(token)

    async def revoke_session(self, session_id: UUID, user_id: UUID) -> bool:
        return await self._manager.revoke_session(session_id, user_id)

    async def revoke_all_sessions(
        self, user_id: UUID, *, exclude_token_hash: str | None = None
    ) -> int:
        return await self._manager.revoke_all_sessions(
            user_id,
            exclude_token_hash=exclude_token_hash,
        )

    async def cleanup_expired(self, *, older_than_days: int = 30) -> int:
        return await self._manager.cleanup_expired(older_than_days=older_than_days)

    async def _require_session_row(self, session_id: UUID) -> UserSession:
        session = await self._session.get(UserSession, session_id)
        if session is None:
            msg = f"Session not found: {session_id}"
            raise LookupError(msg)
        return session


class LegacyAuthContextResolver:
    """Build AuthContext using the legacy repositories instead of direct ORM access."""

    def __init__(
        self,
        users: UserRepository,
        organizations: OrganizationRepository,
        memberships: OrganizationMembershipRepository,
    ) -> None:
        self._users = users
        self._organizations = organizations
        self._memberships = memberships

    @classmethod
    def from_session(cls, session: AsyncSession) -> Self:
        return cls(
            users=LegacyUserRepository.from_session(session),
            organizations=LegacyOrganizationRepository.from_session(session),
            memberships=LegacyOrganizationMembershipRepository.from_session(session),
        )

    async def resolve(self, claims: Mapping[str, Any]) -> AuthContext:
        user_id = self._parse_subject(claims)
        user = await self._users.get_by_id(user_id)
        if user is None:
            msg = f"User not found: {user_id}"
            raise UserNotFoundError(msg)

        organization = None
        membership = None
        raw_org_id = claims.get("org")
        if raw_org_id:
            organization_id = self._parse_optional_uuid(raw_org_id)
            if organization_id is not None:
                organization = await self._organizations.get_by_id(organization_id)
                if organization is not None:
                    membership = await self._memberships.get_for_user(organization.id, user.id)

        scopes = frozenset(str(scope) for scope in claims.get("scopes", []))
        return AuthContext(
            user=user,
            organization=organization,
            org_role=membership.role if membership is not None else None,
            scopes=scopes,
        )

    def _parse_subject(self, claims: Mapping[str, Any]) -> UUID:
        try:
            return UUID(str(claims.get("sub", "")))
        except ValueError as e:
            raise InvalidAuthClaimsError("Invalid token") from e

    def _parse_optional_uuid(self, value: object) -> UUID | None:
        try:
            return UUID(str(value))
        except ValueError:
            return None
