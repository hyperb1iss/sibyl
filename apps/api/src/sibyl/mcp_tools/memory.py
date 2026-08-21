"""Knowledge capture and reflection MCP tools."""

from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Literal, cast
from uuid import UUID

import structlog
from mcp.server import MCPServer

import sibyl.mcp_tools.context as mcp_context
import sibyl.mcp_tools.idempotency as mcp_idempotency
import sibyl.mcp_tools.policy as mcp_policy
from sibyl.api.context_audit import log_reflection_audit
from sibyl.api.idempotency import (
    complete_idempotency_record,
    idempotency_record_pending,
    idempotency_request_hash,
    reserve_idempotency_record,
)
from sibyl.mcp_tools import serialization
from sibyl.mcp_tools.contracts import DeclaredRelatedTo, MemoryKind
from sibyl.persistence.auth_runtime import create_project_record
from sibyl.persistence.content_common import ApiIdempotencyRecord
from sibyl_core.auth.memory_policy import server_provenance_metadata, stamp_memory_scope_metadata
from sibyl_core.memory_pipeline.capture import MemoryCaptureRequest, MemoryCaptureService

log = structlog.get_logger()


async def _claim_remember_idempotency(
    *,
    ctx: mcp_context.McpContext,
    idempotency_key: str | None,
    payload: dict[str, Any],
) -> tuple[ApiIdempotencyRecord | None, dict[str, Any] | None]:
    if idempotency_key is None:
        return None, None
    record, claimed = await reserve_idempotency_record(
        organization_id=UUID(ctx.org_id),
        principal_id=ctx.user_id or "unknown",
        idempotency_key=idempotency_key,
        method="MCP",
        path="mcp/remember",
        payload=payload,
        content_session=None,
    )
    if claimed:
        return record, None
    if record.request_hash != idempotency_request_hash(payload):
        raise ValueError("idempotency_key was already used for a different request")
    if idempotency_record_pending(record):
        # The request serializer holds this key's lock, so no executor is
        # live. Adopt the interrupted reservation and complete its receipt.
        log.warning(
            "mcp_idempotency_interrupted_takeover",
            path="mcp/remember",
            organization_id=ctx.org_id,
        )
        return record, None
    replayed = cast("dict[str, Any]", deepcopy(record.response_body))
    receipt = replayed.get("mutation_receipt")
    if isinstance(receipt, dict):
        replayed["mutation_receipt"] = {**receipt, "replayed": True}
    return None, replayed


@mcp_idempotency.serialize_request("mcp/remember")
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
    retrieval_keys: list[str] | None = None,
    idempotency_key: str | None = None,
    spans: list[dict[str, Any]] | None = None,
    atomic: bool = False,
    probes: list[str] | None = None,
) -> dict[str, Any]:
    from sibyl_core.memory_pipeline.structure import build_memory_structure
    from sibyl_core.services.surreal_content import remember_raw_memory
    from sibyl_core.tools.core import add

    ctx = await mcp_context.require_context(write=True)
    accessible_projects = await mcp_context.resolve_project_scope(
        ctx,
        project,
        require_project_when_restricted=True,
    )
    if not ctx.user_id:
        raise ValueError("User context required to remember raw source material.")
    principal_id = ctx.user_id
    if idempotency_key is not None:
        idempotency_key = idempotency_key.strip()
        if not idempotency_key or len(idempotency_key) > 255:
            raise ValueError("idempotency_key must be a non-empty string up to 255 characters")

    # Ahead of the idempotency claim: a refused plan must not consume the key,
    # or the caller's corrected retry would replay the rejection.
    build_memory_structure(content.strip(), spans=spans, atomic=atomic, probes=probes)

    memory_scope = "project" if project else "private"
    write_decision = mcp_policy.authorize_memory_write_request(
        ctx=ctx,
        memory_scope=memory_scope,
        scope_key=project,
        accessible_projects=accessible_projects,
        surface="mcp_remember",
    )
    resolved_links = await mcp_policy.resolve_capture_links(
        ctx=ctx,
        project=project,
        related_to=related_to,
        task_ids=task_ids,
        active_task=active_task,
        accessible_projects=accessible_projects,
    )
    await mcp_policy.validate_relationship_targets_for_caller(
        ctx=ctx,
        related_to=resolved_links,
        accessible_projects=accessible_projects,
    )

    idempotency_payload = {
        "title": title,
        "content": content,
        "kind": kind,
        "domain": domain,
        "project": project,
        "tags": tags,
        "related_to": related_to,
        "task_ids": task_ids,
        "active_task": active_task,
        "metadata": metadata,
        "retrieval_keys": retrieval_keys,
        "spans": spans,
        "atomic": atomic,
        "probes": probes,
    }
    idempotency_claim, replayed = await _claim_remember_idempotency(
        ctx=ctx,
        idempotency_key=idempotency_key,
        payload=idempotency_payload,
    )
    if replayed is not None:
        return replayed

    full_metadata = dict(metadata or {})
    full_metadata["capture_kind"] = kind
    full_metadata["organization_id"] = ctx.org_id
    if domain:
        full_metadata["domain"] = domain
    if project:
        full_metadata["project_id"] = project
    if ctx.user_id:
        full_metadata["created_by"] = ctx.user_id
    capture_request = MemoryCaptureRequest(
        title=title,
        content=content,
        entity_type=kind,
        domain=domain,
        tags=tags,
        related_to=resolved_links,
        metadata=full_metadata,
        retrieval_keys=retrieval_keys,
        provenance={"remember_kind": kind, "related_to": resolved_links or []},
        source_id=f"mcp:remember:{kind}",
        memory_scope=memory_scope,
        scope_key=project,
        principal_id=ctx.user_id,
        capture_surface="mcp",
        spans=spans,
        atomic=atomic,
        probes=probes,
    )
    raw_revision = 1

    async def remember_raw(
        request: MemoryCaptureRequest,
    ) -> Mapping[str, Any]:
        nonlocal raw_revision
        if request.source_id is None:
            raise ValueError("Memory capture source_id is required.")
        raw_memory = await remember_raw_memory(
            organization_id=ctx.org_id,
            principal_id=principal_id,
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
        raw_revision = getattr(raw_memory, "revision", 1)
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
            # The graph writer strips server-owned keys from `metadata`,
            # because that bag is caller input everywhere else it is used. The
            # capture pipeline stamped these two from the raw write it just
            # completed, so they travel in the argument that survives the
            # strip; without it the row reaches the graph naming no capture,
            # and both the correction write-through and the projection
            # boundary lose the only link back.
            capture_provenance=server_provenance_metadata(graph_metadata),
            project=project,
            memory_scope=request.memory_scope,
            scope_key=request.scope_key,
            principal_id=request.principal_id,
            accessible_projects=accessible_projects,
            allowed_memory_scope_keys=ctx.api_key_memory_scope_keys,
            retrieval_keys=list(request.retrieval_keys)
            if request.retrieval_keys is not None
            else None,
            spans=list(request.spans) if request.spans is not None else None,
            atomic=request.atomic,
            probes=list(request.probes) if request.probes is not None else None,
        )
        return serialization.to_dict(result)

    capture_result = await MemoryCaptureService(
        remember_raw_memory=remember_raw,
        create_graph_entity=create_graph_entity,
    ).capture(capture_request)
    payload = capture_result.to_payload()
    if capture_result.raw_policy_reason is None:
        payload.pop("raw_policy_reason", None)
    payload["policy_reason"] = write_decision.reason
    affected_records = (
        [f"raw_captures:{capture_result.raw_memory_id}"] if capture_result.raw_memory_id else []
    )
    payload["mutation_receipt"] = mcp_idempotency.mutation_receipt(
        {"idempotency_key": idempotency_key},
        applied=True,
        revision=raw_revision,
        affected_records=affected_records,
    )
    if idempotency_claim is not None:
        await complete_idempotency_record(
            idempotency_claim,
            response_status_code=200,
            response_body=payload,
            content_session=None,
        )
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

    ctx = await mcp_context.require_context(write=True)
    accessible_projects = await mcp_context.resolve_project_scope(
        ctx,
        project,
        require_project_when_restricted=persist,
    )
    resolved_links = await mcp_policy.resolve_capture_links(
        ctx=ctx,
        project=project,
        related_to=related_to,
        task_ids=task_ids,
        active_task=active_task and persist,
        accessible_projects=accessible_projects,
    )
    memory_scope = "project" if project else "private"
    scope_key = project
    if persist:
        mcp_policy.authorize_memory_write_request(
            ctx=ctx,
            memory_scope=memory_scope,
            scope_key=scope_key,
            accessible_projects=accessible_projects,
            surface="mcp_reflect",
        )
        await mcp_policy.validate_relationship_targets_for_caller(
            ctx=ctx,
            related_to=resolved_links,
            accessible_projects=accessible_projects,
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


# Work items are addressed by project membership through project_id, which
# every read path already enforces. Giving them a memory scope as well would
# add a second audience channel that only MCP-created rows carry, splitting
# the population in two and hiding them from lookups that thread project
# access but no reader identity.
_UNSCOPED_ENTITY_TYPES = frozenset({"project", "epic", "task"})


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

    ctx = await mcp_context.require_context(write=True)
    normalized_entity_type = entity_type.strip().lower()
    accessible_projects = await mcp_context.resolve_project_scope(
        ctx,
        project,
        require_project_when_restricted=True,
    )
    memory_scope = "project" if project else "private"
    scope_key = project
    write_decision = mcp_policy.authorize_memory_write_request(
        ctx=ctx,
        memory_scope=memory_scope,
        scope_key=scope_key,
        accessible_projects=accessible_projects,
        surface="mcp_add",
    )
    await mcp_policy.validate_relationship_targets_for_caller(
        ctx=ctx,
        related_to=related_to,
        accessible_projects=accessible_projects,
    )

    authorized_scope = None if normalized_entity_type in _UNSCOPED_ENTITY_TYPES else memory_scope
    full_metadata = stamp_memory_scope_metadata(
        metadata,
        memory_scope=authorized_scope,
        scope_key=scope_key,
        principal_id=ctx.user_id,
    )
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
        "memory_scope": authorized_scope,
        "scope_key": scope_key,
        "principal_id": ctx.user_id,
        # A declared supersedes may only name a row this caller can read, so
        # the gate gets the same scope resolution the rest of the request used.
        "accessible_projects": accessible_projects,
        "allowed_memory_scope_keys": ctx.api_key_memory_scope_keys,
    }
    if normalized_entity_type == "project":
        add_kwargs["sync"] = True

    result = await add(**add_kwargs)
    payload = serialization.to_dict(result)
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


def register_memory_tools(mcp: MCPServer) -> None:
    """Register graph and durable-memory write tools."""

    @mcp.tool()
    async def add(
        title: str,
        content: str,
        entity_type: str = "episode",
        category: str | None = None,
        languages: list[str] | None = None,
        tags: list[str] | None = None,
        related_to: DeclaredRelatedTo = None,
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
            tags: Browse-only metadata tags for filtering and organization.
                Tags do not affect ranked recall.
            related_to: IDs of related entities to link. A bare ID links
                untyped; prefixing an ID with a predicate declares what this
                memory does to that one and retrieval weights the declared
                predicate: "supersedes:<id>", "contradicts:<id>",
                "requires:<id>", "supports:<id>", "decides:<id>". The memory
                being written is always the subject.
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
    # TOOL 10: remember
    # =========================================================================

    @mcp.tool()
    async def remember(
        title: str,
        content: str,
        kind: MemoryKind = "episode",
        domain: str | None = None,
        project: str | None = None,
        tags: list[str] | None = None,
        related_to: DeclaredRelatedTo = None,
        task_ids: list[str] | None = None,
        active_task: bool = True,
        metadata: dict[str, Any] | None = None,
        retrieval_keys: list[str] | None = None,
        idempotency_key: str | None = None,
        spans: list[dict[str, Any]] | None = None,
        atomic: bool = False,
        probes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Remember durable context from planning, ideation, building, or any domain.

        Use this aggressively during agent work to capture decisions, plans,
        ideas, claims, procedures, artifacts, sessions, and domain facts. This
        is the capture companion to the context tool: context retrieves what
        matters, remember stores what future agents should not have to relearn.
        Provide task_ids for exact task context. With a project, active_task
        links the memory to the single active doing task when one exists.

        Use retrieval_keys for the exact strings a future agent will search by
        and that similarity cannot reach: an error code, an env var, a symbol,
        a commit SHA, a config flag, an alias. You know them now and the reader
        will not, so declare them even when the body already mentions them, and
        especially when it does not. A query containing one of these strings
        matches this memory exactly, case-insensitively. Up to 16 keys, 200
        characters each. Skip keys for a memory nobody would look up by name.

        Say what this memory does to the ones it relates to. A bare ID in
        related_to links untyped, but prefixing an ID with a predicate is read
        by retrieval when it walks the graph, and the declared predicate is
        what decides how strongly a neighbor gets pulled in:
        related_to=["supersedes:ep_0a1b"] says this memory replaces that one,
        and the same shape works for "contradicts:", "requires:",
        "supports:" (this is evidence for that), and "decides:" (this settles
        that). The memory you are writing is always the subject, so read it as
        "this new memory supersedes ep_0a1b". Nothing else is a predicate: any
        other prefix is treated as part of the ID and links untyped.

        You can describe the shape of what you are storing, and you know it
        better than any cutter reading it afterwards.

        spans: where this memory's sections begin and end, as
            [{"start": 0, "end": 812, "label": "Root cause"}, ...]. Offsets are
            character positions into content after stripping leading and
            trailing whitespace, half-open so one span's end is the next one's
            start. They must cover the whole body with no gap and no overlap,
            first starting at 0 and last ending at the final character. Each
            span becomes an independently retrievable passage holding exactly
            that text; labels are indexed with it, so name the section the way
            someone would ask for it. Without spans, long memories are cut by a
            mechanical prose cutter that guesses at the seams. An invalid plan is
            rejected with the offset that broke it, never silently ignored.
        atomic: true when the body is one thing that must not be cut, such as a
            single fact, a short rule, or a snippet that only reads whole. Not
            combinable with spans.
        probes: up to 5 questions this memory has to answer later, for example
            ["why did the JWT refresh fail silently"]. Each is run through the
            live search path the moment the write lands, and the response reports
            the rank it came back at or that it did not come back at all. Use
            them when it matters that a memory is findable and not merely stored:
            an absent probe is telling you to rewrite the memory now, while you
            still have the context, rather than discovering months later that
            nothing could reach it.
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
            retrieval_keys=retrieval_keys,
            idempotency_key=idempotency_key,
            spans=spans,
            atomic=atomic,
            probes=probes,
        )

    # =========================================================================
    # TOOL 11: reflect
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
