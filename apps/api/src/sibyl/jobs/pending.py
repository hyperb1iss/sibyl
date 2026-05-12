"""Pending entity registry for async operation queueing.

Tracks entities that are being created asynchronously and queues operations
that target them. When the entity materializes, queued operations are processed.

This solves the race condition where:
1. Client creates entity (async) -> gets ID back immediately
2. Client adds note to entity -> fails because entity doesn't exist yet

With this registry:
1. Client creates entity (async) -> ID returned, marked as pending
2. Client adds note -> note operation queued (not executed)
3. Worker creates entity -> clears pending, processes queued operations
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from sibyl.coordination.pending import (
    PENDING_OPS_PREFIX,
    PENDING_PREFIX,
    PENDING_TTL,
    get_pending,
)

if TYPE_CHECKING:
    from sibyl_core.services.native_graph import NativeEntityManager, NativeRelationshipManager

log = structlog.get_logger()

__all__ = [
    "PENDING_OPS_PREFIX",
    "PENDING_PREFIX",
    "PENDING_TTL",
    "clear_pending",
    "clear_pending_operations",
    "get_pending_operations",
    "is_pending",
    "mark_pending",
    "process_pending_operations",
    "queue_pending_operation",
]


async def mark_pending(
    entity_id: str,
    job_id: str,
    entity_type: str,
    group_id: str,
) -> None:
    """Record an entity as pending creation.

    Args:
        entity_id: The entity ID (returned to client immediately)
        job_id: The arq job ID processing creation
        entity_type: Type of entity (task, episode, etc.)
        group_id: Organization ID
    """
    registry = get_pending()
    await registry.mark_pending(entity_id, job_id, entity_type, group_id)


async def is_pending(entity_id: str) -> dict[str, Any] | None:
    """Check if an entity is pending creation.

    Args:
        entity_id: The entity ID to check

    Returns:
        Pending info dict if pending, None if materialized or unknown
    """
    registry = get_pending()
    return await registry.is_pending(entity_id)


async def clear_pending(entity_id: str) -> bool:
    """Remove pending status after entity materializes.

    Args:
        entity_id: The entity ID to clear

    Returns:
        True if was pending and cleared, False if wasn't pending
    """
    registry = get_pending()
    return await registry.clear_pending(entity_id)


async def queue_pending_operation(
    entity_id: str,
    operation: str,
    payload: dict[str, Any],
    user_id: str | None = None,
) -> str:
    """Queue an operation to run when entity materializes.

    Args:
        entity_id: Target entity ID (must be pending)
        operation: Operation type (e.g., "add_note", "update", "add_relationship")
        payload: Operation-specific data
        user_id: User who initiated the operation

    Returns:
        Operation ID for tracking
    """
    registry = get_pending()
    return await registry.queue_pending_operation(entity_id, operation, payload, user_id)


async def get_pending_operations(entity_id: str) -> list[dict[str, Any]]:
    """Get all pending operations for an entity.

    Args:
        entity_id: The entity ID

    Returns:
        List of pending operation dicts, in queue order (FIFO)
    """
    registry = get_pending()
    return await registry.get_pending_operations(entity_id)


async def clear_pending_operations(entity_id: str) -> int:
    """Remove all pending operations for an entity.

    Called after operations have been processed.

    Args:
        entity_id: The entity ID

    Returns:
        Number of operations that were cleared
    """
    registry = get_pending()
    return await registry.clear_pending_operations(entity_id)


async def process_pending_operations(
    entity_id: str,
    group_id: str,
) -> list[dict[str, Any]]:
    """Process all pending operations for a newly materialized entity.

    This is called by the worker after successfully creating an entity.
    Operations are processed in FIFO order.

    Args:
        entity_id: The entity ID that just materialized
        group_id: Organization ID

    Returns:
        List of processed operations with their results
    """
    from sibyl_core.services.native_graph import get_native_graph_runtime

    ops = await get_pending_operations(entity_id)
    if not ops:
        return []

    log.info("process_pending_operations_start", entity_id=entity_id, count=len(ops))

    runtime = await get_native_graph_runtime(group_id)
    entity_manager = runtime.entity_manager
    relationship_manager = runtime.relationship_manager

    results = []
    for op in ops:
        op_id = op["op_id"]
        operation = op["operation"]
        payload = op["payload"]

        try:
            if operation == "add_note":
                result = await _process_add_note(
                    entity_id, payload, entity_manager, relationship_manager
                )
            elif operation == "update":
                result = await _process_update(entity_id, payload, entity_manager)
            elif operation == "add_relationship":
                result = await _process_add_relationship(entity_id, payload, relationship_manager)
            else:
                log.warning("unknown_pending_operation", operation=operation, op_id=op_id)
                result = {"error": f"Unknown operation: {operation}"}

            results.append({"op_id": op_id, "operation": operation, "success": True, **result})
            log.debug("pending_operation_processed", op_id=op_id, operation=operation)

        except Exception as e:
            log.exception(
                "pending_operation_failed", op_id=op_id, operation=operation, error=str(e)
            )
            results.append(
                {"op_id": op_id, "operation": operation, "success": False, "error": str(e)}
            )

    # Clear processed operations
    await clear_pending_operations(entity_id)

    log.info(
        "process_pending_operations_complete",
        entity_id=entity_id,
        total=len(ops),
        succeeded=sum(1 for r in results if r.get("success")),
    )

    return results


async def _process_add_note(
    task_id: str,
    payload: dict[str, Any],
    entity_manager: NativeEntityManager,
    relationship_manager: NativeRelationshipManager,
) -> dict[str, Any]:
    """Process a queued add_note operation."""
    from sibyl_core.models.entities import Relationship, RelationshipType
    from sibyl_core.models.tasks import AuthorType, Note

    note_id = payload.get("note_id", f"note_{uuid.uuid4()}")
    created_at = (
        datetime.fromisoformat(payload["created_at"])
        if payload.get("created_at")
        else datetime.now(UTC)
    )
    content = payload["content"]
    # Generate name from content (matches Note validator logic)
    name = content[:50] + ("..." if len(content) > 50 else "")

    note = Note(
        id=note_id,
        name=name,
        task_id=task_id,
        content=content,
        author_type=AuthorType(payload.get("author_type", "user")),
        author_name=payload.get("author_name", ""),
        created_at=created_at,
        created_by=payload.get("user_id"),
    )

    await entity_manager.create_direct(note)

    # Create BELONGS_TO relationship
    belongs_to = Relationship(
        id=f"rel_{note_id}_belongs_to_{task_id}",
        source_id=note_id,
        target_id=task_id,
        relationship_type=RelationshipType.BELONGS_TO,
    )
    await relationship_manager.create(belongs_to)

    return {"note_id": note_id}


async def _process_update(
    entity_id: str,
    payload: dict[str, Any],
    entity_manager: NativeEntityManager,
) -> dict[str, Any]:
    """Process a queued update operation."""
    updates = payload.get("updates", {})
    if updates:
        await entity_manager.update(entity_id, updates)
    return {"updated_fields": list(updates.keys())}


async def _process_add_relationship(
    entity_id: str,
    payload: dict[str, Any],
    relationship_manager: NativeRelationshipManager,
) -> dict[str, Any]:
    """Process a queued add_relationship operation."""
    from sibyl_core.models.entities import Relationship, RelationshipType

    rel = Relationship(
        id=payload.get("rel_id", f"rel_{uuid.uuid4()}"),
        source_id=payload.get("source_id", entity_id),
        target_id=payload.get("target_id", entity_id),
        relationship_type=RelationshipType(payload["relationship_type"]),
    )
    await relationship_manager.create(rel)

    return {"relationship_id": rel.id}
