"""MCP Server definition using FastMCP with streamable-http transport.

Exposes 11 tools and 2 resources:
- Tools: search, explore, context, remember, reflect, add, manage, logs,
  synthesis_plan, synthesis_draft, synthesis_verify
- Resources: sibyl://health, sibyl://stats
"""

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal
from uuid import UUID

import structlog
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP

from sibyl.api.context_audit import log_context_pack_audit, log_reflection_audit
from sibyl.auth.api_key_common import api_key_memory_scope_key
from sibyl.config import settings
from sibyl.persistence.auth_runtime import (
    authenticate_api_key,
    create_project_record,
    has_owner_membership,
    resolve_accessible_project_graph_ids,
    resolve_org_role,
)
from sibyl.services.recall_limits import (
    RecallConcurrencyLimitExceededError,
    recall_concurrency_slot,
)
from sibyl.services.work_item_workflow import WorkItemAction
from sibyl_core.auth.context import MemoryPolicyContext
from sibyl_core.auth.memory_policy import (
    MemoryPolicyAction,
    MemoryPolicyDecision,
    authorize_memory_write,
)
from sibyl_core.memory_pipeline.capture import MemoryCaptureRequest, MemoryCaptureService
from sibyl_core.services.surreal_content import MemoryScope

log = structlog.get_logger()

MemoryKind = Literal[
    "episode",
    "decision",
    "plan",
    "idea",
    "claim",
    "artifact",
    "procedure",
    "domain",
    "session",
    "pattern",
    "rule",
]
SynthesisOutputKind = Literal[
    "documentation",
    "report",
    "briefing",
    "roadmap",
    "release_notes",
    "audit_packet",
    "custom",
]
SynthesisDepthKind = Literal["brief", "standard", "deep"]
SynthesisArtifactKind = Literal["markdown", "json"]

MCP_ENTITY_PROJECT_POLICY_ACTIONS = {
    "add_note",
    "archive_epic",
    "archive_task",
    "block_task",
    "complete_epic",
    "complete_task",
    "estimate",
    "start_epic",
    "start_task",
    "submit_review",
    "suggest",
    "unblock_task",
    "update_epic",
    "update_task",
}
MCP_PROJECT_ID_POLICY_ACTIONS = {"detect_cycles", "prioritize"}


@dataclass(frozen=True)
class McpContext:
    """Context extracted from MCP authentication token."""

    org_id: str
    user_id: str | None = None
    scopes: list[str] | None = None
    # API key project restrictions (None = all, list = only these)
    api_key_project_ids: list[str] | None = None
    api_key_memory_space_ids: list[str] | None = None
    api_key_memory_scope_keys: list[str] | None = None
    org_role: str | None = None
    delegated_authority: str | None = None
    agent_id: str | None = None

    def to_memory_policy_context(
        self,
        *,
        memory_space: str | None = None,
        scope_key: str | None = None,
        project_id: str | None = None,
        accessible_projects: Iterable[str] | None = None,
        accessible_delegations: Iterable[str] | None = None,
        source_surface: str = "mcp",
    ) -> MemoryPolicyContext:
        return MemoryPolicyContext(
            actor_user_id=self.user_id,
            organization_id=self.org_id,
            organization_role=self.org_role,
            accessible_projects=frozenset(str(value) for value in accessible_projects)
            if accessible_projects is not None
            else None,
            accessible_delegations=frozenset(str(value) for value in accessible_delegations)
            if accessible_delegations is not None
            else None,
            delegated_authority=self.delegated_authority,
            agent_id=self.agent_id,
            project_id=project_id,
            memory_space=memory_space,
            scope_key=scope_key,
            source_surface=source_surface,
        )


async def _get_mcp_context() -> McpContext | None:
    """Extract full context (org_id, user_id, scopes) from MCP token.

    Returns:
        McpContext if authenticated, None otherwise.
    """
    token = get_access_token()
    if token is None:
        return None

    raw = token.token
    if not raw:
        return None

    # API Key authentication
    if raw.startswith("sk_"):
        auth = await authenticate_api_key(raw)
        if auth:
            # Convert project UUIDs to graph IDs (strings)
            project_ids = (
                [str(pid) for pid in auth.project_ids] if auth.project_ids is not None else None
            )
            return McpContext(
                org_id=str(auth.organization_id),
                user_id=str(auth.user_id),
                scopes=auth.scopes,
                api_key_project_ids=project_ids,
                api_key_memory_space_ids=[
                    str(memory_space_id)
                    for memory_space_id in getattr(auth, "memory_space_ids", None) or []
                ]
                if getattr(auth, "memory_space_ids", None) is not None
                else None,
                api_key_memory_scope_keys=[
                    memory_space.policy_key
                    for memory_space in getattr(auth, "memory_spaces", None) or []
                ]
                if getattr(auth, "memory_spaces", None) is not None
                else None,
            )
        return None

    # JWT authentication
    from sibyl.auth.jwt import JwtError, verify_access_token

    try:
        claims = verify_access_token(raw)
    except JwtError:
        return None

    org_id = claims.get("org")
    user_id = claims.get("sub")

    if org_id:
        log.debug("mcp_context", org_id=org_id, user_id=user_id)
        org_role = await resolve_org_role(
            org_id=str(org_id),
            user_id=str(user_id) if user_id else None,
        )
        return McpContext(
            org_id=str(org_id),
            user_id=str(user_id) if user_id else None,
            scopes=claims.get("scopes"),
            org_role=org_role,
        )
    return None


async def _get_org_id_from_context() -> str | None:
    """Extract organization ID from the authenticated MCP context.

    Returns:
        The organization ID string if authenticated and org-scoped, None otherwise.
    """
    ctx = await _get_mcp_context()
    return ctx.org_id if ctx else None


async def _require_mcp_context() -> McpContext:
    """Require full MCP context including user_id.

    Raises:
        ValueError: If no context is available.

    Returns:
        McpContext with org_id and user_id.
    """
    ctx = await _get_mcp_context()
    if not ctx:
        raise ValueError("Organization context required. Authenticate with an org-scoped token.")
    return ctx


async def _get_accessible_projects(ctx: McpContext) -> set[str] | None:
    """Get project IDs the user can access based on their permissions.

    Combines user permissions with API key project restrictions (if any).

    Returns:
        Set of accessible project graph IDs, or None if no filtering needed (admin).
    """
    if not ctx.user_id:
        # No user context - can't filter by user permissions
        # But still enforce API key restrictions if present
        if ctx.api_key_project_ids is not None:
            return set(ctx.api_key_project_ids)
        return None

    return await resolve_accessible_project_graph_ids(
        user_id=ctx.user_id,
        org_id=ctx.org_id,
        scopes=ctx.scopes,
        api_key_project_ids=ctx.api_key_project_ids,
    )


async def _resolve_mcp_project_scope(
    ctx: McpContext,
    project: str | None,
    *,
    require_project_when_restricted: bool = False,
) -> set[str] | None:
    """Resolve accessible project scope for MCP tools."""
    accessible_projects = await _get_accessible_projects(ctx)
    if accessible_projects is None:
        if project:
            return {project}
        return None
    if project:
        if project not in accessible_projects:
            raise ValueError(f"Project access denied: {project}")
        return {project}
    if require_project_when_restricted:
        raise ValueError("Project is required when MCP credentials are project-scoped.")
    return accessible_projects


async def _compile_mcp_context_pack(
    *,
    goal: str,
    intent: Literal["build", "plan", "ideate", "research", "debug", "decide", "learn", "general"],
    layer: Literal["wake", "recall", "deep_search"],
    domain: str | None,
    project: str | None,
    agent_id: str | None,
    limit: int,
    include_related: bool,
    related_limit: int,
    audit: bool = False,
    markdown_token_budget: int | None = None,
) -> dict[str, Any]:
    from sibyl_core.tools.core import (
        compile_context as _compile_context,
        context_pack_to_dict,
        context_pack_to_markdown,
    )

    ctx = await _require_mcp_context()
    accessible_projects = await _resolve_mcp_project_scope(ctx, project)
    memory_scope = "project" if project else "private"
    scope_key = project
    if not _mcp_context_pack_scope_allowed(
        ctx,
        project=project,
        accessible_projects=accessible_projects,
    ):
        _deny_mcp_api_key_memory_scope(
            ctx=ctx,
            action=MemoryPolicyAction.READ,
            memory_scope=memory_scope,
            scope_key=scope_key,
            surface="mcp_context",
        )
    if ctx.user_id is None:
        raise ValueError("User context required for recall.")
    try:
        async with recall_concurrency_slot(
            organization_id=ctx.org_id,
            user_id=ctx.user_id,
            organization_role=ctx.org_role,
        ):
            pack = await _compile_context(
                goal=goal,
                intent=intent,
                layer=layer,
                domain=domain,
                project=project,
                accessible_projects=accessible_projects,
                principal_id=ctx.user_id,
                agent_id=agent_id,
                limit=limit,
                include_related=include_related,
                related_limit=related_limit,
                audit=audit,
                organization_id=ctx.org_id,
                allowed_memory_scope_keys=set(ctx.api_key_memory_scope_keys)
                if ctx.api_key_memory_scope_keys is not None
                else None,
            )
    except RecallConcurrencyLimitExceededError as exc:
        raise ValueError("recall_concurrency_limit_exceeded") from exc
    payload = context_pack_to_dict(pack)
    payload["markdown"] = context_pack_to_markdown(pack, token_budget=markdown_token_budget)
    await log_context_pack_audit(
        user_id=ctx.user_id,
        organization_id=ctx.org_id,
        pack=pack,
        project=project,
        accessible_projects=accessible_projects,
        source_surface="mcp_context",
        agent_id=agent_id,
        limit=limit,
        include_related=include_related,
        related_limit=related_limit,
    )
    return payload


def _log_mcp_policy_decision(
    *,
    ctx: McpContext,
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


def _mcp_memory_scope_allowed(ctx: McpContext, *, memory_scope: str, scope_key: str | None) -> bool:
    allowed = ctx.api_key_memory_scope_keys
    if allowed is None:
        return True
    effective_scope_key = ctx.user_id if memory_scope == MemoryScope.PRIVATE.value else scope_key
    return api_key_memory_scope_key(memory_scope, effective_scope_key) in set(allowed)


def _mcp_context_pack_scope_allowed(
    ctx: McpContext,
    *,
    project: str | None,
    accessible_projects: set[str] | None,
) -> bool:
    if project:
        return _mcp_memory_scope_allowed(
            ctx, memory_scope=MemoryScope.PROJECT.value, scope_key=project
        )
    allowed = ctx.api_key_memory_scope_keys
    if allowed is None:
        return True
    allowed_keys = set(allowed)
    if _mcp_memory_scope_allowed(ctx, memory_scope=MemoryScope.PRIVATE.value, scope_key=None):
        return True
    if accessible_projects is None:
        return False
    return any(
        api_key_memory_scope_key(MemoryScope.PROJECT.value, project_id) in allowed_keys
        for project_id in accessible_projects
    )


def _deny_mcp_api_key_memory_scope(
    *,
    ctx: McpContext,
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
    _log_mcp_policy_decision(ctx=ctx, decision=decision, surface=surface)
    raise ValueError(decision.reason)


def _authorize_mcp_memory_write(
    *,
    ctx: McpContext,
    memory_scope: str,
    scope_key: str | None,
    accessible_projects: set[str] | None,
    surface: str,
) -> MemoryPolicyDecision:
    policy_context = ctx.to_memory_policy_context(
        memory_space=memory_scope,
        scope_key=scope_key,
        project_id=scope_key,
        accessible_projects=accessible_projects,
        source_surface=surface,
    )
    decision = authorize_memory_write(
        policy_context=policy_context,
    )
    _log_mcp_policy_decision(ctx=ctx, decision=decision, surface=surface)
    if not decision.allowed:
        raise ValueError(decision.reason)
    if not _mcp_memory_scope_allowed(ctx, memory_scope=memory_scope, scope_key=scope_key):
        _deny_mcp_api_key_memory_scope(
            ctx=ctx,
            action=MemoryPolicyAction.WRITE,
            memory_scope=memory_scope,
            scope_key=scope_key,
            surface=surface,
        )
    return decision


def _append_unique_ids(existing: list[str] | None, additions: list[str] | None) -> list[str] | None:
    links = list(existing or [])
    seen = set(links)
    for item in additions or []:
        if item not in seen:
            links.append(item)
            seen.add(item)
    return links or None


async def _resolve_mcp_capture_links(
    *,
    ctx: McpContext,
    project: str | None,
    related_to: list[str] | None,
    task_ids: list[str] | None,
    active_task: bool,
) -> list[str] | None:
    links = _append_unique_ids(related_to, task_ids)
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

    return _append_unique_ids(links, [str(task_id)])


async def _remember_mcp_memory(
    *,
    title: str,
    content: str,
    kind: MemoryKind,
    domain: str | None,
    project: str | None,
    tags: list[str] | None,
    related_to: list[str] | None,
    task_ids: list[str] | None = None,
    active_task: bool = True,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from sibyl_core.services.surreal_content import remember_raw_memory
    from sibyl_core.tools.core import add

    ctx = await _require_mcp_context()
    accessible_projects = await _resolve_mcp_project_scope(
        ctx,
        project,
        require_project_when_restricted=True,
    )
    if not ctx.user_id:
        raise ValueError("User context required to remember raw source material.")
    memory_scope = "project" if project else "private"
    write_decision = _authorize_mcp_memory_write(
        ctx=ctx,
        memory_scope=memory_scope,
        scope_key=project,
        accessible_projects=accessible_projects,
        surface="mcp_remember",
    )

    full_metadata = dict(metadata or {})
    full_metadata["capture_kind"] = kind
    full_metadata["organization_id"] = ctx.org_id
    if domain:
        full_metadata["domain"] = domain
    if project:
        full_metadata["project_id"] = project
    if ctx.user_id:
        full_metadata["created_by"] = ctx.user_id
    resolved_links = await _resolve_mcp_capture_links(
        ctx=ctx,
        project=project,
        related_to=related_to,
        task_ids=task_ids,
        active_task=active_task,
    )
    capture_request = MemoryCaptureRequest(
        title=title,
        content=content,
        entity_type=kind,
        domain=domain,
        tags=tags,
        related_to=resolved_links,
        metadata=full_metadata,
        provenance={"remember_kind": kind, "related_to": resolved_links or []},
        source_id=f"mcp:remember:{kind}",
        memory_scope=memory_scope,
        scope_key=project,
        capture_surface="mcp",
    )

    async def remember_raw(
        request: MemoryCaptureRequest,
    ) -> Mapping[str, Any]:
        raw_memory = await remember_raw_memory(
            organization_id=ctx.org_id,
            principal_id=ctx.user_id,
            source_id=request.source_id,
            raw_content=request.content,
            title=request.title,
            memory_scope=request.memory_scope,
            scope_key=request.scope_key,
            tags=list(request.tags) if request.tags is not None else None,
            metadata=dict(request.metadata),
            provenance=dict(request.provenance),
            capture_surface=request.capture_surface,
        )
        return {"id": raw_memory.id, "source_id": raw_memory.source_id}

    async def create_graph_entity(
        request: MemoryCaptureRequest,
        graph_metadata: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        result = await add(
            title=request.title,
            content=request.content,
            entity_type=request.entity_type,
            category=request.domain,
            tags=list(request.tags) if request.tags is not None else None,
            related_to=list(request.related_to) if request.related_to is not None else None,
            metadata=dict(graph_metadata),
            project=project,
        )
        return _to_dict(result)

    capture_result = await MemoryCaptureService(
        remember_raw_memory=remember_raw,
        create_graph_entity=create_graph_entity,
    ).capture(capture_request)
    payload = capture_result.to_payload()
    if capture_result.raw_policy_reason is None:
        payload.pop("raw_policy_reason", None)
    payload["policy_reason"] = write_decision.reason
    return payload


async def _reflect_mcp_memory(
    *,
    content: str,
    source_title: str = "Session reflection",
    intent: Literal[
        "build", "plan", "ideate", "research", "debug", "decide", "learn", "general"
    ] = "general",
    domain: str | None = None,
    project: str | None = None,
    related_to: list[str] | None = None,
    task_ids: list[str] | None = None,
    active_task: bool = True,
    persist: bool = False,
    persist_source: bool = True,
    persist_review: bool = False,
    cited_ids: list[str] | str | None = None,
    limit: int = 12,
) -> dict[str, Any]:
    from sibyl_core.tools.core import (
        reflect_memory,
        reflection_pack_to_dict,
        reflection_pack_to_markdown,
    )

    ctx = await _require_mcp_context()
    accessible_projects = await _resolve_mcp_project_scope(
        ctx,
        project,
        require_project_when_restricted=persist,
    )
    resolved_links = await _resolve_mcp_capture_links(
        ctx=ctx,
        project=project,
        related_to=related_to,
        task_ids=task_ids,
        active_task=active_task and persist,
    )
    memory_scope = "project" if project else "private"
    scope_key = project
    if persist:
        _authorize_mcp_memory_write(
            ctx=ctx,
            memory_scope=memory_scope,
            scope_key=scope_key,
            accessible_projects=accessible_projects,
            surface="mcp_reflect",
        )
    pack = await reflect_memory(
        content=content,
        source_title=source_title,
        intent=intent,
        domain=domain,
        project=project,
        related_to=resolved_links,
        organization_id=ctx.org_id,
        principal_id=ctx.user_id,
        accessible_projects=accessible_projects,
        memory_scope=memory_scope,
        scope_key=scope_key,
        persist=persist,
        persist_source=persist_source,
        persist_review=persist_review,
        limit=limit,
    )
    payload = reflection_pack_to_dict(pack)
    payload["markdown"] = reflection_pack_to_markdown(pack)
    if cited_ids:
        from sibyl_core.tools.usage_citation import record_cited_item_usages

        try:
            payload["citation_usage"] = await record_cited_item_usages(
                cited_ids,
                organization_id=ctx.org_id,
                principal_id=ctx.user_id,
                project_id=project,
                source_surface="mcp_reflect",
                request_metadata={
                    "active_task": active_task,
                    "intent": intent,
                    "persist": persist,
                    "source_title": source_title,
                },
            )
        except Exception as exc:
            log.warning(
                "mcp_reflect_citation_usage_failed",
                error_type=type(exc).__name__,
                exc_info=True,
            )
    await log_reflection_audit(
        user_id=ctx.user_id,
        organization_id=ctx.org_id,
        pack=pack,
        project=project,
        accessible_projects=accessible_projects,
        source_surface="mcp_reflect",
        persist=persist,
        persist_source=persist_source,
        persist_review=persist_review,
        active_task=active_task,
        related_to=resolved_links,
        task_ids=task_ids,
        limit=limit,
    )
    return payload


async def _synthesis_mcp_plan(
    *,
    goal: str,
    output_type: SynthesisOutputKind = "documentation",
    audience: str | None = None,
    depth: SynthesisDepthKind = "standard",
    seed_query: str | None = None,
    project: str | None = None,
    domain: str | None = None,
    entity_ids: list[str] | None = None,
    decision_ids: list[str] | None = None,
    task_ids: list[str] | None = None,
    artifact_ids: list[str] | None = None,
    required_sections: list[dict[str, Any] | str] | None = None,
    constraints: list[str] | None = None,
    max_sections: int = 6,
    include_neighborhoods: bool = True,
) -> dict[str, Any]:
    from sibyl_core.tools.core import synthesis_plan

    ctx = await _require_mcp_context()
    accessible_projects = await _resolve_mcp_project_scope(ctx, project)
    return await synthesis_plan(
        goal=goal,
        output_type=output_type,
        audience=audience,
        depth=depth,
        seed_query=seed_query,
        project=project,
        domain=domain,
        entity_ids=entity_ids,
        decision_ids=decision_ids,
        task_ids=task_ids,
        artifact_ids=artifact_ids,
        required_sections=required_sections,
        constraints=constraints,
        max_sections=max_sections,
        include_neighborhoods=include_neighborhoods,
        organization_id=ctx.org_id,
        principal_id=ctx.user_id,
        accessible_projects=accessible_projects,
    )


async def _synthesis_mcp_verify(
    *,
    goal: str,
    output_type: SynthesisOutputKind = "documentation",
    audience: str | None = None,
    depth: SynthesisDepthKind = "standard",
    seed_query: str | None = None,
    project: str | None = None,
    domain: str | None = None,
    entity_ids: list[str] | None = None,
    decision_ids: list[str] | None = None,
    task_ids: list[str] | None = None,
    artifact_ids: list[str] | None = None,
    required_sections: list[dict[str, Any] | str] | None = None,
    constraints: list[str] | None = None,
    max_sections: int = 6,
    include_neighborhoods: bool = True,
) -> dict[str, Any]:
    from sibyl_core.tools.core import synthesis_verify

    ctx = await _require_mcp_context()
    accessible_projects = await _resolve_mcp_project_scope(ctx, project)
    return await synthesis_verify(
        goal=goal,
        output_type=output_type,
        audience=audience,
        depth=depth,
        seed_query=seed_query,
        project=project,
        domain=domain,
        entity_ids=entity_ids,
        decision_ids=decision_ids,
        task_ids=task_ids,
        artifact_ids=artifact_ids,
        required_sections=required_sections,
        constraints=constraints,
        max_sections=max_sections,
        include_neighborhoods=include_neighborhoods,
        organization_id=ctx.org_id,
        principal_id=ctx.user_id,
        accessible_projects=accessible_projects,
    )


async def _synthesis_mcp_draft(
    *,
    goal: str,
    output_type: SynthesisOutputKind = "documentation",
    audience: str | None = None,
    depth: SynthesisDepthKind = "standard",
    seed_query: str | None = None,
    project: str | None = None,
    domain: str | None = None,
    entity_ids: list[str] | None = None,
    decision_ids: list[str] | None = None,
    task_ids: list[str] | None = None,
    artifact_ids: list[str] | None = None,
    required_sections: list[dict[str, Any] | str] | None = None,
    constraints: list[str] | None = None,
    max_sections: int = 6,
    include_neighborhoods: bool = True,
    output_format: SynthesisArtifactKind = "markdown",
    remember: bool = False,
    memory_scope: str = "private",
    scope_key: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    from sibyl_core.tools.core import synthesis_draft

    ctx = await _require_mcp_context()
    accessible_projects = await _resolve_mcp_project_scope(ctx, project)
    resolved_scope_key = scope_key
    policy_reason: str | None = None
    if remember:
        write_accessible_projects = accessible_projects
        if memory_scope == "project":
            resolved_scope_key = resolved_scope_key or project
            write_accessible_projects = await _resolve_mcp_project_scope(
                ctx,
                resolved_scope_key,
                require_project_when_restricted=True,
            )
        decision = _authorize_mcp_memory_write(
            ctx=ctx,
            memory_scope=memory_scope,
            scope_key=resolved_scope_key,
            accessible_projects=write_accessible_projects,
            surface="mcp_synthesis",
        )
        policy_reason = decision.reason

    payload = await synthesis_draft(
        goal=goal,
        output_type=output_type,
        audience=audience,
        depth=depth,
        seed_query=seed_query,
        project=project,
        domain=domain,
        entity_ids=entity_ids,
        decision_ids=decision_ids,
        task_ids=task_ids,
        artifact_ids=artifact_ids,
        required_sections=required_sections,
        constraints=constraints,
        max_sections=max_sections,
        include_neighborhoods=include_neighborhoods,
        output_format=output_format,
        remember=remember,
        memory_scope=memory_scope,
        scope_key=resolved_scope_key,
        tags=tags,
        organization_id=ctx.org_id,
        principal_id=ctx.user_id,
        accessible_projects=accessible_projects,
    )
    if policy_reason:
        payload["policy_reason"] = policy_reason
    return payload


async def _add_mcp_entity(
    *,
    title: str,
    content: str,
    entity_type: str,
    category: str | None,
    languages: list[str] | None,
    tags: list[str] | None,
    related_to: list[str] | None,
    metadata: dict[str, Any] | None,
    project: str | None,
    priority: str | None,
    assignees: list[str] | None,
    due_date: str | None,
    technologies: list[str] | None,
    depends_on: list[str] | None,
    repository_url: str | None,
    check_conflicts: bool = True,
    skip_conflicts: bool = False,
    conflict_threshold: float = 0.85,
) -> dict[str, Any]:
    from sibyl_core.tools.core import add

    ctx = await _require_mcp_context()
    normalized_entity_type = entity_type.strip().lower()
    accessible_projects = await _resolve_mcp_project_scope(
        ctx,
        project,
        require_project_when_restricted=True,
    )
    memory_scope = "project" if project else "private"
    scope_key = project
    write_decision = _authorize_mcp_memory_write(
        ctx=ctx,
        memory_scope=memory_scope,
        scope_key=scope_key,
        accessible_projects=accessible_projects,
        surface="mcp_add",
    )

    full_metadata = dict(metadata or {})
    full_metadata["organization_id"] = ctx.org_id
    if ctx.user_id:
        full_metadata["created_by"] = ctx.user_id

    add_kwargs = {
        "title": title,
        "content": content,
        "entity_type": entity_type,
        "category": category,
        "languages": languages,
        "tags": tags,
        "related_to": related_to,
        "metadata": full_metadata,
        "project": project,
        "priority": priority,
        "assignees": assignees,
        "due_date": due_date,
        "technologies": technologies,
        "depends_on": depends_on,
        "repository_url": repository_url,
        "check_conflicts": check_conflicts,
        "skip_conflicts": skip_conflicts,
        "conflict_threshold": max(conflict_threshold, 0.85),
    }
    if normalized_entity_type == "project":
        add_kwargs["sync"] = True

    result = await add(**add_kwargs)
    payload = _to_dict(result)
    if normalized_entity_type == "project" and payload.get("success") and payload.get("id"):
        if not ctx.user_id:
            raise ValueError("user_context_required")
        await create_project_record(
            organization_id=UUID(ctx.org_id),
            owner_user_id=UUID(ctx.user_id),
            graph_project_id=str(payload["id"]),
            name=title,
            description=content,
        )
    payload["policy_reason"] = write_decision.reason
    return payload


async def _mcp_entity_project_id(*, organization_id: str, entity_id: str) -> str | None:
    from sibyl_core.services.graph import get_surreal_graph_runtime
    from sibyl_core.tools.helpers import _project_id_for_policy

    runtime = await get_surreal_graph_runtime(organization_id)
    entity = await runtime.entity_manager.get(entity_id)
    if entity is None:
        return None
    return _project_id_for_policy(entity)


async def _authorize_mcp_manage_action(
    *,
    ctx: McpContext,
    action: str,
    entity_id: str | None,
    accessible_projects: set[str] | None,
) -> MemoryPolicyDecision | None:
    normalized_action = action.lower().strip()
    if normalized_action in MCP_PROJECT_ID_POLICY_ACTIONS:
        project_id = entity_id
    elif normalized_action in MCP_ENTITY_PROJECT_POLICY_ACTIONS:
        if not entity_id:
            return None
        project_id = await _mcp_entity_project_id(
            organization_id=ctx.org_id,
            entity_id=entity_id,
        )
    else:
        return None

    policy_projects = (
        {project_id} if accessible_projects is None and project_id else accessible_projects
    )
    return _authorize_mcp_memory_write(
        ctx=ctx,
        memory_scope="project",
        scope_key=project_id,
        accessible_projects=policy_projects,
        surface="mcp_manage",
    )


# MCP transition action -> the unified work-item action. Routing these through
# the shared apps/api workflow service is what gives the MCP path the entity
# lock, WebSocket broadcast, and project-activity bump that REST already had and
# the old core manage() body silently skipped (audit H8).
_MCP_WORKFLOW_TRANSITIONS: dict[str, WorkItemAction] = {
    "start_task": WorkItemAction.START_TASK,
    "block_task": WorkItemAction.BLOCK_TASK,
    "unblock_task": WorkItemAction.UNBLOCK_TASK,
    "submit_review": WorkItemAction.SUBMIT_REVIEW,
    "complete_task": WorkItemAction.COMPLETE_TASK,
    "archive_task": WorkItemAction.ARCHIVE_TASK,
    "start_epic": WorkItemAction.START_EPIC,
    "complete_epic": WorkItemAction.COMPLETE_EPIC,
    "archive_epic": WorkItemAction.ARCHIVE_EPIC,
}


def _mcp_transition_message(action: str, *, learnings: str | None, reason: str | None) -> str:
    """Mirror core manage()'s per-action success message for MCP clients."""
    if action == "start_task":
        return "Task started"
    if action == "block_task":
        return f"Task blocked: {reason or 'No reason provided'}"
    if action == "unblock_task":
        return "Task unblocked, resuming work"
    if action == "submit_review":
        return "Task submitted for review"
    if action == "complete_task":
        return "Task completed" + (" with learnings captured" if learnings else "")
    if action == "archive_task":
        return "Task archived"
    if action == "start_epic":
        return "Epic started"
    if action == "complete_epic":
        return "Epic completed" + (" with learnings captured" if learnings else "")
    # archive_epic
    return "Epic archived" + (f": {reason}" if reason else "")


async def _manage_workflow_transition(
    *,
    ctx: McpContext,
    action: str,
    work_item_action: WorkItemAction,
    entity_id: str | None,
    data: dict[str, Any],
    policy_decision: MemoryPolicyDecision | None,
) -> dict[str, Any]:
    """Run an MCP task/epic transition through the shared workflow service.

    The lock, broadcast, and project-activity bump come from the service by
    construction. The response is shaped like core manage()'s ManageResponse so
    MCP clients see no change, including the deprecation pointer.
    """
    from sibyl.locks import LockAcquisitionError
    from sibyl.services.work_item_workflow import EPIC_TRANSITIONS, transition_work_item
    from sibyl_core.errors import EntityNotFoundError, InvalidTransitionError
    from sibyl_core.tools.manage import _deprecation_notice

    def _response(
        *,
        success: bool,
        message: str,
        action_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "success": success,
            "action": action,
            "entity_id": entity_id,
            "message": message,
            "data": action_data or {},
        }
        deprecation = _deprecation_notice(action)
        if deprecation is not None:
            payload["data"].setdefault("deprecation", deprecation)
        if policy_decision is not None:
            payload["policy_reason"] = policy_decision.reason
        return payload

    if not entity_id:
        suffix = "epic actions" if work_item_action in EPIC_TRANSITIONS else "task actions"
        return _response(success=False, message=f"entity_id required for {suffix}")

    entity: Any | None = None
    if work_item_action in EPIC_TRANSITIONS:
        from sibyl_core.models.entities import EntityType
        from sibyl_core.services.graph import get_surreal_graph_runtime

        runtime = await get_surreal_graph_runtime(ctx.org_id)
        try:
            entity = await runtime.entity_manager.get(entity_id)
        except EntityNotFoundError:
            entity = None
        if not entity:
            return _response(success=False, message=f"Epic not found: {entity_id}")
        if entity.entity_type != EntityType.EPIC:
            return _response(success=False, message=f"Entity is not an epic: {entity_id}")

    learnings = data.get("learnings") if action in {"complete_task", "complete_epic"} else None
    reason = data.get("reason") if action in {"block_task", "archive_epic"} else None

    try:
        result = await transition_work_item(
            ctx.org_id,
            entity_id,
            work_item_action,
            payload=data,
            entity=entity,
        )
    except InvalidTransitionError as exc:
        return _response(success=False, message=str(exc), action_data=exc.details)
    except LockAcquisitionError:
        return _response(
            success=False,
            message=f"{entity_id} is locked by another writer; retry shortly",
        )
    except (EntityNotFoundError, KeyError):
        return _response(success=False, message=f"Work item not found: {entity_id}")

    # complete_task with learnings enqueues the same background learning jobs the
    # REST surface does, using the policy context the authz step resolved.
    response_data = dict(result.response_data)
    cited_ids = data.get("cited_ids") if action == "complete_task" else None
    if cited_ids:
        from sibyl_core.tools.usage_citation import record_cited_item_usages

        policy_context = policy_decision.policy_context if policy_decision else None
        policy_project_id = None
        if policy_context is not None:
            policy_project_id = policy_context.project_id or (
                policy_context.scope_key if policy_context.memory_space == "project" else None
            )
        try:
            response_data["citation_usage"] = await record_cited_item_usages(
                cited_ids,
                organization_id=ctx.org_id,
                principal_id=ctx.user_id,
                project_id=result.task_data.get("project_id") or policy_project_id,
                source_surface="mcp_manage_complete_task",
                request_metadata={
                    "action": action,
                    "has_learnings": bool(learnings),
                    "task_id": entity_id,
                },
            )
        except Exception as exc:
            log.warning(
                "mcp_complete_task_citation_usage_failed",
                task_id=entity_id,
                error_type=type(exc).__name__,
                exc_info=True,
            )
    if (
        action == "complete_task"
        and learnings
        and policy_decision is not None
        and policy_decision.policy_context is not None
    ):
        from sibyl.jobs.entities import serialize_memory_policy_context
        from sibyl.jobs.queue import (
            enqueue_create_learning_episode,
            enqueue_create_learning_procedure,
        )

        policy_payload = serialize_memory_policy_context(policy_decision.policy_context)
        episode_job_id = await enqueue_create_learning_episode(
            result.task_data, ctx.org_id, policy_context=policy_payload
        )
        procedure_job_id = await enqueue_create_learning_procedure(
            result.task_data, ctx.org_id, policy_context=policy_payload
        )
        response_data["learning_episode_job_id"] = episode_job_id
        response_data["learning_procedure_job_id"] = procedure_job_id

    return _response(
        success=True,
        message=_mcp_transition_message(action, learnings=learnings, reason=reason),
        action_data=response_data,
    )


async def _manage_mcp_action(
    *,
    action: str,
    entity_id: str | None,
    data: dict[str, Any] | None,
) -> dict[str, Any]:
    from sibyl_core.tools.manage import manage

    ctx = await _require_mcp_context()
    accessible_projects = await _get_accessible_projects(ctx)
    policy_decision = await _authorize_mcp_manage_action(
        ctx=ctx,
        action=action,
        entity_id=entity_id,
        accessible_projects=accessible_projects,
    )

    full_data = dict(data or {})
    full_data["organization_id"] = ctx.org_id
    if ctx.user_id:
        full_data["user_id"] = ctx.user_id

    # Task/epic transitions route through the shared workflow service so the MCP
    # path gains locking, broadcasting, and project-activity by construction.
    # Everything else (update_task, add_note, crawl, analysis, ...) stays on the
    # core manage() dispatcher unchanged.
    normalized_action = action.lower().strip()
    work_item_action = _MCP_WORKFLOW_TRANSITIONS.get(normalized_action)
    if work_item_action is not None:
        return await _manage_workflow_transition(
            ctx=ctx,
            action=normalized_action,
            work_item_action=work_item_action,
            entity_id=entity_id,
            data=full_data,
            policy_decision=policy_decision,
        )

    result = await manage(
        action=action,
        entity_id=entity_id,
        data=full_data,
        organization_id=ctx.org_id,
    )
    payload = _to_dict(result)
    if policy_decision is not None:
        payload["policy_reason"] = policy_decision.reason
    return payload


async def _require_org_id() -> str:
    """Require organization ID from MCP context.

    Raises:
        ValueError: If no organization context is available.

    Returns:
        The organization ID string.
    """
    org_id = await _get_org_id_from_context()
    if not org_id:
        raise ValueError("Organization context required. Authenticate with an org-scoped token.")
    return org_id


async def _require_owner_mcp_context(ctx: McpContext) -> None:
    """Require OWNER membership for the current MCP context."""
    if not await has_owner_membership(org_id=ctx.org_id, user_id=ctx.user_id):
        raise ValueError("OWNER role required for log access")


def create_mcp_server(
    host: str = "localhost",
    port: int = 3334,
) -> FastMCP:
    """Create and configure the MCP server instance.

    Args:
        host: Host to bind to
        port: Port to listen on

    Returns:
        Configured FastMCP server instance
    """

    auth_mode = settings.mcp_auth_mode
    jwt_secret_set = bool(settings.jwt_secret.get_secret_value())
    auth_enabled = auth_mode == "on" or (auth_mode == "auto" and jwt_secret_set)

    auth_settings = None
    auth_server_provider = None
    token_verifier = None
    if auth_enabled:
        from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions

        server_url = settings.server_url.rstrip("/")
        auth_settings = AuthSettings(
            issuer_url=server_url,
            resource_server_url=f"{server_url}/mcp",
            required_scopes=["mcp"],
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=["mcp"],
                default_scopes=["mcp"],
            ),
        )
        from sibyl.auth.mcp_oauth import SibylMcpOAuthProvider

        auth_server_provider = SibylMcpOAuthProvider()
        # NOTE: FastMCP does not allow configuring both an auth_server_provider
        # and a token_verifier at the same time. Our OAuth provider implements
        # access token validation via `load_access_token()`, so we rely on it.

    mcp = FastMCP(
        settings.server_name,
        host=host,
        port=port,
        stateless_http=False,  # Maintain session state
        auth=auth_settings,
        auth_server_provider=auth_server_provider,
        token_verifier=token_verifier,
    )

    if auth_server_provider is not None:

        @mcp.custom_route("/_oauth/login", methods=["GET"])
        async def _oauth_login_get(request):
            return await auth_server_provider.ui_login_get(request)

        @mcp.custom_route("/_oauth/login", methods=["POST"])
        async def _oauth_login_post(request):
            return await auth_server_provider.ui_login_post(request)

        @mcp.custom_route("/_oauth/org", methods=["GET"])
        async def _oauth_org_get(request):
            return await auth_server_provider.ui_org_get(request)

        @mcp.custom_route("/_oauth/org", methods=["POST"])
        async def _oauth_org_post(request):
            return await auth_server_provider.ui_org_post(request)

    _register_tools(mcp)
    _register_resources(mcp)
    return mcp


def _to_dict(obj: Any) -> Any:
    """Convert dataclass or object to dict for JSON serialization."""
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    if isinstance(obj, list):
        return [_to_dict(item) for item in obj]
    return obj


def _register_tools(mcp: FastMCP) -> None:
    """Register all MCP tools on the server instance."""

    # =========================================================================
    # TOOL 1: search - UNIFIED SEARCH (graph + documents)
    # =========================================================================

    @mcp.tool()
    async def search(
        query: str,
        types: list[str] | None = None,
        language: str | None = None,
        category: str | None = None,
        status: str | None = None,
        project: str | None = None,
        source: str | None = None,
        source_id: str | None = None,
        source_name: str | None = None,
        assignee: str | None = None,
        since: str | None = None,
        limit: int = 10,
        include_content: bool = True,
        content_max_chars: int = 500,
        include_documents: bool = True,
        include_graph: bool = True,
        use_enhanced: bool = True,
        boost_recent: bool = True,
        temporal_decay_days: float | None = None,
    ) -> dict[str, Any]:
        """Unified semantic search across knowledge graph AND documentation.

        Searches both Sibyl's knowledge graph (patterns, rules, episodes, tasks)
        AND crawled documentation (Surreal-backed vector search). Results are
        merged and ranked by relevance score.

        IMPORTANT FOR AGENTS:
        - Results contain PREVIEWS only (truncated content)
        - To get FULL content, use: sibyl show <id>
        - Do NOT try to read URLs directly - content is stored in Sibyl
        - The 'id' field is the entity/chunk ID to fetch full content

        Args:
            query: Natural language search query
            types: Entity types to search. Options: pattern, rule, template,
                   topic, episode, task, project, document.
                   Include 'document' to search crawled docs.
            language: Filter by programming language (python, typescript, etc.)
            category: Filter by category/domain (authentication, database, etc.)
            status: Filter tasks by status (backlog, todo, doing, blocked, review, done)
            project: Filter tasks by project ID
            source: Alias for source_name (for convenience)
            source_id: Filter documents by source UUID
            source_name: Filter documents by source name (partial match)
            assignee: Filter tasks by assignee name
            since: Filter by creation date (ISO format: 2024-03-15 or relative: 7d, 2w)
            limit: Maximum results to return (1-50, default: 10)
            include_content: Include content in results (default: True)
            content_max_chars: Maximum content characters per result (default: 500)
            include_documents: Search crawled documentation (default: True)
            include_graph: Search knowledge graph entities (default: True)
            use_enhanced: Use enhanced hybrid retrieval, vector + graph fusion (default: True)
            boost_recent: Boost recent results in ranking (default: True)

        Returns:
            Search results with:
            - id: Entity/chunk ID (use with 'sibyl show <id>' for full content)
            - type: Entity type (pattern, rule, task, document, etc.)
            - name: Title/name of the result
            - content: PREVIEW only - truncated, use show for full content
            - score: Relevance score (0-1)
            - source: Source name for documentation results
            - result_origin: "graph" or "document" indicating data source
            - usage_hint: Instructions for getting full content

        Examples:
            # Search everything
            search("authentication patterns")

            # Search only documentation
            search("Next.js middleware", include_graph=False)

            # Get full content of a result
            # 1. search("OAuth") -> returns results with IDs
            # 2. sibyl show <id> -> returns full content
        """
        from sibyl_core.tools.core import search as _search

        # Get full context from authenticated MCP session
        ctx = await _require_mcp_context()
        accessible_projects = await _get_accessible_projects(ctx)
        api_key_memory_scope_keys = getattr(ctx, "api_key_memory_scope_keys", None)

        result = await _search(
            query=query,
            types=types,
            language=language,
            category=category,
            status=status,
            project=project,
            accessible_projects=accessible_projects,
            source=source,
            source_id=source_id,
            source_name=source_name,
            assignee=assignee,
            since=since,
            limit=limit,
            include_content=include_content,
            content_max_chars=content_max_chars,
            include_documents=include_documents,
            include_graph=include_graph,
            use_enhanced=use_enhanced,
            boost_recent=boost_recent,
            temporal_decay_days=temporal_decay_days,
            organization_id=ctx.org_id,
            principal_id=getattr(ctx, "user_id", None),
            allowed_memory_scope_keys=(
                set(api_key_memory_scope_keys) if api_key_memory_scope_keys is not None else None
            ),
        )
        return _to_dict(result)

    # =========================================================================
    # TOOL 2: context
    # =========================================================================

    @mcp.tool()
    async def context(
        goal: str,
        intent: Literal[
            "build", "plan", "ideate", "research", "debug", "decide", "learn", "general"
        ] = "build",
        layer: Literal["wake", "recall", "deep_search"] = "recall",
        domain: str | None = None,
        project: str | None = None,
        agent_id: str | None = None,
        limit: int = 24,
        include_related: bool = True,
        related_limit: int = 3,
        audit: bool = False,
        markdown_token_budget: int | None = None,
    ) -> dict[str, Any]:
        """Compile a precise context pack for an agent goal.

        Context packs are structured for action, not generic search browsing.
        They group relevant memories into facets like active work, decisions,
        plans, ideas, constraints, artifacts, procedures, gotchas, and recent
        sessions. Use this before dispatching or resuming agents.

        Args:
            goal: What the agent is trying to accomplish.
            intent: Goal mode - build, plan, ideate, research, debug, decide,
                learn, or general.
            layer: Retrieval depth - wake for compact session start, recall for
                working context, or deep_search for broad research.
            domain: Optional domain/category to scope context. This can be
                software, creative work, home projects, research, or any other
                modeled domain.
            project: Optional project ID to scope active work.
            agent_id: Optional agent diary identity to include alongside normal
                private/project raw memory.
            limit: Maximum total context items, clamped to 1-50.
            include_related: Include one-hop related graph context.
            related_limit: Related items per selected context item.
            audit: Include full retrieval metadata per item for pack auditing.
            markdown_token_budget: Cap rendered markdown at roughly this many
                tokens for small-context consumers.
        """
        return await _compile_mcp_context_pack(
            goal=goal,
            intent=intent,
            layer=layer,
            domain=domain,
            project=project,
            agent_id=agent_id,
            limit=limit,
            include_related=include_related,
            related_limit=related_limit,
            audit=audit,
            markdown_token_budget=markdown_token_budget,
        )

    # =========================================================================
    # TOOL 3: synthesis_plan
    # =========================================================================

    @mcp.tool()
    async def synthesis_plan(
        goal: str,
        output_type: SynthesisOutputKind = "documentation",
        audience: str | None = None,
        depth: SynthesisDepthKind = "standard",
        seed_query: str | None = None,
        project: str | None = None,
        domain: str | None = None,
        entity_ids: list[str] | None = None,
        decision_ids: list[str] | None = None,
        task_ids: list[str] | None = None,
        artifact_ids: list[str] | None = None,
        required_sections: list[dict[str, Any] | str] | None = None,
        constraints: list[str] | None = None,
        max_sections: int = 6,
        include_neighborhoods: bool = True,
    ) -> dict[str, Any]:
        """Plan source-grounded synthesis from authorized memory."""
        return await _synthesis_mcp_plan(
            goal=goal,
            output_type=output_type,
            audience=audience,
            depth=depth,
            seed_query=seed_query,
            project=project,
            domain=domain,
            entity_ids=entity_ids,
            decision_ids=decision_ids,
            task_ids=task_ids,
            artifact_ids=artifact_ids,
            required_sections=required_sections,
            constraints=constraints,
            max_sections=max_sections,
            include_neighborhoods=include_neighborhoods,
        )

    # =========================================================================
    # TOOL 4: synthesis_draft
    # =========================================================================

    @mcp.tool()
    async def synthesis_draft(
        goal: str,
        output_type: SynthesisOutputKind = "documentation",
        audience: str | None = None,
        depth: SynthesisDepthKind = "standard",
        seed_query: str | None = None,
        project: str | None = None,
        domain: str | None = None,
        entity_ids: list[str] | None = None,
        decision_ids: list[str] | None = None,
        task_ids: list[str] | None = None,
        artifact_ids: list[str] | None = None,
        required_sections: list[dict[str, Any] | str] | None = None,
        constraints: list[str] | None = None,
        max_sections: int = 6,
        include_neighborhoods: bool = True,
        output_format: SynthesisArtifactKind = "markdown",
        remember: bool = False,
        memory_scope: str = "private",
        scope_key: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Draft, verify, and optionally remember a source-grounded artifact."""
        return await _synthesis_mcp_draft(
            goal=goal,
            output_type=output_type,
            audience=audience,
            depth=depth,
            seed_query=seed_query,
            project=project,
            domain=domain,
            entity_ids=entity_ids,
            decision_ids=decision_ids,
            task_ids=task_ids,
            artifact_ids=artifact_ids,
            required_sections=required_sections,
            constraints=constraints,
            max_sections=max_sections,
            include_neighborhoods=include_neighborhoods,
            output_format=output_format,
            remember=remember,
            memory_scope=memory_scope,
            scope_key=scope_key,
            tags=tags,
        )

    # =========================================================================
    # TOOL 5: synthesis_verify
    # =========================================================================

    @mcp.tool()
    async def synthesis_verify(
        goal: str,
        output_type: SynthesisOutputKind = "documentation",
        audience: str | None = None,
        depth: SynthesisDepthKind = "standard",
        seed_query: str | None = None,
        project: str | None = None,
        domain: str | None = None,
        entity_ids: list[str] | None = None,
        decision_ids: list[str] | None = None,
        task_ids: list[str] | None = None,
        artifact_ids: list[str] | None = None,
        required_sections: list[dict[str, Any] | str] | None = None,
        constraints: list[str] | None = None,
        max_sections: int = 6,
        include_neighborhoods: bool = True,
    ) -> dict[str, Any]:
        """Verify citation, hidden-context, freshness, and gap coverage."""
        return await _synthesis_mcp_verify(
            goal=goal,
            output_type=output_type,
            audience=audience,
            depth=depth,
            seed_query=seed_query,
            project=project,
            domain=domain,
            entity_ids=entity_ids,
            decision_ids=decision_ids,
            task_ids=task_ids,
            artifact_ids=artifact_ids,
            required_sections=required_sections,
            constraints=constraints,
            max_sections=max_sections,
            include_neighborhoods=include_neighborhoods,
        )

    # =========================================================================
    # TOOL 6: explore
    # =========================================================================

    @mcp.tool()
    async def explore(
        mode: Literal["list", "related", "traverse", "dependencies"] = "list",
        types: list[str] | None = None,
        entity_id: str | None = None,
        relationship_types: list[str] | None = None,
        depth: int = 1,
        language: str | None = None,
        category: str | None = None,
        project: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Explore and browse the knowledge graph.

        Four modes of exploration:
        - list: Browse entities by type with optional filters
        - related: Find entities directly connected to a specific entity
        - traverse: Multi-hop graph traversal from an entity
        - dependencies: Task dependency chains in topological order

        Args:
            mode: Exploration mode - "list", "related", "traverse", or "dependencies"
            types: Entity types to explore (for list mode)
            entity_id: Starting entity ID (required for related/traverse/dependencies modes)
            relationship_types: Filter by relationship types
                               (APPLIES_TO, REQUIRES, CONFLICTS_WITH, SUPERSEDES,
                                DOCUMENTED_IN, ENABLES, BREAKS, PART_OF, RELATED_TO,
                                DERIVED_FROM)
            depth: Traversal depth for traverse mode (1-3, default: 1)
            language: Filter by programming language
            category: Filter by category
            project: Filter tasks by project ID (for list mode with tasks)
            status: Filter tasks by status (for list mode with tasks)
            limit: Maximum results (1-200, default: 50)

        Returns:
            Exploration results with entities and/or relationships

        Examples:
            explore(mode="list", types=["pattern"], language="typescript")
            explore(mode="list", types=["task"], project="proj_abc", status="todo")
            explore(mode="related", entity_id="pattern:error-handling")
            explore(mode="traverse", entity_id="topic:auth", depth=2)
            explore(mode="dependencies", entity_id="task_xyz")
        """
        from sibyl_core.tools.core import explore as _explore

        # Get full context from authenticated MCP session
        ctx = await _require_mcp_context()
        accessible_projects = await _get_accessible_projects(ctx)

        result = await _explore(
            mode=mode,
            types=types,
            entity_id=entity_id,
            relationship_types=relationship_types,
            depth=depth,
            language=language,
            category=category,
            project=project,
            accessible_projects=accessible_projects,
            status=status,
            limit=limit,
            organization_id=ctx.org_id,
        )
        return _to_dict(result)

    # =========================================================================
    # TOOL 7: add
    # =========================================================================

    @mcp.tool()
    async def add(
        title: str,
        content: str,
        entity_type: str = "episode",
        category: str | None = None,
        languages: list[str] | None = None,
        tags: list[str] | None = None,
        related_to: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        # Task-specific parameters
        project: str | None = None,
        priority: str | None = None,
        assignees: list[str] | None = None,
        due_date: str | None = None,
        technologies: list[str] | None = None,
        depends_on: list[str] | None = None,
        # Project-specific parameters
        repository_url: str | None = None,
        # Conflict detection
        check_conflicts: bool = True,
        skip_conflicts: bool = False,
        conflict_threshold: float = 0.85,
    ) -> dict[str, Any]:
        """Add new knowledge to the graph.

        Creates a new knowledge entity that can be searched and explored.
        Supports episodes, patterns, procedures, tasks, epics, projects, and
        domain-general memories such as decisions, plans, ideas, claims,
        artifacts, sessions, and domains.

        ENTITY TYPES:
        - episode: Temporal knowledge (default) - insights, learnings, discoveries
        - pattern: Coding pattern or best practice
        - procedure: Repeatable workflow or runbook
        - decision: Chosen direction with rationale
        - plan: Strategy, sequencing, milestones, or project plan
        - idea: Brainstormed concept or unresolved option
        - claim: Atomic fact or assertion with provenance/confidence
        - artifact: File, object, document, asset, system, or work product
        - session: Conversation or work-session checkpoint
        - domain: Any modeled problem space, software or otherwise
        - task: Work item with workflow state machine (REQUIRES project)
        - epic: Feature initiative grouping tasks (REQUIRES project)
        - project: Container for related tasks

        Args:
            title: Short title for the knowledge (max 200 chars)
            content: Full content/description (max 50000 chars)
            entity_type: Type such as episode, decision, plan, idea, claim,
                artifact, procedure, task, epic, or project
            category: Category for organization (e.g., "debugging", "architecture")
            languages: Applicable programming languages
            tags: Searchable tags for discovery
            related_to: IDs of related entities to link
            metadata: Additional structured metadata (stored as JSON)
            project: Project ID (REQUIRED for tasks). Use explore(types=["project"]) to find projects.
            priority: Task priority - critical, high, medium (default), low, someday
            assignees: List of assignee names for tasks
            due_date: Due date for tasks (ISO format: 2024-03-15)
            technologies: Technologies involved (for tasks)
            depends_on: Task IDs this depends on (creates DEPENDS_ON edges)
            repository_url: Repository URL for projects
            check_conflicts: Check for semantically similar existing knowledge
            skip_conflicts: Skip conflict detection for latency-sensitive captures
            conflict_threshold: Similarity score required to flag a conflict

        Returns:
            Result with success status, entity ID, and message

        Examples:
            # Record a learning
            add("Debug: Redis timeout", "Problem was connection pool exhaustion",
                entity_type="pattern", category="debugging")

            # Create a task (project is REQUIRED)
            add("Implement OAuth", "Add OAuth2 login flow",
                entity_type="task", project="sibyl-project", priority="high")

            # Create a project
            add("Auth System", "Authentication and authorization",
                entity_type="project", repository_url="github.com/org/auth")
        """
        return await _add_mcp_entity(
            title=title,
            content=content,
            entity_type=entity_type,
            category=category,
            languages=languages,
            tags=tags,
            related_to=related_to,
            metadata=metadata,
            project=project,
            priority=priority,
            assignees=assignees,
            due_date=due_date,
            technologies=technologies,
            depends_on=depends_on,
            repository_url=repository_url,
            check_conflicts=check_conflicts,
            skip_conflicts=skip_conflicts,
            conflict_threshold=conflict_threshold,
        )

    # =========================================================================
    # TOOL 8: remember
    # =========================================================================

    @mcp.tool()
    async def remember(
        title: str,
        content: str,
        kind: MemoryKind = "episode",
        domain: str | None = None,
        project: str | None = None,
        tags: list[str] | None = None,
        related_to: list[str] | None = None,
        task_ids: list[str] | None = None,
        active_task: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Remember durable context from planning, ideation, building, or any domain.

        Use this aggressively during agent work to capture decisions, plans,
        ideas, claims, procedures, artifacts, sessions, and domain facts. This
        is the capture companion to the context tool: context retrieves what
        matters, remember stores what future agents should not have to relearn.
        Provide task_ids for exact task context. With a project, active_task
        links the memory to the single active doing task when one exists.
        """

        return await _remember_mcp_memory(
            title=title,
            content=content,
            kind=kind,
            domain=domain,
            project=project,
            tags=tags,
            related_to=related_to,
            task_ids=task_ids,
            active_task=active_task,
            metadata=metadata,
        )

    # =========================================================================
    # TOOL 9: reflect
    # =========================================================================

    @mcp.tool()
    async def reflect(
        content: str,
        source_title: str = "Session reflection",
        intent: Literal[
            "build", "plan", "ideate", "research", "debug", "decide", "learn", "general"
        ] = "general",
        domain: str | None = None,
        project: str | None = None,
        related_to: list[str] | None = None,
        task_ids: list[str] | None = None,
        active_task: bool = True,
        persist: bool = False,
        persist_source: bool = True,
        persist_review: bool = False,
        cited_ids: list[str] | str | None = None,
        limit: int = 12,
    ) -> dict[str, Any]:
        """Reflect raw notes into reviewable durable memory candidates.

        Use this after planning, ideation, debugging, or building sessions to
        extract decisions, plans, ideas, claims, artifacts, procedures, and
        session checkpoints. Set persist=True when the candidates should be
        written back into Sibyl. Set persist_review=True to store them in the
        raw review queue instead of graph promotion. Provide task_ids for exact
        task context. With persist=True and a project, active_task links
        persisted output to the single active doing task when one exists. Use
        cited_ids for context/search IDs that materially informed the reflection.
        """
        return await _reflect_mcp_memory(
            content=content,
            source_title=source_title,
            intent=intent,
            domain=domain,
            project=project,
            related_to=related_to,
            task_ids=task_ids,
            active_task=active_task,
            persist=persist,
            persist_source=persist_source,
            persist_review=persist_review,
            cited_ids=cited_ids,
            limit=limit,
        )

    # =========================================================================
    # TOOL 10: manage
    # =========================================================================

    @mcp.tool()
    async def manage(
        action: str,
        entity_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Manage operations that modify state in the knowledge graph.

        The manage() tool handles all state-changing operations including task
        and epic workflow, source operations, and analysis.

        Task Workflow Actions:
            - start_task: Begin work on a task (sets status to 'doing')
            - block_task: Mark task as blocked (data.reason required)
            - unblock_task: Remove blocked status, resume work
            - submit_review: Submit for code review (sets status to 'review')
            - complete_task: Mark done (data.learnings optional)
              Include data.cited_ids to stamp memories that informed completion.
            - archive_task: Archive without completing
            - update_task: Update task fields (data contains updates)
            - add_note: Append a note to a task (data.content required)

        Epic Workflow Actions:
            - start_epic: Begin work on an epic
            - complete_epic: Mark an epic complete (data.learnings optional)
            - archive_epic: Archive an epic (data.reason optional)
            - update_epic: Update epic fields (data contains updates)

        Source Operations:
            - crawl: Trigger crawl of URL (data.url required, data.depth optional)
            - sync: Re-crawl existing source (entity_id = source ID)
            - refresh: Sync all sources
            - link_graph: Link document chunks to knowledge graph (entity_id = source ID, optional)
            - link_graph_status: Get status of pending graph linking

        Analysis Actions:
            - estimate: Estimate task effort from similar completed tasks
            - prioritize: Get smart task ordering for project
            - detect_cycles: Find circular dependencies in project
            - suggest: Get knowledge suggestions for a task

        Server health and graph statistics are exposed as the sibyl://health and
        sibyl://stats MCP resources, not as manage() actions.

        Args:
            action: Action to perform (see categories above)
            entity_id: Target entity ID (required for most actions)
            data: Action-specific data dict

        Returns:
            Result with success, action, entity_id, message, and data

        Examples:
            manage("start_task", entity_id="task-123")
            manage("complete_task", entity_id="task-123",
                   data={"learnings": "OAuth needs exact redirect URIs"})
            manage("crawl", data={"url": "https://docs.example.com", "depth": 3})
            manage("link_graph")  # Link all pending chunks
            manage("link_graph", entity_id="source-123")  # Link specific source
            manage("link_graph_status")  # Check pending work
            manage("estimate", entity_id="task-456")
            manage("add_note", entity_id="task-123", data={"content": "Root cause found"})
        """
        return await _manage_mcp_action(
            action=action,
            entity_id=entity_id,
            data=data,
        )

    # =========================================================================
    # TOOL 11: logs (Developer Introspection)
    # =========================================================================

    @mcp.tool()
    async def logs(
        limit: int = 50,
        service: str | None = None,
        level: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get recent server logs for debugging and development.

        Returns log entries from the server's in-memory ring buffer.
        Useful for debugging issues without needing direct server access.

        Requires OWNER role (super admin equivalent).

        Args:
            limit: Maximum entries to return (default 50, max 500)
            service: Filter by service name (api, worker)
            level: Filter by log level (debug, info, warning, error)

        Returns:
            List of log entries with timestamp, service, level, event, context

        Examples:
            logs()                    # Last 50 entries
            logs(limit=100)           # Last 100 entries
            logs(service="worker")    # Worker logs only
            logs(level="error")       # Errors only
        """
        from sibyl_core.logging import LogBuffer

        # Require auth context
        ctx = await _require_mcp_context()

        # Check OWNER role (super admin)
        await _require_owner_mcp_context(ctx)

        # Clamp limit
        limit = min(max(1, limit), 500)

        # Get logs from buffer
        buffer = LogBuffer.get()
        entries = buffer.tail(n=limit, service=service, level=level)
        return [e.to_dict() for e in entries]


def _register_resources(mcp: FastMCP) -> None:
    """Register MCP resources on the server instance."""

    # =========================================================================
    # RESOURCE: sibyl://health
    # =========================================================================

    @mcp.resource("sibyl://health")
    async def health_resource() -> str:
        """Server health and connectivity status.

        Returns JSON with:
        - status: "healthy" or "unhealthy"
        - server_name: Name of the server
        - uptime_seconds: Server uptime
        - graph_connected: Whether the active graph runtime is reachable
        - entity_counts: Count of entities by type
        - errors: Any error messages
        """
        import json

        from sibyl_core.tools.core import get_health

        # Get org context (optional for health - basic health works without org)
        org_id = await _get_org_id_from_context()
        health = await get_health(organization_id=org_id)
        return json.dumps(health, indent=2)

    # =========================================================================
    # RESOURCE: sibyl://stats
    # =========================================================================

    @mcp.resource("sibyl://stats")
    async def stats_resource() -> str:
        """Knowledge graph statistics.

        Returns JSON with:
        - entity_counts: Count of entities by type
        - total_entities: Total entity count
        """
        import json

        from sibyl.persistence.graph_runtime import get_graph_stats_payload

        # Get org context (required for stats)
        org_id = await _require_org_id()
        stats = await get_graph_stats_payload(org_id)
        return json.dumps(stats, indent=2)
