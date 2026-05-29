"""Unified work-item state-transition service.

A work item is a task; a task with children is an epic (W14). This module is
the single place where a transition acquires the entity lock, performs the
domain transition through the core ``TaskWorkflowEngine``, refreshes the parent
project's activity timestamp, and broadcasts the WebSocket event. REST handlers
and the MCP ``manage()`` dispatcher both route through here, so neither surface
can drift from the other or silently skip locking and broadcasting (audit H8).

The domain change itself lives in ``sibyl_core``; the lock, broadcast, and
project-activity side effects are apps/api concerns and stay here. Core never
imports this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import structlog

from sibyl.api.event_types import WSEvent
from sibyl.api.websocket import broadcast_event
from sibyl.locks import entity_lock
from sibyl.persistence.graph_runtime import (
    get_task_graph_runtime,
    update_graph_entity,
)
from sibyl_core.models.entities import EntityType
from sibyl_core.tasks.workflow import TaskWorkflowEngine

log = structlog.get_logger()


class WorkItemAction(StrEnum):
    """Lifecycle transitions shared by tasks and epics."""

    START_TASK = "start_task"
    BLOCK_TASK = "block_task"
    UNBLOCK_TASK = "unblock_task"
    SUBMIT_REVIEW = "submit_review"
    COMPLETE_TASK = "complete_task"
    ARCHIVE_TASK = "archive_task"
    START_EPIC = "start_epic"
    COMPLETE_EPIC = "complete_epic"
    ARCHIVE_EPIC = "archive_epic"


TASK_TRANSITIONS: frozenset[WorkItemAction] = frozenset(
    {
        WorkItemAction.START_TASK,
        WorkItemAction.BLOCK_TASK,
        WorkItemAction.UNBLOCK_TASK,
        WorkItemAction.SUBMIT_REVIEW,
        WorkItemAction.COMPLETE_TASK,
        WorkItemAction.ARCHIVE_TASK,
    }
)
EPIC_TRANSITIONS: frozenset[WorkItemAction] = frozenset(
    {
        WorkItemAction.START_EPIC,
        WorkItemAction.COMPLETE_EPIC,
        WorkItemAction.ARCHIVE_EPIC,
    }
)


@dataclass(frozen=True, slots=True)
class WorkItemTransition:
    """Canonical result of a work-item transition.

    ``status`` is the entity's status after the transition. ``fields`` carries
    the transition-specific values a caller surfaces in its response (branch
    name, pr url, learnings); ``broadcast_data`` is the payload that was sent on
    the WebSocket so callers can mirror it without recomputing.
    """

    action: WorkItemAction
    item_id: str
    entity_type: EntityType
    status: str
    name: str
    fields: dict[str, Any] = field(default_factory=dict)
    broadcast_data: dict[str, Any] = field(default_factory=dict)
    task_data: dict[str, Any] = field(default_factory=dict)
    """JSON dump of the transitioned task; empty for epic transitions. Used by
    callers that enqueue follow-up jobs (learning capture)."""

    @property
    def response_data(self) -> dict[str, Any]:
        """Status plus transition fields, the shape route responses expose."""
        return {"status": self.status, **self.fields}


def _broadcast_entity_type(entity_type: EntityType) -> str:
    return "epic" if entity_type == EntityType.EPIC else "task"


async def _broadcast(
    item_id: str,
    action: WorkItemAction,
    entity_type: EntityType,
    data: dict[str, Any],
    *,
    group_id: str,
) -> dict[str, Any]:
    payload = {
        "id": item_id,
        "entity_type": _broadcast_entity_type(entity_type),
        "action": action.value,
        **data,
    }
    await broadcast_event(WSEvent.ENTITY_UPDATED, payload, org_id=group_id)
    return payload


@dataclass(frozen=True, slots=True)
class _TransitionOutcome:
    """Internal per-action result before the wrapper assembles the response.

    ``response_fields`` and ``broadcast_fields`` are kept distinct because the
    REST surface intentionally diverges on a couple of actions (block exposes
    ``reason`` in the response but ``blocker`` on the wire).
    """

    status: str
    name: str
    response_fields: dict[str, Any]
    broadcast_fields: dict[str, Any]
    task: Any | None = None


async def _run_task_transition(
    workflow: TaskWorkflowEngine,
    action: WorkItemAction,
    item_id: str,
    payload: dict[str, Any],
) -> _TransitionOutcome:
    """Apply a task-style transition via the core engine.

    Project-activity is updated inside the engine for these transitions, so the
    wrapper does not touch it again.
    """
    if action == WorkItemAction.START_TASK:
        task = await workflow.start_task(item_id, payload.get("assignee") or "system")
        fields = {"branch_name": task.branch_name}
        return _TransitionOutcome(task.status.value, task.name, fields, fields, task)

    if action == WorkItemAction.BLOCK_TASK:
        reason = payload.get("reason", "No reason provided")
        task = await workflow.block_task(item_id, reason)
        return _TransitionOutcome(
            task.status.value, task.name, {"reason": reason}, {"blocker": reason}, task
        )

    if action == WorkItemAction.UNBLOCK_TASK:
        task = await workflow.unblock_task(item_id)
        return _TransitionOutcome(task.status.value, task.name, {}, {}, task)

    if action == WorkItemAction.SUBMIT_REVIEW:
        commit_shas = payload.get("commit_shas") or []
        pr_url = payload.get("pr_url")
        task = await workflow.submit_for_review(item_id, commit_shas, pr_url)
        fields = {"pr_url": task.pr_url}
        return _TransitionOutcome(task.status.value, task.name, fields, fields, task)

    if action == WorkItemAction.COMPLETE_TASK:
        # create_episode stays False: learning artifacts are enqueued as
        # background jobs by the calling surface, not built inline here.
        task = await workflow.complete_task(
            item_id,
            payload.get("actual_hours"),
            payload.get("learnings") or "",
            create_episode=False,
        )
        fields = {"learnings": payload.get("learnings")}
        return _TransitionOutcome(task.status.value, task.name, fields, fields, task)

    # ARCHIVE_TASK
    task = await workflow.archive_task(item_id, payload.get("reason") or "")
    return _TransitionOutcome(task.status.value, task.name, {}, {}, task)


async def _run_epic_transition(
    group_id: str,
    action: WorkItemAction,
    item_id: str,
    payload: dict[str, Any],
    entity: Any | None,
) -> _TransitionOutcome:
    """Apply an epic-style transition as a direct status write.

    Epics persist a stored status (not yet derived); these mirror the existing
    REST epic write exactly. Project-activity is refreshed by the wrapper.
    """
    name = getattr(entity, "name", "") if entity is not None else ""

    if action == WorkItemAction.START_EPIC:
        await update_graph_entity(group_id, item_id, {"status": "in_progress"})
        return _TransitionOutcome("in_progress", name, {}, {})

    if action == WorkItemAction.COMPLETE_EPIC:
        learnings = payload.get("learnings")
        updates: dict[str, Any] = {
            "status": "completed",
            "completed_date": datetime.now(UTC).isoformat(),
        }
        if learnings:
            updates["learnings"] = learnings
        await update_graph_entity(group_id, item_id, updates)
        fields = {"learnings": learnings or ""}
        return _TransitionOutcome("completed", name, fields, fields)

    # ARCHIVE_EPIC
    await update_graph_entity(group_id, item_id, {"status": "archived"})
    return _TransitionOutcome("archived", name, {}, {})


async def _touch_project_activity(group_id: str, entity: Any) -> None:
    """Refresh the parent project's last_activity_at after an epic transition.

    Task transitions already do this inside the engine; epics write status
    directly, so the activity bump lives here to match REST behavior. Failure
    here must not fail the transition.
    """
    metadata = getattr(entity, "metadata", None) or {}
    project_id = metadata.get("project_id")
    if not project_id:
        return
    try:
        await update_graph_entity(
            group_id,
            project_id,
            {"last_activity_at": datetime.now(UTC).isoformat()},
        )
    except Exception as exc:
        log.warning(
            "work_item_project_activity_failed",
            project_id=project_id,
            entity_id=getattr(entity, "id", None),
            error=str(exc),
        )


async def transition_work_item(
    group_id: str,
    item_id: str,
    action: WorkItemAction,
    *,
    payload: dict[str, Any] | None = None,
    entity: Any | None = None,
    broadcast: bool = True,
) -> WorkItemTransition:
    """Perform a locked, broadcasting work-item transition.

    Acquires the entity lock, runs the domain transition through the core
    workflow engine (tasks) or a direct status write (epics), refreshes project
    activity, and broadcasts the change. The single wrapper REST and MCP share.

    Args:
        group_id: Organization scope (graph namespace).
        item_id: Task or epic UUID.
        action: The transition to apply.
        payload: Transition-specific inputs (assignee, reason, learnings, ...).
        entity: Pre-loaded entity from the caller's access check; reused to
            avoid a redundant read and to source the epic's project_id.
        broadcast: Whether to emit the WebSocket event (default True).

    Returns:
        The canonical transition result.

    Raises:
        LockAcquisitionError: If the entity lock cannot be acquired.
        InvalidTransitionError: If the task transition is disallowed.
        EntityNotFoundError: If a task transition targets a missing entity.
    """
    payload = payload or {}

    async with entity_lock(group_id, item_id, blocking=True) as lock_token:
        if not lock_token:
            from sibyl.locks import LockAcquisitionError

            raise LockAcquisitionError(item_id, group_id, "locked")

        if action in TASK_TRANSITIONS:
            runtime = await get_task_graph_runtime(group_id)
            workflow = TaskWorkflowEngine(
                runtime.entity_manager,
                runtime.relationship_manager,
                runtime.client,
                group_id,
            )
            outcome = await _run_task_transition(workflow, action, item_id, payload)
            entity_type = EntityType.TASK
        else:
            outcome = await _run_epic_transition(group_id, action, item_id, payload, entity)
            entity_type = EntityType.EPIC
            await _touch_project_activity(group_id, entity)

    broadcast_data = {"status": outcome.status, "name": outcome.name, **outcome.broadcast_fields}
    if broadcast:
        await _broadcast(item_id, action, entity_type, broadcast_data, group_id=group_id)

    log.info(
        "work_item_transition",
        action=action.value,
        item_id=item_id,
        status=outcome.status,
        entity_type=entity_type.value,
    )

    task_data: dict[str, Any] = {}
    if outcome.task is not None and hasattr(outcome.task, "model_dump"):
        task_data = outcome.task.model_dump(mode="json")

    return WorkItemTransition(
        action=action,
        item_id=item_id,
        entity_type=entity_type,
        status=outcome.status,
        name=outcome.name,
        fields=outcome.response_fields,
        broadcast_data=broadcast_data,
        task_data=task_data,
    )
