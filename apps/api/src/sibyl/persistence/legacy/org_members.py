"""Legacy organization membership adapters backed by the current auth runtime."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlmodel import col, select
from starlette.requests import Request

from sibyl.auth.audit import AuditLogger
from sibyl.auth.memberships import OrganizationMembershipManager
from sibyl.auth.organizations import OrganizationManager
from sibyl.db.connection import get_session
from sibyl.db.models import OrganizationMember, OrganizationRole, User
from sibyl.persistence.organization_common import LegacyOrgMemberChange


async def _get_org_and_member(
    *,
    slug: str,
    user_id: UUID,
) -> tuple[UUID, OrganizationMember]:
    async with get_session() as session:
        org = await OrganizationManager.from_session(session).get_by_slug(slug)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        member = await OrganizationMembershipManager.from_session(session).get_for_user(
            org.id,
            user_id,
        )
        if member is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        return org.id, member


async def list_legacy_org_members(*, slug: str, actor_id: UUID) -> list[dict[str, object]]:
    org_id, _ = await _get_org_and_member(slug=slug, user_id=actor_id)

    async with get_session() as session:
        result = await session.execute(
            select(OrganizationMember, User)
            .join(User, col(User.id) == col(OrganizationMember.user_id))
            .where(col(OrganizationMember.organization_id) == org_id)
        )
        return [
            {
                "user": {
                    "id": str(member_user.id),
                    "github_id": member_user.github_id,
                    "email": member_user.email,
                    "name": member_user.name,
                    "avatar_url": member_user.avatar_url,
                },
                "role": membership.role.value,
                "created_at": membership.created_at,
            }
            for membership, member_user in result.all()
        ]


async def add_legacy_org_member(
    *,
    slug: str,
    actor_id: UUID,
    target_user_id: UUID,
    role: OrganizationRole,
    request: Request,
) -> LegacyOrgMemberChange:
    org_id, actor_membership = await _get_org_and_member(slug=slug, user_id=actor_id)
    if actor_membership.role not in {OrganizationRole.OWNER, OrganizationRole.ADMIN}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    async with get_session() as session:
        membership = await OrganizationMembershipManager.from_session(session).add_member(
            organization_id=org_id,
            user_id=target_user_id,
            role=role,
        )
        await AuditLogger(session).log(
            action="org.member.add",
            user_id=actor_id,
            organization_id=org_id,
            request=request,
            details={"target_user_id": str(membership.user_id), "role": membership.role.value},
        )
        return LegacyOrgMemberChange(
            org_id=org_id,
            user_id=membership.user_id,
            role=membership.role,
        )


async def update_legacy_org_member_role(
    *,
    slug: str,
    actor_id: UUID,
    target_user_id: UUID,
    role: OrganizationRole,
    request: Request,
) -> LegacyOrgMemberChange:
    org_id, actor_membership = await _get_org_and_member(slug=slug, user_id=actor_id)
    if actor_membership.role not in {OrganizationRole.OWNER, OrganizationRole.ADMIN}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    async with get_session() as session:
        membership = await OrganizationMembershipManager.from_session(session).set_role(
            organization_id=org_id,
            user_id=target_user_id,
            role=role,
        )
        await AuditLogger(session).log(
            action="org.member.update_role",
            user_id=actor_id,
            organization_id=org_id,
            request=request,
            details={"target_user_id": str(membership.user_id), "role": membership.role.value},
        )
        return LegacyOrgMemberChange(
            org_id=org_id,
            user_id=membership.user_id,
            role=membership.role,
        )


async def remove_legacy_org_member(
    *,
    slug: str,
    actor_id: UUID,
    target_user_id: UUID,
    request: Request,
) -> LegacyOrgMemberChange:
    org_id, actor_membership = await _get_org_and_member(slug=slug, user_id=actor_id)

    if actor_id != target_user_id and actor_membership.role not in {
        OrganizationRole.OWNER,
        OrganizationRole.ADMIN,
    }:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    async with get_session() as session:
        try:
            await OrganizationMembershipManager.from_session(session).remove_member(
                organization_id=org_id,
                user_id=target_user_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

        await AuditLogger(session).log(
            action="org.member.remove",
            user_id=actor_id,
            organization_id=org_id,
            request=request,
            details={"target_user_id": str(target_user_id)},
        )
        return LegacyOrgMemberChange(org_id=org_id, user_id=target_user_id)


list_org_members = list_legacy_org_members
add_org_member = add_legacy_org_member
update_org_member_role = update_legacy_org_member_role
remove_org_member = remove_legacy_org_member
