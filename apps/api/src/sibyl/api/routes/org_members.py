"""Organization membership endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from sibyl.auth.audit import AuditLogger
from sibyl.auth.dependencies import get_current_user
from sibyl.auth.memberships import OrganizationMembershipManager
from sibyl.auth.organizations import OrganizationManager
from sibyl.db.connection import get_session_dependency
from sibyl.db.models import OrganizationMember, OrganizationRole, User

router = APIRouter(prefix="/orgs/{slug}/members", tags=["org-members"])


class MemberAddRequest(BaseModel):
    user_id: UUID
    role: OrganizationRole = Field(default=OrganizationRole.MEMBER)


class MemberRoleUpdateRequest(BaseModel):
    role: OrganizationRole


async def _get_org_and_member(
    *,
    slug: str,
    user: User,
    session: AsyncSession,
) -> tuple[UUID, OrganizationMember]:
    org = await OrganizationManager(session).get_by_slug(slug)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    member = await OrganizationMembershipManager(session).get_for_user(org.id, user.id)
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return org.id, member


@router.get("")
async def list_members(
    slug: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dependency),
):
    org_id, _ = await _get_org_and_member(slug=slug, user=user, session=session)
    result = await session.execute(
        select(OrganizationMember, User)
        .join(User, User.id == OrganizationMember.user_id)
        .where(OrganizationMember.organization_id == org_id)
    )
    members = []
    for membership, member_user in result.all():
        members.append(
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
        )
    return {"members": members}


@router.post("")
async def add_member(
    request: Request,
    slug: str,
    body: MemberAddRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dependency),
):
    org_id, me = await _get_org_and_member(slug=slug, user=user, session=session)
    if me.role not in {OrganizationRole.OWNER, OrganizationRole.ADMIN}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    membership = await OrganizationMembershipManager(session).add_member(
        organization_id=org_id,
        user_id=body.user_id,
        role=body.role,
    )
    await AuditLogger(session).log(
        action="org.member.add",
        user_id=user.id,
        organization_id=org_id,
        request=request,
        details={"target_user_id": str(membership.user_id), "role": membership.role.value},
    )
    return {"user_id": str(membership.user_id), "role": membership.role.value}


@router.patch("/{user_id}")
async def update_member_role(
    request: Request,
    slug: str,
    user_id: UUID,
    body: MemberRoleUpdateRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dependency),
):
    org_id, me = await _get_org_and_member(slug=slug, user=user, session=session)
    if me.role not in {OrganizationRole.OWNER, OrganizationRole.ADMIN}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    membership = await OrganizationMembershipManager(session).set_role(
        organization_id=org_id,
        user_id=user_id,
        role=body.role,
    )
    await AuditLogger(session).log(
        action="org.member.update_role",
        user_id=user.id,
        organization_id=org_id,
        request=request,
        details={"target_user_id": str(membership.user_id), "role": membership.role.value},
    )
    return {"user_id": str(membership.user_id), "role": membership.role.value}


@router.delete("/{user_id}")
async def remove_member(
    request: Request,
    slug: str,
    user_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dependency),
):
    org_id, me = await _get_org_and_member(slug=slug, user=user, session=session)

    # You can always remove yourself (subject to last-owner invariant).
    if user.id != user_id and me.role not in {OrganizationRole.OWNER, OrganizationRole.ADMIN}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    try:
        await OrganizationMembershipManager(session).remove_member(
            organization_id=org_id,
            user_id=user_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e

    await AuditLogger(session).log(
        action="org.member.remove",
        user_id=user.id,
        organization_id=org_id,
        request=request,
        details={"target_user_id": str(user_id)},
    )
    return {"success": True}
