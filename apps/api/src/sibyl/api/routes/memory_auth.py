"""Shared authorization, policy, and audit behavior for memory routes."""

from __future__ import annotations

from typing import cast

import structlog
from fastapi import HTTPException, Request

from sibyl.api.routes import memory_serialization as serialization
from sibyl.api.schemas import (
    MemorySharePreviewRequest,
    RawMemoryRecallRequest,
    ReflectionPromotionRequest,
)
from sibyl.auth.api_key_common import api_key_memory_scope_key
from sibyl.auth.authorization import verify_entity_project_access
from sibyl.auth.context import AuthContext
from sibyl.persistence.auth_runtime import (
    list_accessible_project_graph_ids,
    list_accessible_team_scope_keys,
    log_memory_audit_event,
)
from sibyl_core.auth import MemoryPolicyContext, OrganizationRole, ProjectRole
from sibyl_core.auth.memory_policy import (
    MemoryPolicyAction,
    MemoryPolicyDecision,
    authorize_memory_read,
    authorize_memory_write,
)
from sibyl_core.services.memory import (
    ReflectionPromotionResult,
)
from sibyl_core.services.surreal_content import (
    MemoryScope,
    RawMemory,
    get_raw_memory,
    get_raw_memory_by_source_id,
)

log = structlog.get_logger()

_READ_ROLES = (
    OrganizationRole.OWNER,
    OrganizationRole.ADMIN,
    OrganizationRole.MEMBER,
    OrganizationRole.VIEWER,
)
_WRITE_ROLES = (
    OrganizationRole.OWNER,
    OrganizationRole.ADMIN,
    OrganizationRole.MEMBER,
)
_ADMIN_ROLES = (OrganizationRole.OWNER, OrganizationRole.ADMIN)
_ARCHIVEABLE_REFLECTION_EXCEPTION_REASONS = frozenset(
    {
        "duplicate_candidate",
        "stale_candidate",
    }
)

REQUEST_AUTO_INJECT_SENTINEL: Request = cast("Request", None)


def _policy_http_status(reason: str) -> int:
    if reason == "missing_scope_key":
        return 400
    if reason == "principal_mismatch":
        return 401
    return 403


def _log_policy_decision(
    *,
    ctx: AuthContext,
    decision: MemoryPolicyDecision,
    surface: str,
) -> None:
    log.info(
        "memory_policy_decision",
        action=decision.action.value,
        allowed=decision.allowed,
        memory_scope=decision.memory_scope.value,
        organization_id=ctx.organization_id,
        policy_reason=decision.reason,
        principal_id=ctx.user_id,
        scope_key=decision.scope_key,
        surface=surface,
    )


async def log_memory_audit(
    *,
    action: str,
    ctx: AuthContext,
    request: Request | None = None,
    memory_scope: str | None,
    scope_key: str | None,
    source_surface: str,
    policy_allowed: bool | None,
    policy_reason: str | None,
    project_id: str | None = None,
    source_ids: list[str] | None = None,
    derived_ids: list[str] | None = None,
    details: dict[str, object] | None = None,
) -> str | None:
    try:
        return await log_memory_audit_event(
            action=action,
            user_id=ctx.user_id,
            organization_id=ctx.organization_id,
            request=request,
            memory_scope=memory_scope,
            scope_key=scope_key,
            project_id=project_id,
            source_surface=source_surface,
            source_ids=source_ids,
            derived_ids=derived_ids,
            policy_allowed=policy_allowed,
            policy_reason=policy_reason,
            details=details,
        )
    except Exception as exc:
        log.warning("memory_audit_event_failed", action=action, error=str(exc), exc_info=True)
        return None


async def project_accessible_for_policy(
    *,
    ctx: AuthContext,
    memory_scope: str,
    scope_key: str | None,
) -> set[str] | None:
    if memory_scope != "project" or not scope_key:
        return None
    accessible_projects = await list_accessible_project_graph_ids(ctx)
    return {str(project_id) for project_id in accessible_projects or set()}


async def team_accessible_for_policy(
    *,
    ctx: AuthContext,
    memory_scope: str,
    scope_key: str | None,
) -> set[str] | None:
    if memory_scope != "team" or not scope_key:
        return None
    accessible_teams = await list_accessible_team_scope_keys(ctx)
    return {str(team_id) for team_id in accessible_teams or set()}


async def authorize_project_scope_write(
    *,
    ctx: AuthContext,
    memory_scope: str,
    scope_key: str | None,
) -> None:
    if memory_scope != "project" or not scope_key:
        return
    await verify_entity_project_access(
        None,
        ctx,
        scope_key,
        required_role=ProjectRole.CONTRIBUTOR,
        require_existing_project=True,
    )


def api_key_memory_scope_allowed(
    ctx: AuthContext,
    *,
    memory_scope: str,
    scope_key: str | None,
) -> bool:
    allowed_scope_keys = ctx.api_key_memory_scope_keys
    if allowed_scope_keys is None:
        return True
    if not isinstance(allowed_scope_keys, list | tuple | set | frozenset):
        return True
    effective_scope_key = ctx.user_id if memory_scope == "private" and not scope_key else scope_key
    scope_key_id = api_key_memory_scope_key(memory_scope, effective_scope_key)
    return scope_key_id in allowed_scope_keys


def _api_key_memory_scope_denial(
    *,
    action: MemoryPolicyAction,
    memory_scope: str,
    scope_key: str | None,
    policy_context: MemoryPolicyContext,
) -> MemoryPolicyDecision:
    try:
        normalized_scope = MemoryScope(memory_scope)
    except ValueError:
        normalized_scope = MemoryScope.PRIVATE
    return MemoryPolicyDecision(
        action=action,
        allowed=False,
        reason="api_key_memory_space_denied",
        memory_scope=normalized_scope,
        scope_key=scope_key,
        policy_context=policy_context,
    )


async def authorize_memory_policy(
    *,
    ctx: AuthContext,
    action: MemoryPolicyAction,
    memory_scope: str,
    scope_key: str | None,
    surface: str,
    request: Request | None = None,
    agent_id: str | None = None,
    project_id: str | None = None,
) -> MemoryPolicyDecision:
    accessible_projects = await project_accessible_for_policy(
        ctx=ctx,
        memory_scope=memory_scope,
        scope_key=scope_key,
    )
    accessible_teams = await team_accessible_for_policy(
        ctx=ctx,
        memory_scope=memory_scope,
        scope_key=scope_key,
    )
    policy_context = MemoryPolicyContext(
        actor_user_id=ctx.user_id,
        organization_id=ctx.organization_id,
        organization_role=ctx.org_role,
        memory_space=memory_scope,
        scope_key=scope_key,
        project_id=project_id,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        agent_id=agent_id,
        source_surface=surface,
    )
    if action is MemoryPolicyAction.READ:
        decision = authorize_memory_read(
            policy_context=policy_context,
        )
    elif action is MemoryPolicyAction.WRITE:
        decision = authorize_memory_write(
            policy_context=policy_context,
        )
    else:
        msg = f"Unsupported raw memory policy action: {action.value}"
        raise ValueError(msg)

    _log_policy_decision(ctx=ctx, decision=decision, surface=surface)
    if not decision.allowed:
        await log_memory_audit(
            action="memory.policy_deny",
            ctx=ctx,
            request=request,
            memory_scope=memory_scope,
            scope_key=scope_key,
            project_id=project_id,
            source_surface=surface,
            policy_allowed=False,
            policy_reason=decision.reason,
            details={"policy_action": decision.action.value},
        )
        raise HTTPException(
            status_code=_policy_http_status(decision.reason),
            detail=decision.reason,
        )
    if not api_key_memory_scope_allowed(ctx, memory_scope=memory_scope, scope_key=scope_key):
        deny_decision = _api_key_memory_scope_denial(
            action=action,
            memory_scope=memory_scope,
            scope_key=scope_key,
            policy_context=policy_context,
        )
        _log_policy_decision(ctx=ctx, decision=deny_decision, surface=surface)
        await log_memory_audit(
            action="memory.policy_deny",
            ctx=ctx,
            request=request,
            memory_scope=memory_scope,
            scope_key=scope_key,
            project_id=project_id,
            source_surface=surface,
            policy_allowed=False,
            policy_reason=deny_decision.reason,
            details={"policy_action": deny_decision.action.value},
        )
        raise HTTPException(
            status_code=_policy_http_status(deny_decision.reason),
            detail=deny_decision.reason,
        )
    return decision


async def authorize_project_filter(
    *,
    ctx: AuthContext,
    project_id: str | None,
    required_project_role: ProjectRole,
    surface: str,
    memory_scope: str | None,
    scope_key: str | None,
    policy_action: str,
    request: Request | None = None,
) -> None:
    if not project_id:
        return
    try:
        await verify_entity_project_access(
            None,
            ctx,
            project_id,
            required_role=required_project_role,
            require_existing_project=True,
        )
    except HTTPException as exc:
        await log_memory_audit(
            action="memory.policy_deny",
            ctx=ctx,
            request=request,
            memory_scope=memory_scope,
            scope_key=scope_key,
            project_id=project_id,
            source_surface=surface,
            policy_allowed=False,
            policy_reason=str(exc.detail),
            details={
                "policy_action": policy_action,
                "required_project_role": required_project_role.value,
            },
        )
        raise


def diary_metadata(
    *,
    metadata: dict[str, object],
    diary: bool,
    agent_id: str | None,
    project_id: str | None,
) -> dict[str, object]:
    if not diary:
        return dict(metadata)
    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id is required for diary memory")
    out = dict(metadata)
    out["agent_id"] = agent_id
    out["memory_kind"] = "agent_diary"
    if project_id:
        out["project_id"] = project_id
    return out


def validate_diary_request(*, diary: bool, agent_id: str | None, memory_scope: str) -> None:
    if not diary:
        return
    if memory_scope != "private":
        raise HTTPException(status_code=400, detail="diary memory must use private scope")
    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id is required for diary memory")


def raw_recall_audit_details(
    request: RawMemoryRecallRequest,
    *,
    result_count: int,
) -> dict[str, object]:
    details: dict[str, object] = {
        "agent_id": request.agent_id,
        "diary": request.diary,
        "limit": request.limit,
        "result_count": result_count,
    }
    if request.participants:
        details["participants"] = list(request.participants)
    if request.labels:
        details["labels"] = list(request.labels)
    if request.thread_id:
        details["thread_id"] = request.thread_id
    if request.occurred_after:
        details["occurred_after"] = request.occurred_after.isoformat()
    if request.occurred_before:
        details["occurred_before"] = request.occurred_before.isoformat()
    if request.as_of:
        details["as_of"] = request.as_of.isoformat()
    return details


async def load_memory_source_for_org(
    *,
    organization_id: str,
    source_id: str,
) -> RawMemory:
    memory = await get_raw_memory(organization_id=organization_id, memory_id=source_id)
    if memory is None:
        memory = await get_raw_memory_by_source_id(
            organization_id=organization_id,
            source_id=source_id,
        )
    if memory is None:
        raise HTTPException(status_code=404, detail="memory_source_not_found")
    return memory


async def inspect_content_policy(
    *,
    ctx: AuthContext,
    memory: RawMemory,
) -> MemoryPolicyDecision:
    project_id = serialization.memory_project_id(memory)
    accessible_projects = await project_accessible_for_policy(
        ctx=ctx,
        memory_scope=memory.memory_scope.value,
        scope_key=memory.scope_key,
    )
    accessible_teams = await team_accessible_for_policy(
        ctx=ctx,
        memory_scope=memory.memory_scope.value,
        scope_key=memory.scope_key,
    )
    policy_context = MemoryPolicyContext(
        actor_user_id=ctx.user_id,
        organization_id=ctx.organization_id,
        organization_role=ctx.org_role,
        memory_space=memory.memory_scope.value,
        scope_key=memory.scope_key,
        project_id=project_id,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        agent_id=memory.agent_id,
        source_surface="memory_inspect",
    )
    decision = authorize_memory_read(policy_context=policy_context)
    if memory.memory_scope.value == "private" and memory.principal_id != ctx.user_id:
        decision = MemoryPolicyDecision(
            action=MemoryPolicyAction.READ,
            allowed=False,
            reason="principal_mismatch",
            memory_scope=memory.memory_scope,
            scope_key=memory.scope_key,
            policy_context=policy_context,
        )
    _log_policy_decision(ctx=ctx, decision=decision, surface="memory_inspect")
    return decision


async def require_source_policy(
    *,
    ctx: AuthContext,
    memory: RawMemory,
    action: MemoryPolicyAction,
    surface: str,
    request: Request,
) -> MemoryPolicyDecision:
    if memory.memory_scope is MemoryScope.PRIVATE and memory.principal_id != ctx.user_id:
        decision = MemoryPolicyDecision(
            action=action,
            allowed=False,
            reason="principal_mismatch",
            memory_scope=memory.memory_scope,
            scope_key=memory.scope_key,
        )
        _log_policy_decision(ctx=ctx, decision=decision, surface=surface)
        await log_memory_audit(
            action="memory.policy_deny",
            ctx=ctx,
            request=request,
            memory_scope=memory.memory_scope.value,
            scope_key=memory.scope_key,
            project_id=serialization.memory_project_id(memory),
            source_surface=surface,
            policy_allowed=False,
            policy_reason=decision.reason,
            details={"policy_action": action.value},
        )
        raise HTTPException(status_code=403, detail=decision.reason)
    return await authorize_memory_policy(
        ctx=ctx,
        action=action,
        memory_scope=memory.memory_scope.value,
        scope_key=memory.scope_key,
        project_id=serialization.memory_project_id(memory),
        agent_id=memory.agent_id,
        surface=surface,
        request=request,
    )


def validate_memory_audit_action(action: str | None) -> None:
    if action and not action.startswith("memory."):
        raise HTTPException(status_code=400, detail="invalid_memory_audit_action")


def promotion_policy_allowed(result: ReflectionPromotionResult) -> bool | None:
    metadata = dict(result.metadata or {})
    raw_allowed = metadata.get("policy_allowed")
    if isinstance(raw_allowed, bool):
        return raw_allowed
    policy_reasons = metadata.get("policy_reasons")
    if isinstance(policy_reasons, list) and policy_reasons:
        return result.success
    if result.success:
        return True
    return None


async def accessible_projects_for_promotion(
    *,
    ctx: AuthContext,
    request: ReflectionPromotionRequest,
    http_request: Request | None = None,
) -> set[str]:
    project_ids: set[str] = set()
    if request.project:
        project_ids.add(request.project)
    if request.promote_to_scope == "project":
        target_project = request.promote_to_scope_key or request.project
        if target_project:
            project_ids.add(target_project)

    for project_id in project_ids:
        await authorize_project_filter(
            ctx=ctx,
            project_id=project_id,
            required_project_role=ProjectRole.CONTRIBUTOR,
            surface="reflection_promote",
            memory_scope=request.promote_to_scope,
            scope_key=request.promote_to_scope_key or request.project,
            policy_action="promote",
            request=http_request,
        )

    if project_ids:
        return project_ids
    accessible_projects = await list_accessible_project_graph_ids(ctx)
    return {str(project_id) for project_id in accessible_projects or set()}


def _promotion_target_scope(
    request: ReflectionPromotionRequest,
) -> tuple[str, str | None] | None:
    if request.promote_to_scope is None:
        return None
    try:
        target_scope = MemoryScope(request.promote_to_scope)
    except ValueError:
        return None
    target_scope_key = request.promote_to_scope_key
    if target_scope is MemoryScope.PROJECT:
        target_scope_key = target_scope_key or request.project
    return target_scope.value, target_scope_key


async def authorize_raw_promotion_api_key_scopes(
    *,
    ctx: AuthContext,
    request: ReflectionPromotionRequest,
    organization_id: str,
    accessible_projects: set[str],
    http_request: Request | None,
    surface: str,
) -> None:
    allowed_scope_keys = ctx.api_key_memory_scope_keys
    if allowed_scope_keys is None or not isinstance(
        allowed_scope_keys, list | tuple | set | frozenset
    ):
        return

    memory = await get_raw_memory(
        organization_id=organization_id,
        memory_id=request.candidate_id,
    )
    if memory is None:
        return

    checks: tuple[tuple[MemoryPolicyAction, str, str | None, str | None], ...] = (
        (
            MemoryPolicyAction.READ,
            memory.memory_scope.value,
            memory.scope_key,
            serialization.memory_project_id(memory),
        ),
    )
    target_scope = _promotion_target_scope(request)
    if target_scope is not None:
        checks = (
            *checks,
            (MemoryPolicyAction.WRITE, target_scope[0], target_scope[1], request.project),
        )

    for action, memory_scope, scope_key, project_id in checks:
        if api_key_memory_scope_allowed(ctx, memory_scope=memory_scope, scope_key=scope_key):
            continue
        policy_context = MemoryPolicyContext(
            actor_user_id=ctx.user_id,
            organization_id=ctx.organization_id,
            organization_role=ctx.org_role,
            memory_space=memory_scope,
            scope_key=scope_key,
            project_id=project_id,
            accessible_projects=accessible_projects,
            source_surface=surface,
        )
        deny_decision = _api_key_memory_scope_denial(
            action=action,
            memory_scope=memory_scope,
            scope_key=scope_key,
            policy_context=policy_context,
        )
        _log_policy_decision(ctx=ctx, decision=deny_decision, surface=surface)
        await log_memory_audit(
            action="memory.policy_deny",
            ctx=ctx,
            request=http_request,
            memory_scope=memory_scope,
            scope_key=scope_key,
            project_id=project_id,
            source_surface=surface,
            source_ids=[request.candidate_id],
            policy_allowed=False,
            policy_reason=deny_decision.reason,
            details={"policy_action": deny_decision.action.value},
        )
        raise HTTPException(
            status_code=_policy_http_status(deny_decision.reason),
            detail=deny_decision.reason,
        )


def _share_target_policy_scope(
    request: MemorySharePreviewRequest,
) -> tuple[str, str | None, str | None]:
    target_scope = str(request.target_scope)
    target_scope_key = request.target_scope_key
    project_id = request.project_id
    if target_scope == "project":
        target_scope_key = target_scope_key or request.project_id
        project_id = project_id or target_scope_key
    return target_scope, target_scope_key, project_id


async def authorize_share_api_key_scopes(
    *,
    ctx: AuthContext,
    request: MemorySharePreviewRequest,
    organization_id: str,
    accessible_projects: set[str],
    accessible_teams: set[str] | None,
    http_request: Request | None,
    surface: str,
) -> None:
    allowed_scope_keys = ctx.api_key_memory_scope_keys
    if allowed_scope_keys is None or not isinstance(
        allowed_scope_keys, list | tuple | set | frozenset
    ):
        return

    target_scope, target_scope_key, target_project_id = _share_target_policy_scope(request)
    target_denied = not api_key_memory_scope_allowed(
        ctx,
        memory_scope=target_scope,
        scope_key=target_scope_key,
    )
    checks: list[tuple[MemoryPolicyAction, str, str | None, str | None, str | None]] = (
        [
            (
                MemoryPolicyAction.WRITE,
                target_scope,
                target_scope_key,
                target_project_id,
                None,
            )
        ]
        if target_denied
        else []
    )
    if not target_denied:
        for source_id in request.source_ids:
            memory = await get_raw_memory(
                organization_id=organization_id,
                memory_id=source_id,
            )
            if memory is None:
                continue
            checks.append(
                (
                    MemoryPolicyAction.READ,
                    memory.memory_scope.value,
                    memory.scope_key,
                    serialization.memory_project_id(memory),
                    source_id,
                )
            )

    for action, memory_scope, scope_key, project_id, source_id in checks:
        if api_key_memory_scope_allowed(ctx, memory_scope=memory_scope, scope_key=scope_key):
            continue
        policy_context = MemoryPolicyContext(
            actor_user_id=ctx.user_id,
            organization_id=ctx.organization_id,
            organization_role=ctx.org_role,
            memory_space=memory_scope,
            scope_key=scope_key,
            project_id=project_id,
            accessible_projects=accessible_projects,
            accessible_teams=accessible_teams,
            source_surface=surface,
        )
        deny_decision = _api_key_memory_scope_denial(
            action=action,
            memory_scope=memory_scope,
            scope_key=scope_key,
            policy_context=policy_context,
        )
        _log_policy_decision(ctx=ctx, decision=deny_decision, surface=surface)
        await log_memory_audit(
            action="memory.policy_deny",
            ctx=ctx,
            request=http_request,
            memory_scope=memory_scope,
            scope_key=scope_key,
            project_id=project_id,
            source_surface=surface,
            source_ids=[source_id] if source_id else list(request.source_ids),
            policy_allowed=False,
            policy_reason=deny_decision.reason,
            details={"policy_action": deny_decision.action.value},
        )
        raise HTTPException(
            status_code=_policy_http_status(deny_decision.reason),
            detail=deny_decision.reason,
        )


async def accessible_projects_for_share_preview(
    *,
    ctx: AuthContext,
    request: MemorySharePreviewRequest,
    http_request: Request | None = None,
) -> set[str]:
    target_project = request.target_scope_key if request.target_scope == "project" else None
    project_ids = {project_id for project_id in (target_project, request.project_id) if project_id}
    for project_id in project_ids:
        await authorize_project_filter(
            ctx=ctx,
            project_id=project_id,
            required_project_role=ProjectRole.CONTRIBUTOR,
            surface="memory_share_preview",
            memory_scope=request.target_scope,
            scope_key=request.target_scope_key,
            policy_action="share_preview",
            request=http_request,
        )

    accessible_projects = await list_accessible_project_graph_ids(ctx)
    projects = {str(project_id) for project_id in accessible_projects or set()}
    projects.update(project_ids)
    return projects


async def accessible_teams_for_share(
    *,
    ctx: AuthContext,
) -> set[str] | None:
    accessible_teams = await list_accessible_team_scope_keys(ctx)
    return {str(team_id) for team_id in accessible_teams or set()}
