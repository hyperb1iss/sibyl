"""Reconstruct ordinary evidence through the candidate's protected execution lineage."""

import json

from pydantic import TypeAdapter

from sibyl_core.services.source_observations import SourceUnavailableError
from sibyl_core.services.validation_dependencies import dependency_reference
from sibyl_core.services.validation_origin import load_validation_origin
from sibyl_core.tasks.ordinary_packets import QUALIFICATION, OrdinaryEvidencePacket
from sibyl_core.tasks.ordinary_projection import VERSION as COMPLETE_PROJECTION
from sibyl_core.tasks.ordinary_proposal_result import OrdinaryProposalResult
from sibyl_core.tasks.reflection_correction import ReflectionCorrectionResult


async def evidence_for_reflection(memory, derivation, observations, resolver, ancestors):
    """Metadata can describe provenance but cannot select or remove the evidence boundary."""
    origin = await load_validation_origin(derivation)
    if origin is None:
        receipt = memory.metadata.get("ordinary_proposal_receipt")
        if isinstance(receipt, dict) and any(
            receipt.get(key) is not None for key in ("evidence_packet", "evidence_projection")
        ):
            raise SourceUnavailableError()
        return None, ()
    request = json.loads(origin["request_json"])
    value = json.loads(origin["result_json"])
    if value.get("status") == "ordinary_cohort_proposal":
        binding = request.get("evidence_packet")
        projection_binding = request.get("evidence_projection")
        if binding is not None and projection_binding is not None:
            raise SourceUnavailableError()
        if binding is None and projection_binding is None:
            return None, ()
        from sibyl_core.services.ordinary_cohort import prepare_stored_cohort
        from sibyl_core.tasks.ordinary_packets import reconstruct_ordinary_packet

        prepared = await prepare_stored_cohort(
            memory.organization_id,
            memory.principal_id,
            [observation.source.id for observation in observations],
            resolver,
            packet_binding=binding,
            evidence_mode=COMPLETE_PROJECTION if projection_binding is not None else "raw_v1",
            projection_binding=projection_binding,
        )
        result = TypeAdapter(OrdinaryProposalResult).validate_python(value)
        candidate = prepared.prepared.render(result.proposal)
        if (
            result.validation_error is not None
            or result.input_sha256 != prepared.prepared.input_sha256
            or request.get("input") != prepared.prepared.input_sha256
            or request.get("snapshot") != prepared.snapshot_sha256
            or request.get("source_bindings") != prepared.bindings
            or candidate is None
            or candidate.content != memory.raw_content
            or candidate.title != memory.title
            or candidate.kind != memory.entity_type
        ):
            raise SourceUnavailableError()
        if projection_binding is not None:
            from sibyl_core.tasks.ordinary_proposals import PartialCohort, _projection_for_cohort

            evidence = _projection_for_cohort(
                PartialCohort.model_validate_json(prepared.prepared.input_json),
                prepared.prepared.projection_json,
            )
        else:
            source = prepared.sources[0].memory
            evidence = reconstruct_ordinary_packet(source.id, source.raw_content.encode(), binding)
        return evidence, (dependency_reference(origin).model_dump(mode="json"),)
    if value.get("version") == "ordinary-reflection-correction-v1":
        from sibyl_core.services.reflection_validation import prepare_stored_reflection

        result = TypeAdapter(ReflectionCorrectionResult).validate_python(value)
        if result.status != "corrected" or result.content != memory.raw_content:
            raise SourceUnavailableError()
        parent = await prepare_stored_reflection(
            memory.organization_id,
            memory.principal_id,
            origin["parent_id"],
            resolver,
            _ancestors=ancestors,
        )
        if result.parent_candidate_sha256 != parent.snapshot_sha256 or (
            observations != parent.observations
        ):
            raise SourceUnavailableError()
        if parent.evidence is not None:
            from sibyl_core.tasks.ordinary_proposals import QUALIFICATION as COMPLETE_QUALIFICATION

            qualification = (
                QUALIFICATION
                if isinstance(parent.evidence, OrdinaryEvidencePacket)
                else COMPLETE_QUALIFICATION
            )
            if qualification not in memory.raw_content:
                raise SourceUnavailableError()
        if parent.evidence is None:
            return None, ()
        return parent.evidence, (
            dependency_reference(origin).model_dump(mode="json"),
            *parent.origin_dependencies,
        )
    raise SourceUnavailableError()
