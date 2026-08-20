"""Source-grounded synthesis MCP tools."""

from typing import Any

from mcp.server import MCPServer

import sibyl.mcp_tools.context as mcp_context
import sibyl.mcp_tools.policy as mcp_policy
from sibyl.mcp_tools.contracts import (
    SynthesisArtifactKind,
    SynthesisDepthKind,
    SynthesisOutputKind,
)


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

    ctx = await mcp_context.require_context()
    accessible_projects = await mcp_context.resolve_project_scope(ctx, project)
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
        allowed_memory_scope_keys=ctx.api_key_memory_scope_keys,
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

    ctx = await mcp_context.require_context()
    accessible_projects = await mcp_context.resolve_project_scope(ctx, project)
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
        allowed_memory_scope_keys=ctx.api_key_memory_scope_keys,
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

    ctx = await mcp_context.require_context(write=remember)
    accessible_projects = await mcp_context.resolve_project_scope(ctx, project)
    resolved_scope_key = scope_key
    policy_reason: str | None = None
    if remember:
        write_accessible_projects = accessible_projects
        if memory_scope == "project":
            resolved_scope_key = resolved_scope_key or project
            write_accessible_projects = await mcp_context.resolve_project_scope(
                ctx,
                resolved_scope_key,
                require_project_when_restricted=True,
            )
        decision = mcp_policy.authorize_memory_write_request(
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
        allowed_memory_scope_keys=ctx.api_key_memory_scope_keys,
    )
    if policy_reason:
        payload["policy_reason"] = policy_reason
    return payload


def register_synthesis_tools(mcp: MCPServer) -> None:
    """Register synthesis planning, drafting, and verification tools."""

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
