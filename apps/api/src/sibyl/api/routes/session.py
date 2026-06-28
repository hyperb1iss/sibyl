"""Session bundle endpoints."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query

from sibyl.api.schemas import SessionBundleContext, SessionBundleResponse
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl.auth.errors import ProjectAccessDeniedError
from sibyl.persistence.auth_runtime import list_accessible_project_graph_ids
from sibyl.services.recall_limits import (
    RecallConcurrencyLimitExceededError,
    recall_concurrency_slot,
)
from sibyl_core.auth import AuthOrganization, OrganizationRole
from sibyl_core.services.surreal_content import recall_raw_memory
from sibyl_core.session_bundle import (
    derive_query,
    memory_dedupe_keys,
    remember_next,
    summarize_memory,
    summarize_raw_memory,
    summarize_task,
)

log = structlog.get_logger()

_READ_ROLES = (
    OrganizationRole.OWNER,
    OrganizationRole.ADMIN,
    OrganizationRole.MEMBER,
    OrganizationRole.VIEWER,
)
_SESSION_MEMORY_TYPES = [
    "decision",
    "plan",
    "idea",
    "claim",
    "artifact",
    "session",
    "pattern",
    "procedure",
    "rule",
    "template",
    "episode",
    "domain",
    "document",
]

router = APIRouter(
    prefix="/session",
    tags=["session"],
    dependencies=[Depends(require_org_role(*_READ_ROLES))],
)


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if is_dataclass(value):
        return asdict(value)
    return dict(getattr(value, "__dict__", {}))


def _append_unique_memory(
    memories: list[dict[str, Any]], memory: dict[str, Any], limit: int
) -> None:
    if len(memories) >= limit:
        return
    candidate_keys = memory_dedupe_keys(memory)
    if candidate_keys and any(
        candidate_keys & memory_dedupe_keys(existing) for existing in memories
    ):
        return
    memories.append(memory)


async def _append_raw_memories(
    *,
    memories: list[dict[str, Any]],
    query: str,
    organization_id: str,
    principal_id: str | None,
    organization_role: OrganizationRole | str | None,
    selected_project_ids: list[str],
    limit: int,
) -> None:
    if not principal_id or limit <= 0:
        return

    scope_requests: list[tuple[str, str | None]] = [("private", None)]
    scope_requests.extend(("project", project_id) for project_id in selected_project_ids)

    async with recall_concurrency_slot(
        organization_id=organization_id,
        user_id=principal_id,
        organization_role=organization_role,
    ):
        for memory_scope, scope_key in scope_requests:
            remaining = limit - len(memories)
            if remaining <= 0:
                break
            try:
                raw_memories = await recall_raw_memory(
                    organization_id=organization_id,
                    principal_id=principal_id,
                    query=query,
                    memory_scope=memory_scope,
                    scope_key=scope_key,
                    limit=remaining,
                )
            except Exception as exc:
                log.warning(
                    "session_raw_memory_lookup_failed",
                    memory_scope=memory_scope,
                    scope_key=scope_key,
                    error=str(exc),
                )
                continue

            for raw_memory in raw_memories:
                _append_unique_memory(
                    memories,
                    summarize_raw_memory(_as_mapping(raw_memory)),
                    limit,
                )


@router.get("/bundle", response_model=SessionBundleResponse)
async def get_session_bundle(
    query: str | None = Query(default=None, description="Optional focus query"),
    task_limit: int = Query(default=5, ge=1, le=20, description="Maximum active tasks to include"),
    memory_limit: int = Query(
        default=3,
        ge=0,
        le=20,
        description="Maximum relevant memories to include",
    ),
    project_ids: list[str] | None = Query(
        default=None,
        description="Optional project scope from the current web context",
    ),
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> SessionBundleResponse:
    """Package wake-up context for the current org and optional project scope."""
    from sibyl_core.tools.core import explore as core_explore, search as core_search

    try:
        accessible_projects = await list_accessible_project_graph_ids(ctx)
        selected_project_ids = [project_id for project_id in (project_ids or []) if project_id]

        invalid_project_id = next(
            (
                project_id
                for project_id in selected_project_ids
                if project_id not in accessible_projects
            ),
            None,
        )
        if invalid_project_id:
            raise ProjectAccessDeniedError(project_id=invalid_project_id, required_role="viewer")

        scope = "project_selection" if selected_project_ids else "all_projects"
        organization_id = str(org.id)

        task_result = await core_explore(
            mode="list",
            types=["task"],
            entity_id=None,
            relationship_types=None,
            depth=1,
            language=None,
            category=None,
            project=None,
            project_ids=selected_project_ids or None,
            accessible_projects=accessible_projects if not selected_project_ids else None,
            epic=None,
            no_epic=False,
            status="doing,blocked",
            priority=None,
            complexity=None,
            feature=None,
            tags=None,
            include_archived=False,
            limit=task_limit,
            offset=0,
            organization_id=organization_id,
        )
        tasks = [summarize_task(_as_mapping(task)) for task in task_result.entities][:task_limit]

        effective_query = derive_query(query, tasks, project_name=None)
        relevant_entities: list[dict[str, Any]] = []
        if effective_query and memory_limit > 0:
            api_key_memory_scope_keys = getattr(ctx, "api_key_memory_scope_keys", None)
            await _append_raw_memories(
                memories=relevant_entities,
                query=effective_query,
                organization_id=organization_id,
                principal_id=getattr(ctx, "user_id", None),
                organization_role=getattr(ctx, "org_role", None),
                selected_project_ids=selected_project_ids,
                limit=memory_limit,
            )
            single_project_id = selected_project_ids[0] if len(selected_project_ids) == 1 else None
            search_scope = (
                None if single_project_id else (selected_project_ids or accessible_projects)
            )
            if len(relevant_entities) < memory_limit:
                search_result = await core_search(
                    query=effective_query,
                    types=_SESSION_MEMORY_TYPES,
                    language=None,
                    category=None,
                    status=None,
                    project=single_project_id,
                    accessible_projects=search_scope,
                    source=None,
                    source_id=None,
                    source_name=None,
                    assignee=None,
                    since=None,
                    limit=memory_limit + len(tasks),
                    offset=0,
                    include_content=True,
                    include_documents=True,
                    include_graph=True,
                    use_enhanced=True,
                    boost_recent=True,
                    organization_id=organization_id,
                    principal_id=getattr(ctx, "user_id", None),
                    allowed_memory_scope_keys=(
                        set(api_key_memory_scope_keys)
                        if api_key_memory_scope_keys is not None
                        else None
                    ),
                )
                task_ids = {task["id"] for task in tasks}
                for result in search_result.results:
                    if len(relevant_entities) >= memory_limit:
                        break
                    candidate = _as_mapping(result)
                    if candidate.get("id") in task_ids:
                        continue
                    _append_unique_memory(
                        relevant_entities, summarize_memory(candidate), memory_limit
                    )

        return SessionBundleResponse(
            context=SessionBundleContext(
                org_slug=getattr(org, "slug", None),
                project_ids=selected_project_ids,
                scope=scope,
            ),
            query=effective_query,
            tasks=tasks,
            relevant_entities=relevant_entities,
            remember_next=remember_next(tasks, relevant_entities, has_project=True),
        )
    except HTTPException:
        raise
    except RecallConcurrencyLimitExceededError as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "recall_concurrency_limit_exceeded",
                "max_concurrent": exc.max_concurrent,
            },
        ) from exc
    except ProjectAccessDeniedError:
        raise
    except Exception as exc:
        log.exception("session_bundle_failed", error=str(exc))
        raise HTTPException(
            status_code=500,
            detail="Session bundle failed. Please try again.",
        ) from exc
