"""Retire pending correction descendants of an already abstained ancestor.

Retirement does not validate a candidate for publication. Protected origins
establish the correction links, current authority establishes access, and the
canonical save fences every observed source and ancestor against concurrent
changes. Archived ancestors remain ineligible for ordinary validation.
"""

import json
from dataclasses import replace

from pydantic import TypeAdapter

from sibyl_core.memory_pipeline.observations import SourceKind
from sibyl_core.services.content_models import RawMemory
from sibyl_core.services.content_raw_persistence import get_raw_memory, save_raw_memory
from sibyl_core.services.memory_derivations import (
    load_raw_derivation,
    observation_from_record,
    raw_derivation_current,
)
from sibyl_core.services.memory_policy import _authorize_share_source_read
from sibyl_core.services.memory_source_validation import (
    SourceAuthorityResolver,
    SourceReadAuthority,
)
from sibyl_core.services.observed_sources import load_authorized_source_snapshot
from sibyl_core.services.source_observations import SourceUnavailableError
from sibyl_core.services.source_state_store import RawSourceSnapshot
from sibyl_core.services.validation_origin import load_validation_origin
from sibyl_core.tasks.reflection_correction import ReflectionCorrectionResult


def _accessible(memory: RawMemory, authority: SourceReadAuthority) -> bool:
    return (
        memory.principal_id == authority.principal_id
        and memory.deleted_at is None
        and memory.capture_surface == "reflection_candidate"
        and _authorize_share_source_read(
            memory=memory,
            principal_id=authority.principal_id,
            accessible_projects=authority.projects,
            accessible_teams=authority.teams,
            accessible_delegations=authority.delegations,
            allowed_memory_scope_keys=authority.scope_keys,
        ).allowed
    )


async def retire_abstained_correction_chain(
    organization_id: str,
    principal_id: str,
    candidate_id: str,
    resolver: SourceAuthorityResolver,
) -> tuple[str, ...]:
    """Repair verified pending descendants without reopening archived evidence."""
    authority = await resolver(organization_id, principal_id)
    if authority is None or authority.principal_id != principal_id:
        raise SourceUnavailableError()
    chain: list[RawMemory] = []
    sources: dict[str, RawMemory] = {}
    executions: list[str] = []
    seen: set[str] = set()
    while candidate_id not in seen:
        seen.add(candidate_id)
        memory = await get_raw_memory(organization_id=organization_id, memory_id=candidate_id)
        if memory is None or not _accessible(memory, authority):
            raise SourceUnavailableError()
        chain.append(memory)
        if memory.review_state == "archived":
            if memory.metadata.get("autonomy_outcome") != "abstained":
                return ()
            break
        if memory.review_state != "pending":
            return ()
        derivation = await load_raw_derivation(organization_id, memory.id)
        origin = await load_validation_origin(derivation)
        if origin is None:
            return ()
        result = json.loads(origin["result_json"])
        if result.get("version") != "ordinary-reflection-correction-v1":
            return ()
        correction = TypeAdapter(ReflectionCorrectionResult).validate_python(result)
        if correction.content != memory.raw_content or not await raw_derivation_current(
            memory, authority
        ):
            raise SourceUnavailableError()
        assert derivation is not None
        observations = derivation.get("observations")
        if not isinstance(observations, list):
            raise SourceUnavailableError()
        for value in observations:
            observation = observation_from_record(value)
            if observation.source.kind != SourceKind.RAW_CAPTURE:
                raise SourceUnavailableError()
            snapshot = await load_authorized_source_snapshot(
                observation.source, authority, organization_id=organization_id
            )
            if not isinstance(snapshot, RawSourceSnapshot) or not observation.same_evidence(
                snapshot.observation
            ):
                raise SourceUnavailableError()
            sources[snapshot.memory.id] = snapshot.memory
        executions.append(origin["uuid"])
        candidate_id = origin["parent_id"]
    else:
        raise SourceUnavailableError()
    retired: list[str] = []
    for index, memory in enumerate(chain[:-1]):
        current_authority = await resolver(organization_id, principal_id)
        if current_authority != authority or not await raw_derivation_current(memory, authority):
            raise SourceUnavailableError()
        await save_raw_memory(
            replace(
                memory,
                review_state="archived",
                metadata={
                    **memory.metadata,
                    "review_state": "archived",
                    "autonomy_outcome": "abstained",
                    "autonomy_recommended_action": "abstain",
                    "automatic_validation_executions": executions,
                    "archive_reason": "ancestor_abstained",
                },
            ),
            expected_revision=memory.revision,
            source_observations=[*chain[index:], *sources.values()],
        )
        retired.append(memory.id)
    return tuple(retired)
