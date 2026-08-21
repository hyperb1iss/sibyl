"""Knowledge graph management MCP tools."""

from copy import deepcopy
from typing import Any, cast
from uuid import UUID

import structlog
from mcp.server import MCPServer

import sibyl.mcp_tools.context as mcp_context
import sibyl.mcp_tools.idempotency as mcp_idempotency
import sibyl.mcp_tools.policy as mcp_policy
from sibyl.api.idempotency import (
    complete_idempotency_record,
    idempotency_record_pending,
    idempotency_request_hash,
    reserve_idempotency_record,
)
from sibyl.mcp_tools import serialization
from sibyl.persistence.auth_runtime import (
    log_memory_audit_event,
    resolve_accessible_team_scope_keys,
)
from sibyl.persistence.content_common import ApiIdempotencyRecord
from sibyl.services.work_item_workflow import WorkItemAction
from sibyl_core.auth.memory_policy import MemoryPolicyAction, MemoryPolicyDecision
from sibyl_core.services.surreal_content import (
    MemoryScope,
    get_raw_memory,
    get_raw_memory_by_source_id,
)

log = structlog.get_logger()

MCP_ENTITY_PROJECT_POLICY_ACTIONS = {
    "add_note",
    "archive_epic",
    "archive_task",
    "block_task",
    "complete_epic",
    "complete_task",
    "estimate",
    "start_epic",
    "start_task",
    "submit_review",
    "suggest",
    "unblock_task",
    "update_epic",
    "update_task",
}
MCP_PROJECT_ID_POLICY_ACTIONS = {"detect_cycles", "prioritize"}


async def _mcp_entity_write_target(
    *, organization_id: str, entity_id: str
) -> tuple[str | None, dict[str, Any]]:
    """The project a manage target is addressed by, and its own scope metadata.

    Resolving only the project made every entity-targeted action a project
    write, so a contributor could mutate another principal's private row that
    happened to live in a shared project. The scope travels with it so the
    caller's authorization can answer to the row it is actually touching.
    """
    from sibyl_core.services.graph import get_surreal_graph_runtime
    from sibyl_core.tools.helpers import _project_id_for_policy

    runtime = await get_surreal_graph_runtime(organization_id)
    entity = await runtime.entity_manager.get(entity_id)
    if entity is None:
        return None, {}
    metadata = getattr(entity, "metadata", None)
    return _project_id_for_policy(entity), dict(metadata) if isinstance(metadata, dict) else {}


async def _authorize_mcp_manage_action(
    *,
    ctx: mcp_context.McpContext,
    action: str,
    entity_id: str | None,
    accessible_projects: set[str] | None,
) -> MemoryPolicyDecision | None:
    normalized_action = action.lower().strip()
    if normalized_action == "correct_memory":
        if not entity_id:
            return None
        memory = await get_raw_memory(
            organization_id=ctx.org_id,
            memory_id=entity_id,
        )
        if memory is None:
            memory = await get_raw_memory_by_source_id(
                organization_id=ctx.org_id,
                source_id=entity_id,
            )
        if memory is None:
            return None
        if memory.memory_scope is MemoryScope.PRIVATE and memory.principal_id != ctx.user_id:
            raise ValueError("principal_mismatch")
        policy_projects = accessible_projects
        if policy_projects is None and memory.scope_key:
            policy_projects = {memory.scope_key}
        accessible_teams = (
            await resolve_accessible_team_scope_keys(
                user_id=ctx.user_id,
                org_id=ctx.org_id,
                scopes=ctx.scopes,
            )
            if memory.memory_scope is MemoryScope.TEAM and ctx.user_id
            else None
        )
        return mcp_policy.authorize_memory_write_request(
            ctx=ctx,
            memory_scope=memory.memory_scope.value,
            scope_key=memory.scope_key,
            accessible_projects=policy_projects,
            surface="mcp_manage_correct_memory",
            accessible_teams=accessible_teams,
        )
    if normalized_action in MCP_PROJECT_ID_POLICY_ACTIONS:
        project_id = entity_id
    elif normalized_action in MCP_ENTITY_PROJECT_POLICY_ACTIONS:
        if not entity_id:
            return None
        project_id, target_metadata = await _mcp_entity_write_target(
            organization_id=ctx.org_id,
            entity_id=entity_id,
        )
        target_scope = str(target_metadata.get("memory_scope") or "").strip()
        if target_scope == MemoryScope.PRIVATE.value:
            # A private row answers to its owner, never to project membership.
            # The write policy authorizes a principal for their own private
            # space, so the owner comparison has to happen here: the target is
            # someone else's space, not the caller's.
            owner = str(
                target_metadata.get("principal_id") or target_metadata.get("scope_key") or ""
            )
            if not owner or not ctx.user_id or owner != str(ctx.user_id):
                return MemoryPolicyDecision(
                    action=MemoryPolicyAction.WRITE,
                    allowed=False,
                    reason="private_target_not_owned",
                    memory_scope=MemoryScope.PRIVATE,
                    scope_key=owner or None,
                )
            return mcp_policy.authorize_memory_write_request(
                ctx=ctx,
                memory_scope=MemoryScope.PRIVATE.value,
                scope_key=None,
                accessible_projects=accessible_projects,
                surface="mcp_manage",
            )
    else:
        return None

    policy_projects = (
        {project_id} if accessible_projects is None and project_id else accessible_projects
    )
    return mcp_policy.authorize_memory_write_request(
        ctx=ctx,
        memory_scope="project",
        scope_key=project_id,
        accessible_projects=policy_projects,
        surface="mcp_manage",
    )


# MCP transition action -> the unified work-item action. Routing these through
# the shared apps/api workflow service is what gives the MCP path the entity
# lock, WebSocket broadcast, and project-activity bump that REST already had and
# the old core manage() body silently skipped (audit H8).
_MCP_WORKFLOW_TRANSITIONS: dict[str, WorkItemAction] = {
    "start_task": WorkItemAction.START_TASK,
    "block_task": WorkItemAction.BLOCK_TASK,
    "unblock_task": WorkItemAction.UNBLOCK_TASK,
    "submit_review": WorkItemAction.SUBMIT_REVIEW,
    "complete_task": WorkItemAction.COMPLETE_TASK,
    "archive_task": WorkItemAction.ARCHIVE_TASK,
    "start_epic": WorkItemAction.START_EPIC,
    "complete_epic": WorkItemAction.COMPLETE_EPIC,
    "archive_epic": WorkItemAction.ARCHIVE_EPIC,
}


def _mcp_transition_message(action: str, *, learnings: str | None, reason: str | None) -> str:
    """Mirror core manage()'s per-action success message for MCP clients."""
    if action == "start_task":
        return "Task started"
    if action == "block_task":
        return f"Task blocked: {reason or 'No reason provided'}"
    if action == "unblock_task":
        return "Task unblocked, resuming work"
    if action == "submit_review":
        return "Task submitted for review"
    if action == "complete_task":
        return "Task completed" + (" with learnings captured" if learnings else "")
    if action == "archive_task":
        return "Task archived"
    if action == "start_epic":
        return "Epic started"
    if action == "complete_epic":
        return "Epic completed" + (" with learnings captured" if learnings else "")
    # archive_epic
    return "Epic archived" + (f": {reason}" if reason else "")


async def _manage_workflow_transition(
    *,
    ctx: mcp_context.McpContext,
    action: str,
    work_item_action: WorkItemAction,
    entity_id: str | None,
    data: dict[str, Any],
    policy_decision: MemoryPolicyDecision | None,
) -> dict[str, Any]:
    """Run an MCP task/epic transition through the shared workflow service.

    The lock, broadcast, and project-activity bump come from the service by
    construction. The response is shaped like core manage()'s ManageResponse so
    MCP clients see no change, including the deprecation pointer.
    """
    from sibyl.locks import LockAcquisitionError
    from sibyl.services.work_item_workflow import EPIC_TRANSITIONS, transition_work_item
    from sibyl_core.errors import EntityNotFoundError, InvalidTransitionError, RevisionConflictError
    from sibyl_core.tools.manage import _deprecation_notice

    def _response(
        *,
        success: bool,
        message: str,
        action_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "success": success,
            "action": action,
            "entity_id": entity_id,
            "message": message,
            "data": action_data or {},
        }
        deprecation = _deprecation_notice(action)
        if deprecation is not None:
            payload["data"].setdefault("deprecation", deprecation)
        if policy_decision is not None:
            payload["policy_reason"] = policy_decision.reason
        return payload

    if not entity_id:
        suffix = "epic actions" if work_item_action in EPIC_TRANSITIONS else "task actions"
        return _response(success=False, message=f"entity_id required for {suffix}")

    entity: Any | None = None
    if work_item_action in EPIC_TRANSITIONS:
        from sibyl_core.models.entities import EntityType
        from sibyl_core.services.graph import get_surreal_graph_runtime

        runtime = await get_surreal_graph_runtime(ctx.org_id)
        try:
            entity = await runtime.entity_manager.get(entity_id)
        except EntityNotFoundError:
            entity = None
        if not entity:
            return _response(success=False, message=f"Epic not found: {entity_id}")
        if entity.entity_type != EntityType.EPIC:
            return _response(success=False, message=f"Entity is not an epic: {entity_id}")

    learnings = data.get("learnings") if action in {"complete_task", "complete_epic"} else None
    reason = data.get("reason") if action in {"block_task", "archive_epic"} else None
    expected_revision = data.get("expected_revision")
    if expected_revision is not None and (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 1
    ):
        return _response(
            success=False,
            message="expected_revision must be an integer greater than zero",
        )

    try:
        transition_kwargs: dict[str, Any] = {"payload": data, "entity": entity}
        if expected_revision is not None:
            transition_kwargs["expected_revision"] = expected_revision
        result = await transition_work_item(
            ctx.org_id,
            entity_id,
            work_item_action,
            **transition_kwargs,
        )
    except InvalidTransitionError as exc:
        return _response(success=False, message=str(exc), action_data=exc.details)
    except LockAcquisitionError:
        return _response(
            success=False,
            message=f"{entity_id} is locked by another writer; retry shortly",
        )
    except (EntityNotFoundError, KeyError):
        return _response(success=False, message=f"Work item not found: {entity_id}")
    except RevisionConflictError as exc:
        return _response(success=False, message=str(exc), action_data=exc.details)

    # complete_task with learnings enqueues the same background learning jobs the
    # REST surface does, using the policy context the authz step resolved.
    response_data = dict(result.response_data)
    response_data["mutation_receipt"] = mcp_idempotency.mutation_receipt(
        data,
        applied=True,
        revision=result.revision,
        affected_records=[f"entity:{entity_id}"],
    )
    cited_ids = data.get("cited_ids") if action == "complete_task" else None
    if cited_ids:
        from sibyl_core.tools.usage_citation import record_cited_item_usages

        policy_context = policy_decision.policy_context if policy_decision else None
        policy_project_id = None
        if policy_context is not None:
            policy_project_id = policy_context.project_id or (
                policy_context.scope_key if policy_context.memory_space == "project" else None
            )
        try:
            response_data["citation_usage"] = await record_cited_item_usages(
                cited_ids,
                organization_id=ctx.org_id,
                principal_id=ctx.user_id,
                project_id=result.task_data.get("project_id") or policy_project_id,
                source_surface="mcp_manage_complete_task",
                request_metadata={
                    "action": action,
                    "has_learnings": bool(learnings),
                    "task_id": entity_id,
                },
            )
        except Exception as exc:
            log.warning(
                "mcp_complete_task_citation_usage_failed",
                task_id=entity_id,
                error_type=type(exc).__name__,
                exc_info=True,
            )
    if (
        action == "complete_task"
        and learnings
        and policy_decision is not None
        and policy_decision.policy_context is not None
    ):
        from sibyl.jobs.entities import serialize_memory_policy_context
        from sibyl.jobs.queue import (
            enqueue_create_learning_episode,
            enqueue_create_learning_procedure,
        )

        policy_payload = serialize_memory_policy_context(policy_decision.policy_context)
        episode_job_id = await enqueue_create_learning_episode(
            result.task_data, ctx.org_id, policy_context=policy_payload
        )
        procedure_job_id = await enqueue_create_learning_procedure(
            result.task_data, ctx.org_id, policy_context=policy_payload
        )
        response_data["learning_episode_job_id"] = episode_job_id
        response_data["learning_procedure_job_id"] = procedure_job_id

    return _response(
        success=True,
        message=_mcp_transition_message(action, learnings=learnings, reason=reason),
        action_data=response_data,
    )


async def _manage_memory_correction(
    *,
    ctx: mcp_context.McpContext,
    entity_id: str | None,
    data: dict[str, Any],
    accessible_projects: set[str] | None,
    policy_decision: MemoryPolicyDecision | None,
) -> dict[str, Any]:
    from sibyl_core.services.memory import apply_memory_correction

    if not entity_id:
        return {
            "success": False,
            "action": "correct_memory",
            "entity_id": None,
            "message": "entity_id (raw memory ID) required for correct_memory action",
            "data": {},
        }
    correction_action = data.get("action")
    if not isinstance(correction_action, str) or not correction_action.strip():
        return {
            "success": False,
            "action": "correct_memory",
            "entity_id": entity_id,
            "message": "data.action required for correct_memory action",
            "data": {},
        }
    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return {
            "success": False,
            "action": "correct_memory",
            "entity_id": entity_id,
            "message": "data.reason required for correct_memory action",
            "data": {},
        }
    expected_revision = data.get("expected_revision")
    if expected_revision is not None and (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 1
    ):
        return {
            "success": False,
            "action": "correct_memory",
            "entity_id": entity_id,
            "message": "expected_revision must be an integer greater than zero",
            "data": {},
        }
    memory = await get_raw_memory(organization_id=ctx.org_id, memory_id=entity_id)
    if memory is None:
        memory = await get_raw_memory_by_source_id(
            organization_id=ctx.org_id,
            source_id=entity_id,
        )
    if memory is None:
        return {
            "success": False,
            "action": "correct_memory",
            "entity_id": entity_id,
            "message": f"Memory source not found: {entity_id}",
            "data": {},
        }
    result = await apply_memory_correction(
        organization_id=ctx.org_id,
        source_id=memory.id,
        principal_id=ctx.user_id,
        action=correction_action,
        reason=reason.strip(),
        accessible_projects=(
            {memory.scope_key}
            if accessible_projects is None
            and memory.memory_scope is MemoryScope.PROJECT
            and memory.scope_key
            else accessible_projects
        ),
        accessible_teams=(
            policy_decision.policy_context.accessible_teams
            if policy_decision is not None and policy_decision.policy_context is not None
            else None
        ),
        replacement_source_id=(
            str(data["replacement_source_id"]) if data.get("replacement_source_id") else None
        ),
        duplicate_of_source_id=(
            str(data["duplicate_of_source_id"]) if data.get("duplicate_of_source_id") else None
        ),
        revised_content=(
            str(data["revised_content"]) if data.get("revised_content") is not None else None
        ),
        expected_revision=expected_revision,
    )
    revision = result.updated_memory.revision if result.updated_memory else None
    affected_records = (
        [f"raw_captures:{source_id}" for source_id in result.preview.affected_source_ids]
        if result.applied
        else []
    )
    response_data = {
        "allowed": result.preview.allowed,
        "applied": result.applied,
        "correction_action": result.preview.action,
        "reason": result.preview.reason,
        "revision": revision,
        "affected_source_ids": list(result.preview.affected_source_ids),
        "affected_derived_ids": list(result.preview.affected_derived_ids),
        "mutation_receipt": mcp_idempotency.mutation_receipt(
            data,
            applied=result.applied,
            revision=revision,
            affected_records=affected_records,
        ),
    }
    await log_memory_audit_event(
        action=result.preview.audit_action,
        user_id=ctx.user_id,
        organization_id=ctx.org_id,
        request=None,
        memory_scope=memory.memory_scope.value,
        scope_key=memory.scope_key,
        project_id=memory.project_id or memory.scope_key,
        source_surface="mcp_manage_correct_memory",
        source_ids=result.preview.affected_source_ids or [memory.id],
        derived_ids=result.preview.affected_derived_ids,
        policy_allowed=result.preview.allowed and result.applied,
        policy_reason=result.preview.reason,
        details={
            "action": result.preview.action,
            "applied": result.applied,
            "target_lifecycle_state": result.preview.target_lifecycle_state,
            "target_lifecycle_flags": result.preview.target_lifecycle_flags,
        },
    )
    payload = {
        "success": result.applied,
        "action": "correct_memory",
        "entity_id": entity_id,
        "message": (
            f"Memory correction applied: {result.preview.action}"
            if result.applied
            else f"Memory correction denied: {result.preview.reason}"
        ),
        "data": response_data,
    }
    if policy_decision is not None:
        payload["policy_reason"] = policy_decision.reason
    return payload


@mcp_idempotency.serialize_request("mcp/manage", action_scoped=True)
async def _manage_mcp_action(
    *,
    action: str,
    entity_id: str | None,
    data: dict[str, Any] | None,
) -> dict[str, Any]:
    from sibyl_core.tools.manage import manage

    ctx = await mcp_context.require_context(write=True)
    accessible_projects = await mcp_context.get_accessible_projects(ctx)
    policy_decision = await _authorize_mcp_manage_action(
        ctx=ctx,
        action=action,
        entity_id=entity_id,
        accessible_projects=accessible_projects,
    )
    # A denied decision has to stop the call, not annotate it. This ran before
    # the idempotency reservation deliberately: a refused action must not
    # strand a reservation row that a later legitimate retry would replay.
    if policy_decision is not None and not policy_decision.allowed:
        raise ValueError(policy_decision.reason)

    normalized_action = action.lower().strip()
    request_data = dict(data or {})
    idempotency_key = request_data.get("idempotency_key")
    if idempotency_key is not None and (
        not isinstance(idempotency_key, str)
        or not idempotency_key.strip()
        or len(idempotency_key.strip()) > 255
    ):
        return {
            "success": False,
            "action": normalized_action,
            "entity_id": entity_id,
            "message": "idempotency_key must be a non-empty string up to 255 characters",
            "data": {},
        }
    idempotency_key = idempotency_key.strip() if isinstance(idempotency_key, str) else None
    if idempotency_key is not None:
        request_data["idempotency_key"] = idempotency_key
    idempotency_path = f"mcp/manage/{normalized_action}"
    idempotency_payload = {"entity_id": entity_id, "data": dict(request_data)}
    principal_id = ctx.user_id or "unknown"
    idempotency_claim: ApiIdempotencyRecord | None = None
    if idempotency_key is not None:
        record, claimed = await reserve_idempotency_record(
            organization_id=UUID(ctx.org_id),
            principal_id=principal_id,
            idempotency_key=idempotency_key,
            method="MCP",
            path=idempotency_path,
            payload=idempotency_payload,
            content_session=None,
        )
        if not claimed:
            if record.request_hash != idempotency_request_hash(idempotency_payload):
                return {
                    "success": False,
                    "action": normalized_action,
                    "entity_id": entity_id,
                    "message": "idempotency_key was already used for a different request",
                    "data": {},
                }
            if idempotency_record_pending(record):
                # This branch runs under _serialize_mcp_idempotency's lock, so
                # no executor is live on this key: the reservation was
                # interrupted before its receipt completed. Adopt it and
                # re-execute; completion overwrites the same record id.
                log.warning(
                    "mcp_idempotency_interrupted_takeover",
                    path=idempotency_path,
                    organization_id=ctx.org_id,
                )
                idempotency_claim = record
            else:
                replayed = cast("dict[str, Any]", deepcopy(record.response_body))
                raw_response_data = replayed.get("data")
                if isinstance(raw_response_data, dict):
                    response_data = cast("dict[str, Any]", raw_response_data)
                    receipt = response_data.get("mutation_receipt")
                    if isinstance(receipt, dict):
                        response_data["mutation_receipt"] = {**receipt, "replayed": True}
                return replayed
        else:
            idempotency_claim = record

    full_data = request_data
    full_data["organization_id"] = ctx.org_id
    if ctx.user_id:
        full_data["user_id"] = ctx.user_id

    # Task/epic transitions route through the shared workflow service so the MCP
    # path gains locking, broadcasting, and project-activity by construction.
    # Everything else (update_task, add_note, crawl, analysis, ...) stays on the
    # core manage() dispatcher unchanged.
    work_item_action = _MCP_WORKFLOW_TRANSITIONS.get(normalized_action)
    if work_item_action is not None:
        payload = await _manage_workflow_transition(
            ctx=ctx,
            action=normalized_action,
            work_item_action=work_item_action,
            entity_id=entity_id,
            data=full_data,
            policy_decision=policy_decision,
        )
    elif normalized_action == "correct_memory":
        payload = await _manage_memory_correction(
            ctx=ctx,
            entity_id=entity_id,
            data=full_data,
            accessible_projects=accessible_projects,
            policy_decision=policy_decision,
        )
    else:
        result = await manage(
            action=action,
            entity_id=entity_id,
            data=full_data,
            organization_id=ctx.org_id,
            principal_id=ctx.user_id,
            accessible_projects=accessible_projects,
            allowed_memory_scope_keys=(
                set(ctx.api_key_memory_scope_keys)
                if ctx.api_key_memory_scope_keys is not None
                else None
            ),
        )
        payload = serialization.to_dict(result)
        if policy_decision is not None:
            payload["policy_reason"] = policy_decision.reason

    if idempotency_claim is not None:
        await complete_idempotency_record(
            idempotency_claim,
            response_status_code=200,
            response_body=payload,
            content_session=None,
        )
    return payload


def register_management_tools(mcp: MCPServer) -> None:
    """Register the unified state-management tool."""

    @mcp.tool()
    async def manage(
        action: str,
        entity_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Manage operations that modify state in the knowledge graph.

        The manage() tool handles all state-changing operations including task
        and epic workflow, source operations, and analysis.

        Task Workflow Actions:
            - start_task: Begin work on a task (sets status to 'doing')
            - block_task: Mark task as blocked (data.reason required)
            - unblock_task: Remove blocked status, resume work
            - submit_review: Submit for code review (sets status to 'review')
            - complete_task: Mark done (data.learnings optional)
              Include data.cited_ids to stamp memories that informed completion.
            - archive_task: Archive without completing
            - update_task: Update task fields (data contains updates)
            - add_note: Append a note to a task (data.content required)

        Epic Workflow Actions:
            - start_epic: Begin work on an epic
            - complete_epic: Mark an epic complete (data.learnings optional)
            - archive_epic: Archive an epic (data.reason optional)
            - update_epic: Update epic fields (data contains updates)

        Source Operations:
            - crawl: Trigger crawl of URL (data.url required, data.depth optional)
            - sync: Re-crawl existing source (entity_id = source ID)
            - refresh: Sync all sources
            - link_graph: Link document chunks to knowledge graph (entity_id = source ID, optional)
            - link_graph_status: Get status of pending graph linking
            - correct_memory: Correct a raw memory (entity_id = raw memory ID,
              data.action = wrong, stale, duplicate, superseded, or revise)

        Analysis Actions:
            - estimate: Estimate task effort from similar completed tasks
            - prioritize: Get smart task ordering for project
            - detect_cycles: Find circular dependencies in project
            - suggest: Get knowledge suggestions for a task

        Server health and graph statistics are exposed as the sibyl://health and
        sibyl://stats MCP resources, not as manage() actions.

        Args:
            action: Action to perform (see categories above)
            entity_id: Target entity ID (required for most actions)
            data: Action-specific data dict

        Returns:
            Result with success, action, entity_id, message, and data

        Examples:
            manage("start_task", entity_id="task-123")
            manage("complete_task", entity_id="task-123",
                   data={"learnings": "OAuth needs exact redirect URIs"})
            manage("crawl", data={"url": "https://docs.example.com", "depth": 3})
            manage("link_graph")  # Link all pending chunks
            manage("link_graph", entity_id="source-123")  # Link specific source
            manage("link_graph_status")  # Check pending work
            manage("correct_memory", entity_id="raw-123",
                   data={"action": "wrong", "reason": "Contradicted by source"})
            manage("estimate", entity_id="task-456")
            manage("add_note", entity_id="task-123", data={"content": "Root cause found"})
        """
        return await _manage_mcp_action(
            action=action,
            entity_id=entity_id,
            data=data,
        )
