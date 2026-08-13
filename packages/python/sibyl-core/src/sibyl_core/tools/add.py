"""Add tool for creating new knowledge in the Sibyl graph."""

import inspect
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import structlog

from sibyl_core.auth.memory_policy import (
    stamp_memory_scope_metadata,
)
from sibyl_core.embeddings.providers import configured_embedding_provider
from sibyl_core.memory_pipeline.retrieval_keys import (
    coerce_retrieval_keys,
    normalize_retrieval_keys,
)
from sibyl_core.memory_pipeline.structure import (
    PROBE_REHEARSAL_METADATA_KEY,
    MemoryStructure,
    build_memory_structure,
    strip_structure_metadata,
    structure_metadata,
)
from sibyl_core.models.entities import (
    Entity,
    EntityType,
    Episode,
    Pattern,
    Procedure,
    Relationship,
    RelationshipType,
)
from sibyl_core.models.relations import parse_relation_declarations
from sibyl_core.models.tasks import (
    Epic,
    EpicStatus,
    Project,
    ProjectStatus,
    Task,
    TaskPriority,
    TaskStatus,
)
from sibyl_core.projection import project_entity_passages, project_memory_entity
from sibyl_core.runtime_ports import get_queue_port
from sibyl_core.services.graph import get_surreal_graph_runtime
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


async def get_graph_runtime(group_id: str):
    return await get_surreal_graph_runtime(
        group_id,
        embedding_provider=configured_embedding_provider(),
    )


async def _rehearse_write(
    *,
    entity_manager: Any,
    entity: Entity,
    created_id: str,
    org_id: str,
    structure: MemoryStructure,
    passage_ids: list[str],
    principal_id: str | None,
    memory_scope: str | None,
    scope_key: str | None,
    project: str | None,
) -> dict[str, Any] | None:
    """Run the write's probes and record the receipt on the row it describes.

    Everything here is best effort. The memory is durable before this runs, so a
    rehearsal that cannot complete costs the receipt and nothing else, and a
    write that failed on a retrieval hiccup would be the worse outcome by far.
    """
    if not structure.probes:
        return None

    from sibyl_core.memory_pipeline.rehearsal import rehearse_memory_probes

    try:
        receipt = await rehearse_memory_probes(
            probes=structure.probes,
            organization_id=org_id,
            entity_id=created_id,
            passage_ids=passage_ids,
            principal_id=principal_id,
            memory_scope=memory_scope or "private",
            scope_key=scope_key,
            project=project,
            accessible_projects={scope_key} if scope_key else None,
        )
    except Exception as exc:
        log.warning(
            "probe_rehearsal_failed",
            entity_id=created_id,
            error_type=type(exc).__name__,
        )
        return None

    try:
        await entity_manager.update(
            created_id,
            {"metadata": {**(entity.metadata or {}), PROBE_REHEARSAL_METADATA_KEY: receipt}},
        )
    except Exception as exc:
        log.warning(
            "probe_rehearsal_receipt_not_stored",
            entity_id=created_id,
            error_type=type(exc).__name__,
        )
    return receipt


def _build_relationship(rel_data: dict[str, Any]) -> Relationship:
    return Relationship(
        id=rel_data["id"],
        source_id=rel_data["source_id"],
        target_id=rel_data["target_id"],
        relationship_type=RelationshipType(rel_data["type"]),
        metadata=rel_data.get("metadata", {}),
    )


def _declared_relationship_payloads(
    entity_id: str,
    related_to: Sequence[str] | None,
) -> list[dict[str, Any]]:
    """Turn a `related_to` list into edge payloads, honoring declared predicates.

    An undeclared entry keeps the exact id and RELATED_TO type it has always
    had. A declared one carries its predicate into the relationship id too, so
    an agent can both supersede and reference the same target without the two
    edges colliding on a deterministic id.
    """
    payloads: list[dict[str, Any]] = []
    for declaration in parse_relation_declarations(related_to):
        metadata: dict[str, Any] = {"created_at": datetime.now(UTC).isoformat()}
        if declaration.declared:
            metadata["agent_declared"] = True
        payloads.append(
            {
                "id": f"rel_{entity_id}_{declaration.edge_slug}_{declaration.target_id}",
                "source_id": entity_id,
                "target_id": declaration.target_id,
                "type": declaration.relationship_type.value,
                "metadata": metadata,
            }
        )
    return payloads


def _accepts_keyword(function: Any, keyword: str) -> bool:
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return True
    return keyword in signature.parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


async def _create_entity_record(
    entity_manager: Any,
    entity: Entity,
    *,
    generate_embeddings: bool,
) -> str:
    create_direct = getattr(entity_manager, "create_direct", None)
    if inspect.iscoroutinefunction(create_direct):
        if _accepts_keyword(create_direct, "generate_embedding"):
            return await create_direct(entity, generate_embedding=generate_embeddings)
        return await create_direct(entity)
    return await entity_manager.create(entity)


async def _create_relationships_bulk(
    relationship_manager: Any,
    relationships_to_create: list[dict[str, Any]],
    log_event: str,
    *,
    generate_embeddings: bool = True,
) -> tuple[int, int]:
    if not relationships_to_create:
        return 0, 0

    relationships = [_build_relationship(rel_data) for rel_data in relationships_to_create]
    create_direct_bulk = getattr(relationship_manager, "create_direct_bulk", None)
    if inspect.iscoroutinefunction(create_direct_bulk):
        created_ids = await create_direct_bulk(
            relationships,
            generate_embeddings=generate_embeddings,
        )
        created = len(created_ids)
        failed = len(relationships) - created
    else:
        created, failed = await relationship_manager.create_bulk(relationships)

    if failed:
        log.warning(log_event, created=created, failed=failed)
    return created, failed


async def _enqueue_embedding_backfill(
    entities: list[Entity],
    group_id: str,
    relationships_to_create: list[dict[str, Any]],
) -> dict[str, Any]:
    try:
        job_id = await get_queue_port().enqueue_entity_embedding_backfill(
            entities_data=[entity.model_dump(mode="json") for entity in entities],
            group_id=group_id,
            relationships=relationships_to_create or None,
        )
    except Exception as exc:
        log.warning(
            "embedding_backfill_enqueue_failed",
            entity_ids=[entity.id for entity in entities],
            error=str(exc),
        )
        return {
            "embedding_backfill": {
                "status": "failed",
                "queued_entities": 0,
                "queued_relationships": 0,
                "error": str(exc),
            }
        }
    return {
        "embedding_backfill": {
            "status": "queued",
            "job_ids": [job_id],
            "queued_entities": len(entities),
            "queued_relationships": len(relationships_to_create),
        }
    }


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
    # Sync mode - wait for Surreal graph writes instead of returning immediately
    sync: bool = False,
    generate_embeddings: bool = True,
    # Conflict detection - check for contradicting/duplicate knowledge
    check_conflicts: bool = True,
    skip_conflicts: bool = False,
    conflict_threshold: float = 0.85,
    # Authorized scope of the write, resolved by the calling surface
    memory_scope: str | None = None,
    scope_key: str | None = None,
    principal_id: str | None = None,
    retrieval_keys: list[str] | None = None,
    # Structure the writing agent declares for its own memory
    spans: list[dict[str, Any]] | None = None,
    atomic: bool = False,
    probes: list[str] | None = None,
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
        related_to: Entity IDs to explicitly link. A bare ID creates an untyped
              RELATED_TO edge. Prefixing an ID with a predicate from the closed
              vocabulary declares what this memory does to that one, and
              retrieval weights the declared predicate when it walks the graph:
              "supersedes:<id>" (this replaces that), "contradicts:<id>",
              "requires:<id>", "supports:<id>" (this is evidence for that),
              "decides:<id>" (this settles that). The memory being written is
              always the subject. Any other prefix is read as part of the ID
              and links untyped, exactly as before.
        metadata: Additional structured data.
        project: Project ID (REQUIRED for tasks and epics, creates BELONGS_TO edge).
        epic: Epic ID for tasks (optional, creates BELONGS_TO edge).
        priority: Task/epic priority - critical, high, medium (default), low, someday.
        assignees: List of assignee names for tasks/epics.
        due_date: Due date for tasks (ISO format: 2024-03-15).
        technologies: Technologies involved (for tasks).
        depends_on: Task IDs this depends on (creates DEPENDS_ON edges).
        repository_url: Repository URL for projects.
        sync: If True, wait for Surreal graph writes so the entity exists immediately.
              If False (default), return immediately and process in background.
        generate_embeddings: If True, generate native graph embeddings during the write.
              If False, persist lexical records first and queue embedding backfill.
        check_conflicts: If True (default), check for semantically similar existing entities
              that may contradict or duplicate this knowledge. Warnings returned in response.
        skip_conflicts: If True, skip conflict detection even when check_conflicts is True.
        conflict_threshold: Minimum similarity score (0.0-1.0) to flag as potential conflict.
              Default 0.85. Higher = fewer false positives, lower = catch more conflicts.
        memory_scope: Authorized audience for the row, resolved by the calling surface.
        scope_key: Authorized audience key (a verified project, never a payload value).
        principal_id: Authenticated author of the write.
        retrieval_keys: Exact-match identifiers this entity answers to (error strings,
              symbols, config flags, aliases). Matched case-insensitively against
              identifier-shaped queries, so a key may name something the content
              never spells out. Max 16 keys, 200 characters each.
        spans: Agent-authored cut plan as [{"start": int, "end": int, "label": str?}].
              Offsets are half-open into the stored (stripped) content and must tile
              it exactly: no gap, no overlap, first starts at 0, last ends at the
              final character. Honored verbatim in place of the mechanical prose
              cutter; an invalid plan raises rather than falling back.
        atomic: Declare the body one retrievable unit that must not be cut.
              Mutually exclusive with spans.
        probes: Questions this memory must answer later. Each is run through the
              live search path once the write lands, and the ranks come back in
              the response. Supplying probes forces a synchronous write, since a
              rehearsal cannot observe a row that has not been written yet.


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

    # The structure keys are server-owned. A surface that forwards a request body
    # cannot plant a span plan nothing validated, nor hand itself a rehearsal
    # receipt no search produced, for the same reason it cannot name the
    # principal its row reads as.
    if metadata:
        metadata = strip_structure_metadata(metadata)
    structure = build_memory_structure(content, spans=spans, atomic=atomic, probes=probes)
    if structure.probes:
        # A rehearsal has to observe the row it asks about, and the async path
        # returns before the write lands. Probes are opt-in per write, so this
        # buys the receipt only for callers that asked for one.
        sync = True

    if (metadata or {}).get("memory_scope") is not None and memory_scope is None:
        # The stamp rebuilds the triple from the authorized values, so a scope
        # the caller does not restate is dropped — turning a private row into
        # an unscoped one, which reads as org-visible. Refuse instead: an
        # authorized principal alone does not make a declared scope safe to
        # discard, so this fires on the scope rather than on the triple.
        raise ValueError(
            "add() received metadata declaring memory_scope="
            f"{(metadata or {}).get('memory_scope')!r} without the authorized "
            "memory_scope that names its audience"
        )

    log.info(
        "add",
        title=title[:50],
        entity_type=entity_type,
        category=category,
        languages=languages,
    )

    try:
        org_id = (metadata or {}).get("organization_id") or (metadata or {}).get("group_id")
        if not org_id:
            raise ValueError(
                "organization_id is required in metadata - cannot create entity without org context"
            )
        org_id = str(org_id)
        runtime = await get_graph_runtime(org_id)
        entity_manager = runtime.entity_manager

        # Generate deterministic ID
        entity_id = _generate_id(entity_type, title, category or "general")

        if skip_conflicts:
            check_conflicts = False

        # Detect potential conflicts (duplicates, contradictions) before creating
        conflicts: list[ConflictWarning] = []
        if check_conflicts and entity_type in (
            "episode",
            "pattern",
            "rule",
            "template",
            "procedure",
        ):
            # Only check for knowledge types, not workflow items (tasks/projects/epics)
            try:
                from sibyl_core.tools.conflicts import detect_conflicts

                conflicts = await detect_conflicts(
                    title=title,
                    content=content,
                    organization_id=org_id,
                    entity_types=[entity_type] if entity_type else None,
                    exclude_id=entity_id,  # Exclude self for updates
                    max_conflicts=3,
                    min_similarity=conflict_threshold,
                    # The duplicate check reads existing rows back to whoever
                    # is writing, so it answers to the same reader the write
                    # was authorized as. A project write can see its own
                    # project; nothing here widens beyond that.
                    principal_id=principal_id,
                    accessible_projects={scope_key} if scope_key else set(),
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

        # Merge metadata. Every graph write funnels through here, so the owner
        # channels are rebuilt from the authorized values rather than carried
        # over from the incoming bag — a caller that forwards a request body
        # cannot name the principal or project its row will read as.
        full_metadata = {
            "category": category,
            "languages": languages or [],
            "tags": tags or [],
            "added_at": datetime.now(UTC).isoformat(),
            "organization_id": org_id,
            **stamp_memory_scope_metadata(
                metadata,
                memory_scope=memory_scope,
                scope_key=scope_key,
                principal_id=principal_id,
            ),
            **structure_metadata(structure),
        }
        if project:
            full_metadata["project_id"] = project
        # Every graph write funnels through here, so this is where a declaration
        # arriving as an explicit argument and one arriving inside a forwarded
        # metadata bag become the same normalized list. The explicit argument is
        # validated strictly because a surface resolved it and can be told it was
        # refused; the bag is coerced, because it also carries arbitrary caller
        # junk that must not fail an otherwise valid write.
        if retrieval_keys is not None:
            declared_retrieval_keys, _match_forms = normalize_retrieval_keys(retrieval_keys)
        else:
            coerced_keys = coerce_retrieval_keys(full_metadata.get("retrieval_keys"))
            declared_retrieval_keys = coerced_keys[0] if coerced_keys else []
        if declared_retrieval_keys:
            full_metadata["retrieval_keys"] = declared_retrieval_keys
        else:
            full_metadata.pop("retrieval_keys", None)

        # Create appropriate entity type
        entity: Entity | Episode | Pattern | Procedure | Task | Project
        relationship_manager = runtime.relationship_manager

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
            project_tags = await get_project_tags(runtime, project) if project else []

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

            entity = Task(
                id=entity_id,
                name=title,
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
            entity = Project(
                id=entity_id,
                name=title,
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

            entity = Epic(
                id=entity_id,
                name=title,
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

        elif entity_type == "procedure":
            entity = Procedure(
                id=entity_id,
                entity_type=EntityType.PROCEDURE,
                name=title,
                description=content[:500] if len(content) > 500 else content,
                content=content,
                category=category or "",
                metadata=full_metadata,
            )

        else:
            try:
                generic_entity_type = EntityType(entity_type)
            except ValueError:
                generic_entity_type = EntityType.EPISODE

            entity_cls = Episode if generic_entity_type == EntityType.EPISODE else Entity
            entity = entity_cls(
                id=entity_id,
                entity_type=generic_entity_type,
                name=title,
                description=content[:500] if len(content) > 500 else content,
                content=content,
                metadata=full_metadata,
            )

        # Build list of explicit relationships to create
        relationships_to_create: list[dict[str, Any]] = []

        # Entity -> Project (BELONGS_TO)
        if entity_type != "project" and project:
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

        # Declared relationships. A bare id still collapses to RELATED_TO; a
        # "<predicate>:<id>" entry from the closed vocabulary mints the edge
        # type the graph expansion scorer actually weights.
        relationships_to_create.extend(_declared_relationship_payloads(entity_id, related_to))

        # Sync mode: create entity + relationships immediately via Surreal
        if sync:
            # Use create_direct() for structured entities and create() for episode-compatible managers.
            created_id = await _create_entity_record(
                entity_manager,
                entity,
                generate_embeddings=generate_embeddings,
            )

            await _create_relationships_bulk(
                relationship_manager,
                relationships_to_create,
                "relationship_creation_partial_failure",
                generate_embeddings=generate_embeddings,
            )

            # Auto-link to related patterns/rules/templates in sync mode
            auto_relationships: list[dict[str, Any]] = []
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
                auto_relationships = [
                    {
                        "id": f"rel_{created_id}_references_{linked_id}",
                        "source_id": created_id,
                        "target_id": linked_id,
                        "type": RelationshipType.RELATED_TO.value,
                        "metadata": {
                            "created_at": datetime.now(UTC).isoformat(),
                            "auto_linked": True,
                            "similarity_score": score,
                        },
                    }
                    for linked_id, score in auto_link_results
                ]
                await _create_relationships_bulk(
                    relationship_manager,
                    auto_relationships,
                    "auto_link_partial_failure",
                    generate_embeddings=generate_embeddings,
                )
            except Exception as e:
                log.warning("auto_link_search_failed", error=str(e))

            projection_result = await project_memory_entity(
                entity_manager=entity_manager,
                relationship_manager=relationship_manager,
                source=entity,
                group_id=org_id,
                created_source_id=created_id,
                generate_embeddings=generate_embeddings,
            )
            if projection_result.errors:
                log.warning(
                    "add_projection_failed",
                    entity_id=created_id,
                    extracted=projection_result.extracted,
                    projected_entities=projection_result.projected_entities,
                    relationships=projection_result.relationships,
                    projection_state=projection_result.projection_state,
                    errors=projection_result.errors,
                )
            elif projection_result.extracted:
                log.info(
                    "add_projection_complete",
                    entity_id=created_id,
                    extracted=projection_result.extracted,
                    projected_entities=projection_result.projected_entities,
                    relationships=projection_result.relationships,
                    projection_state=projection_result.projection_state,
                    errors=len(projection_result.errors),
                )

            # Ordered after the parent write on purpose: a PART_OF edge whose
            # target does not exist yet is silently dropped and never heals.
            passage_result = await project_entity_passages(
                entity_manager=entity_manager,
                relationship_manager=relationship_manager,
                source=entity,
                group_id=org_id,
                created_source_id=created_id,
                generate_embeddings=generate_embeddings,
            )
            if passage_result.errors:
                log.warning(
                    "add_passage_projection_failed",
                    entity_id=created_id,
                    passages=passage_result.passages,
                    relationships=passage_result.relationships,
                    errors=passage_result.errors,
                )
            elif passage_result.passages:
                log.info(
                    "add_passage_projection_complete",
                    entity_id=created_id,
                    passages=passage_result.passages,
                    relationships=passage_result.relationships,
                )

            # After the passages exist, so a probe that lands on a span counts.
            rehearsal_receipt = await _rehearse_write(
                entity_manager=entity_manager,
                entity=entity,
                created_id=created_id,
                org_id=org_id,
                structure=structure,
                passage_ids=[passage.id for passage in passage_result.created_passages],
                principal_id=principal_id,
                memory_scope=memory_scope,
                scope_key=scope_key,
                project=project,
            )

            message = f"Added: {title}"
            if relationships_to_create:
                message += f" (linked: {len(relationships_to_create)})"
            if conflicts:
                message += f" (⚠️ {len(conflicts)} potential conflict(s) detected)"
            background_jobs = {}
            if not generate_embeddings:
                projection_entities = list(
                    getattr(projection_result, "created_projected_entities", ())
                )
                projection_relationships = [
                    relationship.model_dump(mode="json")
                    for relationship in getattr(
                        projection_result,
                        "created_projection_relationships",
                        (),
                    )
                ]
                passage_relationships = [
                    relationship.model_dump(mode="json")
                    for relationship in passage_result.created_relationships
                ]
                background_jobs = await _enqueue_embedding_backfill(
                    [entity, *projection_entities, *passage_result.created_passages],
                    org_id,
                    relationships_to_create
                    + auto_relationships
                    + projection_relationships
                    + passage_relationships,
                )

            return AddResponse(
                success=True,
                id=created_id,
                message=message,
                timestamp=datetime.now(UTC),
                conflicts=conflicts,
                background_jobs=background_jobs,
                probe_rehearsal=rehearsal_receipt,
            )

        # Async mode (default): queue arq job, return immediately
        try:
            create_job_id = await get_queue_port().enqueue_create_entity(
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
                generate_embeddings=generate_embeddings,
            )
            log.info("add_queued_for_arq", entity_id=entity_id, entity_type=entity_type)

        except Exception as e:
            # If arq queue fails, fall back to sync creation
            log.warning("arq_queue_failed_falling_back_to_sync", error=str(e))
            # Use create_direct() for structured entities and create() for episode-compatible managers.
            created_id = await _create_entity_record(
                entity_manager,
                entity,
                generate_embeddings=generate_embeddings,
            )

            await _create_relationships_bulk(
                relationship_manager,
                relationships_to_create,
                "relationship_creation_partial_failure",
                generate_embeddings=generate_embeddings,
            )

            projection_result = await project_memory_entity(
                entity_manager=entity_manager,
                relationship_manager=relationship_manager,
                source=entity,
                group_id=org_id,
                created_source_id=created_id,
                generate_embeddings=generate_embeddings,
            )
            if projection_result.errors:
                log.warning(
                    "add_projection_failed",
                    entity_id=created_id,
                    extracted=projection_result.extracted,
                    projected_entities=projection_result.projected_entities,
                    relationships=projection_result.relationships,
                    projection_state=projection_result.projection_state,
                    errors=projection_result.errors,
                )
            elif projection_result.extracted:
                log.info(
                    "add_projection_complete",
                    entity_id=created_id,
                    extracted=projection_result.extracted,
                    projected_entities=projection_result.projected_entities,
                    relationships=projection_result.relationships,
                    projection_state=projection_result.projection_state,
                    errors=len(projection_result.errors),
                )

            # Same ordering as the sync branch: the parent exists, so the edges
            # have something to point at. Without this the fallback stored a
            # memory with no spans beside it, agent-planned or otherwise, and
            # nothing later would notice.
            fallback_passages = await project_entity_passages(
                entity_manager=entity_manager,
                relationship_manager=relationship_manager,
                source=entity,
                group_id=org_id,
                created_source_id=created_id,
                generate_embeddings=generate_embeddings,
            )
            if fallback_passages.errors:
                log.warning(
                    "add_fallback_passage_projection_failed",
                    entity_id=created_id,
                    passages=fallback_passages.passages,
                    errors=fallback_passages.errors,
                )

            fallback_message = f"Added (sync fallback): {title}"
            if conflicts:
                fallback_message += f" (⚠️ {len(conflicts)} potential conflict(s) detected)"
            background_jobs = {}
            if not generate_embeddings:
                projection_entities = list(
                    getattr(projection_result, "created_projected_entities", ())
                )
                projection_relationships = [
                    relationship.model_dump(mode="json")
                    for relationship in getattr(
                        projection_result,
                        "created_projection_relationships",
                        (),
                    )
                ]
                background_jobs = await _enqueue_embedding_backfill(
                    [entity, *projection_entities],
                    org_id,
                    relationships_to_create + projection_relationships,
                )
            return AddResponse(
                success=True,
                id=created_id,
                message=fallback_message,
                timestamp=datetime.now(UTC),
                conflicts=conflicts,
                background_jobs=background_jobs,
            )

        # Return immediately with the entity ID - entity will be created in background
        queued_message = f"Queued: {title} (processing in background)"
        if conflicts:
            queued_message += f" (⚠️ {len(conflicts)} potential conflict(s) detected)"
        background_jobs = {}
        if not generate_embeddings:
            background_jobs = {
                "embedding_backfill": {
                    "status": "deferred",
                    "queued_by": create_job_id,
                    "queued_entities": 1,
                    "queued_relationships": len(relationships_to_create),
                }
            }
        return AddResponse(
            success=True,
            id=entity_id,
            message=queued_message,
            timestamp=datetime.now(UTC),
            conflicts=conflicts,
            background_jobs=background_jobs,
        )

    except Exception as e:
        log.warning("add_failed", error=str(e))
        return AddResponse(
            success=False,
            id=None,
            message=f"Failed: {e}",
            timestamp=datetime.now(UTC),
        )
