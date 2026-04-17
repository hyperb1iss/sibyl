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
from sibyl.db.models import Organization, OrganizationRole
from sibyl.persistence.legacy.auth import list_legacy_accessible_project_graph_ids
from sibyl_core.session_bundle import derive_query, remember_next, summarize_memory, summarize_task

log = structlog.get_logger()

_READ_ROLES = (
    OrganizationRole.OWNER,
    OrganizationRole.ADMIN,
    OrganizationRole.MEMBER,
    OrganizationRole.VIEWER,
)
_SESSION_MEMORY_TYPES = ["pattern", "procedure", "rule", "template", "episode", "document"]

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
    org: Organization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
) -> SessionBundleResponse:
    """Package wake-up context for the current org and optional project scope."""
    from sibyl_core.tools.core import explore as core_explore, search as core_search

    try:
        accessible_projects = await list_legacy_accessible_project_graph_ids(ctx)
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
            single_project_id = selected_project_ids[0] if len(selected_project_ids) == 1 else None
            search_scope = (
                None if single_project_id else (selected_project_ids or accessible_projects)
            )
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
            )
            task_ids = {task["id"] for task in tasks}
            for result in search_result.results:
                candidate = _as_mapping(result)
                if candidate.get("id") in task_ids:
                    continue
                relevant_entities.append(summarize_memory(candidate))
                if len(relevant_entities) >= memory_limit:
                    break

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
    except ProjectAccessDeniedError:
        raise
    except Exception as exc:
        log.exception("session_bundle_failed", error=str(exc))
        raise HTTPException(
            status_code=500,
            detail="Session bundle failed. Please try again.",
        ) from exc
