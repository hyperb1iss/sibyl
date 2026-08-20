"""Retrieval and graph-navigation MCP tools."""

from typing import Any, Literal

import structlog
from mcp.server import MCPServer

import sibyl.mcp_tools.context as mcp_context
import sibyl.mcp_tools.policy as mcp_policy
from sibyl.api.context_audit import log_context_pack_audit
from sibyl.mcp_tools import serialization
from sibyl.services.recall_limits import (
    RecallConcurrencyLimitExceededError,
    recall_concurrency_slot,
)
from sibyl_core.auth.memory_policy import MemoryPolicyAction
from sibyl_core.tools.context import DEFAULT_MARKDOWN_TOKEN_BUDGET
from sibyl_core.tools.traverse import (
    DEFAULT_EXPAND_LIMIT,
    DEFAULT_NEIGHBOR_CONTENT_MAX_CHARS,
    DEFAULT_SLICE_CONTENT_MAX_CHARS,
    DEFAULT_SLICE_WINDOW,
    DEFAULT_TRAVERSAL_DEPTH,
)

log = structlog.get_logger()


async def compile_context_pack(
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
    markdown_token_budget: int | None = DEFAULT_MARKDOWN_TOKEN_BUDGET,
) -> dict[str, Any]:
    from sibyl_core.tools.core import (
        compile_context as _compile_context,
        context_pack_to_dict,
        context_pack_to_markdown,
    )

    ctx = await mcp_context.require_context()
    accessible_projects = await mcp_context.resolve_project_scope(ctx, project)
    memory_scope = "project" if project else "private"
    scope_key = project
    if not mcp_policy.context_pack_scope_allowed(
        ctx,
        project=project,
        accessible_projects=accessible_projects,
    ):
        mcp_policy.deny_api_key_memory_scope(
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


def register_retrieval_tools(mcp: MCPServer) -> None:
    """Register search, context, and graph-navigation tools."""

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
        ctx = await mcp_context.require_context()
        accessible_projects = await mcp_context.get_accessible_projects(ctx)
        api_key_memory_scope_keys = ctx.api_key_memory_scope_keys

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
        return serialization.to_dict(result)

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
        markdown_token_budget: int | None = DEFAULT_MARKDOWN_TOKEN_BUDGET,
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
        return await compile_context_pack(
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
        ctx = await mcp_context.require_context()
        accessible_projects = await mcp_context.get_accessible_projects(ctx)

        api_key_memory_scope_keys = ctx.api_key_memory_scope_keys
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
            principal_id=getattr(ctx, "user_id", None),
            allowed_memory_scope_keys=(
                set(api_key_memory_scope_keys) if api_key_memory_scope_keys is not None else None
            ),
        )
        return serialization.to_dict(result)

    # =========================================================================
    # TOOL 7: expand_neighbors
    # =========================================================================

    @mcp.tool()
    async def expand_neighbors(
        entity_ids: list[str],
        relationship_types: list[str] | None = None,
        types: list[str] | None = None,
        depth: int = DEFAULT_TRAVERSAL_DEPTH,
        limit: int = DEFAULT_EXPAND_LIMIT,
        content_max_chars: int = DEFAULT_NEIGHBOR_CONTENT_MAX_CHARS,
        include_incoming: bool = True,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Widen memories you already found into their graph neighborhood.

        TRAVERSAL CONTRACT (read this before calling):
        This is a bounded step in a loop of AT MOST THREE ROUNDS. Round one is
        `search` or `context`. Round two widens with `expand_neighbors` or
        `fetch_slice`. Round three widens once more, and then you answer.
        Two widening rounds capture nearly all of the available gain; a fourth
        costs latency and buys noise.

        SKIP THIS VERB when one hop answers the question. If you want "what do we
        know about X", call `context` and read the pack it composed. Traversal is
        for when you have specific memories in hand and need what sits next to
        them: the tasks blocking this one, the decision a plan supersedes, the
        spans of a memory a search only matched part of.

        COMPOSITION IS NOT YOURS. This verb returns previews and adjacency so you
        can choose what to gather. `context` still renders the evidence, and the
        reserved note lane and evidence ordering stay under its control.

        Every neighbor is authorized for you individually. A neighbor missing
        from the result may exist and be someone else's private memory; that is
        the system working, not a gap to route around.

        Args:
            entity_ids: Seed entity IDs, at most 8 of them.
                Seeds that resolve to nothing you may read come back in
                `unresolved` without saying which reason applied.
            relationship_types: Restrict hops to these relationship names, e.g.
                ["DEPENDS_ON"] or ["PART_OF"]. Empty walks every relationship.
            types: Restrict neighbors to these entity types.
            depth: Hops to walk, 1-3 (default 1). Depth 1 first; deepen only
                when depth 1 came back thin.
            limit: Neighbors returned, up to 24 (default 8).
            content_max_chars: Preview characters per neighbor. These are
                previews by design; widen a promising one with `fetch_slice`.
            include_incoming: Follow edges pointing at the seeds too (default
                True). Dependents and passages are only reachable inbound.
            project: Scope the walk to one project you can read.

        Returns:
            Hop-tagged neighbors with relationship, direction, and distance,
            highest path score first, plus `truncated` when more existed.

        Examples:
            expand_neighbors(["task_abc"], relationship_types=["DEPENDS_ON"])
            expand_neighbors(["decision_1", "decision_2"], depth=2)
        """
        from sibyl_core.tools.core import expand_neighbors as _expand_neighbors

        ctx = await mcp_context.require_context()
        accessible_projects = await mcp_context.resolve_project_scope(ctx, project)
        api_key_memory_scope_keys = ctx.api_key_memory_scope_keys
        result = await _expand_neighbors(
            entity_ids,
            organization_id=ctx.org_id,
            relationship_types=relationship_types,
            types=types,
            depth=depth,
            limit=limit,
            content_max_chars=content_max_chars,
            include_incoming=include_incoming,
            principal_id=getattr(ctx, "user_id", None),
            accessible_projects=accessible_projects,
            allowed_memory_scope_keys=(
                set(api_key_memory_scope_keys) if api_key_memory_scope_keys is not None else None
            ),
        )
        return serialization.to_dict(result)

    # =========================================================================
    # TOOL 8: fetch_slice
    # =========================================================================

    @mcp.tool()
    async def fetch_slice(
        entity_id: str,
        window: int = DEFAULT_SLICE_WINDOW,
        content_max_chars: int = DEFAULT_SLICE_CONTENT_MAX_CHARS,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Read one memory at span granularity, centered where you point.

        TRAVERSAL CONTRACT (read this before calling):
        This is a bounded step in a loop of AT MOST THREE ROUNDS, the same budget
        `expand_neighbors` spends from. Use it when a search hit is a passage, or
        when a memory is long and you need the part around a match rather than
        the whole body.

        SKIP THIS VERB when the memory you found is already short enough to read.
        A result whose content was not truncated needs no widening.

        COMPOSITION IS NOT YOURS. This verb hands back spans, not an answer.
        `context` composes the final evidence, and it keeps control of ordering
        and the reserved note lane whatever you gather here.

        CITE THE PARENT, NOT THE SPAN. The response names `parent_id`, and that
        is the id a later reader can resolve. Span ids are re-minted whenever the
        memory is edited, so a citation pointing at one goes stale silently.

        A memory short enough never to have been cut comes back whole with
        `sliced=false`. That is the answer, not an error to retry.

        Args:
            entity_id: A passage entity ID, or the ID of the memory it came from.
                Given a passage, the window is centered on it. Given a memory,
                the window starts at its first span.
            window: Adjacent spans to return, 1-64. The default of
                three is the measured adjacency: three spans reach the same
                exposure as the whole memory, one span reaches noticeably less.
            content_max_chars: Character budget for the whole window, spent in
                span order. The span that exhausts it says `truncated`.
            project: Scope the read to one project you can read.

        Returns:
            The ordered span window with per-span index and total, plus the
            parent memory a citation resolves to.

        Examples:
            fetch_slice("passage_9f2c1b")
            fetch_slice("decision_abc", window=5)
        """
        from sibyl_core.tools.core import fetch_slice as _fetch_slice

        ctx = await mcp_context.require_context()
        accessible_projects = await mcp_context.resolve_project_scope(ctx, project)
        api_key_memory_scope_keys = ctx.api_key_memory_scope_keys
        result = await _fetch_slice(
            entity_id,
            organization_id=ctx.org_id,
            window=window,
            content_max_chars=content_max_chars,
            principal_id=getattr(ctx, "user_id", None),
            accessible_projects=accessible_projects,
            allowed_memory_scope_keys=(
                set(api_key_memory_scope_keys) if api_key_memory_scope_keys is not None else None
            ),
        )
        return serialization.to_dict(result)
