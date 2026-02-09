"""Entity creation and update jobs.

These jobs handle async entity operations via Graphiti, allowing
the API to return quickly while background processing continues.
"""

from typing import Any

import structlog

from sibyl.api.event_types import WSEvent

log = structlog.get_logger()


async def _safe_broadcast(event: str, data: dict[str, Any], *, org_id: str | None) -> None:
    """Broadcast event via Redis pub/sub (worker runs in separate process)."""
    try:
        from sibyl.api.pubsub import publish_event

        await publish_event(event, data, org_id=org_id)
    except Exception:
        log.debug("Broadcast failed (Redis unavailable)", event=event)


async def create_entity(  # noqa: PLR0915
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
    from sibyl_core.models.tasks import Epic, Project, Task

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
        elif entity_type == "epic":
            entity = Epic.model_validate(entity_data)
        elif entity_type == "pattern":
            entity = Pattern.model_validate(entity_data)
        else:
            entity = Episode.model_validate(entity_data)

        # Use create_direct() for structured entities (faster, generates embeddings)
        # Use create() for episodes (LLM extraction may add value)
        if entity_type in ("task", "project", "epic", "pattern"):
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
                rel_id = (
                    rel_data.get("id")
                    or f"rel_{rel_data.get('source_id')}_{rel_data.get('target_id')}"
                )
                source_id = rel_data.get("source_id") or ""
                target_id = rel_data.get("target_id") or ""
                if not source_id or not target_id:
                    log.warning(
                        "Skipping relationship with missing source/target", rel_data=rel_data
                    )
                    continue
                rel = Relationship(
                    id=rel_id,
                    source_id=source_id,
                    target_id=target_id,
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

        # Clear pending status and process any queued operations
        from sibyl.jobs.pending import clear_pending, process_pending_operations

        await clear_pending(created_id)
        pending_results = await process_pending_operations(created_id, group_id)

        result = {
            "entity_id": created_id,
            "entity_type": entity_type,
            "relationships_created": relationships_created,
            "auto_links_created": auto_links_created,
            "pending_ops_processed": len(pending_results),
        }

        # Broadcast entity creation event
        await _safe_broadcast(
            WSEvent.ENTITY_CREATED,
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
            WSEvent.ENTITY_CREATED,
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


async def update_task(
    ctx: dict[str, Any],  # noqa: ARG001
    task_id: str,
    updates: dict[str, Any],
    group_id: str,
    epic_id: str | None = None,
    new_status: str | None = None,
) -> dict[str, Any]:
    """Update a task asynchronously with epic relationship and auto-start logic.

    Task-aware background job that handles concerns the generic update_entity
    doesn't: BELONGS_TO epic relationships and epic auto-start on forward progress.

    Args:
        ctx: arq context
        task_id: The task entity ID to update
        updates: Dict of field names to new values
        group_id: Organization ID
        epic_id: Epic ID if being set/changed (triggers BELONGS_TO creation)
        new_status: New task status (triggers epic auto-start check)

    Returns:
        Dict with update results
    """
    from sibyl.locks import entity_lock
    from sibyl_core.graph.client import get_graph_client
    from sibyl_core.graph.entities import EntityManager
    from sibyl_core.graph.relationships import RelationshipManager
    from sibyl_core.models.entities import Relationship, RelationshipType

    log.info(
        "update_task_started",
        task_id=task_id,
        fields=list(updates.keys()),
        epic_id=epic_id,
    )

    try:
        async with entity_lock(group_id, task_id, blocking=True) as lock_token:
            if not lock_token:
                log.warning("update_task_lock_failed", task_id=task_id)
                return {"task_id": task_id, "success": False, "message": "Lock contention"}

            client = await get_graph_client()
            entity_manager = EntityManager(client, group_id=group_id)

            # Perform the update
            updated = await entity_manager.update(task_id, updates)
            if not updated:
                log.warning("update_task_no_changes", task_id=task_id)
                return {"task_id": task_id, "success": False, "message": "No changes made"}

            # Create BELONGS_TO relationship for epic (if epic_id was set/changed)
            if epic_id is not None:
                relationship_manager = RelationshipManager(client, group_id=group_id)
                belongs_to_epic = Relationship(
                    id=f"rel_{task_id}_belongs_to_{epic_id}",
                    source_id=task_id,
                    target_id=epic_id,
                    relationship_type=RelationshipType.BELONGS_TO,
                )
                await relationship_manager.create(belongs_to_epic)

            # Auto-start epic if task moves to forward-progress state
            if new_status:
                resolved_epic = epic_id or updated.metadata.get("epic_id")
                if resolved_epic:
                    await _maybe_start_epic_bg(entity_manager, task_id, resolved_epic, new_status)

        # Broadcast outside the lock
        await _safe_broadcast(
            WSEvent.ENTITY_UPDATED,
            {
                "id": task_id,
                "entity_type": "task",
                "action": "update_task",
                "name": updated.name,
                **updates,
            },
            org_id=group_id,
        )

        log.info("update_task_completed", task_id=task_id, fields=list(updates.keys()))
        return {
            "task_id": task_id,
            "updated_fields": list(updates.keys()),
            "success": True,
        }

    except Exception as e:
        log.exception("update_task_failed", task_id=task_id, error=str(e))
        raise


async def _maybe_start_epic_bg(
    entity_manager: Any,
    task_id: str,
    epic_id: str,
    task_status: str,
) -> bool:
    """Auto-start epic if task moves to forward-progress state (background job variant).

    Same logic as tasks.py:_maybe_start_epic but lives here so the background
    job doesn't import from the route module.
    """
    from datetime import UTC, datetime

    from sibyl_core.models.tasks import EpicStatus

    forward_progress_states = {"doing", "review", "blocked"}
    if task_status not in forward_progress_states:
        return False

    epic = await entity_manager.get(epic_id)
    if not epic or epic.metadata.get("status") != "planning":
        return False

    await entity_manager.update(
        epic_id,
        {"status": EpicStatus.IN_PROGRESS, "started_at": datetime.now(UTC)},
    )
    log.info("Epic auto-started (bg)", epic_id=epic_id, task_id=task_id, task_status=task_status)
    return True


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
                WSEvent.ENTITY_UPDATED,
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
