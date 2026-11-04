"""Bot accounts: service principals a person sponsors.

A bot is a ``users`` row whose kind is service, with one organization
membership at member and a sponsor. It signs in only with API keys. Its
project access is whatever it was granted, capped on every request by what
its sponsor holds now (``projects._effective_project_role_from_records``).
Every change a person makes to a bot is audited under that person.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException, status

from sibyl.persistence.content_runtime import soft_delete_private_raw_captures_for_user
from sibyl.persistence.surreal.auth_runtime._common import (
    _ORG_ADMIN_ROLE_VALUES,
    _PROJECT_ROLE_LEVELS,
    QueryClient,
    SurrealRecord,
    _api_key_namespace,
    _auth_client_scope,
    _coerce_optional_uuid,
    _log_audit_event,
    _record_payload,
    _role_value,
    _SurrealRepository,
    bot_role_ceiling_error,
    is_service_record,
)
from sibyl.persistence.surreal.auth_runtime.api_keys import (
    _decorate_api_key_scopes,
    _revoke_api_key_record,
    create_api_key_for_user,
)
from sibyl.persistence.surreal.auth_runtime.projects import (
    BOT_PROJECT_ROLE_CEILING,
    _resolve_project_for_team_link,
    verify_entity_project_access,
)
from sibyl_core.auth import AuthContext, OrganizationRole, PrincipalKind, ProjectRole
from sibyl_core.backends.surreal.in_clumps import clumped
from sibyl_core.backends.surreal.records import (
    coerce_datetime as _coerce_datetime,
    coerce_uuid as _coerce_uuid,
    normalize_record as _normalize_record,
    normalize_records as _normalize_records,
    query_error as _query_error,
    utcnow as _utcnow,
)

BOT_PURGE_AFTER = timedelta(days=30)
# Roles a person can sponsor a bot from. A viewer cannot create one, and a
# sponsor demoted to viewer later still caps the bot at what a viewer holds.
_SPONSOR_ORG_ROLES = frozenset({"owner", "admin", "member"})
_BOT_GRANTABLE_ROLES = frozenset({ProjectRole.VIEWER, ProjectRole.CONTRIBUTOR})


def _bot_not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bot not found")


def _is_admin(org_role: object | None) -> bool:
    return _role_value(org_role) in _ORG_ADMIN_ROLE_VALUES


async def _load_bot(
    client: QueryClient, *, organization_id: UUID, bot_id: UUID
) -> tuple[SurrealRecord, SurrealRecord, SurrealRecord | None]:
    """The bot, its membership here and its sponsor's membership here.

    A bot that is deleted, belongs to another organization, or is a person
    reads as not found.
    """
    payload = _record_payload(
        await client.execute_query(
            """
                RETURN {
                    bot: (
                        SELECT * FROM users
                        WHERE uuid = $bot_id AND kind = 'service' AND deleted_at = NONE
                        LIMIT 1
                    )[0],
                    membership: (
                        SELECT * FROM organization_members
                        WHERE organization_id = $organization_id AND user_id = $bot_id
                        LIMIT 1
                    )[0],
                };
            """,
            bot_id=str(bot_id),
            organization_id=str(organization_id),
        )
    )
    bot = _normalize_record(payload.get("bot"))
    membership = _normalize_record(payload.get("membership"))
    if bot is None or membership is None:
        raise _bot_not_found()
    sponsor_id = _coerce_optional_uuid(bot.get("sponsor_user_id"))
    sponsor = None
    if sponsor_id is not None:
        sponsor_payload = _record_payload(
            await client.execute_query(
                """
                    RETURN {
                        user: (
                            SELECT * FROM users
                            WHERE uuid = $sponsor_id AND deleted_at = NONE AND kind != 'service'
                            LIMIT 1
                        )[0],
                        membership: (
                            SELECT * FROM organization_members
                            WHERE organization_id = $organization_id AND user_id = $sponsor_id
                            LIMIT 1
                        )[0],
                    };
                """,
                sponsor_id=str(sponsor_id),
                organization_id=str(organization_id),
            )
        )
        sponsor_user = _normalize_record(sponsor_payload.get("user"))
        sponsor_membership = _normalize_record(sponsor_payload.get("membership"))
        if sponsor_user is not None:
            sponsor = {
                **sponsor_user,
                "org_role": sponsor_membership.get("role") if sponsor_membership else None,
            }
    return bot, membership, sponsor


def _is_sponsor(ctx: AuthContext, bot: SurrealRecord) -> bool:
    return _coerce_optional_uuid(bot.get("sponsor_user_id")) == ctx.user.id


def _require_manager(ctx: AuthContext, bot: SurrealRecord) -> None:
    """The bot's sponsor and org admins manage a bot; others never see it."""
    if _is_admin(ctx.org_role) or _is_sponsor(ctx, bot):
        return
    raise _bot_not_found()


def _require_sponsor(ctx: AuthContext, bot: SurrealRecord) -> None:
    """Only the bot's sponsor, while an owner, admin or member, extends what it holds.

    Grants and new keys both widen a bot's reach. An admin who needs to do
    either transfers the bot to themselves first, which is audited, rather
    than acting as a bot whose private memory is not theirs.
    """
    _require_manager(ctx, bot)
    if not _is_sponsor(ctx, bot):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "bot_sponsor_required",
                "message": "Only a bot's sponsor grants it project access or issues its keys.",
                "remediation": "An organization admin can transfer the bot to you first.",
            },
        )
    if _role_value(ctx.org_role) not in _SPONSOR_ORG_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "bot_sponsor_viewer",
                "message": "A viewer cannot grant a bot access or issue its keys.",
                "remediation": "An organization admin can restore your role or transfer the bot.",
            },
        )


def _bot_payload(
    bot: SurrealRecord,
    *,
    membership: SurrealRecord | None,
    sponsor: SurrealRecord | None,
) -> dict[str, object]:
    active = sponsor is not None and sponsor.get("org_role") is not None
    return {
        "id": str(_coerce_uuid(bot.get("uuid"), field_name="bot.uuid")),
        "name": str(bot.get("name") or ""),
        "kind": PrincipalKind.SERVICE.value,
        "org_role": str((membership or {}).get("role") or OrganizationRole.MEMBER.value),
        "status": "active" if active else "suspended",
        "sponsor": (
            {
                "id": str(_coerce_uuid(sponsor.get("uuid"), field_name="sponsor.uuid")),
                "name": sponsor.get("name"),
                "email": sponsor.get("email"),
            }
            if sponsor is not None
            else None
        ),
        "sponsor_user_id": str(bot["sponsor_user_id"]) if bot.get("sponsor_user_id") else None,
        "created_at": _coerce_datetime(bot.get("created_at")),
    }


async def create_bot(*, ctx: AuthContext, name: str, request) -> dict[str, object]:
    """Create a bot the caller sponsors, a member of the caller's organization."""
    if ctx.organization is None or _role_value(ctx.org_role) not in _SPONSOR_ORG_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    normalized = name.strip()
    if not normalized:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Name is required")
    organization_id = ctx.organization.id
    bot_id = uuid4()
    now = _utcnow()
    user_record: SurrealRecord = {
        "uuid": str(bot_id),
        "kind": PrincipalKind.SERVICE.value,
        "sponsor_user_id": str(ctx.user.id),
        "name": normalized,
        "email": None,
        "github_id": None,
        "avatar_url": None,
        "bio": None,
        "timezone": "UTC",
        "preferences": {},
        "is_admin": False,
        "created_at": now,
        "updated_at": now,
    }
    membership_record: SurrealRecord = {
        "uuid": str(uuid4()),
        "organization_id": str(organization_id),
        "user_id": str(bot_id),
        "role": OrganizationRole.MEMBER.value,
        "created_at": now,
        "updated_at": now,
    }
    async with _auth_client_scope() as client:
        created = _normalize_records(
            await client.execute_query("CREATE users CONTENT $record;", record=user_record)
        )
        if not created:
            msg = "Failed to create bot"
            raise RuntimeError(msg)
        membership = _normalize_records(
            await client.execute_query(
                "CREATE organization_members CONTENT $record;", record=membership_record
            )
        )
        if not membership:
            await client.execute_query("DELETE FROM users WHERE uuid = $uuid;", uuid=str(bot_id))
            msg = "Failed to create bot membership"
            raise RuntimeError(msg)
        await _log_audit_event(
            client,
            action="bot.create",
            user_id=ctx.user.id,
            organization_id=organization_id,
            request=request,
            details={"bot_user_id": str(bot_id), "name": normalized},
        )
    sponsor = {
        "uuid": str(ctx.user.id),
        "name": ctx.user.name,
        "email": ctx.user.email,
        "org_role": _role_value(ctx.org_role),
    }
    return _bot_payload(created[0], membership=membership[0], sponsor=sponsor)


async def list_bots(*, ctx: AuthContext) -> list[dict[str, object]]:
    """Every bot here for an org admin; a sponsor's own bots for anyone else."""
    if ctx.organization is None or ctx.org_role is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    organization_id = str(ctx.organization.id)
    async with _auth_client_scope() as client:
        memberships = _normalize_records(
            await client.execute_query(
                "SELECT user_id, role, created_at FROM organization_members "
                "WHERE organization_id = $organization_id ORDER BY created_at ASC;",
                organization_id=organization_id,
            )
        )
        role_by_user = {str(record.get("user_id")): record for record in memberships}
        if _is_admin(ctx.org_role):
            member_ids = list(role_by_user)
            query, params = clumped(
                "SELECT * FROM users WHERE uuid IN $ids AND kind = 'service' AND deleted_at = NONE",
                "ids",
                member_ids,
            )
            bots = _normalize_records(await client.execute_query(f"RETURN {query};", **params))
        else:
            bots = _normalize_records(
                await client.execute_query(
                    "SELECT * FROM users WHERE sponsor_user_id = $sponsor_id "
                    "AND kind = 'service' AND deleted_at = NONE;",
                    sponsor_id=str(ctx.user.id),
                )
            )
        bots = [bot for bot in bots if str(bot.get("uuid")) in role_by_user]
        sponsor_ids = sorted(
            {str(bot["sponsor_user_id"]) for bot in bots if bot.get("sponsor_user_id")}
        )
        sponsors: dict[str, SurrealRecord] = {}
        if sponsor_ids:
            query, params = clumped(
                "SELECT * FROM users WHERE uuid IN $ids AND deleted_at = NONE "
                "AND kind != 'service'",
                "ids",
                sponsor_ids,
            )
            for record in _normalize_records(
                await client.execute_query(f"RETURN {query};", **params)
            ):
                sponsor_id = str(record.get("uuid"))
                membership = role_by_user.get(sponsor_id)
                sponsors[sponsor_id] = {
                    **record,
                    "org_role": membership.get("role") if membership else None,
                }
    rows = [
        _bot_payload(
            bot,
            membership=role_by_user.get(str(bot.get("uuid"))),
            sponsor=sponsors.get(str(bot.get("sponsor_user_id") or "")),
        )
        for bot in bots
    ]
    rows.sort(key=lambda row: (str(row["name"]).lower(), str(row["id"])))
    return rows


async def get_bot(*, ctx: AuthContext, bot_id: UUID) -> dict[str, object]:
    """One bot with its project grants, for its sponsor or an org admin."""
    organization_id = _require_org(ctx)
    async with _auth_client_scope() as client:
        bot, membership, sponsor = await _load_bot(
            client, organization_id=organization_id, bot_id=bot_id
        )
        _require_manager(ctx, bot)
        grant_records = _normalize_records(
            await client.execute_query(
                "SELECT project_id, role, created_at FROM project_members "
                "WHERE organization_id = $organization_id AND user_id = $bot_id "
                "ORDER BY created_at ASC;",
                organization_id=str(organization_id),
                bot_id=str(bot_id),
            )
        )
        project_ids = [str(grant.get("project_id")) for grant in grant_records]
        project_records: list[SurrealRecord] = []
        if project_ids:
            query, params = clumped(
                "SELECT uuid, graph_project_id, name FROM projects "
                "WHERE organization_id = $organization_id AND uuid IN $ids",
                "ids",
                project_ids,
            )
            project_records = _normalize_records(
                await client.execute_query(
                    f"RETURN {query};", organization_id=str(organization_id), **params
                )
            )
    projects = {str(record.get("uuid")): record for record in project_records}
    grants = []
    for grant in grant_records:
        project = projects.get(str(grant.get("project_id")))
        if project is None:
            continue
        grants.append(
            {
                "project_id": str(project.get("graph_project_id") or ""),
                "project_name": str(project.get("name") or ""),
                "role": str(grant.get("role") or ProjectRole.VIEWER.value),
            }
        )
    return {
        **_bot_payload(bot, membership=membership, sponsor=sponsor),
        "projects": grants,
    }


def _require_org(ctx: AuthContext) -> UUID:
    if ctx.organization is None or ctx.org_role is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    return ctx.organization.id


async def grant_bot_project(
    *,
    ctx: AuthContext,
    bot_id: UUID,
    project_ref: str,
    role: ProjectRole,
    request,
) -> dict[str, object]:
    """Grant a bot a project role no higher than min(sponsor's role, contributor).

    This is the one path that lets someone who is not a project owner or
    maintainer add a project member, and it reaches only the caller's own
    bot. The bot's role is capped again by its sponsor on every request, so
    this bound keeps a grant that could never take effect from being written.
    """
    if role not in _BOT_GRANTABLE_ROLES:
        raise bot_role_ceiling_error(role.value)
    organization_id = _require_org(ctx)
    project = await _resolve_project_for_team_link(
        organization_id=organization_id, project_ref=project_ref
    )
    graph_project_id = str(project.graph_project_id)
    async with _auth_client_scope() as client:
        bot, _membership, _sponsor = await _load_bot(
            client, organization_id=organization_id, bot_id=bot_id
        )
        _require_sponsor(ctx, bot)
    sponsor_role = await verify_entity_project_access(
        ctx=ctx,
        entity_project_id=graph_project_id,
        required_role=ProjectRole.VIEWER,
        require_existing_project=True,
    )
    ceiling = min(
        sponsor_role or ProjectRole.VIEWER,
        BOT_PROJECT_ROLE_CEILING,
        key=_PROJECT_ROLE_LEVELS.__getitem__,
    )
    if _PROJECT_ROLE_LEVELS[role] > _PROJECT_ROLE_LEVELS[ceiling]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "bot_grant_exceeds_sponsor",
                "message": f"You can grant this bot at most {ceiling.value} on this project.",
                "remediation": "A bot never holds more on a project than its sponsor does.",
            },
        )
    async with _auth_client_scope() as client:
        repo = _SurrealRepository(client)
        existing = await repo.select_one(
            "SELECT * FROM project_members WHERE project_id = $project_id "
            "AND user_id = $bot_id LIMIT 1;",
            project_id=str(project.id),
            bot_id=str(bot_id),
        )
        now = _utcnow()
        if existing is not None:
            result = await client.execute_query(
                "UPDATE project_members SET role = $role, updated_at = $now "
                "WHERE project_id = $project_id AND user_id = $bot_id;",
                role=role.value,
                now=now,
                project_id=str(project.id),
                bot_id=str(bot_id),
            )
        else:
            result = await client.execute_query(
                "CREATE project_members CONTENT $record;",
                record={
                    "uuid": str(uuid4()),
                    "organization_id": str(organization_id),
                    "project_id": str(project.id),
                    "user_id": str(bot_id),
                    "role": role.value,
                    "created_at": now,
                    "updated_at": now,
                },
            )
        error = _query_error(result)
        if error is not None:
            raise RuntimeError(error)
        await _log_audit_event(
            client,
            action="bot.project.grant",
            user_id=ctx.user.id,
            organization_id=organization_id,
            request=request,
            details={
                "bot_user_id": str(bot_id),
                "project_id": graph_project_id,
                "role": role.value,
                "previous_role": existing.get("role") if existing else None,
            },
        )
    return {"bot_id": str(bot_id), "project_id": graph_project_id, "role": role.value}


async def revoke_bot_project(
    *, ctx: AuthContext, bot_id: UUID, project_ref: str, request
) -> dict[str, object]:
    """Remove a bot's grant on a project."""
    organization_id = _require_org(ctx)
    project = await _resolve_project_for_team_link(
        organization_id=organization_id, project_ref=project_ref
    )
    async with _auth_client_scope() as client:
        bot, _membership, _sponsor = await _load_bot(
            client, organization_id=organization_id, bot_id=bot_id
        )
        _require_manager(ctx, bot)
        removed = _normalize_records(
            await client.execute_query(
                "DELETE FROM project_members WHERE project_id = $project_id "
                "AND user_id = $bot_id RETURN BEFORE;",
                project_id=str(project.id),
                bot_id=str(bot_id),
            )
        )
        if not removed:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grant not found")
        await _log_audit_event(
            client,
            action="bot.project.revoke",
            user_id=ctx.user.id,
            organization_id=organization_id,
            request=request,
            details={"bot_user_id": str(bot_id), "project_id": str(project.graph_project_id)},
        )
    return {"bot_id": str(bot_id), "project_id": str(project.graph_project_id)}


async def transfer_bot(
    *, ctx: AuthContext, bot_id: UUID, sponsor_user_id: UUID, request
) -> dict[str, object]:
    """Hand a bot to a new sponsor. Org admins only; audited."""
    organization_id = _require_org(ctx)
    if not _is_admin(ctx.org_role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    async with _auth_client_scope() as client:
        bot, membership, _previous = await _load_bot(
            client, organization_id=organization_id, bot_id=bot_id
        )
        target = _record_payload(
            await client.execute_query(
                """
                    RETURN {
                        user: (
                            SELECT * FROM users
                            WHERE uuid = $sponsor_id AND deleted_at = NONE
                            LIMIT 1
                        )[0],
                        membership: (
                            SELECT * FROM organization_members
                            WHERE organization_id = $organization_id AND user_id = $sponsor_id
                            LIMIT 1
                        )[0],
                    };
                """,
                sponsor_id=str(sponsor_user_id),
                organization_id=str(organization_id),
            )
        )
        sponsor = _normalize_record(target.get("user"))
        sponsor_membership = _normalize_record(target.get("membership"))
        if (
            sponsor is None
            or is_service_record(sponsor)
            or sponsor_membership is None
            or _role_value(sponsor_membership.get("role")) not in _SPONSOR_ORG_ROLES
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "invalid_bot_sponsor",
                    "message": "A sponsor must be a person who is an owner, admin or member here.",
                },
            )
        previous_sponsor = bot.get("sponsor_user_id")
        updated = _normalize_records(
            await client.execute_query(
                "UPDATE users SET sponsor_user_id = $sponsor_id, updated_at = $now "
                "WHERE uuid = $bot_id AND kind = 'service';",
                sponsor_id=str(sponsor_user_id),
                now=_utcnow(),
                bot_id=str(bot_id),
            )
        )
        if not updated:
            raise _bot_not_found()
        await _log_audit_event(
            client,
            action="bot.transfer",
            user_id=ctx.user.id,
            organization_id=organization_id,
            request=request,
            details={
                "bot_user_id": str(bot_id),
                "from_sponsor_user_id": str(previous_sponsor) if previous_sponsor else None,
                "to_sponsor_user_id": str(sponsor_user_id),
            },
        )
    return _bot_payload(
        updated[0],
        membership=membership,
        sponsor={**sponsor, "org_role": sponsor_membership.get("role")},
    )


async def delete_bot(*, ctx: AuthContext, bot_id: UUID, request) -> dict[str, object]:
    """Delete a bot: revoke its keys, drop its memberships, schedule its private memory purge."""
    organization_id = _require_org(ctx)
    async with _auth_client_scope() as client:
        bot, _membership, _sponsor = await _load_bot(
            client, organization_id=organization_id, bot_id=bot_id
        )
        _require_manager(ctx, bot)
    now = _utcnow()
    purge_after = now + BOT_PURGE_AFTER
    private_memories_scheduled = await soft_delete_private_raw_captures_for_user(
        user_id=bot_id, purge_after=purge_after
    )
    async with _auth_client_scope() as client:
        revoked_keys = _normalize_records(
            await client.execute_query(
                "UPDATE api_keys SET revoked_at = $now, updated_at = $now "
                "WHERE user_id = $bot_id AND revoked_at = NONE;",
                now=now,
                bot_id=str(bot_id),
            )
        )
        await client.execute_query(
            "DELETE FROM project_members WHERE user_id = $bot_id;", bot_id=str(bot_id)
        )
        await client.execute_query(
            "DELETE FROM organization_members WHERE user_id = $bot_id;", bot_id=str(bot_id)
        )
        await client.execute_query(
            "UPDATE users SET deleted_at = $now, purge_after = $purge_after, updated_at = $now "
            "WHERE uuid = $bot_id AND kind = 'service';",
            now=now,
            purge_after=purge_after,
            bot_id=str(bot_id),
        )
        await _log_audit_event(
            client,
            action="bot.delete",
            user_id=ctx.user.id,
            organization_id=organization_id,
            request=request,
            details={
                "bot_user_id": str(bot_id),
                "name": str(bot.get("name") or ""),
                "api_keys_revoked": len(revoked_keys),
                "private_memories_scheduled": private_memories_scheduled,
                "purge_after": purge_after.isoformat(),
            },
        )
    return {
        "id": str(bot_id),
        "api_keys_revoked": len(revoked_keys),
        "private_memories_scheduled": private_memories_scheduled,
        "purge_after": purge_after,
    }


async def _require_key_issuer(ctx: AuthContext, bot_id: UUID) -> UUID:
    organization_id = _require_org(ctx)
    async with _auth_client_scope() as client:
        bot, _membership, _sponsor = await _load_bot(
            client, organization_id=organization_id, bot_id=bot_id
        )
    _require_sponsor(ctx, bot)
    return organization_id


async def create_bot_api_key(
    *,
    ctx: AuthContext,
    bot_id: UUID,
    name: str,
    live: bool,
    scopes: list[str],
    project_ids: list[str] | None,
    memory_space_ids: list[UUID] | None,
    expires_at,
    request,
):
    """Issue a key that authenticates as the bot. Its sponsor only; audited under them."""
    organization_id = await _require_key_issuer(ctx, bot_id)
    return await create_api_key_for_user(
        organization_id=organization_id,
        user_id=bot_id,
        actor_user_id=ctx.user.id,
        name=name,
        live=live,
        scopes=scopes,
        project_ids=project_ids,
        memory_space_ids=memory_space_ids,
        expires_at=expires_at,
        request=request,
    )


async def list_bot_api_keys(*, ctx: AuthContext, bot_id: UUID):
    organization_id = _require_org(ctx)
    async with _auth_client_scope() as client:
        bot, _membership, _sponsor = await _load_bot(
            client, organization_id=organization_id, bot_id=bot_id
        )
        _require_manager(ctx, bot)
        repo = _SurrealRepository(client)
        records = await repo.select_many(
            "SELECT * FROM api_keys WHERE organization_id = $organization_id "
            "AND user_id = $bot_id ORDER BY created_at DESC;",
            organization_id=str(organization_id),
            bot_id=str(bot_id),
        )
        keys = [key for record in records if (key := _api_key_namespace(record)) is not None]
        return await _decorate_api_key_scopes(repo, keys)


async def revoke_bot_api_key(*, ctx: AuthContext, bot_id: UUID, api_key_id: UUID, request) -> None:
    organization_id = _require_org(ctx)
    async with _auth_client_scope() as client:
        bot, _membership, _sponsor = await _load_bot(
            client, organization_id=organization_id, bot_id=bot_id
        )
        _require_manager(ctx, bot)
        repo = _SurrealRepository(client)
        record = await repo.select_one(
            "SELECT * FROM api_keys WHERE uuid = $uuid AND organization_id = $organization_id "
            "AND user_id = $bot_id LIMIT 1;",
            uuid=str(api_key_id),
            organization_id=str(organization_id),
            bot_id=str(bot_id),
        )
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
        await _revoke_api_key_record(
            client,
            record=record,
            actor_user_id=ctx.user.id,
            organization_id=organization_id,
            request=request,
        )


__all__ = [
    "BOT_PURGE_AFTER",
    "bot_role_ceiling_error",
    "create_bot",
    "create_bot_api_key",
    "delete_bot",
    "get_bot",
    "grant_bot_project",
    "list_bot_api_keys",
    "list_bots",
    "revoke_bot_api_key",
    "revoke_bot_project",
    "transfer_bot",
]
