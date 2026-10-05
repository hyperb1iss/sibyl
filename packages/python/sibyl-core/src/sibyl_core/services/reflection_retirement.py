"""Retire pending correction descendants of an already abstained ancestor.

Terminal records leave candidate bytes intact because a published descendant
may retain them as evidence. Protected origins establish historical correction
links; current authority and a source-state witness guard terminal writes.
Archived ancestors remain ineligible for ordinary validation.
"""

import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Literal

from pydantic import TypeAdapter

from sibyl_core.backends.surreal.schema_source_witness import SOURCE_STATE_WRITE_WITNESS
from sibyl_core.memory_pipeline.observations import SourceKind
from sibyl_core.services.content_models import RawMemory, raw_memory_from_record
from sibyl_core.services.content_raw_persistence import get_raw_memory
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
from sibyl_core.services.validation_execution import _query
from sibyl_core.services.validation_origin import load_validation_origin
from sibyl_core.tasks.reflection_correction import ReflectionCorrectionResult

_RETIREMENT_SNAPSHOT = """
LET $snapshot = {
    captures: (SELECT * FROM raw_captures WHERE organization_id=$org AND uuid IN $source_ids ORDER BY uuid),
    states: (SELECT * OMIT validation_write_witness FROM source_states WHERE organization_id=$org AND source_kind='raw_capture' AND source_id IN $source_ids ORDER BY source_id),
    derivations: (SELECT * FROM memory_derivations WHERE organization_id=$org AND target_kind='raw_capture' AND target_id IN $source_ids ORDER BY target_id),
    retirements: (SELECT * FROM reflection_supersessions WHERE organization_id=$org AND draft_id IN $source_ids ORDER BY draft_id)
};
LET $snapshot_digest = crypto::sha256(type::string($snapshot));
"""


async def abstained_reflection_frontier(
    organization_id: str, principal_id: str, candidate_id: str
) -> str | None:
    """Look up one terminal decision without walking the organization's ledger."""
    rows = await _query(
        "SELECT principal_id, archive_reason, superseded_by_candidate_id FROM reflection_supersessions "
        "WHERE organization_id=$org AND draft_id=$draft LIMIT 1;",
        org=organization_id,
        draft=candidate_id,
    )
    if (
        not rows
        or rows[0]["principal_id"] != principal_id
        or rows[0]["archive_reason"] not in {"abstained", "ancestor_abstained"}
    ):
        return None
    frontier = rows[0]["superseded_by_candidate_id"]
    if not isinstance(frontier, str) or not frontier:
        raise SourceUnavailableError()
    return frontier


async def record_abstained_reflection(
    memory: RawMemory,
    *,
    frontier_id: str,
    reason: Literal["abstained", "ancestor_abstained"],
    observations: Sequence[RawMemory],
    authorize: Callable[[], Awaitable[None]],
) -> None:
    """Record terminal state beside an exact, freshly authorized source cut."""
    observed = {item.id: item for item in (memory, *observations)}
    if any(item.organization_id != memory.organization_id for item in observed.values()):
        raise SourceUnavailableError()
    params = {"org": memory.organization_id, "source_ids": sorted(observed)}
    rows = await _query(
        "RETURN {" + _RETIREMENT_SNAPSHOT + "RETURN {token:$snapshot_digest, data:$snapshot}; };",
        **params,
    )
    if len(rows) != 1:
        raise SourceUnavailableError()
    snapshot = rows[0]["data"]
    captures = {row["uuid"]: raw_memory_from_record(row) for row in snapshot["captures"]}
    if captures != observed or {row["source_id"] for row in snapshot["states"]} != set(observed):
        raise SourceUnavailableError()
    await authorize()
    try:
        await _query(
            "RETURN {"
            + _RETIREMENT_SNAPSHOT
            + "IF $snapshot_digest != $expected { THROW 'reflection_retirement_source_changed'; };"
            + "LET $standing=(SELECT VALUE id FROM reflection_supersessions "
            "WHERE organization_id=$org AND draft_id=$draft LIMIT 1);"
            "IF array::len($standing)>0 { RETURN false; };"
            "LET $source_states_to_fence=$snapshot.states;"
            + SOURCE_STATE_WRITE_WITNESS
            + "CREATE reflection_supersessions SET organization_id=$org, principal_id=$principal, "
            "draft_id=$draft, superseded_by_candidate_id=$frontier, promoted_entity_id=NONE, "
            "archive_reason=$reason, archived_at=time::now(); RETURN true; };",
            **params,
            expected=rows[0]["token"],
            principal=memory.principal_id,
            draft=memory.id,
            frontier=frontier_id,
            reason=reason,
        )
    except Exception as exc:
        if "reflection_supersession_draft" in str(exc):
            return
        if "reflection_retirement_source_changed" in str(exc):
            raise SourceUnavailableError() from exc
        raise


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
    *,
    expected_root: str | None = None,
) -> tuple[str, ...]:
    """Repair verified pending descendants without reopening archived evidence."""
    authority = await resolver(organization_id, principal_id)
    if authority is None or authority.principal_id != principal_id:
        raise SourceUnavailableError()
    chain: list[RawMemory] = []
    sources: dict[str, RawMemory] = {}
    seen: set[str] = set()
    while candidate_id not in seen:
        seen.add(candidate_id)
        memory = await get_raw_memory(organization_id=organization_id, memory_id=candidate_id)
        if memory is None or not _accessible(memory, authority):
            raise SourceUnavailableError()
        chain.append(memory)
        if await abstained_reflection_frontier(
            organization_id, principal_id, memory.id
        ) is not None and (expected_root is None or memory.id == expected_root):
            break
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
        candidate_id = origin["parent_id"]
    else:
        raise SourceUnavailableError()
    if expected_root is not None and chain[-1].id != expected_root:
        raise SourceUnavailableError()
    retired: list[str] = []
    for memory in chain[:-1]:

        async def authorize(memory: RawMemory = memory) -> None:
            current_authority = await resolver(organization_id, principal_id)
            if current_authority != authority or not await raw_derivation_current(
                memory, authority
            ):
                raise SourceUnavailableError()

        await record_abstained_reflection(
            memory,
            frontier_id=chain[-1].id,
            reason="ancestor_abstained",
            observations=[*chain, *sources.values()],
            authorize=authorize,
        )
        retired.append(memory.id)
    return tuple(retired or [candidate_id])
