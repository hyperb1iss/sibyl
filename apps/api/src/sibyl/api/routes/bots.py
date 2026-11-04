"""Bot accounts: service principals a person sponsors.

Any owner, admin or member creates bots they sponsor. A sponsor grants their
bot project access up to min(their own role there, contributor) and issues
its API keys; org admins see, transfer and delete every bot. A bot's project
role is capped by its sponsor's current role on every request, and a bot
whose sponsor left the organization is refused until an admin transfers it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from sibyl.api.routes.auth import ApiKeyCreateRequest
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, require_org_role, require_person
from sibyl.persistence import auth_runtime
from sibyl_core.auth import OrganizationRole, ProjectRole


async def require_unbound_credential(ctx: AuthContext = Depends(get_auth_context)) -> None:
    """Refuse an API key narrowed to projects or memory spaces.

    A bot made through a narrowed key could hold a key that reaches past that
    narrowing, so bot management needs a session or an unrestricted key.
    """
    if ctx.api_key_project_ids is not None or ctx.api_key_memory_space_ids is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "api_key_scope_denied",
                "message": "Bot management needs a session or an unrestricted API key.",
                "remediation": "Sign in, or use a key with no project or memory-space limits.",
            },
        )


router = APIRouter(
    prefix="/bots",
    tags=["bots"],
    dependencies=[Depends(require_person), Depends(require_unbound_credential)],
)

_ADMIN_ROLES = (OrganizationRole.OWNER, OrganizationRole.ADMIN)
_SPONSOR_ROLES = (OrganizationRole.OWNER, OrganizationRole.ADMIN, OrganizationRole.MEMBER)


class BotCreateRequest(BaseModel):
    # No sponsor field: whoever creates a bot sponsors it.
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=255)


class BotGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(..., min_length=1, description="Graph project ID or project UUID")
    role: ProjectRole = Field(
        default=ProjectRole.CONTRIBUTOR,
        description="project_viewer or project_contributor; a bot holds no higher role",
    )


class BotTransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sponsor_user_id: UUID


def _key_payload(key: object) -> dict[str, object]:
    return {
        "id": str(getattr(key, "id", "")),
        "name": getattr(key, "name", ""),
        "prefix": getattr(key, "key_prefix", ""),
        "scopes": list(getattr(key, "scopes", None) or []),
        "project_ids": list(getattr(key, "project_ids", None) or []),
        "memory_space_ids": list(getattr(key, "memory_space_ids", None) or []),
        "expires_at": getattr(key, "expires_at", None),
        "revoked_at": getattr(key, "revoked_at", None),
        "last_used_at": getattr(key, "last_used_at", None),
        "created_at": getattr(key, "created_at", None),
    }


@router.get("")
async def list_bots(ctx: AuthContext = Depends(get_auth_context)) -> dict[str, object]:
    """Every bot here for an org admin; your own bots otherwise."""
    return {"bots": await auth_runtime.list_bots(ctx=ctx)}


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_org_role(*_SPONSOR_ROLES))],
)
async def create_bot(
    request: Request,
    body: BotCreateRequest,
    ctx: AuthContext = Depends(get_auth_context),
) -> dict[str, object]:
    """Create a bot you sponsor. Viewers cannot."""
    return await auth_runtime.create_bot(ctx=ctx, name=body.name, request=request)


@router.get("/{bot_id}")
async def get_bot(bot_id: UUID, ctx: AuthContext = Depends(get_auth_context)) -> dict[str, object]:
    return await auth_runtime.get_bot(ctx=ctx, bot_id=bot_id)


@router.delete("/{bot_id}")
async def delete_bot(
    request: Request,
    bot_id: UUID,
    ctx: AuthContext = Depends(get_auth_context),
) -> dict[str, object]:
    """Delete a bot and revoke every key it holds."""
    return await auth_runtime.delete_bot(ctx=ctx, bot_id=bot_id, request=request)


@router.post("/{bot_id}/transfer", dependencies=[Depends(require_org_role(*_ADMIN_ROLES))])
async def transfer_bot(
    request: Request,
    bot_id: UUID,
    body: BotTransferRequest,
    ctx: AuthContext = Depends(get_auth_context),
) -> dict[str, object]:
    """Give a bot a new sponsor. Org admins only."""
    return await auth_runtime.transfer_bot(
        ctx=ctx, bot_id=bot_id, sponsor_user_id=body.sponsor_user_id, request=request
    )


@router.post("/{bot_id}/projects")
async def grant_bot_project(
    request: Request,
    bot_id: UUID,
    body: BotGrantRequest,
    ctx: AuthContext = Depends(get_auth_context),
) -> dict[str, object]:
    """Grant your bot a project role up to min(your role there, contributor)."""
    return await auth_runtime.grant_bot_project(
        ctx=ctx, bot_id=bot_id, project_ref=body.project_id, role=body.role, request=request
    )


@router.delete("/{bot_id}/projects/{project_id}")
async def revoke_bot_project(
    request: Request,
    bot_id: UUID,
    project_id: str,
    ctx: AuthContext = Depends(get_auth_context),
) -> dict[str, object]:
    return await auth_runtime.revoke_bot_project(
        ctx=ctx, bot_id=bot_id, project_ref=project_id, request=request
    )


@router.get("/{bot_id}/keys")
async def list_bot_keys(
    bot_id: UUID, ctx: AuthContext = Depends(get_auth_context)
) -> dict[str, object]:
    keys = await auth_runtime.list_bot_api_keys(ctx=ctx, bot_id=bot_id)
    return {"keys": [_key_payload(key) for key in keys]}


@router.post("/{bot_id}/keys", status_code=status.HTTP_201_CREATED)
async def create_bot_key(
    request: Request,
    bot_id: UUID,
    body: ApiKeyCreateRequest,
    ctx: AuthContext = Depends(get_auth_context),
) -> dict[str, object]:
    """Issue a key that authenticates as the bot. The raw key is shown once."""
    expires_at = (
        datetime.now(UTC) + timedelta(days=int(body.expires_days))
        if body.expires_days is not None
        else None
    )
    key, raw = await auth_runtime.create_bot_api_key(
        ctx=ctx,
        bot_id=bot_id,
        name=body.name,
        live=body.live,
        scopes=body.scopes,
        project_ids=body.project_ids,
        memory_space_ids=body.memory_space_ids,
        expires_at=expires_at,
        request=request,
    )
    return {**_key_payload(key), "bot_id": str(bot_id), "api_key": raw}


@router.post("/{bot_id}/keys/{api_key_id}/revoke")
async def revoke_bot_key(
    request: Request,
    bot_id: UUID,
    api_key_id: UUID,
    ctx: AuthContext = Depends(get_auth_context),
) -> dict[str, object]:
    await auth_runtime.revoke_bot_api_key(
        ctx=ctx, bot_id=bot_id, api_key_id=api_key_id, request=request
    )
    return {"success": True, "id": str(api_key_id)}
