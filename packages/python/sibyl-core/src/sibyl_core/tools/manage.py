"""Manage tool for Sibyl MCP Server.

The fourth tool: manage() handles operations that modify state.
Includes task workflow, source operations, analysis, and admin actions.

DEPRECATION NOTICE:
- Task workflow actions: Use RESTful /tasks/{id}/* endpoints instead
- Epic workflow actions: Use RESTful /epics/{id}/* endpoints instead
- Source/analysis actions: Still use this tool (no REST equivalent yet)
"""

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from sibyl_core.graph.client import get_graph_client
from sibyl_core.graph.entities import EntityManager
from sibyl_core.graph.relationships import RelationshipManager
from sibyl_core.models.entities import EntityType
from sibyl_core.tasks.dependencies import detect_dependency_cycles
from sibyl_core.tools.link_graph_status import get_link_graph_status_data

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
    "add_note",  # Add a note to a task
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
        - add_note: Add a note to a task (data.content, data.author_type, data.author_name)

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
    from sibyl_core.errors import InvalidTransitionError
    from sibyl_core.tasks.workflow import TaskWorkflowEngine

    workflow = TaskWorkflowEngine(entity_manager, relationship_manager, client, organization_id)

    # All actions below require entity_id (validated above except for update_task)
    try:
        if action == "start_task":
            assert entity_id is not None  # validated above
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
            assert entity_id is not None  # validated above
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
            assert entity_id is not None  # validated above
            task = await workflow.unblock_task(entity_id)
            return ManageResponse(
                success=True,
                action=action,
                entity_id=entity_id,
                message="Task unblocked, resuming work",
                data={"status": task.status.value},
            )

        if action == "submit_review":
            assert entity_id is not None  # validated above
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
            assert entity_id is not None  # validated above
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
            assert entity_id is not None  # validated above
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

        if action == "add_note":
            return await _add_note(entity_manager, relationship_manager, entity_id, data)

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


async def _add_note(
    entity_manager: EntityManager,
    relationship_manager: RelationshipManager,
    task_id: str | None,
    data: dict[str, Any],
) -> ManageResponse:
    """Add a note to a task.

    Args:
        entity_manager: EntityManager instance
        relationship_manager: RelationshipManager instance
        task_id: Task ID to add note to
        data: Dict containing note fields:
            - content: Note content (required)
            - author_type: "agent" (assistant-authored) or "user" (default: "user")
            - author_name: Author identifier (optional)
    """
    import uuid

    from sibyl_core.models.entities import Relationship, RelationshipType
    from sibyl_core.models.tasks import AuthorType, Note

    if not task_id:
        return ManageResponse(
            success=False,
            action="add_note",
            message="entity_id (task_id) required for add_note",
        )

    content = data.get("content")
    if not content:
        return ManageResponse(
            success=False,
            action="add_note",
            entity_id=task_id,
            message="data.content required for add_note",
        )

    # Verify task exists
    try:
        task = await entity_manager.get(task_id)
        if not task:
            return ManageResponse(
                success=False,
                action="add_note",
                entity_id=task_id,
                message=f"Task not found: {task_id}",
            )
    except Exception:
        return ManageResponse(
            success=False,
            action="add_note",
            entity_id=task_id,
            message=f"Task not found: {task_id}",
        )

    # Parse author_type
    author_type_str = data.get("author_type", "user")
    try:
        author_type = AuthorType(author_type_str)
    except ValueError:
        author_type = AuthorType.USER

    author_name = data.get("author_name", "")

    # Create note entity
    note_id = f"note_{uuid.uuid4()}"
    created_at = datetime.now(UTC)

    note = Note(  # type: ignore[call-arg]  # model_validator sets name from content
        id=note_id,
        task_id=task_id,
        content=content,
        author_type=author_type,
        author_name=author_name,
        created_at=created_at,
    )

    # Create in graph
    await entity_manager.create_direct(note)

    # Create BELONGS_TO relationship with task
    belongs_to = Relationship(
        id=f"rel_{note_id}_belongs_to_{task_id}",
        source_id=note_id,
        target_id=task_id,
        relationship_type=RelationshipType.BELONGS_TO,
    )
    await relationship_manager.create(belongs_to)

    log.info(
        "add_note_success",
        note_id=note_id,
        task_id=task_id,
        author_type=author_type.value,
    )

    return ManageResponse(
        success=True,
        action="add_note",
        entity_id=note_id,
        message="Note added to task",
        data={
            "note_id": note_id,
            "task_id": task_id,
            "author_type": author_type.value,
            "author_name": author_name,
            "created_at": created_at.isoformat(),
        },
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

    # Actions below require entity_id (already validated above except for update_epic)
    if action == "start_epic":
        assert entity_id is not None  # validated above
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
        assert entity_id is not None  # validated above
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
        assert entity_id is not None  # validated above
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

    if action == "crawl":
        url = data.get("url")
        assert isinstance(url, str)  # validated above
        depth = data.get("depth", 2)
        return await _crawl_source(url, depth, data, organization_id=organization_id)

    if action == "sync":
        assert entity_id is not None  # validated above
        return await _sync_source(entity_id, organization_id=organization_id)

    if action == "refresh":
        return await _refresh_all_sources(organization_id)

    if action == "link_graph":
        return await _link_graph(entity_id, data, organization_id)

    if action == "link_graph_status":
        return await _link_graph_status(organization_id)

    return ManageResponse(success=False, action=action, message="Unknown source action")


def _normalize_pattern_list(value: Any) -> list[str]:
    """Normalize a source pattern input into a string list."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list | tuple | set):
        return [str(item) for item in value if item]
    return [str(value)]


async def _create_or_get_crawl_source(
    url: str,
    depth: int,
    data: dict[str, Any],
    *,
    organization_id: str,
) -> tuple[str, bool]:
    """Create or reuse a relational crawl source for the given URL."""
    from uuid import UUID

    from sibyl.db import CrawlSource, SourceType, get_session
    from sqlalchemy import select
    from sqlmodel import col

    normalized_url = url.rstrip("/")
    source_name = str(data.get("name") or normalized_url.split("//")[-1].split("/")[0])
    source_type = str(data.get("source_type") or "website").lower()

    try:
        source_type_enum = SourceType(source_type)
    except ValueError:
        source_type_enum = SourceType.WEBSITE

    include_patterns = _normalize_pattern_list(
        data.get("include_patterns") or data.get("patterns")
    )
    exclude_patterns = _normalize_pattern_list(
        data.get("exclude_patterns") or data.get("exclude")
    )

    async with get_session() as session:
        result = await session.execute(
            select(CrawlSource).where(
                col(CrawlSource.organization_id) == UUID(organization_id),
                col(CrawlSource.url) == normalized_url,
            )
        )
        source = result.scalar_one_or_none()
        if source is not None:
            return str(source.id), False

        source = CrawlSource(
            name=source_name,
            url=normalized_url,
            organization_id=UUID(organization_id),
            source_type=source_type_enum,
            description=data.get("description"),
            crawl_depth=max(0, min(int(depth), 10)),
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
        )
        session.add(source)
        await session.flush()
        await session.refresh(source)
        return str(source.id), True


async def _crawl_source_exists(source_id: str, organization_id: str) -> bool:
    """Return whether a crawl source exists within the organization."""
    from uuid import UUID

    from sibyl.db import CrawlSource, get_session
    from sqlalchemy import select
    from sqlmodel import col

    async with get_session() as session:
        result = await session.execute(
            select(CrawlSource).where(
                col(CrawlSource.id) == UUID(source_id),
                col(CrawlSource.organization_id) == UUID(organization_id),
            )
        )
        return result.scalar_one_or_none() is not None


async def _list_crawl_source_ids(organization_id: str) -> list[str]:
    """List crawl source IDs for an organization."""
    from uuid import UUID

    from sibyl.db import CrawlSource, get_session
    from sqlalchemy import select
    from sqlmodel import col

    async with get_session() as session:
        result = await session.execute(
            select(CrawlSource).where(col(CrawlSource.organization_id) == UUID(organization_id))
        )
        return [str(source.id) for source in result.scalars().all()]


async def _enqueue_source_crawl(
    source_id: str,
    *,
    organization_id: str,
    max_pages: int = 50,
    max_depth: int = 3,
    generate_embeddings: bool = True,
    force: bool = False,
) -> str:
    """Enqueue a crawl job and sync its pending state to the relational source."""
    from uuid import UUID

    from sibyl.db import CrawlSource, CrawlStatus, get_session
    from sibyl.jobs.queue import enqueue_crawl
    from sqlalchemy import select
    from sqlmodel import col

    job_id = await enqueue_crawl(
        source_id,
        max_pages=max_pages,
        max_depth=max_depth,
        generate_embeddings=generate_embeddings,
        force=force,
    )

    async with get_session() as session:
        result = await session.execute(
            select(CrawlSource).where(
                col(CrawlSource.id) == UUID(source_id),
                col(CrawlSource.organization_id) == UUID(organization_id),
            )
        )
        source = result.scalar_one_or_none()
        if source is not None:
            source.current_job_id = job_id
            source.crawl_status = CrawlStatus.PENDING
            source.last_error = None
            session.add(source)

    return job_id


async def _enqueue_source_sync(source_id: str) -> str:
    """Enqueue a source-stat sync job."""
    from sibyl.jobs.queue import enqueue_sync

    return await enqueue_sync(source_id)


async def _crawl_source(
    url: str,
    depth: int,
    data: dict[str, Any],
    *,
    organization_id: str,
) -> ManageResponse:
    """Trigger crawl of a URL."""
    source_id, created = await _create_or_get_crawl_source(
        url,
        depth,
        data,
        organization_id=organization_id,
    )
    max_pages = int(data.get("max_pages", 50))
    generate_embeddings = bool(data.get("generate_embeddings", True))
    job_id = await _enqueue_source_crawl(
        source_id,
        organization_id=organization_id,
        max_pages=max_pages,
        max_depth=max(1, min(int(depth), 5)),
        generate_embeddings=generate_embeddings,
        force=not created or bool(data.get("force", False)),
    )

    return ManageResponse(
        success=True,
        action="crawl",
        entity_id=source_id,
        message=f"Crawl queued for {url}",
        data={
            "source_id": source_id,
            "url": url,
            "depth": depth,
            "status": "queued",
            "job_id": job_id,
            "created": created,
        },
    )


async def _sync_source(
    source_id: str,
    *,
    organization_id: str,
) -> ManageResponse:
    """Sync an existing source's persisted stats."""
    if not await _crawl_source_exists(source_id, organization_id):
        return ManageResponse(
            success=False,
            action="sync",
            entity_id=source_id,
            message=f"Source not found: {source_id}",
        )

    job_id = await _enqueue_source_sync(source_id)

    return ManageResponse(
        success=True,
        action="sync",
        entity_id=source_id,
        message="Sync queued",
        data={"status": "queued", "job_id": job_id},
    )


async def _refresh_all_sources(
    organization_id: str,
) -> ManageResponse:
    """Sync all sources."""
    source_ids = await _list_crawl_source_ids(organization_id)

    for source_id in source_ids:
        await _enqueue_source_sync(source_id)

    return ManageResponse(
        success=True,
        action="refresh",
        message=f"Refresh queued for {len(source_ids)} sources",
        data={"sources_queued": len(source_ids)},
    )


async def _link_graph(
    source_id: str | None,
    data: dict[str, Any],
    organization_id: str,
) -> ManageResponse:
    """Link document chunks to knowledge graph entities via LLM extraction."""
    from uuid import UUID

    from sibyl.crawler.graph_integration import GraphIntegrationService
    from sibyl.db import CrawledDocument, CrawlSource, DocumentChunk, get_session
    from sqlalchemy import select
    from sqlmodel import col

    from sibyl_core.graph.client import get_graph_client

    max_chunks = data.get("max_chunks", 1000)
    create_new_entities = bool(data.get("create_new_entities", False))
    org_uuid = UUID(organization_id)

    # Build query for unlinked chunks
    query = (
        select(DocumentChunk)
        .join(CrawledDocument, col(CrawledDocument.id) == col(DocumentChunk.document_id))
        .join(CrawlSource, col(CrawlSource.id) == col(CrawledDocument.source_id))
        .where(col(CrawlSource.organization_id) == org_uuid)
        .where(col(DocumentChunk.has_entities) == False)  # noqa: E712
    )

    if source_id:
        # Filter to specific source via document join
        query = query.where(CrawlSource.id == UUID(source_id))  # type: ignore[arg-type]

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
            data={
                "chunks_processed": 0,
                "entities_extracted": 0,
                "entities_linked": 0,
                "new_entities_created": 0,
                "errors": 0,
                "create_new_entities": create_new_entities,
            },
        )

    # Process chunks
    client = await get_graph_client()
    integration = GraphIntegrationService(
        client,
        organization_id,
        create_new_entities=create_new_entities,
    )

    source_name = source_id or "all_sources"
    stats = await integration.process_chunks(list(chunks), source_name=source_name)

    message = f"Linked {stats.entities_linked} entities from {stats.chunks_processed} chunks"
    if stats.new_entities_created > 0:
        message += f" and created {stats.new_entities_created} new graph entities"

    return ManageResponse(
        success=True,
        action="link_graph",
        entity_id=source_id,
        message=message,
        data={
            "chunks_processed": stats.chunks_processed,
            "entities_extracted": stats.entities_extracted,
            "entities_linked": stats.entities_linked,
            "new_entities_created": stats.new_entities_created,
            "errors": stats.errors,
            "create_new_entities": create_new_entities,
        },
    )


async def _link_graph_status(organization_id: str) -> ManageResponse:
    """Get status of graph linking (pending chunks per source)."""
    from sibyl.db import get_session

    async with get_session() as session:
        status = await get_link_graph_status_data(session, organization_id)

    return ManageResponse(
        success=True,
        action="link_graph_status",
        message=f"{status.chunks_pending} chunks pending linking",
        data={
            "total_chunks": status.total_chunks,
            "chunks_with_entities": status.chunks_with_entities,
            "chunks_pending": status.chunks_pending,
            "sources": [asdict(source) for source in status.sources],
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
        return await _detect_cycles(client, organization_id, entity_id)

    if action == "suggest":
        return await _suggest_knowledge(entity_manager, relationship_manager, entity_id)

    return ManageResponse(success=False, action=action, message="Unknown analysis action")


async def _estimate_effort(
    entity_manager: EntityManager,
    relationship_manager: RelationshipManager,
    task_id: str,
) -> ManageResponse:
    """Estimate task effort based on similar completed tasks."""
    from sibyl_core.tasks.manager import TaskManager

    task_manager = TaskManager(entity_manager, relationship_manager)

    # Get the task
    try:
        entity = await entity_manager.get(task_id)
        task = task_manager._entity_to_task(entity)
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
    client: Any,
    organization_id: str,
    project_id: str,
) -> ManageResponse:
    """Detect circular dependencies in a project's task graph."""
    cycle_result = await detect_dependency_cycles(
        client,
        organization_id,
        project_id=project_id,
    )
    return ManageResponse(
        success=True,
        action="detect_cycles",
        entity_id=project_id,
        message=cycle_result.message,
        data={
            "cycles": cycle_result.cycles,
            "has_cycles": cycle_result.has_cycles,
            "cycle_count": len(cycle_result.cycles),
        },
    )


async def _suggest_knowledge(
    entity_manager: EntityManager,
    relationship_manager: RelationshipManager,
    task_id: str,
) -> ManageResponse:
    """Suggest relevant knowledge for a task."""
    from sibyl_core.tasks.manager import TaskManager

    task_manager = TaskManager(entity_manager, relationship_manager)

    # Get the task
    try:
        entity = await entity_manager.get(task_id)
        task = task_manager._entity_to_task(entity)
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
