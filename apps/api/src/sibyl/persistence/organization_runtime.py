"""Active organization runtime adapters for the configured auth backend."""

from __future__ import annotations

from sibyl.config import settings
from sibyl.persistence.legacy.org_invitations import (
    accept_legacy_org_invitation as legacy_accept_legacy_org_invitation,
    create_legacy_org_invitation as legacy_create_legacy_org_invitation,
    delete_legacy_org_invitation as legacy_delete_legacy_org_invitation,
    list_legacy_org_invitations as legacy_list_legacy_org_invitations,
)
from sibyl.persistence.legacy.org_members import (
    add_legacy_org_member as legacy_add_legacy_org_member,
    list_legacy_org_members as legacy_list_legacy_org_members,
    remove_legacy_org_member as legacy_remove_legacy_org_member,
    update_legacy_org_member_role as legacy_update_legacy_org_member_role,
)
from sibyl.persistence.legacy.orgs import (
    create_legacy_org as legacy_create_legacy_org,
    delete_legacy_org as legacy_delete_legacy_org,
    get_legacy_org as legacy_get_legacy_org,
    list_legacy_orgs as legacy_list_legacy_orgs,
    switch_legacy_org as legacy_switch_legacy_org,
    update_legacy_org as legacy_update_legacy_org,
)
from sibyl.persistence.legacy.project_members import (
    add_legacy_project_member as legacy_add_legacy_project_member,
    can_manage_legacy_project_members,
    list_legacy_project_members as legacy_list_legacy_project_members,
    remove_legacy_project_member as legacy_remove_legacy_project_member,
    update_legacy_project_member_role as legacy_update_legacy_project_member_role,
)

__all__ = [
    "accept_legacy_org_invitation",
    "add_legacy_org_member",
    "add_legacy_project_member",
    "can_manage_legacy_project_members",
    "create_legacy_org",
    "create_legacy_org_invitation",
    "delete_legacy_org",
    "delete_legacy_org_invitation",
    "get_legacy_org",
    "list_legacy_org_invitations",
    "list_legacy_org_members",
    "list_legacy_orgs",
    "list_legacy_project_members",
    "remove_legacy_org_member",
    "remove_legacy_project_member",
    "switch_legacy_org",
    "update_legacy_org",
    "update_legacy_org_member_role",
    "update_legacy_project_member_role",
]


async def list_legacy_orgs(*, user_id):
    if settings.auth_store == "surreal":
        from sibyl.persistence.surreal.organization_runtime import (
            list_legacy_orgs as surreal_list_legacy_orgs,
        )

        return await surreal_list_legacy_orgs(user_id=user_id)
    return await legacy_list_legacy_orgs(user_id=user_id)


async def get_legacy_org(*, slug, user_id):
    if settings.auth_store == "surreal":
        from sibyl.persistence.surreal.organization_runtime import (
            get_legacy_org as surreal_get_legacy_org,
        )

        return await surreal_get_legacy_org(slug=slug, user_id=user_id)
    return await legacy_get_legacy_org(slug=slug, user_id=user_id)


async def update_legacy_org(*, request, slug, user_id, name=None, new_slug=None):
    if settings.auth_store == "surreal":
        from sibyl.persistence.surreal.organization_runtime import (
            update_legacy_org as surreal_update_legacy_org,
        )

        return await surreal_update_legacy_org(
            request=request,
            slug=slug,
            user_id=user_id,
            name=name,
            new_slug=new_slug,
        )
    return await legacy_update_legacy_org(
        request=request,
        slug=slug,
        user_id=user_id,
        name=name,
        new_slug=new_slug,
    )


async def list_legacy_org_members(*, slug, actor_id):
    if settings.auth_store == "surreal":
        from sibyl.persistence.surreal.organization_runtime import (
            list_legacy_org_members as surreal_list_legacy_org_members,
        )

        return await surreal_list_legacy_org_members(slug=slug, actor_id=actor_id)
    return await legacy_list_legacy_org_members(slug=slug, actor_id=actor_id)


async def add_legacy_org_member(*, slug, actor_id, target_user_id, role, request):
    if settings.auth_store == "surreal":
        from sibyl.persistence.surreal.organization_runtime import (
            add_legacy_org_member as surreal_add_legacy_org_member,
        )

        return await surreal_add_legacy_org_member(
            slug=slug,
            actor_id=actor_id,
            target_user_id=target_user_id,
            role=role,
            request=request,
        )
    return await legacy_add_legacy_org_member(
        slug=slug,
        actor_id=actor_id,
        target_user_id=target_user_id,
        role=role,
        request=request,
    )


async def create_legacy_org(*, request, user_id, name, slug=None):
    if settings.auth_store == "surreal":
        from sibyl.persistence.surreal.organization_runtime import (
            create_legacy_org as surreal_create_legacy_org,
        )

        return await surreal_create_legacy_org(
            request=request,
            user_id=user_id,
            name=name,
            slug=slug,
        )
    return await legacy_create_legacy_org(
        request=request,
        user_id=user_id,
        name=name,
        slug=slug,
    )


async def update_legacy_org_member_role(*, slug, actor_id, target_user_id, role, request):
    if settings.auth_store == "surreal":
        from sibyl.persistence.surreal.organization_runtime import (
            update_legacy_org_member_role as surreal_update_legacy_org_member_role,
        )

        return await surreal_update_legacy_org_member_role(
            slug=slug,
            actor_id=actor_id,
            target_user_id=target_user_id,
            role=role,
            request=request,
        )
    return await legacy_update_legacy_org_member_role(
        slug=slug,
        actor_id=actor_id,
        target_user_id=target_user_id,
        role=role,
        request=request,
    )


async def remove_legacy_org_member(*, slug, actor_id, target_user_id, request):
    if settings.auth_store == "surreal":
        from sibyl.persistence.surreal.organization_runtime import (
            remove_legacy_org_member as surreal_remove_legacy_org_member,
        )

        return await surreal_remove_legacy_org_member(
            slug=slug,
            actor_id=actor_id,
            target_user_id=target_user_id,
            request=request,
        )
    return await legacy_remove_legacy_org_member(
        slug=slug,
        actor_id=actor_id,
        target_user_id=target_user_id,
        request=request,
    )


async def switch_legacy_org(*, request, slug, user_id):
    if settings.auth_store == "surreal":
        from sibyl.persistence.surreal.organization_runtime import (
            switch_legacy_org as surreal_switch_legacy_org,
        )

        return await surreal_switch_legacy_org(
            request=request,
            slug=slug,
            user_id=user_id,
        )
    return await legacy_switch_legacy_org(
        request=request,
        slug=slug,
        user_id=user_id,
    )


async def delete_legacy_org(*, request, slug, user_id):
    if settings.auth_store == "surreal":
        from sibyl.persistence.surreal.organization_runtime import (
            delete_legacy_org as surreal_delete_legacy_org,
        )

        return await surreal_delete_legacy_org(
            request=request,
            slug=slug,
            user_id=user_id,
        )
    return await legacy_delete_legacy_org(
        request=request,
        slug=slug,
        user_id=user_id,
    )


async def list_legacy_project_members(*, project_id, actor, org_id):
    if settings.auth_store == "surreal":
        from sibyl.persistence.surreal.organization_runtime import (
            list_legacy_project_members as surreal_list_legacy_project_members,
        )

        return await surreal_list_legacy_project_members(
            project_id=project_id,
            actor=actor,
            org_id=org_id,
        )
    return await legacy_list_legacy_project_members(project_id=project_id, actor=actor, org_id=org_id)


async def add_legacy_project_member(*, request, project_id, actor, org_id, target_user_id, role):
    if settings.auth_store == "surreal":
        from sibyl.persistence.surreal.organization_runtime import (
            add_legacy_project_member as surreal_add_legacy_project_member,
        )

        return await surreal_add_legacy_project_member(
            request=request,
            project_id=project_id,
            actor=actor,
            org_id=org_id,
            target_user_id=target_user_id,
            role=role,
        )
    return await legacy_add_legacy_project_member(
        request=request,
        project_id=project_id,
        actor=actor,
        org_id=org_id,
        target_user_id=target_user_id,
        role=role,
    )


async def update_legacy_project_member_role(
    *,
    request,
    project_id,
    actor,
    org_id,
    target_user_id,
    role,
):
    if settings.auth_store == "surreal":
        from sibyl.persistence.surreal.organization_runtime import (
            update_legacy_project_member_role as surreal_update_legacy_project_member_role,
        )

        return await surreal_update_legacy_project_member_role(
            request=request,
            project_id=project_id,
            actor=actor,
            org_id=org_id,
            target_user_id=target_user_id,
            role=role,
        )
    return await legacy_update_legacy_project_member_role(
        request=request,
        project_id=project_id,
        actor=actor,
        org_id=org_id,
        target_user_id=target_user_id,
        role=role,
    )


async def remove_legacy_project_member(*, request, project_id, actor, org_id, target_user_id):
    if settings.auth_store == "surreal":
        from sibyl.persistence.surreal.organization_runtime import (
            remove_legacy_project_member as surreal_remove_legacy_project_member,
        )

        return await surreal_remove_legacy_project_member(
            request=request,
            project_id=project_id,
            actor=actor,
            org_id=org_id,
            target_user_id=target_user_id,
        )
    return await legacy_remove_legacy_project_member(
        request=request,
        project_id=project_id,
        actor=actor,
        org_id=org_id,
        target_user_id=target_user_id,
    )


async def list_legacy_org_invitations(*, slug, actor_id):
    if settings.auth_store == "surreal":
        from sibyl.persistence.surreal.organization_runtime import (
            list_legacy_org_invitations as surreal_list_legacy_org_invitations,
        )

        return await surreal_list_legacy_org_invitations(slug=slug, actor_id=actor_id)
    return await legacy_list_legacy_org_invitations(slug=slug, actor_id=actor_id)


async def create_legacy_org_invitation(*, slug, actor_id, email, role, expires_days, request):
    if settings.auth_store == "surreal":
        from sibyl.persistence.surreal.organization_runtime import (
            create_legacy_org_invitation as surreal_create_legacy_org_invitation,
        )

        return await surreal_create_legacy_org_invitation(
            slug=slug,
            actor_id=actor_id,
            email=email,
            role=role,
            expires_days=expires_days,
            request=request,
        )
    return await legacy_create_legacy_org_invitation(
        slug=slug,
        actor_id=actor_id,
        email=email,
        role=role,
        expires_days=expires_days,
        request=request,
    )


async def delete_legacy_org_invitation(*, slug, actor_id, invitation_id, request):
    if settings.auth_store == "surreal":
        from sibyl.persistence.surreal.organization_runtime import (
            delete_legacy_org_invitation as surreal_delete_legacy_org_invitation,
        )

        return await surreal_delete_legacy_org_invitation(
            slug=slug,
            actor_id=actor_id,
            invitation_id=invitation_id,
            request=request,
        )
    return await legacy_delete_legacy_org_invitation(
        slug=slug,
        actor_id=actor_id,
        invitation_id=invitation_id,
        request=request,
    )


async def accept_legacy_org_invitation(*, token, user, request):
    if settings.auth_store == "surreal":
        from sibyl.persistence.surreal.organization_runtime import (
            accept_legacy_org_invitation as surreal_accept_legacy_org_invitation,
        )

        return await surreal_accept_legacy_org_invitation(
            token=token,
            user=user,
            request=request,
        )
    return await legacy_accept_legacy_org_invitation(token=token, user=user, request=request)
