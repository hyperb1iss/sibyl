"""Scheduled recovery of captures and graph rows awaiting source checks."""

import asyncio
from dataclasses import asdict
from typing import Any

import structlog

from sibyl.persistence.auth_common import InvalidAuthClaimsError, UserNotFoundError
from sibyl.persistence.auth_runtime import (
    list_accessible_delegated_scope_keys,
    list_accessible_project_graph_ids,
    list_accessible_team_scope_keys,
    resolve_auth_context,
)
from sibyl.persistence.organization_runtime import list_org_ids
from sibyl_core.projection.repair import repair_graph_lifecycle
from sibyl_core.services.graph_runtime import background_graph_runtime
from sibyl_core.services.memory_source_validation import (
    SourceReadAuthority,
    repair_raw_source_lifecycle,
)

log = structlog.get_logger()


async def resolve_source_authority(
    organization_id: str, principal_id: str
) -> SourceReadAuthority | None:
    """Continue an accepted capture using current membership and its saved ceiling."""
    try:
        context = await resolve_auth_context(claims={"sub": principal_id, "org": organization_id})
    except (InvalidAuthClaimsError, UserNotFoundError):
        return None
    if (
        context.user_id != principal_id
        or context.organization_id != organization_id
        or context.org_role is None
    ):
        return None
    projects, teams, delegations = await asyncio.gather(
        list_accessible_project_graph_ids(context),
        list_accessible_team_scope_keys(context),
        list_accessible_delegated_scope_keys(context),
    )
    return SourceReadAuthority(
        principal_id=principal_id,
        projects=frozenset(projects),
        teams=frozenset(teams),
        delegations=frozenset(delegations),
    )


async def _repair_graph(organization_id: str):
    async with background_graph_runtime(organization_id) as runtime:
        return await repair_graph_lifecycle(runtime)


async def repair_lifecycle_all_orgs(ctx: dict[str, Any]) -> dict[str, int]:  # noqa: ARG001
    summary = {
        "organizations": 0,
        "failed_organizations": 0,
        "checked": 0,
        "recovered": 0,
        "pending": 0,
        "failed": 0,
    }
    for organization_id in await list_org_ids():
        summary["organizations"] += 1
        results = await asyncio.gather(
            _repair_graph(organization_id),
            repair_raw_source_lifecycle(
                organization_id, authority_resolver=resolve_source_authority
            ),
            return_exceptions=True,
        )
        if any(isinstance(result, BaseException) for result in results):
            summary["failed_organizations"] += 1
        for result in results:
            if isinstance(result, BaseException):
                log.warning(
                    "lifecycle_repair_org_failed",
                    group_id=organization_id,
                    error_type=type(result).__name__,
                )
            else:
                for key, value in asdict(result).items():
                    summary[key] += value
    log.info("lifecycle_repair_completed", **summary)
    return summary
