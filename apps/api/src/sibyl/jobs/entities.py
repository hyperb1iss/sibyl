"""Entity creation and update jobs.

These jobs handle async entity operations via SurrealDB, allowing
the API to return quickly while background processing continues.
"""

from typing import Any

import structlog

from sibyl.api.event_types import WSEvent
from sibyl.persistence.auth_runtime import log_memory_audit_event
from sibyl_core.auth import MemoryPolicyContext, OrganizationRole, authorize_memory_write
from sibyl_core.auth.memory_policy import MemoryPolicyAction, MemoryPolicyDecision
from sibyl_core.embeddings.native import configured_native_embedding_provider
from sibyl_core.services.native_graph import get_native_graph_runtime
from sibyl_core.services.surreal_content import MemoryScope
from sibyl_core.tasks.distillation import build_learning_episode, build_learning_procedure

log = structlog.get_logger()


def serialize_memory_policy_context(
    policy_context: MemoryPolicyContext | None,
) -> dict[str, Any] | None:
    if policy_context is None:
        return None
    organization_role = policy_context.organization_role
    return {
        "actor_user_id": policy_context.actor_user_id,
        "organization_id": policy_context.organization_id,
        "organization_role": organization_role.value
        if isinstance(organization_role, OrganizationRole)
        else organization_role,
        "accessible_projects": sorted(policy_context.accessible_projects)
        if policy_context.accessible_projects is not None
        else None,
        "accessible_delegations": sorted(policy_context.accessible_delegations)
        if policy_context.accessible_delegations is not None
        else None,
        "delegated_authority": policy_context.delegated_authority,
        "agent_id": policy_context.agent_id,
        "project_id": policy_context.project_id,
        "memory_space": policy_context.memory_space,
        "scope_key": policy_context.scope_key,
        "source_surface": policy_context.source_surface,
    }


def deserialize_memory_policy_context(
    payload: dict[str, Any] | None,
) -> MemoryPolicyContext | None:
    if payload is None:
        return None
    return MemoryPolicyContext(
        actor_user_id=payload.get("actor_user_id"),
        organization_id=payload.get("organization_id"),
        organization_role=payload.get("organization_role"),
        accessible_projects=payload.get("accessible_projects"),
        accessible_delegations=payload.get("accessible_delegations"),
        delegated_authority=payload.get("delegated_authority"),
        agent_id=payload.get("agent_id"),
        project_id=payload.get("project_id"),
        memory_space=payload.get("memory_space"),
        scope_key=payload.get("scope_key"),
        source_surface=payload.get("source_surface") or "job",
    )


def _policy_context_project_id(policy_context: MemoryPolicyContext) -> str | None:
    if policy_context.project_id:
        return policy_context.project_id
    if policy_context.memory_space == MemoryScope.PROJECT.value:
        return policy_context.scope_key
    return None


async def _safe_broadcast(event: str, data: dict[str, Any], *, org_id: str | None) -> None:
    """Broadcast event via Redis pub/sub (worker runs in separate process)."""
    try:
        from sibyl.api.pubsub import publish_event

        await publish_event(event, data, org_id=org_id)
    except Exception:
        log.debug("Broadcast failed (Redis unavailable)", event=event)


async def _create_learning_artifact_link(
    relationship_manager: Any,
    *,
    source_id: str,
    target_id: str,
    relationship_type: Any,
    link_id: str,
    metadata: dict[str, Any] | None = None,
) -> str:
    from sibyl_core.models.entities import Relationship

    return await relationship_manager.create(
        Relationship(
            id=link_id,
            source_id=source_id,
            target_id=target_id,
            relationship_type=relationship_type,
            metadata=metadata or {},
        )
    )


async def _inherit_task_knowledge(
    relationship_manager: Any,
    *,
    source_id: str,
    task_id: str,
) -> int:
    """Copy task knowledge edges onto a derived learning artifact."""
    from sibyl_core.models.entities import RelationshipType

    task_relationships = await relationship_manager.get_for_entity(
        task_id,
        relationship_types=[
            RelationshipType.REQUIRES,
            RelationshipType.REFERENCES,
            RelationshipType.PART_OF,
        ],
    )

    inherited_count = 0
    for rel in task_relationships:
        try:
            await _create_learning_artifact_link(
                relationship_manager,
                source_id=source_id,
                target_id=rel.target_id,
                relationship_type=RelationshipType.REFERENCES,
                link_id=f"rel_{source_id}_{rel.target_id}",
                metadata={"inherited_from_task": task_id},
            )
            inherited_count += 1
        except Exception as e:
            log.warning(
                "learning_artifact_inherit_failed",
                error=str(e),
                source_id=source_id,
                target_id=getattr(rel, "target_id", None),
            )

    return inherited_count


async def _log_learning_job_audit(
    *,
    action: str,
    group_id: str,
    task_id: str,
    project_id: str | None,
    policy_context: MemoryPolicyContext | None,
    decision: MemoryPolicyDecision,
    derived_id: str | None = None,
) -> None:
    try:
        await log_memory_audit_event(
            action=action,
            user_id=policy_context.actor_user_id if policy_context else None,
            organization_id=group_id,
            request=None,
            memory_scope=decision.memory_scope.value,
            scope_key=decision.scope_key,
            project_id=project_id,
            source_surface="job",
            source_ids=[task_id],
            derived_ids=[derived_id] if derived_id else [],
            policy_allowed=decision.allowed,
            policy_reason=decision.reason,
            details={
                "job": action,
                "task_id": task_id,
                "source_policy_surface": policy_context.source_surface if policy_context else None,
            },
        )
    except Exception as exc:
        log.warning("learning_job_audit_failed", action=action, error=str(exc), exc_info=True)


async def _authorize_learning_job_write(
    *,
    action: str,
    task_id: str,
    project_id: str | None,
    group_id: str,
    policy_context_payload: dict[str, Any] | None,
) -> tuple[MemoryPolicyDecision, MemoryPolicyContext | None]:
    policy_context = deserialize_memory_policy_context(policy_context_payload)
    memory_scope = MemoryScope.PROJECT if project_id else MemoryScope.PRIVATE
    if policy_context is not None and policy_context.organization_id != str(group_id):
        decision = MemoryPolicyDecision(
            action=MemoryPolicyAction.WRITE,
            allowed=False,
            reason="organization_mismatch",
            memory_scope=memory_scope,
            scope_key=project_id,
            policy_context=policy_context,
        )
        await _log_learning_job_audit(
            action=action,
            group_id=group_id,
            task_id=task_id,
            project_id=project_id,
            policy_context=policy_context,
            decision=decision,
        )
        raise ValueError(f"Learning job denied: {decision.reason}")
    if policy_context is not None:
        policy_project_id = _policy_context_project_id(policy_context)
        if policy_project_id != project_id:
            decision = MemoryPolicyDecision(
                action=MemoryPolicyAction.WRITE,
                allowed=False,
                reason="project_mismatch",
                memory_scope=MemoryScope.PROJECT
                if policy_project_id or project_id
                else MemoryScope.PRIVATE,
                scope_key=project_id or policy_project_id,
                policy_context=policy_context,
            )
            await _log_learning_job_audit(
                action=action,
                group_id=group_id,
                task_id=task_id,
                project_id=project_id or policy_project_id,
                policy_context=policy_context,
                decision=decision,
            )
            raise ValueError(f"Learning job denied: {decision.reason}")
    decision = authorize_memory_write(
        policy_context=policy_context,
        memory_scope=memory_scope,
        scope_key=project_id,
    )
    if not decision.allowed:
        await _log_learning_job_audit(
            action=action,
            group_id=group_id,
            task_id=task_id,
            project_id=project_id,
            policy_context=policy_context,
            decision=decision,
        )
        raise ValueError(f"Learning job denied: {decision.reason}")
    return decision, policy_context


def _attach_learning_policy_metadata(entity: Any, decision: MemoryPolicyDecision) -> None:
    metadata = dict(getattr(entity, "metadata", {}) or {})
    metadata.update(
        {
            "memory_scope": decision.memory_scope.value,
            "policy_allowed": decision.allowed,
            "policy_reason": decision.reason,
            "source_surface": "job",
        }
    )
    if decision.scope_key:
        metadata["scope_key"] = decision.scope_key
    object.__setattr__(entity, "metadata", metadata)


async def create_entity(  # noqa: PLR0915
    ctx: dict[str, Any],  # noqa: ARG001
    entity_data: dict[str, Any],
    entity_type: str,
    group_id: str,
    relationships: list[dict[str, Any]] | None = None,
    auto_link_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create entity asynchronously via SurrealDB.

    This job runs in the background so callers get fast responses while
    SurrealDB handles native entity and relationship persistence.

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
    from sibyl_core.models.entities import (
        Entity,
        Episode,
        Pattern,
        Procedure,
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
        runtime = await get_native_graph_runtime(
            group_id,
            embedding_provider=configured_native_embedding_provider(),
        )
        entity_manager = runtime.entity_manager

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
        elif entity_type == "procedure":
            entity = Procedure.model_validate(entity_data)
        else:
            entity = Episode.model_validate(entity_data)

        # Dedup-on-write: check for near-duplicates before creating.
        # If found, still create the entity (callers already have its ID),
        # but enrich the existing duplicate and log the match.
        dedup_target_id: str | None = None
        deduplicated = False
        if entity_type not in ("task", "project", "epic"):
            try:
                from sibyl_core.tools.conflicts import find_similar_entities

                similar = await find_similar_entities(
                    title=entity_data.get("name", ""),
                    content=entity_data.get("content", entity_data.get("description", "")),
                    organization_id=group_id,
                    entity_types=[entity_type],
                    limit=1,
                    min_score=0.95,
                )
                if similar:
                    dedup_target_id = similar[0][0]
                    log.info(
                        "dedup_on_write_match",
                        new_name=entity_data.get("name"),
                        existing_id=dedup_target_id,
                        existing_name=similar[0][1],
                        score=f"{similar[0][3]:.3f}",
                    )
                    deduplicated = True
            except Exception as e:
                log.debug("dedup_on_write_check_skipped", error=str(e))

        # Always create the entity so the queued ID stays valid
        if entity_type in ("task", "project", "epic", "pattern", "procedure"):
            created_id = await entity_manager.create_direct(entity, generate_embedding=True)
        else:
            created_id = await entity_manager.create(entity)

        log.info(
            "create_entity_graph_created",
            entity_id=created_id,
            entity_type=entity_type,
        )

        relationship_manager = runtime.relationship_manager

        # Link to existing duplicate if dedup matched (entity still created for ID stability)
        if deduplicated and dedup_target_id:
            try:
                dedup_rel = Relationship(
                    id=f"rel_{created_id}_duplicate_of_{dedup_target_id}",
                    source_id=created_id,
                    target_id=dedup_target_id,
                    relationship_type=RelationshipType.RELATED_TO,
                    metadata={"dedup_match": True, "auto_linked": True},
                )
                await relationship_manager.create(dedup_rel)
                log.info("dedup_on_write_linked", source=created_id, target=dedup_target_id)
            except Exception as e:
                log.warning("dedup_on_write_link_failed", error=str(e))

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
            "deduplicated": deduplicated,
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
    *,
    policy_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a learning episode from a completed task.

    This job runs in the background so task completion returns fast while
    SurrealDB persists the distilled learning artifact natively.

    Args:
        ctx: arq context
        task_data: Serialized task dict (from task.model_dump())
        group_id: Organization ID
        policy_context: Serialized actor/project policy context

    Returns:
        Dict with episode creation results
    """
    from sibyl_core.models.entities import RelationshipType
    from sibyl_core.models.tasks import Task

    task = Task.model_validate(task_data)

    log.info(
        "create_learning_episode_started",
        task_id=task.id,
        task_title=task.title,
    )

    try:
        decision, restored_policy_context = await _authorize_learning_job_write(
            action="memory.task_learning.episode",
            task_id=task.id,
            project_id=task.project_id,
            group_id=group_id,
            policy_context_payload=policy_context,
        )
        runtime = await get_native_graph_runtime(group_id)
        entity_manager = runtime.entity_manager
        relationship_manager = runtime.relationship_manager

        episode = build_learning_episode(task)
        _attach_learning_policy_metadata(episode, decision)

        episode_id = await entity_manager.create_direct(episode)

        log.info(
            "create_learning_episode_entity_created",
            episode_id=episode_id,
            task_id=task.id,
        )

        # Link episode back to task
        await _create_learning_artifact_link(
            relationship_manager,
            source_id=episode_id,
            target_id=task.id,
            relationship_type=RelationshipType.DERIVED_FROM,
            link_id=f"rel_episode_{task.id}",
        )

        inherited_count = await _inherit_task_knowledge(
            relationship_manager,
            source_id=episode_id,
            task_id=task.id,
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

        await _log_learning_job_audit(
            action="memory.task_learning.episode",
            group_id=group_id,
            task_id=task.id,
            project_id=task.project_id,
            policy_context=restored_policy_context,
            decision=decision,
            derived_id=episode_id,
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


async def create_learning_procedure(
    ctx: dict[str, Any],  # noqa: ARG001
    task_data: dict[str, Any],
    group_id: str,
    *,
    policy_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a reusable procedure from a completed task."""
    from sibyl_core.models.entities import Relationship, RelationshipType
    from sibyl_core.models.tasks import Task

    task = Task.model_validate(task_data)

    log.info(
        "create_learning_procedure_started",
        task_id=task.id,
        task_title=task.title,
    )

    try:
        decision, restored_policy_context = await _authorize_learning_job_write(
            action="memory.task_learning.procedure",
            task_id=task.id,
            project_id=task.project_id,
            group_id=group_id,
            policy_context_payload=policy_context,
        )
        runtime = await get_native_graph_runtime(group_id)
        entity_manager = runtime.entity_manager
        relationship_manager = runtime.relationship_manager

        notes_getter = getattr(entity_manager, "get_notes_for_task", None)
        note_contents: list[str] = []
        if callable(notes_getter):
            try:
                notes = await notes_getter(task.id, limit=20)
                note_contents = [
                    note.content for note in notes if getattr(note, "content", "").strip()
                ]
            except Exception as exc:
                log.debug(
                    "create_learning_procedure_notes_failed",
                    task_id=task.id,
                    error=str(exc),
                )

        procedure = build_learning_procedure(task, note_contents)
        if procedure is None:
            result = {
                "procedure_id": None,
                "task_id": task.id,
                "notes_used": len(note_contents),
                "created": False,
            }
            log.info("create_learning_procedure_skipped", **result)
            return result

        _attach_learning_policy_metadata(procedure, decision)
        procedure_id = await entity_manager.create_direct(procedure)

        await relationship_manager.create(
            Relationship(
                id=f"rel_task_{task.id}_procedure",
                source_id=task.id,
                target_id=procedure_id,
                relationship_type=RelationshipType.USES_PROCEDURE,
            )
        )
        await relationship_manager.create(
            Relationship(
                id=f"rel_procedure_{task.id}",
                source_id=procedure_id,
                target_id=task.id,
                relationship_type=RelationshipType.DERIVED_FROM,
            )
        )

        inherited_count = await _inherit_task_knowledge(
            relationship_manager,
            source_id=procedure_id,
            task_id=task.id,
        )

        result = {
            "procedure_id": procedure_id,
            "task_id": task.id,
            "notes_used": len(note_contents),
            "inherited_relationships": inherited_count,
            "created": True,
        }

        await _safe_broadcast(
            WSEvent.ENTITY_CREATED,
            {
                "id": procedure_id,
                "entity_type": "procedure",
                "name": procedure.name,
                "derived_from": task.id,
            },
            org_id=group_id,
        )

        await _log_learning_job_audit(
            action="memory.task_learning.procedure",
            group_id=group_id,
            task_id=task.id,
            project_id=task.project_id,
            policy_context=restored_policy_context,
            decision=decision,
            derived_id=procedure_id,
        )
        log.info("create_learning_procedure_completed", **result)
        return result

    except Exception as e:
        log.exception(
            "create_learning_procedure_failed",
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
    add_depends_on: list[str] | None = None,
    remove_depends_on: list[str] | None = None,
) -> dict[str, Any]:
    """Update a task asynchronously with epic relationship and auto-start logic.

    Task-aware background job that handles concerns the generic update_entity
    doesn't: BELONGS_TO epic relationships, epic auto-start on forward progress,
    and DEPENDS_ON dependency mutations.

    Args:
        ctx: arq context
        task_id: The task entity ID to update
        updates: Dict of field names to new values
        group_id: Organization ID
        epic_id: Epic ID if being set/changed (triggers BELONGS_TO creation)
        new_status: New task status (triggers epic auto-start check)
        add_depends_on: Task IDs to add as dependencies
        remove_depends_on: Task IDs to remove as dependencies

    Returns:
        Dict with update results
    """
    from sibyl.locks import entity_lock
    from sibyl_core.models.entities import Relationship, RelationshipType

    add_depends_on = add_depends_on or []
    remove_depends_on = remove_depends_on or []

    log.info(
        "update_task_started",
        task_id=task_id,
        fields=list(updates.keys()),
        epic_id=epic_id,
        add_deps=len(add_depends_on),
        remove_deps=len(remove_depends_on),
    )

    try:
        async with entity_lock(group_id, task_id, blocking=True) as lock_token:
            if not lock_token:
                log.warning("update_task_lock_failed", task_id=task_id)
                return {"task_id": task_id, "success": False, "message": "Lock contention"}

            runtime = await get_native_graph_runtime(group_id)
            entity_manager = runtime.entity_manager

            # Perform the entity field update (skip if only dep changes)
            updated = None
            if len(updates) > 1:  # more than just modified_by
                updated = await entity_manager.update(task_id, updates)
                if not updated:
                    log.warning("update_task_no_changes", task_id=task_id)
                    return {"task_id": task_id, "success": False, "message": "No changes made"}

            # Create relationship manager if any relationship changes needed
            needs_rel_mgr = epic_id is not None or add_depends_on or remove_depends_on
            if needs_rel_mgr:
                relationship_manager = runtime.relationship_manager

            # Create BELONGS_TO relationship for epic (if epic_id was set/changed)
            if epic_id is not None:
                belongs_to_epic = Relationship(
                    id=f"rel_{task_id}_belongs_to_{epic_id}",
                    source_id=task_id,
                    target_id=epic_id,
                    relationship_type=RelationshipType.BELONGS_TO,
                )
                await relationship_manager.create(belongs_to_epic)

            # Handle dependency mutations
            for dep_id in add_depends_on:
                dep_rel = Relationship(
                    id=f"rel_{task_id}_depends_on_{dep_id}",
                    source_id=task_id,
                    target_id=dep_id,
                    relationship_type=RelationshipType.DEPENDS_ON,
                )
                await relationship_manager.create(dep_rel)
            for dep_id in remove_depends_on:
                await relationship_manager.delete_between(
                    task_id, dep_id, RelationshipType.DEPENDS_ON
                )

            # Auto-start epic if task moves to forward-progress state
            if new_status:
                task_entity = updated or await entity_manager.get(task_id)
                resolved_epic = epic_id or (
                    task_entity.metadata.get("epic_id") if task_entity else None
                )
                if resolved_epic:
                    await _maybe_start_epic_bg(entity_manager, task_id, resolved_epic, new_status)

        # Broadcast outside the lock
        broadcast_data: dict[str, Any] = {
            "id": task_id,
            "entity_type": "task",
            "action": "update_task",
            **updates,
        }
        if updated:
            broadcast_data["name"] = updated.name
        await _safe_broadcast(WSEvent.ENTITY_UPDATED, broadcast_data, org_id=group_id)

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

    try:
        epic = await entity_manager.get(epic_id)
    except KeyError:
        return False
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
    log.info(
        "update_entity_started",
        entity_id=entity_id,
        entity_type=entity_type,
        fields=list(updates.keys()),
    )

    try:
        runtime = await get_native_graph_runtime(group_id)
        entity_manager = runtime.entity_manager

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
