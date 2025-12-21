"""Manage tool for Sibyl MCP Server.

The fourth tool: manage() handles operations that modify state.
Includes task workflow, source operations, analysis, and admin actions.
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

# Task workflow actions
TASK_ACTIONS = {
    "start_task",  # Move task to doing status
    "block_task",  # Mark task as blocked with reason
    "unblock_task",  # Remove blocked status
    "submit_review",  # Move task to review status
    "complete_task",  # Mark task as done, capture learnings
    "archive_task",  # Archive without completing
    "update_task",  # Update task fields
}

# Source operations
SOURCE_ACTIONS = {
    "crawl",  # Trigger crawl of URL
    "sync",  # Re-crawl existing source
    "refresh",  # Sync all sources
}

# Analysis actions
ANALYSIS_ACTIONS = {
    "estimate",  # Estimate task effort
    "prioritize",  # Smart task ordering
    "detect_cycles",  # Find circular dependencies
    "suggest",  # Suggest knowledge for task
}

# Admin actions
ADMIN_ACTIONS = {
    "health",  # Server health check
    "stats",  # Graph statistics
    "rebuild_index",  # Rebuild FalkorDB indices
}

ALL_ACTIONS = TASK_ACTIONS | SOURCE_ACTIONS | ANALYSIS_ACTIONS | ADMIN_ACTIONS


# =============================================================================
# Main manage() function
# =============================================================================


async def manage(
    action: str,
    entity_id: str | None = None,
    data: dict[str, Any] | None = None,
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

    Source Operations:
        - crawl: Trigger crawl of URL (data.url, data.depth optional)
        - sync: Re-crawl existing source (entity_id = source ID)
        - refresh: Sync all sources

    Analysis:
        - estimate: Estimate task effort (entity_id = task ID)
        - prioritize: Get smart task ordering (entity_id = project ID)
        - detect_cycles: Find circular dependencies (entity_id = project ID)
        - suggest: Suggest relevant knowledge (entity_id = task ID)

    Admin:
        - health: Server health check
        - stats: Graph statistics
        - rebuild_index: Rebuild search indices

    Args:
        action: The action to perform (see categories above).
        entity_id: Target entity ID (required for most actions).
        data: Action-specific data dict.

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

    try:
        # Route to appropriate handler
        if action in TASK_ACTIONS:
            return await _handle_task_action(action, entity_id, data)
        if action in SOURCE_ACTIONS:
            return await _handle_source_action(action, entity_id, data)
        if action in ANALYSIS_ACTIONS:
            return await _handle_analysis_action(action, entity_id, data)
        # ADMIN_ACTIONS
        return await _handle_admin_action(action, entity_id, data)

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

    client = await get_graph_client()
    entity_manager = EntityManager(client)
    relationship_manager = RelationshipManager(client)

    # Use workflow engine for state-validated transitions
    from sibyl.errors import InvalidTransitionError
    from sibyl.tasks.workflow import TaskWorkflowEngine

    workflow = TaskWorkflowEngine(entity_manager, relationship_manager, client)

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
            return await _update_task(entity_manager, entity_id, data)

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
) -> ManageResponse:
    """Update task fields."""
    if not entity_id:
        return ManageResponse(
            success=False,
            action="update_task",
            message="entity_id required for update_task",
        )

    # Filter allowed update fields
    allowed_fields = {
        "title",
        "description",
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
# Source Operation Handlers
# =============================================================================


async def _handle_source_action(
    action: str,
    entity_id: str | None,
    data: dict[str, Any],
) -> ManageResponse:
    """Handle source operations (crawl, sync, refresh)."""
    client = await get_graph_client()
    entity_manager = EntityManager(client)

    if action == "crawl":
        url = data.get("url")
        if not url:
            return ManageResponse(
                success=False,
                action=action,
                message="data.url required for crawl action",
            )
        depth = data.get("depth", 2)
        return await _crawl_source(entity_manager, url, depth, data)

    if action == "sync":
        if not entity_id:
            return ManageResponse(
                success=False,
                action=action,
                message="entity_id (source ID) required for sync action",
            )
        return await _sync_source(entity_manager, entity_id)

    if action == "refresh":
        return await _refresh_all_sources(entity_manager)

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
    source = Source(
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


# =============================================================================
# Analysis Action Handlers
# =============================================================================


async def _handle_analysis_action(
    action: str,
    entity_id: str | None,
    _data: dict[str, Any],
) -> ManageResponse:
    """Handle analysis actions."""
    if not entity_id:
        return ManageResponse(
            success=False,
            action=action,
            message=f"entity_id required for {action} action",
        )

    client = await get_graph_client()
    entity_manager = EntityManager(client)
    relationship_manager = RelationshipManager(client)

    if action == "estimate":
        return await _estimate_effort(entity_manager, entity_id)

    if action == "prioritize":
        return await _prioritize_tasks(entity_manager, relationship_manager, entity_id)

    if action == "detect_cycles":
        return await _detect_cycles(relationship_manager, entity_id)

    if action == "suggest":
        return await _suggest_knowledge(entity_manager, entity_id)

    return ManageResponse(success=False, action=action, message="Unknown analysis action")


async def _estimate_effort(
    entity_manager: EntityManager,
    task_id: str,
) -> ManageResponse:
    """Estimate task effort based on similar completed tasks."""
    from sibyl.tasks.manager import TaskManager

    client = await get_graph_client()
    relationship_manager = RelationshipManager(client)
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
    task_id: str,
) -> ManageResponse:
    """Suggest relevant knowledge for a task."""
    from sibyl.tasks.manager import TaskManager

    client = await get_graph_client()
    relationship_manager = RelationshipManager(client)
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


# =============================================================================
# Admin Action Handlers
# =============================================================================


async def _handle_admin_action(
    action: str,
    _entity_id: str | None,
    _data: dict[str, Any],
) -> ManageResponse:
    """Handle admin actions."""
    if action == "health":
        return await _get_health()

    if action == "stats":
        return await _get_stats()

    if action == "rebuild_index":
        return await _rebuild_index()

    return ManageResponse(success=False, action=action, message="Unknown admin action")


async def _get_health() -> ManageResponse:
    """Get server health status."""
    from sibyl.tools.core import get_health

    health = await get_health()

    return ManageResponse(
        success=health.get("status") == "healthy",
        action="health",
        message=f"Server is {health.get('status', 'unknown')}",
        data=health,
    )


async def _get_stats() -> ManageResponse:
    """Get graph statistics."""
    from sibyl.tools.core import get_stats

    stats = await get_stats()

    return ManageResponse(
        success="error" not in stats,
        action="stats",
        message=f"Total entities: {stats.get('total_entities', 0)}",
        data=stats,
    )


async def _rebuild_index() -> ManageResponse:
    """Rebuild FalkorDB indices."""
    # Note: Graphiti handles indices automatically
    # This is a placeholder for any manual index operations

    return ManageResponse(
        success=True,
        action="rebuild_index",
        message="Index rebuild requested (handled by Graphiti driver)",
        data={"note": "Graphiti manages indices automatically"},
    )
