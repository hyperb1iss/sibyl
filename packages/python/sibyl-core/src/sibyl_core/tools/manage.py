"""Manage tool for Sibyl MCP Server.

The fourth tool: manage() handles operations that modify state.
Includes task workflow, source operations, and analysis actions.

Layering note (audit H8): the task/epic transition bodies here are the pure
domain path — they delegate the status state machine to ``TaskWorkflowEngine``
and apply epic status writes directly, with no entity lock, WebSocket
broadcast, or project-activity side effects. The live MCP server does not call
these transition bodies directly; ``apps/api`` intercepts transition actions and
routes them through its shared ``work_item_workflow`` service, so the served MCP
path gains locking, broadcasting, and project-activity by construction, exactly
like REST. These bodies remain for direct/programmatic callers and tests.

Task and epic workflow actions are soft-deprecated in favor of the RESTful
``/tasks/{id}/*`` and ``/epics/{id}/*`` endpoints (see
``DEPRECATED_ACTION_REPLACEMENTS`` and ``_deprecation_notice``). Source and
analysis actions have no REST equivalent and are not deprecated.
"""

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, cast, get_args
from uuid import uuid4

import structlog

from sibyl_core.auth import MemoryPolicyContext, authorize_memory_write
from sibyl_core.errors import RevisionConflictError
from sibyl_core.models.entities import EntityType
from sibyl_core.runtime_ports import (
    get_audit_port,
    get_content_port,
    get_graph_link_port,
    get_queue_port,
)
from sibyl_core.services.crawl_sources import (
    _crawl_source_exists,
    _create_or_get_crawl_source,
    _enqueue_source_crawl,
    _enqueue_source_sync,
    _list_crawl_source_ids,
    list_unlinked_document_chunks,
)
from sibyl_core.services.link_graph_status import get_link_graph_status_data
from sibyl_core.tasks.dependencies import detect_dependency_cycles
from sibyl_core.tools.helpers import _project_id_for_policy

log = structlog.get_logger()
MEMORY_POLICY_CONTEXT_DATA_KEY = "_memory_policy_context"
DEFAULT_CRAWL_MAX_PAGES = 50
MAX_CRAWL_MAX_PAGES = 500


@dataclass(frozen=True, slots=True)
class ManageGraphRuntime:
    client: Any
    entity_manager: Any
    relationship_manager: Any


type _GraphManagerFactory = Callable[..., Any]
_entity_manager_factory: _GraphManagerFactory | None = None
_relationship_manager_factory: _GraphManagerFactory | None = None


class _MissingGraphManager:
    def __init__(self, manager_name: str) -> None:
        self._manager_name = manager_name

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f"{self._manager_name} factory should be patched in tests")


async def _default_get_graph_client(group_id: str | None = None) -> Any:
    from sibyl_core.services.graph import (
        get_surreal_graph_client,
        prepare_graph_schema,
    )

    client = await get_surreal_graph_client(str(group_id or "default"))
    await prepare_graph_schema(client)
    return client


GraphIntegrationService: Any = None


def _memory_policy_context_from_payload(payload: dict[str, Any]) -> MemoryPolicyContext:
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
        source_surface=payload.get("source_surface") or "manage",
    )


def _memory_policy_payload(data: dict[str, Any]) -> dict[str, Any] | None:
    payload = data.get(MEMORY_POLICY_CONTEXT_DATA_KEY)
    return payload if isinstance(payload, dict) else None


async def get_graph_client(group_id: str | None = None) -> Any:
    return await _default_get_graph_client(group_id)


async def get_graph_runtime(group_id: str) -> ManageGraphRuntime:
    if _entity_manager_factory is None and _relationship_manager_factory is None:
        from sibyl_core.services.graph import get_surreal_graph_runtime

        runtime = await get_surreal_graph_runtime(str(group_id))
        return ManageGraphRuntime(
            client=runtime.client,
            entity_manager=runtime.entity_manager,
            relationship_manager=runtime.relationship_manager,
        )

    client = await get_graph_client(str(group_id))

    entity_manager = (
        _entity_manager_factory(client, group_id=str(group_id))
        if _entity_manager_factory is not None
        else _MissingGraphManager("entity_manager")
    )
    relationship_manager = (
        _relationship_manager_factory(client, group_id=str(group_id))
        if _relationship_manager_factory is not None
        else _MissingGraphManager("relationship_manager")
    )

    return ManageGraphRuntime(
        client=client,
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
    )


def _get_content_read_session() -> Any:
    return get_content_port().read_session()


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


def _manage_mutation_receipt(
    data: dict[str, Any],
    *,
    applied: bool,
    revision: int | None,
    affected_records: list[str],
) -> dict[str, Any]:
    key = data.get("idempotency_key")
    idempotency_key = key.strip() if isinstance(key, str) and key.strip() else None
    return {
        "operation_id": idempotency_key or str(uuid4()),
        "applied": applied,
        "revision": revision,
        "affected_records": affected_records,
        "idempotency_key": idempotency_key,
        "replayed": False,
    }


async def _log_task_learning_capture_denied(
    *,
    task_id: str,
    organization_id: str,
    user_id: str | None,
    memory_scope: str | None,
    project_id: str | None,
    policy_reason: str,
    source_surface: str,
) -> None:
    try:
        await get_audit_port().log_memory_audit_event(
            action="memory.task_learning.manage_denied",
            user_id=user_id,
            organization_id=organization_id,
            request=None,
            memory_scope=memory_scope,
            scope_key=project_id,
            project_id=project_id,
            source_surface=source_surface,
            source_ids=[task_id],
            policy_allowed=False,
            policy_reason=policy_reason,
            details={"task_id": task_id},
        )
    except Exception as exc:
        log.warning("task_learning_manage_deny_audit_failed", error=str(exc), exc_info=True)


async def _authorize_task_learning_capture(
    entity_manager: Any,
    *,
    task_id: str,
    organization_id: str,
    data: dict[str, Any],
) -> ManageResponse | None:
    policy_payload = _memory_policy_payload(data)
    if policy_payload is None:
        await _log_task_learning_capture_denied(
            task_id=task_id,
            organization_id=organization_id,
            user_id=str(data["user_id"]) if data.get("user_id") else None,
            memory_scope=None,
            project_id=None,
            policy_reason="missing_policy_context",
            source_surface="manage",
        )
        return ManageResponse(
            success=False,
            action="complete_task",
            entity_id=task_id,
            message="memory policy context required for task learning capture",
            data={"policy_reason": "missing_policy_context"},
        )

    policy_context = _memory_policy_context_from_payload(policy_payload)
    if policy_context.organization_id != str(organization_id):
        await _log_task_learning_capture_denied(
            task_id=task_id,
            organization_id=organization_id,
            user_id=policy_context.actor_user_id,
            memory_scope=None,
            project_id=None,
            policy_reason="organization_mismatch",
            source_surface=policy_context.source_surface,
        )
        return ManageResponse(
            success=False,
            action="complete_task",
            entity_id=task_id,
            message="task learning capture denied: organization_mismatch",
            data={"policy_reason": "organization_mismatch"},
        )

    entity = await entity_manager.get(task_id)
    project_id = _project_id_for_policy(entity)
    memory_scope = "project" if project_id else "private"
    decision = authorize_memory_write(
        policy_context=policy_context,
        memory_scope=memory_scope,
        scope_key=project_id,
    )
    if decision.allowed:
        return None
    await _log_task_learning_capture_denied(
        task_id=task_id,
        organization_id=organization_id,
        user_id=policy_context.actor_user_id,
        memory_scope=memory_scope,
        project_id=project_id,
        policy_reason=decision.reason,
        source_surface=policy_context.source_surface,
    )
    return ManageResponse(
        success=False,
        action="complete_task",
        entity_id=task_id,
        message=f"task learning capture denied: {decision.reason}",
        data={"policy_reason": decision.reason},
    )


async def _enqueue_task_learning_jobs(
    *,
    task_data: dict[str, Any],
    organization_id: str,
    policy_payload: dict[str, Any],
) -> dict[str, str]:
    queue = get_queue_port()
    episode_job_id = await queue.enqueue_create_learning_episode(
        task_data,
        organization_id,
        policy_context=policy_payload,
    )
    procedure_job_id = await queue.enqueue_create_learning_procedure(
        task_data,
        organization_id,
        policy_context=policy_payload,
    )
    return {
        "learning_episode_job_id": episode_job_id,
        "learning_procedure_job_id": procedure_job_id,
    }


async def _record_task_completion_citations(
    *,
    cited_ids: Any,
    organization_id: str | None,
    principal_id: str | None,
    task_id: str,
    task_project_id: str | None,
    learnings: str,
    actual_hours: Any,
    policy_payload: dict[str, Any] | None,
) -> dict[str, object] | None:
    from sibyl_core.tools.usage_citation import (
        normalize_cited_ids,
        record_cited_item_usages,
    )

    normalized_ids = normalize_cited_ids(cited_ids)
    if not normalized_ids:
        return None

    policy_context = (
        _memory_policy_context_from_payload(policy_payload) if policy_payload is not None else None
    )
    policy_project_id = None
    if policy_context is not None:
        policy_project_id = policy_context.project_id or (
            policy_context.scope_key if policy_context.memory_space == "project" else None
        )
    return await record_cited_item_usages(
        normalized_ids,
        organization_id=organization_id,
        principal_id=principal_id or (policy_context.actor_user_id if policy_context else None),
        project_id=task_project_id or policy_project_id,
        source_surface="manage_complete_task",
        request_metadata={
            "action": "complete_task",
            "actual_hours": actual_hours,
            "has_learnings": bool(learnings),
            "task_id": task_id,
        },
    )


# =============================================================================
# Action Types
# =============================================================================

# Each action category is a Literal so an invalid action is a type error at
# static call sites; the runtime frozensets below are derived from these so the
# type and the validation surface can never drift apart.

# Task workflow (soft-deprecated -> /tasks/{id}/*; still served for MCP clients)
type TaskAction = Literal[
    "start_task",  # Move task to doing status
    "block_task",  # Mark task as blocked with reason
    "unblock_task",  # Remove blocked status
    "submit_review",  # Move task to review status
    "complete_task",  # Mark task as done, capture learnings
    "archive_task",  # Archive without completing
    "update_task",  # Update task fields
    "add_note",  # Add a note to a task
]

# Epic workflow (soft-deprecated -> /epics/{id}/*; still served for MCP clients)
type EpicAction = Literal[
    "start_epic",  # Move epic to in_progress status
    "complete_epic",  # Mark epic as completed with learnings
    "archive_epic",  # Archive epic
    "update_epic",  # Update epic fields
]

# Source operations (no REST equivalent)
type SourceAction = Literal[
    "crawl",  # Trigger crawl of URL
    "sync",  # Re-crawl existing source
    "refresh",  # Sync all sources
    "link_graph",  # Link documents to knowledge graph entities
    "link_graph_status",  # Get linking job status
]

# Analysis (no REST equivalent)
type AnalysisAction = Literal[
    "estimate",  # Estimate task effort
    "prioritize",  # Smart task ordering
    "detect_cycles",  # Find circular dependencies
    "suggest",  # Suggest knowledge for task
]

type ManageAction = TaskAction | EpicAction | SourceAction | AnalysisAction

TASK_ACTIONS: frozenset[str] = frozenset(get_args(TaskAction.__value__))
EPIC_ACTIONS: frozenset[str] = frozenset(get_args(EpicAction.__value__))
SOURCE_ACTIONS: frozenset[str] = frozenset(get_args(SourceAction.__value__))
ANALYSIS_ACTIONS: frozenset[str] = frozenset(get_args(AnalysisAction.__value__))

ALL_ACTIONS: frozenset[str] = TASK_ACTIONS | EPIC_ACTIONS | SOURCE_ACTIONS | ANALYSIS_ACTIONS

# Deprecated workflow actions -> the REST surface that supersedes them. Driving
# the deprecation signal off this map keeps the "what replaces this" answer in
# one place instead of scattered comments.
DEPRECATED_ACTION_REPLACEMENTS: dict[str, str] = {
    "start_task": "POST /tasks/{id}/start",
    "block_task": "POST /tasks/{id}/block",
    "unblock_task": "POST /tasks/{id}/unblock",
    "submit_review": "POST /tasks/{id}/review",
    "complete_task": "POST /tasks/{id}/complete",
    "archive_task": "POST /tasks/{id}/archive",
    "update_task": "PATCH /tasks/{id}",
    "add_note": "POST /tasks/{id}/notes",
    "start_epic": "POST /epics/{id}/start",
    "complete_epic": "POST /epics/{id}/complete",
    "archive_epic": "POST /epics/{id}/archive",
    "update_epic": "PATCH /epics/{id}",
}

assert DEPRECATED_ACTION_REPLACEMENTS.keys() == (TASK_ACTIONS | EPIC_ACTIONS), (
    "deprecation map must cover exactly the task and epic actions"
)


def _deprecation_notice(action: str) -> dict[str, str] | None:
    """Structured deprecation pointer for an action, or None if not deprecated.

    Emitting this on the wire (rather than a Python ``warnings.warn``) is what
    lets an MCP client actually see the replacement, since stderr warnings never
    cross the tool boundary.
    """
    replacement = DEPRECATED_ACTION_REPLACEMENTS.get(action)
    if replacement is None:
        return None
    return {
        "deprecated_action": action,
        "use_instead": replacement,
        "reason": (
            "REST endpoints expose task/epic transitions with idempotency keys "
            "and explicit per-action routes; prefer them over the manage() "
            "grab-bag for lifecycle changes."
        ),
    }


# =============================================================================
# Main manage() function
# =============================================================================


async def manage(
    action: ManageAction | str,
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
        action: The action to perform (see categories above). Static callers get
            a type error for anything outside the valid set; the MCP boundary
            still accepts a raw string and rejects unknown actions at runtime.
        entity_id: Target entity ID (required for most actions).
        data: Action-specific data dict.
        organization_id: Organization ID for graph operations (required).

    Returns:
        ManageResponse with success status, message, and action-specific data.
        Deprecated task/epic actions attach a ``deprecation`` block to ``data``
        pointing at the REST replacement.
    """
    normalized = action.lower().strip()
    data = data or {}

    log.info("manage", action=normalized, entity_id=entity_id, data_keys=list(data.keys()))

    if normalized not in ALL_ACTIONS:
        return ManageResponse(
            success=False,
            action=normalized,
            message=f"Unknown action: {normalized}. Valid actions: {sorted(ALL_ACTIONS)}",
        )

    # Narrowed: the membership guard above proves this is a valid action.
    valid_action = cast("ManageAction", normalized)

    if not organization_id:
        return ManageResponse(
            success=False,
            action=valid_action,
            message="organization_id required for this action",
        )

    deprecation = _deprecation_notice(valid_action)
    if deprecation is not None:
        log.warning(
            "manage_action_deprecated",
            action=valid_action,
            use_instead=deprecation["use_instead"],
        )

    try:
        response = await _dispatch(valid_action, entity_id, data, organization_id=organization_id)
    except Exception as e:
        log.exception("manage_failed", action=valid_action, error=str(e))
        return ManageResponse(
            success=False,
            action=valid_action,
            entity_id=entity_id,
            message=f"Action failed: {e}",
        )

    if deprecation is not None:
        response.data.setdefault("deprecation", deprecation)
    return response


async def _dispatch(
    action: ManageAction,
    entity_id: str | None,
    data: dict[str, Any],
    *,
    organization_id: str,
) -> ManageResponse:
    """Route a validated action to its category handler (exhaustive)."""
    if action in TASK_ACTIONS:
        return await _handle_task_action(
            cast("TaskAction", action), entity_id, data, organization_id=organization_id
        )
    if action in EPIC_ACTIONS:
        return await _handle_epic_action(
            cast("EpicAction", action), entity_id, data, organization_id=organization_id
        )
    if action in SOURCE_ACTIONS:
        return await _handle_source_action(
            cast("SourceAction", action), entity_id, data, organization_id=organization_id
        )
    return await _handle_analysis_action(
        cast("AnalysisAction", action), entity_id, data, organization_id=organization_id
    )


# =============================================================================
# Task Workflow Handlers
# =============================================================================


async def _handle_task_action(
    action: TaskAction,
    entity_id: str | None,
    data: dict[str, Any],
    *,
    organization_id: str | None,
) -> ManageResponse:
    """Handle task workflow actions.

    Uses the TaskWorkflowEngine for proper state machine validation.
    """
    if not entity_id:
        return ManageResponse(
            success=False,
            action=action,
            message=(
                "entity_id required for update_task"
                if action == "update_task"
                else "entity_id required for task actions"
            ),
        )

    if not organization_id:
        return ManageResponse(
            success=False,
            action=action,
            message="organization_id required for task actions",
        )

    runtime = await get_graph_runtime(organization_id)
    client = runtime.client
    entity_manager = runtime.entity_manager
    relationship_manager = runtime.relationship_manager

    # Use workflow engine for state-validated transitions
    from sibyl_core.errors import InvalidTransitionError
    from sibyl_core.tasks.workflow import TaskWorkflowEngine

    workflow = TaskWorkflowEngine(entity_manager, relationship_manager, client, organization_id)
    expected_revision = data.get("expected_revision")
    if expected_revision is not None and (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 1
    ):
        return ManageResponse(
            success=False,
            action=action,
            entity_id=entity_id,
            message="expected_revision must be an integer greater than zero",
        )
    revision_kwargs = (
        {"expected_revision": expected_revision} if expected_revision is not None else {}
    )

    def _task_receipt(task: Any) -> dict[str, Any]:
        revision = getattr(task, "revision", None)
        revision = revision if isinstance(revision, int) else None
        return _manage_mutation_receipt(
            data,
            applied=True,
            revision=revision,
            affected_records=[f"entity:{entity_id}"],
        )

    # All actions below require entity_id (validated above except for update_task)
    try:
        if action == "start_task":
            assert entity_id is not None  # validated above
            assignee = data.get("assignee", "system")
            task = await workflow.start_task(entity_id, assignee, **revision_kwargs)
            return ManageResponse(
                success=True,
                action=action,
                entity_id=entity_id,
                message="Task started",
                data={
                    "status": task.status.value,
                    "branch_name": task.branch_name,
                    "mutation_receipt": _task_receipt(task),
                },
            )

        if action == "block_task":
            assert entity_id is not None  # validated above
            reason = data.get("reason", "No reason provided")
            task = await workflow.block_task(entity_id, reason, **revision_kwargs)
            return ManageResponse(
                success=True,
                action=action,
                entity_id=entity_id,
                message=f"Task blocked: {reason}",
                data={
                    "status": task.status.value,
                    "reason": reason,
                    "mutation_receipt": _task_receipt(task),
                },
            )

        if action == "unblock_task":
            assert entity_id is not None  # validated above
            task = await workflow.unblock_task(entity_id, **revision_kwargs)
            return ManageResponse(
                success=True,
                action=action,
                entity_id=entity_id,
                message="Task unblocked, resuming work",
                data={
                    "status": task.status.value,
                    "mutation_receipt": _task_receipt(task),
                },
            )

        if action == "submit_review":
            assert entity_id is not None  # validated above
            commit_shas = data.get("commit_shas", [])
            pr_url = data.get("pr_url")
            task = await workflow.submit_for_review(
                entity_id,
                commit_shas,
                pr_url,
                **revision_kwargs,
            )
            return ManageResponse(
                success=True,
                action=action,
                entity_id=entity_id,
                message="Task submitted for review",
                data={
                    "status": task.status.value,
                    "pr_url": task.pr_url,
                    "mutation_receipt": _task_receipt(task),
                },
            )

        if action == "complete_task":
            assert entity_id is not None  # validated above
            learnings = data.get("learnings", "")
            actual_hours = data.get("actual_hours")
            policy_payload = _memory_policy_payload(data)
            if learnings:
                policy_error = await _authorize_task_learning_capture(
                    entity_manager,
                    task_id=entity_id,
                    organization_id=organization_id,
                    data=data,
                )
                if policy_error is not None:
                    return policy_error
            task = await workflow.complete_task(
                entity_id,
                actual_hours,
                learnings,
                create_episode=False,
                **revision_kwargs,
            )
            response_data = {
                "status": task.status.value,
                "learnings": learnings,
                "mutation_receipt": _task_receipt(task),
            }
            try:
                citation_usage = await _record_task_completion_citations(
                    cited_ids=data.get("cited_ids"),
                    organization_id=organization_id,
                    principal_id=str(data["user_id"]) if data.get("user_id") else None,
                    task_id=entity_id,
                    task_project_id=task.project_id,
                    learnings=learnings,
                    actual_hours=actual_hours,
                    policy_payload=policy_payload,
                )
            except Exception as exc:
                log.warning(
                    "task_completion_citation_recording_failed",
                    task_id=entity_id,
                    error_type=type(exc).__name__,
                    exc_info=True,
                )
                citation_usage = None
            if citation_usage is not None:
                response_data["citation_usage"] = citation_usage
            if learnings and policy_payload is not None:
                response_data.update(
                    await _enqueue_task_learning_jobs(
                        task_data=task.model_dump(mode="json"),
                        organization_id=organization_id,
                        policy_payload=policy_payload,
                    )
                )
            return ManageResponse(
                success=True,
                action=action,
                entity_id=entity_id,
                message="Task completed" + (" with learnings captured" if learnings else ""),
                data=response_data,
            )

        if action == "archive_task":
            assert entity_id is not None  # validated above
            reason = data.get("reason", "")
            task = await workflow.archive_task(entity_id, reason, **revision_kwargs)
            return ManageResponse(
                success=True,
                action=action,
                entity_id=entity_id,
                message="Task archived",
                data={
                    "status": task.status.value,
                    "mutation_receipt": _task_receipt(task),
                },
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
    except RevisionConflictError as e:
        return ManageResponse(
            success=False,
            action=action,
            entity_id=entity_id,
            message=str(e),
            data=e.details,
        )

    return ManageResponse(success=False, action=action, message="Unknown task action")


async def _update_task(
    entity_manager: Any,
    entity_id: str | None,
    data: dict[str, Any],
    *,
    organization_id: str | None = None,
) -> ManageResponse:
    """Update task fields.

    Args:
        entity_manager: task entity manager
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

    sync = data.get("sync", True)
    expected_revision = data.get("expected_revision")
    if expected_revision is not None and (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 1
    ):
        return ManageResponse(
            success=False,
            action="update_task",
            entity_id=entity_id,
            message="expected_revision must be an integer greater than zero",
        )

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

        job_id = await get_queue_port().enqueue_update_task(
            entity_id,
            updates,
            organization_id,
            expected_revision=expected_revision,
        )
        return ManageResponse(
            success=True,
            action="update_task",
            entity_id=entity_id,
            message=f"Task update queued: {', '.join(updates.keys())}",
            data={
                "job_id": job_id,
                "queued_fields": list(updates.keys()),
                "mutation_receipt": _manage_mutation_receipt(
                    data,
                    applied=False,
                    revision=None,
                    affected_records=[f"entity:{entity_id}"],
                ),
            },
        )

    # Sync mode: update directly
    try:
        if expected_revision is None:
            result = await entity_manager.update(entity_id, updates)
        else:
            result = await entity_manager.update(
                entity_id,
                updates,
                expected_revision=expected_revision,
            )
    except RevisionConflictError as e:
        return ManageResponse(
            success=False,
            action="update_task",
            entity_id=entity_id,
            message=str(e),
            data=e.details,
        )
    if result:
        revision = getattr(result, "revision", None)
        revision = revision if isinstance(revision, int) else None
        return ManageResponse(
            success=True,
            action="update_task",
            entity_id=entity_id,
            message=f"Task updated: {', '.join(updates.keys())}",
            data={
                "updated_fields": list(updates.keys()),
                "revision": revision,
                "mutation_receipt": _manage_mutation_receipt(
                    data,
                    applied=True,
                    revision=revision,
                    affected_records=[f"entity:{entity_id}"],
                ),
            },
        )

    return ManageResponse(
        success=False,
        action="update_task",
        entity_id=entity_id,
        message="Failed to update task",
    )


async def _add_note(
    entity_manager: Any,
    relationship_manager: Any,
    task_id: str | None,
    data: dict[str, Any],
) -> ManageResponse:
    """Add a note to a task.

    Args:
        entity_manager: task entity manager
        relationship_manager: task relationship manager
        task_id: Task ID to add note to
        data: Dict containing note fields:
            - content: Note content (required)
            - author_type: "agent" (assistant-authored) or "user" (default: "user")
            - author_name: Author identifier (optional)
    """
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
    note_id = f"note_{uuid4()}"
    created_at = datetime.now(UTC)

    note = Note(
        id=note_id,
        name=content[:50] + ("..." if len(content) > 50 else ""),
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
            "revision": note.revision,
            "mutation_receipt": _manage_mutation_receipt(
                data,
                applied=True,
                revision=note.revision,
                affected_records=[f"entity:{note_id}"],
            ),
        },
    )


# =============================================================================
# Epic Workflow Handlers
# =============================================================================


async def _handle_epic_action(
    action: EpicAction,
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

    runtime = await get_graph_runtime(organization_id)
    entity_manager = runtime.entity_manager

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
    entity_manager: Any,
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
    action: SourceAction,
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


async def _crawl_source(
    url: str,
    depth: int,
    data: dict[str, Any],
    *,
    organization_id: str,
) -> ManageResponse:
    """Trigger crawl of a URL."""
    sanitized_data = dict(data)
    # Manage crawl only supports website ingestion.
    sanitized_data["source_type"] = "website"

    source_id, created = await _create_or_get_crawl_source(
        url,
        depth,
        sanitized_data,
        organization_id=organization_id,
    )
    raw_max_pages = data.get("max_pages", DEFAULT_CRAWL_MAX_PAGES)
    try:
        max_pages = int(raw_max_pages)
    except (TypeError, ValueError):
        max_pages = DEFAULT_CRAWL_MAX_PAGES
    max_pages = max(1, min(max_pages, MAX_CRAWL_MAX_PAGES))
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

    job_id = await _enqueue_source_sync(source_id, organization_id=organization_id)

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
        await _enqueue_source_sync(source_id, organization_id=organization_id)

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
    max_chunks = data.get("max_chunks", 1000)
    create_new_entities = bool(data.get("create_new_entities", False))
    chunks = await list_unlinked_document_chunks(
        organization_id=organization_id,
        source_id=source_id,
        limit=max_chunks,
    )

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

    runtime = await get_graph_runtime(organization_id)

    source_name = source_id or "all_sources"
    if GraphIntegrationService is not None:
        integration = GraphIntegrationService(
            runtime.client,
            organization_id,
            create_new_entities=create_new_entities,
        )
        stats = await integration.process_chunks(list(chunks), source_name=source_name)
    else:
        stats = await get_graph_link_port().process_chunks(
            graph_client=runtime.client,
            organization_id=organization_id,
            chunks=list(chunks),
            source_name=source_name,
            create_new_entities=create_new_entities,
        )

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
    async with _get_content_read_session() as session:
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
    action: AnalysisAction,
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

    runtime = await get_graph_runtime(organization_id)
    client = runtime.client
    entity_manager = runtime.entity_manager
    relationship_manager = runtime.relationship_manager

    if action == "estimate":
        return await _estimate_effort(entity_manager, relationship_manager, entity_id)

    if action == "prioritize":
        return await _prioritize_tasks(entity_manager, relationship_manager, entity_id)

    if action == "detect_cycles":
        return await _detect_cycles(
            client,
            organization_id,
            entity_id,
            entity_manager=entity_manager,
            relationship_manager=relationship_manager,
        )

    if action == "suggest":
        return await _suggest_knowledge(entity_manager, relationship_manager, entity_id)

    return ManageResponse(success=False, action=action, message="Unknown analysis action")


async def _estimate_effort(
    entity_manager: Any,
    relationship_manager: Any,
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
    entity_manager: Any,
    _relationship_manager: Any,
    project_id: str,
) -> ManageResponse:
    """Get smart task ordering for a project."""
    batch_size = 500
    project_tasks: list[Any] = []
    offset = 0

    while True:
        batch = await entity_manager.list_by_type(
            EntityType.TASK,
            limit=batch_size,
            offset=offset,
            project_id=project_id,
        )
        if not batch:
            break

        project_tasks.extend(batch)
        if len(batch) < batch_size:
            break

        offset += batch_size

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
    *,
    entity_manager: Any | None = None,
    relationship_manager: Any | None = None,
) -> ManageResponse:
    """Detect circular dependencies in a project's task graph."""
    cycle_result = await detect_dependency_cycles(
        client,
        organization_id,
        project_id=project_id,
        entity_manager=entity_manager,
        relationship_manager=relationship_manager,
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
    entity_manager: Any,
    relationship_manager: Any,
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
            "procedures": suggestions.procedures,
            "past_learnings": suggestions.past_learnings,
            "error_patterns": suggestions.error_patterns,
        },
    )
