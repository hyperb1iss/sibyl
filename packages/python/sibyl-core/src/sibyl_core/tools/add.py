"""Add tool for creating new knowledge in the Sibyl graph."""

from datetime import UTC, datetime
from typing import Any

import structlog

from sibyl_core.graph.client import get_graph_client
from sibyl_core.graph.entities import EntityManager
from sibyl_core.graph.relationships import RelationshipManager
from sibyl_core.models.entities import EntityType, Episode, Pattern, Relationship, RelationshipType
from sibyl_core.models.tasks import (
    Epic,
    EpicStatus,
    Project,
    ProjectStatus,
    Task,
    TaskPriority,
    TaskStatus,
)
from sibyl_core.tools.conflicts import detect_conflicts
from sibyl_core.tools.helpers import (
    MAX_CONTENT_LENGTH,
    MAX_TITLE_LENGTH,
    _auto_discover_links,
    _generate_id,
    auto_tag_task,
    get_project_tags,
)
from sibyl_core.tools.responses import AddResponse, ConflictWarning

log = structlog.get_logger()

__all__ = ["add"]


async def add(
    title: str,
    content: str,
    entity_type: str = "episode",
    category: str | None = None,
    languages: list[str] | None = None,
    tags: list[str] | None = None,
    related_to: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    # Task/Epic-specific parameters
    project: str | None = None,
    epic: str | None = None,
    priority: str | None = None,
    assignees: list[str] | None = None,
    due_date: str | None = None,
    technologies: list[str] | None = None,
    depends_on: list[str] | None = None,
    # Project-specific parameters
    repository_url: str | None = None,
    # Sync mode - wait for Graphiti processing instead of returning immediately
    sync: bool = False,
    # Conflict detection - check for contradicting/duplicate knowledge
    check_conflicts: bool = True,
    conflict_threshold: float = 0.70,
) -> AddResponse:
    """Add new knowledge to the Sibyl knowledge graph.

    Use this tool to create entities with automatic relationship discovery.
    Supports episodes (learnings), patterns, tasks, epics, and projects.

    ENTITY TYPES:
    • episode: Temporal knowledge snapshot (default) - insights, learnings, discoveries
    • pattern: Coding pattern or best practice
    • task: Work item with workflow state machine (REQUIRES project)
    • epic: Feature initiative grouping related tasks (REQUIRES project)
    • project: Container for epics and tasks

    USE CASES:
    • Record a learning: add("Redis pooling insight", "Discovered that...", category="debugging")
    • Create a pattern: add("Error handling pattern", "...", entity_type="pattern", languages=["python"])
    • Create an epic: add("OAuth Integration", "...", entity_type="epic", project="proj_abc", priority="high")
    • Create a task: add("Implement OAuth", "...", entity_type="task", project="proj_abc", epic="epic_xyz")
    • Create a project: add("Auth System", "...", entity_type="project", repository_url="...")

    IMPORTANT: Tasks and Epics REQUIRE a project. Always specify project="<project_id>".
    Tasks can optionally belong to an epic via epic="<epic_id>".
    Use explore(mode="list", types=["project"]) to find available projects first.

    Args:
        title: Short title (max 200 chars).
        content: Full content/description (max 50k chars).
        entity_type: Type to create - episode (default), pattern, task, epic, project.
        category: Domain category (authentication, database, api, debugging, etc.).
        languages: Programming languages (python, typescript, rust, etc.).
        tags: Searchable tags for discovery.
        related_to: Entity IDs to explicitly link (creates RELATED_TO edges).
        metadata: Additional structured data.
        project: Project ID (REQUIRED for tasks and epics, creates BELONGS_TO edge).
        epic: Epic ID for tasks (optional, creates BELONGS_TO edge).
        priority: Task/epic priority - critical, high, medium (default), low, someday.
        assignees: List of assignee names for tasks/epics.
        due_date: Due date for tasks (ISO format: 2024-03-15).
        technologies: Technologies involved (for tasks).
        depends_on: Task IDs this depends on (creates DEPENDS_ON edges).
        repository_url: Repository URL for projects.
        sync: If True, wait for Graphiti processing (slower but entity exists immediately).
              If False (default), return immediately and process in background.
        check_conflicts: If True (default), check for semantically similar existing entities
              that may contradict or duplicate this knowledge. Warnings returned in response.
        conflict_threshold: Minimum similarity score (0.0-1.0) to flag as potential conflict.
              Default 0.70. Higher = fewer false positives, lower = catch more conflicts.

    Returns:
        AddResponse with created entity ID, auto-discovered links, conflicts, and timestamp.

    EXAMPLES:
        add("OAuth redirect bug", "Fixed issue where...", category="debugging", languages=["python"])
        add("Add user auth", "Implement login flow", entity_type="task", project="proj_web", priority="high")
        add("E-commerce API", "Backend services for...", entity_type="project", repository_url="github.com/...")
        add("Connection pooling pattern", "Best practice for...", entity_type="pattern")
    """
    # Sanitize inputs
    title = title.strip()
    content = content.strip()

    # Validate
    if not title:
        return AddResponse(
            success=False,
            id=None,
            message="Title cannot be empty",
            timestamp=datetime.now(UTC),
        )

    if len(title) > MAX_TITLE_LENGTH:
        return AddResponse(
            success=False,
            id=None,
            message=f"Title exceeds {MAX_TITLE_LENGTH} characters",
            timestamp=datetime.now(UTC),
        )

    if not content:
        return AddResponse(
            success=False,
            id=None,
            message="Content cannot be empty",
            timestamp=datetime.now(UTC),
        )

    if len(content) > MAX_CONTENT_LENGTH:
        return AddResponse(
            success=False,
            id=None,
            message=f"Content exceeds {MAX_CONTENT_LENGTH} characters",
            timestamp=datetime.now(UTC),
        )

    log.info(
        "add",
        title=title[:50],
        entity_type=entity_type,
        category=category,
        languages=languages,
    )

    try:
        client = await get_graph_client()
        org_id = (metadata or {}).get("organization_id") or (metadata or {}).get("group_id")
        if not org_id:
            raise ValueError(
                "organization_id is required in metadata - cannot create entity without org context"
            )
        org_id = str(org_id)
        entity_manager = EntityManager(client, group_id=org_id)

        # Generate deterministic ID
        entity_id = _generate_id(entity_type, title, category or "general")

        # Detect potential conflicts (duplicates, contradictions) before creating
        conflicts: list[ConflictWarning] = []
        if check_conflicts and entity_type in ("episode", "pattern", "rule", "template"):
            # Only check for knowledge types, not workflow items (tasks/projects/epics)
            try:
                conflicts = await detect_conflicts(
                    title=title,
                    content=content,
                    organization_id=org_id,
                    entity_types=[entity_type] if entity_type else None,
                    exclude_id=entity_id,  # Exclude self for updates
                    max_conflicts=3,
                    min_similarity=conflict_threshold,
                )
                if conflicts:
                    log.info(
                        "conflicts_detected",
                        entity_id=entity_id,
                        count=len(conflicts),
                        types=[c.conflict_type for c in conflicts],
                    )
            except Exception as conflict_err:
                # Don't fail creation if conflict detection fails
                log.warning("conflict_detection_failed", error=str(conflict_err))

        # Merge metadata
        full_metadata = {
            "category": category,
            "languages": languages or [],
            "tags": tags or [],
            "added_at": datetime.now(UTC).isoformat(),
            "organization_id": org_id,
            **(metadata or {}),
        }

        # Create appropriate entity type
        entity: Episode | Pattern | Task | Project
        relationship_manager = RelationshipManager(client, group_id=org_id)

        if entity_type == "task":
            # Validate project_id is provided for tasks
            if not project:
                return AddResponse(
                    success=False,
                    id=None,
                    message="Tasks require a project. Use explore(types=['project']) to find projects.",
                    timestamp=datetime.now(UTC),
                )

            # Parse due date if provided
            parsed_due_date = None
            if due_date:
                try:
                    parsed_due_date = datetime.fromisoformat(due_date)
                except ValueError:
                    log.warning("invalid_due_date", due_date=due_date)

            # Parse priority
            task_priority = TaskPriority.MEDIUM
            if priority:
                try:
                    task_priority = TaskPriority(priority.lower())
                except ValueError:
                    log.warning("invalid_priority", priority=priority)

            # Get existing project tags for consistency (when project-scoped)
            project_tags = await get_project_tags(client, project) if project else []

            # Auto-generate tags based on task content + project context
            task_technologies = technologies or languages or []
            auto_tags = auto_tag_task(
                title=title,
                description=content,
                technologies=task_technologies,
                domain=category,
                explicit_tags=tags,
                project_tags=project_tags,
            )
            full_metadata["tags"] = auto_tags

            log.debug(
                "auto_tags_generated",
                tags=auto_tags,
                count=len(auto_tags),
                project_tags_used=len(project_tags),
            )

            entity = Task(  # type: ignore[call-arg]  # model_validator sets name from title
                id=entity_id,
                title=title,
                description=content,
                status=TaskStatus.TODO,
                priority=task_priority,
                project_id=project or None,
                epic_id=epic or None,
                assignees=assignees or [],
                due_date=parsed_due_date,
                technologies=task_technologies,
                domain=category,
                tags=auto_tags,
                metadata=full_metadata,
            )

        elif entity_type == "project":
            entity = Project(  # type: ignore[call-arg]  # model_validator sets name from title
                id=entity_id,
                title=title,
                description=content,
                status=ProjectStatus.ACTIVE,
                repository_url=repository_url,
                tech_stack=technologies or languages or [],
                tags=tags or [],
                metadata=full_metadata,
            )

        elif entity_type == "epic":
            # Validate project_id is provided for epics
            if not project:
                return AddResponse(
                    success=False,
                    id=None,
                    message="Epics require a project. Use explore(types=['project']) to find projects.",
                    timestamp=datetime.now(UTC),
                )

            # Parse priority
            epic_priority = TaskPriority.MEDIUM
            if priority:
                try:
                    epic_priority = TaskPriority(priority.lower())
                except ValueError:
                    log.warning("invalid_priority", priority=priority)

            # Parse target date if provided
            parsed_target_date = None
            if due_date:
                try:
                    parsed_target_date = datetime.fromisoformat(due_date)
                except ValueError:
                    log.warning("invalid_target_date", due_date=due_date)

            entity = Epic(  # type: ignore[call-arg]  # model_validator sets name from title
                id=entity_id,
                title=title,
                description=content,
                status=EpicStatus.PLANNING,
                priority=epic_priority,
                project_id=project,
                assignees=assignees or [],
                target_date=parsed_target_date,
                tags=tags or [],
                metadata=full_metadata,
            )

        elif entity_type == "pattern":
            entity = Pattern(
                id=entity_id,
                entity_type=EntityType.PATTERN,
                name=title,
                description=content[:500] if len(content) > 500 else content,
                content=content,
                category=category or "",
                languages=languages or [],
                metadata=full_metadata,
            )

        else:
            # Default to Episode for temporal knowledge
            entity = Episode(
                id=entity_id,
                entity_type=EntityType.EPISODE,
                name=title,
                description=content[:500] if len(content) > 500 else content,
                content=content,
                metadata=full_metadata,
            )

        # Build list of explicit relationships to create
        relationships_to_create: list[dict[str, Any]] = []

        # Task -> Project (BELONGS_TO)
        if entity_type == "task" and project:
            relationships_to_create.append(
                {
                    "id": f"rel_{entity_id}_belongs_to_{project}",
                    "source_id": entity_id,
                    "target_id": project,
                    "type": "BELONGS_TO",
                    "metadata": {"created_at": datetime.now(UTC).isoformat()},
                }
            )

        # Task -> Epic (BELONGS_TO)
        if entity_type == "task" and epic:
            relationships_to_create.append(
                {
                    "id": f"rel_{entity_id}_belongs_to_{epic}",
                    "source_id": entity_id,
                    "target_id": epic,
                    "type": "BELONGS_TO",
                    "metadata": {"created_at": datetime.now(UTC).isoformat()},
                }
            )

        # Epic -> Project (BELONGS_TO)
        if entity_type == "epic" and project:
            relationships_to_create.append(
                {
                    "id": f"rel_{entity_id}_belongs_to_{project}",
                    "source_id": entity_id,
                    "target_id": project,
                    "type": "BELONGS_TO",
                    "metadata": {"created_at": datetime.now(UTC).isoformat()},
                }
            )

        # Task -> Task (DEPENDS_ON)
        if entity_type == "task" and depends_on:
            relationships_to_create.extend(
                [
                    {
                        "id": f"rel_{entity_id}_depends_on_{dep_id}",
                        "source_id": entity_id,
                        "target_id": dep_id,
                        "type": "DEPENDS_ON",
                        "metadata": {"created_at": datetime.now(UTC).isoformat()},
                    }
                    for dep_id in depends_on
                ]
            )

        # Generic RELATED_TO relationships
        if related_to:
            relationships_to_create.extend(
                [
                    {
                        "id": f"rel_{entity_id}_related_to_{related_id}",
                        "source_id": entity_id,
                        "target_id": related_id,
                        "type": "RELATED_TO",
                        "metadata": {"created_at": datetime.now(UTC).isoformat()},
                    }
                    for related_id in related_to
                ]
            )

        # Sync mode: create entity + relationships immediately via Graphiti
        if sync:
            # Use create_direct() for structured entities (faster, generates embeddings)
            # Use create() for episodes (LLM extraction may add value)
            if entity_type in ("task", "project", "epic", "pattern"):
                created_id = await entity_manager.create_direct(entity)
            else:
                created_id = await entity_manager.create(entity)

            # Create explicit relationships
            for rel_data in relationships_to_create:
                try:
                    rel = Relationship(
                        id=rel_data["id"],
                        source_id=rel_data["source_id"],
                        target_id=rel_data["target_id"],
                        relationship_type=RelationshipType(rel_data["type"]),
                        metadata=rel_data.get("metadata", {}),
                    )
                    await relationship_manager.create(rel)
                except Exception as e:
                    log.warning("relationship_creation_failed", error=str(e), rel=rel_data)

            # Auto-link to related patterns/rules/templates in sync mode
            try:
                auto_link_results = await _auto_discover_links(
                    entity_manager=entity_manager,
                    title=title,
                    content=content,
                    technologies=technologies or languages or [],
                    category=category,
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
                                "created_at": datetime.now(UTC).isoformat(),
                                "auto_linked": True,
                                "similarity_score": score,
                            },
                        )
                        await relationship_manager.create(rel)
                    except Exception as e:
                        log.warning("auto_link_failed", error=str(e), target=linked_id)
            except Exception as e:
                log.warning("auto_link_search_failed", error=str(e))

            message = f"Added: {title}"
            if relationships_to_create:
                message += f" (linked: {len(relationships_to_create)})"
            if conflicts:
                message += f" (⚠️ {len(conflicts)} potential conflict(s) detected)"

            return AddResponse(
                success=True,
                id=created_id,
                message=message,
                timestamp=datetime.now(UTC),
                conflicts=conflicts,
            )

        # Async mode (default): queue arq job, return immediately
        try:
            from sibyl.jobs.queue import enqueue_create_entity

            await enqueue_create_entity(
                entity_id=entity_id,
                entity_data=entity.model_dump(mode="json"),
                entity_type=entity_type,
                group_id=org_id,
                relationships=relationships_to_create if relationships_to_create else None,
                auto_link_params={
                    "title": title,
                    "content": content,
                    "technologies": technologies or languages or [],
                    "category": category,
                },
            )
            log.info("add_queued_for_arq", entity_id=entity_id, entity_type=entity_type)

        except Exception as e:
            # If arq queue fails, fall back to sync creation
            log.warning("arq_queue_failed_falling_back_to_sync", error=str(e))
            # Use create_direct() for structured entities (faster, generates embeddings)
            if entity_type in ("task", "project", "epic", "pattern"):
                created_id = await entity_manager.create_direct(entity)
            else:
                created_id = await entity_manager.create(entity)

            for rel_data in relationships_to_create:
                try:
                    rel = Relationship(
                        id=rel_data["id"],
                        source_id=rel_data["source_id"],
                        target_id=rel_data["target_id"],
                        relationship_type=RelationshipType(rel_data["type"]),
                        metadata=rel_data.get("metadata", {}),
                    )
                    await relationship_manager.create(rel)
                except Exception as rel_e:
                    log.warning("relationship_creation_failed", error=str(rel_e))

            fallback_message = f"Added (sync fallback): {title}"
            if conflicts:
                fallback_message += f" (⚠️ {len(conflicts)} potential conflict(s) detected)"
            return AddResponse(
                success=True,
                id=created_id,
                message=fallback_message,
                timestamp=datetime.now(UTC),
                conflicts=conflicts,
            )

        # Return immediately with the entity ID - entity will be created in background
        queued_message = f"Queued: {title} (processing in background)"
        if conflicts:
            queued_message += f" (⚠️ {len(conflicts)} potential conflict(s) detected)"
        return AddResponse(
            success=True,
            id=entity_id,
            message=queued_message,
            timestamp=datetime.now(UTC),
            conflicts=conflicts,
        )

    except Exception as e:
        log.warning("add_failed", error=str(e))
        return AddResponse(
            success=False,
            id=None,
            message=f"Failed: {e}",
            timestamp=datetime.now(UTC),
        )
