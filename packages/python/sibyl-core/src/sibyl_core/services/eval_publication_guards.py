"""Original admission bindings checked at review and atomically at publication."""

from sibyl_core.auth.memory_policy import EVAL_CONSOLIDATION_METADATA_KEY
from sibyl_core.services import content_client
from sibyl_core.services.content_models import RawMemory

# These hash-only observations live in the server-only consolidation ledger.
# Fresh source metadata is never evidence of its own original admission.
PUBLICATION_ADMISSION_GUARD = """
IF $publication_operation_id != NONE {
    LET $publication = (SELECT * FROM eval_consolidations
        WHERE organization_id = $organization_id AND uuid = $publication_operation_id LIMIT 1)[0];
    IF $publication = NONE OR $publication.candidate_id != $uuid
        OR $publication.principal_id != $publication_principal_id
        OR $publication.result_kind != 'candidate'
        OR array::len($publication.admission_bindings ?? []) < 2 {
        THROW 'publication_source_observation_changed';
    };
    LET $candidate = (SELECT * FROM raw_captures
        WHERE organization_id = $organization_id AND uuid = $uuid LIMIT 1)[0];
    LET $original_bindings = object::from_entries(
        $publication.admission_bindings.map(|$binding| [$binding.capture_id, $binding.revision]));
    IF $candidate = NONE OR $candidate.metadata.source_bindings != $original_bindings
        OR array::sort($candidate.metadata.raw_source_ids ?? [])
            != array::sort(object::keys($original_bindings)) {
        THROW 'publication_source_observation_changed';
    };
    FOR $binding IN $publication.admission_bindings {
        LET $admission = (SELECT * FROM eval_attempts
            WHERE organization_id = $organization_id
                AND experiment_id = $binding.experiment_id
                AND attempt_id = $binding.attempt_id LIMIT 1)[0];
        LET $source = (SELECT * FROM raw_captures
            WHERE organization_id = $organization_id AND uuid = $binding.capture_id LIMIT 1)[0];
        IF $source = NONE OR $source.deleted_at != NONE
            OR $source.principal_id != $publication_principal_id
            OR $source.memory_scope != 'private'
            OR crypto::sha256($source.raw_content) != $binding.episode_sha256
            OR $source.metadata.eval_admission != $binding.admission_stamp
            OR $admission = NONE OR $admission.admitted_at = NONE
            OR $admission.capture_id != $binding.capture_id
            OR $admission.assignment_sha256 != $binding.assignment_sha256
            OR $admission.receipt_sha256 != $binding.receipt_sha256
            OR $admission.episode_sha256 != $binding.episode_sha256
            OR $admission.outcome_sha256 != $binding.outcome_sha256
            OR $admission.transcript_sha256 != $binding.transcript_sha256
            OR crypto::sha256($admission.assignment_json) != $binding.assignment_artifact_sha256
            OR crypto::sha256($admission.receipt_base64) != $binding.receipt_artifact_sha256 {
            THROW 'publication_source_observation_changed';
        };
    };
};
"""


async def verify_publication_admissions(memory: RawMemory) -> bool:
    """Fail closed if the original admitted sources or artifacts were replaced."""
    operation_id = memory.metadata.get(EVAL_CONSOLIDATION_METADATA_KEY)
    if operation_id is None:
        return True
    async with content_client.surreal_content_client() as client:
        try:
            await client.execute_query(
                "RETURN {" + PUBLICATION_ADMISSION_GUARD + "RETURN true; };",
                publication_operation_id=operation_id,
                publication_principal_id=memory.principal_id,
                organization_id=memory.organization_id,
                uuid=memory.id,
            )
        except Exception as exc:
            if "publication_source_observation_changed" in str(exc):
                return False
            raise
    return True
