"""Propose ordinary memories from one authorized, durable cohort observation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import TypeAdapter
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.models import Model

from sibyl_core.ai.llm.extractor import Extractor
from sibyl_core.backends.surreal.schema_source_witness import SOURCE_STATE_WRITE_WITNESS
from sibyl_core.config import settings
from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind
from sibyl_core.services.content_models import RawMemory
from sibyl_core.services.content_raw_persistence import (
    get_raw_memory,
    remember_reflection_candidate_review,
)
from sibyl_core.services.memory_source_validation import (
    SourceAuthorityResolver,
    SourceReadAuthority,
)
from sibyl_core.services.observed_sources import load_authorized_source_snapshot
from sibyl_core.services.source_observations import SourceUnavailableError
from sibyl_core.services.source_state_store import RawSourceSnapshot
from sibyl_core.services.validation_candidate import ValidationCandidateWrite
from sibyl_core.services.validation_execution import ValidationExecution, _query
from sibyl_core.services.validation_stages import run_validation_stage
from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.consolidation import ConsolidationInputBudgetExceeded
from sibyl_core.tasks.ordinary_evidence import OrdinarySource
from sibyl_core.tasks.ordinary_proposal_result import OrdinaryProposalResult
from sibyl_core.tasks.ordinary_proposals import (
    VERSION,
    PartialCohort,
    PartialEpisode,
    PartialProposal,
    PreparedPartialProposal,
    prepare_partial_proposal,
)
from sibyl_core.tasks.procedure_review import review_digest

COHORT_SNAPSHOT = """
LET $captures=(SELECT * OMIT embedding, retrieval_count, citation_count, misled_count, last_recalled_at, last_used_at, metadata.embedding_metadata FROM raw_captures WHERE organization_id=$org AND uuid IN $source_ids ORDER BY uuid);
LET $states=(SELECT * OMIT validation_write_witness FROM source_states WHERE organization_id=$org
    AND source_kind='raw_capture' AND source_id IN $source_ids ORDER BY source_id);
LET $snapshot={captures:$captures,states:$states};
LET $snapshot_digest=crypto::sha256(type::string($snapshot));
"""
COHORT_GUARD = (
    COHORT_SNAPSHOT
    + "IF $snapshot_digest!=$expected { THROW 'Ordinary cohort sources changed'; };"
    + "LET $source_states_to_fence=$states;"
    + SOURCE_STATE_WRITE_WITNESS
)


@dataclass(frozen=True)
class PreparedCohort:
    prepared: PreparedPartialProposal
    sources: tuple[RawSourceSnapshot, ...]
    snapshot_sha256: str
    authority: SourceReadAuthority

    @property
    def ids(self) -> list[str]:
        return [source.memory.id for source in self.sources]

    @property
    def bindings(self) -> list[dict[str, object]]:
        return [
            {
                "source_id": s.memory.id,
                "incarnation": s.observation.effective_incarnation,
                "generation": s.observation.generation,
            }
            for s in self.sources
        ]


async def prepare_stored_cohort(
    org: str,
    principal: str,
    source_ids: list[str],
    resolver: SourceAuthorityResolver,
) -> PreparedCohort:
    """Resolve actual retained sources; project grouping never proves environment facts."""
    ids = sorted(source_ids)
    if len(ids) < 2 or len(ids) != len(set(ids)):
        raise ValueError("An ordinary cohort requires distinct retained sources")
    authority = await resolver(org, principal)
    if authority is None or authority.principal_id != principal:
        raise SourceUnavailableError()
    rows = await _query(
        "RETURN {" + COHORT_SNAPSHOT + "RETURN {snapshot:$snapshot,token:$snapshot_digest}; };",
        org=org,
        source_ids=ids,
    )
    if len(rows) != 1:
        raise SourceUnavailableError()
    data = rows[0]["snapshot"]
    captures = {r["uuid"]: r for r in data["captures"]}
    states = {r["source_id"]: r for r in data["states"]}
    if set(captures) != set(ids) or set(states) != set(ids):
        raise SourceUnavailableError()
    sources = []
    episodes = []
    scope = None
    for identifier in ids:
        source = await load_authorized_source_snapshot(
            SourceIdentity(org, SourceKind.RAW_CAPTURE, identifier), authority, organization_id=org
        )
        if not isinstance(source, RawSourceSnapshot):
            raise SourceUnavailableError()
        memory = source.memory
        state = states[identifier]
        identity = (memory.memory_scope, memory.scope_key, memory.project_id)
        if memory.principal_id != principal or (scope is not None and identity != scope):
            raise SourceUnavailableError()
        scope = identity
        if (
            memory.raw_content != captures[identifier]["raw_content"]
            or memory.revision != captures[identifier]["revision"]
            or source.observation.effective_incarnation != state["incarnation"]
            or source.observation.generation != state["generation"]
        ):
            raise SourceUnavailableError()
        if memory.capture_surface in {"reflection_candidate", "reflection_source"} or (
            memory.entity_type in {"procedure", "pattern"} and memory.derivation_required
        ):
            raise SourceUnavailableError()
        artifact = memory.raw_content.encode("utf-8")
        episodes.append(
            PartialEpisode(
                episode_id=identifier,
                artifact=artifact,
                source=OrdinarySource(
                    source_id=identifier,
                    incarnation=source.observation.effective_incarnation,
                    generation=source.observation.generation,
                    observed_revision=memory.revision,
                    content_sha256=hashlib.sha256(artifact).hexdigest(),
                ),
            )
        )
        sources.append(source)
    assert scope is not None
    group = PartialCohort(
        group_id=review_digest(ids),
        mechanism="Compare ordinary retained observations",
        organization_id=org,
        owner_principal_id=principal,
        memory_scope=scope[0],
        scope_key=scope[1],
        episodes=tuple(episodes),
    )
    return PreparedCohort(
        prepare_partial_proposal(group), tuple(sources), rows[0]["token"], authority
    )


async def propose_stored_cohort(
    org: str,
    principal: str,
    source_ids: list[str],
    resolver: SourceAuthorityResolver,
    *,
    authorize: Callable[[], Awaitable[None]],
) -> tuple[RawMemory | None, str]:
    """Reuse completed results without redispatch, then persist their protected derivation."""
    from sibyl_core.services.procedure_validation import (
        _close_resources,
        _OwnedValidationExtractor,
        validation_extractor,
    )

    await authorize()
    original = await prepare_stored_cohort(org, principal, source_ids, resolver)
    if len(original.prepared.prompt) > settings.consolidation_max_input_chars:
        raise ConsolidationInputBudgetExceeded(
            len(original.prepared.prompt), settings.consolidation_max_input_chars
        )
    owned, policy = await validation_extractor()
    try:
        extractor = await _proposal_extractor(owned, original.prepared.system)
        schema = await extractor.output_schema()
        policy = canonical({**json.loads(policy), "version": VERSION, "schema": schema})
        actual = (
            len(original.prepared.system) + len(original.prepared.prompt) + len(canonical(schema))
        )
        if actual > settings.consolidation_max_input_chars:
            raise ConsolidationInputBudgetExceeded(actual, settings.consolidation_max_input_chars)
        return await _run_cohort(org, principal, original, resolver, extractor, policy, authorize)
    finally:
        if isinstance(owned, _OwnedValidationExtractor):
            await _close_resources(owned.resources)


async def _run_cohort(org, principal, original, resolver, extractor, policy, authorize):
    async def current():
        await authorize()
        refreshed = await prepare_stored_cohort(org, principal, original.ids, resolver)
        if (
            refreshed.snapshot_sha256 != original.snapshot_sha256
            or refreshed.prepared != original.prepared
        ):
            raise SourceUnavailableError()

    request = {
        "kind": VERSION,
        "org": org,
        "principal": principal,
        "parent": original.ids[0],
        "source_bindings": original.bindings,
        "snapshot": original.snapshot_sha256,
        "input": original.prepared.input_sha256,
        "policy": policy,
    }
    identity = review_digest(request)
    params = {
        "org": org,
        "principal": principal,
        "source_ids": original.ids,
        "expected": original.snapshot_sha256,
    }
    execution = ValidationExecution(
        identity,
        org,
        principal,
        authorize=current,
        dispatch_guard=COHORT_GUARD,
        guard_params={k: v for k, v in params.items() if k not in {"org", "principal"}},
    )

    async def run():
        result = await extractor.extract_with_usage(original.prepared.prompt)
        proposal = PartialProposal.model_validate(result.output.model_dump())
        error = None
        try:
            original.prepared.render(proposal)
        except ValueError as failure:
            error = str(failure)
        return OrdinaryProposalResult(
            "ordinary_cohort_proposal",
            original.prepared.input_sha256,
            proposal,
            result.usage,
            error,
        )

    value = await run_validation_stage(
        execution=execution,
        parent_id=original.ids[0],
        source_ids=original.ids,
        request=request,
        policy=policy,
        check_current=current,
        run=run,
    )
    result = TypeAdapter(OrdinaryProposalResult).validate_python(
        {k: v for k, v in value.items() if k != "execution_id"}
    )
    if result.input_sha256 != original.prepared.input_sha256:
        raise SourceUnavailableError()
    if result.validation_error is not None:
        failure = ValueError(result.validation_error)
        failure.__dict__["extraction_usage"] = result.usage.model_dump(mode="json")
        raise failure
    candidate = original.prepared.render(result.proposal)
    if candidate is None:
        return None, identity
    await current()
    row = await execution.load()
    if row is None:
        raise SourceUnavailableError()
    write = ValidationCandidateWrite(identity, row["result_json"], COHORT_GUARD, params)
    replay = await get_raw_memory(organization_id=org, memory_id=write.id)
    embedding = {"embedding_provider": None} if replay is not None else {}
    memory = await remember_reflection_candidate_review(
        **embedding,
        organization_id=org,
        principal_id=principal,
        candidate=candidate,
        raw_source_ids=original.ids,
        source_id=original.ids[0],
        memory_scope=original.sources[0].memory.memory_scope,
        scope_key=original.sources[0].memory.scope_key,
        suggested_memory_scope=candidate.suggested_memory_scope,
        suggested_scope_key=candidate.suggested_scope_key,
        source_observations=[source.observation for source in original.sources],
        validation_write=write,
        accessible_projects=original.authority.projects,
        accessible_teams=original.authority.teams,
        accessible_delegations=original.authority.delegations,
        allowed_memory_scope_keys=original.authority.scope_keys,
    )
    return memory, identity


async def _proposal_extractor(owned, system: str):
    model = (await owned._get_agent()).model
    if not isinstance(model, Model):
        raise ValueError("Cohort proposal needs a resolved model")
    extractor = Extractor(
        PartialProposal,
        agent=Agent(
            model,
            system_prompt=system,
            output_type=NativeOutput(PartialProposal, strict=True)
            if owned.output_mode == "native_strict"
            else PartialProposal,
            retries={"output": 2},
        ),
        surface=owned.surface,
        system_prompt=system,
        max_tokens=owned.max_tokens,
        output_retries=2,
        output_mode=owned.output_mode,
        openrouter_provider=owned.openrouter_provider,
        model_override=owned.model_override,
    )
    return extractor


async def partition_stored_cohort(org, principal, source_ids, resolver):
    """Pack complete authorized episodes against the actual resolved schema budget."""
    from sibyl_core.services.procedure_validation import (
        _close_resources,
        _OwnedValidationExtractor,
        validation_extractor,
    )

    original = await prepare_stored_cohort(org, principal, source_ids, resolver)
    owned, _policy = await validation_extractor()
    try:
        extractor = await _proposal_extractor(owned, original.prepared.system)
        schema_chars = len(canonical(await extractor.output_schema()))
    finally:
        if isinstance(owned, _OwnedValidationExtractor):
            await _close_resources(owned.resources)
    group = PartialCohort.model_validate_json(original.prepared.input_json)
    bins = []

    def fits(episodes):
        if len(episodes) < 2:
            return True
        ids = [e.episode_id for e in episodes]
        partial = PartialCohort.model_validate(
            {**group.model_dump(), "episodes": tuple(episodes), "group_id": review_digest(ids)}
        )
        prepared = prepare_partial_proposal(partial)
        return (
            len(prepared.prompt) + len(prepared.system) + schema_chars
            <= settings.consolidation_max_input_chars
        )

    for episode in group.episodes:
        for bucket in bins:
            if fits([*bucket, episode]):
                bucket.append(episode)
                break
        else:
            bins.append([episode])
    return [[episode.episode_id for episode in bucket] for bucket in bins]
