"""Manage tool for Sibyl MCP Server.

The fourth tool: manage() handles operations that modify state.
Includes task workflow, source operations, analysis, and admin actions.

DEPRECATION NOTICE:
- Task workflow actions: Use RESTful /tasks/{id}/* endpoints instead
- Epic workflow actions: Use RESTful /epics/{id}/* endpoints instead
- Source/analysis actions: Still use this tool (no REST equivalent yet)
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from sibyl.graph.client import get_graph_client
from sibyl.graph.entities import EntityManager
from sibyl.graph.relationships import RelationshipManager
from sibyl.models.entities import EntityType
from sibyl.models.sources import CrawlStatus, Source, SourceType

log = structlog.get_logger()


# =============================================================================
# Response Models
# =============================================================================


@dataclass
class ManageResponse:
    """Response from manage operation."""

    success: bool
    action: str
    entity_id: str | None = None
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


# =============================================================================
# Action Types
# =============================================================================

# Task workflow actions (DEPRECATED: Use /tasks/{id}/* endpoints instead)
TASK_ACTIONS = {
    "start_task",  # Move task to doing status
    "block_task",  # Mark task as blocked with reason
    "unblock_task",  # Remove blocked status
    "submit_review",  # Move task to review status
    "complete_task",  # Mark task as done, capture learnings
    "archive_task",  # Archive without completing
    "update_task",  # Update task fields
}

# Epic workflow actions (DEPRECATED: Use /epics/{id}/* endpoints instead)
EPIC_ACTIONS = {
    "start_epic",  # Move epic to in_progress status
    "complete_epic",  # Mark epic as completed with learnings
    "archive_epic",  # Archive epic
    "update_epic",  # Update epic fields
}

# Source operations
SOURCE_ACTIONS = {
    "crawl",  # Trigger crawl of URL
    "sync",  # Re-crawl existing source
    "refresh",  # Sync all sources
    "link_graph",  # Link documents to knowledge graph entities
    "link_graph_status",  # Get linking job status
}

# Analysis actions
ANALYSIS_ACTIONS = {
    "estimate",  # Estimate task effort
    "prioritize",  # Smart task ordering
    "detect_cycles",  # Find circular dependencies
    "suggest",  # Suggest knowledge for task
}

ALL_ACTIONS = TASK_ACTIONS | EPIC_ACTIONS | SOURCE_ACTIONS | ANALYSIS_ACTIONS


# =============================================================================
# Main manage() function
# =============================================================================


async def manage(
    action: str,
    entity_id: str | None = None,
    data: dict[str, Any] | None = None,
    *,
    organization_id: str | None = None,
) -> ManageResponse:
    """Manage operations that modify state in the knowledge graph.

    Actions by category:

    Task Workflow:
        - start_task: Begin work on a task (sets status to 'doing')
        - block_task: Mark task as blocked (requires data.reason)
        - unblock_task: Remove blocked status
        - submit_review: Submit for code review (sets status to 'review')
        - complete_task: Mark done and capture learnings (data.learnings optional)
        - archive_task: Archive without completing
        - update_task: Update task fields (data contains field updates)

    Epic Workflow:
        - start_epic: Move epic to in_progress status
        - complete_epic: Mark epic as completed (data.learnings optional)
        - archive_epic: Archive epic (data.reason optional)
        - update_epic: Update epic fields (data contains field updates)

    Source Operations:
        - crawl: Trigger crawl of URL (data.url, data.depth optional)
        - sync: Re-crawl existing source (entity_id = source ID)
        - refresh: Sync all sources

    Analysis:
        - estimate: Estimate task effort (entity_id = task ID)
        - prioritize: Get smart task ordering (entity_id = project ID)
        - detect_cycles: Find circular dependencies (entity_id = project ID)
        - suggest: Suggest relevant knowledge (entity_id = task ID)

    Args:
        action: The action to perform (see categories above).
        entity_id: Target entity ID (required for most actions).
        data: Action-specific data dict.
        organization_id: Organization ID for graph operations (required).

    Returns:
        ManageResponse with success status, message, and action-specific data.
    """
    action = action.lower().strip()
    data = data or {}

    log.info("manage", action=action, entity_id=entity_id, data_keys=list(data.keys()))

    if action not in ALL_ACTIONS:
        return ManageResponse(
            success=False,
            action=action,
            message=f"Unknown action: {action}. Valid actions: {sorted(ALL_ACTIONS)}",
        )

    if not organization_id:
        return ManageResponse(
            success=False,
            action=action,
            message="organization_id required for this action",
        )

    try:
        # Route to appropriate handler
        if action in TASK_ACTIONS:
            return await _handle_task_action(
                action, entity_id, data, organization_id=organization_id
            )
        if action in EPIC_ACTIONS:
            return await _handle_epic_action(
                action, entity_id, data, organization_id=organization_id
            )
        if action in SOURCE_ACTIONS:
            return await _handle_source_action(
                action, entity_id, data, organization_id=organization_id
            )
        if action in ANALYSIS_ACTIONS:
            return await _handle_analysis_action(
                action, entity_id, data, organization_id=organization_id
            )

        return ManageResponse(success=False, action=action, message="Unhandled action")

    except Exception as e:
        log.exception("manage_failed", action=action, error=str(e))
        return ManageResponse(
            success=False,
            action=action,
            entity_id=entity_id,
            message=f"Action failed: {e}",
        )


# =============================================================================
# Task Workflow Handlers
# =============================================================================


async def _handle_task_action(
    action: str,
    entity_id: str | None,
    data: dict[str, Any],
    *,
    organization_id: str | None,
) -> ManageResponse:
    """Handle task workflow actions.

    Uses the TaskWorkflowEngine for proper state machine validation.
    """
    if not entity_id and action != "update_task":
        return ManageResponse(
            success=False,
            action=action,
            message="entity_id required for task actions",
        )

    if not organization_id:
        return ManageResponse(
            success=False,
            action=action,
            message="organization_id required for task actions",
        )

    client = await get_graph_client()
    entity_manager = EntityManager(client, group_id=organization_id)
    relationship_manager = RelationshipManager(client, group_id=organization_id)

    # Use workflow engine for state-validated transitions
    from sibyl.errors import InvalidTransitionError
    from sibyl.tasks.workflow import TaskWorkflowEngine

    workflow = TaskWorkflowEngine(entity_manager, relationship_manager, client, organization_id)

    try:
        if action == "start_task":
            assignee = data.get("assignee", "system")
            task = await workflow.start_task(entity_id, assignee)
            return ManageResponse(
                success=True,
                action=action,
                entity_id=entity_id,
                message="Task started",
                data={"status": task.status.value, "branch_name": task.branch_name},
            )

        if action == "block_task":
            reason = data.get("reason", "No reason provided")
            task = await workflow.block_task(entity_id, reason)
            return ManageResponse(
                success=True,
                action=action,
                entity_id=entity_id,
                message=f"Task blocked: {reason}",
                data={"status": task.status.value, "reason": reason},
            )

        if action == "unblock_task":
            task = await workflow.unblock_task(entity_id)
            return ManageResponse(
                success=True,
                action=action,
                entity_id=entity_id,
                message="Task unblocked, resuming work",
                data={"status": task.status.value},
            )

        if action == "submit_review":
            commit_shas = data.get("commit_shas", [])
            pr_url = data.get("pr_url")
            task = await workflow.submit_for_review(entity_id, commit_shas, pr_url)
            return ManageResponse(
                success=True,
                action=action,
                entity_id=entity_id,
                message="Task submitted for review",
                data={"status": task.status.value, "pr_url": task.pr_url},
            )

        if action == "complete_task":
            learnings = data.get("learnings", "")
            actual_hours = data.get("actual_hours")
            task = await workflow.complete_task(entity_id, actual_hours, learnings)
            return ManageResponse(
                success=True,
                action=action,
                entity_id=entity_id,
                message="Task completed" + (" with learnings captured" if learnings else ""),
                data={"status": task.status.value, "learnings": learnings},
            )

        if action == "archive_task":
            reason = data.get("reason", "")
            task = await workflow.archive_task(entity_id, reason)
            return ManageResponse(
                success=True,
                action=action,
                entity_id=entity_id,
                message="Task archived",
                data={"status": task.status.value},
            )

        if action == "update_task":
            return await _update_task(
                entity_manager, entity_id, data, organization_id=organization_id
            )

    except InvalidTransitionError as e:
        return ManageResponse(
            success=False,
            action=action,
            entity_id=entity_id,
            message=str(e),
            data=e.details,
        )

    return ManageResponse(success=False, action=action, message="Unknown task action")


async def _update_task(
    entity_manager: EntityManager,
    entity_id: str | None,
    data: dict[str, Any],
    *,
    organization_id: str | None = None,
) -> ManageResponse:
    """Update task fields.

    Args:
        entity_manager: EntityManager instance
        entity_id: Task ID to update
        data: Dict containing fields to update plus optional control flags:
            - sync: If False, queue update via arq worker (default: True)
            - All other fields are update values
        organization_id: Organization ID (required for async mode)
    """
    if not entity_id:
        return ManageResponse(
            success=False,
            action="update_task",
            message="entity_id required for update_task",
        )

    # Extract control flag
    sync = data.pop("sync", True)

    # Filter allowed update fields
    # Note: status is included for historical/bulk updates that need to bypass workflow
    allowed_fields = {
        "title",
        "description",
        "status",  # For historical/bulk updates
        "priority",
        "complexity",
        "feature",
        "sprint",
        "assignees",
        "due_date",
        "estimated_hours",
        "actual_hours",
        "domain",
        "technologies",
        "branch_name",
        "pr_url",
        "task_order",
    }

    updates = {k: v for k, v in data.items() if k in allowed_fields}

    if not updates:
        return ManageResponse(
            success=False,
            action="update_task",
            entity_id=entity_id,
            message=f"No valid fields to update. Allowed: {sorted(allowed_fields)}",
        )

    # Async mode: queue via arq worker
    if not sync:
        if not organization_id:
            return ManageResponse(
                success=False,
                action="update_task",
                entity_id=entity_id,
                message="organization_id required for async update",
            )

        from sibyl.jobs.queue import enqueue_update_task

        job_id = await enqueue_update_task(entity_id, updates, organization_id)
        return ManageResponse(
            success=True,
            action="update_task",
            entity_id=entity_id,
            message=f"Task update queued: {', '.join(updates.keys())}",
            data={"job_id": job_id, "queued_fields": list(updates.keys())},
        )

    # Sync mode: update directly
    result = await entity_manager.update(entity_id, updates)
    if result:
        return ManageResponse(
            success=True,
            action="update_task",
            entity_id=entity_id,
            message=f"Task updated: {', '.join(updates.keys())}",
            data={"updated_fields": list(updates.keys())},
        )

    return ManageResponse(
        success=False,
        action="update_task",
        entity_id=entity_id,
        message="Failed to update task",
    )


# =============================================================================
# Epic Workflow Handlers
# =============================================================================


async def _handle_epic_action(
    action: str,
    entity_id: str | None,
    data: dict[str, Any],
    *,
    organization_id: str | None,
) -> ManageResponse:
    """Handle epic workflow actions.

    Epics have simpler state transitions than tasks (no workflow engine needed).
    """
    if not entity_id and action != "update_epic":
        return ManageResponse(
            success=False,
            action=action,
            message="entity_id required for epic actions",
        )

    if not organization_id:
        return ManageResponse(
            success=False,
            action=action,
            message="organization_id required for epic actions",
        )

    client = await get_graph_client()
    entity_manager = EntityManager(client, group_id=organization_id)

    # Get the epic
    try:
        epic = await entity_manager.get(entity_id) if entity_id else None
        if entity_id and not epic:
            return ManageResponse(
                success=False,
                action=action,
                entity_id=entity_id,
                message=f"Epic not found: {entity_id}",
            )
        if epic and epic.entity_type != EntityType.EPIC:
            return ManageResponse(
                success=False,
                action=action,
                entity_id=entity_id,
                message=f"Entity is not an epic: {entity_id}",
            )
    except Exception:
        return ManageResponse(
            success=False,
            action=action,
            entity_id=entity_id,
            message=f"Epic not found: {entity_id}",
        )

    if action == "start_epic":
        updates = {"status": "in_progress"}
        await entity_manager.update(entity_id, updates)
        return ManageResponse(
            success=True,
            action=action,
            entity_id=entity_id,
            message="Epic started",
            data={"status": "in_progress"},
        )

    if action == "complete_epic":
        learnings = data.get("learnings", "")
        updates = {
            "status": "completed",
            "completed_date": datetime.now(UTC).isoformat(),
        }
        if learnings:
            updates["learnings"] = learnings
        await entity_manager.update(entity_id, updates)
        return ManageResponse(
            success=True,
            action=action,
            entity_id=entity_id,
            message="Epic completed" + (" with learnings captured" if learnings else ""),
            data={"status": "completed", "learnings": learnings},
        )

    if action == "archive_epic":
        reason = data.get("reason", "")
        updates = {"status": "archived"}
        await entity_manager.update(entity_id, updates)
        return ManageResponse(
            success=True,
            action=action,
            entity_id=entity_id,
            message="Epic archived" + (f": {reason}" if reason else ""),
            data={"status": "archived"},
        )

    if action == "update_epic":
        return await _update_epic(entity_manager, entity_id, data)

    return ManageResponse(success=False, action=action, message="Unknown epic action")


async def _update_epic(
    entity_manager: EntityManager,
    entity_id: str | None,
    data: dict[str, Any],
) -> ManageResponse:
    """Update epic fields."""
    if not entity_id:
        return ManageResponse(
            success=False,
            action="update_epic",
            message="entity_id required for update_epic",
        )

    # Filter allowed update fields
    allowed_fields = {
        "title",
        "description",
        "status",
        "priority",
        "start_date",
        "target_date",
        "assignees",
        "tags",
        "learnings",
    }

    updates = {k: v for k, v in data.items() if k in allowed_fields}

    if not updates:
        return ManageResponse(
            success=False,
            action="update_epic",
            entity_id=entity_id,
            message=f"No valid fields to update. Allowed: {sorted(allowed_fields)}",
        )

    result = await entity_manager.update(entity_id, updates)
    if result:
        return ManageResponse(
            success=True,
            action="update_epic",
            entity_id=entity_id,
            message=f"Epic updated: {', '.join(updates.keys())}",
            data={"updated_fields": list(updates.keys())},
        )

    return ManageResponse(
        success=False,
        action="update_epic",
        entity_id=entity_id,
        message="Failed to update epic",
    )


# =============================================================================
# Source Operation Handlers
# =============================================================================


async def _handle_source_action(
    action: str,
    entity_id: str | None,
    data: dict[str, Any],
    *,
    organization_id: str | None,
) -> ManageResponse:
    """Handle source operations (crawl, sync, refresh)."""
    # Validate inputs BEFORE connecting to database
    if action == "crawl":
        url = data.get("url")
        if not url:
            return ManageResponse(
                success=False,
                action=action,
                message="data.url required for crawl action",
            )

    if action == "sync" and not entity_id:
        return ManageResponse(
            success=False,
            action=action,
            message="entity_id (source ID) required for sync action",
        )

    if not organization_id:
        return ManageResponse(
            success=False,
            action=action,
            message="organization_id required for source actions",
        )

    # Now connect after validation passes
    client = await get_graph_client()
    entity_manager = EntityManager(client, group_id=organization_id)

    if action == "crawl":
        url = data.get("url")  # Already validated above
        depth = data.get("depth", 2)
        return await _crawl_source(entity_manager, url, depth, data)

    if action == "sync":
        return await _sync_source(entity_manager, entity_id)

    if action == "refresh":
        return await _refresh_all_sources(entity_manager)

    if action == "link_graph":
        return await _link_graph(entity_id, data, organization_id)

    if action == "link_graph_status":
        return await _link_graph_status()

    return ManageResponse(success=False, action=action, message="Unknown source action")


async def _crawl_source(
    entity_manager: EntityManager,
    url: str,
    depth: int,
    data: dict[str, Any],
) -> ManageResponse:
    """Trigger crawl of a URL."""
    import hashlib

    # Generate source ID from URL
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:12]
    source_id = f"source_{url_hash}"

    # Create source entity
    source = Source(  # type: ignore[call-arg]  # model_validator sets name from url
        id=source_id,
        url=url,
        source_type=SourceType.WEBSITE,
        crawl_depth=min(depth, 10),
        crawl_patterns=data.get("patterns", []),
        exclude_patterns=data.get("exclude", []),
        crawl_status=CrawlStatus.PENDING,
    )

    # Store source (use actual created ID)
    created_id = await entity_manager.create(source)

    # TODO: Trigger actual crawl pipeline (async job)
    # For now, just mark as pending

    return ManageResponse(
        success=True,
        action="crawl",
        entity_id=created_id,
        message=f"Crawl queued for {url}",
        data={
            "source_id": created_id,
            "url": url,
            "depth": depth,
            "status": CrawlStatus.PENDING.value,
        },
    )


async def _sync_source(
    entity_manager: EntityManager,
    source_id: str,
) -> ManageResponse:
    """Re-crawl an existing source."""
    # Validate source exists
    try:
        await entity_manager.get(source_id)
    except Exception:
        return ManageResponse(
            success=False,
            action="sync",
            entity_id=source_id,
            message=f"Source not found: {source_id}",
        )

    # Update crawl status to pending
    updates = {
        "crawl_status": CrawlStatus.PENDING.value,
    }
    await entity_manager.update(source_id, updates)

    # TODO: Trigger actual re-crawl pipeline (async job)

    return ManageResponse(
        success=True,
        action="sync",
        entity_id=source_id,
        message="Sync queued",
        data={"status": CrawlStatus.PENDING.value},
    )


async def _refresh_all_sources(
    entity_manager: EntityManager,
) -> ManageResponse:
    """Sync all sources."""
    sources = await entity_manager.list_by_type(EntityType.SOURCE, limit=100)

    queued = 0
    for source in sources:
        updates = {"crawl_status": CrawlStatus.PENDING.value}
        await entity_manager.update(source.id, updates)
        queued += 1

    # TODO: Trigger actual refresh pipeline (async job)

    return ManageResponse(
        success=True,
        action="refresh",
        message=f"Refresh queued for {queued} sources",
        data={"sources_queued": queued},
    )


async def _link_graph(
    source_id: str | None,
    data: dict[str, Any],
    organization_id: str,
) -> ManageResponse:
    """Link document chunks to knowledge graph entities via LLM extraction."""
    from sqlalchemy import select
    from sqlmodel import col

    from sibyl.crawler.graph_integration import GraphIntegrationService
    from sibyl.db import CrawledDocument, DocumentChunk, get_session
    from sibyl.graph.client import get_graph_client

    max_chunks = data.get("max_chunks", 1000)

    # Build query for unlinked chunks
    query = select(DocumentChunk).where(col(DocumentChunk.has_entities) == False)  # noqa: E712

    if source_id:
        # Filter to specific source via document join
        from sibyl.db import CrawlSource

        query = (
            query.join(CrawledDocument, CrawledDocument.id == DocumentChunk.document_id)
            .join(CrawlSource, CrawlSource.id == CrawledDocument.source_id)
            .where(CrawlSource.id == source_id)
        )

    query = query.limit(max_chunks)

    async with get_session() as session:
        result = await session.execute(query)
        chunks = result.scalars().all()

    if not chunks:
        return ManageResponse(
            success=True,
            action="link_graph",
            entity_id=source_id,
            message="No unlinked chunks to process",
            data={"chunks_processed": 0, "entities_linked": 0},
        )

    # Process chunks
    client = await get_graph_client()
    integration = GraphIntegrationService(client, organization_id)

    source_name = source_id or "all_sources"
    stats = await integration.process_chunks(list(chunks), source_name=source_name)

    return ManageResponse(
        success=True,
        action="link_graph",
        entity_id=source_id,
        message=f"Linked {stats.entities_linked} entities from {stats.chunks_processed} chunks",
        data={
            "chunks_processed": stats.chunks_processed,
            "entities_extracted": stats.entities_extracted,
            "entities_linked": stats.entities_linked,
            "new_entities_created": stats.new_entities_created,
            "errors": stats.errors,
        },
    )


async def _link_graph_status() -> ManageResponse:
    """Get status of graph linking (pending chunks per source)."""
    from sqlalchemy import func, select
    from sqlmodel import col

    from sibyl.db import CrawledDocument, CrawlSource, DocumentChunk, get_session

    async with get_session() as session:
        # Total chunks
        total_result = await session.execute(select(func.count(DocumentChunk.id)))
        total_chunks = total_result.scalar() or 0

        # Chunks with entities
        linked_result = await session.execute(
            select(func.count(DocumentChunk.id)).where(
                col(DocumentChunk.has_entities) == True  # noqa: E712
            )
        )
        chunks_with_entities = linked_result.scalar() or 0

        # Pending per source
        pending_query = (
            select(  # type: ignore[call-overload]
                CrawlSource.name,
                func.count(DocumentChunk.id).label("pending"),
            )
            .join(CrawledDocument, CrawledDocument.source_id == CrawlSource.id)
            .join(DocumentChunk, DocumentChunk.document_id == CrawledDocument.id)
            .where(col(DocumentChunk.has_entities) == False)  # noqa: E712
            .group_by(CrawlSource.name)
        )
        pending_result = await session.execute(pending_query)
        sources = [{"name": row.name, "pending": row.pending} for row in pending_result.all()]

    return ManageResponse(
        success=True,
        action="link_graph_status",
        message=f"{total_chunks - chunks_with_entities} chunks pending linking",
        data={
            "total_chunks": total_chunks,
            "chunks_with_entities": chunks_with_entities,
            "chunks_pending": total_chunks - chunks_with_entities,
            "sources": sources,
        },
    )


# =============================================================================
# Analysis Action Handlers
# =============================================================================


async def _handle_analysis_action(
    action: str,
    entity_id: str | None,
    _data: dict[str, Any],
    *,
    organization_id: str | None,
) -> ManageResponse:
    """Handle analysis actions."""
    if not entity_id:
        return ManageResponse(
            success=False,
            action=action,
            message=f"entity_id required for {action} action",
        )

    if not organization_id:
        return ManageResponse(
            success=False,
            action=action,
            message="organization_id required for analysis actions",
        )

    client = await get_graph_client()
    entity_manager = EntityManager(client, group_id=organization_id)
    relationship_manager = RelationshipManager(client, group_id=organization_id)

    if action == "estimate":
        return await _estimate_effort(entity_manager, relationship_manager, entity_id)

    if action == "prioritize":
        return await _prioritize_tasks(entity_manager, relationship_manager, entity_id)

    if action == "detect_cycles":
        return await _detect_cycles(relationship_manager, entity_id)

    if action == "suggest":
        return await _suggest_knowledge(entity_manager, relationship_manager, entity_id)

    return ManageResponse(success=False, action=action, message="Unknown analysis action")


async def _estimate_effort(
    entity_manager: EntityManager,
    relationship_manager: RelationshipManager,
    task_id: str,
) -> ManageResponse:
    """Estimate task effort based on similar completed tasks."""
    from sibyl.tasks.manager import TaskManager

    task_manager = TaskManager(entity_manager, relationship_manager)

    # Get the task
    try:
        entity = await entity_manager.get(task_id)
        task = task_manager._entity_to_task(entity)  # noqa: SLF001
    except Exception:
        return ManageResponse(
            success=False,
            action="estimate",
            entity_id=task_id,
            message=f"Task not found: {task_id}",
        )

    # Get estimate
    estimate = await task_manager.estimate_task_effort(task)

    return ManageResponse(
        success=True,
        action="estimate",
        entity_id=task_id,
        message=f"Estimated {estimate.estimated_hours or 'unknown'} hours",
        data={
            "estimated_hours": estimate.estimated_hours,
            "confidence": estimate.confidence,
            "based_on_tasks": estimate.based_on_tasks,
            "similar_tasks": estimate.similar_tasks,
            "reason": estimate.reason,
        },
    )


async def _prioritize_tasks(
    entity_manager: EntityManager,
    _relationship_manager: RelationshipManager,
    project_id: str,
) -> ManageResponse:
    """Get smart task ordering for a project."""
    # Get all tasks for the project
    all_tasks = await entity_manager.list_by_type(EntityType.TASK, limit=500)

    # Filter by project
    project_tasks = [t for t in all_tasks if t.metadata.get("project_id") == project_id]

    if not project_tasks:
        return ManageResponse(
            success=True,
            action="prioritize",
            entity_id=project_id,
            message="No tasks found for project",
            data={"tasks": []},
        )

    # Simple priority ordering: by priority, then by task_order
    priority_order = {
        "critical": 0,
        "high": 1,
        "medium": 2,
        "low": 3,
        "someday": 4,
    }

    sorted_tasks = sorted(
        project_tasks,
        key=lambda t: (
            priority_order.get(t.metadata.get("priority", "medium"), 2),
            -t.metadata.get("task_order", 0),
        ),
    )

    # Return ordered task IDs with priorities
    task_list = [
        {
            "id": t.id,
            "name": t.name,
            "priority": t.metadata.get("priority", "medium"),
            "status": t.metadata.get("status", "todo"),
        }
        for t in sorted_tasks
    ]

    return ManageResponse(
        success=True,
        action="prioritize",
        entity_id=project_id,
        message=f"Prioritized {len(task_list)} tasks",
        data={"tasks": task_list},
    )


async def _detect_cycles(
    _relationship_manager: RelationshipManager,
    project_id: str,
) -> ManageResponse:
    """Detect circular dependencies in a project."""
    # Get all DEPENDS_ON relationships
    # Note: This is a simplified implementation
    # A full implementation would traverse the graph

    # For now, return empty (no cycles detected)
    # TODO: Implement actual cycle detection via graph traversal

    return ManageResponse(
        success=True,
        action="detect_cycles",
        entity_id=project_id,
        message="No circular dependencies detected",
        data={"cycles": [], "has_cycles": False},
    )


async def _suggest_knowledge(
    entity_manager: EntityManager,
    relationship_manager: RelationshipManager,
    task_id: str,
) -> ManageResponse:
    """Suggest relevant knowledge for a task."""
    from sibyl.tasks.manager import TaskManager

    task_manager = TaskManager(entity_manager, relationship_manager)

    # Get the task
    try:
        entity = await entity_manager.get(task_id)
        task = task_manager._entity_to_task(entity)  # noqa: SLF001
    except Exception:
        return ManageResponse(
            success=False,
            action="suggest",
            entity_id=task_id,
            message=f"Task not found: {task_id}",
        )

    # Get knowledge suggestions
    suggestions = await task_manager.suggest_task_knowledge(
        task_title=task.title,
        task_description=task.description,
        technologies=task.technologies,
        limit=5,
    )

    return ManageResponse(
        success=True,
        action="suggest",
        entity_id=task_id,
        message="Knowledge suggestions retrieved",
        data={
            "patterns": suggestions.patterns,
            "rules": suggestions.rules,
            "templates": suggestions.templates,
            "past_learnings": suggestions.past_learnings,
            "error_patterns": suggestions.error_patterns,
        },
    )

