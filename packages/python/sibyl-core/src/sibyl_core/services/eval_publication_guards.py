"""Original admission bindings checked at review and atomically at publication."""

import asyncio
from collections.abc import Mapping
from typing import Any

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


async def unavailable_publication_ids(
    organization_id: str, rows: Mapping[str, Mapping[str, object] | None]
) -> set[str]:
    """Resolve stable row IDs against the protected ledger before retrieval.

    Marker removal cannot sever a publication's association. Existing source
    bindings also identify projected descendants. A single read statement per
    batch snapshots candidates and their original evidence; decoding and hashing
    run off the event loop.
    """
    if not rows:
        return set()
    unavailable: set[str] = set()
    async with content_client.surreal_content_client() as client:
        for batch in content_client.value_batches(sorted(rows)):
            references = {row_id: _publication_references(row_id, rows[row_id]) for row_id in batch}
            ids = sorted(set().union(*references.values()))
            snapshots = content_client.normalize_records(
                await client.execute_query(
                    """
                RETURN {
                    LET $publications = (SELECT * FROM eval_consolidations
                        WHERE organization_id = $organization_id
                            AND (candidate_id IN $ids OR promoted_entity_id IN $ids));
                    LET $bindings = array::flatten($publications.map(|$row| $row.admission_bindings));
                    LET $capture_ids = array::distinct(array::concat(
                        $publications.map(|$row| $row.candidate_id),
                        $bindings.map(|$binding| $binding.capture_id)));
                    RETURN {
                        publications: $publications,
                        captures: (SELECT * FROM raw_captures
                            WHERE organization_id = $organization_id AND uuid IN $capture_ids),
                        attempts: (SELECT * FROM eval_attempts
                            WHERE organization_id = $organization_id
                                AND experiment_id IN $bindings.map(|$binding| $binding.experiment_id)
                                AND attempt_id IN $bindings.map(|$binding| $binding.attempt_id))
                    };
                };
                """,
                    organization_id=organization_id,
                    ids=ids,
                )
            )
            if len(snapshots) != 1:
                raise RuntimeError("publication retrieval snapshot is unavailable")
            unavailable.update(
                await asyncio.to_thread(
                    _unavailable_snapshot,
                    snapshots[0],
                    references,
                    rows,
                )
            )
    return unavailable


def _publication_references(row_id: str, metadata: Mapping[str, object] | None) -> set[str]:
    references = {row_id}
    metadata = metadata or {}
    for key in ("raw_memory_id", "source_entity_id", "raw_source_id"):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            references.add(value)
    bindings = metadata.get("source_bindings")
    if isinstance(bindings, Mapping):
        references.update(str(key) for key in bindings)
    return references


def _unavailable_snapshot(snapshot: Mapping[str, Any], references, rows) -> set[str]:
    captures = {row["uuid"]: row for row in snapshot["captures"]}
    attempts = {(row["experiment_id"], row["attempt_id"]): row for row in snapshot["attempts"]}
    publications = {
        publication["uuid"]: (
            {publication["candidate_id"], publication.get("promoted_entity_id")},
            _publication_snapshot_recallable(publication, captures, attempts),
        )
        for publication in snapshot["publications"]
    }
    unavailable = set()
    for row_id, ids in references.items():
        associations = {
            key for key, (bound_ids, _allowed) in publications.items() if ids & bound_ids
        }
        marker = publication_operation(rows[row_id])
        if (marker and marker not in associations) or any(
            not publications[key][1] for key in associations
        ):
            unavailable.add(row_id)
    return unavailable


def _publication_snapshot_recallable(publication, captures, attempts) -> bool:
    from sibyl_core.services.content_models import raw_memory_from_record, raw_memory_recallable

    candidate = captures.get(publication.get("candidate_id"))
    bindings = publication.get("admission_bindings") or []
    if candidate is None or len(bindings) < 2 or publication.get("result_kind") != "candidate":
        return False
    memory = raw_memory_from_record(candidate)
    original = {binding["capture_id"]: binding["revision"] for binding in bindings}
    source_ids = memory.metadata.get("raw_source_ids")
    if not isinstance(source_ids, list) or not all(isinstance(value, str) for value in source_ids):
        return False
    if (
        memory.deleted_at is not None
        or not raw_memory_recallable(memory)
        or memory.principal_id != publication["principal_id"]
        or memory.memory_scope != "private"
        or memory.metadata.get(EVAL_CONSOLIDATION_METADATA_KEY) != publication["uuid"]
        or memory.metadata.get("source_bindings") != original
        or set(source_ids) != set(original)
    ):
        return False
    for binding in bindings:
        record = captures.get(binding["capture_id"])
        admission = attempts.get((binding["experiment_id"], binding["attempt_id"]))
        if record is None or admission is None:
            return False
        source = raw_memory_from_record(record)
        if (
            source.deleted_at is not None
            or not raw_memory_recallable(source)
            or source.principal_id != publication["principal_id"]
            or source.memory_scope != "private"
            or _text_digest(source.raw_content) != binding["episode_sha256"]
            or source.metadata.get("eval_admission") != binding["admission_stamp"]
            or admission.get("admitted_at") is None
            or any(
                admission.get(key) != binding[key]
                for key in (
                    "capture_id",
                    "assignment_sha256",
                    "receipt_sha256",
                    "episode_sha256",
                    "outcome_sha256",
                    "transcript_sha256",
                )
            )
            or _text_digest(admission.get("assignment_json"))
            != binding["assignment_artifact_sha256"]
            or _text_digest(admission.get("receipt_base64")) != binding["receipt_artifact_sha256"]
        ):
            return False
    return True


def _text_digest(value: object) -> str | None:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest() if isinstance(value, str) else None


def publication_operation(metadata: Mapping[str, object] | None) -> str | None:
    value = (metadata or {}).get(EVAL_CONSOLIDATION_METADATA_KEY)
    return str(value) if value else None
