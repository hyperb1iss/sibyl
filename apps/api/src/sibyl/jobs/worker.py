"""arq worker - processes background jobs.

Run with: uv run arq sibyl.jobs.WorkerSettings

This worker processes:
- crawl_source: Full documentation crawling
- sync_source: Recalculate source stats
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from arq.connections import RedisSettings
from sqlalchemy import func, select
from sqlmodel import col

from sibyl.config import settings
from sibyl.db import (
    CrawledDocument,
    CrawlSource,
    CrawlStatus,
    DocumentChunk,
    get_session,
)
from sibyl.db.models import utcnow_naive

log = structlog.get_logger()


async def _safe_broadcast(event: str, data: dict[str, Any], *, org_id: str | None) -> None:
    """Broadcast event, silently ignoring failures (WebSocket may not be available)."""
    try:
        from sibyl.api.websocket import broadcast_event

        await broadcast_event(event, data, org_id=org_id)
    except Exception:
        log.debug("Broadcast failed (WebSocket unavailable)", event=event)


def get_redis_settings() -> RedisSettings:
    """Get Redis connection settings."""
    return RedisSettings(
        host=settings.falkordb_host,
        port=settings.falkordb_port,
        password=settings.falkordb_password,
        database=settings.redis_jobs_db,
    )


async def startup(ctx: dict[str, Any]) -> None:
    """Worker startup - initialize resources."""
    from sibyl.banner import log_banner
    from sibyl_core.logging import configure_logging

    # Reconfigure logging for worker (overrides API default)
    configure_logging(service_name="worker")

    log_banner(component="worker")
    log.info("Job worker online")
    ctx["start_time"] = datetime.now(UTC)


async def shutdown(ctx: dict[str, Any]) -> None:  # noqa: ARG001
    """Worker shutdown - cleanup resources."""
    log.info("Job worker shutting down")


async def crawl_source(
    ctx: dict[str, Any],  # noqa: ARG001
    source_id: str,
    *,
    max_pages: int = 100,
    max_depth: int = 3,
    generate_embeddings: bool = True,
) -> dict[str, Any]:
    """Crawl a documentation source.

    This is the main crawl job that:
    1. Fetches source from DB
    2. Runs the ingestion pipeline
    3. Updates source status
    4. Returns stats

    Args:
        ctx: arq context
        source_id: UUID of source to crawl
        max_pages: Maximum pages to crawl
        max_depth: Maximum link depth
        generate_embeddings: Whether to generate embeddings

    Returns:
        Dict with crawl stats
    """
    from sibyl.crawler import IngestionPipeline

    log.info(
        "Starting crawl job",
        source_id=source_id,
        max_pages=max_pages,
        max_depth=max_depth,
    )

    # Get source and update status
    async with get_session() as session:
        source = await session.get(CrawlSource, UUID(source_id))
        if not source:
            raise ValueError(f"Source not found: {source_id}")

        # Update status to IN_PROGRESS
        source.crawl_status = CrawlStatus.IN_PROGRESS
        source.last_error = None
        await session.flush()  # Persist status change before expunge

        # Detach for background processing
        source_name = source.name
        organization_id = str(source.organization_id)
        session.expunge(source)

    # Broadcast start event
    await _safe_broadcast(
        "crawl_started",
        {
            "source_id": source_id,
            "source_name": source_name,
            "max_pages": max_pages,
        },
        org_id=organization_id,
    )

    # Progress callback: update DB + broadcast after each document
    async def on_progress(stats: Any, chunks_added: int) -> None:
        """Update source stats and broadcast progress after each document."""
        async with get_session() as session:
            db_source = await session.get(CrawlSource, UUID(source_id))
            if db_source:
                db_source.document_count = stats.documents_stored
                db_source.chunk_count = stats.chunks_created

        await _safe_broadcast(
            "crawl_progress",
            {
                "source_id": source_id,
                "source_name": source_name,
                "documents_crawled": stats.documents_crawled,
                "documents_stored": stats.documents_stored,
                "chunks_created": stats.chunks_created,
                "chunks_added": chunks_added,
                "errors": stats.errors,
            },
            org_id=organization_id,
        )

    # Run ingestion
    try:
        async with IngestionPipeline(
            organization_id, generate_embeddings=generate_embeddings
        ) as pipeline:
            stats = await pipeline.ingest_source(
                source,
                max_pages=max_pages,
                max_depth=max_depth,
                on_progress=on_progress,
            )

        # Update source with results
        async with get_session() as session:
            db_source = await session.get(CrawlSource, UUID(source_id))
            if db_source:
                db_source.crawl_status = (
                    CrawlStatus.COMPLETED if stats.errors == 0 else CrawlStatus.PARTIAL
                )
                db_source.current_job_id = None  # Clear job ID on completion
                db_source.last_crawled_at = utcnow_naive()
                db_source.document_count = stats.documents_stored
                db_source.chunk_count = stats.chunks_created

        result = {
            "source_id": source_id,
            "source_name": source_name,
            "documents_crawled": stats.documents_crawled,
            "documents_stored": stats.documents_stored,
            "chunks_created": stats.chunks_created,
            "embeddings_generated": stats.embeddings_generated,
            "errors": stats.errors,
            "duration_seconds": stats.duration_seconds,
        }

        # Broadcast completion
        await _safe_broadcast("crawl_complete", result, org_id=organization_id)

        log.info("Crawl job complete", **result)
        return result

    except Exception as e:
        # Update source with error
        async with get_session() as session:
            db_source = await session.get(CrawlSource, UUID(source_id))
            if db_source:
                db_source.crawl_status = CrawlStatus.FAILED
                db_source.current_job_id = None  # Clear job on failure
                db_source.last_error = str(e)[:1000]

        await _safe_broadcast(
            "crawl_complete",
            {"source_id": source_id, "error": str(e)},
            org_id=organization_id,
        )

        log.exception("Crawl job failed", source_id=source_id)
        raise


async def sync_source(ctx: dict[str, Any], source_id: str) -> dict[str, Any]:  # noqa: ARG001
    """Sync source stats from actual data.

    Recalculates document_count, chunk_count, and fixes status.

    Args:
        ctx: arq context
        source_id: UUID of source to sync

    Returns:
        Dict with sync results
    """
    log.info("Starting sync job", source_id=source_id)

    async with get_session() as session:
        source = await session.get(CrawlSource, UUID(source_id))
        if not source:
            raise ValueError(f"Source not found: {source_id}")
        organization_id = str(source.organization_id)

        # Count actual documents
        doc_result = await session.execute(
            select(func.count(CrawledDocument.id)).where(
                col(CrawledDocument.source_id) == UUID(source_id)
            )
        )
        doc_count = doc_result.scalar() or 0

        # Count actual chunks
        chunk_result = await session.execute(
            select(func.count(DocumentChunk.id))
            .join(CrawledDocument)
            .where(col(CrawledDocument.source_id) == UUID(source_id))
        )
        chunk_count = chunk_result.scalar() or 0

        # Update source
        old_status = source.crawl_status
        old_doc_count = source.document_count
        old_chunk_count = source.chunk_count

        source.document_count = doc_count
        source.chunk_count = chunk_count

        if doc_count > 0 and source.crawl_status == CrawlStatus.IN_PROGRESS:
            source.crawl_status = CrawlStatus.COMPLETED
            source.current_job_id = None  # Clear job on sync completion
            if source.last_crawled_at is None:
                source.last_crawled_at = utcnow_naive()
        elif doc_count == 0 and source.crawl_status == CrawlStatus.IN_PROGRESS:
            source.crawl_status = CrawlStatus.PENDING
            source.current_job_id = None  # Clear job on sync reset

        result = {
            "source_id": source_id,
            "document_count": doc_count,
            "chunk_count": chunk_count,
            "status": source.crawl_status.value,
            "changes": {
                "status": f"{old_status.value} -> {source.crawl_status.value}"
                if old_status != source.crawl_status
                else None,
                "document_count": f"{old_doc_count} -> {doc_count}"
                if old_doc_count != doc_count
                else None,
                "chunk_count": f"{old_chunk_count} -> {chunk_count}"
                if old_chunk_count != chunk_count
                else None,
            },
        }

    log.info("Sync job complete", **result)
    await _safe_broadcast("crawl_sync_complete", result, org_id=organization_id)
    return result


async def create_entity(
    ctx: dict[str, Any],  # noqa: ARG001
    entity_data: dict[str, Any],
    entity_type: str,
    group_id: str,
    relationships: list[dict[str, Any]] | None = None,
    auto_link_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create entity asynchronously via Graphiti.

    This job runs in the background so callers get fast responses while
    Graphiti handles LLM-powered entity extraction and relationship discovery.

    Args:
        ctx: arq context
        entity_data: Serialized entity dict (from entity.model_dump())
        entity_type: Type string (episode, pattern, task, project)
        group_id: Organization ID
        relationships: Optional list of explicit relationships to create
        auto_link_params: Parameters for auto-link discovery (always runs if provided)

    Returns:
        Dict with creation results
    """
    from sibyl_core.graph.client import get_graph_client
    from sibyl_core.graph.entities import EntityManager
    from sibyl_core.graph.relationships import RelationshipManager
    from sibyl_core.models.entities import (
        Entity,
        Episode,
        Pattern,
        Relationship,
        RelationshipType,
    )
    from sibyl_core.models.tasks import Project, Task

    relationships = relationships or []

    log.info(
        "create_entity_started",
        entity_id=entity_data.get("id"),
        entity_type=entity_type,
        relationships_count=len(relationships),
    )

    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=group_id)

        # Reconstruct the entity from serialized data
        entity: Entity
        if entity_type == "task":
            entity = Task.model_validate(entity_data)
        elif entity_type == "project":
            entity = Project.model_validate(entity_data)
        elif entity_type == "pattern":
            entity = Pattern.model_validate(entity_data)
        else:
            entity = Episode.model_validate(entity_data)

        # Use create_direct() for structured entities (faster, generates embeddings)
        # Use create() for episodes (LLM extraction may add value)
        if entity_type in ("task", "project", "pattern"):
            created_id = await entity_manager.create_direct(entity)
        else:
            created_id = await entity_manager.create(entity)

        log.info(
            "create_entity_graph_created",
            entity_id=created_id,
            entity_type=entity_type,
        )

        # Relationship manager for explicit and auto-discovered relationships
        relationship_manager = RelationshipManager(client, group_id=group_id)

        # Create explicit relationships (BELONGS_TO, DEPENDS_ON, etc.)
        relationships_created = 0
        for rel_data in relationships:
            try:
                rel_type = RelationshipType(rel_data.get("type", "RELATED_TO"))
                rel = Relationship(
                    id=rel_data.get("id"),
                    source_id=rel_data.get("source_id"),
                    target_id=rel_data.get("target_id"),
                    relationship_type=rel_type,
                    metadata=rel_data.get("metadata", {}),
                )
                await relationship_manager.create(rel)
                relationships_created += 1
                log.debug(
                    "create_entity_relationship_created",
                    rel_type=rel_type.value,
                    source=rel_data.get("source_id"),
                    target=rel_data.get("target_id"),
                )
            except Exception as e:
                log.warning(
                    "create_entity_relationship_failed",
                    error=str(e),
                    rel_data=rel_data,
                )

        # Auto-link: discover related entities via similarity search
        auto_links_created = 0
        if auto_link_params:
            try:
                from sibyl_core.tools.core import _auto_discover_links

                auto_link_results = await _auto_discover_links(
                    entity_manager=entity_manager,
                    title=auto_link_params.get("title", ""),
                    content=auto_link_params.get("content", ""),
                    technologies=auto_link_params.get("technologies", []),
                    category=auto_link_params.get("category"),
                    exclude_id=created_id,
                    threshold=0.75,
                    limit=5,
                )

                for linked_id, score in auto_link_results:
                    try:
                        rel = Relationship(
                            id=f"rel_{created_id}_references_{linked_id}",
                            source_id=created_id,
                            target_id=linked_id,
                            relationship_type=RelationshipType.RELATED_TO,
                            metadata={
                                "auto_linked": True,
                                "similarity_score": score,
                            },
                        )
                        await relationship_manager.create(rel)
                        auto_links_created += 1
                        log.debug(
                            "create_entity_auto_link_created",
                            target=linked_id,
                            score=score,
                        )
                    except Exception as e:
                        log.warning("create_entity_auto_link_failed", error=str(e))

                log.info(
                    "create_entity_auto_link_complete",
                    entity_id=created_id,
                    links_found=len(auto_link_results),
                )
            except Exception as e:
                log.warning("create_entity_auto_link_search_failed", error=str(e))

        result = {
            "entity_id": created_id,
            "entity_type": entity_type,
            "relationships_created": relationships_created,
            "auto_links_created": auto_links_created,
        }

        # Broadcast entity creation event
        await _safe_broadcast(
            "entity_created",
            {
                "id": created_id,
                "entity_type": entity_type,
                "name": entity_data.get("name"),
            },
            org_id=group_id,
        )

        log.info("create_entity_completed", **result)
        return result

    except Exception as e:
        log.exception(
            "create_entity_failed",
            error=str(e),
            entity_id=entity_data.get("id"),
        )
        raise


async def create_learning_episode(
    ctx: dict[str, Any],  # noqa: ARG001
    task_data: dict[str, Any],
    group_id: str,
) -> dict[str, Any]:
    """Create a learning episode from a completed task.

    This job runs in the background so task completion returns fast while
    Graphiti handles LLM-powered entity extraction from the learnings.

    Args:
        ctx: arq context
        task_data: Serialized task dict (from task.model_dump())
        group_id: Organization ID

    Returns:
        Dict with episode creation results
    """
    from sibyl_core.graph.client import get_graph_client
    from sibyl_core.graph.entities import EntityManager
    from sibyl_core.graph.relationships import RelationshipManager
    from sibyl_core.models.entities import (
        EntityType,
        Episode,
        Relationship,
        RelationshipType,
    )
    from sibyl_core.models.tasks import Task

    task = Task.model_validate(task_data)

    log.info(
        "create_learning_episode_started",
        task_id=task.id,
        task_title=task.title,
    )

    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=group_id)
        relationship_manager = RelationshipManager(client, group_id=group_id)

        # Format episode content
        content_parts = [
            f"## Task: {task.title}",
            "",
            f"**Status**: {task.status}",
            f"**Feature**: {task.feature or 'N/A'}",
            f"**Technologies**: {', '.join(task.technologies)}",
        ]

        if task.actual_hours:
            content_parts.append(f"**Time Spent**: {task.actual_hours} hours")

        if task.estimated_hours and task.actual_hours:
            accuracy = (task.estimated_hours / task.actual_hours) * 100
            content_parts.append(f"**Estimation Accuracy**: {accuracy:.1f}%")

        content_parts.extend(
            [
                "",
                "### What Was Done",
                "",
                task.description,
                "",
                "### Learnings",
                "",
                task.learnings or "",
            ]
        )

        if task.blockers_encountered:
            content_parts.extend(
                [
                    "",
                    "### Blockers Encountered",
                    "",
                ]
            )
            content_parts.extend(f"- {b}" for b in task.blockers_encountered)

        if task.commit_shas:
            content_parts.extend(
                [
                    "",
                    "### Related Commits",
                    "",
                ]
            )
            content_parts.extend(f"- `{sha}`" for sha in task.commit_shas)

        # Create episode
        episode = Episode(
            id=f"episode_{task.id}",
            entity_type=EntityType.EPISODE,
            name=f"Task Completed: {task.title}",
            description=task.description,
            content="\n".join(content_parts),
            episode_type="task_completion",
            metadata={
                "task_id": task.id,
                "project_id": task.project_id,
                "feature": task.feature,
                "technologies": task.technologies,
                "complexity": task.complexity.value if task.complexity else None,
                "estimated_hours": task.estimated_hours,
                "actual_hours": task.actual_hours,
                "estimation_accuracy": (
                    task.estimated_hours / task.actual_hours
                    if task.estimated_hours and task.actual_hours
                    else None
                ),
            },
            valid_from=task.completed_at,
        )

        # Use Graphiti create for relationship discovery from learnings
        episode_id = await entity_manager.create(episode)

        log.info(
            "create_learning_episode_entity_created",
            episode_id=episode_id,
            task_id=task.id,
        )

        # Link episode back to task
        await relationship_manager.create(
            Relationship(
                id=f"rel_episode_{task.id}",
                source_id=episode_id,
                target_id=task.id,
                relationship_type=RelationshipType.DERIVED_FROM,
            )
        )

        # Inherit knowledge relationships from task
        task_relationships = await relationship_manager.get_for_entity(
            task.id,
            relationship_types=[
                RelationshipType.REQUIRES,
                RelationshipType.REFERENCES,
                RelationshipType.PART_OF,
            ],
        )

        inherited_count = 0
        for rel in task_relationships:
            try:
                await relationship_manager.create(
                    Relationship(
                        id=f"rel_episode_{episode_id}_{rel.target_id}",
                        source_id=episode_id,
                        target_id=rel.target_id,
                        relationship_type=RelationshipType.REFERENCES,
                        metadata={"inherited_from_task": task.id},
                    )
                )
                inherited_count += 1
            except Exception as e:
                log.warning(
                    "create_learning_episode_inherit_failed",
                    error=str(e),
                    target_id=rel.target_id,
                )

        result = {
            "episode_id": episode_id,
            "task_id": task.id,
            "inherited_relationships": inherited_count,
        }

        # Broadcast episode creation
        await _safe_broadcast(
            "entity_created",
            {
                "id": episode_id,
                "entity_type": "episode",
                "name": episode.name,
                "derived_from": task.id,
            },
            org_id=group_id,
        )

        log.info("create_learning_episode_completed", **result)
        return result

    except Exception as e:
        log.exception(
            "create_learning_episode_failed",
            task_id=task.id,
            error=str(e),
        )
        raise


async def update_entity(
    ctx: dict[str, Any],  # noqa: ARG001
    entity_id: str,
    updates: dict[str, Any],
    entity_type: str,
    group_id: str,
) -> dict[str, Any]:
    """Update entity fields asynchronously.

    Generic entity update job that works for any entity type.
    Runs in the background so callers get fast responses.

    Args:
        ctx: arq context
        entity_id: The entity ID to update
        updates: Dict of field names to new values
        entity_type: Type string (episode, pattern, task, project, etc.)
        group_id: Organization ID

    Returns:
        Dict with update results
    """
    from sibyl_core.graph.client import get_graph_client
    from sibyl_core.graph.entities import EntityManager

    log.info(
        "update_entity_started",
        entity_id=entity_id,
        entity_type=entity_type,
        fields=list(updates.keys()),
    )

    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client, group_id=group_id)

        # Perform the update
        result = await entity_manager.update(entity_id, updates)

        if result:
            # Broadcast update event
            await _safe_broadcast(
                "entity_updated",
                {
                    "id": entity_id,
                    "entity_type": entity_type,
                    "fields": list(updates.keys()),
                },
                org_id=group_id,
            )

            log.info(
                "update_entity_completed",
                entity_id=entity_id,
                entity_type=entity_type,
                fields=list(updates.keys()),
            )

            return {
                "entity_id": entity_id,
                "entity_type": entity_type,
                "updated_fields": list(updates.keys()),
                "success": True,
            }

        log.warning("update_entity_no_changes", entity_id=entity_id)
        return {
            "entity_id": entity_id,
            "entity_type": entity_type,
            "updated_fields": [],
            "success": False,
            "message": "No changes made",
        }

    except Exception as e:
        log.exception(
            "update_entity_failed",
            entity_id=entity_id,
            entity_type=entity_type,
            error=str(e),
        )
        raise


def _get_tool_icon_and_preview(tool_name: str, tool_input: dict[str, Any]) -> tuple[str, str]:
    """Get icon name and preview text for a tool call."""
    if tool_name == "Read":
        return "Page", f"Reading `{tool_input.get('file_path', 'file')}`"
    if tool_name == "Write":
        return "Page", f"Writing to `{tool_input.get('file_path', 'file')}`"
    if tool_name == "Edit":
        return "EditPencil", f"Editing `{tool_input.get('file_path', 'file')}`"
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return "Code", f"`{cmd[:80]}{'...' if len(cmd) > 80 else ''}`"
    if tool_name == "Grep":
        return "Search", f"Searching for `{tool_input.get('pattern', '')}`"
    if tool_name == "Glob":
        return "Folder", f"Finding `{tool_input.get('pattern', '')}`"
    if tool_name == "WebSearch":
        return "Globe", f"Searching: {tool_input.get('query', '')}"
    if tool_name == "WebFetch":
        return "Globe", f"Fetching: {tool_input.get('url', '')[:60]}"
    if tool_name == "Task":
        return "User", f"Spawning agent: {tool_input.get('description', '')[:50]}"
    if tool_name == "TodoWrite":
        return "List", "Updating task list"
    return "Settings", tool_name


def _format_assistant_message(content: Any, timestamp: str) -> dict[str, Any]:
    """Format an AssistantMessage for UI display."""
    if isinstance(content, list):
        blocks = []
        for block in content:
            block_type = type(block).__name__
            if block_type == "TextBlock":
                blocks.append({"type": "text", "content": getattr(block, "text", "")})
            elif block_type == "ToolUseBlock":
                tool_name = getattr(block, "name", "unknown")
                tool_input = getattr(block, "input", {})
                tool_id = getattr(block, "id", "")
                if isinstance(tool_input, dict):
                    icon, preview = _get_tool_icon_and_preview(tool_name, tool_input)
                    blocks.append(
                        {
                            "type": "tool_use",
                            "tool_name": tool_name,
                            "tool_id": tool_id,
                            "icon": icon,
                            "input": tool_input,
                            "preview": preview,
                        }
                    )

        if len(blocks) == 1:
            return {"role": "assistant", "timestamp": timestamp, **blocks[0]}
        return {
            "role": "assistant",
            "type": "multi_block",
            "blocks": blocks,
            "timestamp": timestamp,
            "preview": blocks[0].get("preview", "") if blocks else "",
        }
    return {
        "role": "assistant",
        "type": "text",
        "content": str(content) if content else "",
        "timestamp": timestamp,
        "preview": str(content)[:100] if content else "",
    }


def _format_user_message(content: Any, timestamp: str) -> dict[str, Any]:
    """Format a UserMessage (usually tool results) for UI display."""
    if isinstance(content, list):
        results = []
        for block in content:
            if type(block).__name__ == "ToolResultBlock":
                tool_id = getattr(block, "tool_use_id", "")
                result_content = getattr(block, "content", "")
                is_error = getattr(block, "is_error", False)
                preview = str(result_content)[:200]
                if len(str(result_content)) > 200:
                    preview += "..."
                results.append(
                    {
                        "type": "tool_result",
                        "tool_id": tool_id,
                        "content": str(result_content),
                        "preview": preview,
                        "is_error": is_error,
                        "icon": "Xmark" if is_error else "Check",
                    }
                )
        if len(results) == 1:
            return {"role": "tool", "timestamp": timestamp, **results[0]}
        if results:
            return {
                "role": "tool",
                "type": "multi_result",
                "results": results,
                "timestamp": timestamp,
                "preview": f"{len(results)} tool results",
            }
    return {
        "role": "user",
        "type": "text",
        "content": str(content) if content else "",
        "timestamp": timestamp,
        "preview": str(content)[:100] if content else "",
    }


def _format_agent_message(message: Any) -> dict[str, Any]:
    """Format a Claude SDK message for beautiful UI display."""
    msg_class = type(message).__name__
    content = getattr(message, "content", None)
    timestamp = datetime.now(UTC).isoformat()

    if msg_class == "AssistantMessage":
        return _format_assistant_message(content, timestamp)

    if msg_class == "UserMessage":
        return _format_user_message(content, timestamp)

    if msg_class == "ResultMessage":
        usage = getattr(message, "usage", None)
        cost = getattr(message, "total_cost_usd", None)
        return {
            "role": "system",
            "type": "result",
            "icon": "Dollar",
            "session_id": getattr(message, "session_id", None),
            "usage": {
                "input_tokens": getattr(usage, "input_tokens", 0) if usage else 0,
                "output_tokens": getattr(usage, "output_tokens", 0) if usage else 0,
            }
            if usage
            else None,
            "cost_usd": cost,
            "timestamp": timestamp,
            "preview": f"${cost:.4f}" if cost else "Completed",
        }

    return {
        "role": "unknown",
        "type": msg_class.lower(),
        "content": str(content) if content else "",
        "timestamp": timestamp,
        "preview": f"{msg_class}: {str(content)[:50]}" if content else msg_class,
    }


async def run_agent_execution(
    ctx: dict[str, Any],  # noqa: ARG001
    agent_id: str,
    org_id: str,
    project_id: str,
    prompt: str,
    *,
    agent_type: str = "general",
    task_id: str | None = None,
    created_by: str | None = None,
) -> dict[str, Any]:
    """Execute a Claude agent in the worker process.

    This job runs long-running AI agent tasks in the background worker,
    keeping the API responsive. Creates checkpoints only at completion.

    Args:
        ctx: arq context
        agent_id: Pre-created agent ID
        org_id: Organization ID
        project_id: Project ID
        prompt: Initial prompt for the agent
        agent_type: Type of agent
        task_id: Optional task ID
        created_by: User ID who spawned the agent

    Returns:
        Dict with execution results
    """
    from sibyl.agents import AgentRunner, WorktreeManager
    from sibyl_core.graph.client import get_graph_client
    from sibyl_core.graph.entities import EntityManager
    from sibyl_core.models import AgentCheckpoint, AgentSpawnSource, AgentStatus, AgentType

    log.info(
        "run_agent_execution_started",
        agent_id=agent_id,
        agent_type=agent_type,
        task_id=task_id,
    )

    try:
        client = await get_graph_client()
        manager = EntityManager(client, group_id=org_id)

        # Create worktree manager and agent runner
        worktree_manager = WorktreeManager(
            entity_manager=manager,
            org_id=org_id,
            project_id=project_id,
            repo_path=".",
        )

        runner = AgentRunner(
            entity_manager=manager,
            worktree_manager=worktree_manager,
            org_id=org_id,
            project_id=project_id,
        )

        # Get task if specified
        task = None
        if task_id:
            from sibyl_core.models import Task

            entity = await manager.get(task_id)
            if entity and isinstance(entity, Task):
                task = entity

        # Spawn the agent instance with pre-generated ID
        instance = await runner.spawn(
            prompt=prompt,
            agent_type=AgentType(agent_type),
            task=task,
            spawn_source=AgentSpawnSource.USER,
            create_worktree=False,
            enable_approvals=True,
            agent_id=agent_id,
        )

        # Update with created_by if provided
        if created_by:
            await manager.update(agent_id, {"created_by": created_by})

        # Broadcast that agent is now working
        await _safe_broadcast(
            "agent_status",
            {"agent_id": agent_id, "status": "working"},
            org_id=org_id,
        )

        # Track execution state (in memory only until completion)
        message_count = 0
        session_id = ""
        last_content = ""
        tool_calls: list[str] = []

        # Execute agent - stream messages to UI in real-time
        log.info("run_agent_execution_starting", agent_id=agent_id)
        async for message in instance.execute():
            message_count += 1
            msg_class = type(message).__name__

            # Format message for UI
            formatted = _format_agent_message(message)

            log.debug(
                "run_agent_message",
                agent_id=agent_id,
                message_num=message_count,
                message_type=msg_class,
                content_preview=formatted.get("preview", "")[:100],
            )

            # Broadcast message to UI in real-time
            await _safe_broadcast(
                "agent_message",
                {
                    "agent_id": agent_id,
                    "message_num": message_count,
                    **formatted,
                },
                org_id=org_id,
            )

            # Track session ID
            if sid := getattr(message, "session_id", None):
                session_id = sid

            # Track tool calls for summary
            if "ToolUse" in msg_class or formatted.get("type") == "tool_use":
                tool_name = formatted.get("tool_name", "unknown")
                tool_calls.append(tool_name)

            # Keep last meaningful content for summary
            if formatted.get("content") and formatted.get("type") != "tool_result":
                last_content = formatted.get("content", "")[:500]

        # Create checkpoint only on completion (summary, not full history)
        from uuid import uuid4

        checkpoint_id = f"chkpt_{uuid4().hex[:12]}"
        summary = f"Completed {message_count} turns. Tools: {', '.join(tool_calls[-5:]) or 'none'}"
        checkpoint = AgentCheckpoint(
            id=checkpoint_id,
            name=f"checkpoint-{agent_id[-8:]}",
            agent_id=agent_id,
            session_id=session_id,
            conversation_history=[
                {
                    "role": "user",
                    "content": prompt,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "type": "text",
                },
                {
                    "role": "system",
                    "content": summary,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "type": "text",
                },
            ],
            current_step=last_content[:200] if last_content else None,
        )
        await manager.create_direct(checkpoint)

        # Update agent status to completed
        await manager.update(
            agent_id,
            {
                "status": AgentStatus.COMPLETED.value,
                "conversation_turns": message_count,
            },
        )

        result = {
            "agent_id": agent_id,
            "status": "completed",
            "turns": message_count,
            "tools_used": len(tool_calls),
        }

        # Broadcast completion via WebSocket
        await _safe_broadcast(
            "agent_status",
            {"agent_id": agent_id, "status": "completed", "turns": message_count},
            org_id=org_id,
        )

        log.info("run_agent_execution_completed", **result)
        return result

    except Exception as e:
        log.exception("run_agent_execution_failed", agent_id=agent_id, error=str(e))

        # Update agent status to failed
        try:
            client = await get_graph_client()
            manager = EntityManager(client, group_id=org_id)
            await manager.update(
                agent_id,
                {
                    "status": AgentStatus.FAILED.value,
                    "error_message": str(e),
                },
            )
        except Exception:
            log.warning("Failed to update agent status on error", agent_id=agent_id)

        # Broadcast failure via WebSocket
        await _safe_broadcast(
            "agent_status",
            {"agent_id": agent_id, "status": "failed", "error": str(e)},
            org_id=org_id,
        )

        raise


# Optional: Scheduled job to sync all sources
async def sync_all_sources(ctx: dict[str, Any]) -> dict[str, Any]:
    """Sync all sources - can be run as a cron job."""
    from sibyl.crawler.service import list_sources

    sources = await list_sources()
    synced = 0

    for source in sources:
        try:
            await sync_source(ctx, str(source.id))
            synced += 1
        except Exception as e:
            log.warning("Failed to sync source", source_id=str(source.id), error=str(e))

    return {"synced": synced, "total": len(sources)}


# Worker configuration
class WorkerSettings:
    """arq worker settings."""

    redis_settings = get_redis_settings()

    # Job functions
    functions = [
        crawl_source,
        sync_source,
        sync_all_sources,
        create_entity,
        create_learning_episode,
        update_entity,
        run_agent_execution,
    ]

    # Lifecycle hooks
    on_startup = startup
    on_shutdown = shutdown

    # Worker settings
    max_jobs = 3  # Max concurrent jobs
    job_timeout = 3600  # 1 hour timeout for crawl jobs
    keep_result = 86400  # Keep results for 24 hours
    poll_delay = 0.5  # Check for jobs every 0.5s


async def run_worker_async() -> None:
    """Run the arq worker in-process.

    This allows running the worker as part of the main server process
    instead of as a separate process. Useful for development and
    simpler deployments.
    """
    from arq import Worker

    settings = WorkerSettings.redis_settings
    log.info(
        "Starting in-process job worker",
        redis_host=settings.host,
        redis_port=settings.port,
        redis_db=settings.database,
        max_jobs=WorkerSettings.max_jobs,
    )

    try:
        worker = Worker(
            functions=WorkerSettings.functions,
            redis_settings=settings,
            on_startup=WorkerSettings.on_startup,
            on_shutdown=WorkerSettings.on_shutdown,
            max_jobs=WorkerSettings.max_jobs,
            job_timeout=WorkerSettings.job_timeout,
            keep_result=WorkerSettings.keep_result,
            poll_delay=WorkerSettings.poll_delay,
        )

        await worker.async_run()
    except Exception:
        log.exception("Job worker crashed")
        raise
