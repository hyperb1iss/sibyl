"""Memory authorization and relationship validation for MCP tools."""

import structlog

import sibyl.mcp_tools.context as mcp_context
from sibyl.auth.api_key_common import api_key_memory_scope_key
from sibyl.auth.memory_targets import RelationshipReaderScope, validate_relationship_targets
from sibyl_core.auth.memory_policy import (
    MemoryPolicyAction,
    MemoryPolicyDecision,
    authorize_memory_write,
)
from sibyl_core.services.surreal_content import MemoryScope

log = structlog.get_logger()


def log_policy_decision(
    *,
    ctx: mcp_context.McpContext,
    decision: MemoryPolicyDecision,
    surface: str,
) -> None:
    log.info(
        "mcp_memory_policy_decision",
        action=decision.action.value,
        allowed=decision.allowed,
        memory_scope=decision.memory_scope.value,
        organization_id=ctx.org_id,
        policy_reason=decision.reason,
        principal_id=ctx.user_id,
        scope_key=decision.scope_key,
        surface=surface,
    )


def memory_scope_allowed(
    ctx: mcp_context.McpContext, *, memory_scope: str, scope_key: str | None
) -> bool:
    allowed = ctx.api_key_memory_scope_keys
    if allowed is None:
        return True
    effective_scope_key = ctx.user_id if memory_scope == MemoryScope.PRIVATE.value else scope_key
    return api_key_memory_scope_key(memory_scope, effective_scope_key) in set(allowed)


def context_pack_scope_allowed(
    ctx: mcp_context.McpContext,
    *,
    project: str | None,
    accessible_projects: set[str] | None,
) -> bool:
    if project:
        return memory_scope_allowed(ctx, memory_scope=MemoryScope.PROJECT.value, scope_key=project)
    allowed = ctx.api_key_memory_scope_keys
    if allowed is None:
        return True
    allowed_keys = set(allowed)
    if memory_scope_allowed(ctx, memory_scope=MemoryScope.PRIVATE.value, scope_key=None):
        return True
    if accessible_projects is None:
        return False
    return any(
        api_key_memory_scope_key(MemoryScope.PROJECT.value, project_id) in allowed_keys
        for project_id in accessible_projects
    )


def deny_api_key_memory_scope(
    *,
    ctx: mcp_context.McpContext,
    action: MemoryPolicyAction,
    memory_scope: str,
    scope_key: str | None,
    surface: str,
) -> None:
    try:
        normalized_scope = MemoryScope(memory_scope)
    except ValueError:
        normalized_scope = MemoryScope.PRIVATE
    decision = MemoryPolicyDecision(
        action=action,
        allowed=False,
        reason="api_key_memory_space_denied",
        memory_scope=normalized_scope,
        scope_key=scope_key,
    )
    log_policy_decision(ctx=ctx, decision=decision, surface=surface)
    raise ValueError(decision.reason)


def authorize_memory_write_request(
    *,
    ctx: mcp_context.McpContext,
    memory_scope: str,
    scope_key: str | None,
    accessible_projects: set[str] | None,
    surface: str,
    accessible_teams: set[str] | None = None,
) -> MemoryPolicyDecision:
    policy_context = ctx.to_memory_policy_context(
        memory_space=memory_scope,
        scope_key=scope_key,
        project_id=scope_key,
        accessible_projects=accessible_projects,
        accessible_teams=accessible_teams,
        source_surface=surface,
    )
    decision = authorize_memory_write(
        policy_context=policy_context,
    )
    log_policy_decision(ctx=ctx, decision=decision, surface=surface)
    if not decision.allowed:
        raise ValueError(decision.reason)
    if not memory_scope_allowed(ctx, memory_scope=memory_scope, scope_key=scope_key):
        deny_api_key_memory_scope(
            ctx=ctx,
            action=MemoryPolicyAction.WRITE,
            memory_scope=memory_scope,
            scope_key=scope_key,
            surface=surface,
        )
    return decision


def append_unique_ids(existing: list[str] | None, additions: list[str] | None) -> list[str] | None:
    links = list(existing or [])
    seen = set(links)
    for item in additions or []:
        if item not in seen:
            links.append(item)
            seen.add(item)
    return links or None


async def resolve_capture_links(
    *,
    ctx: mcp_context.McpContext,
    project: str | None,
    related_to: list[str] | None,
    task_ids: list[str] | None,
    active_task: bool,
    accessible_projects: set[str] | None,
) -> list[str] | None:
    links = append_unique_ids(related_to, task_ids)
    if not active_task or not project:
        return links

    from sibyl_core.tools.core import explore

    try:
        response = await explore(
            mode="list",
            types=["task"],
            project=project,
            status="doing",
            limit=2,
            organization_id=ctx.org_id,
            principal_id=ctx.user_id,
            accessible_projects=accessible_projects,
            allowed_memory_scope_keys=ctx.api_key_memory_scope_keys,
        )
    except Exception as exc:
        log.warning("mcp_active_task_lookup_failed", project=project, error=str(exc))
        return links

    entities = getattr(response, "entities", [])
    if len(entities) != 1:
        return links

    task_id = getattr(entities[0], "id", None)
    if not task_id:
        return links

    return append_unique_ids(links, [str(task_id)])


async def validate_relationship_targets_for_caller(
    *,
    ctx: mcp_context.McpContext,
    related_to: list[str] | None,
    accessible_projects: set[str] | None,
) -> None:
    if not related_to:
        return
    from sibyl_core.services.graph import get_surreal_graph_runtime

    runtime = await get_surreal_graph_runtime(ctx.org_id)
    await validate_relationship_targets(
        entity_manager=runtime.entity_manager,
        related_to=related_to,
        scope=RelationshipReaderScope.from_values(
            user_id=ctx.user_id,
            accessible_projects=accessible_projects,
            memory_grants=ctx.api_key_memory_scope_keys,
        ),
    )
