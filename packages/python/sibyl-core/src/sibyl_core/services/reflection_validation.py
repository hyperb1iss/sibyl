"""Resolve ordinary stored reflection evidence for automatic semantic review."""

from dataclasses import asdict, dataclass

from sibyl_core.backends.surreal.schema_source_witness import SOURCE_STATE_WRITE_WITNESS
from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind, SourceObservation
from sibyl_core.models.reflection import ReflectionCandidate, claim_records_from_metadata
from sibyl_core.services.content_models import (
    RawMemory,
    raw_memory_currently_recallable,
    raw_memory_from_record,
)
from sibyl_core.services.dream_checkpoints import _IMMUTABLE_CANDIDATE
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
from sibyl_core.tasks.episode_evidence import EvidenceCitation
from sibyl_core.tasks.memory_validation import (
    OriginalValidationEvidence,
    PreparedMemoryValidation,
    prepare_reflection_validation,
)
from sibyl_core.tasks.procedure_review import review_digest

ORDINARY_SNAPSHOT = """
LET $snapshot = {
    captures: (SELECT * FROM raw_captures WHERE organization_id=$org AND uuid IN $source_ids ORDER BY uuid),
    states: (SELECT * OMIT validation_write_witness FROM source_states WHERE organization_id=$org AND source_kind='raw_capture' AND source_id IN $source_ids ORDER BY source_id),
    checkpoint: (SELECT uuid, organization_id, source_id, request_json, candidate_fingerprints[$parent] AS fingerprint FROM dream_source_checkpoints WHERE organization_id=$org AND candidate_fingerprints[$parent] != NONE),
    derivations: (SELECT * FROM memory_derivations WHERE organization_id=$org AND target_kind='raw_capture' AND target_id IN $source_ids ORDER BY target_id)
};
LET $snapshot_digest = crypto::sha256(type::string($snapshot));
"""


@dataclass(frozen=True)
class AuthorizedReflection:
    memory: RawMemory
    candidate: ReflectionCandidate
    prepared: PreparedMemoryValidation
    sources: list[RawMemory]
    authority: SourceReadAuthority
    snapshot_sha256: str
    source_bindings: list[dict[str, object]]
    observations: list[SourceObservation]
    publication_policy_sha256: str

    @property
    def source_ids(self) -> list[str]:
        return sorted([self.memory.id, *(source.id for source in self.sources)])


async def prepare_stored_reflection(
    organization_id: str,
    principal_id: str,
    parent_id: str,
    resolver: SourceAuthorityResolver,
    *,
    publication: bool = False,
) -> AuthorizedReflection:
    """Require protected derivation observations and freshly resolved memberships."""
    authority = await resolver(organization_id, principal_id)
    if authority is None or authority.principal_id != principal_id:
        raise SourceUnavailableError()
    rows = await _query(
        "SELECT * FROM raw_captures WHERE organization_id=$org AND uuid=$parent;",
        org=organization_id,
        parent=parent_id,
    )
    if len(rows) != 1:
        raise SourceUnavailableError()
    memory = raw_memory_from_record(rows[0])
    if (
        memory.principal_id != principal_id
        or memory.deleted_at is not None
        or memory.review_state not in ({"pending", "promoted"} if publication else {"pending"})
        or not raw_memory_currently_recallable(memory)
        or not _authorize_share_source_read(
            memory=memory,
            principal_id=principal_id,
            accessible_projects=authority.projects,
            accessible_teams=authority.teams,
            accessible_delegations=authority.delegations,
            allowed_memory_scope_keys=authority.scope_keys,
        ).allowed
    ):
        raise SourceUnavailableError()
    derivation = await load_raw_derivation(organization_id, parent_id)
    if derivation is not None:
        if not await raw_derivation_current(memory, authority):
            raise SourceUnavailableError()
        values = derivation["observations"]
        if not isinstance(values, list):
            raise SourceUnavailableError()
        observations = [observation_from_record(value) for value in values]
        parent_operation = review_digest(
            {
                "parent": parent_id,
                "observations": derivation["observations"],
                "body": derivation["body_sha256"],
            }
        )
    else:
        checkpoints = await _query(
            "RETURN { LET $memory=(SELECT * FROM raw_captures WHERE organization_id=$org AND uuid=$parent)[0];"
            "LET $stage=(SELECT * FROM dream_source_checkpoints WHERE organization_id=$org AND candidate_fingerprints[$parent] != NONE);"
            "RETURN {stages:$stage, fingerprint:crypto::sha256(type::string("
            + _IMMUTABLE_CANDIDATE
            + "))}; };",
            org=organization_id,
            parent=parent_id,
        )
        if len(checkpoints) != 1 or len(checkpoints[0]["stages"]) != 1:
            raise SourceUnavailableError()
        stage = checkpoints[0]["stages"][0]
        if stage["candidate_fingerprints"][parent_id]["stored"] != checkpoints[0]["fingerprint"]:
            raise SourceUnavailableError()
        import json

        request = json.loads(stage["request_json"])
        source = await load_authorized_source_snapshot(
            SourceIdentity(organization_id, SourceKind.RAW_CAPTURE, stage["source_id"]),
            authority,
            organization_id=organization_id,
        )
        observation = source.observation
        if (
            observation.generation != request["generation"]
            or observation.effective_incarnation != request["incarnation"]
            or observation.content_sha256 != request["evidence"]
        ):
            raise SourceUnavailableError()
        observations = [observation]
        parent_operation = stage["uuid"]
    sources = []
    evidence = []
    citations = {}
    for index, observation in enumerate(observations):
        # Dream extraction currently consumes raw captures. Graph-derived input
        # requires its own cross-database publication fence before this adapter.
        if observation.source.kind is not SourceKind.RAW_CAPTURE:
            raise SourceUnavailableError()
        source = await load_authorized_source_snapshot(
            observation.source, authority, organization_id=organization_id
        )
        if not isinstance(source, RawSourceSnapshot) or not source.observation.same_evidence(
            observation
        ):
            raise SourceUnavailableError()
        sources.append(source.memory)
        content = source.memory.raw_content.encode("utf-8")
        evidence.append(
            OriginalValidationEvidence(
                source.memory.id, content, review_digest(asdict(observation)), "reported"
            )
        )
        citations[f"s{index}"] = EvidenceCitation(
            episode_id=source.memory.id, ranges=((0, len(content)),)
        )
    confidence = memory.metadata.get("confidence", 0)
    if not isinstance(confidence, int | float):
        raise SourceUnavailableError()
    candidate = ReflectionCandidate(
        kind=memory.entity_type,
        title=memory.title,
        content=memory.raw_content,
        reason=str(memory.metadata.get("reflection_reason", "stored reflection")),
        confidence=float(confidence),
        tags=list(memory.tags),
        raw_source_ids=[source.id for source in sources],
        claim_records=claim_records_from_metadata(memory.metadata),
    )
    source_ids = sorted([parent_id, *(source.id for source in sources)])
    snapshot = await _query(
        "RETURN {" + ORDINARY_SNAPSHOT + "RETURN {token:$snapshot_digest, data:$snapshot}; };",
        org=organization_id,
        source_ids=source_ids,
        parent=parent_id,
    )
    if len(snapshot) != 1:
        raise SourceUnavailableError()
    states = snapshot[0]["data"]["states"]
    if {state["source_id"] for state in states} != set(source_ids):
        raise SourceUnavailableError()
    # The complete server snapshot is the immutable parent artifact identity;
    # the shared core independently hashes its model-facing claim view.
    digest = snapshot[0]["token"]
    prepared = prepare_reflection_validation(
        candidate,
        parent_operation_id=parent_operation,
        parent_candidate_sha256=digest,
        evidence=evidence,
        citations=citations,
    )
    from sibyl_core.services.ordinary_publication import ordinary_policy_digest

    publication_policy = ordinary_policy_digest(
        [raw_memory_from_record(row) for row in snapshot[0]["data"]["captures"]]
    )
    return AuthorizedReflection(
        memory,
        candidate,
        prepared,
        sources,
        authority,
        digest,
        [
            {key: state[key] for key in ("source_id", "incarnation", "generation")}
            for state in states
        ],
        observations,
        publication_policy,
    )


async def validate_reflection_stage(
    original: AuthorizedReflection, resolver: SourceAuthorityResolver, review=None
) -> dict[str, object]:
    """Run the shared semantic critic as a replayable, source-fenced stage."""
    from sibyl_core.services.procedure_validation import (
        _close_resources,
        _OwnedValidationExtractor,
        validation_extractor,
    )

    extractor, policy = await validation_extractor()
    try:
        return await _validate_prepared_reflection(original, resolver, extractor, policy, review)
    finally:
        if isinstance(extractor, _OwnedValidationExtractor):
            await _close_resources(extractor.resources)


async def _validate_prepared_reflection(
    original: AuthorizedReflection,
    resolver: SourceAuthorityResolver,
    extractor,
    policy: str,
    review=None,
) -> dict[str, object]:
    from sibyl_core.config import settings
    from sibyl_core.services.validation_execution import ValidationExecution
    from sibyl_core.services.validation_stages import run_validation_stage
    from sibyl_core.tasks._evidence_json import canonical
    from sibyl_core.tasks.consolidation import ConsolidationInputBudgetExceeded
    from sibyl_core.tasks.memory_validation import run_memory_validation

    memory = original.memory
    prompt = original.prepared.prompt
    schema = await extractor.output_schema()
    if review is not None:
        import json

        from pydantic_ai import Agent, NativeOutput
        from pydantic_ai.models import Model

        from sibyl_core.ai.llm.extractor import Extractor
        from sibyl_core.tasks.reflection_correction import (
            ReflectionCorrection,
            prepare_reflection_correction,
            reconsider_reflection,
        )

        model = (await extractor._get_agent()).model
        if not isinstance(model, Model):
            raise ValueError("Correction requires a resolved model")
        correction = Extractor(
            ReflectionCorrection,
            agent=Agent(
                model,
                output_type=NativeOutput(ReflectionCorrection, strict=True)
                if extractor.output_mode == "native_strict"
                else ReflectionCorrection,
                retries={"output": 2},
            ),
            max_tokens=extractor.max_tokens,
            output_retries=2,
            output_mode=extractor.output_mode,
            openrouter_provider=extractor.openrouter_provider,
            model_override=extractor.model_override,
        )
        prompt = prepare_reflection_correction(original.prepared, review)
        schema = await correction.output_schema()
        policy = canonical(
            {**json.loads(policy), "purpose": "ordinary_correction", "schema": schema}
        )

        async def run():
            return await reconsider_reflection(original.prepared, review, correction)
    else:

        async def run():
            return await run_memory_validation(original.prepared, extractor)

    chars = len(prompt) + len(canonical(schema))
    if chars > settings.consolidation_max_input_chars:
        raise ConsolidationInputBudgetExceeded(chars, settings.consolidation_max_input_chars)
    from sibyl_core.services.ordinary_publication import ordinary_semantic_digest

    request = {
        "kind": "ordinary_reflection_validation-v2",
        "ordinary_semantic_input": ordinary_semantic_digest(original.prepared),
        "ordinary_publication_policy": original.publication_policy_sha256,
        "org": memory.organization_id,
        "principal": memory.principal_id,
        "parent": memory.id,
        "input": original.prepared.input_sha256 if review is None else review_digest(prompt),
        "prompt_sha256": review_digest(prompt),
        "snapshot": original.snapshot_sha256,
        "source_bindings": original.source_bindings,
        "policy": policy,
    }

    async def current():
        refreshed = await prepare_stored_reflection(
            memory.organization_id, memory.principal_id, memory.id, resolver
        )
        if (
            refreshed.snapshot_sha256 != original.snapshot_sha256
            or refreshed.prepared.input_sha256 != original.prepared.input_sha256
        ):
            raise SourceUnavailableError()

    execution = ValidationExecution(
        review_digest(request),
        memory.organization_id,
        memory.principal_id,
        authorize=current,
        dispatch_guard=ORDINARY_SNAPSHOT
        + "IF $snapshot_digest != $expected { THROW 'Ordinary validation source changed'; };"
        + "LET $source_states_to_fence=$snapshot.states;"
        + SOURCE_STATE_WRITE_WITNESS,
        guard_params={
            "parent": memory.id,
            "source_ids": original.source_ids,
            "expected": original.snapshot_sha256,
        },
    )
    return await run_validation_stage(
        execution=execution,
        parent_id=memory.id,
        source_ids=original.source_ids,
        request=request,
        policy=policy,
        check_current=current,
        run=run,
    )
