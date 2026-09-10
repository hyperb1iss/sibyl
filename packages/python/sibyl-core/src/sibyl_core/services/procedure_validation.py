"""Execute source-authorized procedure critique with durable cost history."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

from pydantic_ai import Agent, NativeOutput

from sibyl_core.ai.llm.config import LLMSurface, resolve_llm_config
from sibyl_core.ai.llm.extractor import Extractor
from sibyl_core.ai.providers import build_model
from sibyl_core.ai.transport import transport_policy
from sibyl_core.backends.surreal.schema_source_witness import SOURCE_STATE_WRITE_WITNESS
from sibyl_core.config import settings
from sibyl_core.services.content_models import (
    raw_memory_currently_recallable,
    raw_memory_from_record,
)
from sibyl_core.services.eval_publication_guards import verify_publication_admissions
from sibyl_core.services.memory_policy import _authorize_share_source_read
from sibyl_core.services.procedure_artifact import ProcedureArtifact, resolve_procedure_artifact
from sibyl_core.services.validation_execution import (
    ValidationExecution,
    ValidationExecutionUnavailable,
    _query,
)
from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.consolidation import (
    METADATA_KEY,
    ConsolidationInputBudgetExceeded,
    DraftConditionalProcedure,
    procedure_review_citations,
)
from sibyl_core.tasks.memory_validation import (
    CriticOutput,
    OriginalValidationEvidence,
    PreparedMemoryValidation,
    prepare_procedure_validation,
    run_memory_validation,
)
from sibyl_core.tasks.procedure_review import review_digest

# Hash the server's representation before SDK datetime/null normalization. The
# same selection closes the final write race without persisting source bodies.
_SNAPSHOT = """
LET $validation_ledger = (SELECT * FROM eval_consolidations WHERE organization_id = $org
    AND candidate_id = $parent LIMIT 1)[0];
LET $ids = array::distinct(array::concat([$parent], ($validation_ledger.admission_bindings ?? []).map(|$b| $b.capture_id)));
LET $captures = (SELECT * FROM raw_captures WHERE organization_id = $org AND uuid IN $ids ORDER BY uuid);
LET $states = (SELECT * OMIT validation_write_witness FROM source_states WHERE organization_id = $org AND source_kind = 'raw_capture'
    AND source_id IN $ids ORDER BY source_id);
LET $attempts = (SELECT * FROM eval_attempts WHERE organization_id = $org
    AND capture_id IN $ids ORDER BY uuid);
LET $snapshot = {ledger: $validation_ledger, captures: $captures, states: $states, attempts: $attempts};
LET $snapshot_digest = crypto::sha256(type::string($snapshot));
"""


@dataclass(frozen=True)
class AuthorizedProcedureValidation:
    prepared: PreparedMemoryValidation
    snapshot_sha256: str
    source_ids: list[str]
    source_bindings: list[dict[str, Any]]
    artifact: ProcedureArtifact
    assignments: tuple[dict[str, Any], ...]


async def prepare_stored_procedure_validation(
    org: str, principal: str, parent: str
) -> AuthorizedProcedureValidation:
    """Resolve persisted private artifacts; HTTP/job callers authenticate principal."""
    rows = await _query(
        "RETURN {" + _SNAPSHOT + "RETURN {snapshot: $snapshot, token: $snapshot_digest}; };",
        org=org,
        parent=parent,
    )
    if len(rows) != 1:
        raise ValidationExecutionUnavailable("Parent snapshot unavailable")
    data = rows[0]["snapshot"]
    ledger = data.get("ledger") or {}
    captures = data.get("captures") or []
    by_id = {row["uuid"]: row for row in captures}
    parent_row = by_id.get(parent)
    if parent_row is None or ledger.get("principal_id") != principal:
        raise ValidationExecutionUnavailable("Parent is outside private scope")
    memory = raw_memory_from_record(parent_row)
    # Pending procedures are reviewable, not recallable. All other retirement
    # and audience checks remain unchanged after removing this one recall gate.
    view = replace(
        memory,
        metadata={
            k: v
            for k, v in memory.metadata.items()
            if k not in {"eval_consolidation", "source_validation_pending"}
        },
    )
    if (
        memory.principal_id != principal
        or not _authorize_share_source_read(
            memory=memory, principal_id=principal, accessible_projects=()
        ).allowed
        or memory.deleted_at is not None
        or not raw_memory_currently_recallable(view)
    ):
        raise ValidationExecutionUnavailable("Parent is retired or inaccessible")
    if not await verify_publication_admissions(memory):
        raise ValidationExecutionUnavailable("Parent admissions changed")
    artifact = await asyncio.to_thread(resolve_procedure_artifact, memory, ledger, captures)
    if artifact is None:
        raise ValidationExecutionUnavailable("Parent artifact is invalid")
    states = {row["source_id"]: row for row in data["states"]}
    for identifier, row in by_id.items():
        state = states.get(identifier)
        if (
            state is None
            or state.get("deleted") is not False
            or state.get("revision") != row.get("revision")
            or not state.get("incarnation")
        ):
            raise ValidationExecutionUnavailable("Source identity unavailable")
        source = raw_memory_from_record(row)
        if identifier != parent and (
            source.principal_id != principal
            or not _authorize_share_source_read(
                memory=source, principal_id=principal, accessible_projects=()
            ).allowed
            or source.deleted_at is not None
            or not raw_memory_currently_recallable(source)
        ):
            raise ValidationExecutionUnavailable("Source is retired or inaccessible")
    audit = artifact.candidate.metadata[METADATA_KEY]
    procedure = DraftConditionalProcedure.model_validate(audit["procedure"])
    evidence = []
    citations = await asyncio.to_thread(procedure_review_citations, artifact.group)
    for episode in artifact.group.episodes:
        source_id = episode.stored_sources[0].source_id
        evidence.append(
            OriginalValidationEvidence(
                episode.episode_id,
                episode.artifact,
                review_digest(
                    {
                        key: states[source_id][key]
                        for key in (
                            "organization_id",
                            "source_kind",
                            "source_id",
                            "incarnation",
                            "generation",
                            "revision",
                        )
                    }
                ),
                "signed",
            )
        )
    prepared = prepare_procedure_validation(
        procedure,
        parent_operation_id=ledger["uuid"],
        parent_candidate_sha256=review_digest(audit),
        evidence=evidence,
        citations=citations,
    )
    return AuthorizedProcedureValidation(
        prepared,
        rows[0]["token"],
        sorted(by_id),
        [
            {key: states[identifier][key] for key in ("source_id", "incarnation", "generation")}
            for identifier in sorted(by_id)
        ],
        artifact,
        tuple(json.loads(row["assignment_json"]) for row in data["attempts"]),
    )


async def validation_extractor() -> tuple[Extractor[CriticOutput], str]:
    """Freeze one resolved model and whitelist its effective policy, never repr/key."""
    resolved = await resolve_llm_config(LLMSurface.MEMORY)
    config = resolved.to_llm_config()
    if config.provider not in {"openai", "anthropic"}:
        raise ValueError("Durable validation requires an observed SDK transport")
    model = build_model(config)
    mode = settings.consolidation_output_mode
    agent = Agent(
        model,
        output_type=NativeOutput(CriticOutput, strict=True)
        if mode == "native_strict"
        else CriticOutput,
        retries={"output": 2},
    )
    extractor = Extractor(
        CriticOutput,
        agent=agent,
        surface=LLMSurface.MEMORY,
        model_override=model.model_name,
        output_retries=2,
        max_tokens=config.max_tokens,
        output_mode=mode,
        openrouter_provider=settings.consolidation_openrouter_provider,
    )
    schema = await extractor.output_schema()
    policy = canonical(
        {
            "version": "memory-validation-execution-v1",
            "provider": config.provider,
            "model": model.model_name,
            "model_settings": {
                k: v
                for k, v in (model.settings or {}).items()
                if k in {"temperature", "max_tokens", "timeout"}
            },
            "max_tokens": config.max_tokens,
            "output_retries": 2,
            "output_mode": mode,
            "route": settings.consolidation_openrouter_provider,
            "schema": schema,
            "transport": transport_policy(config),
            "max_input_chars": settings.consolidation_max_input_chars,
        }
    )
    return extractor, policy


async def validate_stored_procedure(
    *,
    organization_id: str,
    principal_id: str,
    parent_id: str,
    authorize: Callable[[], Awaitable[None]],
) -> dict[str, Any]:
    """Run actual shared validation; callbacks reauthenticate, never supply evidence."""
    await authorize()
    original = await prepare_stored_procedure_validation(organization_id, principal_id, parent_id)
    extractor, policy = await validation_extractor()
    actual_chars = len(original.prepared.prompt) + len(canonical(await extractor.output_schema()))
    if actual_chars > settings.consolidation_max_input_chars:
        raise ConsolidationInputBudgetExceeded(actual_chars, settings.consolidation_max_input_chars)
    request = {
        "org": organization_id,
        "principal": principal_id,
        "parent": parent_id,
        "input": original.prepared.input_sha256,
        "snapshot": original.snapshot_sha256,
        "source_bindings": original.source_bindings,
        "policy": policy,
    }
    identity = review_digest(request)
    execution = ValidationExecution(
        identity,
        organization_id,
        principal_id,
        authorize=authorize,
        dispatch_guard=_SNAPSHOT
        + "IF $snapshot_digest != $expected { THROW 'Validation sources changed before dispatch'; };"
        + "LET $source_states_to_fence=$states;"
        + SOURCE_STATE_WRITE_WITNESS,
        guard_params={"parent": parent_id, "expected": original.snapshot_sha256},
    )
    from sibyl_core.services.validation_stages import run_validation_stage

    async def current():
        await authorize()
        refreshed = await prepare_stored_procedure_validation(
            organization_id, principal_id, parent_id
        )
        if refreshed.snapshot_sha256 != original.snapshot_sha256:
            raise ValidationExecutionUnavailable("Validation sources changed")

    return await run_validation_stage(
        execution=execution,
        parent_id=parent_id,
        source_ids=original.source_ids,
        request=request,
        policy=policy,
        check_current=current,
        run=lambda: run_memory_validation(original.prepared, extractor),
    )
