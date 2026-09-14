"""Scheduled ordinary cohorts use current memberships and existing stage owners."""

from collections import defaultdict

from sibyl.jobs.lifecycle_repair import resolve_source_authority
from sibyl.persistence.auth_runtime import (
    resolve_accessible_project_graph_ids,
    resolve_auth_context,
)
from sibyl_core.auth import OrganizationRole, ProjectRole
from sibyl_core.services.content_models import RawMemory
from sibyl_core.services.ordinary_cohort import (
    partition_stored_cohort,
    prepare_stored_source_packets,
    propose_stored_cohort,
)
from sibyl_core.services.source_observations import SourceUnavailableError
from sibyl_core.tasks.episode_evidence import is_controller_episode


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
        if not principal:
            continue
        if len(members) == 1 and not is_controller_episode(members[0].raw_content.encode()):
            continue
        identifiers = sorted(s.id for s in members)
        if dry_run:
            results.append(
                {"source_ids": identifiers, "outcome": "cohort_preview", "dry_run": True}
            )
            consumed.update(identifiers)
            continue
        try:
            bins = (
                await partition_stored_cohort(
                    org, principal, identifiers, writable_source_authority
                )
                if len(identifiers) > 1
                else [identifiers]
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
                source = next(member for member in members if member.id == identifiers[0])
                if is_controller_episode(source.raw_content.encode()):
                    consumed.add(source.id)
                    results.append(await _reflect_packet_source(org, principal, source.id))
                    continue
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


async def _reflect_packet_source(org: str, principal: str, source_id: str):
    """Source completion is all-page proposal coverage, independent of publication."""
    result = {
        "source_ids": [source_id],
        "stage_kind": "ordinary_packet_manifest",
        "independent_source_count": 1,
        "source_pass_complete": False,
        "pages": [],
        "candidate_ids": [],
        "candidate_count": 0,
    }
    try:
        packets = await prepare_stored_source_packets(
            org, principal, source_id, writable_source_authority
        )
    except Exception as exc:
        return {**result, "outcome": "error", "reason": str(exc)}
    result["manifest"] = packets[0].binding["manifest"]
    result["packet_count"] = len(packets)

    async def authorize():
        await writable_source_authority(org, principal)

    for packet in packets:
        page = {"packet_index": packet.binding["index"], "packet_sha256": packet.sha256}
        try:
            candidate, execution = await propose_stored_cohort(
                org,
                principal,
                [source_id],
                writable_source_authority,
                authorize=authorize,
                packet_binding=packet.binding,
            )
            page.update(
                outcome="returned" if candidate else "abstained",
                operation_id=execution,
                candidate_ids=[candidate.id] if candidate else [],
            )
            if candidate:
                result["candidate_ids"].append(candidate.id)
        except Exception as exc:
            state = getattr(exc, "execution_state", None)
            page.update(
                outcome="pending" if state in {"running", "recorded", "returned"} else "failed",
                reason=str(exc),
                execution_state=state,
            )
            if execution_id := getattr(exc, "execution_id", None):
                page["operation_id"] = execution_id
        result["pages"].append(page)
    result["candidate_count"] = len(result["candidate_ids"])
    result["source_pass_complete"] = all(
        page["outcome"] in {"returned", "abstained"} for page in result["pages"]
    )
    result["outcome"] = "reflected" if result["source_pass_complete"] else "error"
    return result
