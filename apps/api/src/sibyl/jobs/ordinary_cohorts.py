"""Scheduled ordinary cohorts use current memberships and existing stage owners."""

from collections import defaultdict

from sibyl.jobs.lifecycle_repair import resolve_source_authority
from sibyl.persistence.auth_runtime import (
    resolve_accessible_project_graph_ids,
    resolve_auth_context,
)
from sibyl_core.auth import OrganizationRole, ProjectRole
from sibyl_core.services.content_models import RawMemory
from sibyl_core.services.ordinary_cohort import partition_stored_cohort, propose_stored_cohort
from sibyl_core.services.source_observations import SourceUnavailableError


async def writable_source_authority(org: str, principal: str):
    """Resolve read authority plus writable project coverage on every provider send."""
    context = await resolve_auth_context(claims={"sub": principal, "org": org})
    if context.org_role not in {
        OrganizationRole.OWNER,
        OrganizationRole.ADMIN,
        OrganizationRole.MEMBER,
    }:
        raise SourceUnavailableError
    authority = await resolve_source_authority(org, principal)
    if authority is None:
        raise SourceUnavailableError
    writable = await resolve_accessible_project_graph_ids(
        user_id=principal, org_id=org, required_role=ProjectRole.CONTRIBUTOR
    )
    # Sources in a project require contributor access. Private sources retain
    # their principal ceiling and need no unrelated project membership.
    from dataclasses import replace

    return replace(authority, projects=authority.projects.intersection(writable))


async def reflect_cohorts(org: str, sources: list[RawMemory], *, dry_run: bool):
    groups = defaultdict(list)
    for source in sources:
        groups[
            (source.principal_id, source.memory_scope, source.scope_key, source.project_id)
        ].append(source)
    results = []
    consumed = set()
    for (principal, _, _, _), members in groups.items():
        if not principal or len(members) < 2:
            continue
        identifiers = sorted(s.id for s in members)
        if dry_run:
            results.append(
                {"source_ids": identifiers, "outcome": "cohort_preview", "dry_run": True}
            )
            consumed.update(identifiers)
            continue
        try:
            bins = await partition_stored_cohort(
                org, principal, identifiers, writable_source_authority
            )
        except Exception as exc:
            consumed.update(identifiers)
            results.append(
                {
                    "source_ids": identifiers,
                    "outcome": "error",
                    "reason": str(exc),
                    "stage_kind": "ordinary_cohort_preparation",
                }
            )
            continue
        for identifiers in bins:
            if len(identifiers) < 2:
                results.append(
                    {
                        "source_ids": identifiers,
                        "outcome": "individual_fallback",
                        "reason": "no_complete_cohort_fits_input_budget",
                    }
                )
                continue
            consumed.update(identifiers)

            async def authorize(principal=principal):
                await writable_source_authority(org, principal)

            try:
                candidate, execution = await propose_stored_cohort(
                    org, principal, identifiers, writable_source_authority, authorize=authorize
                )
                results.append(
                    {
                        "source_ids": identifiers,
                        "outcome": "reflected",
                        "operation_id": execution,
                        "candidate_count": int(candidate is not None),
                        "candidate_ids": [candidate.id] if candidate else [],
                        "stage_kind": "ordinary_cohort",
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "source_ids": identifiers,
                        "outcome": "error",
                        "reason": str(exc),
                        "stage_kind": "ordinary_cohort",
                    }
                )
    return results, consumed
