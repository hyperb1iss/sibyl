"""Legacy organization adapters backed by the current auth runtime."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from fastapi import HTTPException, status
from sqlmodel import col, select
from starlette.requests import Request

from sibyl import config as config_module
from sibyl.auth.audit import AuditLogger
from sibyl.auth.http import select_access_token
from sibyl.auth.jwt import create_access_token, create_refresh_token
from sibyl.auth.memberships import OrganizationMembershipManager
from sibyl.auth.organizations import OrganizationManager, slugify
from sibyl.auth.sessions import SessionManager
from sibyl.db.connection import get_session
from sibyl.db.models import Organization, OrganizationMember
from sibyl.persistence.legacy.graph import ensure_graph_indexes as _service_ensure_graph_indexes
from sibyl.persistence.organization_common import (
    OrgAuthResult,
    OrgRoleResult,
    OrgSummary,
)
from sibyl_core.auth import OrganizationRole

log = structlog.get_logger()

LegacyOrgSummary = OrgSummary
LegacyOrgAuthResult = OrgAuthResult
LegacyOrgRoleResult = OrgRoleResult


async def ensure_legacy_graph_indexes(group_id: str) -> None:
    await _service_ensure_graph_indexes(group_id)


async def ensure_graph_indexes(group_id: str) -> None:
    await ensure_legacy_graph_indexes(group_id)


async def _rotate_or_create_org_session(
    *,
    session_manager: SessionManager,
    request: Request,
    user_id: UUID,
    organization_id: UUID,
    access_token: str,
    refresh_token: str,
    refresh_expires: datetime,
) -> None:
    current = select_access_token(
        authorization=request.headers.get("authorization"),
        cookie_token=request.cookies.get("sibyl_access_token"),
    )
    access_expires = datetime.now(UTC) + timedelta(
        minutes=config_module.settings.access_token_expire_minutes
    )
    if not current:
        return

    existing = await session_manager.get_session_by_token(current)
    if existing is not None:
        await session_manager.rotate_tokens(
            existing,
            new_access_token=access_token,
            new_access_expires_at=access_expires,
            new_refresh_token=refresh_token,
            new_refresh_expires_at=refresh_expires,
        )
        return

    await session_manager.create_session(
        user_id=user_id,
        organization_id=organization_id,
        token=access_token,
        expires_at=access_expires,
        refresh_token=refresh_token,
        refresh_token_expires_at=refresh_expires,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


async def _get_org_and_membership(
    *,
    slug: str,
    user_id: UUID,
    allow_admin: bool = False,
) -> tuple[Organization, OrganizationMember]:
    async with get_session() as session:
        org = await OrganizationManager(session).get_by_slug(slug)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

        member = await OrganizationMembershipManager(session).get_for_user(org.id, user_id)
        if member is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        if allow_admin:
            if member.role not in {OrganizationRole.OWNER, OrganizationRole.ADMIN}:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        return org, member


async def list_legacy_orgs(*, user_id: UUID) -> list[LegacyOrgSummary]:
    async with get_session() as session:
        result = await session.execute(
            select(Organization, OrganizationMember.role)
            .join(
                OrganizationMember,
                col(OrganizationMember.organization_id) == col(Organization.id),
            )
            .where(col(OrganizationMember.user_id) == user_id)
            .order_by(col(Organization.slug).asc())
        )
        return [
            LegacyOrgSummary(
                id=org.id,
                slug=org.slug,
                name=org.name,
                is_personal=org.is_personal,
                role=role,
            )
            for org, role in result.all()
        ]


async def list_legacy_org_ids() -> list[str]:
    async with get_session() as session:
        result = await session.execute(
            select(Organization.id).order_by(col(Organization.created_at).asc())
        )
        return [str(org_id) for org_id in result.scalars().all()]


async def create_legacy_org(
    *,
    request: Request,
    user_id: UUID,
    name: str,
    slug: str | None = None,
) -> LegacyOrgAuthResult:
    async with get_session() as session:
        org_manager = OrganizationManager(session)
        resolved_slug = slugify(slug or name)

        existing = await org_manager.get_by_slug(resolved_slug)
        if existing is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Slug already taken")

        org = await org_manager.create(name=name, slug=resolved_slug, is_personal=False)
        await OrganizationMembershipManager(session).add_member(
            organization_id=org.id,
            user_id=user_id,
            role=OrganizationRole.OWNER,
        )

        try:
            await ensure_graph_indexes(str(org.id))
        except Exception as exc:
            log.debug("Graph index setup deferred", org_id=str(org.id), error=str(exc))

        access_token = create_access_token(user_id=user_id, organization_id=org.id)
        refresh_token, refresh_expires = create_refresh_token(
            user_id=user_id,
            organization_id=org.id,
        )

        await _rotate_or_create_org_session(
            session_manager=SessionManager(session),
            request=request,
            user_id=user_id,
            organization_id=org.id,
            access_token=access_token,
            refresh_token=refresh_token,
            refresh_expires=refresh_expires,
        )

        await AuditLogger(session).log(
            action="org.create",
            user_id=user_id,
            organization_id=org.id,
            request=request,
            details={"slug": org.slug, "name": org.name},
        )

        return LegacyOrgAuthResult(
            id=org.id,
            slug=org.slug,
            name=org.name,
            access_token=access_token,
            refresh_token=refresh_token,
            refresh_expires=refresh_expires,
        )


async def get_legacy_org(*, slug: str, user_id: UUID) -> LegacyOrgRoleResult:
    org, member = await _get_org_and_membership(slug=slug, user_id=user_id)
    return LegacyOrgRoleResult(
        id=org.id,
        slug=org.slug,
        name=org.name,
        role=member.role,
    )


async def update_legacy_org(
    *,
    request: Request,
    slug: str,
    user_id: UUID,
    name: str | None = None,
    new_slug: str | None = None,
) -> LegacyOrgSummary:
    async with get_session() as session:
        org = await OrganizationManager(session).get_by_slug(slug)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

        member = await OrganizationMembershipManager(session).get_for_user(org.id, user_id)
        if member is None or member.role not in {OrganizationRole.OWNER, OrganizationRole.ADMIN}:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

        resolved_slug = slugify(new_slug) if new_slug else None
        if resolved_slug and resolved_slug != org.slug:
            existing = await OrganizationManager(session).get_by_slug(resolved_slug)
            if existing is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail="Slug already taken"
                )

        updated = await OrganizationManager(session).update(org, name=name, slug=resolved_slug)
        await AuditLogger(session).log(
            action="org.update",
            user_id=user_id,
            organization_id=updated.id,
            request=request,
            details={"slug": slug, "new_slug": updated.slug, "name": updated.name},
        )
        return LegacyOrgSummary(id=updated.id, slug=updated.slug, name=updated.name)


async def delete_legacy_org(*, request: Request, slug: str, user_id: UUID) -> None:
    async with get_session() as session:
        org = await OrganizationManager(session).get_by_slug(slug)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        if org.is_personal:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot delete personal organization",
            )

        member = await OrganizationMembershipManager(session).get_for_user(org.id, user_id)
        if member is None or member.role != OrganizationRole.OWNER:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

        await AuditLogger(session).log(
            action="org.delete",
            user_id=user_id,
            organization_id=org.id,
            request=request,
            details={"slug": org.slug, "name": org.name},
        )
        await OrganizationManager(session).delete(org)


async def switch_legacy_org(
    *,
    request: Request,
    slug: str,
    user_id: UUID,
) -> LegacyOrgAuthResult:
    async with get_session() as session:
        org = await OrganizationManager(session).get_by_slug(slug)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

        member = await OrganizationMembershipManager(session).get_for_user(org.id, user_id)
        if member is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

        access_token = create_access_token(user_id=user_id, organization_id=org.id)
        refresh_token, refresh_expires = create_refresh_token(
            user_id=user_id,
            organization_id=org.id,
        )

        await _rotate_or_create_org_session(
            session_manager=SessionManager(session),
            request=request,
            user_id=user_id,
            organization_id=org.id,
            access_token=access_token,
            refresh_token=refresh_token,
            refresh_expires=refresh_expires,
        )

        await AuditLogger(session).log(
            action="org.switch",
            user_id=user_id,
            organization_id=org.id,
            request=request,
            details={"slug": org.slug, "name": org.name},
        )

        return LegacyOrgAuthResult(
            id=org.id,
            slug=org.slug,
            name=org.name,
            access_token=access_token,
            refresh_token=refresh_token,
            refresh_expires=refresh_expires,
        )


list_orgs = list_legacy_orgs
list_org_ids = list_legacy_org_ids
create_org = create_legacy_org
get_org = get_legacy_org
switch_org = switch_legacy_org
update_org = update_legacy_org
delete_org = delete_legacy_org
